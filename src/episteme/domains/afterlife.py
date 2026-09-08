"""Bounded, read-only import of Semantic Afterlife historical records.

``inspect`` reads a local checkout without executing its code or commands. The
returned snapshot freezes the inspected metadata and verification observations;
``import_snapshot`` persists that snapshot, not a later reread of the checkout.
Historical records never become kernel protocols, accepted claims or reviews.
Even matching legacy hashes establish byte identity only, not preregistration,
source closure, independent replication, or scientific validity.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from ..store import ConflictError, IntegrityError, Store, canonical, digest


MIB = 1024 * 1024
SCHEMA = "afterlife-historical-snapshot-v1"


def _reject_constant(value: str) -> None:
    raise ValueError(f"nonfinite JSON constant: {value}")


@dataclass(frozen=True)
class AfterlifeSnapshot:
    """Frozen bytes; modifying ``inventory`` cannot change the snapshot."""

    content: bytes
    blobs: tuple[tuple[str, bytes], ...]

    @property
    def id(self) -> str:
        return digest(self.content)

    @property
    def inventory(self) -> dict[str, Any]:
        return json.loads(self.content)

    def report(self) -> dict[str, Any]:
        inventory = self.inventory
        runs = inventory["runs"]
        checks = Counter(row["verification"] for run in runs for row in run["outputs"])
        return {
            "snapshot_id": self.id, "source": inventory["source"]["path"],
            "git_sha": inventory["source"]["git"].get("sha"),
            "source_dirty": inventory["source"]["git"].get("dirty"),
            "runs": len(runs),
            "run_statuses": dict(sorted(Counter(run["legacy_status"] for run in runs).items())),
            "superseded_runs": sum(run["superseded"] for run in runs),
            "dirty_runs": sum(run["legacy_git"].get("dirty") is True for run in runs),
            "run_integrity": dict(sorted(Counter(run["integrity_status"] for run in runs).items())),
            "output_checks": dict(sorted(checks.items())),
            "documents": len(inventory["documents"]), "artifacts": len(inventory["artifacts"]),
            "captured_blobs": len(self.blobs), "captured_bytes": sum(len(data) for _, data in self.blobs),
            "verified_bytes": inventory["verified_bytes"],
            "warnings": len(inventory["warnings"]),
            "trust": "historical_unverified", "scientific_validity": "not_assessed",
            "preregistered_protocols_created": 0, "accepted_claims_created": 0,
        }


def _relative_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value or "\x00" in value or ":" in value:
        return None
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or PureWindowsPath(value).is_absolute() or ".." in path.parts:
        return None
    if normalized in {".", ""} or normalized.startswith("/"):
        return None
    return path.as_posix()


def _safe_path(root: Path, relative: str) -> Path:
    normalized = _relative_path(relative)
    if normalized is None:
        raise ValueError("unsafe relative path")
    path = root
    for part in PurePosixPath(normalized).parts:
        path = path / part
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("symlinks and reparse points are not imported")
    if not path.resolve().is_relative_to(root):
        raise ValueError("path escapes source root")
    return path


def _git(root: Path) -> dict[str, Any]:
    values: dict[str, Any] = {"sha": None, "branch": None, "dirty": None, "status": None}
    commands = {"sha": ["rev-parse", "HEAD"], "branch": ["rev-parse", "--abbrev-ref", "HEAD"],
                "status": ["status", "--porcelain=v1", "--untracked-files=all"]}
    for key, args in commands.items():
        try:
            result = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
                                    cwd=root, capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and len(result.stdout) <= 2 * MIB:
            values[key] = result.stdout.decode("utf-8", errors="replace").strip()
    if values["status"] is not None:
        values["dirty"] = bool(values["status"])
    return values


class _Inspector:
    def __init__(self, root: Path, limits: dict[str, int]):
        self.root, self.limits = root, limits
        self.blobs: dict[str, bytes] = {}
        self.captured = self.verified = 0
        self.warnings: list[dict[str, str]] = []

    def paths(self, directory: str, pattern: str) -> list[Path]:
        """Only descend through ordinary directories, including on Windows."""
        try:
            start = _safe_path(self.root, directory)
        except (OSError, ValueError):
            self.warnings.append({"path": directory, "reason": "unsafe_directory"})
            return []
        if not start.is_dir():
            return []
        found: list[Path] = []
        pending = [start]
        while pending:
            current = pending.pop()
            try:
                children = sorted(current.iterdir())
            except OSError:
                self.warnings.append({"path": current.relative_to(self.root).as_posix(),
                                      "reason": "unreadable_directory"})
                continue
            for child in children:
                relative = child.relative_to(self.root).as_posix()
                try:
                    child = _safe_path(self.root, relative)
                    if child.is_dir():
                        pending.append(child)
                    elif child.is_file() and child.match(pattern):
                        found.append(child)
                except (OSError, ValueError):
                    self.warnings.append({"path": relative, "reason": "unsafe_or_unreadable_path"})
        return sorted(found)

    def capture(self, data: bytes) -> str | None:
        key = digest(data)
        if key in self.blobs:
            return key
        if self.captured + len(data) > self.limits["max_capture_bytes"]:
            return None
        self.blobs[key] = data
        self.captured += len(data)
        return key

    def metadata(self, path: Path, *, parse_json: bool = False) -> tuple[dict[str, Any], Any]:
        relative = path.relative_to(self.root).as_posix()
        record: dict[str, Any] = {"path": relative, "blob": None, "status": "unreadable"}
        value = None
        try:
            path = _safe_path(self.root, relative)
            size = path.stat().st_size
            record["size_bytes"] = size
            if size > self.limits["max_metadata_bytes"]:
                record["status"] = "metadata_size_limit"
                return record, None
            with path.open("rb") as stream:
                data = stream.read(self.limits["max_metadata_bytes"] + 1)
            if len(data) > self.limits["max_metadata_bytes"]:
                record["status"] = "metadata_size_limit"
                return record, None
            record["sha256"] = digest(data)
            record["blob"] = self.capture(data)
            record["status"] = "captured" if record["blob"] else "capture_budget_exhausted"
            if parse_json:
                value = json.loads(data, parse_constant=_reject_constant)
                canonical(value)
                if not isinstance(value, dict):
                    raise ValueError("metadata must be a JSON object")
            else:
                value = data.decode("utf-8", errors="replace")
        except (OSError, ValueError, UnicodeError, RecursionError):
            record["status"] = "invalid_json" if parse_json and record.get("sha256") else "unreadable"
            value = None
        finally:
            if record["status"] != "captured":
                self.warnings.append({"path": relative, "reason": record["status"]})
        return record, value

    def output(self, run_root: Path, name: Any, expected: Any) -> dict[str, Any]:
        row: dict[str, Any] = {"path": str(name), "expected_sha256": expected,
                              "actual_sha256": None, "blob": None, "verification": "invalid_reference"}
        relative = _relative_path(name)
        if relative is None:
            return row
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
            row["verification"] = "invalid_expected_hash"
            return row
        try:
            path = _safe_path(self.root, (run_root / relative).relative_to(self.root).as_posix())
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                return row
            row["size_bytes"] = info.st_size
            if info.st_size > self.limits["max_verify_file_bytes"]:
                row["verification"] = "verification_size_limit"
                return row
            if self.verified + info.st_size > self.limits["max_verify_total_bytes"]:
                row["verification"] = "verification_budget_exhausted"
                return row
            hasher = hashlib.sha256()
            copy_limit = self.limits["copy_outputs_under_bytes"]
            copied: list[bytes] | None = [] if copy_limit > 0 and info.st_size <= copy_limit else None
            read = 0
            with path.open("rb") as stream:
                while chunk := stream.read(min(MIB, self.limits["max_verify_file_bytes"] - read + 1,
                                                self.limits["max_verify_total_bytes"] - self.verified + 1)):
                    read += len(chunk)
                    self.verified += len(chunk)
                    if read > self.limits["max_verify_file_bytes"] or self.verified > self.limits["max_verify_total_bytes"]:
                        row["verification"] = "changed_during_read"
                        return row
                    hasher.update(chunk)
                    if copied is not None:
                        if read <= self.limits["copy_outputs_under_bytes"]:
                            copied.append(chunk)
                        else:
                            copied = None
            after = path.stat()
            if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or read != info.st_size:
                row["verification"] = "changed_during_read"
                return row
            row["actual_sha256"] = hasher.hexdigest()
            row["verification"] = "matched" if row["actual_sha256"] == expected.lower() else "mismatch"
            if copied is not None:
                row["blob"] = self.capture(b"".join(copied))
        except FileNotFoundError:
            row["verification"] = "missing"
        except (OSError, ValueError):
            row["verification"] = "unsafe_or_unreadable"
        return row


def inspect(source: str | Path, *, max_metadata_bytes: int = 2 * MIB,
            max_verify_file_bytes: int = 64 * MIB, max_verify_total_bytes: int = 256 * MIB,
            copy_outputs_under_bytes: int = 0, max_capture_bytes: int = 32 * MIB) -> AfterlifeSnapshot:
    """Observe metadata and available declared hashes, with bounded byte budgets.

    Output bytes are not copied by default. Explicitly enable a small threshold
    for fixtures; neither inspection nor import executes any historical command.
    The inventory records skipped/missing/corrupt files and never equates a
    complete declared hash list with complete provenance or scientific validity.
    """
    root = Path(source).resolve()
    if not root.is_dir():
        raise ValueError("afterlife source must be an existing directory")
    limits = dict(max_metadata_bytes=max_metadata_bytes, max_verify_file_bytes=max_verify_file_bytes,
                  max_verify_total_bytes=max_verify_total_bytes, copy_outputs_under_bytes=copy_outputs_under_bytes,
                  max_capture_bytes=max_capture_bytes)
    if any(type(value) is not int or value < 0 for value in limits.values()):
        raise ValueError("byte limits must be nonnegative integers")
    scanner = _Inspector(root, limits)
    git_before = _git(root)
    runs = []
    for path in scanner.paths("runs", "manifest.json"):
        record, manifest = scanner.metadata(path, parse_json=True)
        manifest = manifest or {}
        markers: dict[str, Any] = {}
        for name in ("STATUS", "SUPERSEDED", "FAILED", "config.resolved.yaml"):
            candidate = path.parent / name
            if candidate.exists() or candidate.is_symlink():
                marker, value = scanner.metadata(candidate)
                markers[name] = {**marker, "text": value if name != "config.resolved.yaml" else None}
        inventory = manifest.get("integrity")
        outputs = [scanner.output(path.parent, name, expected) for name, expected in sorted(inventory.items())] if isinstance(inventory, dict) else []
        statuses = {row["verification"] for row in outputs}
        integrity_status = "declared_hashes_matched" if outputs and statuses == {"matched"} else "historical_unverified"
        if "mismatch" in statuses:
            integrity_status = "integrity_mismatch"
        legacy_git = manifest.get("git") if isinstance(manifest.get("git"), dict) else {}
        notes = manifest.get("notes")
        superseded = bool(manifest.get("superseded") or "SUPERSEDED" in markers or
                          (isinstance(notes, list) and any(isinstance(note, str) and "SUPERSEDED" in note for note in notes)))
        status_marker = markers.get("STATUS", {}).get("text")
        if isinstance(status_marker, str) and status_marker.strip() != manifest.get("status"):
            scanner.warnings.append({"path": record["path"], "reason": "status_marker_conflicts_with_manifest"})
        runs.append({
            "external_id": "afterlife:" + str(manifest.get("run_id", path.parent.name)),
            "manifest": record, "legacy_run_id": manifest.get("run_id"), "legacy_status": str(manifest.get("status", "UNKNOWN")),
            "legacy_stage": manifest.get("stage"), "execution_mode": manifest.get("execution_mode"),
            "started_at": manifest.get("started_at"), "finished_at": manifest.get("finished_at"),
            "legacy_git": legacy_git, "markers": markers, "superseded": superseded,
            "supersession": manifest.get("superseded"), "notes": notes,
            "config_sha256": manifest.get("config_sha256"),
            "source_run_id": (manifest.get("config_resolved") or {}).get("source_run_id") if isinstance(manifest.get("config_resolved"), dict) else None,
            "outputs": outputs, "integrity_status": integrity_status,
            "trust": "historical_unverified", "scientific_validity": "not_assessed",
            "preregistration": "not_established", "source_closure": "not_established",
            "independent_replication": "not_established",
        })
    documents = []
    for path in scanner.paths("docs", "*.md"):
        name = path.name
        if name not in {"PLAN.md", "REPORT.md", "HANDOFF.md", "REVIEW.md", "research-plan.md"} and not name.startswith("ADR-"):
            continue
        record, _ = scanner.metadata(path)
        kind = "imported_protocol" if name == "PLAN.md" else "historical_decision" if name.startswith("ADR-") else "historical_document"
        documents.append({**record, "kind": kind, "trust": "historical_unverified", "preregistered": False})
    artifacts = []
    known_runs = {run["legacy_run_id"] for run in runs if isinstance(run["legacy_run_id"], str)}
    for path in scanner.paths("artifacts", "*.meta.json"):
        record, metadata = scanner.metadata(path, parse_json=True)
        metadata = metadata or {}
        stem = path.name.removesuffix(".meta.json")
        # Metadata only: large figure/raw-data files stay as external references.
        companions = []
        for suffix in (".data.parquet", ".data.npz", ".csv", ".png", ".svg", ".pdf", ".html"):
            candidate = path.parent / (stem + suffix)
            if candidate.exists():
                try:
                    candidate = _safe_path(root, candidate.relative_to(root).as_posix())
                    companions.append({"path": candidate.relative_to(root).as_posix(), "size_bytes": candidate.stat().st_size,
                                       "verification": "not_declared_in_artifact_metadata", "blob": None})
                except (OSError, ValueError):
                    companions.append({"path": candidate.relative_to(root).as_posix(), "verification": "unsafe_or_unreadable", "blob": None})
        run_ids = metadata.get("run_ids")
        lineage = "resolved_external_ids" if isinstance(run_ids, list) and run_ids and all(
            isinstance(run_id, str) and run_id in known_runs for run_id in run_ids) else "historical_unverified"
        artifacts.append({"metadata": record, "run_ids": run_ids, "lineage": lineage, "name": metadata.get("name"),
                          "caption": metadata.get("caption"), "limitations": metadata.get("limitations"),
                          "companions": companions, "trust": "historical_unverified", "scientific_validity": "not_assessed"})
    sources = []
    for name in ("LICENSE", "pyproject.toml", "src/semantic_afterlife/provenance.py", "src/semantic_afterlife/hashing.py",
                 "src/semantic_afterlife/runctx.py", "src/semantic_afterlife/paths.py"):
        path = root / name
        if path.exists():
            record, _ = scanner.metadata(path)
            sources.append(record)
    git_after = _git(root)
    if git_after != git_before:
        scanner.warnings.append({"path": ".", "reason": "git_state_changed_during_inspection"})
    inventory = {
        "schema": SCHEMA, "adapter": "afterlife", "source": {"path": root.as_posix(), "git": git_before,
            "git_after": git_after, "source_files": sources, "source_closure": "not_established"},
        "limits": limits, "runs": runs, "documents": documents, "artifacts": artifacts,
        "verified_bytes": scanner.verified, "warnings": scanner.warnings,
        "blob_digests": sorted(scanner.blobs), "trust": "historical_unverified",
        "limitations": ["Historical timestamps and plans do not establish preregistration.",
                        "Hash checks cover declared paths only; undeclared outputs and full source closure are unverified.",
                        "The checkout is observed sequentially, not as an atomic filesystem snapshot.",
                        "Role labels, mock/replay modes and distinct source hashes do not establish independent replication.",
                        "No scientific approval or accepted claim is created."],
    }
    return AfterlifeSnapshot(canonical(inventory), tuple(sorted(scanner.blobs.items())))


def import_snapshot(store: Store, snapshot: AfterlifeSnapshot, *, actor: str = "afterlife-importer") -> dict[str, Any]:
    """Atomically append one idempotent import event for frozen inspection bytes.

    All required captured blobs are verified before committing. A concurrent
    writer produces ``ConflictError``; retrying the same snapshot is safe.
    Imported metadata remains in a separate namespace from live kernel records.
    """
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("import actor is required")
    inventory = snapshot.inventory
    if inventory.get("schema") != SCHEMA or inventory.get("trust") != "historical_unverified":
        raise IntegrityError("unsupported historical snapshot")
    if store.root.is_relative_to(Path(inventory["source"]["path"]).resolve()):
        raise ValueError("import destination must be outside the read-only source checkout")
    blobs = dict(snapshot.blobs)
    if len(blobs) != len(snapshot.blobs) or sorted(blobs) != inventory.get("blob_digests"):
        raise IntegrityError("snapshot blob inventory mismatch")
    for key, data in blobs.items():
        if digest(data) != key:
            raise IntegrityError("snapshot blob digest mismatch")
    event_id = "afterlife:" + snapshot.id
    payload = {"adapter": "afterlife", "snapshot": snapshot.id, "trust": "historical_unverified",
               "source": inventory["source"]["path"], "report": snapshot.report()}
    history = store.events()
    existing = next((event for event in history if event["id"] == event_id), None)
    if existing:
        if existing["kind"] != "afterlife_snapshot" or existing["payload"] != payload:
            raise ConflictError("historical snapshot id already has a different payload")
        store.read(snapshot.id)
        for key in blobs:
            store.read(key)
        return {**snapshot.report(), "event_id": event_id, "created": False}
    for key, data in blobs.items():
        store.put(data)
    store.put(snapshot.content)
    store.append(id=event_id, kind="afterlife_snapshot", actor=actor, role="historical_importer",
                 payload=payload, expected_revision=len(history))
    return {**snapshot.report(), "event_id": event_id, "created": True}
