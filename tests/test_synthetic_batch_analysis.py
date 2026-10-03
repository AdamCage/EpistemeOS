"""Domain-local arithmetic and provenance checks for synthetic batch proposals."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from episteme.domains.synthetic_batch_analysis import (
    NUMERIC_TOLERANCE, SyntheticCausalBatchAnalysisAdapter,
)
from episteme.store import Store


class SyntheticBatchAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / "research")
        self.addCleanup(self.store.close)

    def event(self, kind: str, payload: dict) -> dict:
        return self.store.append(id=uuid4().hex, kind=kind, actor="fixture",
                                 role="planner", payload=payload,
                                 expected_revision=len(self.store.events()))

    def fixture(self, *, analysis: str = "difference_in_means",
                observed_assignment: str | None = None,
                wrong_primary_metric: bool = False,
                wrong_reanalysis_metric: bool = False,
                different_reanalysis_raw: bool = False,
                world_effect: float = 2.0) -> dict:
        parameters = dict(n_samples=32, assignment="randomized", analysis=analysis)
        data = self.store.put_json(dict(schema_version=1, domain="synthetic_causal_v1",
                                        parameters=parameters,
                                        world=dict(treatment_effect=world_effect,
                                                   confounding_strength=1.0, noise_std=0.0)))
        protocol = self.event("protocol", dict(metric="treatment_effect",
                                                protocol_mode="exploratory", data=data,
                                                seeds=[7, 11]))
        compilation = self.store.put_json(dict(compiled=dict(parameters=parameters)))
        self.event("agent_application", dict(protocol=protocol["id"], compilation=compilation))
        specs, rows = [], []
        for seed in (7, 11):
            observations = []
            for i in range(32):
                if analysis == "adjusted_ols":
                    # U is confounded with T: the naive contrast is 3.5,
                    # while the registered adjusted coefficient is 2.0.
                    u = int(i >= 16)
                    t = int(i >= 12) if u == 0 else int(i < 28)
                    y = 2.0 * t + 3.0 * u
                else:
                    u, t = i % 2, (i // 2) % 2
                    y = 2.0 * t + float(u)
                observations.append(dict(unit=i, u=u, t=t, y=y))
            raw_parameters = dict(parameters, assignment=observed_assignment or parameters["assignment"])
            raw = self.store.put_json(dict(schema_version=1, domain="synthetic_causal_v1",
                                           seed=seed, parameters=raw_parameters, rows=observations))
            for label in ("primary", "reanalysis"):
                mode = "primary" if label == "primary" else "independent_reanalysis"
                slot = f"{label}:{seed}"
                source = f"primary:{seed}" if label == "reanalysis" else None
                prior = next((row for row in rows if row["slot"] == source), None)
                run = self.event("run", dict(seed=seed, replicate_of=prior["run"] if prior else None))
                metric = 3.0 if ((label == "primary" and wrong_primary_metric)
                                 or (label == "reanalysis" and wrong_reanalysis_metric)) else 2.0
                outputs = dict(raw_data=(self.store.put(b"different raw")
                                         if label == "reanalysis" and different_reanalysis_raw else raw),
                               metrics=self.store.put_json(dict(treatment_effect=metric)))
                result = self.event("result", dict(run=run["id"], status="completed", outputs=outputs))
                specs.append(dict(slot=slot, seed=seed, mode=mode))
                rows.append(dict(slot=slot, seed=seed, mode=mode, status="completed",
                                 run=run["id"], result=result["id"]))
        plan = self.event("batch_plan", dict(protocol=protocol["id"], slots=specs,
                                             outputs=dict(raw_data="raw.json", metrics="metrics.json")))
        settlement = self.event("batch_settlement", dict(status="completed",
                                                         scientific_validity="not_assessed"))
        terminal = self.event("search_terminal", dict(status="completed"))
        return dict(plan=plan, settlement=settlement, terminal=terminal, slots=rows)

    def test_recomputes_every_seed_and_preserves_inconclusive_status(self) -> None:
        for analysis in ("difference_in_means", "adjusted_ols"):
            with self.subTest(analysis=analysis):
                state = self.fixture(analysis=analysis, world_effect=99.0)
                before = self.store.export()
                proposal = SyntheticCausalBatchAnalysisAdapter.propose(self.store, state)
                self.assertEqual(before, self.store.export())
                self.assertEqual(set(proposal), {"schema_version", "adapter_id", "adapter_version",
                                                  "statement", "limitations", "outcome", "inference_mode",
                                                  "details"})
                self.assertEqual(proposal["outcome"], "inconclusive")
                self.assertEqual(proposal["inference_mode"], "exploratory")
                self.assertNotIn("world", proposal["details"])
                self.assertNotIn("99", proposal["statement"])
                self.assertEqual(proposal["details"]["absolute_numeric_tolerance"],
                                 NUMERIC_TOLERANCE)
                results = proposal["details"]["seed_results"]
                self.assertEqual([item["seed"] for item in results], [7, 11])
                self.assertTrue(all(item["recomputed_treatment_effect"] == 2.0 for item in results))
                self.assertTrue(all(item["n_units"] == 32 and item["n_treated"] == 16
                                    and item["n_control"] == 16 for item in results))
                self.assertTrue(all(item["primary_absolute_difference"] == 0.0
                                    and item["reanalysis_absolute_difference"] == 0.0
                                    for item in results))

    def test_rejects_misreported_metric_or_changed_reanalysis_input(self) -> None:
        for kwargs in (dict(wrong_primary_metric=True),
                       dict(wrong_reanalysis_metric=True),
                       dict(different_reanalysis_raw=True),
                       dict(observed_assignment="observational")):
            with self.subTest(kwargs=kwargs):
                state = self.fixture(**kwargs)
                with self.assertRaises(ValueError):
                    SyntheticCausalBatchAnalysisAdapter.propose(self.store, state)

    def test_hidden_world_artifact_is_never_read(self) -> None:
        state = self.fixture(world_effect=99.0)
        protocol = next(event for event in self.store.events()
                        if event["id"] == state["plan"]["payload"]["protocol"])
        hidden_data = protocol["payload"]["data"]
        original_read = self.store.read

        def deny_hidden(key: str) -> bytes:
            if key == hidden_data:
                raise AssertionError("analysis read the hidden generator world")
            return original_read(key)

        with patch.object(self.store, "read", side_effect=deny_hidden):
            proposal = SyntheticCausalBatchAnalysisAdapter.propose(self.store, state)
        self.assertEqual(proposal["outcome"], "inconclusive")
        self.assertNotIn("99", proposal["statement"])

    def test_requires_complete_settlement_and_all_slots(self) -> None:
        state = self.fixture()
        state["slots"][-1]["status"] = "unknown"
        with self.assertRaisesRegex(ValueError, "incomplete"):
            SyntheticCausalBatchAnalysisAdapter.propose(self.store, state)
        state["slots"][-1]["status"] = "completed"
        state["settlement"] = None
        with self.assertRaisesRegex(ValueError, "completed"):
            SyntheticCausalBatchAnalysisAdapter.propose(self.store, state)


if __name__ == "__main__":
    unittest.main()
