"""Receipted research sessions, policies and role handoffs over the existing Store."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any

from .codex_provider import validate_provider
from .execution import _object
from .kernel import Actor, Kernel, require
from .research_agents import INSTRUCTIONS, fixture_answer, inspect_worker, validate_answer
from .store import Store, canonical, digest


KINDS = frozenset({"research_session", "research_step"})
ACTIONS = frozenset({"research.begin", "research.record"})
QUESTIONS = (
    "Какой вывод сделал бы исследование ценным и для кого?",
    "Какое наблюдение или контроль могли бы опровергнуть гипотезу?",
    "Какие данные, ограничения и неразрешённые вопросы следует учитывать?",
)
_LIMITS = {"stages": 12, "attempts": 96, "model_calls": 96, "literature_calls": 16,
           "wall_seconds": 86400, "operation_seconds": 600}
_RECIPE = {"label", "pack_id", "pack_version", "pack_code_digest", "parameters", "host_inputs",
           "capture", "phase", "purpose"}
_CONTRACT = {"budget", "scope", "candidates", "literature", "provider", "environment", "instructions",
             "release", "release_root"}
_ROLES = {"analyst", "critic", "developer", "tester", "reviewer"}
_NOTE_ROLES = {"reply": "author", "accept": "author"}
_LABEL = r"[a-z0-9][a-z0-9_-]{0,47}"


def _text(value: Any, limit: int = 2048) -> bool:
    return type(value) is str and 0 < len(value.strip()) and len(value) <= limit and "\x00" not in value


def _key(value: Any) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _read(store: Store, key: str) -> dict[str, Any]:
    require(_key(key), "expected an immutable artifact digest")
    return _object(store.read(key))


def _contract(store: Store, value: dict[str, Any]) -> None:
    from .research_literature import validate_literature_action

    require(type(value) is dict and set(value) == _CONTRACT, "invalid research contract fields")
    budget = value["budget"]
    require(type(budget) is dict and set(budget) == set(_LIMITS), "research contract needs every budget limit")
    for key, ceiling in _LIMITS.items():
        floor = 0 if key in {"model_calls", "literature_calls", "attempts"} else 1
        require(type(budget[key]) is int and floor <= budget[key] <= ceiling, f"invalid {key} budget")
    require(budget["operation_seconds"] <= budget["wall_seconds"], "operation limit exceeds study wall limit")
    scope = value["scope"]
    require(type(scope) is dict and 1 <= len(scope) <= 16
            and all(_text(k, 128) and _text(v, 512) for k, v in scope.items()), "invalid research scope")
    recipes = value["candidates"]
    require(type(recipes) is list and 1 <= len(recipes) <= 12, "1 to 12 bounded research recipes required")
    seen_labels, seen_recipes, confirmatory_captures = set(), set(), set()
    for item in recipes:
        require(type(item) is dict and set(item) == _RECIPE, "invalid research recipe fields")
        label = item["label"]
        require(type(label) is str and re.fullmatch(_LABEL, label) and label not in seen_labels,
                "research recipe labels must be unique")
        seen_labels.add(label)
        require(_text(item["pack_id"], 128) and _text(item["pack_version"], 32)
                and _key(item["pack_code_digest"]) and _text(item["purpose"]), "invalid pinned recipe metadata")
        require(item["phase"] in {"exploratory", "confirmatory"}, "invalid research recipe phase")
        require(all(type(item[name]) is dict and len(canonical(item[name])) <= 16384
                    for name in ("parameters", "host_inputs")), "invalid research recipe inputs")
        capture = item["capture"]
        if capture is not None:
            _read(store, capture)
        if item["phase"] == "confirmatory":
            require(capture is not None and capture not in confirmatory_captures,
                    "confirmation needs a distinct frozen capture per recipe")
            confirmatory_captures.add(capture)
        signature = digest(canonical([item[key] for key in ("pack_id", "parameters", "host_inputs", "capture")]))
        require(signature not in seen_recipes, "equivalent research recipes cannot be repeated")
        seen_recipes.add(signature)
    require(not any(item["capture"] in confirmatory_captures for item in recipes if item["phase"] == "exploratory"),
            "confirmatory capture is also an exploratory input")
    actions = value["literature"]
    require(type(actions) is list and len(actions) <= budget["literature_calls"], "literature budget exceeded")
    for action in actions:
        require(validate_literature_action(action) == action, "literature action differs from normalized form")
    require(len({digest(canonical(action)) for action in actions}) == len(actions), "duplicate literature action")
    provider = value["provider"]
    if provider is not None:
        validate_provider(_read(store, provider))
    _read(store, value["environment"])
    instructions = _read(store, value["instructions"])
    require(set(instructions) == set(INSTRUCTIONS) and all(_text(v, 8192) for v in instructions.values()),
            "invalid research role instructions")
    if value["release"] is not None:
        pin = _read(store, value["release"])
        require(_text(pin.get("release"), 160) and _text(value["release_root"], 4096), "invalid local release pin")
    else:
        require(value["release_root"] is None, "release root needs a pinned release")


def _begin(store: Store, hypothesis: str, contract: str, study: str) -> dict[str, Any]:
    require(_text(hypothesis, 8192), "research hypothesis needs nonempty text of at most 8192 characters")
    _contract(store, _read(store, contract))
    return dict(schema_version=1, study_id=study, hypothesis=hypothesis, contract=contract,
                questions=list(QUESTIONS), scientific_validity="not_assessed")


def session_view(history: list[dict[str, Any]], session: str) -> dict[str, Any]:
    opened = Kernel._get(history, session, "research_session")
    rows = [row for row in history if row["kind"] == "research_step" and row["payload"]["session"] == session]
    steps = {row["payload"]["key"]: row for row in rows if row["payload"]["step"] != "reply"}
    require(len(steps) == sum(row["payload"]["step"] != "reply" for row in rows), "duplicate research step")
    return dict(opened=opened, steps=steps, rows=rows)


def _refs(history: list[dict[str, Any]], refs: Any) -> list[dict[str, str]]:
    require(type(refs) is list and len(refs) <= 128, "invalid research references")
    indexed = {row["id"]: row for row in history}
    unique = set()
    for ref in refs:
        require(type(ref) is dict and set(ref) == {"id", "hash"} and type(ref["id"]) is str
                and ref["id"] in indexed and indexed[ref["id"]]["hash"] == ref["hash"],
                "research reference is missing or stale")
        require(ref["id"] not in unique, "duplicate research reference")
        unique.add(ref["id"])
    return refs


def task_basis(store: Store, history: list[dict[str, Any]], context: dict[str, Any]) -> None:
    result = context.get("result")
    if result:
        close = Kernel._get(history, result["close"], "stage_close")
        claim = close["payload"]["claim"]
        require(claim == result["claim"] and claim is not None, "invalid research review result")
        kernel = Kernel(store, Actor("research-observer", "observer"))
        require(kernel._gate(history, claim)["basis_hash"] == context["basis"],
                "research review evidence changed")


def review_key(store: Store, history: list[dict[str, Any]], stage: str, claim: str) -> tuple[str, str]:
    basis = Kernel(store, Actor("research-observer", "observer"))._gate(history, claim)["basis_hash"]
    return f"reviewer:{stage}:{basis[:16]}", basis


def _dispatch(store: Store, history: list[dict[str, Any]], contract: dict[str, Any],
              view: dict[str, Any], key: str, data: dict[str, Any]) -> None:
    require(set(data) == {"role", "context", "spec", "workspace"} and data["role"] in _ROLES
            and key.startswith(f"task-intent:{data['role']}"), "invalid research role intent")
    role = data["role"]
    context = _read(store, data["context"])
    refs = _refs(history, context["references_with_hashes"])
    require(context["references"] == [ref["id"] for ref in refs] and context["role"] == role
            and context["session"] == view["opened"]["id"] and context["instructions"] == contract["instructions"],
            "role context differs from the approved research session")
    task_basis(store, history, context)
    if contract["provider"] is None:
        require(data["spec"] is None and data["workspace"] is None, "fixture has external worker details")
        return
    from .research_agents import validate_worker_spec
    require(type(data["workspace"]) is str and re.fullmatch(r"[0-9a-f]{32}", data["workspace"]),
            "invalid role workspace token")
    validate_worker_spec(store, _read(store, data["spec"]), role, context,
                         _read(store, contract["instructions"])[role], contract["provider"],
                         contract["budget"]["operation_seconds"])
    used = sum(row["payload"]["step"] == "task-intent" for row in view["rows"])
    require(used < contract["budget"]["model_calls"], "research model-call budget exhausted")


def _answer(store: Store, history: list[dict[str, Any]], view: dict[str, Any],
            key: str, data: dict[str, Any]) -> dict[str, Any]:
    require(set(data) == {"intent", "completion"}, "invalid research role result")
    intent = view["steps"].get("task-intent:" + key.removeprefix("task:"))
    require(intent is not None and intent["id"] == data["intent"], "role response has no matching intent")
    p = intent["payload"]["data"]
    role, context = p["role"], _read(store, p["context"])
    if p["spec"] is None:
        require(data["completion"] is None, "fixture role cannot use a provider completion")
        result = dict(status="answered", usage=None, answer=fixture_answer(role, context), reason=None)
    else:
        result = inspect_worker(store, data["completion"], _read(store, p["spec"]), identity(intent))
    if result["status"] == "answered":
        try:
            validate_answer(role, context, result["answer"])
            task_basis(store, history, context)
        except (ValueError, KeyError, TypeError) as exc:
            result = dict(result, status="invalid", reason=str(exc))
    return dict(data, result=result)


def identity(intent: dict[str, Any]) -> dict[str, Any]:
    return dict(schema_version=1, request=intent["id"], dispatch=intent["hash"],
                specification=intent["payload"]["data"]["spec"])


def _step(store: Store, history: list[dict[str, Any]], session: str, key: str,
          step: str, data: dict[str, Any], study: str) -> dict[str, Any]:
    view = session_view(history, session)
    opened, rows, steps = view["opened"], view["rows"], view["steps"]
    require(opened["payload"]["study_id"] == study, "research session belongs to another study")
    require("finish" not in steps and key not in steps, "research step is already present or session is closed")
    require(type(data) is dict and _text(key, 96), "invalid research step input")
    contract = _read(store, opened["payload"]["contract"])
    if step == "reply":
        require("accept" not in steps and key == "reply" and set(data) == {"text"} and _text(data["text"], 8192)
                and len(rows) < 12, "invalid author reply or research already accepted")
    elif step == "accept":
        draft = rows[-1] if rows else opened
        require(key == step and data == {"revision": draft["hash"]}, "author must confirm the current research draft")
    else:
        require("accept" in steps, "research requires explicit author acceptance")
        if step.startswith("literature"):
            _literature_step(store, contract, steps, key, step, data)
        elif step == "task-intent":
            _dispatch(store, history, contract, view, key, data)
        elif step == "task":
            data = _answer(store, history, view, key, data)
        elif step == "policy":
            _policy(contract, view, key, data)
        elif step == "finish":
            require(key == step and set(data) == {"reason", "detail", "stages", "paper"}
                    and data["reason"] in {"planned_checks_finished", "budget_exhausted", "blocked", "inconclusive"}
                    and _text(data["detail"]), "invalid research disposition")
            data = _finish(store, history, view, data)
        else:
            raise ValueError("unknown research step")
    return dict(schema_version=1, study_id=study, session=session, session_hash=opened["hash"],
                key=key, step=step, data=data, scientific_validity="not_assessed")


def _policy(contract: dict[str, Any], view: dict[str, Any], key: str, data: dict[str, Any]) -> None:
    from .search import COMPONENTS

    require(set(data) == {"developer", "release", "priorities"} and key.startswith("policy:")
            and contract["release"] is not None and data["release"] == contract["release"], "invalid pinned policy result")
    developer = view["steps"].get("task:developer:" + key.removeprefix("policy:"))
    require(developer is not None and developer["id"] == data["developer"], "policy has no pinned developer proposal")
    output = developer["payload"]["data"]["result"]
    require(output["status"] == "answered", "policy requires a valid developer proposal")
    priorities = data["priorities"]
    require(type(priorities) is dict and set(priorities) == {row["label"] for row in output["answer"]["priorities"]},
            "local release emitted invalid priorities")
    for scores in priorities.values():
        require(type(scores) is dict and set(scores) == set(COMPONENTS)
                and all(type(v) in {int, float} and 0 <= v <= 1 for v in scores.values()),
                "local release may only rank known recipes")


def _literature_step(store: Store, contract: dict[str, Any], steps: dict[str, Any],
                     key: str, step: str, data: dict[str, Any]) -> None:
    from .research_literature import validate_literature_result

    require(step in {"literature-intent", "literature"} and type(data.get("index")) is int
            and 0 <= data["index"] < len(contract["literature"]) and key == f"{step}:{data['index']}",
            "literature action was not authorized")
    if step == "literature-intent":
        require(set(data) == {"index"}, "invalid literature intent")
    else:
        require(set(data) == {"index", "result", "failure"} and f"literature-intent:{data['index']}" in steps,
                "literature response lacks authorization")
        if data["failure"] is None:
            validate_literature_result(store, contract["literature"][data["index"]], data["result"])
        else:
            require(_text(data["failure"]) and data["result"] is None, "invalid literature failure")


def _finish(store: Store, history: list[dict[str, Any]], view: dict[str, Any],
            data: dict[str, Any]) -> dict[str, Any]:
    stages = _refs(history, data["stages"])
    closed = [Kernel._get(history, row["id"], "stage_close") for row in stages]
    closed = [row["payload"] for row in closed]
    study = view["opened"]["payload"]["study_id"]
    require(all(row["study_id"] == study for row in closed), "research result references another study")
    require([row["id"] for row in stages] == [row["id"] for row in history if row["kind"] == "stage_close"
                                               and row["payload"]["study_id"] == study],
            "research result omits or reorders a closed stage")
    if data["reason"] == "planned_checks_finished":
        require(bool(closed) and all(row["analysis"] and row["disposition"] in {"criteria_met", "criteria_not_met"}
                                     for row in closed), "research cannot finish with missing checked evidence")
    outcomes = [dict(claim=row["claim"], outcome=row["outcome"], inference_mode=row["inference_mode"],
                     stage=row["stage"]) for row in closed if row["claim"]]
    if data["reason"] == "planned_checks_finished":
        for index, outcome in enumerate(outcomes):
            key, _ = review_key(store, history, outcome["stage"], outcome["claim"])
            reviewer = view["steps"].get(f"task:{key}")
            require(reviewer is not None, "completed stage has no current machine review")
            result = reviewer["payload"]["data"]["result"]
            require(result["status"] == "answered" and result["answer"]["decision"] != "unresolved",
                    "completed stage still has an unresolved review")
            if index == len(outcomes) - 1:
                require(result["answer"]["decision"] == "finish", "final review requests more evidence")
            intent = Kernel._get(history, reviewer["payload"]["data"]["intent"], "research_step")
            task_basis(store, history, _read(store, intent["payload"]["data"]["context"]))
    if data["paper"] is not None:
        paper = Kernel._get(history, data["paper"], "paper")
        require(set(paper["payload"]["claims"]) <= {row["claim"] for row in outcomes},
                "paper includes claims outside the research result")
    return dict(data, outcomes=outcomes)


class Research:
    """Admission runs inside CommandService's persisted revision check."""

    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def begin(self, *, hypothesis: str, contract: str) -> str:
        history = self.store.events()
        context = self.store._command_context
        require(context is not None, "research needs a command receipt")
        require(not any(row["kind"] == "research_session" and row["payload"]["study_id"] == context["study_id"]
                        for row in history), "study already has a research session")
        payload = _begin(self.store, hypothesis, contract, context["study_id"])
        return Kernel(self.store, self.actor)._write(history, "research_session", payload, {"author"})

    def record(self, *, session: str, key: str, step: str, data: dict[str, Any]) -> str:
        history = self.store.events()
        context = self.store._command_context
        require(context is not None and self.actor.role == _NOTE_ROLES.get(step, "planner"),
                "research role cannot record this step")
        opened = Kernel._get(history, session, "research_session")
        if step in _NOTE_ROLES:
            require(opened["actor"] == self.actor.id, "research reply or acceptance must come from its author")
        payload = _step(self.store, history, session, key, step, data, context["study_id"])
        return Kernel(self.store, self.actor)._write(history, "research_step", payload, {self.actor.role})


def research_index(store: Store, history: list[dict[str, Any]], receipts: list[dict[str, Any]]) -> None:
    events = {row["id"] for row in history if row["kind"] in KINDS}
    observed, studies = set(), set()
    for receipt in receipts:
        request, context = receipt["request"], receipt["context"]
        if request["action"] not in ACTIONS:
            continue
        before, after = receipt["before_revision"], receipt["after_revision"]
        require(after == before + 1, "research receipt must commit one event")
        created = history[before]
        require(created["id"] == receipt["result"] and created["id"] in events
                and created["actor"] == context["actor"] and created["role"] == context["role"],
                "research receipt differs from its event")
        study, payload = context["study_id"], request["payload"]
        if request["action"] == "research.begin":
            require(study not in studies and created["kind"] == "research_session" and created["role"] == "author",
                    "invalid research session origin")
            studies.add(study)
            expected = _begin(store, payload["hypothesis"], payload["contract"], study)
        else:
            require(created["kind"] == "research_step" and created["role"] == _NOTE_ROLES.get(payload["step"], "planner"),
                    "invalid research step origin")
            if created["role"] == "author":
                opened = Kernel._get(history[:before], payload["session"], "research_session")
                require(opened["actor"] == created["actor"], "research author mismatched")
            expected = _step(store, history[:before], **payload, study=study)
        require(created["payload"] == expected, "research event differs from admitted transition")
        observed.add(created["id"])
    require(events == observed, "research record lacks its original receipt")


def deadline(accepted: dict[str, Any], budget: dict[str, Any]) -> float:
    return datetime.fromisoformat(accepted["created_at"]).timestamp() + budget["wall_seconds"]


def remaining_seconds(accepted: dict[str, Any], budget: dict[str, Any]) -> int:
    now = datetime.now(timezone.utc).timestamp()
    return max(0, int(deadline(accepted, budget) - now))


def event_references(store: Store, event: dict[str, Any]) -> list[dict[str, Any]]:
    if event["kind"] == "research_session":
        return []
    p = event["payload"]
    data, step = p["data"], p["step"]
    refs = [dict(id=p["session"], hash=p["session_hash"])]
    if step == "task-intent":
        refs.extend(_read(store, data["context"])["references_with_hashes"])
    elif step == "task":
        refs.append(dict(id=data["intent"], hash=None))
    elif step == "policy":
        refs.append(dict(id=data["developer"], hash=None))
    elif step == "finish":
        refs.extend(data["stages"])
        if data["paper"] is not None:
            refs.append(dict(id=data["paper"], hash=None))
    return refs


def event_artifacts(store: Store, event: dict[str, Any]) -> set[str]:
    p = event["payload"]
    if event["kind"] == "research_session":
        value = _read(store, p["contract"])
        keys = {p["contract"], value["instructions"], value["environment"]}
        keys.update(value[key] for key in ("release", "provider") if value[key] is not None)
        for recipe in value["candidates"]:
            if recipe["capture"] is not None:
                keys.add(recipe["capture"])
                keys.update(row["sha256"] for row in _read(store, recipe["capture"])["inventory"])
        return keys
    data, step = p["data"], p["step"]
    if step == "task-intent":
        keys = {data["context"]}
        if data["spec"] is not None:
            keys.add(data["spec"])
            keys.update(_read(store, data["spec"])["expected_inputs"].values())
        return keys
    if step == "task" and data["completion"] is not None:
        manifest = _read(store, data["completion"])
        return {data["completion"], manifest["stdout"]["sha256"], manifest["stderr"]["sha256"],
                *(item["sha256"] for item in manifest["outputs"].values())}
    if step == "literature" and data["result"] is not None:
        return {data["result"]["artifact_digest"]}
    return set()
