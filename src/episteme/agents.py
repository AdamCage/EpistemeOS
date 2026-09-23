"""Durable model proposals. Transport completion is never scientific validity."""

from __future__ import annotations

from datetime import datetime
import math
import re
import sys
from typing import Any

from .agent_profiles import DEFAULT_PROFILE, profile
from .codex_provider import PROGRAM, validate_provider
from .execution import _object
from .execution_authority import establish_authority, require_authority
from .kernel import Actor, Kernel, require
from .planning import Planning, _Index as PlanningIndex
from .runner_backend import fingerprint
from .store import Store, canonical


KINDS = {"agent_budget", "agent_request", "agent_dispatch", "agent_response", "agent_application"}


def _text(value: Any) -> bool:
    return type(value) is str and bool(value.strip())


def _get(history: list[dict[str, Any]], id: str, kind: str) -> dict[str, Any]:
    return Kernel._get(history, id, kind)


def _context(history: list[dict[str, Any]], question: str, *, current: bool,
             version: str = DEFAULT_PROFILE) -> dict[str, Any]:
    profile(version)
    index = PlanningIndex(history)
    event = index.get(question, "research_question")
    if current:
        index.head(event)
    return dict(schema_version=1, task="hypothesis_proposal", prompt_version=version, question=event)


def _proposal(store: Store, state: dict[str, Any], raw: bytes) -> dict[str, Any]:
    context = _object(store.read(state["request"]["payload"]["context"]))
    return profile(context["prompt_version"])[2](raw)


def _identity(state: dict[str, Any]) -> dict[str, Any]:
    return dict(schema_version=1, request=state["request"]["id"], dispatch=state["dispatch"]["id"],
                specification=state["request"]["payload"]["specification"])


def _specification(store: Store, p: dict[str, Any]) -> dict[str, Any]:
    spec = _object(store.read(p["specification"]))
    require(set(spec) == {"schema_version", "command", "outputs", "wall_seconds", "max_output_bytes",
                          "expected_inputs", "environment_fingerprint"}
            and type(spec["schema_version"]) is int and spec["schema_version"] == 1,
            "invalid agent execution specification")
    require(type(spec["wall_seconds"]) is int and 1 <= spec["wall_seconds"] <= 600,
            "agent wall limit must be 1..600 seconds")
    require(type(spec["max_output_bytes"]) is int and 1024 <= spec["max_output_bytes"] <= 4*1024*1024,
            "agent capture limit must be 1 KiB..4 MiB")
    require(type(spec["command"]) is list and len(spec["command"]) == 7
            and spec["command"][1:] == ["-I", "-S", "program.py", "input.dat", "--seed", "0"]
            and _text(spec["command"][0]), "invalid agent worker command")
    require(spec["outputs"] == {"proposal": "proposal.json"}
            and type(spec["environment_fingerprint"]) is dict
            and set(spec["expected_inputs"]) == {"program.py", "input.dat"}, "invalid agent worker inputs/outputs")
    for key in spec["expected_inputs"].values():
        store.read(key)
    context = _object(store.read(p["context"]))
    provider = _object(store.read(p["provider"]))
    validate_provider(provider)
    prompt, schema, _ = profile(context["prompt_version"])
    supplied = _object(store.read(spec["expected_inputs"]["input.dat"]))
    require(supplied == dict(provider=provider, prompt=prompt + "\n\n" + canonical(context).decode(),
                              output_schema=schema), "agent input differs from frozen context/profile")
    return spec


def _stream(data: bytes) -> tuple[dict[str, Any] | None, list[str]]:
    """Audit observed CLI actions; this detects use, it does not undo a tool call."""
    errors: list[str] = []
    usage = None
    started = 0
    completed = 0
    threads: set[str] = set()
    messages = 0
    phase = "new"
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeError:
        return None, ["provider stream is not UTF-8"]
    for line in lines:
        try:
            event = _object(line.encode())
            kind = event.get("type")
            if kind == "thread.started":
                require(phase == "new", "provider thread event out of order")
                require(_text(event.get("thread_id")), "provider thread identity missing")
                threads.add(event["thread_id"])
                phase = "thread"
            elif kind == "turn.started":
                started += 1
                require(phase == "thread", "provider turn start out of order")
                phase = "turn"
            elif kind == "turn.completed":
                completed += 1
                observed = event.get("usage")
                require(type(observed) is dict and {"input_tokens", "output_tokens"} <= observed.keys(),
                        "provider usage is missing")
                require(all(type(v) is int and v >= 0 for v in observed.values()), "invalid provider usage")
                usage = dict(observed)
                require(phase == "turn", "provider completion out of order")
                phase = "complete"
            elif kind in {"item.started", "item.updated", "item.completed"}:
                require(phase == "turn", "provider item out of order")
                require(type(event.get("item")) is dict and event["item"].get("type") in {"agent_message", "reasoning"},
                        "provider used a tool or an unsupported item type")
                if kind == "item.completed" and event["item"]["type"] == "agent_message":
                    require(_text(event["item"].get("text")), "provider message text missing")
                    messages += 1
            elif kind in {"turn.failed", "error"}:
                errors.append("provider reported an error or failed turn")
            else:
                errors.append("unsupported provider stream event")
        except (ValueError, TypeError, UnicodeError) as exc:
            errors.append(str(exc))
    if not (len(threads) == 1 and started == completed == 1):
        errors.append("exactly one completed provider turn required")
    if not messages:
        errors.append("completed provider message missing")
    return usage, errors


def _completion(store: Store, state: dict[str, Any], manifest: str) -> dict[str, Any]:
    record = _object(store.read(manifest))
    spec = state["spec"]
    require(record.get("identity") == _identity(state) and type(record.get("schema_version")) is int
            and record["schema_version"] == 1,
            "agent completion identity mismatch")
    require(record.get("status") in {"completed", "failed"} and record.get("termination_confirmed") is True,
            "agent completion has no confirmed local termination")
    require(record.get("command") == spec["command"] and type(record.get("reason")) is str,
            "agent completion command/reason mismatch")
    require(type(record.get("elapsed_seconds")) in {int, float} and math.isfinite(record["elapsed_seconds"])
            and record["elapsed_seconds"] >= 0, "invalid agent elapsed time")
    for name in ("started_at", "finished_at"):
        require(type(record.get(name)) is str and datetime.fromisoformat(record[name]).tzinfo is not None,
                "agent timestamps must have a timezone")
    require(record.get("returncode") is None or type(record["returncode"]) is int, "invalid agent return code")
    outputs = record.get("outputs")
    require(type(outputs) is dict and set(outputs) <= {"proposal"}, "invalid agent completion outputs")
    blobs: dict[str, str] = {}
    for name, item in {**outputs, "stdout": record.get("stdout"), "stderr": record.get("stderr")}.items():
        path = {"proposal": "proposal.json", "stdout": "stdout.bin", "stderr": "stderr.bin"}[name]
        require(type(item) is dict and set(item) == {"path", "sha256", "bytes"}
                and item["path"] == path and type(item["bytes"]) is int
                and 0 <= item["bytes"] <= spec["max_output_bytes"], "invalid agent output entry")
        require(len(store.read(item["sha256"])) == item["bytes"], "agent output length mismatch")
        blobs[name] = item["sha256"]
    if record["status"] == "completed":
        require(record["returncode"] == 0 and record.get("runtime") == spec["environment_fingerprint"]
                and record.get("inputs_before") == record.get("inputs_after") == spec["expected_inputs"]
                and set(outputs) == {"proposal"}, "completed agent lacks frozen inputs/runtime/output")
    usage, errors = _stream(store.read(blobs["stdout"]))
    proposal = None
    if record["status"] != "completed":
        status = "failed"
        errors.insert(0, record["reason"] or "agent transport failed")
    elif errors:
        status = "invalid"
    else:
        try:
            proposal = _proposal(store, state, store.read(blobs["proposal"]))
            status = proposal["status"]
        except ValueError as exc:
            status = "invalid"
            errors.append(str(exc))
    return dict(transport_status=record["status"], proposal_status=status, errors=errors, usage=usage,
                artifacts=blobs, scientific_validity="not_assessed")


def _index(store: Store, history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not any(event["kind"] in KINDS for event in history):
        return {}
    receipts = store.receipts()
    def receipt(events: list[dict[str, Any]], action: str) -> None:
        ids = [event["id"] for event in events]
        require(any(row["event_ids"] == ids and row["request"]["action"] == action for row in receipts),
                "agent transition lacks its complete original command receipt")
    budgets: dict[str, dict[str, Any]] = {}
    states: dict[str, dict[str, Any]] = {}
    for offset, event in enumerate(history):
        kind, p = event["kind"], event["payload"]
        if kind not in KINDS:
            continue
        require(event["role"] == "planner" and type(p.get("schema_version")) is int and p["schema_version"] == 1,
                "agent transition role/schema mismatch")
        before = history[:offset]
        if kind == "agent_budget":
            require(set(p) == {"schema_version", "study_id", "max_calls"} and _text(p["study_id"])
                    and type(p["max_calls"]) is int and 1 <= p["max_calls"] <= 100,
                    "invalid model call admission budget")
            receipt([event], "agent.register_budget")
            budgets[event["id"]] = event
        elif kind == "agent_request":
            require(set(p) == {"schema_version", "budget", "budget_hash", "question", "question_hash", "assignee",
                                "provider", "specification", "context", "execution_authority"}, "invalid agent request fields")
            require(p["budget"] in budgets and p["budget_hash"] == budgets[p["budget"]]["hash"], "unknown agent budget")
            budget = budgets[p["budget"]]
            require(sum(s["request"]["payload"]["budget"] == p["budget"] for s in states.values()) < budget["payload"]["max_calls"],
                    "model call admission budget exhausted")
            frozen = _object(store.read(p["context"]))
            context = _context(before, p["question"], current=True, version=frozen.get("prompt_version"))
            require(context["question"]["hash"] == p["question_hash"]
                    and context["question"]["payload"]["study_id"] == budget["payload"]["study_id"]
                    and _object(store.read(p["context"])) == context, "request context/study differs from frozen question")
            require(_text(p["assignee"]) and type(p["execution_authority"]) is str
                    and re.fullmatch(r"[0-9a-f]{64}", p["execution_authority"]), "invalid agent assignment/authority")
            receipt([event], "agent.request_hypotheses")
            states[event["id"]] = dict(request=event, spec=_specification(store, p), dispatch=None, response=None, application=None)
        else:
            require(p.get("request") in states, "agent event needs a prior request")
            state = states[p["request"]]
            request = state["request"]
            require(p.get("request_hash") == request["hash"] and event["actor"] == request["payload"]["assignee"],
                    "agent request hash or assigned actor mismatch")
            if kind == "agent_dispatch":
                require(set(p) == {"schema_version", "request", "request_hash", "workspace_token"}
                        and state["dispatch"] is None and type(p["workspace_token"]) is str
                        and re.fullmatch(r"[0-9a-f]{32}", p["workspace_token"]), "agent already dispatched or invalid token")
                _context(before, request["payload"]["question"], current=True)
                receipt([event], "agent.dispatch")
                state["dispatch"] = event
            elif kind == "agent_response":
                require(set(p) == {"schema_version", "request", "request_hash", "dispatch", "manifest", "assessment"}
                        and state["dispatch"] is not None and state["response"] is None
                        and p["dispatch"] == state["dispatch"]["id"], "invalid/duplicate agent response")
                require(p["assessment"] == _completion(store, state, p["manifest"]), "response assessment differs from captured bytes")
                receipt([event], "agent.finalize")
                state["response"] = event
            else:
                require(set(p) == {"schema_version", "request", "request_hash", "response", "response_hash", "mapping",
                                    "explanation_set", "explanation_set_hash", "limitations"}, "invalid agent application fields")
                require(state["response"] is not None and state["application"] is None
                        and p["response"] == state["response"]["id"] and p["response_hash"] == state["response"]["hash"],
                        "invalid or repeated agent application")
                response = state["response"]["payload"]
                require(response["assessment"]["proposal_status"] == "proposed", "agent response is not applicable")
                proposal = _proposal(store, state, store.read(response["assessment"]["artifacts"]["proposal"]))
                count = len(proposal["candidates"])
                created = history[offset-count-1:offset]
                require(len(created) == count+1 and [e["kind"] for e in created] == ["hypothesis"]*count + ["explanation_set"],
                        "application must immediately follow its new hypotheses/set")
                _context(history[:offset-count-1], request["payload"]["question"], current=True)
                context = _object(store.read(request["payload"]["context"]))["question"]["payload"]
                mapping = []
                for index, (candidate, hypothesis) in enumerate(zip(proposal["candidates"], created)):
                    require(hypothesis["payload"] == dict(statement=candidate["statement"], prediction=candidate["prediction"],
                        falsifier=candidate["falsifier"], scope=context["scope"]), "created hypothesis differs from model proposal")
                    mapping.append(dict(index=index, hypothesis=hypothesis["id"], hash=hypothesis["hash"], kind=candidate["kind"]))
                selected = created[-1]
                require(p["mapping"] == mapping and p["limitations"] == proposal["limitations"]
                        and p["explanation_set"] == selected["id"] and p["explanation_set_hash"] == selected["hash"],
                        "application entity mapping differs from its proposal")
                require(selected["payload"]["question"] == request["payload"]["question"]
                        and selected["payload"]["hypotheses"] == [e["id"] for e in created[:-1]]
                        and selected["payload"]["comparison_plan"] == proposal["comparison_plan"]
                        and selected["payload"]["parent"] is None, "application set differs from proposal")
                receipt([*created, event], "agent.apply_hypotheses")
                state["application"] = event
    return states


class Agents:
    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def _history(self) -> list[dict[str, Any]]:
        require(self.store._command_context is not None and self.actor.role == "planner",
                "agent transitions require planner CommandService transactions")
        return self.store.events()

    def _write(self, kind: str, payload: dict[str, Any]) -> str:
        return Kernel(self.store, self.actor)._write(self.store.events(), kind, dict(schema_version=1, **payload), {"planner"})

    def register_budget(self, *, study_id: str, max_calls: int) -> str:
        self._history()
        require(_text(study_id) and type(max_calls) is int and 1 <= max_calls <= 100, "invalid call budget")
        return self._write("agent_budget", dict(study_id=study_id, max_calls=max_calls))

    def request_hypotheses(self, *, budget: str, question: str, assignee: str, provider: str,
                           wall_seconds: int = 120, max_output_bytes: int = 1048576) -> str:
        history = self._history()
        states = _index(self.store, history)
        bound = _get(history, budget, "agent_budget")
        require(sum(s["request"]["payload"]["budget"] == budget for s in states.values()) < bound["payload"]["max_calls"],
                "model call admission budget exhausted")
        context = _context(history, question, current=True)
        require(context["question"]["payload"]["study_id"] == bound["payload"]["study_id"] and _text(assignee),
                "question/budget study mismatch or empty assignee")
        backend = _object(self.store.read(provider))
        validate_provider(backend)
        context_key = self.store.put_json(context)
        prompt, schema, _ = profile(context["prompt_version"])
        data = self.store.put_json(dict(provider=backend, prompt=prompt+"\n\n"+canonical(context).decode(),
                                       output_schema=schema))
        spec = self.store.put_json(dict(schema_version=1, command=[sys.executable,"-I","-S","program.py","input.dat","--seed","0"],
            outputs={"proposal":"proposal.json"}, wall_seconds=wall_seconds, max_output_bytes=max_output_bytes,
            expected_inputs={"program.py":self.store.put(PROGRAM),"input.dat":data}, environment_fingerprint=fingerprint()))
        p = dict(budget=budget, budget_hash=bound["hash"], question=question, question_hash=context["question"]["hash"],
            assignee=assignee, provider=provider, specification=spec, context=context_key,
            execution_authority=establish_authority(self.store))
        _specification(self.store, p)
        return self._write("agent_request", p)

    def _assigned(self, request: str) -> dict[str, Any]:
        states = _index(self.store, self._history())
        require(request in states, "unknown agent request")
        state = states[request]
        require(state["request"]["payload"]["assignee"] == self.actor.id, "wrong assigned agent actor")
        return state

    def dispatch(self, *, request: str, workspace_token: str) -> str:
        state = self._assigned(request)
        require_authority(self.store, state["request"]["payload"]["execution_authority"])
        _context(self.store.events(), state["request"]["payload"]["question"], current=True)
        require(state["dispatch"] is None and type(workspace_token) is str and re.fullmatch(r"[0-9a-f]{32}",workspace_token),
                "agent already dispatched or invalid token; reconcile instead")
        return self._write("agent_dispatch", dict(request=request, request_hash=state["request"]["hash"], workspace_token=workspace_token))

    def finalize(self, *, request: str, manifest: str) -> str:
        state = self._assigned(request)
        require(state["dispatch"] is not None and state["response"] is None, "agent not dispatched or already finalized")
        assessment = _completion(self.store, state, manifest)
        return self._write("agent_response", dict(request=request, request_hash=state["request"]["hash"],
            dispatch=state["dispatch"]["id"], manifest=manifest, assessment=assessment))

    def apply_hypotheses(self, *, request: str) -> str:
        state = self._assigned(request)
        require(state["response"] is not None and state["application"] is None, "agent response missing or already applied")
        response = state["response"]
        require(response["payload"]["assessment"]["proposal_status"] == "proposed", "agent response is not applicable")
        question = state["request"]["payload"]["question"]
        context = _context(self.store.events(), question, current=True)
        proposal = _proposal(self.store, state, self.store.read(response["payload"]["assessment"]["artifacts"]["proposal"]))
        mapping = []
        kernel = Kernel(self.store, self.actor)
        for index, candidate in enumerate(proposal["candidates"]):
            id = kernel.hypothesis(candidate["statement"], candidate["prediction"], candidate["falsifier"],
                                   context["question"]["payload"]["scope"])
            mapping.append(dict(index=index, hypothesis=id, hash=_get(self.store.events(),id,"hypothesis")["hash"],kind=candidate["kind"]))
        selected = Planning(self.store,self.actor).explanation_set(question=question,
            hypotheses=[row["hypothesis"] for row in mapping], comparison_plan=proposal["comparison_plan"])
        return self._write("agent_application", dict(request=request, request_hash=state["request"]["hash"],
            response=response["id"], response_hash=response["hash"], mapping=mapping, explanation_set=selected,
            explanation_set_hash=_get(self.store.events(),selected,"explanation_set")["hash"], limitations=proposal["limitations"]))


def agent_context(store: Store, history: list[dict[str, Any]], entity_ids: set[str]) -> list[dict[str, Any]]:
    states = _index(store, history)
    included: set[str] = set()
    for state in states.values():
        applied = state["application"]
        if applied is None:
            continue
        p = applied["payload"]
        if not ({p["explanation_set"], *(row["hypothesis"] for row in p["mapping"])} & entity_ids):
            continue
        included.add(state["request"]["payload"]["budget"])
        included.update(state[key]["id"] for key in ("request","dispatch","response","application"))
        question = state["request"]["payload"]["question"]
        while question is not None:
            included.add(question)
            question = _get(history, question, "research_question")["payload"]["parent"]
    return [event for event in history if event["id"] in included]


def agent_artifacts(store: Store, event: dict[str, Any]) -> set[str]:
    p = event["payload"]
    if event["kind"] == "agent_request":
        spec = _specification(store,p)
        return {p["provider"],p["context"],p["specification"],*spec["expected_inputs"].values()}
    if event["kind"] == "agent_response":
        return {p["manifest"],*p["assessment"]["artifacts"].values()}
    return set()


def agent_state(store: Store, request: str) -> dict[str, Any]:
    history = store.events()
    states = _index(store, history)
    require(request in states, "unknown agent request")
    state = states[request]
    assessment = state["response"]["payload"]["assessment"] if state["response"] else None
    status = ("applied" if state["application"] else state["response"]["payload"]["assessment"]["proposal_status"]
              if state["response"] else "unknown" if state["dispatch"] else "queued")
    return dict(request=request, revision=len(history), status=status,
        response=state["response"]["id"] if state["response"] else None,
        application=state["application"]["id"] if state["application"] else None,
        explanation_set=state["application"]["payload"]["explanation_set"] if state["application"] else None,
        transport_status=assessment["transport_status"] if assessment else None,
        errors=assessment["errors"] if assessment else [], usage=assessment["usage"] if assessment else None,
        isolation="trusted local CLI; fresh context request, not authenticated or clean-room",
        scientific_validity="not_assessed")
