"""Test-only DomainPack on execution profile v2: describe frozen integer groups by their means.

This fixture exercises ``ExecutionPlanV2``: multi-file source trees and a uv
project whose only dependency is a local wheel embedded below, installed
offline. Its values are invented, its programs are trivial and nothing it
reports is scientific evidence.
"""

import hashlib
import json
import math

from episteme.domains import api


PACK_ID = "locked_fixture_v2"
PACK_VERSION = "1"
METRIC = "unit_mean"
OUTPUTS = {"raw_data": "raw.json", "metrics": "metrics.json"}
CAPABILITIES = ["locked_environment", "workspace_outside_store"]
VARIABLES = {"PYTHONHASHSEED": "0"}

MANIFEST = {
    "contract_version": 1, "pack_id": PACK_ID, "pack_version": PACK_VERSION,
    "description": "Test-only fixture on profile v2: means of frozen integer groups.",
    "roster_semantics": ["frozen_unit_index"],
    "metrics": [{"name": METRIC, "unit": "points", "description": "Mean of one frozen group"}],
    "outputs": {"raw_data": {"path": "raw.json", "schema_id": "locked_fixture_v2/raw-v1"},
                "metrics": {"path": "metrics.json", "schema_id": "locked_fixture_v2/metrics-v1"}},
    "numeric_tolerance": 1e-12, "hidden_inputs": [],
    "execution_profiles": ["uv_locked_python_v2"],
    "statistical_capabilities": ["estimand", "estimator", "point_estimate", "sample_size",
                                 "stopping_rule", "sensitivity_analysis", "deviations"],
    "interpretation_cautions": ["Invented fixture values; not evidence about anything."],
    "capture": False,
}

PRIMARY = {
    "main.py": b'''import json, pathlib, sys
import episteme_pack_dep
from fixture_lib.scaling import scaled
if len(sys.argv) != 4 or sys.argv[2] != "--seed":
    raise SystemExit("expected INPUT --seed N")
unit = int(sys.argv[3])
data = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
values = scaled(data["groups"][unit], data["scale"])
raw = {"schema_version": 1, "unit": unit, "values": values}
pathlib.Path("raw.json").write_bytes(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode())
pathlib.Path("metrics.json").write_bytes(json.dumps({"unit_mean": episteme_pack_dep.mean(values)}).encode())
''',
    "fixture_lib/__init__.py": b"",
    "fixture_lib/scaling.py": b"def scaled(values, scale):\n    return [value * scale for value in values]\n",
}

REANALYSIS = {
    "reanalyse.py": b'''import json, pathlib, sys
from recompute import mean
if len(sys.argv) != 4 or sys.argv[2] != "--seed":
    raise SystemExit("expected INPUT --seed N")
source = pathlib.Path(sys.argv[1]).read_bytes()
raw = json.loads(source)
if raw["unit"] != int(sys.argv[3]):
    raise SystemExit("raw data belong to another unit")
pathlib.Path("raw.json").write_bytes(source)
pathlib.Path("metrics.json").write_bytes(json.dumps({"unit_mean": mean(raw["values"])}).encode())
''',
    "recompute.py": b"import math\n\n\ndef mean(values):\n    return math.fsum(values) / len(values)\n",
}

PYPROJECT = (
    b'[project]\r\n'
    b'name = "locked-fixture-programs"\r\n'
    b'version = "0"\r\n'
    b'requires-python = ">=3.11"\r\n'
    b'dependencies = ["episteme-pack-dep==0.1.0"]\r\n'
    b'\r\n'
    b'[tool.uv]\r\n'
    b'package = false\r\n'
    b'no-index = true\r\n'
    b'find-links = ["wheels"]\r\n'
)

UV_LOCK = (
    b'version = 1\n'
    b'revision = 3\n'
    b'requires-python = ">=3.11"\n'
    b'\n'
    b'[[package]]\n'
    b'name = "episteme-pack-dep"\n'
    b'version = "0.1.0"\n'
    b'source = { registry = "wheels" }\n'
    b'wheels = [\n'
    b'    { path = "episteme_pack_dep-0.1.0-py3-none-any.whl" },\n'
    b']\n'
    b'\n'
    b'[[package]]\n'
    b'name = "locked-fixture-programs"\n'
    b'version = "0"\n'
    b'source = { virtual = "." }\n'
    b'dependencies = [\n'
    b'    { name = "episteme-pack-dep" },\n'
    b']\n'
    b'\n'
    b'[package.metadata]\n'
    b'requires-dist = [{ name = "episteme-pack-dep", specifier = "==0.1.0" }]\n'
)

WHEEL_PATH = "wheels/episteme_pack_dep-0.1.0-py3-none-any.whl"
# A pure-Python wheel built with zipfile (stored, fixed timestamps) by
# tests/locked_support.build_wheel; uv.lock references it by relative path.
WHEEL = (
    b'PK\x03\x04\x14\x00\x00\x00\x00\x00\x00\x00!P\x96\x18\xb6P7\x00\x00\x007\x00'
    b'\x00\x00\x1d\x00\x00\x00episteme_pack_dep/'
    b'__init__.pydef mean(valu'
    b'es):\n    return sum(valu'
    b'es) / len(values)\nPK\x03\x04\x14\x00'
    b'\x00\x00\x00\x00\x00\x00!P\xcf\x96\x12\xc7=\x00\x00\x00=\x00\x00\x00*\x00\x00\x00'
    b'episteme_pack_dep-0.1.0.'
    b'dist-info/METADATAMetada'
    b'ta-Version: 2.1\nName: ep'
    b'isteme_pack_dep\nVersion:'
    b' 0.1.0\nPK\x03\x04\x14\x00\x00\x00\x00\x00\x00\x00!Px\xa3\xda'
    b"\xbaT\x00\x00\x00T\x00\x00\x00'\x00\x00\x00episteme_pa"
    b'ck_dep-0.1.0.dist-info/W'
    b'HEELWheel-Version: 1.0\nG'
    b'enerator: episteme-test\n'
    b'Root-Is-Purelib: true\nTa'
    b'g: py3-none-any\nPK\x03\x04\x14\x00\x00\x00'
    b'\x00\x00\x00\x00!P%$\x0c\xa9>\x00\x00\x00>\x00\x00\x002\x00\x00\x00ep'
    b'isteme_pack_dep-0.1.0.di'
    b'st-info/entry_points.txt'
    b'[console_scripts]\nepiste'
    b'me-fixture = episteme_pa'
    b'ck_dep:answer\nPK\x03\x04\x14\x00\x00\x00\x00\x00'
    b'\x00\x00!P\x8f\x82p\xd3\xa7\x01\x00\x00\xa7\x01\x00\x00(\x00\x00\x00epis'
    b'teme_pack_dep-0.1.0.dist'
    b'-info/RECORDepisteme_pac'
    b'k_dep/__init__.py,sha256'
    b'=C7BoINXb1K7roJSkNXRHcqK'
    b'nb1HXlqbyZoTrJZn4CnM,55\n'
    b'episteme_pack_dep-0.1.0.'
    b'dist-info/METADATA,sha25'
    b'6=qM5bgHHsuKOfrmsIN1p7Iz'
    b'TDq2ZNBwVmQ-RmSLvmxGI,61'
    b'\nepisteme_pack_dep-0.1.0'
    b'.dist-info/WHEEL,sha256='
    b'BHVwbVHoovwH3I-vLqKUcXFd'
    b'hd0x8JhpBzt1e0-ERr0,84\ne'
    b'pisteme_pack_dep-0.1.0.d'
    b'ist-info/entry_points.tx'
    b't,sha256=KpxNerhcX4flRxW'
    b'2sh3yJL5ANDKnjCV130seVvP'
    b'hQbU,62\nepisteme_pack_de'
    b'p-0.1.0.dist-info/RECORD'
    b',,\nPK\x01\x02\x14\x00\x14\x00\x00\x00\x00\x00\x00\x00!P\x96\x18\xb6P7'
    b'\x00\x00\x007\x00\x00\x00\x1d\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x80\x01\x00\x00\x00'
    b'\x00episteme_pack_dep/__ini'
    b't__.pyPK\x01\x02\x14\x00\x14\x00\x00\x00\x00\x00\x00\x00!P\xcf\x96'
    b'\x12\xc7=\x00\x00\x00=\x00\x00\x00*\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x80\x01'
    b'r\x00\x00\x00episteme_pack_dep-0.'
    b'1.0.dist-info/METADATAPK'
    b'\x01\x02\x14\x00\x14\x00\x00\x00\x00\x00\x00\x00!Px\xa3\xda\xbaT\x00\x00\x00T\x00'
    b"\x00\x00'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x80\x01\xf7\x00\x00\x00epis"
    b'teme_pack_dep-0.1.0.dist'
    b'-info/WHEELPK\x01\x02\x14\x00\x14\x00\x00\x00\x00\x00\x00'
    b'\x00!P%$\x0c\xa9>\x00\x00\x00>\x00\x00\x002\x00\x00\x00\x00\x00\x00\x00\x00'
    b'\x00\x00\x00\x80\x01\x90\x01\x00\x00episteme_pack_d'
    b'ep-0.1.0.dist-info/entry'
    b'_points.txtPK\x01\x02\x14\x00\x14\x00\x00\x00\x00\x00\x00'
    b'\x00!P\x8f\x82p\xd3\xa7\x01\x00\x00\xa7\x01\x00\x00(\x00\x00\x00\x00\x00\x00\x00\x00'
    b'\x00\x00\x00\x80\x01\x1e\x02\x00\x00episteme_pack_d'
    b'ep-0.1.0.dist-info/RECOR'
    b'DPK\x05\x06\x00\x00\x00\x00\x05\x00\x05\x00\xae\x01\x00\x00\x0b\x04\x00\x00\x00\x00'
)


def project():
    return {"pyproject.toml": PYPROJECT, "uv.lock": UV_LOCK, WHEEL_PATH: WHEEL}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def describe():
    return api.ParameterCatalog(
        schema_version=1, pack_id=PACK_ID, pack_version=PACK_VERSION,
        description="Scale frozen integer groups and describe each group mean (profile v2).",
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
        schema_version=1, design="Scale and describe frozen fixture groups (profile v2).",
        analysis_plan="Report each group mean; recompute it from raw values.",
        metric=METRIC, stopping_rule=rule, statistical_design=design,
        roster=list(range(len(groups))), roster_semantics="frozen_unit_index",
        sample_size_scope="total", run_limit=2 * len(groups), replication_tolerance=0.0,
        seen_data=[])


def compile_execution(request):
    validate_parameters(request.parameters)
    return api.ExecutionPlanV2(
        primary_program=api.SourceTree(entry_point="main.py", files=PRIMARY),
        reanalysis_program=api.SourceTree(entry_point="reanalyse.py", files=REANALYSIS),
        input=_input(request), outputs=dict(OUTPUTS), wall_seconds=60, max_output_bytes=65536,
        required_capabilities=list(CAPABILITIES), project=project(), variables=dict(VARIABLES))


def validate_protocol(context):
    protocol = context.protocol
    _require(protocol["metric"] == METRIC and protocol["data"] == context.execution_plan["input"]["sha256"]
             and protocol["implementation"] == context.execution_plan["primary_program"]["sha256"],
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
