"""DomainPack envelopes are strict, frozen and match their published schemas.

These fixtures are synthetic. A valid envelope is well-formed; it does not show
that a pack computed a statistic correctly or that a claim is valid.
"""

from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import unittest

from episteme.domains import api
from episteme.domains.synthetic_causal import compile_recipe, describe
from episteme.protocols import StatisticalDesign
from episteme.store import canonical, digest


SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"
PROTOCOL_HASH = "a" * 64


def compiled():
    return compile_recipe(dict(n_samples=32, assignment="randomized",
                               analysis="difference_in_means"),
                          dict(treatment_effect=2.0, confounding_strength=0.0, noise_std=0.0))


def report(**changes):
    value = dict(
        schema_version=1, protocol_hash=PROTOCOL_HASH,
        estimand=api.supplied(dict(protocol_hash=PROTOCOL_HASH, text="Host-known effect")),
        estimator=api.supplied(dict(name="difference_in_means", hook="recompute_metrics",
                                    description="Mean outcome difference")),
        point_estimate=api.supplied(dict(metric="treatment_effect", unit="outcome units",
                                         value=None, by_roster_unit=[dict(roster_unit=7, value=2.0)])),
        uncertainty=api.not_applicable("Preregistered uncertainty.method is not_applicable."),
        confidence_interval=api.not_applicable("Point estimate only by preregistration."),
        effect_size=api.not_supplied("No pooled or standardized effect size is computed."),
        assumptions=api.not_supplied("Balance and positivity checks are not computed."),
        sample_size=api.supplied(dict(experimental_unit="synthetic unit",
                                      unit_scope="per_roster_unit", planned=32, planned_total=32,
                                      analysed=32, exclusions=[], missing_slots=[])),
        multiple_testing=api.not_applicable("No significance tests are planned."),
        stopping_rule=api.supplied(dict(rule_sha256="b" * 64, roster_complete=True,
                                        interim_looks=0)),
        sensitivity_analysis=api.supplied([]),
        deviations=api.supplied([]))
    value.update(changes)
    return value


class DomainApiTests(unittest.TestCase):
    def test_published_schema_files_equal_runtime_schemas(self):
        for name in api.SCHEMAS:
            with self.subTest(schema=name):
                path = SCHEMAS / f"{name}.schema.json"
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                                 api.published_schema(name))
                self.assertEqual(api.SCHEMAS[name]["additionalProperties"], False)
                self.assertEqual(set(api.SCHEMAS[name]["required"]),
                                 set(api.SCHEMAS[name]["properties"]))

    def test_statistical_report_rejects_missing_field_empty_reason_and_bad_status(self):
        accepted = api.StatisticalReport.from_dict(report())
        self.assertEqual(accepted.to_dict(), report())
        self.assertEqual(accepted.digest(), digest(canonical(report())))
        for field in api.STATISTICAL_FIELDS:
            with self.subTest(missing=field):
                value = report()
                value.pop(field)
                with self.assertRaisesRegex(api.EnvelopeError, f"lacks required fields: {field}"):
                    api.StatisticalReport.from_dict(value)
        for reason in ("", "   ", None):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(api.EnvelopeError, "without a reason"):
                    api.StatisticalReport.from_dict(report(
                        assumptions=dict(status="not_supplied", reason=reason)))
        cases = {
            "unknown status": dict(effect_size=dict(status="pending", reason="later")),
            "value and reason": dict(effect_size=dict(status="not_supplied", reason="r", value=1)),
            "supplied without value": dict(effect_size=dict(status="supplied")),
            "estimator not supplied": dict(estimator=api.not_supplied("unknown")),
            "point estimate not applicable": dict(point_estimate=api.not_applicable("n/a")),
            "sample size not supplied": dict(sample_size=api.not_supplied("unknown")),
            "extra report field": dict(p_value=api.supplied(0.5)),
            "empty assumptions": dict(assumptions=api.supplied([])),
            "inverted interval": dict(confidence_interval=api.supplied(
                dict(level=0.95, lower=2.0, upper=1.0, method="t"))),
            "foreign estimand": dict(estimand=api.supplied(dict(protocol_hash="c" * 64,
                                                                text="Other"))),
            "empty point estimate": dict(point_estimate=api.supplied(dict(
                metric="treatment_effect", unit="outcome units", value=None, by_roster_unit=[]))),
            "unknown deviation field": dict(deviations=api.supplied([dict(
                field="p_hacking", plan="a", actual="b", reason="c")])),
            "boolean count": dict(sample_size=api.supplied(dict(
                experimental_unit="synthetic unit", unit_scope="per_roster_unit", planned=True,
                planned_total=32, analysed=32, exclusions=[], missing_slots=[]))),
        }
        for label, change in cases.items():
            with self.subTest(case=label), self.assertRaises(api.EnvelopeError):
                api.StatisticalReport.from_dict(report(**change))

    def test_not_applicable_must_follow_from_the_preregistered_design(self):
        exploratory = StatisticalDesign.from_dict(compiled()["statistical_design"])
        api.StatisticalReport.from_dict(report()).validate_design(exploratory)
        with self.assertRaisesRegex(api.EnvelopeError, "effect_size is not_applicable contrary"):
            api.StatisticalReport.from_dict(report(
                effect_size=api.not_applicable("No effect size."))).validate_design(exploratory)
        estimated = deepcopy(compiled()["statistical_design"])
        estimated["uncertainty"] = dict(method="bootstrap", resampling_unit="synthetic unit",
                                        rationale="Planned percentile bootstrap.")
        estimated["multiple_testing"] = dict(family=["primary"], correction="holm",
                                             rationale="One planned family.")
        design = StatisticalDesign.from_dict(estimated)
        missing = dict(uncertainty=api.not_supplied("Bootstrap was not run."),
                       confidence_interval=api.not_supplied("No interval was computed."))
        cases = {"uncertainty": {}, "confidence_interval": dict(uncertainty=missing["uncertainty"]),
                 "multiple_testing": missing}
        for field, change in cases.items():
            with self.subTest(field=field):
                with self.assertRaisesRegex(api.EnvelopeError, f"{field} is not_applicable contrary"):
                    api.StatisticalReport.from_dict(report(**change)).validate_design(design)
        for field in ("estimand", "assumptions", "stopping_rule", "deviations"):
            with self.subTest(never=field), self.assertRaisesRegex(api.EnvelopeError, "contrary"):
                api.StatisticalReport.from_dict(report(**{field: api.not_applicable("n/a")})
                                                ).validate_design(exploratory)

    def test_envelopes_are_frozen_and_reject_noncanonical_json(self):
        value = api.StatisticalReport.from_dict(report())
        with self.assertRaises(FrozenInstanceError):
            value.protocol_hash = "c" * 64
        with self.assertRaises(TypeError):
            value.sample_size["value"]["planned"] = 1  # type: ignore[index]
        with self.assertRaisesRegex(api.EnvelopeError, "duplicate key"):
            api.strict_loads(b'{"a":1,"a":2}')
        with self.assertRaisesRegex(api.EnvelopeError, "non-finite"):
            api.strict_loads(b'{"a":NaN}')
        with self.assertRaisesRegex(api.EnvelopeError, "non-finite"):
            api.strict_json({"a": float("inf")})
        with self.assertRaisesRegex(api.EnvelopeError, "not strict JSON"):
            api.strict_json({"a": (1, 2)})
        with self.assertRaisesRegex(api.EnvelopeError, "exceeds"):
            api.StatisticalReport.from_dict(report(
                deviations=api.supplied([dict(field="analysis_plan", plan=f"{index}".ljust(4096, "x"),
                                              actual="y" * 4096, reason="z" * 4096)
                                         for index in range(64)]),
                assumptions=api.supplied([dict(assumption=f"{index}".ljust(4096, "a"),
                                               status="unchecked", reference="r" * 4096)
                                          for index in range(64)])))

    def test_protocol_draft_and_execution_plan_contracts(self):
        recipe = compiled()
        draft = dict(schema_version=1, design=recipe["design"], analysis_plan=recipe["analysis_plan"],
                     metric=recipe["metric"], stopping_rule=recipe["stopping_rule"],
                     statistical_design=recipe["statistical_design"], roster=[7, 11],
                     roster_semantics="rng_seed", sample_size_scope="per_roster_unit",
                     run_limit=4, replication_tolerance=1e-9, seen_data=[])
        parsed = api.ProtocolDraft.from_dict(draft)
        self.assertEqual(parsed.typed_design().mode, "exploratory")
        self.assertEqual(api.ProtocolDraft.from_dict(parsed.to_dict()), parsed)
        for label, change in {
                "short run limit": dict(run_limit=3), "duplicate roster": dict(roster=[7, 7]),
                "unknown semantics": dict(roster_semantics="bootstrap_seed"),
                "single semantics": dict(roster_semantics="deterministic_single"),
                "metric drift": dict(metric="other_metric"),
                "stopping drift": dict(stopping_rule="Stop when convenient."),
                "unsorted exposure": dict(seen_data=["b" * 64, "a" * 64]),
                "negative tolerance": dict(replication_tolerance=-1),
                "extra field": dict(world={"treatment_effect": 2.0})}.items():
            with self.subTest(draft=label), self.assertRaises(api.EnvelopeError):
                api.ProtocolDraft.from_dict(dict(draft, **change))
        plan = api.ExecutionPlan(primary_program=recipe["implementation"],
                                 reanalysis_program=recipe["reanalysis_implementation"],
                                 input=recipe["data"], outputs=recipe["outputs"], wall_seconds=10,
                                 max_output_bytes=262144, required_capabilities=[],
                                 execution_profile="trusted_local_python_v1",
                                 environment_requirements=api.environment_requirements())
        frozen = plan.to_dict()
        self.assertEqual(frozen["input"], dict(sha256=digest(recipe["data"]), bytes=len(recipe["data"])))
        blobs = plan.blobs()
        self.assertEqual(api.ExecutionPlan.from_frozen(frozen, blobs.__getitem__), plan)
        for label, change in {
                "same programs": dict(reanalysis_program=recipe["implementation"]),
                "future profile": dict(execution_profile="container_v1"),
                "gpu reservation": dict(environment_requirements=dict(
                    api.environment_requirements(), accelerator="cuda")),
                "missing metrics output": dict(outputs={"raw_data": "raw.json", "other": "x.json"}),
                "colliding outputs": dict(outputs={"raw_data": "raw.json", "metrics": "RAW.json"}),
                "empty input": dict(input=b""), "unknown capability": dict(
                    required_capabilities=["network"])}.items():
            with self.subTest(plan=label), self.assertRaises(api.EnvelopeError):
                api.ExecutionPlan(**{**dict(primary_program=recipe["implementation"],
                    reanalysis_program=recipe["reanalysis_implementation"], input=recipe["data"],
                    outputs=recipe["outputs"], wall_seconds=10, max_output_bytes=262144,
                    required_capabilities=[], execution_profile="trusted_local_python_v1",
                    environment_requirements=api.environment_requirements()), **change})

    def test_catalog_capture_checks_recomputation_and_analysis_report(self):
        catalog = describe()
        parsed = api.ParameterCatalog(schema_version=1, pack_id="synthetic_causal_v1",
            pack_version="1", description=catalog["description"],
            parameters_schema=catalog["parameters_schema"], metric=catalog["metric"],
            outputs=catalog["outputs"], limitations=catalog["limitations"])
        self.assertEqual(parsed.validate_parameters(dict(n_samples=64, assignment="observational",
                                                         analysis="adjusted_ols"))["n_samples"], 64)
        for parameters in (dict(n_samples=64, assignment="observational"),
                           dict(n_samples=2049, assignment="observational", analysis="adjusted_ols"),
                           dict(n_samples=64, assignment="observational", analysis="adjusted_ols",
                                world={})):
            with self.subTest(parameters=parameters), self.assertRaises(api.EnvelopeError):
                parsed.validate_parameters(parameters)
        with self.assertRaisesRegex(api.EnvelopeError, "unsupported schema keywords"):
            api.ParameterCatalog(**dict(parsed.to_dict(), parameters_schema={"format": "email"}))
        bundle = api.CaptureBundle(pack_id="afterlife_seed_v1", pack_version="1",
                                   source_label="fixture-run", audit={"files": 2},
                                   files={"z.txt": b"z", "a/b.json": b"{}"})
        self.assertEqual([row["path"] for row in bundle.to_dict()["inventory"]], ["a/b.json", "z.txt"])
        self.assertEqual(api.CaptureBundle.from_frozen(bundle.to_dict(), bundle.blobs().__getitem__),
                         bundle)
        for path in ("../x", "/abs", "a//b", "C:/x", "a\\b", "./a"):
            with self.subTest(path=path), self.assertRaises(api.EnvelopeError):
                api.CaptureBundle(pack_id="afterlife_seed_v1", pack_version="1",
                                  source_label="fixture", audit={}, files={path: b"x"})
        check = dict(schema_version=1, slot="reanalysis:7", roster_unit=7,
                     mode="independent_reanalysis", result="result-0123456789abcdef",
                     raw_data="c" * 64, metrics="d" * 64, status="passed", findings=[])
        api.OutputCheck.from_dict(check)
        for change in (dict(mode="primary"), dict(roster_unit=8), dict(status="failed")):
            with self.subTest(check=change), self.assertRaises(api.EnvelopeError):
                api.OutputCheck.from_dict(dict(check, **change))
        recomputation = dict(schema_version=1, roster_unit=7, metric="treatment_effect",
                             recomputed=2.0, primary_recorded=2.0, reanalysis_recorded=2.0 + 1e-12,
                             primary_absolute_difference=0.0,
                             reanalysis_absolute_difference=abs(2.0 - (2.0 + 1e-12)),
                             tolerance=1e-9)
        api.Recomputation.from_dict(recomputation)
        for change in (dict(primary_absolute_difference=1e-13),
                       dict(reanalysis_recorded=2.1, reanalysis_absolute_difference=abs(2.0 - 2.1))):
            with self.subTest(recomputation=change), self.assertRaises(api.EnvelopeError):
                api.Recomputation.from_dict(dict(recomputation, **change))
        statistical = api.StatisticalReport.from_dict(report())
        analysis = api.AnalysisReport(pack_id="synthetic_causal_v1", pack_version="1",
            protocol_hash=PROTOCOL_HASH, statement="Point estimates were recorded.",
            limitations=["Synthetic fixture."], outcome="inconclusive",
            inference_mode="exploratory", details={"seeds": [7]}, statistical_report=statistical)
        frozen = analysis.to_dict()
        self.assertEqual(frozen["statistical_report"], statistical.digest())
        self.assertEqual(api.AnalysisReport.from_frozen(frozen, statistical), analysis)
        with self.assertRaisesRegex(api.EnvelopeError, "different protocols"):
            api.AnalysisReport(pack_id="synthetic_causal_v1", pack_version="1",
                               protocol_hash="e" * 64, statement="s", limitations=["l"],
                               outcome="inconclusive", inference_mode="exploratory", details={},
                               statistical_report=statistical)
        with self.assertRaises(api.EnvelopeError):
            api.AnalysisReport.check(dict(frozen, outcome="proven"))

    def test_pack_manifest_requires_mandatory_capabilities_and_unique_outputs(self):
        manifest = dict(contract_version=1, pack_id="fixture_pack_v1", pack_version="1",
            description="Fixture manifest", roster_semantics=["rng_seed"],
            metrics=[dict(name="score", unit="points", description="Fixture score")],
            outputs={"raw_data": dict(path="raw.json", schema_id="fixture/raw-v1"),
                     "metrics": dict(path="metrics.json", schema_id="fixture/metrics-v1")},
            numeric_tolerance=1e-9, hidden_inputs=["protocol_data"],
            execution_profiles=["trusted_local_python_v1"],
            statistical_capabilities=["estimand", "estimator", "point_estimate", "sample_size"],
            interpretation_cautions=["Fixture only."], capture=False)
        parsed = api.PackManifest.from_dict(manifest)
        self.assertEqual(parsed.output_paths(), {"raw_data": "raw.json", "metrics": "metrics.json"})
        for change in (dict(statistical_capabilities=["estimand", "estimator", "point_estimate"]),
                       dict(outputs={"raw_data": dict(path="x.json", schema_id="a"),
                                     "metrics": dict(path="X.json", schema_id="b")}),
                       dict(metrics=[manifest["metrics"][0], manifest["metrics"][0]]),
                       dict(contract_version=2), dict(hidden_inputs=["world"]),
                       dict(execution_profiles=["subprocess_v1"])):
            with self.subTest(manifest=change), self.assertRaises(api.EnvelopeError):
                api.PackManifest.from_dict(dict(manifest, **change))


if __name__ == "__main__":
    unittest.main()
