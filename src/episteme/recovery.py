"""Portable, verified local state copies; no event replay or experiment execution.

A SQLite online backup captures one database revision. The append-only CAS is
copied afterwards, including observed orphans: this is an artifact superset,
not a simultaneous filesystem snapshot. Publication reserves a new directory
exclusively and installs its readiness file last. A crash may leave an incomplete
reserved directory; existing destinations are never reused or overlaid.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from typing import Any, Iterator

from .graph import ResearchGraph
from .store import IntegrityError, Store, canonical, digest


_DIGEST = re.compile(r"[0-9a-f]{64}")
_FORMAT = "episteme-state-directory"
_MANIFEST_KEYS = {"format", "version", "created_at", "database", "artifacts",
                  "revision", "snapshot_hash", "receipt_count", "receipts_hash"}


def _plain(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
    if reparse or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise IntegrityError(f"expected a plain {'directory' if directory else 'file'}: {path}")


def _destination(source: Path, destination: str | Path) -> Path:
    requested = Path(destination).absolute()
    if os.path.lexists(requested):
        raise FileExistsError(f"destination already exists: {requested}")
    parent = requested.parent.resolve(strict=True)
    _plain(parent, directory=True)
    target = parent / requested.name
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError("source and destination must not overlap")
    return target


@contextmanager
def _stage(parent: Path) -> Iterator[Path]:
    temporary = tempfile.TemporaryDirectory(prefix=".episteme-recovery-", dir=parent)
    path = Path(temporary.name).resolve(strict=True)
    # Only this newly allocated sibling may be removed by TemporaryDirectory.
    if path.parent != parent or not path.name.startswith(".episteme-recovery-"):
        raise IntegrityError("unexpected recovery staging path")
    try:
        yield path
    finally:
        _plain(path, directory=True)
        temporary.cleanup()


def _copy(source: Path, target: Path, *, expected_hash: str | None = None,
          expected_size: int | None = None) -> dict[str, Any]:
    _plain(source)
    checksum, size = hashlib.sha256(), 0
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        while block := incoming.read(1024 * 1024):
            size += len(block)
            if expected_size is not None and size > expected_size:
                raise IntegrityError(f"file size mismatch: {source.name}")
            checksum.update(block)
            outgoing.write(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    value = checksum.hexdigest()
    if expected_hash is not None and value != expected_hash:
        raise IntegrityError(f"file hash mismatch: {source.name}")
    if expected_size is not None and size != expected_size:
        raise IntegrityError(f"file size mismatch: {source.name}")
    return dict(sha256=value, size=size)


def _state(root: Path) -> dict[str, Any]:
    with Store(root, read_only=True) as store:
        if store.db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise IntegrityError("snapshot database must be standalone in DELETE journal mode")
        check = [row[0] for row in store.db.execute("PRAGMA integrity_check")]
        if check != ["ok"]:
            raise IntegrityError("SQLite integrity check failed")
        history, receipts = store.events(), store.receipts()
        graph = ResearchGraph.from_store(store)  # Includes missing/corrupt referenced artifacts.
        return dict(revision=len(history), snapshot_hash=graph.snapshot_hash,
                    receipt_count=len(receipts), receipts_hash=digest(canonical(receipts)))


def _database_backup(store: Store, path: Path) -> None:
    with closing(sqlite3.connect(path)) as target:
        store.db.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")


def _publish(stage: Path, target: Path, *, snapshot: bool) -> None:
    # mkdir is the no-overwrite reservation on both Windows and POSIX. Do not
    # replace a directory: POSIX rename could overwrite an existing empty one.
    target.mkdir(exist_ok=False)
    (stage / "artifacts").rename(target / "artifacts")
    (stage / "state.sqlite3").rename(target / "state.sqlite3")
    if snapshot:
        (stage / "manifest.json").rename(target / "manifest.json")


def backup(store: Store, destination: str | Path) -> dict[str, Any]:
    """Copy a coherent DB plus all observed digest-named CAS files to a new path."""
    if store.db.in_transaction:
        raise ValueError("backup requires a connection outside any active transaction")
    target = _destination(store.root, destination)
    _plain(store.root, directory=True)
    _plain(store.root / "state.sqlite3")
    _plain(store.root / "artifacts", directory=True)
    _plain(store.blobs, directory=True)
    with _stage(target.parent) as stage:
        database = stage / "state.sqlite3"
        _database_backup(store, database)
        blobs = stage / "artifacts" / "sha256"
        blobs.mkdir(parents=True)
        artifacts = {}
        for path in sorted(store.blobs.iterdir()):
            if _DIGEST.fullmatch(path.name):
                artifacts[path.name] = _copy(path, blobs / path.name, expected_hash=path.name)["size"]
        identity = _state(stage)
        with database.open("rb") as stream:
            database_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest = dict(format=_FORMAT, version=1, created_at=datetime.now(timezone.utc).isoformat(),
                        database=dict(sha256=database_hash, size=database.stat().st_size),
                        artifacts=artifacts, **identity)
        (stage / "manifest.json").write_bytes(canonical(manifest))
        _publish(stage, target, snapshot=True)
    return manifest


def _manifest(snapshot: Path) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise IntegrityError(f"duplicate manifest key: {key}")
            result[key] = value
        return result

    path = snapshot / "manifest.json"
    _plain(path)
    with path.open("rb") as stream:
        raw = stream.read(64 * 1024 * 1024 + 1)
    if len(raw) > 64 * 1024 * 1024:
        raise IntegrityError("manifest exceeds 64 MiB")
    value = json.loads(raw, object_pairs_hook=unique)
    if (type(value) is not dict or set(value) != _MANIFEST_KEYS
            or value["format"] != _FORMAT or type(value["version"]) is not int or value["version"] != 1):
        raise IntegrityError("unsupported recovery manifest")
    if not isinstance(value["created_at"], str) or not value["created_at"].strip():
        raise IntegrityError("manifest creation time required")
    for key in ("revision", "receipt_count"):
        if type(value[key]) is not int or value[key] < 0:
            raise IntegrityError(f"invalid manifest {key}")
    for key in ("snapshot_hash", "receipts_hash"):
        if not isinstance(value[key], str) or not _DIGEST.fullmatch(value[key]):
            raise IntegrityError(f"invalid manifest {key}")
    db = value["database"]
    if (type(db) is not dict or set(db) != {"sha256", "size"}
            or not isinstance(db["sha256"], str) or not _DIGEST.fullmatch(db["sha256"])
            or type(db["size"]) is not int or db["size"] <= 0):
        raise IntegrityError("invalid database manifest")
    if type(value["artifacts"]) is not dict or any(
            not _DIGEST.fullmatch(key) or type(size) is not int or size < 0
            for key, size in value["artifacts"].items()):
        raise IntegrityError("invalid artifact manifest")
    return value


def restore(snapshot: str | Path, destination: str | Path) -> dict[str, Any]:
    """Validate a directory snapshot and restore exact events/receipts into a new root."""
    source = Path(snapshot).absolute()
    _plain(source, directory=True)
    source = source.resolve(strict=True)
    target = _destination(source, destination)
    manifest = _manifest(source)
    if {p.name for p in source.iterdir()} != {"manifest.json", "state.sqlite3", "artifacts"}:
        raise IntegrityError("unexpected snapshot members")
    _plain(source / "artifacts", directory=True)
    if {p.name for p in (source / "artifacts").iterdir()} != {"sha256"}:
        raise IntegrityError("unexpected artifacts directory members")
    incoming = source / "artifacts" / "sha256"
    _plain(incoming, directory=True)
    if {p.name for p in incoming.iterdir()} != set(manifest["artifacts"]):
        raise IntegrityError("artifact inventory mismatch")
    with _stage(target.parent) as stage:
        _copy(source / "state.sqlite3", stage / "state.sqlite3",
              expected_hash=manifest["database"]["sha256"], expected_size=manifest["database"]["size"])
        blobs = stage / "artifacts" / "sha256"
        blobs.mkdir(parents=True)
        for key, size in manifest["artifacts"].items():
            _copy(incoming / key, blobs / key, expected_hash=key, expected_size=size)
        identity = _state(stage)
        if any(manifest[key] != value for key, value in identity.items()):
            raise IntegrityError("snapshot identity does not match manifest")
        _publish(stage, target, snapshot=False)
    return manifest
