"""A small, known-ground-truth causal *fixture* for domain-adapter exercises.

The host supplies the world and freezes both source and input bytes before a
run.  Model proposals may choose only the documented recipe parameters.  The
generated observations and estimates do not constitute scientific evidence
about a real population, and the second program is a distinct calculation on
the same observations, not an independently collected replication.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any


DOMAIN_ID = "synthetic_causal_v1"
DEFAULT_REANALYSIS_TOLERANCE = 1e-9  # Absolute numeric agreement, not scientific uncertainty.
METRIC = "treatment_effect"
OUTPUTS = {"raw_data": "raw.json", "metrics": "metrics.json"}


# Frozen single-file implementation for runner_backend.  No repository imports
# or ambient data are needed by the child process.
PRIMARY_SOURCE = b'''"""Synthetic causal fixture: primary generator and analysis."""
import json
import math
import pathlib
import random
import sys


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def estimate(rows, method):
    if method == "difference_in_means":
        treated = [row["y"] for row in rows if row["t"] == 1]
        control = [row["y"] for row in rows if row["t"] == 0]
        if not treated or not control:
            raise ValueError("treatment or control group is empty")
        return math.fsum(treated) / len(treated) - math.fsum(control) / len(control)
    if method != "adjusted_ols":
        raise ValueError("unsupported analysis")
    numerator = denominator = 0.0
    for u in (0, 1):
        group = [row for row in rows if row["u"] == u]
        if not group:
            raise ValueError("adjusted design has an empty U stratum")
        t_mean = math.fsum(row["t"] for row in group) / len(group)
        y_mean = math.fsum(row["y"] for row in group) / len(group)
        numerator += math.fsum((row["t"] - t_mean) * (row["y"] - y_mean) for row in group)
        denominator += math.fsum((row["t"] - t_mean) ** 2 for row in group)
    if denominator <= 0.0:
        raise ValueError("treatment coefficient is unidentified")
    return numerator / denominator


def main():
    if len(sys.argv) != 4 or sys.argv[1:3] != ["input.dat", "--seed"]:
        raise ValueError("expected input.dat --seed N")
    seed = int(sys.argv[3])
    recipe = json.loads(pathlib.Path("input.dat").read_bytes())
    parameters, world = recipe["parameters"], recipe["world"]
    rng = random.Random(seed)
    rows = []
    for index in range(parameters["n_samples"]):
        u = int(rng.random() < 0.5)
        probability = 0.5 if parameters["assignment"] == "randomized" else (0.8 if u else 0.2)
        t = int(rng.random() < probability)
        noise = rng.gauss(0.0, world["noise_std"]) if world["noise_std"] else 0.0
        y = world["treatment_effect"] * t + world["confounding_strength"] * u + noise
        if not math.isfinite(y):
            raise ValueError("nonfinite generated outcome")
        rows.append({"unit": index, "u": u, "t": t, "y": y})
    raw = {"schema_version": 1, "domain": "synthetic_causal_v1", "seed": seed,
           "parameters": parameters, "rows": rows}
    pathlib.Path("raw.json").write_bytes(encoded(raw))
    value = estimate(rows, parameters["analysis"])
    if not math.isfinite(value):
        raise ValueError("nonfinite estimate")
    pathlib.Path("metrics.json").write_bytes(encoded({"treatment_effect": value}))


if __name__ == "__main__":
    main()
'''


# This program takes the primary raw.json as input.dat.  It uses a separate
# weighted-stratum calculation for adjusted OLS, while the primary
# implementation uses within-stratum residualization.  It deliberately does
# not generate new data.
REANALYSIS_SOURCE = b'''"""Synthetic causal fixture: reanalyse existing observations."""
import json
import math
import pathlib
import statistics
import sys


def estimate(rows, method):
    if method == "difference_in_means":
        groups = {0: [], 1: []}
        for row in rows:
            groups[row["t"]].append(row["y"])
        if not groups[0] or not groups[1]:
            raise ValueError("treatment or control group is empty")
        return statistics.fmean(groups[1]) - statistics.fmean(groups[0])
    if method != "adjusted_ols":
        raise ValueError("unsupported analysis")
    # With binary U, OLS Y ~ 1 + T + U equals the weighted mean of the
    # within-U treatment/control differences.  We compute it independently
    # from the primary program's residualized observation-level covariance.
    weighted_differences = []
    weights = []
    for u in (0, 1):
        stratum = [row for row in rows if row["u"] == u]
        if not stratum:
            raise ValueError("adjusted design has an empty U stratum")
        treated = [row["y"] for row in stratum if row["t"] == 1]
        control = [row["y"] for row in stratum if row["t"] == 0]
        if treated and control:
            weight = len(treated) * len(control) / len(stratum)
            weights.append(weight)
            weighted_differences.append(weight * (statistics.fmean(treated) - statistics.fmean(control)))
    if not weights:
        raise ValueError("treatment coefficient is unidentified")
    return math.fsum(weighted_differences) / math.fsum(weights)


def main():
    if len(sys.argv) != 4 or sys.argv[1:3] != ["input.dat", "--seed"]:
        raise ValueError("expected input.dat --seed N")
    seed = int(sys.argv[3])
    source = pathlib.Path("input.dat").read_bytes()
    raw = json.loads(source)
    if (raw["schema_version"] != 1 or raw["domain"] != "synthetic_causal_v1"
            or raw["seed"] != seed or len(raw["rows"]) != raw["parameters"]["n_samples"]):
        raise ValueError("raw observation identity mismatch")
    rows = raw["rows"]
    if any(type(row["u"]) is not int or row["u"] not in (0, 1)
           or type(row["t"]) is not int or row["t"] not in (0, 1)
           or not math.isfinite(row["y"]) for row in rows):
        raise ValueError("invalid raw observation")
    value = estimate(rows, raw["parameters"]["analysis"])
    if not math.isfinite(value):
        raise ValueError("nonfinite reanalysis estimate")
    pathlib.Path("raw.json").write_bytes(source)
    pathlib.Path("metrics.json").write_bytes(json.dumps(
        {"treatment_effect": value}, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8"))


if __name__ == "__main__":
    main()
'''


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _validate(parameters: dict[str, Any], world: dict[str, Any]) -> tuple[dict[str, Any], dict[str, float]]:
    if type(parameters) is not dict or set(parameters) != {"n_samples", "assignment", "analysis"}:
        raise ValueError("parameters require exactly n_samples, assignment and analysis")
    count = parameters["n_samples"]
    if type(count) is not int or not 32 <= count <= 2048:
        raise ValueError("n_samples must be an integer in [32, 2048]")
    if type(parameters["assignment"]) is not str or parameters["assignment"] not in {"randomized", "observational"}:
        raise ValueError("unsupported assignment")
    if type(parameters["analysis"]) is not str or parameters["analysis"] not in {"difference_in_means", "adjusted_ols"}:
        raise ValueError("unsupported analysis")
    if type(world) is not dict or set(world) != {"treatment_effect", "confounding_strength", "noise_std"}:
        raise ValueError("world requires exactly treatment_effect, confounding_strength and noise_std")
    normalized: dict[str, float] = {}
    for name, value in world.items():
        if type(value) not in {int, float}:
            raise ValueError(f"{name} must be a finite number")
        try:
            normalized[name] = float(value)
        except OverflowError as exc:
            raise ValueError(f"{name} must be a finite number") from exc
        if not math.isfinite(normalized[name]):
            raise ValueError(f"{name} must be a finite number")
    if normalized["noise_std"] < 0:
        raise ValueError("noise_std must be nonnegative")
    return dict(parameters), normalized


def describe() -> dict[str, Any]:
    """Return the JSON-only, model-visible recipe declaration (no world)."""
    return {
        "schema_version": 1,
        "id": DOMAIN_ID,
        "description": "Known-ground-truth synthetic causal fixture; estimates are descriptive.",
        "parameters_schema": {
            "type": "object", "additionalProperties": False,
            "required": ["n_samples", "assignment", "analysis"],
            "properties": {
                "n_samples": {"type": "integer", "minimum": 32, "maximum": 2048},
                "assignment": {"enum": ["randomized", "observational"]},
                "analysis": {"enum": ["difference_in_means", "adjusted_ols"]},
            },
        },
        "metric": METRIC,
        "outputs": dict(OUTPUTS),
        "limitations": [
            "The generated observations are synthetic fixtures, not real-world evidence.",
            "No uncertainty interval, power calculation or significance test is supplied.",
            "A distinct reanalysis implementation uses the same observations and is not new-data replication.",
        ],
    }


def compile_recipe(parameters: dict[str, Any], world: dict[str, Any]) -> dict[str, Any]:
    """Freeze a validated recipe for `Kernel.preregister_for_set` and the runner.

    The returned `data` is the generator configuration, not observed rows; the
    data split is therefore explicitly open and descriptive.  Callers persist
    `implementation`, `reanalysis_implementation`, and `data` in CAS and supply
    their digests to the kernel.  Seeds and run limits remain host-owned.
    """
    parameters, world = _validate(parameters, world)
    data = _json_bytes({"schema_version": 1, "domain": DOMAIN_ID,
                        "parameters": parameters, "world": world})
    count = parameters["n_samples"]
    assignment = parameters["assignment"]
    analysis = parameters["analysis"]
    design = (
        "Synthetic fixture: independently generated U~Bernoulli(0.5) per unit; "
        + ("T~Bernoulli(0.5) independently of U. " if assignment == "randomized"
           else "T~Bernoulli(0.8) when U=1 and Bernoulli(0.2) when U=0. ")
        + "Y = treatment_effect*T + confounding_strength*U + Gaussian noise. "
        "The host freezes the world and registered seeds; one seed generates "
        f"{count} units. No population or real-world inference is claimed."
    )
    if analysis == "difference_in_means":
        analysis_plan = (
            "Primary metric treatment_effect is mean(Y | T=1) - mean(Y | T=0) "
            "from all generated rows. An empty group fails the run. This estimator "
            "does not adjust observational confounding. Report no interval or p-value."
        )
    else:
        analysis_plan = (
            "Primary metric treatment_effect is the treatment coefficient in "
            "ordinary least squares Y ~ 1 + T + U, computed by within-U "
            "residualization from all generated rows. A singular design fails the "
            "run. Report no interval or p-value."
        )
    stopping_rule = (
        f"Generate exactly {count} synthetic units for each registered seed; "
        "analyse all rows once, with no interim looks or exclusions."
    )
    statistical_design = {
        "schema_version": 1, "mode": "exploratory",
        "experimental_unit": "synthetic unit",
        "estimand": (
            "Host-known structural effect of T on Y in the synthetic world; "
            "the reported estimator may be biased by confounding."
        ),
        "primary_metric": {"name": METRIC, "unit": "outcome units"},
        "secondary_metrics": [], "sample_size": count,
        "sample_size_rationale": (
            f"Fixed {count} generated units per seed for a bounded fixture; "
            "no power or target-population claim."
        ),
        "uncertainty": {
            "method": "not_applicable", "resampling_unit": None,
            "rationale": "This fixture reports a point estimate only; no inferential uncertainty is asserted.",
        },
        "exclusions": [],
        "stopping_rule": {"kind": "fixed_sample", "rule": stopping_rule},
        "multiple_testing": {
            "family": [], "correction": "not_applicable",
            "rationale": "No significance tests or hypothesis-family error claim.",
        },
        "data_splits": [{
            "id": "open_synthetic_generator_config",
            "digest": hashlib.sha256(data).hexdigest(),
            "role": "discovery", "exposure_policy": "open",
        }],
    }
    return {
        "schema_version": 1, "domain": DOMAIN_ID, "parameters": parameters,
        "implementation": PRIMARY_SOURCE,
        "reanalysis_implementation": REANALYSIS_SOURCE,
        "data": data, "metric": METRIC, "outputs": dict(OUTPUTS),
        "design": design, "analysis_plan": analysis_plan,
        "stopping_rule": stopping_rule, "statistical_design": statistical_design,
    }
