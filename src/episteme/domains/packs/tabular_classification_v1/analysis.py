"""Analysis hooks. They read a CasView, never a Store.

The point estimate is the preregistered holdout accuracy difference. The
outcome follows the fixed rule in ``model.compare``. A complete report may
still be inconclusive when that rule is not met.
"""

import hashlib

from episteme.domains import api

from .model import (ALPHA, METRIC, Z_975, compare, parse_table, score_holdout)


PACK_ID = "tabular_classification_v1"
PACK_VERSION = "1"

LIMITATIONS = [
    "The comparison uses only the captured rows. It is not a sample estimate and not evidence about a population.",
    "The committed examples for this pack are generated mechanism-test tables, not a scientific dataset. This report does not establish that every captured file has that origin.",
    "The reanalysis recounts the same scored rows with a second program from this pack. It is same-data replay of one implementation family, not independent reanalysis and not new-data replication.",
    "Profile v1 copies both CSV files into one program input. Compile does not fit on holdout labels, and there is no process boundary keeping those bytes out of the planner.",
    "The Wald interval is a normal approximation with a fixed quantile. The exact McNemar probability is the second preregistered condition. Neither is a population effect.",
    "scientific_validity is not assessed. A mechanical confirmatory label is not a reviewer approval.",
]


def _metric(document):
    value = document.get(METRIC)
    if type(value) not in (int, float):
        raise ValueError("metrics file lacks accuracy_difference")
    return float(value)


def _raw(cas, key, seed):
    document = cas.json(key, "raw data")
    rows = document.get("rows") if type(document) is dict else None
    if (type(document) is not dict or document.get("schema_version") != 1
            or document.get("domain") != PACK_ID or document.get("seed") != seed
            or type(rows) is not list):
        raise ValueError("raw data does not match the tabular classification schema")
    return document


def _splits(context):
    found = {row["id"]: row["digest"] for row in context.protocol["statistical_design"]["data_splits"]}
    if set(found) != {"training", "holdout"}:
        raise ValueError("tabular protocol splits are not the training and holdout artifacts")
    return found["training"], found["holdout"]


def validate_outputs(context, cas):
    checks = []
    primary_raw = {}
    for row in context.slots:
        findings = []
        try:
            document = _raw(cas, row["outputs"]["raw_data"], row["roster_unit"])
            _metric(cas.json(row["outputs"]["metrics"], "metrics"))
            if row["mode"] == "primary":
                primary_raw[row["roster_unit"]] = row["outputs"]["raw_data"]
            elif primary_raw.get(row["roster_unit"]) != row["outputs"]["raw_data"]:
                raise ValueError("reanalysis did not keep the primary raw data")
            if any(item.get("i") != index for index, item in enumerate(document["rows"])):
                raise ValueError("raw rows are not the holdout order")
        except (api.EnvelopeError, ValueError, TypeError) as exc:
            findings.append(str(exc))
        checks.append(api.OutputCheck(
            schema_version=1, slot=row["slot"], roster_unit=row["roster_unit"], mode=row["mode"],
            result=row["result"], raw_data=row["outputs"]["raw_data"],
            metrics=row["outputs"]["metrics"], status="failed" if findings else "passed",
            findings=findings))
    return checks


def _recorded_rows(document, holdout):
    rows = document["rows"]
    if len(rows) != len(holdout):
        raise ValueError("raw row count differs from the captured holdout")
    model_only = baseline_only = 0
    for index, (row, original) in enumerate(zip(rows, holdout)):
        if (row.get("i") != index or row.get("label") != original[2]
                or row.get("logistic") not in (0, 1) or row.get("majority") not in (0, 1)):
            raise ValueError("raw row does not match the captured holdout label")
        if row["logistic"] == row["label"] and row["majority"] != row["label"]:
            model_only += 1
        elif row["logistic"] != row["label"] and row["majority"] == row["label"]:
            baseline_only += 1
    return model_only, baseline_only


def recompute_metrics(context, cas):
    train_key, holdout_key = _splits(context)
    train = parse_table(cas.read(train_key))
    holdout = parse_table(cas.read(holdout_key))
    scored = score_holdout(train, holdout)
    result = []
    for unit in context.draft.roster:
        primary = context.slot(f"primary:{unit}")
        reanalysis = context.slot(f"reanalysis:{unit}")
        if reanalysis["outputs"]["raw_data"] != primary["outputs"]["raw_data"]:
            raise ValueError("reanalysis did not keep the primary raw data")
        document = _raw(cas, primary["outputs"]["raw_data"], unit)
        if document["rows"] != scored["rows"]:
            raise ValueError("recorded holdout predictions differ from a refit on the training artifact")
        recounted_model, recounted_baseline = _recorded_rows(document, holdout)
        if (recounted_model, recounted_baseline) != (scored["model_only"], scored["baseline_only"]):
            raise ValueError("recorded discordant counts differ from the refit")
        comparison = compare(scored["model_only"], scored["baseline_only"], scored["n"])
        primary_metric = _metric(cas.json(primary["outputs"]["metrics"], "metrics"))
        reanalysis_metric = _metric(cas.json(reanalysis["outputs"]["metrics"], "metrics"))
        primary_gap = abs(comparison["difference"] - primary_metric)
        reanalysis_gap = abs(comparison["difference"] - reanalysis_metric)
        if primary_gap > context.numeric_tolerance or reanalysis_gap > context.numeric_tolerance:
            raise ValueError("recorded accuracy difference disagrees with the refit")
        result.append(api.Recomputation(
            schema_version=1, roster_unit=unit, metric=METRIC, recomputed=comparison["difference"],
            primary_recorded=primary_metric, reanalysis_recorded=reanalysis_metric,
            primary_absolute_difference=primary_gap, reanalysis_absolute_difference=reanalysis_gap,
            tolerance=context.numeric_tolerance))
    return result


def _statement(comparison):
    rendered = (f"difference {comparison['difference']:.16g}, "
                f"95% Wald interval [{comparison['lower']:.16g}, {comparison['upper']:.16g}], "
                f"exact McNemar p {comparison['p_value']:.16g}")
    if comparison["outcome"] == "supports":
        decision = ("The preregistered rule records supports: the interval lies entirely above 0 "
                    "and the exact test is below 0.05.")
    elif comparison["outcome"] == "refutes":
        decision = ("The preregistered rule records refutes: the interval lies entirely below 0 "
                    "and the exact test is below 0.05.")
    else:
        decision = ("The preregistered rule records inconclusive. A difference whose interval "
                    "covers 0, or an exact test that does not meet 0.05, is not recorded as support.")
    return ("On the captured holdout rows, fixed-step logistic regression minus the training "
            f"majority class has {rendered}. {decision} This describes those rows under the "
            "preregistered rule, not a population effect.")


def analyse(context, cas, checks, recomputations):
    failed = [check for check in checks if check.status != "passed"]
    if failed:
        raise ValueError("tabular outputs failed pack checks: "
                         + "; ".join(item for check in failed for item in check.findings))
    by_unit = {row.roster_unit: row for row in recomputations}
    if sorted(by_unit) != sorted(context.draft.roster):
        raise ValueError("tabular recomputations do not cover the roster")
    train_key, holdout_key = _splits(context)
    train = parse_table(cas.read(train_key))
    holdout = parse_table(cas.read(holdout_key))
    scored = score_holdout(train, holdout)
    comparison = compare(scored["model_only"], scored["baseline_only"], scored["n"])
    design = context.draft.typed_design()
    point = by_unit[context.draft.roster[0]].recomputed
    if point != comparison["difference"]:
        raise ValueError("point estimate differs from the refit")
    interval_above = comparison["lower"] > 0.0 and comparison["p_value"] < ALPHA
    interval_below = comparison["upper"] < 0.0 and comparison["p_value"] < ALPHA
    sensitivity = (
        f"Wald interval [{comparison['lower']:.16g}, {comparison['upper']:.16g}] with fixed quantile "
        f"{Z_975}; exact McNemar p {comparison['p_value']:.16g} against alpha {ALPHA}. "
        + ("Both conditions of the rule hold in the same direction."
           if interval_above or interval_below else
           "The two conditions do not both hold, so the rule stays inconclusive."))
    report = api.StatisticalReport.from_dict({
        "schema_version": 1, "protocol_hash": context.protocol_hash,
        "estimand": api.supplied({"protocol_hash": context.protocol_hash, "text": design.estimand}),
        "estimator": api.supplied({
            "name": "logistic_gd_minus_training_majority", "hook": "recompute_metrics",
            "description": ("Batch gradient descent for a fixed step count, fit on the training "
                            "file only, compared with the training majority class.")}),
        "point_estimate": api.supplied({
            "metric": METRIC, "unit": design.primary_metric.unit, "value": point,
            "by_roster_unit": [{"roster_unit": unit, "value": by_unit[unit].recomputed}
                               for unit in context.draft.roster]}),
        "uncertainty": api.supplied({"method": design.uncertainty.method,
                                     "resampling_unit": design.uncertainty.resampling_unit}),
        "confidence_interval": api.supplied({
            "level": 0.95, "lower": comparison["lower"], "upper": comparison["upper"],
            "method": "paired_wald"}),
        "effect_size": api.supplied({
            "measure": "paired accuracy difference", "value": comparison["difference"],
            "reference": "0, the training majority classifier on the same holdout rows"}),
        "assumptions": api.supplied([
            {"assumption": ("Logistic weights and the majority class are fit on the training "
                            "file only. Holdout labels are used to score rows, not to fit."),
             "status": "holds",
             "reference": "The pinned program fits before it reads holdout labels, and the refit matches recorded predictions."},
            {"assumption": "Every captured holdout row is scored by both classifiers and none is excluded.",
             "status": "holds",
             "reference": "The analysed count equals the preregistered holdout row count."},
            {"assumption": ("The Wald interval uses the pack's fixed normal quantile, and the "
                            "exact McNemar probability is the two-sided binomial tail of the discordant counts."),
             "status": "holds",
             "reference": "Z_975 and alpha are constants of pack version 1, not estimated from the holdout."},
        ]),
        "sample_size": api.supplied({
            "experimental_unit": design.experimental_unit, "unit_scope": "total",
            "planned": design.sample_size, "planned_total": design.sample_size,
            "analysed": scored["n"], "exclusions": [], "missing_slots": []}),
        "multiple_testing": api.supplied({
            "family": list(design.multiple_testing.family), "claim_position": "1",
            "correction": design.multiple_testing.correction,
            "adjusted_result": "No adjustment: the preregistered family has one comparison."}),
        "stopping_rule": api.supplied({
            "rule_sha256": hashlib.sha256(design.stopping_rule.rule.encode("utf-8")).hexdigest(),
            "roster_complete": True, "interim_looks": 0}),
        "sensitivity_analysis": api.supplied([
            {"name": "wald_interval_and_exact_mcnemar", "timing": "preregistered", "result": sensitivity}]),
        "deviations": api.supplied([]),
    })
    return api.AnalysisReport(
        pack_id=PACK_ID, pack_version=PACK_VERSION, protocol_hash=context.protocol_hash,
        statement=_statement(comparison), limitations=list(LIMITATIONS),
        outcome=comparison["outcome"], inference_mode="confirmatory",
        details=dict(domain=PACK_ID, model_only_correct=scored["model_only"],
                     baseline_only_correct=scored["baseline_only"], holdout_rows=scored["n"],
                     majority_class=scored["majority_class"], difference=comparison["difference"],
                     wald_lower=comparison["lower"], wald_upper=comparison["upper"],
                     mcnemar_p=comparison["p_value"], outcome_rule="wald_and_exact_mcnemar"),
        statistical_report=report)
