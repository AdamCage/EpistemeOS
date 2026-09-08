"""Offline integration tests for actual fixture execution and persisted CLI state.

The numerical fixture exercises reproducibility on the current Python runtime;
it does not test independent scientific agents or establish a scientific finding.
"""

from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from episteme.cli import main
from episteme.demo import PRIMARY_SOURCE, REANALYSIS_SOURCE, run_demo
from episteme.kernel import Actor, Kernel
from episteme.store import Store, digest


PROJECT = Path(__file__).resolve().parents[1]


def command(*args):
    environment = dict(os.environ, PYTHONPATH=str(PROJECT / "src"))
    return subprocess.run(
        [sys.executable, "-m", "episteme", *map(str, args)], cwd=PROJECT,
        env=environment, capture_output=True, text=True, encoding="utf-8",
        timeout=40, check=False,
    )


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_directory = tempfile.TemporaryDirectory(prefix="episteme-cli-fixture-")
        cls.addClassCleanup(cls.fixture_directory.cleanup)
        cls.fixture_root = Path(cls.fixture_directory.name) / "state"
        process = command("demo", "--root", cls.fixture_root)
        if process.returncode:
            raise AssertionError(f"real CLI demo failed: {process.stderr}")
        cls.demo_summary = json.loads(process.stdout)
        with Store(cls.fixture_root) as store:
            cls.fixture_events = store.events()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="episteme-cli-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "state"
        shutil.copytree(self.fixture_root, self.root)
        self.claim = self.demo_summary["claim"]

    def invoke(self, *args, status=0):
        process = command(*args)
        self.assertEqual(process.returncode, status, process.stderr or process.stdout)
        if status == 0:
            self.assertEqual(process.stderr, "")
        self.assertNotIn("Traceback", process.stderr)
        return json.loads(process.stdout if process.stdout else process.stderr)

    def test_demo_executes_three_primary_and_three_reanalysis_programs(self):
        summary = self.demo_summary
        self.assertTrue(summary["synthetic_demo"])
        self.assertEqual(summary["counts"], {
            "hypothesis": 2, "protocol": 1, "run": 6, "result": 6, "claim": 1,
        })
        self.assertEqual(summary["event_count"], 16)
        self.assertEqual(summary["next_action"]["action"], "scientific_review")
        self.assertEqual(summary["claims"][0]["gate"]["scientific_validity"], "not_assessed")
        self.assertTrue(summary["claims"][0]["gate"]["passed"])
        self.assertFalse((self.root / ".demo-in-progress").exists())
        with Store(self.root) as store:
            history = store.events()
            self.assertFalse(any(event["kind"] == "review" for event in history))
            protocol = next(event for event in history if event["kind"] == "protocol")
            runs = [event for event in history if event["kind"] == "run"]
            results = {event["payload"]["run"]: event["payload"] for event in history
                       if event["kind"] == "result"}
            primary = {event["id"]: event for event in runs if not event["payload"]["replicate_of"]}
            self.assertEqual(sorted(event["payload"]["seed"] for event in primary.values()), [17, 41, 73])
            for run in runs:
                with self.subTest(run=run["id"]):
                    payload = run["payload"]
                    self.assertLess(protocol["seq"], run["seq"])
                    self.assertEqual(payload["protocol_hash"], protocol["hash"])
                    self.assertEqual(results[run["id"]]["status"], "completed")
                    outputs = results[run["id"]]["outputs"]
                    metric = json.loads(store.read(outputs["metrics"]))
                    log = json.loads(store.read(outputs["log"]))
                    self.assertEqual(metric["seed"], payload["seed"])
                    self.assertEqual(metric["samples"], 64)
                    self.assertGreater(metric["slope"], 1.0)
                    self.assertEqual(log["returncode"], 0)
                    self.assertGreater(log["child_pid"], 0)
                    self.assertEqual(log["command"], payload["command"])
                    self.assertEqual(log["source_sha256"], payload["implementation"])
                    self.assertEqual(log["environment_sha256"], payload["environment"])
                    self.assertGreaterEqual(log["elapsed_seconds"], 0)
                    self.assertEqual(json.loads(log["stdout"])["metrics"], metric)
                    if payload["replicate_of"]:
                        original = primary[payload["replicate_of"]]
                        original_outputs = results[original["id"]]["outputs"]
                        original_metric = json.loads(store.read(original_outputs["metrics"]))
                        self.assertEqual(outputs["raw_data"], original_outputs["raw_data"])
                        self.assertLessEqual(abs(metric["slope"] - original_metric["slope"]), 1e-10)
                        self.assertNotEqual(run["actor"], original["actor"])
                        self.assertEqual(store.read(payload["implementation"]), REANALYSIS_SOURCE)
                    else:
                        self.assertEqual(store.read(payload["implementation"]), PRIMARY_SOURCE)

    def test_graph_cli_verifies_snapshot_and_exports_dot(self):
        graph = self.invoke("graph", "--root", self.root)
        self.assertEqual(graph["revision"], len(self.fixture_events))
        self.assertEqual(graph["scientific_validity"], "not_assessed")
        self.assertEqual(graph["node_kinds"]["claim"], 1)
        self.assertGreater(graph["edges"], 0)
        dot = command("graph", "--root", self.root, "--format", "dot")
        self.assertEqual(dot.returncode, 0, dot.stderr)
        self.assertIn("digraph research", dot.stdout)
        self.assertIn(self.claim, dot.stdout)

    def test_afterlife_cli_preserves_source_and_idempotently_exports_historical_closure(self):
        source = Path(self.directory.name) / "legacy"
        source.mkdir()
        document = source / "docs" / "stages" / "stage-1" / "PLAN.md"
        document.parent.mkdir(parents=True)
        document.write_text("Historical unverified plan fixture", encoding="utf-8")
        before = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob("*") if p.is_file()}
        self.invoke("afterlife", "inspect", source)
        nested = source / "not-allowed"
        self.invoke("afterlife", "import", source, "--root", nested, status=2)
        self.assertFalse(nested.exists())
        destination = Path(self.directory.name) / "historical"
        first = self.invoke("afterlife", "import", source, "--root", destination)
        second = self.invoke("afterlife", "import", source, "--root", destination)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(first["preregistered_protocols_created"], 0)
        self.assertEqual(first["accepted_claims_created"], 0)
        graph = self.invoke("graph", "--root", destination)
        self.assertEqual(graph["revision"], 1)
        self.assertEqual(graph["node_kinds"]["afterlife_snapshot"], 1)
        self.assertNotIn("claim", graph["node_kinds"])
        files = self.invoke("export", "--root", destination)
        bundle = json.loads(Path(files["review-bundle.json"]).read_text(encoding="utf-8"))
        self.assertEqual(len(bundle["artifacts"]), first["captured_blobs"] + 1)
        self.assertEqual(before, {p.relative_to(source).as_posix(): p.read_bytes()
                                  for p in source.rglob("*") if p.is_file()})

    def test_inspect_gate_and_export_reopen_without_appending_events(self):
        before = self.fixture_events
        summary = self.invoke("inspect", "--root", self.root)
        gate = self.invoke("gate", self.claim, "--root", self.root)
        files = self.invoke("export", "--root", self.root)
        self.assertEqual(summary["root"], str(self.root.resolve()))
        self.assertEqual(summary["event_count"], len(before))
        self.assertEqual(summary["last_event_hash"], before[-1]["hash"])
        self.assertEqual(gate, summary["claims"][0]["gate"])
        self.assertEqual(set(files), {"review-bundle.json", "report.md", "events.jsonl"})
        with Store(self.root) as store:
            self.assertEqual(store.events(), before)
            self.assertEqual(Kernel(store, Actor("observer", "observer")).gate(self.claim), gate)

    def test_export_is_repeatable_and_artifact_manifest_resolves(self):
        files = self.invoke("export", "--root", self.root)
        first = {name: Path(path).read_bytes() for name, path in files.items()}
        self.invoke("export", "--root", self.root)
        self.assertEqual(first, {name: Path(path).read_bytes() for name, path in files.items()})
        bundle = json.loads(first["review-bundle.json"])
        events = [json.loads(line) for line in first["events.jsonl"].splitlines()]
        self.assertEqual(bundle["events"], self.fixture_events)
        self.assertEqual(events, bundle["events"])
        self.assertEqual(bundle["snapshot_hash"], events[-1]["hash"])
        self.assertEqual(bundle["summary"]["last_event_hash"], bundle["snapshot_hash"])
        self.assertEqual(bundle["summary"]["event_count"], len(events))
        for artifact in bundle["artifacts"]:
            with self.subTest(artifact=artifact["sha256"]):
                data = (self.root / artifact["path"]).read_bytes()
                self.assertEqual(digest(data), artifact["sha256"])
                self.assertEqual(len(data), artifact["bytes"])
        report = first["report.md"].decode("utf-8")
        self.assertIn("synthetic", report.lower())
        self.assertIn("not_assessed", report)
        self.assertIn("scientific_review", report)

    def test_demo_refuses_nonempty_root_and_preserves_existing_evidence(self):
        before = {path.relative_to(self.root): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        result = self.invoke("demo", "--root", self.root, status=2)
        self.assertIn("absent or empty", result["error"])
        after = {path.relative_to(self.root): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_inspection_commands_do_not_initialize_missing_state(self):
        absent = Path(self.directory.name) / "never-created"
        for name, extra, status in [("inspect", [], 2), ("export", [], 2),
                                    ("gate", ["claim-unknown"], 1)]:
            with self.subTest(command=name):
                result = self.invoke(name, *extra, "--root", absent, status=status)
                self.assertIn("existing state.sqlite3", result["error"])
                self.assertFalse(absent.exists())

    def test_unknown_claim_returns_structured_error_without_mutation(self):
        result = self.invoke("gate", "claim-unknown", "--root", self.root, status=1)
        self.assertIn("unknown claim", result["error"])
        with Store(self.root) as store:
            self.assertEqual(store.events(), self.fixture_events)

    def test_missing_or_corrupt_result_log_blocks_gate_and_export(self):
        result = next(event for event in self.fixture_events if event["kind"] == "result")
        path = self.root / "artifacts" / "sha256" / result["payload"]["outputs"]["log"]
        original = path.read_bytes()
        old_bundle = (self.root / "review-bundle.json").read_bytes()
        for corrupt, expected in [(True, "hash mismatch"), (False, "missing artifact")]:
            with self.subTest(corrupt=corrupt):
                if corrupt:
                    path.write_bytes(b"tampered execution log")
                else:
                    path.unlink()
                gate = self.invoke("gate", self.claim, "--root", self.root, status=1)
                self.assertFalse(gate["passed"])
                self.assertIn(expected, "; ".join(gate["failures"]))
                exported = self.invoke("export", "--root", self.root, status=2)
                self.assertIn(expected, exported["error"])
                self.assertEqual((self.root / "review-bundle.json").read_bytes(), old_bundle)
                path.write_bytes(original)

    def test_event_tampering_returns_structured_errors_for_existing_state_commands(self):
        with closing(sqlite3.connect(self.root / "state.sqlite3")) as database:
            database.execute("DROP TRIGGER events_no_update")
            database.execute("UPDATE events SET actor = 'forged-actor' WHERE seq = 1")
            database.commit()
        for name, extra, status in [("inspect", [], 2), ("export", [], 2),
                                    ("gate", [self.claim], 1)]:
            with self.subTest(command=name):
                result = self.invoke(name, *extra, "--root", self.root, status=status)
                self.assertIn("event chain corrupt", result["error"])

    def test_two_real_demos_reproduce_raw_data_and_metrics_on_same_runtime(self):
        second = Path(self.directory.name) / "second-demo"
        self.invoke("demo", "--root", second)

        def scientific_outputs(root):
            with Store(root) as store:
                history = store.events()
                runs = {event["id"]: event["payload"] for event in history if event["kind"] == "run"}
                return {(runs[event["payload"]["run"]]["seed"],
                         bool(runs[event["payload"]["run"]]["replicate_of"])):
                        {key: store.read(event["payload"]["outputs"][key])
                         for key in ("raw_data", "metrics")}
                        for event in history if event["kind"] == "result"}

        self.assertEqual(scientific_outputs(self.root), scientific_outputs(second))

    def test_subprocess_failure_is_recorded_and_demo_lock_is_released(self):
        failed_root = Path(self.directory.name) / "failed-demo"
        failed = subprocess.CompletedProcess(args=[], returncode=7,
                                             stdout="", stderr="deliberate test failure")
        with patch("episteme.demo.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "synthetic run failed"):
                run_demo(failed_root)
        self.assertFalse((failed_root / ".demo-in-progress").exists())
        with Store(failed_root) as store:
            events = store.events()
            results = [event for event in events if event["kind"] == "result"]
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["payload"]["status"], "failed")
            self.assertIn("exited 7", results[0]["payload"]["reason"])
            log = json.loads(store.read(results[0]["payload"]["outputs"]["log"]))
            self.assertEqual(log["returncode"], 7)
            self.assertEqual(log["stderr"], "deliberate test failure")
            self.assertFalse(any(event["kind"] in {"claim", "review"} for event in events))
        self.assertFalse((failed_root / "review-bundle.json").exists())

    def test_programmatic_cli_main_matches_subprocess_inspection(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(["inspect", "--root", str(self.root)])
        self.assertEqual(result, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["claim"], self.claim)

    def review_input(self, **changes):
        review = dict(reviewer_id="test-external-reviewer", verdict="approve",
                      rationale="Test fixture opinion only; not a scientific endorsement.",
                      actions=[], expected_basis=self.demo_summary["claims"][0]["gate"]["basis_hash"])
        review.update(changes)
        path = Path(self.directory.name) / "review.json"
        path.write_text(json.dumps(review), encoding="utf-8")
        return path

    def test_paper_command_refuses_unreviewed_claim_without_appending(self):
        result = self.invoke("paper", self.claim, "--title", "Synthetic integration draft",
                             "--actor", "test-writer", "--root", self.root, status=2)
        self.assertIn("not eligible for paper", result["error"])
        with Store(self.root) as store:
            self.assertEqual(store.events(), self.fixture_events)
        self.assertEqual(list(self.root.glob("paper-*.md")), [])

    def test_review_cli_then_paper_creates_only_a_traceable_internal_draft(self):
        review_path = self.review_input()
        review = self.invoke("review", self.claim, "--input", review_path, "--root", self.root)
        self.assertEqual(review["next_action"]["action"], "paper_candidate")
        draft = self.invoke("paper", self.claim, "--title", "Synthetic integration draft",
                            "--actor", "test-writer", "--root", self.root)
        self.assertEqual(draft["status"], "internal_draft")
        self.assertEqual(set(draft["files"]), {"manuscript", "bundle"})
        manuscript = Path(draft["files"]["manuscript"]).read_bytes()
        bundle_bytes = Path(draft["files"]["bundle"]).read_bytes()
        bundle = json.loads(bundle_bytes)
        self.assertEqual(bundle["selected_claims"], [self.claim])
        self.assertEqual(bundle["reviewed_bases"], {self.claim: review["next_action"]["basis_hash"]})
        self.assertIn("Internal evidence-linked draft", manuscript.decode("utf-8"))
        self.assertIn("not a submission-ready paper", manuscript.decode("utf-8"))
        with Store(self.root) as store:
            events = store.events()
            self.assertEqual(len(events), len(self.fixture_events) + 2)
            self.assertEqual(events[-2]["id"], review["review"])
            self.assertEqual(events[-1]["id"], draft["paper"])
            self.assertEqual(events[-1]["payload"]["source_snapshot"], events[-2]["hash"])
            self.assertEqual(events[-1]["payload"]["manuscript"], digest(manuscript))
            self.assertEqual(events[-1]["payload"]["bundle"], digest(bundle_bytes))
            self.assertEqual(store.read(digest(manuscript)), manuscript)
            self.assertEqual(bundle["events"], events[:-1])

    def test_review_cli_rejects_stale_basis_contributor_and_malformed_payload(self):
        for changes, message in [({"expected_basis": "0" * 64}, "stale review"),
                                 ({"reviewer_id": "fixture-executor"}, "independent"),
                                 ({"unexpected_field": True}, "exactly")]:
            with self.subTest(changes=changes):
                review_path = self.review_input(**changes)
                result = self.invoke("review", self.claim, "--input", review_path,
                                     "--root", self.root, status=2)
                self.assertIn(message, result["error"])
                with Store(self.root) as store:
                    self.assertEqual(store.events(), self.fixture_events)

    def test_review_cli_negative_opinion_routes_to_replan_and_blocks_paper(self):
        review_path = self.review_input(verdict="request_changes", actions=["Add a held-out control."])
        review = self.invoke("review", self.claim, "--input", review_path, "--root", self.root)
        self.assertEqual(review["next_action"], {"action": "replan", "reasons": ["Add a held-out control."]})
        result = self.invoke("paper", self.claim, "--title", "Blocked draft", "--actor", "test-writer",
                             "--root", self.root, status=2)
        self.assertIn("not eligible for paper", result["error"])
        with Store(self.root) as store:
            self.assertEqual(store.events()[-1]["id"], review["review"])
            self.assertFalse(any(event["kind"] == "paper" for event in store.events()))


if __name__ == "__main__":
    unittest.main()
