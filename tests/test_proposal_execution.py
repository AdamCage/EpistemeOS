"""Receipt-backed selection and batch planning for a model-proposed fixture.

The provider and scientific program are tiny local fixtures. Preparation never
starts an experiment or confers scientific approval or independent authorship.
"""

from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from episteme.agent_controller import advance_agent
from episteme.agents import freeze_recipe_binding
from episteme.batch import batch_state
from episteme.commands import CommandService
from episteme.execution import freeze_environment
from episteme.graph import ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.planning import Planning
from episteme.proposal_execution import validate_prepared
from episteme.recovery import backup, restore
from episteme.search import COMPONENTS, Search
from episteme.store import Store, canonical, digest


def fixture_program(proposal: dict) -> bytes:
    raw = canonical(proposal)
    stream = [
        dict(type="thread.started", thread_id="proposal-batch-fixture"),
        dict(type="turn.started"),
        dict(type="item.completed", item=dict(type="agent_message", id="answer",
                                               text=raw.decode())),
        dict(type="turn.completed", usage=dict(input_tokens=7, cached_input_tokens=0,
                                                output_tokens=5)),
    ]
    output = b"\n".join(canonical(row) for row in stream) + b"\n"
    return ("import pathlib, sys\n"
            f"pathlib.Path('proposal.json').write_bytes({raw!r})\n"
            f"sys.stdout.buffer.write({output!r})\n").encode()


class ProposalExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="episteme-proposal-batch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "research"
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.study = "proposal-batch-fixture"
        self.planner = Actor("fixture-planner", "planner")
        self.model_actor = Actor("fixture-model-planner", "planner")
        self.scope = {"population": "synthetic fixture only"}
        self.question_fields = dict(study_id=self.study,
            statement="Which synthetic explanation fits the fixture?",
            objective="Test a bounded model proposal to batch transition",
            scope=self.scope, constraints=["Synthetic data only"],
            stopping_criteria=["One bounded batch"])
        self.question = Planning(self.store, self.planner).question(**self.question_fields)
        kernel = Kernel(self.store, self.planner)
        self.hypotheses = [
            kernel.hypothesis("No effect", "Zero contrast", "Positive contrast", self.scope),
            kernel.hypothesis("Positive effect", "Positive contrast", "Zero contrast", self.scope),
        ]
        self.explanation_set = Planning(self.store, self.planner).explanation_set(
            question=self.question, hypotheses=self.hypotheses,
            comparison_plan="Compare the registered alternatives")
        self.tree = Search(self.store, self.planner).register_tree(
            weights={key: 1 for key in COMPONENTS}, cost_weight=0, budget=4,
            cost_unit="enqueued_attempt", max_nodes=4, max_depth=2,
            max_width=4, max_selections=3, max_retries=0)
        environment = freeze_environment(self.store)
        self.recipe_binding = freeze_recipe_binding(self.store,
            world=dict(treatment_effect=1.0, confounding_strength=0.4,
                       noise_std=1.0), seeds=[7, 11], environment=environment)
        self.budget = self.command("agent.register_budget",
                                   dict(study_id=self.study, max_calls=2))
        self.provider = self.store.put_json(dict(
            schema_version=1, provider="codex_cli_v1", profile_version=1,
            executable=str(Path(sys.executable).resolve()),
            executable_sha256=digest(Path(sys.executable).read_bytes()),
            cli_version="codex-cli 0.0.0", model="fixture-no-model",
            reasoning_effort="low"))
        self.application = self._apply_fixture_proposal()
        self.applied = self.event(self.application)["payload"]

    def envelope(self, action: str, payload: dict, *, store: Store | None = None) -> dict:
        store = store or self.store
        return dict(context=dict(command_id=uuid4().hex,
            expected_revision=len(store.events()), actor=self.planner.id,
            role=self.planner.role, study_id=self.study,
            correlation_id="proposal-batch", causation_id=None),
            request=dict(version=1, action=action, payload=deepcopy(payload)))

    def command(self, action: str, payload: dict) -> str:
        return CommandService(self.store).execute(self.envelope(action, payload))

    def event(self, id: str) -> dict:
        return next(row for row in self.store.events() if row["id"] == id)

    def proposal(self) -> dict:
        return dict(schema_version=1, status="proposed",
            reason="The frozen alternatives predict distinct synthetic contrasts",
            experiment=dict(recipe_id="synthetic_causal_v1",
                parameters=dict(n_samples=32, assignment="randomized",
                                analysis="difference_in_means"),
                mode="exploratory", action="discriminate",
                hypothesis_predictions=[
                    dict(hypothesis=self.hypotheses[0], expected_observation="Near zero"),
                    dict(hypothesis=self.hypotheses[1], expected_observation="Positive"),
                ],
                discriminating_contrast="Estimate the treatment contrast",
                rationale="Test the competing predictions",
                components=dict(discrimination=0.9, uncertainty=0.8,
                                coverage=0.7, invalidity_risk=0.1)),
            limitations=["Synthetic generator is a fixture, not empirical evidence"])

    def _apply_fixture_proposal(self) -> str:
        payload = dict(budget=self.budget, explanation_set=self.explanation_set,
            tree=self.tree, recipe_binding=self.recipe_binding,
            assignee=self.model_actor.id, provider=self.provider,
            wall_seconds=10, max_output_bytes=65536)
        envelope = self.envelope("agent.request_experiment", payload)
        with patch("episteme.agents.PROGRAM", fixture_program(self.proposal())):
            request = CommandService(self.store).execute(envelope)
        result = advance_agent(self.store, request)
        self.assertEqual(result["status"], "applied")
        return result["application"]

    def prepare(self, **kwargs) -> dict:
        payload = dict(tree=self.tree, executor="fixture-executor",
                       replicator="fixture-replicator", wall_seconds=10,
                       max_output_bytes=65536)
        payload.update(kwargs)
        return self.envelope("proposal.prepare_next", payload)

    def test_selected_model_node_and_frozen_sources_share_one_receipt(self):
        envelope = self.prepare()
        before = len(self.store.events())
        with patch("episteme.execution.subprocess.Popen") as worker:
            batch = CommandService(self.store).execute(envelope)
            worker.assert_not_called()
        created = self.store.events()[before:]
        self.assertEqual([event["kind"] for event in created],
                         ["search_selection", "batch_plan"])
        self.assertEqual(batch, created[-1]["id"])
        receipt = self.store.receipts()[-1]
        self.assertEqual(receipt["event_ids"], [event["id"] for event in created])
        self.assertEqual(receipt["request"]["action"], "proposal.prepare_next")
        selected, plan = (event["payload"] for event in created)
        self.assertEqual(selected["node"], self.applied["experiment_node"])
        self.assertEqual(selected["reserved_cost"], 4)
        self.assertEqual(plan["reserved_cost"], 4)
        self.assertEqual(plan["cost_unit"], "enqueued_attempt")
        manifest = json.loads(self.store.read(self.applied["compilation"]))
        self.assertEqual(plan["reanalysis_implementation"],
                         manifest["sources"]["reanalysis_implementation"])
        self.assertEqual(plan["outputs"], manifest["compiled"]["outputs"])
        self.assertEqual(len(batch_state(self.store, batch)["slots"]), 4)
        self.assertEqual(Search(self.store, self.planner).tree_state(self.tree)["reserved"], "4")
        self.assertFalse(any(e["kind"] in {"run", "result", "claim", "review", "paper"}
                             for e in self.store.events()))
        self.assertEqual(CommandService(self.store).execute(envelope), batch)
        self.assertEqual((self.store.events()[before:]), created)

    def test_manual_priority_winner_is_rejected_without_reservation(self):
        manual = self.command("search.add_node", dict(tree=self.tree,
            protocol=self.applied["protocol"], action="baseline",
            components=dict(discrimination=1.0, uncertainty=1.0,
                            coverage=1.0, invalidity_risk=0.0),
            estimated_cost=4, rationale="Manual higher-priority fixture"))
        self.assertNotEqual(manual, self.applied["experiment_node"])
        before = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "applied experiment proposal"):
            CommandService(self.store).execute(self.prepare())
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))

    def test_batch_gate_failure_and_stale_question_roll_back_selection(self):
        before = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "executor and replicator must differ"):
            CommandService(self.store).execute(
                self.prepare(executor="same-actor", replicator="same-actor"))
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))
        self.command("planning.question", dict(**self.question_fields,
            parent=self.question, revision_reason="Fixture question revision"))
        before = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "current lineage head"):
            CommandService(self.store).execute(self.prepare())
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))

    def test_no_eligible_selection_is_not_persisted_as_a_batch(self):
        self.command("proposal.prepare_next", self.prepare()["request"]["payload"])
        before = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "no experiment node can be batched"):
            CommandService(self.store).execute(self.prepare())
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))

    def test_backup_restore_replays_without_another_selection_or_worker(self):
        envelope = self.prepare()
        batch = CommandService(self.store).execute(envelope)
        graph = ResearchGraph.from_store(self.store).snapshot_hash
        expected = self.store.export(), self.store.export_receipts()
        snapshot = self.root.parent / "backup"
        restored = self.root.parent / "restored"
        backup(self.store, snapshot)
        restore(snapshot, restored)
        with Store(restored) as recovered, patch("episteme.agent_controller.subprocess.Popen") as worker:
            self.assertEqual(CommandService(recovered).execute(envelope), batch)
            self.assertEqual(ResearchGraph.from_store(recovered).snapshot_hash, graph)
            self.assertEqual((recovered.export(), recovered.export_receipts()), expected)
            worker.assert_not_called()

    def test_replay_validator_checks_frozen_sources_without_recompiling(self):
        batch = CommandService(self.store).execute(self.prepare())
        selection, plan = self.store.events()[-2:]
        prefix = self.store.events()[:-2]
        with patch("episteme.agents.synthetic_causal.compile_recipe",
                   side_effect=AssertionError("historical compiler called")), \
             patch("episteme.agents.synthetic_causal.describe",
                   side_effect=AssertionError("historical catalog called")):
            validate_prepared(self.store, prefix, selection, plan)
            self.assertEqual(batch_state(self.store, batch)["batch"], batch)
        changed = deepcopy(plan)
        changed["payload"]["reanalysis_implementation"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "frozen proposal compilation"):
            validate_prepared(self.store, prefix, selection, changed)
        wrong_order = deepcopy(plan)
        wrong_order["seq"] += 1
        with self.assertRaisesRegex(ValueError, "consecutive planner events"):
            validate_prepared(self.store, prefix, selection, wrong_order)


if __name__ == "__main__":
    unittest.main()
