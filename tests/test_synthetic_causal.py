"""Bounded, deterministic synthetic causal fixtures; no scientific approval."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from episteme.domains.synthetic_causal import compile_recipe, describe
from episteme.protocols import StatisticalDesign


WORLD = {"treatment_effect": 2.0, "confounding_strength": 3.0, "noise_std": 0.0}


def run_source(source: bytes, input_data: bytes, seed: int) -> tuple[bytes, bytes]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "program.py").write_bytes(source)
        (root / "input.dat").write_bytes(input_data)
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "program.py", "input.dat", "--seed", str(seed)],
            cwd=root, capture_output=True, timeout=10, check=False,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr.decode("utf-8", errors="replace"))
        return (root / "raw.json").read_bytes(), (root / "metrics.json").read_bytes()


class SyntheticCausalTests(unittest.TestCase):
    def test_frozen_recipe_is_complete_and_descriptive(self):
        params = {"n_samples": 128, "assignment": "observational", "analysis": "adjusted_ols"}
        recipe = compile_recipe(params, WORLD)
        self.assertEqual(recipe, compile_recipe(params, WORLD))
        self.assertEqual(set(recipe), {
            "schema_version", "domain", "parameters", "implementation", "reanalysis_implementation",
            "data", "metric", "outputs", "design", "analysis_plan", "stopping_rule",
            "statistical_design",
        })
        self.assertNotEqual(recipe["implementation"], recipe["reanalysis_implementation"])
        self.assertEqual(recipe["outputs"], {"raw_data": "raw.json", "metrics": "metrics.json"})
        self.assertEqual(recipe["metric"], "treatment_effect")
        self.assertEqual(recipe["statistical_design"]["mode"], "exploratory")
        self.assertEqual(recipe["statistical_design"]["data_splits"][0]["digest"],
                         hashlib.sha256(recipe["data"]).hexdigest())
        self.assertEqual(recipe["stopping_rule"], recipe["statistical_design"]["stopping_rule"]["rule"])
        self.assertEqual(StatisticalDesign.from_dict(recipe["statistical_design"]).to_dict(),
                         recipe["statistical_design"])
        declaration = describe()
        json.dumps(declaration, allow_nan=False)
        self.assertNotIn("world", declaration)
        self.assertEqual(declaration["parameters_schema"]["additionalProperties"], False)
        self.assertEqual(declaration["metric"], recipe["metric"])

    def test_deterministic_replay_and_fresh_seed_are_distinct(self):
        recipe = compile_recipe(
            {"n_samples": 64, "assignment": "randomized", "analysis": "difference_in_means"}, WORLD)
        first = run_source(recipe["implementation"], recipe["data"], 17)
        self.assertEqual(first, run_source(recipe["implementation"], recipe["data"], 17))
        other = run_source(recipe["implementation"], recipe["data"], 18)
        self.assertNotEqual(first[0], other[0])
        raw = json.loads(first[0])
        self.assertEqual(len(raw["rows"]), 64)
        self.assertEqual([row["unit"] for row in raw["rows"]], list(range(64)))
        replicated = run_source(recipe["reanalysis_implementation"], first[0], 17)
        self.assertEqual(replicated[0], first[0])
        self.assertAlmostEqual(json.loads(replicated[1])["treatment_effect"],
                               json.loads(first[1])["treatment_effect"], places=12)

    def test_confounded_naive_estimate_and_adjustment(self):
        base = {"n_samples": 2048, "assignment": "observational"}
        naive = compile_recipe({**base, "analysis": "difference_in_means"}, WORLD)
        adjusted = compile_recipe({**base, "analysis": "adjusted_ols"}, WORLD)
        raw_naive, metric_naive = run_source(naive["implementation"], naive["data"], 19)
        raw_adjusted, metric_adjusted = run_source(adjusted["implementation"], adjusted["data"], 19)
        self.assertEqual(json.loads(raw_naive)["rows"], json.loads(raw_adjusted)["rows"])
        self.assertGreater(json.loads(metric_naive)["treatment_effect"], 3.4)
        self.assertAlmostEqual(json.loads(metric_adjusted)["treatment_effect"], 2.0, places=12)
        _, reanalysis = run_source(adjusted["reanalysis_implementation"], raw_adjusted, 19)
        self.assertAlmostEqual(json.loads(reanalysis)["treatment_effect"], 2.0, places=10)

    def test_random_assignment_removes_systematic_confounding(self):
        recipe = compile_recipe(
            {"n_samples": 2048, "assignment": "randomized", "analysis": "difference_in_means"}, WORLD)
        estimates = []
        for seed in range(5):
            _, metric = run_source(recipe["implementation"], recipe["data"], seed)
            estimates.append(json.loads(metric)["treatment_effect"])
        self.assertLess(abs(sum(estimates) / len(estimates) - WORLD["treatment_effect"]), 0.2)

    def test_rejects_invalid_or_ambiguous_recipes(self):
        valid = {"n_samples": 32, "assignment": "randomized", "analysis": "adjusted_ols"}
        bad_params = [
            {**valid, "n_samples": True}, {**valid, "n_samples": 31},
            {**valid, "n_samples": 2049}, {**valid, "assignment": []},
            {**valid, "analysis": "unknown"}, {**valid, "extra": 1},
        ]
        for bad in bad_params:
            with self.subTest(parameters=bad), self.assertRaises(ValueError):
                compile_recipe(bad, WORLD)
        for bad in (
            {**WORLD, "noise_std": -0.1}, {**WORLD, "noise_std": float("nan")},
            {**WORLD, "treatment_effect": float("inf")}, {**WORLD, "extra": 1},
            {**WORLD, "confounding_strength": True},
        ):
            with self.subTest(world=bad), self.assertRaises(ValueError):
                compile_recipe(valid, bad)


if __name__ == "__main__":
    unittest.main()
