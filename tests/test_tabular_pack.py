"""Confirmatory ceiling path for ``tabular_classification_v1``.

The tables are generated mechanism-test fixtures. Reviewer assignment is a
mechanical step. These tests do not record a reviewer verdict or a paper, and
they do not treat the ceiling as a scientific result.
"""

from __future__ import annotations

import copy
import importlib.util
import inspect
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from episteme import domain_packs
from episteme.analysis_controller import advance_pack_analysis
from episteme.batch_controller import advance_batch
from episteme.commands import CommandService
from episteme.domains import api, registry
from episteme.execution import freeze_environment
from episteme.kernel import Actor, Kernel, lenient_digest
from episteme.planning import Planning
from episteme.recovery import backup, restore
from episteme.review_assignment import _index as assignment_index
from episteme.search import COMPONENTS, Search
from episteme.store import Store, canonical, digest


STUDY = "tabular-classification-fixture"
PLANNER = Actor("tabular-planner", "planner")
ANALYST = Actor("tabular-analyst", "analyst")
REVIEWER = "tabular-reviewer"
PACK = "tabular_classification_v1"
ROOT = Path(__file__).resolve().parents[1] / "examples" / "tabular_classification_v1"


def command(store, action, payload, *, actor=PLANNER):
    return CommandService(store).execute(dict(
        context=dict(command_id=uuid4().hex, expected_revision=len(store.events()),
                     actor=actor.id, role=actor.role, study_id=STUDY,
                     correlation_id="tabular-workflow", causation_id=None),
        request=dict(version=1, action=action, payload=copy.deepcopy(payload))))


def generator():
    path = ROOT / "generate.py"
    spec = importlib.util.spec_from_file_location("tabular_generate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def materialize(kind, directory):
    directory.mkdir(parents=True)
    for name in ("train.csv", "holdout.csv"):
        shutil.copyfile(ROOT / kind / name, directory / name)
    return directory


def bind(store, kind):
    temporary = TemporaryDirectory(prefix="episteme-tabular-source-")
    try:
        return _bind(store, kind, Path(temporary.name))
    finally:
        temporary.cleanup()


def _bind(store, kind, root):
    source = materialize(kind, root / "source")
    captured = domain_packs.store_capture(store, PACK, source)
    scope = {"population": "generated mechanism-test table, not a scientific population"}
    plan = Planning(store, PLANNER)
    question = plan.question(
        study_id=STUDY, statement="Does the preregistered holdout comparison clear its own rule?",
        objective="Exercise the confirmatory claim-strength ceiling on a generated table",
        scope=scope, constraints=["Captured CSV fixtures only"],
        stopping_criteria=["One primary run and one same-data reanalysis"])
    kernel = Kernel(store, PLANNER)
    hypotheses = [kernel.hypothesis(text, prediction, falsifier, scope) for text, prediction, falsifier in (
        ("No accuracy difference", "The interval does not clear the rule", "The rule records supports or refutes"),
        ("A positive accuracy difference", "The rule records supports", "The rule records inconclusive or refutes"))]
    explanation = plan.explanation_set(question=question, hypotheses=hypotheses,
                                       comparison_plan="Apply the preregistered paired comparison")
    loaded = registry.load_pack(PACK)
    registered = command(store, "pack.preregister", dict(
        explanation_set=explanation, pack_id=PACK, pack_version=loaded.pack_version,
        pack_code_digest=loaded.pack_code_digest, parameters={}, host_inputs={},
        capture=captured["capture"], environment=freeze_environment(store)))
    return registered, captured


def run_batch(store, protocol):
    search = Search(store, PLANNER)
    tree = search.register_tree(weights={key: 1 for key in COMPONENTS}, cost_weight=0, budget=2,
                                cost_unit="enqueued_attempt", max_nodes=1, max_depth=0,
                                max_width=1, max_selections=1, max_retries=0)
    search.add_node(tree, protocol=protocol, action="discriminate", estimated_cost=2,
                    components=dict(discrimination=1, uncertainty=1, coverage=1, invalidity_risk=0),
                    rationale="Exercise the pinned tabular recipe")
    binding = domain_packs.pack_bindings(store, store.events())[protocol]
    batch = command(store, "batch.plan", dict(
        selection=search.select_next(tree)["id"], executor="tabular-executor",
        replicator="tabular-reanalyst", **domain_packs.execution_fields(store, binding)))
    assert advance_batch(store, batch)["status"] == "completed"
    return batch


class TabularPackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temporary = TemporaryDirectory(prefix="episteme-tabular-pack-")
        cls.base = Path(cls._temporary.name)
        with Store(cls.base / "origin") as store:
            registered, captured = bind(store, "planted")
            cls.protocol, cls.binding_id = registered["protocol"], registered["binding"]
            cls.capture = captured
            cls.batch = run_batch(store, cls.protocol)
            backup(store, cls.base / "completed")

    @classmethod
    def tearDownClass(cls):
        cls._temporary.cleanup()

    def restored(self):
        temporary = TemporaryDirectory(prefix="episteme-tabular-copy-")
        self.addCleanup(temporary.cleanup)
        target = Path(temporary.name) / "state"
        restore(self.base / "completed", target)
        store = Store(target)
        self.addCleanup(store.close)
        return store

    def honest(self, store):
        loaded = registry.load_pack(PACK)
        _, context, cas = domain_packs.analysis_inputs(store, store.events(), self.batch)
        hooks = domain_packs.run_hooks(loaded, context, cas)
        settlement = domain_packs._batch_state(store, store.events(), self.batch, None)["state"]["settlement"]
        return dict(batch=self.batch, expected_settlement=settlement["id"], pack_id=PACK,
                    pack_version=loaded.pack_version, pack_code_digest=loaded.pack_code_digest,
                    checks=hooks["checks"], recomputations=hooks["recomputations"],
                    report=hooks["report"], statistical_report=hooks["statistical_report"],
                    reviewer_actor=REVIEWER)

    def test_committed_tables_match_the_generator_and_are_distinct_files(self):
        produced = generator().tables()
        for kind in ("planted", "null"):
            for split in ("train", "holdout"):
                self.assertEqual((ROOT / kind / f"{split}.csv").read_bytes(), produced[(kind, split)])
        planted_train = produced[("planted", "train")]
        planted_holdout = produced[("planted", "holdout")]
        self.assertNotEqual(planted_train, planted_holdout)
        self.assertNotEqual(lenient_digest(planted_train), lenient_digest(planted_holdout))
        self.assertNotEqual(produced[("null", "train")], produced[("null", "holdout")])

    def test_flipping_holdout_labels_does_not_change_the_frozen_rule(self):
        loaded = registry.load_pack(PACK)
        temporary = TemporaryDirectory(prefix="episteme-tabular-flip-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)

        def compiled(holdout):
            source = root / digest(holdout)[:16]
            source.mkdir()
            (source / "train.csv").write_bytes((ROOT / "planted" / "train.csv").read_bytes())
            (source / "holdout.csv").write_bytes(holdout)
            request = api.CompileRequest(parameters={}, host_inputs={},
                                         capture=loaded.hook("capture")(source))
            return (loaded.hook("compile_protocol")(request),
                    loaded.hook("compile_execution")(request))

        original = (ROOT / "planted" / "holdout.csv").read_bytes()
        flipped_lines = []
        for index, line in enumerate(original.decode("utf-8").splitlines()):
            if index == 0:
                flipped_lines.append(line)
                continue
            left, right, label = line.split(",")
            flipped_lines.append(f"{left},{right},{1 - int(label)}")
        flipped = ("\n".join(flipped_lines) + "\n").encode("utf-8")
        first_draft, first_plan = compiled(original)
        second_draft, second_plan = compiled(flipped)
        self.assertEqual(first_plan.primary_program, second_plan.primary_program)
        self.assertEqual(first_plan.reanalysis_program, second_plan.reanalysis_program)
        self.assertEqual(first_draft.design, second_draft.design)
        self.assertEqual(first_draft.analysis_plan, second_draft.analysis_plan)
        self.assertEqual(first_draft.stopping_rule, second_draft.stopping_rule)
        self.assertEqual(first_draft.seen_data, second_draft.seen_data)
        first_design, second_design = first_draft.typed_design(), second_draft.typed_design()
        self.assertEqual(first_design.estimand, second_design.estimand)
        self.assertEqual(first_design.sample_size, second_design.sample_size)
        self.assertEqual(first_design.uncertainty, second_design.uncertainty)
        self.assertNotEqual(first_plan.input, second_plan.input)
        self.assertNotEqual(first_design.data_splits[1].digest, second_design.data_splits[1].digest)
        self.assertNotIn(first_design.data_splits[1].digest, first_draft.seen_data)
        self.assertNotEqual(digest(first_plan.input), first_design.data_splits[1].digest)
        self.assertNotEqual(digest(first_plan.input), first_design.data_splits[0].digest)

    def test_hooks_do_not_accept_a_store(self):
        loaded = registry.load_pack(PACK)
        for name in ("describe", "validate_parameters", "compile_protocol", "compile_execution",
                     "validate_protocol", "capture", "validate_outputs", "recompute_metrics", "analyse"):
            self.assertNotIn("store", inspect.signature(loaded.hook(name)).parameters)

    def test_confirmatory_support_is_admitted_through_review_assignment(self):
        store = self.restored()
        result = advance_pack_analysis(store, self.batch, planner=PLANNER, analyst=ANALYST,
                                       reviewer_actor=REVIEWER)
        self.assertEqual(result["scientific_validity"], "not_assessed")
        self.assertEqual(result["status"], "awaiting_review")
        history = store.events()
        claim = Kernel._get(history, result["claim"], "claim")["payload"]
        analysis = Kernel._get(history, result["analysis"], "pack_analysis")["payload"]
        self.assertEqual((claim["outcome"], claim["inference_mode"]), ("supports", "confirmatory"))
        self.assertEqual(analysis["ceiling"]["max_inference_mode"], "confirmatory")
        self.assertEqual(analysis["ceiling"]["allowed_outcomes"], ["supports", "refutes", "inconclusive"])
        self.assertEqual(analysis["ceiling"]["not_supplied"], [])
        self.assertEqual(analysis["ceiling"]["reasons"], [])
        self.assertEqual(analysis["report_origin"], "recomputed_by_pinned_pack_at_admission")
        self.assertEqual(analysis["replication"]["roster_repetition"], "single_deterministic_unit")
        self.assertEqual(analysis["replication"]["independence"]["context"], "not_established")
        protocol = Kernel._get(history, self.protocol, "protocol")["payload"]
        splits = {row["id"]: row for row in protocol["statistical_design"]["data_splits"]}
        self.assertNotIn(splits["holdout"]["digest"], protocol["seen_data"])
        self.assertIn(splits["training"]["digest"], protocol["seen_data"])
        self.assertNotEqual(protocol["data"], splits["holdout"]["digest"])
        self.assertTrue(Kernel(store, ANALYST).gate(result["claim"])["passed"])
        report = json.loads(store.read(analysis["statistical_report"]))
        self.assertTrue(all(report[name]["status"] == "supplied" for name in api.STATISTICAL_FIELDS))
        self.assertEqual(report["confidence_interval"]["value"]["lower"] > 0, True)
        self.assertIn("not a population effect", claim["statement"])
        self.assertNotIn("discovery", claim["statement"].lower())
        assignment = assignment_index(store, history)[result["assignment"]]
        manifest = json.loads(store.read(assignment["payload"]["bundle"]))
        self.assertEqual(manifest["policy"], "blind_initial_review_v2")
        self.assertFalse(any(event["kind"] in {"review", "paper"} for event in history))
        self.assertIn("Kernel claim-strength ceiling v1: inference at most confirmatory",
                      claim["limitations"][-1])

    def test_ceiling_still_rejects_a_confirmatory_proposal_without_the_interval(self):
        store = self.restored()
        honest = self.honest(store)
        before = (store.export(), store.export_receipts())

        def submit(edit):
            payload = copy.deepcopy(honest)
            edit(payload)
            payload["report"]["statistical_report"] = digest(canonical(payload["statistical_report"]))
            with self.assertRaisesRegex(ValueError, "exceeds the ceiling"):
                command(store, "pack.analyse", payload, actor=ANALYST)
            self.assertEqual((store.export(), store.export_receipts()), before)

        submit(lambda payload: payload["statistical_report"].__setitem__(
            "confidence_interval", api.not_supplied("Interval withheld.")))
        assumptions = copy.deepcopy(honest["statistical_report"]["assumptions"])
        assumptions["value"][0]["status"] = "violated"
        submit(lambda payload: payload["statistical_report"].__setitem__("assumptions", assumptions))

    def test_null_table_stays_inconclusive_under_the_same_rule(self):
        temporary = TemporaryDirectory(prefix="episteme-tabular-null-")
        self.addCleanup(temporary.cleanup)
        with Store(Path(temporary.name) / "state") as store:
            registered, _ = bind(store, "null")
            batch = run_batch(store, registered["protocol"])
            result = advance_pack_analysis(store, batch, planner=PLANNER, analyst=ANALYST,
                                           reviewer_actor=REVIEWER)
            claim = Kernel._get(store.events(), result["claim"], "claim")["payload"]
            analysis = Kernel._get(store.events(), result["analysis"], "pack_analysis")["payload"]
            self.assertEqual((claim["outcome"], claim["inference_mode"]), ("inconclusive", "confirmatory"))
            self.assertEqual(analysis["ceiling"]["max_inference_mode"], "confirmatory")
            self.assertIn("records inconclusive", claim["statement"])
            self.assertEqual(result["scientific_validity"], "not_assessed")
            self.assertFalse(any(event["kind"] in {"review", "paper"} for event in store.events()))

    def test_previously_exposed_or_reencoded_holdout_cannot_be_preregistered(self):
        holdout = (ROOT / "planted" / "holdout.csv").read_bytes()
        cases = {"exact": holdout, "trailing newline": holdout + b"\n"}
        for label, exposed in cases.items():
            with self.subTest(label=label):
                temporary = TemporaryDirectory(prefix="episteme-tabular-exposed-")
                self.addCleanup(temporary.cleanup)
                with Store(Path(temporary.name) / "state") as store:
                    scope = {"population": "generated mechanism-test table"}
                    plan = Planning(store, PLANNER)
                    question = plan.question(
                        study_id=STUDY, statement="Can an opened holdout be reused?",
                        objective="Check the confirmatory exposure tripwire", scope=scope,
                        constraints=["Fixture only"], stopping_criteria=["Stop before a run"])
                    kernel = Kernel(store, PLANNER)
                    hypotheses = [kernel.hypothesis(text, "No", "Yes", scope) for text in (
                        "Opened holdout", "Fresh holdout")]
                    explanation = plan.explanation_set(
                        question=question, hypotheses=hypotheses, comparison_plan="Do not reuse an opened file")
                    kernel.expose_data(data=store.put(exposed), purpose="opened before preregistration")
                    source = materialize("planted", Path(temporary.name) / "source")
                    captured = domain_packs.store_capture(store, PACK, source)
                    loaded = registry.load_pack(PACK)
                    with self.assertRaisesRegex(ValueError, "confirmatory split was already exposed"):
                        command(store, "pack.preregister", dict(
                            explanation_set=explanation, pack_id=PACK, pack_version=loaded.pack_version,
                            pack_code_digest=loaded.pack_code_digest, parameters={}, host_inputs={},
                            capture=captured["capture"], environment=freeze_environment(store)))


if __name__ == "__main__":
    unittest.main()
