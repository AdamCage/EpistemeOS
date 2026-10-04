"""Confirmatory comparison of two classifiers on a frozen tabular holdout.

Training rows and holdout rows are separate captured files. ``compile_protocol``
records their digests and the holdout row count. It does not fit a model and
does not put holdout label values into the estimand, the step count, or the
decision rule. Profile v1 can pass only one program file and one ``input.dat``,
so ``compile_execution`` quotes both CSV texts into that input. The runner,
which starts only after the protocol event exists, is what first uses holdout
labels to score rows.

That is a property of these hooks, not an OS or process boundary. The capture
bundle in the planner process contains the label bytes during compilation.
``seen_data`` lists the training file only. The confirmatory split is the
holdout file, not the combined program input.
"""

import hashlib
import json

from episteme.domains import api

from . import analysis, programs
# Imported before the hook named capture replaces this package attribute.
from . import capture as reader
from .model import (ALPHA, LEARNING_RATE, METRIC, STEPS, THRESHOLD, TIE_CLASS, Z_975, parse_table)


PACK_ID = reader.PACK_ID
PACK_VERSION = reader.PACK_VERSION
NUMERIC_TOLERANCE = 0
WALL_SECONDS = 60
MAX_OUTPUT_BYTES = 1024 * 1024
ESTIMAND = ("On the frozen holdout rows, accuracy of fixed-step logistic regression "
            "minus accuracy of the majority class of the training labels.")
STOPPING = ("Fixed sample: score every captured holdout row once, in one primary run "
            "and one same-data reanalysis. No interim look and no rows added after the protocol.")
FAMILY = "logistic_minus_majority_holdout_accuracy"
OUTPUTS = {"raw_data": "raw.json", "metrics": "metrics.json"}

CAUTIONS = [
    "Training and holdout are separate captured files. The program input quotes both because profile v1 has one input file.",
    "Holdout labels are not used to choose the model, the baseline, the threshold, or the decision rule.",
    "A confirmatory label from the kernel ceiling is not a population estimate and not a reviewer approval.",
    "The same-data reanalysis repeats one pack's arithmetic. It is not independent evidence.",
]


MANIFEST = {
    "contract_version": 1, "pack_id": PACK_ID, "pack_version": PACK_VERSION,
    "description": ("Confirmatory paired accuracy comparison of fixed-step logistic regression "
                    "and a training majority baseline on a frozen tabular holdout."),
    "roster_semantics": ["deterministic_single"],
    "metrics": [{"name": METRIC, "unit": "proportion",
                 "description": "Holdout accuracy of logistic regression minus holdout accuracy of the training majority class"}],
    "outputs": {"raw_data": {"path": "raw.json", "schema_id": "tabular_classification_v1/raw-v1"},
                "metrics": {"path": "metrics.json", "schema_id": "tabular_classification_v1/metrics-v1"}},
    "numeric_tolerance": NUMERIC_TOLERANCE, "hidden_inputs": [],
    "execution_profiles": ["trusted_local_python_v1"],
    "statistical_capabilities": ["estimand", "estimator", "point_estimate", "uncertainty",
                                 "confidence_interval", "effect_size", "assumptions", "sample_size",
                                 "multiple_testing", "stopping_rule", "sensitivity_analysis", "deviations"],
    "interpretation_cautions": CAUTIONS,
    "capture": True,
}

PARAMETERS_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {}}


def describe():
    return api.ParameterCatalog(
        schema_version=1, pack_id=PACK_ID, pack_version=PACK_VERSION,
        description=MANIFEST["description"], parameters_schema=PARAMETERS_SCHEMA, metric=METRIC,
        outputs=dict(OUTPUTS), limitations=list(analysis.LIMITATIONS))


def validate_parameters(parameters):
    if api.thaw(parameters) != {}:
        raise ValueError("tabular classification pack takes no parameters")


def proposal_schema():
    """The model has no free parameters. The comparison is the pack's, not the model's."""
    return {"schema_version": 1, "parameters_schema": PARAMETERS_SCHEMA}


def proposal_attempts(host_inputs, capture):
    """One primary run and one same-data reanalysis, fixed before any model text."""
    if api.thaw(host_inputs) != {}:
        raise ValueError("tabular classification pack takes no host inputs")
    if capture is None or set(capture.files) != {"train.csv", "holdout.csv"}:
        raise ValueError("tabular classification pack requires a captured train.csv and holdout.csv")
    return 2


def _files(request):
    if api.thaw(request.host_inputs) != {}:
        raise ValueError("tabular classification pack takes no host inputs")
    if request.capture is None:
        raise ValueError("tabular classification pack requires a captured train.csv and holdout.csv")
    files = request.capture.files
    if set(files) != {"train.csv", "holdout.csv"}:
        raise ValueError("capture must contain exactly train.csv and holdout.csv")
    return files["train.csv"], files["holdout.csv"]


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _execution_input(train, holdout):
    """Quote both CSV texts. The holdout text is not parsed here."""
    payload = {"holdout_csv": holdout.decode("utf-8"), "learning_rate": LEARNING_RATE,
               "steps": STEPS, "threshold": THRESHOLD, "tie_class": TIE_CLASS,
               "train_csv": train.decode("utf-8")}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def compile_protocol(request):
    train, holdout = _files(request)
    parse_table(train)  # Training labels are declared seen. Reject a malformed file now.
    holdout_rows = len(parse_table(holdout))  # Count and schema only; the label vector is discarded.
    train_digest, holdout_digest = _sha256(train), _sha256(holdout)
    design = {
        "schema_version": 1, "mode": "confirmatory", "experimental_unit": "holdout row",
        "estimand": ESTIMAND,
        "primary_metric": {"name": METRIC, "unit": "proportion"}, "secondary_metrics": [],
        "sample_size": holdout_rows,
        "sample_size_rationale": ("The sample size is the number of rows in the captured holdout "
                                  "file. It is not a power calculation for a population."),
        "uncertainty": {
            "method": "paired_wald", "resampling_unit": "holdout row",
            "rationale": ("Each holdout row is the experimental unit of the paired accuracy "
                          "difference. The name is that unit, not a bootstrap."),
        },
        "exclusions": [],
        "stopping_rule": {"kind": "fixed_sample", "rule": STOPPING},
        "multiple_testing": {
            "family": [FAMILY], "correction": "none",
            "rationale": "The family contains one preregistered comparison, so no multiplicity adjustment is applied.",
        },
        "data_splits": [
            {"id": "training", "digest": train_digest, "role": "discovery", "exposure_policy": "open"},
            {"id": "holdout", "digest": holdout_digest, "role": "confirmatory", "exposure_policy": "holdout"},
        ],
    }
    plan = (
        f"Fit logistic regression for {STEPS} batch gradient steps at learning rate {LEARNING_RATE}, "
        f"from weights of zero, on training features standardized with the training mean and "
        f"root-mean-square. Predict label 1 when the probability is at least {THRESHOLD}. "
        f"The baseline predicts the majority training label, and a tie predicts {TIE_CLASS}. "
        f"On the holdout, the estimand is the paired accuracy difference. A 95% Wald interval "
        f"uses the fixed normal quantile {Z_975}. The exact two-sided McNemar probability uses "
        f"the discordant counts. Record supports only when the interval lies entirely above 0 and "
        f"that probability is below {ALPHA}; record refutes only for the symmetric rule below 0; "
        "otherwise record inconclusive. Do not add comparisons or rows."
    )
    return api.ProtocolDraft(
        schema_version=1,
        design=("Confirmatory comparison on one frozen holdout file. The training file is declared "
                "seen. The holdout file is the confirmatory split and is not listed in seen_data. "
                f"{plan}"),
        analysis_plan=plan, metric=METRIC, stopping_rule=STOPPING, statistical_design=design,
        roster=[0], roster_semantics="deterministic_single", sample_size_scope="total",
        run_limit=2, replication_tolerance=NUMERIC_TOLERANCE, seen_data=[train_digest])


def compile_execution(request):
    train, holdout = _files(request)
    return api.ExecutionPlan(
        primary_program=programs.PRIMARY_SOURCE, reanalysis_program=programs.REANALYSIS_SOURCE,
        input=_execution_input(train, holdout), outputs=dict(OUTPUTS), wall_seconds=WALL_SECONDS,
        max_output_bytes=MAX_OUTPUT_BYTES, required_capabilities=[],
        execution_profile="trusted_local_python_v1",
        environment_requirements=api.environment_requirements())


def capture(source):
    return reader.read_directory(source)


def _fail(condition, message):
    if condition:
        raise ValueError(message)


def validate_protocol(context):
    protocol = context.protocol
    design = protocol["statistical_design"]
    splits = {row["id"]: row for row in design["data_splits"]}
    inventory = {row["path"]: row["sha256"] for row in context.capture["inventory"]}
    train, holdout = inventory["train.csv"], inventory["holdout.csv"]
    _fail(api.thaw(context.parameters) != {}, "tabular protocol has parameters")
    _fail(protocol.get("protocol_mode") != "confirmatory", "tabular protocol is not confirmatory")
    _fail(protocol["metric"] != METRIC or list(protocol["seeds"]) != [0],
          "tabular protocol metric or roster differs")
    _fail(protocol["data"] != context.execution_plan["input"]["sha256"],
          "program input differs from protocol data")
    _fail(protocol["data"] in {train, holdout}, "program input is the training or holdout file itself")
    _fail(train not in protocol["seen_data"], "training artifact is not declared seen")
    _fail(holdout in protocol["seen_data"], "holdout artifact is declared seen")
    _fail(splits["training"]["digest"] != train
          or (splits["training"]["role"], splits["training"]["exposure_policy"]) != ("discovery", "open"),
          "training split does not match the captured training file")
    _fail(splits["holdout"]["digest"] != holdout
          or (splits["holdout"]["role"], splits["holdout"]["exposure_policy"]) != ("confirmatory", "holdout"),
          "holdout split does not match the captured holdout file")
    _fail(design["uncertainty"]["method"] != "paired_wald", "uncertainty method differs")
    _fail(design["sample_size"] < 2, "holdout sample size is below 2")
    _fail(design["experimental_unit"] != "holdout row", "experimental unit differs")
    _fail(list(design["multiple_testing"]["family"]) != [FAMILY], "multiple-testing family differs")
    _fail(design["multiple_testing"]["correction"] != "none", "multiple-testing correction differs")


def validate_outputs(context, cas):
    return analysis.validate_outputs(context, cas)


def recompute_metrics(context, cas):
    return analysis.recompute_metrics(context, cas)


def analyse(context, cas, checks, recomputations):
    return analysis.analyse(context, cas, checks, recomputations)
