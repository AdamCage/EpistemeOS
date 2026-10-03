"""The synthetic_causal_v1 pack facade keeps the legacy computations unchanged.

Compilation is compared with the legacy compiler on a parameter grid, and the
pack hooks are run over the saved golden histories (legacy bindings) and
compared with the legacy adapter's frozen proposal. All data are synthetic
fixtures; equality is mechanical, not a scientific review.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import golden_support
from episteme.batch import _index as batch_index
from episteme.domains import api, registry
from episteme.domains import synthetic_causal as legacy
from episteme.kernel import Kernel
from episteme.store import canonical, digest


PACK = "synthetic_causal_v1"
WORLD = dict(treatment_effect=2.0, confounding_strength=0.5, noise_std=1.0)


def legacy_context(store, batch: str, loaded) -> tuple[api.AnalysisContext, api.CasView, str]:
    """Test-only: an AnalysisContext for a legacy-bound batch, as the kernel builds it."""
    history = store.events()
    state = batch_index(store, history)[batch]
    plan = state["plan"]["payload"]
    protocol = Kernel._get(history, plan["protocol"], "protocol")
    hidden_world = json.loads(store.read(protocol["payload"]["data"]))
    request = api.CompileRequest(
        parameters=hidden_world["parameters"],
        host_inputs=dict(world=hidden_world["world"], seeds=protocol["payload"]["seeds"],
                         replication_tolerance=protocol["payload"]["replication_tolerance"]),
        capture=None)
    draft = loaded.hook("compile_protocol")(request)
    execution = loaded.hook("compile_execution")(request)
    slots = []
    blobs = {}
    for row in state["slots"]:
        result = Kernel._get(history, row["result"], "result")["payload"]["outputs"]
        outputs = {label: result[label] for label in ("raw_data", "metrics")}
        for key in outputs.values():
            blobs[key] = store.read(key)
        slots.append(dict(slot=row["slot"], roster_unit=row["seed"], mode=row["mode"],
                          run=row["run"], result=row["result"], outputs=outputs))
    context = api.AnalysisContext(
        pack_id=PACK, pack_version=loaded.pack_version, protocol=protocol["payload"],
        protocol_hash=protocol["hash"], parameters=request.parameters, draft=draft,
        execution_plan=execution.to_dict(), capture=None,
        batch=dict(plan=batch, settlement=state["settlement"]["id"]), slots=slots,
        numeric_tolerance=loaded.manifest.numeric_tolerance)
    return context, api.CasView(blobs, hidden={protocol["payload"]["data"]}), protocol["payload"]["data"]


class SyntheticPackTests(unittest.TestCase):
    def setUp(self):
        self.loaded = registry.load_pack(PACK)

    def test_pack_is_registered_and_compiles_like_the_legacy_compiler(self):
        self.assertEqual(registry.PACKS[PACK], "episteme.domains.packs.synthetic_causal_v1")
        catalog = self.loaded.hook("describe")()
        frozen = legacy.describe()
        self.assertEqual((catalog.description, api.thaw(catalog.parameters_schema), catalog.metric,
                          api.thaw(catalog.outputs), api.thaw(catalog.limitations)),
                         (frozen["description"], frozen["parameters_schema"], frozen["metric"],
                          frozen["outputs"], frozen["limitations"]))
        for count in (32, 512):
            for assignment in ("randomized", "observational"):
                for method in ("difference_in_means", "adjusted_ols"):
                    parameters = dict(n_samples=count, assignment=assignment, analysis=method)
                    with self.subTest(parameters=parameters):
                        old = legacy.compile_recipe(parameters, WORLD)
                        request = api.CompileRequest(parameters=parameters, host_inputs=dict(
                            world=WORLD, seeds=[7, 11], replication_tolerance=0.1), capture=None)
                        draft = self.loaded.hook("compile_protocol")(request)
                        plan = self.loaded.hook("compile_execution")(request)
                        self.assertEqual((draft.design, draft.analysis_plan, draft.metric,
                                          draft.stopping_rule, api.thaw(draft.statistical_design)),
                                         (old["design"], old["analysis_plan"], old["metric"],
                                          old["stopping_rule"], old["statistical_design"]))
                        self.assertEqual((plan.primary_program, plan.reanalysis_program, plan.input,
                                          api.thaw(plan.outputs)),
                                         (old["implementation"], old["reanalysis_implementation"],
                                          old["data"], old["outputs"]))
                        self.assertEqual((list(draft.roster), draft.roster_semantics,
                                          draft.sample_size_scope, draft.run_limit),
                                         ([7, 11], "rng_seed", "per_roster_unit", 4))
        for host in (dict(world=WORLD, seeds=[], replication_tolerance=0.1),
                     dict(world=WORLD, seeds=[1, 1], replication_tolerance=0.1),
                     dict(world=dict(WORLD, noise_std=-1), seeds=[1], replication_tolerance=0.1),
                     dict(world=WORLD, seeds=[1], replication_tolerance=-1),
                     dict(world=WORLD, seeds=[1])):
            with self.subTest(host=host), self.assertRaises(ValueError):
                self.loaded.hook("compile_protocol")(api.CompileRequest(
                    parameters=dict(n_samples=32, assignment="randomized",
                                    analysis="difference_in_means"),
                    host_inputs=host, capture=None))

    def test_hooks_reproduce_legacy_proposals_without_reading_the_world(self):
        expected = golden_support.read_json("expected.json")["stores"]
        for name in ("model_path", "manual_binding"):
            with self.subTest(store=name), TemporaryDirectory(prefix="episteme-pack-golden-") as temporary:
                store = golden_support.load_history(golden_support.read_json(f"{name}.json"),
                                                    Path(temporary) / "state")
                try:
                    (analysis_id, row), = expected[name]["analyses"].items()
                    event = Kernel._get(store.events(), analysis_id, "batch_analysis")
                    proposal = json.loads(store.read(row["proposal_digest"]))
                    context, cas, world = legacy_context(store, event["payload"]["batch"], self.loaded)
                    checks = self.loaded.hook("validate_outputs")(context, cas)
                    recomputations = self.loaded.hook("recompute_metrics")(context, cas)
                    report = self.loaded.hook("analyse")(context, cas, tuple(checks),
                                                         tuple(recomputations))
                    self.assertTrue(all(check.status == "passed" for check in checks))
                    self.assertEqual(api.thaw(report.details), proposal["details"])
                    self.assertEqual((report.statement, api.thaw(report.limitations),
                                      report.outcome, report.inference_mode),
                                     (proposal["statement"], proposal["limitations"],
                                      proposal["outcome"], proposal["inference_mode"]))
                    self.assertNotIn(world, cas.reads)
                    self.assertNotIn(world, cas.allowlist)
                    with self.assertRaisesRegex(api.PackAccessError, "hidden input"):
                        cas.read(world)
                    report.statistical_report.validate_design(context.draft.typed_design())
                    statistical = report.statistical_report
                    self.assertEqual(statistical.uncertainty["status"], "not_applicable")
                    self.assertEqual(statistical.assumptions["status"], "not_supplied")
                    self.assertEqual(statistical.sample_size["value"]["planned_total"],
                                     32 * len(context.draft.roster))
                finally:
                    store.close()

    def test_tampered_metric_and_foreign_parameters_are_rejected(self):
        with TemporaryDirectory(prefix="episteme-pack-tamper-") as temporary:
            store = golden_support.load_history(golden_support.read_json("manual_binding.json"),
                                                Path(temporary) / "state")
            try:
                event = next(row for row in store.events() if row["kind"] == "batch_analysis")
                context, cas, _ = legacy_context(store, event["payload"]["batch"], self.loaded)
                primary = context.slot(f"primary:{context.draft.roster[0]}")
                forged = canonical({"treatment_effect": 99.0})
                blobs = {key: cas.read(key) for key in cas.allowlist}
                blobs[digest(forged)] = forged
                slots = [dict(api.thaw(row), outputs=dict(api.thaw(row["outputs"]), metrics=digest(forged)))
                         if row["slot"] == primary["slot"] else api.thaw(row) for row in context.slots]
                tampered = api.AnalysisContext(**{**{name: getattr(context, name) for name in (
                    "pack_id", "pack_version", "protocol", "protocol_hash", "parameters", "draft",
                    "execution_plan", "capture", "batch", "numeric_tolerance")}, "slots": slots})
                with self.assertRaisesRegex(ValueError, "disagrees with recomputed raw data"):
                    self.loaded.hook("recompute_metrics")(tampered, api.CasView(blobs))
                foreign = api.AnalysisContext(**{**{name: getattr(context, name) for name in (
                    "pack_id", "pack_version", "protocol", "protocol_hash", "draft",
                    "execution_plan", "capture", "batch", "slots", "numeric_tolerance")},
                    "parameters": dict(n_samples=64, assignment="randomized",
                                       analysis="difference_in_means")})
                checks = self.loaded.hook("validate_outputs")(foreign, api.CasView(
                    {key: cas.read(key) for key in cas.allowlist}))
                self.assertTrue(any("frozen experiment recipe" in finding
                                    for check in checks for finding in check.findings))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
