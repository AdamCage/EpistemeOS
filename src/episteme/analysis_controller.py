"""Resume a completed batch through analysis admission and reviewer assignment.

The adapter and actors run in the same trusted local OS context. This
controller records no reviewer verdict, independence proof, or publication.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .batch import _index as batch_index
from .batch_analysis import _index as analysis_index
from .commands import CommandService
from .kernel import Actor, Kernel, require
from .review_assignment import _index as assignment_index
from .review_submission import _index as submission_index
from .store import ConflictError, Store


class AnalysisAdapter(Protocol):
    adapter_id: str
    adapter_version: str

    def propose(self, store: Store, state: dict[str, Any]) -> dict[str, Any]: ...


def analysis_state(store: Store, batch: str) -> dict[str, Any]:
    """Read-only verified progress, including unresolved reviewer assignment."""
    history = store.events()
    states = batch_index(store, history)
    require(batch in states, "unknown batch")
    settlement = states[batch]["settlement"]
    if settlement is None:
        return dict(batch=batch, status="awaiting_settlement", scientific_validity="not_assessed")
    if settlement["payload"]["status"] != "completed":
        return dict(batch=batch, status="repair_evidence", scientific_validity="not_assessed")
    analyses = [event for event in analysis_index(store, history).values()
                if event["payload"]["batch"] == batch]
    if not analyses:
        return dict(batch=batch, status="awaiting_analysis", scientific_validity="not_assessed")
    rows = []
    assignments = assignment_index(store, history)
    submissions = submission_index(store, history)
    observer = Kernel(store, Actor("analysis-status-observer", "observer"))
    for event in analyses:
        gate = observer._gate(history, event["payload"]["claim"])
        assigned = [item for item in assignments.values()
                    if item["payload"]["claim"] == event["payload"]["claim"]
                    and item["payload"]["basis_hash"] == gate["basis_hash"]
                    and item["payload"]["reviewer_actor"] == event["payload"]["reviewer_actor"]]
        submitted = [submissions[item["id"]] for item in assigned if item["id"] in submissions]
        rows.append(dict(analysis=event["id"], claim=event["payload"]["claim"],
                         reviewer_actor=event["payload"]["reviewer_actor"],
                         mechanical_gate=gate, assignments=[item["id"] for item in assigned],
                         submitted_reviews=[row["review"]["id"] for row in submitted],
                         next_action=observer._next_action(history, event["payload"]["claim"])))
    status = ("stale_evidence" if any(not row["mechanical_gate"]["passed"] for row in rows)
              else "awaiting_assignment" if any(not row["assignments"] for row in rows)
              else "awaiting_review" if any(not row["submitted_reviews"] for row in rows)
              else "review_recorded")
    return dict(batch=batch, status=status, analyses=rows, scientific_validity="not_assessed")


def advance_batch_analysis(store: Store, batch: str, *, planner: Actor, analyst: Actor,
                           reviewer_actor: str, adapter: AnalysisAdapter) -> dict[str, str]:
    """Make at most two persisted decisions, resuming from either receipt."""
    require(analyst.role == "analyst", "batch analysis controller needs an analyst")
    require(planner.role == "planner", "batch analysis controller needs a planner")
    require(type(reviewer_actor) is str and bool(reviewer_actor.strip()),
            "reviewer actor is required")
    require(type(adapter.adapter_id) is str and type(adapter.adapter_version) is str,
            "analysis adapter identity is required")
    for _ in range(3):
        history = store.events()
        states = batch_index(store, history)
        require(batch in states, "unknown batch")
        state = states[batch]
        plan, settlement = state["plan"], state["settlement"]
        require(planner.id == plan["actor"], "controller planner differs from batch planner")
        require(settlement is not None and settlement["payload"]["status"] == "completed",
                "batch analysis requires completed technical settlement")
        origin = next((r for r in store.receipts() if batch in r["event_ids"]), None)
        require(origin is not None, "batch planning receipt is missing")
        study_id = origin["context"]["study_id"]
        correlation_id = origin["context"]["correlation_id"]
        existing = [event for event in analysis_index(store, history).values()
                    if event["payload"]["batch"] == batch]
        # The first controller handles one selected analysis task. Explicit
        # other tasks may coexist, but then the caller must disambiguate.
        require(len(existing) <= 1, "batch has multiple analysis tasks; choose one explicitly")
        if existing:
            analysis = existing[0]
            p = analysis["payload"]
            require(analysis["actor"] == analyst.id and p["reviewer_actor"] == reviewer_actor
                    and (p["adapter_id"], p["adapter_version"])
                    == (adapter.adapter_id, adapter.adapter_version),
                    "persisted analysis actor/reviewer/adapter differs from requested controller policy")
            claim_id = p["claim"]
        else:
            proposal = adapter.propose(store, state)
            require(type(proposal) is dict and
                    (proposal.get("adapter_id"), proposal.get("adapter_version"))
                    == (adapter.adapter_id, adapter.adapter_version),
                    "analysis proposal differs from selected adapter identity")
            source = Path(inspect.getfile(type(adapter))).read_bytes()
            source_digest = store.put(source)
            context = dict(command_id=f"analysis-{uuid4().hex}", expected_revision=len(history),
                           actor=analyst.id, role=analyst.role, study_id=study_id,
                           correlation_id=correlation_id, causation_id=settlement["id"])
            request = dict(version=1, action="analysis.apply", payload=dict(
                batch=batch, expected_settlement=settlement["id"], proposal=proposal,
                adapter_source_digest=source_digest, reviewer_actor=reviewer_actor))
            try:
                CommandService(store).execute(dict(context=context, request=request))
            except ConflictError:
                continue
            # Reload from receipts rather than trusting an in-memory proposal.
            continue
        kernel = Kernel(store, Actor("analysis-controller-observer", "observer"))
        gate = kernel._gate(history, claim_id)
        require(gate["passed"], "analysis claim no longer passes mechanical gate: "
                + "; ".join(gate["failures"]))
        assignments = [event for event in assignment_index(store, history).values()
                       if event["payload"]["claim"] == claim_id
                       and event["payload"]["basis_hash"] == gate["basis_hash"]
                       and event["payload"]["reviewer_actor"] == reviewer_actor]
        require(len(assignments) <= 1, "duplicate reviewer assignments for analysis basis")
        if assignments:
            submission = submission_index(store, history).get(assignments[0]["id"])
            return dict(status="review_recorded" if submission else "awaiting_review",
                        batch=batch, analysis=analysis["id"],
                        claim=claim_id, assignment=assignments[0]["id"],
                        basis_hash=gate["basis_hash"], scientific_validity="not_assessed")
        context = dict(command_id=f"analysis-assign-{uuid4().hex}",
                       expected_revision=len(history), actor=planner.id, role=planner.role,
                       study_id=study_id, correlation_id=correlation_id,
                       causation_id=analysis["id"])
        request = dict(version=1, action="review.assign", payload=dict(
            claim=claim_id, reviewer_actor=reviewer_actor, expected_basis=gate["basis_hash"]))
        try:
            CommandService(store).execute(dict(context=context, request=request))
        except ConflictError:
            continue
        # The assignment receipt is durable; return its reloaded projection.
        history = store.events()
        assignments = [event for event in assignment_index(store, history).values()
                       if event["payload"]["claim"] == claim_id
                       and event["payload"]["basis_hash"] == gate["basis_hash"]
                       and event["payload"]["reviewer_actor"] == reviewer_actor]
        require(len(assignments) == 1, "review assignment did not persist")
        return dict(status="awaiting_review", batch=batch, analysis=analysis["id"],
                    claim=claim_id, assignment=assignments[0]["id"],
                    basis_hash=gate["basis_hash"], scientific_validity="not_assessed")
    raise ConflictError("analysis controller lost concurrent admissions; retry from persisted state")
