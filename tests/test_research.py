"""Offline acceptance of the CLI research loop on two registered packs."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from episteme.cli import main
from episteme.commands import CommandService
from episteme.graph import ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.store import Store, digest


TABULAR_NULL = Path(__file__).resolve().parents[1] / "examples" / "tabular_classification_v1" / "null"
LITERATURE_FIXTURE = {"kind": "fixture_source", "fixture_id": "banknote-prior-art-v1"}


def synthetic(label: str, *, seed: int) -> dict:
    return dict(label=label, pack_id="synthetic_causal_v1", phase="exploratory",
                purpose="Compare the predeclared estimand with a null contrast on generated data.",
                parameters=dict(n_samples=32, assignment="randomized", analysis="difference_in_means"),
                host_inputs=dict(world=dict(treatment_effect=0.0, confounding_strength=0.0, noise_std=0.5),
                                 seeds=[seed], replication_tolerance=1e-9))


class _NoCompletion:
    def wait(self) -> int:
        return 0


class _ControllerStopped(BaseException):
    pass


class ResearchCliTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="episteme-research-cli-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "state"

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["research", *args, "--root", str(self.root)])
        return code, stdout.getvalue(), stderr.getvalue()

    def cli(self, *args: str) -> dict:
        code, stdout, stderr = self.run_cli(*args)
        self.assertEqual(code, 0, stderr)
        return json.loads(stdout)

    def snapshot(self) -> tuple[list[dict], list[dict]]:
        with Store(self.root, read_only=True) as store:
            return store.snapshot()

    def start(self, candidates: list[dict], *, attempts: int = 8,
              literature: list[dict] | None = None, model: str | None = None, model_calls: int = 0) -> dict:
        hypothesis = self.directory / "hypothesis.txt"
        hypothesis.write_text("The registered contrast is distinguishable from a null contrast.\n",
                              encoding="utf-8")
        config_path = self.directory / "research.json"
        config = dict(budget=dict(stages=4, attempts=attempts, model_calls=model_calls,
                                  literature_calls=len(literature or []),
                                  wall_seconds=600, operation_seconds=120),
                      scope={"population": "generated mechanism-test fixtures"},
                      candidates=candidates, literature=literature or [])
        if model is not None:
            config["model"] = model
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.cli("start", "--hypothesis", str(hypothesis), "--config", str(config_path),
                 "--author", "fixture-author")
        history, _ = self.snapshot()
        return next(event for event in history if event["kind"] == "research_session")

    def verify_history(self) -> None:
        with Store(self.root, read_only=True) as store:
            ResearchGraph.from_store(store)

    def test_accepts_current_draft_and_runs_two_frozen_stages(self):
        opened = self.start([synthetic("first", seed=7), synthetic("control", seed=13)],
                            attempts=4, literature=[LITERATURE_FIXTURE])
        session = opened["id"]
        frozen = self.snapshot()
        self.cli("status", session)
        self.run_cli("run", session)
        self.assertEqual(self.snapshot(), frozen)

        reply = self.directory / "reply.txt"
        reply.write_text("Preserve inconclusive outcomes and vary only the declared seed.", encoding="utf-8")
        self.cli("discuss", session, "--reply", str(reply))
        history, _ = self.snapshot()
        latest = history[-1]
        self.assertEqual(latest["payload"]["step"], "reply")
        rejected = self.snapshot()
        code, _, _ = self.run_cli("run", session, "--accept", opened["hash"])
        self.assertNotEqual(code, 0)
        self.assertEqual(self.snapshot(), rejected)

        execute = CommandService.execute

        def stop_after_close(service, envelope):
            result = execute(service, envelope)
            if envelope["request"]["action"] == "stage.close":
                raise _ControllerStopped()
            return result

        with patch.object(CommandService, "execute", autospec=True, side_effect=stop_after_close):
            with self.assertRaises(_ControllerStopped):
                self.run_cli("run", session, "--accept", latest["hash"])
        partial, _ = self.snapshot()
        closes_before_resume = [event for event in partial if event["kind"] == "stage_close"]
        self.assertEqual(len(closes_before_resume), 1)

        self.cli("run", session)
        history, receipts = self.snapshot()
        opens = [event for event in history if event["kind"] == "stage_open"]
        closes = [event for event in history if event["kind"] == "stage_close"]
        self.assertEqual(len(opens), 2)
        self.assertEqual(len(closes), 2)
        self.assertEqual(closes[0], closes_before_resume[0])
        self.assertEqual(opens[-1]["payload"]["predecessor"], opens[0]["id"])
        self.assertEqual([event["payload"]["outcome"] for event in closes], ["inconclusive"] * 2)
        self.assertEqual([event["payload"]["inference_mode"] for event in closes], ["exploratory"] * 2)
        self.assertEqual(len([event for event in history if event["kind"] == "execution_finalized"]), 4)
        self.assertFalse({"review", "paper", "literature_check"} & {event["kind"] for event in history})

        steps = [event["payload"] for event in history if event["kind"] == "research_step"]
        roles = {step["data"]["role"] for step in steps if step["step"] == "task-intent"}
        self.assertEqual(roles, {"analyst", "critic", "developer", "tester", "reviewer"})
        with Store(self.root, read_only=True) as store:
            reviews = [json.loads(store.read(step["data"]["context"])) for step in steps
                       if step["step"] == "task-intent" and step["data"]["role"] == "reviewer"]
        first_basis = [item["basis"] for item in reviews if item["result"]["close"] == closes[0]["id"]]
        self.assertEqual(len(first_basis), 2)
        self.assertEqual(len(set(first_basis)), 2)
        evidence = next(step["data"]["result"] for step in steps if step["step"] == "literature")
        self.assertEqual(evidence["content_kind"], "fixture_source")
        self.assertEqual(steps[-1]["step"], "finish")
        self.assertEqual(steps[-1]["data"]["reason"], "planned_checks_finished")
        self.assertEqual(steps[-1]["scientific_validity"], "not_assessed")

        self.cli("run", session)
        self.cli("status", session)
        output = self.directory / "exported"
        self.cli("export", session, "--out", str(output))
        self.assertTrue((output / "manuscript.md").read_text(encoding="utf-8").strip())
        exported = json.loads((output / "research.json").read_bytes())
        self.assertEqual(exported["disposition"]["outcomes"], steps[-1]["data"]["outcomes"])
        self.assertEqual(exported["scientific_validity"], "not_assessed")
        manifest = json.loads((output / "manifest.json").read_bytes())
        for member in manifest["files"]:
            content = (output / member["path"]).read_bytes()
            self.assertEqual(len(content), member["bytes"], member["path"])
            self.assertEqual(digest(content), member["sha256"], member["path"])
        self.assertEqual(self.snapshot(), (history, receipts))
        self.verify_history()

    def test_tabular_capture_keeps_its_own_inference_mode_and_null_result(self):
        opened = self.start([dict(label="null-table", pack_id="tabular_classification_v1",
                                  phase="confirmatory", parameters={}, host_inputs={},
                                  purpose="Exercise the fixed paired comparison on a generated null table.",
                                  capture_source=str(TABULAR_NULL))], attempts=2)
        self.cli("run", opened["id"], "--accept", opened["hash"])
        history, _ = self.snapshot()
        close = next(event["payload"] for event in history if event["kind"] == "stage_close")
        self.assertEqual(close["outcome"], "inconclusive")
        self.assertEqual(close["inference_mode"], "confirmatory")
        self.assertEqual(close["scientific_validity"], "not_assessed")
        self.assertFalse({"review_assignment", "review", "paper"} & {event["kind"] for event in history})
        self.verify_history()

    def test_unknown_local_attempt_is_never_launched_again(self):
        opened = self.start([synthetic("first", seed=7)], attempts=2)
        real_popen = subprocess.Popen
        launches = []

        def drop_local_completion(argv, *args, **kwargs):
            if isinstance(argv, list) and len(argv) > 1 and Path(argv[1]).name == "runner_backend.py":
                launches.append(argv)
                return _NoCompletion()
            return real_popen(argv, *args, **kwargs)

        with patch("episteme.execution.subprocess.Popen", side_effect=drop_local_completion):
            self.cli("run", opened["id"], "--accept", opened["hash"])
            frozen = self.snapshot()
            self.cli("run", opened["id"])
        self.assertEqual(len(launches), 1)
        self.assertEqual(self.snapshot(), frozen)
        history, _ = frozen
        self.assertEqual(len([event for event in history if event["kind"] == "execution_dispatch"]), 1)
        self.assertFalse(any(event["kind"] in {"execution_finalized", "review", "paper"} for event in history))
        close = next(event["payload"] for event in history if event["kind"] == "stage_close")
        self.assertEqual(close["disposition"], "blocked")
        self.assertIsNone(close["outcome"])
        self.verify_history()

    def test_attempt_ceiling_below_the_roster_stops_before_dispatch(self):
        opened = self.start([synthetic("first", seed=7)], attempts=1)
        self.cli("run", opened["id"], "--accept", opened["hash"])
        history, _ = self.snapshot()
        self.assertFalse(any(event["kind"] in {"execution_dispatch", "pack_analysis"} for event in history))
        finish = next(event["payload"] for event in history if event["kind"] == "research_step"
                      and event["payload"]["step"] == "finish")
        self.assertEqual(finish["data"]["reason"], "budget_exhausted")
        self.assertEqual(finish["data"]["outcomes"], [])
        self.verify_history()

    def test_resume_requires_time_for_the_unlaunched_roster(self):
        opened = self.start([synthetic("first", seed=7)], attempts=2)
        execute = CommandService.execute

        def stop_after_reserve(service, envelope):
            result = execute(service, envelope)
            if envelope["request"]["action"] == "batch.plan":
                raise _ControllerStopped()
            return result

        with patch.object(CommandService, "execute", autospec=True, side_effect=stop_after_reserve):
            with self.assertRaises(_ControllerStopped):
                self.run_cli("run", opened["id"], "--accept", opened["hash"])

        with patch("episteme.research_controller.remaining_seconds", return_value=1):
            result = self.cli("run", opened["id"])
        self.assertEqual(result["disposition"]["reason"], "budget_exhausted")
        history, _ = self.snapshot()
        self.assertEqual(sum(row["kind"] == "batch_plan" for row in history), 1)
        self.assertFalse(any(row["kind"] in {"execution_dispatch", "stage_open"} for row in history))
        self.verify_history()

    def test_review_answer_is_rejected_after_a_new_evidence_revision(self):
        opened = self.start([synthetic("first", seed=7)], attempts=2)
        execute = CommandService.execute

        def expose_after_review_intent(service, envelope):
            recorded = execute(service, envelope)
            request = envelope["request"]
            if request["action"] == "research.record" and request["payload"]["key"].startswith(
                    "task-intent:reviewer:"):
                history = service.store.events()
                close = next(event["payload"] for event in history if event["kind"] == "stage_close")
                result = next(event["payload"] for event in history if event["kind"] == "result")
                Kernel(service.store, Actor("fixture-analyst", "analyst")).expose_data(
                    data=result["outputs"]["raw_data"], protocol=close["protocol"],
                    purpose="Fixture exposure during the advisory review")
            return recorded

        with patch.object(CommandService, "execute", autospec=True, side_effect=expose_after_review_intent):
            self.cli("run", opened["id"], "--accept", opened["hash"])

        history, _ = self.snapshot()
        steps = [event["payload"] for event in history if event["kind"] == "research_step"]
        response = next(step["data"]["result"] for step in steps if step["key"].startswith("task:reviewer:"))
        self.assertEqual(response["status"], "invalid")
        self.assertIn("evidence changed", response["reason"])
        self.assertEqual(steps[-1]["data"]["reason"], "blocked")
        self.assertFalse(any(event["kind"] in {"review", "paper"} for event in history))
        self.verify_history()

    def test_unknown_provider_attempt_waits_without_redispatch(self):
        def fixture_provider(store, *, model, reasoning_effort):
            return store.put_json(dict(schema_version=1, provider="codex_cli_v1", profile_version=2,
                                       executable=str(self.directory / "fake-codex"),
                                       executable_sha256=digest(b"fake CLI fixture"),
                                       cli_version="codex-cli 0.153.4", model=model,
                                       reasoning_effort=reasoning_effort))

        with patch("episteme.research_controller.freeze_provider", side_effect=fixture_provider):
            opened = self.start([synthetic("first", seed=7)], model="fixture-model", model_calls=1)

        session = opened["id"]
        with patch("episteme.research_controller.launch_worker", side_effect=OSError("dispatch interrupted")) as send:
            waiting = self.cli("run", session, "--accept", opened["hash"])
            self.assertEqual(waiting["status"], "awaiting_completion")
            self.assertEqual(waiting["remaining"]["model_calls"], 0)
            frozen = self.snapshot()
            restored = self.cli("run", session)
            self.assertEqual(restored["status"], "awaiting_completion")
            self.assertEqual(self.snapshot(), frozen)
            send.assert_called_once()

        with patch("episteme.research_controller.remaining_seconds", return_value=0), patch(
                "episteme.research_controller.launch_worker") as send:
            terminal = self.cli("run", session)
            self.assertEqual(terminal["disposition"]["reason"], "blocked")
            self.assertEqual(terminal["disposition"]["outcomes"], [])
            send.assert_not_called()
        self.verify_history()


if __name__ == "__main__":
    unittest.main()
