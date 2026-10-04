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

Decisions and writes use a replaying projection: obligations, submissions and
resolutions are replay-verified first. Replay of historical receipts uses a
structural projection of the same prefix instead, because the enclosing replay
verifies each of those events against its own receipt; replaying again inside
would repeat the work for every earlier prefix.
"""

from __future__ import annotations

import json
from typing import Any

from .claim_context import resolve_context
from .kernel import Actor, Kernel, canonical_actor, independent_of, protocol_data, require
from .store import Store, canonical, digest

NEGATIVE_VERDICTS = frozenset({"request_changes", "reject"})
BLIND_V1 = "blind_initial_review_v1"


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


def _response_opinion(store: Store, event: dict[str, Any]) -> dict[str, Any] | None:
    """A completed, unsubmitted delivery whose bytes parse as a negative review response."""
    from .commands import parse_command
    try:
        decision = parse_command(store.read(event["payload"]["response"]).decode("utf-8"))
    except (UnicodeError, ValueError):
        return None
    if type(decision) is not dict or decision.get("verdict") not in NEGATIVE_VERDICTS:
        return None
    findings = decision.get("findings")
    actions = [finding["action"] for finding in findings
               if type(finding) is dict and type(finding.get("action")) is str
               and finding["action"].strip()] if type(findings) is list else []
    rationale = decision.get("rationale")
    p = event["payload"]
    return dict(id=event["id"], seq=event["seq"], hash=event["hash"], kind="review_response",
                actor=p["reviewer_actor"], role="reviewer",
                payload=dict(claim=p["claim"], verdict=decision["verdict"],
                             rationale=rationale if type(rationale) is str else "",
                             actions=actions or ["Unsubmitted negative review response"],
                             basis_hash=p["basis_hash"]))


class Admission:
    """Review decisions for one event snapshot, shared by every claim decision."""

    def __init__(self, store: Store, history: list[dict[str, Any]], *, replay: bool = True):
        self.store, self.history, self.replay = store, history, replay
        self.roots = protocol_components(history)
        self.claim_protocol = {event["id"]: event["payload"]["protocol"] for event in history
                               if event["kind"] == "claim"}
        self.position = {event["id"]: index for index, event in enumerate(history)}
        self._check_reviews()
        self._families: dict[str, tuple[str, ...]] = {}
        self._obligations: list[dict[str, Any]] | None = None
        self._resolutions: list[dict[str, Any]] | None = None
        self._submissions: list[dict[str, Any]] | None = None
        self.negatives = self._negatives()
        self.withdrawn = {(row["opinion"], event["payload"]["claim"])
                          for event in self.submissions()
                          for row in event["payload"].get("withdrawals", [])}

    def submissions(self) -> list[dict[str, Any]]:
        """Review submissions of this snapshot, replay-verified unless replaying a prefix."""
        if self._submissions is None:
            events = [event for event in self.history if event["kind"] == "review_submission"]
            if events and self.replay:
                from .review_submission import _index
                verified = {state["submission"]["id"] for state in _index(self.store, self.history).values()}
                events = [event for event in events if event["id"] in verified]
            self._submissions = events
        return self._submissions

    def _negatives(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Open negative opinions, including completed responses never submitted (§3.6)."""
        submitted = {event["payload"]["assignment"] for event in self.submissions()}
        records = [event for event in self.history if event["kind"] == "review"]
        for event in self.history:
            if (event["kind"] == "review_response" and event["payload"]["status"] == "completed"
                    and event["payload"]["assignment"] not in submitted):
                opinion = _response_opinion(self.store, event)
                if opinion is not None:
                    records.append(opinion)
        # Only an explicit withdrawal lifts an opinion (§3.5); a later approval does not.
        opinions: dict[tuple[str, str], dict[str, Any]] = {}
        for event in sorted(records, key=lambda record: record["seq"]):
            if event["payload"]["verdict"] in NEGATIVE_VERDICTS:
                opinions[(event["actor"], event["payload"]["claim"])] = event
        return opinions

    def _check_reviews(self) -> None:
        """Fail closed on reviews no kernel path can write (§3.3, audit A-10)."""
        reviews = [event for event in self.history if event["kind"] == "review"]
        if not reviews:
            return
        reader = Kernel(self.store, Actor("admission-reader", "observer"))
        for event in reviews:
            prefix = self.history[:self.position[event["id"]]]
            _, contributors, _ = reader._review_members(prefix, event["payload"]["claim"])
            require(event["role"] == "reviewer" and event["actor"] not in contributors,
                    f"review by a non-reviewer role or an evidence contributor: {event['id']}")

    def approval_defect(self, event: dict[str, Any]) -> str | None:
        """Why an approval does not count for decisions (§3.1), or None if it does."""
        submission = self._submitted().get(event["id"])
        if submission is None:
            return "approval lacks a verified review.submit chain"
        actor, claim = event["actor"], event["payload"]["claim"]
        index = self.position[event["id"]]
        prefix = self.history[:index]
        if not canonical_actor(actor):
            return "approval by a non-canonical historical reviewer ID"
        reader = Kernel(self.store, Actor("admission-reader", "observer"))
        if not independent_of(actor, reader._review_members(prefix, claim)[1]):
            return "approval by a reviewer whose key matches an evidence contributor"
        assignment = self.history[self.position[submission["payload"]["assignment"]]]
        manifest = json.loads(self.store.read(assignment["payload"]["bundle"]))
        if manifest.get("projection", manifest["policy"]) == BLIND_V1:
            before = self.history[:self.position[assignment["id"]]]
            roots = protocol_components(before)
            protocol = {e["id"]: e["payload"]["protocol"] for e in before if e["kind"] == "claim"}[claim]
            family_runs = {e["id"] for e in before if e["kind"] == "run"
                           and roots.get(e["payload"]["protocol"]) == roots[protocol]}
            observed = {row["run"] for row in manifest["context"]["observed_runs"]}
            if not family_runs <= observed:
                return "assignment context omits runs of the claim family"
        from .batch_analysis import KIND, UNVERIFIED_ORIGIN, analysis_provenance
        if (any(e["kind"] == KIND and e["payload"]["claim"] == claim
                and analysis_provenance(e) == UNVERIFIED_ORIGIN for e in prefix)
                and submission["payload"].get("analysis_verification") != "recomputed_match"):
            return "approval lacks recomputation of the unverified historical analysis"
        if (submission["payload"]["schema_version"] == 1
                and Admission(self.store, prefix, replay=False).own_findings(actor, claim)["opinions"]):
            return "approval did not withdraw the reviewer's own open opinions"
        return None

    def approvals(self, claim: str, basis: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Admissible and advisory approvals of claim at basis, in event order."""
        admissible, advisory = [], []
        for event in self.history:
            p = event["payload"]
            if (event["kind"] == "review" and p["verdict"] == "approve"
                    and p["claim"] == claim and p["basis_hash"] == basis):
                (advisory if self.approval_defect(event) else admissible).append(event)
        return admissible, advisory

    def _submitted(self) -> dict[str, dict[str, Any]]:
        return {event["payload"]["review"]: event for event in self.submissions()}

    def family(self, claim: str) -> tuple[str, ...]:
        if claim not in self._families:
            root = self.roots[self.claim_protocol[claim]]
            self._families[claim] = tuple(id for id, protocol in self.claim_protocol.items()
                                          if self.roots.get(protocol) == root)
        return self._families[claim]

    def lineage(self, claim: str) -> list[str]:
        """Protocols of K(protocol(claim)), in event order."""
        root = self.roots[self.claim_protocol[claim]]
        return [event["id"] for event in self.history
                if event["kind"] == "protocol" and self.roots.get(event["id"]) == root]

    def family_ledger(self, claim: str) -> list[dict[str, str]]:
        """Every run of the lineage that has a terminal result, as (run, result) revisions."""
        protocols = set(self.lineage(claim))
        runs = {event["id"]: event for event in self.history
                if event["kind"] == "run" and event["payload"]["protocol"] in protocols}
        ledger = []
        for event in self.history:
            run = runs.get(event["payload"]["run"]) if event["kind"] == "result" else None
            if run is not None:
                ledger.append(dict(run=run["id"], run_hash=run["hash"],
                                   result=event["id"], result_hash=event["hash"]))
        return ledger

    def family_ledger_digest(self, claim: str) -> str:
        return digest(canonical(self.family_ledger(claim)))

    def obligations(self) -> list[dict[str, Any]]:
        """Obligations of this snapshot, in event order; replay-verified for decisions."""
        if self._obligations is None:
            events = [event for event in self.history if event["kind"] == "review_obligation"]
            if events and self.replay:
                from .replanning import _index
                verified = {event["id"] for state in _index(self.store, self.history).values()
                            for event in state["obligations"]}
                events = [event for event in events if event["id"] in verified]
            self._obligations = events
        return self._obligations

    def resolutions(self) -> list[dict[str, Any]]:
        """Resolution records with their effective status, v1 and v2."""
        if self._resolutions is None:
            from .resolution import resolution_records
            self._resolutions = (resolution_records(self.store, self.history, replay=self.replay)
                                 if self.obligations() else [])
        return self._resolutions

    def resolved_for(self, obligation: str, claim: str) -> bool:
        """An effective resolution exists for exactly this (obligation, claim) pair."""
        return any(record["status"] == "reviewer_satisfied"
                   and record["resolution"]["payload"]["obligation"] == obligation
                   and record["resolution"]["payload"]["claim"] == claim
                   for record in self.resolutions())

    def resolved_anywhere(self, obligation: str) -> bool:
        return any(record["status"] == "reviewer_satisfied"
                   and record["resolution"]["payload"]["obligation"] == obligation
                   for record in self.resolutions())

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
            elif source in linked and not self.resolved_anywhere(event["id"]):
                blocking.append(event)
        return blocking

    def vetoes(self, claim: str) -> list[dict[str, Any]]:
        """Open negative opinions about family members that their owners have not withdrawn for claim."""
        family = set(self.family(claim))
        vetoes = []
        for (_, subject), opinion in self.negatives.items():
            if subject in family and (opinion["id"], claim) not in self.withdrawn:
                vetoes.append(opinion)
        return sorted(vetoes, key=lambda event: event["seq"])

    def own_findings(self, reviewer: str, claim: str) -> dict[str, list[dict[str, Any]]]:
        """The reviewer's own vetoes and unresolved obligations that bind claim.

        Ownership compares exact IDs: a historical variant of an ID is not
        provably the same actor (ADR 0018, step 5).
        """
        family = set(self.family(claim))
        return dict(
            opinions=[opinion for opinion in self.vetoes(claim) if opinion["actor"] == reviewer],
            obligations=[event for event in self.obligations()
                         if event["actor"] == reviewer and event["payload"]["claim"] in family
                         and not self.resolved_for(event["id"], claim)])

    def response_opinion(self, id: str) -> dict[str, Any] | None:
        return next((opinion for opinion in self.negatives.values() if opinion["id"] == id), None)


def admission(store: Store, history: list[dict[str, Any]], *, replay: bool = True) -> Admission:
    """Projection for this snapshot; reused within one read scope or command transaction."""
    memo = store._cas_memo
    if memo is None:
        return Admission(store, history, replay=replay)
    cache = memo.__dict__.setdefault("admission_projections", {})
    key = (len(history), history[-1]["hash"] if history else None, replay)
    if key not in cache:
        cache[key] = Admission(store, history, replay=replay)
    return cache[key]


def opinion_summary(opinion: dict[str, Any]) -> dict[str, Any]:
    """Manifest row of one own negative opinion; rationale and actions are the owner's own."""
    p = opinion["payload"]
    return dict(id=opinion["id"], hash=opinion["hash"], kind=opinion["kind"], claim=p["claim"],
                verdict=p["verdict"], rationale=p["rationale"], actions=list(p["actions"]))
