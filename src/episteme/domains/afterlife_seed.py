"""Freeze and reanalyse one historical Afterlife seed-independence run offline.

The legacy run is already observed. Slot numbers select frozen trajectories;
they are not new random seeds or new-data replication. This module checks byte
identity and reported counters, not the scientific interpretation of the run.
"""

from __future__ import annotations

import base64
from collections import Counter
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
from typing import Any

from ..execution import freeze_environment
from ..store import Store, canonical, digest


DOMAIN_ID = "afterlife_seed_v1"
METRIC = "stop_event_rate"
OUTPUTS = {"raw_data": "raw.json", "metrics": "metrics.json"}
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_STEPS = 10_000
# The analysis adapter rereads each frozen steps file under the same bound.
MAX_STEPS_BYTES = 1024 * 1024
STEP_FIELDS = {"step", "text", "finish_reason", "prompt_tokens", "completion_tokens",
               "cost_usd", "served_provider", "from_cache"}
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_TRAJECTORY = re.compile(r"[A-Za-z0-9_.-]+(?:__[A-Za-z0-9_.-]+)*\Z")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"nonfinite JSON constant: {value}")


def _json(data: bytes, label: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique,
                          parse_constant=_reject_constant)
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc


def _nonnegative(value: Any) -> bool:
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _unsafe(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _relative(name: Any) -> PurePosixPath:
    _require(type(name) is str and bool(name) and "\\" not in name and ":" not in name
             and "\x00" not in name and not PureWindowsPath(name).is_absolute(),
             "unsafe historical output path")
    result = PurePosixPath(name)
    _require(not result.is_absolute() and result.as_posix() == name
             and all(part not in {"", ".", ".."} for part in name.split("/")),
             "unsafe historical output path")
    return result


def _path(root: Path, name: str, *, directory: bool = False) -> Path:
    relative = _relative(name)
    current = root
    parts = relative.parts
    for index, part in enumerate(parts):
        current = current / part
        _require(not _unsafe(current), f"symlink or reparse point in historical output: {name}")
        mode = current.lstat().st_mode
        expected_directory = directory or index < len(parts) - 1
        _require(stat.S_ISDIR(mode) if expected_directory else stat.S_ISREG(mode),
                 f"historical output is not a regular {'directory' if expected_directory else 'file'}: {name}")
    _require(current.resolve().is_relative_to(root), "historical output escapes source root")
    return current


def _read(root: Path, name: str, limit: int) -> bytes:
    path = _path(root, name)
    _require(path.stat().st_size <= limit, f"historical output exceeds size limit: {name}")
    with path.open("rb") as source:
        data = source.read(limit + 1)
    _require(len(data) <= limit, f"historical output exceeds size limit: {name}")
    _require(not _unsafe(path), f"historical output changed to a link: {name}")
    return data


def _steps(data: bytes, trajectory_id: str, record: dict[str, Any]) -> dict[str, Any]:
    lines = data.splitlines()
    _require(0 < len(data) <= MAX_STEPS_BYTES and 0 < len(lines) <= MAX_STEPS
             and all(line.strip() for line in lines),
             f"invalid step count, size or blank record: {trajectory_id}")
    stops = completion = prompt = 0
    providers: Counter[str] = Counter()
    for index, line in enumerate(lines, 1):
        step = _json(line, f"step {index} of {trajectory_id}")
        _require(type(step) is dict and set(step) == STEP_FIELDS
                 and type(step["step"]) is int and step["step"] == index
                 and type(step["text"]) is str and type(step["from_cache"]) is bool
                 and _nonnegative(step["cost_usd"]),
                 f"invalid step record or sequence: {trajectory_id}")
        reason = step["finish_reason"]
        _require(reason is None or type(reason) is str, f"invalid finish reason: {trajectory_id}")
        stops += reason not in (None, "length")
        for field in ("prompt_tokens", "completion_tokens"):
            _require(type(step[field]) is int and step[field] >= 0,
                     f"invalid {field}: {trajectory_id}")
        prompt += step["prompt_tokens"]
        completion += step["completion_tokens"]
        provider = step["served_provider"]
        _require(type(provider) is str and bool(provider.strip()),
                 f"missing served provider: {trajectory_id}")
        providers[provider] += 1
    rate = stops / len(lines)
    _require(type(record.get("n_steps")) is int and record["n_steps"] == len(lines)
             and type(record.get("stop_events")) is int and record["stop_events"] == stops
             and type(record.get("stop_event_rate")) in {int, float}
             and round(rate, 4) == record["stop_event_rate"],
             f"step/stop counters disagree with manifest: {trajectory_id}")
    _require(type(record.get("prompt_tokens_total")) is int and record["prompt_tokens_total"] == prompt
             and type(record.get("completion_tokens_total")) is int
             and record["completion_tokens_total"] == completion,
             f"billed token totals disagree with manifest: {trajectory_id}")
    _require(type(record.get("served_providers")) is dict
             and record["served_providers"] == dict(providers),
             f"served provider counts disagree with manifest: {trajectory_id}")
    # generated_tokens is a local tokenizer quantity and is deliberately not
    # equated to provider-billed completion_tokens_total.
    return {"n_steps": len(lines), "stop_events": stops, METRIC: rate}


# Both programs are frozen one-file standard-library sources for the common
# runner. Their distinct calculations use the same observed trajectory bytes;
# distinct files alone do not prove independent authorship or OS isolation.
PRIMARY_SOURCE = b'''"""Project one previously observed Afterlife trajectory."""
import base64
import hashlib
import json
from pathlib import Path
import sys


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def main():
    if len(sys.argv) != 4 or sys.argv[1:3] != ["input.dat", "--seed"]:
        raise ValueError("expected input.dat --seed SLOT_INDEX")
    slot = int(sys.argv[3])
    bundle = json.loads(Path("input.dat").read_bytes())
    if (bundle.get("schema_version") != 1 or bundle.get("domain") != "afterlife_seed_v1"
            or len(bundle.get("trajectories", [])) != 9 or not 0 <= slot < 9):
        raise ValueError("invalid frozen trajectory bundle or slot")
    entry = bundle["trajectories"][slot]
    if entry["slot_index"] != slot:
        raise ValueError("trajectory slot mismatch")
    steps = base64.b64decode(entry["steps_b64"], validate=True)
    if hashlib.sha256(steps).hexdigest() != entry["steps_sha256"]:
        raise ValueError("trajectory bytes disagree with frozen digest")
    lines = steps.splitlines()
    if not lines:
        raise ValueError("empty trajectory")
    stopped = 0
    for index, line in enumerate(lines, 1):
        row = json.loads(line)
        if row["step"] != index:
            raise ValueError("noncontiguous steps")
        stopped += row["finish_reason"] not in (None, "length")
    rate = stopped / len(lines)
    record = entry["manifest"]
    if (record["n_steps"] != len(lines) or record["stop_events"] != stopped
            or record["stop_event_rate"] != round(rate, 4)):
        raise ValueError("observations disagree with historical manifest")
    raw = {"schema_version": 1, "domain": "afterlife_seed_v1",
           "manifest_sha256": bundle["manifest_sha256"], "trajectory": entry}
    Path("raw.json").write_bytes(encoded(raw))
    Path("metrics.json").write_bytes(encoded({"stop_event_rate": rate}))


if __name__ == "__main__":
    main()
'''


REANALYSIS_SOURCE = b'''"""Recount stops from the frozen primary raw observations."""
import base64
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys


def main():
    if len(sys.argv) != 4 or sys.argv[1:3] != ["input.dat", "--seed"]:
        raise ValueError("expected input.dat --seed SLOT_INDEX")
    slot = int(sys.argv[3])
    original = Path("input.dat").read_bytes()
    raw = json.loads(original)
    if (raw.get("schema_version") != 1 or raw.get("domain") != "afterlife_seed_v1"
            or not 0 <= slot < 9):
        raise ValueError("invalid primary raw observations")
    trajectory = raw["trajectory"]
    if trajectory["slot_index"] != slot:
        raise ValueError("raw observations belong to another slot")
    steps = base64.b64decode(trajectory["steps_b64"], validate=True)
    if hashlib.sha256(steps).hexdigest() != trajectory["steps_sha256"]:
        raise ValueError("raw trajectory digest mismatch")
    counts = Counter()
    for position, line in enumerate(steps.splitlines(), 1):
        row = json.loads(line)
        if row["step"] != position:
            raise ValueError("noncontiguous steps")
        counts[row["finish_reason"]] += 1
    total = sum(counts.values())
    if total == 0:
        raise ValueError("empty trajectory")
    stopped = total - counts[None] - counts["length"]
    rate = stopped / total
    record = trajectory["manifest"]
    if (record["n_steps"] != total or record["stop_events"] != stopped
            or record["stop_event_rate"] != round(rate, 4)):
        raise ValueError("independent recount disagrees with historical manifest")
    Path("raw.json").write_bytes(original)
    Path("metrics.json").write_text(json.dumps({"stop_event_rate": rate},
                                     sort_keys=True, separators=(",", ":"), allow_nan=False),
                                    encoding="utf-8")


if __name__ == "__main__":
    main()
'''


def compile_seed_bundle(store: Store, run_root: str | Path) -> dict[str, Any]:
    """Verify a selected historical run, then freeze all declared output bytes.

    No Afterlife program or provider is called. CAS writes happen only after
    every declared output and trajectory has passed its read-only audit.
    """
    source = Path(run_root).absolute()
    _require(source.is_dir() and not _unsafe(source), "historical run root must be a regular directory")
    for ancestor in source.parents:
        _require(not _unsafe(ancestor), "historical run path traverses a link or reparse point")
    root = source.resolve()
    _require(not store.root.is_relative_to(root), "research state must be outside the historical run")
    manifest_bytes = _read(root, "manifest.json", MAX_MANIFEST_BYTES)
    manifest = _json(manifest_bytes, "historical manifest")
    _require(type(manifest) is dict and manifest.get("status") == "COMPLETED",
             "historical run is not completed")
    integrity = manifest.get("integrity")
    _require(type(integrity) is dict and 10 <= len(integrity) <= 256
             and "config.resolved.yaml" in integrity, "historical integrity inventory is incomplete")
    captured: dict[str, bytes] = {}
    total = 0
    for name, expected in sorted(integrity.items()):
        _relative(name)
        _require(type(expected) is str and _SHA.fullmatch(expected) is not None,
                 f"invalid historical SHA-256: {name}")
        data = _read(root, name, MAX_FILE_BYTES)
        total += len(data)
        _require(total <= MAX_TOTAL_BYTES, "historical outputs exceed total capture limit")
        _require(digest(data) == expected, f"historical output SHA-256 mismatch: {name}")
        captured[name] = data
    configuration = manifest.get("config_resolved")
    trajectories = manifest.get("trajectories")
    totals = manifest.get("totals")
    _require(type(configuration) is dict and type(trajectories) is dict
             and type(totals) is dict, "historical run lacks configuration or trajectory roster")
    _require(type(manifest.get("config_sha256")) is str
             and _SHA.fullmatch(manifest["config_sha256"]) is not None
             and digest(canonical(configuration)) == manifest["config_sha256"],
             "resolved configuration differs from its manifest SHA-256")
    semantic = configuration.get("semantic_seeds")
    stochastic = configuration.get("stochastic_seeds")
    _require(type(semantic) is list and len(semantic) == 3
             and all(type(value) is str and value and "__" not in value for value in semantic)
             and len(set(semantic)) == 3 and type(stochastic) is list and len(stochastic) == 3
             and all(type(value) is int and value >= 0 for value in stochastic)
             and len(set(stochastic)) == 3, "historical seed grid must be three by three")
    grid = [(name, seed) for name in semantic for seed in stochastic]
    _require(len(trajectories) == len(grid) == 9
             and type(totals.get("n_trajectories")) is int and totals["n_trajectories"] == 9
             and type(totals.get("n_completed")) is int and totals["n_completed"] == 9,
             "historical trajectory roster is incomplete")
    ordered: list[dict[str, Any]] = []
    used: set[str] = set()
    for slot, (name, seed) in enumerate(grid):
        suffix = f"__{name}__s{seed}"
        matches = [id for id in trajectories if type(id) is str and id.endswith(suffix)]
        _require(len(matches) == 1, f"missing or ambiguous historical trajectory for {name}/{seed}")
        trajectory_id = matches[0]
        _require(_TRAJECTORY.fullmatch(trajectory_id) is not None and trajectory_id not in used,
                 "unsafe or duplicate historical trajectory ID")
        used.add(trajectory_id)
        record = trajectories[trajectory_id]
        _require(type(record) is dict and record.get("trajectory_id") == trajectory_id
                 and record.get("status") == "COMPLETED", "historical trajectory is not completed")
        path = f"data/trajectories/{trajectory_id}.steps.jsonl"
        _require(path in captured, f"historical steps are absent from integrity inventory: {trajectory_id}")
        _steps(captured[path], trajectory_id, record)
        ordered.append(dict(slot_index=slot, trajectory_id=trajectory_id,
                            semantic_seed=name, stochastic_seed=seed,
                            steps_sha256=integrity[path],
                            steps_b64=base64.b64encode(captured[path]).decode("ascii"),
                            manifest=record))
    _require(set(trajectories) == used, "historical manifest contains unaccounted trajectories")
    expected_inventory = {"config.resolved.yaml", "data/chunks.parquet",
                          "data/trajectories.parquet"}
    for trajectory_id in used:
        expected_inventory.update({
            f"data/trajectories/{trajectory_id}.steps.jsonl",
            f"data/trajectories/{trajectory_id}.text",
            f"requests/{trajectory_id}.jsonl",
        })
    _require(set(integrity) == expected_inventory and len(integrity) == 30,
             "historical integrity inventory omits or adds a selected-run output")
    actual_inventory = {"config.resolved.yaml"}
    for directory, suffixes in (("data", (".parquet",)),
                                ("data/trajectories", (".steps.jsonl", ".text")),
                                ("requests", (".jsonl",))):
        for item in _path(root, directory, directory=True).iterdir():
            if item.name.endswith(suffixes):
                name = f"{directory}/{item.name}"
                _path(root, name)  # Reject unlisted links and non-regular files too.
                actual_inventory.add(name)
    _require(actual_inventory == expected_inventory,
             "historical run has a missing or unlisted trajectory, request, or parquet output")
    manifest_sha256 = digest(manifest_bytes)
    bundle = dict(schema_version=1, domain=DOMAIN_ID, manifest_sha256=manifest_sha256,
                  trajectories=ordered)
    bundle_bytes = canonical(bundle)
    _require(len(bundle_bytes) <= MAX_BUNDLE_BYTES, "historical input bundle exceeds size limit")
    for name, data in captured.items():
        _require(store.put(data) == integrity[name], f"CAS capture mismatch: {name}")
    _require(store.put(manifest_bytes) == manifest_sha256, "manifest CAS capture mismatch")
    data_digest = store.put(bundle_bytes)
    implementation = store.put(PRIMARY_SOURCE)
    reanalysis_implementation = store.put(REANALYSIS_SOURCE)
    environment = freeze_environment(store)
    recipe = dict(schema_version=1, domain=DOMAIN_ID, data=data_digest,
                  manifest_sha256=manifest_sha256,
                  trajectory_ids=[entry["trajectory_id"] for entry in ordered])
    return dict(implementation=implementation,
                reanalysis_implementation=reanalysis_implementation,
                environment=environment, reanalysis_environment=environment,
                data=data_digest, recipe=recipe,
                recipe_artifacts=sorted({manifest_sha256, *integrity.values()}),
                outputs=OUTPUTS.copy(), wall_seconds=30, max_output_bytes=8 * 1024 * 1024,
                required_capabilities=[], seeds=list(range(9)), metric=METRIC,
                trajectory_ids=recipe["trajectory_ids"], manifest_digest=manifest_sha256,
                config_digest=integrity["config.resolved.yaml"],
                integrity_count=len(integrity), captured_bytes=total)
