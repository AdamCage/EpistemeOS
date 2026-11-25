import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from episteme.evolution import DockerBackend, EvolutionChannel, EvaluationError
from episteme.evolution_docker import _BOOTSTRAP, _OUTPUT_BYTES, _bounded_run
from episteme.evolution_gepa import optimize_instructions
from episteme.store import canonical


IMAGE = "sha256:" + "1" * 64
GOOD = dict(role_instructions={"orchestrator": "better instruction"})
OLD = dict(role_instructions={"orchestrator": "baseline instruction"})


def suite(name="holdout", *, critical="gate"):
    return dict(suite_id=name, cases=[
        dict(id="precondition", input=dict(problem=critical, split=name),
             expected=dict(action="blocked"), critical=True),
        dict(id="choose", input=dict(problem="choose", split=name),
             expected=dict(action="continue"), critical=False),
    ])


def docker_worker(args, data, timeout):
    request = json.loads(data)
    case = request["payload"]
    good = request["profile"] == GOOD
    if case["problem"] == "gate":
        action = "blocked"
    elif case["problem"] == "health":
        action = "continue" if good else "blocked"
    else:
        action = "continue" if good else "blocked"
    return 0, canonical(dict(action=action)) + b"\n", b""


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(TemporaryDirectory())
        self.channel = self.enterContext(EvolutionChannel(self.directory))
        self.base = self.channel.initialize(OLD, base_commit="2" * 40)
        self.backend = DockerBackend(IMAGE)

    def test_candidate_activation_and_health_rollback_keep_prior_runs_pinned(self):
        active_candidate = self.channel.propose(profile=GOOD, provenance="development comparison")
        stale_candidate = self.channel.propose(profile=dict(role_instructions={"critic": "probe again"}),
                                               provenance="different candidate")
        with patch("episteme.evolution_docker._bounded_run", side_effect=docker_worker) as run:
            evaluation = self.channel.evaluate(active_candidate, suite=suite(), backend=self.backend)
        self.assertEqual(run.call_count, 4)
        self.assertEqual(evaluation["score"]["correctness_gain"], 1)
        self.assertTrue(evaluation["score"]["eligible"])
        release = self.channel.advance()["release"]
        self.assertEqual(self.channel.pin()["profile"], GOOD)
        self.assertEqual(self.channel.pin(release)["backend"], self.backend.descriptor)
        self.assertEqual(self.channel.pin(self.base)["profile"], OLD)
        late = self.channel.record_signal(kind="verified_failure", key="blocked", source="c" * 64,
                                          expected_release=self.base)
        self.assertEqual(late["status"], "inactive_release")
        self.assertFalse(late["recorded"])
        with self.assertRaisesRegex(ValueError, "parent is stale"):
            self.channel.evaluate(stale_candidate, suite=suite(), backend=self.backend)
        with patch("episteme.evolution_docker._bounded_run", side_effect=docker_worker):
            health = self.channel.health_check(release, suite=suite("health", critical="health"),
                                               backend=self.backend)
        self.assertEqual(health["transition"]["status"], "rolled_back")
        self.assertEqual(self.channel.pin()["release"], self.base)
        self.assertEqual(self.channel.pin(release)["profile"], GOOD)
        self.assertTrue(self.channel.is_quarantined(release))
        self.assertFalse(self.channel.is_quarantined(self.base))
        self.assertEqual(self.channel.advance(active_candidate)["status"], "no_eligible_candidate")
        repeated = self.channel.propose(profile=GOOD, provenance="same version after rollback")
        with patch("episteme.evolution_docker._bounded_run", side_effect=docker_worker):
            self.channel.evaluate(repeated, suite=suite(), backend=self.backend)
        self.assertEqual(self.channel.advance(repeated)["status"], "no_eligible_candidate")
        with EvolutionChannel(self.directory, read_only=True) as reopened:
            self.assertEqual(reopened.pin(release)["release_hash"], self.channel.pin(release)["release_hash"])
            self.assertEqual(len(reopened.status()["evaluations"]), 3)

    def test_tied_or_regressing_candidate_cannot_activate(self):
        candidate = self.channel.propose(profile=GOOD, provenance="held-out comparison")
        with patch("episteme.evolution_docker._bounded_run", side_effect=docker_worker):
            result = self.channel.evaluate(candidate, suite=suite(critical="health"), backend=self.backend)
        self.assertEqual(result["score"]["critical_failures"], ["precondition"])
        self.assertEqual(result["score"]["correctness_gain"], 0)
        self.assertEqual(self.channel.advance()["release"], self.base)
        self.assertFalse(result["score"]["eligible"])

    def test_failing_backend_protects_release_and_records_each_pair(self):
        candidate = self.channel.propose(profile=GOOD, provenance="alternative")
        with patch("episteme.evolution_docker._bounded_run", side_effect=FileNotFoundError), \
                patch("episteme.evolution_docker.subprocess.run"):
            result = self.channel.evaluate(candidate, suite=suite(), backend=self.backend)
        self.assertFalse(result["score"]["eligible"])
        self.assertEqual(len(result["score"]["errors"]), 4)
        self.assertEqual(self.channel.advance()["release"], self.base)
        self.assertIsNotNone(self.channel.status()["evaluations"][0]["result"])

    def test_unknown_evaluation_does_not_run_again(self):
        candidate = self.channel.propose(profile=GOOD, provenance="alternative")
        with patch("episteme.evolution_docker._bounded_run", side_effect=KeyboardInterrupt), \
                patch("episteme.evolution_docker.subprocess.run"):
            with self.assertRaises(KeyboardInterrupt):
                self.channel.evaluate(candidate, suite=suite(), backend=self.backend)
        with patch("episteme.evolution_docker._bounded_run") as worker:
            with self.assertRaisesRegex(ValueError, "unknown completion"):
                self.channel.evaluate(candidate, suite=suite(), backend=self.backend)
            worker.assert_not_called()
        self.assertIsNone(self.channel.status()["evaluations"][0]["result"])

    def test_threshold_counts_distinct_observations_for_current_release(self):
        def record(value):
            return self.channel.record_signal(kind="verified_failure", key="mismatch", source=value * 64)

        self.assertFalse(record("a")["triggered"])
        self.assertFalse(record("a")["recorded"])
        self.assertTrue(record("b")["triggered"])
        self.assertEqual(self.channel.status()["triggers"], [dict(kind="verified_failure", key="mismatch",
                                                                  observations=2)])

    def test_roles_and_patch_allowlist_cannot_change_admission_or_tests(self):
        with self.assertRaisesRegex(ValueError, "unsupported instruction role"):
            self.channel.propose(profile=dict(role_instructions={"kernel": "approve"}), provenance="wrong")
        for path in ("src/episteme/kernel.py", "tests/test_evolution.py", "src/episteme/domains/packs/../../kernel.py"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "patch path"):
                self.channel.propose(patch={path: "pass"}, provenance="wrong")
        path = "src/episteme/research_policy.py"
        self.channel.propose(patch={path: "raise RuntimeError('must never execute on host')"}, provenance="isolated")
        self.assertEqual(self.channel.pin()["patch"], {})

    def test_pinned_docker_uses_stdin_without_mounts_or_closed_answers(self):
        candidate = self.channel.propose(profile=GOOD, provenance="shadow test")
        with patch("episteme.evolution_docker._bounded_run", side_effect=docker_worker) as run:
            self.channel.evaluate(candidate, suite=suite(), backend=self.backend)
        for call in run.call_args_list:
            args, body, timeout = call.args
            self.assertEqual(args[:4], ["docker", "--context", "default", "run"])
            self.assertEqual(args[args.index("--network") + 1], "none")
            self.assertEqual(args[args.index("--pull") + 1], "never")
            self.assertIn("--read-only", args)
            self.assertIn(IMAGE, args)
            self.assertNotIn("--mount", args)
            self.assertEqual(timeout, self.backend.timeout)
            payload = json.loads(body)
            self.assertEqual(payload["base_commit"], "2" * 40)
            self.assertNotIn("expected", payload["payload"])
            self.assertNotIn("critical", payload["payload"])
        with self.assertRaisesRegex(EvaluationError, "differs"):
            DockerBackend("sha256:" + "2" * 64).invoke("policy", {}, dict(backend=self.backend.descriptor))

    def test_process_read_bound_terminates_excessive_output(self):
        with self.assertRaisesRegex(EvaluationError, "stream exceeded"):
            _bounded_run([sys.executable, "-c", f"import sys; sys.stdout.buffer.write(b'x' * {_OUTPUT_BYTES + 1})"],
                         b"", 5)

    def test_bootstrap_overlays_only_candidate_without_changing_base_package(self):
        repo = Path(__file__).resolve().parent.parent
        worker = repo / "examples" / "evolution"
        source = "def recommend(projection, instruction):\n    return {'priorities': {}}\n"
        name = "src/episteme/research_policy.py"
        projection = {"priorities": {"contrast": {"discrimination": 0.7}}}
        # Exercise the same isolated-tree bootstrap with harmless fixture code.
        # Docker enforcement is tested at the launcher boundary separately.
        program = f"import sys\nsys.path.insert(0, {str(worker)!r})\n" + _BOOTSTRAP.replace(
            'dir="/tmp"', f'dir={self.directory!r}')
        args = [sys.executable, "-B", "-c", program, "episteme_evolution_worker"]
        for overlay, expected in (({}, projection), ({name: source}, {"priorities": {}}), ({}, projection)):
            body = canonical(dict(base_commit=None, operation="policy", payload=projection, profile=GOOD,
                                  patch=overlay))
            result = subprocess.run(args, input=body, capture_output=True, check=True, timeout=10)
            self.assertEqual(json.loads(result.stdout), expected)

    @unittest.skipUnless(shutil.which("git"), "Git is needed for the linked worktree fixture")
    def test_developer_changes_are_captured_from_a_linked_worktree(self):
        folder = Path(self.enterContext(TemporaryDirectory()))
        base, candidate = folder / "base", folder / "candidate"
        base.mkdir()

        def git(*args):
            return subprocess.run(["git", "-C", str(base), *args], capture_output=True, check=True,
                                  timeout=10).stdout.decode("utf-8").strip()

        git("init")
        relative = "src/episteme/research_policy.py"
        (base / relative).parent.mkdir(parents=True)
        (base / relative).write_text("old = True\n", encoding="utf-8")
        git("add", relative)
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
        commit = git("rev-parse", "HEAD")
        git("worktree", "add", "--detach", str(candidate), commit)
        (candidate / relative).write_text("old = False\n", encoding="utf-8")
        channel = self.enterContext(EvolutionChannel(folder / "ledger"))
        channel.initialize(OLD, base_commit=commit)
        with self.assertRaisesRegex(ValueError, "separate linked Git worktree"):
            channel.propose_worktree(base, provenance="policy change")
        proposed = channel.propose_worktree(candidate, provenance="policy change")
        self.assertEqual(channel.status()["candidates"][0]["id"], proposed)
        self.assertEqual((base / relative).read_text(encoding="utf-8"), "old = True\n")

    def test_gepa_uses_real_optional_adapter_and_disjoint_release_suite(self):
        try:
            version("gepa")
        except PackageNotFoundError:
            self.skipTest("GEPA extra is not installed")
        with patch("episteme.evolution_docker._bounded_run", side_effect=docker_worker) as worker:
            result = optimize_instructions(self.channel, options={"orchestrator": ["better instruction"]},
                                           training=suite("training"), validation=suite("validation"),
                                           backend=self.backend, max_evaluations=8)
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(result["evaluations"], worker.call_count)
        self.assertLessEqual(worker.call_count, 8)
        self.assertEqual(self.channel.pin()["release"], self.base)
        with self.assertRaisesRegex(ValueError, "contains an optimization case"):
            self.channel.evaluate(result["candidate"], suite=suite("training"), backend=self.backend)
        with patch("episteme.evolution_docker._bounded_run", side_effect=docker_worker):
            self.channel.evaluate(result["candidate"], suite=suite(), backend=self.backend)
        self.assertEqual(self.channel.advance()["status"], "activated")

    def test_automatic_campaign_starts_once_after_two_distinct_signals(self):
        self.channel.configure(options={"orchestrator": ["better instruction"]}, training=suite("dev"),
                               validation=suite("validation"), suite=suite(), backend=self.backend,
                               max_evaluations=8)
        with patch("episteme.evolution_gepa.require_gepa"), \
                patch("episteme.evolution_gepa.optimize_instructions", side_effect=KeyboardInterrupt) as optimizer:
            self.channel.record_signal(kind="verified_failure", key="blocked", source="a" * 64)
            self.channel.record_signal(kind="verified_failure", key="blocked", source="a" * 64)
            self.assertEqual(self.channel.advance()["status"], "no_eligible_candidate")
            optimizer.assert_not_called()
            self.channel.record_signal(kind="verified_failure", key="blocked", source="b" * 64)
            with self.assertRaises(KeyboardInterrupt):
                self.channel.advance()
            self.assertEqual(self.channel.advance()["status"], "no_eligible_candidate")
            optimizer.assert_called_once()
        self.assertIsNone(self.channel.status()["campaigns"][0]["completion"])

    def test_config_fixes_the_acceptance_suite_before_proposals(self):
        args = dict(options={"orchestrator": ["better instruction"]}, training=suite("dev"),
                    validation=suite("validation"), backend=self.backend, max_evaluations=8)
        with self.assertRaisesRegex(ValueError, "suite overlaps"):
            self.channel.configure(**args, suite=suite("dev"))
        self.channel.configure(**args, suite=suite())
        with self.assertRaisesRegex(ValueError, "frozen before candidate proposals"):
            self.channel.configure(**args, suite=suite("changed"))
        candidate = self.channel.propose(profile=GOOD, provenance="test proposal")
        with patch("episteme.evolution_docker._bounded_run") as worker:
            with self.assertRaisesRegex(ValueError, "frozen campaign suite"):
                self.channel.evaluate(candidate, suite=suite("changed"), backend=self.backend)
            worker.assert_not_called()

    def test_trigger_runs_gepa_then_independent_gate_and_activates(self):
        try:
            version("gepa")
        except PackageNotFoundError:
            self.skipTest("GEPA extra is not installed")
        self.channel.configure(options={"orchestrator": ["better instruction"]}, training=suite("dev"),
                               validation=suite("validation"), suite=suite(), backend=self.backend,
                               max_evaluations=8)
        for source in ("a", "b"):
            self.channel.record_signal(kind="measured_resource_loss", key="inefficient", source=source * 64)
        with patch("episteme.evolution_docker._bounded_run", side_effect=docker_worker) as worker:
            result = self.channel.advance()
        self.assertEqual(result["status"], "activated")
        self.assertEqual(result["optimization"]["evaluations"], 8)
        self.assertEqual(worker.call_count, 12)
        self.assertEqual(result["evaluation"]["score"]["correctness_gain"], 1)
        self.assertEqual(self.channel.pin()["profile"], GOOD)
        self.assertEqual(self.channel.status()["campaigns"][0]["completion"]["result"]["status"], "activated")


if __name__ == "__main__":
    unittest.main()
