"""Mechanism test: a pack supplies the proposal schema and the kernel freezes it.

The fixture response is local bytes. No network, no model call, no run, and no
review. Confirmatory on the tabular protocol is the pack's preregistered design
mode, not a scientific result and not a claim.
"""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from episteme.agent_controller import advance_agent
from episteme.agents import agent_state
from episteme.commands import CommandService
from episteme.domains import registry
from episteme.execution import freeze_environment
from episteme.experiment_proposals_v2 import PROPOSAL_SCHEMA, validate_proposal
from episteme.graph import ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.pack_proposals import freeze_proposal_binding
from episteme.planning import Planning
from episteme.recovery import backup, restore
from episteme.search import COMPONENTS, Search
from episteme.store import Store, canonical, digest
from episteme import domain_packs


USAGE = {"input_tokens": 11, "cached_input_tokens": 0, "output_tokens": 7}
TABULAR = Path(__file__).resolve().parents[1] / "examples" / "tabular_classification_v1" / "planted"


def fixture_program(proposal: dict) -> bytes:
    raw = canonical(proposal)
    stream = [
        {"type": "thread.started", "thread_id": "pack-proposal-fixture"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "id": "pack-message",
                                             "text": raw.decode("utf-8")}},
        {"type": "turn.completed", "usage": USAGE},
    ]
    stdout = b"\n".join(canonical(item) for item in stream) + b"\n"
    source = (
        "import pathlib, sys\n"
        f"pathlib.Path('proposal.json').write_bytes({raw!r})\n"
        f"sys.stdout.buffer.write({stdout!r})\n"
        "sys.stdout.buffer.flush()\n"
        "raise SystemExit(0)\n"
    )
    return source.encode()


class PackProposalTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="episteme-pack-proposal-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "research"
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.study = "pack-proposal-mechanism"
        self.manager = Actor("fixture-pack-manager", "planner")
        self.assignee = Actor("fixture-pack-agent", "planner")
        self.environment = freeze_environment(self.store)

    def envelope(self, action, payload, *, actor=None):
        actor = actor or self.manager
        return dict(context=dict(
            command_id=uuid4().hex, expected_revision=len(self.store.events()),
            actor=actor.id, role=actor.role, study_id=self.study,
            correlation_id="pack-proposal-mechanism", causation_id=None),
            request=dict(version=1, action=action, payload=deepcopy(payload)))

    def command(self, action, payload, *, actor=None):
        return CommandService(self.store).execute(self.envelope(action, payload, actor=actor))

    def planning(self, statement):
        scope = {"population": "mechanism-test fixture, not a scientific population"}
        planner = Planning(self.store, self.manager)
        question = planner.question(
            study_id=self.study, statement=statement,
            objective="Exercise pack-scoped proposal admission without running an experiment",
            scope=scope, constraints=["Fixture bytes only"],
            stopping_criteria=["Stop after the frozen protocol"])
        kernel = Kernel(self.store, self.manager)
        hypotheses = [
            kernel.hypothesis("Fixture null", "No separation", "A separation appears", scope),
            kernel.hypothesis("Fixture alternative", "A separation appears", "No separation", scope),
        ]
        explanation = planner.explanation_set(
            question=question, hypotheses=hypotheses, comparison_plan="Contrast the two fixture statements")
        tree = Search(self.store, self.manager).register_tree(
            weights={key: 1 for key in COMPONENTS}, cost_weight=0, budget=8,
            cost_unit="enqueued_attempt", max_nodes=4, max_depth=2, max_width=4,
            max_selections=3, max_retries=0)
        return hypotheses, explanation, tree

    def provider(self):
        return self.store.put_json(dict(
            schema_version=1, provider="codex_cli_v1", profile_version=1,
            executable=str(Path(sys.executable).resolve()),
            executable_sha256=digest(Path(sys.executable).read_bytes()),
            cli_version="codex-cli 0.0.0", model="synthetic-fixture-no-model",
            reasoning_effort="low"))

    def proposal(self, pack_id, hypotheses, parameters):
        return dict(schema_version=2, status="proposed",
                    reason="The frozen pack schema admits one mechanism-test proposal",
                    experiment=dict(
                        pack_id=pack_id, parameters=parameters, action="discriminate",
                        hypothesis_predictions=[
                            dict(hypothesis=hypotheses[0], expected_observation="No separation"),
                            dict(hypothesis=hypotheses[1], expected_observation="A separation"),
                        ],
                        discriminating_contrast="Compare the two fixture expectations",
                        rationale="The alternatives predict different observations",
                        components={key: 0.5 for key in COMPONENTS}),
                    limitations=["This proposal is a mechanism test, not a measurement"])

    def admit(self, proposal, *, explanation, tree, binding):
        self.command("agent.register_budget", dict(study_id=self.study, max_calls=2))
        budget = next(event["id"] for event in self.store.events() if event["kind"] == "agent_budget")
        source = fixture_program(proposal)
        request_envelope = self.envelope("agent.request_pack_experiment", dict(
            budget=budget, explanation_set=explanation, tree=tree, proposal_binding=binding,
            assignee=self.assignee.id, provider=self.provider(),
            wall_seconds=10, max_output_bytes=65536))
        with patch("episteme.agents.PROGRAM", source):
            request = CommandService(self.store).execute(request_envelope)
        self.assertEqual(advance_agent(self.store, request)["status"], "applied")
        return request

    def test_published_schema_matches_the_kernel_envelope(self):
        path = Path(__file__).resolve().parents[1] / "schemas" / "experiment-proposal-v2.schema.json"
        self.assertEqual(path.read_bytes(), canonical(PROPOSAL_SCHEMA) + b"\n")

    def test_tabular_pack_schema_freezes_through_the_kernel_without_a_claim(self):
        source = Path(self.temp.name) / "tables"
        source.mkdir()
        for name in ("train.csv", "holdout.csv"):
            shutil.copyfile(TABULAR / name, source / name)
        captured = domain_packs.store_capture(self.store, "tabular_classification_v1", source)
        hypotheses, explanation, tree = self.planning(
            "Does the frozen tabular schema admit a proposal?")
        binding = freeze_proposal_binding(
            self.store, pack_id="tabular_classification_v1", host_inputs={},
            capture=captured["capture"], environment=self.environment)
        request = self.admit(self.proposal("tabular_classification_v1", hypotheses, {}),
                             explanation=explanation, tree=tree, binding=binding)
        context = json.loads(self.store.read(next(
            event["payload"]["context"] for event in self.store.events()
            if event["kind"] == "agent_request")))
        rendered = canonical(context).decode()
        self.assertNotIn("host_inputs", context)
        self.assertNotIn("-24,-2,0", rendered)
        self.assertEqual(context["pack_id"], "tabular_classification_v1")
        self.assertEqual(context["proposal_schema"]["parameters_schema"]["additionalProperties"], False)
        self.assertEqual(context["planned_attempts"], 2)
        created = [event["kind"] for event in self.store.events()]
        self.assertIn("pack_binding", created)
        self.assertNotIn("claim", created)
        self.assertNotIn("review", created)
        self.assertNotIn("paper", created)
        self.assertNotIn("run", created)
        protocol = next(event for event in self.store.events() if event["kind"] == "protocol")
        self.assertEqual(protocol["payload"]["protocol_mode"], "confirmatory")
        self.assertEqual(protocol["payload"]["metric"], "accuracy_difference")
        state = agent_state(self.store, request)
        self.assertEqual(state["status"], "applied")
        self.assertEqual(state["scientific_validity"], "not_assessed")
        self.assertEqual(state["usage"], USAGE)
        graph = ResearchGraph.from_store(self.store)
        self.assertGreater(len(graph.edges), 0)
        self.assertTrue(all(edge.scientific_validity == "not_assessed" for edge in graph.edges))

    def test_synthetic_pack_uses_the_same_kernel_path_and_hides_the_world(self):
        hypotheses, explanation, tree = self.planning("Which fixture statement matches the hidden world?")
        world = {"treatment_effect": 1.0, "confounding_strength": 0.5, "noise_std": 1.0}
        binding = freeze_proposal_binding(
            self.store, pack_id="synthetic_causal_v1",
            host_inputs={"world": world, "seeds": [7, 11], "replication_tolerance": 1e-9},
            capture=None, environment=self.environment)
        parameters = {"n_samples": 64, "assignment": "randomized", "analysis": "difference_in_means"}
        request = self.admit(self.proposal("synthetic_causal_v1", hypotheses, parameters),
                             explanation=explanation, tree=tree, binding=binding)
        context = json.loads(self.store.read(next(
            event["payload"]["context"] for event in self.store.events()
            if event["kind"] == "agent_request")))
        rendered = canonical(context).decode()
        self.assertNotIn("world", context)
        self.assertNotIn("confounding_strength", rendered)
        self.assertNotIn("noise_std", rendered)
        self.assertEqual(context["planned_attempts"], 4)
        protocol = next(event for event in self.store.events() if event["kind"] == "protocol")
        self.assertEqual(protocol["payload"]["protocol_mode"], "exploratory")
        self.assertEqual(protocol["payload"]["seeds"], [7, 11])
        self.assertNotIn("Frozen model contrast", protocol["payload"]["design"])
        node = next(event for event in self.store.events() if event["kind"] == "experiment_node")
        self.assertEqual(node["payload"]["estimated_cost"], 4)
        self.assertEqual(node["payload"]["rationale"], "The alternatives predict different observations")
        self.assertEqual(agent_state(self.store, request)["scientific_validity"], "not_assessed")
        self.assertFalse(any(event["kind"] in {"claim", "review", "paper", "run"}
                             for event in self.store.events()))
        ResearchGraph.from_store(self.store)

    def test_wrong_parameter_schema_and_claim_fields_are_rejected(self):
        hypotheses = ["hypothesis-a", "hypothesis-b"]
        tabular = self.proposal("tabular_classification_v1", hypotheses, {})
        validate_proposal(canonical(tabular), hypotheses, pack_id="tabular_classification_v1",
                          parameters_schema={"type": "object", "additionalProperties": False, "properties": {}})
        foreign = self.proposal("tabular_classification_v1", hypotheses,
                                {"n_samples": 64, "assignment": "randomized", "analysis": "difference_in_means"})
        with self.assertRaises(ValueError):
            validate_proposal(canonical(foreign), hypotheses, pack_id="tabular_classification_v1",
                              parameters_schema={"type": "object", "additionalProperties": False, "properties": {}})
        claimed = self.proposal("synthetic_causal_v1", hypotheses,
                                {"n_samples": 64, "assignment": "randomized", "analysis": "difference_in_means"})
        claimed["experiment"]["mode"] = "confirmatory"
        with self.assertRaises(ValueError):
            validate_proposal(canonical(claimed), hypotheses, pack_id="synthetic_causal_v1",
                              parameters_schema={"type": "object"})

    def test_a_pack_without_a_proposal_schema_cannot_enter_the_model_path(self):
        with self.assertRaises(ValueError):
            freeze_proposal_binding(self.store, pack_id="afterlife_seed_v1", host_inputs={},
                                    capture=None, environment=self.environment)

    def test_prepare_next_freezes_a_schema_3_batch_and_keeps_schema_2_defaults(self):
        import inspect
        from episteme.proposal_execution import ProposalExecution
        signature = inspect.signature(ProposalExecution.prepare_next)
        self.assertEqual(signature.parameters["wall_seconds"].default, 120)
        self.assertEqual(signature.parameters["max_output_bytes"].default, 1048576)
        self.assertIsNone(signature.parameters["required_capabilities"].default)
        hypotheses, explanation, tree = self.planning("Which fixture statement matches the hidden world?")
        binding = freeze_proposal_binding(
            self.store, pack_id="synthetic_causal_v1",
            host_inputs={"world": {"treatment_effect": 0.0, "confounding_strength": 0.0, "noise_std": 1.0},
                         "seeds": [3], "replication_tolerance": 1e-9},
            capture=None, environment=self.environment)
        self.admit(self.proposal("synthetic_causal_v1", hypotheses,
                                 {"n_samples": 32, "assignment": "randomized",
                                  "analysis": "difference_in_means"}),
                   explanation=explanation, tree=tree, binding=binding)
        batch = self.command("proposal.prepare_next", dict(
            tree=tree, executor="pack-executor", replicator="pack-reanalyst"))
        plan = Kernel._get(self.store.events(), batch, "batch_plan")["payload"]
        self.assertEqual((plan["wall_seconds"], plan["max_output_bytes"], plan["required_capabilities"]),
                         (120, 1048576, []))
        self.assertFalse(any(event["kind"] in {"run", "claim", "review", "paper"}
                             for event in self.store.events()))
        self.assertTrue(all(event["payload"].get("scientific_validity", "not_assessed") == "not_assessed"
                            for event in self.store.events()))
        receipt = next(row for row in self.store.receipts()
                       if row["request"]["action"] == "proposal.prepare_next")
        self.assertEqual(CommandService(self.store).execute(
            dict(context=receipt["context"], request=receipt["request"])), batch)

    def test_prepare_next_uses_pinned_tabular_limits_and_rejects_a_different_override(self):
        source = Path(self.temp.name) / "tables"
        source.mkdir()
        for name in ("train.csv", "holdout.csv"):
            shutil.copyfile(TABULAR / name, source / name)
        captured = domain_packs.store_capture(self.store, "tabular_classification_v1", source)
        hypotheses, explanation, tree = self.planning("Does the frozen tabular schema admit a proposal?")
        binding = freeze_proposal_binding(
            self.store, pack_id="tabular_classification_v1", host_inputs={},
            capture=captured["capture"], environment=self.environment)
        self.admit(self.proposal("tabular_classification_v1", hypotheses, {}),
                   explanation=explanation, tree=tree, binding=binding)
        before = len(self.store.events())
        with self.assertRaisesRegex(ValueError, "pinned pack execution plan"):
            self.command("proposal.prepare_next", dict(
                tree=tree, executor="tabular-executor", replicator="tabular-reanalyst",
                wall_seconds=90))
        self.assertEqual(len(self.store.events()), before)
        batch = self.command("proposal.prepare_next", dict(
            tree=tree, executor="tabular-executor", replicator="tabular-reanalyst"))
        plan = Kernel._get(self.store.events(), batch, "batch_plan")["payload"]
        self.assertEqual(plan["wall_seconds"], 60)
        self.assertEqual(plan["max_output_bytes"], 1048576)
        self.assertEqual(plan["reserved_cost"], 2)
        self.assertFalse(any(event["kind"] == "run" for event in self.store.events()))

    def test_applied_pack_proposal_replays_and_restores(self):
        hypotheses, explanation, tree = self.planning("Which fixture statement matches the hidden world?")
        binding = freeze_proposal_binding(
            self.store, pack_id="synthetic_causal_v1",
            host_inputs={"world": {"treatment_effect": 1.0, "confounding_strength": 0.0, "noise_std": 1.0},
                         "seeds": [5], "replication_tolerance": 1e-9},
            capture=None, environment=self.environment)
        self.admit(self.proposal("synthetic_causal_v1", hypotheses,
                                 {"n_samples": 32, "assignment": "observational", "analysis": "adjusted_ols"}),
                   explanation=explanation, tree=tree, binding=binding)
        apply = next(row for row in self.store.receipts()
                     if row["request"]["action"] == "agent.apply_experiment")
        self.assertEqual(CommandService(self.store).execute(
            dict(context=apply["context"], request=apply["request"])), apply["result"])
        snapshot = self.root.parent / "backup"
        restored = self.root.parent / "restored"
        backup(self.store, snapshot)
        restore(snapshot, restored)
        with Store(restored) as copy:
            self.assertEqual(copy.export(), self.store.export())
            ResearchGraph.from_store(copy)
            self.assertEqual(registry.load_pack("synthetic_causal_v1").pack_id, "synthetic_causal_v1")


if __name__ == "__main__":
    unittest.main()
