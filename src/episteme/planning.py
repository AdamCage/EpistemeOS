"""Versioned research questions and explanation pools with immutable references.

Pure helpers consume an ordered, already hash-chain-verified history. They check
planning declarations and prior references, not artifact bytes, authentication,
scientific discriminability or execution of declared constraints. Historical
bindings retain excluded hypotheses; later revisions never rewrite a protocol.
"""

from __future__ import annotations

import re
from typing import Any, Protocol
from uuid import uuid4

from .store import Store


class PlanningError(ValueError):
    """A planning declaration, lineage or frozen binding is inconsistent."""


class ActorIdentity(Protocol):
    id: str
    role: str


_DIGEST = re.compile(r"[0-9a-f]{64}")
_COMMON = {"schema_version", "study_id", "parent", "parent_hash", "revision_reason"}
_QUESTION = _COMMON | {"statement", "objective", "scope", "constraints", "stopping_criteria"}
_SET = _COMMON | {"question", "question_hash", "hypotheses", "hypothesis_hashes",
                  "comparison_plan", "excluded_reasons"}
_BINDING = {"schema_version", "study_id", "question", "question_hash",
            "explanation_set", "explanation_set_hash"}


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise PlanningError(message)


def _text(value: Any) -> bool:
    return type(value) is str and bool(value.strip())


def _hash(value: Any) -> bool:
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _scope(value: Any) -> None:
    _require(type(value) is dict and value and all(_text(k) and _text(v)
             for k, v in value.items()), "scope must be a nonempty string mapping")


class _Index:
    def __init__(self, history: list[dict[str, Any]]):
        _require(type(history) is list, "planning history must be a list")
        self.events: dict[str, dict[str, Any]] = {}
        self.revised: set[str] = set()
        self.roots: dict[str, str] = {}
        previous_seq = 0
        for event in history:
            _require(type(event) is dict, "history events must be objects")
            id, seq = event.get("id"), event.get("seq")
            _require(_text(id) and id not in self.events,
                     "history event IDs must be nonempty and unique")
            _require(type(seq) is int and seq > previous_seq,
                     "history events must have strictly increasing positive sequences")
            _require(_text(event.get("kind")) and type(event.get("payload")) is dict
                     and _hash(event.get("hash")), "invalid history event kind, payload or hash")
            if event["kind"] in {"research_question", "explanation_set"}:
                _require(_text(event.get("actor")) and event.get("role") == "planner",
                         "planning events require a declared planner actor")
                self.validate(event["kind"], event["payload"])
                parent = event["payload"]["parent"]
                self.roots[id] = self.roots[parent] if parent is not None else id
                if parent is not None:
                    self.revised.add(parent)
            self.events[id] = event
            previous_seq = seq

    def get(self, id: Any, kind: str) -> dict[str, Any]:
        event = self.events.get(id) if _text(id) else None
        _require(event is not None and event["kind"] == kind,
                 f"unknown prior {kind} or wrong reference kind: {id!r}")
        return event

    def head(self, event: dict[str, Any]) -> None:
        _require(event["id"] not in self.revised,
                 f"{event['kind']} must be the current lineage head")

    def parent(self, kind: str, value: dict[str, Any]) -> dict[str, Any] | None:
        if value["parent"] is None:
            _require(value["parent_hash"] is None and value["revision_reason"] is None,
                     "root planning records require null parent_hash and revision_reason")
            return None
        _require(_text(value["revision_reason"]), "revision requires a nonempty revision_reason")
        parent = self.get(value["parent"], kind)
        self.head(parent)
        _require(_hash(value["parent_hash"]) and value["parent_hash"] == parent["hash"],
                 "planning parent hash mismatch")
        _require(value["study_id"] == parent["payload"]["study_id"],
                 "planning revision study_id mismatch")
        return parent

    def validate(self, kind: str, value: dict[str, Any]) -> None:
        expected = _QUESTION if kind == "research_question" else _SET
        _require(type(value) is dict and set(value) == expected,
                 f"{kind} payload fields must match schema v1 exactly")
        _require(type(value["schema_version"]) is int and value["schema_version"] == 1,
                 "unsupported planning schema_version")
        _require(_text(value["study_id"]), "study_id must be a nonempty string")
        parent = self.parent(kind, value)
        if kind == "research_question":
            _require(_text(value["statement"]) and _text(value["objective"]),
                     "question statement and objective must be nonempty strings")
            _scope(value["scope"])
            for field in ("constraints", "stopping_criteria"):
                _require(type(value[field]) is list and value[field]
                         and all(_text(item) for item in value[field]),
                         f"{field} must be a nonempty list of nonempty strings")
            return
        question = self.get(value["question"], "research_question")
        self.head(question)
        _require(_hash(value["question_hash"]) and value["question_hash"] == question["hash"],
                 "explanation set question hash mismatch")
        _require(value["study_id"] == question["payload"]["study_id"],
                 "explanation set study_id mismatch")
        _require(_text(value["comparison_plan"]), "comparison_plan must be a nonempty string")
        hypotheses = value["hypotheses"]
        _require(type(hypotheses) is list and len(hypotheses) >= 2
                 and all(_text(id) for id in hypotheses) and len(set(hypotheses)) == len(hypotheses),
                 "at least two unique hypothesis IDs required")
        hashes = value["hypothesis_hashes"]
        _require(type(hashes) is dict and set(hashes) == set(hypotheses),
                 "hypothesis_hashes must bind exactly the selected hypotheses")
        for id in hypotheses:
            hypothesis = self.get(id, "hypothesis")
            _require(_hash(hashes[id]) and hashes[id] == hypothesis["hash"],
                     "explanation set hypothesis hash mismatch")
            _scope(hypothesis["payload"].get("scope"))
            _require(hypothesis["payload"]["scope"] == question["payload"]["scope"],
                     "hypothesis scope must exactly match the research question")
        excluded = value["excluded_reasons"]
        _require(type(excluded) is dict and all(_text(k) and _text(v) for k, v in excluded.items()),
                 "excluded_reasons must be a string mapping with nonempty reasons")
        removed: set[str] = set()
        if parent is not None:
            previous_question = parent["payload"]["question"]
            _require(self.roots[previous_question] == self.roots[question["id"]],
                     "explanation set revision must stay in the same question lineage")
            removed = set(parent["payload"]["hypotheses"]) - set(hypotheses)
        _require(set(excluded) == removed,
                 "excluded_reasons must name exactly hypotheses removed from the parent")

    def binding(self, explanation_set: str, *, current: bool) -> dict[str, Any]:
        selected = self.get(explanation_set, "explanation_set")
        question = self.get(selected["payload"]["question"], "research_question")
        if current:
            self.head(selected)
            self.head(question)
        return dict(schema_version=1, study_id=question["payload"]["study_id"],
                    question=question["id"], question_hash=question["hash"],
                    explanation_set=selected["id"], explanation_set_hash=selected["hash"])


def validate_planning(history: list[dict[str, Any]]) -> None:
    """Validate planning records against their own prefixes, without mutation."""
    _Index(history)


def binding_for(history: list[dict[str, Any]], explanation_set: str, *,
                current: bool = True) -> dict[str, Any]:
    """Freeze question/set IDs and hashes; current=False permits historical heads."""
    _require(type(current) is bool, "current must be a boolean")
    return _Index(history).binding(explanation_set, current=current)


def planning_context(history: list[dict[str, Any]], binding: dict[str, Any]
                     ) -> list[dict[str, Any]]:
    """Return the bound planning ancestry and all its hypotheses in event order.

    Removed hypotheses and prior questions remain in context. Future descendants
    and other lineages are excluded. This is recorded context, not endorsement.
    """
    _require(type(binding) is dict and set(binding) == _BINDING,
             "planning binding fields must match schema v1 exactly")
    _require(type(binding["schema_version"]) is int and binding["schema_version"] == 1,
             "unsupported planning binding schema_version")
    _require(all(_text(binding[field]) for field in ("study_id", "question", "explanation_set"))
             and all(_hash(binding[field]) for field in ("question_hash", "explanation_set_hash")),
             "planning binding requires exact string IDs and lowercase SHA-256 hashes")
    index = _Index(history)
    expected = index.binding(binding["explanation_set"], current=False)
    _require(binding == expected, "planning binding does not match its immutable records")
    included: set[str] = set()
    current = index.get(binding["explanation_set"], "explanation_set")
    while True:
        included.add(current["id"])
        included.update(current["payload"]["hypotheses"])
        question = index.get(current["payload"]["question"], "research_question")
        while question["id"] not in included:
            included.add(question["id"])
            if question["payload"]["parent"] is None:
                break
            question = index.get(question["payload"]["parent"], "research_question")
        if current["payload"]["parent"] is None:
            break
        current = index.get(current["payload"]["parent"], "explanation_set")
    return [event for id, event in index.events.items() if id in included]


class Planning:
    """Trusted local planner API; actor/study IDs are declarations, not isolation."""

    def __init__(self, store: Store, actor: ActorIdentity):
        _require(_text(actor.id) and _text(actor.role), "actor ID and role required")
        self.store, self.actor = store, actor

    def _history(self) -> tuple[list[dict[str, Any]], _Index]:
        _require(self.actor.role == "planner", "only planner may create planning records")
        history = self.store.events()
        return history, _Index(history)

    def _write(self, history: list[dict[str, Any]], index: _Index,
               kind: str, payload: dict[str, Any]) -> str:
        index.validate(kind, payload)
        id = f"{kind}-{uuid4().hex[:16]}"
        self.store.append(id=id, kind=kind, actor=self.actor.id, role=self.actor.role,
                          payload=payload, expected_revision=len(history))
        return id

    def question(self, *, study_id: str, statement: str, objective: str,
                 scope: dict[str, str], constraints: list[str], stopping_criteria: list[str],
                 parent: str | None = None, revision_reason: str | None = None) -> str:
        history, index = self._history()
        previous = index.get(parent, "research_question") if parent is not None else None
        payload = dict(schema_version=1, study_id=study_id, statement=statement,
                       objective=objective, scope=scope, constraints=constraints,
                       stopping_criteria=stopping_criteria, parent=parent,
                       parent_hash=previous["hash"] if previous else None,
                       revision_reason=revision_reason)
        return self._write(history, index, "research_question", payload)

    def explanation_set(self, *, question: str, hypotheses: list[str], comparison_plan: str,
                        parent: str | None = None, revision_reason: str | None = None,
                        excluded_reasons: dict[str, str] | None = None) -> str:
        history, index = self._history()
        selected = index.get(question, "research_question")
        previous = index.get(parent, "explanation_set") if parent is not None else None
        _require(type(hypotheses) is list and all(_text(id) for id in hypotheses),
                 "hypotheses must be a list of hypothesis IDs")
        hashes = {id: index.get(id, "hypothesis")["hash"] for id in hypotheses}
        payload = dict(schema_version=1, study_id=selected["payload"]["study_id"],
                       question=question, question_hash=selected["hash"], hypotheses=hypotheses,
                       hypothesis_hashes=hashes, comparison_plan=comparison_plan, parent=parent,
                       parent_hash=previous["hash"] if previous else None,
                       revision_reason=revision_reason,
                       excluded_reasons={} if excluded_reasons is None else excluded_reasons)
        return self._write(history, index, "explanation_set", payload)
