"""Recorded relation closure uses pure fixtures, never scientific approvals."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import unittest

from episteme.claim_context import ClaimContext, ClaimContextError, resolve_context, validate_link, validate_links
from episteme.claims import ClaimLink


class ClaimContextTests(unittest.TestCase):
    def setUp(self):
        self.history = []

    def event(self, id, kind="claim", payload=None):
        seq = len(self.history) + 1
        event = dict(id=id, seq=seq, kind=kind, hash=f"{seq:064x}",
                     payload={"scope": {"dataset": "fixture", "split": "heldout"}} if payload is None else payload)
        self.history.append(event)
        return event

    def candidate(self, source, target, relation="supports"):
        events = {event["id"]: event for event in self.history}
        return ClaimLink(schema_version=1, source=source, target=target, relation=relation,
                         rationale="Synthetic declared relation", source_hash=events[source]["hash"],
                         target_hash=events[target]["hash"], source_basis="a" * 64, target_basis="b" * 64)

    def link(self, source, target, relation="supports", id=None):
        candidate = self.candidate(source, target, relation)
        validate_link(self.history, candidate)
        return self.event(id or f"link-{len(self.history)}", "claim_link", candidate.to_dict())

    def test_no_links_returns_only_requested_claim_and_frozen_ids(self):
        self.event("a")
        self.event("unrelated")
        context = resolve_context(self.history, "a")
        self.assertEqual(context, ClaimContext(("a",), ()))
        self.assertEqual(validate_links(self.history), ())
        with self.assertRaises(FrozenInstanceError):
            context.claim_ids = ()
        with self.assertRaises(ClaimContextError):
            resolve_context(self.history, "missing")

    def test_transitive_support_and_limits_closure_changes_when_upstream_context_changes(self):
        for id in ("a", "b", "c", "unrelated"):
            self.event(id)
        first = self.link("a", "b")
        second = self.link("b", "c", "limits")
        excluded = self.link("c", "unrelated")
        before = resolve_context(self.history, "c")
        self.assertEqual(before.claim_ids, ("a", "b", "c"))
        self.assertEqual(before.link_ids, (first["id"], second["id"]))
        self.assertNotIn(excluded["id"], before.link_ids)
        self.event("upstream")
        addition = self.link("upstream", "a")
        after = resolve_context(self.history, "c")
        self.assertEqual(after.claim_ids, ("a", "b", "c", "upstream"))
        self.assertEqual(after.link_ids, (first["id"], second["id"], addition["id"]))
        self.assertEqual(before.claim_ids, ("a", "b", "c"))

    def test_contradictions_are_symmetric_and_pull_other_endpoint_ancestors(self):
        for id in ("a", "b", "b-parent", "unrelated"):
            self.event(id)
        incoming = self.link("b-parent", "b", "limits")
        contradiction = self.link("a", "b", "contradicts")
        self.link("a", "unrelated")
        expected = ClaimContext(("a", "b", "b-parent"), (incoming["id"], contradiction["id"]))
        self.assertEqual(resolve_context(self.history, "a"), expected)
        self.assertEqual(resolve_context(self.history, "b"), expected)

    def test_supersession_review_context_allows_old_supports_new_mixed_edges(self):
        for id in ("old", "old-source", "new"):
            self.event(id)
        first = self.link("old-source", "old")
        second = self.link("old", "new", "supports")
        supersession = self.link("new", "old", "supersedes")
        expected = ClaimContext(("old", "old-source", "new"),
                                (first["id"], second["id"], supersession["id"]))
        self.assertEqual(resolve_context(self.history, "old"), expected)
        self.assertEqual(resolve_context(self.history, "new"), expected)
        self.assertEqual(len(validate_links(self.history)), 3)

    def test_supports_and_limits_share_dag_but_contradictions_do_not_create_directed_cycles(self):
        for id in ("a", "b", "c"):
            self.event(id)
        self.link("a", "b", "supports")
        self.link("b", "c", "limits")
        for relation in ("supports", "limits"):
            with self.subTest(relation=relation), self.assertRaisesRegex(ClaimContextError, "cycle"):
                validate_link(self.history, self.candidate("c", "a", relation))
        self.link("c", "a", "contradicts")
        self.assertEqual(resolve_context(self.history, "a").claim_ids, ("a", "b", "c"))

    def test_duplicates_and_reversed_contradiction_are_rejected(self):
        self.event("a")
        self.event("b")
        self.link("a", "b")
        with self.assertRaisesRegex(ClaimContextError, "duplicate"):
            validate_link(self.history, self.candidate("a", "b"))
        self.link("a", "b", "limits")
        self.link("a", "b", "contradicts")
        for source, target in (("a", "b"), ("b", "a")):
            with self.subTest(source=source), self.assertRaisesRegex(ClaimContextError, "duplicate"):
                validate_link(self.history, self.candidate(source, target, "contradicts"))

    def test_new_links_between_existing_context_endpoints_are_retained(self):
        self.event("a")
        self.event("b")
        support = self.link("a", "b")
        before = resolve_context(self.history, "b")
        limitation = self.link("a", "b", "limits")
        after = resolve_context(self.history, "b")
        self.assertEqual(before.claim_ids, after.claim_ids)
        self.assertEqual(after.link_ids, (support["id"], limitation["id"]))

    def test_superseding_source_must_be_newer(self):
        self.event("old")
        self.event("new")
        with self.assertRaisesRegex(ClaimContextError, "newer"):
            validate_link(self.history, self.candidate("old", "new", "supersedes"))
        self.link("new", "old", "supersedes")

    def test_endpoint_event_hashes_kinds_and_dangling_references_fail_closed(self):
        self.event("a")
        self.event("b")
        self.event("not-claim", "result", {"status": "completed"})
        candidate = self.candidate("a", "b")
        cases = [(replace(candidate, source_hash="f" * 64), "hash mismatch"),
                 (replace(candidate, target_hash="f" * 64), "hash mismatch"),
                 (replace(candidate, source="missing"), "missing prior claim"),
                 (self.candidate("a", "not-claim"), "wrong reference kind")]
        for invalid, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ClaimContextError, reason):
                validate_link(self.history, invalid)
        # Basis verification belongs to kernel admission, not this resolver.
        validate_link(self.history, replace(candidate, source_basis="c" * 64, target_basis="d" * 64))

    def test_scope_must_match_exactly_without_subset_generalization(self):
        self.event("a")
        for id, scope in (("different", {"dataset": "fixture", "split": "discovery"}),
                          ("subset", {"dataset": "fixture"}), ("empty", {})):
            self.event(id, payload={"scope": scope})
            with self.subTest(id=id), self.assertRaises(ClaimContextError):
                validate_link(self.history, self.candidate("a", id))

    def test_recorded_forward_reference_and_bad_sequence_order_are_rejected(self):
        a, b = self.event("a"), self.event("b")
        payload = self.candidate("a", "b").to_dict()
        forward = [a, dict(id="link", kind="claim_link", seq=2, hash="e" * 64, payload=payload),
                   {**b, "seq": 3}]
        with self.assertRaisesRegex(ClaimContextError, "missing prior claim"):
            validate_links(forward)
        for malformed in ([b, a], [a, {**b, "seq": a["seq"]}], [a, {**b, "id": a["id"]}]):
            with self.subTest(malformed=malformed), self.assertRaises(ClaimContextError):
                validate_links(malformed)

    def test_existing_malformed_link_invalidates_validation_and_resolution_without_mutation(self):
        self.event("a")
        self.event("b")
        self.link("a", "b")
        before = deepcopy(self.history)
        validate_links(self.history)
        resolve_context(self.history, "b")
        self.assertEqual(self.history, before)
        self.history[-1]["payload"]["source_hash"] = "e" * 64
        for operation in (lambda: validate_links(self.history), lambda: resolve_context(self.history, "b"),
                          lambda: validate_link(self.history, self.candidate("a", "b", "limits"))):
            with self.assertRaisesRegex(ClaimContextError, "hash mismatch"):
                operation()


if __name__ == "__main__":
    unittest.main()
