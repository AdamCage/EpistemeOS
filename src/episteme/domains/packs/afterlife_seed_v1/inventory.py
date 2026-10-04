"""Pure re-verification of one captured historical Afterlife run.

``verify`` takes the captured files as bytes, never paths. It repeats the
checks of the legacy ``afterlife_seed.compile_seed_bundle`` that do not need
the filesystem and rebuilds the same ``input.dat`` bundle bytes. Agreement with
the legacy manifest is a capture precondition, not an experimental outcome.
"""

import base64
from collections import Counter
import hashlib
import json
import math
import re


DOMAIN_ID = "afterlife_seed_v1"
METRIC = "stop_event_rate"
OUTPUTS = {"raw_data": "raw.json", "metrics": "metrics.json"}
MANIFEST_PATH = "manifest.json"
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_STEPS = 10_000
MAX_STEPS_BYTES = 1024 * 1024
STEP_FIELDS = {"step", "text", "finish_reason", "prompt_tokens", "completion_tokens",
               "cost_usd", "served_provider", "from_cache"}
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_TRAJECTORY = re.compile(r"[A-Za-z0-9_.-]+(?:__[A-Za-z0-9_.-]+)*\Z")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def strict_json(data, label):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject(value):
        raise ValueError(f"nonfinite JSON constant: {value}")

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=unique, parse_constant=reject)
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc


def nonnegative(value):
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def relative(name):
    require(type(name) is str and bool(name) and "\\" not in name and ":" not in name
            and "\x00" not in name and not name.startswith("/")
            and all(part not in {"", ".", ".."} for part in name.split("/")),
            "unsafe historical output path")
    return name


def steps(data, trajectory_id, record):
    """Recount one saved trajectory and require its legacy manifest counters."""
    lines = data.splitlines()
    require(0 < len(data) <= MAX_STEPS_BYTES and 0 < len(lines) <= MAX_STEPS
            and all(line.strip() for line in lines),
            f"invalid step count, size or blank record: {trajectory_id}")
    stops = completion = prompt = 0
    providers = Counter()
    for index, line in enumerate(lines, 1):
        step = strict_json(line, f"step {index} of {trajectory_id}")
        require(type(step) is dict and set(step) == STEP_FIELDS
                and type(step["step"]) is int and step["step"] == index
                and type(step["text"]) is str and type(step["from_cache"]) is bool
                and nonnegative(step["cost_usd"]),
                f"invalid step record or sequence: {trajectory_id}")
        reason = step["finish_reason"]
        require(reason is None or type(reason) is str, f"invalid finish reason: {trajectory_id}")
        stops += reason not in (None, "length")
        for field in ("prompt_tokens", "completion_tokens"):
            require(type(step[field]) is int and step[field] >= 0, f"invalid {field}: {trajectory_id}")
        prompt += step["prompt_tokens"]
        completion += step["completion_tokens"]
        provider = step["served_provider"]
        require(type(provider) is str and bool(provider.strip()),
                f"missing served provider: {trajectory_id}")
        providers[provider] += 1
    rate = stops / len(lines)
    require(type(record.get("n_steps")) is int and record["n_steps"] == len(lines)
            and type(record.get("stop_events")) is int and record["stop_events"] == stops
            and type(record.get("stop_event_rate")) in {int, float}
            and round(rate, 4) == record["stop_event_rate"],
            f"step/stop counters disagree with manifest: {trajectory_id}")
    require(type(record.get("prompt_tokens_total")) is int and record["prompt_tokens_total"] == prompt
            and type(record.get("completion_tokens_total")) is int
            and record["completion_tokens_total"] == completion,
            f"billed token totals disagree with manifest: {trajectory_id}")
    require(type(record.get("served_providers")) is dict
            and record["served_providers"] == dict(providers),
            f"served provider counts disagree with manifest: {trajectory_id}")
    return {"n_steps": len(lines), "stop_events": stops, METRIC: rate}


def expected_inventory(trajectory_ids):
    paths = {"config.resolved.yaml", "data/chunks.parquet", "data/trajectories.parquet"}
    for name in trajectory_ids:
        paths.update((f"data/trajectories/{name}.steps.jsonl", f"data/trajectories/{name}.text",
                      f"requests/{name}.jsonl"))
    return paths


def verify(files):
    """Check captured manifest and outputs; return the trajectory roster and bundle."""
    require(MANIFEST_PATH in files, "capture lacks the historical manifest")
    manifest_bytes = files[MANIFEST_PATH]
    require(len(manifest_bytes) <= MAX_MANIFEST_BYTES, "historical manifest exceeds size limit")
    manifest = strict_json(manifest_bytes, "historical manifest")
    require(type(manifest) is dict and manifest.get("status") == "COMPLETED",
            "historical run is not completed")
    run_id = manifest.get("run_id")
    require(type(run_id) is str and 0 < len(run_id) <= 256 and run_id.strip() == run_id,
            "historical run lacks a run_id")
    integrity = manifest.get("integrity")
    require(type(integrity) is dict and 10 <= len(integrity) <= 256
            and "config.resolved.yaml" in integrity, "historical integrity inventory is incomplete")
    require(set(files) == set(integrity) | {MANIFEST_PATH},
            "capture holds files other than the declared outputs and manifest")
    total = 0
    for name, expected in sorted(integrity.items()):
        relative(name)
        require(type(expected) is str and _SHA.fullmatch(expected) is not None,
                f"invalid historical SHA-256: {name}")
        data = files[name]
        require(len(data) <= MAX_FILE_BYTES, f"historical output exceeds size limit: {name}")
        total += len(data)
        require(total <= MAX_TOTAL_BYTES, "historical outputs exceed total capture limit")
        require(sha256(data) == expected, f"historical output SHA-256 mismatch: {name}")
    configuration = manifest.get("config_resolved")
    trajectories = manifest.get("trajectories")
    totals = manifest.get("totals")
    require(type(configuration) is dict and type(trajectories) is dict and type(totals) is dict,
            "historical run lacks configuration or trajectory roster")
    require(type(manifest.get("config_sha256")) is str
            and _SHA.fullmatch(manifest["config_sha256"]) is not None
            and sha256(canonical(configuration)) == manifest["config_sha256"],
            "resolved configuration differs from its manifest SHA-256")
    semantic = configuration.get("semantic_seeds")
    stochastic = configuration.get("stochastic_seeds")
    require(type(semantic) is list and len(semantic) == 3
            and all(type(value) is str and value and "__" not in value for value in semantic)
            and len(set(semantic)) == 3 and type(stochastic) is list and len(stochastic) == 3
            and all(type(value) is int and value >= 0 for value in stochastic)
            and len(set(stochastic)) == 3, "historical seed grid must be three by three")
    grid = [(name, seed) for name in semantic for seed in stochastic]
    require(len(trajectories) == len(grid) == 9
            and type(totals.get("n_trajectories")) is int and totals["n_trajectories"] == 9
            and type(totals.get("n_completed")) is int and totals["n_completed"] == 9,
            "historical trajectory roster is incomplete")
    ordered = []
    used = set()
    observed_steps = 0
    for slot, (name, seed) in enumerate(grid):
        suffix = f"__{name}__s{seed}"
        matches = [key for key in trajectories if type(key) is str and key.endswith(suffix)]
        require(len(matches) == 1, f"missing or ambiguous historical trajectory for {name}/{seed}")
        trajectory_id = matches[0]
        require(_TRAJECTORY.fullmatch(trajectory_id) is not None and trajectory_id not in used,
                "unsafe or duplicate historical trajectory ID")
        used.add(trajectory_id)
        record = trajectories[trajectory_id]
        require(type(record) is dict and record.get("trajectory_id") == trajectory_id
                and record.get("status") == "COMPLETED", "historical trajectory is not completed")
        path = f"data/trajectories/{trajectory_id}.steps.jsonl"
        require(path in integrity, f"historical steps are absent from integrity inventory: {trajectory_id}")
        observed_steps += steps(files[path], trajectory_id, record)["n_steps"]
        ordered.append(dict(slot_index=slot, trajectory_id=trajectory_id, semantic_seed=name,
                            stochastic_seed=seed, steps_sha256=integrity[path],
                            steps_b64=base64.b64encode(files[path]).decode("ascii"),
                            manifest=record))
    require(set(trajectories) == used, "historical manifest contains unaccounted trajectories")
    require(set(integrity) == expected_inventory(used) and len(integrity) == 30,
            "historical integrity inventory omits or adds a selected-run output")
    manifest_sha256 = sha256(manifest_bytes)
    bundle = canonical(dict(schema_version=1, domain=DOMAIN_ID, manifest_sha256=manifest_sha256,
                            trajectories=ordered))
    require(len(bundle) <= MAX_BUNDLE_BYTES, "historical input bundle exceeds size limit")
    return dict(run_id=run_id, manifest=manifest, manifest_sha256=manifest_sha256,
                integrity=integrity, trajectories=ordered, bundle=bundle,
                config_digest=integrity["config.resolved.yaml"], captured_bytes=total,
                observed_steps=observed_steps)
