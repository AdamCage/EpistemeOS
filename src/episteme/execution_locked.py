"""Controller side of execution profile v2 (ADR 0019); trusted local, no sandbox.

Freezes multi-file source trees and uv-locked environment closures into CAS,
builds v2 job specifications, materializes each job directory outside the
store root and admits a verified completion. Jobs never install from the
network; only the explicit ``prepare --online`` path may fill the local uv
cache, and it writes no events. A v2 run is still trusted local execution:
the same OS user, no file-system or network enforcement.
"""

from __future__ import annotations

import argparse
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from . import environment_closure as rules
from .kernel import Actor, Kernel, require
from .store import Store, canonical, digest


PROFILE = rules.PROFILE
SETUP_SECONDS = 1800
CAPABILITIES = {"separate_cwd", "bounded_output_capture", "process_group_timeout", "job_object_timeout",
                "environment_allowlist", "workspace_outside_store", "locked_environment"}
CONTROLLER_OUTPUTS = {"log", "execution_manifest", "environment_record"}
_RESERVED = {"src", "inputs"}
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}
_LABEL = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_BASENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_COMPLETION_LIMIT = 1024 * 1024


def _object(data: bytes) -> dict[str, Any]:
    from .execution import _object as strict
    return strict(data)


# ---------------------------------------------------------------- CAS parts


def source_parts(store: Store, key: str, cache: dict[str, Any] | None = None
                 ) -> tuple[dict[str, Any], dict[str, bytes]]:
    """A canonical source-tree manifest and the verified bytes of its files."""
    if cache is not None and ("source", key) in cache:
        return cache[("source", key)]
    data = store.read(key)
    manifest = rules.check_source_manifest(_object(data))
    require(canonical(manifest) == data, "source tree manifest must be canonical JSON")
    parts = manifest, rules.read_rows(manifest["files"], store.read, label="source tree")
    if cache is not None:
        cache[("source", key)] = parts
    return parts


def closure_parts(store: Store, key: str, cache: dict[str, Any] | None = None
                  ) -> tuple[dict[str, Any], dict[str, bytes]]:
    """A canonical v2 closure and the verified bytes of its project files."""
    if cache is not None and ("closure", key) in cache:
        return cache[("closure", key)]
    data = store.read(key)
    declaration = rules.check_closure(_object(data))
    require(canonical(declaration) == data, "environment closure must be canonical JSON")
    files = rules.read_rows(declaration["project"], store.read, label="closure project")
    rules.check_project(declaration, files)
    if cache is not None:
        cache[("closure", key)] = declaration, files
    return declaration, files


def is_closure(declaration: Any) -> bool:
    return rules.is_closure(declaration)


def freeze_source(store: Store, files: Mapping[str, bytes], *, entry_point: str,
                  input_name: str = "input.dat") -> str:
    manifest = rules.source_manifest(entry_point, input_name, files)
    for data in files.values():
        store.put(data)
    return store.put(canonical(manifest))


def freeze_source_directory(store: Store, root: Path, *, entry_point: str,
                            input_name: str = "input.dat") -> dict[str, Any]:
    files = rules.directory_files(Path(root))
    key = freeze_source(store, files, entry_point=entry_point, input_name=input_name)
    return dict(implementation=key, files=len(files), bytes=sum(map(len, files.values())),
                entry_point=entry_point, input_name=input_name, events_written=0)


def host_uv_version() -> str:
    from .runner_locked import find_uv, uv_version
    host = dict(os.environ)
    return uv_version(find_uv(host), host)


def freeze_closure(store: Store, project: Mapping[str, bytes], *, implementation: str | None = None,
                   version: str | None = None, inherit: list[str] | None = None,
                   set_variables: Mapping[str, str] | None = None,
                   installer_version: str | None = None) -> str:
    """Freeze project bytes plus this host's interpreter request, platform and uv version."""
    host = rules.host_platform()
    declaration = rules.closure_declaration(
        project, implementation=implementation or sys.implementation.name,
        version=version or platform.python_version(), system=host["system"], machine=host["machine"],
        installer_version=installer_version or host_uv_version(), inherit=inherit, set_variables=set_variables)
    for data in project.values():
        store.put(data)
    return store.put(canonical(declaration))


def project_files(directory: Path) -> dict[str, bytes]:
    """pyproject.toml, uv.lock and the local wheels the lock references."""
    directory = Path(directory)
    files = {}
    for name in rules.PROJECT_FILES:
        require(rules.is_plain(directory / name), f"closure project needs a plain {name}")
        files[name] = (directory / name).read_bytes()
    for path in rules.lock_summary(files["uv.lock"])["local_files"]:
        target = directory.joinpath(*path.split("/"))
        require(rules.is_plain(target), f"locked local wheel is missing or not a plain file: {path}")
        files[path] = target.read_bytes()
    return files


def spec_artifacts(store: Store, spec: dict[str, Any]) -> set[str]:
    manifest, _ = source_parts(store, spec["program"])
    declaration, _ = closure_parts(store, spec["environment"])
    return {spec["program"], spec["environment"], spec["input"]["sha256"],
            *rules.tree_digests(manifest), *(row["sha256"] for row in declaration["project"])}


# ------------------------------------------------------------ specification


def check_outputs(outputs: Any) -> None:
    """The v1 output rules, with the v2 run-directory layout names reserved."""
    require(type(outputs) is dict and {"raw_data", "metrics"} <= outputs.keys()
            and not outputs.keys() & CONTROLLER_OUTPUTS,
            "declared outputs need raw_data and metrics; controller output names are reserved")
    require(all(type(k) is str and _LABEL.fullmatch(k) and type(v) is str and _BASENAME.fullmatch(v)
                and not v.endswith((".", " ")) and v.lower() not in _RESERVED
                and v.split(".")[0].upper() not in _WINDOWS_RESERVED
                for k, v in outputs.items()), "output paths must be portable, distinct plain basenames")
    require(len({v.lower() for v in outputs.values()}) == len(outputs), "output paths collide")


def command_for(manifest: dict[str, Any], seed: int) -> list[str]:
    return ["python", "-s", "-B", f"src/{manifest['entry_point']}", f"inputs/{manifest['input_name']}",
            "--seed", str(seed)]


def check_spec(spec: Any) -> None:
    require(type(spec) is dict and set(spec) == {"schema_version", "profile", "program", "environment", "input",
                                                  "command", "outputs", "wall_seconds", "setup_seconds",
                                                  "max_output_bytes"}
            and type(spec["schema_version"]) is int and spec["schema_version"] == 2 and spec["profile"] == PROFILE,
            "unsupported execution specification")
    for key in ("program", "environment"):
        require(type(spec[key]) is str and re.fullmatch(r"[0-9a-f]{64}", spec[key]) is not None,
                "invalid frozen program or environment digest")
    require(type(spec["input"]) is dict and set(spec["input"]) == {"path", "sha256"}
            and type(spec["input"]["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", spec["input"]["sha256"])
            and type(spec["input"]["path"]) is str and spec["input"]["path"].startswith("inputs/"),
            "invalid frozen input")
    rules.check_component(spec["input"]["path"][len("inputs/"):])
    require(type(spec["wall_seconds"]) is int and 1 <= spec["wall_seconds"] <= 86400,
            "wall_seconds must be an integer from 1 to 86400")
    require(spec["setup_seconds"] == SETUP_SECONDS, "unsupported environment setup limit")
    require(type(spec["max_output_bytes"]) is int and 1 <= spec["max_output_bytes"] <= 1024**3,
            "max_output_bytes must be an integer from 1 to 1 GiB")
    check_outputs(spec["outputs"])
    command = spec["command"]
    require(type(command) is list and len(command) == 7 and command[:3] == ["python", "-s", "-B"]
            and type(command[3]) is str and command[3].startswith("src/") and command[4] == spec["input"]["path"]
            and command[5] == "--seed" and type(command[6]) is str and re.fullmatch(r"-?[0-9]+", command[6]),
            "unsupported locked Python invocation")


def build_spec(store: Store, *, implementation: str, environment: str, data: str, seed: int,
               outputs: dict[str, str], wall_seconds: int, max_output_bytes: int) -> dict[str, Any]:
    manifest, _ = source_parts(store, implementation)
    closure_parts(store, environment)
    store.read(data)
    command = command_for(manifest, seed)
    spec = dict(schema_version=2, profile=PROFILE, program=implementation, environment=environment,
                input=dict(path=command[4], sha256=data), command=command, outputs=outputs,
                wall_seconds=wall_seconds, setup_seconds=SETUP_SECONDS, max_output_bytes=max_output_bytes)
    check_spec(spec)
    return spec


def check_job(store: Store, spec: dict[str, Any], run: dict[str, Any], expected_data: str,
              cache: dict[str, Any] | None = None) -> None:
    """A v2 specification binds exactly the frozen run, its program, closure and input."""
    check_spec(spec)
    manifest, _ = source_parts(store, spec["program"], cache)
    closure_parts(store, spec["environment"], cache)
    require(spec["program"] == run["implementation"] and spec["environment"] == run["environment"]
            and spec["command"] == run["command"] == command_for(manifest, run["seed"])
            and spec["input"]["sha256"] == expected_data,
            "execution specification differs from frozen run inputs")
    store.read(expected_data)


def available_capabilities() -> set[str]:
    available = CAPABILITIES - {"process_group_timeout", "job_object_timeout"}
    return available | ({"job_object_timeout"} if os.name == "nt" else
                        {"process_group_timeout"} if os.name == "posix" else set())


# ---------------------------------------------------------- job directories


def default_root() -> Path:
    configured = os.environ.get("EPISTEME_EXECUTION_ROOT")
    return Path(configured or Path(tempfile.gettempdir()) / "episteme-executions").resolve()


def check_root(store: Store, root: str) -> Path:
    require(type(root) is str and 0 < len(root) <= 1024 and Path(root).is_absolute(),
            "workspace root must be an absolute path")
    resolved = Path(root).resolve()
    require(resolved != store.root and store.root not in resolved.parents,
            "profile v2 job directories must be outside the store root")
    return resolved


def _check_dir(path: Path) -> None:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        require(stat.S_ISDIR(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400),
                "execution workspace must be a plain directory")


def job_directory(store: Store, dispatch: dict[str, Any]) -> Path:
    p = dispatch["payload"]
    root = Path(p["workspace_root"])
    path = root / p["workspace_token"]
    require(store.root != path.resolve() and store.root not in path.resolve().parents,
            "profile v2 job directory resolves inside the store root")
    for directory in (root, path):
        _check_dir(directory)
    return path


def materialize(store: Store, spec: dict[str, Any], specification: str, identity: dict[str, Any],
                path: Path) -> None:
    """Write program, input and closure bytes from CAS into a fresh job directory."""
    from .runner_locked import CLOSURE, CONTROL, HOME, INPUT_DIR, RUN, SOURCE_DIR, TMP
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(mode=0o700, exist_ok=False)
    manifest, files = source_parts(store, spec["program"])
    declaration, project = closure_parts(store, spec["environment"])
    for name in (CONTROL, RUN, f"{RUN}/{SOURCE_DIR}", f"{RUN}/{INPUT_DIR}", CLOSURE, TMP, HOME):
        (path / name).mkdir()
    for root, tree in ((path / RUN / SOURCE_DIR, files), (path / CLOSURE, project)):
        for relative, data in tree.items():
            target = root.joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    (path / RUN / INPUT_DIR / manifest["input_name"]).write_bytes(store.read(spec["input"]["sha256"]))
    control = path / CONTROL
    control.joinpath("spec.json").write_bytes(store.read(specification))
    control.joinpath("identity.json").write_bytes(canonical(identity))
    control.joinpath("closure.json").write_bytes(store.read(spec["environment"]))
    control.joinpath("program.json").write_bytes(store.read(spec["program"]))


def spawn(path: Path) -> None:
    worker = Path(__file__).with_name("runner_locked.py")
    process = subprocess.Popen([sys.executable, str(worker), "--job", str(path)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # The worker may outlive this controller. Its durable completion remains reconcilable.
    process.wait()


def cleanup(path: Path) -> None:
    """Drop the venv copy after its completion is in CAS; other job files stay for inspection."""
    shutil.rmtree(path / "venv", ignore_errors=True)


def _read_plain(path: Path, limit: int) -> bytes:
    from .runner_backend import plain_file
    require(plain_file(path) and path.stat().st_size <= limit, f"unsafe or oversized job file: {path.name}")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    require(len(data) <= limit, f"oversized job file: {path.name}")
    return data


def import_completion(store: Store, spec: dict[str, Any], identity: dict[str, Any], path: Path) -> str | None:
    """Import a finished completion and its referenced files; None while unknown."""
    from .runner_locked import CONTROL, ENVIRONMENT_LIMIT, LOG_LIMIT, RUN
    completion = path / CONTROL / "completion.json"
    if not completion.is_file():
        return None
    data = _read_plain(completion, _COMPLETION_LIMIT)
    record = _object(data)
    require(record.get("identity") == identity, "completion identity mismatch")
    if record.get("status") == "unknown":
        return None
    entries = record.get("outputs")
    require(type(entries) is dict and set(entries) <= set(spec["outputs"]), "invalid completion outputs")
    files = {name: (RUN, spec["outputs"][name], spec["max_output_bytes"], item) for name, item in entries.items()}
    files["_stdout"] = (CONTROL, "stdout.bin", spec["max_output_bytes"], record.get("stdout"))
    files["_stderr"] = (CONTROL, "stderr.bin", spec["max_output_bytes"], record.get("stderr"))
    for name, filename, limit in (("environment_record", "environment.json", ENVIRONMENT_LIMIT),
                                  ("installer_log", "installer.log", LOG_LIMIT)):
        if record.get(name) is not None:
            files[name] = (CONTROL, filename, limit, record[name])
    for _, (directory, filename, limit, item) in files.items():
        require(type(item) is dict and item.get("path") == filename, "invalid completion path")
        content = _read_plain(path / directory / filename, limit)
        require(len(content) == item.get("bytes") and digest(content) == item.get("sha256"),
                "completion output hash mismatch")
        require(store.put(content) == item["sha256"], "output changed during capture")
    return store.put(data)


# --------------------------------------------------------------- completion


def _entry(store: Store, item: Any, path: str, limit: int) -> str:
    require(type(item) is dict and set(item) == {"path", "sha256", "bytes"} and item["path"] == path
            and type(item["bytes"]) is int and 0 <= item["bytes"] <= limit, "invalid completion output path/size")
    require(len(store.read(item["sha256"])) == item["bytes"], "completion output size mismatch")
    return item["sha256"]


def check_environment_record(store: Store, key: str, spec: dict[str, Any],
                             declaration: dict[str, Any]) -> dict[str, Any]:
    record = _object(store.read(key))
    interpreter, host, installer = (record.get("interpreter", {}), record.get("platform", {}),
                                    record.get("installer", {}))
    require(record.get("schema_version") == 1 and record.get("kind") == "realized_environment"
            and record.get("closure") == spec["environment"]
            and (interpreter.get("implementation"), interpreter.get("version"))
            == (declaration["python"]["implementation"], declaration["python"]["version"])
            and (host.get("system"), host.get("machine"))
            == (declaration["platform"]["system"], declaration["platform"]["machine"])
            and installer.get("version") == declaration["installer"]["version"]
            and type(record.get("inventory", {}).get("sha256")) is str
            and type(record.get("distributions")) is list,
            "realized environment differs from its frozen closure")
    return record


def check_completion(store: Store, spec: dict[str, Any], identity: dict[str, Any], capabilities: list[str],
                     plan_metric: str | None, manifest: str,
                     result: dict[str, Any] | None = None) -> tuple[str, dict[str, str], str]:
    from .runner_locked import ENVIRONMENT_LIMIT, ISOLATION, LOG_LIMIT, expected_inputs
    record = _object(store.read(manifest))
    require(record.get("schema_version") == 2 and record.get("profile") == PROFILE
            and record.get("identity") == identity, "completion belongs to a different execution")
    require(record.get("command") == spec["command"], "completion command differs from specification")
    require(record.get("status") in {"completed", "failed"}, "unknown execution cannot become terminal evidence")
    status = record["status"]
    elapsed = record.get("elapsed_seconds")
    require(type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0,
            "completion requires finite observed elapsed time")
    for field in ("started_at", "finished_at"):
        require(type(record.get(field)) is str and datetime.fromisoformat(record[field]).utcoffset() is not None,
                "completion requires timezone-aware timestamps")
    require(record.get("returncode") is None or type(record["returncode"]) is int, "invalid observed exit code")
    reason = record.get("reason")
    require(type(reason) is str and (status == "completed" or bool(reason.strip())), "failed completion needs reason")
    require(status != "completed" or type(record.get("returncode")) is int and record["returncode"] == 0,
            "completed execution requires confirmed exit zero")
    require(record.get("termination_confirmed") is True, "completion lacks confirmed process control")
    require(record.get("isolation") == ISOLATION, "completion misstates its isolation")
    declaration, _ = closure_parts(store, spec["environment"])
    environment = None
    if record.get("environment_record") is not None:
        key = _entry(store, record["environment_record"], "environment.json", ENVIRONMENT_LIMIT)
        environment = check_environment_record(store, key, spec, declaration)
    if record.get("installer_log") is not None:
        _entry(store, record["installer_log"], "installer.log", LOG_LIMIT)
    if status == "completed":
        expected = expected_inputs(spec, declaration)
        require(record.get("inputs_before") == expected and record.get("inputs_after") == expected,
                "completion inputs differ from frozen specification")
        require(environment is not None and record.get("environment_after")
                == dict(inventory_sha256=environment["inventory"]["sha256"]),
                "completion lacks an unchanged realized environment")
        variables = record.get("environment_variables")
        fixed = declaration["variables"]["set"]
        allowed = {*declaration["variables"]["inherit"], *fixed, *rules.CONTROLLED}
        require(type(variables) is dict and set(variables) <= allowed
                and all(variables.get(name) == value for name, value in fixed.items()),
                "completion environment variables exceed the closure allowlist")
        launch = record.get("launch_command")
        require(type(launch) is list and launch[-4:] == spec["command"][3:], "completion launch command differs")
        control = record.get("process_control") or ""
        require(("job_object_timeout" not in capabilities or control.startswith("Windows Job Object"))
                and ("process_group_timeout" not in capabilities or control.startswith("POSIX process group")),
                "completion did not provide the requested process control")
    outputs: dict[str, str] = {}
    entries = record.get("outputs")
    require(type(entries) is dict and set(entries) <= set(spec["outputs"]), "unexpected completion output")
    require(status != "completed" or set(entries) == set(spec["outputs"]), "completion misses expected outputs")
    for name, item in entries.items():
        outputs[name] = _entry(store, item, spec["outputs"][name], spec["max_output_bytes"])
    for name, path in (("stdout", "stdout.bin"), ("stderr", "stderr.bin")):
        _entry(store, record.get(name), path, spec["max_output_bytes"])
    outputs["log"] = manifest
    outputs["execution_manifest"] = manifest
    if environment is not None:
        outputs["environment_record"] = record["environment_record"]["sha256"]
    if status == "completed" and plan_metric is not None:
        try:
            Kernel(store, Actor("execution-validator", "observer"))._metric(outputs["metrics"], plan_metric)
        except ValueError as exc:
            status, reason = "failed", f"execution output validation failed: {exc}"
    if result is not None:
        require(result["status"] == status and result["outputs"] == outputs and result["reason"] == reason,
                "terminal result differs from verified completion")
    return status, outputs, reason


def completion_artifacts(record: dict[str, Any]) -> set[str]:
    return {record[name]["sha256"] for name in ("environment_record", "installer_log")
            if record.get(name) is not None}


# -------------------------------------------------------------- preparation


def prepare(store: Store, environment: str, *, online: bool = False) -> dict[str, Any]:
    """Create and verify an environment from a closure in a temporary directory.

    No events are written. ``online`` lets uv download locked distributions
    into its cache; job installation stays offline either way.
    """
    from .runner_locked import materialize_environment
    declaration, project = closure_parts(store, environment)
    root = default_root()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="prepare-", dir=root, ignore_cleanup_errors=True) as temporary:
        base = Path(temporary)
        for relative, data in project.items():
            target = (base / "closure").joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        record, _ = materialize_environment(declaration, base / "closure", base / "venv", base / "tmp",
                                            online=online)
    return dict(environment=environment, status="prepared", network="allowed" if online else "offline",
                installer=record["installer"]["version"], interpreter=record["interpreter"],
                distributions=record["distributions"], inventory_sha256=record["inventory"]["sha256"],
                events_written=0, meaning="environment created and verified in a temporary directory; "
                                          "no run, no evidence")


# --------------------------------------------------------------------- CLI


OPERATIONS = {"source", "closure", "prepare"}


def add_arguments(operations: Any) -> None:
    source = operations.add_parser("source", help="Freeze a multi-file program tree into CAS; no events")
    source.add_argument("--tree", type=Path, required=True, help="Program directory")
    source.add_argument("--entry", required=True, help="Entry point path relative to the tree")
    source.add_argument("--input-name", default="input.dat", help="File name of the materialized input")
    closure = operations.add_parser("closure", help="Freeze a uv-locked environment closure into CAS; no events")
    closure.add_argument("--project", type=Path, required=True,
                         help="Directory with pyproject.toml, uv.lock and any relative local wheels")
    closure.add_argument("--python", default=None, help="Exact interpreter version (default: current)")
    closure.add_argument("--implementation", default=None, help="Interpreter implementation (default: current)")
    closure.add_argument("--inherit", action="append", default=None, help="Host variable passed to programs")
    closure.add_argument("--set", action="append", default=None, metavar="NAME=VALUE",
                         help="Fixed variable for programs (replaces the default PYTHONHASHSEED/PYTHONUTF8)")
    preparation = operations.add_parser("prepare", help="Create and verify a closure environment; no events")
    preparation.add_argument("environment", help="Frozen v2 environment closure digest")
    preparation.add_argument("--online", action="store_true",
                             help="Allow uv to download locked distributions into its cache")
    for parser in (source, closure, preparation):
        parser.add_argument("--root", type=Path, required=True)


def run_cli(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.operation == "prepare":
        require((args.root / "state.sqlite3").is_file(), "existing research state is required")
    with Store(args.root) as store:
        if args.operation == "source":
            return freeze_source_directory(store, args.tree, entry_point=args.entry,
                                           input_name=args.input_name), 0
        if args.operation == "closure":
            fixed = None
            if args.set is not None:
                pairs = [item.partition("=") for item in args.set]
                require(all(sep for _, sep, _ in pairs), "--set needs NAME=VALUE")
                fixed = {name: value for name, _, value in pairs}
            key = freeze_closure(store, project_files(args.project), implementation=args.implementation,
                                 version=args.python, inherit=args.inherit, set_variables=fixed)
            declaration, _ = closure_parts(store, key)
            return dict(environment=key, profile=PROFILE, python=declaration["python"],
                        platform=declaration["platform"], installer=declaration["installer"],
                        variables=declaration["variables"], project_files=len(declaration["project"]),
                        events_written=0, isolation="trusted local; no sandbox"), 0
        return prepare(store, args.environment, online=args.online), 0
