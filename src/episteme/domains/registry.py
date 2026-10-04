"""Explicit DomainPack allowlist and pinning of complete pack code (ADR 0016).

Adding a pack is a reviewed change of ``PACKS``; there is no entry-point or
directory discovery. ``load_pack`` hashes every file of the pack directory and
executes exactly those bytes under a private module name, so the code that runs
is the code that ``pack_code_digest`` identifies.

A pack remains trusted local Python in the kernel process. The manifest detects
accidental drift and wrong versions; it does not detect malicious code, a
substituted interpreter or standard library, or reads outside the pack contract.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
from pathlib import Path
import re
import stat
import sys
import threading
from types import MappingProxyType, ModuleType
from typing import Any, Callable, Mapping

from ..store import canonical, digest
from . import api


# pack_id -> importable package whose directory contains all pack files.
PACKS: dict[str, str] = {
    "synthetic_causal_v1": "episteme.domains.packs.synthetic_causal_v1",
    "afterlife_seed_v1": "episteme.domains.packs.afterlife_seed_v1",
    "tabular_classification_v1": "episteme.domains.packs.tabular_classification_v1",
}

# adapter_id -> (module, class) of the legacy batch analysis adapters that
# analysis.apply recomputes (ADR 0013-0015, ADR 0018). Explicit, no discovery.
LEGACY_ANALYSIS_ADAPTERS: dict[str, tuple[str, str]] = {
    "synthetic_causal_v1": ("episteme.domains.synthetic_batch_analysis",
                            "SyntheticCausalBatchAnalysisAdapter"),
    "afterlife_seed_v1": ("episteme.domains.afterlife_seed_batch_analysis",
                          "AfterlifeSeedBatchAnalysisAdapter"),
}


def legacy_analysis_adapter(adapter_id: str) -> tuple[Any, bytes]:
    """A registered legacy adapter instance and the bytes of its module source.

    The source digest identifies the code that will run; like a pack, the
    adapter remains trusted local code in the kernel process.
    """
    if adapter_id not in LEGACY_ANALYSIS_ADAPTERS:
        raise ValueError(f"no registered legacy analysis adapter: {adapter_id}")
    module_name, class_name = LEGACY_ANALYSIS_ADAPTERS[adapter_id]
    module = importlib.import_module(module_name)
    return getattr(module, class_name)(), Path(module.__file__).read_bytes()


HOOKS = ("describe", "validate_parameters", "compile_protocol", "compile_execution",
         "validate_protocol", "validate_outputs", "recompute_metrics", "analyse")
CAPTURE_HOOK = "capture"
MAX_PACK_FILES = 256
MAX_PACK_BYTES = 4 * 1024 * 1024
ALLOWED_EPISTEME_IMPORTS = {"episteme.domains.api"}
# Standard-library modules whose use would break hook purity (time, randomness,
# processes, network, environment) or the pinned-code boundary (dynamic import).
DENIED_STDLIB = {
    "_thread", "asyncio", "builtins", "code", "codeop", "concurrent", "ctypes", "datetime",
    "dbm", "ftplib", "gc", "getpass", "http", "imaplib", "imp", "importlib", "inspect",
    "marshal", "msvcrt", "multiprocessing", "os", "pickle", "pkgutil", "platform", "poplib",
    "pty", "random", "resource", "runpy", "secrets", "select", "selectors", "shelve", "shutil",
    "signal", "smtplib", "socket", "sqlite3", "ssl", "subprocess", "sys", "telnetlib",
    "tempfile", "termios", "threading", "time", "tty", "urllib", "uuid", "webbrowser",
    "winreg", "winsound", "xmlrpc", "zoneinfo"}
DENIED_CALLS = {"__import__", "breakpoint", "compile", "eval", "exec", "input"}
# File access belongs only to the read-only capture hook, kept in capture.py.
CAPTURE_MODULE = "capture.py"
FILE_IO_STDLIB = {"bz2", "fileinput", "glob", "gzip", "io", "logging", "lzma", "mmap",
                  "pathlib", "tarfile", "zipfile"}
FILE_IO_CALLS = {"open"}
_PACK_ID = re.compile(r"[a-z][a-z0-9_]{1,63}\Z")
_LOCK = threading.Lock()
_LOADED: dict[str, LoadedPack] = {}


class PackError(ValueError):
    """A DomainPack is unregistered, malformed, drifted or differs from its pin."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PackError(message)


def _unsafe(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def pack_files(root: Path) -> dict[str, bytes]:
    """Every regular file of a pack directory except bytecode caches."""
    _require(root.is_dir() and not _unsafe(root), f"pack root must be a plain directory: {root}")
    files: dict[str, bytes] = {}
    total = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        for entry in sorted(directory.iterdir()):
            _require(not _unsafe(entry), f"pack contains a link or reparse point: {entry}")
            if entry.is_dir():
                if entry.name != "__pycache__":
                    pending.append(entry)
                continue
            _require(entry.is_file(), f"pack contains a non-regular file: {entry}")
            relative = entry.relative_to(root).as_posix()
            # Installed wheels contain only Python modules; another file kind
            # would make source and installed pack digests disagree.
            _require(relative.endswith(".py"), f"pack files must be Python modules: {relative}")
            data = entry.read_bytes()
            total += len(data)
            _require(len(files) < MAX_PACK_FILES and total <= MAX_PACK_BYTES,
                     "pack code exceeds its file count or size limit")
            files[relative] = data
    _require("__init__.py" in files, "pack directory needs __init__.py")
    return dict(sorted(files.items()))


def code_manifest(pack_id: str, files: Mapping[str, bytes]) -> dict[str, Any]:
    return api.validate_code_manifest(dict(
        schema_version=1, pack_id=pack_id,
        files=[dict(path=path, sha256=digest(data), bytes=len(data))
               for path, data in sorted(files.items())]))


def code_digest(manifest: Mapping[str, Any]) -> str:
    return digest(canonical(dict(manifest)))


def import_violations(files: Mapping[str, bytes]) -> list[str]:
    """Static check: stdlib, ``episteme.domains.api`` and the pack's own modules only."""
    stdlib = set(sys.stdlib_module_names)
    violations: list[str] = []

    def module_allowed(name: str, capture: bool) -> bool:
        if name in ALLOWED_EPISTEME_IMPORTS:
            return True
        top = name.split(".")[0]
        return (top in stdlib and top not in DENIED_STDLIB
                and (capture or top not in FILE_IO_STDLIB))

    for path, data in sorted(files.items()):
        capture = path == CAPTURE_MODULE
        denied_calls = DENIED_CALLS | (set() if capture else FILE_IO_CALLS)
        try:
            tree = ast.parse(data, filename=path)
        except (SyntaxError, ValueError) as exc:
            violations.append(f"{path}: cannot parse: {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                violations.extend(f"{path}:{node.lineno}: import {alias.name}"
                                  for alias in node.names if not module_allowed(alias.name, capture))
            elif isinstance(node, ast.ImportFrom):
                if node.level == 1:
                    continue
                if node.level > 1:
                    violations.append(f"{path}:{node.lineno}: relative import outside the pack")
                elif node.module == "episteme.domains" and [a.name for a in node.names] == ["api"]:
                    continue
                elif node.module is None or not module_allowed(node.module, capture):
                    violations.append(f"{path}:{node.lineno}: from {node.module} import")
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id in denied_calls):
                violations.append(f"{path}:{node.lineno}: call {node.func.id}()")
    return violations


class _Loader(importlib.abc.Loader):
    def __init__(self, files: Mapping[str, bytes], root: Path):
        self.files, self.root = files, root

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        relative = module.__spec__.loader_state  # type: ignore[union-attr]
        code = compile(self.files[relative], str(self.root / relative), "exec", dont_inherit=True)
        exec(code, module.__dict__)


class _Finder(importlib.abc.MetaPathFinder):
    """Serve one private package from in-memory hashed bytes, never from disk."""

    def __init__(self, name: str, files: Mapping[str, bytes], root: Path):
        self.name, self.files, self.root = name, files, root
        self.loader = _Loader(files, root)

    def find_spec(self, fullname: str, path: Any = None, target: Any = None
                  ) -> importlib.machinery.ModuleSpec | None:
        if fullname != self.name and not fullname.startswith(self.name + "."):
            return None
        relative = fullname[len(self.name) + 1:].replace(".", "/") if fullname != self.name else ""
        candidates = ["__init__.py"] if not relative else [f"{relative}.py", f"{relative}/__init__.py"]
        for candidate in candidates:
            if candidate in self.files:
                package = candidate.endswith("__init__.py")
                spec = importlib.machinery.ModuleSpec(
                    fullname, self.loader, origin=str(self.root / candidate),
                    loader_state=candidate, is_package=package)
                spec.has_location = True
                if package:
                    spec.submodule_search_locations = []
                return spec
        return None


@dataclass(frozen=True)
class LoadedPack:
    """A pack executed from exactly the bytes listed in ``code_manifest``."""

    pack_id: str
    manifest: api.PackManifest
    code_manifest: Mapping[str, Any]
    pack_code_digest: str
    files: Mapping[str, bytes]
    module: ModuleType
    root: Path

    @property
    def pack_version(self) -> str:
        return self.manifest.pack_version

    def hook(self, name: str) -> Callable[..., Any]:
        _require(name in HOOKS or (name == CAPTURE_HOOK and self.manifest.capture),
                 f"pack {self.pack_id} has no hook {name}")
        return getattr(self.module, name)

    def manifest_json(self) -> dict[str, Any]:
        return self.manifest.to_dict()

    def require_pin(self, *, pack_id: str, pack_version: str, pack_code_digest: str) -> None:
        _require((self.pack_id, self.pack_version) == (pack_id, pack_version),
                 f"pack identity {self.pack_id} {self.pack_version} differs from the pinned "
                 f"{pack_id} {pack_version}")
        _require(self.pack_code_digest == pack_code_digest,
                 f"pack code {self.pack_code_digest} differs from the pinned {pack_code_digest}")


def _package_root(module_name: str) -> Path:
    spec = importlib.util.find_spec(module_name)  # type: ignore[attr-defined]
    _require(spec is not None and spec.submodule_search_locations is not None
             and len(list(spec.submodule_search_locations)) == 1,
             f"pack module must be a regular package: {module_name}")
    return Path(next(iter(spec.submodule_search_locations))).resolve()


def load_pack(pack_id: str) -> LoadedPack:
    """Hash the pack directory now and run those bytes; drift yields a new digest."""
    _require(type(pack_id) is str and _PACK_ID.fullmatch(pack_id) is not None,
             "invalid pack ID")
    _require(pack_id in PACKS, f"pack is not in the explicit registry: {pack_id}")
    root = _package_root(PACKS[pack_id])
    _require(root.name == pack_id, f"pack directory name must equal its ID: {root}")
    files = pack_files(root)
    manifest = code_manifest(pack_id, files)
    key = code_digest(manifest)
    with _LOCK:
        cached = _LOADED.get(pack_id)
        if cached is not None and cached.pack_code_digest == key and cached.root == root:
            return cached
        violations = import_violations(files)
        _require(not violations, f"pack {pack_id} violates the import contract: "
                 + "; ".join(violations))
        name = f"_episteme_pack_{pack_id}_{key}"
        if name not in sys.modules:
            frozen = MappingProxyType(dict(files))
            sys.meta_path.insert(0, _Finder(name, frozen, root))
            try:
                module = importlib.import_module(name)
            except BaseException:
                sys.modules.pop(name, None)
                raise
        module = sys.modules[name]
        declared = getattr(module, "MANIFEST", None)
        try:
            parsed = api.PackManifest.from_dict(declared)
        except ValueError as exc:
            raise PackError(f"pack {pack_id} has an invalid manifest: {exc}") from exc
        _require(parsed.pack_id == pack_id, "pack manifest ID differs from its registry key")
        missing = [hook for hook in HOOKS if not callable(getattr(module, hook, None))]
        _require(not missing, f"pack {pack_id} lacks hooks: {', '.join(missing)}")
        _require(callable(getattr(module, CAPTURE_HOOK, None)) == parsed.capture,
                 "pack capture hook must exist exactly when its manifest declares capture")
        loaded = LoadedPack(pack_id=pack_id, manifest=parsed,
                            code_manifest=api.freeze(manifest), pack_code_digest=key,
                            files=MappingProxyType(dict(files)), module=module, root=root)
        _LOADED[pack_id] = loaded
        return loaded


def registered() -> list[str]:
    return sorted(PACKS)
