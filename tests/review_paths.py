"""Test-only review path: assignment, projected delivery, then review.submit.

Every opinion written here is an explicit synthetic fixture, not scientific
review. The helpers let tests reach decisions through the assigned review
chain instead of legacy review commands. The fixture provider returns a
prepared decision; it never reads the Store or calls a model.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from episteme.commands import CommandService
from episteme.kernel import Actor, Kernel
from episteme.reviewer_controller import ReviewerController
from episteme.store import Store, canonical

FIXTURE_RATIONALE = "Explicit test fixture opinion; no scientific review was performed"
FIXTURE_PLANNER = "fixture-review-planner"
FIXTURE_PROVIDER = "fixture-review-provider"
DEFAULT_STUDY = "fixture-review-study"


class FixtureReviewProvider:
    """Answers one projected request with a prepared, test-authored decision."""

    def __init__(self, decision: dict[str, Any]):
        self.decision = decision
        self.requests: list[dict[str, Any]] = []

    def invoke(self, request: dict[str, Any]) -> tuple[bytes, dict[str, int | float]]:
        self.requests.append(request)
        # Reconsideration responses (ADR 0018) carry withdrawals and resolutions.
        version = 2 if "withdrawals" in self.decision else 1
        response = dict(schema_version=version, assignment=request["assignment"],
                        bundle=request["bundle"], claim=request["claim"],
                        basis_hash=request["basis_hash"],
                        reviewer_actor=request["reviewer_actor"], **self.decision)
        return canonical(response), {"fixture_calls": 1}


def study_of(store: Store, claim: str) -> str:
    history = store.events()
    protocol = Kernel._get(history, Kernel._get(history, claim, "claim")["payload"]["protocol"],
                           "protocol")
    return protocol["payload"].get("planning", {}).get("study_id", DEFAULT_STUDY)


def command(store: Store, actor: str, role: str, action: str, payload: dict[str, Any],
            study: str) -> Any:
    return CommandService(store).execute(dict(
        context=dict(command_id=f"{action}-{uuid4().hex}", expected_revision=len(store.events()),
                     actor=actor, role=role, study_id=study,
                     correlation_id="fixture-review-path", causation_id=None),
        request=dict(version=1, action=action, payload=payload)))


def current_basis(store: Store, claim: str) -> str:
    return Kernel(store, Actor("fixture-review-reader", "observer")).gate(claim)["basis_hash"]


def assign(store: Store, claim: str, *, reviewer: str, planner: str = FIXTURE_PLANNER,
           study: str | None = None, expected_basis: str | None = None) -> dict[str, Any]:
    study = study_of(store, claim) if study is None else study
    basis = current_basis(store, claim) if expected_basis is None else expected_basis
    return command(store, planner, "planner", "review.assign",
                   dict(claim=claim, reviewer_actor=reviewer, expected_basis=basis), study)


def deliver(store: Store, assignment: str, decision: dict[str, Any], *,
            planner: str = FIXTURE_PLANNER, study: str) -> dict[str, Any]:
    provider = FixtureReviewProvider(decision)
    delivered = ReviewerController(store, Actor(planner, "planner"), study, FIXTURE_PROVIDER,
                                   provider).advance(assignment)
    if delivered["status"] != "completed":
        raise AssertionError(f"fixture review delivery did not complete: {delivered}")
    return delivered


def submit_review(store: Store, claim: str, *, reviewer: str = "fixture-reviewer",
                  verdict: str = "approve", rationale: str = FIXTURE_RATIONALE,
                  findings: list[dict[str, Any]] | None = None,
                  link_assessments: dict[str, Any] | None = None,
                  planner: str = FIXTURE_PLANNER, study: str | None = None,
                  expected_basis: str | None = None,
                  withdrawals: list[dict[str, Any]] | None = None,
                  resolutions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Assign, deliver and submit one fixture opinion; returns all command results."""
    study = study_of(store, claim) if study is None else study
    basis = current_basis(store, claim) if expected_basis is None else expected_basis
    assigned = assign(store, claim, reviewer=reviewer, planner=planner, study=study,
                      expected_basis=basis)
    decision = dict(verdict=verdict, rationale=rationale,
                    findings=[] if findings is None else findings,
                    link_assessments=link_assessments)
    policy = Kernel._get(store.events(), assigned["assignment"], "review_assignment")["payload"]["policy"]
    if policy == "veto_reconsideration_v1" and withdrawals is None and resolutions is None:
        # A veto owner's later verdict is a reconsideration; approval withdraws every own opinion.
        ids = open_opinions(store, reviewer, claim) if verdict == "approve" else []
        withdrawals, resolutions = [dict(opinion=id, rationale=FIXTURE_WITHDRAWAL) for id in ids], []
    if withdrawals is not None or resolutions is not None:
        decision.update(withdrawals=withdrawals or [], resolutions=resolutions or [])
    delivered = deliver(store, assigned["assignment"], decision, planner=planner, study=study)
    submitted = command(store, reviewer, "reviewer", "review.submit",
                        dict(assignment=assigned["assignment"], response=delivered["response"],
                             expected_basis=basis), study)
    return dict(submitted, assignment=assigned["assignment"], bundle=assigned["bundle"],
                response=delivered["response"])


def approve(store: Store, claim: str, *, reviewer: str = "fixture-reviewer", **options: Any) -> str:
    """Record an assigned fixture approval and return its review event ID."""
    return submit_review(store, claim, reviewer=reviewer, verdict="approve", **options)["review"]


FIXTURE_WITHDRAWAL = "Explicit test fixture withdrawal for this claim; no scientific review was performed"


def open_opinions(store: Store, reviewer: str, claim: str) -> list[str]:
    """The reviewer's own open opinions that bind the claim (what a withdrawal must list)."""
    from episteme.review_admission import admission
    return [opinion["id"] for opinion in
            admission(store, store.events()).own_findings(reviewer, claim)["opinions"]]


def reconsider(store: Store, claim: str, *, reviewer: str, verdict: str = "approve",
               withdraw: list[str] | None = None,
               resolutions: list[dict[str, Any]] | None = None, **options: Any) -> dict[str, Any]:
    """Submit a v2 reconsideration response; by default it withdraws every open own opinion."""
    ids = open_opinions(store, reviewer, claim) if withdraw is None else withdraw
    withdrawals = ([dict(opinion=id, rationale=FIXTURE_WITHDRAWAL) for id in ids]
                   if verdict == "approve" else [])
    return submit_review(store, claim, reviewer=reviewer, verdict=verdict,
                         withdrawals=withdrawals, resolutions=resolutions or [], **options)
