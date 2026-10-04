"""Claim families bind vetoes and obligations (ADR 0018, audit findings A-01, A-02).

All opinions are explicit synthetic fixtures; nothing here assesses science.
Scenarios use only command APIs that existed at audit commit da6aa2a.
"""

import tempfile
import unittest
from uuid import uuid4

from episteme.commands import CommandService
from episteme.kernel import Actor, Kernel
from episteme.reporting import PaperBuilder
from episteme.store import Store
from review_paths import approve, current_basis, submit_review

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


if __name__ == "__main__":
    unittest.main()
