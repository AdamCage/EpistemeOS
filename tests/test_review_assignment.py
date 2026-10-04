"""Reviewer assignment freezes a context policy, not independent review access."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from episteme.commands import CommandService
from episteme.graph import GraphIntegrityError, NodeKind, Relation, ResearchGraph
from episteme.kernel import Actor, GateError, Kernel
from episteme.planning import Planning
from episteme.recovery import backup, restore
from episteme.review_assignment import ReviewAssignment, _index
from episteme.store import ConflictError, IntegrityError, Store, _request_hash
from review_paths import FIXTURE_RATIONALE, assign, deliver, reconsider, submit_review


class ReviewAssignmentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="episteme-review-assignment-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "working"
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.service = CommandService(self.store)
        self.scope = {"population": "two synthetic units"}
        planner = Kernel(self.store, Actor("fixture-planner", "planner"))
        planning = Planning(self.store, Actor("fixture-planner", "planner"))
        hypotheses = [planner.hypothesis(name, name, "Opposite outcome", self.scope)
                      for name in ("signal", "null")]
        self.question = planning.question(study_id="fixture-study", statement="Is the fixture mean zero?",
            objective="Test two competing explanations", scope=self.scope,
            constraints=["Synthetic fixture"], stopping_criteria=["One scheduled seed"])
        self.explanations = planning.explanation_set(question=self.question, hypotheses=hypotheses,
            comparison_plan="Compare the registered mean against zero")
        self.code = self.store.put(b"secret original implementation source")
        self.recode = self.store.put(b"secret reanalysis implementation source")
        self.environment = self.store.put(b"secret environment")
        self.raw = self.store.put(b"unit,value\na,-1\nb,1\n")
        self.holdout = self.store.put(b"unobserved holdout bytes")
        self.metrics = self.store.put_json({"mean": 0.0})
        self.log = self.store.put(b"secret execution log")
        design = dict(schema_version=1, mode="exploratory", experimental_unit="unit",
            estimand="Arithmetic mean", primary_metric={"name": "mean", "unit": "value"},
            secondary_metrics=[], sample_size=2, sample_size_rationale="Two fixture units",
            uncertainty={"method": "not_applicable", "resampling_unit": None,
                         "rationale": "Fixture equality only"}, exclusions=[],
            stopping_rule={"kind": "fixed_sample", "rule": "One scheduled seed"},
            multiple_testing={"family": ["mean"], "correction": "not_applicable",
                              "rationale": "No inference"},
            data_splits=[{"id": "unobserved", "digest": self.holdout,
                          "role": "discovery", "exposure_policy": "open"}])
        self.protocol = planner.preregister_for_set(explanation_set=self.explanations,
            design="Synthetic two-explanation test", metric="mean", analysis_plan="Arithmetic mean",
            stopping_rule="One scheduled seed", seeds=[7], run_limit=6,
            implementation=self.code, environment=self.environment, data=self.raw,
            replication_tolerance=0.0, statistical_design=design)
        outputs = dict(raw_data=self.raw, metrics=self.metrics, log=self.log)
        executor = Kernel(self.store, Actor("fixture-executor", "executor"))
        replicator = Kernel(self.store, Actor("fixture-replicator", "replicator"))
        self.primary = executor.start_run(self.protocol, seed=7, implementation=self.code,
                                          environment=self.environment, command=["secret-command"])
        executor.finish_run(self.primary, status="completed", outputs=outputs)
        replica = replicator.start_run(self.protocol, seed=7, implementation=self.recode,
                                       environment=self.environment, command=["secret-reanalysis"],
                                       replicate_of=self.primary)
        replicator.finish_run(replica, status="completed", outputs=outputs)
        self.claim = Kernel(self.store, Actor("fixture-analyst", "analyst")).claim(
            protocol=self.protocol, statement="Observed fixture mean is zero", scope=self.scope,
            evidence=[self.primary, replica], limitations=["Synthetic observations only"],
            outcome="inconclusive")
        self.basis = planner.gate(self.claim)["basis_hash"]
        self.assertTrue(planner.gate(self.claim)["passed"])

    def envelope(self, *, reviewer="fixture-reviewer", basis=None, command_id="assign-reviewer"):
        return dict(context=dict(command_id=command_id, expected_revision=len(self.store.events()),
                    actor="fixture-planner", role="planner", study_id="fixture-study",
                    correlation_id="fixture-cycle", causation_id=None),
                    request=dict(version=1, action="review.assign", payload=dict(
                        claim=self.claim, reviewer_actor=reviewer,
                        expected_basis=self.basis if basis is None else basis)))

    def test_freezes_blind_bundle_and_graph_without_claiming_independence(self):
        prior = Kernel(self.store, Actor("earlier-reviewer", "reviewer")).review(
            self.claim, verdict="approve", rationale="Prior opinion hidden from initial bundle",
            actions=[], expected_basis=self.basis)
        envelope = self.envelope()
        result = self.service.execute(envelope)
        event = self.store.events()[-1]
        self.assertEqual(event["kind"], "review_assignment")
        self.assertEqual(event["payload"]["identity_assurance"], "caller_declared")
        self.assertEqual(event["payload"]["read_isolation"], "not_enforced")
        self.assertEqual(self.store.receipts()[-1]["event_ids"], [event["id"]])
        manifest = json.loads(self.store.read(result["bundle"]))
        self.assertEqual(manifest["allowed_artifact_digests"], sorted([self.raw, self.metrics]))
        self.assertEqual(manifest["context"]["questions"][0]["id"], self.question)
        self.assertEqual(len(manifest["context"]["hypotheses"]), 2)
        self.assertEqual(len(manifest["context"]["observed_runs"]), 2)
        self.assertEqual(manifest["target"]["scientific_validity"], "not_assessed")
        self.assertEqual(manifest["context"]["protocols"][0]["statistical_design"]
                         ["data_splits"][0]["id"], "unobserved")
        encoded = self.store.read(result["bundle"]).decode()
        for hidden in (self.code, self.recode, self.environment, self.log, self.holdout,
                       prior, "Prior opinion hidden", "secret-command"):
            self.assertNotIn(hidden, encoded)
        self.assertNotIn("independent", event["payload"])
        self.assertNotIn("independent", manifest)
        graph = ResearchGraph.from_store(self.store)
        self.assertEqual(graph.node(result["assignment"]).kind, NodeKind.REVIEW_ASSIGNMENT)
        self.assertEqual(graph.node(result["assignment"]).scientific_validity, "not_assessed")
        self.assertTrue(any(edge.source == self.claim and edge.target == result["assignment"]
                            and edge.relation == Relation.REVIEW_ASSIGNMENT_TARGET
                            for edge in graph.edges))
        self.assertEqual(set(_index(self.store, self.store.events())), {result["assignment"]})

    def test_replay_stale_basis_contributor_conflict_and_wrong_role(self):
        for command in (self.envelope(basis="0" * 64, command_id="stale"),
                        self.envelope(reviewer="fixture-executor", command_id="contributor")):
            before = self.store.events()
            with self.assertRaises(GateError):
                self.service.execute(command)
            self.assertEqual(self.store.events(), before)
            self.assertEqual(self.store.receipts(), [])
        wrong_role = self.envelope(command_id="wrong-role")
        wrong_role["context"]["role"] = "reviewer"
        with self.assertRaisesRegex(ValueError, "cannot execute"):
            self.service.execute(wrong_role)
        envelope = self.envelope()
        result = self.service.execute(envelope)
        self.assertEqual(Kernel(self.store, Actor("fixture-reader", "planner"))
                         .next_action(self.claim)["action"], "scientific_review")
        with patch.object(ReviewAssignment, "assign", side_effect=AssertionError("replayed handler")):
            self.assertEqual(self.service.execute(envelope), result)
        changed = copy.deepcopy(envelope)
        changed["request"]["payload"]["reviewer_actor"] = "different-reviewer"
        with self.assertRaises(ConflictError):
            self.service.execute(changed)
        # ADR 0018 §3.6: without a submitted verdict the same reviewer may be assigned again.
        again = self.service.execute(self.envelope(command_id="duplicate"))
        self.assertNotEqual(again["assignment"], result["assignment"])
        self.assertEqual(set(_index(self.store, self.store.events())),
                         {result["assignment"], again["assignment"]})

    def test_later_evidence_change_does_not_rewrite_historical_assignment(self):
        envelope = self.envelope()
        result = self.service.execute(envelope)
        executor = Kernel(self.store, Actor("fixture-executor", "executor"))
        executor.start_run(self.protocol, seed=7, implementation=self.code,
                           environment=self.environment, command=["later attempt"])
        self.assertFalse(executor.gate(self.claim)["passed"])
        self.assertEqual(self.service.execute(envelope), result)
        self.assertEqual(set(_index(self.store, self.store.events())), {result["assignment"]})
        with self.assertRaises(GateError):
            self.service.execute(self.envelope(command_id="after-change"))

    def test_artifact_alias_cannot_expose_implementation_as_raw_data(self):
        planner = Kernel(self.store, Actor("fixture-planner", "planner"))
        protocol = planner.preregister_for_set(explanation_set=self.explanations,
            design="Aliased synthetic source test", metric="mean", analysis_plan="Fixture mean",
            stopping_rule="One scheduled seed", seeds=[7], run_limit=4,
            implementation=self.raw, environment=self.environment, data=self.raw,
            replication_tolerance=0.0)
        outputs = dict(raw_data=self.raw, metrics=self.metrics, log=self.log)
        executor = Kernel(self.store, Actor("fixture-executor", "executor"))
        replicator = Kernel(self.store, Actor("fixture-replicator", "replicator"))
        primary = executor.start_run(protocol, seed=7, implementation=self.raw,
                                     environment=self.environment, command=["fixture"])
        executor.finish_run(primary, status="completed", outputs=outputs)
        repeated = replicator.start_run(protocol, seed=7, implementation=self.recode,
                                        environment=self.environment, command=["fixture"],
                                        replicate_of=primary)
        replicator.finish_run(repeated, status="completed", outputs=outputs)
        claim = Kernel(self.store, Actor("fixture-analyst", "analyst")).claim(
            protocol=protocol, statement="Aliased fixture result", scope=self.scope,
            evidence=[primary, repeated], limitations=["Fixture only"], outcome="inconclusive")
        basis = planner.gate(claim)["basis_hash"]
        self.assertTrue(planner.gate(claim)["passed"])
        envelope = self.envelope(command_id="aliased")
        envelope["request"]["payload"].update(claim=claim, expected_basis=basis)
        with self.assertRaisesRegex(GateError, "also recorded as source"):
            self.service.execute(envelope)

    def test_missing_or_mutated_bundle_and_orphan_event_fail_replay(self):
        result = self.service.execute(self.envelope())
        bundle = self.store.blobs / result["bundle"]
        original = bundle.read_bytes()
        bundle.write_bytes(original + b"tampered")
        with self.assertRaises(IntegrityError):
            _index(self.store, self.store.events())
        bundle.write_bytes(original)
        self.assertEqual(set(_index(self.store, self.store.events())), {result["assignment"]})
        forged = copy.deepcopy(self.store.events()[-1]["payload"])
        self.store.append(id="review_assignment-orphan", kind="review_assignment",
                          actor="fixture-planner", role="planner", payload=forged,
                          expected_revision=len(self.store.events()))
        with self.assertRaisesRegex(GateError, "lacks its original command receipt"):
            _index(self.store, self.store.events())
        with self.assertRaises(GraphIntegrityError):
            ResearchGraph.from_store(self.store)

    def test_valid_structural_receipt_cannot_launder_contributor_assignment(self):
        before = self.store.events()
        legitimate = self.service.execute(self.envelope())
        original = self.store.events()[-1]
        self.assertEqual(original["id"], legitimate["assignment"])
        prefix = self.store.events()
        forged = dict(original["payload"], reviewer_actor="fixture-executor")
        event = self.store.append(id="review_assignment-forged", kind="review_assignment",
                                  actor="fixture-planner", role="planner", payload=forged,
                                  expected_revision=len(prefix))
        context = dict(command_id="forged-assign", expected_revision=len(prefix),
                       actor="fixture-planner", role="planner", study_id="fixture-study",
                       correlation_id="fixture-cycle", causation_id=None)
        request = dict(version=1, action="review.assign", payload=dict(
            claim=self.claim, reviewer_actor="fixture-executor", expected_basis=self.basis))
        receipt = dict(schema_version=1, command_id=context["command_id"], context=context,
                       request=request, request_hash=_request_hash(context, request),
                       before_revision=len(prefix), before_hash=prefix[-1]["hash"],
                       after_revision=len(prefix) + 1, after_hash=event["hash"],
                       event_ids=[event["id"]], event_hashes=[event["hash"]],
                       result=dict(assignment=event["id"], bundle=forged["bundle"]),
                       created_at="2026-09-28T00:00:00+00:00")
        self.store._insert_receipt(receipt)
        self.store.db.commit()
        self.assertEqual(len(self.store.receipts()), 2)
        with self.assertRaisesRegex(GateError, "contributed"):
            _index(self.store, self.store.events())
        self.assertEqual(len(before) + 2, len(self.store.events()))

    def test_graph_rejects_assignment_receipt_without_assignment_event(self):
        before = self.store.events()
        decoy = Kernel(self.store, Actor("fixture-planner", "planner")).hypothesis(
            "decoy", "Unrelated fixture explanation", "No effect", self.scope)
        event = self.store.events()[-1]
        self.assertEqual(event["id"], decoy)
        self.assertEqual(event["kind"], "hypothesis")
        self.assertIsNotNone(ResearchGraph.from_store(self.store))
        context = dict(command_id="forged-assignment-kind", expected_revision=len(before),
                       actor="fixture-planner", role="planner", study_id="fixture-study",
                       correlation_id="fixture-cycle", causation_id=None)
        request = dict(version=1, action="review.assign", payload=dict(
            claim=self.claim, reviewer_actor="fixture-reviewer", expected_basis=self.basis))
        receipt = dict(schema_version=1, command_id=context["command_id"], context=context,
                       request=request, request_hash=_request_hash(context, request),
                       before_revision=len(before), before_hash=before[-1]["hash"],
                       after_revision=len(before) + 1, after_hash=event["hash"],
                       event_ids=[event["id"]], event_hashes=[event["hash"]],
                       result=dict(assignment="review_assignment-nonexistent", bundle="0" * 64),
                       created_at="2026-09-28T00:00:00+00:00")
        self.store._insert_receipt(receipt)
        self.store.db.commit()
        self.assertEqual(len(self.store.receipts()), 1)
        with self.assertRaisesRegex(GraphIntegrityError, "invalid review assignment history"):
            ResearchGraph.from_store(self.store)

    def test_backup_restore_preserves_exact_bundle_receipt_and_graph(self):
        result = self.service.execute(self.envelope())
        graph = ResearchGraph.from_store(self.store).to_json()
        snapshot = self.root.parent / "snapshot"
        restored = self.root.parent / "restored"
        backup(self.store, snapshot)
        restore(snapshot, restored)
        with Store(restored) as reopened:
            self.assertEqual(reopened.events(), self.store.events())
            self.assertEqual(reopened.receipts(), self.store.receipts())
            self.assertEqual(reopened.read(result["bundle"]), self.store.read(result["bundle"]))
            self.assertEqual(ResearchGraph.from_store(reopened).to_json(), graph)
            self.assertEqual(set(_index(reopened, reopened.events())), {result["assignment"]})

    def test_command_cli_replay_and_graph_projection(self):
        envelope = self.envelope(command_id="cli-review-assignment")
        input_path = self.root.parent / "assign.json"
        input_path.write_text(json.dumps(envelope), encoding="utf-8")

        def cli(*args):
            return subprocess.run([sys.executable, "-m", "episteme.cli", *args],
                                  capture_output=True, text=True, check=False)

        argv = ("command", "--input", str(input_path), "--root", str(self.root))
        first = cli(*argv)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = cli(*argv)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout), json.loads(first.stdout))
        assigned = json.loads(first.stdout)["result"]
        self.assertEqual(len([e for e in self.store.events() if e["kind"] == "review_assignment"]), 1)
        self.assertEqual(len(self.store.receipts()), 1)
        self.assertEqual(self.store.events()[-1]["id"], assigned["assignment"])
        graph = cli("graph", "--root", str(self.root))
        self.assertEqual(graph.returncode, 0, graph.stderr)
        self.assertEqual(json.loads(graph.stdout)["node_kinds"]["review_assignment"], 1)
        gate = cli("gate", self.claim, "--root", str(self.root))
        self.assertEqual(gate.returncode, 0, gate.stderr)
        self.assertTrue(json.loads(gate.stdout)["passed"])

    # ADR 0018 §3.6 (audit A-14); every opinion below is an explicit synthetic fixture.
    def test_unsubmitted_negative_response_vetoes(self):
        assigned = assign(self.store, self.claim, reviewer="fixture-reviewer")
        deliver(self.store, assigned["assignment"], dict(
            verdict="request_changes", rationale=FIXTURE_RATIONALE, link_assessments=None,
            findings=[dict(kind="narrow_claim", action="Bound the statement",
                           closure_criterion="A bounded claim is reviewed",
                           evidence_refs=[self.claim])]), study="fixture-study")
        reader = Kernel(self.store, Actor("fixture-reader", "observer"))
        decision = reader.next_action(self.claim)
        self.assertEqual(decision["action"], "replan", decision)
        self.assertIn("Bound the statement", decision["reasons"])
        # The same reviewer can be assigned again, but only to reconsider that opinion.
        submitted = reconsider(self.store, self.claim, reviewer="fixture-reviewer")
        manifest = json.loads(self.store.read(submitted["bundle"]))
        self.assertEqual(manifest["policy"], "veto_reconsideration_v1")
        self.assertEqual([row["kind"] for row in manifest["own_findings"]["opinions"]],
                         ["review_response"])
        self.assertEqual(reader.next_action(self.claim)["action"], "paper_candidate")
        ResearchGraph.from_store(self.store)

    def test_reassignment_after_unsubmitted_response(self):
        first = assign(self.store, self.claim, reviewer="fixture-reviewer")
        deliver(self.store, first["assignment"], dict(
            verdict="approve", rationale=FIXTURE_RATIONALE, findings=[], link_assessments=None),
            study="fixture-study")
        try:
            second = submit_review(self.store, self.claim, reviewer="fixture-reviewer")
        except GateError as exc:
            self.fail(f"reassignment after an unsubmitted response was refused: {exc}")
        self.assertNotEqual(second["assignment"], first["assignment"])
        reader = Kernel(self.store, Actor("fixture-reader", "observer"))
        self.assertEqual(reader.next_action(self.claim)["action"], "paper_candidate")
        before = self.store.events()
        with self.assertRaisesRegex(GateError, "already submitted"):
            assign(self.store, self.claim, reviewer="fixture-reviewer")
        self.assertEqual(self.store.events(), before)
        self.assertEqual(len(_index(self.store, before)), 2)
        ResearchGraph.from_store(self.store)


if __name__ == "__main__":
    unittest.main()
