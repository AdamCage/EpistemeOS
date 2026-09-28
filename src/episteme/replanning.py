"""Persist typed negative-review obligations without pretending they are resolved.

This is a local, caller-identified reviewer transition. Role separation and a
mechanically qualified evidence basis do not attest independent reasoning or
scientific correctness. A later experiment, batch or approval never closes an
obligation in this version; closure requires a separately designed transition.
"""

from __future__ import annotations

from typing import Any

from .kernel import Actor, Kernel, require
from .store import Store


FINDING_KINDS = frozenset({
    "discriminating_experiment", "independent_reanalysis", "narrow_claim",
    "request_data", "stop_inconclusive",
})
_FINDING_FIELDS = {"kind", "action", "closure_criterion", "evidence_refs"}
_OBLIGATION_FIELDS = {"schema_version", "review", "review_hash", "claim", "claim_hash",
                      "basis_hash", "finding_index", "kind", "action", "closure_criterion",
                      "evidence_refs"}


def _findings(history: list[dict[str, Any]], claim: str, findings: Any,
              admissible: set[str]) -> list[dict[str, Any]]:
    """Validate reviewer-authored requests and bind citations to event revisions."""
    require(type(findings) is list and 1 <= len(findings) <= 32,
            "negative review needs 1-32 typed findings")
    by_id = {event["id"]: event for event in history}
    admissible = admissible | {claim}
    normalized: list[dict[str, Any]] = []
    for finding in findings:
        require(type(finding) is dict and set(finding) == _FINDING_FIELDS,
                "invalid typed finding fields")
        kind = finding["kind"]
        require(type(kind) is str and kind in FINDING_KINDS, "invalid finding kind")
        for field in ("action", "closure_criterion"):
            value = finding[field]
            require(type(value) is str and bool(value.strip()) and len(value) <= 4096
                    and "\x00" not in value, f"finding {field} must be nonempty bounded text")
        refs = finding["evidence_refs"]
        require(type(refs) is list and 1 <= len(refs) <= 32
                and all(type(ref) is str and ref in admissible for ref in refs)
                and len(set(refs)) == len(refs),
                "finding needs unique evidence references from its review context")
        normalized.append(dict(kind=kind, action=finding["action"],
                               closure_criterion=finding["closure_criterion"],
                               evidence_refs=[dict(id=ref, hash=by_id[ref]["hash"]) for ref in refs]))
    return normalized


def _index(store: Store, history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Replay-verify complete review/obligation commands at historical boundaries.

    Current evidence changes cannot retroactively validate or invalidate the
    admission decision. Event/receipt chain verification remains Store's job.
    """
    obligations = {event["id"]: event for event in history if event["kind"] == "review_obligation"}
    receipts = [receipt for receipt in store.receipts()
                if receipt["request"]["action"] == "replanning.record_review"
                and receipt["after_revision"] <= len(history)]
    if not obligations and not receipts:
        return {}
    seen: set[str] = set()
    states: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        request = receipt["request"]
        payload = request["payload"]
        require(request["version"] == 1 and set(payload) == {
            "claim", "verdict", "rationale", "findings", "expected_basis", "link_assessments"},
            "invalid historical replanning request")
        context = receipt["context"]
        require(context["role"] == "reviewer", "replanning requires a reviewer actor")
        before = history[:receipt["before_revision"]]
        events = history[receipt["before_revision"]:receipt["after_revision"]]
        require(receipt["event_ids"] == [event["id"] for event in events],
                "replanning receipt must bind the exact event sequence")
        require(len(events) >= 2 and events[0]["kind"] == "review"
                and all(event["kind"] == "review_obligation" for event in events[1:]),
                "review obligations need their complete original command receipt")
        review, created = events[0], events[1:]
        require(review["actor"] == context["actor"] and review["role"] == "reviewer"
                and payload["verdict"] in {"request_changes", "reject"}
                and review["payload"]["verdict"] == payload["verdict"],
                "replanning review actor or verdict mismatch")
        kernel = Kernel(store, Actor(context["actor"], "reviewer"))
        require(type(payload["claim"]) is str and bool(payload["claim"].strip())
                and type(payload["rationale"]) is str and bool(payload["rationale"].strip())
                and type(payload["expected_basis"]) is str,
                "historical typed review fields are malformed")
        claim_event = kernel._get(before, payload["claim"], "claim")
        gate = kernel._gate(before, claim_event["id"])
        basis, _ = kernel._basis(before, claim_event["id"])
        require(gate["passed"] and gate["basis_hash"] == basis == payload["expected_basis"],
                "historical review basis was stale or mechanically unqualified")
        linked, contributors, admissible = kernel._review_members(before, claim_event["id"])
        require(context["actor"] not in contributors,
                "historical review actor contributed to its evidence context")
        normalized = _findings(before, claim_event["id"], payload["findings"], admissible)
        require(len(created) == len(normalized), "review and obligations are not one-to-one")
        expected_review = dict(claim=claim_event["id"], verdict=payload["verdict"],
                               rationale=payload["rationale"],
                               actions=[item["action"] for item in normalized], basis_hash=basis)
        assessments = payload["link_assessments"]
        require(not linked.link_ids or assessments is not None,
                "linked context needs explicit link assessments")
        if assessments is not None:
            kernel._validate_assessments(assessments, set(linked.link_ids), admissible,
                                         payload["verdict"])
            expected_review.update(review_schema_version=2, link_assessments=assessments)
        require(review["payload"] == expected_review,
                "review differs from its typed findings and historical evidence basis")
        for index, (event, finding) in enumerate(zip(created, normalized)):
            expected = dict(schema_version=1, review=review["id"], review_hash=review["hash"],
                            claim=claim_event["id"], claim_hash=claim_event["hash"],
                            basis_hash=basis, finding_index=index, **finding)
            require(event["payload"] == expected and set(event["payload"]) == _OBLIGATION_FIELDS
                    and event["actor"] == review["actor"] and event["role"] == "reviewer",
                    "obligation differs from its review, citation, or original finding")
            seen.add(event["id"])
        require(receipt["result"] == dict(review=review["id"],
                                          obligations=[event["id"] for event in created]),
                "replanning receipt result differs from committed events")
        states[review["id"]] = dict(review=review, obligations=created)
    require(seen == set(obligations), "review obligation lacks its original command receipt")
    return states


def open_obligations(store: Store, history: list[dict[str, Any]], claim: str
                     ) -> list[dict[str, Any]]:
    """Return replay-validated, still-open declarations for one claim.

    There is no closure vocabulary yet. A positive review or follow-up experiment
    does not silently erase a negative reviewer's requested work.
    """
    states = _index(store, history)
    return [event for state in states.values() for event in state["obligations"]
            if event["payload"]["claim"] == claim]


class Replanning:
    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def record_review(self, *, claim: str, verdict: str, rationale: str,
                      findings: list[dict[str, Any]], expected_basis: str,
                      link_assessments: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        """Atomically record an external negative opinion and typed open work."""
        require(self.store._command_context is not None and self.actor.role == "reviewer",
                "typed review requires a reviewer CommandService transaction")
        history = self.store.events()
        _index(self.store, history)
        require(verdict in {"request_changes", "reject"},
                "typed obligations require a negative review verdict")
        kernel = Kernel(self.store, self.actor)
        claim_event = kernel._get(history, claim, "claim")
        _, _, admissible = kernel._review_members(history, claim)
        normalized = _findings(history, claim, findings, admissible)
        actions = [item["action"] for item in normalized]
        if link_assessments is None:
            review_id = kernel.review(claim, verdict=verdict, rationale=rationale,
                                      actions=actions, expected_basis=expected_basis)
        else:
            review_id = kernel.review_with_links(claim, verdict=verdict, rationale=rationale,
                                                 actions=actions, expected_basis=expected_basis,
                                                 link_assessments=link_assessments)
        review = kernel._get(self.store.events(), review_id, "review")
        ids: list[str] = []
        for index, finding in enumerate(normalized):
            obligation = dict(schema_version=1, review=review_id, review_hash=review["hash"],
                              claim=claim, claim_hash=claim_event["hash"],
                              basis_hash=expected_basis, finding_index=index, **finding)
            ids.append(kernel._write(self.store.events(), "review_obligation", obligation, {"reviewer"}))
        return dict(review=review_id, obligations=ids)
