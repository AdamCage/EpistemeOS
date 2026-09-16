"""Recovery snapshot, publication, and adversarial metadata invariants."""

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from threading import Barrier
import unittest
from unittest.mock import patch

from tests import test_recovery as fixtures
from episteme import recovery
from episteme.commands import CommandService
from episteme.store import IntegrityError, Store, canonical, digest


class RecoveryConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RecoveryTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_append_after_database_snapshot_cannot_mix_event_and_receipt_revisions(self):
        f = self.fixture
        original = recovery._database_backup
        old_events, old_receipts = f.store.export(), f.store.export_receipts()
        late_blob = None

        def append_after_snapshot(store, path):
            nonlocal late_blob
            original(store, path)
            with Store(f.source) as writer:
                late_blob = writer.put(b"Late append-only CAS fixture")
                command = f.command()
                command["context"].update(command_id="late-command", expected_revision=1)
                CommandService(writer).execute(command)

        snapshot, target = f.root / "snapshot", f.root / "target"
        with patch.object(recovery, "_database_backup", side_effect=append_after_snapshot):
            manifest = recovery.backup(f.store, snapshot)
        self.assertEqual(len(f.store.events()), 2)
        self.assertEqual(manifest["revision"], 1)
        self.assertEqual(manifest["receipt_count"], 1)
        recovery.restore(snapshot, target)
        with Store(target, read_only=True) as store:
            self.assertEqual(store.export(), old_events)
            self.assertEqual(store.export_receipts(), old_receipts)
            self.assertEqual(store.read(late_blob), b"Late append-only CAS fixture")

    def test_two_restores_reserve_one_destination_without_overwriting(self):
        f = self.fixture
        snapshot, target = f.root / "snapshot", f.root / "target"
        recovery.backup(f.store, snapshot)
        barrier, publish = Barrier(2), recovery._publish

        def simultaneous(stage, destination, *, snapshot):
            barrier.wait(timeout=15)
            publish(stage, destination, snapshot=snapshot)

        def attempt():
            try:
                recovery.restore(snapshot, target)
                return "restored"
            except FileExistsError:
                return "conflict"

        with patch.object(recovery, "_publish", side_effect=simultaneous), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(attempt) for _ in range(2)]
            self.assertCountEqual([future.result(timeout=25) for future in futures], ["restored", "conflict"])
        with Store(target, read_only=True) as store:
            self.assertEqual(store.export(), f.store.export())
            self.assertEqual(store.export_receipts(), f.store.export_receipts())

    def test_manifest_paths_duplicates_and_unknown_members_fail_before_destination(self):
        f = self.fixture
        for case in ("traversal", "duplicate", "extra", "version"):
            with self.subTest(case=case):
                snapshot, target = f.root / f"snapshot-{case}", f.root / f"target-{case}"
                manifest = recovery.backup(f.store, snapshot)
                path = snapshot / "manifest.json"
                if case == "traversal":
                    manifest["artifacts"]["../../outside"] = 0
                elif case == "duplicate":
                    path.write_bytes(b'{"version":1,' + path.read_bytes()[1:])
                elif case == "extra":
                    (snapshot / "unexpected").write_text("Fixture")
                else:
                    manifest["version"] = 2
                if case in {"traversal", "version"}:
                    path.write_bytes(canonical(manifest))
                with self.assertRaises(IntegrityError):
                    recovery.restore(snapshot, target)
                self.assertFalse(target.exists())
        self.assertFalse((f.root / "outside").exists())

    def test_rehashed_database_cannot_hide_invalid_receipt_event_binding(self):
        f = self.fixture
        snapshot, target = f.root / "snapshot", f.root / "target"
        manifest = recovery.backup(f.store, snapshot)
        database = snapshot / "state.sqlite3"
        connection = sqlite3.connect(database)
        try:
            receipt = json.loads(connection.execute("SELECT receipt FROM command_receipts").fetchone()[0])
            receipt["context"]["actor"] = "incorrect-event-actor"
            receipt["request_hash"] = digest(canonical(dict(context=receipt["context"], request=receipt["request"])))
            connection.execute("DROP TRIGGER command_receipts_no_update")
            connection.execute("UPDATE command_receipts SET receipt=?, hash=?",
                               (canonical(receipt).decode(), digest(canonical(receipt))))
            connection.commit()
        finally:
            connection.close()
        manifest["database"] = dict(sha256=digest(database.read_bytes()), size=database.stat().st_size)
        (snapshot / "manifest.json").write_bytes(canonical(manifest))
        with self.assertRaisesRegex(IntegrityError, "receipt event binding"):
            recovery.restore(snapshot, target)
        self.assertFalse(target.exists())
