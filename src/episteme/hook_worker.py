"""Run one DomainPack hook in a fresh interpreter (ADR 0024).

The child receives the pack's pinned source bytes, JSON arguments and, for
analysis hooks, the allowlisted artifact bytes. It does not receive a Store,
a SQLite connection, the database path or the parent process environment.
A same-user subprocess is not a sandbox: the operating-system user can still
attach to the child. The static import check remains a heuristic; this process
is the boundary that was added.

stdout belongs to the protocol. Pack prints are discarded. The child imports
``episteme.domains.api`` and refuses ``episteme.store`` and ``sqlite3``.
"""

from __future__ import annotations

import base64
from contextlib import redirect_stdout
import importlib
import importlib.abc
import importlib.machinery
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from typing import Any, Mapping


_TIMEOUT_SECONDS = 120
# Windows needs these to load the interpreter. They are not the parent environment:
# a name that exists only on the parent, including a store path, is not copied.
_WINDOWS_LOADER = ("SYSTEMROOT", "SystemRoot", "WINDIR", "SYSTEMDRIVE", "PATHEXT")


class HookFailed(ValueError):
    """A pack hook subprocess failed, timed out or returned an unusable result."""


def child_environment() -> dict[str, str]:
    """A constructed interpreter environment, not a copy of ``os.environ``."""
    env = {
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONHASHSEED": "0",
    }
    if os.name == "nt":
        for name in _WINDOWS_LOADER:
            value = os.environ.get(name)
            if value:
                env[name] = value
    return env


def probe(files: Mapping[str, bytes], module_name: str) -> dict[str, Any]:
    """Execute pinned bytes in the child and return the manifest and hook names."""
    return _exchange({"op": "probe", "files": _files(files), "module_name": module_name})


def call(files: Mapping[str, bytes], module_name: str, hook: str, args: tuple[Any, ...]) -> Any:
    """Run ``hook`` on ``args`` in the child and rebuild the value in this process."""
    encoded, views = [], []
    for arg in args:
        if _is_cas(arg):
            views.append(arg)
        encoded.append(_encode(arg))
    result = _exchange({"op": "call", "files": _files(files), "module_name": module_name,
                        "hook": hook, "args": encoded})
    _apply_views(views, result.get("cas") or [])
    return _decode(result["value"])


def _files(files: Mapping[str, bytes]) -> dict[str, str]:
    return {path: base64.b64encode(data).decode("ascii") for path, data in files.items()}


def _exchange(payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-S", str(Path(__file__).resolve())],
            input=body, capture_output=True, timeout=_TIMEOUT_SECONDS,
            env=child_environment(), check=False)
    except subprocess.TimeoutExpired as exc:
        raise HookFailed("pack hook subprocess timed out") from exc
    except OSError as exc:
        raise HookFailed(f"pack hook subprocess did not start: {exc}") from exc
    if completed.returncode != 0 or not completed.stdout:
        detail = completed.stderr.decode("utf-8", "replace")[:2000]
        raise HookFailed(f"pack hook subprocess failed: {detail or completed.returncode}")
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HookFailed("pack hook subprocess returned invalid JSON") from exc
    if type(result) is not dict or result.get("ok") is not True:
        message = result.get("error") if type(result) is dict else "invalid hook result"
        raise HookFailed(str(message))
    return result


def _is_cas(value: Any) -> bool:
    return type(value).__name__ == "CasView" and type(value).__module__ == "episteme.domains.api"


def _apply_views(views: list[Any], effects: list[Any]) -> None:
    if len(effects) != len(views):
        raise HookFailed("pack hook subprocess returned a mismatched CAS view")
    for view, effect in zip(views, effects):
        if type(effect) is not dict:
            raise HookFailed("pack hook subprocess returned a malformed CAS view")
        view._reads.update(str(item) for item in effect.get("reads") or [])
        view._refused.update(str(item) for item in effect.get("refused") or [])


def _api() -> Any:
    from episteme.domains import api
    return api


def _b64_blobs(blobs: Mapping[str, bytes]) -> dict[str, str]:
    return {key: base64.b64encode(data).decode("ascii") for key, data in blobs.items()}


def _raw_blobs(blobs: Mapping[str, str]) -> dict[str, bytes]:
    return {key: base64.b64decode(data, validate=True) for key, data in blobs.items()}


def _encode(value: Any) -> Any:
    api = _api()
    if value is None or type(value) in (str, bool, int, float):
        return {"t": "json", "v": value}
    if isinstance(value, Path):
        return {"t": "path", "v": str(value)}
    if type(value) is api.ParameterCatalog:
        return {"t": "catalog", "v": value.to_dict()}
    if type(value) is api.ProtocolDraft:
        return {"t": "draft", "v": value.to_dict()}
    if type(value) is api.CompileRequest:
        return {"t": "compile", "parameters": api.thaw(value.parameters),
                "host_inputs": api.thaw(value.host_inputs),
                "capture": None if value.capture is None else _encode(value.capture)}
    if type(value) is api.CaptureBundle:
        return {"t": "capture", "v": value.to_dict(), "blobs": _b64_blobs(value.blobs())}
    if type(value) in (api.ExecutionPlan, api.ExecutionPlanV2):
        return {"t": "plan", "v": value.to_dict(), "blobs": _b64_blobs(value.blobs())}
    if type(value) is api.ProtocolContext:
        return {"t": "protocol_context", "parameters": api.thaw(value.parameters),
                "draft": value.draft.to_dict(), "execution_plan": api.thaw(value.execution_plan),
                "capture": None if value.capture is None else api.thaw(value.capture),
                "protocol": api.thaw(value.protocol), "protocol_hash": value.protocol_hash}
    if type(value) is api.AnalysisContext:
        return {"t": "analysis_context", "pack_id": value.pack_id, "pack_version": value.pack_version,
                "protocol": api.thaw(value.protocol), "protocol_hash": value.protocol_hash,
                "parameters": api.thaw(value.parameters), "draft": value.draft.to_dict(),
                "execution_plan": api.thaw(value.execution_plan),
                "capture": None if value.capture is None else api.thaw(value.capture),
                "batch": api.thaw(value.batch), "slots": api.thaw(value.slots),
                "numeric_tolerance": value.numeric_tolerance}
    if _is_cas(value):
        return {"t": "cas", "blobs": _b64_blobs(value._blobs), "hidden": sorted(value._hidden)}
    if type(value) is api.OutputCheck:
        return {"t": "check", "v": value.to_dict()}
    if type(value) is api.Recomputation:
        return {"t": "recomputation", "v": value.to_dict()}
    if type(value) is api.AnalysisReport:
        return {"t": "report", "v": value.to_dict(),
                "statistical": value.statistical_report.to_dict()}
    if type(value) is api.StatisticalReport:
        return {"t": "statistical", "v": value.to_dict()}
    if type(value) is tuple:
        return {"t": "tuple", "v": [_encode(item) for item in value]}
    if type(value) is list:
        return {"t": "list", "v": [_encode(item) for item in value]}
    if type(value) is dict or type(value).__name__ == "mappingproxy":
        return {"t": "json", "v": api.thaw(value)}
    raise HookFailed(f"pack hook argument is not transferable: {type(value).__name__}")


def _read_blobs(blobs: Mapping[str, str]) -> Any:
    raw = _raw_blobs(blobs)

    def read(key: str) -> bytes:
        if key not in raw:
            raise ValueError(f"pack hook result omitted bytes {key}")
        return raw[key]

    return read


def _decode(value: Any) -> Any:
    api = _api()
    if type(value) is not dict or "t" not in value:
        raise HookFailed("pack hook returned an untyped value")
    kind = value["t"]
    if kind == "json":
        return value["v"]
    if kind == "none":
        return None
    if kind == "tuple":
        return tuple(_decode(item) for item in value["v"])
    if kind == "list":
        return [_decode(item) for item in value["v"]]
    if kind == "path":
        return Path(value["v"])
    if kind == "compile":
        capture = None if value["capture"] is None else _decode(value["capture"])
        return api.CompileRequest(parameters=value["parameters"], host_inputs=value["host_inputs"],
                                  capture=capture)
    if kind == "protocol_context":
        return api.ProtocolContext(
            parameters=value["parameters"], draft=api.ProtocolDraft.from_dict(value["draft"]),
            execution_plan=value["execution_plan"], capture=value["capture"],
            protocol=value["protocol"], protocol_hash=value["protocol_hash"])
    if kind == "analysis_context":
        return api.AnalysisContext(
            pack_id=value["pack_id"], pack_version=value["pack_version"],
            protocol=value["protocol"], protocol_hash=value["protocol_hash"],
            parameters=value["parameters"], draft=api.ProtocolDraft.from_dict(value["draft"]),
            execution_plan=value["execution_plan"], capture=value["capture"],
            batch=value["batch"], slots=value["slots"], numeric_tolerance=value["numeric_tolerance"])
    if kind == "cas":
        return api.CasView(_raw_blobs(value["blobs"]), hidden=value["hidden"])
    if kind == "catalog":
        return api.ParameterCatalog.from_dict(value["v"])
    if kind == "draft":
        return api.ProtocolDraft.from_dict(value["v"])
    if kind == "capture":
        return api.CaptureBundle.from_frozen(value["v"], _read_blobs(value["blobs"]))
    if kind == "plan":
        return api.plan_from_frozen(value["v"], _read_blobs(value["blobs"]))
    if kind == "check":
        return api.OutputCheck.from_dict(value["v"])
    if kind == "recomputation":
        return api.Recomputation.from_dict(value["v"])
    if kind == "statistical":
        return api.StatisticalReport.from_dict(value["v"])
    if kind == "report":
        return api.AnalysisReport.from_frozen(value["v"], api.StatisticalReport.from_dict(value["statistical"]))
    raise HookFailed(f"pack hook returned an unknown value kind: {kind}")


# ------------------------------------------------------------------- child


class _DenyKernel(importlib.abc.MetaPathFinder):
    """Refuse the store and sqlite inside a hook. This is not a sandbox."""

    def find_spec(self, fullname: str, path: Any = None, target: Any = None
                  ) -> importlib.machinery.ModuleSpec | None:
        if fullname == "episteme.store" or fullname.startswith("episteme.store."):
            raise ImportError("pack hook cannot import episteme.store")
        if fullname == "sqlite3" or fullname.startswith("sqlite3."):
            raise ImportError("pack hook cannot import sqlite3")
        return None


class _Loader(importlib.abc.Loader):
    def __init__(self, files: Mapping[str, bytes]):
        self.files = files

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        relative = module.__spec__.loader_state  # type: ignore[union-attr]
        code = compile(self.files[relative], relative, "exec", dont_inherit=True)
        exec(code, module.__dict__)


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, name: str, files: Mapping[str, bytes]):
        self.name, self.files = name, files
        self.loader = _Loader(files)

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
                    fullname, self.loader, origin=candidate, loader_state=candidate, is_package=package)
                spec.has_location = False
                if package:
                    spec.submodule_search_locations = []
                return spec
        return None


def _load_pack(files: Mapping[str, bytes], module_name: str) -> ModuleType:
    if module_name in sys.modules:
        return sys.modules[module_name]
    sys.meta_path.insert(0, _Finder(module_name, files))
    try:
        return importlib.import_module(module_name)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise


def _prepare_import_path() -> None:
    source = Path(__file__).resolve().parents[1]
    script_dir = str(Path(__file__).resolve().parent)
    sys.path = [item for item in sys.path if item != script_dir]
    sys.path.insert(0, str(source))


def _child(request: dict[str, Any]) -> dict[str, Any]:
    files = {path: base64.b64decode(data, validate=True) for path, data in request["files"].items()}
    module_name = request["module_name"]
    if type(module_name) is not str or not module_name.startswith("_episteme_pack_"):
        raise ValueError("invalid pack module name")
    module = _load_pack(files, module_name)
    if request["op"] == "probe":
        manifest = getattr(module, "MANIFEST", None)
        if type(manifest) is not dict:
            raise ValueError("pack MANIFEST must be a dict")
        hooks = ("describe", "validate_parameters", "compile_protocol", "compile_execution",
                 "validate_protocol", "validate_outputs", "recompute_metrics", "analyse")
        return {"ok": True, "manifest": manifest,
                "hooks": [name for name in hooks if callable(getattr(module, name, None))],
                "capture": callable(getattr(module, "capture", None)),
                "proposal_schema": callable(getattr(module, "proposal_schema", None)),
                "proposal_attempts": callable(getattr(module, "proposal_attempts", None))}
    if request["op"] != "call":
        raise ValueError("unknown pack hook operation")
    hook = getattr(module, request["hook"], None)
    if not callable(hook):
        raise ValueError(f"pack has no hook {request['hook']}")
    views: list[Any] = []
    args = []
    for encoded in request["args"]:
        value = _decode(encoded)
        args.append(value)
        if _is_cas(value):
            views.append(value)
    returned = hook(*args)
    return {"ok": True, "value": _encode(returned),
            "cas": [{"reads": list(view.reads), "refused": list(view.refused)} for view in views]}


def main() -> None:
    sys.meta_path.insert(0, _DenyKernel())
    _prepare_import_path()
    # api is imported only after the store and sqlite3 are refused.
    sink = io.StringIO()
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        with redirect_stdout(sink):
            result = _child(request)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    sys.stdout.buffer.write(json.dumps(result, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=False, allow_nan=False).encode("utf-8"))


if __name__ == "__main__":
    main()
