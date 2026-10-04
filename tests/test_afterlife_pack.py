"""afterlife_seed_v1 pack: read-only capture, pure compile, legacy equivalence, CLI path.

The historical run here is the tiny synthetic 30-output fixture of the ADR 0015
tests, not real Afterlife data. Agreement with the legacy manifest is a capture
precondition; the claim stays inconclusive/exploratory and no verdict is made.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import pack_fixtures
from episteme import domain_packs
from episteme.domains import afterlife_seed as legacy
from episteme.domains import api, registry
from episteme.graph import ResearchGraph
from episteme.kernel import Kernel
from episteme.search import Search
from episteme.store import Store, canonical, digest

from test_pack_workflow import ANALYST, PLANNER, REVIEWER, STUDY, planning, search_node


PACK = "afterlife_seed_v1"
REPO = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, PYTHONPATH=str(REPO / "src"))


def cli(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "episteme", *arguments], cwd=REPO, env=ENV,
                          capture_output=True, text=True, timeout=1200, check=check)


def source_bytes(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


class AfterlifePackTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="episteme-afterlife-pack-")
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        self.source = pack_fixtures.historical_run(self.temporary)
        self.loaded = registry.load_pack(PACK)

    def request(self) -> api.CompileRequest:
        capture = self.loaded.hook("capture")(self.source)
        return api.CompileRequest(parameters={}, host_inputs={}, capture=capture)

    def test_capture_and_compile_reproduce_the_legacy_bundle_without_touching_the_source(self):
        before = source_bytes(self.source)
        request = self.request()
        self.assertEqual(source_bytes(self.source), before)
        draft = self.loaded.hook("compile_protocol")(request)
        plan = self.loaded.hook("compile_execution")(request)
        with Store(self.temporary / "legacy") as store:
            old = legacy.compile_seed_bundle(store, self.source)
            self.assertEqual(plan.input, store.read(old["data"]))
            self.assertEqual(plan.primary_program, store.read(old["implementation"]))
            self.assertEqual(plan.reanalysis_program, store.read(old["reanalysis_implementation"]))
        self.assertEqual(set(request.capture.blobs()), set(old["recipe_artifacts"]))
        self.assertEqual(list(draft.seen_data), sorted({old["data"], *old["recipe_artifacts"]}))
        self.assertEqual((list(draft.roster), draft.roster_semantics, draft.sample_size_scope,
                          draft.run_limit, draft.replication_tolerance),
                         (list(range(9)), "frozen_unit_index", "total", 18, 0))
        self.assertEqual(api.thaw(plan.outputs), old["outputs"])
        self.assertEqual((plan.wall_seconds, plan.max_output_bytes),
                         (old["wall_seconds"], old["max_output_bytes"]))
        self.assertEqual(request.capture.source_label, "historical-nine-fixture")
        self.assertEqual(request.capture.audit["declared_outputs"], 30)

    def test_defective_runs_are_refused_by_capture_and_tampered_bytes_by_compile(self):
        original = source_bytes(self.source)
        changed = "data/trajectories.parquet"
        (self.source / changed).write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            self.loaded.hook("capture")(self.source)
        (self.source / changed).write_bytes(original[changed])
        (self.source / "data" / "unlisted.parquet").write_bytes(b"PAR1unlistedPAR1")
        with self.assertRaisesRegex(ValueError, "unlisted"):
            self.loaded.hook("capture")(self.source)
        (self.source / "data" / "unlisted.parquet").unlink()
        request = self.request()
        files = dict(request.capture.files)
        trace = next(name for name in files if name.endswith(".steps.jsonl"))
        files[trace] = files[trace].replace(b'"finish_reason":"length"', b'"finish_reason":"stop"', 1)
        forged = api.CaptureBundle(pack_id=PACK, pack_version="1", source_label="forged",
                                   files=files, audit={})
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            self.loaded.hook("compile_protocol")(api.CompileRequest(parameters={}, host_inputs={},
                                                                    capture=forged))
        with self.assertRaisesRegex(ValueError, "needs a capture"):
            self.loaded.hook("compile_protocol")(api.CompileRequest(parameters={}, host_inputs={},
                                                                    capture=None))
        with self.assertRaisesRegex(ValueError, "no host inputs"):
            self.loaded.hook("compile_protocol")(api.CompileRequest(
                parameters={}, host_inputs={"world": {}}, capture=request.capture))

    def test_cli_capture_bind_batch_analysis_and_verify(self):
        root = self.temporary / "state"
        before = source_bytes(self.source)
        inside = cli("pack", "capture", PACK, "--source", str(self.source),
                     "--root", str(self.source / "state"), check=False)
        self.assertNotEqual(inside.returncode, 0)
        self.assertFalse((self.source / "state").exists())
        captured = json.loads(cli("pack", "capture", PACK, "--source", str(self.source),
                                  "--root", str(root)).stdout)
        self.assertEqual(captured["events_written"], 0)
        with Store(root) as store:
            self.assertEqual(store.events(), [])
            explanation_set = planning(store)
            from episteme.execution import freeze_environment
            payload = dict(explanation_set=explanation_set, pack_id=PACK, pack_version="1",
                           pack_code_digest=self.loaded.pack_code_digest, parameters={},
                           host_inputs={}, capture=captured["capture"],
                           environment=freeze_environment(store))
            revision = len(store.events())
        envelope = dict(context=dict(command_id="afterlife-pack-bind", expected_revision=revision,
                                     actor=PLANNER.id, role=PLANNER.role, study_id=STUDY,
                                     correlation_id="afterlife-pack", causation_id=None),
                        request=dict(version=1, action="pack.preregister", payload=payload))
        path = self.temporary / "bind.json"
        path.write_text(json.dumps(envelope), encoding="utf-8")
        bound = json.loads(cli("command", "--input", str(path), "--root", str(root)).stdout)["result"]
        with Store(root) as store:
            protocol = Kernel._get(store.events(), bound["protocol"], "protocol")["payload"]
            self.assertEqual((protocol["protocol_mode"], protocol["seeds"]), ("exploratory", list(range(9))))
            self.assertIn(protocol["data"], protocol["seen_data"])
            binding = Kernel._get(store.events(), bound["binding"], "pack_binding")
            self.assertEqual(binding["payload"]["roster_semantics"], "frozen_unit_index")
            closure = {node.id for node in ResearchGraph.from_store(store).ancestors(bound["binding"])}
            for key in json.loads(store.read(captured["capture"]))["inventory"]:
                self.assertIn(ResearchGraph.artifact_id(key["sha256"]), closure)
            tree = search_node(store, bound["protocol"], 18)
            selection = Search(store, PLANNER).select_next(tree)["id"]
            fields = domain_packs.execution_fields(store, binding)
            revision = len(store.events())
        envelope["context"].update(command_id="afterlife-pack-plan", expected_revision=revision)
        envelope["request"].update(action="batch.plan", payload=dict(
            selection=selection, executor="pack-executor", replicator="pack-reanalyst", **fields))
        path.write_text(json.dumps(envelope), encoding="utf-8")
        batch = json.loads(cli("command", "--input", str(path), "--root", str(root)).stdout)["result"]
        self.assertEqual(json.loads(cli("batch", "advance", batch, "--root", str(root)).stdout)["status"],
                         "completed")
        result = json.loads(cli("analysis", "advance", batch, "--root", str(root), "--planner",
                                PLANNER.id, "--analyst", ANALYST.id, "--reviewer", REVIEWER).stdout)
        self.assertEqual((result["status"], result["scientific_validity"]),
                         ("awaiting_review", "not_assessed"))
        with Store(root, read_only=True) as store:
            history = store.events()
            claim = Kernel._get(history, result["claim"], "claim")["payload"]
            analysis = Kernel._get(history, result["analysis"], "pack_analysis")["payload"]
        self.assertEqual((claim["outcome"], claim["inference_mode"]), ("inconclusive", "exploratory"))
        self.assertIn("matched the legacy manifest for all nine", claim["statement"])
        self.assertIn("ranged from 0.0000 to 1.0000", claim["statement"])
        self.assertEqual(len(claim["limitations"]), 11)
        self.assertEqual([line.split(" was not supplied")[0] for line in claim["limitations"][8:10]],
                         ["Statistical report field confidence_interval",
                          "Statistical report field effect_size"])
        self.assertEqual(analysis["replication"]["roster_repetition"],
                         "distinct_frozen_units_without_new_randomness")
        self.assertEqual(analysis["hidden_digests"], [])
        verified = cli("pack", "verify", "--root", str(root))
        self.assertEqual(json.loads(verified.stdout)["status"], "matched")
        self.assertEqual(source_bytes(self.source), before)


if __name__ == "__main__":
    unittest.main()
