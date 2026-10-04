"""Test-only DomainPack: describe frozen integer groups by their means.

This fixture exercises the pack contract. Its values are invented, its
programs are trivial and nothing it reports is scientific evidence.
"""

import hashlib
import json
import math

from episteme.domains import api


PACK_ID = "conformance_fixture_v1"
PACK_VERSION = "1"
METRIC = "unit_mean"
OUTPUTS = {"raw_data": "raw.json", "metrics": "metrics.json"}

MANIFEST = {
    "contract_version": 1, "pack_id": PACK_ID, "pack_version": PACK_VERSION,
    "description": "Test-only fixture: means of frozen integer groups.",
    "roster_semantics": ["frozen_unit_index"],
    "metrics": [{"name": METRIC, "unit": "points", "description": "Mean of one frozen group"}],
    "outputs": {"raw_data": {"path": "raw.json", "schema_id": "conformance_fixture_v1/raw-v1"},
                "metrics": {"path": "metrics.json", "schema_id": "conformance_fixture_v1/metrics-v1"}},
    "numeric_tolerance": 1e-12, "hidden_inputs": [],
    "execution_profiles": ["trusted_local_python_v1"],
    "statistical_capabilities": ["estimand", "estimator", "point_estimate", "sample_size",
                                 "stopping_rule", "sensitivity_analysis", "deviations"],
    "interpretation_cautions": ["Invented fixture values; not evidence about anything."],
    "capture": False,
}

PRIMARY = b'''import json, pathlib, sys
if len(sys.argv) != 4 or sys.argv[1:3] != ["input.dat", "--seed"]:
    raise ValueError("expected input.dat --seed N")
unit = int(sys.argv[3])
data = json.loads(pathlib.Path("input.dat").read_bytes())
values = [value * data["scale"] for value in data["groups"][unit]]
raw = {"schema_version": 1, "unit": unit, "values": values}
pathlib.Path("raw.json").write_bytes(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode())
mean = sum(values) / len(values)
pathlib.Path("metrics.json").write_bytes(json.dumps({"unit_mean": mean}).encode())
'''

REANALYSIS = b'''import json, math, pathlib, sys
if len(sys.argv) != 4 or sys.argv[1:3] != ["input.dat", "--seed"]:
    raise ValueError("expected input.dat --seed N")
source = pathlib.Path("input.dat").read_bytes()
raw = json.loads(source)
if raw["unit"] != int(sys.argv[3]):
    raise ValueError("raw data belong to another unit")
pathlib.Path("raw.json").write_bytes(source)
mean = math.fsum(raw["values"]) / len(raw["values"])
pathlib.Path("metrics.json").write_bytes(json.dumps({"unit_mean": mean}).encode())
'''


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def describe():
    return api.ParameterCatalog(
        schema_version=1, pack_id=PACK_ID, pack_version=PACK_VERSION,
        description="Scale frozen integer groups and describe each group mean.",
        parameters_schema={"type": "object", "additionalProperties": False,
                           "required": ["scale", "outcome", "inference_mode"],
                           "properties": {"scale": {"type": "integer", "minimum": 1,
                                                    "maximum": 10},
                                          "outcome": {"enum": ["supports", "refutes", "inconclusive"]},
                                          "inference_mode": {"enum": ["descriptive", "exploratory",
                                                                      "confirmatory"]}}},
        metric=METRIC, outputs=dict(OUTPUTS),
        limitations=["Invented fixture values; group means are descriptive only."])


def validate_parameters(parameters):
    # outcome and inference_mode let tests ask this honest fixture for a proposal
    # above the kernel ceiling; a real pack derives them from its analysis.
    _require(set(parameters) == {"scale", "outcome", "inference_mode"}
             and type(parameters["scale"]) is int and 1 <= parameters["scale"] <= 10,
             "scale must be an integer in [1, 10]")


def _groups(request):
    host = request.host_inputs
    _require(set(host) == {"groups"}, "host inputs require exactly groups")
    groups = [list(group) for group in host["groups"]]
    _require(1 <= len(groups) <= 16 and all(
        1 <= len(group) <= 64 and all(type(value) is int and abs(value) <= 1000 for value in group)
        for group in groups), "groups must be 1-16 nonempty lists of small integers")
    return groups


def _input(request):
    return _canonical({"groups": _groups(request), "scale": request.parameters["scale"]})


def compile_protocol(request):
    validate_parameters(request.parameters)
    groups = _groups(request)
    data = _input(request)
    rule = f"Describe each of the {len(groups)} frozen groups once; no interim looks."
    design = {
        "schema_version": 1, "mode": "descriptive", "experimental_unit": "frozen group",
        "estimand": "Mean of each frozen fixture group after scaling.",
        "primary_metric": {"name": METRIC, "unit": "points"}, "secondary_metrics": [],
        "sample_size": len(groups), "sample_size_rationale": "Every frozen fixture group.",
        "uncertainty": {"method": "not_applicable", "resampling_unit": None,
                        "rationale": "Descriptive means of the analysed groups only."},
        "exclusions": [], "stopping_rule": {"kind": "fixed_sample", "rule": rule},
        "multiple_testing": {"family": [], "correction": "not_applicable",
                             "rationale": "No tests."},
        "data_splits": [{"id": "fixture_groups", "digest": hashlib.sha256(data).hexdigest(),
                         "role": "discovery", "exposure_policy": "open"}],
    }
    return api.ProtocolDraft(
        schema_version=1, design="Scale and describe frozen fixture groups.",
        analysis_plan="Report each group mean; recompute it from raw values.",
        metric=METRIC, stopping_rule=rule, statistical_design=design,
        roster=list(range(len(groups))), roster_semantics="frozen_unit_index",
        sample_size_scope="total", run_limit=2 * len(groups), replication_tolerance=0.0,
        seen_data=[])


def compile_execution(request):
    validate_parameters(request.parameters)
    return api.ExecutionPlan(primary_program=PRIMARY, reanalysis_program=REANALYSIS,
                             input=_input(request), outputs=dict(OUTPUTS), wall_seconds=30,
                             max_output_bytes=65536, required_capabilities=[],
                             execution_profile="trusted_local_python_v1",
                             environment_requirements=api.environment_requirements())


def validate_protocol(context):
    protocol = context.protocol
    _require(protocol["metric"] == METRIC and protocol["data"] == context.execution_plan["input"]["sha256"],
             "fixture protocol differs from its compiled execution plan")


def _raw(cas, key, unit):
    raw = cas.json(key, "fixture raw data")
    _require(set(raw) == {"schema_version", "unit", "values"} and raw["unit"] == unit
             and raw["values"] and all(type(value) is int for value in raw["values"]),
             "fixture raw data are malformed")
    return raw


def _metric(cas, key):
    metrics = cas.json(key, "fixture metrics")
    _require(set(metrics) == {METRIC} and type(metrics[METRIC]) in (int, float)
             and math.isfinite(metrics[METRIC]), "fixture metrics are malformed")
    return float(metrics[METRIC])


def validate_outputs(context, cas):
    checks = []
    for row in context.slots:
        findings = []
        try:
            _raw(cas, row["outputs"]["raw_data"], row["roster_unit"])
            _metric(cas, row["outputs"]["metrics"])
            if row["mode"] == "independent_reanalysis":
                primary = context.slot(f"primary:{row['roster_unit']}")
                _require(primary["outputs"]["raw_data"] == row["outputs"]["raw_data"],
                         "reanalysis did not keep the primary raw data")
        except ValueError as exc:
            findings.append(str(exc))
        checks.append(api.OutputCheck(
            schema_version=1, slot=row["slot"], roster_unit=row["roster_unit"], mode=row["mode"],
            result=row["result"], raw_data=row["outputs"]["raw_data"],
            metrics=row["outputs"]["metrics"], status="failed" if findings else "passed",
            findings=findings))
    return checks


def recompute_metrics(context, cas):
    rows = []
    for unit in context.draft.roster:
        primary = context.slot(f"primary:{unit}")
        reanalysis = context.slot(f"reanalysis:{unit}")
        raw = _raw(cas, primary["outputs"]["raw_data"], unit)
        value = math.fsum(raw["values"]) / len(raw["values"])
        recorded = _metric(cas, primary["outputs"]["metrics"])
        repeated = _metric(cas, reanalysis["outputs"]["metrics"])
        rows.append(api.Recomputation(
            schema_version=1, roster_unit=unit, metric=METRIC, recomputed=value,
            primary_recorded=recorded, reanalysis_recorded=repeated,
            primary_absolute_difference=abs(value - recorded),
            reanalysis_absolute_difference=abs(value - repeated),
            tolerance=context.numeric_tolerance))
    return rows


def analyse(context, cas, checks, recomputations):
    design = context.draft.typed_design()
    values = {row.roster_unit: row.recomputed for row in recomputations}
    analysed = sum(len(_raw(cas, context.slot(f"primary:{unit}")["outputs"]["raw_data"], unit)["values"])
                   for unit in context.draft.roster)
    report = api.StatisticalReport.from_dict({
        "schema_version": 1, "protocol_hash": context.protocol_hash,
        "estimand": api.supplied({"protocol_hash": context.protocol_hash, "text": design.estimand}),
        "estimator": api.supplied({"name": "arithmetic_mean", "hook": "recompute_metrics",
                                   "description": "Mean of the scaled values of one group."}),
        "point_estimate": api.supplied({"metric": METRIC, "unit": "points", "value": None,
                                        "by_roster_unit": [{"roster_unit": unit, "value": value}
                                                           for unit, value in sorted(values.items())]}),
        "uncertainty": api.not_applicable("Preregistered uncertainty.method is not_applicable."),
        "confidence_interval": api.not_applicable("Descriptive design without uncertainty."),
        "effect_size": api.not_applicable("Descriptive design without a comparison."),
        "assumptions": api.not_supplied("The fixture checks no distributional assumptions."),
        "sample_size": api.supplied({"experimental_unit": design.experimental_unit,
                                     "unit_scope": "total", "planned": design.sample_size,
                                     "planned_total": design.sample_size,
                                     "analysed": len(context.draft.roster), "exclusions": [],
                                     "missing_slots": []}),
        "multiple_testing": api.not_applicable("No tests were preregistered."),
        "stopping_rule": api.supplied({"rule_sha256": hashlib.sha256(
            design.stopping_rule.rule.encode("utf-8")).hexdigest(), "roster_complete": True,
            "interim_looks": 0}),
        "sensitivity_analysis": api.supplied([]),
        "deviations": api.supplied([]),
    })
    return api.AnalysisReport(
        pack_id=PACK_ID, pack_version=PACK_VERSION, protocol_hash=context.protocol_hash,
        statement=f"Fixture group means were {values}; they describe invented values only.",
        limitations=["Invented fixture values; not evidence about anything."],
        outcome=context.parameters["outcome"], inference_mode=context.parameters["inference_mode"],
        details={"analysed_values": analysed}, statistical_report=report)
