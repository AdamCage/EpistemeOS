"""An offline synthetic fixture that executes real, fixed CPU programs.

The named actors demonstrate separation of duties. They are not independent AI
agents, and this fixture deliberately stops before scientific review.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .kernel import Actor, Kernel
from .store import Store
from .reporting import FIXTURE_NOTICE, export_store, inspect_store
from .search import Search


PRIMARY_SOURCE = b'''import csv, io, json, os, random, sys
config = json.loads(sys.argv[1])
seed = int(sys.argv[2])
rng = random.Random(seed)
points = []
for i in range(config["samples"]):
    x = -1.0 + 2.0 * i / (config["samples"] - 1)
    y = config["intercept"] + config["slope"] * x + rng.gauss(0.0, config["noise_sd"])
    points.append((x, y))
output = io.StringIO(newline="")
writer = csv.writer(output, lineterminator="\\n")
writer.writerow(["x", "y"])
writer.writerows(points)
x_mean = sum(x for x, y in points) / len(points)
y_mean = sum(y for x, y in points) / len(points)
slope = sum((x-x_mean)*(y-y_mean) for x,y in points) / sum((x-x_mean)**2 for x,y in points)
print(json.dumps({"raw_data": output.getvalue(), "metrics": {"slope": slope, "samples": len(points), "seed": seed}, "pid": os.getpid()}, allow_nan=False))
'''


REANALYSIS_SOURCE = b'''import csv, io, json, math, os, sys
raw = sys.argv[1]
rows = list(csv.DictReader(io.StringIO(raw)))
xs = [float(row["x"]) for row in rows]
ys = [float(row["y"]) for row in rows]
n = len(xs)
sx, sy = math.fsum(xs), math.fsum(ys)
sxx = math.fsum(x*x for x in xs)
sxy = math.fsum(x*y for x,y in zip(xs,ys))
slope = (n*sxy - sx*sy) / (n*sxx - sx*sx)
print(json.dumps({"raw_data": raw, "metrics": {"slope": slope, "samples": n, "seed": int(sys.argv[2])}, "pid": os.getpid()}, allow_nan=False))
'''




def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _execute_fixture(store: Store, kernel: Kernel, *, protocol: str, seed: int,
                     source_key: str, environment: str, input_text: str,
                     replicate_of: str | None = None) -> tuple[str, dict[str, str]]:
    """Execute only this module's two fixed programs, never arbitrary stored code."""
    source = store.read(source_key)
    expected = REANALYSIS_SOURCE if replicate_of else PRIMARY_SOURCE
    if source != expected:
        raise ValueError("demo execution accepts only its fixed source bytes")
    command = [sys.executable, "-I", "-c", source.decode("utf-8"), input_text, str(seed)]
    run = kernel.start_run(protocol, seed=seed, implementation=source_key,
                           environment=environment, command=command, replicate_of=replicate_of)
    log: dict[str, Any] = dict(synthetic_fixture=True, source_sha256=source_key,
                               environment_sha256=environment, started_at=_timestamp(),
                               command=command, parent_pid=None)
    started = time.monotonic()
    try:
        process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                 timeout=20, check=False)
        log.update(returncode=process.returncode, stdout=process.stdout, stderr=process.stderr)
        if process.returncode:
            raise RuntimeError(f"fixed demo program exited {process.returncode}")
        result = json.loads(process.stdout)
        if not isinstance(result.get("raw_data"), str) or not isinstance(result.get("metrics"), dict):
            raise ValueError("fixed demo program produced malformed outputs")
        log["child_pid"] = result["pid"]
        outputs = {"raw_data": store.put(result["raw_data"].encode("utf-8")),
                   "metrics": store.put_json(result["metrics"])}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, RuntimeError) as exc:
        log.update(finished_at=_timestamp(), elapsed_seconds=time.monotonic() - started,
                   error=str(exc))
        outputs = {"log": store.put_json(log)}
        kernel.finish_run(run, status="failed", outputs=outputs, reason=str(exc))
        raise RuntimeError(f"synthetic run failed: {run}: {exc}") from exc
    log.update(finished_at=_timestamp(), elapsed_seconds=time.monotonic() - started)
    outputs["log"] = store.put_json(log)
    kernel.finish_run(run, status="completed", outputs=outputs)
    return run, outputs


def run_demo(root: str | Path, *, with_search: bool = False) -> dict[str, Any]:
    """Create a new demonstration, refusing to mix it into any nonempty folder."""
    root = Path(root).resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError("demo root must be absent or empty; existing evidence is never replaced")
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".demo-in-progress"
    # Exclusive creation prevents two demo invocations from populating one root.
    with lock.open("x", encoding="utf-8") as stream:
        stream.write("EpistemeOS synthetic fixture\n")
    try:
        with Store(root) as store:
            planner = Kernel(store, Actor("fixture-planner", "planner"))
            executor = Kernel(store, Actor("fixture-executor", "executor"))
            replicator = Kernel(store, Actor("fixture-reanalyst", "replicator"))
            analyst = Kernel(store, Actor("fixture-analyst", "analyst"))
            scope = {"mode": "synthetic_demo", "population": "three seeded 64-row synthetic datasets",
                     "inference": "sample regression slope only; no real-world discovery"}
            signal = planner.hypothesis(
                "The fixture's computed slope recovers the deliberately planted positive signal.",
                "Every registered sample has OLS slope greater than 1.0.",
                "Any registered sample has slope at most 1.0.", scope)
            null = planner.hypothesis(
                "The sample relation is explained by seed noise with slope near zero.",
                "At least one registered sample has absolute OLS slope at most 0.5.",
                "Every registered sample has absolute slope greater than 0.5.", scope)
            source_key = store.put(PRIMARY_SOURCE)
            replica_source = store.put(REANALYSIS_SOURCE)
            inputs = {"synthetic": True, "samples": 64, "intercept": 1.0,
                      "slope": 2.0, "noise_sd": 0.25}
            data = store.put_json(inputs)
            runtime = {"fixture": True, "python": sys.version, "implementation": platform.python_implementation(),
                       "executable": sys.executable, "platform": platform.platform(),
                       "machine": platform.machine(), "dependencies": "Python standard library only",
                       "isolation": "fresh Python subprocess with -I; not an adversarial sandbox",
                       "primary_source_sha256": source_key, "reanalysis_source_sha256": replica_source}
            environment = store.put_json(runtime)
            protocol = planner.preregister(
                hypotheses=[signal, null], scope=scope,
                design="Generate 64 x/y pairs for each of seeds 17, 41, 73 from the frozen synthetic configuration.",
                metric="slope",
                analysis_plan="Estimate OLS with intercept by centered covariance. A separate implementation "
                              "reanalyzes each raw CSV using cross-product sums. Report all sample slopes; "
                              "support the planted-signal prediction only if every slope exceeds 1.0. "
                              "Do not report significance or scientific discovery from this synthetic fixture.",
                stopping_rule="Exactly three primary computations and one reanalysis per primary; no adaptive retries.",
                seeds=[17, 41, 73], run_limit=6, implementation=source_key,
                environment=environment, data=data, replication_tolerance=1e-10)
            search_state = None
            if with_search:
                search = Search(store, planner.actor)
                tournament = search.register_tournament(candidates=[signal, null], seed=17,
                    rubric="fixture-v1: prioritize a testable positive-signal explanation; not truth", rounds=2)
                judge = Search(store, Actor("fixture-priority-judge", "judge"))
                for pairing in search.pairings(tournament):
                    judge.ballot(tournament, **pairing,
                        verdict="a" if pairing["a"] == signal else "b",
                        rationale="Fixed scheduling fixture: prefer the signal candidate; empirical claims still require runs.",
                        judge={"model": "scripted-fixture", "model_version": "1", "prompt_version": "fixture-v1"})
                alternative_inputs = dict(inputs, noise_sd=4.0)
                alternative = dict(Kernel._get(store.events(), protocol, "protocol")["payload"])
                alternative.update(data=store.put_json(alternative_inputs),
                    design="Same registered seeds under a high-noise sensitivity condition; separate frozen protocol.")
                alternative_protocol = planner.preregister(**alternative)
                tree = search.register_tree(weights=dict(discrimination=1.0, uncertainty=0.3,
                    coverage=0.2, invalidity_risk=0.5), cost_weight=0.01, budget=6,
                    cost_unit="fixture_subprocess_count", max_nodes=2, max_depth=0,
                    max_width=2, max_selections=2, seed=17, tournament=tournament)
                main_node = search.add_node(tree, protocol=protocol, action="discriminate",
                    components=dict(discrimination=0.9, uncertainty=0.5, coverage=1.0, invalidity_risk=0.0),
                    estimated_cost=6, rationale="Fixed fixture estimate: low noise more clearly separates sample slopes.")
                search.add_node(tree, protocol=alternative_protocol, action="robustness",
                    components=dict(discrimination=0.3, uncertainty=0.7, coverage=0.5, invalidity_risk=0.2),
                    estimated_cost=6, rationale="High-noise follow-up retained in the frontier; full cost does not fit after first node.")
                selection = search.select_next(tree)
                if selection["node"] != main_node:
                    raise RuntimeError("fixed search fixture did not select its registered main protocol")
                search_state = (search, tournament, tree, selection)
            evidence: list[str] = []
            slopes: list[float] = []
            for seed in (17, 41, 73):
                primary, outputs = _execute_fixture(
                    store, executor, protocol=protocol, seed=seed, source_key=source_key,
                    environment=environment, input_text=store.read(data).decode("utf-8"))
                slopes.append(json.loads(store.read(outputs["metrics"]))["slope"])
                reanalysis, _ = _execute_fixture(
                    store, replicator, protocol=protocol, seed=seed, source_key=replica_source,
                    environment=environment, input_text=store.read(outputs["raw_data"]).decode("utf-8"),
                    replicate_of=primary)
                evidence.extend((primary, reanalysis))
            supported = all(slope > 1.0 for slope in slopes)
            claim = analyst.claim(
                protocol=protocol,
                statement=("All three registered synthetic samples have slope greater than 1.0."
                           if supported else "The registered synthetic results do not meet the planted-signal criterion."),
                scope=scope, evidence=evidence, outcome="supports" if supported else "inconclusive",
                limitations=[FIXTURE_NOTICE, "The generator intentionally plants the signal; this is not a novel finding.",
                             "Three seeds exercise the workflow and do not establish general statistical reliability.",
                             "No scientific review has been performed; no reviewer approval is fabricated."])
            if search_state:
                search, tournament, tree, selection = search_state
                search.finish_selection(selection["id"], status="completed", actual_cost=len(evidence),
                    reason="All six registered fixture subprocesses completed; raw result claim remains unreviewed.",
                    run=evidence[0], claim=claim)
                search.select_next(tree)  # Persist the budget stop and retained alternative.
            summary = inspect_store(store)
            summary["claim"] = claim
            if search_state:
                summary["search"] = dict(tournament=search.ranking(tournament), tree=search.tree_state(tree))
            export_store(store)
            return summary
    finally:
        lock.unlink(missing_ok=True)
