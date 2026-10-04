"""Histories written by audit commit da6aa2a, read under ADR 0018 admission rules.

The fixture stores in ``tests/fixtures/adr0018`` were produced by
``tests/fixtures/adr0018/generate.py`` on a da6aa2a snapshot. Their approvals
are synthetic fixtures accepted by that code; current code must keep them as
history, refuse to count them and never turn them into eligibility.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from episteme.graph import ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.reporting import PaperBuilder, inspect_store

import golden_support
from review_paths import FIXTURE_RATIONALE, assign, command, deliver, study_of

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "adr0018"


class HistoricalAdmissionTests(unittest.TestCase):
    def load(self, name):
        temporary = TemporaryDirectory(prefix="episteme-adr0018-history-")
        self.addCleanup(temporary.cleanup)
        fixture = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        store = golden_support.load_history(fixture, Path(temporary.name) / "store")
        self.addCleanup(store.close)
        ResearchGraph.from_store(store)
        return store

    def claim(self, store, kind="claim"):
        return next(e["id"] for e in store.events() if e["kind"] == kind)

    def next_action(self, store, claim):
        return Kernel(store, Actor("fixture-reader", "observer")).next_action(claim)

    def test_legacy_review_paper_is_not_eligible(self):
        # Audit finding A-06: a CLI approval without assignment, then a paper of the demo claim.
        store = self.load("legacy_review_paper")
        claim = self.claim(store)
        paper = self.claim(store, "paper")
        decision = self.next_action(store, claim)
        self.assertEqual(decision["action"], "scientific_review")
        self.assertEqual([row["defect"] for row in decision["advisory_approvals"]],
                         ["approval lacks a verified review.submit chain"])
        self.assertEqual(inspect_store(store)["papers"],
                         [dict(id=paper, status="not_eligible_under_current_rules")])
        before = store.export()
        with self.assertRaisesRegex(ValueError, "not eligible under current rules"):
            PaperBuilder(store, Actor("fixture-writer", "writer")).materialize(paper)
        self.assertEqual(store.export(), before)

    def test_historical_noncanonical_reviewer_cannot_approve(self):
        # Audit finding A-22: an assigned approval by a case variant of the executor ID.
        store = self.load("noncanonical_reviewer")
        decision = self.next_action(store, self.claim(store))
        self.assertEqual(decision["action"], "scientific_review")
        self.assertEqual([(row["reviewer"], row["defect"]) for row in decision["advisory_approvals"]],
                         [("Fixture-Executor", "approval by a non-canonical historical reviewer ID")])

    def test_unverified_v1_analysis_needs_recomputation_before_approval(self):
        # Audit finding A-04: a forged schema 1 analysis approved through review.submit.
        store = self.load("forged_v1_analysis")
        claim = self.claim(store)
        decision = self.next_action(store, claim)
        self.assertEqual(decision["action"], "scientific_review")
        self.assertEqual([row["defect"] for row in decision["advisory_approvals"]],
                         ["approval lacks recomputation of the unverified historical analysis"])
        study = study_of(store, claim)
        basis = Kernel(store, Actor("fixture-reader", "observer")).gate(claim)["basis_hash"]
        assigned = assign(store, claim, reviewer="fixture-second-reviewer", study=study,
                          expected_basis=basis)
        delivered = deliver(store, assigned["assignment"], dict(
            verdict="approve", rationale=FIXTURE_RATIONALE, findings=[], link_assessments=None),
            study=study)
        before = store.export(), store.export_receipts()
        with self.assertRaisesRegex(ValueError, "adapter"):
            command(store, "fixture-second-reviewer", "reviewer", "review.submit",
                    dict(assignment=assigned["assignment"], response=delivered["response"],
                         expected_basis=basis), study)
        self.assertEqual((store.export(), store.export_receipts()), before)
        self.assertEqual(self.next_action(store, claim)["action"], "scientific_review")


if __name__ == "__main__":
    unittest.main()
