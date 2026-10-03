"""Frozen runner programs of the synthetic causal fixture.

These bytes equal ``PRIMARY_SOURCE`` and ``REANALYSIS_SOURCE`` of the legacy
``episteme.domains.synthetic_causal`` module; a golden test compares them. The
second program is a distinct calculation on the same observations, not an
independently collected replication or independently authored code.
"""

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
