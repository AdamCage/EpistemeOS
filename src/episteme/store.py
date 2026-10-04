"""Append-only event storage and content-addressed artifacts.

This protects against accidental mutation, not a malicious process with filesystem
access. BEFORE INSERT triggers reject a duplicate key and a sequence gap, so
INSERT OR REPLACE cannot rewrite a row while those triggers remain. An external
checkpoint is still necessary to detect a rewritten or truncated history.

Each Store instance keeps the event chain and receipts it has verified. A read
reuses them only while a fingerprint of the database (SQLite data_version and
schema_version, this connection's total_changes, row counts and the chain head)
is unchanged; otherwise every row is reread and compared with the verified bytes,
and changed or new rows are verified again. See ADR 0017.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


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
_EVENT_COLUMNS = {
    "seq": ("INTEGER", 0, 1), "id": ("TEXT", 1, 0), "kind": ("TEXT", 1, 0),
    "actor": ("TEXT", 1, 0), "role": ("TEXT", 1, 0), "created_at": ("TEXT", 1, 0),
    "schema_version": ("INTEGER", 1, 0), "payload": ("TEXT", 1, 0),
    "previous_hash": ("TEXT", 1, 0), "hash": ("TEXT", 1, 0),
}
_EVENT_INDEX = "sqlite_autoindex_events_1"
_RECEIPT_INDEX = "sqlite_autoindex_command_receipts_1"


def _schema_text(sql: str) -> str:
    return " ".join(sql.split())


# SQLite stores the trigger body without CREATE's IF NOT EXISTS. A different body
# (for example SELECT 1 under the same name) is not this guard.
_APPEND_ONLY_TRIGGERS = {
    "events_no_delete": _schema_text(
        "CREATE TRIGGER events_no_delete BEFORE DELETE ON events "
        "BEGIN SELECT RAISE(ABORT, 'events are append-only'); END"),
    "events_no_insert": _schema_text(
        "CREATE TRIGGER events_no_insert BEFORE INSERT ON events "
        "BEGIN SELECT RAISE(ABORT, 'events are append-only') "
        "WHERE EXISTS (SELECT 1 FROM events WHERE seq = NEW.seq OR id = NEW.id) "
        "OR NEW.seq != COALESCE((SELECT MAX(seq) FROM events), 0) + 1; END"),
    "events_no_update": _schema_text(
        "CREATE TRIGGER events_no_update BEFORE UPDATE ON events "
        "BEGIN SELECT RAISE(ABORT, 'events are append-only'); END"),
    "command_receipts_no_delete": _schema_text(
        "CREATE TRIGGER command_receipts_no_delete BEFORE DELETE ON command_receipts "
        "BEGIN SELECT RAISE(ABORT, 'command receipts are append-only'); END"),
    "command_receipts_no_insert": _schema_text(
        "CREATE TRIGGER command_receipts_no_insert BEFORE INSERT ON command_receipts "
        "BEGIN SELECT RAISE(ABORT, 'command receipts are append-only') "
        "WHERE EXISTS (SELECT 1 FROM command_receipts WHERE command_id = NEW.command_id); END"),
    "command_receipts_no_update": _schema_text(
        "CREATE TRIGGER command_receipts_no_update BEFORE UPDATE ON command_receipts "
        "BEGIN SELECT RAISE(ABORT, 'command receipts are append-only'); END"),
}
_STATE = ("SELECT (SELECT data_version FROM pragma_data_version()),"
          " (SELECT schema_version FROM pragma_schema_version()),"
          " (SELECT count(*) FROM events), (SELECT max(seq) FROM events),"
          " (SELECT hash FROM events ORDER BY seq DESC LIMIT 1)")
_STATE_WITH_RECEIPTS = _STATE + ", (SELECT count(*) FROM command_receipts)"
# A read scope keeps verified CAS bytes up to these sizes; larger reads are rehashed.
_MEMO_BLOB_BYTES = 64 * 1024 * 1024
_MEMO_TOTAL_BYTES = 512 * 1024 * 1024


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


def _verify_receipt(row: sqlite3.Row, history: list[dict[str, Any]]) -> dict[str, Any]:
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
        return dict(receipt, hash=row["hash"])
    except (TypeError, ValueError, KeyError, OverflowError, UnicodeError) as exc:
        raise IntegrityError(f"command receipt corrupt: {row['command_id']}: {exc}") from exc


def _ordered_receipts(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    receipts.sort(key=lambda receipt: receipt["before_revision"])
    previous_end = 0
    for receipt in receipts:
        if receipt["before_revision"] < previous_end:
            raise IntegrityError("command receipt event ranges overlap")
        previous_end = receipt["after_revision"]
    return receipts


class _CasMemo:
    """Verified CAS bytes for one read scope or write transaction."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.size = 0


class Store:
    def __init__(self, root: str | Path, *, read_only: bool = False):
        self.root = Path(root).resolve()
        self.read_only = read_only
        self._command_context: dict[str, Any] | None = None
        self._command_rollback_only = False
        self._receipt_table_known = False
        # Verified snapshot; lists and dicts are replaced, never mutated in place.
        self._event_columns: tuple[str, ...] = ()
        self._event_rows: list[tuple[Any, ...]] = []
        self._events: list[dict[str, Any]] = []
        self._events_epoch = 0
        self._events_state: tuple[Any, ...] | None = None
        self._receipt_rows: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self._receipts: list[dict[str, Any]] = []
        self._receipts_epoch = -1
        self._receipts_state: tuple[Any, ...] | None = None
        self._receipt_presence: tuple[int, bool] | None = None
        self._cas_memo: _CasMemo | None = None
        # Deterministic work counters for regression tests; not wall-clock time.
        self.verification_counts: Counter[str] = Counter()
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
            CREATE TRIGGER IF NOT EXISTS events_no_insert BEFORE INSERT ON events
                BEGIN SELECT RAISE(ABORT, 'events are append-only')
                WHERE EXISTS (SELECT 1 FROM events WHERE seq = NEW.seq OR id = NEW.id)
                   OR NEW.seq != COALESCE((SELECT MAX(seq) FROM events), 0) + 1; END;
            """)
        columns = {
            row["name"]: (str(row["type"]).upper(), row["notnull"], row["pk"])
            for row in self.db.execute("PRAGMA table_info(events)")}
        if columns != _EVENT_COLUMNS:
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
                CREATE TRIGGER IF NOT EXISTS command_receipts_no_insert
                    BEFORE INSERT ON command_receipts
                    BEGIN SELECT RAISE(ABORT, 'command receipts are append-only')
                    WHERE EXISTS (SELECT 1 FROM command_receipts
                                  WHERE command_id = NEW.command_id); END;
                """)
                self._receipt_table_known = True
            self._require_schema()
        except BaseException:
            self.db.close()
            raise

    def _require_schema(self) -> None:
        """Reject a trigger body or an extra object that is not the append-only guard.

        A read-only open does not install missing triggers: an old database that
        has not yet been opened for writing still lacks ``BEFORE INSERT``. A body
        that is not the guard is rejected in both modes. Writable open installs
        any missing guard first, then requires the full set.
        """
        rows = list(self.db.execute("SELECT type, name, tbl_name, sql FROM sqlite_master"))
        receipt_table = any(row["type"] == "table" and row["name"] == "command_receipts"
                            for row in rows)
        allowed = {name: sql for name, sql in _APPEND_ONLY_TRIGGERS.items()
                   if receipt_table or name.startswith("events_")}
        seen: set[str] = set()
        for row in rows:
            kind, name, table, sql = row["type"], row["name"], row["tbl_name"], row["sql"]
            if kind == "table" and name in ({"events", "command_receipts"} if receipt_table else {"events"}):
                continue
            if kind == "index" and sql is None and (
                    (name == _EVENT_INDEX and table == "events")
                    or (receipt_table and name == _RECEIPT_INDEX and table == "command_receipts")):
                continue
            if (kind == "trigger" and name in allowed and isinstance(sql, str)
                    and _schema_text(sql) == allowed[name]):
                seen.add(name)
                continue
            raise IntegrityError("unsupported store schema; explicit migration is required")
        names = {(row["type"], row["name"]) for row in rows}
        if ("table", "events") not in names or ("index", _EVENT_INDEX) not in names:
            raise IntegrityError("unsupported event store schema; explicit migration is required")
        if receipt_table and ("index", _RECEIPT_INDEX) not in names:
            raise IntegrityError("unsupported command receipt schema; explicit migration is required")
        if not self.read_only and seen != set(allowed):
            raise IntegrityError("unsupported store schema; explicit migration is required")

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
        memo = self._cas_memo
        if memo is not None and key in memo.blobs:
            self.verification_counts["cas_memo_hits"] += 1
            return memo.blobs[key]
        try:
            data = (self.blobs / key).read_bytes()
        except OSError as exc:
            raise IntegrityError(f"missing artifact: {key}") from exc
        self.verification_counts["cas_blobs_hashed"] += 1
        self.verification_counts["cas_bytes_hashed"] += len(data)
        if digest(data) != key:
            raise IntegrityError(f"artifact hash mismatch: {key}")
        if (memo is not None and len(data) <= _MEMO_BLOB_BYTES
                and memo.size + len(data) <= _MEMO_TOTAL_BYTES):
            memo.blobs[key] = data
            memo.size += len(data)
        return data

    @contextmanager
    def reading(self) -> Iterator[Store]:
        """Hash each CAS blob at most once in this scope; nested scopes share it.

        Verified bytes never outlive a write transaction: a command transaction
        starts its own empty memo, so a persisted command rereads the artifacts
        it depends on from disk, and the scope's memo starts empty again after
        every command or append.
        """
        if self._cas_memo is not None:
            yield self
            return
        self._cas_memo = _CasMemo()
        try:
            yield self
        finally:
            self._cas_memo = None

    def _fingerprint(self) -> tuple[Any, ...]:
        """Cheap identity of the visible database state; any change alters it.

        data_version changes when another connection commits, schema_version on
        any DDL and total_changes on every row this connection writes.
        """
        state = None
        if self._receipt_presence is not None and self._receipt_presence[1]:
            try:
                state = tuple(self.db.execute(_STATE_WITH_RECEIPTS).fetchone())
            except sqlite3.OperationalError:
                state = None  # E.g. a dropped receipt table; the lookup below decides.
            if state is not None and state[1] != self._receipt_presence[0]:
                state = None  # The schema changed; look the receipt table up again.
        if state is None:
            state = tuple(self.db.execute(_STATE).fetchone())
            entry = self.db.execute(
                "SELECT type FROM sqlite_master WHERE name = 'command_receipts'").fetchone()
            self._receipt_presence = (state[1], entry is not None and entry["type"] == "table")
            state += (self.db.execute("SELECT count(*) FROM command_receipts").fetchone()[0]
                      if self._receipt_presence[1] else None,)
        return (*state, self.db.total_changes)

    def _sync(self, *, receipts: bool, reread: bool = False) -> None:
        """Reverify only if the fingerprint differs from the verified snapshot.

        A changed fingerprint, or ``reread``, rereads every row in one read
        transaction and compares it with the verified bytes: unchanged rows keep
        their verification, changed or new rows are verified again.
        """
        self.verification_counts["state_checks"] += 1
        state = self._fingerprint()
        if not reread and state == self._events_state and (
                not receipts or state == self._receipts_state):
            return
        owns_transaction = not self.db.in_transaction
        try:
            if owns_transaction:
                self.db.execute("BEGIN")
                state = self._fingerprint()
            if reread or state != self._events_state:
                self._reload_events()
                self._events_state = state
            if receipts and (reread or state != self._receipts_state):
                self._reload_receipts()
                self._receipts_state = state
            if owns_transaction:
                self.db.commit()
        except BaseException:
            if owns_transaction:
                self.db.rollback()
            raise

    def _reload_events(self) -> None:
        self.verification_counts["event_table_reads"] += 1
        cursor = self.db.cursor()
        cursor.row_factory = None
        rows = cursor.execute("SELECT * FROM events ORDER BY seq").fetchall()
        columns = tuple(column[0] for column in cursor.description)
        kept = self._event_rows if columns == self._event_columns else []
        same = min(len(rows), len(kept))
        if rows[:same] != kept[:same]:
            same = next(index for index in range(same) if rows[index] != kept[index])
        result = self._events[:same]
        previous = result[-1]["hash"] if result else _ZERO_HASH
        for row in rows[same:]:
            event = dict(zip(columns, row))
            expected_hash = event.pop("hash")
            event["payload"] = json.loads(event["payload"])
            if (event["schema_version"] != 1 or event["seq"] != len(result) + 1
                    or event["previous_hash"] != previous
                    or digest(canonical(event)) != expected_hash):
                raise IntegrityError(f"event chain corrupt at sequence {event['seq']}")
            event["hash"] = expected_hash
            result.append(event)
            previous = expected_hash
        self.verification_counts["event_rows_verified"] += len(rows) - same
        if same < len(self._event_rows):
            # A verified row changed or vanished; every receipt is checked again.
            self._events_epoch += 1
        self._event_columns, self._event_rows, self._events = columns, rows, result

    def _reload_receipts(self) -> None:
        if not self._receipt_schema():
            self._receipt_rows, self._receipts = {}, []
            return
        self.verification_counts["receipt_table_reads"] += 1
        history = self._events
        cached = self._receipt_rows if self._receipts_epoch == self._events_epoch else {}
        rows: dict[str, tuple[str, str, dict[str, Any]]] = {}
        receipts = []
        for row in self.db.execute("SELECT * FROM command_receipts"):
            prior = cached.get(row["command_id"])
            if prior is not None and prior[:2] == (row["receipt"], row["hash"]):
                receipt = prior[2]
            else:
                receipt = _verify_receipt(row, history)
                self.verification_counts["receipt_rows_verified"] += 1
            rows[row["command_id"]] = (row["receipt"], row["hash"], receipt)
            receipts.append(receipt)
        self._receipts = _ordered_receipts(receipts)
        self._receipt_rows, self._receipts_epoch = rows, self._events_epoch

    def _checkpoint(self) -> tuple[Any, ...]:
        return (self._event_columns, self._event_rows, self._events, self._events_epoch,
                self._receipt_rows, self._receipts, self._receipts_epoch)

    def _restore(self, checkpoint: tuple[Any, ...]) -> None:
        """Forget rows verified inside a rolled-back transaction."""
        (self._event_columns, self._event_rows, self._events, self._events_epoch,
         self._receipt_rows, self._receipts, self._receipts_epoch) = checkpoint
        self._events_state = self._receipts_state = None

    def events(self) -> list[dict[str, Any]]:
        """Return the verified chain; callers must not mutate the shared event dicts."""
        self._sync(receipts=False)
        return list(self._events)

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
        checkpoint = self._checkpoint()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            owns_transaction = True
            self._sync(receipts=False, reread=True)
            event = self._append_event(id=id, kind=kind, actor=actor, role=role,
                                       payload=payload, expected_revision=expected_revision)
            self.db.commit()
            return event
        except BaseException:
            if owns_transaction:
                self.db.rollback()
                self._restore(checkpoint)
            raise
        finally:
            if self._cas_memo is not None:
                self._cas_memo = _CasMemo()

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
        """Receipts verified against ``history``; the cache serves only an equal chain."""
        self._sync(receipts=True)
        if history == self._events:
            return list(self._receipts)
        if not self._receipt_schema():
            return []
        self.verification_counts["receipt_table_reads"] += 1
        receipts = []
        for row in self.db.execute("SELECT * FROM command_receipts"):
            receipts.append(_verify_receipt(row, history))
            self.verification_counts["receipt_rows_verified"] += 1
        return _ordered_receipts(receipts)

    def receipts(self) -> list[dict[str, Any]]:
        """Read verified delivery history; old read-only stores have no receipts.

        A single read snapshot prevents a newly committed receipt from being
        compared with an earlier event snapshot. This is not scientific approval.
        Callers must not mutate the shared receipt dicts.
        """
        self._sync(receipts=True)
        return list(self._receipts)

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
        checkpoint = self._checkpoint()
        outer_memo, self._cas_memo = self._cas_memo, _CasMemo()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            owns_transaction = True
            # Under the write lock, reread every event and receipt row and compare
            # it with the verified copy, whatever the fingerprint says.
            self._sync(receipts=True, reread=True)
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
                self._restore(checkpoint)
            raise
        finally:
            self._command_context = None
            self._command_rollback_only = False
            self._cas_memo = None if outer_memo is None else _CasMemo()

    def export_receipts(self) -> str:
        """Delivery-ledger backup; events-only JSONL cannot preserve idempotency."""
        return "".join(canonical(receipt).decode() + "\n" for receipt in self.receipts())

    def export(self) -> str:
        return "".join(canonical(event).decode() + "\n" for event in self.events())
