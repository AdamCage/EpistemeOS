"""Universality acceptance for every registered DomainPack (ADR 0016 step 10).

The same kernel path preregisters, executes, analyses, assigns review, records
an explicit synthetic fixture review and builds an internal paper scaffold.
The fixture approval is not a scientific review: scientific_validity stays
not_assessed, and the manuscript says so. Backup, restore and tamper checks
use temporary copies. They never open .research/afterlife-pilot-20261004.
"""

from __future__ import annotations

import copy
import importlib
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

import pack_fixtures
from episteme import domain_packs
from episteme.analysis_controller import advance_pack_analysis
from episteme.batch_controller import advance_batch
from episteme.commands import CommandService
from episteme.domains import api, registry
from episteme.execution import freeze_environment
from episteme.graph import ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.recovery import backup, restore
from episteme.reporting import export_store
from episteme.store import IntegrityError, Store, canonical, digest
from review_paths import FIXTURE_RATIONALE, approve

from test_afterlife_pack import source_bytes
from test_pack_workflow import (
    ANALYST, PLANNER, REVIEWER, STUDY, planning, plan_batch, preregistration, search_node,
)
from test_tabular_pack import (
    ANALYST as TABULAR_ANALYST, PACK as TABULAR, PLANNER as TABULAR_PLANNER,
    REVIEWER as TABULAR_REVIEWER, STUDY as TABULAR_STUDY, bind, run_batch,
)


WRITER = Actor("fixture-writer", "writer")
PACKS = ("synthetic_causal_v1", "afterlife_seed_v1", "tabular_classification_v1")


def run_command(store: Store, study: str, actor: Actor, action: str, payload: dict) -> object:
    return CommandService(store).execute(dict(
        context=dict(command_id=uuid4().hex, expected_revision=len(store.events()),
                     actor=actor.id, role=actor.role, study_id=study,
                     correlation_id="pack-acceptance", causation_id=None),
        request=dict(version=1, action=action, payload=copy.deepcopy(payload))))


def snapshot(store: Store) -> tuple:
    return store.export(), store.export_receipts()


class drifted_pack:
    """Register an edited copy of one pack under the same pack ID."""

    def __init__(self, pack_id: str):
        self.pack_id = pack_id

    def __enter__(self) -> registry.LoadedPack:
        self.temporary = TemporaryDirectory(prefix="episteme-pack-acceptance-drift-")
        parent = Path(self.temporary.name)
        source = registry.load_pack(self.pack_id).root
        target = parent / self.pack_id
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        with (target / "__init__.py").open("ab") as stream:
            stream.write(b"\n# Edited after binding; same pack ID and version.\n")
        sys.path.insert(0, str(parent))
        importlib.invalidate_caches()
        self.patcher = patch.dict(registry.PACKS, {self.pack_id: self.pack_id})
        self.patcher.start()
        registry._LOADED.pop(self.pack_id, None)
        return registry.load_pack(self.pack_id)

    def __exit__(self, *args):
        self.patcher.stop()
        sys.path.remove(str(Path(self.temporary.name)))
        importlib.invalidate_caches()
        registry._LOADED.pop(self.pack_id, None)
        self.temporary.cleanup()


class PackAcceptanceTests(unittest.TestCase):
    """Completed batches for the three registered packs, restored per test."""

    @classmethod
    def setUpClass(cls):
        cls._temporary = TemporaryDirectory(prefix="episteme-pack-acceptance-")
        cls.base = Path(cls._temporary.name)
        cls.cases = {
            "synthetic_causal_v1": cls._synthetic(),
            "afterlife_seed_v1": cls._afterlife(),
            "tabular_classification_v1": cls._tabular(),
        }

    @classmethod
    def tearDownClass(cls):
        cls._temporary.cleanup()

    @classmethod
    def _finish(cls, name: str, store: Store, registered: dict, batch: str, **fields) -> dict:
        assert advance_batch(store, batch)["status"] == "completed"
        backup(store, cls.base / name)
        return dict(pack=name, backup=cls.base / name, batch=batch,
                    protocol=registered["protocol"], binding=registered["binding"], **fields)

    @classmethod
    def _synthetic(cls) -> dict:
        with Store(cls.base / "synthetic-origin") as store:
            registered = run_command(store, STUDY, PLANNER, "pack.preregister", preregistration(
                store, planning(store)))
            binding = Kernel._get(store.events(), registered["binding"], "pack_binding")
            batch = plan_batch(store, binding, search_node(store, registered["protocol"], 2))
            return cls._finish("synthetic_causal_v1", store, registered, batch,
                               study=STUDY, planner=PLANNER, analyst=ANALYST, reviewer=REVIEWER,
                               semantics="rng_seed", hidden=("protocol_data",), above="outcome")

    @classmethod
    def _afterlife(cls) -> dict:
        with Store(cls.base / "afterlife-origin") as store:
            source = pack_fixtures.historical_run(cls.base / "afterlife-source")
            before = source_bytes(source)
            captured = domain_packs.store_capture(store, "afterlife_seed_v1", source)
            assert source_bytes(source) == before
            loaded = registry.load_pack("afterlife_seed_v1")
            registered = run_command(store, STUDY, PLANNER, "pack.preregister", dict(
                explanation_set=planning(store), pack_id="afterlife_seed_v1",
                pack_version=loaded.pack_version, pack_code_digest=loaded.pack_code_digest,
                parameters={}, host_inputs={}, capture=captured["capture"],
                environment=freeze_environment(store)))
            binding = Kernel._get(store.events(), registered["binding"], "pack_binding")
            batch = plan_batch(store, binding, search_node(store, registered["protocol"], 18))
            return cls._finish("afterlife_seed_v1", store, registered, batch,
                               study=STUDY, planner=PLANNER, analyst=ANALYST, reviewer=REVIEWER,
                               semantics="frozen_unit_index", hidden=(), above="outcome")

    @classmethod
    def _tabular(cls) -> dict:
        with Store(cls.base / "tabular-origin") as store:
            registered, _captured = bind(store, "planted")
            batch = run_batch(store, registered["protocol"])
            return cls._finish(TABULAR, store, registered, batch,
                               study=TABULAR_STUDY, planner=TABULAR_PLANNER, analyst=TABULAR_ANALYST,
                               reviewer=TABULAR_REVIEWER, semantics="deterministic_single",
                               hidden=(), above="assumption")

    def restored(self, pack: str) -> Store:
        temporary = TemporaryDirectory(prefix="episteme-pack-acceptance-copy-")
        self.addCleanup(temporary.cleanup)
        target = Path(temporary.name) / "state"
        restore(self.cases[pack]["backup"], target)
        store = Store(target)
        self.addCleanup(store.close)
        return store

    def case(self, pack: str) -> dict:
        return self.cases[pack]

    def advance(self, store: Store, pack: str) -> dict:
        case = self.case(pack)
        return advance_pack_analysis(store, case["batch"], planner=case["planner"],
                                     analyst=case["analyst"], reviewer_actor=case["reviewer"])

    def honest(self, store: Store, pack: str) -> dict:
        case = self.case(pack)
        loaded = registry.load_pack(pack)
        _facts, _context, _cas = domain_packs.analysis_inputs(store, store.events(), case["batch"])
        hooks = domain_packs.run_hooks(loaded, _context, _cas)
        settlement = domain_packs._batch_state(store, store.events(), case["batch"], None)["state"]["settlement"]
        return dict(batch=case["batch"], expected_settlement=settlement["id"], pack_id=pack,
                    pack_version=loaded.pack_version, pack_code_digest=loaded.pack_code_digest,
                    checks=hooks["checks"], recomputations=hooks["recomputations"],
                    report=hooks["report"], statistical_report=hooks["statistical_report"],
                    reviewer_actor=case["reviewer"])

    def analyse(self, store: Store, pack: str, payload: dict) -> dict:
        case = self.case(pack)
        return run_command(store, case["study"], case["analyst"], "pack.analyse", payload)

    def test_registered_packs_are_exactly_the_acceptance_set(self):
        self.assertEqual(tuple(registry.PACKS), PACKS)
        self.assertNotIn("locked_fixture_v2", registry.PACKS)

    def test_fixture_review_builds_an_internal_scaffold_without_scientific_approval(self):
        semantics = set()
        for pack in PACKS:
            with self.subTest(pack=pack):
                case = self.case(pack)
                store = self.restored(pack)
                result = self.advance(store, pack)
                review = approve(store, result["claim"], reviewer=case["reviewer"])
                decision = Kernel(store, Actor("fixture-auditor", "observer")).next_action(result["claim"])
                self.assertEqual(decision["action"], "paper_candidate")
                self.assertIn("human release", decision["limitation"])
                paper = run_command(store, case["study"], WRITER, "paper.build", dict(
                    title="Internal fixture scaffold; no scientific review",
                    claims=[result["claim"]], expected_bases={result["claim"]: decision["basis_hash"]}))
                history = store.events()
                analysis = Kernel._get(history, result["analysis"], "pack_analysis")["payload"]
                claim = Kernel._get(history, result["claim"], "claim")["payload"]
                manuscript = store.read(Kernel._get(history, paper, "paper")["payload"]["manuscript"]).decode()
                self.assertEqual(analysis["scientific_validity"], "not_assessed")
                self.assertEqual(analysis["replication"]["mode"], "same_data_reanalysis")
                self.assertEqual(analysis["replication"]["independence"]["context"], "not_established")
                self.assertTrue(all(event["payload"].get("scientific_validity", "not_assessed") == "not_assessed"
                                    for event in history))
                self.assertIn(FIXTURE_RATIONALE, manuscript)
                self.assertIn("not a submission-ready paper", manuscript)
                self.assertIn("not a scientific assessment", manuscript)
                self.assertIn(f"roster_semantics: `{case['semantics']}`", manuscript)
                self.assertIn(f"Roster column label is pinned roster_semantics `{case['semantics']}`.", manuscript)
                self.assertIn("same_data_reanalysis", manuscript)
                self.assertNotIn("scientifically valid", manuscript.lower())
                self.assertNotIn(review, claim["statement"])
                semantics.add(case["semantics"])
                graph = ResearchGraph.from_store(store)
                analysis_closure = {node.id for node in graph.ancestors(result["analysis"])}
                binding_event = Kernel._get(history, case["binding"], "pack_binding")
                binding = binding_event["payload"]
                plan = api.strict_loads(store.read(binding["execution_plan"]), "execution plan")
                self.assertIn(case["binding"], analysis_closure)
                for key in (analysis["report"], analysis["statistical_report"]):
                    self.assertIn(ResearchGraph.artifact_id(key), analysis_closure, key)
                recipe_closure = {node.id for node in graph.ancestors(case["binding"])}
                for key in (analysis["pack_code_digest"], binding["execution_plan"],
                            plan["primary_program"]["sha256"], plan["reanalysis_program"]["sha256"]):
                    self.assertIn(ResearchGraph.artifact_id(key), recipe_closure, key)
                for data in registry.load_pack(pack).files.values():
                    self.assertIn(ResearchGraph.artifact_id(digest(data)), recipe_closure)
                export_store(store)
                report = (store.root / "report.md").read_text(encoding="utf-8")
                self.assertIn(f"roster_semantics `{case['semantics']}`", report)
                self.assertIn("replication mode `same_data_reanalysis`", report)
                self.assertIn("context `not_established`", report)
                self.assertIn("Scientific validity: **not_assessed**.", report)
                missing = analysis["ceiling"]["not_supplied"]
                if missing:
                    for name in missing:
                        self.assertIn(f"`{name}`", report)
                else:
                    self.assertIn("Statistical report not_supplied: none.", report)
                recorded = len(history)
                for receipt in list(store.receipts()):
                    CommandService(store).execute(dict(context=receipt["context"], request=receipt["request"]))
                self.assertEqual(len(store.events()), recorded)
                folder = Path(store.root).parent
                backup(store, folder / "snapshot")
                restore(folder / "snapshot", folder / "restored")
                with Store(folder / "restored", read_only=True) as copy:
                    self.assertEqual(copy.export(), store.export())
                    self.assertEqual(ResearchGraph.from_store(copy).to_json(), graph.to_json())
        self.assertEqual(semantics, {"rng_seed", "frozen_unit_index", "deterministic_single"})

    def test_controller_restart_between_analysis_and_assignment_does_not_duplicate(self):
        original = CommandService.execute
        for pack in PACKS:
            with self.subTest(pack=pack):
                store = self.restored(pack)

                def interrupted(service, envelope, _original=original):
                    if envelope["request"]["action"] == "review.assign":
                        raise RuntimeError("controller stopped after the analysis receipt")
                    return _original(service, envelope)

                with patch.object(CommandService, "execute", interrupted):
                    with self.assertRaisesRegex(RuntimeError, "controller stopped"):
                        self.advance(store, pack)
                kinds = [event["kind"] for event in store.events()]
                self.assertEqual((kinds.count("claim"), kinds.count("pack_analysis"),
                                  kinds.count("review_assignment")), (1, 1, 0))
                result = self.advance(store, pack)
                kinds = [event["kind"] for event in store.events()]
                self.assertEqual((kinds.count("claim"), kinds.count("pack_analysis"),
                                  kinds.count("review_assignment")), (1, 1, 1))
                recorded = snapshot(store)
                self.assertEqual(self.advance(store, pack), result)
                self.assertEqual(snapshot(store), recorded)
                self.assertFalse(any(event["kind"] in {"review", "paper"} for event in store.events()))

    def test_tamper_suite_rejects_the_same_classes_for_every_registered_pack(self):
        for pack in PACKS:
            with self.subTest(pack=pack):
                case = self.case(pack)
                store = self.restored(pack)
                honest = self.honest(store, pack)
                before = snapshot(store)

                def changed(edit):
                    payload = copy.deepcopy(honest)
                    edit(payload)
                    return payload

                def retie(payload):
                    payload["report"]["statistical_report"] = digest(canonical(payload["statistical_report"]))

                def above(payload):
                    if case["above"] == "outcome":
                        payload["report"]["outcome"] = "supports"
                        return
                    row = payload["statistical_report"]["assumptions"]["value"][0]
                    row["status"] = "violated"
                    retie(payload)

                cases = {
                    "deleted field": (changed(lambda payload: (
                        payload["statistical_report"].pop("assumptions"), retie(payload))),
                        "lacks required fields: assumptions"),
                    "not_applicable contrary to protocol": (changed(lambda payload: (
                        payload["statistical_report"].__setitem__(
                            "effect_size", api.not_applicable("Contrary to the preregistered design.")),
                        retie(payload))), "contrary"),
                    "proposal above the ceiling": (changed(above), "exceeds the ceiling"),
                    "substituted version": (changed(lambda payload: payload.__setitem__("pack_version", "2")),
                                            "differs from the pinned binding"),
                }
                for label, (payload, message) in cases.items():
                    with self.subTest(pack=pack, case=label):
                        with self.assertRaisesRegex(ValueError, message):
                            self.analyse(store, pack, payload)
                        self.assertEqual(snapshot(store), before)
                _facts, context, cas = domain_packs.analysis_inputs(store, store.events(), case["batch"])
                if case["hidden"]:
                    with self.assertRaisesRegex(api.PackAccessError, "hidden input"):
                        cas.read(context.protocol["data"])
                else:
                    self.assertEqual(self.binding_hidden(store, pack), [])
                    with self.assertRaisesRegex(api.PackAccessError, "outside the pack allowlist"):
                        cas.read("0" * 64)
                with drifted_pack(pack) as loaded:
                    with self.assertRaisesRegex(ValueError, "differs from the pinned"):
                        self.advance(store, pack)
                    self.assertEqual(snapshot(store), before)
                    self.assertNotEqual(loaded.pack_code_digest,
                                        Kernel._get(store.events(), case["binding"], "pack_binding")
                                        ["payload"]["pack_code_digest"])
                raw = context.slot("primary:" + str(context.protocol["seeds"][0]))["outputs"]["raw_data"]
                path = store.blobs / raw
                original = path.read_bytes()
                path.write_bytes(original[:-1] + bytes([original[-1] ^ 0x01]))
                try:
                    with self.assertRaises(IntegrityError):
                        self.advance(store, pack)
                    self.assertEqual(snapshot(store), before)
                finally:
                    path.write_bytes(original)

    def binding_hidden(self, store: Store, pack: str) -> list:
        return Kernel._get(store.events(), self.case(pack)["binding"], "pack_binding")["payload"]["hidden_inputs"]


if __name__ == "__main__":
    unittest.main()
