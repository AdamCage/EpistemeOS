"""Bind a local reviewer decision to an assigned, delivered evidence revision.

The caller-supplied reviewer ID and provider response are not authenticated.
Recording their provenance does not establish independent scientific judgment.

Schema 2 (ADR 0018 §3.5) adds reconsideration: under ``veto_reconsideration_v1``
an approval must withdraw, for this claim only, every open negative opinion of
its reviewer in the claim family, and may resolve that reviewer's own
obligations whose kind has an admission rule.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .kernel import Actor, Kernel, independent_of, require
from .replanning import _findings, _OBLIGATION_FIELDS
from .review_assignment import RECONSIDERATION
from .reviewer_controller import MAX_RESPONSE_BYTES, _index as delivery_index
from .store import Store


_RESPONSE_FIELDS = {"schema_version", "assignment", "bundle", "claim", "basis_hash",
                    "reviewer_actor", "verdict", "rationale", "findings", "link_assessments"}
_RESPONSE_FIELDS_V2 = _RESPONSE_FIELDS | {"withdrawals", "resolutions"}
_REQUEST_FIELDS = {"assignment", "response", "expected_basis"}
_SUBMISSION_FIELDS = {"schema_version", "assignment", "assignment_hash", "dispatch",
                      "dispatch_hash", "response_event", "response_event_hash", "response",
                      "bundle", "claim", "basis_hash", "reviewer_actor", "review",
                      "review_hash", "obligations", "identity_assurance", "read_isolation"}
_SUBMISSION_FIELDS_V2 = _SUBMISSION_FIELDS | {"policy", "withdrawals", "resolutions",
                                              "family_ledger_digest", "analysis_verification"}


def _text(value: Any, limit: int) -> bool:
    return type(value) is str and bool(value.strip()) and len(value) <= limit and "\x00" not in value


def _analysis_verification(store: Store, history: list[dict[str, Any]], claim: str,
                           verdict: str, *, recompute: bool) -> str:
    """Approval of a claim admitted by unverified schema 1 analysis requires recomputation.

    Only the command reruns the adapter; replay checks which value the snapshot requires.
    """
    from .batch_analysis import KIND, UNVERIFIED_ORIGIN, analysis_provenance, recomputation
    unverified = [event for event in history if event["kind"] == KIND
                  and event["payload"]["claim"] == claim
                  and analysis_provenance(event) == UNVERIFIED_ORIGIN]
    if verdict != "approve" or not unverified:
        return "not_required"
    if recompute:
        for event in unverified:
            outcome = recomputation(store, history, event)
            require(outcome == "matched",
                    f"approval needs the historical analysis recomputed by its adapter: {outcome}")
    return "recomputed_match"


def _reconsideration(store: Store, history: list[dict[str, Any]], state: dict[str, Any],
                     decision: dict[str, Any], claim: dict[str, Any], basis: str,
                     *, in_replay: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Check withdrawals and resolutions against the reviewer's own findings on this snapshot."""
    from .resolution import admit_v2
    from .review_admission import admission
    withdrawals, resolutions = decision["withdrawals"], decision["resolutions"]
    require(type(withdrawals) is list and type(resolutions) is list
            and len(withdrawals) <= 256 and len(resolutions) <= 32,
            "reconsideration needs bounded withdrawal and resolution lists")
    if decision["verdict"] != "approve":
        require(withdrawals == [] and resolutions == [],
                "only a reconsideration approval can withdraw or resolve findings")
        return [], []
    reviewer = state["assignment"]["payload"]["reviewer_actor"]
    own = admission(store, history, replay=not in_replay).own_findings(reviewer, claim["id"])
    opinions = {opinion["id"]: opinion for opinion in own["opinions"]}
    require(all(type(row) is dict and set(row) == {"opinion", "rationale"}
                and _text(row["rationale"], 4096) for row in withdrawals),
            "each withdrawal needs an opinion ID and a bounded rationale")
    listed = [row["opinion"] for row in withdrawals]
    require(len(set(listed)) == len(listed) and set(listed) == set(opinions),
            "reconsideration approval must withdraw exactly the reviewer's open opinions in the claim family")
    obligations = {event["id"]: event for event in own["obligations"]}
    require(all(type(row) is dict and set(row) == {"obligation", "rationale", "evidence_refs"}
                for row in resolutions), "invalid resolution fields")
    named = [row["obligation"] for row in resolutions]
    require(len(set(named)) == len(named) and set(named) <= set(obligations),
            "resolutions may name only the reviewer's own obligations unresolved for this claim")
    study_id = state["assignment"]["payload"]["study_id"]
    checked = [admit_v2(store, history, obligation=obligations[row["obligation"]], claim=claim,
                        basis=basis, rationale=row["rationale"], evidence_refs=row["evidence_refs"],
                        study_id=study_id, replay=in_replay)
               for row in resolutions]
    return ([dict(opinion=row["opinion"], opinion_hash=opinions[row["opinion"]]["hash"],
                  rationale=row["rationale"]) for row in withdrawals], checked)


def _decision(store: Store, history: list[dict[str, Any]], *, assignment: str,
              response: str, expected_basis: str, reviewer_actor: str,
              study_id: str, receipts: list[dict[str, Any]] | None = None,
              keyed: bool = False) -> dict[str, Any]:
    """Recheck the current mechanical basis and exact completed delivery.

    New submissions compare normalized actor keys (ADR 0018); replay keeps the
    exact comparison under which a historical submission was admitted.
    """
    in_replay = not keyed
    deliveries = delivery_index(store, history, receipts=receipts)
    require(assignment in deliveries, "review submission needs a verified assignment dispatch")
    state = deliveries[assignment]
    assigned, completed = state["assignment"], state["response"]
    p = assigned["payload"]
    require(p["reviewer_actor"] == reviewer_actor and p["study_id"] == study_id,
            "review submission actor or study differs from assignment")
    require(completed is not None and completed["payload"]["status"] == "completed"
            and completed["payload"]["response"] == response,
            "review submission needs the exact completed delivery response")
    raw = store.read(response)
    require(0 < len(raw) <= MAX_RESPONSE_BYTES, "review response exceeds byte limit")
    from .commands import parse_command

    try:
        decision = parse_command(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"invalid reviewer response JSON: {exc}") from exc
    reconsideration = p["policy"] == RECONSIDERATION
    require(type(decision) is dict and type(decision.get("schema_version")) is int
            and (set(decision) == _RESPONSE_FIELDS_V2 and decision["schema_version"] == 2
                 if reconsideration else
                 set(decision) == _RESPONSE_FIELDS and decision["schema_version"] == 1),
            "review response must match v2 fields under reconsideration, otherwise v1")
    for field, expected in (("assignment", assignment), ("bundle", p["bundle"]),
                            ("claim", p["claim"]), ("basis_hash", expected_basis),
                            ("reviewer_actor", reviewer_actor)):
        require(decision[field] == expected and type(decision[field]) is str,
                f"review response {field} differs from frozen assignment")
    require(expected_basis == p["basis_hash"], "review assignment basis differs from response")
    require(decision["verdict"] in {"approve", "request_changes", "reject"},
            "invalid review response verdict")
    require(type(decision["rationale"]) is str and 0 < len(decision["rationale"].strip()) <= 8192
            and "\x00" not in decision["rationale"], "review response needs bounded rationale")
    kernel = Kernel(store, Actor(reviewer_actor, "reviewer"))
    claim = kernel._get(history, p["claim"], "claim")
    gate = kernel._gate(history, p["claim"])
    basis, _ = kernel._basis(history, p["claim"])
    require(gate["passed"] and gate["basis_hash"] == basis == expected_basis,
            "review submission needs the assigned current mechanical basis")
    linked, contributors, admissible = kernel._review_members(history, p["claim"])
    require(independent_of(reviewer_actor, contributors) if keyed else reviewer_actor not in contributors,
            "reviewer contributed to submitted claim context")
    findings = decision["findings"]
    if decision["verdict"] == "approve":
        require(findings == [], "approval cannot contain open findings")
        normalized: list[dict[str, Any]] = []
    else:
        normalized = _findings(history, p["claim"], findings, admissible)
    assessments = decision["link_assessments"]
    require(not linked.link_ids or assessments is not None,
            "linked claim review needs explicit link assessments")
    if assessments is not None:
        kernel._validate_assessments(assessments, set(linked.link_ids), admissible,
                                     decision["verdict"])
        if decision["verdict"] == "approve":
            acknowledged = {id for assessment in assessments.values()
                            for id in assessment["evidence"]}
            require(kernel._context_findings(history, p["claim"])[1] <= acknowledged,
                    "approval must acknowledge open linked-context reviews")
            accepted_replacements = {
                kernel._get(history, id, "claim_link")["payload"]["target"]
                for id, assessment in assessments.items()
                if assessment["judgment"] == "accepted"
                and kernel._get(history, id, "claim_link")["payload"]["relation"] == "supersedes"}
            for id, assessment in assessments.items():
                if assessment["judgment"] != "accepted":
                    continue
                link = kernel._get(history, id, "claim_link")["payload"]
                source = kernel._get(history, link["source"], "claim")
                current_source = (link["relation"] == "supersedes"
                                  and source["id"] not in accepted_replacements)
                basis_history = (history if current_source else
                                 [e for e in history if e["seq"] <= source["seq"]])
                checked = kernel._gate_local(basis_history, source["id"])
                require(checked["passed"],
                        f"accepted link has mechanically unqualified source: {id}")
    withdrawals: list[dict[str, Any]] = []
    resolutions: list[dict[str, Any]] = []
    if reconsideration:
        withdrawals, resolutions = _reconsideration(store, history, state, decision, claim, basis,
                                                    in_replay=in_replay)
    return dict(state=state, decision=decision, claim=claim, findings=normalized,
                withdrawals=withdrawals, resolutions=resolutions)


def _submission_payload(state: dict[str, Any], response: str,
                        review: dict[str, Any], obligations: list[str]) -> dict[str, Any]:
    assignment, dispatch, completed = (state[key] for key in
                                       ("assignment", "dispatch", "response"))
    p = assignment["payload"]
    return dict(schema_version=1, assignment=assignment["id"],
                assignment_hash=assignment["hash"], dispatch=dispatch["id"],
                dispatch_hash=dispatch["hash"], response_event=completed["id"],
                response_event_hash=completed["hash"], response=response,
                bundle=p["bundle"], claim=p["claim"], basis_hash=p["basis_hash"],
                reviewer_actor=p["reviewer_actor"], review=review["id"],
                review_hash=review["hash"], obligations=obligations,
                identity_assurance="caller_declared", read_isolation="not_enforced")


def _submission_payload_v2(checked: dict[str, Any], response: str, review: dict[str, Any],
                           obligations: list[str], resolutions: list[str], *,
                           ledger_digest: str, analysis_verification: str) -> dict[str, Any]:
    payload = _submission_payload(checked["state"], response, review, obligations)
    payload.update(schema_version=2, policy=checked["state"]["assignment"]["payload"]["policy"],
                   withdrawals=checked["withdrawals"], resolutions=resolutions,
                   family_ledger_digest=ledger_digest,
                   analysis_verification=analysis_verification)
    return payload


def _ledger_digest(store: Store, history: list[dict[str, Any]], claim: str, *, in_replay: bool) -> str:
    from .review_admission import admission
    return admission(store, history, replay=not in_replay).family_ledger_digest(claim)


def _index(store: Store, history: list[dict[str, Any]], *,
           receipts: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Replay-check every submitted verdict, typed obligation, resolution and receipt."""
    from .resolution import payload_v2
    source = store.receipts() if receipts is None else receipts
    relevant = [receipt for receipt in source
                if receipt["request"]["action"] == "review.submit"
                and receipt["after_revision"] <= len(history)]
    submissions = {e["id"] for e in history if e["kind"] == "review_submission"}
    seen: set[str] = set()
    states: dict[str, dict[str, Any]] = {}
    for receipt in relevant:
        context, request = receipt["context"], receipt["request"]
        args = request["payload"]
        require(context["role"] == "reviewer" and request["version"] == 1
                and set(args) == _REQUEST_FIELDS, "invalid historical review submission request")
        before = history[:receipt["before_revision"]]
        checked = _decision(
            store, before, assignment=args["assignment"], response=args["response"],
            expected_basis=args["expected_basis"], reviewer_actor=context["actor"],
            study_id=context["study_id"], receipts=source)
        delivery, decision, claim = checked["state"], checked["decision"], checked["claim"]
        findings, resolutions = checked["findings"], checked["resolutions"]
        events = history[receipt["before_revision"]:receipt["after_revision"]]
        require(len(events) == len(findings) + len(resolutions) + 2
                and receipt["event_ids"] == [event["id"] for event in events]
                and events[0]["kind"] == "review"
                and all(event["kind"] == ("review_obligation" if findings else
                                          "review_obligation_resolution")
                        for event in events[1:-1])
                and events[-1]["kind"] == "review_submission",
                "review submission needs its exact review, obligations or resolutions, "
                "and provenance event")
        require(all(event["actor"] == context["actor"] and event["role"] == "reviewer"
                    for event in events), "review submission actor differs from command")
        review, middle, provenance = events[0], events[1:-1], events[-1]
        obligations = middle if findings else []
        resolved = [] if findings else middle
        expected_review = dict(claim=claim["id"], verdict=decision["verdict"],
                               rationale=decision["rationale"],
                               actions=[finding["action"] for finding in findings],
                               basis_hash=args["expected_basis"])
        if decision["link_assessments"] is not None:
            expected_review.update(review_schema_version=2,
                                   link_assessments=decision["link_assessments"])
        require(review["payload"] == expected_review,
                "review differs from delivered response or historical basis")
        for index, (event, finding) in enumerate(zip(obligations, findings)):
            expected = dict(schema_version=1, review=review["id"],
                            review_hash=review["hash"], claim=claim["id"],
                            claim_hash=claim["hash"], basis_hash=args["expected_basis"],
                            finding_index=index, **finding)
            require(set(event["payload"]) == _OBLIGATION_FIELDS
                    and event["payload"] == expected,
                    "submitted obligation differs from delivered reviewer finding")
        for event, refs in zip(resolved, resolutions):
            require(event["payload"] == payload_v2(refs, review, delivery["assignment"]),
                    "submitted resolution differs from its reconsideration response")
        ids = [event["id"] for event in obligations]
        resolution_ids = [event["id"] for event in resolved]
        version = provenance["payload"].get("schema_version")
        require(version in (1, 2) and (version == 2 or delivery["assignment"]["payload"]["policy"]
                                       != RECONSIDERATION and not resolved),
                "unsupported review submission schema")
        if version == 1:
            expected_provenance = _submission_payload(delivery, args["response"], review, ids)
            fields = _SUBMISSION_FIELDS
        else:
            expected_provenance = _submission_payload_v2(
                checked, args["response"], review, ids, resolution_ids,
                ledger_digest=_ledger_digest(store, before, claim["id"], in_replay=True),
                analysis_verification=_analysis_verification(
                    store, before, claim["id"], decision["verdict"], recompute=False))
            fields = _SUBMISSION_FIELDS_V2
        require(set(provenance["payload"]) == fields and provenance["payload"] == expected_provenance
                and receipt["result"] == dict(review=review["id"], obligations=ids,
                                              resolutions=resolution_ids,
                                              submission=provenance["id"])
                if version == 2 else
                set(provenance["payload"]) == fields and provenance["payload"] == expected_provenance
                and receipt["result"] == dict(review=review["id"], obligations=ids,
                                              submission=provenance["id"]),
                "review submission provenance differs from its original receipt")
        require(args["assignment"] not in states, "assignment has multiple submitted verdicts")
        states[args["assignment"]] = dict(review=review, obligations=obligations,
                                          resolutions=resolved, submission=provenance)
        seen.add(provenance["id"])
    require(seen == submissions, "review submission event lacks its original receipt")
    return states


class ReviewSubmission:
    """Reviewer-owned local transition from delivered bytes to recorded opinion."""

    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def submit(self, *, assignment: str, response: str, expected_basis: str) -> dict[str, Any]:
        from .resolution import payload_v2
        require(self.store._command_context is not None and self.actor.role == "reviewer",
                "review submission needs a reviewer CommandService transaction")
        history = self.store.events()
        require(assignment not in _index(self.store, history),
                "review assignment already has a submitted verdict")
        checked = _decision(
            self.store, history, assignment=assignment, response=response,
            expected_basis=expected_basis, reviewer_actor=self.actor.id,
            study_id=self.store._command_context["study_id"], keyed=True)
        state, decision, claim = checked["state"], checked["decision"], checked["claim"]
        findings = checked["findings"]
        verification = _analysis_verification(self.store, history, claim["id"],
                                              decision["verdict"], recompute=True)
        ledger_digest = _ledger_digest(self.store, history, claim["id"], in_replay=False)
        kernel = Kernel(self.store, self.actor)
        actions = [finding["action"] for finding in findings]
        id = kernel._record_review(claim["id"], verdict=decision["verdict"],
                                   rationale=decision["rationale"], actions=actions,
                                   expected_basis=expected_basis,
                                   link_assessments=decision["link_assessments"],
                                   allow_approval=True)
        review = kernel._get(self.store.events(), id, "review")
        obligations: list[str] = []
        for index, finding in enumerate(findings):
            payload = dict(schema_version=1, review=id, review_hash=review["hash"],
                           claim=claim["id"], claim_hash=claim["hash"],
                           basis_hash=expected_basis, finding_index=index, **finding)
            obligations.append(kernel._write(self.store.events(), "review_obligation",
                                             payload, {"reviewer"}))
        resolutions = [kernel._write(self.store.events(), "review_obligation_resolution",
                                     payload_v2(refs, review, state["assignment"]), {"reviewer"})
                       for refs in checked["resolutions"]]
        provenance = f"review_submission-{uuid4().hex[:16]}"
        self.store.append(id=provenance, kind="review_submission", actor=self.actor.id,
                          role="reviewer",
                          payload=_submission_payload_v2(checked, response, review, obligations,
                                                         resolutions, ledger_digest=ledger_digest,
                                                         analysis_verification=verification),
                          expected_revision=len(self.store.events()))
        return dict(review=id, obligations=obligations, resolutions=resolutions,
                    submission=provenance)
