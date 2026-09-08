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
from typing import Any


class IntegrityError(ValueError):
    """Stored evidence or the event chain failed verification."""


class ConflictError(ValueError):
    """A stale writer must reload state before retrying its decision."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Store:
    def __init__(self, root: str | Path, *, read_only: bool = False):
        self.root = Path(root).resolve()
        self.read_only = read_only
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
        try:
            self.db.execute("BEGIN IMMEDIATE")
            history = self.events()
            if len(history) != expected_revision:
                raise ConflictError("research state changed; reload before retry")
            event = dict(seq=len(history) + 1, id=id, kind=kind, actor=actor, role=role,
                         created_at=datetime.now(timezone.utc).isoformat(), schema_version=1,
                         payload=payload,
                         previous_hash=history[-1]["hash"] if history else "0" * 64)
            event_hash = digest(canonical(event))
            self.db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (event["seq"], id, kind, actor, role, event["created_at"],
                             event["schema_version"],
                             canonical(payload).decode(), event["previous_hash"], event_hash))
            self.db.commit()
            return dict(event, hash=event_hash)
        except BaseException:
            self.db.rollback()
            raise

    def export(self) -> str:
        return "".join(canonical(event).decode() + "\n" for event in self.events())
