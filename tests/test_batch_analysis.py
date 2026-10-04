"""Completed synthetic batch admission is durable, bounded, and unreviewed."""

import copy
import inspect
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from episteme.analysis_controller import advance_batch_analysis, analysis_state
from episteme.batch_analysis import _index as analysis_index
from episteme.batch import _index as batch_index
from episteme.batch_controller import advance_batch
from episteme.commands import CommandService
from episteme.domains.synthetic_batch_analysis import SyntheticCausalBatchAnalysisAdapter
from episteme.graph import GraphIntegrityError, NodeKind, ResearchGraph
from episteme.kernel import Actor, GateError, Kernel
from episteme.recovery import backup, restore
from episteme.reporting import artifact_inventory, review_bundle
from episteme.review_assignment import ReviewAssignment, _index as assignment_index
from episteme.store import IntegrityError, Store

import test_proposal_execution as proposal_fixtures


class BatchAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.fixture = proposal_fixtures.ProposalExecutionTests(
            methodName="test_selected_model_node_and_frozen_sources_share_one_receipt")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.root = self.fixture.root
        self.analyst = Actor("fixture-analyst", "analyst")
        self.reviewer = "fixture-reviewer"
        self.adapter = SyntheticCausalBatchAnalysisAdapter()

    def _complete(self):
        batch = self.fixture.command("proposal.prepare_next",
                                     self.fixture.prepare()["request"]["payload"])
        state = advance_batch(self.store, batch)
        self.assertEqual(state["status"], "completed")
        return batch

    def _advance(self, batch, *, reviewer=None):
        return advance_batch_analysis(self.store, batch, analyst=self.analyst,
            planner=self.fixture.planner, reviewer_actor=reviewer or self.reviewer,
            adapter=self.adapter)

    def test_full_batch_to_frozen_claim_assignment_and_restart(self):
        batch = self._complete()
        self.assertEqual(analysis_state(self.store, batch)["status"], "awaiting_analysis")
        result = self._advance(batch)
        self.assertEqual(result["status"], "awaiting_review")
        self.assertEqual(result["scientific_validity"], "not_assessed")
        self.assertEqual(len(analysis_index(self.store, self.store.events())), 1)
        self.assertEqual(len(assignment_index(self.store, self.store.events())), 1)
        analysis = Kernel._get(self.store.events(), result["analysis"], "batch_analysis")
        proposal = json.loads(self.store.read(analysis["payload"]["proposal_digest"]))
        self.assertEqual(proposal["outcome"], "inconclusive")
        self.assertEqual(len(analysis["payload"]["runs"]), 4)
        self.assertEqual(Kernel(self.store, self.analyst).gate(result["claim"])["basis_hash"],
                         result["basis_hash"])
        with patch.object(Kernel, "next_action", side_effect=AssertionError("reread snapshot")):
            self.assertEqual(analysis_state(self.store, batch)["status"], "awaiting_review")
        graph = ResearchGraph.from_store(self.store)
        self.assertEqual(graph.node(result["analysis"]).kind, NodeKind.BATCH_ANALYSIS)
        inventory = {row["sha256"] for row in artifact_inventory(self.store, self.store.events())}
        self.assertIn(analysis["payload"]["proposal_digest"], inventory)
        self.assertIn(analysis["payload"]["adapter_source_digest"], inventory)
        self.assertEqual(len(review_bundle(self.store, self.store.events())["batch_analyses"]), 1)
        recorded = self.store.export(), self.store.export_receipts()
        self.assertEqual(self._advance(batch), result)
        self.assertEqual(recorded, (self.store.export(), self.store.export_receipts()))
        class WrongAdapter(SyntheticCausalBatchAnalysisAdapter):
            adapter_id = "different_adapter_v1"
        with self.assertRaises(GateError):
            advance_batch_analysis(self.store, batch, planner=self.fixture.planner,
                analyst=self.analyst, reviewer_actor=self.reviewer, adapter=WrongAdapter())
        with Store(self.root) as reopened:
            self.assertEqual(advance_batch_analysis(reopened, batch, analyst=self.analyst,
                planner=self.fixture.planner, reviewer_actor=self.reviewer, adapter=self.adapter), result)
        self.assertFalse(any(e["kind"] in {"review", "paper"} for e in self.store.events()))

    def test_interrupted_after_claim_receipt_resumes_without_duplicate(self):
        batch = self._complete()
        def interrupted(self, *, claim: str, reviewer_actor: str,
                        expected_basis: str) -> dict[str, str]:
            raise RuntimeError("interrupted")
        with patch.dict("episteme.commands._ACTIONS", {"review.assign":
                        (ReviewAssignment, interrupted, frozenset({"planner"}))}):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self._advance(batch)
        self.assertEqual(len(analysis_index(self.store, self.store.events())), 1)
        self.assertEqual(len(assignment_index(self.store, self.store.events())), 0)
        first = analysis_state(self.store, batch)
        self.assertEqual(first["status"], "awaiting_assignment")
        with patch.object(SyntheticCausalBatchAnalysisAdapter, "propose",
                          side_effect=AssertionError("adapter called twice")):
            result = self._advance(batch)
        self.assertEqual(first["analyses"][0]["claim"], result["claim"])
        self.assertEqual(len(analysis_index(self.store, self.store.events())), 1)
        self.assertEqual(len(assignment_index(self.store, self.store.events())), 1)

    def test_unsettled_and_contributing_reviewer_fail_without_claim(self):
        batch = self.fixture.command("proposal.prepare_next",
                                     self.fixture.prepare()["request"]["payload"])
        with self.assertRaises(GateError):
            self._advance(batch)
        self.assertFalse(any(e["kind"] == "claim" for e in self.store.events()))
        advance_batch(self.store, batch)
        before = self.store.export(), self.store.export_receipts()
        with self.assertRaises(GateError):
            self._advance(batch, reviewer="fixture-executor")
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))

    def test_analysis_proposal_and_source_are_gate_bound(self):
        batch = self._complete()
        result = self._advance(batch)
        analysis = Kernel._get(self.store.events(), result["analysis"], "batch_analysis")
        source = self.store.blobs / analysis["payload"]["adapter_source_digest"]
        original = source.read_bytes()
        source.write_bytes(original + b"changed")
        try:
            gate = Kernel(self.store, self.analyst).gate(result["claim"])
            self.assertFalse(gate["passed"])
            with self.assertRaises(IntegrityError):
                analysis_index(self.store, self.store.events())
        finally:
            source.write_bytes(original)
        self.assertTrue(Kernel(self.store, self.analyst).gate(result["claim"])["passed"])

    def test_direct_command_study_settlement_schema_alias_and_replay(self):
        batch = self._complete()
        state = batch_index(self.store, self.store.events())[batch]
        proposal = self.adapter.propose(self.store, state)
        source = self.store.put(Path(inspect.getfile(type(self.adapter))).read_bytes())
        envelope = dict(context=dict(command_id=f"analysis-fixture-{uuid4().hex}",
            expected_revision=len(self.store.events()), actor=self.analyst.id,
            role="analyst", study_id=self.fixture.study,
            correlation_id="analysis-direct-fixture", causation_id=state["settlement"]["id"]),
            request=dict(version=1, action="analysis.apply", payload=dict(
                batch=batch, expected_settlement=state["settlement"]["id"],
                proposal=proposal, adapter_source_digest=source,
                reviewer_actor=self.reviewer)))
        original = self.store.export(), self.store.export_receipts()
        cases = []
        wrong_study = copy.deepcopy(envelope)
        wrong_study["context"].update(command_id="analysis-wrong-study", study_id="foreign-study")
        cases.append(wrong_study)
        wrong_settlement = copy.deepcopy(envelope)
        wrong_settlement["context"]["command_id"] = "analysis-wrong-settlement"
        wrong_settlement["request"]["payload"]["expected_settlement"] = "batch_settlement-missing"
        cases.append(wrong_settlement)
        malformed = copy.deepcopy(envelope)
        malformed["context"]["command_id"] = "analysis-malformed"
        malformed["request"]["payload"]["proposal"]["details"] = []
        cases.append(malformed)
        aliased = copy.deepcopy(envelope)
        aliased["context"]["command_id"] = "analysis-aliased-source"
        first_result = state["settlement"]["payload"]["results"][0]
        aliased["request"]["payload"]["adapter_source_digest"] = Kernel._get(
            self.store.events(), first_result, "result")["payload"]["outputs"]["raw_data"]
        cases.append(aliased)
        for case in cases:
            with self.subTest(case=case["context"]["command_id"]):
                with self.assertRaises(ValueError):
                    CommandService(self.store).execute(case)
                self.assertEqual(original, (self.store.export(), self.store.export_receipts()))
        result = CommandService(self.store).execute(envelope)
        after = self.store.export(), self.store.export_receipts()
        self.assertEqual(CommandService(self.store).execute(envelope), result)
        self.assertEqual(after, (self.store.export(), self.store.export_receipts()))
        self.assertEqual(len(analysis_index(self.store, self.store.events())), 1)
        self.assertEqual(len(assignment_index(self.store, self.store.events())), 0)
        duplicate = copy.deepcopy(envelope)
        duplicate["context"].update(command_id="analysis-duplicate-task",
                                     expected_revision=len(self.store.events()))
        with self.assertRaises(GateError):
            CommandService(self.store).execute(duplicate)
        self.assertEqual(after, (self.store.export(), self.store.export_receipts()))

    def test_orphan_analysis_is_rejected_by_replay_and_graph(self):
        batch = self._complete()
        result = self._advance(batch)
        analysis = Kernel._get(self.store.events(), result["analysis"], "batch_analysis")
        self.store.append(id="batch_analysis-orphan", kind="batch_analysis",
            actor=self.analyst.id, role="analyst", payload=analysis["payload"],
            expected_revision=len(self.store.events()))
        with self.assertRaises(GateError):
            analysis_index(self.store, self.store.events())
        with self.assertRaises(GraphIntegrityError):
            ResearchGraph.from_store(self.store)

    def test_cli_status_and_verified_backup(self):
        batch = self._complete()
        env = {"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        before = subprocess.run([sys.executable, "-m", "episteme", "analysis", "status", batch,
                                 "--root", str(self.root)], capture_output=True, text=True,
                                env={**__import__("os").environ, **env}, check=True)
        self.assertEqual(json.loads(before.stdout)["status"], "awaiting_analysis")
        advance = subprocess.run([sys.executable, "-m", "episteme", "analysis", "advance", batch,
                                  "--root", str(self.root), "--analyst", self.analyst.id,
                                  "--planner", self.fixture.planner.id,
                                  "--reviewer", self.reviewer], capture_output=True, text=True,
                                 env={**__import__("os").environ, **env}, check=True)
        self.assertEqual(json.loads(advance.stdout)["status"], "awaiting_review")
        snapshot = self.root.parent / "snapshot"
        backup(self.store, snapshot)
        recovered = self.root.parent / "recovered"
        restore(snapshot, recovered)
        with Store(recovered, read_only=True) as store:
            self.assertEqual(analysis_state(store, batch)["status"], "awaiting_review")

    def _apply(self, batch, proposal, source_digest):
        state = batch_index(self.store, self.store.events())[batch]
        return CommandService(self.store).execute(dict(
            context=dict(command_id=f"direct-{uuid4().hex}", expected_revision=len(self.store.events()),
                         actor=self.analyst.id, role="analyst", study_id=self.fixture.study,
                         correlation_id="adr0018-a04", causation_id=None),
            request=dict(version=1, action="analysis.apply", payload=dict(
                batch=batch, expected_settlement=state["settlement"]["id"], proposal=proposal,
                adapter_source_digest=source_digest, reviewer_actor=self.reviewer))))

    def test_analysis_apply_rejects_a_proposal_the_adapter_did_not_compute(self):
        # Audit finding A-04: an analyst's own proposal under the adapter's ID and digest.
        batch = self._complete()
        state = batch_index(self.store, self.store.events())[batch]
        honest = self.adapter.propose(self.store, state)
        self.assertEqual((honest["outcome"], honest["inference_mode"]), ("inconclusive", "exploratory"))
        source = self.store.put(Path(inspect.getfile(type(self.adapter))).read_bytes())
        forged = dict(honest, outcome="supports",
                      statement="The treatment causes the outcome in this population.")
        drifted = self.store.put(b"# a different adapter source\n")
        before = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "registered adapter computes"):
            self._apply(batch, forged, source)
        self.assertEqual((self.store.export(), self.store.export_receipts()), before)
        with self.assertRaisesRegex(ValueError, "adapter source differs"):
            self._apply(batch, honest, drifted)
        self.assertEqual((self.store.export(), self.store.export_receipts()), before)
        admitted = self._apply(batch, honest, source)
        history = self.store.events()
        analysis = Kernel._get(history, admitted["analysis"], "batch_analysis")
        self.assertEqual(analysis["payload"]["schema_version"], 2)
        self.assertEqual(analysis["payload"]["proposal_origin"],
                         "recomputed_by_registered_adapter_at_admission")
        self.assertEqual(Kernel._get(history, admitted["claim"], "claim")["payload"]["outcome"],
                         "inconclusive")
        self.assertIn(admitted["analysis"], analysis_index(self.store, history))
        ResearchGraph.from_store(self.store)


if __name__ == "__main__":
    unittest.main()
