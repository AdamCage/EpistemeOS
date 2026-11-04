"""Stage-only research control: one frozen contract, then resume and close.

The operator opens a stage and reads its close. Slot, dispatch, analysis and
review assignment are not separate operator commands. ``pack.analyse`` keeps
its payload; this path supplies ``reviewer_actor=stage-unreviewed`` and never
calls ``review.assign``. That label is not a scientific reviewer and is not
an approval. ``scientific_validity`` stays ``not_assessed``.

Isolation is the existing trusted local runner: this OS user, no sandbox.
Same-code replay is not fresh-seed repetition or new-data replication.
A missing number, an inconclusive report or a failed attempt is not success.
"""

from __future__ import annotations

import json
import math
from typing import Any
from uuid import uuid4

from .kernel import Actor, GateError, Kernel, finite, require, require_canonical_actor
from .search import COMPONENTS
from .store import ConflictError, Store


UNREVIEWED = "stage-unreviewed"
REVIEW = "not_requested"
KINDS = frozenset({"stage_open", "stage_close"})
ACTIONS = frozenset({"stage.open", "stage.close"})
CRITERION_KINDS = frozenset({"analysis_recorded", "primary_metric"})
METRIC_OPS = frozenset({">=", ">", "<=", "<", "=="})
DISPOSITIONS = frozenset({"criteria_met", "criteria_not_met", "stopped_budget", "blocked"})
ISOLATION = {
    "backend": "local_subprocess",
    "os_isolation": "none",
    "host_user": "same",
    "sandbox": False,
    "network_enforcement": "not_enforced",
    "filesystem_enforcement": "not_enforced",
    "independent_replication": False,
    "same_code_replay": True,
}
ISOLATION_NOTE = (
    "trusted local OS user in this process; no sandbox; same-code execution is "
    "not independent replication"
)
_OPEN_FIELDS = (
    "schema_version", "study_id", "question", "question_hash", "explanation_set",
    "explanation_set_hash", "pack_id", "preregistration", "preregistration_hash",
    "protocol", "protocol_hash", "attempt_ceiling", "roster_size", "criteria",
    "predecessor", "predecessor_hash", "isolation", "scientific_validity",
)
_CLOSE_FIELDS = (
    "schema_version", "study_id", "stage", "stage_hash", "disposition",
    "criteria_evaluations", "attempt_ledger", "protocol", "protocol_hash",
    "batch", "batch_hash", "settlement", "settlement_hash", "analysis",
    "analysis_hash", "claim", "claim_hash", "outcome", "inference_mode",
    "review", "history_head", "isolation", "scientific_validity",
)
_OPEN_REQUEST = frozenset({
    "question", "explanation_set", "pack_id", "preregistration",
    "attempt_ceiling", "criteria", "predecessor",
})


def _text(value: Any, label: str) -> str:
    require(type(value) is str and bool(value.strip()), f"invalid stage {label}")
    return value


def _history_head(history: list[dict[str, Any]]) -> str:
    return history[-1]["hash"] if history else "0" * 64


def _closes(history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {event["payload"]["stage"]: event for event in history if event["kind"] == "stage_close"}


def _opens(history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {event["id"]: event for event in history if event["kind"] == "stage_open"}


def _roster_size(protocol: dict[str, Any]) -> int:
    seeds = protocol["payload"]["seeds"]
    require(type(seeds) is list and seeds and all(type(item) is int for item in seeds),
            "stage protocol has no roster seeds")
    return 2 * len(seeds)


def _validate_criteria(criteria: Any) -> list[dict[str, Any]]:
    require(type(criteria) is list and criteria and len(criteria) == len(set(
        item["kind"] if type(item) is dict and type(item.get("kind")) is str else id(item)
        for item in criteria)), "stage criteria must be a nonempty list of unique kinds")
    frozen = []
    for item in criteria:
        require(type(item) is dict and type(item.get("kind")) is str and item["kind"] in CRITERION_KINDS,
                "unknown stage criterion")
        if item["kind"] == "analysis_recorded":
            require(set(item) == {"kind"}, "analysis_recorded takes no extra fields")
            frozen.append(dict(kind="analysis_recorded"))
            continue
        require(set(item) == {"kind", "name", "op", "threshold"},
                "primary_metric fields must be kind, name, op, threshold")
        name, op, threshold = item["name"], item["op"], item["threshold"]
        require(type(name) is str and bool(name.strip()) and len(name) <= 128,
                "invalid primary_metric name")
        require(type(op) is str and op in METRIC_OPS, "invalid primary_metric comparison")
        require(type(threshold) in (int, float) and math.isfinite(threshold),
                "primary_metric threshold must be a finite number")
        frozen.append(dict(kind="primary_metric", name=name, op=op, threshold=threshold))
    return frozen


def _compare(op: str, left: float, right: float) -> bool:
    if op == ">=":
        return left >= right
    if op == ">":
        return left > right
    if op == "<=":
        return left <= right
    if op == "<":
        return left < right
    return left == right


def _metric_numbers(statistical: dict[str, Any], name: str) -> list[float] | None:
    point = statistical.get("point_estimate")
    if type(point) is not dict or point.get("status") != "supplied":
        return None
    value = point.get("value")
    if type(value) is not dict or value.get("metric") != name:
        return None
    overall = value.get("value")
    if finite(overall):
        return [overall]
    rows = value.get("by_roster_unit")
    if type(rows) is not list or not rows:
        return None
    numbers = []
    for row in rows:
        if type(row) is not dict or not finite(row.get("value")):
            return None
        numbers.append(row["value"])
    return numbers


def _batches_for(store: Store, history: list[dict[str, Any]], protocol: str) -> list[str]:
    from .batch import _index as batch_index
    return [batch for batch, state in batch_index(store, history).items()
            if state["plan"]["payload"]["protocol"] == protocol]


def _analysis_for(store: Store, history: list[dict[str, Any]], batch: str | None
                  ) -> dict[str, Any] | None:
    if batch is None:
        return None
    from .domain_packs import pack_analyses
    rows = [event for event in pack_analyses(store, history).values()
            if event["payload"]["batch"] == batch]
    require(len(rows) <= 1, "stage protocol has multiple pack analyses; choose one explicitly")
    return rows[0] if rows else None


def _ledger(store: Store, history: list[dict[str, Any]], *, ceiling: int, roster_size: int,
            batch: str | None) -> dict[str, Any]:
    counts = dict(enqueued=0, completed=0, failed=0, unknown=0, blocked_dependency=0)
    if batch is not None:
        from .batch import batch_state
        for slot in batch_state(store, batch)["slots"]:
            if slot["job"] is not None or slot["status"] != "pending":
                counts["enqueued"] += 1
            key = slot["status"]
            if key in counts:
                counts[key] += 1
    return dict(ceiling=ceiling, roster_size=roster_size, launched=counts["enqueued"] > 0, **counts)


def _snapshot(store: Store, history: list[dict[str, Any]], opened: dict[str, Any]
              ) -> dict[str, Any]:
    protocol_id = opened["payload"]["protocol"]
    batches = _batches_for(store, history, protocol_id)
    require(len(batches) <= 1, "stage protocol has multiple batches; choose one explicitly")
    batch_id = batches[0] if batches else None
    settlement = analysis = claim = None
    outcome = inference = None
    unknown = failed = gate_failed = False
    if batch_id is not None:
        from .batch import batch_state
        state = batch_state(store, batch_id)
        settlement = (Kernel._get(history, state["settlement"], "batch_settlement")
                      if state["settlement"] else None)
        unknown = any(slot["status"] == "unknown" for slot in state["slots"])
        if settlement is not None:
            failed = settlement["payload"]["status"] != "completed"
        analysis = _analysis_for(store, history, batch_id)
        if analysis is not None:
            claim = Kernel._get(history, analysis["payload"]["claim"], "claim")
            outcome = claim["payload"]["outcome"]
            inference = claim["payload"]["inference_mode"]
            gate = Kernel(store, Actor("stage-observer", "observer"))._gate(history, claim["id"])
            gate_failed = not gate["passed"]
    protocol = Kernel._get(history, protocol_id, "protocol")
    return dict(protocol=protocol, batch=Kernel._get(history, batch_id, "batch_plan") if batch_id else None,
                settlement=settlement, analysis=analysis, claim=claim, outcome=outcome,
                inference_mode=inference, unknown=unknown, failed=failed, gate_failed=gate_failed,
                ledger=_ledger(store, history, ceiling=opened["payload"]["attempt_ceiling"],
                               roster_size=opened["payload"]["roster_size"], batch=batch_id))


def _evaluate_criterion(kind_spec: dict[str, Any], facts: dict[str, Any],
                        store: Store) -> dict[str, Any]:
    if kind_spec["kind"] == "analysis_recorded":
        met = (facts["settlement"] is not None
               and facts["settlement"]["payload"]["status"] == "completed"
               and facts["analysis"] is not None)
        return dict(kind="analysis_recorded", met=met,
                    outcome=facts["outcome"], inference_mode=facts["inference_mode"],
                    detail=("completed batch and admitted pack.analyse"
                            if met else "completed batch and admitted pack.analyse are required"))
    name, op, threshold = kind_spec["name"], kind_spec["op"], kind_spec["threshold"]
    if facts["outcome"] == "inconclusive":
        return dict(kind="primary_metric", met=False, name=name, op=op, threshold=threshold,
                    detail="inconclusive outcome does not satisfy primary_metric")
    if facts["analysis"] is None:
        return dict(kind="primary_metric", met=False, name=name, op=op, threshold=threshold,
                    detail="missing recomputed primary metric")
    statistical = json.loads(store.read(facts["analysis"]["payload"]["statistical_report"]))
    numbers = _metric_numbers(statistical, name)
    if numbers is None:
        return dict(kind="primary_metric", met=False, name=name, op=op, threshold=threshold,
                    detail="missing recomputed primary metric")
    met = all(_compare(op, number, threshold) for number in numbers)
    return dict(kind="primary_metric", met=met, name=name, op=op, threshold=threshold,
                observed=numbers,
                detail=("recomputed primary metric meets the frozen threshold" if met
                        else "recomputed primary metric does not meet the frozen threshold"))


def evaluate_close(store: Store, history: list[dict[str, Any]], opened: dict[str, Any]
                   ) -> dict[str, Any]:
    """Read-only disposition from the current history. This is not a write."""
    require(opened["kind"] == "stage_open", "stage close needs a stage.open record")
    require(opened["id"] not in _closes(history), "stage is already closed")
    p = opened["payload"]
    facts = _snapshot(store, history, opened)
    evaluations = [_evaluate_criterion(item, facts, store) for item in p["criteria"]]
    ledger = facts["ledger"]
    if facts["unknown"] or facts["failed"] or facts["gate_failed"]:
        if facts["unknown"]:
            reason = "an unknown attempt has no verified completion and is not rerun"
        elif facts["failed"]:
            reason = "the batch failed technically; the failed attempt is not rerun"
        else:
            reason = "the admitted analysis no longer passes the mechanical gate"
        disposition = "blocked"
    elif p["attempt_ceiling"] < p["roster_size"] and not ledger["launched"]:
        disposition = "stopped_budget"
        reason = "attempt ceiling does not cover the roster; no attempt was started"
    elif facts["analysis"] is None:
        if not p["protocol"]:
            disposition = "blocked"
            reason = "stage has no frozen protocol; the runner does not invent one"
        else:
            raise GateError("stage is not ready to close: no admitted analysis")
    else:
        disposition = "criteria_met" if all(item["met"] for item in evaluations) else "criteria_not_met"
        reason = ("every frozen criterion holds on the current bytes" if disposition == "criteria_met"
                  else "analysis is recorded and at least one frozen criterion does not hold")
    return dict(disposition=disposition, reason=reason, criteria_evaluations=evaluations,
                facts=facts, ledger=ledger)


def _open_payload(store: Store, history: list[dict[str, Any]], request: dict[str, Any],
                  study: str) -> dict[str, Any]:
    require(type(request) is dict and set(request) == _OPEN_REQUEST, "invalid stage.open fields")
    question = Kernel._get(history, _text(request["question"], "question"), "research_question")
    require(question["payload"]["study_id"] == study, "stage question is in another study")
    explanation = Kernel._get(history, _text(request["explanation_set"], "explanation_set"),
                              "explanation_set")
    require(explanation["payload"]["study_id"] == study, "stage explanation set is in another study")
    require(explanation["payload"]["question"] == question["id"],
            "stage explanation set does not belong to the named question")
    binding = Kernel._get(history, _text(request["preregistration"], "preregistration"),
                          "pack_binding")
    require(binding["payload"]["study_id"] == study, "stage preregistration is in another study")
    require(binding["payload"]["explanation_set"] == explanation["id"],
            "stage preregistration is bound to another explanation set")
    pack_id = _text(request["pack_id"], "pack_id")
    require(binding["payload"]["pack_id"] == pack_id, "stage pack_id differs from the preregistration")
    protocol = Kernel._get(history, binding["payload"]["protocol"], "protocol")
    require(binding["payload"]["protocol_hash"] == protocol["hash"],
            "stage preregistration refers to another protocol revision")
    ceiling = request["attempt_ceiling"]
    require(type(ceiling) is int and ceiling >= 0, "attempt_ceiling must be a nonnegative integer")
    criteria = _validate_criteria(request["criteria"])
    predecessor = request["predecessor"]
    predecessor_hash = None
    if predecessor is not None:
        prior = Kernel._get(history, _text(predecessor, "predecessor"), "stage_open")
        require(prior["payload"]["study_id"] == study, "predecessor stage is in another study")
        closed = _closes(history).get(prior["id"])
        require(closed is not None, "predecessor stage is not closed")
        predecessor_hash = prior["hash"]
    opens = [event for event in history if event["kind"] == "stage_open"
             and event["payload"]["preregistration"] == binding["id"]
             and event["id"] not in _closes(history)]
    require(not opens, "an open stage already covers this preregistration")
    return dict(schema_version=1, study_id=study, question=question["id"],
                question_hash=question["hash"], explanation_set=explanation["id"],
                explanation_set_hash=explanation["hash"], pack_id=pack_id,
                preregistration=binding["id"], preregistration_hash=binding["hash"],
                protocol=protocol["id"], protocol_hash=protocol["hash"],
                attempt_ceiling=ceiling, roster_size=_roster_size(protocol),
                criteria=criteria, predecessor=predecessor,
                predecessor_hash=predecessor_hash, isolation=dict(ISOLATION),
                scientific_validity="not_assessed")


def _close_payload(store: Store, history: list[dict[str, Any]], stage: str,
                   study: str) -> dict[str, Any]:
    opened = Kernel._get(history, _text(stage, "stage"), "stage_open")
    require(opened["payload"]["study_id"] == study, "stage.close study differs from the open record")
    head = _history_head(history)
    decision = evaluate_close(store, history, opened)
    facts = decision["facts"]

    def ref(event: dict[str, Any] | None) -> tuple[str | None, str | None]:
        return (event["id"], event["hash"]) if event is not None else (None, None)

    batch, batch_hash = ref(facts["batch"])
    settlement, settlement_hash = ref(facts["settlement"])
    analysis, analysis_hash = ref(facts["analysis"])
    claim, claim_hash = ref(facts["claim"])
    return dict(schema_version=1, study_id=study, stage=opened["id"], stage_hash=opened["hash"],
                disposition=decision["disposition"], criteria_evaluations=decision["criteria_evaluations"],
                attempt_ledger=decision["ledger"], protocol=opened["payload"]["protocol"],
                protocol_hash=opened["payload"]["protocol_hash"], batch=batch, batch_hash=batch_hash,
                settlement=settlement, settlement_hash=settlement_hash, analysis=analysis,
                analysis_hash=analysis_hash, claim=claim, claim_hash=claim_hash,
                outcome=facts["outcome"], inference_mode=facts["inference_mode"], review=REVIEW,
                history_head=head, isolation=dict(ISOLATION), scientific_validity="not_assessed")


def _index(store: Store, history: list[dict[str, Any]], *,
           receipts: list[dict[str, Any]] | None = None) -> None:
    """Each stage event must match the command that appended it."""
    source = store.receipts() if receipts is None else receipts
    relevant = [row for row in source if row["request"]["action"] in ACTIONS
                and row["after_revision"] <= len(history)]
    events = {event["id"]: event for event in history if event["kind"] in KINDS}
    seen: set[str] = set()
    for row in relevant:
        before = history[:row["before_revision"]]
        created = history[row["before_revision"]:row["after_revision"]]
        context, command = row["context"], row["request"]
        require(command["version"] == 1 and context["role"] == "planner"
                and len(created) == 1 and row["event_ids"] == [created[0]["id"]],
                "stage command needs one complete receipt")
        event = created[0]
        require(event["actor"] == context["actor"] and event["role"] == "planner",
                "stage actor differs from its command")
        if command["action"] == "stage.open":
            expected = _open_payload(store, before, command["payload"], context["study_id"])
            require(event["kind"] == "stage_open" and set(event["payload"]) == set(_OPEN_FIELDS)
                    and event["payload"] == expected
                    and row["result"]["stage"] == event["id"],
                    "stage.open record differs from its command")
        else:
            expected = _close_payload(store, before, command["payload"]["stage"], context["study_id"])
            require(event["kind"] == "stage_close" and set(event["payload"]) == set(_CLOSE_FIELDS)
                    and event["payload"] == expected
                    and row["result"]["close"] == event["id"],
                    "stage.close record differs from its command")
        seen.add(event["id"])
    require(seen == set(events), "stage record lacks its original command receipt")


def stage_index(store: Store, history: list[dict[str, Any]], *,
                receipts: list[dict[str, Any]] | None = None) -> None:
    _index(store, history, receipts=receipts)


class Stage:
    """Planner commands that freeze and close one mechanical research stage."""

    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def open(self, *, question: str, explanation_set: str, pack_id: str, preregistration: str,
             attempt_ceiling: int, criteria: list[dict[str, Any]],
             predecessor: str | None = None) -> dict[str, Any]:
        require(self.store._command_context is not None and self.actor.role == "planner",
                "stage.open requires a planner CommandService transaction")
        history = self.store.events()
        _index(self.store, history)
        payload = _open_payload(self.store, history, dict(
            question=question, explanation_set=explanation_set, pack_id=pack_id,
            preregistration=preregistration, attempt_ceiling=attempt_ceiling,
            criteria=criteria, predecessor=predecessor),
                                self.store._command_context["study_id"])
        stage = Kernel(self.store, self.actor)._write(history, "stage_open", payload, {"planner"})
        return dict(stage=stage, protocol=payload["protocol"], roster_size=payload["roster_size"],
                    scientific_validity="not_assessed")

    def close(self, *, stage: str) -> dict[str, Any]:
        require(self.store._command_context is not None and self.actor.role == "planner",
                "stage.close requires a planner CommandService transaction")
        history = self.store.events()
        _index(self.store, history)
        payload = _close_payload(self.store, history, stage, self.store._command_context["study_id"])
        close = Kernel(self.store, self.actor)._write(history, "stage_close", payload, {"planner"})
        return dict(close=close, stage=stage, disposition=payload["disposition"], review=REVIEW,
                    scientific_validity="not_assessed")


def _origin(store: Store, stage: str) -> dict[str, Any]:
    receipt = next((item for item in store.receipts() if stage in item["event_ids"]), None)
    require(receipt is not None, "stage.open receipt is missing")
    return receipt["context"]


def _execute(store: Store, origin: dict[str, Any], *, actor: str, role: str, action: str,
             payload: dict[str, Any], causation: str) -> Any:
    from .commands import CommandService
    context = dict(command_id=f"stage-{action}-{uuid4().hex}",
                   expected_revision=len(store.events()), actor=actor, role=role,
                   study_id=origin["study_id"], correlation_id=origin["correlation_id"],
                   causation_id=causation)
    return CommandService(store).execute(dict(context=context,
        request=dict(version=1, action=action, payload=payload)))


def stage_open(store: Store, *, actor: Actor, study: str, question: str, explanation_set: str,
               pack_id: str, preregistration: str, attempt_ceiling: int,
               criteria: list[dict[str, Any]], predecessor: str | None = None,
               command_id: str | None = None, correlation_id: str | None = None) -> dict[str, Any]:
    """Admit ``stage.open``. The returned id is the frozen contract, not success."""
    from .commands import CommandService
    require(actor.role == "planner", "stage.open needs a planner")
    require_canonical_actor(actor.id, "planner")
    context = dict(command_id=command_id or f"stage-open-{uuid4().hex}",
                   expected_revision=len(store.events()), actor=actor.id, role=actor.role,
                   study_id=study, correlation_id=correlation_id or f"stage-{uuid4().hex}",
                   causation_id=preregistration)
    return CommandService(store).execute(dict(context=context, request=dict(
        version=1, action="stage.open", payload=dict(
            question=question, explanation_set=explanation_set, pack_id=pack_id,
            preregistration=preregistration, attempt_ceiling=attempt_ceiling,
            criteria=criteria, predecessor=predecessor))))


def stage_status(store: Store, stage: str) -> dict[str, Any]:
    """Read-only progress. Closing is a separate persisted command."""
    history = store.events()
    opened = Kernel._get(history, stage, "stage_open")
    closed = _closes(history).get(stage)
    facts = _snapshot(store, history, opened)
    report = dict(stage=stage, status="closed" if closed else "open",
                  study=opened["payload"]["study_id"],
                  protocol=opened["payload"]["protocol"],
                  pack_id=opened["payload"]["pack_id"],
                  preregistration=opened["payload"]["preregistration"],
                  attempt_ceiling=opened["payload"]["attempt_ceiling"],
                  roster_size=opened["payload"]["roster_size"],
                  criteria=opened["payload"]["criteria"],
                  batch=facts["batch"]["id"] if facts["batch"] else None,
                  settlement=facts["settlement"]["id"] if facts["settlement"] else None,
                  analysis=facts["analysis"]["id"] if facts["analysis"] else None,
                  claim=facts["claim"]["id"] if facts["claim"] else None,
                  outcome=facts["outcome"], inference_mode=facts["inference_mode"],
                  review=REVIEW, attempt_ledger=facts["ledger"],
                  isolation=ISOLATION_NOTE, scientific_validity="not_assessed")
    if closed is None:
        return report
    p = closed["payload"]
    report.update(disposition=p["disposition"], close=closed["id"],
                  criteria_evaluations=p["criteria_evaluations"],
                  review=p["review"])
    return report


def _ensure_batch(store: Store, opened: dict[str, Any], origin: dict[str, Any],
                  *, executor: str, replicator: str) -> str:
    from .domain_packs import execution_fields
    history = store.events()
    existing = _batches_for(store, history, opened["payload"]["protocol"])
    if existing:
        require(len(existing) == 1, "stage protocol has multiple batches; choose one explicitly")
        return existing[0]
    protocol = opened["payload"]["protocol"]
    roster = opened["payload"]["roster_size"]
    tree = _execute(store, origin, actor=origin["actor"], role="planner",
                    action="search.register_tree", payload=dict(
                        weights={key: 1 for key in COMPONENTS}, cost_weight=0, budget=roster,
                        cost_unit="enqueued_attempt", max_nodes=1, max_depth=0, max_width=1,
                        max_selections=1, max_retries=0), causation=opened["id"])
    _execute(store, origin, actor=origin["actor"], role="planner", action="search.add_node",
             payload=dict(tree=tree, protocol=protocol, action="discriminate",
                          estimated_cost=roster,
                          components=dict(discrimination=1, uncertainty=1, coverage=1,
                                          invalidity_risk=0),
                          rationale="Stage executes the already frozen pack protocol; "
                                    "this node is a mechanical roster, not a tournament judgment"),
             causation=opened["id"])
    selection = _execute(store, origin, actor=origin["actor"], role="planner",
                         action="search.select_next", payload=dict(tree=tree),
                         causation=opened["id"])
    require(selection.get("node") is not None, "stage search did not select the frozen protocol")
    binding = Kernel._get(store.events(), opened["payload"]["preregistration"], "pack_binding")
    return _execute(store, origin, actor=origin["actor"], role="planner", action="batch.plan",
                    payload=dict(selection=selection["id"], executor=executor,
                                 replicator=replicator, **execution_fields(store, binding)),
                    causation=opened["id"])


def _admit_analysis(store: Store, batch: str, *, analyst: Actor, origin: dict[str, Any],
                    causation: str) -> dict[str, Any]:
    """Admit pack.analyse with the unreviewed label. Do not assign review."""
    from .domain_packs import analysis_inputs, pack_analyses, require_live_pack, run_hooks
    require(analyst.role == "analyst", "stage analysis needs an analyst")
    require_canonical_actor(analyst.id, "analyst")
    for _ in range(3):
        history = store.events()
        existing = [event for event in pack_analyses(store, history).values()
                    if event["payload"]["batch"] == batch]
        require(len(existing) <= 1, "batch has multiple analysis tasks; choose one explicitly")
        if existing:
            analysis = existing[0]
            require(analysis["payload"]["reviewer_actor"] == UNREVIEWED,
                    "persisted analysis reviewer differs from stage-unreviewed")
            return analysis
        from .batch import batch_state
        settlement = Kernel._get(history, batch_state(store, batch)["settlement"], "batch_settlement")
        facts, context, cas = analysis_inputs(store, history, batch)
        loaded = require_live_pack(store, facts["binding"])
        hooks = run_hooks(loaded, context, cas)
        pin = facts["binding"]["payload"]
        try:
            _execute(store, origin, actor=analyst.id, role="analyst", action="pack.analyse",
                     payload=dict(batch=batch, expected_settlement=settlement["id"],
                                  pack_id=pin["pack_id"], pack_version=pin["pack_version"],
                                  pack_code_digest=pin["pack_code_digest"], checks=hooks["checks"],
                                  recomputations=hooks["recomputations"], report=hooks["report"],
                                  statistical_report=hooks["statistical_report"],
                                  reviewer_actor=UNREVIEWED),
                     causation=causation)
        except ConflictError:
            continue
    raise ConflictError("stage analysis lost concurrent admissions; retry from persisted state")


def _close_now(store: Store, stage: str, origin: dict[str, Any]) -> dict[str, Any]:
    history = store.events()
    opened = Kernel._get(history, stage, "stage_open")
    if stage in _closes(history):
        return stage_status(store, stage)
    head = _history_head(history)
    evaluate_close(store, history, opened)
    if _history_head(store.events()) != head:
        raise GateError("persisted state changed before the stage close")
    result = _execute(store, origin, actor=origin["actor"], role="planner",
                      action="stage.close", payload=dict(stage=stage), causation=stage)
    report = stage_status(store, stage)
    report.update(close=result["close"], disposition=result["disposition"])
    return report


def stage_run(store: Store, stage: str, *, analyst: Actor, executor: str,
              replicator: str) -> dict[str, Any]:
    """Resume mechanical steps until the stage is terminal, then close.

    The caller does not name the next action. A failed or unknown attempt is
    not relaunched. Close re-reads the chain head and does not write a stale
    disposition.
    """
    from .batch_controller import advance_batch
    require(analyst.role == "analyst", "stage run needs an analyst")
    for value, label in ((analyst.id, "analyst"), (executor, "executor"),
                         (replicator, "replicator")):
        require_canonical_actor(value, label)
    origin = _origin(store, stage)
    opened = Kernel._get(store.events(), stage, "stage_open")
    if not opened["payload"]["protocol"]:
        return _close_now(store, stage, origin)
    for _ in range(3):
        history = store.events()
        if stage in _closes(history):
            return stage_status(store, stage)
        opened = Kernel._get(history, stage, "stage_open")
        ceiling, roster = opened["payload"]["attempt_ceiling"], opened["payload"]["roster_size"]
        facts = _snapshot(store, history, opened)
        if ceiling < roster and not facts["ledger"]["launched"]:
            try:
                return _close_now(store, stage, origin)
            except ConflictError:
                continue
        if facts["unknown"] or facts["failed"] or facts["gate_failed"]:
            try:
                return _close_now(store, stage, origin)
            except ConflictError:
                continue
        if facts["analysis"] is not None:
            try:
                return _close_now(store, stage, origin)
            except ConflictError:
                continue
        try:
            batch = facts["batch"]["id"] if facts["batch"] else _ensure_batch(
                store, opened, origin, executor=executor, replicator=replicator)
            from .batch import batch_state
            state = batch_state(store, batch)
            if state["settlement"] is None:
                state = advance_batch(store, batch)
            if state["status"] == "unknown" or (
                    state["settlement"] is not None and state["status"] != "completed"):
                return _close_now(store, stage, origin)
            if state["settlement"] is None:
                continue
            if state["status"] == "completed" and _analysis_for(
                    store, store.events(), batch) is None:
                _admit_analysis(store, batch, analyst=analyst, origin=origin,
                                causation=state["settlement"])
            return _close_now(store, stage, origin)
        except ConflictError:
            continue
    raise ConflictError("stage runner lost concurrent admissions; retry from persisted state")
