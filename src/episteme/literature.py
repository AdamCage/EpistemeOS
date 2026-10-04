"""Immutable literature links. The kernel stores and checks them.

A locator check is a persisted human or fixture statement. It names the
locator record's exact bytes and a passage digest already in the store. This
module does not search, retrieve, parse documents, call a model, or decide that
a work is novel. ``verified_by_recorded_check`` is not scientific validity, a
librarian's verification, or a review. Role labels do not create those either.
"""

from __future__ import annotations

from typing import Any

from .kernel import Actor, Kernel, require
from .store import Store, digest


LITERATURE_KINDS = frozenset({
    "literature_source", "literature_locator", "literature_claim",
    "literature_citation", "literature_check"})
_COMMANDS = {
    "literature.record_source": ("literature_source", "planner"),
    "literature.record_locator": ("literature_locator", "planner"),
    "literature.record_claim": ("literature_claim", "planner"),
    "literature.record_check": ("literature_check", "planner"),
    "literature.cite": ("literature_citation", "writer"),
}
LITERATURE_ACTIONS = frozenset(_COMMANDS)
_LOCATOR_KINDS = frozenset({"doi", "url", "page", "fixture"})
_OUTCOMES = frozenset({"verified_by_recorded_check", "contradicted"})
_CHECKERS = frozenset({"fixture", "recorded_human"})
_SOURCE_FIELDS = {"schema_version", "study_id", "title", "year", "authors", "scientific_validity"}
_LOCATOR_FIELDS = {"schema_version", "study_id", "source", "source_hash", "locator_kind",
                   "locator", "scientific_validity"}
_CLAIM_FIELDS = {"schema_version", "study_id", "locator", "locator_hash", "statement",
                 "extraction_actor", "scientific_validity"}
_CITATION_FIELDS = {"schema_version", "study_id", "locator", "locator_hash", "scientific_validity"}
_CHECK_FIELDS = {"schema_version", "study_id", "locator", "locator_hash", "locator_sha256",
                 "passage_digest", "outcome", "checker_kind", "statement", "scientific_validity"}
_REQUESTS = {
    "literature.record_source": frozenset({"title", "year", "authors"}),
    "literature.record_locator": frozenset({"source", "locator_kind", "locator"}),
    "literature.record_claim": frozenset({"locator", "statement"}),
    "literature.record_check": frozenset({"locator", "locator_sha256", "passage_digest",
                                          "outcome", "checker_kind", "statement"}),
    "literature.cite": frozenset({"locator"}),
}
_MAX_PASSAGE = 1024 * 1024


def _text(value: Any, label: str, limit: int) -> str:
    require(type(value) is str and value == value.strip() and 0 < len(value) <= limit,
            f"invalid literature {label}")
    return value


def _locator_sha256(locator: str) -> str:
    return digest(locator.encode("utf-8"))


def _matching_checks(history: list[dict[str, Any]], locator: dict[str, Any]) -> list[dict[str, Any]]:
    """Checks that name this locator record and its exact locator bytes."""
    sha = _locator_sha256(locator["payload"]["locator"])
    return [event for event in history
            if event["kind"] == "literature_check"
            and event["payload"].get("locator") == locator["id"]
            and event["payload"].get("locator_hash") == locator["hash"]
            and event["payload"].get("locator_sha256") == sha]


def locator_status(history: list[dict[str, Any]], locator_id: str) -> str:
    """Derive one status. A recorded contradiction blocks support."""
    locator = Kernel._get(history, locator_id, "literature_locator")
    checks = _matching_checks(history, locator)
    if any(event["payload"].get("outcome") == "contradicted" for event in checks):
        return "contradicted"
    if any(event["payload"].get("outcome") == "verified_by_recorded_check" for event in checks):
        return "verified_by_recorded_check"
    return "unverified"


def _require_support_status(status: str) -> None:
    require(status == "verified_by_recorded_check",
            "unverified locator cannot support a claim" if status == "unverified"
            else "contradicted locator cannot support a claim" if status == "contradicted"
            else "locator cannot support a claim")


def paper_literature(history: list[dict[str, Any]], citations: list[str] | None,
                     support: list[str] | None) -> dict[str, Any] | None:
    """Bind a scaffold to citation records. Empty means the historical paper shape.

    A citation id that is not a ``literature_citation`` event is rejected. Support
    is re-checked against the locator status on this history, not remembered from
    an earlier call. Absence of citations is not represented as novelty.
    """
    cited = [] if citations is None else citations
    used = [] if support is None else support
    require(type(cited) is list and type(used) is list, "paper citations must be lists")
    if not cited and not used:
        return None
    require(len(cited) <= 32 and len(cited) == len(set(cited))
            and all(type(item) is str for item in cited),
            "paper citations must be unique citation records")
    require(len(used) == len(set(used)) and set(used) <= set(cited),
            "support citation is not in the manuscript citations")
    hashes: dict[str, str] = {}
    checks: dict[str, str] = {}
    for citation_id in cited:
        matches = [event for event in history
                   if event["id"] == citation_id and event["kind"] == "literature_citation"]
        require(len(matches) == 1,
                "manuscript citation must be a citation record, not a bare string")
        citation = matches[0]
        locator = Kernel._get(history, citation["payload"]["locator"], "literature_locator")
        require(citation["payload"]["locator_hash"] == locator["hash"],
                "citation locator hash differs from the locator record")
        hashes[citation_id] = citation["hash"]
        if citation_id not in used:
            continue
        status = locator_status(history, locator["id"])
        _require_support_status(status)
        verified = [event for event in _matching_checks(history, locator)
                    if event["payload"]["outcome"] == "verified_by_recorded_check"]
        require(bool(verified), "verified locator has no recorded check")
        checks[citation_id] = verified[0]["id"]
    return dict(citations=cited, support=used, citation_hashes=hashes, support_checks=checks)


def assert_current_support(history: list[dict[str, Any]], payload: dict[str, Any]) -> None:
    """Re-read locator status before a scaffold is copied out of the store."""
    if "support" not in payload:
        return
    for citation_id in payload["support"]:
        citation = Kernel._get(history, citation_id, "literature_citation")
        _require_support_status(locator_status(history, citation["payload"]["locator"]))


def literature_artifacts(event: dict[str, Any]) -> set[str]:
    if event["kind"] == "literature_check":
        return {event["payload"]["passage_digest"]}
    return set()


def _expected(store: Store, before: list[dict[str, Any]], action: str, request: dict[str, Any],
              study: str, actor: str) -> dict[str, Any]:
    require(type(request) is dict and set(request) == _REQUESTS[action],
            "invalid literature request fields")
    if action == "literature.record_source":
        title = _text(request["title"], "title", 500)
        year = request["year"]
        require(type(year) is int and 1 <= year <= 9999, "invalid literature year")
        authors = request["authors"]
        require(type(authors) is list and 1 <= len(authors) <= 32, "invalid literature authors")
        recorded = [_text(author, "author", 200) for author in authors]
        return dict(schema_version=1, study_id=study, title=title, year=year, authors=recorded,
                    scientific_validity="not_assessed")
    if action == "literature.record_locator":
        source = Kernel._get(before, request["source"], "literature_source")
        require(source["payload"]["study_id"] == study, "locator source is in another study")
        kind = request["locator_kind"]
        require(type(kind) is str and kind in _LOCATOR_KINDS, "invalid locator kind")
        locator = _text(request["locator"], "locator", 2000)
        return dict(schema_version=1, study_id=study, source=source["id"], source_hash=source["hash"],
                    locator_kind=kind, locator=locator, scientific_validity="not_assessed")
    locator = Kernel._get(before, request["locator"], "literature_locator")
    require(locator["payload"]["study_id"] == study, "literature locator is in another study")
    if action == "literature.record_claim":
        statement = _text(request["statement"], "statement", 4000)
        return dict(schema_version=1, study_id=study, locator=locator["id"],
                    locator_hash=locator["hash"], statement=statement, extraction_actor=actor,
                    scientific_validity="not_assessed")
    if action == "literature.cite":
        return dict(schema_version=1, study_id=study, locator=locator["id"],
                    locator_hash=locator["hash"], scientific_validity="not_assessed")
    statement = _text(request["statement"], "statement", 4000)
    outcome, checker = request["outcome"], request["checker_kind"]
    require(type(outcome) is str and outcome in _OUTCOMES, "invalid locator check outcome")
    require(type(checker) is str and checker in _CHECKERS, "invalid locator check kind")
    sha = request["locator_sha256"]
    require(type(sha) is str and sha == _locator_sha256(locator["payload"]["locator"]),
            "check does not name the locator bytes")
    passage_digest = request["passage_digest"]
    require(type(passage_digest) is str, "invalid passage digest")
    passage = store.read(passage_digest)
    require(0 < len(passage) <= _MAX_PASSAGE, "passage must be a bounded CAS artifact")
    return dict(schema_version=1, study_id=study, locator=locator["id"], locator_hash=locator["hash"],
                locator_sha256=sha, passage_digest=passage_digest, outcome=outcome,
                checker_kind=checker, statement=statement, scientific_validity="not_assessed")


def _index(store: Store, history: list[dict[str, Any]], *,
           receipts: list[dict[str, Any]] | None = None) -> None:
    """Each literature event must match the command that appended it."""
    source = store.receipts() if receipts is None else receipts
    relevant = [row for row in source if row["request"]["action"] in _COMMANDS
                and row["after_revision"] <= len(history)]
    events = {event["id"]: event for event in history if event["kind"] in LITERATURE_KINDS}
    seen: set[str] = set()
    fields = {"literature_source": _SOURCE_FIELDS, "literature_locator": _LOCATOR_FIELDS,
              "literature_claim": _CLAIM_FIELDS, "literature_citation": _CITATION_FIELDS,
              "literature_check": _CHECK_FIELDS}
    for row in relevant:
        before = history[:row["before_revision"]]
        created = history[row["before_revision"]:row["after_revision"]]
        context, command = row["context"], row["request"]
        kind, role = _COMMANDS[command["action"]]
        require(command["version"] == 1 and context["role"] == role
                and len(created) == 1 and row["event_ids"] == [created[0]["id"]]
                and created[0]["kind"] == kind,
                "literature command needs one complete receipt")
        event = created[0]
        require(event["actor"] == context["actor"] and event["role"] == role
                and row["result"] == event["id"],
                "literature actor or receipt result mismatch")
        expected = _expected(store, before, command["action"], command["payload"],
                             context["study_id"], context["actor"])
        require(set(event["payload"]) == fields[kind] and event["payload"] == expected,
                "literature record differs from its command")
        seen.add(event["id"])
    require(seen == set(events), "literature record lacks its original command receipt")


class Literature:
    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def _append(self, action: str, request: dict[str, Any], role: str) -> str:
        require(self.store._command_context is not None and self.actor.role == role,
                f"{action} requires a {role} CommandService transaction")
        history = self.store.events()
        _index(self.store, history)
        payload = _expected(self.store, history, action, request,
                            self.store._command_context["study_id"], self.actor.id)
        kind = _COMMANDS[action][0]
        return Kernel(self.store, self.actor)._write(history, kind, payload, {role})

    def record_source(self, *, title: str, year: int, authors: list[str]) -> str:
        return self._append("literature.record_source",
                            dict(title=title, year=year, authors=authors), "planner")

    def record_locator(self, *, source: str, locator_kind: str, locator: str) -> str:
        return self._append("literature.record_locator",
                            dict(source=source, locator_kind=locator_kind, locator=locator), "planner")

    def record_claim(self, *, locator: str, statement: str) -> str:
        return self._append("literature.record_claim",
                            dict(locator=locator, statement=statement), "planner")

    def record_check(self, *, locator: str, locator_sha256: str, passage_digest: str, outcome: str,
                     checker_kind: str, statement: str) -> str:
        return self._append("literature.record_check", dict(
            locator=locator, locator_sha256=locator_sha256, passage_digest=passage_digest,
            outcome=outcome, checker_kind=checker_kind, statement=statement), "planner")

    def cite(self, *, locator: str) -> str:
        return self._append("literature.cite", dict(locator=locator), "writer")
