"""Frozen review context specifications, without identity or filesystem isolation.

An assignment records what a reviewer *should* receive. Actor IDs are supplied
by the caller, and a reviewer using the same Store/OS account can still read
other local files. This event is neither an access-control grant nor a review.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .kernel import Actor, Kernel, require
from .store import Store, canonical, digest


POLICY = "blind_initial_review_v1"
IDENTITY_ASSURANCE = "caller_declared"
READ_ISOLATION = "not_enforced"
_REQUEST_FIELDS = {"claim", "reviewer_actor", "expected_basis"}
_EVENT_FIELDS = {"schema_version", "claim", "claim_hash", "basis_hash", "reviewer_actor",
                 "study_id", "policy", "bundle", "identity_assurance", "read_isolation"}
_EXCLUSIONS = [
    "original_implementation_source_and_digest",
    "replication_implementation_source_and_digest",
    "execution_commands_and_environment_artifacts",
    "execution_logs_and_unselected_outputs",
    "author_reports_and_agent_responses",
    "analysis_proposals_and_adapter_source",
    "prior_review_verdicts_and_rationales",
    "unobserved_protocol_data_and_split_digests",
]


def _safe_design(design: dict[str, Any]) -> dict[str, Any]:
    """Keep preregistered statistical assumptions, not holdout byte locators."""
    fields = ("schema_version", "mode", "experimental_unit", "estimand", "primary_metric",
              "secondary_metrics", "sample_size", "sample_size_rationale", "uncertainty",
              "exclusions", "stopping_rule", "multiple_testing")
    return {**{field: design[field] for field in fields},
            "data_splits": [{field: split[field] for field in ("id", "role", "exposure_policy")}
                            for split in design["data_splits"]]}


def _manifest(store: Store, history: list[dict[str, Any]], *, claim: str,
              reviewer_actor: str, expected_basis: str, study_id: str) -> dict[str, Any]:
    if any(event["kind"] == "batch_analysis" for event in history):
        from .batch_analysis import _index as analysis_index
        analysis_index(store, history)
    packs = any(event["kind"] in {"pack_binding", "pack_analysis"} for event in history)
    if packs:
        from .domain_packs import pack_analyses, pack_bindings
        pack_bindings(store, history)
        pack_analyses(store, history)
    require(type(reviewer_actor) is str and bool(reviewer_actor.strip()),
            "reviewer actor must be a nonempty caller-declared ID")
    require(type(study_id) is str and bool(study_id.strip()), "assignment study_id is required")
    kernel = Kernel(store, Actor("review-assignment-validator", "observer"))
    target = kernel._get(history, claim, "claim")
    gate = kernel._gate(history, claim)
    basis, _ = kernel._basis(history, claim)
    require(gate["passed"] and gate["basis_hash"] == basis == expected_basis,
            "review assignment needs a current mechanically passed claim basis")
    context, contributors, _ = kernel._review_members(history, claim)
    require(reviewer_actor not in contributors,
            "reviewer actor contributed to the claim evidence context")

    by_id = {event["id"]: event for event in history}
    claims = [by_id[id] for id in context.claim_ids]
    protocols = {event["payload"]["protocol"] for event in claims}
    protocol_events = [event for event in history if event["id"] in protocols]
    bound_studies = {event["payload"].get("planning", {}).get("study_id")
                     for event in protocol_events if "planning" in event["payload"]}
    require(not bound_studies or bound_studies == {study_id},
            "assignment study differs from planning-bound claim context")
    hypothesis_ids = {id for event in protocol_events for id in event["payload"]["hypotheses"]}
    questions = {event["payload"]["planning"]["question"] for event in protocol_events
                 if "planning" in event["payload"]}
    sets = {event["payload"]["planning"]["explanation_set"] for event in protocol_events
            if "planning" in event["payload"]}
    runs = [event for event in history if event["kind"] == "run"
            and event["payload"]["protocol"] in protocols]
    run_ids = {event["id"] for event in runs}
    results = {event["payload"]["run"]: event for event in history
               if event["kind"] == "result" and event["payload"]["run"] in run_ids}
    allowed: set[str] = set()
    forbidden = {key for event in protocol_events
                 for key in (event["payload"]["implementation"], event["payload"]["environment"])}
    forbidden.update(key for event in history if event["kind"] == "batch_analysis"
                     and event["payload"]["claim"] in context.claim_ids
                     for key in (event["payload"]["proposal_digest"],
                                 event["payload"]["adapter_source_digest"]))
    forbidden.update(key for event in history if event["kind"] == "domain_binding"
                     and event["payload"]["protocol"] in protocols
                     for key in (event["payload"]["recipe_digest"],
                                 event["payload"]["adapter_source_digest"]))
    if packs:
        # Policy v1: no pack source, recipe, host inputs or report in the initial context.
        from .domain_packs import review_excluded
        forbidden.update(key for event in history
                         if (event["kind"] == "pack_binding" and event["payload"]["protocol"] in protocols)
                         or (event["kind"] == "pack_analysis"
                             and event["payload"]["claim"] in context.claim_ids)
                         for key in review_excluded(store, event))
    observed = []
    for run in runs:
        result = results.get(run["id"])
        forbidden.update((run["payload"]["implementation"], run["payload"]["environment"]))
        if result is not None:
            forbidden.update(key for label, key in result["payload"]["outputs"].items()
                             if label not in {"raw_data", "metrics"})
        outputs = ({} if result is None else
                   {key: result["payload"]["outputs"][key] for key in ("raw_data", "metrics")
                    if key in result["payload"]["outputs"]})
        allowed.update(outputs.values())
        observed.append(dict(run=run["id"], run_hash=run["hash"],
                             protocol=run["payload"]["protocol"], seed=run["payload"]["seed"],
                             declared_reanalysis_of=run["payload"]["replicate_of"],
                             result=None if result is None else dict(
                                 id=result["id"], hash=result["hash"],
                                 status=result["payload"]["status"], outputs=outputs)))
    require(not allowed & forbidden,
            "review bundle cannot expose bytes also recorded as source, environment, or log")
    for key in allowed:
        store.read(key)

    return dict(
        schema_version=1, policy=POLICY, purpose="initial_scientific_review_context",
        identity_assurance=IDENTITY_ASSURANCE, read_isolation=READ_ISOLATION,
        study_id=study_id, reviewer_actor=reviewer_actor,
        snapshot_revision=len(history),
        snapshot_hash=history[-1]["hash"] if history else "0" * 64,
        target=dict(claim=claim, claim_hash=target["hash"], basis_hash=basis,
                    mechanical_gate="passed", scientific_validity="not_assessed"),
        context=dict(
            questions=[dict(id=e["id"], hash=e["hash"], **{
                key: e["payload"][key] for key in
                ("statement", "objective", "scope", "constraints", "stopping_criteria")})
                for e in history if e["id"] in questions],
            explanation_sets=[dict(id=e["id"], hash=e["hash"],
                                   question=e["payload"]["question"],
                                   hypotheses=e["payload"]["hypotheses"],
                                   comparison_plan=e["payload"]["comparison_plan"])
                              for e in history if e["id"] in sets],
            hypotheses=[dict(id=e["id"], hash=e["hash"], **{
                key: e["payload"][key] for key in
                ("statement", "prediction", "falsifier", "scope")})
                for e in history if e["id"] in hypothesis_ids],
            claims=[dict(id=e["id"], hash=e["hash"], **{
                key: e["payload"][key] for key in
                ("statement", "scope", "outcome", "limitations", "protocol", "evidence")},
                inference_mode=e["payload"].get("inference_mode", "unclassified"))
                for e in claims],
            claim_links=[dict(id=e["id"], hash=e["hash"], **{
                key: e["payload"][key] for key in ("source", "target", "relation")})
                for e in history if e["id"] in context.link_ids],
            protocols=[dict(id=e["id"], hash=e["hash"], **{
                key: e["payload"][key] for key in
                ("hypotheses", "scope", "design", "metric", "analysis_plan",
                 "stopping_rule", "seeds", "run_limit", "replication_tolerance", "parent")},
                statistical_design=(_safe_design(e["payload"]["statistical_design"])
                                    if "statistical_design" in e["payload"] else None),
                protocol_mode=e["payload"].get("protocol_mode", "unclassified"))
                for e in protocol_events],
            observed_runs=observed,
            unbound_legacy_protocols=[e["id"] for e in protocol_events
                                      if "planning" not in e["payload"]]),
        allowed_artifact_digests=sorted(allowed), exclusions=list(_EXCLUSIONS))


def _payload(manifest: dict[str, Any], bundle: str) -> dict[str, Any]:
    target = manifest["target"]
    return dict(schema_version=1, claim=target["claim"], claim_hash=target["claim_hash"],
                basis_hash=target["basis_hash"], reviewer_actor=manifest["reviewer_actor"],
                study_id=manifest["study_id"], policy=POLICY, bundle=bundle,
                identity_assurance=IDENTITY_ASSURANCE, read_isolation=READ_ISOLATION)


def _index(store: Store, history: list[dict[str, Any]], *,
           receipts: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Verify the exact original command and frozen bundle at each assignment."""
    assignments = {event["id"]: event for event in history if event["kind"] == "review_assignment"}
    receipts = [receipt for receipt in (store.receipts() if receipts is None else receipts)
                if receipt["request"]["action"] == "review.assign"
                and receipt["after_revision"] <= len(history)]
    if not assignments and not receipts:
        return {}
    seen: set[str] = set()
    keys: set[tuple[str, str, str]] = set()
    for receipt in receipts:
        context, request = receipt["context"], receipt["request"]
        require(context["role"] == "planner" and request["version"] == 1
                and set(request["payload"]) == _REQUEST_FIELDS,
                "invalid historical review assignment request")
        events = history[receipt["before_revision"]:receipt["after_revision"]]
        require(len(events) == 1 and events[0]["kind"] == "review_assignment"
                and receipt["event_ids"] == [events[0]["id"]],
                "review assignment needs its exact single-event command receipt")
        event = events[0]
        before = history[:receipt["before_revision"]]
        require(event["actor"] == context["actor"] and event["role"] == "planner",
                "review assignment actor differs from its command")
        manifest = _manifest(store, before, study_id=context["study_id"], **request["payload"])
        bundle = digest(canonical(manifest))
        require(set(event["payload"]) == _EVENT_FIELDS
                and event["payload"] == _payload(manifest, bundle),
                "review assignment differs from its historical basis or bundle")
        require(store.read(bundle) == canonical(manifest),
                "review assignment context bundle differs from its historical projection")
        require(receipt["result"] == dict(assignment=event["id"], bundle=bundle),
                "review assignment receipt result differs from its event")
        key = (manifest["target"]["claim"], manifest["target"]["basis_hash"],
               manifest["reviewer_actor"])
        require(key not in keys, "duplicate historical reviewer assignment")
        keys.add(key)
        seen.add(event["id"])
    require(seen == set(assignments), "review assignment lacks its original command receipt")
    return {id: assignments[id] for id in seen}


class ReviewAssignment:
    """Planner records a review-context policy, not independent reviewer access."""

    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def assign(self, *, claim: str, reviewer_actor: str, expected_basis: str) -> dict[str, str]:
        require(self.store._command_context is not None and self.actor.role == "planner",
                "review assignment requires a planner CommandService transaction")
        history = self.store.events()
        prior = _index(self.store, history)
        study_id = self.store._command_context["study_id"]
        manifest = _manifest(self.store, history, claim=claim,
                             reviewer_actor=reviewer_actor, expected_basis=expected_basis,
                             study_id=study_id)
        require(not any(event["payload"]["claim"] == claim
                        and event["payload"]["basis_hash"] == expected_basis
                        and event["payload"]["reviewer_actor"] == reviewer_actor
                        for event in prior.values()),
                "reviewer already assigned to this claim basis")
        bundle = self.store.put_json(manifest)
        id = f"review_assignment-{uuid4().hex[:16]}"
        self.store.append(id=id, kind="review_assignment", actor=self.actor.id,
                          role="planner", payload=_payload(manifest, bundle),
                          expected_revision=len(history))
        return dict(assignment=id, bundle=bundle)
