"""Stage-only loop on synthetic_causal_v1. Fixture outcomes are not scientific results.

Closing a stage records a mechanical disposition. It does not assign review,
raise scientific_validity, or treat an inconclusive exploratory report as
success. Failed and unknown attempts stay recorded and are not rerun.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from episteme.batch_controller import advance_batch
from episteme.cli import main
from episteme.commands import CommandService
from episteme.graph import NodeKind, ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.stages import UNREVIEWED, stage_open, stage_run, stage_status
from episteme.store import Store
from test_pack_workflow import (
    ANALYST, PLANNER, STUDY, command, plan_batch, planning, preregistration, search_node)


EXECUTOR = "pack-executor"
REPLICATOR = "pack-reanalyst"
METRIC = "treatment_effect"
_REAL_POPEN = subprocess.Popen


def _question(store: Store, explanation: str) -> str:
    return Kernel._get(store.events(), explanation, "explanation_set")["payload"]["question"]


def _open(store: Store, explanation: str, binding: str, *, ceiling: int = 2,
          criteria: list[dict] | None = None, predecessor: str | None = None) -> dict:
    return stage_open(store, actor=PLANNER, study=STUDY, question=_question(store, explanation),
                      explanation_set=explanation, pack_id="synthetic_causal_v1",
                      preregistration=binding, attempt_ceiling=ceiling,
                      criteria=criteria or [dict(kind="analysis_recorded")],
                      predecessor=predecessor)


def _run(store: Store, stage: str) -> dict:
    return stage_run(store, stage, analyst=ANALYST, executor=EXECUTOR, replicator=REPLICATOR)


class _Dead:
    def wait(self) -> int:
        return 0


def _failing_worker(argv, **kwargs):
    workspace = Path(argv[-1])
    program = workspace / "program.py"
    program.write_bytes(program.read_bytes() + b"\n")
    return _REAL_POPEN(argv, **kwargs)


class StageControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temporary = TemporaryDirectory(prefix="episteme-stage-")
        cls.base = Path(cls._temporary.name)
        with Store(cls.base / "origin") as store:
            cls.explanation = planning(store)
            registered = command(store, "pack.preregister", preregistration(store, cls.explanation))
            cls.protocol = registered["protocol"]
            cls.binding = registered["binding"]
        shutil.copytree(cls.base / "origin", cls.base / "planned")
        with Store(cls.base / "planned") as store:
            tree = search_node(store, cls.protocol, 2)
            binding = Kernel._get(store.events(), cls.binding, "pack_binding")
            cls.batch = plan_batch(store, binding, tree)
        shutil.copytree(cls.base / "planned", cls.base / "completed")
        with Store(cls.base / "completed") as store:
            assert advance_batch(store, cls.batch)["status"] == "completed"

    @classmethod
    def tearDownClass(cls):
        cls._temporary.cleanup()

    def copy(self, name: str) -> Store:
        temporary = TemporaryDirectory(prefix="episteme-stage-case-")
        self.addCleanup(temporary.cleanup)
        target = Path(temporary.name) / "state"
        shutil.copytree(self.base / name, target)
        store = Store(target)
        self.addCleanup(store.close)
        return store

    def test_close_without_review_keeps_validity_unassessed(self):
        store = self.copy("completed")
        opened = _open(store, self.explanation, self.binding)
        before_reviews = [event["kind"] for event in store.events()]
        result = _run(store, opened["stage"])
        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["disposition"], "criteria_met")
        self.assertEqual(result["review"], "not_requested")
        self.assertEqual(result["scientific_validity"], "not_assessed")
        self.assertEqual(result["outcome"], "inconclusive")
        self.assertEqual(result["inference_mode"], "exploratory")
        history = store.events()
        analysis = Kernel._get(history, result["analysis"], "pack_analysis")
        self.assertEqual(analysis["payload"]["reviewer_actor"], UNREVIEWED)
        claim = Kernel._get(history, result["claim"], "claim")
        self.assertEqual(claim["payload"]["outcome"], "inconclusive")
        members = Kernel(store, Actor("stage-test-observer", "observer"))._review_members(
            history, result["claim"])[1]
        self.assertNotIn(UNREVIEWED, members)
        self.assertFalse(any(event["kind"] in {"review_assignment", "review", "paper"}
                             for event in history))
        self.assertTrue(all(event["payload"].get("scientific_validity", "not_assessed") == "not_assessed"
                            for event in history if isinstance(event.get("payload"), dict)))
        graph = ResearchGraph.from_store(store)
        self.assertEqual(graph.node(opened["stage"]).kind, NodeKind.STAGE_OPEN)
        self.assertEqual(graph.node(result["close"]).kind, NodeKind.STAGE_CLOSE)
        self.assertEqual(before_reviews.count("review_assignment"), 0)
        again = _run(store, opened["stage"])
        self.assertEqual(again["close"], result["close"])
        self.assertEqual(len([event for event in store.events() if event["kind"] == "stage_close"]), 1)

    def test_inconclusive_primary_metric_is_criteria_not_met(self):
        store = self.copy("completed")
        opened = _open(store, self.explanation, self.binding, criteria=[
            dict(kind="analysis_recorded"),
            dict(kind="primary_metric", name=METRIC, op=">=", threshold=0),
        ])
        result = _run(store, opened["stage"])
        self.assertEqual(result["disposition"], "criteria_not_met")
        self.assertEqual(result["outcome"], "inconclusive")
        self.assertEqual(result["review"], "not_requested")
        self.assertEqual(result["scientific_validity"], "not_assessed")
        kinds = {row["kind"]: row for row in result["criteria_evaluations"]}
        self.assertTrue(kinds["analysis_recorded"]["met"])
        self.assertFalse(kinds["primary_metric"]["met"])
        self.assertIn("inconclusive", kinds["primary_metric"]["detail"])
        claim = Kernel._get(store.events(), result["claim"], "claim")
        self.assertEqual(claim["payload"]["outcome"], "inconclusive")
        self.assertEqual(claim["payload"]["inference_mode"], "exploratory")

    def test_run_from_preregister_executes_roster_without_review(self):
        store = self.copy("origin")
        opened = _open(store, self.explanation, self.binding)
        result = _run(store, opened["stage"])
        self.assertEqual(result["disposition"], "criteria_met")
        self.assertEqual(result["review"], "not_requested")
        self.assertIsNotNone(result["batch"])
        self.assertEqual(result["attempt_ledger"]["completed"], 2)
        self.assertFalse(any(event["kind"] == "review_assignment" for event in store.events()))
        ResearchGraph.from_store(store)

    def test_budget_below_roster_starts_no_attempts(self):
        store = self.copy("origin")
        opened = _open(store, self.explanation, self.binding, ceiling=1)
        self.assertEqual(opened["roster_size"], 2)
        result = _run(store, opened["stage"])
        self.assertEqual(result["disposition"], "stopped_budget")
        self.assertIsNone(result["batch"])
        self.assertIsNone(result["analysis"])
        self.assertFalse(result["attempt_ledger"]["launched"])
        self.assertEqual(result["scientific_validity"], "not_assessed")
        self.assertFalse(any(event["kind"] in {"run", "batch_plan", "pack_analysis"}
                             for event in store.events()))

    def test_unknown_and_failed_attempts_block_without_rerun(self):
        unknown = self.copy("planned")
        opened = _open(unknown, self.explanation, self.binding)
        with patch("episteme.execution.subprocess.Popen", return_value=_Dead()):
            first = _run(unknown, opened["stage"])
        self.assertEqual(first["disposition"], "blocked")
        self.assertGreater(first["attempt_ledger"]["unknown"], 0)
        self.assertEqual(first["scientific_validity"], "not_assessed")
        self.assertIsNone(first["outcome"])
        frozen = unknown.export()
        with patch("episteme.execution.subprocess.Popen") as spawn:
            again = _run(unknown, opened["stage"])
        spawn.assert_not_called()
        self.assertEqual(again["close"], first["close"])
        self.assertEqual(unknown.export(), frozen)
        self.assertEqual(len([event for event in unknown.events() if event["kind"] == "run"]), 1)

        failed = self.copy("planned")
        opened = _open(failed, self.explanation, self.binding)
        with patch("episteme.execution.subprocess.Popen", side_effect=_failing_worker):
            first = _run(failed, opened["stage"])
        self.assertEqual(first["disposition"], "blocked")
        self.assertGreater(first["attempt_ledger"]["failed"], 0)
        self.assertEqual(first["scientific_validity"], "not_assessed")
        self.assertIsNone(first["outcome"])
        result = next(event for event in failed.events() if event["kind"] == "result")
        self.assertEqual(result["payload"]["status"], "failed")
        frozen = failed.export()
        with patch("episteme.execution.subprocess.Popen") as spawn:
            again = _run(failed, opened["stage"])
        spawn.assert_not_called()
        self.assertEqual(again["close"], first["close"])
        self.assertEqual(failed.export(), frozen)
        self.assertEqual(len([event for event in failed.events() if event["kind"] == "run"]), 1)

    def test_close_envelope_replay_does_not_write_a_second_close(self):
        store = self.copy("completed")
        opened = _open(store, self.explanation, self.binding)
        first = _run(store, opened["stage"])
        receipt = next(item for item in store.receipts() if item["request"]["action"] == "stage.close")
        frozen = store.export(), store.export_receipts()
        replayed = CommandService(store).execute(dict(context=receipt["context"],
                                                      request=receipt["request"]))
        self.assertEqual(replayed["close"], first["close"])
        self.assertEqual(replayed["disposition"], first["disposition"])
        self.assertEqual((store.export(), store.export_receipts()), frozen)
        self.assertEqual(len([event for event in store.events() if event["kind"] == "stage_close"]), 1)

    def test_predecessor_links_a_new_stage_without_rewriting_the_protocol(self):
        store = self.copy("completed")
        first = _open(store, self.explanation, self.binding)
        closed = _run(store, first["stage"])
        second = _open(store, self.explanation, self.binding, predecessor=first["stage"],
                       criteria=[dict(kind="analysis_recorded"),
                                 dict(kind="primary_metric", name=METRIC, op=">=", threshold=99)])
        result = _run(store, second["stage"])
        opened = Kernel._get(store.events(), second["stage"], "stage_open")
        self.assertEqual(opened["payload"]["predecessor"], first["stage"])
        self.assertEqual(opened["payload"]["protocol"], self.protocol)
        self.assertEqual(result["disposition"], "criteria_not_met")
        self.assertEqual(result["protocol"], closed["protocol"])
        self.assertEqual(len([event for event in store.events() if event["kind"] == "protocol"]), 1)

    def test_cli_open_run_and_status(self):
        temporary = TemporaryDirectory(prefix="episteme-stage-cli-")
        self.addCleanup(temporary.cleanup)
        target = Path(temporary.name) / "state"
        shutil.copytree(self.base / "completed", target)
        with Store(target) as store:
            question = _question(store, self.explanation)
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([
                "stage", "open", "--root", str(target), "--study", STUDY,
                "--actor", PLANNER.id, "--question", question,
                "--explanation-set", self.explanation, "--pack-id", "synthetic_causal_v1",
                "--preregistration", self.binding, "--attempt-ceiling", "2",
                "--criteria", '[{"kind":"analysis_recorded"}]',
            ])
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn("not_assessed", stdout.getvalue())
        with Store(target) as store:
            stage = next(event["id"] for event in store.events() if event["kind"] == "stage_open")
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([
                "stage", "run", stage, "--root", str(target),
                "--analyst", ANALYST.id, "--executor", EXECUTOR, "--replicator", REPLICATOR,
            ])
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertIn("criteria_met", stdout.getvalue())
        self.assertIn("not_requested", stdout.getvalue())
        self.assertIn("not_assessed", stdout.getvalue())
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["stage", "status", stage, "--root", str(target)])
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertIn("closed", stdout.getvalue())
        self.assertIn("not_assessed", stdout.getvalue())
        with Store(target) as store:
            self.assertFalse(any(event["kind"] == "review_assignment" for event in store.events()))
            status = stage_status(store, stage)
            self.assertEqual(status["disposition"], "criteria_met")


if __name__ == "__main__":
    unittest.main()
