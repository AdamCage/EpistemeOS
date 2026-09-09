"""Transactional delivery tests; receipts do not certify scientific validity."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from episteme.store import ConflictError, IntegrityError, Store, canonical, digest


class CommandStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="episteme-command-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(self.root)
        self.addCleanup(lambda: self.store.close())

    def context(self, **changes):
        context = dict(command_id="command-1", expected_revision=0, actor="planner-1",
                       role="planner", study_id="study-1", correlation_id="cycle-1",
                       causation_id=None)
        context.update(changes)
        return context

    def request(self, **changes):
        request = dict(version=1, action="fixture.register", payload={"value": 7})
        request.update(changes)
        return request

    def append(self, store=None, *, id=None, actor="planner-1", role="planner"):
        store = self.store if store is None else store
        revision = len(store.events())
        event = store.append(id=id or f"event-{revision + 1}", kind="fixture",
                             actor=actor, role=role, payload={"value": 7},
                             expected_revision=revision)
        return event["id"]

    def test_reopen_replay_after_new_event_returns_original_json_and_no_handler(self):
        calls = []

        def handler():
            calls.append("run")
            return {"id": self.append(), "nested": {"values": [1, 2]}}

        expected = {"id": "event-1", "nested": {"values": [1, 2]}}
        result = self.store.command(self.context(), self.request(), handler)
        self.assertEqual(result, expected)
        result["nested"]["values"].append(3)
        original_receipt = self.store.receipts()[0]
        self.append(id="legacy-event-after-command")
        self.store.close()
        self.store = Store(self.root)
        replay = self.store.command(self.context(), self.request(), handler)
        self.assertEqual(replay, expected)
        self.assertEqual(calls, ["run"])
        self.assertEqual(self.store.receipts(), [original_receipt])
        self.assertEqual(len(self.store.events()), 2)
        replay["nested"]["values"].clear()
        self.assertEqual(self.store.command(self.context(), self.request(), handler), expected)

    def test_same_id_different_context_or_body_conflicts_before_handler(self):
        self.store.command(self.context(), self.request(), self.append)
        variants = [(self.context(**{key: value}), self.request()) for key, value in (
            ("expected_revision", 1), ("actor", "planner-2"), ("role", "executor"),
            ("study_id", "study-2"), ("correlation_id", "cycle-2"), ("causation_id", "event-1"))]
        variants.extend((self.context(), self.request(**{key: value})) for key, value in (
            ("action", "fixture.other"), ("payload", {"value": 8})))
        for context, request in variants:
            with self.subTest(context=context, request=request):
                handler = Mock()
                with self.assertRaisesRegex(ConflictError, "different request or context"):
                    self.store.command(context, request, handler)
                handler.assert_not_called()
        self.assertEqual(len(self.store.events()), 1)
        self.assertEqual(len(self.store.receipts()), 1)

    def test_strict_context_and_json_rejected_without_handler_or_blobs(self):
        variants = [(self.context(**{key: value}), self.request()) for key, value in (
            ("expected_revision", True), ("expected_revision", -1),
            ("expected_revision", 0.0), ("command_id", " "), ("actor", ""),
            ("role", None), ("study_id", 1), ("correlation_id", ""),
            ("causation_id", 3), ("causation_id", " "))]
        incomplete = self.context()
        del incomplete["causation_id"]
        variants.extend([(incomplete, self.request()),
                         (self.context(extra="unexpected"), self.request())])
        for payload in ([], {1: "integer key"}, {"bad": float("nan")},
                        {"bad": float("inf")}, {"bad": (1, 2)}, {"bad": b"bytes"}):
            variants.append((self.context(), self.request(payload=payload)))
        variants.extend((self.context(), self.request(**{key: value})) for key, value in (
            ("version", True), ("version", 2), ("action", ""), ("extra", "unknown")))
        circular = {}
        circular["self"] = circular
        variants.append((self.context(), self.request(payload=circular)))
        for context, request in variants:
            with self.subTest(context=context):
                handler = Mock()
                with self.assertRaises(IntegrityError):
                    self.store.command(context, request, handler)
                handler.assert_not_called()
        self.assertEqual(self.store.events(), [])
        self.assertEqual(self.store.receipts(), [])
        self.assertEqual(list(self.store.blobs.iterdir()), [])

    def test_stale_and_unknown_causation_rejected_before_handler(self):
        self.append(id="preceding-event")
        for context in (self.context(), self.context(expected_revision=1,
                                                      causation_id="missing-event")):
            with self.subTest(context=context):
                handler = Mock()
                with self.assertRaises((ConflictError, IntegrityError)):
                    self.store.command(context, self.request(), handler)
                handler.assert_not_called()
        result = self.store.command(self.context(expected_revision=1,
                                                 causation_id="preceding-event"),
                                    self.request(), self.append)
        self.assertEqual(result, "event-2")
        self.assertEqual(self.store.receipts()[0]["before_revision"], 1)

    def test_two_events_and_receipt_commit_together_with_exact_range(self):
        self.append(id="legacy-before")

        def handler():
            return [self.append(id="event-a"), self.append(id="event-b")]

        result = self.store.command(self.context(expected_revision=1), self.request(), handler)
        self.assertEqual(result, ["event-a", "event-b"])
        receipt = self.store.receipts()[0]
        history = self.store.events()
        self.assertEqual(receipt["event_ids"], result)
        self.assertEqual(receipt["event_hashes"], [event["hash"] for event in history[1:]])
        self.assertEqual(receipt["before_revision"], 1)
        self.assertEqual(receipt["before_hash"], history[0]["hash"])
        self.assertEqual(receipt["after_revision"], 3)
        self.assertEqual(receipt["after_hash"], history[-1]["hash"])
        self.assertFalse(self.store.db.in_transaction)

    def test_handler_failure_rolls_back_all_events_but_preserves_cas_orphan(self):
        key = digest(b"prepared immutable artifact")

        def handler():
            self.append(id="first")
            self.store.put(b"prepared immutable artifact")
            self.append(id="second")
            raise RuntimeError("interrupted before receipt")

        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.store.command(self.context(), self.request(), handler)
        self.assertEqual(self.store.events(), [])
        self.assertEqual(self.store.receipts(), [])
        self.assertEqual(self.store.read(key), b"prepared immutable artifact")
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.store.command(self.context(), self.request(), self.append), "event-1")

    def test_result_serialization_and_receipt_insert_failures_rollback_events(self):
        def bad_result():
            self.append()
            return {"non_json": {1, 2}}

        with self.assertRaisesRegex(IntegrityError, "command result"):
            self.store.command(self.context(), self.request(), bad_result)
        self.assertEqual(self.store.events(), [])
        with patch.object(self.store, "_insert_receipt", side_effect=sqlite3.OperationalError("fault")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "fault"):
                self.store.command(self.context(), self.request(), self.append)
        self.assertEqual(self.store.events(), [])
        self.assertEqual(self.store.receipts(), [])
        self.assertEqual(self.store.command(self.context(), self.request(), self.append), "event-1")

    def test_caught_append_error_poisoned_transaction_cannot_commit_partial_command(self):
        def handler():
            self.append(id="first")
            try:
                self.append(id="first")  # unique ID violation must poison the whole command
            except sqlite3.IntegrityError:
                pass
            return "partial outcome hidden by handler"

        with self.assertRaisesRegex(IntegrityError, "rollback-only"):
            self.store.command(self.context(), self.request(), handler)
        self.assertEqual(self.store.events(), [])
        self.assertEqual(self.store.receipts(), [])
        self.assertEqual(self.store.command(self.context(), self.request(), self.append), "event-1")

    def test_event_actor_and_role_must_match_command_context(self):
        for changes in ({"actor": "other"}, {"role": "reviewer"}):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(IntegrityError, "actor/role"):
                    self.store.command(self.context(), self.request(), lambda: self.append(**changes))
                self.assertEqual(self.store.events(), [])
                self.assertEqual(self.store.receipts(), [])

    def test_nested_command_cannot_be_suppressed_to_commit_outer_events(self):
        def handler():
            self.append()
            try:
                self.store.command(self.context(command_id="inner", expected_revision=1),
                                   self.request(), self.append)
            except IntegrityError:
                pass
            return "caught nested command"

        with self.assertRaisesRegex(IntegrityError, "rollback-only"):
            self.store.command(self.context(), self.request(), handler)
        self.assertEqual(self.store.events(), [])
        self.assertEqual(self.store.receipts(), [])

    def test_commands_and_legacy_appends_reject_unowned_transaction_without_rollback(self):
        self.store.db.execute("BEGIN IMMEDIATE")
        handler = Mock()
        with self.assertRaisesRegex(IntegrityError, "unowned transaction"):
            self.store.command(self.context(), self.request(), handler)
        with self.assertRaisesRegex(IntegrityError, "unowned transaction"):
            self.append()
        self.assertTrue(self.store.db.in_transaction)
        handler.assert_not_called()
        self.store.db.rollback()
        self.assertEqual(self.store.command(self.context(), self.request(), self.append), "event-1")

    def test_handler_without_events_cannot_create_a_success_receipt(self):
        with self.assertRaisesRegex(IntegrityError, "must append events"):
            self.store.command(self.context(), self.request(), lambda: "no transition")
        self.assertEqual(self.store.receipts(), [])

    def concurrent_commands(self, *, same_id):
        barrier = threading.Barrier(2)
        calls = []
        calls_lock = threading.Lock()

        def writer(index):
            with Store(self.root) as store:
                context = self.context(command_id="same" if same_id else f"writer-{index}")
                barrier.wait(timeout=10)

                def handler():
                    with calls_lock:
                        calls.append(index)
                    return self.append(store, id="reserved-transition")

                try:
                    return store.command(context, self.request(), handler)
                except ConflictError:
                    return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(writer, range(2)))
        return results, calls

    def test_two_connections_identical_delivery_execute_handler_once(self):
        results, calls = self.concurrent_commands(same_id=True)
        self.assertEqual(results, ["reserved-transition", "reserved-transition"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.store.events()), 1)
        self.assertEqual(len(self.store.receipts()), 1)

    def test_two_connections_different_ids_cannot_admit_same_revision(self):
        results, calls = self.concurrent_commands(same_id=False)
        self.assertCountEqual(results, ["reserved-transition", "conflict"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.store.events()), 1)
        self.assertEqual(len(self.store.receipts()), 1)

    def test_lost_process_response_after_commit_replays_in_new_connection(self):
        script = """
import json, os, sys
from episteme.store import Store
with Store(sys.argv[1]) as store:
    def handler():
        store.append(id='committed-before-crash', kind='fixture', actor='planner-1',
                     role='planner', payload={'value': 7}, expected_revision=0)
        return {'id': 'committed-before-crash'}
    store.command(json.loads(sys.argv[2]), json.loads(sys.argv[3]), handler)
    os._exit(17)
"""
        environment = dict(os.environ)
        source_root = str(Path(__file__).resolve().parents[1] / "src")
        environment["PYTHONPATH"] = source_root
        completed = subprocess.run([sys.executable, "-c", script, str(self.root),
                                    json.dumps(self.context()), json.dumps(self.request())],
                                   env=environment, capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.returncode, 17, completed.stderr)
        handler = Mock()
        with Store(self.root) as store:
            replay = store.command(self.context(), self.request(), handler)
            self.assertEqual(replay, {"id": "committed-before-crash"})
            self.assertEqual(len(store.events()), 1)
            self.assertEqual(len(store.receipts()), 1)
        handler.assert_not_called()

    def corrupt_receipt(self, mutate, *, recompute_checksum):
        row = self.store.db.execute("SELECT * FROM command_receipts").fetchone()
        receipt = json.loads(row["receipt"])
        mutate(receipt)
        data = canonical(receipt)
        self.store.db.execute("DROP TRIGGER IF EXISTS command_receipts_no_update")
        self.store.db.execute("UPDATE command_receipts SET receipt = ?, hash = ?",
                              (data.decode(), digest(data) if recompute_checksum else row["hash"]))
        self.store.db.commit()

    def test_corrupt_receipt_checksum_blocks_replay_and_new_command(self):
        self.store.command(self.context(), self.request(), self.append)
        self.corrupt_receipt(lambda receipt: receipt.update(result="forged"),
                             recompute_checksum=False)
        for context in (self.context(), self.context(command_id="new", expected_revision=1)):
            handler = Mock()
            with self.assertRaisesRegex(IntegrityError, "checksum mismatch"):
                self.store.command(context, self.request(), handler)
            handler.assert_not_called()

    def test_receipt_fingerprint_range_and_event_binding_checked_beyond_checksum(self):
        original = self.store.command(self.context(), self.request(), self.append)
        self.assertEqual(original, "event-1")
        saved = dict(self.store.db.execute("SELECT * FROM command_receipts").fetchone())
        for field, value, message in (("request_hash", "f" * 64, "fingerprint"),
                                      ("after_revision", 2, "event range"),
                                      ("event_ids", ["other-event"], "event binding"),
                                      ("before_hash", "f" * 64, "event binding"),
                                      ("schema_version", 2, "receipt version")):
            with self.subTest(field=field):
                self.corrupt_receipt(lambda receipt: receipt.update({field: value}),
                                     recompute_checksum=True)
                with self.assertRaisesRegex(IntegrityError, message):
                    self.store.receipts()
                self.store.db.execute("UPDATE command_receipts SET receipt = ?, hash = ?",
                                      (saved["receipt"], saved["hash"]))
                self.store.db.commit()

    def test_overlapping_receipts_rejected_even_when_individually_bound_to_events(self):
        self.store.command(self.context(), self.request(), self.append)
        duplicate = deepcopy(self.store.receipts()[0])
        duplicate.pop("hash")
        duplicate["command_id"] = "another-command"
        duplicate["context"]["command_id"] = "another-command"
        duplicate["request_hash"] = digest(canonical(dict(context=duplicate["context"],
                                                          request=duplicate["request"])))
        self.store._insert_receipt(duplicate)
        self.store.db.commit()
        with self.assertRaisesRegex(IntegrityError, "overlap"):
            self.store.receipts()

    def test_receipts_append_only_and_export_round_trip(self):
        self.store.command(self.context(), self.request(), self.append)
        expected = self.store.receipts()
        for sql in ("UPDATE command_receipts SET hash = 'forged'", "DELETE FROM command_receipts"):
            with self.subTest(sql=sql):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    self.store.db.execute(sql)
                self.store.db.rollback()
        self.assertEqual(self.store.receipts(), expected)
        self.assertEqual([json.loads(line) for line in self.store.export_receipts().splitlines()],
                         expected)
        with Store(self.root, read_only=True) as observer:
            self.assertEqual(observer.receipts(), expected)
            handler = Mock()
            with self.assertRaisesRegex(IntegrityError, "read-only"):
                observer.command(self.context(), self.request(), handler)
            handler.assert_not_called()

    def test_corrupt_event_chain_blocks_receipt_replay(self):
        self.store.command(self.context(), self.request(), self.append)
        self.store.db.execute("DROP TRIGGER events_no_update")
        self.store.db.execute("UPDATE events SET actor = 'forged'")
        self.store.db.commit()
        handler = Mock()
        with self.assertRaisesRegex(IntegrityError, "event chain corrupt"):
            self.store.command(self.context(), self.request(), handler)
        handler.assert_not_called()

    def test_old_read_only_store_has_no_migration_and_writable_preserves_v1_export(self):
        legacy_root = self.root / "legacy"
        legacy_root.mkdir()
        database = legacy_root / "state.sqlite3"
        event = dict(seq=1, id="legacy-id", kind="fixture", actor="legacy", role="planner",
                     created_at="2026-09-08T00:00:00+00:00", schema_version=1,
                     payload={"negative_result": "retained"}, previous_hash="0" * 64)
        event["hash"] = digest(canonical(event))
        with sqlite3.connect(database) as db:
            db.execute("""CREATE TABLE events (
                seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
                actor TEXT NOT NULL, role TEXT NOT NULL, created_at TEXT NOT NULL,
                schema_version INTEGER NOT NULL, payload TEXT NOT NULL,
                previous_hash TEXT NOT NULL, hash TEXT NOT NULL)""")
            db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       tuple(canonical(value).decode() if key == "payload" else value
                             for key, value in event.items()))
        db.close()
        original_database_bytes = database.read_bytes()
        expected_export = canonical(event).decode() + "\n"
        with Store(legacy_root, read_only=True) as observer:
            self.assertEqual(observer.export(), expected_export)
            self.assertEqual(observer.receipts(), [])
            self.assertEqual(observer.export_receipts(), "")
            self.assertIsNone(observer.db.execute(
                "SELECT name FROM sqlite_master WHERE name = 'command_receipts'").fetchone())
        self.assertEqual(database.read_bytes(), original_database_bytes)
        self.assertFalse((legacy_root / "artifacts").exists())
        with Store(legacy_root) as writable:
            self.assertEqual(writable.export(), expected_export)
            writable.command(self.context(expected_revision=1, causation_id="legacy-id"),
                             self.request(), lambda: self.append(writable))
            receipt_export = writable.export_receipts()
            full_export = writable.export()
        with Store(legacy_root) as reopened:
            self.assertEqual(reopened.export(), full_export)
            self.assertEqual(reopened.export_receipts(), receipt_export)
            self.assertEqual(reopened.events()[0], event)

    def test_unknown_receipt_table_schema_requires_migration(self):
        self.store.db.execute("ALTER TABLE command_receipts ADD COLUMN future_schema TEXT")
        self.store.db.commit()
        for read_only in (False, True):
            with self.subTest(read_only=read_only):
                with self.assertRaisesRegex(IntegrityError, "unsupported command receipt schema"):
                    Store(self.root, read_only=read_only)

    def test_receipt_table_without_unique_command_id_is_not_a_supported_schema(self):
        self.store.db.execute("DROP TABLE command_receipts")
        self.store.db.execute("CREATE TABLE command_receipts (command_id TEXT NOT NULL, "
                              "receipt TEXT NOT NULL, hash TEXT NOT NULL)")
        self.store.db.commit()
        for read_only in (False, True):
            with self.subTest(read_only=read_only):
                with self.assertRaisesRegex(IntegrityError, "unsupported command receipt schema"):
                    Store(self.root, read_only=read_only)


if __name__ == "__main__":
    unittest.main()
