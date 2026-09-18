"""Real small CPU jobs and crash boundaries; no scientific reviewer fixtures."""

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from episteme.commands import CommandService
from episteme.execution import Execution, _index, _workspace, freeze_environment, job_state, reconcile_job, work_job
from episteme.graph import ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.recovery import backup, restore
from episteme.reporting import export_store
from episteme.store import Store, canonical


SOURCE = b'''import json, pathlib, sys
raw = pathlib.Path(sys.argv[1]).read_bytes()
pathlib.Path("raw.bin").write_bytes(raw)
pathlib.Path("metrics.json").write_text(json.dumps({"value": json.loads(raw)["value"]}))
print("actual CPU fixture completed")
'''


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "research"
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.actor = Actor("fixture-executor", "executor")
        self.source = self.store.put(SOURCE)
        self.environment = freeze_environment(self.store)
        self.data = self.store.put_json({"value": 2})
        self.planner = Kernel(self.store, Actor("fixture-planner", "planner"))
        self.scope = {"mode": "execution_contract_fixture"}
        self.pool = [self.planner.hypothesis(text, "prediction", "falsifier", self.scope)
                     for text in ("known fixture effect", "fixture null alternative")]
        self.protocol = self.plan()

    def plan(self, source=None, run_limit=4):
        return self.planner.preregister(hypotheses=self.pool, scope=self.scope, design="small local fixture",
            metric="value", analysis_plan="read the fixture value", stopping_rule="registered attempts only",
            seeds=[1], run_limit=run_limit, implementation=source or self.source,
            environment=self.environment, data=self.data, replication_tolerance=0)

    def envelope(self, action, payload, actor=None, store=None):
        actor, store = actor or self.actor, store or self.store
        return dict(context=dict(command_id=uuid4().hex, expected_revision=len(store.events()),
            actor=actor.id, role=actor.role, study_id="fixture-study", correlation_id="fixture-cycle", causation_id=None),
            request=dict(version=1, action=action, payload=payload))

    def enqueue(self, protocol=None, actor=None, **kwargs):
        payload = dict(protocol=protocol or self.protocol, seed=1, outputs={"raw_data": "raw.bin", "metrics": "metrics.json"},
                       wall_seconds=10, max_output_bytes=65536)
        payload.update(kwargs)
        envelope = self.envelope("execution.enqueue", payload, actor)
        return CommandService(self.store).execute(envelope), envelope

    def test_real_execution_replay_graph_export_and_backup(self):
        job, request = self.enqueue()
        self.assertEqual(CommandService(self.store).execute(request), job)
        self.assertEqual(job_state(self.store, job)["status"], "queued")
        completed = work_job(self.store, job)
        result = Kernel._get(self.store.events(), completed["result"], "result")["payload"] if completed["result"] else {}
        self.assertEqual(completed["status"], "completed", self.store.read(result["outputs"]["log"]).decode() if result else completed)
        self.assertEqual(completed["scientific_validity"], "not_assessed")
        before = self.store.export(), self.store.export_receipts()
        self.assertEqual(work_job(self.store, job), completed)
        self.assertEqual(reconcile_job(self.store, job), completed)
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))
        graph = ResearchGraph.from_store(self.store).to_dict()
        self.assertIn("execution_finalized", {n["kind"] for n in graph["nodes"]})
        files = export_store(self.store)
        self.assertTrue(files)
        snapshot, target = Path(self.temp.name) / "backup", Path(self.temp.name) / "restored"
        backup(self.store, snapshot)
        restore(snapshot, target)
        with Store(target) as restored:
            self.assertEqual(ResearchGraph.from_store(restored).to_dict(), graph)
            self.assertEqual(work_job(restored, job), completed)

    def test_direct_finish_and_direct_nontransactional_enqueue_are_rejected(self):
        job, request = self.enqueue()
        run = job_state(self.store, job)["run"]
        before = self.store.export()
        with self.assertRaisesRegex(ValueError, "managed runs"):
            Kernel(self.store, self.actor).finish_run(run, status="failed", outputs={}, reason="bypass")
        with self.assertRaisesRegex(ValueError, "CommandService"):
            Execution(self.store, self.actor).enqueue(**request["request"]["payload"])
        self.assertEqual(before, self.store.export())

    def test_intent_before_spawn_remains_unknown_and_never_relaunches(self):
        job, _ = self.enqueue()
        envelope = self.envelope("execution.dispatch", dict(job=job, workspace_token=uuid4().hex))
        dispatch = CommandService(self.store).execute(envelope)
        self.assertEqual(CommandService(self.store).execute(envelope), dispatch)
        with patch("episteme.execution.subprocess.Popen") as spawn:
            self.assertEqual(work_job(self.store, job)["status"], "unknown")
            spawn.assert_not_called()
        with self.assertRaisesRegex(ValueError, "already dispatched"):
            CommandService(self.store).execute(self.envelope("execution.dispatch", dict(job=job, workspace_token=uuid4().hex)))
        self.assertFalse(any(e["kind"] == "result" for e in self.store.events()))
        ResearchGraph.from_store(self.store)

    def test_completion_after_controller_failure_is_reconciled_without_spawn(self):
        job, _ = self.enqueue()
        with patch("episteme.execution.reconcile_job", side_effect=RuntimeError("controller interrupted")):
            with self.assertRaisesRegex(RuntimeError, "controller interrupted"):
                work_job(self.store, job)
        self.assertEqual(job_state(self.store, job)["status"], "unknown")
        with Store(self.root) as reopened, patch("episteme.execution.subprocess.Popen") as spawn:
            self.assertEqual(reconcile_job(reopened, job)["status"], "completed")
            spawn.assert_not_called()

    def test_actual_controller_kill_does_not_lose_worker_completion(self):
        source = self.store.put(b"import time\ntime.sleep(0.5)\n" + SOURCE)
        job, _ = self.enqueue(protocol=self.plan(source))
        process = subprocess.Popen([sys.executable, "-m", "episteme", "execution", "work", job, "--root", str(self.root)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 15
            workspace = None
            while time.monotonic() < deadline:
                state = _index(self.store, self.store.events())[job]
                if state["dispatch"]:
                    workspace = _workspace(self.store, state)
                    if (workspace / "started.json").exists():
                        break
                time.sleep(0.03)
            self.assertIsNotNone(workspace)
            self.assertTrue((workspace / "started.json").exists())
            process.kill()
            process.communicate(timeout=5)
            while time.monotonic() < deadline and not (workspace / "completion.json").exists():
                time.sleep(0.03)
            self.assertTrue((workspace / "completion.json").exists())
            self.assertEqual(reconcile_job(self.store, job)["status"], "completed")
            self.assertEqual(len([e for e in self.store.events() if e["kind"] == "run"]), 1)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_failures_preserve_exit_logs_inputs_and_no_false_completion(self):
        cases = {"exit": b"print('failure log'); raise SystemExit(3)",
                 "missing": b"print('outputs omitted')",
                 "metric": SOURCE + b"\npathlib.Path('metrics.json').write_text('{}')",
                 "input": SOURCE + b"\npathlib.Path('input.dat').write_text('changed')",
                 "timeout": b"import time\ntime.sleep(30)",
                 "capture": b"print('x'*100000)",
                 "output_limit": SOURCE + b"\npathlib.Path('raw.bin').write_bytes(b'x'*100000)"}
        for name, source in cases.items():
            with self.subTest(name=name):
                job, _ = self.enqueue(protocol=self.plan(self.store.put(source)), wall_seconds=1, max_output_bytes=4096)
                state = work_job(self.store, job)
                self.assertEqual(state["status"], "failed", state)
                result = Kernel._get(self.store.events(), state["result"], "result")["payload"]
                self.assertTrue(result["reason"])
                self.assertIn("log", result["outputs"])
                ResearchGraph.from_store(self.store)

    def test_corrupted_completion_and_outputs_cannot_finalize(self):
        job, _ = self.enqueue()
        with patch("episteme.execution.reconcile_job", return_value={}):
            work_job(self.store, job)
        state = _index(self.store, self.store.events())[job]
        workspace = _workspace(self.store, state)
        path = workspace / "completion.json"
        original = path.read_bytes()
        record = json.loads(original)
        record["identity"]["dispatch"] = "another-dispatch"
        path.write_bytes(canonical(record))
        with self.assertRaisesRegex(ValueError, "identity"):
            reconcile_job(self.store, job)
        path.write_bytes(original)
        (workspace / "raw.bin").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            reconcile_job(self.store, job)
        self.assertEqual(job_state(self.store, job)["status"], "unknown")

    def test_unsupported_capabilities_and_unsafe_paths_fail_before_reservation(self):
        for changes in ({"required_capabilities": ["network_isolation"]},
                        {"outputs": {"raw_data": "../outside", "metrics": "m.json"}},
                        {"outputs": {"raw_data": "RAW.bin", "metrics": "raw.bin"}},
                        {"outputs": {"raw_data": "CON.txt", "metrics": "m.json"}},
                        {"wall_seconds": 0}):
            with self.subTest(changes=changes):
                before = self.store.export(), self.store.export_receipts()
                with self.assertRaises(ValueError):
                    self.enqueue(**changes)
                self.assertEqual(before, (self.store.export(), self.store.export_receipts()))

    def test_two_writers_compete_for_last_attempt_slot(self):
        protocol = self.plan(run_limit=2)
        self.enqueue(protocol=protocol)  # Keep one queued attempt occupying the first slot.
        payload = dict(protocol=protocol, seed=1, outputs={"raw_data": "raw.bin", "metrics": "metrics.json"},
                       wall_seconds=10, max_output_bytes=4096)
        requests = [self.envelope("execution.enqueue", payload) for _ in range(2)]
        barrier = threading.Barrier(2)
        outcomes = []
        def writer(envelope):
            with Store(self.root) as store:
                barrier.wait(timeout=10)
                try:
                    outcomes.append(CommandService(store).execute(envelope))
                except ValueError as exc:
                    outcomes.append(exc)
        threads = [threading.Thread(target=writer, args=(request,)) for request in requests]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(sum(isinstance(item, str) for item in outcomes), 1)
        self.assertEqual(len([e for e in self.store.events() if e["kind"] == "execution_job"]), 2)
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            self.enqueue(protocol=protocol)

    def test_reanalysis_actual_same_data_different_implementation_and_review_boundary(self):
        job, _ = self.enqueue()
        primary = work_job(self.store, job)
        replica, _ = self.enqueue(actor=Actor("fixture-reanalyst", "replicator"),
            implementation=self.store.put(SOURCE + b"\n# separately executed contract fixture, not independent AI\n"),
            replicate_of=primary["run"])
        repeated = work_job(self.store, replica)
        claim = Kernel(self.store, Actor("fixture-analyst", "analyst")).claim(protocol=self.protocol,
            statement="fixture value was preserved", scope=self.scope, evidence=[primary["run"], repeated["run"]],
            limitations=["synthetic engineering fixture; no independent scientific reasoning"], outcome="inconclusive")
        reader = Kernel(self.store, Actor("reader", "observer"))
        gate = reader.gate(claim)
        self.assertTrue(gate["passed"], gate)
        self.assertEqual(reader.next_action(claim)["action"], "scientific_review")
        context = reader._local_evidence(self.store.events(), claim)[0]
        self.assertEqual(len([e for e in context if e["kind"].startswith("execution_")]), 6)
        ResearchGraph.from_store(self.store)
        self.assertFalse(any(e["kind"] == "review" for e in self.store.events()))

    def test_real_cli_work_status_and_reconcile(self):
        job, _ = self.enqueue()
        for operation, expected in (("status", "queued"), ("work", "completed"), ("reconcile", "completed")):
            process = subprocess.run([sys.executable, "-m", "episteme", "execution", operation, job,
                "--root", str(self.root)], capture_output=True, text=True, timeout=25)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout)["status"], expected)

    def test_concurrent_controllers_launch_only_one_payload(self):
        source = self.store.put(SOURCE + b"\npathlib.Path('count.txt').open('a').write('once')\n")
        job, _ = self.enqueue(protocol=self.plan(source))
        command = [sys.executable, "-m", "episteme", "execution", "work", job, "--root", str(self.root)]
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        for process in processes:
            output, error = process.communicate(timeout=25)
            self.assertIn(process.returncode, (0, 2), error)
        self.assertEqual(reconcile_job(self.store, job)["status"], "completed")
        state = _index(self.store, self.store.events())[job]
        self.assertEqual((_workspace(self.store, state) / "count.txt").read_text(), "once")
        self.assertEqual(len([e for e in self.store.events() if e["kind"] == "execution_dispatch"]), 1)

    def test_finalization_receipt_replays_and_modified_body_conflicts(self):
        job, _ = self.enqueue()
        work_job(self.store, job)
        receipt = next(r for r in self.store.receipts() if r["request"]["action"] == "execution.finalize")
        envelope = dict(context=receipt["context"], request=receipt["request"])
        before = self.store.export()
        self.assertEqual(CommandService(self.store).execute(envelope), receipt["result"])
        envelope["request"]["payload"]["manifest"] = self.store.put_json({"other": "completion"})
        with self.assertRaisesRegex(ValueError, "different request"):
            CommandService(self.store).execute(envelope)
        self.assertEqual(before, self.store.export())

    def test_unknown_backup_does_not_restore_launch_authority(self):
        job, _ = self.enqueue()
        CommandService(self.store).execute(self.envelope("execution.dispatch", dict(job=job, workspace_token=uuid4().hex)))
        snapshot, target = Path(self.temp.name) / "unknown-backup", Path(self.temp.name) / "unknown-restored"
        backup(self.store, snapshot)
        restore(snapshot, target)
        with Store(target) as restored, patch("episteme.execution.subprocess.Popen") as spawn:
            self.assertEqual(work_job(restored, job)["status"], "unknown")
            spawn.assert_not_called()

    def test_graph_rejects_a_managed_result_without_finalization(self):
        job, _ = self.enqueue()
        run = job_state(self.store, job)["run"]
        self.store.append(id="forged-terminal", kind="result", actor=self.actor.id, role=self.actor.role,
            payload=dict(run=run, status="failed", outputs={}, reason="inconsistent low-level fixture write"),
            expected_revision=len(self.store.events()))
        with self.assertRaisesRegex(ValueError, "no verified execution finalization"):
            ResearchGraph.from_store(self.store)


if __name__ == "__main__":
    unittest.main()
