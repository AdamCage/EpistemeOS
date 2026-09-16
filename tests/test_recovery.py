"""Coherent backup/restore contracts; no experiments or external services."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from episteme.commands import CommandService
from episteme.graph import ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.recovery import backup, restore
from episteme.store import ConflictError, Store


PROJECT = Path(__file__).resolve().parents[1]


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="episteme-recovery-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source"
        self.store = Store(self.source)
        self.addCleanup(self.store.close)
        self.envelope = self.command()
        self.command_result = CommandService(self.store).execute(self.envelope)
        # A CAS blob not cited by an event must still survive and be verified.
        self.blob_bytes = b"Unreferenced immutable recovery fixture\n"
        self.blob = self.store.put(self.blob_bytes)

    @staticmethod
    def command():
        return dict(context=dict(command_id="recovery-command-1", expected_revision=0,
                    actor="fixture-planner", role="planner", study_id="recovery-fixture",
                    correlation_id="recovery-cycle", causation_id=None),
                    request=dict(version=1, action="kernel.hypothesis", payload=dict(
                        statement="Synthetic recovery fixture", prediction="A persisted ID is stable",
                        falsifier="Replay changes the recorded ID", scope={"mode": "recovery_fixture"})))

    def test_round_trip_preserves_events_receipts_graph_and_stale_command_replay(self):
        Kernel(self.store, Actor("fixture-planner", "planner")).hypothesis(
            "A second fixture", "Another observation", "Contrary observation", {"mode": "recovery_fixture"})
        events, receipts = self.store.export(), self.store.export_receipts()
        graph = ResearchGraph.from_store(self.store).to_dict()
        snapshot, destination = self.root / "snapshot", self.root / "restored"
        self.assertIsInstance(backup(self.store, snapshot), dict)
        self.assertTrue((snapshot / "state.sqlite3").is_file())
        self.assertIsInstance(json.loads((snapshot / "manifest.json").read_text(encoding="utf-8")), dict)
        self.assertIsInstance(restore(snapshot, destination), dict)
        with Store(destination) as recovered:
            self.assertEqual(recovered.export(), events)
            self.assertEqual(recovered.export_receipts(), receipts)
            self.assertEqual(ResearchGraph.from_store(recovered).to_dict(), graph)
            self.assertEqual(CommandService(recovered).execute(self.envelope), self.command_result)
            changed = json.loads(json.dumps(self.envelope))
            changed["request"]["payload"]["statement"] = "Changed body under the original command ID"
            with self.assertRaises(ConflictError):
                CommandService(recovered).execute(changed)
            self.assertEqual(recovered.export(), events)
            self.assertEqual(recovered.export_receipts(), receipts)
        self.assertEqual(self.store.export(), events)
        self.assertEqual(self.store.export_receipts(), receipts)

    def test_all_cas_blobs_including_orphans_survive_backup_and_restore(self):
        second_bytes = b"A second orphan from an interrupted preparation"
        second = self.store.put(second_bytes)
        snapshot, destination = self.root / "snapshot", self.root / "restored"
        backup(self.store, snapshot)
        for key, data in ((self.blob, self.blob_bytes), (second, second_bytes)):
            self.assertEqual((snapshot / "artifacts" / "sha256" / key).read_bytes(), data)
        restore(snapshot, destination)
        with Store(destination, read_only=True) as recovered:
            self.assertEqual(recovered.read(self.blob), self.blob_bytes)
            self.assertEqual(recovered.read(second), second_bytes)
            self.assertEqual({path.name for path in recovered.blobs.iterdir()}, {self.blob, second})

    def test_restore_rejects_corrupt_database_or_orphan_blob_or_manifest_without_publishing(self):
        for kind in ("database", "orphan", "manifest"):
            with self.subTest(kind=kind):
                snapshot = self.root / f"snapshot-{kind}"
                destination = self.root / f"restored-{kind}"
                backup(self.store, snapshot)
                if kind == "database":
                    path = snapshot / "state.sqlite3"
                    original = path.read_bytes()
                    path.write_bytes(b"X" + original[1:])  # Same size; digest must still change.
                elif kind == "orphan":
                    path = snapshot / "artifacts" / "sha256" / self.blob
                    path.write_bytes(b"X" + self.blob_bytes[1:])
                else:
                    (snapshot / "manifest.json").write_text("{broken JSON", encoding="utf-8")
                with self.assertRaises((ValueError, OSError)):
                    restore(snapshot, destination)
                self.assertFalse(destination.exists())
        self.assertEqual(self.store.read(self.blob), self.blob_bytes)

    def test_backup_rejects_invalid_receipts_and_graph_despite_well_formed_sqlite(self):
        for kind in ("receipt", "graph"):
            with self.subTest(kind=kind):
                source = self.root / f"invalid-source-{kind}"
                destination = self.root / f"invalid-snapshot-{kind}"
                with Store(source) as store:
                    CommandService(store).execute(self.command())
                    if kind == "receipt":
                        store.db.execute("DROP TRIGGER command_receipts_no_update")
                        store.db.execute("UPDATE command_receipts SET hash = ?", ("f" * 64,))
                        store.db.commit()
                    else:
                        store.append(id="dangling-claim", kind="claim", actor="fixture-analyst",
                            role="analyst", expected_revision=1,
                            payload=dict(protocol="missing-protocol", statement="Fixture only",
                                         scope={"mode": "recovery_fixture"}, evidence=[],
                                         limitations=["Synthetic invalid reference"], outcome="inconclusive"))
                        self.assertEqual(len(store.events()), 2)  # Valid chain, invalid typed reference.
                    with self.assertRaises((ValueError, OSError)):
                        backup(store, destination)
                    self.assertFalse(destination.exists())

    def test_existing_file_empty_directory_and_nonempty_directory_are_never_overlaid(self):
        snapshot = self.root / "snapshot"
        backup(self.store, snapshot)
        for operation in ("backup", "restore"):
            for kind in ("file", "empty-directory", "nonempty-directory"):
                with self.subTest(operation=operation, kind=kind):
                    target = self.root / f"existing-{operation}-{kind}"
                    marker = b"Pre-existing user data must remain byte-identical"
                    if kind == "file":
                        target.write_bytes(marker)
                    else:
                        target.mkdir()
                        if kind == "nonempty-directory":
                            (target / "keep.txt").write_bytes(marker)
                    with self.assertRaises((ValueError, OSError)):
                        if operation == "backup":
                            backup(self.store, target)
                        else:
                            restore(snapshot, target)
                    if kind == "file":
                        self.assertEqual(target.read_bytes(), marker)
                    elif kind == "empty-directory":
                        self.assertEqual(list(target.iterdir()), [])
                    else:
                        self.assertEqual((target / "keep.txt").read_bytes(), marker)
                        self.assertEqual({path.name for path in target.iterdir()}, {"keep.txt"})

    def test_broken_symlink_destinations_are_existing_paths_and_remain_untouched(self):
        missing = self.root / "missing-symlink-target"
        backup_target = self.root / "backup-link"
        try:
            backup_target.symlink_to(missing, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("This host does not grant symlink creation")
        restore_target = self.root / "restore-link"
        restore_target.symlink_to(missing, target_is_directory=True)
        snapshot = self.root / "snapshot"
        backup(self.store, snapshot)
        for operation, target in (("backup", backup_target), ("restore", restore_target)):
            with self.subTest(operation=operation):
                with self.assertRaises((ValueError, OSError)):
                    backup(self.store, target) if operation == "backup" else restore(snapshot, target)
                self.assertTrue(target.is_symlink())
                self.assertEqual(target.readlink(), missing)
                self.assertFalse(missing.exists())

    def test_backup_rejects_active_transaction_without_committing_or_rolling_it_back(self):
        destination = self.root / "snapshot"
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises((ValueError, OSError)):
                backup(self.store, destination)
            self.assertTrue(self.store.db.in_transaction)
            self.assertFalse(destination.exists())
        finally:
            self.store.db.rollback()
        self.assertEqual(len(self.store.events()), 1)
        self.assertEqual(len(self.store.receipts()), 1)

    def test_source_destination_containment_is_rejected_before_any_snapshot_is_published(self):
        nested_snapshot = self.source / "nested-snapshot"
        with self.assertRaises((ValueError, OSError)):
            backup(self.store, nested_snapshot)
        self.assertFalse(nested_snapshot.exists())
        snapshot = self.root / "snapshot"
        backup(self.store, snapshot)
        nested_restore = snapshot / "nested-restored"
        with self.assertRaises((ValueError, OSError)):
            restore(snapshot, nested_restore)
        self.assertFalse(nested_restore.exists())
        self.assertEqual(CommandService(self.store).execute(self.envelope), self.command_result)

    def test_real_cli_backup_restore_produces_replayable_state(self):
        snapshot, destination = self.root / "cli-snapshot", self.root / "cli-restored"
        environment = dict(os.environ, PYTHONPATH=str(PROJECT / "src"))
        for args in (("backup", "--root", self.source, "--output", snapshot),
                     ("restore", snapshot, "--root", destination)):
            with self.subTest(command=args[0]):
                process = subprocess.run([sys.executable, "-m", "episteme", *map(str, args)],
                    cwd=PROJECT, env=environment, capture_output=True, text=True,
                    encoding="utf-8", timeout=30, check=False)
                self.assertEqual(process.returncode, 0, process.stderr or process.stdout)
                self.assertIsInstance(json.loads(process.stdout), dict)
                self.assertNotIn("Traceback", process.stderr)
        with Store(destination) as recovered:
            self.assertEqual(recovered.export(), self.store.export())
            self.assertEqual(recovered.export_receipts(), self.store.export_receipts())
            self.assertEqual(recovered.read(self.blob), self.blob_bytes)
            self.assertEqual(CommandService(recovered).execute(self.envelope), self.command_result)
            self.assertEqual(len(recovered.events()), 1)


if __name__ == "__main__":
    unittest.main()
