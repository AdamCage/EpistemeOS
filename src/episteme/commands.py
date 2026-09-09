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
    "kernel.hypothesis": (Kernel, Kernel.hypothesis, frozenset({"planner"})),
    "kernel.preregister": (Kernel, Kernel.preregister, frozenset({"planner"})),
    "kernel.start_run": (Kernel, Kernel.start_run, frozenset({"executor", "replicator"})),
    "kernel.finish_run": (Kernel, Kernel.finish_run, frozenset({"executor", "replicator"})),
    "kernel.claim": (Kernel, Kernel.claim, frozenset({"analyst", "executor"})),
    "kernel.review": (Kernel, Kernel.review, frozenset({"reviewer"})),
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
            target = target_type(self.store, Actor(context.actor, context.role))
            return handler(target, **payload)

        try:
            return self.store.command(context.to_dict(), normalized, invoke)
        except TypeError as exc:
            raise ValueError(f"invalid {action} payload: {exc}") from exc
