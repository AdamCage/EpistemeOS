"""Source-tree manifests and uv-locked environment closures (ADR 0019).

Pure, stdlib-only rules shared by the kernel, the DomainPack facade and the
profile v2 worker, which imports this file without the package. A valid
manifest or closure is complete and well-formed as bytes. It does not show
that a program is correct, that installed code is benign, or that a host left
no influence on a run; those limits are part of the profile, not hidden here.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import stat
import tomllib
from pathlib import Path
from typing import Any, Mapping


PROFILE = "uv_locked_python_v2"
INSTALL_POLICY = "uv_sync_frozen_offline_no_build_copy_v1"
SOURCE_KIND = "source_tree"
PROJECT_FILES = ("pyproject.toml", "uv.lock")
MAX_FILES = 4096
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TREE_BYTES = 256 * 1024 * 1024
MAX_DEPTH = 16
MAX_PATH = 1024
DEFAULT_SET = {"PYTHONHASHSEED": "0", "PYTHONUTF8": "1"}
# Windows programs need these for DLL loading and platform.machine(); values are recorded per run.
DEFAULT_INHERIT = {"Windows": ("NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "SYSTEMDRIVE", "SYSTEMROOT",
                               "WINDIR")}
# Values the worker derives from the job directory; a closure cannot declare them.
CONTROLLED = {"PATH", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "VIRTUAL_ENV"}
PYTHON_SETTABLE = {"PYTHONHASHSEED", "PYTHONUTF8", "PYTHONIOENCODING", "PYTHONWARNINGS",
                   "PYTHONFAULTHANDLER"}
# A heuristic against persisting credentials in CAS; it is not secret scanning.
SENSITIVE = re.compile(r"TOKEN|SECRET|PASSW|CREDENTIAL|API_?KEY|PRIVATE|ACCESS_?KEY|SESSION|COOKIE|AUTH",
                       re.IGNORECASE)
_COMPONENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,127}\Z")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_PYTHON_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?\Z")
_TOOL_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
_MACHINE = re.compile(r"[A-Za-z0-9_.-]{1,32}\Z")
_IMPLEMENTATIONS = {"cpython", "pypy"}
_SYSTEMS = {"Windows", "Linux", "Darwin"}


class ClosureError(ValueError):
    """A source tree, lock or environment closure is malformed or incomplete."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ClosureError(message)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# ------------------------------------------------------------ relative paths


def check_component(name: Any) -> str:
    _require(type(name) is str and _COMPONENT.fullmatch(name) is not None and not name.endswith(".")
             and name.split(".")[0].upper() not in _RESERVED,
             f"path component is not portable: {name!r}")
    return name


def check_path(path: Any) -> str:
    _require(type(path) is str and 0 < len(path) <= MAX_PATH, f"invalid relative path: {path!r}")
    parts = path.split("/")
    _require(len(parts) <= MAX_DEPTH, f"path is nested too deeply: {path}")
    for part in parts:
        check_component(part)
    return path


def _check_rows(rows: Any, *, label: str) -> None:
    _require(type(rows) is list and 0 < len(rows) <= MAX_FILES, f"{label} needs 1-{MAX_FILES} files")
    total = 0
    seen: dict[str, str] = {}
    for row in rows:
        _require(type(row) is dict and set(row) == {"path", "sha256", "bytes"}
                 and type(row["sha256"]) is str and _DIGEST.fullmatch(row["sha256"]) is not None
                 and type(row["bytes"]) is int and 0 <= row["bytes"] <= MAX_FILE_BYTES,
                 f"invalid {label} row")
        path = check_path(row["path"])
        total += row["bytes"]
        parts = path.split("/")
        # Case-insensitive file systems merge prefixes that differ only by case.
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            entry = f"{'file' if depth == len(parts) else 'dir'}:{prefix}"
            _require(seen.setdefault(prefix.lower(), entry) == entry,
                     f"{label} paths collide or differ only by case: {prefix}")
    _require([row["path"] for row in rows] == sorted(row["path"] for row in rows),
             f"{label} paths must be sorted")
    _require(len({row["path"] for row in rows}) == len(rows), f"{label} paths must be unique")
    _require(total <= MAX_TREE_BYTES, f"{label} exceeds {MAX_TREE_BYTES} bytes")


def file_rows(files: Mapping[str, bytes], *, label: str = "file tree") -> list[dict[str, Any]]:
    rows = []
    for path in sorted(files):
        data = files[path]
        _require(type(data) is bytes, f"{label} file must be bytes: {path}")
        rows.append(dict(path=check_path(path), sha256=digest(data), bytes=len(data)))
    _check_rows(rows, label=label)
    return rows


def read_rows(rows: list[dict[str, Any]], read: Any, *, label: str) -> dict[str, bytes]:
    """Bytes of every row through ``read(digest)``; sizes must agree."""
    files = {}
    for row in rows:
        data = read(row["sha256"])
        _require(type(data) is bytes and len(data) == row["bytes"] and digest(data) == row["sha256"],
                 f"{label} file differs from its row: {row['path']}")
        files[row["path"]] = data
    return files


def directory_files(root: Path) -> dict[str, bytes]:
    """Regular files below ``root``; hidden names and ``__pycache__`` are skipped.

    Links and reparse points are refused, not followed. Capturing a directory
    does not make it a clean checkout: whatever bytes are present are frozen.
    """
    root = Path(root)
    _require(is_plain(root, directory=True), f"source root must be a plain directory: {root}")
    files: dict[str, bytes] = {}
    total = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        for entry in sorted(directory.iterdir()):
            if entry.name.startswith(".") or entry.name == "__pycache__":
                continue
            _require(not is_link(entry), f"source tree contains a link or reparse point: {entry}")
            if entry.is_dir():
                pending.append(entry)
                continue
            _require(is_plain(entry), f"source tree contains a non-regular file: {entry}")
            relative = entry.relative_to(root).as_posix()
            check_path(relative)
            data = entry.read_bytes()
            total += len(data)
            _require(len(files) < MAX_FILES and len(data) <= MAX_FILE_BYTES and total <= MAX_TREE_BYTES,
                     "source tree exceeds its file count or size limit")
            files[relative] = data
    _require(bool(files), "source tree has no files")
    return dict(sorted(files.items()))


def is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def is_plain(path: Path, *, directory: bool = False) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    return kind and not is_link(path)


# ------------------------------------------------------------- source trees


def source_manifest(entry_point: str, input_name: str, files: Mapping[str, bytes]) -> dict[str, Any]:
    return check_source_manifest(dict(schema_version=1, kind=SOURCE_KIND, entry_point=entry_point,
                                      input_name=input_name, files=file_rows(files, label="source tree")))


def check_source_manifest(value: Any) -> dict[str, Any]:
    _require(type(value) is dict and set(value) == {"schema_version", "kind", "entry_point",
                                                     "input_name", "files"}
             and type(value["schema_version"]) is int and value["schema_version"] == 1
             and value["kind"] == SOURCE_KIND, "unsupported source tree manifest")
    _check_rows(value["files"], label="source tree")
    check_path(value["entry_point"])
    _require(value["entry_point"].endswith(".py")
             and value["entry_point"] in {row["path"] for row in value["files"]},
             "source tree entry point must be one of its .py files")
    check_component(value["input_name"])
    return value


def tree_digests(manifest: Mapping[str, Any]) -> set[str]:
    return {row["sha256"] for row in manifest["files"]}


# ----------------------------------------------------------------- uv.lock


def lock_summary(data: bytes) -> dict[str, Any]:
    """Packages of a uv.lock and the local wheel files it references.

    Only the root project and registry wheels are accepted. A remote registry
    distribution needs a sha256 hash; a local registry must be a relative path
    so that its wheels can be frozen next to the lock and relocated.
    """
    try:
        lock = tomllib.loads(data.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ClosureError(f"uv.lock is not valid TOML: {exc}") from exc
    _require(lock.get("version") == 1, "unsupported uv.lock format version")
    packages, local = [], set()
    for package in lock.get("package", []):
        _require(type(package) is dict and type(package.get("name")) is str
                 and type(package.get("source")) is dict and len(package["source"]) == 1,
                 "uv.lock package needs a name and one source")
        (kind, location), = package["source"].items()
        if kind in {"virtual", "editable"}:
            _require(location == ".", "only the root project may be virtual or editable in a v2 lock")
            continue
        _require(kind == "registry" and type(location) is str and type(package.get("version")) is str,
                 f"v2 closures install registry wheels only; {package['name']} uses {kind}")
        remote = location.startswith(("https://", "http://"))
        rows = list(package.get("wheels", []))
        _require(remote or "sdist" not in package,
                 f"local source distributions need a build; {package['name']} is refused")
        for row in rows + ([package["sdist"]] if "sdist" in package else []):
            _require(type(row) is dict, "invalid uv.lock distribution row")
            if remote:
                hash_value = row.get("hash")
                _require(type(hash_value) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", hash_value) is not None,
                         f"remote distribution of {package['name']} lacks a sha256 hash")
            else:
                _require(type(row.get("path")) is str, f"local wheel of {package['name']} lacks a path")
                local.add(check_path(f"{_relative_registry(location)}/{row['path']}"))
        packages.append(dict(name=normalize_name(package["name"]), version=package["version"],
                             source="remote_registry" if remote else "local_registry"))
    return dict(packages=sorted(packages, key=lambda row: (row["name"], row["version"])),
                local_files=sorted(local))


def _relative_registry(location: str) -> str:
    location = location[2:] if location.startswith("./") else location
    _require(not re.match(r"[A-Za-z]:|/|\\", location) and "\\" not in location
             and ".." not in location.split("/"),
             f"local registry must be a relative directory below the project: {location}")
    return check_path(location.rstrip("/"))


# ------------------------------------------------------- environment closure


def host_platform() -> dict[str, str]:
    return dict(system=platform.system(), machine=platform.machine())


def check_variables(variables: Any) -> dict[str, Any]:
    _require(type(variables) is dict and set(variables) == {"inherit", "set"}
             and type(variables["inherit"]) is list and type(variables["set"]) is dict,
             "closure variables need inherit and set")
    inherit, fixed = variables["inherit"], variables["set"]
    _require(inherit == sorted(inherit) and len(inherit) <= 64 and len(fixed) <= 64,
             "inherited variable names must be sorted and bounded")
    seen: set[str] = set()
    for name in [*inherit, *fixed]:
        _require(type(name) is str and _VARIABLE.fullmatch(name) is not None and not name.startswith("__"),
                 f"invalid environment variable name: {name!r}")
        key = name.upper()
        _require(key not in seen, f"environment variable declared twice: {name}")
        seen.add(key)
        _require(key not in CONTROLLED and not key.startswith("UV_"),
                 f"environment variable is controlled by the profile: {name}")
        _require(not SENSITIVE.search(name), f"environment variable looks like a credential: {name}")
        _require(not key.startswith("PYTHON") or (name in fixed and key in PYTHON_SETTABLE),
                 f"Python variable cannot be declared by a closure: {name}")
    for name, value in fixed.items():
        _require(type(value) is str and len(value) <= 4096 and "\x00" not in value,
                 f"invalid value for environment variable {name}")
    return variables


def closure_declaration(project: Mapping[str, bytes], *, implementation: str, version: str,
                        system: str, machine: str, installer_version: str,
                        inherit: list[str] | None = None,
                        set_variables: Mapping[str, str] | None = None) -> dict[str, Any]:
    inherit = list(DEFAULT_INHERIT.get(system, ())) if inherit is None else list(inherit)
    fixed = dict(DEFAULT_SET if set_variables is None else set_variables)
    value = dict(schema_version=2, profile=PROFILE, install_policy=INSTALL_POLICY,
                 project=file_rows(project, label="closure project"),
                 python=dict(implementation=implementation, version=version),
                 platform=dict(system=system, machine=machine),
                 installer=dict(tool="uv", version=installer_version),
                 variables=dict(inherit=sorted(set(inherit)), set=fixed))
    check_closure(value)
    check_project(value, project)
    return value


def is_closure(value: Any) -> bool:
    return type(value) is dict and value.get("schema_version") == 2 and value.get("profile") == PROFILE


def check_closure(value: Any) -> dict[str, Any]:
    _require(type(value) is dict and set(value) == {"schema_version", "profile", "install_policy",
                                                     "project", "python", "platform", "installer",
                                                     "variables"}
             and type(value["schema_version"]) is int and value["schema_version"] == 2
             and value["profile"] == PROFILE and value["install_policy"] == INSTALL_POLICY,
             "unsupported environment closure")
    _check_rows(value["project"], label="closure project")
    paths = {row["path"] for row in value["project"]}
    _require(set(PROJECT_FILES) <= paths, "closure project needs pyproject.toml and uv.lock")
    python, host, installer = value["python"], value["platform"], value["installer"]
    _require(type(python) is dict and set(python) == {"implementation", "version"}
             and python["implementation"] in _IMPLEMENTATIONS
             and type(python["version"]) is str and _PYTHON_VERSION.fullmatch(python["version"]) is not None,
             "closure python needs an implementation and an exact version")
    _require(type(host) is dict and set(host) == {"system", "machine"} and host["system"] in _SYSTEMS
             and type(host["machine"]) is str and _MACHINE.fullmatch(host["machine"]) is not None,
             "closure platform needs a system and machine")
    _require(type(installer) is dict and set(installer) == {"tool", "version"} and installer["tool"] == "uv"
             and type(installer["version"]) is str and _TOOL_VERSION.fullmatch(installer["version"]) is not None,
             "closure installer must be an exact uv version")
    check_variables(value["variables"])
    return value


def check_project(value: Mapping[str, Any], files: Mapping[str, bytes]) -> dict[str, Any]:
    """The project bytes are exactly pyproject, lock and the lock's local wheels."""
    summary = lock_summary(files["uv.lock"])
    expected = {*PROJECT_FILES, *summary["local_files"]}
    _require(set(files) == expected,
             "closure project must contain exactly pyproject.toml, uv.lock and the lock's local wheels")
    _require([row["path"] for row in value["project"]] == sorted(files), "closure project rows differ")
    return summary


def project_digest(value: Mapping[str, Any]) -> str:
    return digest(canonical(value["project"]))


def payload_environment(value: Mapping[str, Any], host: Mapping[str, str], *, bin_dir: str,
                        tmp: str, home: str) -> tuple[dict[str, str], list[str]]:
    """The exact environment of a payload: allowlisted host values plus fixed ones."""
    system = value["platform"]["system"]
    lookup = {name.upper(): item for name, item in host.items()} if system == "Windows" else dict(host)
    env: dict[str, str] = {}
    missing = []
    for name in value["variables"]["inherit"]:
        key = name.upper() if system == "Windows" else name
        if key in lookup:
            env[name] = lookup[key]
        else:
            missing.append(name)
    env.update(value["variables"]["set"])
    if system == "Windows":
        root = lookup.get("SYSTEMROOT", r"C:\Windows")
        env.update(PATH=";".join((bin_dir, root + r"\System32", root)), TEMP=tmp, TMP=tmp,
                   HOME=home, USERPROFILE=home)
    else:
        env.update(PATH=":".join((bin_dir, "/usr/bin", "/bin")), TMPDIR=tmp, HOME=home)
    return env, missing
