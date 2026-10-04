"""Resume a completed batch through analysis admission and reviewer assignment.

The adapter and actors run in the same trusted local OS context. This
controller records no reviewer verdict, independence proof, or publication.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .batch import _index as batch_index
from .batch_analysis import _index as analysis_index
from .commands import CommandService
from .kernel import Actor, GateError, Kernel, require, require_canonical_actor
from .review_assignment import _index as assignment_index
from .review_submission import _index as submission_index
from .store import ConflictError, Store


class AnalysisAdapter(Protocol):
    adapter_id: str
    adapter_version: str

    def propose(self, store: Store, state: dict[str, Any]) -> dict[str, Any]: ...


def _matching(store: Store, history: list[dict[str, Any]], claim: str, basis: str,
              reviewer_actor: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Assignments for this claim, basis and reviewer, split by whether a review can still decide.

    A submitted assignment keeps its recorded review. An unsubmitted one whose
    approval could never count (ADR 0018 §3.1, §4.1) is reported with its defect
    and does not satisfy the assignment step.
    """
    from .review_admission import admission
    submissions = submission_index(store, history)
    projection = admission(store, history)
    usable, unusable = [], {}
    for event in assignment_index(store, history).values():
        payload = event["payload"]
        if (payload["claim"], payload["basis_hash"], payload["reviewer_actor"]) != (claim, basis, reviewer_actor):
            continue
        defect = None if event["id"] in submissions else projection.assignment_defect(event)
        if defect is None:
            usable.append(event)
        else:
            unusable[event["id"]] = defect
    return usable, unusable


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
    if any(event["kind"] == "pack_analysis" for event in history):
        from .domain_packs import pack_analyses
        analyses.extend(event for event in pack_analyses(store, history).values()
                        if event["payload"]["batch"] == batch)
    if not analyses:
        return dict(batch=batch, status="awaiting_analysis", scientific_validity="not_assessed")
    rows = []
    submissions = submission_index(store, history)
    observer = Kernel(store, Actor("analysis-status-observer", "observer"))
    for event in analyses:
        gate = observer._gate(history, event["payload"]["claim"])
        assigned, unusable = _matching(store, history, event["payload"]["claim"], gate["basis_hash"],
                                       event["payload"]["reviewer_actor"])
        submitted = [submissions[item["id"]] for item in assigned if item["id"] in submissions]
        row = dict(analysis=event["id"], claim=event["payload"]["claim"],
                   reviewer_actor=event["payload"]["reviewer_actor"],
                   mechanical_gate=gate, assignments=[item["id"] for item in assigned],
                   submitted_reviews=[row["review"]["id"] for row in submitted],
                   next_action=observer._next_action(history, event["payload"]["claim"]))
        if unusable:
            row["superseded_assignments"] = unusable
        rows.append(row)
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
    for value, label in ((planner.id, "planner"), (analyst.id, "analyst"), (reviewer_actor, "reviewer actor")):
        require_canonical_actor(value, label)
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
        assignments = _matching(store, history, claim_id, gate["basis_hash"], reviewer_actor)[0]
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
        assignments = _matching(store, history, claim_id, gate["basis_hash"], reviewer_actor)[0]
        require(len(assignments) == 1, "review assignment did not persist")
        return dict(status="awaiting_review", batch=batch, analysis=analysis["id"],
                    claim=claim_id, assignment=assignments[0]["id"],
                    basis_hash=gate["basis_hash"], scientific_validity="not_assessed")
    raise ConflictError("analysis controller lost concurrent admissions; retry from persisted state")


def bound_analysis(store: Store, batch: str) -> dict[str, str]:
    """Which analysis the batch protocol is bound to; the caller never chooses it.

    A pack binding selects the pack path. A legacy manual binding or a model
    application selects the legacy adapter recorded there. Controllers verify the
    binding again; this lookup only routes the request.
    """
    history = store.events()
    states = batch_index(store, history)
    require(batch in states, "unknown batch")
    protocol = states[batch]["plan"]["payload"]["protocol"]
    packs = [event for event in history if event["kind"] == "pack_binding"
             and event["payload"].get("protocol") == protocol]
    if packs:
        return dict(kind="pack", id=packs[0]["payload"]["pack_id"], binding=packs[0]["id"])
    manual = [event for event in history if event["kind"] == "domain_binding"
              and event["payload"].get("protocol") == protocol]
    if manual:
        return dict(kind="legacy_domain_binding", id=manual[0]["payload"]["adapter_id"],
                    binding=manual[0]["id"])
    applied = [event for event in history if event["kind"] == "agent_application"
               and event["payload"].get("protocol") == protocol]
    if applied:
        compiled = json.loads(store.read(applied[0]["payload"]["compilation"]))["compiled"]
        return dict(kind="legacy_model_application", id=compiled["domain"], binding=applied[0]["id"])
    raise GateError("batch protocol has no pack binding or frozen domain recipe")


def _assign(store: Store, history: list[dict[str, Any]], *, batch: str, analysis: dict[str, Any],
            planner: Actor, reviewer_actor: str, study_id: str, correlation_id: str
            ) -> dict[str, str] | None:
    """Assign review on the current basis; None means a concurrent writer won."""
    claim_id = analysis["payload"]["claim"]
    gate = Kernel(store, Actor("analysis-controller-observer", "observer"))._gate(history, claim_id)
    require(gate["passed"], "analysis claim no longer passes mechanical gate: "
            + "; ".join(gate["failures"]))

    def current(snapshot: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _matching(store, snapshot, claim_id, gate["basis_hash"], reviewer_actor)[0]

    assignments = current(history)
    require(len(assignments) <= 1, "duplicate reviewer assignments for analysis basis")
    if not assignments:
        context = dict(command_id=f"analysis-assign-{uuid4().hex}",
                       expected_revision=len(history), actor=planner.id, role=planner.role,
                       study_id=study_id, correlation_id=correlation_id,
                       causation_id=analysis["id"])
        request = dict(version=1, action="review.assign", payload=dict(
            claim=claim_id, reviewer_actor=reviewer_actor, expected_basis=gate["basis_hash"]))
        try:
            CommandService(store).execute(dict(context=context, request=request))
        except ConflictError:
            return None
        assignments = current(store.events())
        require(len(assignments) == 1, "review assignment did not persist")
        status = "awaiting_review"
    else:
        submission = submission_index(store, history).get(assignments[0]["id"])
        status = "review_recorded" if submission else "awaiting_review"
    return dict(status=status, batch=batch, analysis=analysis["id"], claim=claim_id,
                assignment=assignments[0]["id"], basis_hash=gate["basis_hash"],
                scientific_validity="not_assessed")


def advance_pack_analysis(store: Store, batch: str, *, planner: Actor, analyst: Actor,
                          reviewer_actor: str) -> dict[str, str]:
    """Pack path: hooks run outside the write transaction, then two persisted decisions.

    The pack is taken from the protocol's pack binding, never from the caller.
    Hooks are trusted local code in this process; the kernel re-verifies their
    envelopes, the pinned code and the claim-strength ceiling inside the command.
    """
    from .domain_packs import analysis_inputs, pack_analyses, require_live_pack, run_hooks
    require(analyst.role == "analyst", "batch analysis controller needs an analyst")
    require(planner.role == "planner", "batch analysis controller needs a planner")
    for value, label in ((planner.id, "planner"), (analyst.id, "analyst"), (reviewer_actor, "reviewer actor")):
        require_canonical_actor(value, label)
    for _ in range(3):
        history = store.events()
        states = batch_index(store, history)
        require(batch in states, "unknown batch")
        plan, settlement = states[batch]["plan"], states[batch]["settlement"]
        require(planner.id == plan["actor"], "controller planner differs from batch planner")
        require(settlement is not None and settlement["payload"]["status"] == "completed",
                "batch analysis requires completed technical settlement")
        origin = next((r for r in store.receipts() if batch in r["event_ids"]), None)
        require(origin is not None, "batch planning receipt is missing")
        study_id = origin["context"]["study_id"]
        correlation_id = origin["context"]["correlation_id"]
        existing = [event for event in pack_analyses(store, history).values()
                    if event["payload"]["batch"] == batch]
        require(len(existing) <= 1, "batch has multiple analysis tasks; choose one explicitly")
        if not existing:
            facts, context_, cas = analysis_inputs(store, history, batch)
            loaded = require_live_pack(store, facts["binding"])
            hooks = run_hooks(loaded, context_, cas)
            pin = facts["binding"]["payload"]
            context = dict(command_id=f"pack-analysis-{uuid4().hex}", expected_revision=len(history),
                           actor=analyst.id, role=analyst.role, study_id=study_id,
                           correlation_id=correlation_id, causation_id=settlement["id"])
            request = dict(version=1, action="pack.analyse", payload=dict(
                batch=batch, expected_settlement=settlement["id"], pack_id=pin["pack_id"],
                pack_version=pin["pack_version"], pack_code_digest=pin["pack_code_digest"],
                checks=hooks["checks"], recomputations=hooks["recomputations"],
                report=hooks["report"], statistical_report=hooks["statistical_report"],
                reviewer_actor=reviewer_actor))
            try:
                CommandService(store).execute(dict(context=context, request=request))
            except ConflictError:
                pass
            # Reload from receipts rather than trusting the in-memory report.
            continue
        analysis = existing[0]
        require(analysis["actor"] == analyst.id
                and analysis["payload"]["reviewer_actor"] == reviewer_actor,
                "persisted analysis actor/reviewer differs from requested controller policy")
        result = _assign(store, history, batch=batch, analysis=analysis, planner=planner,
                         reviewer_actor=reviewer_actor, study_id=study_id,
                         correlation_id=correlation_id)
        if result is not None:
            return result
    raise ConflictError("analysis controller lost concurrent admissions; retry from persisted state")
