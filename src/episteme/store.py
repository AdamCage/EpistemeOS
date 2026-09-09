"""Append-only event storage and content-addressed artifacts.

This protects against accidental mutation, not a malicious process with filesystem
access. An external checkpoint is necessary to detect a rewritten/truncated history.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class IntegrityError(ValueError):
    """Stored evidence or the event chain failed verification."""


class ConflictError(ValueError):
    """A stale writer must reload state before retrying its decision."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_CONTEXT_KEYS = {"command_id", "expected_revision", "actor", "role", "study_id",
                 "correlation_id", "causation_id"}
_REQUEST_KEYS = {"version", "action", "payload"}
_RECEIPT_KEYS = {"schema_version", "command_id", "context", "request", "request_hash",
                 "before_revision", "before_hash", "after_revision", "after_hash",
                 "event_ids", "event_hashes", "result", "created_at"}
_ZERO_HASH = "0" * 64


def _json_copy(value: Any, label: str) -> Any:
    """Freeze strict JSON without silently accepting tuples or non-string keys."""
    def validate(item: Any) -> None:
        if type(item) is dict:
            if not all(type(key) is str for key in item):
                raise ValueError("object keys must be strings")
            for child in item.values():
                validate(child)
        elif type(item) is list:
            for child in item:
                validate(child)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError("value is not a JSON type")

    try:
        validate(value)
        return json.loads(canonical(value))
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError) as exc:
        raise IntegrityError(f"invalid {label}: {exc}") from exc


def _command_snapshot(context: Any, request: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    context = _json_copy(context, "command context")
    request = _json_copy(request, "command request")
    if type(context) is not dict or set(context) != _CONTEXT_KEYS:
        raise IntegrityError("command context requires exactly the supported fields")
    if (type(context["expected_revision"]) is not int
            or context["expected_revision"] < 0):
        raise IntegrityError("expected revision must be a nonnegative integer")
    for key in ("command_id", "actor", "role", "study_id", "correlation_id"):
        if type(context[key]) is not str or not context[key].strip():
            raise IntegrityError(f"command {key} must be a nonempty string")
    cause = context["causation_id"]
    if cause is not None and (type(cause) is not str or not cause.strip()):
        raise IntegrityError("command causation_id must be null or a nonempty event ID")
    if type(request) is not dict or set(request) != _REQUEST_KEYS:
        raise IntegrityError("command request requires exactly version, action and payload")
    if type(request["version"]) is not int or request["version"] != 1:
        raise IntegrityError("unsupported command request version")
    if type(request["action"]) is not str or not request["action"].strip():
        raise IntegrityError("command action must be a nonempty string")
    if type(request["payload"]) is not dict:
        raise IntegrityError("command payload must be a JSON object")
    return context, request


def _request_hash(context: dict[str, Any], request: dict[str, Any]) -> str:
    return digest(canonical(dict(context=context, request=request)))


def _validate_causation(context: dict[str, Any], history: list[dict[str, Any]]) -> None:
    cause = context["causation_id"]
    if cause is not None and not any(event["id"] == cause for event in history):
        raise IntegrityError("command causation_id must refer to a preceding event")


class Store:
    def __init__(self, root: str | Path, *, read_only: bool = False):
        self.root = Path(root).resolve()
        self.read_only = read_only
        self._command_context: dict[str, Any] | None = None
        self._command_rollback_only = False
        self._receipt_table_known = False
        self.blobs = self.root / "artifacts" / "sha256"
        database = self.root / "state.sqlite3"
        if read_only:
            self.db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10)
        else:
            self.blobs.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(database, timeout=10)
        self.db.row_factory = sqlite3.Row
        if not read_only:
            self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY,
                id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                actor TEXT NOT NULL,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                payload TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                hash TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
            """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(events)")}
        if "schema_version" not in columns:
            self.db.close()
            raise IntegrityError("unsupported event store schema; explicit migration is required")
        try:
            self._receipt_table_known = self._receipt_schema()
            if not read_only:
                # Additive transport metadata: v1 event rows and their hashes are unchanged.
                self.db.executescript("""
                CREATE TABLE IF NOT EXISTS command_receipts (
                    command_id TEXT PRIMARY KEY NOT NULL,
                    receipt TEXT NOT NULL,
                    hash TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS command_receipts_no_update
                    BEFORE UPDATE ON command_receipts
                    BEGIN SELECT RAISE(ABORT, 'command receipts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS command_receipts_no_delete
                    BEFORE DELETE ON command_receipts
                    BEGIN SELECT RAISE(ABORT, 'command receipts are append-only'); END;
                """)
                self._receipt_table_known = True
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def put(self, data: bytes) -> str:
        if self.read_only:
            raise IntegrityError("read-only store cannot write artifacts")
        key = digest(data)
        path = self.blobs / key
        if path.exists():
            self.read(key)
            return key
        fd, temporary = tempfile.mkstemp(dir=self.blobs)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return key

    def put_json(self, value: Any) -> str:
        return self.put(canonical(value))

    def read(self, key: str) -> bytes:
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise IntegrityError("invalid artifact digest")
        try:
            data = (self.blobs / key).read_bytes()
        except OSError as exc:
            raise IntegrityError(f"missing artifact: {key}") from exc
        if digest(data) != key:
            raise IntegrityError(f"artifact hash mismatch: {key}")
        return data

    def events(self) -> list[dict[str, Any]]:
        result = []
        previous = "0" * 64
        for row in self.db.execute("SELECT * FROM events ORDER BY seq"):
            event = dict(row)
            expected_hash = event.pop("hash")
            event["payload"] = json.loads(event["payload"])
            if (event["schema_version"] != 1 or event["seq"] != len(result) + 1
                    or event["previous_hash"] != previous
                    or digest(canonical(event)) != expected_hash):
                raise IntegrityError(f"event chain corrupt at sequence {event['seq']}")
            event["hash"] = expected_hash
            result.append(event)
            previous = expected_hash
        return result

    def append(self, *, id: str, kind: str, actor: str, role: str,
               payload: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        # BEGIN IMMEDIATE serializes validation of the revision and the insert.
        # Domain decisions are made against events() and must pass that revision.
        # Only the explicit command owner may commit/roll back participating appends.
        if self.read_only:
            raise IntegrityError("read-only store cannot append events")
        if self._command_context is not None:
            try:
                if not self.db.in_transaction or self._command_rollback_only:
                    raise IntegrityError("command transaction is not active or is rollback-only")
                if (actor != self._command_context["actor"]
                        or role != self._command_context["role"]):
                    raise IntegrityError("command event actor/role must match its context")
                return self._append_event(id=id, kind=kind, actor=actor, role=role,
                                          payload=payload, expected_revision=expected_revision)
            except BaseException:
                self._command_rollback_only = True
                raise
        if self.db.in_transaction:
            raise IntegrityError("append cannot join an unowned transaction")
        owns_transaction = False
        try:
            self.db.execute("BEGIN IMMEDIATE")
            owns_transaction = True
            event = self._append_event(id=id, kind=kind, actor=actor, role=role,
                                       payload=payload, expected_revision=expected_revision)
            self.db.commit()
            return event
        except BaseException:
            if owns_transaction:
                self.db.rollback()
            raise

    def _append_event(self, *, id: str, kind: str, actor: str, role: str,
                      payload: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        if type(expected_revision) is not int or expected_revision < 0:
            raise IntegrityError("expected revision must be a nonnegative integer")
        history = self.events()
        if len(history) != expected_revision:
            raise ConflictError("research state changed; reload before retry")
        event = dict(seq=len(history) + 1, id=id, kind=kind, actor=actor, role=role,
                     created_at=datetime.now(timezone.utc).isoformat(), schema_version=1,
                     payload=payload,
                     previous_hash=history[-1]["hash"] if history else _ZERO_HASH)
        event_hash = digest(canonical(event))
        self.db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (event["seq"], id, kind, actor, role, event["created_at"],
                         event["schema_version"],
                         canonical(payload).decode(), event["previous_hash"], event_hash))
        return dict(event, hash=event_hash)

    def _receipt_schema(self) -> bool:
        entry = self.db.execute(
            "SELECT type FROM sqlite_master WHERE name = 'command_receipts'").fetchone()
        if entry is None:
            if self._receipt_table_known:
                raise IntegrityError("command receipt table is missing")
            return False
        columns = {row["name"]: (row["type"].upper(), row["notnull"], row["pk"])
                   for row in self.db.execute("PRAGMA table_info(command_receipts)")}
        expected = {"command_id": ("TEXT", 1, 1), "receipt": ("TEXT", 1, 0), "hash": ("TEXT", 1, 0)}
        if entry["type"] != "table" or columns != expected:
            raise IntegrityError("unsupported command receipt schema; explicit migration is required")
        self._receipt_table_known = True
        return True

    def _verified_receipts(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self._receipt_schema():
            return []
        receipts = []
        for row in self.db.execute("SELECT * FROM command_receipts"):
            try:
                receipt = _json_copy(json.loads(row["receipt"]), "stored command receipt")
                if type(receipt) is not dict or set(receipt) != _RECEIPT_KEYS:
                    raise IntegrityError("unsupported command receipt fields")
                if (type(receipt["schema_version"]) is not int
                        or receipt["schema_version"] != 1):
                    raise IntegrityError("unsupported command receipt version")
                if digest(canonical(receipt)) != row["hash"]:
                    raise IntegrityError("command receipt checksum mismatch")
                context, request = _command_snapshot(receipt["context"], receipt["request"])
                if (receipt["command_id"] != context["command_id"]
                        or receipt["command_id"] != row["command_id"]
                        or receipt["request_hash"] != _request_hash(context, request)):
                    raise IntegrityError("command receipt fingerprint mismatch")
                before, after = receipt["before_revision"], receipt["after_revision"]
                if (type(before) is not int or type(after) is not int
                        or not 0 <= before < after <= len(history)
                        or before != context["expected_revision"]):
                    raise IntegrityError("invalid command receipt event range")
                prior_hash = history[before - 1]["hash"] if before else _ZERO_HASH
                events = history[before:after]
                if (receipt["before_hash"] != prior_hash
                        or receipt["after_hash"] != events[-1]["hash"]
                        or receipt["event_ids"] != [event["id"] for event in events]
                        or receipt["event_hashes"] != [event["hash"] for event in events]
                        or any(event["actor"] != context["actor"]
                               or event["role"] != context["role"] for event in events)):
                    raise IntegrityError("command receipt event binding mismatch")
                _validate_causation(context, history[:before])
                if type(receipt["created_at"]) is not str or not receipt["created_at"].strip():
                    raise IntegrityError("invalid command receipt timestamp")
                receipts.append(dict(receipt, hash=row["hash"]))
            except (TypeError, ValueError, KeyError, OverflowError, UnicodeError) as exc:
                raise IntegrityError(f"command receipt corrupt: {row['command_id']}: {exc}") from exc
        receipts.sort(key=lambda receipt: receipt["before_revision"])
        previous_end = 0
        for receipt in receipts:
            if receipt["before_revision"] < previous_end:
                raise IntegrityError("command receipt event ranges overlap")
            previous_end = receipt["after_revision"]
        return receipts

    def receipts(self) -> list[dict[str, Any]]:
        """Read verified delivery history; old read-only stores have no receipts.

        A single read transaction prevents a newly committed receipt from being
        compared with an earlier event snapshot. This is not scientific approval.
        """
        owns_transaction = not self.db.in_transaction
        try:
            if owns_transaction:
                self.db.execute("BEGIN")
            receipts = self._verified_receipts(self.events())
            if owns_transaction:
                self.db.commit()
            return receipts
        except BaseException:
            if owns_transaction:
                self.db.rollback()
            raise

    def _insert_receipt(self, receipt: dict[str, Any]) -> None:
        data = canonical(receipt)
        self.db.execute("INSERT INTO command_receipts VALUES (?, ?, ?)",
                        (receipt["command_id"], data.decode(), digest(data)))

    def command(self, context: dict[str, Any], request: dict[str, Any],
                handler: Callable[[], Any]) -> Any:
        """Commit a short trusted handler once, replaying its historical JSON result.

        Role authorization/action dispatch belongs to the service. Context actor
        IDs are not authenticated here, and study_id is metadata, not isolation.
        Handlers may append events and CAS blobs, never run external work or issue
        SQL commits. Rolled-back blobs can be harmless orphans. No nested commands.
        """
        if self.read_only:
            raise IntegrityError("read-only store cannot admit commands")
        if self._command_context is not None:
            self._command_rollback_only = True
            raise IntegrityError("nested commands are not supported")
        if self.db.in_transaction:
            raise IntegrityError("command cannot join an unowned transaction")
        context, request = _command_snapshot(context, request)
        if not callable(handler):
            raise IntegrityError("command handler must be callable")
        fingerprint = _request_hash(context, request)
        owns_transaction = False
        try:
            self.db.execute("BEGIN IMMEDIATE")
            owns_transaction = True
            history = self.events()
            receipts = self._verified_receipts(history)
            prior = next((receipt for receipt in receipts
                          if receipt["command_id"] == context["command_id"]), None)
            if prior is not None:
                if prior["request_hash"] != fingerprint:
                    raise ConflictError("command ID already used with a different request or context")
                self.db.commit()
                return _json_copy(prior["result"], "command result")
            if context["expected_revision"] != len(history):
                raise ConflictError("research state changed; reload before retry")
            _validate_causation(context, history)
            self._command_context = context
            self._command_rollback_only = False
            result = _json_copy(handler(), "command result")
            if not self.db.in_transaction or self._command_rollback_only:
                raise IntegrityError("command transaction is not active or is rollback-only")
            updated = self.events()
            events = updated[len(history):]
            if not events or updated[:len(history)] != history:
                raise IntegrityError("command must append events without changing preceding history")
            if any(event["actor"] != context["actor"] or event["role"] != context["role"]
                   for event in events):
                raise IntegrityError("command event actor/role must match its context")
            receipt = dict(schema_version=1, command_id=context["command_id"],
                           context=context, request=request, request_hash=fingerprint,
                           before_revision=len(history),
                           before_hash=history[-1]["hash"] if history else _ZERO_HASH,
                           after_revision=len(updated), after_hash=events[-1]["hash"],
                           event_ids=[event["id"] for event in events],
                           event_hashes=[event["hash"] for event in events], result=result,
                           created_at=datetime.now(timezone.utc).isoformat())
            self._insert_receipt(receipt)
            self.db.commit()
            return result
        except BaseException:
            if owns_transaction:
                self.db.rollback()
            raise
        finally:
            self._command_context = None
            self._command_rollback_only = False

    def export_receipts(self) -> str:
        """Delivery-ledger backup; events-only JSONL cannot preserve idempotency."""
        return "".join(canonical(receipt).decode() + "\n" for receipt in self.receipts())

    def export(self) -> str:
        return "".join(canonical(event).decode() + "\n" for event in self.events())
