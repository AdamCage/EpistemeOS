"""Versioned local command admission; no external execution or authentication.

Use this boundary for retryable delivery. Direct Kernel/Search calls remain
optimistic legacy APIs without command receipts. Results acknowledge historical
commits; callers must query current gates before a new scientific transition.
"""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import json
import math
import types
from typing import Any, Callable, get_args, get_origin, get_type_hints

from .kernel import Actor, Kernel
from .planning import Planning
from .execution import Execution
from .batch import Batch
from .agents import Agents
from .reporting import PaperBuilder
from .search import Search
from .store import Store, canonical


def json_value(value: Any) -> Any:
    """Return a detached strict JSON value, rejecting implicit Python coercions."""
    def check(item: Any) -> None:
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child)
            return
        raise ValueError("command must contain strict JSON values with finite numbers and string keys")
    check(value)
    return json.loads(canonical(value))


def parse_command(text: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate command JSON key: {key}")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ValueError(f"non-finite command JSON number: {value}")

    return json_value(json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant))


@dataclass(frozen=True)
class CommandContext:
    command_id: str
    expected_revision: int
    actor: str
    role: str
    study_id: str
    correlation_id: str
    causation_id: str | None

    @classmethod
    def from_dict(cls, value: Any) -> CommandContext:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("command context fields must be exactly: " +
                             ", ".join(cls.__dataclass_fields__))
        context = cls(**value)
        context.to_dict()
        return context

    def to_dict(self) -> dict[str, Any]:
        for field in ("command_id", "actor", "role", "study_id", "correlation_id"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a nonempty string")
        if type(self.expected_revision) is not int or self.expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        if self.causation_id is not None and (
                not isinstance(self.causation_id, str) or not self.causation_id.strip()):
            raise ValueError("causation_id must be null or a nonempty prior event ID")
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


# An explicit allowlist prevents payloads from naming arbitrary methods or tools.
# No handler here starts a process, contacts a provider or materializes exports.
_ACTIONS: dict[str, tuple[type, Callable[..., Any], frozenset[str]]] = {
    "agent.register_budget": (Agents, Agents.register_budget, frozenset({"planner"})),
    "agent.request_hypotheses": (Agents, Agents.request_hypotheses, frozenset({"planner"})),
    "agent.request_experiment": (Agents, Agents.request_experiment, frozenset({"planner"})),
    "agent.dispatch": (Agents, Agents.dispatch, frozenset({"planner"})),
    "agent.finalize": (Agents, Agents.finalize, frozenset({"planner"})),
    "agent.apply_hypotheses": (Agents, Agents.apply_hypotheses, frozenset({"planner"})),
    "agent.apply_experiment": (Agents, Agents.apply_experiment, frozenset({"planner"})),
    "batch.plan": (Batch, Batch.plan, frozenset({"planner"})),
    "batch.enqueue_slot": (Batch, Batch.enqueue_slot, frozenset({"executor", "replicator"})),
    "batch.settle": (Batch, Batch.settle, frozenset({"planner"})),
    "execution.enqueue": (Execution, Execution.enqueue, frozenset({"executor", "replicator"})),
    "execution.dispatch": (Execution, Execution.dispatch, frozenset({"executor", "replicator"})),
    "execution.finalize": (Execution, Execution.finalize, frozenset({"executor", "replicator"})),
    "planning.question": (Planning, Planning.question, frozenset({"planner"})),
    "planning.explanation_set": (Planning, Planning.explanation_set, frozenset({"planner"})),
    "kernel.hypothesis": (Kernel, Kernel.hypothesis, frozenset({"planner"})),
    "kernel.preregister": (Kernel, Kernel.preregister, frozenset({"planner"})),
    "kernel.preregister_for_set": (Kernel, Kernel.preregister_for_set, frozenset({"planner"})),
    "kernel.start_run": (Kernel, Kernel.start_run, frozenset({"executor", "replicator"})),
    "kernel.finish_run": (Kernel, Kernel.finish_run, frozenset({"executor", "replicator"})),
    "kernel.claim": (Kernel, Kernel.claim, frozenset({"analyst", "executor"})),
    "kernel.review": (Kernel, Kernel.review, frozenset({"reviewer"})),
    "kernel.review_with_links": (Kernel, Kernel.review_with_links, frozenset({"reviewer"})),
    "kernel.link_claims": (Kernel, Kernel.link_claims, frozenset({"planner", "analyst"})),
    "kernel.expose_data": (Kernel, Kernel.expose_data,
                           frozenset({"planner", "executor", "replicator", "analyst", "reviewer"})),
    "search.register_tournament": (Search, Search.register_tournament, frozenset({"planner"})),
    "search.ballot": (Search, Search.ballot, frozenset({"judge", "reviewer"})),
    "search.register_tree": (Search, Search.register_tree, frozenset({"planner"})),
    "search.add_node": (Search, Search.add_node, frozenset({"planner"})),
    "search.select_next": (Search, Search.select_next, frozenset({"planner"})),
    "search.finish_selection": (Search, Search.finish_selection, frozenset({"planner"})),
    "paper.build": (PaperBuilder, PaperBuilder.build, frozenset({"writer"})),
}


def _check_study(history: list[dict[str, Any]], action: str, payload: dict[str, Any], study: str) -> None:
    """Keep typed planning provenance consistent; this is not an access boundary.

    Legacy records have no implicit study assignment. A typed reference binds
    its command metadata, including when a later run/review uses a legacy action.
    Admission runs inside Store.command, after the historical-replay fast path.
    """
    if action in {"planning.question", "agent.register_budget"} and payload["study_id"] != study:
        raise ValueError("command study_id differs from the research question")
    events = {event["id"]: event for event in history}
    refs = [payload[key] for key in ("parent", "question", "explanation_set", "protocol", "run",
                                    "claim", "source", "target", "selection", "tree", "experiment_node",
                                    "job", "batch", "request", "budget")
            if isinstance(payload.get(key), str)]
    if action == "paper.build":
        refs.extend(payload["claims"])
    visited: set[str] = set()
    while refs:
        id = refs.pop()
        if id in visited or id not in events:
            continue  # The handler validates missing references and their types.
        visited.add(id)
        event = events[id]
        p, kind = event["payload"], event["kind"]
        assigned = (p.get("study_id") if kind in {"research_question", "explanation_set", "agent_budget"}
                    else p.get("planning", {}).get("study_id") if kind == "protocol" else None)
        if assigned is not None and assigned != study:
            raise ValueError("command study_id differs from its planning-bound references")
        fields = {
            "run": ("protocol",), "result": ("run",), "claim": ("protocol",), "review": ("claim",),
            "claim_link": ("source", "target"), "experiment_node": ("protocol", "tree"),
            "search_selection": ("node", "tree"), "search_terminal": ("selection",),
            "execution_job": ("run",), "execution_dispatch": ("job",), "execution_finalized": ("job",),
            "batch_plan": ("protocol", "selection"), "batch_slot": ("batch",), "batch_settlement": ("batch",),
            "agent_request": (("question", "budget", "explanation_set", "tree")
                              if p.get("schema_version") == 2 else ("question", "budget")),
            "agent_dispatch": ("request",), "agent_response": ("request",),
            "agent_application": (("request", "protocol", "experiment_node")
                                  if p.get("schema_version") == 2 else ("request",)),
        }.get(kind, ())
        refs.extend(p[field] for field in fields if isinstance(p.get(field), str))
        if kind == "paper":
            refs.extend(p["claims"])
        if kind == "search_tree":
            refs.extend(other["id"] for other in history
                        if other["kind"] == "experiment_node" and other["payload"]["tree"] == id)


def _matches_type(value: Any, annotation: Any) -> bool:
    """Enforce the JSON subset of the public method annotations before dispatch."""
    if annotation is Any:
        return True
    if annotation is type(None):
        return value is None
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is types.UnionType:
        return any(_matches_type(value, member) for member in args)
    if origin is list:
        return type(value) is list and all(_matches_type(item, args[0]) for item in value)
    if origin is dict:
        return type(value) is dict and all(_matches_type(key, args[0]) and _matches_type(item, args[1])
                                          for key, item in value.items())
    if annotation is float:
        return type(value) in (int, float)
    return type(value) is annotation


class CommandService:
    """Trusted local caller API; study IDs are metadata, not access boundaries.

Stable IDs and the original expected_revision must be reused after a lost
response. An intentional new decision requires a new command_id. Optional
method defaults are normalized before fingerprinting, so omission and an
explicit default denote the same request within this API version.
"""

    def __init__(self, store: Store):
        self.store = store

    def execute(self, envelope: dict[str, Any]) -> Any:
        envelope = json_value(envelope)
        if not isinstance(envelope, dict) or set(envelope) != {"context", "request"}:
            raise ValueError("command envelope fields must be exactly: context, request")
        context = CommandContext.from_dict(envelope["context"])
        request = envelope["request"]
        if not isinstance(request, dict) or set(request) != {"version", "action", "payload"}:
            raise ValueError("command request fields must be exactly: version, action, payload")
        if type(request["version"]) is not int or request["version"] != 1:
            raise ValueError("unsupported command request version")
        action = request["action"]
        if not isinstance(action, str) or action not in _ACTIONS:
            raise ValueError("unknown command action")
        if not isinstance(request["payload"], dict):
            raise ValueError("command payload must be an object")
        target_type, handler, roles = _ACTIONS[action]
        if context.role not in roles:
            raise ValueError(f"{context.role} cannot execute {action}")
        # Binding validates argument names and required fields before admission.
        try:
            bound = inspect.signature(handler).bind(None, **request["payload"])
        except TypeError as exc:
            raise ValueError(f"invalid {action} arguments: {exc}") from exc
        bound.apply_defaults()
        payload = json_value({key: value for key, value in bound.arguments.items() if key != "self"})
        annotations = get_type_hints(handler)
        for name, value in payload.items():
            if not _matches_type(value, annotations[name]):
                raise ValueError(f"invalid {action} payload type for {name}")
        if action == "kernel.start_run":
            required_role = "replicator" if payload["replicate_of"] else "executor"
            if context.role != required_role:
                raise ValueError(f"{context.role} cannot execute this run mode")
        normalized = dict(version=1, action=action, payload=payload)

        def invoke() -> Any:
            _check_study(self.store.events(), action, payload, context.study_id)
            target = target_type(self.store, Actor(context.actor, context.role))
            return handler(target, **payload)

        try:
            return self.store.command(context.to_dict(), normalized, invoke)
        except TypeError as exc:
            raise ValueError(f"invalid {action} payload: {exc}") from exc
