"""Verified snapshot reuse in Store (ADR 0017): tamper detection and work bounds.

Fixtures are synthetic. Counters are deterministic measures of verification
work, not timings. A warm Store must fail or succeed exactly where a cold Store
opened on the same files does. Passing says nothing about scientific validity.
"""

from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from episteme.store import ConflictError, IntegrityError, Store, canonical, digest


ACTOR = "fixture-planner"


def context(store: Store, **changes: object) -> dict:
    value = dict(command_id=uuid4().hex, expected_revision=len(store.events()), actor=ACTOR,
                 role="planner", study_id="store-verification",
                 correlation_id="store-verification", causation_id=None)
    value.update(changes)
    return value


def note(store: Store, text: str, **changes: object) -> dict:
    """Admit one receipt-backed fixture event through Store.command."""
    def handler() -> dict:
        id = f"note-{uuid4().hex[:12]}"
        store.append(id=id, kind="fixture_note", actor=ACTOR, role="planner",
                     payload=dict(note=text), expected_revision=len(store.events()))
        return dict(id=id)
    return store.command(context(store, **changes),
                         dict(version=1, action="fixture.note", payload=dict(note=text)), handler)


def counted(store: Store) -> dict:
    return dict(store.verification_counts)


def delta(store: Store, before: dict, key: str) -> int:
    return store.verification_counts[key] - before.get(key, 0)


class StoreSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="episteme-store-verification-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "state"
        with Store(self.root) as store:
            for index in range(10):
                note(store, f"fixture note {index}")
        self.store = self.open()

    def open(self) -> Store:
        store = Store(self.root)
        self.addCleanup(store.close)
        return store

    def raw(self) -> closing[sqlite3.Connection]:
        """A separate connection, as another process would use."""
        return closing(sqlite3.connect(self.root / "state.sqlite3"))

    def cold(self) -> tuple:
        with Store(self.root) as store:
            return store.events(), store.receipts()

    def test_repeated_lookups_verify_each_row_once(self):
        for _ in range(50):
            self.assertEqual(len(self.store.events()), 10)
            self.assertEqual(len(self.store.receipts()), 10)
        counts = self.store.verification_counts
        self.assertEqual((counts["event_rows_verified"], counts["receipt_rows_verified"]), (10, 10))
        self.assertEqual((counts["event_table_reads"], counts["receipt_table_reads"]), (1, 1))
        self.assertEqual(counts["state_checks"], 100)
        self.assertEqual((self.store.events(), self.store.receipts()), self.cold())

    def test_a_command_rereads_under_its_lock_and_verifies_only_new_rows(self):
        self.store.receipts()
        before = counted(self.store)
        note(self.store, "new")
        # Both tables are reread under the write lock although the fingerprint was
        # unchanged; afterwards only the appended row is read back and verified.
        self.assertEqual(delta(self.store, before, "event_table_reads"), 2)
        self.assertEqual(delta(self.store, before, "receipt_table_reads"), 1)
        self.assertEqual(delta(self.store, before, "event_rows_verified"), 1)
        self.assertEqual(len(self.store.receipts()), 11)
        self.assertEqual(delta(self.store, before, "receipt_rows_verified"), 1)

    def test_another_connection_append_is_never_hidden_by_the_cache(self):
        self.store.receipts()
        stale = len(self.store.events())
        with Store(self.root) as other:
            note(other, "concurrent")
        before = counted(self.store)
        self.assertEqual(len(self.store.events()), 11)
        self.assertEqual(len(self.store.receipts()), 11)
        self.assertEqual(delta(self.store, before, "event_rows_verified"), 1)
        with self.assertRaises(ConflictError):
            note(self.store, "stale decision", expected_revision=stale)
        self.assertEqual((self.store.events(), self.store.receipts()), self.cold())

    def test_another_process_append_is_seen_by_the_next_read(self):
        self.store.receipts()
        code = ("import sys; from episteme.store import Store\n"
                "with Store(sys.argv[1]) as s:\n"
                "    s.append(id='legacy-x', kind='fixture_note', actor='other', role='planner',"
                " payload={'note': 'other process'}, expected_revision=len(s.events()))\n")
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        subprocess.run([sys.executable, "-c", code, str(self.root)], env=env, check=True,
                       capture_output=True, timeout=120)
        self.assertEqual(self.store.events()[-1]["id"], "legacy-x")
        self.assertEqual((self.store.events(), self.store.receipts()), self.cold())

    def test_event_tampering_by_another_connection_is_detected_by_a_warm_store(self):
        self.store.receipts()
        with self.raw() as db:
            db.execute("DROP TRIGGER events_no_update")
            db.execute("UPDATE events SET payload = ? WHERE seq = 3", ('{"note":"forged"}',))
            db.commit()
        for store in (self.store, self.open()):
            with self.assertRaisesRegex(IntegrityError, "event chain corrupt at sequence 3"):
                store.events()

    def test_event_tampering_through_the_same_connection_is_detected(self):
        self.store.events()
        self.store.db.execute("DROP TRIGGER events_no_update")
        self.store.db.execute("UPDATE events SET actor = 'forged' WHERE seq = 7")
        self.store.db.commit()
        with self.assertRaisesRegex(IntegrityError, "event chain corrupt at sequence 7"):
            self.store.events()
        with self.assertRaisesRegex(IntegrityError, "event chain corrupt at sequence 7"):
            note(self.store, "after tampering")

    def test_reordered_history_is_detected(self):
        self.store.events()
        columns = "id, kind, actor, role, created_at, schema_version, payload, previous_hash, hash"
        with self.raw() as db:
            db.execute("DROP TRIGGER events_no_update")
            rows = {seq: db.execute(f"SELECT {columns} FROM events WHERE seq = ?", (seq,)).fetchone()
                    for seq in (4, 5)}
            db.execute("UPDATE events SET id = 'swap-placeholder' WHERE seq = 4")
            for seq, other in ((5, 4), (4, 5)):
                db.execute("UPDATE events SET id = ?, kind = ?, actor = ?, role = ?, created_at = ?,"
                           " schema_version = ?, payload = ?, previous_hash = ?, hash = ?"
                           " WHERE seq = ?", (*rows[other], seq))
            db.commit()
        for store in (self.store, self.open()):
            with self.assertRaisesRegex(IntegrityError, "event chain corrupt at sequence 4"):
                store.events()

    def test_receipt_tampering_is_detected_and_events_remain_readable(self):
        self.store.receipts()
        target = self.store.receipts()[2]["command_id"]
        with self.raw() as db:
            db.execute("DROP TRIGGER command_receipts_no_update")
            text = db.execute("SELECT receipt FROM command_receipts WHERE command_id = ?",
                              (target,)).fetchone()[0]
            forged = json.loads(text)
            forged["result"] = {"id": "forged"}
            db.execute("UPDATE command_receipts SET receipt = ? WHERE command_id = ?",
                       (canonical(forged).decode(), target))
            db.commit()
        for store in (self.store, self.open()):
            self.assertEqual(len(store.events()), 10)
            with self.assertRaisesRegex(IntegrityError, f"{target}: command receipt checksum mismatch"):
                store.receipts()
            with self.assertRaisesRegex(IntegrityError, "checksum mismatch"):
                note(store, "after receipt tampering")

    def test_coherently_forged_receipt_must_still_bind_its_events(self):
        self.store.receipts()
        target = self.store.receipts()[4]["command_id"]
        with self.raw() as db:
            db.execute("DROP TRIGGER command_receipts_no_update")
            forged = json.loads(db.execute("SELECT receipt FROM command_receipts WHERE command_id = ?",
                                           (target,)).fetchone()[0])
            forged["event_hashes"] = ["f" * 64]
            data = canonical(forged)
            db.execute("UPDATE command_receipts SET receipt = ?, hash = ? WHERE command_id = ?",
                       (data.decode(), digest(data), target))
            db.commit()
        for store in (self.store, self.open()):
            with self.assertRaisesRegex(IntegrityError, "event binding mismatch"):
                store.receipts()

    def test_truncated_tail_is_detected_through_its_receipt(self):
        self.store.receipts()
        with self.raw() as db:
            db.execute("DROP TRIGGER events_no_delete")
            db.execute("DELETE FROM events WHERE seq = 10")
            db.commit()
        for store in (self.store, self.open()):
            self.assertEqual(len(store.events()), 9)
            with self.assertRaisesRegex(IntegrityError, "invalid command receipt event range"):
                store.receipts()

    def test_coherent_truncation_stays_undetectable_without_a_checkpoint(self):
        # Documented limitation: removing a tail together with its receipt leaves
        # a consistent shorter history. A warm Store must not hide or invent anything.
        self.store.receipts()
        last = self.store.receipts()[-1]["command_id"]
        with self.raw() as db:
            db.execute("DROP TRIGGER events_no_delete")
            db.execute("DROP TRIGGER command_receipts_no_delete")
            db.execute("DELETE FROM events WHERE seq = 10")
            db.execute("DELETE FROM command_receipts WHERE command_id = ?", (last,))
            db.commit()
        self.assertEqual((self.store.events(), self.store.receipts()), self.cold())
        self.assertEqual(len(self.store.receipts()), 9)

    def test_coherent_rewrite_reverifies_every_dependent_receipt(self):
        self.store.receipts()
        with self.raw() as db:
            db.execute("DROP TRIGGER events_no_update")
            previous = "0" * 64
            for row in db.execute("SELECT * FROM events ORDER BY seq").fetchall():
                event = dict(seq=row[0], id=row[1], kind=row[2], actor=row[3], role=row[4],
                             created_at=row[5], schema_version=row[6],
                             payload=json.loads(row[7]) | {"rewritten": True}, previous_hash=previous)
                previous = digest(canonical(event))
                db.execute("UPDATE events SET payload = ?, previous_hash = ?, hash = ? WHERE seq = ?",
                           (canonical(event["payload"]).decode(), event["previous_hash"], previous, row[0]))
            db.commit()
        cold_events = Store(self.root)
        self.addCleanup(cold_events.close)
        self.assertEqual(self.store.events(), cold_events.events())
        for store in (self.store, cold_events):
            with self.assertRaisesRegex(IntegrityError, "event binding mismatch"):
                store.receipts()

    def test_rolled_back_command_leaves_no_verified_ghost_rows(self):
        before = self.store.events()

        def failing() -> None:
            self.store.append(id="ghost", kind="fixture_note", actor=ACTOR, role="planner",
                              payload=dict(note="ghost"), expected_revision=len(before))
            self.assertEqual(self.store.events()[-1]["id"], "ghost")
            raise RuntimeError("handler failed after appending")

        with self.assertRaises(RuntimeError):
            self.store.command(context(self.store), dict(version=1, action="fixture.note",
                                                         payload=dict(note="ghost")), failing)
        self.assertEqual(self.store.events(), before)
        self.assertEqual((self.store.events(), self.store.receipts()), self.cold())
        note(self.store, "after rollback", expected_revision=len(before))

    def test_handler_rewriting_preceding_history_is_rejected(self):
        self.store.events()

        def rewriting() -> dict:
            self.store.db.execute("DROP TRIGGER events_no_update")
            self.store.db.execute("UPDATE events SET actor = 'forged' WHERE seq = 2")
            self.store.append(id="after", kind="fixture_note", actor=ACTOR, role="planner",
                              payload=dict(note="x"), expected_revision=10)
            return {}

        with self.assertRaisesRegex(IntegrityError, "event chain corrupt at sequence 2"):
            self.store.command(context(self.store), dict(version=1, action="fixture.note",
                                                         payload=dict(note="x")), rewriting)
        self.assertEqual(len(self.store.events()), 10)


class CasScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="episteme-cas-scope-")
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / "state")
        self.addCleanup(self.store.close)
        self.data = b"verified fixture bytes"
        self.key = self.store.put(self.data)
        self.path = self.store.blobs / self.key

    def read_in_command(self) -> None:
        def handler() -> dict:
            self.store.read(self.key)
            self.store.append(id=f"note-{uuid4().hex[:8]}", kind="fixture_note", actor=ACTOR,
                              role="planner", payload={}, expected_revision=len(self.store.events()))
            return {}
        self.store.command(context(self.store), dict(version=1, action="fixture.read",
                                                     payload={"key": self.key}), handler)

    def test_reads_outside_a_scope_always_rehash(self):
        for _ in range(3):
            self.assertEqual(self.store.read(self.key), self.data)
        self.assertEqual(self.store.verification_counts["cas_blobs_hashed"], 3)
        self.path.write_bytes(b"corrupt")
        with self.assertRaisesRegex(IntegrityError, "artifact hash mismatch"):
            self.store.read(self.key)

    def test_verified_bytes_never_outlive_a_write_transaction(self):
        hashed = lambda: self.store.verification_counts["cas_blobs_hashed"]  # noqa: E731
        with self.store.reading():
            for _ in range(3):
                self.store.read(self.key)
            self.assertEqual(hashed(), 1)
            self.read_in_command()  # Its own empty memo: rehash inside the transaction.
            self.assertEqual(hashed(), 2)
            for _ in range(2):
                self.store.read(self.key)  # The scope starts again after the transaction.
            self.assertEqual(hashed(), 3)
            self.store.append(id="legacy-note", kind="fixture_note", actor=ACTOR, role="planner",
                              payload={}, expected_revision=len(self.store.events()))
            self.store.read(self.key)
            self.assertEqual(hashed(), 4)
        with self.store.reading():
            self.store.read(self.key)
        self.assertEqual(hashed(), 5)

    def test_corruption_after_a_scoped_read_fails_the_next_persisted_transition(self):
        with self.store.reading():
            self.assertEqual(self.store.read(self.key), self.data)
            self.path.write_bytes(b"corrupt")
            # Bytes verified earlier in this read scope remain the verified bytes.
            self.assertEqual(self.store.read(self.key), self.data)
            revision = len(self.store.events())
            with self.assertRaisesRegex(IntegrityError, "artifact hash mismatch"):
                self.read_in_command()
            self.assertEqual(len(self.store.events()), revision)
        with self.assertRaisesRegex(IntegrityError, "artifact hash mismatch"):
            self.store.read(self.key)


class WorkflowBoundTests(unittest.TestCase):
    """A synthetic pack study: rows verified once per Store, blobs once per read scope."""

    @classmethod
    def setUpClass(cls):
        import test_pack_workflow as workflow
        cls.workflow = workflow
        cls._temporary = TemporaryDirectory(prefix="episteme-verification-workflow-")
        cls.root = Path(cls._temporary.name) / "state"
        with Store(cls.root) as store:
            explanation_set = workflow.planning(store)
            result = workflow.command(store, "pack.preregister",
                                      workflow.preregistration(store, explanation_set))
            tree = workflow.search_node(store, result["protocol"], 2)
            binding = next(event for event in store.events() if event["id"] == result["binding"])
            cls.batch = workflow.plan_batch(store, binding, tree)

    @classmethod
    def tearDownClass(cls):
        cls._temporary.cleanup()

    def assert_each_row_verified_once(self, store: Store) -> None:
        history, receipts = store.events(), store.receipts()
        self.assertEqual(store.verification_counts["event_rows_verified"], len(history))
        self.assertEqual(store.verification_counts["receipt_rows_verified"], len(receipts))
        with Store(self.root, read_only=True) as cold:
            self.assertEqual((history, receipts), (cold.events(), cold.receipts()))
        for event in history:
            self.assertEqual(digest(canonical({key: value for key, value in event.items()
                                               if key != "hash"})), event["hash"])

    def test_controllers_and_status_verify_each_row_once(self):
        from episteme.analysis_controller import advance_pack_analysis, analysis_state
        from episteme.batch_controller import advance_batch
        from episteme.graph import ResearchGraph
        with Store(self.root) as store:
            self.assertEqual(advance_batch(store, self.batch)["status"], "completed")
            self.assert_each_row_verified_once(store)
        with Store(self.root) as store:
            result = advance_pack_analysis(store, self.batch, planner=self.workflow.PLANNER,
                                           analyst=self.workflow.ANALYST,
                                           reviewer_actor=self.workflow.REVIEWER)
            self.assertEqual(result["status"], "awaiting_review")
            self.assert_each_row_verified_once(store)
        with Store(self.root, read_only=True) as store, store.reading():
            self.assertEqual(analysis_state(store, self.batch)["status"], "awaiting_review")
            ResearchGraph.from_store(store)
            self.assert_each_row_verified_once(store)
            blobs = [path for path in store.blobs.iterdir() if len(path.name) == 64]
            self.assertLessEqual(store.verification_counts["cas_blobs_hashed"], len(blobs))
            self.assertGreater(store.verification_counts["cas_memo_hits"], 0)


if __name__ == "__main__":
    unittest.main()
