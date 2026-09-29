"""Synthetic fixture checks for a paper's recorded review-driven provenance.

The local reviewer opinions below exercise traceability and gates; they are not
evidence of an independent Scientific Reviewer or a publication-ready paper.
"""

import json
import unittest

from episteme.kernel import Actor, Kernel
from episteme.reporting import PaperBuilder
from episteme.replanning import open_obligations

from tests import test_resolution


class PaperFollowupProvenanceTests(unittest.TestCase):
    components = staticmethod(test_resolution.SoleObligationResolutionTests.components)
    _finish = test_resolution.SoleObligationResolutionTests._finish
    envelope = test_resolution.SoleObligationResolutionTests.envelope
    command = test_resolution.SoleObligationResolutionTests.command
    prepare = test_resolution.SoleObligationResolutionTests.prepare
    complete_child = test_resolution.SoleObligationResolutionTests.complete_child
    resolve = test_resolution.SoleObligationResolutionTests.resolve

    def setUp(self):
        test_resolution.SoleObligationResolutionTests.setUp(self)
        self.writer = PaperBuilder(self.store, Actor("fixture-writer", "writer"))

    def build(self, claim, basis):
        paper = self.writer.build(title="Synthetic bounded follow-up draft",
                                  claims=[claim], expected_bases={claim: basis})
        payload = Kernel._get(self.store.events(), paper, "paper")["payload"]
        return paper, json.loads(self.store.read(payload["bundle"])), self.store.read(
            payload["manuscript"]).decode("utf-8")

    def assert_lineage(self, bundle, manuscript, selected_claim, resolution, results):
        history = self.store.events()
        events = {event["id"]: event for event in history}
        selected = bundle["selected_context"]
        self.assertEqual(selected["links"], [])
        lineage = selected["followup_lineage"][selected_claim]
        self.assertEqual(len(lineage), 1)
        step = lineage[0]
        source_review = events[events[self.obligation]["payload"]["review"]]
        for key, id in (("source_claim", self.claim), ("source_review", source_review["id"]),
                        ("obligation", self.obligation), ("followup", self.followup),
                        ("resolution", resolution)):
            self.assertEqual((step[key]["id"], step[key]["hash"]),
                             (id, events[id]["hash"]))
            self.assertIn(id, manuscript)
        self.assertEqual(step["source_review"]["verdict"], "request_changes")
        self.assertEqual(step["obligation"]["closure_criterion"],
                         events[self.obligation]["payload"]["closure_criterion"])
        self.assertEqual(step["obligation"]["evidence_refs"],
                         events[self.obligation]["payload"]["evidence_refs"])
        for field, hash_field in (("protocol", "protocol_hash"),
                                  ("experiment_node", "experiment_node_hash")):
            self.assertEqual(step["followup"][hash_field],
                             events[step["followup"][field]]["hash"])
            self.assertIn(step["followup"][hash_field], manuscript)
        self.assertEqual(step["resolution"]["recorded_disposition"], "reviewer_satisfied")
        self.assertEqual(step["resolution"]["effective_status"], "reviewer_satisfied")
        self.assertEqual([ref["id"] for ref in step["resolution"]["evidence_refs"]],
                         [ref["id"] for ref in events[resolution]["payload"]["evidence_refs"]])
        self.assertEqual({ref["id"] for ref in step["resolution"]["evidence_refs"]},
                         {step["resolution"]["claim"], *results})
        for ref in step["resolution"]["evidence_refs"]:
            self.assertEqual(ref["hash"], events[ref["id"]]["hash"])
            self.assertEqual(ref["kind"], events[ref["id"]]["kind"])
            self.assertIn(ref["id"], manuscript)
            self.assertIn(ref["hash"], manuscript)
        self.assertIn("Review-driven follow-up lineage", manuscript)
        self.assertIn("does not establish independent scientific validity", manuscript)
        self.assertIn("Original finding citations", manuscript)
        self.assertIn("Cited new evidence", manuscript)
        self.assertEqual(bundle["snapshot_hash"], bundle["events"][-1]["hash"])

    def test_direct_child_paper_traces_negative_review_and_new_results(self):
        child, basis, results = self.complete_child()
        resolution = self.resolve(child, basis, results)
        paper, bundle, manuscript = self.build(child, basis)
        self.assertEqual(bundle["selected_context"]["claims"], [child])
        self.assert_lineage(bundle, manuscript, child, resolution, results)
        self.assertEqual(bundle["selected_context"]["followup_lineage"][child][0]
                         ["followup"]["protocol"],
                         Kernel._get(self.store.events(), child, "claim")["payload"]["protocol"])
        self.assertEqual(self.writer.materialize(paper)["manuscript"],
                         str(self.root / f"{paper}.md"))

    def test_descendant_protocol_paper_keeps_ancestral_review_lineage(self):
        child, basis, results = self.complete_child()
        resolution = self.resolve(child, basis, results)
        child_protocol = Kernel._get(self.store.events(), child, "claim")["payload"]["protocol"]
        protocol = Kernel(self.store, self.planner).preregister_for_set(
            explanation_set=self.explanation_set, parent=child_protocol,
            design="Frozen descendant check", metric="mean",
            analysis_plan="Compute the synthetic descendant mean",
            stopping_rule="One registered seed and one same-data reanalysis",
            seeds=[9], run_limit=2, implementation=self.child_source,
            environment=self.environment, data=self.data, replication_tolerance=0)
        primary = Kernel(self.store, self.executor).start_run(protocol, seed=9,
            implementation=self.child_source, environment=self.environment,
            command=["python", "descendant.py"])
        self._finish(primary, self.executor)
        replica = Kernel(self.store, self.replicator).start_run(protocol, seed=9,
            implementation=self.reanalysis, environment=self.environment,
            command=["python", "descendant-reanalysis.py"], replicate_of=primary)
        self._finish(replica, self.replicator)
        descendant = Kernel(self.store, self.analyst).claim(
            protocol=protocol, statement="Synthetic descendant remains inconclusive",
            scope=self.scope, evidence=[primary, replica],
            limitations=["Synthetic fixture; no new-data replication"], outcome="inconclusive")
        basis = Kernel(self.store, self.reviewer).gate(descendant)["basis_hash"]
        Kernel(self.store, self.reviewer).review(descendant, verdict="approve",
            rationale="Local fixture opinion on the bounded descendant", actions=[],
            expected_basis=basis)
        self.assertEqual(Kernel(self.store, self.reviewer).next_action(descendant)["action"],
                         "paper_candidate")
        _, bundle, manuscript = self.build(descendant, basis)
        self.assertEqual(bundle["selected_context"]["claims"], [descendant])
        self.assert_lineage(bundle, manuscript, descendant, resolution, results)
        self.assertNotIn(primary, {ref["id"] for ref in bundle["selected_context"]
                         ["followup_lineage"][descendant][0]["resolution"]["evidence_refs"]})

    def test_unresolved_sibling_finding_still_vetoes_paper(self):
        child, basis, results = self.complete_child()
        other_reviewer = Actor("other-fixture-reviewer", "reviewer")
        sibling = self.command("replanning.record_review", other_reviewer, dict(
            claim=self.claim, verdict="request_changes", rationale="A second fixture limitation",
            findings=[dict(kind="narrow_claim", action="Limit the broad source statement",
                           closure_criterion="A separate revised statement is reviewed",
                           evidence_refs=[self.claim])], expected_basis=self.basis,
            link_assessments=None))["obligations"][0]
        self.resolve(child, basis, results)
        self.assertEqual([event["id"] for event in open_obligations(
            self.store, self.store.events(), self.claim)], [sibling])
        self.assertEqual(Kernel(self.store, self.reviewer).next_action(child)["action"], "replan")
        before = self.store.events()
        with self.assertRaisesRegex(ValueError, "not eligible for paper"):
            self.writer.build(title="Blocked synthetic draft", claims=[child],
                              expected_bases={child: basis})
        self.assertEqual(self.store.events(), before)
        self.assertFalse(any(event["kind"] == "paper" for event in before))

    def test_later_reviewer_change_stales_the_recorded_resolution_for_materialization(self):
        child, basis, results = self.complete_child()
        self.resolve(child, basis, results)
        paper, bundle, _ = self.build(child, basis)
        self.assertEqual(bundle["selected_context"]["followup_lineage"][child][0]
                         ["resolution"]["effective_status"], "reviewer_satisfied")
        Kernel(self.store, self.reviewer).review(self.claim, verdict="approve",
            rationale="Later fixture opinion on the original source", actions=[],
            expected_basis=self.basis)
        self.assertEqual(Kernel(self.store, self.reviewer).next_action(child)["action"], "replan")
        with self.assertRaisesRegex(ValueError, "no longer current"):
            self.writer.materialize(paper)


if __name__ == "__main__":
    unittest.main()
