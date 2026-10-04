"""Claim families bind vetoes and obligations (ADR 0018, audit findings A-01, A-02).

All opinions are explicit synthetic fixtures; nothing here assesses science.
Scenarios use only command APIs that existed at audit commit da6aa2a.
"""

import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from episteme.commands import CommandService
from episteme.graph import ResearchGraph
from episteme.kernel import Actor, GateError, Kernel
from episteme.recovery import backup, restore
from episteme.reporting import PaperBuilder
from episteme.store import Store
from review_paths import (approve, assign, command, current_basis, deliver, reconsider,
                          study_of, submit_review)

from tests import test_resolution


class FamilyFixture:
    """Two competing hypotheses, legacy protocols and claims via CommandService."""

    def __init__(self, test: unittest.TestCase):
        directory = tempfile.TemporaryDirectory(prefix="episteme-families-")
        test.addCleanup(directory.cleanup)
        self.store = Store(directory.name)
        test.addCleanup(self.store.close)
        self.scope = {"population": "synthetic family fixture"}
        self.hypotheses = [self.cmd("planner-1", "planner", "kernel.hypothesis", statement=name,
                                    prediction=name, falsifier="opposite sign", scope=self.scope)
                           for name in ("signal", "null")]
        self.environment = self.store.put(b"fixture environment")
        self.data = self.store.put(b"value\n0\n1\n")

    def cmd(self, actor, role, action, **payload):
        return CommandService(self.store).execute(dict(
            context=dict(command_id=f"{action}-{uuid4().hex}", expected_revision=len(self.store.events()),
                         actor=actor, role=role, study_id="family-study", correlation_id="families",
                         causation_id=None),
            request=dict(version=1, action=action, payload=payload)))

    def protocol(self, *, data=None, label="primary"):
        code = self.store.put(f"{label} code".encode())
        protocol = self.cmd("planner-1", "planner", "kernel.preregister", hypotheses=self.hypotheses,
                            scope=self.scope, design="d", metric="mean", analysis_plan="a",
                            stopping_rule="one seed", seeds=[7], run_limit=4, implementation=code,
                            environment=self.environment, data=self.data if data is None else data,
                            replication_tolerance=0.0)
        outputs = dict(raw_data=self.data if data is None else data,
                       metrics=self.store.put_json({"mean": 0.0}), log=self.store.put(b"fixture log"))
        primary = self.cmd(f"exec-{label}", "executor", "kernel.start_run", protocol=protocol, seed=7,
                           implementation=code, environment=self.environment, command=["x"])
        self.cmd(f"exec-{label}", "executor", "kernel.finish_run", run=primary, status="completed",
                 outputs=outputs)
        replica = self.cmd(f"repl-{label}", "replicator", "kernel.start_run", protocol=protocol, seed=7,
                           implementation=self.store.put(f"{label} reanalysis".encode()),
                           environment=self.environment, command=["x"], replicate_of=primary)
        self.cmd(f"repl-{label}", "replicator", "kernel.finish_run", run=replica, status="completed",
                 outputs=outputs)
        return protocol, [primary, replica]

    def claim(self, protocol, runs, *, statement="Effect X exists", outcome="supports"):
        return self.cmd("analyst-1", "analyst", "kernel.claim", protocol=protocol, statement=statement,
                        scope=self.scope, evidence=runs, limitations=["synthetic fixture"],
                        outcome=outcome)

    def next_action(self, claim):
        return Kernel(self.store, Actor("family-observer", "observer")).next_action(claim)

    def paper(self, claim):
        return PaperBuilder(self.store, Actor("writer-1", "writer")).build(
            title="Fixture draft", claims=[claim], expected_bases={claim: current_basis(self.store, claim)})


class ClaimFamilyTests(unittest.TestCase):
    def test_resubmitted_claim_inherits_family_veto_and_obligations(self):
        for typed in (True, False):
            with self.subTest(typed=typed):
                lab = FamilyFixture(self)
                protocol, runs = lab.protocol()
                claim_a = lab.claim(protocol, runs)
                if typed:
                    rejected = lab.cmd("reviewer-r1", "reviewer", "replanning.record_review", claim=claim_a,
                        verdict="reject", rationale="Confounded design", findings=[dict(
                            kind="discriminating_experiment", action="Rule out confounding",
                            closure_criterion="A discriminating experiment is reviewed",
                            evidence_refs=[claim_a, runs[0]])],
                        expected_basis=current_basis(lab.store, claim_a), link_assessments=None)
                    review, obligations = rejected["review"], rejected["obligations"]
                else:
                    review = lab.cmd("reviewer-r1", "reviewer", "kernel.review", claim=claim_a,
                                     verdict="reject", rationale="Confounded design",
                                     actions=["Rule out confounding"],
                                     expected_basis=current_basis(lab.store, claim_a))
                    obligations = []
                self.assertEqual(lab.next_action(claim_a)["action"], "replan")
                # Same protocol, evidence and text; a second reviewer approves via the blind path.
                claim_b = lab.claim(protocol, runs)
                approved = submit_review(lab.store, claim_b, reviewer="reviewer-r2")
                manifest = lab.store.read(approved["bundle"]).decode()
                self.assertNotIn(claim_a, manifest)
                decision = lab.next_action(claim_b)
                self.assertEqual(decision["action"], "replan", decision)
                self.assertIn("Rule out confounding", decision["reasons"])
                if typed:
                    self.assertEqual(decision["obligations"], obligations)
                else:
                    self.assertEqual(decision["family_vetoes"],
                                     [dict(review=review, claim=claim_a, reviewer="reviewer-r1")])
                before = lab.store.events()
                with self.assertRaisesRegex(ValueError, "not eligible for paper"):
                    lab.paper(claim_b)
                self.assertEqual(lab.store.events(), before)
                self.assertEqual(lab.next_action(claim_a)["action"], "replan")

    def test_reregistered_protocol_on_same_bytes_joins_the_family(self):
        lab = FamilyFixture(self)
        first, first_runs = lab.protocol(label="first")
        claim_a = lab.claim(first, first_runs)
        review = lab.cmd("reviewer-r1", "reviewer", "kernel.review", claim=claim_a, verdict="request_changes",
                         rationale="Needs a control", actions=["Add a control"],
                         expected_basis=current_basis(lab.store, claim_a))
        # Re-registration: a new root protocol on the same observed bytes.
        second, second_runs = lab.protocol(label="second")
        claim_b = lab.claim(second, second_runs)
        approve(lab.store, claim_b, reviewer="reviewer-r2")
        decision = lab.next_action(claim_b)
        self.assertEqual(decision["action"], "replan", decision)
        self.assertEqual(decision["family_vetoes"],
                         [dict(review=review, claim=claim_a, reviewer="reviewer-r1")])
        # Different bytes and different code form an unrelated family.
        other, other_runs = lab.protocol(data=lab.store.put(b"value\n5\n6\n"), label="other")
        claim_c = lab.claim(other, other_runs)
        approve(lab.store, claim_c, reviewer="reviewer-r2")
        self.assertEqual(lab.next_action(claim_c)["action"], "paper_candidate")


class ResolutionScopeTests(unittest.TestCase):
    components = staticmethod(test_resolution.SoleObligationResolutionTests.components)
    _finish = test_resolution.SoleObligationResolutionTests._finish
    envelope = test_resolution.SoleObligationResolutionTests.envelope
    command = test_resolution.SoleObligationResolutionTests.command
    prepare = test_resolution.SoleObligationResolutionTests.prepare
    complete_child = test_resolution.SoleObligationResolutionTests.complete_child
    resolve = test_resolution.SoleObligationResolutionTests.resolve

    def setUp(self):
        test_resolution.SoleObligationResolutionTests.setUp(self)

    def test_resolution_applies_only_to_the_claim_it_evaluated(self):
        child, basis, results = self.complete_child()
        self.resolve(child, basis, results)                # the original reviewer accepts narrow C1
        narrow = Kernel._get(self.store.events(), child, "claim")["payload"]
        broad = Kernel(self.store, self.analyst).claim(protocol=narrow["protocol"],
            statement="The broad mechanism is confirmed", scope=self.scope,
            evidence=narrow["evidence"], limitations=["none of note"], outcome="supports")
        approve(self.store, broad, reviewer="friendly-reviewer")
        reader = Kernel(self.store, Actor("family-observer", "observer"))
        self.assertEqual(reader.next_action(self.claim)["action"], "replan")
        decision = reader.next_action(broad)
        self.assertEqual(decision["action"], "replan", decision)
        self.assertEqual(decision["obligations"], [self.obligation])
        before = self.store.events()
        with self.assertRaisesRegex(ValueError, "not eligible for paper"):
            PaperBuilder(self.store, Actor("fixture-writer", "writer")).build(
                title="Broad", claims=[broad], expected_bases={broad: reader.gate(broad)["basis_hash"]})
        self.assertEqual(self.store.events(), before)
        self.assertEqual(reader.next_action(child)["action"], "paper_candidate")

    def test_reconsideration_resolves_a_discriminating_finding_for_its_claim(self):
        child, basis, results = self.complete_child()
        submitted = reconsider(self.store, child, reviewer=self.reviewer.id, expected_basis=basis,
            resolutions=[dict(obligation=self.obligation, evidence_refs=[child, *results],
                              rationale="The new control addresses the recorded confound")])
        reader = Kernel(self.store, Actor("family-observer", "observer"))
        self.assertEqual(reader.next_action(child)["action"], "paper_candidate")
        self.assertEqual(reader.next_action(self.claim)["action"], "replan")
        paper = PaperBuilder(self.store, Actor("fixture-writer", "writer")).build(
            title="Bounded fixture", claims=[child], expected_bases={child: basis})
        bundle = json.loads(self.store.read(
            Kernel._get(self.store.events(), paper, "paper")["payload"]["bundle"]))
        step = bundle["selected_context"]["followup_lineage"][child][0]["resolution"]
        self.assertEqual((step["id"], step["effective_status"]),
                         (submitted["resolutions"][0], "reviewer_satisfied"))


class ReconsiderationTests(unittest.TestCase):
    """ADR 0018 §3.5: only the owner withdraws a veto, one claim at a time."""

    def test_reconsideration_withdraws_the_veto_only_for_its_claim(self):
        lab = FamilyFixture(self)
        protocol, runs = lab.protocol()
        claim_a = lab.claim(protocol, runs)
        review = lab.cmd("reviewer-r1", "reviewer", "kernel.review", claim=claim_a, verdict="reject",
                         rationale="Confounded design", actions=["Rule out confounding"],
                         expected_basis=current_basis(lab.store, claim_a))
        other = lab.cmd("reviewer-r3", "reviewer", "kernel.review", claim=claim_a,
                        verdict="request_changes", rationale="A separate fixture concern",
                        actions=["Report the variance"], expected_basis=current_basis(lab.store, claim_a))
        claim_b = lab.claim(protocol, runs, statement="Effect X is bounded")
        with self.assertRaisesRegex(ValueError, "withdraw exactly"):
            reconsider(lab.store, claim_b, reviewer="reviewer-r1", withdraw=[])
        with self.assertRaisesRegex(ValueError, "only a reconsideration approval"):
            submit_review(lab.store, claim_b, reviewer="reviewer-r1", verdict="request_changes",
                          findings=[dict(kind="narrow_claim", action="Narrow it",
                                         closure_criterion="Reviewed", evidence_refs=[claim_b])],
                          withdrawals=[dict(opinion=review, rationale="Fixture")], resolutions=[])
        self.assertFalse(any(event["kind"] == "review_submission" for event in lab.store.events()))
        submitted = reconsider(lab.store, claim_b, reviewer="reviewer-r1")
        manifest = json.loads(lab.store.read(submitted["bundle"]))
        self.assertEqual(manifest["policy"], "veto_reconsideration_v1")
        # The refused negative submission left a completed response: it vetoes too (§3.6).
        opinions = manifest["own_findings"]["opinions"]
        self.assertEqual([(row["id"] == review, row["kind"]) for row in opinions],
                         [(True, "review"), (False, "review_response")])
        self.assertNotIn("A separate fixture concern", json.dumps(manifest))
        decision = lab.next_action(claim_b)
        self.assertEqual(decision["action"], "replan", decision)
        self.assertEqual(decision["family_vetoes"],
                         [dict(review=other, claim=claim_a, reviewer="reviewer-r3")])
        self.assertEqual(lab.next_action(claim_a)["action"], "replan")
        ResearchGraph.from_store(lab.store)

    def test_reconsideration_refuses_a_v1_response(self):
        lab = FamilyFixture(self)
        protocol, runs = lab.protocol()
        claim_a = lab.claim(protocol, runs)
        lab.cmd("reviewer-r1", "reviewer", "kernel.review", claim=claim_a, verdict="reject",
                rationale="Confounded design", actions=["Rule out confounding"],
                expected_basis=current_basis(lab.store, claim_a))
        claim_b = lab.claim(protocol, runs, statement="Effect X is bounded")
        study, basis = study_of(lab.store, claim_b), current_basis(lab.store, claim_b)
        assigned = assign(lab.store, claim_b, reviewer="reviewer-r1")
        delivered = deliver(lab.store, assigned["assignment"],
                            dict(verdict="approve", rationale="Fixture", findings=[],
                                 link_assessments=None), study=study)
        with self.assertRaisesRegex(GateError, "v2 fields under reconsideration"):
            command(lab.store, "reviewer-r1", "reviewer", "review.submit",
                    dict(assignment=assigned["assignment"], response=delivered["response"],
                         expected_basis=basis), study)
        self.assertEqual(lab.next_action(claim_b)["action"], "replan")

    def test_narrow_claim_resolution_is_bound_to_its_claim(self):
        lab = FamilyFixture(self)
        protocol, runs = lab.protocol()
        claim_a = lab.claim(protocol, runs)
        obligation = lab.cmd("reviewer-r1", "reviewer", "replanning.record_review", claim=claim_a,
            verdict="request_changes", rationale="Overbroad statement", findings=[dict(
                kind="narrow_claim", action="Narrow the statement",
                closure_criterion="A bounded statement is reviewed", evidence_refs=[claim_a])],
            expected_basis=current_basis(lab.store, claim_a), link_assessments=None)["obligations"][0]
        claim_b = lab.claim(protocol, runs, statement="Effect X is bounded to this fixture",
                            outcome="inconclusive")
        self.assertEqual(lab.next_action(claim_b)["obligations"], [obligation])
        submitted = reconsider(lab.store, claim_b, reviewer="reviewer-r1", resolutions=[dict(
            obligation=obligation, evidence_refs=[claim_b],
            rationale="The bounded statement meets the closure criterion in this fixture")])
        history = lab.store.events()
        resolution = Kernel._get(history, submitted["resolutions"][0], "review_obligation_resolution")
        self.assertEqual({key: resolution["payload"][key] for key in
                          ("schema_version", "kind", "claim", "followup", "terminal")},
                         dict(schema_version=2, kind="narrow_claim", claim=claim_b,
                              followup=None, terminal=None))
        self.assertEqual(lab.next_action(claim_b)["action"], "paper_candidate")
        self.assertEqual(lab.next_action(claim_a)["action"], "replan")
        claim_c = lab.claim(protocol, runs, statement="Effect X exists broadly")
        decision = lab.next_action(claim_c)
        self.assertEqual((decision["action"], decision["obligations"]), ("replan", [obligation]))
        lab.paper(claim_b)
        # Replay, Graph and backup/restore keep the schema 2 receipts.
        graph = ResearchGraph.from_store(lab.store)
        self.assertEqual(graph.node(resolution["id"]).kind.value, "review_obligation_resolution")
        exported = lab.store.export(), lab.store.export_receipts()
        spare = tempfile.TemporaryDirectory(prefix="episteme-families-restore-")
        self.addCleanup(spare.cleanup)
        snapshot, restored = Path(spare.name) / "snapshot", Path(spare.name) / "restored"
        backup(lab.store, snapshot)
        restore(snapshot, restored)
        with Store(restored) as reopened:
            self.assertEqual((reopened.export(), reopened.export_receipts()), exported)
            self.assertEqual(ResearchGraph.from_store(reopened).snapshot_hash, graph.snapshot_hash)
            reader = Kernel(reopened, Actor("family-observer", "observer"))
            self.assertEqual(reader.next_action(claim_c)["action"], "replan")


if __name__ == "__main__":
    unittest.main()
