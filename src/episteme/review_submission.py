"""Bind a local reviewer decision to an assigned, delivered evidence revision.

The caller-supplied reviewer ID and provider response are not authenticated.
Recording their provenance does not establish independent scientific judgment.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .kernel import Actor, Kernel, independent_of, require
from .replanning import _findings, _OBLIGATION_FIELDS
from .reviewer_controller import MAX_RESPONSE_BYTES, _index as delivery_index
from .store import Store


_RESPONSE_FIELDS = {"schema_version", "assignment", "bundle", "claim", "basis_hash",
                    "reviewer_actor", "verdict", "rationale", "findings", "link_assessments"}
_REQUEST_FIELDS = {"assignment", "response", "expected_basis"}
_SUBMISSION_FIELDS = {"schema_version", "assignment", "assignment_hash", "dispatch",
                      "dispatch_hash", "response_event", "response_event_hash", "response",
                      "bundle", "claim", "basis_hash", "reviewer_actor", "review",
                      "review_hash", "obligations", "identity_assurance", "read_isolation"}


def _decision(store: Store, history: list[dict[str, Any]], *, assignment: str,
              response: str, expected_basis: str, reviewer_actor: str,
              study_id: str, receipts: list[dict[str, Any]] | None = None,
              keyed: bool = False) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any],
                                            list[dict[str, Any]]]:
    """Recheck the current mechanical basis and exact completed delivery.

    New submissions compare normalized actor keys (ADR 0018); replay keeps the
    exact comparison under which a historical submission was admitted.
    """
    deliveries = delivery_index(store, history, receipts=receipts)
    require(assignment in deliveries, "review submission needs a verified assignment dispatch")
    state = deliveries[assignment]
    assigned, completed = state["assignment"], state["response"]
    p = assigned["payload"]
    require(p["reviewer_actor"] == reviewer_actor and p["study_id"] == study_id,
            "review submission actor or study differs from assignment")
    require(completed is not None and completed["payload"]["status"] == "completed"
            and completed["payload"]["response"] == response,
            "review submission needs the exact completed delivery response")
    raw = store.read(response)
    require(0 < len(raw) <= MAX_RESPONSE_BYTES, "review response exceeds byte limit")
    from .commands import parse_command

    try:
        decision = parse_command(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"invalid reviewer response JSON: {exc}") from exc
    require(type(decision) is dict and set(decision) == _RESPONSE_FIELDS
            and type(decision["schema_version"]) is int and decision["schema_version"] == 1,
            "review response must match v1 fields")
    for field, expected in (("assignment", assignment), ("bundle", p["bundle"]),
                            ("claim", p["claim"]), ("basis_hash", expected_basis),
                            ("reviewer_actor", reviewer_actor)):
        require(decision[field] == expected and type(decision[field]) is str,
                f"review response {field} differs from frozen assignment")
    require(expected_basis == p["basis_hash"], "review assignment basis differs from response")
    require(decision["verdict"] in {"approve", "request_changes", "reject"},
            "invalid review response verdict")
    require(type(decision["rationale"]) is str and 0 < len(decision["rationale"].strip()) <= 8192
            and "\x00" not in decision["rationale"], "review response needs bounded rationale")
    kernel = Kernel(store, Actor(reviewer_actor, "reviewer"))
    claim = kernel._get(history, p["claim"], "claim")
    gate = kernel._gate(history, p["claim"])
    basis, _ = kernel._basis(history, p["claim"])
    require(gate["passed"] and gate["basis_hash"] == basis == expected_basis,
            "review submission needs the assigned current mechanical basis")
    linked, contributors, admissible = kernel._review_members(history, p["claim"])
    require(independent_of(reviewer_actor, contributors) if keyed else reviewer_actor not in contributors,
            "reviewer contributed to submitted claim context")
    findings = decision["findings"]
    if decision["verdict"] == "approve":
        require(findings == [], "approval cannot contain open findings")
        normalized: list[dict[str, Any]] = []
    else:
        normalized = _findings(history, p["claim"], findings, admissible)
    assessments = decision["link_assessments"]
    require(not linked.link_ids or assessments is not None,
            "linked claim review needs explicit link assessments")
    if assessments is not None:
        kernel._validate_assessments(assessments, set(linked.link_ids), admissible,
                                     decision["verdict"])
        if decision["verdict"] == "approve":
            acknowledged = {id for assessment in assessments.values()
                            for id in assessment["evidence"]}
            require(kernel._context_findings(history, p["claim"])[1] <= acknowledged,
                    "approval must acknowledge open linked-context reviews")
            accepted_replacements = {
                kernel._get(history, id, "claim_link")["payload"]["target"]
                for id, assessment in assessments.items()
                if assessment["judgment"] == "accepted"
                and kernel._get(history, id, "claim_link")["payload"]["relation"] == "supersedes"}
            for id, assessment in assessments.items():
                if assessment["judgment"] != "accepted":
                    continue
                link = kernel._get(history, id, "claim_link")["payload"]
                source = kernel._get(history, link["source"], "claim")
                current_source = (link["relation"] == "supersedes"
                                  and source["id"] not in accepted_replacements)
                basis_history = (history if current_source else
                                 [e for e in history if e["seq"] <= source["seq"]])
                checked = kernel._gate_local(basis_history, source["id"])
                require(checked["passed"],
                        f"accepted link has mechanically unqualified source: {id}")
    return state, decision, claim, normalized


def _submission_payload(state: dict[str, Any], response: str,
                        review: dict[str, Any], obligations: list[str]) -> dict[str, Any]:
    assignment, dispatch, completed = (state[key] for key in
                                       ("assignment", "dispatch", "response"))
    p = assignment["payload"]
    return dict(schema_version=1, assignment=assignment["id"],
                assignment_hash=assignment["hash"], dispatch=dispatch["id"],
                dispatch_hash=dispatch["hash"], response_event=completed["id"],
                response_event_hash=completed["hash"], response=response,
                bundle=p["bundle"], claim=p["claim"], basis_hash=p["basis_hash"],
                reviewer_actor=p["reviewer_actor"], review=review["id"],
                review_hash=review["hash"], obligations=obligations,
                identity_assurance="caller_declared", read_isolation="not_enforced")


def _index(store: Store, history: list[dict[str, Any]], *,
           receipts: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Replay-check every submitted verdict, typed obligation, and receipt."""
    source = store.receipts() if receipts is None else receipts
    relevant = [receipt for receipt in source
                if receipt["request"]["action"] == "review.submit"
                and receipt["after_revision"] <= len(history)]
    submissions = {e["id"] for e in history if e["kind"] == "review_submission"}
    seen: set[str] = set()
    states: dict[str, dict[str, Any]] = {}
    for receipt in relevant:
        context, request = receipt["context"], receipt["request"]
        args = request["payload"]
        require(context["role"] == "reviewer" and request["version"] == 1
                and set(args) == _REQUEST_FIELDS, "invalid historical review submission request")
        before = history[:receipt["before_revision"]]
        delivery, decision, claim, findings = _decision(
            store, before, assignment=args["assignment"], response=args["response"],
            expected_basis=args["expected_basis"], reviewer_actor=context["actor"],
            study_id=context["study_id"], receipts=source)
        events = history[receipt["before_revision"]:receipt["after_revision"]]
        require(len(events) == len(findings) + 2
                and receipt["event_ids"] == [event["id"] for event in events]
                and events[0]["kind"] == "review"
                and all(event["kind"] == "review_obligation" for event in events[1:-1])
                and events[-1]["kind"] == "review_submission",
                "review submission needs its exact review, obligations, and provenance event")
        require(all(event["actor"] == context["actor"] and event["role"] == "reviewer"
                    for event in events), "review submission actor differs from command")
        review, obligations, provenance = events[0], events[1:-1], events[-1]
        expected_review = dict(claim=claim["id"], verdict=decision["verdict"],
                               rationale=decision["rationale"],
                               actions=[finding["action"] for finding in findings],
                               basis_hash=args["expected_basis"])
        if decision["link_assessments"] is not None:
            expected_review.update(review_schema_version=2,
                                   link_assessments=decision["link_assessments"])
        require(review["payload"] == expected_review,
                "review differs from delivered response or historical basis")
        for index, (event, finding) in enumerate(zip(obligations, findings)):
            expected = dict(schema_version=1, review=review["id"],
                            review_hash=review["hash"], claim=claim["id"],
                            claim_hash=claim["hash"], basis_hash=args["expected_basis"],
                            finding_index=index, **finding)
            require(set(event["payload"]) == _OBLIGATION_FIELDS
                    and event["payload"] == expected,
                    "submitted obligation differs from delivered reviewer finding")
        ids = [event["id"] for event in obligations]
        require(set(provenance["payload"]) == _SUBMISSION_FIELDS
                and provenance["payload"] == _submission_payload(delivery, args["response"],
                                                                   review, ids)
                and receipt["result"] == dict(review=review["id"], obligations=ids,
                                              submission=provenance["id"]),
                "review submission provenance differs from its original receipt")
        require(args["assignment"] not in states, "assignment has multiple submitted verdicts")
        states[args["assignment"]] = dict(review=review, obligations=obligations,
                                          submission=provenance)
        seen.add(provenance["id"])
    require(seen == submissions, "review submission event lacks its original receipt")
    return states


class ReviewSubmission:
    """Reviewer-owned local transition from delivered bytes to recorded opinion."""

    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def submit(self, *, assignment: str, response: str, expected_basis: str) -> dict[str, Any]:
        require(self.store._command_context is not None and self.actor.role == "reviewer",
                "review submission needs a reviewer CommandService transaction")
        history = self.store.events()
        require(assignment not in _index(self.store, history),
                "review assignment already has a submitted verdict")
        state, decision, claim, findings = _decision(
            self.store, history, assignment=assignment, response=response,
            expected_basis=expected_basis, reviewer_actor=self.actor.id,
            study_id=self.store._command_context["study_id"], keyed=True)
        kernel = Kernel(self.store, self.actor)
        actions = [finding["action"] for finding in findings]
        if decision["link_assessments"] is None:
            id = kernel.review(claim["id"], verdict=decision["verdict"],
                               rationale=decision["rationale"], actions=actions,
                               expected_basis=expected_basis)
        else:
            id = kernel.review_with_links(claim["id"], verdict=decision["verdict"],
                                          rationale=decision["rationale"], actions=actions,
                                          expected_basis=expected_basis,
                                          link_assessments=decision["link_assessments"])
        review = kernel._get(self.store.events(), id, "review")
        obligations: list[str] = []
        for index, finding in enumerate(findings):
            payload = dict(schema_version=1, review=id, review_hash=review["hash"],
                           claim=claim["id"], claim_hash=claim["hash"],
                           basis_hash=expected_basis, finding_index=index, **finding)
            obligations.append(kernel._write(self.store.events(), "review_obligation",
                                             payload, {"reviewer"}))
        provenance = f"review_submission-{uuid4().hex[:16]}"
        self.store.append(id=provenance, kind="review_submission", actor=self.actor.id,
                          role="reviewer", payload=_submission_payload(state, response,
                                                                         review, obligations),
                          expected_revision=len(self.store.events()))
        return dict(review=id, obligations=obligations, submission=provenance)
