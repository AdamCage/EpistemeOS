"""Local ownership fixtures; no payload execution or scientific approvals."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from episteme.execution import freeze_environment, job_state
from episteme.execution_authority import (
    AuthorityError, MARKER_NAME, establish_authority, require_authority,
)
from episteme.kernel import Actor, Kernel
from episteme.recovery import backup, restore
from episteme.store import Store, canonical


class ExecutionAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "original"
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.marker = self.root / MARKER_NAME

    def test_creation_is_stable_and_does_not_change_scientific_state(self):
        before = self.store.export(), self.store.export_receipts()
        authority = establish_authority(self.store)
        original = self.marker.read_bytes()
        self.assertRegex(authority, r"^[0-9a-f]{64}$")
        self.assertEqual(establish_authority(self.store), authority)
        self.assertIsNone(require_authority(self.store, authority))
        self.assertEqual(self.marker.read_bytes(), original)
        self.assertEqual((self.store.export(), self.store.export_receipts()), before)
        self.assertEqual(list(self.store.blobs.iterdir()), [])
        self.assertEqual(list(self.root.glob(".execution-authority-*")), [])

    def test_require_missing_authority_is_read_only_and_never_creates_it(self):
        before = {path.name for path in self.root.iterdir()}
        with self.assertRaisesRegex(AuthorityError, "missing"):
            require_authority(self.store, "0" * 64)
        self.assertFalse(self.marker.exists())
        self.assertEqual({path.name for path in self.root.iterdir()}, before)

    def test_matching_check_works_on_read_only_store_without_rewriting(self):
        authority = establish_authority(self.store)
        before = self.marker.read_bytes(), self.marker.stat().st_mtime_ns
        with Store(self.root, read_only=True) as reader:
            require_authority(reader, authority)
            with self.assertRaisesRegex(AuthorityError, "read-only"):
                establish_authority(reader)
        self.assertEqual((self.marker.read_bytes(), self.marker.stat().st_mtime_ns), before)

    def test_wrong_or_malformed_digest_cannot_change_marker(self):
        authority = establish_authority(self.store)
        before = self.marker.read_bytes()
        for value in ("0" * 64, "invalid", "A" * 64, None, True, b"0" * 64):
            with self.subTest(value=value), self.assertRaises(AuthorityError):
                require_authority(self.store, value)
            self.assertEqual(self.marker.read_bytes(), before)
        self.assertEqual(establish_authority(self.store), authority)

    def test_corrupt_existing_markers_are_not_replaced(self):
        malformed = [b"", b"{}", b"x" * 513,
                     canonical(dict(schema_version=True, token="1" * 64)),
                     canonical(dict(schema_version=2, token="1" * 64)),
                     canonical(dict(schema_version=1, token="not-a-token")),
                     canonical(dict(schema_version=1, token="1" * 64, extra="unexpected")),
                     canonical(dict(schema_version=1, token="1" * 64)) + b"\n",
                     b'{"schema_version":1,"token":"' + b"1" * 64 + b'","token":"' + b"2" * 64 + b'"}']
        for data in malformed:
            self.marker.write_bytes(data)
            with self.subTest(data=data):
                with self.assertRaises(AuthorityError):
                    establish_authority(self.store)
                with self.assertRaises(AuthorityError):
                    require_authority(self.store, "0" * 64)
                self.assertEqual(self.marker.read_bytes(), data)
                self.assertEqual(list(self.root.glob(".execution-authority-*")), [])

    def test_unsafe_directory_marker_is_rejected_without_mutation(self):
        self.marker.mkdir()
        with self.assertRaisesRegex(AuthorityError, "plain file"):
            establish_authority(self.store)
        with self.assertRaisesRegex(AuthorityError, "plain file"):
            require_authority(self.store, "0" * 64)
        self.assertTrue(self.marker.is_dir())
        self.assertEqual(list(self.marker.iterdir()), [])

    def test_symlink_marker_and_broken_symlink_are_rejected(self):
        target = self.base / "external-marker.json"
        target.write_bytes(canonical(dict(schema_version=1, token="1" * 64)))
        try:
            self.marker.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        for broken in (False, True):
            if broken:
                target.unlink()
            with self.subTest(broken=broken):
                with self.assertRaisesRegex(AuthorityError, "plain file"):
                    establish_authority(self.store)
                with self.assertRaisesRegex(AuthorityError, "plain file"):
                    require_authority(self.store, "0" * 64)
                self.assertTrue(self.marker.is_symlink())

    def test_concurrent_creators_observe_one_complete_token(self):
        barrier = threading.Barrier(8, timeout=10)

        def create(_):
            barrier.wait()
            # The helper accesses only root/read_only, never this SQLite connection.
            return establish_authority(self.store)

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(create, range(8)))
        self.assertEqual(len(set(results)), 1)
        require_authority(self.store, results[0])
        self.assertEqual(list(self.root.glob(".execution-authority-*")), [])

    def test_failed_publication_leaves_no_marker_or_temporary_authority(self):
        with patch("episteme.execution_authority.os.link", side_effect=OSError("publication unavailable")):
            with self.assertRaisesRegex(AuthorityError, "atomically publish"):
                establish_authority(self.store)
        self.assertFalse(self.marker.exists())
        self.assertEqual(list(self.root.glob(".execution-authority-*")), [])
        authority = establish_authority(self.store)
        require_authority(self.store, authority)

    def test_database_and_cas_restore_excludes_local_authority(self):
        authority = establish_authority(self.store)
        snapshot, restored_root = self.base / "snapshot", self.base / "restored"
        backup(self.store, snapshot)
        self.assertFalse((snapshot / MARKER_NAME).exists())
        restore(snapshot, restored_root)
        self.assertFalse((restored_root / MARKER_NAME).exists())
        with Store(restored_root) as restored:
            with self.assertRaisesRegex(AuthorityError, "missing"):
                require_authority(restored, authority)
            replacement = establish_authority(restored)
            self.assertNotEqual(replacement, authority)
            with self.assertRaisesRegex(AuthorityError, "differs"):
                require_authority(restored, authority)
        require_authority(self.store, authority)

    def test_stale_queued_snapshot_cannot_adopt_original_execution_authority(self):
        from episteme.commands import CommandService

        authority = establish_authority(self.store)
        planner = Kernel(self.store, Actor("fixture-planner", "planner"))
        scope = {"mode": "authority-fixture"}
        hypotheses = [planner.hypothesis(name, "fixture prediction", "fixture falsifier", scope)
                      for name in ("fixture effect", "fixture null")]
        source = self.store.put(b"# Never executed by this ownership test.\n")
        environment = freeze_environment(self.store)
        protocol = planner.preregister(hypotheses=hypotheses, scope=scope,
            design="ownership fixture", metric="value", analysis_plan="fixture declaration",
            stopping_rule="one primary and one possible reanalysis", seeds=[1], run_limit=2, implementation=source,
            environment=environment, data=self.store.put_json({"value": 1}), replication_tolerance=0)

        def command(action, payload):
            return CommandService(self.store).execute(dict(context=dict(command_id=uuid4().hex,
                expected_revision=len(self.store.events()), actor="fixture-executor", role="executor",
                study_id="fixture-study", correlation_id="authority-fixture", causation_id=None),
                request=dict(version=1, action=action, payload=payload)))

        job = command("execution.enqueue", dict(protocol=protocol, seed=1,
            outputs={"raw_data": "raw.bin", "metrics": "metrics.json"},
            wall_seconds=1, max_output_bytes=1024))
        snapshot, restored_root = self.base / "queued-snapshot", self.base / "stale-restored"
        backup(self.store, snapshot)
        # The original history advances after its queued snapshot. No process is spawned.
        command("execution.dispatch", dict(job=job, workspace_token=uuid4().hex))
        self.assertEqual(job_state(self.store, job)["status"], "unknown")
        restore(snapshot, restored_root)
        with Store(restored_root) as restored:
            self.assertEqual(job_state(restored, job)["status"], "queued")
            with self.assertRaises(AuthorityError):
                require_authority(restored, authority)
            establish_authority(restored)
            with self.assertRaisesRegex(AuthorityError, "differs"):
                require_authority(restored, authority)
        require_authority(self.store, authority)


if __name__ == "__main__":
    unittest.main()
