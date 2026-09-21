"""A local execution-ownership marker, intentionally outside SQLite and CAS.

This marker prevents automatic adoption of a DB/CAS-only restored batch. It is
not authentication, a host identity or a scientific approval: a filesystem owner
can copy it together with a store. Its digest is frozen in a batch declaration;
read checks never mint a replacement token or transfer authority to a clone.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from typing import Any

from .store import IntegrityError, Store, canonical, digest


MARKER_NAME = ".execution-authority.json"
_MAX_BYTES = 512
_DIGEST = re.compile(r"[0-9a-f]{64}")


class AuthorityError(IntegrityError):
    """The local execution authority is absent, malformed or different."""


def _plain(path: Path, *, directory: bool = False) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise AuthorityError(f"missing or unreadable execution authority path: {path}") from exc
    reparse = getattr(info, "st_file_attributes", 0) & 0x400
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if reparse or not expected:
        raise AuthorityError(f"execution authority requires a plain {'directory' if directory else 'file'}: {path}")


def _marker(store: Store) -> Path:
    _plain(store.root, directory=True)
    return store.root / MARKER_NAME


def _read(path: Path) -> str:
    _plain(path)
    try:
        with path.open("rb") as stream:
            data = stream.read(_MAX_BYTES + 1)
    except OSError as exc:
        raise AuthorityError("execution authority cannot be read") from exc
    if len(data) > _MAX_BYTES:
        raise AuthorityError("execution authority marker exceeds its size limit")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise AuthorityError("duplicate field in execution authority marker")
            result[key] = value
        return result

    try:
        value = json.loads(data, object_pairs_hook=unique)
        valid = (type(value) is dict and set(value) == {"schema_version", "token"}
                 and type(value["schema_version"]) is int and value["schema_version"] == 1
                 and type(value["token"]) is str and _DIGEST.fullmatch(value["token"]) is not None)
        if not valid or canonical(value) != data:
            raise AuthorityError("invalid or noncanonical execution authority marker")
    except (ValueError, TypeError, UnicodeError) as exc:
        if isinstance(exc, AuthorityError):
            raise
        raise AuthorityError("invalid execution authority marker") from exc
    return digest(data)


def establish_authority(store: Store) -> str:
    """Return the existing local marker digest, or atomically publish a new one.

    A completed, fsynced temporary file is linked exclusively at the marker name.
    Concurrent creators therefore observe one complete token, never a partially
    written O_EXCL file. Unsupported atomic hard-link publication fails closed.
    Temporary artifacts from an interrupted creator confer no execution rights.
    """
    if store.read_only:
        raise AuthorityError("read-only store cannot establish execution authority")
    marker = _marker(store)
    if os.path.lexists(marker):
        return _read(marker)
    data = canonical(dict(schema_version=1, token=secrets.token_hex(32)))
    descriptor, name = tempfile.mkstemp(prefix=".execution-authority-", dir=store.root)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, marker)
        except FileExistsError:
            pass  # Another creator published a complete record first.
        except OSError as exc:
            raise AuthorityError("cannot atomically publish local execution authority") from exc
        return _read(marker)
    finally:
        # Only this call's mkstemp file is removed; the published marker persists.
        temporary.unlink(missing_ok=True)


def require_authority(store: Store, expected_digest: str) -> None:
    """Require a matching pre-existing marker, without any filesystem writes."""
    if type(expected_digest) is not str or _DIGEST.fullmatch(expected_digest) is None:
        raise AuthorityError("execution authority digest must be a lowercase SHA-256 value")
    observed = _read(_marker(store))
    if not secrets.compare_digest(observed, expected_digest):
        raise AuthorityError("local execution authority differs from the frozen batch; no automatic transfer")
