"""Pure resolution of recorded claim relations, without scientific promotion.

The caller supplies an ordered, already hash-chain-verified event history. This
module checks event identity/order and link reference structure, including exact
scope and endpoint event hashes. It does not verify the event hash chain, read
artifacts, authenticate actors, check evidence basis hashes or accept claims.

``supports`` and ``limits`` form one directed graph, source -> target.
``supersedes`` is a separate newer-to-older lineage: mixing its reverse review
context traversal with the dependency DAG must not manufacture a graph cycle.
Contradiction and supersession both require reviewing the other endpoint; neither
deletes a claim or establishes that a finding has been resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .claims import ClaimLink, ClaimLinkError


class ClaimContextError(ClaimLinkError):
    """Recorded links are structurally inconsistent with the supplied history."""


@dataclass(frozen=True)
class ClaimContext:
    """IDs in event order; their inclusion means review context, not support."""

    claim_ids: tuple[str, ...]
    link_ids: tuple[str, ...]


_DIRECTED = frozenset({"supports", "limits"})


def _scope(event: dict[str, Any]) -> dict[str, str]:
    scope = event["payload"].get("scope")
    if (type(scope) is not dict or not scope or not all(
            type(key) is str and key.strip() and type(value) is str and value.strip()
            for key, value in scope.items())):
        raise ClaimContextError(f"claim {event['id']} requires a nonempty string scope mapping")
    return scope


class _LinkIndex:
    def __init__(self, history: list[dict[str, Any]]):
        self.events: dict[str, dict[str, Any]] = {}
        self.links: list[tuple[dict[str, Any], ClaimLink]] = []
        self.duplicates: set[tuple[str, str, str]] = set()
        self.directed: dict[str, set[str]] = {}
        previous_seq = 0
        for event in history:
            if type(event) is not dict:
                raise ClaimContextError("history events must be objects")
            id, seq = event.get("id"), event.get("seq")
            if type(id) is not str or not id.strip() or id in self.events:
                raise ClaimContextError("history event IDs must be nonempty and unique")
            if type(seq) is not int or seq <= previous_seq:
                raise ClaimContextError("history events must have strictly increasing positive sequences")
            if type(event.get("kind")) is not str or not event["kind"]:
                raise ClaimContextError("history event kind must be a nonempty string")
            if type(event.get("payload")) is not dict:
                raise ClaimContextError("history event payload must be an object")
            if (type(event.get("hash")) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", event["hash"]) is None):
                raise ClaimContextError("history event hash must be a lowercase SHA-256 digest")
            if event["kind"] == "claim_link":
                try:
                    candidate = ClaimLink.from_dict(event["payload"])
                except ClaimLinkError as exc:
                    raise ClaimContextError(f"invalid claim_link {id}: {exc}") from exc
                # Only the prefix is visible, so forward references fail here.
                self.validate(candidate)
                self.duplicates.add(self.key(candidate))
                if candidate.relation in _DIRECTED:
                    self.directed.setdefault(candidate.source, set()).add(candidate.target)
                self.links.append((event, candidate))
            self.events[id] = event
            previous_seq = seq

    @staticmethod
    def key(candidate: ClaimLink) -> tuple[str, str, str]:
        if candidate.relation == "contradicts":
            first, second = sorted((candidate.source, candidate.target))
            return candidate.relation, first, second
        return candidate.relation, candidate.source, candidate.target

    def claim(self, id: str) -> dict[str, Any]:
        event = self.events.get(id) if type(id) is str else None
        if event is None or event["kind"] != "claim":
            raise ClaimContextError(f"missing prior claim or wrong reference kind: {id!r}")
        _scope(event)
        return event

    def validate(self, candidate: ClaimLink) -> None:
        if type(candidate) is not ClaimLink:
            raise ClaimContextError("candidate must be a ClaimLink declaration")
        try:
            candidate.validate()
        except ClaimLinkError as exc:
            raise ClaimContextError(str(exc)) from exc
        source, target = self.claim(candidate.source), self.claim(candidate.target)
        if source["hash"] != candidate.source_hash or target["hash"] != candidate.target_hash:
            raise ClaimContextError("claim link endpoint event hash mismatch")
        if _scope(source) != _scope(target):
            raise ClaimContextError("claim link requires exactly matching endpoint scopes")
        if candidate.relation == "supersedes" and source["seq"] <= target["seq"]:
            raise ClaimContextError("superseding source must be a newer claim than its target")
        if self.key(candidate) in self.duplicates:
            raise ClaimContextError("duplicate claim pair and relation")
        if candidate.relation in _DIRECTED:
            pending, visited = [candidate.target], set()
            while pending:
                current = pending.pop()
                if current == candidate.source:
                    raise ClaimContextError("supports/limits dependency cycle")
                if current not in visited:
                    visited.add(current)
                    pending.extend(self.directed.get(current, ()))


def validate_link(history: list[dict[str, Any]], candidate: ClaimLink) -> None:
    """Validate a new declaration against all prior events and links.

    Existing malformed links also fail closed. Endpoint basis hashes are left
    for the kernel to check at its persisted admission boundary.
    """
    _LinkIndex(history).validate(candidate)


def validate_links(history: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Check each recorded link against its prefix; return links in event order.

    Returned events are the supplied history records, not copies or a new state
    authority. This function and the resolver never mutate those records.
    """
    return tuple(event for event, _ in _LinkIndex(history).links)


def resolve_context(history: list[dict[str, Any]], claim: str) -> ClaimContext:
    """Resolve incoming dependencies plus symmetric contradiction/lineage context.

    Repeatedly include incoming supports/limits ancestors, then the opposite
    endpoint of contradictions and supersessions and its own incoming ancestors.
    A supports/limits edge pointing out to an unrelated target is not included.
    All applicable links inside the resulting context are kept, even when an
    endpoint had already been included by a different path. New relation records
    can therefore change review context without introducing a new claim ID.
    """
    index = _LinkIndex(history)
    index.claim(claim)
    claims, links = {claim}, set()
    changed = True
    while changed:
        changed = False
        for event, link in index.links:
            applicable = (link.target in claims if link.relation in _DIRECTED
                          else link.source in claims or link.target in claims)
            if applicable:
                if link.source not in claims or link.target not in claims:
                    changed = True
                claims.update((link.source, link.target))
                links.add(event["id"])
    return ClaimContext(
        claim_ids=tuple(event["id"] for event in index.events.values() if event["id"] in claims),
        link_ids=tuple(event["id"] for event, _ in index.links if event["id"] in links))
