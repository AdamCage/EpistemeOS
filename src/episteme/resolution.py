"""Evidence-bound reviewer opinions resolving one planned review obligation.

The event records what the original reviewer accepted at one immutable evidence
revision. Its effective status is recomputed for later decisions; neither an
actor label nor a mechanical gate proves independent or correct scientific review.
"""

from __future__ import annotations

from typing import Any

from .batch import _index as batch_index
from .followup import _index as followup_index
from .kernel import Actor, GateError, Kernel, independent_of, require
from .replanning import _index as review_index
from .search import Search
from .store import Store


_REQUEST_FIELDS = {"obligation", "claim", "expected_basis", "review_rationale",
                   "resolution_rationale", "evidence_refs", "link_assessments"}
_RESOLUTION_FIELDS = {"schema_version", "obligation", "obligation_hash", "followup",
                      "followup_hash", "source_review", "source_review_hash", "source_claim",
                      "source_claim_hash", "source_basis_hash", "claim", "claim_hash",
                      "basis_hash", "review", "review_hash", "terminal", "terminal_hash",
                      "evidence_refs", "resolution_rationale", "disposition"}


def _review_payload(store: Store, before: list[dict[str, Any]], claim: str, actor: str,
                    basis: str, rationale: str,
                    link_assessments: dict[str, dict[str, Any]] | None,
                    keyed: bool = False) -> dict[str, Any]:
    kernel = Kernel(store, Actor(actor, "reviewer"))
    context, contributors, admissible = kernel._review_members(before, claim)
    require(independent_of(actor, contributors) if keyed else actor not in contributors,
            "resolution reviewer contributed to child evidence")
    require(not context.link_ids or link_assessments is not None,
            "linked child claim requires explicit relation assessments")
    require(type(rationale) is str and bool(rationale.strip()) and len(rationale) <= 4096
            and "\x00" not in rationale, "resolution review needs bounded rationale")
    payload = dict(claim=claim, verdict="approve", rationale=rationale,
                   actions=[], basis_hash=basis)
    if link_assessments is not None:
        kernel._validate_assessments(link_assessments, set(context.link_ids), admissible, "approve")
        acknowledged = {id for assessment in link_assessments.values()
                        for id in assessment["evidence"]}
        require(kernel._context_findings(before, claim)[1] <= acknowledged,
                "resolution review omits open linked findings")
        accepted_replacements = {
            kernel._get(before, id, "claim_link")["payload"]["target"]
            for id, assessment in link_assessments.items()
            if assessment["judgment"] == "accepted"
            and kernel._get(before, id, "claim_link")["payload"]["relation"] == "supersedes"}
        for id, assessment in link_assessments.items():
            if assessment["judgment"] != "accepted":
                continue
            link = kernel._get(before, id, "claim_link")["payload"]
            source = kernel._get(before, link["source"], "claim")
            current_source = (link["relation"] == "supersedes"
                              and source["id"] not in accepted_replacements)
            basis_history = (before if current_source else
                             [event for event in before if event["seq"] <= source["seq"]])
            require(kernel._gate_local(basis_history, source["id"])["passed"],
                    f"accepted relation has mechanically unqualified source: {id}")
        payload.update(review_schema_version=2, link_assessments=link_assessments)
    return payload


def _admit(store: Store, before: list[dict[str, Any]], *, obligation: str, claim: str,
           expected_basis: str, review_rationale: str, resolution_rationale: str,
           evidence_refs: list[str], link_assessments: dict[str, dict[str, Any]] | None,
           actor: str, study_id: str, keyed: bool = False) -> dict[str, Any]:
    """Check current mechanical/context prerequisites without changing history."""
    kernel = Kernel(store, Actor(actor, "reviewer"))
    obligations = {event["id"]: event for state in review_index(store, before).values()
                   for event in state["obligations"]}
    require(obligation in obligations, "unknown typed review obligation")
    source = obligations[obligation]
    require(source["payload"]["kind"] == "discriminating_experiment",
            "only a discriminating-experiment obligation can be resolved here")
    bound = followup_index(store, before).get(obligation)
    require(bound is not None, "obligation has no frozen follow-up experiment")
    followup = bound["followup"]
    source_review = kernel._get(before, source["payload"]["review"], "review")
    source_claim = kernel._get(before, source["payload"]["claim"], "claim")
    require(actor == source_review["actor"],
            "only the original negative reviewer can resolve this obligation")
    latest_source = next((event for event in reversed(before) if event["kind"] == "review"
                          and event["actor"] == actor
                          and event["payload"]["claim"] == source_claim["id"]), None)
    require(latest_source is not None and latest_source["id"] == source_review["id"],
            "original negative review is no longer this reviewer's latest source opinion")
    child = kernel._get(before, claim, "claim")
    protocol = bound["protocol"]
    require(child["payload"]["protocol"] == protocol["id"]
            and child["payload"]["scope"] == source_claim["payload"]["scope"],
            "resolved claim must use the frozen child protocol and source scope")
    planning = protocol["payload"].get("planning")
    require(type(planning) is dict and planning["study_id"] == study_id,
            "resolution command study differs from its frozen follow-up")
    source_gate = kernel._gate(before, source_claim["id"])
    source_basis, _ = kernel._basis(before, source_claim["id"])
    require(source_gate["passed"] and source_gate["basis_hash"] == source_basis
            and source_basis == source["payload"]["basis_hash"]
            and source_basis == followup["payload"]["basis_hash"],
            "source evidence differs from the original negative review basis")
    gate = kernel._gate(before, child["id"])
    child_basis, _ = kernel._basis(before, child["id"])
    require(type(expected_basis) is str and gate["passed"]
            and gate["basis_hash"] == child_basis == expected_basis,
            "child evidence basis is stale or mechanically unqualified")
    require(type(resolution_rationale) is str and bool(resolution_rationale.strip())
            and len(resolution_rationale) <= 4096 and "\x00" not in resolution_rationale,
            "resolution needs a bounded assessment of the original closure criterion")

    terminal, citations = _followup_evidence(store, before, bound, child, evidence_refs,
                                             descendant=False)
    review_payload = _review_payload(store, before, child["id"], actor, expected_basis,
                                     review_rationale, link_assessments, keyed)
    return dict(obligation=source, followup=followup, source_review=source_review,
                source_claim=source_claim, source_basis=source_basis, child=child,
                basis=child_basis, terminal=terminal, citations=citations,
                review_payload=review_payload)


def _descends(history: list[dict[str, Any]], protocol: str, ancestor: str) -> bool:
    seen: set[str] = set()
    current: str | None = protocol
    while current is not None and current not in seen:
        if current == ancestor:
            return True
        seen.add(current)
        current = Kernel._get(history, current, "protocol")["payload"]["parent"]
    return False


def _followup_evidence(store: Store, before: list[dict[str, Any]], bound: dict[str, Any],
                       child: dict[str, Any], evidence_refs: Any, *, descendant: bool
                       ) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Completed follow-up terminal, new results and exact citations for one claim.

    A claim on the frozen child protocol must be the one its terminal bound; a
    claim on a descendant protocol (ADR 0018) cites its own new results.
    """
    followup, protocol = bound["followup"], bound["protocol"]
    on_child = child["payload"]["protocol"] == protocol["id"]
    require(on_child or (descendant and _descends(before, child["payload"]["protocol"], protocol["id"])),
            "resolved claim must use the frozen child protocol or its descendant")
    node = bound["node"]
    state = Search._projection(before, node["payload"]["tree"])
    require(state["nodes"][node["id"]]["state"] == "completed",
            "follow-up experiment has no completed terminal selection")
    terminals = [event for event in before if event["kind"] == "search_terminal"
                 and event["payload"]["node"] == node["id"]]
    require(terminals and terminals[-1]["payload"]["status"] == "completed",
            "follow-up terminal is missing or did not complete")
    terminal = terminals[-1]
    require(terminal["seq"] > followup["seq"],
            "follow-up cannot be resolved from a preceding technical result")
    if on_child and "batch" in terminal["payload"]:
        batches = batch_index(store, before)
        batch = batches.get(terminal["payload"]["batch"])
        require(batch is not None and batch["terminal"]["id"] == terminal["id"]
                and batch["settlement"]["payload"]["status"] == "completed",
                "follow-up batch lacks its verified complete settlement")
        require(terminal["payload"]["claim"] is None and child["seq"] > terminal["seq"]
                and set(child["payload"]["evidence"]) == set(terminal["payload"]["runs"]),
                "child claim must cite exactly the completed batch roster")
    elif on_child:
        require(terminal["payload"]["claim"] == child["id"]
                and terminal["payload"]["run"] in child["payload"]["evidence"],
                "single-run terminal must bind the child claim and its selected run")
    results = [event for event in before if event["kind"] == "result"
               and event["payload"]["run"] in child["payload"]["evidence"]]
    require(len(results) >= 2 and all(event["seq"] > followup["seq"] for event in results),
            "resolution needs new completed primary and reanalysis results")
    required_refs = {child["id"], *(event["id"] for event in results)}
    require(type(evidence_refs) is list and len(evidence_refs) == len(required_refs)
            and all(type(ref) is str for ref in evidence_refs)
            and set(evidence_refs) == required_refs,
            "resolution citations must cover the child claim and every cited run result")
    by_id = {event["id"]: event for event in before}
    return terminal, [dict(id=ref, hash=by_id[ref]["hash"]) for ref in evidence_refs]


def admit_v2(store: Store, before: list[dict[str, Any]], *, obligation: dict[str, Any],
             claim: dict[str, Any], basis: str, rationale: Any, evidence_refs: Any,
             study_id: str, replay: bool) -> dict[str, Any]:
    """Check one resolution carried by a reconsideration approval (ADR 0018 §3.5).

    The caller has already checked that the submitting reviewer owns the
    obligation, that it binds this claim's family and is unresolved for it,
    and that the claim's basis is current and mechanically passed. During
    replay the follow-up binding is read structurally; its own receipt is
    verified by the enclosing history replay.
    """
    kernel = Kernel(store, Actor("resolution-validator", "observer"))
    op = obligation["payload"]
    require(type(rationale) is str and bool(rationale.strip()) and len(rationale) <= 4096
            and "\x00" not in rationale,
            "resolution needs a bounded assessment of the original closure criterion")
    source_review = kernel._get(before, op["review"], "review")
    source_claim = kernel._get(before, op["claim"], "claim")
    source_gate = kernel._gate(before, source_claim["id"])
    source_basis, _ = kernel._basis(before, source_claim["id"])
    require(source_gate["passed"] and source_gate["basis_hash"] == source_basis == op["basis_hash"],
            "source evidence differs from the original negative review basis")
    followup = terminal = None
    if op["kind"] == "discriminating_experiment":
        if replay:
            by_id = {event["id"]: event for event in before}
            bound = next((dict(followup=event, protocol=by_id[event["payload"]["protocol"]],
                               node=by_id[event["payload"]["experiment_node"]])
                          for event in before if event["kind"] == "replan_followup"
                          and event["payload"]["obligation"] == obligation["id"]), None)
        else:
            bound = followup_index(store, before).get(obligation["id"])
        require(bound is not None, "obligation has no frozen follow-up experiment")
        followup = bound["followup"]
        require(source_basis == followup["payload"]["basis_hash"],
                "source evidence differs from the frozen follow-up basis")
        planning = bound["protocol"]["payload"].get("planning")
        require(type(planning) is dict and planning["study_id"] == study_id,
                "resolution command study differs from its frozen follow-up")
        require(claim["payload"]["scope"] == source_claim["payload"]["scope"],
                "resolved claim must keep the source scope")
        terminal, citations = _followup_evidence(store, before, bound, claim, evidence_refs,
                                                 descendant=True)
    elif op["kind"] == "narrow_claim":
        from .review_admission import claim_family
        require(claim["seq"] > obligation["seq"]
                and claim["id"] in claim_family(before, source_claim["id"])
                and claim["payload"]["scope"] == source_claim["payload"]["scope"],
                "a narrow claim must follow the finding in its family with the source scope")
        _, _, admissible = kernel._review_members(before, claim["id"])
        require(type(evidence_refs) is list and 1 <= len(evidence_refs) <= 32
                and all(type(ref) is str and ref in admissible | {claim["id"]} for ref in evidence_refs)
                and len(set(evidence_refs)) == len(evidence_refs) and claim["id"] in evidence_refs,
                "narrow-claim resolution must cite the claim and only its review context")
        by_id = {event["id"]: event for event in before}
        citations = [dict(id=ref, hash=by_id[ref]["hash"]) for ref in evidence_refs]
    else:
        raise GateError(f"{op['kind']} findings cannot be resolved under ADR 0018")
    return dict(kind=op["kind"], obligation=obligation, source_review=source_review,
                source_claim=source_claim, source_basis=source_basis, claim=claim, basis=basis,
                followup=followup, terminal=terminal, citations=citations, rationale=rationale)


def payload_v2(refs: dict[str, Any], review: dict[str, Any], assignment: dict[str, Any]) -> dict[str, Any]:
    followup, terminal = refs["followup"], refs["terminal"]
    return dict(schema_version=2, kind=refs["kind"],
                obligation=refs["obligation"]["id"], obligation_hash=refs["obligation"]["hash"],
                followup=None if followup is None else followup["id"],
                followup_hash=None if followup is None else followup["hash"],
                source_review=refs["source_review"]["id"],
                source_review_hash=refs["source_review"]["hash"],
                source_claim=refs["source_claim"]["id"],
                source_claim_hash=refs["source_claim"]["hash"],
                source_basis_hash=refs["source_basis"],
                claim=refs["claim"]["id"], claim_hash=refs["claim"]["hash"],
                basis_hash=refs["basis"], review=review["id"], review_hash=review["hash"],
                assignment=assignment["id"], assignment_hash=assignment["hash"],
                terminal=None if terminal is None else terminal["id"],
                terminal_hash=None if terminal is None else terminal["hash"],
                evidence_refs=refs["citations"], resolution_rationale=refs["rationale"],
                disposition="reviewer_satisfied")


def _payload(refs: dict[str, Any], review: dict[str, Any], rationale: str) -> dict[str, Any]:
    return dict(schema_version=1,
                obligation=refs["obligation"]["id"], obligation_hash=refs["obligation"]["hash"],
                followup=refs["followup"]["id"], followup_hash=refs["followup"]["hash"],
                source_review=refs["source_review"]["id"],
                source_review_hash=refs["source_review"]["hash"],
                source_claim=refs["source_claim"]["id"],
                source_claim_hash=refs["source_claim"]["hash"],
                source_basis_hash=refs["source_basis"],
                claim=refs["child"]["id"], claim_hash=refs["child"]["hash"],
                basis_hash=refs["basis"], review=review["id"], review_hash=review["hash"],
                terminal=refs["terminal"]["id"], terminal_hash=refs["terminal"]["hash"],
                evidence_refs=refs["citations"], resolution_rationale=rationale,
                disposition="reviewer_satisfied")


def _index(store: Store, history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Replay-check each exact two-event v1 decision at its historical prefix.

    Schema 2 resolutions are part of ``review.submit`` receipts; they must be
    exactly the ones that replay of those receipts admits.
    """
    all_resolutions = {event["id"]: event for event in history
                       if event["kind"] == "review_obligation_resolution"
                       and event["payload"].get("schema_version") != 2}
    v2 = {event["id"] for event in history if event["kind"] == "review_obligation_resolution"
          and event["payload"].get("schema_version") == 2}
    if v2:
        from .review_submission import _index as submission_index
        admitted = {event["id"] for state in submission_index(store, history).values()
                    for event in state["resolutions"]}
        require(v2 == admitted, "review obligation resolution lacks its original command receipt")
    receipts = [receipt for receipt in store.receipts()
                if receipt["request"]["action"] == "replanning.resolve_obligation"
                and receipt["after_revision"] <= len(history)]
    seen: set[str] = set()
    by_obligation: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        request = receipt["request"]
        args = request["payload"]
        require(request["version"] == 1 and set(args) == _REQUEST_FIELDS,
                "invalid historical resolution request")
        context = receipt["context"]
        require(context["role"] == "reviewer", "resolution requires a reviewer receipt")
        before = history[:receipt["before_revision"]]
        events = history[receipt["before_revision"]:receipt["after_revision"]]
        require([event["kind"] for event in events] == ["review", "review_obligation_resolution"]
                and [event["id"] for event in events] == receipt["event_ids"],
                "resolution needs its complete original review and decision receipt")
        review, resolution = events
        require(args["obligation"] not in by_obligation,
                "review obligation has more than one resolution decision")
        refs = _admit(store, before, **args, actor=context["actor"], study_id=context["study_id"])
        require(review["actor"] == context["actor"] and review["role"] == "reviewer"
                and review["payload"] == refs["review_payload"],
                "resolution review differs from its requested opinion or evidence basis")
        expected = _payload(refs, review, args["resolution_rationale"])
        require(set(resolution["payload"]) == _RESOLUTION_FIELDS
                and resolution["payload"] == expected
                and resolution["actor"] == context["actor"]
                and resolution["role"] == "reviewer"
                and receipt["result"] == resolution["id"],
                "resolution differs from its reviewer, citations, or frozen evidence")
        seen.add(resolution["id"])
        by_obligation[args["obligation"]] = dict(resolution=resolution, review=review)
    require(seen == set(all_resolutions),
            "review obligation resolution lacks its original command receipt")
    return by_obligation


def effective_status(store: Store, history: list[dict[str, Any]],
                     state: dict[str, Any] | None) -> str:
    """A past opinion stays recorded, while later evidence can invalidate coverage."""
    if state is None:
        return "open"
    p = state["resolution"]["payload"]
    reader = Kernel(store, Actor("resolution-reader", "observer"))
    try:
        source_gate = reader._gate(history, p["source_claim"])
        child_gate = reader._gate(history, p["claim"])
        source_basis, _ = reader._basis(history, p["source_claim"])
        child_basis, _ = reader._basis(history, p["claim"])
    except (ValueError, KeyError, TypeError):
        return "stale_resolution"
    source_latest = next((event for event in reversed(history) if event["kind"] == "review"
                          and event["actor"] == state["review"]["actor"]
                          and event["payload"]["claim"] == p["source_claim"]), None)
    child_latest = next((event for event in reversed(history) if event["kind"] == "review"
                         and event["actor"] == state["review"]["actor"]
                         and event["payload"]["claim"] == p["claim"]), None)
    obligation = reader._get(history, p["obligation"], "review_obligation")
    # A v2 resolution belongs to a reconsideration that withdraws the source
    # opinion for this claim; the source opinion need not stay the latest one.
    source_current = (p.get("schema_version") == 2
                      or (source_latest is not None and source_latest["id"] == p["source_review"]))
    if (source_gate["passed"] and source_gate["basis_hash"] == source_basis
            == p["source_basis_hash"] == obligation["payload"]["basis_hash"]
            and child_gate["passed"] and child_gate["basis_hash"] == child_basis == p["basis_hash"]
            and source_current
            and child_latest is not None and child_latest["id"] == p["review"]):
        return "reviewer_satisfied"
    return "stale_resolution"


def resolution_records(store: Store, history: list[dict[str, Any]], *,
                       replay: bool = True) -> list[dict[str, Any]]:
    """Every resolution, v1 and v2, with its effective status, in event order.

    ``replay=False`` is for replay of an enclosing receipt on a prefix: it reads
    the events structurally and leaves their verification to that replay.
    """
    if replay:
        _index(store, history)
    by_id = {event["id"]: event for event in history}
    return [dict(resolution=event, review=by_id[event["payload"]["review"]],
                 status=effective_status(store, history, dict(
                     resolution=event, review=by_id[event["payload"]["review"]])))
            for event in history if event["kind"] == "review_obligation_resolution"]


def resolution_states(store: Store, history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """One state per obligation: its v1 decision, else its latest v2 resolution for any claim."""
    states = {record["resolution"]["payload"]["obligation"]: record
              for record in resolution_records(store, history)
              if record["resolution"]["payload"].get("schema_version") == 2}
    for obligation, state in _index(store, history).items():
        states[obligation] = dict(**state, status=effective_status(store, history, state))
    return states


class Resolution:
    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def resolve_obligation(self, *, obligation: str, claim: str, expected_basis: str,
                           review_rationale: str, resolution_rationale: str,
                           evidence_refs: list[str],
                           link_assessments: dict[str, dict[str, Any]] | None = None) -> str:
        """Atomically approve a bounded child claim and address one source finding."""
        require(self.store._command_context is not None and self.actor.role == "reviewer",
                "resolution requires a reviewer CommandService transaction")
        before = self.store.events()
        require(obligation not in _index(self.store, before),
                "review obligation already has a resolution decision")
        context = self.store._command_context
        refs = _admit(self.store, before, obligation=obligation, claim=claim,
                      expected_basis=expected_basis, review_rationale=review_rationale,
                      resolution_rationale=resolution_rationale, evidence_refs=evidence_refs,
                      link_assessments=link_assessments, actor=self.actor.id,
                      study_id=context["study_id"], keyed=True)
        kernel = Kernel(self.store, self.actor)
        if link_assessments is None:
            review_id = kernel.review(claim, verdict="approve", rationale=review_rationale,
                                      actions=[], expected_basis=expected_basis)
        else:
            review_id = kernel.review_with_links(claim, verdict="approve",
                rationale=review_rationale, actions=[], expected_basis=expected_basis,
                link_assessments=link_assessments)
        review = kernel._get(self.store.events(), review_id, "review")
        return kernel._write(self.store.events(), "review_obligation_resolution",
                             _payload(refs, review, resolution_rationale), {"reviewer"})
