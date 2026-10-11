"""DomainPack boundary for a future Risen Universalis study. Nothing here is that study.

No description of Risen Universalis was in the repository. This pack does not
invent a dataset, a population, or a result. The smoke input is two declared
integers. ``paired_difference`` and ``sum_of_scores`` are competing experiment
parameters frozen before any run. The kernel, not this pack, ceilings claim
strength. ``scientific_validity`` stays not_assessed.

``uv_locked_python_v2`` is the only execution profile. It is environment
hygiene, not a sandbox: the payload still runs as the same OS user.
``trusted_local_python_v1`` is rejected. A hosted ``model_id``, if a later
version records one, is an observed execution. The same id can later mean a
different backend, so exact reproduction is not promised. This smoke writes
those observation fields as null.
"""

import hashlib
import json
import math

from episteme.domains import api


PACK_ID = "risen_universalis_v1"
PACK_VERSION = "1"
PROFILE = "uv_locked_python_v2"
METRIC = "score_gap"
SECONDARY = "score_a"
FIXTURE_ID = "local-smoke-rows-v1"
CONTRASTS = ("paired_difference", "sum_of_scores")
ROWS = ({"score_a": 3, "score_b": 1}, {"score_a": 0, "score_b": 2})
OUTPUTS = {"raw_data": "raw.json", "metrics": "metrics.json"}
CAPABILITIES = ["locked_environment", "workspace_outside_store"]
VARIABLES = {"PYTHONHASHSEED": "0"}
NUMERIC_TOLERANCE = 0.0
NOTE = ("No GPU and no hosted API were used. A hosted model_id, if a later run records one, "
        "is an observed execution and not an exact-reproduction promise: the same model_id "
        "can later mean a different backend.")
DEFINITIONS = {
    "exploratory_result": ("An exploratory result describes the declared discovery split. "
                           "It is not confirmation."),
    "confirmatory_result": ("A confirmatory result would require a preregistered unseen holdout "
                            "or new data, a complete report, and the kernel ceiling. This smoke "
                            "does not meet that bar, so confirmatory strength stays unavailable."),
    "independent_reanalysis": ("Independent reanalysis is a second implementation, with a different "
                               "source digest, of the same estimand on the same raw bytes. It is not "
                               "a same-code replay, not a fresh-seed repetition, and not new-data replication."),
    "replication": ("Replication is a new sample or new data under a preregistered protocol. "
                    "A same-code rerun is reproduce, not replication, and is not evidence."),
}
OBSERVED_FIELDS = (
    "kind", "reproduction_promise", "model_revision", "tokenizer_revision", "cuda_runtime",
    "nvidia_driver", "gpu_model", "dtype", "quantization", "inference_engine",
    "inference_engine_version", "sampling", "prompt_digest", "seed", "context_length",
    "provider", "model_id", "raw_request", "raw_response", "usage", "note",
)
NULL_OBSERVATIONS = (
    "model_revision", "tokenizer_revision", "cuda_runtime", "nvidia_driver", "gpu_model", "dtype",
    "quantization", "inference_engine", "inference_engine_version", "sampling", "prompt_digest",
    "seed", "context_length", "provider", "model_id", "raw_request", "raw_response", "usage",
)

MANIFEST = {
    "contract_version": 1, "pack_id": PACK_ID, "pack_version": PACK_VERSION,
    "description": ("Mechanical adapter for a not-yet-run Risen Universalis question. "
                    "The smoke computes a paired gap on two invented rows."),
    "roster_semantics": ["frozen_unit_index"],
    "metrics": [
        {"name": METRIC, "unit": "score points",
         "description": "Declared contrast of score_a and score_b on one frozen fixture row"},
        {"name": SECONDARY, "unit": "score points",
         "description": "score_a of the same row; not in the testing family"},
    ],
    "outputs": {"raw_data": {"path": "raw.json", "schema_id": "risen_universalis_v1/raw-v1"},
                "metrics": {"path": "metrics.json", "schema_id": "risen_universalis_v1/metrics-v1"}},
    "numeric_tolerance": NUMERIC_TOLERANCE, "hidden_inputs": [],
    "execution_profiles": [PROFILE],
    "statistical_capabilities": ["estimand", "estimator", "point_estimate", "uncertainty",
                                 "confidence_interval", "effect_size", "assumptions", "sample_size",
                                 "multiple_testing", "stopping_rule", "sensitivity_analysis",
                                 "deviations"],
    "interpretation_cautions": [
        "These rows are an invented smoke fixture. They are not a sample and not a real study.",
        "The pack cannot raise claim strength. The kernel computes the ceiling.",
        "uv_locked_python_v2 is not a sandbox. The same OS user can still read the store.",
        "Role labels do not prove actor, context, or OS isolation.",
        DEFINITIONS["exploratory_result"],
        DEFINITIONS["confirmatory_result"],
        DEFINITIONS["independent_reanalysis"],
        DEFINITIONS["replication"],
        NOTE,
    ],
    "capture": False,
}

PRIMARY = {
    "main.py": b'''import json, pathlib, sys
from scoring import score
if len(sys.argv) != 4 or sys.argv[2] != "--seed":
    raise SystemExit("expected INPUT --seed N")
unit = int(sys.argv[3])
data = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
row = data["rows"][unit]
gap = float(score(data["contrast"], row["score_a"], row["score_b"]))
raw = {"schema_version": 1, "unit": unit, "contrast": data["contrast"],
       "score_a": row["score_a"], "score_b": row["score_b"],
       "observed_execution": {"kind": "local_deterministic", "reproduction_promise": False,
                              "model_revision": None, "tokenizer_revision": None, "cuda_runtime": None,
                              "nvidia_driver": None, "gpu_model": None, "dtype": None, "quantization": None,
                              "inference_engine": None, "inference_engine_version": None, "sampling": None,
                              "prompt_digest": None, "seed": None, "context_length": None, "provider": None,
                              "model_id": None, "raw_request": None, "raw_response": None, "usage": None,
                              "note": data["observed_execution_note"]}}
encoded = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
pathlib.Path("raw.json").write_bytes(encoded)
pathlib.Path("metrics.json").write_bytes(json.dumps(
    {"score_gap": gap, "score_a": float(row["score_a"])}, sort_keys=True, separators=(",", ":")).encode())
''',
    "scoring.py": b'''def score(contrast, score_a, score_b):
    if contrast == "paired_difference":
        return score_a - score_b
    if contrast == "sum_of_scores":
        return score_a + score_b
    raise SystemExit("unknown contrast")
''',
}

REANALYSIS = {
    "reanalyse.py": b'''import json, pathlib, sys
from alternate import score
if len(sys.argv) != 4 or sys.argv[2] != "--seed":
    raise SystemExit("expected INPUT --seed N")
source = pathlib.Path(sys.argv[1]).read_bytes()
raw = json.loads(source)
if raw["unit"] != int(sys.argv[3]):
    raise SystemExit("raw data belong to another unit")
gap = float(score(raw["contrast"], raw["score_a"], raw["score_b"]))
pathlib.Path("raw.json").write_bytes(source)
pathlib.Path("metrics.json").write_bytes(json.dumps(
    {"score_gap": gap, "score_a": float(raw["score_a"])}, sort_keys=True, separators=(",", ":")).encode())
''',
    "alternate.py": b'''import math

def score(contrast, score_a, score_b):
    if contrast == "paired_difference":
        return math.fsum((score_a, -score_b))
    if contrast == "sum_of_scores":
        return math.fsum((score_a, score_b))
    raise SystemExit("unknown contrast")
''',
}

PYPROJECT = (
    b'[project]\n'
    b'name = "risen-universalis-smoke"\n'
    b'version = "0"\n'
    b'requires-python = ">=3.11"\n'
    b'dependencies = []\n'
    b'\n'
    b'[tool.uv]\n'
    b'package = false\n'
)

UV_LOCK = (
    b'version = 1\n'
    b'revision = 3\n'
    b'requires-python = ">=3.11"\n'
    b'\n'
    b'[[package]]\n'
    b'name = "risen-universalis-smoke"\n'
    b'version = "0"\n'
    b'source = { virtual = "." }\n'
)


def project():
    return {"pyproject.toml": PYPROJECT, "uv.lock": UV_LOCK}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _contrast(parameters):
    _require(type(parameters) is dict, "parameters must be an object")
    if parameters.get("execution_profile") not in (None, PROFILE):
        raise ValueError("risen_universalis_v1 accepts only uv_locked_python_v2")
    _require(set(parameters) == {"contrast"} and parameters["contrast"] in CONTRASTS,
             "parameters must be exactly contrast=paired_difference or sum_of_scores; "
             "trusted_local_python_v1 is not an experiment parameter")
    return parameters["contrast"]


def _rows(host):
    _require(type(host) is dict and set(host) == {"fixture_id"} and host["fixture_id"] == FIXTURE_ID,
             "host inputs must name the declared smoke fixture and nothing else")
    return [dict(row) for row in ROWS]


def _input(contrast, rows):
    return _canonical({"fixture_id": FIXTURE_ID, "contrast": contrast, "rows": rows,
                       "observed_execution_note": NOTE})


def _estimand(contrast):
    if contrast == "paired_difference":
        return ("Mean of score_a minus score_b over the two declared smoke rows. "
                "This is fixture arithmetic, not an estimand about a population.")
    return ("Mean of score_a plus score_b over the two declared smoke rows. "
            "This competing contrast does not answer the paired-gap explanations.")


def _stopping(contrast):
    return (f"Compute {contrast} once for each of the {len(ROWS)} declared rows. "
            "No interim looks and no extra rows.")


def describe():
    return api.ParameterCatalog(
        schema_version=1, pack_id=PACK_ID, pack_version=PACK_VERSION,
        description="Choose a frozen contrast on the declared smoke rows. Profile v2 only.",
        parameters_schema={"type": "object", "additionalProperties": False, "required": ["contrast"],
                           "properties": {"contrast": {"enum": list(CONTRASTS)}}},
        metric=METRIC, outputs=dict(OUTPUTS),
        limitations=[
            "Invented rows only. No population, no external dataset, no scientific approval.",
            "Only uv_locked_python_v2 is accepted.",
        ])


def validate_parameters(parameters):
    _contrast(parameters)


def _compiled(request):
    contrast = _contrast(api.thaw(request.parameters))
    if request.capture is not None:
        raise ValueError("risen universalis smoke takes no captured source")
    rows = _rows(api.thaw(request.host_inputs))
    return contrast, rows, _input(contrast, rows)


def compile_protocol(request):
    contrast, rows, data = _compiled(request)
    rule = _stopping(contrast)
    design = {
        "schema_version": 1, "mode": "exploratory", "experimental_unit": "frozen smoke row",
        "estimand": _estimand(contrast),
        "primary_metric": {"name": METRIC, "unit": "score points"},
        "secondary_metrics": [{"name": SECONDARY, "unit": "score points"}],
        "sample_size": len(rows),
        "sample_size_rationale": "Every declared smoke row, and no other row.",
        "uncertainty": {"method": "not_applicable", "resampling_unit": None,
                        "rationale": "These rows are the whole fixture. They are not a sample."},
        "exclusions": [],
        "stopping_rule": {"kind": "fixed_sample", "rule": rule},
        "multiple_testing": {"family": [METRIC], "correction": "none",
                             "rationale": "One primary contrast. score_a is not in the family."},
        "data_splits": [{"id": "smoke_discovery", "digest": hashlib.sha256(data).hexdigest(),
                         "role": "discovery", "exposure_policy": "open"}],
    }
    definitions = " ".join(DEFINITIONS.values())
    return api.ProtocolDraft(
        schema_version=1,
        design=("Exploratory smoke of a frozen contrast on invented rows. " + definitions + " " + NOTE),
        analysis_plan=("Recompute the contrast from raw scores. The reanalysis program is a different "
                       "implementation on the same bytes. Do not treat a same-code rerun as replication."),
        metric=METRIC, stopping_rule=rule, statistical_design=design, roster=list(range(len(rows))),
        roster_semantics="frozen_unit_index", sample_size_scope="total", run_limit=2 * len(rows),
        replication_tolerance=NUMERIC_TOLERANCE, seen_data=[])


def compile_execution(request):
    contrast, rows, data = _compiled(request)
    _require(PROFILE == "uv_locked_python_v2", "risen_universalis_v1 accepts only uv_locked_python_v2")
    return api.ExecutionPlanV2(
        primary_program=api.SourceTree(entry_point="main.py", files=PRIMARY),
        reanalysis_program=api.SourceTree(entry_point="reanalyse.py", files=REANALYSIS),
        input=data, outputs=dict(OUTPUTS), wall_seconds=60, max_output_bytes=65536,
        required_capabilities=list(CAPABILITIES), project=project(), variables=dict(VARIABLES))


def validate_protocol(context):
    protocol = context.protocol
    plan = context.execution_plan
    _require(plan.get("schema_version") == 2 and plan.get("execution_profile") == PROFILE,
             "risen_universalis_v1 accepts only uv_locked_python_v2")
    _require(protocol["metric"] == METRIC and protocol.get("protocol_mode") == "exploratory"
             and protocol["data"] == plan["input"]["sha256"]
             and protocol["implementation"] == plan["primary_program"]["sha256"]
             and list(protocol["seeds"]) == list(context.draft.roster),
             "risen protocol differs from its exploratory v2 plan")


def _raw(cas, key, unit, contrast):
    raw = cas.json(key, "smoke raw data")
    observed = raw.get("observed_execution")
    _require(set(raw) == {"schema_version", "unit", "contrast", "score_a", "score_b",
                          "observed_execution"}
             and raw["schema_version"] == 1 and raw["unit"] == unit and raw["contrast"] == contrast
             and type(raw["score_a"]) is int and type(raw["score_b"]) is int
             and type(observed) is dict and set(observed) == set(OBSERVED_FIELDS)
             and observed["kind"] == "local_deterministic" and observed["reproduction_promise"] is False
             and observed["note"] == NOTE
             and all(observed[name] is None for name in NULL_OBSERVATIONS),
             "smoke raw data are malformed or claim an observation this run did not make")
    return raw


def _metrics(cas, key):
    metrics = cas.json(key, "smoke metrics")
    _require(set(metrics) == {METRIC, SECONDARY}
             and all(type(metrics[name]) in (int, float) and math.isfinite(metrics[name])
                     for name in (METRIC, SECONDARY)),
             "smoke metrics are malformed")
    return float(metrics[METRIC]), float(metrics[SECONDARY])


def _unit_gap(contrast, score_a, score_b):
    if contrast == "paired_difference":
        return float(math.fsum((score_a, -score_b)))
    return float(math.fsum((score_a, score_b)))


def validate_outputs(context, cas):
    contrast = _contrast(api.thaw(context.parameters))
    checks = []
    for row in context.slots:
        findings = []
        try:
            raw = _raw(cas, row["outputs"]["raw_data"], row["roster_unit"], contrast)
            gap, score_a = _metrics(cas, row["outputs"]["metrics"])
            _require(score_a == float(raw["score_a"]), "recorded score_a differs from the raw row")
            _require(gap == _unit_gap(contrast, raw["score_a"], raw["score_b"]),
                     "recorded score_gap differs from the raw scores")
            if row["mode"] == "independent_reanalysis":
                primary = context.slot(f"primary:{row['roster_unit']}")
                _require(primary["outputs"]["raw_data"] == row["outputs"]["raw_data"],
                         "reanalysis did not keep the primary raw bytes")
        except ValueError as exc:
            findings.append(str(exc))
        checks.append(api.OutputCheck(
            schema_version=1, slot=row["slot"], roster_unit=row["roster_unit"], mode=row["mode"],
            result=row["result"], raw_data=row["outputs"]["raw_data"],
            metrics=row["outputs"]["metrics"], status="failed" if findings else "passed",
            findings=findings))
    return checks


def recompute_metrics(context, cas):
    contrast = _contrast(api.thaw(context.parameters))
    rows = []
    for unit in context.draft.roster:
        primary = context.slot(f"primary:{unit}")
        reanalysis = context.slot(f"reanalysis:{unit}")
        raw = _raw(cas, primary["outputs"]["raw_data"], unit, contrast)
        _require(reanalysis["outputs"]["raw_data"] == primary["outputs"]["raw_data"],
                 "reanalysis did not keep the primary raw bytes")
        value = _unit_gap(contrast, raw["score_a"], raw["score_b"])
        recorded, _ = _metrics(cas, primary["outputs"]["metrics"])
        repeated, _ = _metrics(cas, reanalysis["outputs"]["metrics"])
        rows.append(api.Recomputation(
            schema_version=1, roster_unit=unit, metric=METRIC, recomputed=value,
            primary_recorded=recorded, reanalysis_recorded=repeated,
            primary_absolute_difference=abs(value - recorded),
            reanalysis_absolute_difference=abs(value - repeated),
            tolerance=context.numeric_tolerance))
    return rows


def analyse(context, cas, checks, recomputations):
    failed = [check for check in checks if check.status != "passed"]
    if failed:
        raise ValueError("smoke outputs failed pack checks")
    contrast = _contrast(api.thaw(context.parameters))
    design = context.draft.typed_design()
    values = {row.roster_unit: row.recomputed for row in recomputations}
    _require(sorted(values) == sorted(context.draft.roster), "recomputations do not cover the rows")
    mean = float(math.fsum(values[unit] for unit in context.draft.roster) / len(values))
    rendered = ", ".join(f"row {unit}: {values[unit]:.12g}" for unit in context.draft.roster)
    report = api.StatisticalReport.from_dict({
        "schema_version": 1, "protocol_hash": context.protocol_hash,
        "estimand": api.supplied({"protocol_hash": context.protocol_hash, "text": design.estimand}),
        "estimator": api.supplied({"name": contrast, "hook": "recompute_metrics",
                                   "description": "Arithmetic contrast of the two frozen scores on one row."}),
        "point_estimate": api.supplied({
            "metric": METRIC, "unit": "score points", "value": mean,
            "by_roster_unit": [{"roster_unit": unit, "value": values[unit]}
                               for unit in context.draft.roster]}),
        "uncertainty": api.not_applicable("Preregistered uncertainty.method is not_applicable."),
        "confidence_interval": api.not_applicable("The fixture is not a sample, so no interval is declared."),
        "effect_size": api.not_supplied("No population effect was preregistered. The raw gap is not an effect size."),
        "assumptions": api.supplied([
            {"assumption": "The rows are the entire declared fixture, not draws from a population.",
             "status": "unchecked", "reference": None},
            {"assumption": "score_a and score_b are the frozen input numbers, not measurements.",
             "status": "unchecked", "reference": None},
        ]),
        "sample_size": api.supplied({
            "experimental_unit": design.experimental_unit, "unit_scope": "total",
            "planned": design.sample_size, "planned_total": design.sample_size,
            "analysed": len(context.draft.roster), "exclusions": [], "missing_slots": []}),
        "multiple_testing": api.supplied({
            "family": [METRIC], "claim_position": "1", "correction": "none", "adjusted_result": None}),
        "stopping_rule": api.supplied({
            "rule_sha256": hashlib.sha256(design.stopping_rule.rule.encode("utf-8")).hexdigest(),
            "roster_complete": True, "interim_looks": 0}),
        "sensitivity_analysis": api.supplied([]),
        "deviations": api.supplied([]),
    })
    return api.AnalysisReport(
        pack_id=PACK_ID, pack_version=PACK_VERSION, protocol_hash=context.protocol_hash,
        statement=(f"Smoke {contrast} on the declared rows was {rendered}; mean {mean:.12g}. "
                   "This is fixture arithmetic. It is exploratory and inconclusive. "
                   "Confirmatory strength is not available, and this is not a scientific result."),
        limitations=[
            "Invented smoke rows, not a sample and not an external study.",
            DEFINITIONS["exploratory_result"],
            DEFINITIONS["confirmatory_result"],
            DEFINITIONS["independent_reanalysis"],
            DEFINITIONS["replication"],
            "uv_locked_python_v2 does not stop the same OS user from reading the store.",
            NOTE,
        ],
        outcome="inconclusive", inference_mode="exploratory",
        details={"contrast": contrast, "mean_score_gap": mean, "by_row": [
            {"roster_unit": unit, "score_gap": values[unit]} for unit in context.draft.roster]},
        statistical_report=report)
