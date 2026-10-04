"""A pack binding governs its protocol lineage (ADR 0018, audit finding A-03).

Synthetic fixture states with caller-declared actors. The scenarios use only
commands and Store.append calls that existed at audit commit da6aa2a; nothing
here runs an unbound experiment or assesses science.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import test_pack_workflow as workflow
from episteme.analysis_controller import advance_pack_analysis
from episteme.batch_controller import advance_batch
from episteme.graph import ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.recovery import backup, restore
from episteme.search import COMPONENTS, Search
from episteme.store import Store
from review_paths import current_basis

COMPONENT_SCORES = dict(discrimination=1, uncertainty=1, coverage=1, invalidity_risk=0)
_PROTOCOL_FIELDS = ("design", "metric", "analysis_plan", "stopping_rule", "seeds", "run_limit",
                    "implementation", "environment", "data", "replication_tolerance",
                    "statistical_design")


class PackLineageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temporary = TemporaryDirectory(prefix="episteme-pack-lineage-")
        cls.base = Path(cls._temporary.name)
        with Store(cls.base / "origin") as store:
            cls.explanation_set = workflow.planning(store)
            result = workflow.command(store, "pack.preregister",
                                      workflow.preregistration(store, cls.explanation_set))
            cls.protocol, cls.binding_id = result["protocol"], result["binding"]
            search = Search(store, workflow.PLANNER)
            # Room for a follow-up node, so only the lineage rule can refuse it.
            cls.tree = search.register_tree(
                weights={key: 1 for key in COMPONENTS}, cost_weight=0, budget=8,
                cost_unit="enqueued_attempt", max_nodes=3, max_depth=2, max_width=2,
                max_selections=3, max_retries=0)
            cls.node = search.add_node(cls.tree, protocol=cls.protocol, action="discriminate",
                                       estimated_cost=2, components=COMPONENT_SCORES,
                                       rationale="Exercise the pinned pack recipe")
            backup(store, cls.base / "bound")
            binding = Kernel._get(store.events(), cls.binding_id, "pack_binding")
            batch = workflow.plan_batch(store, binding, cls.tree)
            assert advance_batch(store, batch)["status"] == "completed"
            cls.claim = advance_pack_analysis(store, batch, planner=workflow.PLANNER,
                                              analyst=workflow.ANALYST,
                                              reviewer_actor=workflow.REVIEWER)["claim"]
            backup(store, cls.base / "analysed")

    @classmethod
    def tearDownClass(cls):
        cls._temporary.cleanup()

    def restored(self, name: str) -> Store:
        temporary = TemporaryDirectory(prefix="episteme-pack-lineage-copy-")
        self.addCleanup(temporary.cleanup)
        target = Path(temporary.name) / "state"
        restore(self.base / name, target)
        store = Store(target)
        self.addCleanup(store.close)
        return store

    def parent_fields(self, store: Store) -> dict:
        payload = Kernel._get(store.events(), self.protocol, "protocol")["payload"]
        return {field: payload[field] for field in _PROTOCOL_FIELDS}

    def test_amendment_of_pack_bound_protocol_is_rejected(self):
        store = self.restored("bound")
        fields = self.parent_fields(store)
        parent = Kernel._get(store.events(), self.protocol, "protocol")["payload"]
        before = workflow.snapshot(store)
        with self.assertRaisesRegex(ValueError, "cannot be amended outside its pack"):
            workflow.command(store, "kernel.preregister_for_set", dict(
                explanation_set=self.explanation_set, parent=self.protocol,
                amendment_reason="audit amendment", seen_data=[], **fields))
        with self.assertRaisesRegex(ValueError, "cannot be amended outside its pack"):
            workflow.command(store, "kernel.preregister", dict(
                hypotheses=parent["hypotheses"], scope=parent["scope"], parent=self.protocol,
                amendment_reason="audit amendment", seen_data=[], **fields))
        self.assertEqual(workflow.snapshot(store), before)

    def test_followup_of_pack_bound_claim_is_rejected(self):
        store = self.restored("analysed")
        basis = current_basis(store, self.claim)
        recorded = workflow.command(store, "replanning.record_review", dict(
            claim=self.claim, verdict="request_changes", rationale="Needs a discriminating control",
            findings=[dict(kind="discriminating_experiment", action="Run a discriminating control",
                           closure_criterion="A reviewed control result exists",
                           evidence_refs=[self.claim])],
            expected_basis=basis, link_assessments=None), actor=Actor("lineage-reviewer", "reviewer"))
        spec = dict(self.parent_fields(store), amendment_reason="Follow-up control", seen_data=[])
        before = workflow.snapshot(store)
        with self.assertRaisesRegex(ValueError, "cannot be amended outside its pack"):
            workflow.command(store, "followup.apply", dict(
                obligation=recorded["obligations"][0], parent_node=self.node,
                explanation_set=self.explanation_set, protocol_spec=spec,
                node_spec=dict(action="discriminate", components=COMPONENT_SCORES, estimated_cost=2,
                               rationale="Discriminating control"),
                expected_basis=basis))
        self.assertEqual(workflow.snapshot(store), before)

    def test_legacy_root_cannot_reuse_pack_pinned_bytes(self):
        store = self.restored("bound")
        binding = Kernel._get(store.events(), self.binding_id, "pack_binding")
        plan = json.loads(store.read(binding["payload"]["execution_plan"]))
        scope = {"population": "legacy reuse fixture"}
        legacy = Actor("legacy-planner", "planner")
        hypotheses = [Kernel(store, legacy).hypothesis(text, text, "opposite sign", scope)
                      for text in ("signal", "null")]
        fresh_code, fresh_data = store.put(b"legacy code"), store.put(b"legacy data")
        environment = store.put(b"legacy environment")

        def preregister(implementation, data):
            return workflow.command(store, "kernel.preregister", dict(
                hypotheses=hypotheses, scope=scope, design="d", metric="mean", analysis_plan="a",
                stopping_rule="s", seeds=[7], run_limit=2, implementation=implementation,
                environment=environment, data=data, replication_tolerance=0.0), actor=legacy)

        for label, implementation, data in (
                ("primary program", plan["primary_program"]["sha256"], fresh_data),
                ("reanalysis program", plan["reanalysis_program"]["sha256"], fresh_data),
                ("pack input", fresh_code, plan["input"]["sha256"])):
            before = workflow.snapshot(store)
            with self.subTest(reused=label), self.assertRaisesRegex(ValueError, "pinned by a pack binding"):
                preregister(implementation, data)
            self.assertEqual(workflow.snapshot(store), before)
        self.assertTrue(preregister(fresh_code, fresh_data).startswith("protocol-"))

    def test_stray_claim_on_pack_lineage_fails_gate(self):
        store = self.restored("bound")
        history = store.events()
        parent = Kernel._get(history, self.protocol, "protocol")
        p = parent["payload"]
        # Store.append as da6aa2a allowed: an unbound amendment, manual runs and a claim.
        child = dict(p, parent=self.protocol, amendment_reason="unbound audit amendment",
                     seen_data=sorted(set(p["seen_data"]) | Kernel._known_seen_data(history, self.protocol)))
        child_event = store.append(id="protocol-stray-amendment", kind="protocol", actor=workflow.PLANNER.id,
                                   role="planner", payload=child, expected_revision=len(history))
        outputs = dict(raw_data=p["data"], metrics=store.put_json({p["metric"]: 3.14}),
                       log=store.put(b"no process ran"))
        runs = []
        for actor, implementation, original in (
                (Actor("manual-executor", "executor"), p["implementation"], None),
                (Actor("manual-replicator", "replicator"), store.put(b"manual reanalysis"), "first")):
            run = f"run-stray-{actor.role}"
            store.append(id=run, kind="run", actor=actor.id, role=actor.role, payload=dict(
                protocol=child_event["id"], protocol_hash=child_event["hash"], seed=p["seeds"][0],
                implementation=implementation, environment=p["environment"], command=["python", "x.py"],
                replicate_of=runs[0] if original else None), expected_revision=len(store.events()))
            store.append(id=f"result-stray-{actor.role}", kind="result", actor=actor.id, role=actor.role,
                         payload=dict(run=run, status="completed", outputs=outputs, reason=""),
                         expected_revision=len(store.events()))
            runs.append(run)
        store.append(id="claim-stray-lineage", kind="claim", actor="manual-analyst", role="analyst",
                     payload=dict(protocol=child_event["id"], statement="The treatment robustly increases Y.",
                                  scope=p["scope"], evidence=runs, limitations=["fixture"],
                                  outcome="supports", inference_mode="exploratory"),
                     expected_revision=len(store.events()))
        reader = Kernel(store, Actor("lineage-observer", "observer"))
        gate = reader.gate("claim-stray-lineage")
        self.assertFalse(gate["passed"], gate)
        self.assertIn("claim on a pack-bound protocol lineage lacks pack.analyse admission", gate["failures"])
        self.assertEqual(reader.next_action("claim-stray-lineage")["action"], "repair_evidence")
        graph = ResearchGraph.from_store(store)
        self.assertEqual(graph.node("claim-stray-lineage").kind.value, "claim")


if __name__ == "__main__":
    unittest.main()
