"""Raw-data checks and the separate estimator of the synthetic causal fixture.

The arithmetic equals ``_raw``, ``_estimate`` and ``_metric`` of the legacy
``episteme.domains.synthetic_batch_analysis`` adapter; a golden test compares
the recomputed values on saved histories. The 1e-9 tolerance concerns
arithmetic agreement only; it is not an uncertainty bound.
"""

import math
import statistics

from .compiler import DOMAIN_ID, METRIC


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value, label):
    _require(type(value) in {int, float}, f"{label} must be a finite number")
    try:
        converted = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    _require(math.isfinite(converted), f"{label} must be a finite number")
    return converted


def raw(cas, key, *, seed):
    """Rows and observed recipe parameters of one primary raw.json."""
    document = cas.json(key, "synthetic raw data")
    _require(type(document) is dict, "synthetic raw data must be an object")
    _require(set(document) == {"schema_version", "domain", "seed", "parameters", "rows"}
             and type(document["schema_version"]) is int and document["schema_version"] == 1
             and document["domain"] == DOMAIN_ID and type(document["seed"]) is int
             and document["seed"] == seed,
             "synthetic raw data has the wrong domain or seed")
    observed = document["parameters"]
    _require(type(observed) is dict
             and set(observed) == {"n_samples", "assignment", "analysis"}
             and type(observed["n_samples"]) is int
             and 32 <= observed["n_samples"] <= 2048
             and type(observed["assignment"]) is str
             and observed["assignment"] in {"randomized", "observational"}
             and type(observed["analysis"]) is str
             and observed["analysis"] in {"difference_in_means", "adjusted_ols"},
             "invalid observed synthetic recipe parameters")
    rows = document["rows"]
    _require(type(rows) is list and len(rows) == observed["n_samples"],
             "synthetic raw data has an incomplete sample")
    for index, row in enumerate(rows):
        _require(type(row) is dict and set(row) == {"unit", "u", "t", "y"}
                 and type(row["unit"]) is int and row["unit"] == index
                 and type(row["u"]) is int and row["u"] in (0, 1)
                 and type(row["t"]) is int and row["t"] in (0, 1),
                 "synthetic raw data has an invalid or duplicated unit")
        number(row["y"], "synthetic outcome")
    return rows, observed


def estimate(rows, method):
    # This is separate from the primary program's implementation.  For OLS,
    # binary-U within-stratum contrasts are combined using n1*n0/(n1+n0).
    if method == "difference_in_means":
        treated = [row["y"] for row in rows if row["t"] == 1]
        control = [row["y"] for row in rows if row["t"] == 0]
        _require(bool(treated) and bool(control), "synthetic sample has an empty treatment group")
        value = statistics.fmean(treated) - statistics.fmean(control)
    else:
        _require(method == "adjusted_ols", "unsupported synthetic analysis method")
        numerators = []
        weights = []
        for u in (0, 1):
            stratum = [row for row in rows if row["u"] == u]
            _require(bool(stratum), "adjusted synthetic sample has an empty U stratum")
            treated = [row["y"] for row in stratum if row["t"] == 1]
            control = [row["y"] for row in stratum if row["t"] == 0]
            if treated and control:
                weight = len(treated) * len(control) / len(stratum)
                weights.append(weight)
                numerators.append(weight * (statistics.fmean(treated) - statistics.fmean(control)))
        _require(bool(weights), "adjusted treatment coefficient is unidentified")
        value = math.fsum(numerators) / math.fsum(weights)
    _require(math.isfinite(value), "non-finite recomputed synthetic effect")
    return value


def metric(cas, key):
    metrics = cas.json(key, "synthetic metrics")
    _require(type(metrics) is dict and set(metrics) == {METRIC},
             "synthetic metrics require only treatment_effect")
    return number(metrics[METRIC], "recorded treatment_effect")
