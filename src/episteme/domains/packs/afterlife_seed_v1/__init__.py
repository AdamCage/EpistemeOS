"""DomainPack facade of the offline historical Afterlife stop-rate pilot (ADR 0015).

``capture`` audits one completed run read-only; compilation re-verifies the
captured bytes purely and rebuilds the legacy input bundle. Programs and
recount equal the legacy ``afterlife_seed`` modules, which stay unchanged for
legacy bindings. The nine trajectories were observed before any protocol, come
from one model/configuration, and steps within a trajectory are not
independent units; the pack never claims more than an exploratory description.
"""

import hashlib

from episteme.domains import api

from . import analysis, inventory, programs
# Imported before the hook named ``capture`` is defined below, which then
# replaces the package attribute that the submodule import set.
from . import capture as reader


PACK_ID = inventory.DOMAIN_ID
PACK_VERSION = "1"
NUMERIC_TOLERANCE = 1e-12  # Absolute arithmetic agreement, not scientific uncertainty.
WALL_SECONDS = 30
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
LIMITATIONS = [
    "All nine trajectories and their legacy manifest were seen before this protocol; this is not prospective preregistration.",
    "Capture rejects any run whose saved steps disagree with its legacy manifest, so the competing inconsistency explanation cannot produce a recorded outcome in this pilot.",
    "The --seed values are deterministic trajectory slot indices, not fresh stochastic samples.",
    "The separate reanalysis reads the same historical observations, including the legacy manifest counts it checks against; it is neither blind nor new-data replication.",
    "Both analysis programs and this adapter come from one local development context; distinct digests do not establish independent authorship.",
    "finish_reason is provider-reported; the stop-event rate is a protocol diagnostic and does not recompute the S1 semantic gap, whose embeddings are absent from the captured files.",
    "One historical model/configuration is examined; steps within a trajectory are not independent units, and no causal, population, or cross-model conclusion follows.",
    "No uncertainty interval, significance test, scientific reviewer verdict, or source-environment closure is supplied.",
]

MANIFEST = {
    "contract_version": 1, "pack_id": PACK_ID, "pack_version": PACK_VERSION,
    "description": "Offline recount of stop events in one captured historical Semantic Afterlife run.",
    "roster_semantics": ["frozen_unit_index"],
    "metrics": [{"name": inventory.METRIC, "unit": "fraction",
                 "description": "Share of steps whose provider finish_reason is neither null nor length"}],
    "outputs": {"raw_data": {"path": "raw.json", "schema_id": "afterlife_seed_v1/raw-v1"},
                "metrics": {"path": "metrics.json", "schema_id": "afterlife_seed_v1/metrics-v1"}},
    "numeric_tolerance": NUMERIC_TOLERANCE, "hidden_inputs": [],
    "execution_profiles": ["trusted_local_python_v1"],
    "statistical_capabilities": ["estimand", "estimator", "point_estimate", "assumptions",
                                 "sample_size", "stopping_rule", "sensitivity_analysis", "deviations"],
    "interpretation_cautions": LIMITATIONS[:3] + LIMITATIONS[5:7],
    "capture": True,
}


def describe():
    return api.ParameterCatalog(
        schema_version=1, pack_id=PACK_ID, pack_version=PACK_VERSION,
        description="Recount stop events of the nine trajectories of one captured historical run.",
        parameters_schema={"type": "object", "additionalProperties": False, "required": [],
                           "properties": {}},
        metric=inventory.METRIC, outputs=dict(inventory.OUTPUTS),
        limitations=["The captured trajectories were observed before the protocol.",
                     "Slot indices select existing trajectories; they are not random seeds."])


def validate_parameters(parameters):
    if dict(parameters) != {}:
        raise ValueError("the historical pilot takes no model-visible parameters")


def capture(source):
    return reader.read_run(source, pack_id=PACK_ID, pack_version=PACK_VERSION)


def _verified(request):
    validate_parameters(request.parameters)
    if dict(request.host_inputs) != {}:
        raise ValueError("the historical pilot takes no host inputs besides its capture")
    if request.capture is None or (request.capture.pack_id, request.capture.pack_version) != (PACK_ID, PACK_VERSION):
        raise ValueError("the historical pilot needs a capture made by this pack")
    return inventory.verify(dict(request.capture.files))


def compile_protocol(request):
    checked = _verified(request)
    data = hashlib.sha256(checked["bundle"]).hexdigest()
    rule = ("Use exactly the nine already observed trajectories in the selected historical "
            "run, with no exclusions or interim selection. Execute one primary analysis "
            "and one same-data reanalysis per trajectory.")
    design = {
        "schema_version": 1, "mode": "exploratory", "experimental_unit": "historical trajectory",
        "estimand": ("Nine trajectory-specific stop-event fractions in the selected legacy run; "
                     "no population or causal estimand is claimed."),
        "primary_metric": {"name": inventory.METRIC, "unit": "fraction"}, "secondary_metrics": [],
        "sample_size": 9,
        "sample_size_rationale": ("All nine pre-existing trajectories in the selected run; "
                                  "the count was observed before this protocol and is not a power calculation."),
        "uncertainty": {"method": "not_applicable", "resampling_unit": None,
                        "rationale": ("Only per-trajectory descriptive values are reported; no interval, "
                                      "significance test or independence assumption is asserted.")},
        "exclusions": [], "stopping_rule": {"kind": "fixed_sample", "rule": rule},
        "multiple_testing": {"family": [], "correction": "not_applicable",
                             "rationale": "No hypothesis-family error claim or significance test."},
        "data_splits": [{"id": "historical_nine_open", "digest": data, "role": "discovery",
                         "exposure_policy": "open"}],
    }
    seen = sorted({data, checked["manifest_sha256"], *checked["integrity"].values()})
    return api.ProtocolDraft(
        schema_version=1,
        design=("Offline consistency check of nine saved Afterlife step traces; "
                "slot indices 0-8 identify existing trajectories, not fresh stochastic seeds."),
        analysis_plan=("Count a stop event when finish_reason is neither null nor 'length'; "
                       "report the unrounded fraction and compare its four-decimal rounding "
                       "with the legacy manifest. Check the "
                       "full saved-step roster against the legacy manifest, and separately "
                       "reanalyze the primary normalized raw output."),
        metric=inventory.METRIC, stopping_rule=rule, statistical_design=design,
        roster=list(range(9)), roster_semantics="frozen_unit_index", sample_size_scope="total",
        run_limit=18, replication_tolerance=0, seen_data=seen)


def compile_execution(request):
    checked = _verified(request)
    return api.ExecutionPlan(
        primary_program=programs.PRIMARY_SOURCE, reanalysis_program=programs.REANALYSIS_SOURCE,
        input=checked["bundle"], outputs=dict(inventory.OUTPUTS), wall_seconds=WALL_SECONDS,
        max_output_bytes=MAX_OUTPUT_BYTES, required_capabilities=[],
        execution_profile="trusted_local_python_v1",
        environment_requirements=api.environment_requirements())


def validate_protocol(context):
    """Historical exposure must be declared: open discovery data already seen."""
    protocol = context.protocol
    design = protocol["statistical_design"]
    if (protocol["metric"] != inventory.METRIC or protocol.get("protocol_mode") != "exploratory"
            or design["sample_size"] != 9 or design["experimental_unit"] != "historical trajectory"
            or list(protocol["seeds"]) != list(range(9))
            or protocol["data"] != context.execution_plan["input"]["sha256"]
            or protocol["data"] not in protocol["seen_data"]
            or not any(row["digest"] == protocol["data"] and row["role"] == "discovery"
                       and row["exposure_policy"] == "open" for row in design["data_splits"])
            or context.capture is None):
        raise ValueError("historical exposure is not declared as open discovery data")


def validate_outputs(context, cas):
    document, _ = analysis.bundle(context, cas)
    manifest_key = document["manifest_sha256"]
    checks = []
    for row in context.slots:
        findings = []
        try:
            entry = document["trajectories"][row["roster_unit"]]
            if row["mode"] == "primary":
                analysis.raw(cas, row["outputs"]["raw_data"], manifest_key, entry)
            elif context.slot(f"primary:{row['roster_unit']}")["outputs"]["raw_data"] != row["outputs"]["raw_data"]:
                raise ValueError("historical reanalysis did not retain identical raw data")
            analysis.metric(cas, row["outputs"]["metrics"])
        except ValueError as exc:
            findings.append(str(exc))
        checks.append(api.OutputCheck(
            schema_version=1, slot=row["slot"], roster_unit=row["roster_unit"], mode=row["mode"],
            result=row["result"], raw_data=row["outputs"]["raw_data"],
            metrics=row["outputs"]["metrics"], status="failed" if findings else "passed",
            findings=findings))
    return checks


def recompute_metrics(context, cas):
    document, _ = analysis.bundle(context, cas)
    rows = []
    for unit in context.draft.roster:
        entry = document["trajectories"][unit]
        primary = context.slot(f"primary:{unit}")
        reanalysis = context.slot(f"reanalysis:{unit}")
        analysis.raw(cas, primary["outputs"]["raw_data"], document["manifest_sha256"], entry)
        if reanalysis["outputs"]["raw_data"] != primary["outputs"]["raw_data"]:
            raise ValueError("historical reanalysis did not retain identical raw data")
        observed = analysis.steps(cas, entry)
        value = observed[inventory.METRIC]
        recorded = analysis.metric(cas, primary["outputs"]["metrics"])
        repeated = analysis.metric(cas, reanalysis["outputs"]["metrics"])
        if abs(value - recorded) > context.numeric_tolerance or abs(value - repeated) > context.numeric_tolerance:
            raise ValueError("historical stop rate differs from raw steps")
        rows.append(api.Recomputation(
            schema_version=1, roster_unit=unit, metric=inventory.METRIC, recomputed=value,
            primary_recorded=recorded, reanalysis_recorded=repeated,
            primary_absolute_difference=abs(value - recorded),
            reanalysis_absolute_difference=abs(value - repeated), tolerance=context.numeric_tolerance))
    return rows


def analyse(context, cas, checks, recomputations):
    failed = [check for check in checks if check.status != "passed"]
    if failed:
        raise ValueError("historical outputs failed pack checks: "
                         + "; ".join(item for check in failed for item in check.findings))
    document, _ = analysis.bundle(context, cas)
    by_unit = {row.roster_unit: row for row in recomputations}
    if sorted(by_unit) != list(range(9)):
        raise ValueError("historical recomputations do not cover the nine slots")
    details = []
    for entry in document["trajectories"]:
        unit = entry["slot_index"]
        primary = context.slot(f"primary:{unit}")
        observed = analysis.steps(cas, entry)
        details.append(dict(slot_index=unit, trajectory_id=entry["trajectory_id"],
                            semantic_seed=entry["semantic_seed"],
                            original_stochastic_seed=entry["stochastic_seed"],
                            raw_data=primary["outputs"]["raw_data"], primary_run=primary["run"],
                            reanalysis_run=context.slot(f"reanalysis:{unit}")["run"],
                            n_steps=observed["n_steps"], stop_events=observed["stop_events"],
                            stop_event_rate=by_unit[unit].recomputed))
    rates = [row["stop_event_rate"] for row in details]
    design = context.draft.typed_design()
    unchecked = "Not tested by this pack."
    report = api.StatisticalReport.from_dict({
        "schema_version": 1, "protocol_hash": context.protocol_hash,
        "estimand": api.supplied({"protocol_hash": context.protocol_hash, "text": design.estimand}),
        "estimator": api.supplied({"name": "stop_event_fraction", "hook": "recompute_metrics",
                                   "description": ("Steps whose provider finish_reason is neither null "
                                                   "nor 'length', divided by all steps of one trajectory.")}),
        "point_estimate": api.supplied({
            "metric": inventory.METRIC, "unit": design.primary_metric.unit, "value": None,
            "by_roster_unit": [{"roster_unit": row["slot_index"], "value": row["stop_event_rate"]}
                               for row in details]}),
        "uncertainty": api.not_applicable(
            "Preregistered uncertainty.method is not_applicable; per-trajectory descriptions only."),
        "confidence_interval": api.not_supplied(
            "Steps within a trajectory are not independent and all nine trajectories come from one "
            "model/configuration; no interval is computed."),
        "effect_size": api.not_supplied(
            "No comparison between conditions was preregistered or computed for one historical run."),
        "assumptions": api.supplied([
            {"assumption": "Steps within a trajectory are independent observations.",
             "status": "violated", "reference": "Consecutive steps of one trajectory share context."},
            {"assumption": "The nine trajectories represent a population beyond the selected run.",
             "status": "unchecked", "reference": unchecked},
            {"assumption": "Provider-reported finish_reason values are accurate.",
             "status": "unchecked", "reference": unchecked},
        ]),
        "sample_size": api.supplied({
            "experimental_unit": design.experimental_unit, "unit_scope": "total",
            "planned": design.sample_size, "planned_total": design.sample_size, "analysed": len(details),
            "exclusions": [], "missing_slots": []}),
        "multiple_testing": api.not_applicable("No hypothesis family or significance test was preregistered."),
        "stopping_rule": api.supplied({
            "rule_sha256": hashlib.sha256(design.stopping_rule.rule.encode("utf-8")).hexdigest(),
            "roster_complete": True, "interim_looks": 0}),
        "sensitivity_analysis": api.supplied([]),
        "deviations": api.supplied([]),
    })
    return api.AnalysisReport(
        pack_id=PACK_ID, pack_version=PACK_VERSION, protocol_hash=context.protocol_hash,
        statement=("In one selected historical Semantic Afterlife run, stop-event "
                   "counts recomputed from the saved step records matched the legacy "
                   "manifest for all nine previously observed trajectories; "
                   f"per-trajectory stop-event rates ranged from {min(rates):.4f} "
                   f"to {max(rates):.4f}. Capture required this agreement before the "
                   "protocol was registered, so this exploratory description neither "
                   "discriminates the registered explanations nor explains the "
                   "variation."),
        limitations=list(LIMITATIONS), outcome="inconclusive", inference_mode="exploratory",
        details=dict(domain=PACK_ID, metric=inventory.METRIC, n_trajectories=9,
                     manifest_sha256=document["manifest_sha256"], numeric_tolerance=context.numeric_tolerance,
                     trajectories=details),
        statistical_report=report)
