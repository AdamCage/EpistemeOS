"""A recorded review follow-up becomes one selected, reserved batch."""

from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from episteme.batch import batch_state
from episteme.batch_controller import advance_batch
from episteme.cli import main as cli_main
from episteme.commands import CommandService
from episteme.execution import freeze_environment
from episteme.followup import followup_state
from episteme.followup_execution import validate_prepared
from episteme.graph import ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.planning import Planning
from episteme.recovery import backup, restore
from episteme.search import COMPONENTS, Search
from episteme.store import Store


class FollowupExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="episteme-followup-batch-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "research"
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.study = "followup-batch-fixture"
        self.planner = Actor("fixture-planner", "planner")
        self.reviewer = Actor("fixture-reviewer", "reviewer")
        self.executor = Actor("fixture-executor", "executor")
        self.replicator = Actor("fixture-replicator", "replicator")
        self.analyst = Actor("fixture-analyst", "analyst")
        self.scope = {"population": "synthetic follow-up fixture"}
        self.source = self.store.put(b"source fixture program")
        self.child_source = self.store.put(b"child fixture program")
        self.reanalysis = self.store.put(b"distinct fixture reanalysis program")
        self.environment = freeze_environment(self.store)
        self.data = self.store.put_json({"values": [1, 3]})

        planning = Planning(self.store, self.planner)
        self.question = planning.question(study_id=self.study, statement="Which mechanism explains the value?",
            objective="Test a review-driven child experiment", scope=self.scope,
            constraints=["Synthetic fixture"], stopping_criteria=["Stop after one control"])
        kernel = Kernel(self.store, self.planner)
        hypotheses = [kernel.hypothesis(text, "distinct prediction", "falsifying observation", self.scope)
                      for text in ("Null mechanism", "Alternative mechanism")]
        self.explanation_set = planning.explanation_set(question=self.question, hypotheses=hypotheses,
            comparison_plan="Compare a control against the source observation")
        self.source_protocol = kernel.preregister_for_set(explanation_set=self.explanation_set,
            design="Frozen source comparison", metric="mean", analysis_plan="Compute fixture mean",
            stopping_rule="One seed and reanalysis", seeds=[7], run_limit=4,
            implementation=self.source, environment=self.environment, data=self.data,
            replication_tolerance=0)
        self.search = Search(self.store, self.planner)
        self.tree = self.search.register_tree(weights={key: 1 for key in COMPONENTS},
            cost_weight=0, budget=6, cost_unit="enqueued_attempt", max_nodes=4,
            max_depth=2, max_width=2, max_selections=3, max_retries=0)
        self.source_node = self.search.add_node(self.tree, protocol=self.source_protocol,
            action="discriminate", components=self.components(), estimated_cost=2,
            rationale="Source synthetic experiment")
        source_selection = self.search.select_next(self.tree)["id"]
        primary = Kernel(self.store, self.executor).start_run(self.source_protocol, seed=7,
            implementation=self.source, environment=self.environment, command=["python", "source.py"])
        self._finish(primary, self.executor)
        second = Kernel(self.store, self.replicator).start_run(self.source_protocol, seed=7,
            implementation=self.reanalysis, environment=self.environment,
            command=["python", "reanalysis.py"], replicate_of=primary)
        self._finish(second, self.replicator)
        claim = Kernel(self.store, self.analyst).claim(protocol=self.source_protocol,
            statement="The fixture value is positive", scope=self.scope,
            evidence=[primary, second], limitations=["Synthetic fixture only"], outcome="supports")
        self.search.finish_selection(source_selection, status="completed", actual_cost=2,
            reason="Synthetic source completed; review pending", run=primary, claim=claim)
        self.basis = Kernel(self.store, self.reviewer).gate(claim)["basis_hash"]
        review = self.command("replanning.record_review", self.reviewer,
            dict(claim=claim, verdict="request_changes", rationale="Potential confound",
                 findings=[dict(kind="discriminating_experiment", action="Add a control",
                                closure_criterion="A reviewed control addresses the confound",
                                evidence_refs=[claim])], expected_basis=self.basis,
                 link_assessments=None))
        self.claim, self.obligation = claim, review["obligations"][0]
        self.followup = self.command("followup.apply", self.planner, dict(
            obligation=self.obligation, parent_node=self.source_node,
            explanation_set=self.explanation_set,
            protocol_spec=dict(design="Frozen control", metric="mean",
                analysis_plan="Compute control mean", stopping_rule="One seed and reanalysis",
                seeds=[8], run_limit=2, implementation=self.child_source,
                environment=self.environment, data=self.data, replication_tolerance=0),
            node_spec=dict(action="discriminate", components=self.components(),
                estimated_cost=2, rationale="Resolve the recorded confound"),
            expected_basis=self.basis))

    @staticmethod
    def components():
        return dict(discrimination=1.0, uncertainty=0.5, coverage=0.5, invalidity_risk=0.0)

    def _finish(self, run, actor):
        Kernel(self.store, actor).finish_run(run, status="completed",
            outputs=dict(raw_data=self.data, metrics=self.store.put_json({"mean": 2.0}),
                         log=self.store.put(b"completed fixture")))

    def envelope(self, action, actor, payload):
        return dict(context=dict(command_id=uuid4().hex, expected_revision=len(self.store.events()),
            actor=actor.id, role=actor.role, study_id=self.study,
            correlation_id="followup-batch-fixture", causation_id=self.followup if hasattr(self, "followup") else None),
            request=dict(version=1, action=action, payload=payload))

    def command(self, action, actor, payload):
        return CommandService(self.store).execute(self.envelope(action, actor, payload))

    def prepare(self, **changes):
        payload = dict(obligation=self.obligation, executor="child-executor",
                       replicator="child-replicator", reanalysis_implementation=self.reanalysis,
                       reanalysis_environment=self.environment,
                       outputs={"raw_data": "raw.json", "metrics": "metrics.json"},
                       wall_seconds=10, max_output_bytes=65536)
        payload.update(changes)
        return self.envelope("followup.prepare_next", self.planner, payload)

    def snapshot(self):
        return self.store.export(), self.store.export_receipts()

    def test_atomic_selection_and_batch_receipt_preserves_open_obligation(self):
        envelope = self.prepare()
        before = len(self.store.events())
        with patch("episteme.execution.subprocess.Popen") as worker:
            batch = CommandService(self.store).execute(envelope)
            worker.assert_not_called()
        selection, plan = self.store.events()[before:]
        self.assertEqual([selection["kind"], plan["kind"]], ["search_selection", "batch_plan"])
        self.assertEqual(self.store.receipts()[-1]["event_ids"], [selection["id"], plan["id"]])
        self.assertEqual(plan["id"], batch)
        self.assertEqual(selection["payload"]["node"], followup_state(self.store, self.obligation)["experiment_node"])
        self.assertEqual(plan["payload"]["reanalysis_implementation"], self.reanalysis)
        self.assertEqual(plan["payload"]["reserved_cost"], 2)
        self.assertEqual(len(batch_state(self.store, batch)["slots"]), 2)
        self.assertEqual(followup_state(self.store, self.obligation)["obligation_resolution"], "open")
        self.assertEqual(Kernel(self.store, self.reviewer).next_action(self.claim)["action"], "replan")
        self.assertFalse(any(event["kind"] in {"run", "result", "claim", "review", "paper"}
                             for event in self.store.events()[before:]))
        self.assertEqual(CommandService(self.store).execute(envelope), batch)
        self.assertEqual(self.store.events()[before:], [selection, plan])
        self.assertEqual(ResearchGraph.from_store(self.store).node(batch).kind.value, "batch_plan")

    def test_invalid_recipe_or_other_winner_rolls_back_selection(self):
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "executor and replicator must differ"):
            CommandService(self.store).execute(self.prepare(executor="same", replicator="same"))
        self.assertEqual(self.snapshot(), before)
        with self.assertRaisesRegex(ValueError, "frozen local Python environment"):
            CommandService(self.store).execute(self.prepare(reanalysis_environment=self.store.put_json({"python": "bad"})))
        self.assertEqual(self.snapshot(), before)
        other_protocol = Kernel(self.store, self.planner).preregister_for_set(
            explanation_set=self.explanation_set, design="Competing fixture", metric="mean",
            analysis_plan="Compute mean", stopping_rule="One seed", seeds=[9], run_limit=2,
            implementation=self.store.put(b"competitor program"), environment=self.environment,
            data=self.data, replication_tolerance=0)
        other = self.search.add_node(self.tree, protocol=other_protocol, action="baseline",
            components=dict(discrimination=1.0, uncertainty=1.0, coverage=1.0, invalidity_risk=0.0),
            estimated_cost=2, rationale="Higher priority competing experiment")
        before = self.snapshot()
        self.assertNotEqual(other, followup_state(self.store, self.obligation)["experiment_node"])
        with self.assertRaisesRegex(ValueError, "winner is not the obligated"):
            CommandService(self.store).execute(self.prepare())
        self.assertEqual(self.snapshot(), before)

    def test_stale_source_review_rejects_preparation(self):
        Kernel(self.store, self.reviewer).review(self.claim, verdict="approve",
            rationale="Withdraw prior concern", actions=[], expected_basis=self.basis)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "latest opinion"):
            CommandService(self.store).execute(self.prepare())
        self.assertEqual(self.snapshot(), before)

    def test_new_source_evidence_or_planning_revision_rejects_preparation(self):
        new_run = Kernel(self.store, self.executor).start_run(self.source_protocol, seed=7,
            implementation=self.source, environment=self.environment,
            command=["python", "source.py", "--repeat"])
        self._finish(new_run, self.executor)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "stale|mechanically"):
            CommandService(self.store).execute(self.prepare())
        self.assertEqual(self.snapshot(), before)

    def test_revised_question_rejects_stale_followup_planning(self):
        Planning(self.store, self.planner).question(study_id=self.study,
            statement="Which mechanism explains the revised value?",
            objective="Reassess the review-driven child experiment", scope=self.scope,
            constraints=["Synthetic fixture"], stopping_criteria=["Stop after one control"],
            parent=self.question, revision_reason="Changed research question")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "current lineage head"):
            CommandService(self.store).execute(self.prepare())
        self.assertEqual(self.snapshot(), before)

    def test_failed_batch_write_rolls_back_selection(self):
        before = self.snapshot()
        append = self.store.append

        def crash(*args, **kwargs):
            if kwargs.get("kind") == "batch_plan":
                raise RuntimeError("batch write crash")
            return append(*args, **kwargs)

        with patch.object(self.store, "append", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "batch write crash"):
                CommandService(self.store).execute(self.prepare())
        self.assertEqual(self.snapshot(), before)

    def test_replay_validator_rejects_forged_selection_or_recipe(self):
        envelope = self.prepare()
        batch = CommandService(self.store).execute(envelope)
        selection, plan = self.store.events()[-2:]
        prefix = self.store.events()[:-2]
        request = self.store.receipts()[-1]["request"]["payload"]
        validate_prepared(self.store, prefix, selection, plan, request, self.study)
        forged = deepcopy(selection)
        forged["payload"]["reason"] = "not-the-policy-decision"
        with self.assertRaisesRegex(ValueError, "search policy decision"):
            validate_prepared(self.store, prefix, forged, plan, request, self.study)
        changed = deepcopy(plan)
        changed["payload"]["outputs"] = {"raw_data": "other.json", "metrics": "metrics.json"}
        with self.assertRaisesRegex(ValueError, "requested frozen execution recipe"):
            validate_prepared(self.store, prefix, selection, changed, request, self.study)
        self.assertEqual(batch_state(self.store, batch)["batch"], batch)

    def test_later_reviewer_change_does_not_rewrite_historical_batch(self):
        batch = CommandService(self.store).execute(self.prepare())
        Kernel(self.store, self.reviewer).review(self.claim, verdict="approve",
            rationale="Withdraw prior concern after batch planning", actions=[], expected_basis=self.basis)
        self.assertEqual(batch_state(self.store, batch)["batch"], batch)
        self.assertEqual(ResearchGraph.from_store(self.store).node(batch).kind.value, "batch_plan")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "latest opinion"):
            CommandService(self.store).execute(self.envelope("batch.enqueue_slot",
                Actor("child-executor", "executor"), dict(batch=batch, slot="primary:8")))
        self.assertEqual(self.snapshot(), before)

    def test_new_source_evidence_after_reserve_blocks_enqueue(self):
        batch = CommandService(self.store).execute(self.prepare())
        new_run = Kernel(self.store, self.executor).start_run(self.source_protocol, seed=7,
            implementation=self.source, environment=self.environment,
            command=["python", "source.py", "--repeat"])
        self._finish(new_run, self.executor)
        before = self.snapshot()
        self.assertEqual(batch_state(self.store, batch)["status"], "planned")
        with self.assertRaisesRegex(ValueError, "stale|mechanically"):
            CommandService(self.store).execute(self.envelope("batch.enqueue_slot",
                Actor("child-executor", "executor"), dict(batch=batch, slot="primary:8")))
        self.assertEqual(self.snapshot(), before)

    def test_new_review_after_enqueue_blocks_dispatch_and_worker(self):
        batch = CommandService(self.store).execute(self.prepare())
        binding = CommandService(self.store).execute(self.envelope("batch.enqueue_slot",
            Actor("child-executor", "executor"), dict(batch=batch, slot="primary:8")))
        slot = Kernel._get(self.store.events(), binding, "batch_slot")
        job = slot["payload"]["job"]
        Kernel(self.store, self.reviewer).review(self.claim, verdict="approve",
            rationale="Withdraw prior concern before dispatch", actions=[], expected_basis=self.basis)
        before = self.snapshot()
        with patch("episteme.execution.subprocess.Popen") as worker:
            with self.assertRaisesRegex(ValueError, "latest opinion"):
                advance_batch(self.store, batch)
            worker.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(next((event for event in self.store.events()
                                if event["kind"] == "execution_dispatch"
                                and event["payload"]["job"] == job), None))

    def test_current_source_admits_dispatch_record_without_running_worker(self):
        batch = CommandService(self.store).execute(self.prepare())
        binding = CommandService(self.store).execute(self.envelope("batch.enqueue_slot",
            Actor("child-executor", "executor"), dict(batch=batch, slot="primary:8")))
        job = Kernel._get(self.store.events(), binding, "batch_slot")["payload"]["job"]
        with patch("episteme.execution.subprocess.Popen") as worker:
            dispatch = CommandService(self.store).execute(self.envelope("execution.dispatch",
                Actor("child-executor", "executor"), dict(job=job, workspace_token="1" * 32)))
            worker.assert_not_called()
        self.assertEqual(Kernel._get(self.store.events(), dispatch, "execution_dispatch")["payload"]["job"], job)
        self.assertEqual(batch_state(self.store, batch)["status"], "unknown")

    def test_backup_restore_and_cli_command_preserve_exact_receipts(self):
        envelope = self.prepare()
        input_path = self.root.parent / "prepare-command.json"
        input_path.write_text(json.dumps(envelope), encoding="utf-8")
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli_main(["command", "--input", str(input_path), "--root", str(self.root)]), 0)
        batch = json.loads(output.getvalue())["result"]
        graph = ResearchGraph.from_store(self.store).snapshot_hash
        expected = self.snapshot()
        snapshot = self.root.parent / "snapshot"
        restored = self.root.parent / "restored"
        backup(self.store, snapshot)
        restore(snapshot, restored)
        with Store(restored) as recovered:
            self.assertEqual(CommandService(recovered).execute(envelope), batch)
            self.assertEqual((recovered.export(), recovered.export_receipts()), expected)
            self.assertEqual(ResearchGraph.from_store(recovered).snapshot_hash, graph)
            self.assertEqual(batch_state(recovered, batch)["status"], "planned")


if __name__ == "__main__":
    unittest.main()
