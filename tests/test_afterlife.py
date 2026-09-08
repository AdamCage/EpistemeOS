"""Offline historical imports are provenance observations, never scientific approvals."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from episteme.domains.afterlife import AfterlifeSnapshot, import_snapshot, inspect
from episteme.store import IntegrityError, Store, canonical, digest


class AfterlifeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="episteme-afterlife-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.close)
        self.git = patch("episteme.domains.afterlife._git", return_value={
            "sha": "1" * 40, "branch": "stage-2", "dirty": False, "status": ""})
        self.git.start()
        self.addCleanup(self.git.stop)

    def write(self, path, data):
        target = self.source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data if isinstance(data, bytes) else data.encode())
        return target

    def run_fixture(self, run_id="mock-run", status="COMPLETED", **changes):
        base = f"runs/s0/{run_id}"
        output = b"step,topic\n1,physics\n2,love\n"
        config = b"execution_mode: mock\n"
        self.write(f"{base}/data/result.csv", output)
        self.write(f"{base}/config.resolved.yaml", config)
        manifest = dict(run_id=run_id, stage="s0", status=status, command="DO NOT EXECUTE THIS COMMAND",
                        execution_mode="mock", git={"sha": "a" * 40, "dirty": True,
                                                    "status": " M experiment.py\n?? untracked.py",
                                                    "diff": "historical patch"},
                        config_resolved={"source_run_id": "upstream"}, config_sha256="c" * 64,
                        integrity={"data/result.csv": digest(output), "config.resolved.yaml": digest(config)},
                        notes=["Synthetic fixture; not scientific evidence"])
        manifest.update(changes)
        self.write(f"{base}/manifest.json", canonical(manifest))
        self.write(f"{base}/STATUS", status)
        return manifest

    def file_snapshot(self):
        return {path.relative_to(self.source).as_posix(): (digest(path.read_bytes()), path.stat().st_mtime_ns)
                for path in self.source.rglob("*") if path.is_file()}

    def test_import_is_read_only_frozen_idempotent_and_never_promotes_history(self):
        self.run_fixture()
        self.run_fixture("failed", "FAILED", integrity={}, notes=["negative finding retained"])
        self.run_fixture("old", superseded={"superseded_by": "new", "reason": "degeneracy omitted"})
        self.write("runs/s0/old/SUPERSEDED", "superseded_by: new\nreason: degeneracy omitted\n")
        self.write("docs/stages/stage-1/PLAN.md", "# PLAN\nPre-registered predictions: A\n")
        self.write("docs/stages/stage-1/REPORT.md", "# REPORT\nNegative finding: no half-life observed.\n")
        self.write("docs/decisions/ADR-0008.md", "A plateau requires a mechanism-control experiment.\n")
        self.write("artifacts/stage-0/example.meta.json", canonical({"name": "example", "run_ids": ["mock-run"],
                   "caption": "Fixture", "limitations": "No scientific meaning"}))
        self.write("artifacts/stage-0/example.data.parquet", b"small fixture data")
        before = self.file_snapshot()
        snapshot = inspect(self.source)
        self.assertEqual(snapshot.id, inspect(self.source).id)
        result = import_snapshot(self.store, snapshot)
        self.assertTrue(result["created"])
        self.assertEqual((result["runs"], result["superseded_runs"], result["dirty_runs"]), (3, 1, 3))
        self.assertEqual(result["run_statuses"], {"COMPLETED": 2, "FAILED": 1})
        self.assertEqual(result["trust"], "historical_unverified")
        self.assertEqual(before, self.file_snapshot())
        self.assertFalse(import_snapshot(self.store, snapshot)["created"])
        with Store(self.root / "state") as reopened:
            self.assertFalse(import_snapshot(reopened, snapshot)["created"])
            self.assertEqual(len(reopened.events()), 1)
            event = reopened.events()[0]
            self.assertEqual(event["kind"], "afterlife_snapshot")
            self.assertEqual(reopened.read(event["payload"]["snapshot"]), snapshot.content)
        inventory = snapshot.inventory
        runs = {run["legacy_run_id"]: run for run in inventory["runs"]}
        self.assertEqual(runs["failed"]["integrity_status"], "historical_unverified")
        self.assertEqual(runs["old"]["supersession"]["reason"], "degeneracy omitted")
        self.assertEqual(runs["mock-run"]["source_run_id"], "upstream")
        plan = next(document for document in inventory["documents"] if document["kind"] == "imported_protocol")
        self.assertFalse(plan["preregistered"])
        self.assertIn(b"Pre-registered predictions", self.store.read(plan["blob"]))
        self.assertEqual(inventory["artifacts"][0]["limitations"], "No scientific meaning")
        self.assertIsNone(inventory["artifacts"][0]["companions"][0]["blob"])
        self.assertFalse(any(event["kind"] in {"protocol", "claim", "review", "run"} for event in self.store.events()))

    def test_hash_mismatch_missing_empty_and_unsafe_references_remain_visible(self):
        self.run_fixture(integrity={"data/result.csv": "0" * 64, "data/missing.csv": "1" * 64,
                                   "../../outside": "2" * 64, "C:\\outside.txt": "3" * 64,
                                   "data/malformed": "not-sha256"})
        snapshot = inspect(self.source)
        run = snapshot.inventory["runs"][0]
        self.assertEqual(run["integrity_status"], "integrity_mismatch")
        checks = {row["path"]: row["verification"] for row in run["outputs"]}
        self.assertEqual(checks, {"data/result.csv": "mismatch", "data/missing.csv": "missing",
                                 "../../outside": "invalid_reference", "C:\\outside.txt": "invalid_reference",
                                 "data/malformed": "invalid_expected_hash"})
        import_snapshot(self.store, snapshot)
        self.assertEqual(snapshot.report()["scientific_validity"], "not_assessed")

    def test_output_limits_do_not_copy_large_outputs_and_small_fixture_copy_is_explicit(self):
        self.run_fixture()
        ordinary = inspect(self.source)
        output = next(row for row in ordinary.inventory["runs"][0]["outputs"] if row["path"].endswith("csv"))
        self.assertEqual(output["verification"], "matched")
        self.assertIsNone(output["blob"])
        limited = inspect(self.source, max_verify_file_bytes=8)
        self.assertEqual(limited.report()["output_checks"], {"verification_size_limit": 2})
        budget = inspect(self.source, max_verify_total_bytes=1)
        self.assertEqual(budget.report()["output_checks"], {"verification_budget_exhausted": 2})
        copied = inspect(self.source, copy_outputs_under_bytes=100)
        import_snapshot(self.store, copied)
        outputs = copied.inventory["runs"][0]["outputs"]
        for output in outputs:
            self.assertEqual(digest(self.store.read(output["blob"])), output["actual_sha256"])

    def test_captured_snapshot_survives_source_change_and_new_bytes_create_new_revision(self):
        manifest = self.run_fixture()
        original = inspect(self.source, copy_outputs_under_bytes=100)
        manifest["notes"].append("Later amendment")
        self.write("runs/s0/mock-run/manifest.json", canonical(manifest))
        self.write("runs/s0/mock-run/data/result.csv", "changed result")
        newer = inspect(self.source, copy_outputs_under_bytes=100)
        self.assertNotEqual(original.id, newer.id)
        import_snapshot(self.store, original)
        import_snapshot(self.store, newer)
        self.assertEqual(len(self.store.events()), 2)
        old_output = next(row for row in original.inventory["runs"][0]["outputs"] if row["path"].endswith("csv"))
        self.assertIn(b"physics", self.store.read(old_output["blob"]))
        self.assertEqual(newer.inventory["runs"][0]["integrity_status"], "integrity_mismatch")
        inventory = original.inventory
        inventory["runs"].clear()
        self.assertEqual(original.report()["runs"], 1)

    def test_malformed_manifest_and_capture_limits_do_not_manufacture_success(self):
        self.write("runs/s1/broken/manifest.json", '{"status":NaN}')
        self.write("runs/s1/array/manifest.json", '[]')
        self.write("runs/s1/oversize/manifest.json", '{"notes":"' + "x" * 100 + '"}')
        snapshot = inspect(self.source, max_metadata_bytes=50, max_capture_bytes=5)
        self.assertEqual(snapshot.report()["run_statuses"], {"UNKNOWN": 3})
        self.assertEqual(snapshot.report()["run_integrity"], {"historical_unverified": 3})
        self.assertLessEqual(snapshot.report()["captured_bytes"], 5)
        self.assertGreater(snapshot.report()["warnings"], 0)
        import_snapshot(self.store, snapshot)

    def test_corrupted_snapshot_or_stored_blob_prevents_idempotent_success(self):
        self.run_fixture()
        snapshot = inspect(self.source)
        corrupted = AfterlifeSnapshot(snapshot.content, ((snapshot.blobs[0][0], b"bad"), *snapshot.blobs[1:]))
        with self.assertRaises(IntegrityError):
            import_snapshot(self.store, corrupted)
        self.assertEqual(self.store.events(), [])
        missing = AfterlifeSnapshot(snapshot.content, snapshot.blobs[1:])
        with self.assertRaises(IntegrityError):
            import_snapshot(self.store, missing)
        import_snapshot(self.store, snapshot)
        (self.store.blobs / snapshot.blobs[0][0]).write_bytes(b"corrupted")
        with self.assertRaises(IntegrityError):
            import_snapshot(self.store, snapshot)

    def test_import_destination_inside_source_is_rejected(self):
        self.run_fixture()
        snapshot = inspect(self.source)
        with Store(self.source / "bad-destination") as nested:
            with self.assertRaisesRegex(ValueError, "outside"):
                import_snapshot(nested, snapshot)
            self.assertEqual(nested.events(), [])

    def test_symlinks_are_not_followed(self):
        self.run_fixture()
        outside = self.root / "outside.txt"
        outside.write_text("not in source")
        link = self.source / "runs/s0/mock-run/data/link"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("This host does not grant symlink creation")
        path = self.source / "runs/s0/mock-run/manifest.json"
        manifest = json.loads(path.read_bytes())
        manifest["integrity"]["data/link"] = digest(outside.read_bytes())
        path.write_bytes(canonical(manifest))
        snapshot = inspect(self.source)
        output = next(row for row in snapshot.inventory["runs"][0]["outputs"] if row["path"] == "data/link")
        self.assertEqual(output["verification"], "unsafe_or_unreadable")

    def test_git_observations_preserve_dirty_and_do_not_run_legacy_commands(self):
        self.git.stop()
        self.run_fixture()
        def observe(args, **kwargs):
            self.assertEqual(args[0], "git")
            self.assertIn("--no-optional-locks", args)
            if "status" in args:
                output = b" M tracked.py\n?? untracked.py\n"
            elif "--abbrev-ref" in args:
                output = b"stage-2\n"
            else:
                output = b"a" * 40 + b"\n"
            return subprocess.CompletedProcess(args, 0, output, b"")
        with patch("episteme.domains.afterlife.subprocess.run", side_effect=observe):
            snapshot = inspect(self.source)
        self.assertTrue(snapshot.report()["source_dirty"])
        self.assertIn("untracked.py", snapshot.inventory["source"]["git"]["status"])
        self.assertEqual(snapshot.inventory["source"]["source_closure"], "not_established")


if __name__ == "__main__":
    unittest.main()
