"""Descriptive analysis of a completed synthetic causal execution batch.

This adapter is trusted local analysis code, not an independent scientific
reviewer.  It recomputes point estimates from the frozen raw observations and
checks both reported metrics with an absolute 1e-9 numerical tolerance.  That
tolerance concerns arithmetic agreement only; it is not an uncertainty bound.
The generator's known world parameters live in protocol.data; this module
never reads that artifact.  Its recipe parameters come from observed raw.json
artifacts, and a separate admission layer checks the compiled domain binding.
"""

from __future__ import annotations

import json
import math
import statistics
from typing import Any

from ..kernel import Kernel, require
from ..store import Store
from .synthetic_causal import DEFAULT_REANALYSIS_TOLERANCE, DOMAIN_ID, METRIC


ADAPTER_ID = DOMAIN_ID
ADAPTER_VERSION = "1"
NUMERIC_TOLERANCE = DEFAULT_REANALYSIS_TOLERANCE


def _object(data: bytes, label: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            require(key not in result, f"duplicate key in {label}: {key}")
            result[key] = value
        return result

    def invalid(value: str) -> None:
        raise ValueError(f"non-finite constant in {label}: {value}")

    try:
        result = json.loads(data, object_pairs_hook=unique, parse_constant=invalid)
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    require(type(result) is dict, f"{label} must be an object")
    return result


def _number(value: Any, label: str) -> float:
    require(type(value) in {int, float}, f"{label} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    require(math.isfinite(number), f"{label} must be a finite number")
    return number


def _raw(store: Store, digest: str, *, seed: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw = _object(store.read(digest), "synthetic raw data")
    require(set(raw) == {"schema_version", "domain", "seed", "parameters", "rows"}
            and type(raw["schema_version"]) is int and raw["schema_version"] == 1
            and raw["domain"] == DOMAIN_ID and type(raw["seed"]) is int
            and raw["seed"] == seed,
            "synthetic raw data has the wrong domain or seed")
    parameters = raw["parameters"]
    require(type(parameters) is dict
            and set(parameters) == {"n_samples", "assignment", "analysis"}
            and type(parameters["n_samples"]) is int
            and 32 <= parameters["n_samples"] <= 2048
            and type(parameters["assignment"]) is str
            and parameters["assignment"] in {"randomized", "observational"}
            and type(parameters["analysis"]) is str
            and parameters["analysis"] in {"difference_in_means", "adjusted_ols"},
            "invalid observed synthetic recipe parameters")
    rows = raw["rows"]
    require(type(rows) is list and len(rows) == parameters["n_samples"],
            "synthetic raw data has an incomplete sample")
    for index, row in enumerate(rows):
        require(type(row) is dict and set(row) == {"unit", "u", "t", "y"}
                and type(row["unit"]) is int and row["unit"] == index
                and type(row["u"]) is int and row["u"] in (0, 1)
                and type(row["t"]) is int and row["t"] in (0, 1),
                "synthetic raw data has an invalid or duplicated unit")
        _number(row["y"], "synthetic outcome")
    return rows, parameters


def _estimate(rows: list[dict[str, Any]], method: str) -> float:
    # This is separate from the primary program's implementation.  For OLS,
    # binary-U within-stratum contrasts are combined using n1*n0/(n1+n0).
    if method == "difference_in_means":
        treated = [row["y"] for row in rows if row["t"] == 1]
        control = [row["y"] for row in rows if row["t"] == 0]
        require(bool(treated) and bool(control), "synthetic sample has an empty treatment group")
        value = statistics.fmean(treated) - statistics.fmean(control)
    else:
        require(method == "adjusted_ols", "unsupported synthetic analysis method")
        numerators: list[float] = []
        weights: list[float] = []
        for u in (0, 1):
            stratum = [row for row in rows if row["u"] == u]
            require(bool(stratum), "adjusted synthetic sample has an empty U stratum")
            treated = [row["y"] for row in stratum if row["t"] == 1]
            control = [row["y"] for row in stratum if row["t"] == 0]
            if treated and control:
                weight = len(treated) * len(control) / len(stratum)
                weights.append(weight)
                numerators.append(weight * (statistics.fmean(treated) - statistics.fmean(control)))
        require(bool(weights), "adjusted treatment coefficient is unidentified")
        value = math.fsum(numerators) / math.fsum(weights)
    require(math.isfinite(value), "non-finite recomputed synthetic effect")
    return value


def _metric(store: Store, digest: str) -> float:
    metrics = _object(store.read(digest), "synthetic metrics")
    require(set(metrics) == {METRIC}, "synthetic metrics require only treatment_effect")
    return _number(metrics[METRIC], "recorded treatment_effect")


def _frozen_parameters(store: Store, history: list[dict[str, Any]], protocol: str) -> dict[str, Any]:
    applications = [event for event in history if event["kind"] == "agent_application"
                    and event["payload"].get("protocol") == protocol]
    require(len(applications) == 1, "synthetic analysis requires one frozen experiment application")
    manifest = _object(store.read(applications[0]["payload"]["compilation"]),
                       "frozen experiment compilation")
    require(type(manifest.get("compiled")) is dict
            and type(manifest["compiled"].get("parameters")) is dict,
            "synthetic compilation lacks registered parameters")
    return manifest["compiled"]["parameters"]


class SyntheticCausalBatchAnalysisAdapter:
    """Build an inconclusive, exploratory proposal from verified batch state.

    ``state`` must be a state from ``batch._index`` for the same Store.  This
    adapter adds domain checks but does not replace batch, execution, or claim
    mechanical validation.  It does not create any state or decide a verdict.
    """

    adapter_id = ADAPTER_ID
    adapter_version = ADAPTER_VERSION

    @staticmethod
    def propose(store: Store, state: dict[str, Any]) -> dict[str, Any]:
        require(state["settlement"] is not None and state["terminal"] is not None
                and state["settlement"]["payload"]["status"] == "completed"
                and state["settlement"]["payload"]["scientific_validity"] == "not_assessed",
                "synthetic analysis requires a completed, unreviewed batch")
        plan = state["plan"]["payload"]
        rows = state["slots"]
        require(type(rows) is list and len(rows) == len(plan["slots"])
                and all(row["status"] == "completed" for row in rows)
                and [row["slot"] for row in rows] == [slot["slot"] for slot in plan["slots"]],
                "synthetic batch has missing or incomplete slots")
        history = store.events()
        protocol = Kernel._get(history, plan["protocol"], "protocol")["payload"]
        frozen_parameters = _frozen_parameters(store, history, plan["protocol"])
        require(protocol["metric"] == METRIC and protocol.get("protocol_mode") == "exploratory",
                "synthetic analysis requires an exploratory treatment_effect protocol")
        require(plan["outputs"]["raw_data"] == "raw.json"
                and plan["outputs"]["metrics"] == "metrics.json",
                "batch output names differ from the synthetic recipe")
        by_slot = {row["slot"]: row for row in rows}
        seeds = protocol["seeds"]
        require(type(seeds) is list and bool(seeds)
                and len(rows) == 2 * len(seeds), "synthetic batch has an incomplete seed roster")
        details: list[dict[str, Any]] = []
        parameters: dict[str, Any] | None = None
        for seed in seeds:
            require(type(seed) is int, "synthetic seed must be an integer")
            primary = by_slot.get(f"primary:{seed}")
            reanalysis = by_slot.get(f"reanalysis:{seed}")
            require(primary is not None and reanalysis is not None
                    and primary["mode"] == "primary"
                    and reanalysis["mode"] == "independent_reanalysis",
                    "synthetic batch lacks a primary/reanalysis seed pair")
            primary_run = Kernel._get(history, primary["run"], "run")
            reanalysis_run = Kernel._get(history, reanalysis["run"], "run")
            primary_result = Kernel._get(history, primary["result"], "result")
            reanalysis_result = Kernel._get(history, reanalysis["result"], "result")
            require(primary_run["payload"]["seed"] == seed
                    and primary_run["payload"]["replicate_of"] is None
                    and reanalysis_run["payload"]["seed"] == seed
                    and reanalysis_run["payload"]["replicate_of"] == primary_run["id"]
                    and primary_result["payload"]["run"] == primary_run["id"]
                    and reanalysis_result["payload"]["run"] == reanalysis_run["id"]
                    and primary_result["payload"]["status"] == "completed"
                    and reanalysis_result["payload"]["status"] == "completed",
                    "synthetic seed pair lacks completed, linked results")
            primary_outputs = primary_result["payload"]["outputs"]
            reanalysis_outputs = reanalysis_result["payload"]["outputs"]
            raw_digest = primary_outputs["raw_data"]
            require(reanalysis_outputs["raw_data"] == raw_digest,
                    "reanalysis did not use the primary observations")
            raw_rows, observed_parameters = _raw(store, raw_digest, seed=seed)
            if parameters is None:
                parameters = observed_parameters
            require(observed_parameters == parameters,
                    "synthetic seed pairs use inconsistent observed recipe parameters")
            require(observed_parameters == frozen_parameters,
                    "observed synthetic parameters differ from the frozen experiment recipe")
            recomputed = _estimate(raw_rows, parameters["analysis"])
            reported_primary = _metric(store, primary_outputs["metrics"])
            reported_reanalysis = _metric(store, reanalysis_outputs["metrics"])
            primary_delta = abs(recomputed - reported_primary)
            reanalysis_delta = abs(recomputed - reported_reanalysis)
            require(primary_delta <= NUMERIC_TOLERANCE
                    and reanalysis_delta <= NUMERIC_TOLERANCE,
                    f"synthetic metric disagrees with recomputed raw data for seed {seed}")
            details.append(dict(seed=seed, primary_run=primary_run["id"],
                                primary_result=primary_result["id"],
                                reanalysis_run=reanalysis_run["id"],
                                reanalysis_result=reanalysis_result["id"],
                                raw_data=raw_digest, n_units=len(raw_rows),
                                n_treated=sum(row["t"] for row in raw_rows),
                                n_control=sum(row["t"] == 0 for row in raw_rows),
                                recomputed_treatment_effect=recomputed,
                                primary_reported_treatment_effect=reported_primary,
                                reanalysis_reported_treatment_effect=reported_reanalysis,
                                primary_absolute_difference=primary_delta,
                                reanalysis_absolute_difference=reanalysis_delta))
        assert parameters is not None  # The verified roster requires at least one seed.
        rendered = ", ".join(f"seed {item['seed']}: {item['recomputed_treatment_effect']:.12g}"
                             for item in details)
        return dict(schema_version=1, adapter_id=ADAPTER_ID, adapter_version=ADAPTER_VERSION,
                    statement=("Synthetic causal fixture treatment_effect point estimates from "
                               f"all registered observations were {rendered}. These exploratory "
                               "descriptions do not establish a causal or real-world finding."),
                    limitations=[
                        "All observations were generated by a synthetic fixture, not collected from a real population.",
                        "The separate reanalysis uses the same observations; it is not new-data replication.",
                        "No uncertainty interval, power analysis, significance test or scientific review is supplied.",
                    ],
                    outcome="inconclusive", inference_mode="exploratory",
                    details=dict(domain=DOMAIN_ID, assignment=parameters["assignment"],
                                 analysis=parameters["analysis"], metric=METRIC,
                                 absolute_numeric_tolerance=NUMERIC_TOLERANCE,
                                 seed_results=details))
