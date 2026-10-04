"""A historical nine-trace fixture exercises the offline Afterlife pilot.

The fixture is deliberately tiny and contains no scientific evidence. It has
the selected run's 30-output layout so missing legacy bytes cannot be quietly
treated as a complete historical dataset.
"""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from episteme.analysis_controller import advance_batch_analysis
from episteme.batch import _index as batch_index
from episteme.batch_controller import advance_batch
from episteme.domains.afterlife_seed import compile_seed_bundle
from episteme.domains.afterlife_seed_batch_analysis import (
    AfterlifeSeedBatchAnalysisAdapter, _steps as reread_steps,
)
from episteme.graph import ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.reporting import artifact_inventory
from episteme.store import IntegrityError, Store, canonical, digest


SEMANTIC_SEEDS = ("biology", "surreal", "war")
STOCHASTIC_SEEDS = (1, 2, 3)
TRACE = "or-qwen3-8b__W4096__T1__biology__s1"
TRACE_STEPS = f"data/trajectories/{TRACE}.steps.jsonl"
REPO = Path(__file__).resolve().parents[1]
# Analysis replays every batch receipt; nine paired slots take about a minute locally.
CLI_TIMEOUT = 600


def _jsonl(*rows) -> bytes:
    return b"".join(row if type(row) is bytes else canonical(row) + b"\n" for row in rows)


class AfterlifeSeedBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="episteme-afterlife-seed-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "historical-run"
        self.source.mkdir()
        self.manifest = self._build_historical_fixture()
        self.env = dict(os.environ, PYTHONPATH=str(REPO / "src"))

    def _write(self, name: str, data: bytes) -> None:
        target = self.source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def _source_bytes(self) -> dict[str, bytes]:
        return {path.relative_to(self.source).as_posix(): path.read_bytes()
                for path in self.source.rglob("*") if path.is_file()}

    def _restore(self, original: dict[str, bytes]) -> None:
        for name, data in original.items():
            (self.source / name).write_bytes(data)

    def _rewrite_trace(self, data: bytes, **record) -> None:
        """Replace one saved trace and re-declare its hash so later checks are reached."""
        manifest = json.loads(canonical(self.manifest))
        manifest["integrity"][TRACE_STEPS] = digest(data)
        manifest["trajectories"][TRACE].update(record)
        self._write(TRACE_STEPS, data)
        self._write("manifest.json", canonical(manifest))

    def _trace_rows(self) -> list[dict]:
        return [json.loads(line) for line in (self.source / TRACE_STEPS).read_bytes().splitlines()]

    def _prepare(self, state: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPO / "examples" / "afterlife_historical_pilot.py"),
             "prepare", "--source-run", str(self.source), "--root", str(state)],
            cwd=REPO, env=self.env, capture_output=True, text=True, timeout=120)

    def _cli(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "episteme", *arguments], cwd=REPO,
                              env=self.env, capture_output=True, text=True,
                              timeout=CLI_TIMEOUT, check=check)

    def _build_historical_fixture(self) -> dict:
        integrity: dict[str, str] = {}

        def output(name: str, data: bytes) -> None:
            self._write(name, data)
            integrity[name] = digest(data)

        output("config.resolved.yaml", b"stage: s1\nname: tiny-historical-fixture\n")
        output("data/chunks.parquet", b"PAR1fixture-chunksPAR1")
        output("data/trajectories.parquet", b"PAR1fixture-summaryPAR1")
        trajectories = {}
        for slot, (semantic, stochastic) in enumerate(
                (semantic, stochastic)
                for semantic in SEMANTIC_SEEDS for stochastic in STOCHASTIC_SEEDS):
            trajectory_id = f"or-qwen3-8b__W4096__T1__{semantic}__s{stochastic}"
            # Rates range from 0 to 1. The unweighted trajectory mean is not
            # automatically the pooled per-step fraction.
            reasons = ("length", "length") if slot == 0 else (
                ("stop", "stop") if slot == 8 else ("length", "stop"))
            steps = []
            requests = []
            for index, reason in enumerate(reasons):
                text = f"fixture trace {slot}/{index}"
                step = dict(step=index + 1, text=text, finish_reason=reason,
                            prompt_tokens=10 + index, completion_tokens=5 + index,
                            cost_usd=0.0001, served_provider="Alibaba", from_cache=False)
                steps.append(step)
                requests.append(dict(step=index,
                    request=dict(model_id="fixture/qwen3-8b", is_chat=False,
                                 max_tokens=1024, temperature=1.0, top_p=1.0,
                                 seed=slot * 100 + index, provider_slug="alibaba",
                                 allow_fallbacks=False, service_tier=None,
                                 prompt_sha256="0" * 64, prompt_chars=0,
                                 prompt_head="", prompt_tail=""),
                    response=dict(finish_reason=reason, served_provider="Alibaba",
                                  model_returned="fixture/qwen3-8b", latency_s=0.0,
                                  attempts=1, from_cache=False,
                                  usage=dict(prompt_tokens=step["prompt_tokens"],
                                             completion_tokens=step["completion_tokens"],
                                             total_tokens=step["prompt_tokens"]
                                                 + step["completion_tokens"],
                                             cost_usd=step["cost_usd"], cost_rub=None,
                                             cached_tokens=0, from_cache=False),
                                  text=text, logprobs=None)))
            steps_bytes = b"".join(canonical(row) + b"\n" for row in steps)
            requests_bytes = b"".join(canonical(row) + b"\n" for row in requests)
            output(f"data/trajectories/{trajectory_id}.steps.jsonl", steps_bytes)
            output(f"data/trajectories/{trajectory_id}.text",
                   f"Original fixture text for {trajectory_id}\n".encode())
            output(f"requests/{trajectory_id}.jsonl", requests_bytes)
            stop_events = sum(reason != "length" for reason in reasons)
            trajectories[trajectory_id] = dict(
                trajectory_id=trajectory_id, status="COMPLETED", error=None,
                n_steps=len(steps), stop_events=stop_events,
                stop_event_rate=round(stop_events / len(steps), 4),
                prompt_tokens_total=sum(row["prompt_tokens"] for row in steps),
                completion_tokens_total=sum(row["completion_tokens"] for row in steps),
                # Legacy generated_tokens uses a local tokenizer, not billed
                # completion tokens. The fixture keeps the quantities distinct.
                generated_tokens=100 + slot, n_chunks=1, empty_completions=0,
                roundtrip_failures=0, served_providers={"Alibaba": len(steps)},
                cost_usd=0.0002)

        configuration = dict(stage="s1", name="tiny-historical-fixture",
                             generators=[{"model_id": "fixture/qwen3-8b"}],
                             semantic_seeds=list(SEMANTIC_SEEDS),
                             stochastic_seeds=list(STOCHASTIC_SEEDS))
        manifest = dict(run_id="historical-nine-fixture", stage="s1",
                        status="COMPLETED", execution_mode="live",
                        config_resolved=configuration,
                        config_sha256=digest(canonical(configuration)),
                        integrity=integrity, trajectories=trajectories,
                        totals={"n_trajectories": 9, "n_completed": 9})
        self._write("manifest.json", canonical(manifest))
        self._write("STATUS", b"COMPLETED")
        self._write("events.jsonl", b'{"event":"run.finished"}\n')
        self._write("logs/run.log", b"fixture only\n")
        self.assertEqual(len(integrity), 30)
        return manifest

    def test_complete_roster_is_frozen_byte_for_byte_without_touching_source(self):
        before = self._source_bytes()
        with Store(self.root / "state") as store:
            compiled = compile_seed_bundle(store, self.source)
            self.assertEqual(compiled["integrity_count"], 30)
            self.assertEqual(compiled["seeds"], list(range(9)))
            self.assertEqual(compiled["recipe"]["manifest_sha256"], digest(before["manifest.json"]))
            self.assertEqual(set(compiled["recipe_artifacts"]),
                             {digest(data) for name, data in before.items()
                              if name in self.manifest["integrity"]} | {digest(before["manifest.json"])})
            for name, sha256 in self.manifest["integrity"].items():
                self.assertEqual(store.read(sha256), before[name], name)
            self.assertEqual(store.read(compiled["manifest_digest"]), before["manifest.json"])
            bundle = json.loads(store.read(compiled["data"]))
            self.assertEqual(len(bundle["trajectories"]), 9)
            for slot, entry in enumerate(bundle["trajectories"]):
                self.assertEqual(entry["slot_index"], slot)
                raw = before[f'data/trajectories/{entry["trajectory_id"]}.steps.jsonl']
                self.assertEqual(base64.b64decode(entry["steps_b64"]), raw)
                self.assertEqual(store.read(entry["steps_sha256"]), raw)
            self.assertNotEqual(compiled["implementation"], compiled["reanalysis_implementation"])
            self.assertEqual(self._source_bytes(), before)
            first = next(iter(self.manifest["integrity"]))
            (self.source / first).write_bytes(b"changed after capture")
            self.assertEqual(store.read(self.manifest["integrity"][first]), before[first])

    def test_corrupt_missing_or_undeclared_historical_output_is_rejected_before_capture(self):
        with Store(self.root / "state") as store:
            original = self._source_bytes()
            changed = "data/trajectories.parquet"
            (self.source / changed).write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                compile_seed_bundle(store, self.source)
            (self.source / changed).write_bytes(original[changed])
            self.assertEqual(list(store.blobs.iterdir()), [])

            missing = "requests/or-qwen3-8b__W4096__T1__biology__s1.jsonl"
            (self.source / missing).unlink()
            with self.assertRaises((OSError, ValueError)):
                compile_seed_bundle(store, self.source)
            (self.source / missing).write_bytes(original[missing])
            self.assertEqual(list(store.blobs.iterdir()), [])

            # A physically present output omitted from the manifest's hash
            # inventory must also fail. Checking only listed hashes is not a
            # complete historical capture.
            for omitted in ("data/trajectories/or-qwen3-8b__W4096__T1__biology__s1.text",
                            missing, "data/chunks.parquet"):
                with self.subTest(omitted=omitted):
                    manifest = json.loads(original["manifest.json"])
                    manifest["integrity"].pop(omitted)
                    (self.source / "manifest.json").write_bytes(canonical(manifest))
                    with self.assertRaises(ValueError):
                        compile_seed_bundle(store, self.source)
                    self.assertEqual(list(store.blobs.iterdir()), [])
            (self.source / "manifest.json").write_bytes(original["manifest.json"])

    def test_malformed_step_records_are_rejected_before_capture(self):
        original = self._source_bytes()
        first, second = self._trace_rows()
        encoded = canonical(first)
        cases = {
            "extra field": (_jsonl(dict(first, logprobs=None), second), "invalid step record"),
            "missing field": (_jsonl({key: value for key, value in first.items()
                                      if key != "from_cache"}, second), "invalid step record"),
            "text type": (_jsonl(dict(first, text=5), second), "invalid step record"),
            "cache flag type": (_jsonl(dict(first, from_cache=0), second), "invalid step record"),
            "negative cost": (_jsonl(dict(first, cost_usd=-0.0001), second), "invalid step record"),
            "string cost": (_jsonl(dict(first, cost_usd="0.0001"), second), "invalid step record"),
            "overflowing cost": (_jsonl(dict(first, cost_usd=10 ** 400), second),
                                 "invalid step record"),
            "boolean step": (_jsonl(dict(first, step=True), second), "invalid step record"),
            "noncontiguous step": (_jsonl(first, dict(second, step=3)), "invalid step record"),
            "finish reason type": (_jsonl(dict(first, finish_reason=7), second),
                                   "invalid finish reason"),
            "negative tokens": (_jsonl(dict(first, prompt_tokens=-1), second),
                                "invalid prompt_tokens"),
            "boolean tokens": (_jsonl(dict(first, completion_tokens=True), second),
                               "invalid completion_tokens"),
            "blank provider": (_jsonl(dict(first, served_provider=" "), second),
                               "missing served provider"),
            "nonfinite constant": (_jsonl(encoded.replace(b'"cost_usd":0.0001', b'"cost_usd":NaN')
                                          + b"\n", second), "nonfinite JSON constant"),
            "duplicate key": (_jsonl(b'{"step":1,' + encoded[1:] + b"\n", second),
                              "duplicate JSON key"),
            "invalid UTF-8": (_jsonl(encoded.replace(b"fixture", b"\xff") + b"\n", second),
                              "invalid step 1"),
            "blank record": (_jsonl(first) + b"\n" + _jsonl(second), "blank record"),
            "oversized trace": (_jsonl(dict(first, text="x" * (1024 * 1024)), second), "size"),
        }
        with Store(self.root / "state") as store:
            for label, (data, message) in cases.items():
                with self.subTest(label=label):
                    self._rewrite_trace(data)
                    try:
                        with self.assertRaisesRegex(ValueError, message):
                            compile_seed_bundle(store, self.source)
                        self.assertEqual(list(store.blobs.iterdir()), [])
                    finally:
                        self._restore(original)
            self.assertEqual(self._source_bytes(), original)
            self.assertEqual(compile_seed_bundle(store, self.source)["integrity_count"], 30)

    def test_manifest_counters_must_match_saved_steps_before_capture(self):
        original = self._source_bytes()
        first, second = self._trace_rows()
        cases = {
            "stop count": (_jsonl(dict(first, finish_reason="stop"), second), {},
                           "step/stop counters"),
            "step count": (_jsonl(first, second, dict(second, step=3)), {}, "step/stop counters"),
            "rounded rate": ((self.source / TRACE_STEPS).read_bytes(),
                             dict(stop_event_rate=0.0001), "step/stop counters"),
            "billed tokens": (_jsonl(dict(first, completion_tokens=7), second), {},
                              "billed token totals"),
            "served provider": (_jsonl(dict(first, served_provider="Other"), second), {},
                                "served provider counts"),
        }
        with Store(self.root / "state") as store:
            for label, (data, record, message) in cases.items():
                with self.subTest(label=label):
                    self._rewrite_trace(data, **record)
                    try:
                        with self.assertRaisesRegex(ValueError, message):
                            compile_seed_bundle(store, self.source)
                        self.assertEqual(list(store.blobs.iterdir()), [])
                    finally:
                        self._restore(original)

    def test_capture_and_analysis_split_saved_steps_identically(self):
        rows = self._trace_rows()
        # Raw U+2028 and U+0085 are legal inside JSON strings but are line
        # breaks for str.splitlines; every reader must split bytes on \n/\r.
        rows[0]["text"] = "line\u2028separator\x85kept"
        data = b"".join(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8") + b"\n" for row in rows)
        self.assertEqual(len(data.splitlines()), 2)
        self.assertEqual(len(data.decode("utf-8").splitlines()), 4)
        self._rewrite_trace(data)
        record = self.manifest["trajectories"][TRACE]
        with Store(self.root / "state") as store:
            compiled = compile_seed_bundle(store, self.source)
            entry = json.loads(store.read(compiled["data"]))["trajectories"][0]
            self.assertEqual(base64.b64decode(entry["steps_b64"]), data)
            observed = reread_steps(store, entry)
            self.assertEqual((observed["n_steps"], observed["stop_events"], observed["stop_event_rate"]),
                             (record["n_steps"], record["stop_events"], 0.0))
            self.assertEqual(observed["completion_tokens_total"], record["completion_tokens_total"])

            def frozen(steps: bytes, **manifest) -> dict:
                return dict(entry, steps_sha256=store.put(steps),
                            steps_b64=base64.b64encode(steps).decode("ascii"),
                            manifest=dict(entry["manifest"], **manifest))

            rejected = {
                "extra field": frozen(_jsonl(dict(rows[0], logprobs=None), rows[1])),
                "blank record": frozen(_jsonl(rows[0]) + b"\n" + _jsonl(rows[1])),
                "manifest counter": frozen(data, stop_events=1),
                "billed tokens": frozen(data, completion_tokens_total=0),
                "noncanonical base64": dict(entry, steps_b64=entry["steps_b64"] + "\n"),
                "foreign digest": dict(entry, steps_sha256=digest(b"other bytes")),
            }
            for label, candidate in rejected.items():
                with self.subTest(label=label), self.assertRaises(ValueError):
                    reread_steps(store, candidate)

    def test_prepare_refuses_state_inside_the_source_checkout_or_an_existing_root(self):
        checkout = self.root / "checkout"
        run = checkout / "runs" / "s1" / "historical-run"
        shutil.copytree(self.source, run)
        existing = self.root / "existing"
        existing.mkdir()
        example = str(REPO / "examples" / "afterlife_historical_pilot.py")
        for source, target in ((run, checkout / "state"), (run, run / "state"),
                               (run, existing), (self.root / "missing-run", self.root / "fresh")):
            with self.subTest(target=target):
                refused = subprocess.run(
                    [sys.executable, example, "prepare", "--source-run", str(source),
                     "--root", str(target)],
                    cwd=REPO, env=self.env, capture_output=True, text=True, timeout=120)
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("ValueError", refused.stderr)
        self.assertFalse((checkout / "state").exists())
        self.assertFalse((run / "state").exists())
        self.assertFalse((self.root / "fresh").exists())
        self.assertEqual(list(existing.iterdir()), [])

    def test_cli_prepares_open_exploratory_protocol_then_runs_only_local_analysis(self):
        before = self._source_bytes()
        state = self.root / "pilot-state"
        prepared = self._prepare(state)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        plan = json.loads(prepared.stdout)
        self.assertEqual(plan["status"], "prepared")
        self.assertEqual(plan["scientific_validity"], "not_assessed")
        self.assertEqual(plan["historical_exposure"],
                         "declared_before_exploratory_protocol")
        self.assertEqual(self._source_bytes(), before)
        planner, analyst, reviewer = ("afterlife-pilot-planner", "afterlife-pilot-analyst",
                                      "afterlife-pilot-reviewer")
        analysis = ["analysis", "advance", plan["batch"], "--root", str(state),
                    "--planner", planner, "--analyst", analyst, "--reviewer", reviewer]
        with Store(state) as store:
            events = store.events()
            protocol = Kernel._get(events, plan["protocol"], "protocol")["payload"]
            self.assertEqual(protocol["protocol_mode"], "exploratory")
            self.assertIn(protocol["data"], protocol["seen_data"])
            self.assertEqual(protocol["statistical_design"]["data_splits"][0]
                             ["exposure_policy"], "open")
            node = Kernel._get(events, plan["node"], "experiment_node")["payload"]
            self.assertEqual((node["action"], node["components"]["discrimination"]),
                             ("baseline", 0))
            self.assertFalse(any(event["kind"] in {"claim", "review", "result"}
                                 for event in events))

            settled = advance_batch(store, plan["batch"])
            self.assertEqual(settled["status"], "completed")
            self.assertEqual(json.loads(self._cli("analysis", "status", plan["batch"],
                                                  "--root", str(state)).stdout)["status"],
                             "awaiting_analysis")
            pending = (store.export(), store.export_receipts())

            # The binding fixes the adapter (ADR 0016); a contradicting --adapter is refused.
            wrong_adapter = self._cli(*analysis, "--adapter", "synthetic_causal_v1", check=False)
            self.assertNotEqual(wrong_adapter.returncode, 0)
            self.assertIn("differs from the bound", wrong_adapter.stderr)
            self.assertEqual((store.export(), store.export_receipts()), pending)

            # Valid CAS bytes that contradict the raw steps are rejected by the
            # domain arithmetic, not only by digest verification.
            current = batch_index(store, store.events())[plan["batch"]]
            slot = next(row for row in current["slots"] if row["slot"] == "reanalysis:4")
            metric_key = Kernel._get(store.events(), slot["result"],
                                     "result")["payload"]["outputs"]["metrics"]
            primary = next(row for row in current["slots"] if row["slot"] == "primary:4")
            raw_key = Kernel._get(store.events(), primary["result"],
                                  "result")["payload"]["outputs"]["raw_data"]
            raw = json.loads(store.read(raw_key))
            raw["trajectory"]["manifest"]["stop_events"] += 1
            substitutes = {
                metric_key: (b'{"stop_event_rate":0.123}', "stop rate differs from raw steps"),
                raw_key: (canonical(raw), "raw trajectory differs from the frozen"),
            }
            read = store.read
            for key, (replacement, message) in substitutes.items():
                with self.subTest(substitute=message), patch.object(
                        store, "read", side_effect=lambda k, key=key, data=replacement:
                        data if k == key else read(k)):
                    with self.assertRaisesRegex(ValueError, message):
                        AfterlifeSeedBatchAnalysisAdapter.propose(store, current)
            incomplete = dict(current, slots=[dict(row) for row in current["slots"]])
            incomplete["slots"][-1]["status"] = "unknown"
            with self.assertRaisesRegex(ValueError, "incomplete"):
                AfterlifeSeedBatchAnalysisAdapter.propose(store, incomplete)

            class EditedAdapter(AfterlifeSeedBatchAnalysisAdapter):
                """Same arithmetic, but source bytes differ from the frozen binding."""

            with self.assertRaisesRegex(ValueError, "source differs from frozen domain binding"):
                advance_batch_analysis(store, plan["batch"], planner=Actor(planner, "planner"),
                                       analyst=Actor(analyst, "analyst"),
                                       reviewer_actor=reviewer, adapter=EditedAdapter())
            self.assertEqual((store.export(), store.export_receipts()), pending)

            original_metric = store.read(metric_key)
            (store.blobs / metric_key).write_bytes(b'{"stop_event_rate":0.123}')
            tampered = self._cli(*analysis, "--adapter", "afterlife_seed_v1", check=False)
            self.assertNotEqual(tampered.returncode, 0)
            self.assertFalse(any(event["kind"] == "claim" for event in store.events()))
            (store.blobs / metric_key).write_bytes(original_metric)

            admitted = self._cli(*analysis, "--adapter", "afterlife_seed_v1")
            result = json.loads(admitted.stdout)
            self.assertEqual(result["status"], "awaiting_review")
            self.assertEqual(result["scientific_validity"], "not_assessed")
            claim = Kernel._get(store.events(), result["claim"], "claim")["payload"]
            self.assertEqual((claim["outcome"], claim["inference_mode"]),
                             ("inconclusive", "exploratory"))
            self.assertEqual(len(claim["evidence"]), 18)
            self.assertIn("matched the legacy manifest for all nine", claim["statement"])
            self.assertIn("ranged from 0.0000 to 1.0000", claim["statement"])
            self.assertTrue(any(item.startswith("Capture rejects") for item in claim["limitations"]))
            snapshot = (store.export(), store.export_receipts())
            replay = self._cli(*analysis, "--adapter", "afterlife_seed_v1")
            self.assertEqual(json.loads(replay.stdout), result)
            self.assertEqual((store.export(), store.export_receipts()), snapshot)
            status = json.loads(self._cli("analysis", "status", plan["batch"],
                                          "--root", str(state)).stdout)
            self.assertEqual(status["status"], "awaiting_review")
            self.assertEqual(status["analyses"][0]["assignments"], [result["assignment"]])
            self.assertEqual(status["analyses"][0]["submitted_reviews"], [])
            self.assertTrue(Kernel(store, Actor("fixture-auditor", "analyst"))
                            .gate(result["claim"])["passed"])
            self.assertFalse(any(event["kind"] == "review" for event in store.events()))
            graph = ResearchGraph.from_store(store)
            self.assertEqual(graph.node(plan["binding"]).kind.value, "domain_binding")
            inventory = {item["sha256"] for item in artifact_inventory(store, store.events())}
            self.assertLessEqual({digest(before["manifest.json"]),
                                  *self.manifest["integrity"].values()}, inventory)
            raw_key = self.manifest["integrity"][TRACE_STEPS]
            original_blob = store.read(raw_key)
            (store.blobs / raw_key).write_bytes(b"tampered after analysis")
            with self.assertRaises(IntegrityError):
                Kernel(store, Actor("fixture-auditor", "analyst")).gate(result["claim"])
            (store.blobs / raw_key).write_bytes(original_blob)
        self.assertEqual(self._source_bytes(), before)


if __name__ == "__main__":
    unittest.main()
