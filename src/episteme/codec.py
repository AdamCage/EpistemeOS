"""Canonical JSON and SHA-256 shared by the store and pack-hook subprocess.

Hook children import this module. They do not import ``episteme.store``.
The encoding is the one history hashes use: sorted keys, no spaces, UTF-8,
and no non-finite numbers.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
