"""Frozen review context specifications, without identity or filesystem isolation.

An assignment records what a reviewer *should* receive. Actor IDs are supplied
by the caller, and a reviewer using the same Store/OS account can still read
other local files. This event is neither an access-control grant nor a review.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .kernel import Actor, Kernel, independent_of, require
from .store import Store, canonical, digest


POLICY = "blind_initial_review_v1"
RECONSIDERATION = "veto_reconsideration_v1"
IDENTITY_ASSURANCE = "caller_declared"
READ_ISOLATION = "not_enforced"
_REQUEST_FIELDS = {"claim", "reviewer_actor", "expected_basis"}
_EVENT_FIELDS = {"schema_version", "claim", "claim_hash", "basis_hash", "reviewer_actor",
                 "study_id", "policy", "bundle", "identity_assurance", "read_isolation"}
# Schema 2 records which blind projection the policy extends, so replay rebuilds
# the exact manifest even after newer code projects a newer version.
_EVENT_FIELDS_V2 = _EVENT_FIELDS | {"projection"}
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
_RECONSIDERATION_EXCLUSIONS = [
    *(item for item in _EXCLUSIONS if item != "prior_review_verdicts_and_rationales"),
    "other_reviewers_verdicts_and_rationales",
]


def _safe_design(design: dict[str, Any]) -> dict[str, Any]:
    """Keep preregistered statistical assumptions, not holdout byte locators."""
    fields = ("schema_version", "mode", "experimental_unit", "estimand", "primary_metric",
              "secondary_metrics", "sample_size", "sample_size_rationale", "uncertainty",
              "exclusions", "stopping_rule", "multiple_testing")
    return {**{field: design[field] for field in fields},
            "data_splits": [{field: split[field] for field in ("id", "role", "exposure_policy")}
                            for split in design["data_splits"]]}


def select_policy(store: Store, history: list[dict[str, Any]], claim: str,
                  reviewer_actor: str, *, in_replay: bool) -> str:
    """Reconsideration iff the reviewer's own vetoes or obligations bind the claim (ADR 0018 §3.5)."""
    from .review_admission import admission
    own = admission(store, history, replay=not in_replay).own_findings(reviewer_actor, claim)
    return RECONSIDERATION if own["opinions"] or own["obligations"] else POLICY


def _own_findings(store: Store, history: list[dict[str, Any]], claim: str,
                  reviewer_actor: str, *, in_replay: bool) -> dict[str, Any]:
    """Only the assigned reviewer's own findings; other reviewers' opinions stay out."""
    from .review_admission import admission, opinion_summary
    projection = admission(store, history, replay=not in_replay)
    own = projection.own_findings(reviewer_actor, claim)
    followups = {event["payload"]["obligation"]: event["id"] for event in history
                 if event["kind"] == "replan_followup"}
    return dict(
        opinions=[opinion_summary(opinion) for opinion in own["opinions"]],
        obligations=[dict(id=event["id"], hash=event["hash"], claim=event["payload"]["claim"],
                          kind=event["payload"]["kind"], action=event["payload"]["action"],
                          closure_criterion=event["payload"]["closure_criterion"],
                          evidence_refs=event["payload"]["evidence_refs"],
                          followup=followups.get(event["id"]),
                          resolutions=[dict(claim=record["resolution"]["payload"]["claim"],
                                            resolution=record["resolution"]["id"],
                                            status=record["status"])
                                       for record in projection.resolutions()
                                       if record["resolution"]["payload"]["obligation"] == event["id"]])
                     for event in own["obligations"]])


def _manifest(store: Store, history: list[dict[str, Any]], *, claim: str,
              reviewer_actor: str, expected_basis: str, study_id: str,
              keyed: bool = False, policy: str = POLICY, in_replay: bool = True) -> dict[str, Any]:
    """Assignment projection; new assignments compare normalized actor keys (ADR 0018)."""
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
    require(independent_of(reviewer_actor, contributors) if keyed else reviewer_actor not in contributors,
            "reviewer actor contributed to the claim evidence context")

    by_id = {event["id"]: event for event in history}
    claims = [by_id[id] for id in context.claim_ids]
    protocols = {event["payload"]["protocol"] for event in claims}
    if policy == RECONSIDERATION:
        # The owner reconsiders for the whole family: list every lineage protocol and run.
        from .review_admission import protocol_components
        roots = protocol_components(history)
        protocols |= {id for id, root in roots.items() if root == roots[target["payload"]["protocol"]]}
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

    manifest = dict(
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
    if policy == RECONSIDERATION:
        manifest.update(policy=RECONSIDERATION, projection=POLICY,
                        purpose="veto_reconsideration_context",
                        own_findings=_own_findings(store, history, claim, reviewer_actor, in_replay=in_replay),
                        exclusions=list(_RECONSIDERATION_EXCLUSIONS))
    else:
        require(policy == POLICY, "unsupported review assignment policy")
    return manifest


def _payload(manifest: dict[str, Any], bundle: str, *, schema_version: int = 2) -> dict[str, Any]:
    target = manifest["target"]
    payload = dict(schema_version=schema_version, claim=target["claim"],
                   claim_hash=target["claim_hash"], basis_hash=target["basis_hash"],
                   reviewer_actor=manifest["reviewer_actor"], study_id=manifest["study_id"],
                   policy=manifest["policy"], bundle=bundle,
                   identity_assurance=IDENTITY_ASSURANCE, read_isolation=READ_ISOLATION)
    if schema_version == 2:
        payload["projection"] = POLICY
    return payload


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
    keys: dict[tuple[str, str, str], list[str]] = {}
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
        version = event["payload"].get("schema_version")
        require(version in (1, 2), "unsupported review assignment schema")
        # Schema 1 predates policy selection; replay keeps its original v1 projection.
        policy = (POLICY if version == 1 else
                  select_policy(store, before, request["payload"]["claim"],
                                request["payload"]["reviewer_actor"], in_replay=True))
        require(version == 1 or event["payload"].get("projection") == POLICY,
                "unsupported review assignment projection")
        manifest = _manifest(store, before, study_id=context["study_id"], policy=policy,
                             **request["payload"])
        bundle = digest(canonical(manifest))
        require(set(event["payload"]) == (_EVENT_FIELDS if version == 1 else _EVENT_FIELDS_V2)
                and event["payload"] == _payload(manifest, bundle, schema_version=version),
                "review assignment differs from its historical basis, policy or bundle")
        require(store.read(bundle) == canonical(manifest),
                "review assignment context bundle differs from its historical projection")
        require(receipt["result"] == dict(assignment=event["id"], bundle=bundle),
                "review assignment receipt result differs from its event")
        key = (manifest["target"]["claim"], manifest["target"]["basis_hash"],
               manifest["reviewer_actor"])
        require(version == 2 or key not in keys, "duplicate historical reviewer assignment")
        require(not _submitted(before, keys.get(key, [])),
                "reviewer already submitted a verdict for this claim basis")
        keys.setdefault(key, []).append(event["id"])
        seen.add(event["id"])
    require(seen == set(assignments), "review assignment lacks its original command receipt")
    return {id: assignments[id] for id in seen}


def _submitted(history: list[dict[str, Any]], assignments: list[str]) -> bool:
    """Whether any of these assignments has a submission event (verified by its own replay)."""
    return any(event["kind"] == "review_submission" and event["payload"]["assignment"] in assignments
               for event in history)


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
        policy = select_policy(self.store, history, claim, reviewer_actor, in_replay=False)
        manifest = _manifest(self.store, history, claim=claim,
                             reviewer_actor=reviewer_actor, expected_basis=expected_basis,
                             study_id=study_id, keyed=True, policy=policy, in_replay=False)
        # ADR 0018 §3.6: a reviewer may be assigned again until one of the
        # assignments for this claim basis receives a submitted verdict.
        require(not _submitted(history, [id for id, event in prior.items()
                                         if event["payload"]["claim"] == claim
                                         and event["payload"]["basis_hash"] == expected_basis
                                         and event["payload"]["reviewer_actor"] == reviewer_actor]),
                "reviewer already submitted a verdict for this claim basis")
        bundle = self.store.put_json(manifest)
        id = f"review_assignment-{uuid4().hex[:16]}"
        self.store.append(id=id, kind="review_assignment", actor=self.actor.id,
                          role="planner", payload=_payload(manifest, bundle),
                          expected_revision=len(history))
        return dict(assignment=id, bundle=bundle)
