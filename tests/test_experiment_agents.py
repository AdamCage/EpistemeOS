"""End-to-end fixture tests for experiment proposal admission and provenance.

The tiny local worker stands in for transport only. It performs no experiment,
model call, scientific review, or publication step.
"""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from episteme.agent_controller import advance_agent, work_agent
from episteme.agents import agent_state, freeze_recipe_binding
from episteme.commands import CommandService
from episteme.domains import synthetic_causal
from episteme.execution import freeze_environment
from episteme.graph import ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.planning import Planning
from episteme.recovery import backup, restore
from episteme.search import COMPONENTS, Search
from episteme.store import Store, canonical, digest


USAGE = {"input_tokens": 23, "cached_input_tokens": 0, "output_tokens": 17}


def fixture_program(proposal: dict | bytes, *, exitcode: int = 0,
                    message: str | None = None) -> tuple[bytes, bytes]:
    raw = canonical(proposal) if isinstance(proposal, dict) else proposal
    answer = raw.decode("utf-8") if message is None else message
    stream = [
        {"type": "thread.started", "thread_id": "synthetic-experiment-fixture"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message",
                                             "id": "synthetic-message", "text": answer}},
        {"type": "turn.completed", "usage": USAGE},
    ]
    stdout = b"\n".join(canonical(item) for item in stream) + b"\n"
    source = (
        "import pathlib, sys\n"
        f"pathlib.Path('proposal.json').write_bytes({raw!r})\n"
        f"sys.stdout.buffer.write({stdout!r})\n"
        "sys.stdout.buffer.flush()\n"
        f"raise SystemExit({exitcode})\n"
    )
    return source.encode(), raw


class ExperimentAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="episteme-experiment-agents-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "research"
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.study = "synthetic-experiment-study"
        self.manager = Actor("fixture-manager", "planner")
        self.assignee = Actor("fixture-experiment-agent", "planner")
        self.scope = {"population": "synthetic causal fixture only"}
        self.question_payload = dict(
            study_id=self.study,
            statement="Which synthetic explanation predicts the fixture outcome?",
            objective="Test experiment proposal admission without performing an experiment",
            scope=self.scope,
            constraints=["Synthetic fixture only"],
            stopping_criteria=["Stop after bounded proposal"],
        )
        planner = Planning(self.store, self.manager)
        self.question = planner.question(**self.question_payload)
        kernel = Kernel(self.store, self.manager)
        self.hypotheses = [
            kernel.hypothesis("Fixture null", "No treatment effect", "Positive effect", self.scope),
            kernel.hypothesis("Fixture mechanism", "Positive treatment effect", "Zero effect", self.scope),
        ]
        self.explanation_set = planner.explanation_set(
            question=self.question, hypotheses=self.hypotheses,
            comparison_plan="Contrast the two fixture explanations")
        self.tree = Search(self.store, self.manager).register_tree(
            weights={key: 1 for key in COMPONENTS}, cost_weight=0,
            budget=8, cost_unit="enqueued_attempt", max_nodes=4, max_depth=2,
            max_width=4, max_selections=3, max_retries=0)
        self.environment = freeze_environment(self.store)
        self.world = {"treatment_effect": 1.0, "confounding_strength": 0.5,
                      "noise_std": 1.0}
        self.recipe_binding = freeze_recipe_binding(
            self.store, world=self.world, seeds=[7, 11], environment=self.environment)
        self.budget = self.command("agent.register_budget", dict(study_id=self.study, max_calls=4))
        self.provider = self.store.put_json(dict(
            schema_version=1, provider="codex_cli_v1", profile_version=1,
            executable=str(Path(sys.executable).resolve()),
            executable_sha256=digest(Path(sys.executable).read_bytes()),
            cli_version="codex-cli 0.0.0", model="synthetic-fixture-no-model",
            reasoning_effort="low"))

    def envelope(self, action: str, payload: dict, *, actor: Actor | None = None,
                 store: Store | None = None) -> dict:
        actor, store = actor or self.manager, store or self.store
        return dict(context=dict(
            command_id=uuid4().hex, expected_revision=len(store.events()),
            actor=actor.id, role=actor.role, study_id=self.study,
            correlation_id="synthetic-experiment-cycle", causation_id=None),
            request=dict(version=1, action=action, payload=deepcopy(payload)))

    def command(self, action: str, payload: dict, *, actor: Actor | None = None) -> str:
        return CommandService(self.store).execute(self.envelope(action, payload, actor=actor))

    def event(self, id: str) -> dict:
        return next(event for event in self.store.events() if event["id"] == id)

    def proposal(self) -> dict:
        return dict(schema_version=1, status="proposed",
            reason="Two fixture predictions warrant an exploratory synthetic contrast",
            experiment=dict(recipe_id="synthetic_causal_v1",
                parameters=dict(n_samples=64, assignment="randomized",
                                analysis="difference_in_means"),
                mode="exploratory", action="discriminate",
                hypothesis_predictions=[
                    dict(hypothesis=self.hypotheses[0], expected_observation="Near zero contrast"),
                    dict(hypothesis=self.hypotheses[1], expected_observation="Positive contrast"),
                ],
                discriminating_contrast="Compare estimated treatment effect with zero",
                rationale="The fixture alternatives predict distinct treatment contrasts",
                components=dict(discrimination=0.9, uncertainty=0.8, coverage=0.7,
                                invalidity_risk=0.1)),
            limitations=["Synthetic generator values are not empirical observations"])

    def request(self, proposal: dict | bytes | None = None, *, message: str | None = None,
                tree: str | None = None) -> tuple[str, dict, bytes]:
        source, raw = fixture_program(self.proposal() if proposal is None else proposal,
                                      message=message)
        envelope = self.envelope("agent.request_experiment", dict(
            budget=self.budget, explanation_set=self.explanation_set,
            tree=self.tree if tree is None else tree, recipe_binding=self.recipe_binding,
            assignee=self.assignee.id, provider=self.provider,
            wall_seconds=10, max_output_bytes=65536))
        with patch("episteme.agents.PROGRAM", source):
            request = CommandService(self.store).execute(envelope)
        return request, envelope, raw

    def assert_no_generated_experiment(self):
        self.assertFalse(any(event["kind"] in {"protocol", "experiment_node", "run", "result",
                                                 "claim", "review", "paper", "agent_application"}
                             for event in self.store.events()))

    def test_successful_application_is_atomic_frozen_and_not_scientific_approval(self):
        request, request_envelope, _ = self.request()
        self.assertEqual(CommandService(self.store).execute(request_envelope), request)
        frozen_context = json.loads(self.store.read(self.event(request)["payload"]["context"]))
        self.assertNotIn("world", frozen_context)
        self.assertEqual(frozen_context["hypothesis_ids"], self.hypotheses)
        self.assertEqual(frozen_context["planned_attempts"], 4)
        self.assertEqual(work_agent(self.store, request)["status"], "proposed")
        self.assert_no_generated_experiment()
        envelope = self.envelope("agent.apply_experiment", dict(request=request), actor=self.assignee)
        before = len(self.store.events())
        application = CommandService(self.store).execute(envelope)
        created = self.store.events()[before:]
        self.assertEqual([event["kind"] for event in created],
                         ["protocol", "experiment_node", "agent_application"])
        self.assertEqual(created[-1]["id"], application)
        receipt = next(row for row in self.store.receipts() if row["result"] == application)
        self.assertEqual(receipt["event_ids"], [event["id"] for event in created])
        self.assertEqual(receipt["request"]["action"], "agent.apply_experiment")
        protocol, node, applied = created
        self.assertEqual(protocol["payload"]["hypotheses"], self.hypotheses)
        self.assertEqual(protocol["payload"]["planning"]["explanation_set"], self.explanation_set)
        self.assertEqual(protocol["payload"]["seeds"], [7, 11])
        self.assertEqual(protocol["payload"]["run_limit"], 4)
        self.assertEqual(protocol["payload"]["protocol_mode"], "exploratory")
        self.assertEqual(node["payload"]["tree"], self.tree)
        self.assertEqual(node["payload"]["protocol"], protocol["id"])
        self.assertEqual(node["payload"]["estimated_cost"], 4)
        self.assertEqual(applied["payload"]["scientific_validity"], "not_assessed")
        manifest = json.loads(self.store.read(applied["payload"]["compilation"]))
        self.assertEqual(manifest["protocol"], protocol)
        self.assertEqual(manifest["experiment_node"], node)
        self.assertNotEqual(manifest["sources"]["implementation"],
                            manifest["sources"]["reanalysis_implementation"])
        self.assertTrue(all(self.store.read(key) for key in manifest["sources"].values()))
        state = agent_state(self.store, request)
        self.assertEqual(state["status"], "applied")
        self.assertEqual(state["usage"], USAGE)
        self.assertEqual(state["scientific_validity"], "not_assessed")
        self.assertEqual(state["protocol"], protocol["id"])
        self.assertEqual(state["experiment_node"], node["id"])
        self.assertFalse(any(event["kind"] in {"search_selection", "run", "result",
                                                  "claim", "review", "paper"}
                             for event in self.store.events()))
        graph = ResearchGraph.from_store(self.store)
        self.assertGreater(len(graph.edges), 0)
        self.assertEqual(CommandService(self.store).execute(envelope), application)
        self.assertEqual(len(self.store.events()), before + 3)

    def test_revised_explanation_set_blocks_application_and_retains_response(self):
        request, _, raw = self.request()
        self.assertEqual(work_agent(self.store, request)["status"], "proposed")
        response = deepcopy(self.event(agent_state(self.store, request)["response"]))
        Planning(self.store, self.manager).explanation_set(
            question=self.question, hypotheses=self.hypotheses,
            comparison_plan="Revised fixture comparison", parent=self.explanation_set,
            revision_reason="The fixture plan changed")
        before = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "head|current|stale|superseded"):
            self.command("agent.apply_experiment", dict(request=request), actor=self.assignee)
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))
        self.assertEqual(self.event(response["id"]), response)
        self.assertEqual(self.store.read(response["payload"]["assessment"]["artifacts"]["proposal"]), raw)
        self.assertEqual(agent_state(self.store, request)["status"], "proposed")
        self.assert_no_generated_experiment()

    def test_revised_set_blocks_queued_dispatch_before_external_worker(self):
        request, _, _ = self.request()
        Planning(self.store, self.manager).explanation_set(
            question=self.question, hypotheses=self.hypotheses,
            comparison_plan="New fixture comparison before dispatch",
            parent=self.explanation_set,
            revision_reason="The fixture plan changed before model invocation")
        before = self.store.export(), self.store.export_receipts()
        with patch("episteme.agent_controller.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(ValueError, "head|current|stale|superseded"):
                work_agent(self.store, request)
            spawn.assert_not_called()
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))
        self.assertEqual(agent_state(self.store, request)["status"], "queued")
        self.assert_no_generated_experiment()

    def test_changed_tree_frontier_blocks_application_and_retains_response(self):
        kernel = Kernel(self.store, self.manager)
        source = self.store.put(b"fixture source; never executed")
        data = self.store.put(b"fixture input; never executed")
        prior = kernel.preregister_for_set(
            explanation_set=self.explanation_set, design="Preexisting synthetic fixture",
            metric="fixture", analysis_plan="No run in this test", stopping_rule="One fixed step",
            seeds=[3], run_limit=2, implementation=source, environment=self.environment,
            data=data, replication_tolerance=0)
        Search(self.store, self.manager).add_node(
            self.tree, protocol=prior, action="baseline",
            components=dict(discrimination=0.5, uncertainty=0.5, coverage=0.5,
                            invalidity_risk=0.1), estimated_cost=2,
            rationale="Preexisting fixture frontier")
        request, _, raw = self.request()
        self.assertEqual(work_agent(self.store, request)["status"], "proposed")
        response = deepcopy(self.event(agent_state(self.store, request)["response"]))
        self.assertIsNotNone(Search(self.store, self.manager).select_next(self.tree)["node"])
        before = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "stale|context|budget"):
            self.command("agent.apply_experiment", dict(request=request), actor=self.assignee)
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))
        self.assertEqual(self.event(response["id"]), response)
        self.assertEqual(self.store.read(response["payload"]["assessment"]["artifacts"]["proposal"]), raw)
        self.assertEqual(agent_state(self.store, request)["status"], "proposed")
        self.assertFalse(any(event["kind"] == "agent_application" for event in self.store.events()))

    def test_invalid_and_abstained_responses_keep_original_bytes_without_protocols(self):
        malformed = b'{"schema_version":1,"schema_version":1}'
        abstention = dict(schema_version=1, status="abstained",
                          reason="No bounded test for this fixture", experiment=None,
                          limitations=["Synthetic plan only"])
        for proposal, expected in ((malformed, "invalid"), (abstention, "abstained")):
            with self.subTest(expected=expected):
                request, _, raw = self.request(proposal)
                result = advance_agent(self.store, request)
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["usage"], USAGE)
                response = self.event(result["response"])["payload"]
                self.assertEqual(self.store.read(response["assessment"]["artifacts"]["proposal"]), raw)
                self.assertEqual(response["assessment"]["transport_status"], "completed")
                with self.assertRaisesRegex(ValueError, "not applicable"):
                    self.command("agent.apply_experiment", dict(request=request), actor=self.assignee)
        self.assert_no_generated_experiment()

    def test_provider_message_must_match_captured_proposal_file(self):
        different = deepcopy(self.proposal())
        different["reason"] = "Different valid answer in the final provider message"
        request, _, raw = self.request(message=canonical(different).decode())
        result = advance_agent(self.store, request)
        self.assertEqual(result["status"], "invalid")
        self.assertIn("differs", " ".join(result["errors"]))
        response = self.event(result["response"])["payload"]
        self.assertEqual(self.store.read(response["assessment"]["artifacts"]["proposal"]), raw)
        self.assert_no_generated_experiment()

    def test_new_result_before_tree_terminal_stales_the_frozen_evidence_view(self):
        source = self.store.put(b"fixture source; not executed")
        data = self.store.put(b"fixture data; not executed")
        protocol = Kernel(self.store, self.manager).preregister_for_set(
            explanation_set=self.explanation_set, design="Prior fixture", metric="fixture",
            analysis_plan="No analysis", stopping_rule="One attempt", seeds=[3], run_limit=2,
            implementation=source, environment=self.environment, data=data,
            replication_tolerance=0)
        Search(self.store, self.manager).add_node(self.tree, protocol=protocol, action="baseline",
            components=dict(discrimination=0.5, uncertainty=0.5, coverage=0.5,
                            invalidity_risk=0.1), estimated_cost=2, rationale="Prior fixture node")
        Search(self.store, self.manager).select_next(self.tree)
        executor = Kernel(self.store, Actor("fixture-executor", "executor"))
        run = executor.start_run(protocol, seed=3, implementation=source,
                                 environment=self.environment, command=["fixture-only"])
        request, _, raw = self.request()
        self.assertEqual(work_agent(self.store, request)["status"], "proposed")
        executor.finish_run(run, status="failed", outputs={}, reason="Synthetic failure fixture")
        before = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "stale|context"):
            self.command("agent.apply_experiment", dict(request=request), actor=self.assignee)
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))
        response = self.event(agent_state(self.store, request)["response"])["payload"]
        self.assertEqual(self.store.read(response["assessment"]["artifacts"]["proposal"]), raw)

    def test_foreign_tree_receipt_and_invalid_environment_block_admission(self):
        foreign = self.envelope("search.register_tree", dict(
            weights={key:1 for key in COMPONENTS}, cost_weight=0, budget=8,
            cost_unit="enqueued_attempt", max_nodes=4, max_depth=2,
            max_width=4, max_selections=3))
        foreign["context"]["study_id"] = "foreign-study"
        foreign_tree = CommandService(self.store).execute(foreign)
        before = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "another study"):
            self.request(tree=foreign_tree)
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))
        invalid_environment = self.store.put_json(dict(schema_version=1, backend="wrong",
                                                       fingerprint={}))
        with self.assertRaisesRegex(ValueError, "frozen local Python environment"):
            freeze_recipe_binding(self.store, world=self.world, seeds=[7],
                                  environment=invalid_environment)

    def test_default_reanalysis_tolerance_is_domain_owned_and_nonzero(self):
        binding = json.loads(self.store.read(self.recipe_binding))
        self.assertEqual(binding["replication_tolerance"],
                         synthetic_causal.DEFAULT_REANALYSIS_TOLERANCE)
        self.assertGreater(binding["replication_tolerance"], 0)

    def test_failed_node_creation_rolls_back_protocol_and_complete_receipt(self):
        request, _, _ = self.request()
        self.assertEqual(work_agent(self.store, request)["status"], "proposed")
        envelope = self.envelope("agent.apply_experiment", dict(request=request), actor=self.assignee)
        before = self.store.export(), self.store.export_receipts()
        with patch("episteme.agents.Search.add_node", side_effect=ValueError("synthetic node failure")):
            with self.assertRaisesRegex(ValueError, "synthetic node failure"):
                CommandService(self.store).execute(envelope)
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))
        self.assert_no_generated_experiment()
        CommandService(self.store).execute(envelope)
        self.assertEqual(agent_state(self.store, request)["status"], "applied")

    def test_backup_restore_replays_applied_request_without_new_worker(self):
        request, _, _ = self.request()
        self.assertEqual(work_agent(self.store, request)["status"], "proposed")
        envelope = self.envelope("agent.apply_experiment", dict(request=request), actor=self.assignee)
        application = CommandService(self.store).execute(envelope)
        expected = agent_state(self.store, request)
        original_graph = ResearchGraph.from_store(self.store).snapshot_hash
        location = Path(self.temp.name)
        snapshot, target = location / "backup", location / "restored"
        backup(self.store, snapshot)
        restore(snapshot, target)
        with Store(target) as recovered, patch("episteme.agent_controller.subprocess.Popen") as spawn:
            self.assertEqual(agent_state(recovered, request), expected)
            self.assertEqual(ResearchGraph.from_store(recovered).snapshot_hash, original_graph)
            self.assertEqual(CommandService(recovered).execute(envelope), application)
            self.assertEqual(advance_agent(recovered, request), expected)
            spawn.assert_not_called()
            self.assertEqual(recovered.export(), self.store.export())
            self.assertEqual(recovered.export_receipts(), self.store.export_receipts())

    def test_applied_history_does_not_recompile_when_adapter_changes(self):
        request, _, _ = self.request()
        expected = advance_agent(self.store, request)
        self.assertEqual(expected["status"], "applied")
        original_graph = ResearchGraph.from_store(self.store).snapshot_hash
        with patch("episteme.agents.synthetic_causal.compile_recipe",
                   side_effect=AssertionError("historical replay called today's compiler")), \
             patch("episteme.agents.synthetic_causal.describe",
                   side_effect=AssertionError("historical replay called today's catalog")):
            self.assertEqual(agent_state(self.store, request), expected)
            self.assertEqual(ResearchGraph.from_store(self.store).snapshot_hash, original_graph)
        before = self.store.export(), self.store.export_receipts()
        with patch("episteme.agents.synthetic_causal.compile_recipe",
                   side_effect=AssertionError("new request called today's compiler")):
            with self.assertRaisesRegex(AssertionError, "new request called today's compiler"):
                self.request()
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))

    def test_cli_status_graph_and_export_read_applied_provenance(self):
        request, _, _ = self.request()
        expected = advance_agent(self.store, request)
        self.assertEqual(expected["status"], "applied")
        before = self.store.export(), self.store.export_receipts()
        commands = [
            ["agent", "status", request, "--root", str(self.root)],
            ["graph", "--root", str(self.root), "--format", "json"],
            ["export", "--root", str(self.root)],
        ]
        for arguments in commands:
            with self.subTest(arguments=arguments):
                completed = subprocess.run([sys.executable, "-m", "episteme", *arguments],
                                           capture_output=True, text=True, timeout=30)
                self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
                parsed = json.loads(completed.stdout)
                if arguments[0] == "agent":
                    self.assertEqual(parsed, expected)
                elif arguments[0] == "graph":
                    self.assertTrue(any(node["id"] == expected["protocol"] for node in parsed["nodes"]))
                else:
                    self.assertIn("review-bundle.json", parsed)
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))


if __name__ == "__main__":
    unittest.main()
