"""pack.preregister → batch → pack.analyse → review.assign through the kernel.

Every study here is a synthetic fixture with caller-declared actors. Passing
means the contract and the claim-strength ceiling hold mechanically; no
reviewer verdict is created and scientific_validity stays not_assessed.
"""

from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

import golden_support
import pack_fixtures
from episteme import domain_packs
from episteme.analysis_controller import advance_batch_analysis, advance_pack_analysis, analysis_state
from episteme.batch_controller import advance_batch
from episteme.commands import CommandService
from episteme.domains import api, registry
from episteme.domains.synthetic_batch_analysis import SyntheticCausalBatchAnalysisAdapter
from episteme.execution import freeze_environment
from episteme.graph import NodeKind, ResearchGraph
from episteme.kernel import Actor, GateError, Kernel
from episteme.planning import Planning
from episteme.recovery import backup, restore
from episteme.reporting import artifact_inventory, review_bundle
from episteme.review_assignment import _index as assignment_index
from episteme.search import COMPONENTS, Search
from episteme.store import IntegrityError, Store, canonical, digest


STUDY = "pack-workflow-fixture"
PLANNER = Actor("pack-planner", "planner")
ANALYST = Actor("pack-analyst", "analyst")
REVIEWER = "pack-reviewer"
PARAMETERS = dict(n_samples=32, assignment="randomized", analysis="difference_in_means")
HOST = dict(world=dict(treatment_effect=1.0, confounding_strength=0.0, noise_std=0.5),
            seeds=[7], replication_tolerance=1e-9)


def command(store: Store, action: str, payload: dict, *, actor: Actor = PLANNER,
            cause: str | None = None) -> object:
    return CommandService(store).execute(dict(
        context=dict(command_id=uuid4().hex, expected_revision=len(store.events()),
                     actor=actor.id, role=actor.role, study_id=STUDY,
                     correlation_id="pack-workflow", causation_id=cause),
        request=dict(version=1, action=action, payload=copy.deepcopy(payload))))


def planning(store: Store) -> str:
    scope = {"population": "synthetic pack workflow fixture"}
    plan = Planning(store, PLANNER)
    question = plan.question(study_id=STUDY, statement="Does the synthetic treatment shift Y?",
                             objective="Exercise the DomainPack contract end to end", scope=scope,
                             constraints=["Fixture data only"],
                             stopping_criteria=["One primary and one same-data reanalysis per unit"])
    kernel = Kernel(store, PLANNER)
    hypotheses = [kernel.hypothesis(text, prediction, falsifier, scope)
                  for text, prediction, falsifier in (
                      ("Null synthetic effect", "Zero contrast", "Nonzero contrast"),
                      ("Positive synthetic effect", "Positive contrast", "Zero contrast"))]
    return plan.explanation_set(question=question, hypotheses=hypotheses,
                                comparison_plan="Compare the registered contrast")


def preregistration(store: Store, explanation_set: str, *, pack_id: str = "synthetic_causal_v1",
                    parameters: dict | None = None, host: dict | None = None, **changes) -> dict:
    loaded = registry.load_pack(pack_id)
    payload = dict(explanation_set=explanation_set, pack_id=pack_id,
                   pack_version=loaded.pack_version, pack_code_digest=loaded.pack_code_digest,
                   parameters=PARAMETERS if parameters is None else parameters,
                   host_inputs=HOST if host is None else host, capture=None,
                   environment=freeze_environment(store))
    payload.update(changes)
    return payload


def search_node(store: Store, protocol: str, cost: int) -> str:
    search = Search(store, PLANNER)
    tree = search.register_tree(weights={key: 1 for key in COMPONENTS}, cost_weight=0, budget=cost,
                                cost_unit="enqueued_attempt", max_nodes=1, max_depth=0,
                                max_width=1, max_selections=1, max_retries=0)
    search.add_node(tree, protocol=protocol, action="discriminate", estimated_cost=cost,
                    components=dict(discrimination=1, uncertainty=1, coverage=1, invalidity_risk=0),
                    rationale="Exercise the pinned pack recipe")
    return tree


def batch_plan(store: Store, binding: dict, selection: str) -> str:
    return command(store, "batch.plan", dict(
        selection=selection, executor="pack-executor", replicator="pack-reanalyst",
        **domain_packs.execution_fields(store, binding)))


def plan_batch(store: Store, binding: dict, tree: str) -> str:
    return batch_plan(store, binding, Search(store, PLANNER).select_next(tree)["id"])


def snapshot(store: Store) -> tuple:
    return store.export(), store.export_receipts()


class PackWorkflowTests(unittest.TestCase):
    """One bound and one completed fixture state, restored fresh for every test."""

    @classmethod
    def setUpClass(cls):
        cls._temporary = TemporaryDirectory(prefix="episteme-pack-workflow-")
        cls.base = Path(cls._temporary.name)
        with Store(cls.base / "origin") as store:
            explanation_set = planning(store)
            result = command(store, "pack.preregister", preregistration(store, explanation_set))
            cls.protocol, cls.binding_id = result["protocol"], result["binding"]
            cls.tree = search_node(store, cls.protocol, 2)
            backup(store, cls.base / "bound")
            binding = Kernel._get(store.events(), cls.binding_id, "pack_binding")
            cls.batch = plan_batch(store, binding, cls.tree)
            assert advance_batch(store, cls.batch)["status"] == "completed"
            backup(store, cls.base / "completed")

    @classmethod
    def tearDownClass(cls):
        cls._temporary.cleanup()

    def restored(self, name: str) -> Store:
        temporary = TemporaryDirectory(prefix="episteme-pack-copy-")
        self.addCleanup(temporary.cleanup)
        target = Path(temporary.name) / "state"
        restore(self.base / name, target)
        store = Store(target)
        self.addCleanup(store.close)
        return store

    def binding(self, store: Store) -> dict:
        return Kernel._get(store.events(), self.binding_id, "pack_binding")

    def advance(self, store: Store) -> dict:
        return advance_pack_analysis(store, self.batch, planner=PLANNER, analyst=ANALYST,
                                     reviewer_actor=REVIEWER)

    def honest(self, store: Store) -> dict:
        loaded = registry.load_pack("synthetic_causal_v1")
        _, context, cas = domain_packs.analysis_inputs(store, store.events(), self.batch)
        hooks = domain_packs.run_hooks(loaded, context, cas)
        settlement = domain_packs._batch_state(store, store.events(), self.batch, None)["state"]["settlement"]
        return dict(batch=self.batch, expected_settlement=settlement["id"],
                    pack_id="synthetic_causal_v1", pack_version=loaded.pack_version,
                    pack_code_digest=loaded.pack_code_digest, checks=hooks["checks"],
                    recomputations=hooks["recomputations"], report=hooks["report"],
                    statistical_report=hooks["statistical_report"], reviewer_actor=REVIEWER)

    def analyse(self, store: Store, payload: dict) -> dict:
        return command(store, "pack.analyse", payload, actor=ANALYST)

    def test_pinned_binding_records_protocol_code_and_envelopes(self):
        store = self.restored("bound")
        binding = self.binding(store)
        p = binding["payload"]
        loaded = registry.load_pack("synthetic_causal_v1")
        self.assertEqual((p["pack_id"], p["pack_version"], p["pack_code_digest"]),
                         ("synthetic_causal_v1", "1", loaded.pack_code_digest))
        self.assertEqual((p["roster_semantics"], p["sample_size_scope"], p["execution_profile"],
                          p["closure_level"], p["pack_trust"], p["hook_isolation"],
                          p["pack_pinning"], p["hidden_inputs"], p["scientific_validity"]),
                         ("rng_seed", "per_roster_unit", "trusted_local_python_v1",
                          "interpreter_fingerprint", "trusted_local_code", "in_process",
                          "pack_code_manifest", ["protocol_data"], "not_assessed"))
        self.assertEqual(json.loads(store.read(p["pack_code_digest"])), api.thaw(loaded.code_manifest))
        for path, data in loaded.files.items():
            self.assertEqual(store.read(digest(data)), data, path)
        self.assertEqual(json.loads(store.read(p["host_inputs"])), HOST)
        protocol = Kernel._get(store.events(), self.protocol, "protocol")["payload"]
        self.assertEqual(protocol["seeds"], [7])
        self.assertEqual(protocol["protocol_mode"], "exploratory")
        self.assertEqual(domain_packs._binding_index(store, store.events())[self.protocol]["id"],
                         self.binding_id)
        graph = ResearchGraph.from_store(store)
        self.assertEqual(graph.node(self.binding_id).kind, NodeKind.PACK_BINDING)
        closure = {node.id for node in graph.ancestors(self.binding_id)}
        for data in loaded.files.values():
            self.assertIn(ResearchGraph.artifact_id(digest(data)), closure)
        self.assertNotIn("world", json.dumps(p))

    def test_completed_batch_to_bounded_claim_assignment_replay_and_restore(self):
        store = self.restored("completed")
        self.assertEqual(analysis_state(store, self.batch)["status"], "awaiting_analysis")
        result = self.advance(store)
        self.assertEqual((result["status"], result["scientific_validity"]),
                         ("awaiting_review", "not_assessed"))
        history = store.events()
        analysis = Kernel._get(history, result["analysis"], "pack_analysis")["payload"]
        claim = Kernel._get(history, result["claim"], "claim")["payload"]
        self.assertEqual((claim["outcome"], claim["inference_mode"]), ("inconclusive", "exploratory"))
        self.assertEqual(claim["limitations"][:3], json.loads(store.read(analysis["report"]))["limitations"])
        self.assertEqual([line.split(" was not supplied")[0] for line in claim["limitations"][3:5]],
                         ["Statistical report field effect_size", "Statistical report field assumptions"])
        self.assertEqual(claim["limitations"][5], (
            "Kernel claim-strength ceiling v1: inference at most exploratory; allowed outcomes "
            f"inconclusive; {domain_packs.REASON_INTERVAL}."))
        self.assertEqual(len(claim["limitations"]), 6)
        self.assertEqual(analysis["ceiling"]["max_inference_mode"], "exploratory")
        self.assertEqual(analysis["ceiling"]["allowed_outcomes"], ["inconclusive"])
        self.assertEqual(analysis["report_origin"], "recomputed_by_pinned_pack_at_admission")
        self.assertEqual(analysis["replication"]["roster_repetition"],
                         "fresh_seed_repetition_of_one_generator")
        protocol = Kernel._get(history, self.protocol, "protocol")["payload"]
        self.assertEqual(analysis["hidden_digests"], [protocol["data"]])
        self.assertNotIn(protocol["data"], analysis["cas_allowlist"])
        evidence = Kernel(store, Actor("fixture-auditor", "observer"))._local_evidence(history, result["claim"])[0]
        self.assertTrue({"pack_binding", "pack_analysis"} <= {event["kind"] for event in evidence})
        self.assertTrue(Kernel(store, ANALYST).gate(result["claim"])["passed"])
        assignment = assignment_index(store, history)[result["assignment"]]
        manifest = json.loads(store.read(assignment["payload"]["bundle"]))
        excluded = domain_packs.review_excluded(store, self.binding(store)) | \
            domain_packs.review_excluded(store, Kernel._get(history, result["analysis"], "pack_analysis"))
        self.assertFalse(set(manifest["allowed_artifact_digests"]) & excluded)
        self.assertEqual(manifest["policy"], "blind_initial_review_v2")
        [pack_report] = manifest["pack_reports"]
        self.assertEqual(pack_report["analysis"], result["analysis"])
        self.assertNotIn("details", pack_report["statistical_report"])
        self.assertIn(dict(claim=result["claim"], analysis=result["analysis"],
                           provenance="recomputed_by_pinned_pack_at_admission"), manifest["analysis_provenance"])
        graph = ResearchGraph.from_store(store)
        self.assertEqual(graph.node(result["analysis"]).kind, NodeKind.PACK_ANALYSIS)
        closure = {node.id for node in graph.ancestors(result["analysis"])}
        for key in (analysis["report"], analysis["statistical_report"], analysis["pack_code_digest"]):
            self.assertIn(ResearchGraph.artifact_id(key), closure)
        inventory = {row["sha256"] for row in artifact_inventory(store, history)}
        self.assertLessEqual(domain_packs.binding_artifacts(store, self.binding(store)) | excluded,
                             inventory)
        bundle = review_bundle(store, history)
        self.assertEqual([event["id"] for event in bundle["pack_analyses"]], [result["analysis"]])
        self.assertEqual([event["id"] for event in bundle["pack_bindings"]], [self.binding_id])
        recorded = snapshot(store)
        self.assertEqual(self.advance(store), result)
        self.assertEqual(snapshot(store), recorded)
        status = analysis_state(store, self.batch)
        self.assertEqual((status["status"], status["analyses"][0]["assignments"]),
                         ("awaiting_review", [result["assignment"]]))
        self.assertFalse(any(event["kind"] in {"review", "paper"} for event in store.events()))
        restored = Path(store.root).parent / "restored"
        backup(store, Path(store.root).parent / "snapshot")
        restore(Path(store.root).parent / "snapshot", restored)
        with Store(restored, read_only=True) as copy_:
            self.assertEqual(ResearchGraph.from_store(copy_).to_json(), graph.to_json())
            self.assertEqual(list(domain_packs._analysis_index(copy_, copy_.events())), [result["analysis"]])

    def test_restart_after_analysis_receipt_resumes_without_duplicate(self):
        store = self.restored("completed")
        original = CommandService.execute

        def interrupted(service, envelope):
            if envelope["request"]["action"] == "review.assign":
                raise RuntimeError("controller stopped after the analysis receipt")
            return original(service, envelope)

        with patch.object(CommandService, "execute", interrupted):
            with self.assertRaisesRegex(RuntimeError, "controller stopped"):
                self.advance(store)
        kinds = [event["kind"] for event in store.events()]
        self.assertEqual((kinds.count("claim"), kinds.count("pack_analysis"),
                          kinds.count("review_assignment")), (1, 1, 0))
        result = self.advance(store)
        kinds = [event["kind"] for event in store.events()]
        self.assertEqual((kinds.count("claim"), kinds.count("pack_analysis"),
                          kinds.count("review_assignment")), (1, 1, 1))
        self.assertEqual(result["status"], "awaiting_review")

    def test_report_tampering_and_proposals_above_the_ceiling_are_rejected(self):
        store = self.restored("completed")
        honest = self.honest(store)
        before = snapshot(store)

        def changed(edit):
            payload = copy.deepcopy(honest)
            edit(payload)
            return payload

        def statistical(**fields):
            def edit(payload):
                payload["statistical_report"].update(fields)
                payload["report"]["statistical_report"] = digest(canonical(payload["statistical_report"]))
            return edit

        def misstate(payload):
            row = payload["recomputations"][0]
            row["primary_recorded"] += 1e-12
            row["primary_absolute_difference"] = abs(row["recomputed"] - row["primary_recorded"])

        cases = {
            "missing field": (changed(lambda p: (p["statistical_report"].pop("assumptions"),
                p["report"].__setitem__("statistical_report", digest(canonical(p["statistical_report"]))))),
                "lacks required fields: assumptions"),
            "not_applicable contrary to design": (changed(statistical(
                effect_size=api.not_applicable("No effect size here."))), "contrary"),
            "empty reason": (changed(statistical(assumptions=dict(status="not_supplied", reason=" "))),
                             "without a reason"),
            "supports above ceiling": (changed(lambda p: p["report"].__setitem__("outcome", "supports")),
                                       "exceeds the ceiling"),
            "confirmatory above ceiling": (changed(lambda p: p["report"].__setitem__(
                "inference_mode", "confirmatory")), "exceeds the ceiling"),
            "misstated record": (changed(misstate), "misstates a recorded metric"),
            "wrong planned total": (changed(statistical(sample_size=api.supplied(dict(
                honest["statistical_report"]["sample_size"]["value"], planned_total=64)))),
                "sample size differs"),
            "undeclared deviation": (changed(statistical(estimand=api.supplied(dict(
                honest["statistical_report"]["estimand"]["value"], text="Another target."))) ),
                "undeclared deviation"),
            "check order": (changed(lambda p: p["checks"].reverse()), "cover the batch roster"),
            "foreign pin": (changed(lambda p: p.__setitem__("pack_code_digest", "0" * 64)),
                            "differs from the pinned binding"),
            "foreign protocol": (changed(lambda p: p["report"].__setitem__("protocol_hash", "1" * 64)),
                                 "different protocols"),
            "consistent but not computed by the pin": (changed(lambda p: p["report"].__setitem__(
                "statement", "A different, internally consistent statement.")),
                "differs from what the pinned pack computes"),
        }
        for label, (payload, message) in cases.items():
            with self.subTest(case=label):
                with self.assertRaisesRegex(ValueError, message):
                    self.analyse(store, payload)
                self.assertEqual(snapshot(store), before)

    def test_declared_deviation_caps_inference_and_edited_reports_are_not_admitted(self):
        store = self.restored("completed")
        payload = self.honest(store)
        estimand = payload["statistical_report"]["estimand"]["value"]
        payload["statistical_report"]["estimand"] = api.supplied(dict(estimand, text="A narrower target."))
        payload["statistical_report"]["deviations"] = api.supplied([dict(
            field="estimand", plan=estimand["text"], actual="A narrower target.",
            reason="Fixture deviation used to exercise the ceiling.")])
        payload["report"]["statistical_report"] = digest(canonical(payload["statistical_report"]))
        facts = domain_packs._batch_state(store, store.events(), self.batch, None)
        statistical = api.StatisticalReport.from_dict(payload["statistical_report"])
        report = api.AnalysisReport.from_frozen(payload["report"], statistical)
        recomputations = [api.Recomputation.from_dict(row) for row in payload["recomputations"]]
        ceiling = domain_packs.ceiling(statistical, report, facts,
                                       domain_packs._kernel_fields(store, facts), recomputations)
        self.assertEqual(ceiling["detected_deviations"], ["estimand"])
        self.assertEqual(ceiling["max_inference_mode"], "exploratory")
        self.assertIn(domain_packs.REASON_DEVIATIONS, ceiling["reasons"])
        self.assertTrue(ceiling["kernel_limitations"][-2].startswith(
            "Statistical report deviation in estimand"))
        before = snapshot(store)
        # The ceiling admits this report, but the pinned pack never computed it.
        with self.assertRaisesRegex(ValueError, "differs from what the pinned pack computes"):
            self.analyse(store, payload)
        self.assertEqual(snapshot(store), before)

    def test_legacy_claims_and_manual_runs_cannot_bypass_the_pack_ceiling(self):
        store = self.restored("completed")
        settlement = domain_packs._batch_state(store, store.events(), self.batch, None)["state"]["settlement"]
        before = snapshot(store)
        with self.assertRaisesRegex(ValueError, "admitted only by pack.analyse"):
            command(store, "kernel.claim", dict(
                protocol=self.protocol, statement="Supported without the pack ceiling.",
                scope=Kernel._get(store.events(), self.protocol, "protocol")["payload"]["scope"],
                evidence=settlement["payload"]["runs"], limitations=["None"], outcome="supports",
                inference_mode="exploratory"), actor=ANALYST)
        self.assertEqual(snapshot(store), before)
        bound = self.restored("bound")
        protocol = Kernel._get(bound.events(), self.protocol, "protocol")["payload"]
        pending = snapshot(bound)
        with self.assertRaisesRegex(ValueError, "only through their frozen batch"):
            command(bound, "kernel.start_run", dict(
                protocol=self.protocol, seed=7, implementation=protocol["implementation"],
                environment=protocol["environment"], command=["python", "program.py"]),
                actor=Actor("manual-executor", "executor"))
        self.assertEqual(snapshot(bound), pending)
        result = self.advance(store)
        claim = Kernel._get(store.events(), result["claim"], "claim")
        store.append(id="claim-stray00000000000", kind="claim", actor=ANALYST.id, role="analyst",
                     payload=dict(claim["payload"], statement="A stray claim outside pack.analyse."),
                     expected_revision=len(store.events()))
        with self.assertRaisesRegex(ValueError, "must come from pack.analyse"):
            ResearchGraph.from_store(store)

    def test_noncanonical_reviewer_variants_are_rejected(self):
        # Audit finding A-22: whitespace, case and homoglyph variants of the analyst ID.
        for variant in ("pack-analyst ", "PACK-ANALYST", "p\u0430ck-analyst"):
            store = self.restored("completed")
            before = snapshot(store)
            with self.subTest(reviewer=variant), self.assertRaisesRegex(ValueError, "canonical"):
                advance_pack_analysis(store, self.batch, planner=PLANNER, analyst=ANALYST,
                                      reviewer_actor=variant)
            with self.subTest(reviewer=variant, path="command"), self.assertRaisesRegex(ValueError, "canonical"):
                self.analyse(store, dict(self.honest(store), reviewer_actor=variant))
            self.assertEqual(snapshot(store), before)

    def test_noncanonical_reviewer_variants_are_rejected(self):
        # Audit finding A-22: whitespace, case and homoglyph variants of the analyst ID.
        for variant in ("pack-analyst ", "PACK-ANALYST", "p\u0430ck-analyst"):
            store = self.restored("completed")
            before = snapshot(store)
            with self.subTest(reviewer=variant), self.assertRaisesRegex(ValueError, "canonical"):
                advance_pack_analysis(store, self.batch, planner=PLANNER, analyst=ANALYST,
                                      reviewer_actor=variant)
            with self.subTest(reviewer=variant, path="command"), self.assertRaisesRegex(ValueError, "canonical"):
                self.analyse(store, dict(self.honest(store), reviewer_actor=variant))
            self.assertEqual(snapshot(store), before)

    def test_swallowed_refused_reads_still_fail_the_hooks(self):
        store = self.restored("completed")
        _, context, cas = domain_packs.analysis_inputs(store, store.events(), self.batch)
        honest = registry.load_pack("synthetic_causal_v1")

        class Swallowing:
            pack_id = "synthetic_causal_v1"

            @staticmethod
            def hook(name):
                def call(*args):
                    if name == "validate_outputs":
                        try:
                            args[1].read(context.protocol["data"])
                        except ValueError:
                            pass
                    return honest.hook(name)(*args)
                return call

        with self.assertRaisesRegex(ValueError, "outside the allowlist"):
            domain_packs.run_hooks(Swallowing(), context, cas)
        self.assertEqual(cas.refused, (context.protocol["data"],))
        self.assertEqual(set(api.ProtocolContext.__dataclass_fields__),
                         {"parameters", "draft", "execution_plan", "capture", "protocol",
                          "protocol_hash"})

    def test_pack_drift_wrong_pin_and_unregistered_pack_are_rejected(self):
        store = self.restored("bound")
        explanation_set = Kernel._get(store.events(), self.protocol, "protocol")["payload"]["planning"]["explanation_set"]
        before = snapshot(store)
        for change, message in ((dict(pack_version="2"), "identity"),
                                (dict(pack_code_digest="0" * 64), "differs from the pinned"),
                                (dict(pack_id="unregistered_pack_v1"), "explicit registry"),
                                (dict(parameters=dict(PARAMETERS, n_samples=8)), "minimum"),
                                (dict(host_inputs=dict(HOST, world={"treatment_effect": 1})),
                                 "world requires")):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                command(store, "pack.preregister", preregistration(store, explanation_set, **change))
            self.assertEqual(snapshot(store), before)
        selection = Search(store, PLANNER).select_next(self.tree)["id"]
        before = snapshot(store)
        with drifted_pack() as loaded:
            with self.assertRaisesRegex(ValueError, "differs from the pinned"):
                batch_plan(store, self.binding(store), selection)
            self.assertEqual(snapshot(store), before)
            completed = self.restored("completed")
            pending = snapshot(completed)
            with self.assertRaisesRegex(ValueError, "differs from the pinned"):
                self.advance(completed)
            self.assertEqual(snapshot(completed), pending)
            self.assertNotEqual(loaded.pack_code_digest, self.binding(store)["payload"]["pack_code_digest"])

    def test_tampered_raw_output_and_hidden_input_are_refused(self):
        store = self.restored("completed")
        _, context, cas = domain_packs.analysis_inputs(store, store.events(), self.batch)
        data = context.protocol["data"]
        with self.assertRaisesRegex(api.PackAccessError, "hidden input"):
            cas.read(data)
        raw = context.slot("primary:7")["outputs"]["raw_data"]
        path = store.blobs / raw
        original = path.read_bytes()
        path.write_bytes(original.replace(b'"seed":7', b'"seed":8'))
        before = snapshot(store)
        try:
            with self.assertRaises(IntegrityError):
                self.advance(store)
            self.assertEqual(snapshot(store), before)
        finally:
            path.write_bytes(original)

    def test_legacy_paths_and_pack_paths_do_not_cross(self):
        store = self.restored("bound")
        before = snapshot(store)
        binding = self.binding(store)
        with self.assertRaisesRegex(ValueError, "pack binding"):
            command(store, "domain.bind", dict(
                protocol=self.protocol, adapter_id="synthetic_causal_v1", adapter_version="1",
                adapter_source_digest=store.put(b"legacy adapter source"),
                recipe=dict(schema_version=1, domain="synthetic_causal_v1"), recipe_artifacts=[],
                **domain_packs.execution_fields(store, binding)))
        self.assertEqual(snapshot(store), before)
        completed = self.restored("completed")
        pending = snapshot(completed)
        with self.assertRaises(GateError):
            advance_batch_analysis(completed, self.batch, planner=PLANNER, analyst=ANALYST,
                                   reviewer_actor=REVIEWER, adapter=SyntheticCausalBatchAnalysisAdapter())
        self.assertEqual(snapshot(completed), pending)
        with TemporaryDirectory(prefix="episteme-pack-legacy-") as temporary:
            legacy = golden_support.load_history(golden_support.read_json("manual_binding.json"),
                                                 Path(temporary) / "state")
            try:
                batch = next(row for row in legacy.events() if row["kind"] == "batch_plan")["id"]
                with self.assertRaisesRegex(ValueError, "requires a pack binding"):
                    domain_packs.analysis_inputs(legacy, legacy.events(), batch)
            finally:
                legacy.close()

    def test_forged_pack_events_and_corrupt_pack_code_fail_closed(self):
        store = self.restored("bound")
        binding = self.binding(store)
        key = next(row["sha256"] for row in json.loads(store.read(binding["payload"]["pack_code_digest"]))["files"])
        path = store.blobs / key
        original = path.read_bytes()
        path.write_bytes(original + b"# corrupt\n")
        try:
            with self.assertRaises(IntegrityError):
                ResearchGraph.from_store(store)
        finally:
            path.write_bytes(original)
        forged = dict(binding["payload"], protocol_validation="passed")
        store.append(id="pack_binding-forged0000000000", kind="pack_binding", actor=PLANNER.id,
                     role="planner", payload=forged, expected_revision=len(store.events()))
        with self.assertRaisesRegex(ValueError, "original command receipt"):
            ResearchGraph.from_store(store)


class PackCommandCliTests(unittest.TestCase):
    """The real CLI admits and replays pack.preregister and projects it in Graph."""

    def test_cli_command_replay_and_graph(self):
        import os
        import subprocess
        with TemporaryDirectory(prefix="episteme-pack-cli-") as temporary:
            root = Path(temporary) / "state"
            with Store(root) as store:
                explanation_set = planning(store)
                payload = preregistration(store, explanation_set)
                revision = len(store.events())
            envelope = dict(context=dict(command_id="pack-cli-fixture", expected_revision=revision,
                                         actor=PLANNER.id, role=PLANNER.role, study_id=STUDY,
                                         correlation_id="pack-cli", causation_id=None),
                            request=dict(version=1, action="pack.preregister", payload=payload))
            path = Path(temporary) / "command.json"
            path.write_text(json.dumps(envelope), encoding="utf-8")
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
            call = [sys.executable, "-m", "episteme"]
            first = subprocess.run([*call, "command", "--input", str(path), "--root", str(root)],
                                   capture_output=True, text=True, env=env, check=True, timeout=300)
            replay = subprocess.run([*call, "command", "--input", str(path), "--root", str(root)],
                                    capture_output=True, text=True, env=env, check=True, timeout=300)
            self.assertEqual(json.loads(first.stdout)["result"], json.loads(replay.stdout)["result"])
            graph = subprocess.run([*call, "graph", "--root", str(root)], capture_output=True,
                                   text=True, env=env, check=True, timeout=300)
            kinds = json.loads(graph.stdout)["node_kinds"]
            self.assertEqual((kinds["pack_binding"], kinds["protocol"]), (1, 1))
            with Store(root, read_only=True) as store:
                self.assertEqual([event["kind"] for event in store.events()].count("pack_binding"), 1)
                self.assertEqual(len([row for row in store.receipts()
                                      if row["request"]["action"] == "pack.preregister"]), 1)


class DescriptiveCeilingTests(unittest.TestCase):
    """The test-only fixture pack exercises the descriptive branch of the ceiling."""

    def setUp(self):
        patcher = patch.dict(registry.PACKS, {pack_fixtures.FIXTURE_PACK: pack_fixtures.FIXTURE_PACK})
        patcher.start()
        self.addCleanup(patcher.stop)
        temporary = TemporaryDirectory(prefix="episteme-pack-descriptive-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def completed(self, name: str, outcome: str, inference_mode: str) -> tuple[Store, str]:
        """An honest fixture pack whose pinned parameters ask for this proposal."""
        store = Store(self.root / name)
        self.addCleanup(store.close)
        result = command(store, "pack.preregister", preregistration(
            store, planning(store), pack_id=pack_fixtures.FIXTURE_PACK,
            parameters={"scale": 3, "outcome": outcome, "inference_mode": inference_mode},
            host={"groups": [[1, 2, 4]]}))
        binding = Kernel._get(store.events(), result["binding"], "pack_binding")
        batch = plan_batch(store, binding, search_node(store, result["protocol"], 2))
        self.assertEqual(advance_batch(store, batch)["status"], "completed")
        return store, batch

    def test_descriptive_design_allows_supports_only_in_descriptive_mode(self):
        store, batch = self.completed("above", "supports", "exploratory")
        before = snapshot(store)
        with self.assertRaisesRegex(ValueError, "exceeds the ceiling descriptive"):
            advance_pack_analysis(store, batch, planner=PLANNER, analyst=ANALYST,
                                  reviewer_actor=REVIEWER)
        self.assertEqual(snapshot(store), before)
        store, batch = self.completed("within", "supports", "descriptive")
        result = advance_pack_analysis(store, batch, planner=PLANNER, analyst=ANALYST,
                                       reviewer_actor=REVIEWER)
        self.assertEqual(result["status"], "awaiting_review")
        claim = Kernel._get(store.events(), result["claim"], "claim")["payload"]
        self.assertEqual((claim["outcome"], claim["inference_mode"]), ("supports", "descriptive"))
        ceiling = Kernel._get(store.events(), result["analysis"], "pack_analysis")["payload"]["ceiling"]
        self.assertEqual(ceiling["max_inference_mode"], "descriptive")
        self.assertEqual(ceiling["reasons"], [domain_packs.REASON_DESCRIPTIVE])


class drifted_pack:
    """Register an edited copy of the synthetic pack under the same pack ID."""

    def __enter__(self) -> registry.LoadedPack:
        self.temporary = TemporaryDirectory(prefix="episteme-pack-drift-")
        parent = Path(self.temporary.name)
        source = registry.load_pack("synthetic_causal_v1").root
        target = parent / "synthetic_causal_v1"
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        with (target / "__init__.py").open("ab") as stream:
            stream.write(b"\n# Edited after binding; same pack ID and version.\n")
        sys.path.insert(0, str(parent))
        importlib.invalidate_caches()
        self.patcher = patch.dict(registry.PACKS, {"synthetic_causal_v1": "synthetic_causal_v1"})
        self.patcher.start()
        return registry.load_pack("synthetic_causal_v1")

    def __exit__(self, *args):
        self.patcher.stop()
        sys.path.remove(str(Path(self.temporary.name)))
        importlib.invalidate_caches()
        registry._LOADED.pop("synthetic_causal_v1", None)
        self.temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
