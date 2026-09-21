"""Durable multi-seed scheduling boundaries, using small synthetic processes."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from episteme.batch import batch_state
from episteme.batch_controller import advance_batch
from episteme.commands import CommandService
from episteme.execution import freeze_environment, job_state, work_job
from episteme.graph import ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.recovery import backup, restore
from episteme.reporting import artifact_inventory, export_store
from episteme.search import COMPONENTS, Search
from episteme.store import Store


PRIMARY = b'''import json, pathlib, sys
raw = pathlib.Path(sys.argv[1]).read_bytes()
pathlib.Path("raw.json").write_bytes(raw)
pathlib.Path("metrics.json").write_text(json.dumps({"mean": sum(json.loads(raw))/len(json.loads(raw))}))
'''
REANALYSIS = b'''import json, pathlib, statistics, sys
raw = pathlib.Path(sys.argv[1]).read_bytes()
pathlib.Path("raw.json").write_bytes(raw)
pathlib.Path("metrics.json").write_text(json.dumps({"mean": statistics.mean(json.loads(raw))}))
'''


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "state"
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.planner = Actor("protocol-planner", "planner")
        self.manager = Actor("batch-manager", "planner")
        self.executor = Actor("executor", "executor")
        self.replicator = Actor("replicator", "replicator")
        self.kernel = Kernel(self.store, self.planner)
        self.search = Search(self.store, self.planner)
        self.scope = {"data": "synthetic batch fixture"}
        self.pool = [self.kernel.hypothesis(text, "prediction", "falsifier", self.scope)
                     for text in ("positive mean", "nonpositive mean")]
        self.source = self.store.put(PRIMARY)
        self.recode = self.store.put(REANALYSIS)
        self.environment = freeze_environment(self.store)
        self.outputs = {"raw_data": "raw.json", "metrics": "metrics.json"}

    def envelope(self, action, payload, actor=None, store=None):
        actor, store = actor or self.manager, store or self.store
        return dict(context=dict(command_id=uuid4().hex, expected_revision=len(store.events()),
            actor=actor.id, role=actor.role, study_id="batch-fixture", correlation_id="batch-cycle", causation_id=None),
            request=dict(version=1, action=action, payload=payload))

    def command(self, action, payload, actor=None):
        return CommandService(self.store).execute(self.envelope(action, payload, actor))

    def prepare(self, seeds=(1, 2), source=None, recode=None, cost_unit="enqueued_attempt", cost=None):
        self.protocol = self.kernel.preregister(hypotheses=self.pool, scope=self.scope,
            design="Fixed synthetic values", metric="mean", analysis_plan="Compute and independently reanalyse mean",
            stopping_rule="one primary and one reanalysis per registered seed", seeds=list(seeds),
            run_limit=2*len(seeds), implementation=source or self.source, environment=self.environment,
            data=self.store.put_json([1, 2, 3]), replication_tolerance=0)
        self.tree = self.search.register_tree(weights={key: 1 for key in COMPONENTS}, cost_weight=0,
            budget=2*len(seeds), cost_unit=cost_unit, max_nodes=4, max_depth=3, max_width=3,
            max_selections=3, max_retries=1)
        self.node = self.search.add_node(self.tree, protocol=self.protocol, action="discriminate",
            components=dict(discrimination=1, uncertainty=1, coverage=1, invalidity_risk=0),
            estimated_cost=2*len(seeds) if cost is None else cost, rationale="Synthetic scheduling test")
        self.selection = self.search.select_next(self.tree)["id"]
        return dict(selection=self.selection, executor=self.executor.id, replicator=self.replicator.id,
            reanalysis_implementation=recode or self.recode, reanalysis_environment=self.environment,
            outputs=self.outputs, wall_seconds=10, max_output_bytes=65536)

    def plan(self, **options):
        return self.command("batch.plan", self.prepare(**options))

    def enqueue(self, batch, slot="primary:1", actor=None):
        return self.command("batch.enqueue_slot", dict(batch=batch, slot=slot), actor or self.executor)

    def test_full_roster_real_processes_reopen_receipts_graph_and_no_review(self):
        envelope = self.envelope("batch.plan", self.prepare())
        batch = CommandService(self.store).execute(envelope)
        before = artifact_inventory(self.store, self.store.events())
        self.assertIn(self.recode, {item["sha256"] for item in before})
        result = advance_batch(self.store, batch)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["next_action"], "awaiting_analysis")
        self.assertEqual(result["scientific_validity"], "not_assessed")
        self.assertEqual([s["status"] for s in result["slots"]], ["completed"]*4)
        self.assertEqual(len({s["run"] for s in result["slots"]}), 4)
        budget = self.search.tree_state(self.tree)
        self.assertEqual(budget["reserved"], "0")
        self.assertEqual(budget["spent"], "4")
        state = self.store.export(), self.store.export_receipts()
        self.assertEqual(CommandService(self.store).execute(envelope), batch)
        with Store(self.root) as reopened:
            self.assertEqual(advance_batch(reopened, batch), result)
            self.assertEqual(state, (reopened.export(), reopened.export_receipts()))
            ResearchGraph.from_store(reopened)
            export_store(reopened)
        self.assertFalse(any(e["kind"] in {"claim", "review", "paper"} for e in self.store.events()))

    def test_attempt_quota_ownership_wrong_actor_and_legacy_settlement(self):
        batch = self.plan()
        before = self.store.export()
        with self.assertRaises(ValueError):
            Kernel(self.store, self.executor).start_run(self.protocol, seed=1, implementation=self.source,
                environment=self.environment, command=["python", "outside.py"])
        with self.assertRaises(ValueError):
            self.command("execution.enqueue", dict(protocol=self.protocol, seed=1, outputs=self.outputs,
                wall_seconds=10, max_output_bytes=1024), self.executor)
        with self.assertRaises(ValueError):
            self.enqueue(batch, actor=Actor("other-executor", "executor"))
        with self.assertRaisesRegex(ValueError, "batch"):
            self.search.finish_selection(self.selection, status="cancelled", actual_cost=0, reason="bypass")
        self.assertEqual(before, self.store.export())

    def test_slot_receipt_atomicity_lost_response_and_duplicate_new_command(self):
        batch = self.plan()
        envelope = self.envelope("batch.enqueue_slot", dict(batch=batch, slot="primary:1"), self.executor)
        original = self.store.append
        def fail_binding(*args, **kwargs):
            if kwargs.get("kind") == "batch_slot" or (args and args[0] == "batch_slot"):
                raise RuntimeError("binding crash")
            return original(*args, **kwargs)
        before = self.store.export(), self.store.export_receipts()
        with patch.object(self.store, "append", side_effect=fail_binding):
            with self.assertRaisesRegex(RuntimeError, "binding crash"):
                CommandService(self.store).execute(envelope)
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))
        binding = CommandService(self.store).execute(envelope)
        self.assertEqual(CommandService(self.store).execute(envelope), binding)
        with self.assertRaises(ValueError):
            self.enqueue(batch)
        receipt = self.store.receipts()[-1]
        self.assertEqual([next(e["kind"] for e in self.store.events() if e["id"] == id) for id in receipt["event_ids"]],
                         ["run", "execution_job", "batch_slot"])

    def test_unknown_keeps_full_reserve_and_resume_does_not_spawn(self):
        batch = self.plan()
        self.enqueue(batch)
        job = batch_state(self.store, batch)["slots"][0]["job"]
        self.command("execution.dispatch", dict(job=job, workspace_token=uuid4().hex), self.executor)
        with patch("episteme.execution.subprocess.Popen") as spawn:
            state = advance_batch(self.store, batch)
            self.assertEqual(state["slots"][0]["status"], "unknown")
            spawn.assert_not_called()
        with self.assertRaises(ValueError):
            self.command("batch.settle", dict(batch=batch))
        self.assertEqual(self.search.tree_state(self.tree)["reserved"], "4")

    def test_failed_primary_blocks_only_dependency_preserves_other_seed_and_cost(self):
        source = self.store.put(b'import sys\nif sys.argv[-1] == "1": raise SystemExit(3)\n' + PRIMARY)
        batch = self.plan(source=source)
        state = advance_batch(self.store, batch)
        slots = {s["slot"]: s for s in state["slots"]}
        self.assertEqual(state["status"], "failed")
        self.assertEqual(slots["primary:1"]["status"], "failed")
        self.assertEqual(slots["reanalysis:1"]["status"], "blocked_dependency")
        self.assertIsNone(slots["reanalysis:1"]["run"])
        self.assertEqual(slots["primary:2"]["status"], "completed")
        self.assertEqual(slots["reanalysis:2"]["status"], "completed")
        self.assertEqual(state["enqueued_attempts"], 3)
        self.assertEqual(self.search.tree_state(self.tree)["spent"], "3")
        self.assertEqual(self.search.tree_state(self.tree)["reserved"], "0")
        ResearchGraph.from_store(self.store)

    def test_completed_batch_does_not_hide_metric_disagreement_or_approve_claim(self):
        self.search = Search(self.store, Actor("experiment-selector", "planner"))
        bad = self.store.put(REANALYSIS.replace(b"statistics.mean(json.loads(raw))", b"99"))
        batch = self.plan(seeds=(1,), recode=bad)
        state = advance_batch(self.store, batch)
        self.assertEqual(state["status"], "completed")
        claim = Kernel(self.store, Actor("analyst", "analyst")).claim(protocol=self.protocol,
            statement="Synthetic inconsistent result", scope=self.scope, outcome="inconclusive",
            evidence=[s["run"] for s in state["slots"]], limitations=["Fixture mismatch"])
        self.assertFalse(self.kernel.gate(claim)["passed"])
        self.assertNotEqual(self.kernel.next_action(claim)["action"], "paper_candidate")
        contributors = self.kernel._review_members(self.store.events(), claim)[1]
        self.assertIn(self.manager.id, contributors)
        self.assertIn("experiment-selector", contributors)

    def test_stale_selection_and_invalid_units_rejected_before_writes(self):
        options = self.prepare()
        Kernel(self.store, self.executor).start_run(self.protocol, seed=1, implementation=self.source,
            environment=self.environment, command=["python", "outside.py"])
        before = self.store.export()
        with self.assertRaises(ValueError):
            self.command("batch.plan", options)
        self.assertEqual(before, self.store.export())
        for kwargs in ({"cost_unit": "USD"}, {"cost": 1}):
            with self.subTest(kwargs=kwargs):
                options = self.prepare(**kwargs)
                before = self.store.export()
                with self.assertRaises(ValueError):
                    self.command("batch.plan", options)
                self.assertEqual(before, self.store.export())

    def test_concurrent_slot_admission_creates_one_attempt(self):
        batch = self.plan()
        barrier = threading.Barrier(2)
        def submit(_):
            with Store(self.root) as store:
                envelope = self.envelope("batch.enqueue_slot", dict(batch=batch, slot="primary:1"), self.executor, store)
                barrier.wait()
                try:
                    return CommandService(store).execute(envelope)
                except ValueError:
                    return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, range(2)))
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertEqual(len([e for e in self.store.events() if e["kind"] == "run"]), 1)
        ResearchGraph.from_store(self.store)

    def test_queued_backup_cannot_automatically_execute_after_original_progress(self):
        batch = self.plan(seeds=(1,))
        self.enqueue(batch)
        snapshot, target = Path(self.temp.name)/"backup", Path(self.temp.name)/"restored"
        backup(self.store, snapshot)
        self.assertEqual(advance_batch(self.store, batch)["status"], "completed")
        restore(snapshot, target)
        with Store(target) as restored, patch("episteme.execution.subprocess.Popen") as spawn:
            before = restored.export()
            with self.assertRaises(ValueError):
                advance_batch(restored, batch)
            job = batch_state(restored, batch)["slots"][0]["job"]
            with self.assertRaises(ValueError):
                work_job(restored, job)
            with self.assertRaises(ValueError):
                CommandService(restored).execute(self.envelope("execution.dispatch",
                    dict(job=job, workspace_token=uuid4().hex), self.executor, restored))
            spawn.assert_not_called()
            self.assertEqual(before, restored.export())
            self.assertEqual(batch_state(restored, batch)["slots"][0]["status"], "queued")
            ResearchGraph.from_store(restored)

    def test_real_cli_advances_and_status_replays_no_computations(self):
        batch = self.plan(seeds=(1,))
        command = [sys.executable, "-m", "episteme", "batch", "advance", batch, "--root", str(self.root)]
        process = subprocess.run(command, capture_output=True, text=True, timeout=60)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["status"], "completed")
        before = self.store.export()
        command[4] = "status"
        process = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), result)
        self.assertEqual(before, self.store.export())

    def test_resume_after_verified_result_and_atomic_settlement_failure(self):
        batch = self.plan(seeds=(1,))
        calls = []
        def interrupt_after_result(store, job):
            result = work_job(store, job)
            calls.append(job)
            raise RuntimeError("controller lost result response")
        with patch("episteme.batch_controller.work_job", side_effect=interrupt_after_result):
            with self.assertRaisesRegex(RuntimeError, "lost result response"):
                advance_batch(self.store, batch)
        self.assertEqual(batch_state(self.store, batch)["slots"][0]["status"], "completed")
        self.enqueue(batch, "reanalysis:1", self.replicator)
        job = batch_state(self.store, batch)["slots"][1]["job"]
        self.assertNotIn(job, calls)
        self.assertEqual(work_job(self.store, job)["status"], "completed")
        original = self.store.append
        def interrupt_terminal(**kwargs):
            if kwargs["kind"] == "search_terminal":
                raise RuntimeError("settlement transaction interrupted")
            return original(**kwargs)
        before = self.store.export(), self.store.export_receipts()
        with patch.object(self.store, "append", side_effect=interrupt_terminal):
            with self.assertRaisesRegex(RuntimeError, "transaction interrupted"):
                advance_batch(self.store, batch)
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))
        with patch("episteme.batch_controller.work_job") as launch:
            self.assertEqual(advance_batch(self.store, batch)["status"], "completed")
            launch.assert_not_called()
        ResearchGraph.from_store(self.store)

    def test_selector_is_bound_to_evidence_and_cannot_review_own_experiment(self):
        self.search = Search(self.store, Actor("experiment-selector", "planner"))
        batch = self.plan(seeds=(1,))
        state = advance_batch(self.store, batch)
        claim = Kernel(self.store, Actor("analyst", "analyst")).claim(protocol=self.protocol,
            statement="The explicitly synthetic mean is 2", scope=self.scope, outcome="inconclusive",
            evidence=[s["run"] for s in state["slots"]], limitations=["Engineering fixture"])
        gate = self.kernel.gate(claim)
        self.assertTrue(gate["passed"], gate)
        before = self.store.export()
        with self.assertRaisesRegex(ValueError, "independent of contributors"):
            Kernel(self.store, Actor("experiment-selector", "reviewer")).review(claim,
                verdict="request_changes", rationale="Self-review must be rejected",
                actions=["External review needed"], expected_basis=gate["basis_hash"])
        self.assertEqual(before, self.store.export())
        Kernel(self.store, Actor("external-fixture-reviewer", "reviewer")).review(claim,
            verdict="request_changes", rationale="Synthetic review-boundary test only",
            actions=["Obtain an actual independent scientific assessment"], expected_basis=gate["basis_hash"])
        graph = ResearchGraph.from_store(self.store).to_dict()
        self.assertTrue(any(e["relation"] == "review_batch" and e["source"] == self.selection
                            for e in graph["edges"]))
