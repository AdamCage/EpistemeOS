"""Statistical declarations reject structural faults without certifying science."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import unittest

from episteme.protocols import DesignError, Metric, StatisticalDesign


def design_fixture(mode="exploratory"):
    """Synthetic declaration only; these digests are not research evidence."""
    return dict(
        schema_version=1, mode=mode, experimental_unit="task",
        estimand="Mean task-level paired score difference over the registered task population",
        primary_metric=dict(name="mean_difference", unit="score"),
        secondary_metrics=[dict(name="failure_fraction", unit="fraction")],
        sample_size=12,
        sample_size_rationale="Twelve-task development fixture, no confirmatory power claim",
        uncertainty=dict(method="cluster bootstrap, 1000 draws, 95% interval",
                         resampling_unit="task", rationale="Resample independent tasks, retaining all seeds per task"),
        exclusions=[],
        stopping_rule=dict(kind="fixed_sample", rule="Run all twelve tasks; retain failures"),
        multiple_testing=dict(family=["paired mean difference vs baseline"], correction="not_applicable",
                              rationale="One registered comparison; secondary fraction is descriptive"),
        data_splits=[dict(id="development", digest="a" * 64, role="discovery", exposure_policy="open")],
    )


class StatisticalDesignTests(unittest.TestCase):
    def test_round_trip_is_immutable_and_detached_from_caller_data(self):
        source = design_fixture()
        expected = deepcopy(source)
        design = StatisticalDesign.from_dict(source)
        self.assertEqual(design.to_dict(), expected)
        self.assertEqual(StatisticalDesign.from_dict(json.loads(json.dumps(design.to_dict()))), design)
        source["secondary_metrics"][0]["name"] = "chosen_after_result"
        source["exclusions"].append("negative outcomes")
        exported = design.to_dict()
        exported["data_splits"][0]["digest"] = "b" * 64
        self.assertEqual(design.to_dict(), expected)
        with self.assertRaises(FrozenInstanceError):
            design.primary_metric.name = "different"
        with self.assertRaises(DesignError):
            replace(design, secondary_metrics=[])
        with self.assertRaises(DesignError):
            replace(design, primary_metric={"name": "x", "unit": "score"})

    def test_descriptive_census_permits_explicit_noninferential_design(self):
        source = design_fixture("descriptive")
        source["uncertainty"] = dict(method="not_applicable", resampling_unit=None,
                                     rationale="Complete finite census; no population inference")
        source["multiple_testing"]["family"] = []
        design = StatisticalDesign.from_dict(source)
        self.assertEqual(design.mode, "descriptive")
        self.assertIsNone(design.uncertainty.resampling_unit)
        self.assertEqual(design.exclusions, ())
        design.validate_metric("mean_difference")

    def test_declared_seed_level_pseudoreplication_is_rejected(self):
        source = design_fixture()
        source["uncertainty"]["resampling_unit"] = "seed"
        with self.assertRaisesRegex(DesignError, "pseudoreplication"):
            StatisticalDesign.from_dict(source)

    def test_secondary_and_unregistered_metrics_cannot_become_primary(self):
        design = StatisticalDesign.from_dict(design_fixture())
        design.validate_metric("mean_difference")
        for name in ("failure_fraction", "best_seed_score", "", None):
            with self.subTest(name=name), self.assertRaises(DesignError):
                design.validate_metric(name)

    def test_confirmatory_requires_protected_confirmatory_data(self):
        source = design_fixture("confirmatory")
        for role, policy in (("discovery", "open"), ("confirmatory", "open"),
                             ("validation", "holdout"), ("discovery", "new_data")):
            source["data_splits"][0].update(role=role, exposure_policy=policy)
            with self.subTest(role=role, policy=policy), self.assertRaisesRegex(DesignError, "confirmatory design"):
                StatisticalDesign.from_dict(source)
        for policy in ("holdout", "new_data"):
            source["data_splits"][0].update(role="confirmatory", exposure_policy=policy)
            design = StatisticalDesign.from_dict(source)
            self.assertEqual(design.data_splits[0].exposure_policy, policy)

    def test_sequential_plan_requires_prespecified_error_control(self):
        source = design_fixture("confirmatory")
        source["data_splits"][0].update(role="confirmatory", exposure_policy="sequential")
        with self.assertRaisesRegex(DesignError, "sequential stopping"):
            StatisticalDesign.from_dict(source)
        source["stopping_rule"] = dict(kind="sequential", rule="Four registered looks, at most twelve tasks")
        for control in (None, "", "  "):
            source["stopping_rule"]["error_control"] = control
            with self.subTest(control=control), self.assertRaises(DesignError):
                StatisticalDesign.from_dict(source)
        source["stopping_rule"]["error_control"] = "Bonferroni across four fixed looks, family alpha 0.05"
        self.assertEqual(StatisticalDesign.from_dict(source).stopping_rule.kind, "sequential")
        source["stopping_rule"]["kind"] = "fixed_sample"
        with self.assertRaisesRegex(DesignError, "fixed_sample"):
            StatisticalDesign.from_dict(source)

    def test_same_bytes_cannot_be_declared_separate_discovery_and_holdout(self):
        source = design_fixture("confirmatory")
        source["data_splits"].append(dict(id="holdout", digest="a" * 64,
                                          role="confirmatory", exposure_policy="holdout"))
        with self.assertRaisesRegex(DesignError, "unique digest"):
            StatisticalDesign.from_dict(source)
        source["data_splits"][1]["digest"] = "b" * 64
        self.assertEqual(len(StatisticalDesign.from_dict(source).data_splits), 2)
        source["data_splits"][1]["id"] = "development"
        with self.assertRaisesRegex(DesignError, "unique id"):
            StatisticalDesign.from_dict(source)

    def test_metric_names_are_unique_across_primary_and_secondary(self):
        source = design_fixture()
        source["secondary_metrics"][0] = dict(name="mean_difference", unit="different-unit")
        with self.assertRaisesRegex(DesignError, "metric names must be unique"):
            StatisticalDesign.from_dict(source)
        source["secondary_metrics"] = [dict(name="secondary", unit="score"), dict(name="secondary", unit="fraction")]
        with self.assertRaisesRegex(DesignError, "metric names must be unique"):
            StatisticalDesign.from_dict(source)

    def test_unknown_and_missing_fields_fail_at_every_object_boundary(self):
        paths = ((), ("primary_metric",), ("secondary_metrics", 0), ("uncertainty",),
                 ("stopping_rule",), ("multiple_testing",), ("data_splits", 0))
        for path in paths:
            source = design_fixture()
            target = source
            for key in path:
                target = target[key]
            target["unplanned"] = True
            with self.subTest(path=path, error="unknown"), self.assertRaisesRegex(DesignError, "unknown fields"):
                StatisticalDesign.from_dict(source)
            del target["unplanned"]
            for key in list(target):
                value = target.pop(key)
                with self.subTest(path=path, key=key, error="missing"), self.assertRaisesRegex(DesignError, "missing required"):
                    StatisticalDesign.from_dict(source)
                target[key] = value

    def test_nonfinite_boolean_empty_and_wrong_type_fields_are_rejected(self):
        paths = (("mode",), ("experimental_unit",), ("estimand",), ("sample_size_rationale",),
                 ("primary_metric", "name"), ("primary_metric", "unit"),
                 ("uncertainty", "method"), ("uncertainty", "resampling_unit"), ("uncertainty", "rationale"),
                 ("stopping_rule", "rule"), ("multiple_testing", "correction"), ("multiple_testing", "rationale"),
                 ("data_splits", 0, "id"), ("data_splits", 0, "digest"), ("data_splits", 0, "role"),
                 ("data_splits", 0, "exposure_policy"))
        for path in paths:
            for invalid in ("", " \t\n", None, True, 1, float("nan"), float("inf"), [], {}):
                source = design_fixture()
                target = source
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = invalid
                with self.subTest(path=path, invalid=invalid), self.assertRaises(DesignError):
                    StatisticalDesign.from_dict(source)
        for field in ("sample_size", "schema_version"):
            for invalid in (0, -1, True, None, "12", 1.0, float("nan"), float("inf")):
                source = design_fixture()
                source[field] = invalid
                with self.subTest(field=field, invalid=invalid), self.assertRaises(DesignError):
                    StatisticalDesign.from_dict(source)
        source = design_fixture()
        source["schema_version"] = 2
        with self.assertRaisesRegex(DesignError, "unsupported"):
            StatisticalDesign.from_dict(source)

    def test_arrays_require_json_arrays_and_valid_unique_elements(self):
        paths = (("secondary_metrics",), ("exclusions",), ("data_splits",), ("multiple_testing", "family"))
        for path in paths:
            for invalid in (None, "", {}, (), [""], [None], [float("nan")]):
                source = design_fixture()
                target = source
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = invalid
                with self.subTest(path=path, invalid=invalid), self.assertRaises(DesignError):
                    StatisticalDesign.from_dict(source)
        for path in (("exclusions",), ("multiple_testing", "family")):
            source = design_fixture()
            target = source
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = ["same", "same"]
            with self.subTest(path=path), self.assertRaisesRegex(DesignError, "unique"):
                StatisticalDesign.from_dict(source)
        source = design_fixture()
        source["data_splits"] = []
        with self.assertRaisesRegex(DesignError, "not be empty"):
            StatisticalDesign.from_dict(source)

    def test_uncertainty_exemption_and_multiplicity_need_explicit_reason(self):
        source = design_fixture()
        source["multiple_testing"].update(correction="Holm", family=[])
        with self.assertRaisesRegex(DesignError, "must not be empty"):
            StatisticalDesign.from_dict(source)
        source["multiple_testing"]["family"] = ["comparison-1", "comparison-2"]
        StatisticalDesign.from_dict(source)
        source["uncertainty"]["method"] = "not_applicable"
        with self.assertRaisesRegex(DesignError, "null resampling_unit"):
            StatisticalDesign.from_dict(source)
        source["uncertainty"]["resampling_unit"] = None
        source["uncertainty"]["rationale"] = " "
        with self.assertRaisesRegex(DesignError, "rationale"):
            StatisticalDesign.from_dict(source)

    def test_public_schema_examples_are_valid_and_contract_versions_match(self):
        path = Path(__file__).resolve().parents[1] / "schemas" / "statistical-design-v1.schema.json"
        schema = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertEqual(set(schema["required"]), set(StatisticalDesign.__dataclass_fields__))
        self.assertEqual(set(schema["properties"]), set(schema["required"]))
        self.assertFalse(schema["additionalProperties"])
        self.assertTrue(schema["examples"])
        for example in schema["examples"]:
            self.assertEqual(StatisticalDesign.from_dict(example).to_dict(), example)
        self.assertEqual(Metric.from_dict({"name": "custom_domain_metric", "unit": "domain-unit"}).name,
                         "custom_domain_metric")


if __name__ == "__main__":
    unittest.main()
