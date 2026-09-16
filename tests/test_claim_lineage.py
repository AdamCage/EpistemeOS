"""Multi-step claim replacement contracts; all opinions are explicit fixtures."""

import unittest

from tests import test_claim_workflow as workflow

from episteme.kernel import GateError


class ClaimLineageTests(unittest.TestCase):
    def setUp(self):
        # Compose the fixture rather than inherit its tests or expose an imported
        # TestCase class to unittest discovery a second time.
        self.fixture = workflow.ClaimWorkflowTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_accepted_lineage_uses_historical_internal_versions_and_current_frontier(self):
        fixture = self.fixture
        a = fixture.branch("lineage-A")
        branch = fixture.branches[a]
        evidence = list(branch["evidence"])

        def next_version(label):
            primary, _ = fixture.attempt(branch)
            replica, _ = fixture.attempt(branch, replica_of=primary)
            evidence.extend((primary, replica))
            return fixture.kernel(branch["authors"]["analyst"], "analyst").claim(
                protocol=branch["protocol"], statement=f"Synthetic revised claim {label}",
                scope=branch["scope"], evidence=list(evidence), outcome="inconclusive",
                limitations=["Synthetic version-chain fixture; no scientific assessment"])

        b = next_version("B")
        b_replaces_a = fixture.link(b, a, "supersedes")
        fixture.review(b)
        self.assertEqual(fixture.reader.next_action(b)["action"], "paper_candidate")

        c = next_version("C")
        c_replaces_b = fixture.link(c, b, "supersedes")
        self.assertEqual({fixture.event(id)["payload"]["protocol"] for id in (a, b, c)},
                         {branch["protocol"]})
        gate = fixture.reader.gate(c)
        self.assertTrue(gate["passed"], gate["failures"])
        b_readiness = next(item for item in gate["related_claims"] if item["claim"] == b)
        self.assertTrue(b_readiness["historical_gate"]["passed"])
        self.assertFalse(b_readiness["current_gate"]["passed"])
        self.assertTrue(any("claim must include all completed runs" in failure
                            for failure in b_readiness["current_gate"]["failures"]))

        # B is still a valid historical replacement of A. Its immutable evidence
        # list does not contain the later runs that justify C, so it is no longer
        # a current frontier. Accepting C must not require rejecting B's history.
        review = fixture.review(c)
        assessments = fixture.event(review)["payload"]["link_assessments"]
        self.assertEqual(set(assessments), {b_replaces_a, c_replaces_b})
        self.assertTrue(all(item["judgment"] == "accepted" for item in assessments.values()))
        self.assertEqual(fixture.reader.next_action(c)["action"], "paper_candidate")

        # Only an accepted successor makes B historical for source qualification.
        # Rejecting C -> B leaves B as an active replacement and requires its
        # current readiness, which the newer runs have invalidated.
        rejected_successor = fixture.assessments(c)
        rejected_successor[c_replaces_b]["judgment"] = "rejected"
        before = fixture.store.events()
        with self.assertRaisesRegex(GateError, "mechanically unqualified source.*" + b_replaces_a):
            fixture.review(c, assessments=rejected_successor)
        self.assertEqual(fixture.store.events(), before)

        # Use an independent dependent claim to exercise C's source qualification:
        # its own gate remains ready even when C's later results exceed C's frozen
        # citations. Testing review(C) alone would only exercise C's own gate.
        dependent = fixture.branch("lineage-dependent")
        fixture.link(c, dependent, "supports")
        fixture.review(dependent)
        primary, _ = fixture.attempt(branch)
        fixture.attempt(branch, replica_of=primary)
        gate = fixture.reader.gate(dependent)
        self.assertTrue(gate["passed"], gate["failures"])
        c_readiness = next(item for item in gate["related_claims"] if item["claim"] == c)
        self.assertTrue(c_readiness["historical_gate"]["passed"])
        self.assertFalse(c_readiness["current_gate"]["passed"])
        before = fixture.store.events()
        with self.assertRaisesRegex(GateError, "mechanically unqualified source.*" + c_replaces_b):
            fixture.review(dependent)
        self.assertEqual(fixture.store.events(), before)
        self.assertEqual(fixture.reader.next_action(dependent)["action"], "scientific_review")


if __name__ == "__main__":
    unittest.main()
