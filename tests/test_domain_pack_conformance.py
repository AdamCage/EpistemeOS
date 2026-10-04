"""Every registered DomainPack passes one conformance suite (ADR 0016).

The suite runs for every pack in ``registry.PACKS`` plus a test-only fixture
pack. It checks the manifest, the complete-code pin, the import contract,
envelope validity and repeatability of hooks over a ``CasView``. Programs run
through a minimal local runner without a Store; outputs are synthetic and the
checks are mechanical, not a scientific review of any pack.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pack_fixtures
from episteme.domains import api, registry
from episteme.store import canonical, digest


def _packs() -> dict[str, str]:
    return {**registry.PACKS, pack_fixtures.FIXTURE_PACK: pack_fixtures.FIXTURE_PACK}


class PackConformanceTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(registry.PACKS, _packs())
        patcher.start()
        self.addCleanup(patcher.stop)
        temporary = TemporaryDirectory(prefix="episteme-pack-conformance-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def compiled(self, pack_id: str):
        loaded = registry.load_pack(pack_id)
        request = pack_fixtures.FIXTURES[pack_id][1](self.root / f"{pack_id}-inputs")
        draft = loaded.hook("compile_protocol")(request)
        plan = loaded.hook("compile_execution")(request)
        return loaded, request, draft, plan

    def test_registry_is_explicit_and_every_pack_has_conformance_inputs(self):
        tree = ast.parse(Path(registry.__file__).read_bytes())
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names}
        imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                     and node.module}
        self.assertFalse(imported & {"importlib.metadata", "pkgutil", "pkg_resources"})
        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        names |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertFalse(names & {"entry_points", "iter_modules", "walk_packages"})
        literal = [node.value for node in tree.body if isinstance(node, ast.AnnAssign)
                   and isinstance(node.target, ast.Name) and node.target.id == "PACKS"]
        self.assertEqual(len(literal), 1)
        self.assertIsInstance(literal[0], ast.Dict)
        self.assertTrue(all(isinstance(item, ast.Constant) and type(item.value) is str
                            for item in [*literal[0].keys, *literal[0].values]))
        for pack_id, module in registry.PACKS.items():
            self.assertEqual(type(pack_id), str)
            self.assertEqual(type(module), str)
            self.assertIn(pack_id, pack_fixtures.FIXTURES, "registered pack lacks conformance inputs")

    def test_manifest_code_pin_and_import_contract(self):
        for pack_id in _packs():
            with self.subTest(pack=pack_id):
                loaded = registry.load_pack(pack_id)
                self.assertIs(registry.load_pack(pack_id), loaded)
                self.assertEqual(loaded.manifest.pack_id, pack_id)
                manifest = api.validate_code_manifest(api.thaw(loaded.code_manifest))
                self.assertEqual(loaded.pack_code_digest, digest(canonical(manifest)))
                on_disk = registry.pack_files(loaded.root)
                self.assertEqual(dict(loaded.files), on_disk)
                self.assertEqual([row["path"] for row in manifest["files"]], sorted(on_disk))
                self.assertTrue(all(path.endswith(".py") for path in on_disk))
                self.assertEqual(registry.import_violations(on_disk), [])
                self.assertTrue(loaded.module.__name__.startswith("_episteme_pack_"))
                for hook in registry.HOOKS:
                    self.assertTrue(callable(loaded.hook(hook)))
                self.assertEqual(callable(getattr(loaded.module, "capture", None)),
                                 loaded.manifest.capture)

    def test_describe_and_compile_are_repeatable_valid_envelopes(self):
        for pack_id in _packs():
            with self.subTest(pack=pack_id):
                loaded, request, draft, plan = self.compiled(pack_id)
                catalog = loaded.hook("describe")()
                self.assertIsInstance(catalog, api.ParameterCatalog)
                self.assertEqual(catalog.canonical(), loaded.hook("describe")().canonical())
                self.assertEqual((catalog.pack_id, catalog.pack_version),
                                 (pack_id, loaded.pack_version))
                self.assertEqual(api.thaw(catalog.outputs), loaded.manifest.output_paths())
                catalog.validate_parameters(api.thaw(request.parameters))
                loaded.hook("validate_parameters")(request.parameters)
                self.assertIsInstance(draft, api.ProtocolDraft)
                self.assertIsInstance(plan, api.ExecutionPlan)
                again = self.compiled(pack_id)
                self.assertEqual(draft.digest(), again[2].digest())
                self.assertEqual(plan.digest(), again[3].digest())
                self.assertIn(draft.roster_semantics, loaded.manifest.roster_semantics)
                self.assertIn(plan.execution_profile, loaded.manifest.execution_profiles)
                self.assertEqual(api.thaw(plan.outputs), loaded.manifest.output_paths())
                design = draft.typed_design()
                self.assertEqual(loaded.manifest.metric(draft.metric)["unit"],
                                 design.primary_metric.unit)
                protocol = pack_fixtures.protocol_payload(draft, plan)
                loaded.hook("validate_protocol")(api.ProtocolContext(
                    parameters=request.parameters, draft=draft, execution_plan=plan.to_dict(),
                    capture=None if request.capture is None else request.capture.to_dict(),
                    protocol=protocol, protocol_hash="e" * 64))

    def test_analysis_hooks_are_repeatable_over_the_allowlist_only(self):
        for pack_id in _packs():
            with self.subTest(pack=pack_id):
                loaded, request, draft, plan = self.compiled(pack_id)
                slots, blobs = pack_fixtures.run_programs(plan, draft.roster,
                                                          self.root / f"{pack_id}-run")
                results = []
                for _ in range(2):
                    context, cas = pack_fixtures.analysis_inputs(loaded.manifest, request, draft,
                                                                 plan, slots, blobs)
                    checks = loaded.hook("validate_outputs")(context, cas)
                    recomputations = loaded.hook("recompute_metrics")(context, cas)
                    report = loaded.hook("analyse")(context, cas, tuple(checks),
                                                    tuple(recomputations))
                    results.append((canonical([check.to_dict() for check in checks]),
                                    canonical([row.to_dict() for row in recomputations]),
                                    report.canonical() if hasattr(report, "canonical")
                                    else canonical(report.to_dict()),
                                    report.statistical_report.canonical()))
                    self.assertTrue(set(cas.reads) <= set(cas.allowlist))
                self.assertEqual(results[0], results[1])
                self.assertEqual([check.slot for check in checks], [row["slot"] for row in slots])
                self.assertTrue(all(check.status == "passed" for check in checks))
                self.assertEqual(sorted(row.roster_unit for row in recomputations),
                                 sorted(draft.roster))
                self.assertIsInstance(report, api.AnalysisReport)
                self.assertEqual((report.pack_id, report.pack_version),
                                 (pack_id, loaded.pack_version))
                statistical = report.statistical_report
                statistical.validate_design(draft.typed_design())
                supplied = {name for name in api.STATISTICAL_FIELDS
                            if statistical.field(name)["status"] == "supplied"}
                self.assertLessEqual(supplied, set(loaded.manifest.statistical_capabilities))
                if "protocol_data" in loaded.manifest.hidden_inputs:
                    with self.assertRaisesRegex(api.PackAccessError, "hidden input"):
                        cas.read(plan.to_dict()["input"]["sha256"])

    def test_tampered_outputs_fail_pack_checks(self):
        for pack_id in _packs():
            with self.subTest(pack=pack_id):
                loaded, request, draft, plan = self.compiled(pack_id)
                slots, blobs = pack_fixtures.run_programs(plan, draft.roster,
                                                          self.root / f"{pack_id}-tamper")
                primary = next(row for row in slots if row["mode"] == "primary")
                metrics = blobs[primary["outputs"]["metrics"]]
                document = api.strict_loads(metrics)
                document[draft.metric] = float(document[draft.metric]) + 1.0
                forged = canonical(document)
                tampered = [dict(row, outputs=dict(row["outputs"], metrics=digest(forged)))
                            if row is primary else row for row in slots]
                context, cas = pack_fixtures.analysis_inputs(
                    loaded.manifest, request, draft, plan, tampered, {**blobs, digest(forged): forged})
                with self.assertRaises(ValueError):
                    loaded.hook("recompute_metrics")(context, cas)
                foreign = canonical({"unrelated": True})
                broken = [dict(row, outputs=dict(row["outputs"], raw_data=digest(foreign)))
                          if row is primary else row for row in slots]
                context, cas = pack_fixtures.analysis_inputs(
                    loaded.manifest, request, draft, plan, broken, {**blobs, digest(foreign): foreign})
                try:
                    checks = loaded.hook("validate_outputs")(context, cas)
                except ValueError:
                    continue
                self.assertTrue(any(check.status == "failed" for check in checks))


class LoaderTests(unittest.TestCase):
    """Pinning, drift and the import contract on throwaway pack copies."""

    def setUp(self):
        temporary = TemporaryDirectory(prefix="episteme-pack-loader-")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        sys.path.insert(0, str(self.parent))
        self.addCleanup(sys.path.remove, str(self.parent))

    def copy(self, pack_id: str) -> Path:
        target = self.parent / pack_id
        shutil.copytree(pack_fixtures.PACKS / pack_fixtures.FIXTURE_PACK, target,
                        ignore=shutil.ignore_patterns("__pycache__"))
        source = (target / "__init__.py").read_text(encoding="utf-8")
        (target / "__init__.py").write_text(
            source.replace(f'PACK_ID = "{pack_fixtures.FIXTURE_PACK}"', f'PACK_ID = "{pack_id}"'),
            encoding="utf-8", newline="\n")
        importlib.invalidate_caches()
        return target

    def load(self, pack_id: str) -> registry.LoadedPack:
        with patch.dict(registry.PACKS, {pack_id: pack_id}):
            return registry.load_pack(pack_id)

    def test_executed_code_is_the_hashed_bytes_and_drift_changes_the_pin(self):
        target = self.copy("drift_fixture_v1")
        first = self.load("drift_fixture_v1")
        self.assertEqual(first.module.PACK_ID, "drift_fixture_v1")
        source = (target / "__init__.py").read_bytes()
        (target / "__init__.py").write_bytes(source.replace(
            b'PACK_VERSION = "1"', b'PACK_VERSION = "1"  # edited after binding'))
        # The already loaded module keeps executing the bytes that were hashed.
        self.assertEqual(first.files["__init__.py"], source)
        second = self.load("drift_fixture_v1")
        self.assertNotEqual(first.pack_code_digest, second.pack_code_digest)
        self.assertNotEqual(first.module.__name__, second.module.__name__)
        with self.assertRaisesRegex(registry.PackError, "differs from the pinned"):
            second.require_pin(pack_id="drift_fixture_v1", pack_version="1",
                               pack_code_digest=first.pack_code_digest)
        with self.assertRaisesRegex(registry.PackError, "identity"):
            second.require_pin(pack_id="drift_fixture_v1", pack_version="2",
                               pack_code_digest=second.pack_code_digest)
        (target / "helper.py").write_bytes(b"VALUE = 1\n")
        self.assertNotEqual(self.load("drift_fixture_v1").pack_code_digest, second.pack_code_digest)

    def test_import_contract_and_malformed_packs_are_rejected(self):
        cases = {
            "private kernel": b"from episteme.kernel import Kernel\n",
            "store": b"import episteme.store\n",
            "relative escape": b"from ..kernel import Kernel\n",
            "randomness": b"import random\n",
            "environment": b"import os\n",
            "dynamic import": b"__import__('episteme.kernel')\n",
            "evaluation": b"eval('1')\n",
            "importlib": b"from importlib import import_module\n",
        }
        for label, source in cases.items():
            with self.subTest(case=label):
                self.assertTrue(registry.import_violations({"__init__.py": source}))
        self.assertEqual(registry.import_violations({"__init__.py": (
            b"import json\nimport hashlib\nfrom episteme.domains import api\n"
            b"from episteme.domains.api import supplied\nfrom . import helper\n")}), [])
        target = self.copy("private_fixture_v1")
        with (target / "__init__.py").open("ab") as stream:
            stream.write(b"\nfrom episteme.kernel import Kernel\n")
        with self.assertRaisesRegex(registry.PackError, "import contract"):
            self.load("private_fixture_v1")
        target = self.copy("data_fixture_v1")
        (target / "notes.txt").write_text("not code", encoding="utf-8")
        with self.assertRaisesRegex(registry.PackError, "Python modules"):
            self.load("data_fixture_v1")
        target = self.copy("hookless_fixture_v1")
        source = (target / "__init__.py").read_text(encoding="utf-8")
        (target / "__init__.py").write_text(source.replace("def analyse(", "def _analyse("),
                                            encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(registry.PackError, "lacks hooks: analyse"):
            self.load("hookless_fixture_v1")
        target = self.copy("renamed_fixture_v1")
        source = (target / "__init__.py").read_text(encoding="utf-8")
        (target / "__init__.py").write_text(
            source.replace('"pack_id": PACK_ID', '"pack_id": "someone_else_v1"'),
            encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(registry.PackError, "registry key"):
            self.load("renamed_fixture_v1")
        with self.assertRaisesRegex(registry.PackError, "explicit registry"):
            registry.load_pack("never_registered_v1")
        with self.assertRaisesRegex(registry.PackError, "invalid pack ID"):
            registry.load_pack("../escape")


if __name__ == "__main__":
    unittest.main()
