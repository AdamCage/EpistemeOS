"""Pack hooks run in a subprocess and do not see the kernel store (ADR 0024).

The child is the same OS user. That is not a sandbox. These tests check the
boundary that was added: parent environment, store import, and the write lock.
Synthetic report bytes are a fixture comparison, not a scientific result.
"""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pack_fixtures
from test_pack_workflow import command, planning, preregistration
from test_synthetic_pack import legacy_context
import golden_support
from episteme import domain_packs
from episteme.domains import api, registry
from episteme.store import ConflictError, Store, canonical, digest


SECRET = b"CANARY-SECRET-DO-NOT-LEAK"
# Digests of the synthetic facade on the saved manual_binding history, before
# hooks left the kernel process. A change here is a changed report, not a review.
REPORT = "e6f849058bf95bae520e71c55fe9db7fc5f8b3b4cd052ce17d02493efd126621"
STATISTICAL = "f1c5cd8ae082b45ba399ad0d62b663480b083dd8841db05ae7461bce3685519e"
CHECKS = "98bbcac78c00bea654d41ac2efb8def12fd39f93bd04c7806d3fe4a168ab659a"
RECOMPUTATIONS = "0302ec9a38cc0950cab8c76294488b29a34c90d31af1e52f3dbeaf9719514bd6"
ESCAPE = "store_escape_v1"


class HookIsolationTests(unittest.TestCase):
    def test_synthetic_analysis_report_bytes_are_unchanged(self):
        loaded = registry.load_pack("synthetic_causal_v1")
        with TemporaryDirectory(prefix="episteme-hook-report-") as temporary:
            store = golden_support.load_history(golden_support.read_json("manual_binding.json"),
                                                Path(temporary) / "state")
            try:
                event = next(row for row in store.events() if row["kind"] == "batch_analysis")
                context, cas, _ = legacy_context(store, event["payload"]["batch"], loaded)
                checks = loaded.hook("validate_outputs")(context, cas)
                recomputations = loaded.hook("recompute_metrics")(context, cas)
                report = loaded.hook("analyse")(context, cas, tuple(checks), tuple(recomputations))
            finally:
                store.close()
        self.assertEqual(digest(canonical(report.to_dict())), REPORT)
        self.assertEqual(digest(canonical(report.statistical_report.to_dict())), STATISTICAL)
        self.assertEqual(digest(canonical([check.to_dict() for check in checks])), CHECKS)
        self.assertEqual(digest(canonical([row.to_dict() for row in recomputations])), RECOMPUTATIONS)
        self.assertEqual(report.outcome, "inconclusive")
        self.assertEqual(report.inference_mode, "exploratory")

    def test_escape_pack_cannot_read_a_parent_canary_or_import_the_store(self):
        self.assertNotIn(ESCAPE, registry.PACKS)
        self.assertTrue((pack_fixtures.PACKS / ESCAPE / "__init__.py").is_file())
        with TemporaryDirectory(prefix="episteme-hook-canary-") as temporary:
            path = Path(temporary) / "canary.bin"
            path.write_bytes(SECRET)
            os.environ["EPISTEME_CANARY_PATH"] = str(path)
            self.addCleanup(os.environ.pop, "EPISTEME_CANARY_PATH", None)
            with patch.dict(registry.PACKS, {**registry.PACKS, ESCAPE: ESCAPE}):
                registry._LOADED.pop(ESCAPE, None)
                self.addCleanup(registry._LOADED.pop, ESCAPE, None)
                loaded = registry.load_pack(ESCAPE)
                with self.assertRaises(registry.PackError) as caught:
                    loaded.hook("compile_protocol")(api.CompileRequest(
                        parameters={"scale": 1, "outcome": "inconclusive",
                                    "inference_mode": "descriptive"},
                        host_inputs={"groups": [[1]]}, capture=None))
            message = str(caught.exception)
            self.assertIn("stolen=b''", message)
            self.assertIn("store=ImportError", message)
            self.assertIn("sqlite=ImportError", message)
            self.assertIn("opened=False", message)
            self.assertNotIn(SECRET.decode(), message)
            self.assertEqual(path.read_bytes(), SECRET)
            source = Path(registry.__file__).read_text(encoding="utf-8")
            self.assertNotIn(f'"{ESCAPE}"', source)

    def test_escape_preregister_is_rejected_and_writes_nothing(self):
        with TemporaryDirectory(prefix="episteme-hook-escape-") as temporary:
            root = Path(temporary)
            canary = root / "canary.bin"
            canary.write_bytes(SECRET)
            os.environ["EPISTEME_CANARY_PATH"] = str(canary)
            self.addCleanup(os.environ.pop, "EPISTEME_CANARY_PATH", None)
            with Store(root / "state") as store, patch.dict(
                    registry.PACKS, {**registry.PACKS, ESCAPE: ESCAPE}):
                registry._LOADED.pop(ESCAPE, None)
                self.addCleanup(registry._LOADED.pop, ESCAPE, None)
                explanation = planning(store)
                before = [event["id"] for event in store.events()]
                loaded = registry.load_pack(ESCAPE)
                payload = preregistration(store, explanation, pack_id=ESCAPE,
                                          parameters={"scale": 1, "outcome": "inconclusive",
                                                      "inference_mode": "descriptive"},
                                          host={"groups": [[1]]})
                self.assertEqual(payload["pack_code_digest"], loaded.pack_code_digest)
                with self.assertRaises(registry.PackError) as caught:
                    command(store, "pack.preregister", payload)
                self.assertNotIn(SECRET.decode(), str(caught.exception))
                self.assertEqual([event["id"] for event in store.events()], before)
                self.assertFalse(any(event["kind"] == "pack_binding" for event in store.events()))
                self.assertEqual(canary.read_bytes(), SECRET)

    def test_append_between_compute_and_commit_retries_and_writes_nothing(self):
        with TemporaryDirectory(prefix="episteme-hook-race-") as temporary:
            root = Path(temporary) / "state"
            with Store(root) as store:
                explanation = planning(store)
                payload = preregistration(store, explanation)

                def interfere() -> None:
                    other = Store(root)
                    try:
                        other.append(
                            id="concurrent-hook-window", kind="hypothesis", actor="pack-planner",
                            role="planner", payload=dict(
                                statement="Fixture", prediction="Fixture", falsifier="Fixture",
                                scope={"population": "synthetic pack workflow fixture"}),
                            expected_revision=len(other.events()))
                    finally:
                        other.close()

                domain_packs.before_pack_commit = interfere
                self.addCleanup(setattr, domain_packs, "before_pack_commit", None)
                with self.assertRaises(ConflictError) as caught:
                    command(store, "pack.preregister", payload)
                self.assertIn("retry", str(caught.exception))
                self.assertIn("concurrent-hook-window", [event["id"] for event in store.events()])
                self.assertFalse(any(event["kind"] in {"protocol", "pack_binding"}
                                     for event in store.events()))
                self.assertFalse(any(row["request"]["action"] == "pack.preregister"
                                     for row in store.receipts()))


if __name__ == "__main__":
    unittest.main()
