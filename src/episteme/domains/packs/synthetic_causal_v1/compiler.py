"""Recipe validation and preregistration text of the synthetic causal fixture.

The computations and texts equal ``compile_recipe`` of the legacy
``episteme.domains.synthetic_causal`` module; a golden test compares them on a
parameter grid. The world is a host input: it enters ``input.dat`` and the
protocol data digest but never the parameter catalog or analysis context.
"""

import hashlib
import json
import math


DOMAIN_ID = "synthetic_causal_v1"
METRIC = "treatment_effect"
OUTPUTS = {"raw_data": "raw.json", "metrics": "metrics.json"}
PARAMETERS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["n_samples", "assignment", "analysis"],
    "properties": {
        "n_samples": {"type": "integer", "minimum": 32, "maximum": 2048},
        "assignment": {"enum": ["randomized", "observational"]},
        "analysis": {"enum": ["difference_in_means", "adjusted_ols"]},
    },
}
LIMITATIONS = [
    "The generated observations are synthetic fixtures, not real-world evidence.",
    "No uncertainty interval, power calculation or significance test is supplied.",
    "A distinct reanalysis implementation uses the same observations and is not new-data replication.",
]


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def parameters(value):
    if type(value) is not dict or set(value) != {"n_samples", "assignment", "analysis"}:
        raise ValueError("parameters require exactly n_samples, assignment and analysis")
    count = value["n_samples"]
    if type(count) is not int or not 32 <= count <= 2048:
        raise ValueError("n_samples must be an integer in [32, 2048]")
    if type(value["assignment"]) is not str or value["assignment"] not in {"randomized", "observational"}:
        raise ValueError("unsupported assignment")
    if type(value["analysis"]) is not str or value["analysis"] not in {"difference_in_means", "adjusted_ols"}:
        raise ValueError("unsupported analysis")
    return dict(value)


def world(value):
    if type(value) is not dict or set(value) != {"treatment_effect", "confounding_strength", "noise_std"}:
        raise ValueError("world requires exactly treatment_effect, confounding_strength and noise_std")
    normalized = {}
    for name, item in value.items():
        if type(item) not in {int, float}:
            raise ValueError(f"{name} must be a finite number")
        try:
            normalized[name] = float(item)
        except OverflowError as exc:
            raise ValueError(f"{name} must be a finite number") from exc
        if not math.isfinite(normalized[name]):
            raise ValueError(f"{name} must be a finite number")
    if normalized["noise_std"] < 0:
        raise ValueError("noise_std must be nonnegative")
    return normalized


def host(value):
    """Host-owned world, RNG seed roster and reanalysis tolerance."""
    if type(value) is not dict or set(value) != {"world", "seeds", "replication_tolerance"}:
        raise ValueError("host inputs require exactly world, seeds and replication_tolerance")
    seeds = value["seeds"]
    if (type(seeds) not in (list, tuple) or not 1 <= len(seeds) <= 64
            or not all(type(seed) is int and -(2 ** 31) <= seed < 2 ** 31 for seed in seeds)
            or len(set(seeds)) != len(seeds)):
        raise ValueError("seeds must be 1 to 64 unique 32-bit integers")
    tolerance = value["replication_tolerance"]
    if type(tolerance) not in (int, float) or not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("replication_tolerance must be finite and nonnegative")
    return world(value["world"]), list(seeds), float(tolerance)


def data(chosen, normalized_world):
    return json_bytes({"schema_version": 1, "domain": DOMAIN_ID,
                       "parameters": chosen, "world": normalized_world})


def protocol_texts(chosen, input_bytes):
    """Design, analysis plan, stopping rule and typed design of one recipe."""
    count = chosen["n_samples"]
    assignment = chosen["assignment"]
    analysis = chosen["analysis"]
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
            "digest": hashlib.sha256(input_bytes).hexdigest(),
            "role": "discovery", "exposure_policy": "open",
        }],
    }
    return design, analysis_plan, stopping_rule, statistical_design
