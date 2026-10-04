"""Claim families and the review decisions that bind them (ADR 0018).

A family is every claim on a protocol connected to the claim's protocol by an
amendment, a review follow-up, shared observed bytes or a supersession link.
An open negative opinion or obligation about any member binds every member;
a withdrawal or a resolution counts only for the claim it names. Families are
computed from event payloads alone: no CAS bytes are read and no scientific
validity is decided. Caller-declared actor IDs remain unauthenticated.

A projection is cached only inside a ``Store.reading()`` scope or a command
transaction, keyed by the snapshot's length and chain head, so it never
outlives the verified CAS bytes of that scope.
"""

from __future__ import annotations

from typing import Any

from .claim_context import resolve_context
from .kernel import protocol_data
from .store import Store

NEGATIVE_VERDICTS = frozenset({"request_changes", "reject"})


def protocol_components(history: list[dict[str, Any]]) -> dict[str, str]:
    """Map each protocol to a representative of its lineage K(P)."""
    parent: dict[str, str] = {}

    def find(protocol: str) -> str:
        root = protocol
        while parent[root] != root:
            root = parent[root]
        while parent[protocol] != root:
            parent[protocol], protocol = root, parent[protocol]
        return root

    def union(a: str | None, b: str | None) -> None:
        if a in parent and b in parent:
            first, second = find(a), find(b)
            if first != second:
                parent[second] = first

    owners: dict[str, str] = {}

    def share(key: str, protocol: str) -> None:
        if key in owners:
            union(owners[key], protocol)
        else:
            owners[key] = protocol

    run_protocol: dict[str, str] = {}
    claim_protocol: dict[str, str] = {}
    obligation_claim: dict[str, str] = {}
    for event in history:
        kind, p = event["kind"], event["payload"]
        if kind == "protocol":
            parent[event["id"]] = event["id"]
            union(p.get("parent"), event["id"])
            for key in protocol_data(p):
                share(key, event["id"])
        elif kind == "run":
            run_protocol[event["id"]] = p["protocol"]
        elif kind == "result":
            raw = p["outputs"].get("raw_data")
            protocol = run_protocol.get(p["run"])
            if isinstance(raw, str) and protocol in parent:
                share(raw, protocol)
        elif kind == "claim":
            claim_protocol[event["id"]] = p["protocol"]
        elif kind == "review_obligation":
            obligation_claim[event["id"]] = p["claim"]
        elif kind == "replan_followup":
            source = claim_protocol.get(obligation_claim.get(p.get("obligation"), ""))
            union(source, p.get("protocol"))
        elif kind == "claim_link" and p.get("relation") == "supersedes":
            union(claim_protocol.get(p.get("source", "")), claim_protocol.get(p.get("target", "")))
    return {protocol: find(protocol) for protocol in parent}


def claim_family(history: list[dict[str, Any]], claim: str) -> list[str]:
    """F(c): claims on the lineage of the claim's protocol, in event order, including c."""
    roots = protocol_components(history)
    protocols = {event["id"]: event["payload"]["protocol"] for event in history
                 if event["kind"] == "claim"}
    root = roots[protocols[claim]]
    return [id for id, protocol in protocols.items() if roots.get(protocol) == root]


def open_negative_opinions(history: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Latest negative opinion of each (reviewer, claim) not followed by an approval of that claim.

    Every recorded negative review counts, whatever path wrote it, so missing
    provenance never lifts an objection. Until ADR 0018 step 7, any later
    approval of the same claim by the same reviewer withdraws it.
    """
    opinions: dict[tuple[str, str], dict[str, Any]] = {}
    for event in history:
        if event["kind"] != "review":
            continue
        key = (event["actor"], event["payload"]["claim"])
        if event["payload"]["verdict"] in NEGATIVE_VERDICTS:
            opinions[key] = event
        elif event["payload"]["verdict"] == "approve":
            opinions.pop(key, None)
    return opinions


class Admission:
    """Review decisions for one event snapshot, shared by every claim decision."""

    def __init__(self, store: Store, history: list[dict[str, Any]]):
        self.store, self.history = store, history
        self.roots = protocol_components(history)
        self.claim_protocol = {event["id"]: event["payload"]["protocol"] for event in history
                               if event["kind"] == "claim"}
        self.negatives = open_negative_opinions(history)
        self.approvals: dict[tuple[str, str], list[int]] = {}
        for event in history:
            if event["kind"] == "review" and event["payload"]["verdict"] == "approve":
                self.approvals.setdefault((event["actor"], event["payload"]["claim"]), []).append(event["seq"])
        self._families: dict[str, tuple[str, ...]] = {}
        self._obligations: list[dict[str, Any]] | None = None
        self._resolutions: dict[str, dict[str, Any]] | None = None

    def family(self, claim: str) -> tuple[str, ...]:
        if claim not in self._families:
            root = self.roots[self.claim_protocol[claim]]
            self._families[claim] = tuple(id for id, protocol in self.claim_protocol.items()
                                          if self.roots.get(protocol) == root)
        return self._families[claim]

    def obligations(self) -> list[dict[str, Any]]:
        """Replay-verified obligations, in event order."""
        if self._obligations is None:
            self._obligations = []
            if any(event["kind"] == "review_obligation" for event in self.history):
                from .replanning import _index
                verified = {event["id"] for state in _index(self.store, self.history).values()
                            for event in state["obligations"]}
                self._obligations = [event for event in self.history
                                     if event["kind"] == "review_obligation" and event["id"] in verified]
        return self._obligations

    def resolutions(self) -> dict[str, dict[str, Any]]:
        if self._resolutions is None:
            from .resolution import resolution_states
            self._resolutions = resolution_states(self.store, self.history) if self.obligations() else {}
        return self._resolutions

    def resolved_for(self, obligation: str, claim: str) -> bool:
        """An effective resolution exists for exactly this (obligation, claim) pair."""
        state = self.resolutions().get(obligation)
        return (state is not None and state["status"] == "reviewer_satisfied"
                and state["resolution"]["payload"]["claim"] == claim)

    def blocking_obligations(self, claim: str) -> list[dict[str, Any]]:
        """Family obligations without a resolution for this claim, plus linked-context ones.

        An obligation of a linked claim outside the family keeps blocking, as
        before ADR 0018, until it has any effective resolution.
        """
        family = set(self.family(claim))
        linked = set(resolve_context(self.history, claim).claim_ids) - family
        blocking = []
        for event in self.obligations():
            source = event["payload"]["claim"]
            if source in family and not self.resolved_for(event["id"], claim):
                blocking.append(event)
            elif source in linked and self.resolutions().get(event["id"], {}).get(
                    "status") != "reviewer_satisfied":
                blocking.append(event)
        return blocking

    def vetoes(self, claim: str) -> list[dict[str, Any]]:
        """Open negative opinions about family members that their owners have not withdrawn for claim."""
        family = set(self.family(claim))
        vetoes = []
        for (owner, subject), opinion in self.negatives.items():
            if subject not in family:
                continue
            if subject != claim and any(seq > opinion["seq"]
                                        for seq in self.approvals.get((owner, claim), [])):
                continue
            vetoes.append(opinion)
        return sorted(vetoes, key=lambda event: event["seq"])


def admission(store: Store, history: list[dict[str, Any]]) -> Admission:
    """Projection for this snapshot; reused within one read scope or command transaction."""
    memo = store._cas_memo
    if memo is None:
        return Admission(store, history)
    cache = memo.__dict__.setdefault("admission_projections", {})
    key = (len(history), history[-1]["hash"] if history else None)
    if key not in cache:
        cache[key] = Admission(store, history)
    return cache[key]
