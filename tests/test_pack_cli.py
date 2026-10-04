"""CLI takes the pack from the protocol binding; describe and verify are read-only.

Synthetic fixtures through real subprocess CLI calls. Verification compares
canonical bytes of re-run hooks; it is mechanical, not a scientific review.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from episteme import domain_packs
from episteme.domains import registry
from episteme.kernel import Kernel
from episteme.search import Search
from episteme.store import Store

from test_pack_workflow import (ANALYST, PLANNER, REVIEWER, STUDY, batch_plan, drifted_pack,
                                planning, preregistration, search_node)


REPO = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, PYTHONPATH=str(REPO / "src"))


def cli(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "episteme", *arguments], cwd=REPO, env=ENV,
                          capture_output=True, text=True, timeout=600, check=check)


class PackCliTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="episteme-pack-cli-")
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        self.root = self.temporary / "state"

    def command(self, action: str, payload: dict, *, actor=PLANNER, name: str) -> dict:
        with Store(self.root) as store:
            revision = len(store.events())
        path = self.temporary / f"{name}.json"
        path.write_text(json.dumps(dict(
            context=dict(command_id=name, expected_revision=revision, actor=actor.id,
                         role=actor.role, study_id=STUDY, correlation_id="pack-cli",
                         causation_id=None),
            request=dict(version=1, action=action, payload=payload))), encoding="utf-8")
        return json.loads(cli("command", "--input", str(path), "--root", str(self.root)).stdout)["result"]

    def prepared(self) -> str:
        with Store(self.root) as store:
            payload = preregistration(store, planning(store))
        bound = self.command("pack.preregister", payload, name="preregister")
        with Store(self.root) as store:
            tree = search_node(store, bound["protocol"], 2)
            selection = Search(store, PLANNER).select_next(tree)["id"]
            binding = Kernel._get(store.events(), bound["binding"], "pack_binding")
            fields = domain_packs.execution_fields(store, binding)
        batch = self.command("batch.plan", dict(selection=selection, executor="pack-executor",
                                                replicator="pack-reanalyst", **fields), name="plan")
        self.assertEqual(json.loads(cli("batch", "advance", batch, "--root", str(self.root)).stdout)["status"],
                         "completed")
        return batch

    def test_describe_is_read_only_and_reports_the_pin(self):
        result = json.loads(cli("pack", "describe", "synthetic_causal_v1").stdout)
        loaded = registry.load_pack("synthetic_causal_v1")
        self.assertEqual(result["pack_code_digest"], loaded.pack_code_digest)
        self.assertEqual(result["manifest"], loaded.manifest.to_dict())
        self.assertEqual(result["catalog"]["pack_id"], "synthetic_causal_v1")
        self.assertEqual([row["path"] for row in result["code_manifest"]["files"]], sorted(loaded.files))
        self.assertEqual(result["registered"], sorted(registry.PACKS))
        unknown = cli("pack", "describe", "unregistered_pack_v1", check=False)
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("explicit registry", unknown.stderr)

    def test_analysis_advance_takes_the_pack_from_the_binding_and_verify_matches(self):
        batch = self.prepared()
        analysis = ["analysis", "advance", batch, "--root", str(self.root), "--planner", PLANNER.id,
                    "--analyst", ANALYST.id, "--reviewer", REVIEWER]
        with Store(self.root) as store:
            pending = store.export(), store.export_receipts()
        mismatch = cli(*analysis, "--adapter", "afterlife_seed_v1", check=False)
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("differs from the bound", mismatch.stderr)
        with Store(self.root) as store:
            self.assertEqual((store.export(), store.export_receipts()), pending)
        result = json.loads(cli(*analysis).stdout)
        self.assertEqual((result["status"], result["scientific_validity"]),
                         ("awaiting_review", "not_assessed"))
        self.assertEqual(json.loads(cli(*analysis, "--adapter", "synthetic_causal_v1").stdout), result)
        status = json.loads(cli("analysis", "status", batch, "--root", str(self.root)).stdout)
        self.assertEqual(status["status"], "awaiting_review")
        verified = cli("pack", "verify", "--root", str(self.root))
        report = json.loads(verified.stdout)
        self.assertEqual(verified.returncode, 0, verified.stdout)
        self.assertEqual(report["status"], "matched")
        self.assertEqual(len(report["bindings"]), 1)
        self.assertEqual(len(report["analyses"]), 1)
        self.assertTrue(all(value == "matched" for row in report["bindings"] + report["analyses"]
                            for key, value in row.items() if key in domain_packs.VERIFIED_PARTS))
        with Store(self.root) as store:
            unchanged = store.export(), store.export_receipts()
        with drifted_pack():
            with Store(self.root, read_only=True) as store:
                drifted = domain_packs.verify(store)
        self.assertEqual(drifted["status"], "mismatched")
        self.assertTrue(all(row["pin"].startswith("live pack code differs")
                            for row in drifted["bindings"] + drifted["analyses"]))
        with Store(self.root) as store:
            self.assertEqual((store.export(), store.export_receipts()), unchanged)


if __name__ == "__main__":
    unittest.main()
