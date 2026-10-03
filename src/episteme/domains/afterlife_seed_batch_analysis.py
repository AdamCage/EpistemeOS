"""Exploratory analysis of historical Semantic Afterlife step trajectories.

This trusted adapter checks byte identity and arithmetic on already observed
data. It does not establish an independent data replication, a scientific
verdict, or the historical run's original preregistration status.
"""

from __future__ import annotations

import base64
import binascii
from collections import Counter
import hashlib
import json
import math
from typing import Any

from ..domain_binding import _index as binding_index
from ..kernel import Kernel, require
from ..store import Store, canonical, digest


ADAPTER_ID = "afterlife_seed_v1"
ADAPTER_VERSION = "1"
METRIC = "stop_event_rate"
NUMERIC_TOLERANCE = 1e-12
_ENTRY_FIELDS = {"slot_index", "trajectory_id", "semantic_seed", "stochastic_seed",
                 "steps_sha256", "steps_b64", "manifest"}
_STEP_FIELDS = {"step", "text", "finish_reason", "prompt_tokens", "completion_tokens",
                "cost_usd", "served_provider", "from_cache"}


def _object(data: bytes, label: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        row: dict[str, Any] = {}
        for key, value in pairs:
            require(key not in row, f"duplicate key in {label}: {key}")
            row[key] = value
        return row

    def invalid(value: str) -> None:
        raise ValueError(f"non-finite constant in {label}: {value}")

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid)
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    require(type(value) is dict, f"{label} must be a JSON object")
    return value


def _number(value: Any, label: str) -> float:
    require(type(value) in {int, float}, f"{label} must be numeric")
    try:
        converted = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be finite") from exc
    require(math.isfinite(converted), f"{label} must be finite")
    return converted


def _steps(store: Store, entry: dict[str, Any]) -> dict[str, Any]:
    require(type(entry) is dict and set(entry) == _ENTRY_FIELDS,
            "invalid historical trajectory entry")
    encoded = entry["steps_b64"]
    require(type(encoded) is str and 0 < len(encoded) <= 4 * 1024 * 1024,
            "historical steps are missing or oversized")
    try:
        source = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid historical steps base64") from exc
    require(base64.b64encode(source).decode("ascii") == encoded
            and 0 < len(source) <= 1024 * 1024,
            "noncanonical or oversized historical steps")
    key = entry["steps_sha256"]
    require(type(key) is str and hashlib.sha256(source).hexdigest() == key
            and store.read(key) == source, "historical steps differ from frozen CAS bytes")
    # Split bytes exactly as capture and both runner programs do; str.splitlines
    # would also break inside JSON strings at U+2028 or U+0085.
    lines = source.splitlines()
    require(0 < len(lines) <= 10000 and all(line.strip() for line in lines),
            "historical steps are empty or contain blank records")
    stopped = prompt_tokens = completion_tokens = 0
    providers: Counter[str] = Counter()
    for position, line in enumerate(lines, start=1):
        step = _object(line, "historical step")
        require(set(step) == _STEP_FIELDS and type(step["step"]) is int
                and step["step"] == position and type(step["text"]) is str
                and (step["finish_reason"] is None or type(step["finish_reason"]) is str)
                and type(step["prompt_tokens"]) is int and step["prompt_tokens"] >= 0
                and type(step["completion_tokens"]) is int and step["completion_tokens"] >= 0
                and type(step["served_provider"]) is str and bool(step["served_provider"].strip())
                and type(step["from_cache"]) is bool
                and _number(step["cost_usd"], "historical step cost") >= 0,
                "invalid historical step record or sequence")
        stopped += step["finish_reason"] not in (None, "length")
        prompt_tokens += step["prompt_tokens"]
        completion_tokens += step["completion_tokens"]
        providers[step["served_provider"]] += 1
    rate = stopped / len(lines)
    legacy = entry["manifest"]
    require(type(legacy) is dict and legacy.get("trajectory_id") == entry["trajectory_id"]
            and legacy.get("status") == "COMPLETED"
            and type(legacy.get("n_steps")) is int and legacy["n_steps"] == len(lines)
            and type(legacy.get("stop_events")) is int and legacy["stop_events"] == stopped
            and _number(legacy.get("stop_event_rate"), "legacy stop rate") == round(rate, 4)
            and type(legacy.get("prompt_tokens_total")) is int
            and legacy["prompt_tokens_total"] == prompt_tokens
            and type(legacy.get("completion_tokens_total")) is int
            and legacy["completion_tokens_total"] == completion_tokens
            and legacy.get("served_providers") == dict(providers),
            "historical manifest disagrees with raw step observations")
    return dict(n_steps=len(lines), stop_events=stopped, stop_event_rate=rate,
                prompt_tokens_total=prompt_tokens, completion_tokens_total=completion_tokens,
                served_providers=dict(providers))


def _metric(store: Store, key: str) -> float:
    metric = _object(store.read(key), "historical metrics")
    require(set(metric) == {METRIC}, "historical metrics require only stop_event_rate")
    value = _number(metric[METRIC], METRIC)
    require(0 <= value <= 1, "stop_event_rate is outside [0, 1]")
    return value


class AfterlifeSeedBatchAnalysisAdapter:
    """Propose one bounded, inconclusive claim from a complete local batch."""

    adapter_id = ADAPTER_ID
    adapter_version = ADAPTER_VERSION

    @staticmethod
    def propose(store: Store, state: dict[str, Any]) -> dict[str, Any]:
        require(state["settlement"] is not None and state["terminal"] is not None
                and state["settlement"]["payload"]["status"] == "completed"
                and state["settlement"]["payload"]["scientific_validity"] == "not_assessed",
                "historical analysis requires a completed, unreviewed batch")
        plan = state["plan"]["payload"]
        history = store.events()
        protocol = Kernel._get(history, plan["protocol"], "protocol")["payload"]
        require(protocol["metric"] == METRIC and protocol.get("protocol_mode") == "exploratory"
                and plan["outputs"] == {"raw_data": "raw.json", "metrics": "metrics.json"},
                "historical analysis needs the exploratory stop-rate recipe")
        binding = binding_index(store, history).get(plan["protocol"])
        require(binding is not None and binding["payload"]["adapter_id"] == ADAPTER_ID
                and binding["payload"]["adapter_version"] == ADAPTER_VERSION,
                "historical analysis lacks its frozen domain binding")
        bound = binding["payload"]
        recipe = _object(store.read(bound["recipe_digest"]), "historical recipe")
        require(set(recipe) == {"schema_version", "domain", "data", "manifest_sha256",
                                "trajectory_ids"}
                and recipe["schema_version"] == 1 and recipe["domain"] == ADAPTER_ID
                and recipe["data"] == protocol["data"]
                and type(recipe["trajectory_ids"]) is list,
                "historical recipe differs from frozen protocol")
        manifest_key = recipe["manifest_sha256"]
        manifest = _object(store.read(manifest_key), "historical manifest")
        integrity = manifest.get("integrity")
        trajectories = manifest.get("trajectories")
        require(type(trajectories) is dict and type(integrity) is dict
                and len(integrity) == 30
                and all(type(name) is str and type(key) is str for name, key in integrity.items())
                and set(bound["recipe_artifacts"]) == set(integrity.values()) | {manifest_key},
                "historical recipe lacks the complete manifest artifact closure")
        bundle = _object(store.read(protocol["data"]), "historical trajectory bundle")
        require(set(bundle) == {"schema_version", "domain", "manifest_sha256", "trajectories"}
                and bundle["schema_version"] == 1 and bundle["domain"] == ADAPTER_ID
                and bundle["manifest_sha256"] == manifest_key
                and type(bundle["trajectories"]) is list
                and len(bundle["trajectories"]) == len(protocol["seeds"]) == 9,
                "historical trajectory bundle has the wrong domain or roster")
        entries = bundle["trajectories"]
        require(all(type(row) is dict for row in entries)
                and all(type(name) is str for name in recipe["trajectory_ids"])
                and protocol["seeds"] == list(range(9))
                and [row.get("slot_index") for row in entries] == protocol["seeds"]
                and [row.get("trajectory_id") for row in entries] == recipe["trajectory_ids"]
                and len(set(recipe["trajectory_ids"])) == 9
                and set(recipe["trajectory_ids"]) == set(trajectories),
                "historical trajectory IDs differ from the registered nine-slot roster")
        configuration = manifest.get("config_resolved")
        require(type(configuration) is dict
                and manifest.get("config_sha256") == digest(canonical(configuration)),
                "historical configuration differs from its manifest SHA-256")
        semantic, stochastic = (configuration.get("semantic_seeds"),
                                configuration.get("stochastic_seeds"))
        require(type(semantic) is list and type(stochastic) is list
                and [(row.get("semantic_seed"), row.get("stochastic_seed")) for row in entries]
                == [(name, seed) for name in semantic for seed in stochastic]
                and all(type(row["semantic_seed"]) is str and type(row["stochastic_seed"]) is int
                        and row["trajectory_id"].endswith(
                            f"__{row['semantic_seed']}__s{row['stochastic_seed']}")
                        for row in entries),
                "historical seed labels differ from the resolved three-by-three grid")
        expected_paths = {"config.resolved.yaml", "data/chunks.parquet",
                          "data/trajectories.parquet"}
        for name in recipe["trajectory_ids"]:
            expected_paths.update((f"data/trajectories/{name}.steps.jsonl",
                                   f"data/trajectories/{name}.text",
                                   f"requests/{name}.jsonl"))
        require(set(integrity) == expected_paths,
                "historical manifest omits or adds declared source outputs")
        design = protocol.get("statistical_design", {})
        require(design.get("sample_size") == 9
                and design.get("experimental_unit") == "historical trajectory"
                and any(row.get("digest") == protocol["data"] and row.get("role") == "discovery"
                        and row.get("exposure_policy") == "open"
                        for row in design.get("data_splits", []))
                and protocol["data"] in protocol.get("seen_data", []),
                "historical exposure is not declared as open discovery data")
        rows = state["slots"]
        require(type(rows) is list and len(rows) == 18
                and all(row["status"] == "completed" for row in rows)
                and [row["slot"] for row in rows] == [slot["slot"] for slot in plan["slots"]],
                "historical batch has an incomplete primary/reanalysis roster")
        by_slot = {row["slot"]: row for row in rows}
        details: list[dict[str, Any]] = []
        for entry in entries:
            slot = entry["slot_index"]
            trajectory_id = entry["trajectory_id"]
            require(type(slot) is int and type(trajectory_id) is str
                    and trajectories.get(trajectory_id) == entry["manifest"]
                    and integrity.get(f"data/trajectories/{trajectory_id}.steps.jsonl")
                    == entry["steps_sha256"]
                    and entry["steps_sha256"] in bound["recipe_artifacts"],
                    "historical trajectory differs from source manifest and CAS binding")
            primary = by_slot.get(f"primary:{slot}")
            reanalysis = by_slot.get(f"reanalysis:{slot}")
            require(primary is not None and reanalysis is not None
                    and primary["mode"] == "primary"
                    and reanalysis["mode"] == "independent_reanalysis",
                    "historical trajectory lacks a paired analysis")
            primary_run = Kernel._get(history, primary["run"], "run")
            reanalysis_run = Kernel._get(history, reanalysis["run"], "run")
            primary_result = Kernel._get(history, primary["result"], "result")
            reanalysis_result = Kernel._get(history, reanalysis["result"], "result")
            require(primary_run["payload"]["seed"] == slot
                    and primary_run["payload"]["replicate_of"] is None
                    and reanalysis_run["payload"]["seed"] == slot
                    and reanalysis_run["payload"]["replicate_of"] == primary_run["id"]
                    and primary_result["payload"]["status"] == "completed"
                    and reanalysis_result["payload"]["status"] == "completed",
                    "historical paired runs differ from frozen slot indices")
            primary_outputs = primary_result["payload"]["outputs"]
            reanalysis_outputs = reanalysis_result["payload"]["outputs"]
            raw_key = primary_outputs["raw_data"]
            require(reanalysis_outputs["raw_data"] == raw_key,
                    "historical reanalysis did not retain identical raw data")
            raw = _object(store.read(raw_key), "historical raw trajectory")
            require(set(raw) == {"schema_version", "domain", "manifest_sha256", "trajectory"}
                    and raw["schema_version"] == 1 and raw["domain"] == ADAPTER_ID
                    and raw["manifest_sha256"] == manifest_key
                    and raw["trajectory"] == entry,
                    "observed raw trajectory differs from the frozen historical bundle")
            observed = _steps(store, entry)
            primary_value = _metric(store, primary_outputs["metrics"])
            reanalysis_value = _metric(store, reanalysis_outputs["metrics"])
            require(abs(observed[METRIC] - primary_value) <= NUMERIC_TOLERANCE
                    and abs(observed[METRIC] - reanalysis_value) <= NUMERIC_TOLERANCE,
                    "historical stop rate differs from raw steps")
            details.append(dict(slot_index=slot, trajectory_id=trajectory_id,
                                semantic_seed=entry["semantic_seed"],
                                original_stochastic_seed=entry["stochastic_seed"],
                                raw_data=raw_key, primary_run=primary_run["id"],
                                reanalysis_run=reanalysis_run["id"],
                                **observed))
        rates = [row[METRIC] for row in details]
        return dict(schema_version=1, adapter_id=ADAPTER_ID,
                    adapter_version=ADAPTER_VERSION,
                    statement=("In one selected historical Semantic Afterlife run, stop-event "
                               "counts recomputed from the saved step records matched the legacy "
                               "manifest for all nine previously observed trajectories; "
                               f"per-trajectory stop-event rates ranged from {min(rates):.4f} "
                               f"to {max(rates):.4f}. Capture required this agreement before the "
                               "protocol was registered, so this exploratory description neither "
                               "discriminates the registered explanations nor explains the "
                               "variation."),
                    limitations=[
                        "All nine trajectories and their legacy manifest were seen before this protocol; this is not prospective preregistration.",
                        "Capture rejects any run whose saved steps disagree with its legacy manifest, so the competing inconsistency explanation cannot produce a recorded outcome in this pilot.",
                        "The --seed values are deterministic trajectory slot indices, not fresh stochastic samples.",
                        "The separate reanalysis reads the same historical observations, including the legacy manifest counts it checks against; it is neither blind nor new-data replication.",
                        "Both analysis programs and this adapter come from one local development context; distinct digests do not establish independent authorship.",
                        "finish_reason is provider-reported; the stop-event rate is a protocol diagnostic and does not recompute the S1 semantic gap, whose embeddings are absent from the captured files.",
                        "One historical model/configuration is examined; steps within a trajectory are not independent units, and no causal, population, or cross-model conclusion follows.",
                        "No uncertainty interval, significance test, scientific reviewer verdict, or source-environment closure is supplied.",
                    ],
                    outcome="inconclusive", inference_mode="exploratory",
                    details=dict(domain=ADAPTER_ID, metric=METRIC, n_trajectories=9,
                                 manifest_sha256=manifest_key,
                                 numeric_tolerance=NUMERIC_TOLERANCE,
                                 trajectories=details))
