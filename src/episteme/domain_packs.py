"""Receipt-backed DomainPack preregistration and analysis admission (ADR 0016).

A pack proposes and computes; this module admits transitions. ``pack.preregister``
records a protocol and its ``pack_binding`` in one receipt: pack identity, the
digest of the complete pack code, catalog, parameters, compiled protocol draft
and execution plan. ``pack.analyse`` admits one bounded claim for a completed
batch of such a protocol. It re-verifies the pinned code, every envelope, the
fields the kernel computes itself and the claim-strength ceiling, and rejects a
proposal above the ceiling instead of downgrading it.

Packs are trusted local Python executed in this process; events record
``pack_trust`` and ``hook_isolation`` accordingly. Pinning detects drift and a
wrong version, not malicious code. Historical replay is structural and never
imports pack code; ``pack verify`` re-runs hooks separately. Mechanical
passage is not scientific review: ``scientific_validity`` stays not_assessed.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from .domains import api, registry
from .kernel import Actor, Kernel, independent_of, require
from .planning import binding_for
from .protocols import StatisticalDesign
from .store import Store, canonical, digest

Plan = api.ExecutionPlan | api.ExecutionPlanV2


BINDING = "pack_binding"
ANALYSIS = "pack_analysis"
KINDS = {BINDING, ANALYSIS}
PACK_TRUST = "trusted_local_code"
HOOK_ISOLATION = "in_process"
PINNING = "pack_code_manifest"
IMPLEMENTED_PROFILES = {"trusted_local_python_v1", api.LOCKED_PROFILE}
MODE_ORDER = {"descriptive": 0, "exploratory": 1, "confirmatory": 2}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PREREGISTER_FIELDS = {"explanation_set", "pack_id", "pack_version", "pack_code_digest",
                       "parameters", "host_inputs", "capture", "environment"}
_BINDING_FIELDS = {
    "schema_version", "contract_version", "protocol", "protocol_hash", "study_id",
    "explanation_set", "pack_id", "pack_version", "pack_code_digest", "pack_manifest",
    "catalog", "parameters", "host_inputs", "capture", "protocol_draft", "execution_plan",
    "roster_semantics", "sample_size_scope", "execution_profile", "environment",
    "closure_level", "numeric_tolerance", "hidden_inputs", "protocol_validation",
    "pack_trust", "hook_isolation", "pack_pinning", "scientific_validity"}
_ANALYSE_FIELDS = {"batch", "expected_settlement", "pack_id", "pack_version", "pack_code_digest",
                   "checks", "recomputations", "report", "statistical_report", "reviewer_actor"}
_ANALYSIS_FIELDS = {
    "schema_version", "contract_version", "batch", "batch_hash", "settlement", "settlement_hash",
    "terminal", "terminal_hash", "protocol", "protocol_hash", "binding", "binding_hash",
    "claim", "claim_hash", "pack_id", "pack_version", "pack_code_digest", "report",
    "statistical_report", "checks", "recomputations", "cas_allowlist", "hidden_digests",
    "ceiling", "replication", "report_origin", "task_id", "reviewer_actor", "runs", "results",
    "pack_trust", "hook_isolation", "scientific_validity"}
# pack.analyse re-runs the pinned hooks in the command and admits only identical bytes.
REPORT_ORIGIN = "recomputed_by_pinned_pack_at_admission"
_REPETITION = {
    "rng_seed": "fresh_seed_repetition_of_one_generator",
    "partition_seed": "repartition_of_the_same_data",
    "frozen_unit_index": "distinct_frozen_units_without_new_randomness",
    "deterministic_single": "single_deterministic_unit",
}
# Ceiling schema v1. Replay recomputes these texts exactly; a wording change needs
# ceiling schema_version 2 with this v1 path kept for stored histories.
CEILING_VERSION = 1
REASON_DESCRIPTIVE = ("descriptive design without preregistered uncertainty: conclusions "
                      "cover the analysed units only")
REASON_INTERVAL = "supports/refutes need a supplied confidence interval and effect size"
REASON_DEVIATIONS = "deviations from the preregistration cap inference at exploratory"
REASON_CONFIRMATORY = ("confirmatory inference needs a complete report, no deviations, the "
                       "planned units and only holding assumptions")


def _call(loaded: registry.LoadedPack, name: str, *args: Any) -> Any:
    """Run one pack hook; any failure becomes a PackError naming the hook."""
    try:
        return loaded.hook(name)(*args)
    except registry.PackError:
        raise
    except Exception as exc:
        raise registry.PackError(f"pack {loaded.pack_id} hook {name} failed: {exc}") from exc


def _object(store: Store, key: str, label: str) -> Any:
    require(type(key) is str and _DIGEST.fullmatch(key) is not None, f"invalid {label} digest")
    return api.strict_loads(store.read(key), label)


def _envelope(build: Any, value: Any, label: str) -> Any:
    try:
        return build(value)
    except ValueError as exc:
        raise api.EnvelopeError(f"invalid {label}: {exc}") from exc


# ------------------------------------------------------- pinned binding parts


def _pinned(store: Store, binding: Mapping[str, Any]) -> dict[str, Any]:
    """Parse every CAS envelope a binding pins; never imports pack code."""
    p = binding
    code = _envelope(api.validate_code_manifest, _object(store, p["pack_code_digest"],
                                                          "pack code manifest"), "pack code manifest")
    require(code["pack_id"] == p["pack_id"] and digest(canonical(code)) == p["pack_code_digest"],
            "pack code manifest differs from its pinned digest")
    for row in code["files"]:
        require(len(store.read(row["sha256"])) == row["bytes"], f"pack file size mismatch: {row['path']}")
    manifest = _envelope(api.PackManifest.from_dict, _object(store, p["pack_manifest"],
                                                             "pack manifest"), "pack manifest")
    catalog = _envelope(api.ParameterCatalog.from_dict, _object(store, p["catalog"],
                                                                "parameter catalog"), "catalog")
    draft = _envelope(api.ProtocolDraft.from_dict, _object(store, p["protocol_draft"],
                                                           "protocol draft"), "protocol draft")
    plan = _envelope(lambda value: api.plan_from_frozen(value, store.read),
                     _object(store, p["execution_plan"], "execution plan"), "execution plan")
    host = _object(store, p["host_inputs"], "host inputs")
    require(type(host) is dict, "host inputs must be a JSON object")
    capture = None
    if p["capture"] is not None:
        capture = _envelope(lambda value: api.CaptureBundle.from_frozen(value, store.read),
                            _object(store, p["capture"], "capture bundle"), "capture bundle")
    for value, name in ((manifest, "pack manifest"), (catalog, "catalog")):
        require((value.pack_id, value.pack_version) == (p["pack_id"], p["pack_version"]),
                f"{name} identity differs from the pinned pack")
    require(capture is None or (capture.pack_id, capture.pack_version) == (p["pack_id"], p["pack_version"]),
            "capture bundle was made by another pack or version")
    return dict(code=code, manifest=manifest, catalog=catalog, draft=draft, plan=plan,
                host=host, capture=capture)


def plan_keys(store: Store, plan: dict[str, Any]) -> set[str]:
    """CAS keys of a frozen execution plan: programs (v2: manifests and tree files), input, project."""
    keys = {plan[name]["sha256"] for name in ("primary_program", "reanalysis_program", "input")}
    if plan["schema_version"] == 2:
        from .environment_closure import tree_digests
        for name in ("primary_program", "reanalysis_program"):
            keys |= tree_digests(api.strict_loads(store.read(plan[name]["sha256"]), "source tree manifest"))
        keys |= {row["sha256"] for row in plan["environment_requirements"]["project"]}
    return keys


def binding_artifacts(store: Store, event: dict[str, Any]) -> set[str]:
    """CAS closure of a pack binding: code, envelopes, programs, input and capture."""
    p = event["payload"]
    code = api.strict_loads(store.read(p["pack_code_digest"]), "pack code manifest")
    plan = api.strict_loads(store.read(p["execution_plan"]), "execution plan")
    keys = {p["pack_code_digest"], p["pack_manifest"], p["catalog"], p["host_inputs"],
            p["protocol_draft"], p["execution_plan"], p["environment"],
            *(row["sha256"] for row in code["files"]), *plan_keys(store, plan)}
    if p["capture"] is not None:
        bundle = api.strict_loads(store.read(p["capture"]), "capture bundle")
        keys.add(p["capture"])
        keys.update(row["sha256"] for row in bundle["inventory"])
    return keys


def pack_lineage(history: list[dict[str, Any]], protocol: str | None) -> dict[str, Any] | None:
    """Nearest pack binding on the parent chain of protocol, itself first (ADR 0018).

    Presence is structural: pack_bindings, Graph, export and backup replay each
    binding. An unverified binding can only add refusals, never admit a claim.
    """
    bindings = {event["payload"].get("protocol"): event for event in history if event["kind"] == BINDING}
    if not bindings:
        return None
    parents = {event["id"]: event["payload"].get("parent") for event in history
               if event["kind"] == "protocol"}
    seen: set[str] = set()
    while protocol is not None and protocol not in seen:
        if protocol in bindings:
            return bindings[protocol]
        seen.add(protocol)
        protocol = parents.get(protocol)
    return None


def pinned_bytes(store: Store, history: list[dict[str, Any]]) -> set[str]:
    """Programs, code files, input and captured bytes pinned by any recorded pack binding."""
    pinned: set[str] = set()
    for event in history:
        if event["kind"] != BINDING:
            continue
        p = event["payload"]
        code = api.strict_loads(store.read(p["pack_code_digest"]), "pack code manifest")
        plan = api.strict_loads(store.read(p["execution_plan"]), "execution plan")
        pinned.update(row["sha256"] for row in code["files"])
        pinned.update(plan_keys(store, plan))
        if p["capture"] is not None:
            bundle = api.strict_loads(store.read(p["capture"]), "capture bundle")
            pinned.add(p["capture"])
            pinned.update(row["sha256"] for row in bundle["inventory"])
    return pinned


def analysis_artifacts(event: dict[str, Any]) -> set[str]:
    p = event["payload"]
    return {p["report"], p["statistical_report"], p["checks"], p["recomputations"]}


def review_excluded(store: Store, event: dict[str, Any]) -> set[str]:
    """Pack bytes kept out of a blind initial review context.

    Pack source, envelopes, host inputs and reports are excluded. A profile v2
    binding also excludes each source-tree file and each closure project file,
    so a copy of those bytes cannot pass as an observed output. Protocol data
    and captured files are not listed: like other unobserved data they are only
    absent from the allowlist, and stay allowed where they are observed outputs.
    """
    if event["kind"] == ANALYSIS:
        return analysis_artifacts(event)
    p = event["payload"]
    code = api.strict_loads(store.read(p["pack_code_digest"]), "pack code manifest")
    keys = {p["pack_code_digest"], p["pack_manifest"], p["catalog"], p["host_inputs"],
            p["protocol_draft"], p["execution_plan"], *(row["sha256"] for row in code["files"])}
    if p["capture"] is not None:
        keys.add(p["capture"])
    plan = api.strict_loads(store.read(p["execution_plan"]), "execution plan")
    if plan.get("schema_version") == 2:
        keys |= plan_keys(store, plan) - {plan["input"]["sha256"]}
    return keys


def pack_artifacts(store: Store, event: dict[str, Any]) -> set[str]:
    if event["kind"] == BINDING:
        return binding_artifacts(store, event)
    require(event["kind"] == ANALYSIS, "unsupported pack event kind")
    return analysis_artifacts(event)


def execution_fields(store: Store, binding: dict[str, Any]) -> dict[str, Any]:
    """Batch recipe fields a pack binding pins; batch.plan must equal them."""
    p = binding["payload"]
    plan = api.strict_loads(store.read(p["execution_plan"]), "execution plan")
    return dict(reanalysis_implementation=plan["reanalysis_program"]["sha256"],
                reanalysis_environment=p["environment"], outputs=plan["outputs"],
                wall_seconds=plan["wall_seconds"], max_output_bytes=plan["max_output_bytes"],
                required_capabilities=plan["required_capabilities"])


# ----------------------------------------------------------- preregistration


def _compile(store: Store, loaded: registry.LoadedPack, parameters: Any, host_inputs: Any,
             capture: Any) -> dict[str, Any]:
    catalog = _call(loaded, "describe")
    require(type(catalog) is api.ParameterCatalog
            and (catalog.pack_id, catalog.pack_version) == (loaded.pack_id, loaded.pack_version),
            "pack describe() must return its own ParameterCatalog")
    chosen = catalog.validate_parameters(parameters)
    _call(loaded, "validate_parameters", api.freeze(chosen))
    require(type(host_inputs) is dict, "pack host inputs must be a JSON object")
    bundle = None
    if capture is not None:
        require(loaded.manifest.capture, "pack does not capture external sources")
        bundle = _envelope(lambda value: api.CaptureBundle.from_frozen(value, store.read),
                           _object(store, capture, "capture bundle"), "capture bundle")
        require(bundle.digest() == capture
                and (bundle.pack_id, bundle.pack_version) == (loaded.pack_id, loaded.pack_version),
                "capture bundle was made by another pack or version")
    request = api.CompileRequest(parameters=chosen, host_inputs=host_inputs, capture=bundle)
    draft = _call(loaded, "compile_protocol", request)
    plan = _call(loaded, "compile_execution", request)
    require(type(draft) is api.ProtocolDraft and type(plan) in (api.ExecutionPlan, api.ExecutionPlanV2),
            "pack compile hooks must return a ProtocolDraft and an ExecutionPlan or ExecutionPlanV2")
    return dict(catalog=catalog, parameters=chosen, request=request, draft=draft, plan=plan,
                capture=bundle)


def _check_compilation(store: Store, manifest: api.PackManifest, catalog: api.ParameterCatalog,
                       draft: api.ProtocolDraft, plan: Plan,
                       capture: api.CaptureBundle | None, environment: str) -> None:
    """Kernel checks of a compilation; the same on live admission and replay."""
    require(draft.roster_semantics in manifest.roster_semantics,
            "protocol draft roster semantics is not declared by the pack")
    design = draft.typed_design()
    require(manifest.metric(draft.metric)["unit"] == design.primary_metric.unit
            and catalog.metric == draft.metric,
            "protocol metric and unit differ from the pack declaration")
    require(plan.execution_profile in manifest.execution_profiles
            and plan.execution_profile in IMPLEMENTED_PROFILES,
            "unsupported or undeclared execution profile")
    outputs = manifest.output_paths()
    require(api.thaw(plan.outputs) == outputs and api.thaw(catalog.outputs) == outputs,
            "execution plan outputs differ from the pack manifest")
    frozen = plan.to_dict()
    if type(plan) is api.ExecutionPlanV2:
        from .execution_locked import check_outputs, closure_parts
        check_outputs(frozen["outputs"])
        declaration, _ = closure_parts(store, environment)
        requirements = frozen["environment_requirements"]
        require(declaration["project"] == requirements["project"],
                "environment closure project differs from the execution plan")
        require(all(declaration["variables"]["set"].get(name) == value
                    for name, value in requirements["variables"].items()),
                "environment closure does not set the variables the execution plan requires")
    else:
        declaration = _object(store, environment, "environment declaration")
        from .execution import BACKEND
        require(type(declaration) is dict and set(declaration) == {"schema_version", "backend", "fingerprint"}
                and declaration["schema_version"] == 1 and declaration["backend"] == BACKEND
                and type(declaration["fingerprint"]) is dict,
                "pack execution requires a frozen local Python environment")
    bound = {frozen["input"]["sha256"]} | (set() if capture is None else set(capture.blobs()))
    require({split.digest for split in design.data_splits} <= bound,
            "data splits must refer to bytes frozen by this binding")
    require(set(draft.seen_data) <= bound, "declared exposure must refer to bytes frozen by this binding")
    code_like = {frozen["primary_program"]["sha256"], frozen["reanalysis_program"]["sha256"]}
    if type(plan) is api.ExecutionPlanV2:
        code_like |= {key for tree in (plan.primary_program, plan.reanalysis_program) for key in tree.blobs()}
        code_like |= {row["sha256"] for row in frozen["environment_requirements"]["project"]}
    require(not code_like & bound, "program bytes cannot alias the protocol input or captured data")


def _protocol_payload(history: list[dict[str, Any]], explanation_set: str,
                      draft: api.ProtocolDraft, plan: Plan,
                      environment: str) -> dict[str, Any]:
    """The protocol payload Kernel.preregister_for_set records for this draft."""
    planning = binding_for(history, explanation_set, current=True)
    question = Kernel._get(history, planning["question"], "research_question")["payload"]
    explanations = Kernel._get(history, explanation_set, "explanation_set")["payload"]
    design = StatisticalDesign.from_dict(api.thaw(draft.statistical_design))
    frozen = plan.to_dict()
    return dict(hypotheses=list(explanations["hypotheses"]), scope=dict(question["scope"]),
                design=draft.design, metric=draft.metric, analysis_plan=draft.analysis_plan,
                stopping_rule=draft.stopping_rule, seeds=list(draft.roster),
                run_limit=draft.run_limit, implementation=frozen["primary_program"]["sha256"],
                environment=environment, data=frozen["input"]["sha256"],
                replication_tolerance=draft.replication_tolerance, parent=None, planning=planning,
                statistical_design=design.to_dict(), protocol_mode=design.mode, amendment_reason=None,
                seen_data=sorted(set(draft.seen_data) | Kernel._known_seen_data(history, None)))


def _binding_payload(*, protocol: dict[str, Any], study_id: str, request: dict[str, Any],
                     keys: dict[str, str], manifest: api.PackManifest, parameters: Any,
                     draft: api.ProtocolDraft, plan: Plan) -> dict[str, Any]:
    return dict(schema_version=1, contract_version=api.CONTRACT_VERSION, protocol=protocol["id"],
                protocol_hash=protocol["hash"], study_id=study_id,
                explanation_set=request["explanation_set"], pack_id=request["pack_id"],
                pack_version=request["pack_version"], pack_code_digest=request["pack_code_digest"],
                pack_manifest=keys["pack_manifest"], catalog=keys["catalog"],
                parameters=api.thaw(parameters), host_inputs=keys["host_inputs"],
                capture=request["capture"], protocol_draft=keys["protocol_draft"],
                execution_plan=keys["execution_plan"], roster_semantics=draft.roster_semantics,
                sample_size_scope=draft.sample_size_scope, execution_profile=plan.execution_profile,
                environment=request["environment"],
                closure_level=plan.to_dict()["environment_requirements"]["closure_level"],
                numeric_tolerance=manifest.numeric_tolerance,
                hidden_inputs=sorted(manifest.hidden_inputs), protocol_validation="passed",
                pack_trust=PACK_TRUST, hook_isolation=HOOK_ISOLATION, pack_pinning=PINNING,
                scientific_validity="not_assessed")


def _validate_request(request: Any) -> None:
    require(type(request) is dict and set(request) == _PREREGISTER_FIELDS,
            "invalid pack preregistration request fields")
    require(type(request["pack_id"]) is str and type(request["pack_version"]) is str
            and type(request["pack_code_digest"]) is str
            and _DIGEST.fullmatch(request["pack_code_digest"]) is not None,
            "pack preregistration needs an exact pack pin")
    require(type(request["environment"]) is str and _DIGEST.fullmatch(request["environment"]) is not None,
            "pack preregistration needs a frozen environment digest")
    require(request["capture"] is None or (type(request["capture"]) is str
                                           and _DIGEST.fullmatch(request["capture"]) is not None),
            "capture must be null or a CAS digest")
    for name in ("parameters", "host_inputs"):
        require(type(request[name]) is dict and len(canonical(request[name])) <= api.MAX_ENVELOPE_BYTES,
                f"pack {name} must be a bounded JSON object")


def _preceding_checks(history: list[dict[str, Any]], explanation_set: str) -> None:
    Kernel._get(history, explanation_set, "explanation_set")


def _binding_index(store: Store, history: list[dict[str, Any]], *,
                   receipts: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Structurally replay every pack binding on its historical prefix."""
    events = {event["id"]: event for event in history if event["kind"] == BINDING}
    if receipts is None and not events:
        # Without supplied receipts, skip the costly read for histories without packs.
        # Graph construction passes receipts, so forged pack receipts still fail there.
        return {}
    relevant = [row for row in (store.receipts() if receipts is None else receipts)
                if row["request"]["action"] == "pack.preregister"
                and row["after_revision"] <= len(history)]
    by_protocol: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for receipt in relevant:
        context, command = receipt["context"], receipt["request"]
        require(command["version"] == 1 and context["role"] == "planner",
                "pack binding needs a planner command")
        request = command["payload"]
        _validate_request(request)
        before = history[:receipt["before_revision"]]
        created = history[receipt["before_revision"]:receipt["after_revision"]]
        require(len(created) == 2 and [event["kind"] for event in created] == ["protocol", BINDING]
                and receipt["event_ids"] == [event["id"] for event in created],
                "pack binding needs its exact protocol and binding receipt")
        protocol, event = created
        require(all((item["actor"], item["role"]) == (context["actor"], "planner") for item in created),
                "pack binding actor differs from its command")
        p = event["payload"]
        require(set(p) == _BINDING_FIELDS, "invalid pack binding fields")
        pinned = _pinned(store, p)
        require(pinned["code"]["pack_id"] == request["pack_id"]
                and p["pack_code_digest"] == request["pack_code_digest"]
                and canonical(pinned["host"]) == canonical(request["host_inputs"])
                and p["capture"] == request["capture"],
                "pack binding differs from its command request")
        parameters = pinned["catalog"].validate_parameters(request["parameters"])
        require(p["capture"] is None or (pinned["manifest"].capture
                                         and pinned["capture"].digest() == p["capture"]),
                "pack capture differs from its declaration or canonical digest")
        _check_compilation(store, pinned["manifest"], pinned["catalog"], pinned["draft"],
                           pinned["plan"], pinned["capture"], request["environment"])
        expected_protocol = _protocol_payload(before, request["explanation_set"], pinned["draft"],
                                              pinned["plan"], request["environment"])
        require(protocol["payload"] == expected_protocol,
                "pack protocol differs from its pinned compilation")
        observer = Kernel(store, Actor("pack-binding-validator", "observer"))
        observer._validate_planning_protocol(before, expected_protocol)
        observer._validate_typed_protocol(before, expected_protocol)
        from .batch import _recipe as validate_batch_recipe
        validate_batch_recipe(store, execution_fields(store, event), expected_protocol)
        keys = dict(pack_manifest=p["pack_manifest"], catalog=p["catalog"],
                    host_inputs=p["host_inputs"], protocol_draft=p["protocol_draft"],
                    execution_plan=p["execution_plan"])
        expected = _binding_payload(protocol=protocol, study_id=context["study_id"], request=request,
                                    keys=keys, manifest=pinned["manifest"], parameters=parameters,
                                    draft=pinned["draft"], plan=pinned["plan"])
        require(p == expected, "pack binding differs from its frozen command and envelopes")
        require(digest(pinned["manifest"].canonical()) == p["pack_manifest"]
                and pinned["catalog"].digest() == p["catalog"]
                and pinned["draft"].digest() == p["protocol_draft"]
                and pinned["plan"].digest() == p["execution_plan"],
                "pack binding envelope digests are not canonical")
        require(receipt["result"] == dict(protocol=protocol["id"], binding=event["id"],
                                          pack_code_digest=p["pack_code_digest"]),
                "pack binding receipt result differs from its events")
        require(protocol["id"] not in by_protocol, "duplicate pack binding for protocol")
        by_protocol[protocol["id"]] = event
        seen.add(event["id"])
    require(seen == set(events), "pack binding lacks its original command receipt")
    return by_protocol


def pack_bindings(store: Store, history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not any(event["kind"] in KINDS for event in history):
        return {}
    return _binding_index(store, history)


class PackPreregistration:
    """Planner command: compile with pinned pack code and record protocol + binding."""

    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def preregister(self, *, explanation_set: str, pack_id: str, pack_version: str,
                    pack_code_digest: str, parameters: dict[str, Any],
                    host_inputs: dict[str, Any], capture: str | None,
                    environment: str) -> dict[str, str]:
        require(self.store._command_context is not None and self.actor.role == "planner",
                "pack preregistration requires a planner CommandService transaction")
        request = dict(explanation_set=explanation_set, pack_id=pack_id, pack_version=pack_version,
                       pack_code_digest=pack_code_digest, parameters=parameters,
                       host_inputs=host_inputs, capture=capture, environment=environment)
        _validate_request(request)
        history = self.store.events()
        pack_bindings(self.store, history)
        _preceding_checks(history, explanation_set)
        loaded = registry.load_pack(pack_id)
        loaded.require_pin(pack_id=pack_id, pack_version=pack_version,
                           pack_code_digest=pack_code_digest)
        compiled = _compile(self.store, loaded, parameters, host_inputs, capture)
        _check_compilation(self.store, loaded.manifest, compiled["catalog"], compiled["draft"],
                           compiled["plan"], compiled["capture"], environment)
        for data in loaded.files.values():
            self.store.put(data)
        require(self.store.put_json(api.thaw(loaded.code_manifest)) == pack_code_digest,
                "pack code manifest CAS digest mismatch")
        keys = dict(pack_manifest=self.store.put(loaded.manifest.canonical()),
                    catalog=self.store.put(compiled["catalog"].canonical()),
                    host_inputs=self.store.put_json(host_inputs),
                    protocol_draft=self.store.put(compiled["draft"].canonical()))
        for key, data in compiled["plan"].blobs().items():
            require(self.store.put(data) == key, "execution plan bytes CAS digest mismatch")
        keys["execution_plan"] = self.store.put_json(compiled["plan"].to_dict())
        draft, plan = compiled["draft"], compiled["plan"]
        frozen = plan.to_dict()
        kernel = Kernel(self.store, self.actor)
        protocol_id = kernel._preregister_for_set(
            pack_path=True,
            explanation_set=explanation_set, design=draft.design, metric=draft.metric,
            analysis_plan=draft.analysis_plan, stopping_rule=draft.stopping_rule,
            seeds=list(draft.roster), run_limit=draft.run_limit,
            implementation=frozen["primary_program"]["sha256"], environment=environment,
            data=frozen["input"]["sha256"], replication_tolerance=draft.replication_tolerance,
            statistical_design=api.thaw(draft.statistical_design), seen_data=list(draft.seen_data))
        updated = self.store.events()
        protocol = Kernel._get(updated, protocol_id, "protocol")
        require(protocol["payload"] == _protocol_payload(history, explanation_set, draft, plan,
                                                         environment),
                "recorded protocol differs from the pack compilation")
        from .batch import _recipe as validate_batch_recipe
        validate_batch_recipe(self.store, dict(
            reanalysis_implementation=frozen["reanalysis_program"]["sha256"],
            reanalysis_environment=environment, outputs=frozen["outputs"],
            wall_seconds=frozen["wall_seconds"], max_output_bytes=frozen["max_output_bytes"],
            required_capabilities=frozen["required_capabilities"]), protocol["payload"])
        _call(loaded, "validate_protocol", _protocol_context(
            compiled["parameters"], draft, plan, compiled["capture"], protocol))
        payload = _binding_payload(protocol=protocol, study_id=self.store._command_context["study_id"],
                                   request=request, keys=keys, manifest=loaded.manifest,
                                   parameters=compiled["parameters"], draft=draft, plan=plan)
        binding = kernel._write(updated, BINDING, payload, {"planner"})
        return dict(protocol=protocol_id, binding=binding, pack_code_digest=pack_code_digest)


def _protocol_context(parameters: Any, draft: api.ProtocolDraft, plan: Plan,
                      capture: api.CaptureBundle | None, protocol: dict[str, Any]
                      ) -> api.ProtocolContext:
    return api.ProtocolContext(parameters=parameters, draft=draft, execution_plan=plan.to_dict(),
                               capture=None if capture is None else capture.to_dict(),
                               protocol=protocol["payload"], protocol_hash=protocol["hash"])


def require_live_pack(store: Store, binding: dict[str, Any]) -> registry.LoadedPack:
    """Load the pinned pack now and re-run its pure protocol validation.

    Host inputs and captured bytes are not passed: they stay with compilation.
    """
    p = binding["payload"]
    loaded = registry.load_pack(p["pack_id"])
    loaded.require_pin(pack_id=p["pack_id"], pack_version=p["pack_version"],
                       pack_code_digest=p["pack_code_digest"])
    pinned = _pinned(store, p)
    protocol = Kernel._get(store.events(), p["protocol"], "protocol")
    _call(loaded, "validate_protocol", _protocol_context(
        p["parameters"], pinned["draft"], pinned["plan"], pinned["capture"], protocol))
    return loaded


# --------------------------------------------------------------- analysis


def _batch_state(store: Store, history: list[dict[str, Any]], batch: str,
                 expected_settlement: str | None,
                 bindings: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    from .batch import _index as batch_index
    states = batch_index(store, history)
    require(batch in states, "unknown batch")
    state = states[batch]
    plan, settlement, terminal = state["plan"], state["settlement"], state["terminal"]
    require(settlement is not None and terminal is not None
            and (expected_settlement is None or settlement["id"] == expected_settlement)
            and settlement["payload"]["status"] == "completed"
            and settlement["payload"]["next_action"] == "awaiting_analysis"
            and terminal["payload"]["status"] == "completed",
            "pack analysis requires an exactly matched completed batch settlement")
    if bindings is None:
        bindings = _binding_index(store, history)
    binding = bindings.get(plan["payload"]["protocol"])
    require(binding is not None and binding["seq"] < plan["seq"],
            "pack analysis requires a pack binding that precedes the batch plan")
    rows = state["slots"]
    require(len(rows) == len(plan["payload"]["slots"])
            and all(row["status"] == "completed" for row in rows)
            and [row["slot"] for row in rows] == [slot["slot"] for slot in plan["payload"]["slots"]],
            "pack analysis requires the entire completed batch roster")
    protocol = Kernel._get(history, plan["payload"]["protocol"], "protocol")
    require(binding["payload"]["protocol_hash"] == protocol["hash"],
            "pack binding refers to another protocol revision")
    pinned = _pinned(store, binding["payload"])
    labels = set(pinned["manifest"].outputs)
    slots = []
    for row in rows:
        outputs = Kernel._get(history, row["result"], "result")["payload"]["outputs"]
        require(labels <= set(outputs), "batch result lacks a declared pack output")
        slots.append(dict(slot=row["slot"], roster_unit=row["seed"], mode=row["mode"],
                          run=row["run"], result=row["result"],
                          outputs={label: outputs[label] for label in sorted(labels)}))
    return dict(state=state, binding=binding, protocol=protocol, pinned=pinned, slots=slots)


def _allowlist(store: Store, facts: dict[str, Any]) -> tuple[dict[str, bytes], set[str]]:
    """Observed declared outputs, recipe bytes and captured data minus hidden inputs."""
    p = facts["binding"]["payload"]
    pinned = facts["pinned"]
    observed = {key for row in facts["slots"] for key in row["outputs"].values()}
    hidden: set[str] = set()
    extra: set[str] = set()
    data = facts["protocol"]["payload"]["data"]
    (hidden if "protocol_data" in p["hidden_inputs"] else extra).add(data)
    if pinned["capture"] is not None:
        captured = set(pinned["capture"].blobs())
        (hidden if "captured_artifacts" in p["hidden_inputs"] else extra).update(captured)
    require(not hidden & observed, "a declared hidden input aliases an observed output")
    allowed = sorted((observed | extra) - hidden)
    return {key: store.read(key) for key in allowed}, hidden


def analysis_inputs(store: Store, history: list[dict[str, Any]], batch: str
                    ) -> tuple[dict[str, Any], api.AnalysisContext, api.CasView]:
    """Copied payloads and a CasView for analysis hooks on one completed batch."""
    facts = _batch_state(store, history, batch, None)
    return (facts, *_hook_inputs(store, facts))


def _hook_inputs(store: Store, facts: dict[str, Any]) -> tuple[api.AnalysisContext, api.CasView]:
    p, pinned = facts["binding"]["payload"], facts["pinned"]
    blobs, hidden = _allowlist(store, facts)
    state = facts["state"]
    context = api.AnalysisContext(
        pack_id=p["pack_id"], pack_version=p["pack_version"],
        protocol=facts["protocol"]["payload"], protocol_hash=facts["protocol"]["hash"],
        parameters=p["parameters"], draft=pinned["draft"], execution_plan=pinned["plan"].to_dict(),
        capture=None if pinned["capture"] is None else pinned["capture"].to_dict(),
        batch=dict(plan=state["plan"]["id"], plan_hash=state["plan"]["hash"],
                   settlement=state["settlement"]["id"], settlement_hash=state["settlement"]["hash"]),
        slots=facts["slots"], numeric_tolerance=p["numeric_tolerance"])
    return context, api.CasView(blobs, hidden=hidden)


def _kernel_fields(store: Store, facts: dict[str, Any]) -> dict[str, Any]:
    """Values the kernel computes itself; a report that differs is rejected."""
    protocol = facts["protocol"]["payload"]
    design = StatisticalDesign.from_dict(protocol["statistical_design"])
    roster = list(protocol["seeds"])
    scope = facts["binding"]["payload"]["sample_size_scope"]
    observer = Kernel(store, Actor("pack-analysis-validator", "observer"))
    recorded: dict[str, float] = {}
    for row in facts["slots"]:
        recorded[row["slot"]] = observer._metric(row["outputs"]["metrics"], protocol["metric"])
    missing = [row["slot"] for row in facts["state"]["slots"] if row["status"] != "completed"]
    return dict(design=design, roster=roster,
                planned_total=design.sample_size * len(roster) if scope == "per_roster_unit"
                else design.sample_size,
                scope=scope, missing=missing, roster_complete=not missing, recorded=recorded)


def ceiling(statistical: api.StatisticalReport, report: api.AnalysisReport,
            facts: dict[str, Any], kernel: dict[str, Any],
            recomputations: list[api.Recomputation]) -> dict[str, Any]:
    """Claim-strength ceiling: reject inconsistencies, cap outcome and inference mode."""
    design: StatisticalDesign = kernel["design"]
    protocol = facts["protocol"]
    statistical.validate_design(design)
    require(statistical.protocol_hash == protocol["hash"] == report.protocol_hash,
            "statistical report refers to another protocol")
    value = lambda name: statistical.field(name).get("value")  # noqa: E731
    status = lambda name: statistical.field(name)["status"]  # noqa: E731
    sizes = value("sample_size")
    require(sizes["experimental_unit"] == design.experimental_unit
            and sizes["unit_scope"] == kernel["scope"] and sizes["planned"] == design.sample_size
            and sizes["planned_total"] == kernel["planned_total"]
            and list(sizes["missing_slots"]) == kernel["missing"],
            "statistical report sample size differs from values the kernel computes")
    point = value("point_estimate")
    require(point["metric"] == protocol["payload"]["metric"]
            and point["unit"] == design.primary_metric.unit,
            "point estimate metric or unit differs from the preregistration")
    recomputed = {row.roster_unit: row.recomputed for row in recomputations}
    for row in point["by_roster_unit"]:
        require(row["roster_unit"] in recomputed and row["value"] == recomputed[row["roster_unit"]],
                "point estimate differs from the recomputed metric")
    if point["value"] is not None and len(kernel["roster"]) == 1:
        require(point["value"] == recomputed[kernel["roster"][0]],
                "point estimate differs from the recomputed metric")
    if status("stopping_rule") == "supplied":
        stop = value("stopping_rule")
        require(stop["rule_sha256"] == hashlib.sha256(design.stopping_rule.rule.encode("utf-8")).hexdigest()
                and stop["roster_complete"] == kernel["roster_complete"],
                "stopping rule report differs from the preregistration or roster")
    if status("multiple_testing") == "supplied":
        tests = value("multiple_testing")
        require(list(tests["family"]) == list(design.multiple_testing.family)
                and tests["correction"] == design.multiple_testing.correction,
                "multiple-testing family or correction differs from the preregistration")
    detected: list[str] = []
    if status("estimand") == "supplied" and value("estimand")["text"] != design.estimand:
        detected.append("estimand")
    if status("uncertainty") == "supplied":
        uncertainty = value("uncertainty")
        if (uncertainty["method"], uncertainty["resampling_unit"]) != (
                design.uncertainty.method, design.uncertainty.resampling_unit):
            detected.append("uncertainty")
    if (status("stopping_rule") == "supplied" and value("stopping_rule")["interim_looks"] > 0
            and design.stopping_rule.kind == "fixed_sample"):
        detected.append("stopping_rule")
    exclusions = list(sizes["exclusions"])
    for row in exclusions:
        if row["preregistered"]:
            require(row["rule"] in design.exclusions,
                    "an exclusion marked preregistered is absent from the design")
        elif "sample_size" not in detected:
            detected.append("sample_size")
    excluded = sum(row["count"] for row in exclusions)
    if sizes["analysed"] != kernel["planned_total"] - excluded and "sample_size" not in detected:
        detected.append("sample_size")
    declared: list[Any] = list(value("deviations")) if status("deviations") == "supplied" else []
    fields = {row["field"] for row in declared}
    if status("deviations") != "supplied":
        require(not detected, "deviations are not supplied but the kernel found: " + ", ".join(detected))
    undeclared = [name for name in detected if name not in fields]
    require(not undeclared, "undeclared deviation from the preregistration: " + ", ".join(undeclared))
    reasons: list[str] = []
    mode_cap = design.mode
    outcomes = ["supports", "refutes", "inconclusive"]
    if not (status("confidence_interval") == "supplied" and status("effect_size") == "supplied"):
        if design.mode == "descriptive" and design.uncertainty.method == "not_applicable":
            reasons.append(REASON_DESCRIPTIVE)
        else:
            outcomes = ["inconclusive"]
            reasons.append(REASON_INTERVAL)
    if declared or status("deviations") != "supplied":
        reasons.append(REASON_DEVIATIONS)
        mode_cap = min(mode_cap, "exploratory", key=MODE_ORDER.__getitem__)
    if mode_cap == "confirmatory":
        complete = all(status(name) == "supplied" or (status(name) == "not_applicable")
                       for name in api.STATISTICAL_FIELDS)
        assumptions_ok = status("assumptions") == "supplied" and all(
            row["status"] == "holds" for row in value("assumptions"))
        if not (complete and not declared and status("deviations") == "supplied"
                and sizes["analysed"] == kernel["planned_total"] - sum(
                    row["count"] for row in exclusions if row["preregistered"])
                and kernel["roster_complete"] and assumptions_ok):
            mode_cap = "exploratory"
            reasons.append(REASON_CONFIRMATORY)
    require(report.inference_mode in MODE_ORDER,
            "pack analysis needs a descriptive, exploratory or confirmatory inference mode")
    require(MODE_ORDER[report.inference_mode] <= MODE_ORDER[mode_cap],
            f"proposed inference mode {report.inference_mode} exceeds the ceiling {mode_cap}")
    require(report.outcome in outcomes,
            f"proposed outcome {report.outcome} exceeds the ceiling {'/'.join(outcomes)}")
    if (report.outcome != "inconclusive" and design.mode == "descriptive"
            and design.uncertainty.method == "not_applicable"
            and not (status("confidence_interval") == "supplied" and status("effect_size") == "supplied")):
        require(report.inference_mode == "descriptive",
                "a descriptive conclusion without uncertainty must stay descriptive")
    added = [f"Statistical report field {name} was not supplied: {statistical.field(name)['reason']}"
             for name in api.STATISTICAL_FIELDS if status(name) == "not_supplied"]
    added.extend(f"Statistical report deviation in {row['field']}: planned {row['plan']}; "
                 f"actual {row['actual']}; reason: {row['reason']}" for row in declared)
    added.append(f"Kernel claim-strength ceiling v1: inference at most {mode_cap}; allowed outcomes "
                 f"{'/'.join(outcomes)}"
                 + (f"; {'; '.join(reasons)}." if reasons else "."))
    return dict(schema_version=CEILING_VERSION, max_inference_mode=mode_cap, allowed_outcomes=outcomes,
                reasons=reasons, detected_deviations=detected,
                not_supplied=[name for name in api.STATISTICAL_FIELDS if status(name) == "not_supplied"],
                kernel_limitations=added)


def _assess(store: Store, facts: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """Validate submitted envelopes against kernel facts; no pack code is imported."""
    p = facts["binding"]["payload"]
    require((request["pack_id"], request["pack_version"], request["pack_code_digest"])
            == (p["pack_id"], p["pack_version"], p["pack_code_digest"]),
            "analysis pack identity or code differs from the pinned binding")
    require(type(request["checks"]) is list and type(request["recomputations"]) is list,
            "pack analysis checks and recomputations must be lists")
    checks = [_envelope(api.OutputCheck.from_dict, row, "output check") for row in request["checks"]]
    recomputations = [_envelope(api.Recomputation.from_dict, row, "recomputation")
                      for row in request["recomputations"]]
    statistical = _envelope(api.StatisticalReport.from_dict, request["statistical_report"],
                            "statistical report")
    report = _envelope(lambda value: api.AnalysisReport.from_frozen(value, statistical),
                       request["report"], "analysis report")
    require((report.pack_id, report.pack_version) == (p["pack_id"], p["pack_version"]),
            "analysis report was made by another pack or version")
    kernel = _kernel_fields(store, facts)
    require([check.slot for check in checks] == [row["slot"] for row in facts["slots"]],
            "output checks must cover the batch roster in order")
    for check, row in zip(checks, facts["slots"]):
        require((check.roster_unit, check.mode, check.result, check.raw_data, check.metrics)
                == (row["roster_unit"], row["mode"], row["result"], row["outputs"]["raw_data"],
                    row["outputs"]["metrics"]),
                "output check differs from the recorded batch slot")
        require(check.status == "passed", "pack output check failed: " + "; ".join(check.findings))
    require(sorted(row.roster_unit for row in recomputations) == sorted(kernel["roster"])
            and len(recomputations) == len(kernel["roster"]),
            "recomputations must cover every roster unit once")
    metric = facts["protocol"]["payload"]["metric"]
    for row in recomputations:
        require(row.metric == metric and row.tolerance == p["numeric_tolerance"],
                "recomputation metric or tolerance differs from the pinned pack")
        require(row.primary_recorded == kernel["recorded"][f"primary:{row.roster_unit}"]
                and row.reanalysis_recorded == kernel["recorded"][f"reanalysis:{row.roster_unit}"],
                "recomputation misstates a recorded metric")
    limits = ceiling(statistical, report, facts, kernel, recomputations)
    return dict(checks=checks, recomputations=recomputations, statistical=statistical,
                report=report, ceiling=limits,
                limitations=[*report.limitations, *limits["kernel_limitations"]])


def replication(binding: dict[str, Any]) -> dict[str, Any]:
    """Replication mode and independence facts the kernel derives, never the pack."""
    return dict(mode="same_data_reanalysis", data="same_raw_data",
                implementations="distinct_programs_from_one_pinned_pack",
                roster_repetition=_REPETITION[binding["payload"]["roster_semantics"]],
                independence=dict(implementation="same_pack_author", context="not_established",
                                  data="not_independent", actors="caller_declared"))


def _task(batch: str, settlement: str, pin: dict[str, str], report: str) -> str:
    return digest(canonical(dict(batch=batch, settlement=settlement, report=report, **pin)))


def _analysis_payload(facts: dict[str, Any], claim: dict[str, Any], assessed: dict[str, Any],
                      keys: dict[str, str], cas_allowlist: list[str], hidden: list[str],
                      task_id: str, reviewer_actor: str) -> dict[str, Any]:
    state, binding, protocol = facts["state"], facts["binding"], facts["protocol"]
    p = binding["payload"]
    return dict(schema_version=1, contract_version=api.CONTRACT_VERSION,
                batch=state["plan"]["id"], batch_hash=state["plan"]["hash"],
                settlement=state["settlement"]["id"], settlement_hash=state["settlement"]["hash"],
                terminal=state["terminal"]["id"], terminal_hash=state["terminal"]["hash"],
                protocol=protocol["id"], protocol_hash=protocol["hash"], binding=binding["id"],
                binding_hash=binding["hash"], claim=claim["id"], claim_hash=claim["hash"],
                pack_id=p["pack_id"], pack_version=p["pack_version"],
                pack_code_digest=p["pack_code_digest"], report=keys["report"],
                statistical_report=keys["statistical_report"], checks=keys["checks"],
                recomputations=keys["recomputations"], cas_allowlist=cas_allowlist,
                hidden_digests=hidden, ceiling=assessed["ceiling"], replication=replication(binding),
                report_origin=REPORT_ORIGIN, task_id=task_id, reviewer_actor=reviewer_actor,
                runs=state["settlement"]["payload"]["runs"],
                results=state["settlement"]["payload"]["results"], pack_trust=PACK_TRUST,
                hook_isolation=HOOK_ISOLATION, scientific_validity="not_assessed")


def _keys(assessed: dict[str, Any]) -> dict[str, tuple[str, bytes]]:
    values = dict(report=assessed["report"].to_dict(),
                  statistical_report=assessed["statistical"].to_dict(),
                  checks=[check.to_dict() for check in assessed["checks"]],
                  recomputations=[row.to_dict() for row in assessed["recomputations"]])
    return {name: (digest(canonical(value)), canonical(value)) for name, value in values.items()}


def _claim_payload(facts: dict[str, Any], assessed: dict[str, Any]) -> dict[str, Any]:
    protocol = facts["protocol"]
    report = assessed["report"]
    return dict(protocol=protocol["id"], statement=report.statement,
                scope=protocol["payload"]["scope"], evidence=facts["state"]["settlement"]["payload"]["runs"],
                limitations=assessed["limitations"], outcome=report.outcome,
                inference_mode=report.inference_mode)


def _validate_analysis_request(request: Any) -> None:
    require(type(request) is dict and set(request) == _ANALYSE_FIELDS,
            "invalid pack analysis request fields")
    require(type(request["reviewer_actor"]) is str and bool(request["reviewer_actor"].strip())
            and len(request["reviewer_actor"]) <= 128, "analysis reviewer actor is required")
    require(len(canonical(request)) <= 8 * api.MAX_ENVELOPE_BYTES, "pack analysis request is too large")


def _admission(store: Store, history: list[dict[str, Any]], request: dict[str, Any],
               study_id: str, bindings: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    _validate_analysis_request(request)
    facts = _batch_state(store, history, request["batch"], request["expected_settlement"], bindings)
    originals = [row for row in store.receipts() if request["batch"] in row["event_ids"]]
    require(len(originals) == 1 and originals[0]["context"]["study_id"] == study_id
            and facts["binding"]["payload"]["study_id"] == study_id,
            "analysis study differs from the batch and binding study")
    assessed = _assess(store, facts, request)
    blobs, hidden = _allowlist(store, facts)
    keys = _keys(assessed)
    observed = {key for row in facts["slots"] for key in row["outputs"].values()}
    require(not {key for key, _ in keys.values()} & (observed | set(blobs)),
            "analysis artifacts cannot alias observed outputs or allowed inputs")
    p = facts["binding"]["payload"]
    pin = dict(pack_id=p["pack_id"], pack_version=p["pack_version"],
               pack_code_digest=p["pack_code_digest"])
    task_id = _task(request["batch"], request["expected_settlement"], pin, keys["report"][0])
    return dict(facts=facts, assessed=assessed, keys=keys, allowlist=sorted(blobs),
                hidden=sorted(hidden), task_id=task_id)


def _analysis_index(store: Store, history: list[dict[str, Any]], *,
                    receipts: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Recompute each committed pack analysis on its historical prefix."""
    events = {event["id"]: event for event in history if event["kind"] == ANALYSIS}
    if receipts is None and not any(event["kind"] in KINDS for event in history):
        return {}
    source = store.receipts() if receipts is None else receipts
    relevant = [row for row in source if row["request"]["action"] == "pack.analyse"
                and row["after_revision"] <= len(history)]
    # Bindings are replayed once; each analysis uses those preceding its receipt.
    bindings = _binding_index(store, history, receipts=source)
    seen: set[str] = set()
    tasks: set[str] = set()
    for receipt in relevant:
        context, command = receipt["context"], receipt["request"]
        require(context["role"] == "analyst" and command["version"] == 1,
                "invalid historical pack analysis command")
        before = history[:receipt["before_revision"]]
        created = history[receipt["before_revision"]:receipt["after_revision"]]
        require(len(created) == 2 and [event["kind"] for event in created] == ["claim", ANALYSIS]
                and receipt["event_ids"] == [event["id"] for event in created],
                "pack analysis needs its exact claim and analysis receipt")
        claim, event = created
        require(all((item["actor"], item["role"]) == (context["actor"], "analyst") for item in created),
                "pack analysis actor differs from its command")
        prior = {protocol: event for protocol, event in bindings.items()
                 if event["seq"] <= len(before)}
        admitted = _admission(store, before, command["payload"], context["study_id"], prior)
        require(claim["payload"] == _claim_payload(admitted["facts"], admitted["assessed"]),
                "pack analysis claim differs from its frozen report and roster")
        expected = _analysis_payload(admitted["facts"], claim, admitted["assessed"],
                                     {name: key for name, (key, _) in admitted["keys"].items()},
                                     admitted["allowlist"], admitted["hidden"], admitted["task_id"],
                                     command["payload"]["reviewer_actor"])
        require(set(event["payload"]) == _ANALYSIS_FIELDS and event["payload"] == expected,
                "pack analysis event differs from its frozen evidence")
        for key, data in admitted["keys"].values():
            require(store.read(key) == data, "pack analysis artifact differs from its command")
        observer = Kernel(store, Actor("pack-analysis-validator", "observer"))
        prefix = history[:receipt["after_revision"]]
        gate = observer._gate(prefix, claim["id"])
        require(gate["passed"], "historical pack analysis mechanical gate failed")
        require(command["payload"]["reviewer_actor"] not in observer._review_members(prefix, claim["id"])[1],
                "historical pack analysis reviewer contributed to evidence")
        require(receipt["result"] == dict(analysis=event["id"], claim=claim["id"],
                                          basis_hash=gate["basis_hash"],
                                          report=admitted["keys"]["report"][0],
                                          task_id=admitted["task_id"],
                                          scientific_validity="not_assessed"),
                "pack analysis receipt result differs from its historical gate")
        require(admitted["task_id"] not in tasks, "duplicate historical pack analysis task")
        tasks.add(admitted["task_id"])
        seen.add(event["id"])
    require(seen == set(events), "pack analysis lacks its original command receipt")
    admitted_claims = {events[id]["payload"]["claim"] for id in seen}
    stray = [event["id"] for event in history if event["kind"] == "claim"
             and event["payload"]["protocol"] in bindings and event["id"] not in admitted_claims]
    require(not stray, "claims on pack-bound protocols must come from pack.analyse: "
            + ", ".join(stray))
    return {id: events[id] for id in seen}


def pack_analyses(store: Store, history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not any(event["kind"] in KINDS for event in history):
        return {}
    return _analysis_index(store, history)


class PackAnalysis:
    """Analyst command: admit a pack report for a completed batch under the ceiling."""

    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def analyse(self, *, batch: str, expected_settlement: str, pack_id: str, pack_version: str,
                pack_code_digest: str, checks: list[dict[str, Any]],
                recomputations: list[dict[str, Any]], report: dict[str, Any],
                statistical_report: dict[str, Any], reviewer_actor: str) -> dict[str, str]:
        require(self.store._command_context is not None and self.actor.role == "analyst",
                "pack analysis requires an analyst CommandService transaction")
        request = dict(batch=batch, expected_settlement=expected_settlement, pack_id=pack_id,
                       pack_version=pack_version, pack_code_digest=pack_code_digest, checks=checks,
                       recomputations=recomputations, report=report,
                       statistical_report=statistical_report, reviewer_actor=reviewer_actor)
        history = self.store.events()
        prior = _analysis_index(self.store, history)
        admitted = _admission(self.store, history, request, self.store._command_context["study_id"])
        require(not any(event["payload"]["task_id"] == admitted["task_id"] for event in prior.values()),
                "pack analysis task already applied")
        # The live pack must still be exactly the pinned code (decision 3 of ADR 0016),
        # and the admitted envelopes must be what that code computes on this snapshot.
        loaded = require_live_pack(self.store, admitted["facts"]["binding"])
        rerun = run_hooks(loaded, *_hook_inputs(self.store, admitted["facts"]))
        require(all(canonical(rerun[name]) == canonical(request[name]) for name in
                    ("checks", "recomputations", "report", "statistical_report")),
                "submitted analysis differs from what the pinned pack computes on this snapshot")
        for key, data in admitted["keys"].values():
            require(self.store.put(data) == key, "pack analysis artifact CAS digest mismatch")
        claim_fields = _claim_payload(admitted["facts"], admitted["assessed"])
        kernel = Kernel(self.store, self.actor)
        claim_id = kernel._claim(**claim_fields, pack_admission=True)
        claim = Kernel._get(self.store.events(), claim_id, "claim")
        require(claim["payload"] == claim_fields, "recorded claim differs from the admitted report")
        analysis_id = kernel._write(self.store.events(), ANALYSIS, _analysis_payload(
            admitted["facts"], claim, admitted["assessed"],
            {name: key for name, (key, _) in admitted["keys"].items()}, admitted["allowlist"],
            admitted["hidden"], admitted["task_id"], reviewer_actor), {"analyst"})
        updated = self.store.events()
        gate = kernel._gate(updated, claim_id)
        require(gate["passed"], "pack analysis mechanical gate failed: " + "; ".join(gate["failures"]))
        require(independent_of(reviewer_actor, kernel._review_members(updated, claim_id)[1]),
                "analysis reviewer contributed to evidence")
        return dict(analysis=analysis_id, claim=claim_id, basis_hash=gate["basis_hash"],
                    report=admitted["keys"]["report"][0], task_id=admitted["task_id"],
                    scientific_validity="not_assessed")


def store_capture(store: Store, pack_id: str, source: Any) -> dict[str, Any]:
    """Run a pack's read-only capture once and store the bytes; no event is written.

    Only ``pack.preregister`` turns a capture into evidence provenance. The
    hashes identify captured bytes, not when or how completely they were made.
    """
    loaded = registry.load_pack(pack_id)
    require(loaded.manifest.capture, "pack does not capture external sources")
    bundle = _call(loaded, "capture", source)
    require(type(bundle) is api.CaptureBundle
            and (bundle.pack_id, bundle.pack_version) == (loaded.pack_id, loaded.pack_version),
            "capture hook must return this pack's CaptureBundle")
    for key, data in bundle.blobs().items():
        require(store.put(data) == key, "captured bytes CAS digest mismatch")
    key = store.put_json(bundle.to_dict())
    require(key == bundle.digest(), "capture bundle CAS digest mismatch")
    return dict(capture=key, pack_id=loaded.pack_id, pack_version=loaded.pack_version,
                pack_code_digest=loaded.pack_code_digest, source_label=bundle.source_label,
                files=len(bundle.files), audit=api.thaw(bundle.audit), events_written=0,
                meaning="captured bytes stored in CAS without events; bind them with pack.preregister")


VERIFIED_PARTS = ("catalog", "protocol_draft", "execution_plan", "validate_protocol",
                  "checks", "recomputations", "report", "statistical_report")


def describe_pack(pack_id: str) -> dict[str, Any]:
    """Read-only identity of the live registered pack; no Store is opened."""
    loaded = registry.load_pack(pack_id)
    catalog = _call(loaded, "describe")
    require(type(catalog) is api.ParameterCatalog, "pack describe() must return a ParameterCatalog")
    return dict(pack_id=loaded.pack_id, pack_version=loaded.pack_version,
                pack_code_digest=loaded.pack_code_digest, manifest=loaded.manifest.to_dict(),
                catalog=catalog.to_dict(), code_manifest=api.thaw(loaded.code_manifest),
                registered=registry.registered(), pack_trust=PACK_TRUST, hook_isolation=HOOK_ISOLATION,
                meaning="live registered pack code; a binding pins one exact pack_code_digest")


def _pin_row(event: dict[str, Any]) -> tuple[dict[str, Any], registry.LoadedPack | None]:
    p = event["payload"]
    row = dict(pack_id=p["pack_id"], pack_version=p["pack_version"],
               pack_code_digest=p["pack_code_digest"])
    try:
        loaded = registry.load_pack(p["pack_id"])
        loaded.require_pin(pack_id=p["pack_id"], pack_version=p["pack_version"],
                           pack_code_digest=p["pack_code_digest"])
    except ValueError as exc:
        row["pin"] = f"live pack code differs from the pin: {exc}"
        return row, None
    row["pin"] = "matched"
    return row, loaded


def _compare(row: dict[str, Any], name: str, actual: str, expected: str) -> None:
    row[name] = "matched" if actual == expected else f"differs: recomputed {actual}"


def _verify_binding(store: Store, history: list[dict[str, Any]], event: dict[str, Any]) -> dict[str, Any]:
    p = event["payload"]
    row, loaded = _pin_row(event)
    row.update(binding=event["id"], protocol=p["protocol"])
    if loaded is None:
        return row
    pinned = _pinned(store, p)
    try:
        compiled = _compile(store, loaded, p["parameters"], pinned["host"], p["capture"])
        _compare(row, "catalog", compiled["catalog"].digest(), p["catalog"])
        _compare(row, "protocol_draft", compiled["draft"].digest(), p["protocol_draft"])
        _compare(row, "execution_plan", compiled["plan"].digest(), p["execution_plan"])
    except ValueError as exc:
        row["compile"] = f"failed: {exc}"
    try:
        protocol = Kernel._get(history, p["protocol"], "protocol")
        _call(loaded, "validate_protocol", _protocol_context(
            p["parameters"], pinned["draft"], pinned["plan"], pinned["capture"], protocol))
        row["validate_protocol"] = "matched"
    except ValueError as exc:
        row["validate_protocol"] = f"failed: {exc}"
    return row


def _verify_analysis(store: Store, history: list[dict[str, Any]], event: dict[str, Any],
                     receipts: list[dict[str, Any]]) -> dict[str, Any]:
    p = event["payload"]
    row, loaded = _pin_row(event)
    row.update(analysis=event["id"], batch=p["batch"], claim=p["claim"])
    if loaded is None:
        return row
    receipt = next(item for item in receipts if event["id"] in item["event_ids"])
    try:
        facts = _batch_state(store, history[:receipt["before_revision"]], p["batch"], p["settlement"])
        hooks = run_hooks(loaded, *_hook_inputs(store, facts))
    except ValueError as exc:
        row["hooks"] = f"failed: {exc}"
        return row
    for name in ("checks", "recomputations", "report", "statistical_report"):
        _compare(row, name, digest(canonical(hooks[name])), p[name])
    return row


def verify(store: Store) -> dict[str, Any]:
    """Read-only: re-run pinned hooks for every recorded binding and analysis.

    Replay first proves the records are internally consistent; this then asks
    whether the live registered code is the pinned code and reproduces the
    recorded bytes. It writes nothing and does not assess scientific validity.
    """
    history = store.events()
    receipts = store.receipts()
    bindings = _binding_index(store, history, receipts=receipts)
    analyses = _analysis_index(store, history, receipts=receipts)
    rows = dict(bindings=[_verify_binding(store, history, event) for event in bindings.values()],
                analyses=[_verify_analysis(store, history, event, receipts)
                          for event in analyses.values()])
    passed = all(row["pin"] == "matched" and all(row.get(name, "matched") == "matched"
                                                 for name in (*VERIFIED_PARTS, "compile", "hooks"))
                 for row in rows["bindings"] + rows["analyses"])
    return dict(revision=len(history), snapshot_hash=history[-1]["hash"] if history else "0" * 64,
                status="matched" if passed else "mismatched", **rows,
                scientific_validity="not_assessed",
                meaning="read-only re-execution of pinned pack hooks; equal bytes, not correct statistics")


def run_hooks(loaded: registry.LoadedPack, context: api.AnalysisContext, cas: api.CasView
              ) -> dict[str, Any]:
    """Run the analysis hooks over one CasView; returns their command envelopes.

    The controller calls this outside the write transaction; ``pack.analyse``
    calls it again on the same snapshot and admits only identical bytes.
    """
    checks = _call(loaded, "validate_outputs", context, cas)
    require(type(checks) in (list, tuple) and all(type(check) is api.OutputCheck for check in checks),
            "validate_outputs must return OutputCheck envelopes")
    recomputations = _call(loaded, "recompute_metrics", context, cas)
    require(type(recomputations) in (list, tuple)
            and all(type(row) is api.Recomputation for row in recomputations),
            "recompute_metrics must return Recomputation envelopes")
    report = _call(loaded, "analyse", context, cas, tuple(checks), tuple(recomputations))
    require(type(report) is api.AnalysisReport, "analyse must return an AnalysisReport")
    require(not cas.refused, "pack hooks asked for bytes outside the allowlist: "
            + ", ".join(cas.refused))
    return dict(checks=[check.to_dict() for check in checks],
                recomputations=[row.to_dict() for row in recomputations],
                report=report.to_dict(), statistical_report=report.statistical_report.to_dict(),
                reads=list(cas.reads))
