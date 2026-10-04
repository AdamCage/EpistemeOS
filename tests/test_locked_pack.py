"""DomainPack and execution batches on profile v2 (ADR 0019, step 3).

``locked_fixture_v2`` is a test pack, not a registered adapter. Its groups and
means are invented. A finished batch is managed execution of that fixture;
``scientific_validity`` stays not_assessed, and ``reproduce`` is a recorded
same-code replay, never independent evidence. No test reaches the network.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

import locked_support as fx
from episteme import domain_packs, execution_locked as locked
from episteme.analysis_controller import advance_pack_analysis
from episteme.batch import _recipe
from episteme.batch_controller import advance_batch
from episteme.commands import CommandService
from episteme.domains import api, registry
from episteme.domains.synthetic_causal import compile_recipe
from episteme.execution import freeze_environment
from episteme.graph import ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.planning import Planning
from episteme.recovery import backup, restore
from episteme.reporting import artifact_inventory
from episteme.reproduction import reproduce
from episteme.review_assignment import _index as assignment_index
from episteme.search import COMPONENTS, Search
from episteme.store import Store, digest


_PACKS = Path(__file__).resolve().parent / "fixtures" / "packs"
sys.path.insert(0, str(_PACKS))
importlib.invalidate_caches()
import locked_fixture_v2 as fixture  # noqa: E402


STUDY = "locked-pack-fixture"
PLANNER = Actor("locked-planner", "planner")
ANALYST = Actor("locked-analyst", "analyst")
REVIEWER = "locked-reviewer"
PARAMETERS = dict(scale=1, outcome="inconclusive", inference_mode="descriptive")
HOST = dict(groups=[[1, 2]])
OUTPUTS = {"raw_data": "raw.json", "metrics": "metrics.json"}
_REGISTERED = patch.dict(registry.PACKS, {fixture.PACK_ID: fixture.PACK_ID})


def command(store: Store, action: str, payload: dict, *, actor: Actor = PLANNER) -> object:
    return CommandService(store).execute(dict(
        context=dict(command_id=uuid4().hex, expected_revision=len(store.events()),
                     actor=actor.id, role=actor.role, study_id=STUDY,
                     correlation_id="locked-pack-v2", causation_id=None),
        request=dict(version=1, action=action, payload=payload)))


def planning(store: Store) -> str:
    scope = {"population": "locked fixture groups"}
    plan = Planning(store, PLANNER)
    question = plan.question(study_id=STUDY, statement="What is the mean of each frozen fixture group?",
                             objective="Exercise profile v2 through a DomainPack", scope=scope,
                             constraints=["Invented integers only"],
                             stopping_criteria=["One primary and one same-data reanalysis"])
    kernel = Kernel(store, PLANNER)
    hypotheses = [kernel.hypothesis(text, prediction, falsifier, scope) for text, prediction, falsifier in (
        ("Fixture mean is the scaled average", "The recomputed mean matches the program",
         "The recomputed mean differs"),
        ("Fixture mean is not the scaled average", "The recomputed mean differs",
         "The recomputed mean matches"))]
    return plan.explanation_set(question=question, hypotheses=hypotheses,
                                comparison_plan="Compare the recomputed group mean with the recorded one")


def closure(store: Store, project: dict[str, bytes] | None = None, **options) -> str:
    return locked.freeze_closure(store, fixture.project() if project is None else project,
                                 set_variables=options.pop("set_variables", fixture.VARIABLES), **options)


def preregister(store: Store, explanation_set: str, environment: str) -> dict:
    loaded = registry.load_pack(fixture.PACK_ID)
    return command(store, "pack.preregister", dict(
        explanation_set=explanation_set, pack_id=fixture.PACK_ID, pack_version=loaded.pack_version,
        pack_code_digest=loaded.pack_code_digest, parameters=dict(PARAMETERS),
        host_inputs=dict(HOST), capture=None, environment=environment))


def open_store(test: unittest.TestCase) -> Store:
    from tempfile import TemporaryDirectory
    directory = TemporaryDirectory(prefix="episteme-locked-pack-")
    test.addCleanup(directory.cleanup)
    store = Store(Path(directory.name) / "state")
    test.addCleanup(store.close)
    return store


class RegistryBoundaryTests(unittest.TestCase):
    def test_fixture_pack_is_not_registered_and_shipped_packs_stay_on_v1(self):
        self.assertNotIn(fixture.PACK_ID, registry.PACKS)
        with self.assertRaisesRegex(registry.PackError, "explicit registry"):
            registry.load_pack(fixture.PACK_ID)
        for pack_id in ("synthetic_causal_v1", "afterlife_seed_v1"):
            manifest = registry.load_pack(pack_id).manifest
            self.assertEqual(list(manifest.execution_profiles), ["trusted_local_python_v1"])


@_REGISTERED
class LockedPackAdmissionTests(unittest.TestCase):
    """Plan, preregistration and batch recipe checks; no process is launched."""

    def request(self):
        return api.CompileRequest(parameters=dict(PARAMETERS), host_inputs={"groups": [[1, 2]]},
                                   capture=None)

    def test_execution_plan_v2_round_trips_and_v1_plans_still_load(self):
        plan = fixture.compile_execution(self.request())
        self.assertIsInstance(plan, api.ExecutionPlanV2)
        self.assertEqual(plan.execution_profile, "uv_locked_python_v2")
        frozen = plan.to_dict()
        api.validate_schema("execution-plan-v2", frozen)
        self.assertEqual(frozen["environment_requirements"]["closure_level"], "uv_lock")
        self.assertEqual(frozen["environment_requirements"]["network_policy"], "not_enforced")
        blobs = plan.blobs()
        self.assertEqual(api.plan_from_frozen(frozen, blobs.__getitem__), plan)
        self.assertEqual(api.ExecutionPlanV2.from_frozen(frozen, blobs.__getitem__).digest(), plan.digest())
        with self.assertRaisesRegex(api.EnvelopeError, "unsupported execution plan"):
            api.plan_from_frozen({"schema_version": 3}, blobs.__getitem__)
        recipe = compile_recipe(dict(n_samples=32, assignment="randomized", analysis="difference_in_means"),
                                dict(treatment_effect=0.0, confounding_strength=0.0, noise_std=0.0))
        legacy = api.ExecutionPlan(
            primary_program=recipe["implementation"], reanalysis_program=recipe["reanalysis_implementation"],
            input=recipe["data"], outputs=recipe["outputs"], wall_seconds=10, max_output_bytes=262144,
            required_capabilities=[], execution_profile="trusted_local_python_v1",
            environment_requirements=api.environment_requirements())
        self.assertEqual(api.plan_from_frozen(legacy.to_dict(), legacy.blobs().__getitem__), legacy)

    def test_preregister_pins_v2_and_rejects_a_different_closure(self):
        store = open_store(self)
        explanation = planning(store)
        environment = closure(store, installer_version="9.9.9")
        result = preregister(store, explanation, environment)
        binding = Kernel._get(store.events(), result["binding"], "pack_binding")
        payload = binding["payload"]
        self.assertEqual((payload["execution_profile"], payload["closure_level"],
                          payload["scientific_validity"], payload["pack_trust"]),
                         ("uv_locked_python_v2", "uv_lock", "not_assessed", "trusted_local_code"))
        protocol = Kernel._get(store.events(), result["protocol"], "protocol")["payload"]
        plan = api.strict_loads(store.read(payload["execution_plan"]), "execution plan")
        self.assertEqual(plan["schema_version"], 2)
        self.assertEqual(protocol["implementation"], plan["primary_program"]["sha256"])
        self.assertEqual(protocol["environment"], environment)
        self.assertEqual(protocol["data"], plan["input"]["sha256"])
        keys = domain_packs.plan_keys(store, plan)
        self.assertIn(digest(fixture.PRIMARY["main.py"]), keys)
        self.assertIn(digest(fixture.project()[fixture.WHEEL_PATH]), keys)
        graph = ResearchGraph.from_store(store)
        ancestors = {node.id for node in graph.ancestors(result["binding"])}
        for key in keys:
            self.assertIn(ResearchGraph.artifact_id(key), ancestors)
        excluded = domain_packs.review_excluded(store, binding)
        self.assertIn(digest(fixture.PRIMARY["main.py"]), excluded)
        self.assertIn(digest(fixture.project()[fixture.WHEEL_PATH]), excluded)
        self.assertNotIn(plan["input"]["sha256"], excluded)
        before = len(store.events())
        other = dict(fixture.project())
        other["pyproject.toml"] = other["pyproject.toml"].replace(b'version = "0"', b'version = "1"')
        with self.assertRaisesRegex(ValueError, "closure project differs"):
            preregister(store, explanation, closure(store, other, installer_version="9.9.9"))
        with self.assertRaisesRegex(ValueError, "does not set the variables"):
            preregister(store, explanation, closure(
                store, set_variables={"PYTHONUTF8": "1"}, installer_version="9.9.9"))
        with self.assertRaisesRegex(ValueError, "unsupported environment closure"):
            preregister(store, explanation, freeze_environment(store))
        self.assertEqual(len(store.events()), before)

    def test_batch_recipe_checks_each_pair_under_its_own_profile(self):
        store = open_store(self)
        data = store.put(fixture.compile_execution(self.request()).input)
        primary = locked.freeze_source(store, fixture.PRIMARY, entry_point="main.py")
        reanalysis = locked.freeze_source(store, fixture.REANALYSIS, entry_point="reanalyse.py")
        v2 = closure(store, installer_version="9.9.9")
        legacy = store.put(b"print('v1 fixture')\n")
        v1 = freeze_environment(store)
        protocol = dict(implementation=primary, environment=v2, data=data)
        limits = dict(outputs=dict(OUTPUTS), wall_seconds=30, max_output_bytes=65536)
        _recipe(store, dict(required_capabilities=["locked_environment", "workspace_outside_store"],
                            reanalysis_implementation=reanalysis, reanalysis_environment=v2, **limits),
                protocol)
        _recipe(store, dict(required_capabilities=["separate_cwd", "bounded_output_capture"],
                            reanalysis_implementation=legacy, reanalysis_environment=v1, **limits),
                protocol)
        with self.assertRaisesRegex(ValueError, "invalid batch capabilities"):
            _recipe(store, dict(required_capabilities=["locked_environment"],
                                reanalysis_implementation=legacy, reanalysis_environment=v1, **limits),
                    protocol)
        with self.assertRaisesRegex(ValueError, "invalid batch capabilities"):
            _recipe(store, dict(required_capabilities=["python_isolated_mode"],
                                reanalysis_implementation=reanalysis, reanalysis_environment=v2, **limits),
                    protocol)


@_REGISTERED
class LockedPackWorkflowTests(unittest.TestCase):
    """pack.preregister → batch → reproduce → pack.analyse, offline."""

    @unittest.skipUnless(fx.UV, fx.UV_REASON)
    def test_completed_batch_is_managed_evidence_and_replay_is_not(self):
        store = open_store(self)
        environment = closure(store)
        registered = preregister(store, planning(store), environment)
        protocol_id, binding_id = registered["protocol"], registered["binding"]
        binding = Kernel._get(store.events(), binding_id, "pack_binding")
        search = Search(store, PLANNER)
        tree = search.register_tree(weights={key: 1 for key in COMPONENTS}, cost_weight=0, budget=2,
                                    cost_unit="enqueued_attempt", max_nodes=1, max_depth=0,
                                    max_width=1, max_selections=1, max_retries=0)
        search.add_node(tree, protocol=protocol_id, action="discriminate", estimated_cost=2,
                        components=dict(discrimination=1, uncertainty=1, coverage=1, invalidity_risk=0),
                        rationale="Run the pinned profile v2 fixture once per frozen group")
        batch = command(store, "batch.plan", dict(
            selection=search.select_next(tree)["id"], executor="locked-executor",
            replicator="locked-reanalyst", **domain_packs.execution_fields(store, binding)))
        settled = advance_batch(store, batch)
        self.assertEqual(settled["status"], "completed",
                         [(row["slot"], row["status"]) for row in settled["slots"]])
        history = store.events()
        runs = [event for event in history if event["kind"] == "run"]
        jobs = {event["payload"]["run"]: event for event in history if event["kind"] == "execution_job"}
        finalized = {event["payload"]["job"] for event in history if event["kind"] == "execution_finalized"}
        self.assertEqual(len(runs), 2)
        for run in runs:
            job = jobs[run["id"]]
            self.assertIn(job["id"], finalized)
            spec = json.loads(store.read(job["payload"]["specification"]))
            self.assertEqual((spec["schema_version"], spec["profile"]), (2, "uv_locked_python_v2"))
        primary = next(run["id"] for run in runs if run["payload"]["replicate_of"] is None)
        replay = reproduce(store, primary)
        self.assertEqual((replay["status"], replay["replay"], replay["counts_as_evidence"],
                          replay["scientific_validity"]),
                         ("matched", "same_code_fresh_environment", False, "not_assessed"))
        self.assertEqual([event["id"] for event in store.events() if event["kind"] == "run"],
                         [run["id"] for run in runs])
        self.assertFalse(any(event["kind"] == "result" and event["payload"]["run"] not in {run["id"] for run in runs}
                             for event in store.events()))
        reproduced = next(event for event in store.events() if event["kind"] == "execution_reproduction")
        self.assertEqual((reproduced["payload"]["counts_as_evidence"], reproduced["payload"]["replication_mode"],
                          reproduced["payload"]["scientific_validity"], reproduced["payload"]["verdict"]),
                         (False, "none", "not_assessed", "matched"))
        analysed = advance_pack_analysis(store, batch, planner=PLANNER, analyst=ANALYST,
                                         reviewer_actor=REVIEWER)
        self.assertEqual(analysed["scientific_validity"], "not_assessed")
        history = store.events()
        claim = Kernel._get(history, analysed["claim"], "claim")["payload"]
        self.assertEqual((claim["outcome"], claim["inference_mode"]), ("inconclusive", "descriptive"))
        self.assertEqual(set(claim["evidence"]), {run["id"] for run in runs})
        self.assertNotIn(reproduced["id"], claim["evidence"])
        gate = Kernel(store, Actor("locked-auditor", "observer")).gate(analysed["claim"])
        self.assertTrue(gate["passed"], gate["failures"])
        self.assertEqual(gate["scientific_validity"], "not_assessed")
        basis, evidence_runs = Kernel(store, Actor("locked-auditor", "observer"))._local_evidence(
            history, analysed["claim"])
        self.assertTrue(any(event["kind"] == "execution_reproduction" for event in basis))
        self.assertEqual({event["kind"] for event in evidence_runs}, {"run"})
        self.assertTrue(all(jobs[event["id"]]["id"] in finalized for event in evidence_runs))
        assignment = assignment_index(store, history)[analysed["assignment"]]
        allowed = set(json.loads(store.read(assignment["payload"]["bundle"]))["allowed_artifact_digests"])
        excluded = domain_packs.review_excluded(store, Kernel._get(history, binding_id, "pack_binding"))
        self.assertIn(digest(fixture.PRIMARY["main.py"]), excluded)
        self.assertIn(digest(fixture.project()[fixture.WHEEL_PATH]), excluded)
        self.assertFalse(allowed & excluded)
        inventory = {row["sha256"] for row in artifact_inventory(store, history)}
        self.assertLessEqual(domain_packs.binding_artifacts(store, Kernel._get(history, binding_id, "pack_binding")),
                             inventory)
        self.assertFalse(any(event["kind"] in {"review", "paper"} for event in history))
        snapshot = Path(store.root).parent / "snapshot"
        backup(store, snapshot)
        restored = Path(store.root).parent / "restored"
        restore(snapshot, restored)
        with Store(restored) as copy:
            self.assertEqual({row["sha256"] for row in artifact_inventory(copy, copy.events())}, inventory)
            self.assertEqual(len(copy.events()), len(history))


if __name__ == "__main__":
    unittest.main()
