"""Advance a study by at most one already-legal mechanical step.

``next_action`` only recommends. This controller reads the persisted history,
chooses the step itself, and never trusts a caller-supplied next action.
With an explicit attempt budget it may persist one command the kernel already
admits. Without a budget, or without ``apply``, it only reports. The report
is not an event.

It does not invent a protocol, choose among competing experiments, call a
model, submit a review, resolve an obligation, raise a claim outcome, or
build a paper. ``scientific_validity`` stays ``not_assessed``. Stopping is
success of the controller. There is no network, LLM, GPU, or publication.

Isolation of an applied launch is the existing trusted local runner: this OS
user, no sandbox. A failed or unknown attempt is not relaunched.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .analysis_controller import _matching, analysis_state, bound_analysis
from .batch import _index as batch_index
from .batch import batch_state
from .commands import CommandService
from .execution import _envelope, _identity, _index as execution_index, _workspace
from .kernel import Actor, GateError, Kernel, require_canonical_actor
from .review_admission import admission
from .store import ConflictError, IntegrityError, Store, canonical


ISOLATION = ("trusted local OS user in this process; no sandbox, network, LLM, or GPU; "
             "a launch uses the existing local runner and is not an independent replication")
_TERMINAL = {"completed", "failed", "cancelled", "blocked_dependency"}
_FIXTURE_NOTE = ("paper_candidate: the counted approval is a synthetic fixture opinion, "
                 "not a scientific result; scientific_validity remains not_assessed")
_PAPER_NOTE = ("paper_candidate: a recorded local approval does not set scientific_validity; "
               "human release and venue review are still required")
_MISSING_BUDGET = "missing_budget: no attempt cap was supplied, so this invocation only reports"
_BUDGET_EXHAUSTED = ("budget_exhausted: this invocation's cap on newly executed attempts is "
                     "exhausted; no further attempt was started")
_UNKNOWN = "blocked: an unknown attempt has no verified completion and is not rerun"
_OBLIGATION = ("open_obligation: a typed obligation is open; the controller does not design "
               "a follow-up or resolve it")
_VETO = ("open_veto: an open negative opinion binds this claim; the controller does not "
         "withdraw it or submit a review")
_HUMAN_EXPERIMENT = ("human_scientific_input: a new experiment has to be chosen by a human; "
                     "the controller does not invent a protocol or choose among competing experiments")
_HUMAN_COMPETING = ("human_scientific_input: more than one open batch or claim is eligible; "
                    "the controller does not choose among competing experiments")
_HUMAN_VERDICT = ("human_scientific_input: a review verdict is required; the controller does "
                  "not submit a review")
_HUMAN_CONFIRMATORY = ("human_scientific_input: confirmatory interpretation requires a human "
                       "review verdict; the controller does not raise the claim outcome")
_HUMAN_FOLLOWUP = ("human_scientific_input: follow-up design stays with a human; the controller "
                   "does not prepare a protocol or a batch for it")
_FAILED_BATCH = ("blocked: the batch failed technically; the failed attempt is not rerun and "
                 "the claim outcome is not raised")
_ACTORS = ("blocked: analysis admission needs caller-declared analyst and reviewer ids; "
           "the controller does not invent them")


@dataclass(frozen=True)
class _Step:
    recommendation: str
    reason: str
    stop: str | None = None
    consumes_attempt: bool = False
    fixture_approval: bool = False
    kind: str | None = None
    batch: str | None = None
    slot: str | None = None
    job: str | None = None


def cycle_step(store: Store, *, study: str | None = None, claim: str | None = None,
               apply: bool = False, budget: int | None = None,
               analyst: str | None = None, reviewer: str | None = None) -> dict[str, Any]:
    """Report the next mechanical step, and apply it only under an explicit budget.

    ``budget`` caps newly executed attempts in this invocation. Admitting an
    attempt (``batch.enqueue_slot``) or launching one (``execution.dispatch``
    or ``execution.dispatch_v2``) consumes one. Finalizing a dispatch that
    already exists does not. A missing budget forces a report even when
    ``apply`` is set.
    """
    if type(apply) is not bool:
        raise ValueError("apply must be a boolean")
    if (study is None) == (claim is None):
        raise ValueError("cycle step needs exactly one of study or claim")
    if budget is not None and (type(budget) is not int or budget < 0):
        raise ValueError("budget must be a nonnegative integer when supplied")
    for value, label in ((analyst, "analyst"), (reviewer, "reviewer")):
        if value is not None:
            require_canonical_actor(value, label)
    step = _decide(store, study=study, claim=claim)
    subject = {"kind": "claim" if claim is not None else "study", "id": claim or study}
    report = dict(subject=subject, recommendation=step.recommendation,
                  recommendation_reason=step.reason, stop=step.stop, reason=step.reason,
                  applied=False, report_only=not apply, scientific_validity="not_assessed",
                  budget=dict(supplied=budget, consumed_attempts=0), command=None,
                  fixture_approval=step.fixture_approval, isolation=ISOLATION)
    if apply and budget is None and step.stop is None:
        report.update(stop="missing_budget", reason=_MISSING_BUDGET, report_only=True)
        return report
    if not apply or step.stop is not None:
        return report
    if step.kind in {"admit"} and (analyst is None or reviewer is None):
        report.update(stop="blocked", reason=_ACTORS)
        return report
    if reviewer is not None and step.kind == "assign":
        named = _analysis_reviewer(store, step.batch)
        if reviewer != named:
            report.update(stop="blocked",
                          reason="blocked: caller reviewer differs from the reviewer named "
                                 "by the admitted analysis")
            return report
    if step.consumes_attempt and budget < 1:
        report.update(stop="budget_exhausted", reason=_BUDGET_EXHAUSTED)
        return report
    before_events = len(store.events())
    before_ids = {item["command_id"] for item in store.receipts()}
    try:
        fresh = _decide(store, study=study, claim=claim)
        if (fresh.kind, fresh.batch, fresh.slot, fresh.job, fresh.stop) != (
                step.kind, step.batch, step.slot, step.job, step.stop):
            raise GateError("persisted state changed before the cycle transition")
        _perform(store, fresh, analyst=analyst, reviewer=reviewer)
    except (GateError, ValueError, IntegrityError, ConflictError, OSError) as exc:
        report.update(stop="blocked", reason=f"blocked: {exc}",
                      applied=len(store.events()) != before_events)
        return report
    new = [item for item in store.receipts() if item["command_id"] not in before_ids]
    if len(new) != 1:
        report.update(stop="blocked",
                      reason="blocked: the cycle step did not persist exactly one command",
                      applied=len(store.events()) != before_events)
        return report
    report.update(applied=True, report_only=False,
                  budget=dict(supplied=budget, consumed_attempts=1 if step.consumes_attempt else 0),
                  command=dict(action=new[0]["request"]["action"], command_id=new[0]["command_id"]))
    return report


def _decide(store: Store, *, study: str | None, claim: str | None) -> _Step:
    history, protocol_ids, claims = _scope(store, study=study, claim=claim)
    decisions = [(event, Kernel(store, Actor("cycle-observer", "observer"))._next_action(history, event["id"]))
                 for event in claims]
    if any(item.get("obligations") for _, item in decisions):
        return _stop("open_obligation", _OBLIGATION)
    if any(item["action"] == "replan" for _, item in decisions):
        return _stop("open_veto", _VETO)
    batches = _batches(store, history, protocol_ids)
    unsettled = [item for item in batches if item["settlement"] is None]
    if any(item["action"] == "paper_candidate" for _, item in decisions):
        if unsettled or any(item["action"] != "paper_candidate" for _, item in decisions):
            return _stop("human_scientific_input", _HUMAN_COMPETING)
        return _paper(store, history, [event for event, item in decisions])
    if len(unsettled) > 1:
        return _stop("human_scientific_input", _HUMAN_COMPETING)
    if len(unsettled) == 1:
        return _batch_step(store, history, unsettled[0])
    if _unbatched_followup(history, protocol_ids, batches):
        return _stop("human_scientific_input", _HUMAN_FOLLOWUP)
    completed = [item for item in batches if item["status"] == "completed"]
    failed = [item for item in batches if item["settlement"] is not None and item["status"] != "completed"]
    mechanical: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    waiting_review: list[dict[str, Any]] = []
    for summary in completed:
        state = analysis_state(store, summary["batch"])
        status = state["status"]
        if status in {"awaiting_analysis", "awaiting_assignment"}:
            mechanical.append((status, summary, state))
        elif status in {"stale_evidence", "repair_evidence", "awaiting_settlement"}:
            mechanical.append(("blocked", summary, state))
        elif status in {"awaiting_review", "review_recorded"}:
            waiting_review.append(state)
    if len(mechanical) > 1:
        return _stop("human_scientific_input", _HUMAN_COMPETING)
    if len(mechanical) == 1:
        status, summary, state = mechanical[0]
        if status == "blocked":
            failures = _gate_failures(state)
            reason = "blocked: " + ("; ".join(failures) if failures else
                                    "the admitted analysis no longer passes the mechanical gate")
            return _stop("blocked", reason)
        if status == "awaiting_analysis":
            try:
                kind = bound_analysis(store, summary["batch"])["kind"]
            except GateError as exc:
                return _stop("blocked", f"blocked: {exc}")
            action = "pack.analyse" if kind == "pack" else "analysis.apply"
            return _Step(recommendation=action,
                         reason=f"{action}: the completed batch has no admitted analysis; "
                                "the pinned adapter admits it without choosing an experiment",
                         kind="admit", batch=summary["batch"])
        return _Step(recommendation="review.assign",
                     reason="review.assign: the admitted analysis names a reviewer and the "
                            "assignment policy still requires an assignment",
                     kind="assign", batch=summary["batch"])
    if len(waiting_review) > 1:
        return _stop("human_scientific_input", _HUMAN_COMPETING)
    if len(waiting_review) == 1:
        return _verdict(history, waiting_review[0])
    if failed:
        return _stop("blocked", _FAILED_BATCH)
    if any(item["action"] == "repair_evidence" for _, item in decisions):
        reasons = [reason for _, item in decisions if item["action"] == "repair_evidence"
                   for reason in item.get("reasons", [])]
        return _stop("blocked", "blocked: " + ("; ".join(reasons) if reasons else
                                               "mechanical evidence does not pass the gate"))
    if any(item["action"] == "superseded" for _, item in decisions):
        return _stop("human_scientific_input",
                     "human_scientific_input: a reviewed replacement is recorded; the controller "
                     "does not treat it as a new result or build a paper")
    return _stop("human_scientific_input", _HUMAN_EXPERIMENT)


def _perform(store: Store, step: _Step, *, analyst: str | None, reviewer: str | None) -> None:
    if step.kind == "enqueue":
        _enqueue(store, step.batch, step.slot)
    elif step.kind == "launch":
        _launch(store, step.job)
    elif step.kind == "finalize":
        from .execution import reconcile_job
        state = reconcile_job(store, step.job)
        if state["status"] == "unknown":
            raise GateError("unknown attempt has no verified completion and is not rerun")
    elif step.kind == "settle":
        _settle(store, step.batch)
    elif step.kind == "admit":
        _admit(store, step.batch, analyst=analyst, reviewer=reviewer)
    elif step.kind == "assign":
        _assign(store, step.batch)
    else:
        raise GateError("cycle step has no legal command to apply")


def _scope(store: Store, *, study: str | None, claim: str | None
           ) -> tuple[list[dict[str, Any]], set[str], list[dict[str, Any]]]:
    history = store.events()
    if claim is not None:
        event = Kernel._get(history, claim, "claim")
        protocol_id = event["payload"]["protocol"]
        protocol = Kernel._get(history, protocol_id, "protocol")
        bound = protocol["payload"].get("planning", {}).get("study_id")
        if study is not None and study != bound:
            raise ValueError("claim does not belong to the supplied study")
        return history, {protocol_id}, [event]
    questions = [event for event in history if event["kind"] == "research_question"
                 and event["payload"].get("study_id") == study]
    protocols = [event for event in history if event["kind"] == "protocol"
                 and event["payload"].get("planning", {}).get("study_id") == study]
    if not questions and not protocols:
        raise ValueError("unknown study")
    protocol_ids = {event["id"] for event in protocols}
    claims = [event for event in history if event["kind"] == "claim"
              and event["payload"]["protocol"] in protocol_ids]
    return history, protocol_ids, claims


def _batches(store: Store, history: list[dict[str, Any]], protocol_ids: set[str]) -> list[dict[str, Any]]:
    if not protocol_ids:
        return []
    selected = []
    for batch_id, state in batch_index(store, history).items():
        if state["plan"]["payload"]["protocol"] in protocol_ids:
            selected.append(batch_state(store, batch_id))
    return selected


def _batch_step(store: Store, history: list[dict[str, Any]], summary: dict[str, Any]) -> _Step:
    batch = summary["batch"]
    executions = execution_index(store, history) if any(slot["job"] for slot in summary["slots"]) else {}
    for slot in summary["slots"]:
        if slot["status"] != "unknown":
            continue
        if _completion_ready(store, executions[slot["job"]]):
            return _Step(recommendation="execution.finalize",
                         reason="execution.finalize: a dispatched attempt has a local completion "
                                "to admit; this is not a new launch and not a retry",
                         kind="finalize", batch=batch, job=slot["job"])
        return _stop("blocked", _UNKNOWN)
    for slot in summary["slots"]:
        if slot["status"] == "pending":
            return _Step(recommendation="batch.enqueue_slot",
                         reason="batch.enqueue_slot: the prepared batch has one pending slot; "
                                "admitting it consumes one attempt and does not choose an experiment",
                         consumes_attempt=True, kind="enqueue", batch=batch, slot=slot["slot"])
        if slot["status"] == "queued":
            version = executions[slot["job"]]["spec"]["schema_version"]
            action = "execution.dispatch_v2" if version == 2 else "execution.dispatch"
            return _Step(recommendation=action,
                         reason=f"{action}: one queued attempt can be launched; a failed or "
                                "unknown result is not relaunched",
                         consumes_attempt=True, kind="launch", batch=batch, job=slot["job"])
    if summary["slots"] and all(slot["status"] in _TERMINAL for slot in summary["slots"]):
        if any(slot["status"] != "completed" for slot in summary["slots"]):
            reason = ("batch.settle: every slot is terminal, including a failed attempt that "
                      "is not rerun; settlement does not raise a claim outcome")
        else:
            reason = "batch.settle: every slot is technically completed; settlement is not a scientific result"
        return _Step(recommendation="batch.settle", reason=reason, kind="settle", batch=batch)
    return _stop("blocked", "blocked: the prepared batch has no legal mechanical step")


def _completion_ready(store: Store, state: dict[str, Any]) -> bool:
    if state["dispatch"] is None or state["finalized"] is not None:
        return False
    workspace = _workspace(store, state)
    relative = Path("control") / "completion.json" if state["spec"]["schema_version"] == 2 else Path("completion.json")
    path = workspace / relative
    return path.is_file() and not path.is_symlink()


def _paper(store: Store, history: list[dict[str, Any]], claims: list[dict[str, Any]]) -> _Step:
    projection = admission(store, history)
    observer = Kernel(store, Actor("cycle-observer", "observer"))
    fixture = True
    counted = False
    for event in claims:
        basis = observer._gate(history, event["id"])["basis_hash"]
        approvals, _ = projection.approvals(event["id"], basis)
        counted = counted or bool(approvals)
        fixture = fixture and bool(approvals) and all(_fixture_opinion(item) for item in approvals)
    if counted and fixture:
        return _stop("paper_candidate", _FIXTURE_NOTE, fixture_approval=True)
    return _stop("paper_candidate", _PAPER_NOTE, fixture_approval=False)


def _fixture_opinion(event: dict[str, Any]) -> bool:
    """A recorded rationale that already says the opinion is a fixture, not a grade of it."""
    text = event["payload"].get("rationale", "")
    if type(text) is not str:
        return False
    folded = text.casefold()
    return "fixture" in folded and "no scientific review" in folded


def _verdict(history: list[dict[str, Any]], state: dict[str, Any]) -> _Step:
    rows = state.get("analyses") or []
    if len(rows) != 1:
        return _stop("human_scientific_input", _HUMAN_COMPETING)
    claim = next(event for event in history if event["id"] == rows[0]["claim"])
    if claim["payload"].get("inference_mode") == "confirmatory":
        return _stop("human_scientific_input", _HUMAN_CONFIRMATORY)
    return _stop("human_scientific_input", _HUMAN_VERDICT)


def _gate_failures(state: dict[str, Any]) -> list[str]:
    rows = state.get("analyses") or []
    failures = []
    for row in rows:
        gate = row.get("mechanical_gate") or {}
        failures.extend(gate.get("failures") or [])
    return failures


def _unbatched_followup(history: list[dict[str, Any]], protocol_ids: set[str],
                        batches: list[dict[str, Any]]) -> bool:
    batched = {item["protocol"] for item in batches}
    followups = {event["payload"]["protocol"] for event in history
                 if event["kind"] == "replan_followup" and event["payload"].get("protocol") in protocol_ids}
    return bool(followups - batched)


def _analysis_reviewer(store: Store, batch: str | None) -> str:
    state = analysis_state(store, batch)
    rows = state.get("analyses") or []
    if len(rows) != 1:
        raise GateError("analysis assignment needs exactly one admitted analysis")
    return rows[0]["reviewer_actor"]


def _origin(store: Store, batch: str) -> dict[str, Any]:
    receipt = next((item for item in store.receipts() if batch in item["event_ids"]), None)
    if receipt is None:
        raise GateError("batch planning receipt is missing")
    return receipt["context"]


def _context(origin: dict[str, Any], *, actor: str, role: str, revision: int,
             causation: str, action: str) -> dict[str, Any]:
    return dict(command_id=f"cycle-{action}-{uuid4().hex}", expected_revision=revision,
                actor=actor, role=role, study_id=origin["study_id"],
                correlation_id=origin["correlation_id"], causation_id=causation)


def _execute(store: Store, origin: dict[str, Any], *, actor: str, role: str,
             action: str, payload: dict[str, Any], causation: str) -> None:
    context = _context(origin, actor=actor, role=role, revision=len(store.events()),
                       causation=causation, action=action)
    CommandService(store).execute(dict(context=context, request=dict(version=1, action=action, payload=payload)))


def _enqueue(store: Store, batch: str, slot: str) -> None:
    summary = batch_state(store, batch)
    spec = next((item for item in summary["slots"] if item["slot"] == slot), None)
    if spec is None or spec["status"] != "pending":
        raise GateError("batch slot is no longer a pending attempt")
    _execute(store, _origin(store, batch), actor=spec["actor"], role=spec["role"],
             action="batch.enqueue_slot", payload=dict(batch=batch, slot=slot), causation=batch)


def _settle(store: Store, batch: str) -> None:
    summary = batch_state(store, batch)
    if summary["settlement"] is not None or not all(slot["status"] in _TERMINAL for slot in summary["slots"]):
        raise GateError("batch is not ready for technical settlement")
    plan = Kernel._get(store.events(), batch, "batch_plan")
    _execute(store, _origin(store, batch), actor=plan["actor"], role="planner",
             action="batch.settle", payload=dict(batch=batch), causation=batch)


def _launch(store: Store, job: str) -> None:
    """Persist one dispatch, then run the existing local worker. Do not finalize here."""
    state = execution_index(store, store.events())[job]
    if state["dispatch"] is not None:
        raise GateError("attempt is already dispatched and is not relaunched")
    version = state["spec"]["schema_version"]
    if version == 2:
        from . import execution_locked as locked
        envelope = _envelope(store, state, "execution.dispatch_v2", dict(
            job=job, workspace_token=uuid4().hex, workspace_root=str(locked.default_root())))
        CommandService(store).execute(envelope)
        state = execution_index(store, store.events())[job]
        path = _workspace(store, state)
        locked.materialize(store, state["spec"], state["job"]["payload"]["specification"],
                           _identity(state), path)
        locked.spawn(path)
        return
    envelope = _envelope(store, state, "execution.dispatch", dict(job=job, workspace_token=uuid4().hex))
    CommandService(store).execute(envelope)
    state = execution_index(store, store.events())[job]
    workspace = _workspace(store, state)
    workspace.mkdir(parents=True, exist_ok=False)
    for name, key in state["spec"]["expected_inputs"].items():
        (workspace / name).write_bytes(store.read(key))
    (workspace / "spec.json").write_bytes(store.read(state["job"]["payload"]["specification"]))
    (workspace / "identity.json").write_bytes(canonical(_identity(state)))
    worker = Path(__file__).with_name("runner_backend.py")
    process = subprocess.Popen([sys.executable, str(worker), "--workspace", str(workspace)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    process.wait()


def _admit(store: Store, batch: str, *, analyst: str, reviewer: str) -> None:
    require_canonical_actor(analyst, "analyst")
    require_canonical_actor(reviewer, "reviewer")
    history = store.events()
    summary = batch_state(store, batch)
    if summary["status"] != "completed" or analysis_state(store, batch)["status"] != "awaiting_analysis":
        raise GateError("batch analysis is no longer the legal step")
    bound = bound_analysis(store, batch)
    origin = _origin(store, batch)
    settlement = Kernel._get(history, summary["settlement"], "batch_settlement")
    if bound["kind"] == "pack":
        from .domain_packs import analysis_inputs, require_live_pack, run_hooks
        facts, context, cas = analysis_inputs(store, history, batch)
        loaded = require_live_pack(store, facts["binding"])
        hooks = run_hooks(loaded, context, cas)
        pin = facts["binding"]["payload"]
        payload = dict(batch=batch, expected_settlement=settlement["id"], pack_id=pin["pack_id"],
                       pack_version=pin["pack_version"], pack_code_digest=pin["pack_code_digest"],
                       checks=hooks["checks"], recomputations=hooks["recomputations"],
                       report=hooks["report"], statistical_report=hooks["statistical_report"],
                       reviewer_actor=reviewer)
        action = "pack.analyse"
    else:
        from .domains.registry import legacy_analysis_adapter
        adapter, source = legacy_analysis_adapter(bound["id"])
        proposal = adapter.propose(store, batch_index(store, history)[batch])
        if (proposal.get("adapter_id"), proposal.get("adapter_version")) != (
                adapter.adapter_id, adapter.adapter_version):
            raise GateError("analysis proposal differs from the registered adapter identity")
        payload = dict(batch=batch, expected_settlement=settlement["id"], proposal=proposal,
                       adapter_source_digest=store.put(source), reviewer_actor=reviewer)
        action = "analysis.apply"
    _execute(store, origin, actor=analyst, role="analyst", action=action, payload=payload,
             causation=settlement["id"])


def _assign(store: Store, batch: str) -> None:
    history = store.events()
    state = analysis_state(store, batch)
    if state["status"] != "awaiting_assignment":
        raise GateError("review assignment is no longer required")
    rows = state.get("analyses") or []
    if len(rows) != 1:
        raise GateError("review assignment needs exactly one admitted analysis")
    row = rows[0]
    reviewer = row["reviewer_actor"]
    gate = Kernel(store, Actor("cycle-observer", "observer"))._gate(history, row["claim"])
    if not gate["passed"]:
        raise GateError("analysis claim no longer passes the mechanical gate")
    usable, _ = _matching(store, history, row["claim"], gate["basis_hash"], reviewer)
    if usable:
        raise GateError("a usable review assignment is already recorded")
    plan = Kernel._get(history, batch, "batch_plan")
    analysis_id = row["analysis"]
    _execute(store, _origin(store, batch), actor=plan["actor"], role="planner",
             action="review.assign",
             payload=dict(claim=row["claim"], reviewer_actor=reviewer, expected_basis=gate["basis_hash"]),
             causation=analysis_id)


def _stop(stop: str, reason: str, *, fixture_approval: bool = False) -> _Step:
    return _Step(recommendation=stop, reason=reason, stop=stop, fixture_approval=fixture_approval)
