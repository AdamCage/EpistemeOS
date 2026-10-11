"""risen_universalis_v1 is a mechanical smoke, not a scientific result.

Existing packs stay on trusted_local_python_v1. This pack accepts only
uv_locked_python_v2. Confirmatory strength is left unavailable. No test calls
a model, a GPU, or the network. The full smoke needs uv and is skipped with a
reason when uv is absent; the ceiling and profile checks are not skipped.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from tempfile import TemporaryDirectory

import pack_fixtures
from episteme.domain_packs import ceiling
from episteme.domains import api, registry
from episteme.domains.packs import risen_universalis_v1 as pack
from episteme.execution import freeze_environment
from episteme.execution_locked import freeze_closure
from episteme.kernel import Actor, Kernel
from episteme.planning import Planning
from episteme.store import Store


PROJECT = Path(__file__).resolve().parents[1]
SMOKE = PROJECT / "examples" / "risen_universalis_v1" / "smoke.py"
UV = shutil.which("uv")
UV_REASON = "uv is not on PATH; the risen smoke needs the v2 runner (offline, no downloads)"


def _load_smoke():
    spec = importlib.util.spec_from_file_location("risen_universalis_smoke", SMOKE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RisenPackTests(unittest.TestCase):
    def test_old_packs_keep_v1_and_this_pack_rejects_it(self):
        self.assertEqual(registry.PACKS["risen_universalis_v1"],
                         "episteme.domains.packs.risen_universalis_v1")
        for pack_id in ("synthetic_causal_v1", "afterlife_seed_v1", "tabular_classification_v1"):
            profiles = list(registry.load_pack(pack_id).manifest.execution_profiles)
            self.assertEqual(profiles, ["trusted_local_python_v1"])
        loaded = registry.load_pack(pack.PACK_ID)
        self.assertEqual(list(loaded.manifest.execution_profiles), ["uv_locked_python_v2"])
        self.assertIn("confirmatory_result", pack.DEFINITIONS)
        self.assertIn("model_id", pack.OBSERVED_FIELDS)
        with self.assertRaisesRegex(ValueError, "uv_locked_python_v2"):
            pack.validate_parameters({"contrast": "paired_difference",
                                      "execution_profile": "trusted_local_python_v1"})

    def test_kernel_ceiling_blocks_confirmatory_on_the_smoke_report(self):
        loaded = registry.load_pack(pack.PACK_ID)
        request = pack_fixtures.FIXTURES[pack.PACK_ID][1](Path("unused"))
        draft = loaded.hook("compile_protocol")(request)
        plan = loaded.hook("compile_execution")(request)
        self.assertIsInstance(plan, api.ExecutionPlanV2)
        self.assertEqual(plan.execution_profile, "uv_locked_python_v2")
        self.assertEqual(draft.typed_design().mode, "exploratory")
        with TemporaryDirectory() as work:
            slots, blobs = pack_fixtures.run_programs(plan, draft.roster, Path(work))
        context, cas = pack_fixtures.analysis_inputs(
            loaded.manifest, request, draft, plan, slots, blobs)
        checks = loaded.hook("validate_outputs")(context, cas)
        recomputations = loaded.hook("recompute_metrics")(context, cas)
        report = loaded.hook("analyse")(context, cas, tuple(checks), tuple(recomputations))
        self.assertEqual((report.outcome, report.inference_mode), ("inconclusive", "exploratory"))
        self.assertEqual(report.statistical_report.effect_size["status"], "not_supplied")
        facts = {"protocol": {"hash": "f" * 64, "payload": {"metric": pack.METRIC}}}
        kernel = {"design": draft.typed_design(), "scope": "total", "planned_total": 2,
                  "missing": [], "roster": list(draft.roster), "roster_complete": True}
        promoted = api.AnalysisReport(
            pack_id=report.pack_id, pack_version=report.pack_version,
            protocol_hash=report.protocol_hash, statement=report.statement,
            limitations=report.limitations, outcome="supports", inference_mode="confirmatory",
            details=api.thaw(report.details), statistical_report=report.statistical_report)
        with self.assertRaisesRegex(ValueError, "exceeds the ceiling"):
            ceiling(report.statistical_report, promoted, facts, kernel, list(recomputations))

    def test_preregister_rejects_v1_environment_and_pins_v2(self):
        smoke = _load_smoke()
        with TemporaryDirectory(prefix="episteme-risen-admit-") as directory:
            root = Path(directory) / "state"
            with Store(root) as store:
                plan = Planning(store, smoke.PLANNER)
                question = plan.question(
                    study_id=smoke.STUDY, statement="Smoke question", objective="Pin the profile",
                    scope=smoke.SCOPE, constraints=["Fixture"], stopping_criteria=["No run"])
                kernel = Kernel(store, smoke.PLANNER)
                hypotheses = [kernel.hypothesis(text, text, text, smoke.SCOPE) for text in ("a", "b")]
                explanation = plan.explanation_set(
                    question=question, hypotheses=hypotheses, comparison_plan="Keep both")
                before = len(store.events())
                with self.assertRaisesRegex(ValueError, "unsupported environment closure"):
                    smoke._preregister(store, explanation, freeze_environment(store),
                                       "paired_difference")
                self.assertEqual(len(store.events()), before)
                environment = freeze_closure(store, pack.project(), set_variables=pack.VARIABLES,
                                             installer_version="0.0.0")
                registered = smoke._preregister(store, explanation, environment, "paired_difference")
                binding = Kernel._get(store.events(), registered["binding"], "pack_binding")
                self.assertEqual(binding["payload"]["execution_profile"], "uv_locked_python_v2")
                self.assertEqual(binding["payload"]["scientific_validity"], "not_assessed")


@unittest.skipUnless(UV, UV_REASON)
class RisenSmokeTests(unittest.TestCase):
    def test_smoke_assigns_review_and_does_not_approve_or_publish(self):
        smoke = _load_smoke()
        with TemporaryDirectory(prefix="episteme-risen-smoke-") as directory:
            root = Path(directory) / "state"
            result = smoke.run_smoke(root)
            self.assertEqual(result["scientific_validity"], "not_assessed")
            self.assertFalse(result["question"]["scientific_success"])
            self.assertFalse(result["package"]["scientific_success"])
            self.assertIn("не научная публикация", result["package"]["note"])
            checkpoints = root.parent / (root.name + "-checkpoints")
            resolved_root, resolved_checkpoints = root.resolve(), checkpoints.resolve()
            self.assertNotEqual(resolved_checkpoints, resolved_root)
            self.assertNotIn(resolved_root, resolved_checkpoints.parents)
            self.assertNotIn(resolved_checkpoints, resolved_root.parents)
            with Store(root) as store:
                self.assertEqual(store.external_checkpoint(), result["package"]["digest"])
                self.assertFalse(any(event["kind"] in {"review", "paper"} for event in store.events()))
            environment = dict(os.environ, PYTHONPATH=str(PROJECT / "src"))
            completed = subprocess.run(
                [sys.executable, "-m", "episteme", "checkpoint", "--root", str(root),
                 "--expect", result["question"]["digest"]],
                cwd=PROJECT, env=environment, capture_output=True, text=True, encoding="utf-8",
                timeout=60, check=False)
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertIn("does not match", completed.stdout + completed.stderr)
            fresh = subprocess.run(
                [sys.executable, "-m", "episteme", "checkpoint", "--root", str(root),
                 "--expect", result["package"]["digest"]],
                cwd=PROJECT, env=environment, capture_output=True, text=True, encoding="utf-8",
                timeout=60, check=False)
            self.assertEqual(fresh.returncode, 0, fresh.stderr)
            body = json.loads(fresh.stdout)
            self.assertEqual(body["meaning"], "external_history_digest")
            self.assertEqual(body["digest"], result["package"]["digest"])
