"""Storage-integrity regressions for ADR 0023.

Fixtures are synthetic. These checks do not establish scientific validity.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from episteme.store import Store, canonical, digest


ACTOR = "fixture-planner"


def context(store: Store, **changes: object) -> dict:
    value = dict(command_id=uuid4().hex, expected_revision=len(store.events()), actor=ACTOR,
                 role="planner", study_id="storage-integrity",
                 correlation_id="storage-integrity", causation_id=None)
    value.update(changes)
    return value


def note(store: Store, text: str) -> dict:
    def handler() -> dict:
        id = f"note-{uuid4().hex[:12]}"
        store.append(id=id, kind="fixture_note", actor=ACTOR, role="planner",
                     payload=dict(note=text), expected_revision=len(store.events()))
        return dict(id=id)

    return store.command(context(store),
                         dict(version=1, action="fixture.note", payload=dict(note=text)), handler)


class AppendOnlyUpsertTests(unittest.TestCase):
    """A-13: INSERT OR REPLACE must not rewrite a row while the triggers remain."""

    def setUp(self):
        temporary = TemporaryDirectory(prefix="episteme-upsert-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "state"
        with Store(root) as writer:
            for index in range(4):
                note(writer, f"fixture note {index}")
        self.store = Store(root)
        self.addCleanup(self.store.close)

    def event_values(self, seq: int, payload: dict) -> tuple:
        row = self.store.db.execute("SELECT * FROM events WHERE seq = ?", (seq,)).fetchone()
        body = dict(seq=row["seq"], id=row["id"], kind=row["kind"], actor=row["actor"],
                    role=row["role"], created_at=row["created_at"],
                    schema_version=row["schema_version"], payload=payload,
                    previous_hash=row["previous_hash"])
        return (body["seq"], body["id"], body["kind"], body["actor"], body["role"],
                body["created_at"], body["schema_version"], canonical(payload).decode(),
                body["previous_hash"], digest(canonical(body)))

    def test_insert_or_replace_cannot_rewrite_events_or_receipts(self):
        before = self.store.events()
        receipt = self.store.receipts()[0]
        forged = dict(receipt)
        forged.pop("hash")
        forged["result"] = {"id": "hypothesis-FORGED"}
        receipt_bytes = canonical(forged)
        statements = (
            "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
            "REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
            """INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(seq) DO UPDATE SET payload = excluded.payload""",
        )
        values = self.event_values(before[-1]["seq"], dict(note="replaced", verdict="approve"))
        for sql in statements:
            with self.subTest(sql=sql.split()[0]):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    self.store.db.execute(sql, values)
                self.store.db.rollback()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.store.db.execute(
                "INSERT OR REPLACE INTO command_receipts VALUES (?, ?, ?)",
                (forged["command_id"], receipt_bytes.decode(), digest(receipt_bytes)))
        self.store.db.rollback()
        self.assertEqual(self.store.events(), before)
        self.assertEqual(self.store.receipts()[0]["result"], receipt["result"])
        note(self.store, "ordinary append still works")
        self.assertEqual(self.store.events()[-1]["payload"]["note"], "ordinary append still works")

    def test_sequence_gap_and_duplicate_id_are_rejected(self):
        count = len(self.store.events())
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.store.db.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                (count + 5, "gap-id", "fixture_note", ACTOR, "planner",
                 "2026-10-04T00:00:00+00:00", 1, "{}", "0" * 64, "ab" * 32))
        self.store.db.rollback()
        existing = self.store.events()[0]["id"]
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.store.db.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                (count + 1, existing, "fixture_note", ACTOR, "planner",
                 "2026-10-04T00:00:00+00:00", 1, "{}", "0" * 64, "cd" * 32))
        self.store.db.rollback()
        self.assertEqual(len(self.store.events()), count)

    def test_reads_still_verify_each_row_once_with_insert_guards(self):
        for _ in range(40):
            self.assertEqual(len(self.store.events()), 4)
            self.assertEqual(len(self.store.receipts()), 4)
        counts = self.store.verification_counts
        self.assertEqual((counts["event_rows_verified"], counts["receipt_rows_verified"]), (4, 4))
        self.assertEqual((counts["event_table_reads"], counts["receipt_table_reads"]), (1, 1))
        before = dict(counts)
        note(self.store, "one more")
        self.assertEqual(len(self.store.receipts()), 5)
        self.assertEqual(self.store.verification_counts["event_rows_verified"],
                         before["event_rows_verified"] + 1)
        self.assertEqual(self.store.verification_counts["receipt_rows_verified"],
                         before["receipt_rows_verified"] + 1)
        verified_events = self.store.verification_counts["event_rows_verified"]
        verified_receipts = self.store.verification_counts["receipt_rows_verified"]
        for _ in range(10):
            self.store.events()
            self.store.receipts()
        self.assertEqual(self.store.verification_counts["event_rows_verified"], verified_events)
        self.assertEqual(self.store.verification_counts["receipt_rows_verified"], verified_receipts)


if __name__ == "__main__":
    unittest.main()
