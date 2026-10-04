"""Storage-integrity regressions for ADR 0023.

Fixtures are synthetic. These checks do not establish scientific validity.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from episteme.commands import CommandService
from episteme.recovery import backup, restore
from episteme.store import IntegrityError, Store, _APPEND_ONLY_TRIGGERS, _schema_text, canonical, digest


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


def hypothesis_envelope() -> dict:
    return dict(context=dict(command_id="schema-command-1", expected_revision=0,
                actor=ACTOR, role="planner", study_id="schema-fixture",
                correlation_id="schema-fixture", causation_id=None),
                request=dict(version=1, action="kernel.hypothesis", payload=dict(
                    statement="Synthetic schema fixture", prediction="The id is stable",
                    falsifier="A second id appears", scope={"mode": "schema_fixture"})))


def neuter(database: sqlite3.Connection) -> None:
    """Replace every append-only trigger with SELECT 1, keeping the name."""
    rows = list(database.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger'"))
    for name, table, sql in rows:
        if "BEFORE DELETE" in sql:
            when = "DELETE"
        elif "BEFORE INSERT" in sql:
            when = "INSERT"
        else:
            when = "UPDATE"
        database.execute(f"DROP TRIGGER {name}")
        database.execute(
            f"CREATE TRIGGER {name} BEFORE {when} ON {table} BEGIN SELECT 1; END")
    database.commit()


class SchemaGuardTests(unittest.TestCase):
    """A-16: a trigger whose body is not the guard must not survive open or restore."""

    def test_fresh_store_matches_the_trigger_allowlist(self):
        temporary = TemporaryDirectory(prefix="episteme-schema-fresh-")
        self.addCleanup(temporary.cleanup)
        with Store(Path(temporary.name) / "state") as store:
            found = {row["name"]: _schema_text(row["sql"]) for row in store.db.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")}
        self.assertEqual(found, _APPEND_ONLY_TRIGGERS)

    def test_neutered_triggers_are_rejected_on_open(self):
        temporary = TemporaryDirectory(prefix="episteme-schema-open-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "state"
        with Store(root) as store:
            note(store, "kept")
            neuter(store.db)
        for read_only in (False, True):
            with self.subTest(read_only=read_only):
                with self.assertRaisesRegex(IntegrityError, "unsupported store schema"):
                    Store(root, read_only=read_only)

    def test_extra_view_is_rejected(self):
        temporary = TemporaryDirectory(prefix="episteme-schema-view-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "state"
        with Store(root) as store:
            store.db.execute("CREATE VIEW fixture_leak AS SELECT seq FROM events")
            store.db.commit()
        with self.assertRaisesRegex(IntegrityError, "unsupported store schema"):
            Store(root)

    def test_restore_rejects_a_snapshot_whose_triggers_were_replaced(self):
        temporary = TemporaryDirectory(prefix="episteme-schema-restore-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "source"
        with Store(source) as store:
            CommandService(store).execute(hypothesis_envelope())
            snapshot = root / "snapshot"
            backup(store, snapshot)
        database = sqlite3.connect(snapshot / "state.sqlite3")
        try:
            neuter(database)
        finally:
            database.close()
        manifest = json.loads((snapshot / "manifest.json").read_bytes())
        raw = (snapshot / "state.sqlite3").read_bytes()
        manifest["database"] = dict(sha256=digest(raw), size=len(raw))
        (snapshot / "manifest.json").write_bytes(canonical(manifest))
        destination = root / "restored"
        with self.assertRaisesRegex(IntegrityError, "unsupported store schema"):
            restore(snapshot, destination)
        self.assertFalse(destination.exists())


class CanonicalPayloadTests(unittest.TestCase):
    """A-18: the hash does not excuse stored bytes that are not canonical JSON."""

    def test_duplicate_key_payload_is_rejected_even_when_the_hash_matches(self):
        temporary = TemporaryDirectory(prefix="episteme-canonical-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "state"
        with Store(root) as store:
            note(store, "fixture note")
            text = store.db.execute("SELECT payload, hash FROM events").fetchone()
            original, event_hash = text["payload"], text["hash"]
            store.db.execute("DROP TRIGGER events_no_update")
            # First key wins in SQLite json_extract; Python's last key is the original object.
            forged = '{"note":"approve",' + original[1:]
            self.assertEqual(json.loads(forged)["note"], json.loads(original)["note"])
            self.assertNotEqual(forged, original)
            store.db.execute("UPDATE events SET payload = ? WHERE seq = 1", (forged,))
            store.db.commit()
            sqlite_note = store.db.execute(
                "SELECT json_extract(payload, '$.note') FROM events").fetchone()[0]
            self.assertEqual(sqlite_note, "approve")
            self.assertEqual(store.db.execute("SELECT hash FROM events").fetchone()[0], event_hash)
        with self.assertRaisesRegex(IntegrityError, "payload is not canonical"):
            with Store(root) as store:
                store.events()

    def test_spaced_payload_with_a_recomputed_hash_is_rejected(self):
        temporary = TemporaryDirectory(prefix="episteme-canonical-space-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "state"
        with Store(root) as store:
            note(store, "fixture note")
            row = store.db.execute("SELECT * FROM events").fetchone()
            forged = "{ " + row["payload"][1:]
            payload = json.loads(forged)
            body = dict(seq=row["seq"], id=row["id"], kind=row["kind"], actor=row["actor"],
                        role=row["role"], created_at=row["created_at"],
                        schema_version=row["schema_version"], payload=payload,
                        previous_hash=row["previous_hash"])
            store.db.execute("DROP TRIGGER events_no_update")
            store.db.execute("UPDATE events SET payload = ?, hash = ? WHERE seq = 1",
                             (forged, digest(canonical(body))))
            store.db.commit()
        with self.assertRaisesRegex(IntegrityError, "payload is not canonical"):
            with Store(root) as store:
                store.events()

    def test_canonical_payload_bytes_are_left_unchanged(self):
        temporary = TemporaryDirectory(prefix="episteme-canonical-keep-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "state"
        with Store(root) as store:
            note(store, "уже канонично")
            before = store.db.execute("SELECT payload FROM events").fetchone()[0]
            self.assertEqual(before, canonical(json.loads(before)).decode())
            self.assertEqual(store.events()[0]["payload"]["note"], "уже канонично")
            after = store.db.execute("SELECT payload FROM events").fetchone()[0]
        self.assertEqual(after, before)

    def test_noncanonical_receipt_text_is_rejected(self):
        temporary = TemporaryDirectory(prefix="episteme-canonical-receipt-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "state"
        with Store(root) as store:
            note(store, "fixture note")
            text = store.db.execute("SELECT receipt FROM command_receipts").fetchone()[0]
            forged = "{ " + text[1:]
            self.assertEqual(json.loads(forged), json.loads(text))
            store.db.execute("DROP TRIGGER command_receipts_no_update")
            store.db.execute("UPDATE command_receipts SET receipt = ?", (forged,))
            store.db.commit()
        with Store(root) as store:
            self.assertEqual(len(store.events()), 1)
            with self.assertRaisesRegex(IntegrityError, "not canonical"):
                store.receipts()


if __name__ == "__main__":
    unittest.main()
