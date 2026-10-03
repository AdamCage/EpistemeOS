"""A manually frozen domain recipe can feed a batch without claiming validity."""

import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from episteme.analysis_controller import advance_batch_analysis
from episteme.batch_controller import advance_batch
from episteme.commands import CommandService
from episteme.domain_binding import _index as binding_index
from episteme.domains.synthetic_batch_analysis import SyntheticCausalBatchAnalysisAdapter
from episteme.domains.synthetic_causal import compile_recipe
from episteme.execution import freeze_environment
from episteme.graph import ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.planning import Planning
from episteme.reporting import artifact_inventory, review_bundle
from episteme.recovery import backup, restore
from episteme.search import COMPONENTS, Search
from episteme.store import IntegrityError, Store


class DomainBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="episteme-domain-binding-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "research"
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.study = "manual-domain-fixture"
        self.planner = Actor("manual-planner", "planner")
        self.analyst = Actor("manual-analyst", "analyst")
        self.adapter = SyntheticCausalBatchAnalysisAdapter()
        scope = {"population": "synthetic domain binding fixture"}
        planning = Planning(self.store, self.planner)
        question = planning.question(study_id=self.study,
            statement="Does the synthetic treatment have an effect?",
            objective="Verify a manually frozen domain recipe and execution path",
            scope=scope, constraints=["Fixture data only"],
            stopping_criteria=["One primary and one same-data reanalysis"])
        kernel = Kernel(self.store, self.planner)
        hypotheses = [kernel.hypothesis(text, prediction, falsifier, scope)
                      for text, prediction, falsifier in (
                          ("Null synthetic effect", "Zero contrast", "Nonzero contrast"),
                          ("Nonzero synthetic effect", "Nonzero contrast", "Zero contrast"))]
        explanation_set = planning.explanation_set(question=question,
            hypotheses=hypotheses, comparison_plan="Compare the registered contrast")
        compiled = compile_recipe(
            dict(n_samples=32, assignment="randomized", analysis="difference_in_means"),
            dict(treatment_effect=1.0, confounding_strength=0.0, noise_std=0.0))
        self.primary = self.store.put(compiled["implementation"])
        self.reanalysis = self.store.put(compiled["reanalysis_implementation"])
        self.data = self.store.put(compiled["data"])
        self.environment = freeze_environment(self.store)
        self.protocol = kernel.preregister_for_set(explanation_set=explanation_set,
            design=compiled["design"], metric=compiled["metric"],
            analysis_plan=compiled["analysis_plan"], stopping_rule=compiled["stopping_rule"],
            seeds=[7], run_limit=2, implementation=self.primary,
            environment=self.environment, data=self.data,
            replication_tolerance=1e-9,
            statistical_design=compiled["statistical_design"])
        self.tree = Search(self.store, self.planner).register_tree(
            weights={key: 1 for key in COMPONENTS}, cost_weight=0,
            budget=2, cost_unit="enqueued_attempt", max_nodes=1,
            max_depth=0, max_width=1, max_selections=1, max_retries=0)
        self.node = Search(self.store, self.planner).add_node(self.tree,
            protocol=self.protocol, action="discriminate", estimated_cost=2,
            components=dict(discrimination=1, uncertainty=1, coverage=1, invalidity_risk=0),
            rationale="Exercise the frozen synthetic fixture")
        self.adapter_source = self.store.put(Path(inspect.getfile(type(self.adapter))).read_bytes())
        self.recipe = dict(schema_version=1, domain=self.adapter.adapter_id,
                           parameters=compiled["parameters"], data=self.data)
        self.execution = dict(reanalysis_implementation=self.reanalysis,
            reanalysis_environment=self.environment, outputs=compiled["outputs"],
            wall_seconds=10, max_output_bytes=262144, required_capabilities=[])

    def command(self, action, payload, *, command_id=None, study=None):
        envelope = dict(context=dict(command_id=command_id or uuid4().hex,
            expected_revision=len(self.store.events()), actor=self.planner.id,
            role="planner", study_id=study or self.study,
            correlation_id="manual-domain-fixture", causation_id=None),
            request=dict(version=1, action=action, payload=payload))
        return CommandService(self.store).execute(envelope)

    def bind(self, **changes):
        payload = dict(protocol=self.protocol, adapter_id=self.adapter.adapter_id,
            adapter_version=self.adapter.adapter_version,
            adapter_source_digest=self.adapter_source, recipe=self.recipe,
            recipe_artifacts=[self.data], **self.execution)
        payload.update(changes)
        return self.command("domain.bind", payload)

    def test_manual_recipe_to_completed_batch_claim_and_review_assignment(self):
        binding = self.bind()
        self.assertEqual(binding_index(self.store, self.store.events())[self.protocol]["id"], binding)
        selection = Search(self.store, self.planner).select_next(self.tree)["id"]
        batch = self.command("batch.plan", dict(selection=selection,
            executor="manual-executor", replicator="manual-reanalyst", **self.execution))
        self.assertEqual(advance_batch(self.store, batch)["status"], "completed")
        result = advance_batch_analysis(self.store, batch, planner=self.planner,
            analyst=self.analyst, reviewer_actor="manual-reviewer", adapter=self.adapter)
        self.assertEqual(result["status"], "awaiting_review")
        self.assertEqual(result["scientific_validity"], "not_assessed")
        self.assertTrue(Kernel(self.store, self.analyst).gate(result["claim"])["passed"])
        self.assertEqual(ResearchGraph.from_store(self.store).node(binding).kind.value,
                         "domain_binding")
        inventory = {item["sha256"] for item in artifact_inventory(self.store, self.store.events())}
        self.assertIn(self.adapter_source, inventory)
        self.assertIn(self.store.put_json(self.recipe), inventory)
        self.assertEqual(len(review_bundle(self.store, self.store.events())["domain_bindings"]), 1)
        prior = self.store.export(), self.store.export_receipts()
        self.assertEqual(advance_batch_analysis(self.store, batch, planner=self.planner,
            analyst=self.analyst, reviewer_actor="manual-reviewer", adapter=self.adapter), result)
        self.assertEqual((self.store.export(), self.store.export_receipts()), prior)
        with Store(self.root, read_only=True) as reopened:
            self.assertEqual(ResearchGraph.from_store(reopened).node(binding).kind.value,
                             "domain_binding")

    def test_binding_rejects_wrong_study_duplicate_and_unfrozen_artifacts(self):
        prior = self.store.export(), self.store.export_receipts()
        with self.assertRaises(ValueError):
            self.bind(recipe_artifacts=["0" * 64])
        with self.assertRaises(ValueError):
            self.command("domain.bind", dict(protocol=self.protocol,
                adapter_id=self.adapter.adapter_id, adapter_version="1",
                adapter_source_digest=self.adapter_source, recipe=self.recipe,
                recipe_artifacts=[self.data], **self.execution), study="foreign-study")
        self.assertEqual((self.store.export(), self.store.export_receipts()), prior)
        self.bind()
        after = self.store.export(), self.store.export_receipts()
        with self.assertRaises(ValueError):
            self.bind()
        self.assertEqual((self.store.export(), self.store.export_receipts()), after)

    def test_batch_recipe_drift_is_rejected_before_execution(self):
        self.bind()
        selection = Search(self.store, self.planner).select_next(self.tree)["id"]
        prior = self.store.export(), self.store.export_receipts()
        drifted = dict(self.execution, wall_seconds=11)
        with self.assertRaisesRegex(ValueError, "frozen domain binding"):
            self.command("batch.plan", dict(selection=selection,
                executor="manual-executor", replicator="manual-reanalyst", **drifted))
        self.assertEqual((self.store.export(), self.store.export_receipts()), prior)
        self.assertFalse(any(event["kind"] == "run" for event in self.store.events()))

    def test_binding_cannot_be_added_after_batch_plan(self):
        selection = Search(self.store, self.planner).select_next(self.tree)["id"]
        self.command("batch.plan", dict(selection=selection,
            executor="manual-executor", replicator="manual-reanalyst", **self.execution))
        prior = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "precede"):
            self.bind()
        self.assertEqual((self.store.export(), self.store.export_receipts()), prior)

    def test_bound_recipe_corruption_blocks_claim_gate(self):
        binding = self.bind()
        selection = Search(self.store, self.planner).select_next(self.tree)["id"]
        batch = self.command("batch.plan", dict(selection=selection,
            executor="manual-executor", replicator="manual-reanalyst", **self.execution))
        advance_batch(self.store, batch)
        result = advance_batch_analysis(self.store, batch, planner=self.planner,
            analyst=self.analyst, reviewer_actor="manual-reviewer", adapter=self.adapter)
        event = Kernel._get(self.store.events(), binding, "domain_binding")
        path = self.store.blobs / event["payload"]["recipe_digest"]
        original = path.read_bytes()
        path.write_bytes(original + b"corrupt")
        try:
            with self.assertRaises(IntegrityError):
                Kernel(self.store, self.analyst).gate(result["claim"])
            with self.assertRaises(ValueError):
                binding_index(self.store, self.store.events())
        finally:
            path.write_bytes(original)
        self.assertTrue(Kernel(self.store, self.analyst).gate(result["claim"])["passed"])

    def test_cli_admission_graph_and_verified_restore(self):
        payload = dict(protocol=self.protocol, adapter_id=self.adapter.adapter_id,
            adapter_version=self.adapter.adapter_version,
            adapter_source_digest=self.adapter_source, recipe=self.recipe,
            recipe_artifacts=[self.data], **self.execution)
        envelope = dict(context=dict(command_id="domain-cli-fixture",
            expected_revision=len(self.store.events()), actor=self.planner.id,
            role=self.planner.role, study_id=self.study,
            correlation_id="domain-cli-fixture", causation_id=None),
            request=dict(version=1, action="domain.bind", payload=payload))
        path = self.root.parent / "command.json"
        path.write_text(json.dumps(envelope), encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        call = [sys.executable, "-m", "episteme"]
        first = subprocess.run([*call, "command", "--input", str(path), "--root", str(self.root)],
            capture_output=True, text=True, env=env, check=True)
        binding = json.loads(first.stdout)["result"]
        replay = subprocess.run([*call, "command", "--input", str(path), "--root", str(self.root)],
            capture_output=True, text=True, env=env, check=True)
        self.assertEqual(json.loads(replay.stdout)["result"], binding)
        graph = subprocess.run([*call, "graph", "--root", str(self.root)],
            capture_output=True, text=True, env=env, check=True)
        self.assertEqual(json.loads(graph.stdout)["node_kinds"]["domain_binding"], 1)
        snapshot = self.root.parent / "snapshot"
        backup(self.store, snapshot)
        recovered = self.root.parent / "recovered"
        restore(snapshot, recovered)
        with Store(recovered, read_only=True) as store:
            self.assertEqual(binding_index(store, store.events())[self.protocol]["id"], binding)
            self.assertEqual(ResearchGraph.from_store(store).node(binding).kind.value,
                             "domain_binding")


if __name__ == "__main__":
    unittest.main()
