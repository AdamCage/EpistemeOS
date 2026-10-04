"""Profile v2 worker: a locked uv environment, allowlisted variables, outside the store.

No Store access and no sandbox claim. The worker checks the materialized
program, input and closure bytes, creates a fresh environment from the frozen
uv lock without network access and records what was installed. The payload
gets only allowlisted variables and runs under the v1 process controls. It
still runs as the controller's OS user: it can read files, reach the network
and modify anything that user may modify, including this job directory.
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
from typing import Any

try:
    from . import environment_closure as rules
    from .runner_backend import (_WindowsJob, _atomic, _canonical, _confirm_posix_group, _entry,
                                 _hash, _now, _prepare_posix)
except ImportError:  # The controller runs this file as a script, outside the package.
    import environment_closure as rules  # type: ignore[no-redef]
    from runner_backend import (_WindowsJob, _atomic, _canonical, _confirm_posix_group,  # type: ignore[no-redef]
                                _entry, _hash, _now, _prepare_posix)


CONTROL, RUN, CLOSURE, VENV, TMP, HOME = "control", "run", "closure", "venv", "tmp", "home"
SOURCE_DIR, INPUT_DIR = "src", "inputs"
SETUP_SECONDS = 1800
ENVIRONMENT_LIMIT = 64 * 1024 * 1024
LOG_LIMIT = 1024 * 1024
ISOLATION = dict(os_identity="same_user_as_controller", filesystem="not_enforced",
                 network="not_enforced", environment_variables="allowlist",
                 workspace="outside_store_root")
# The gated process is the base interpreter, never a venv launcher: on Windows a
# launcher starts the real interpreter as a child, which could precede job assignment.
_SHIM = ("import subprocess,sys\npermit=sys.stdin.buffer.readline();sys.stdin.close()\n"
         "if permit!=b'GO\\n':sys.exit(125)\n"
         "sys.exit(subprocess.call(sys.argv[1:],stdin=subprocess.DEVNULL))")
_BOOTSTRAP = ("import os,runpy,sys\nsys.argv=sys.argv[1:]\n"
              "if sys.path and sys.path[0]=='':del sys.path[0]\n"
              "sys.path.insert(0,os.path.dirname(os.path.abspath(sys.argv[0])))\n"
              "runpy.run_path(sys.argv[0],run_name='__main__')")
_PROBE = ("import json,platform,sys,sysconfig\nprint(json.dumps(dict("
          "implementation=sys.implementation.name,version=platform.python_version(),"
          "sys_version=sys.version,executable=sys.executable,"
          "base_executable=getattr(sys,'_base_executable',sys.executable),prefix=sys.prefix,"
          "base_prefix=sys.base_prefix,system=platform.system(),machine=platform.machine(),"
          "release=platform.release(),purelib=sysconfig.get_path('purelib'),"
          "platlib=sysconfig.get_path('platlib'))))")
_VOLATILE = {"INSTALLER", "REQUESTED", "RECORD", "direct_url.json", "uv_cache.json"}


class SetupError(RuntimeError):
    """The locked environment could not be created or differs from its closure."""


def _bin(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


def venv_python(venv: Path) -> Path:
    return _bin(venv) / ("python.exe" if os.name == "nt" else "python")


def _file_sha256(path: Path) -> str:
    import hashlib
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise SetupError("environment setup time limit exceeded")
    return left


def find_uv(host: dict[str, str]) -> str:
    override = host.get("EPISTEME_UV")
    found = override or shutil.which("uv", path=host.get("PATH"))
    if not found or not Path(found).is_file():
        raise SetupError("uv executable not found; set EPISTEME_UV or put uv on PATH")
    return str(Path(found).resolve())


def uv_version(uv: str, host: dict[str, str]) -> str:
    process = subprocess.run([uv, "--version"], capture_output=True, text=True, timeout=60, env=host)
    match = re.match(r"uv ([0-9]+\.[0-9]+\.[0-9]+)", process.stdout.strip())
    if process.returncode or match is None:
        raise SetupError(f"cannot read the uv version: {process.stderr.strip()[:200]}")
    return match.group(1)


def _installer_env(host: dict[str, str], tmp: Path) -> dict[str, str]:
    """uv's own variables; host PATH is passed only to interpreter discovery."""
    names = ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "HOME", "USERPROFILE", "LOCALAPPDATA",
             "APPDATA", "PROGRAMDATA", "XDG_CACHE_HOME", "XDG_DATA_HOME")
    upper = {name.upper(): value for name, value in host.items()} if os.name == "nt" else host
    env = {name: upper[name] for name in names if name in upper}
    env.update(TEMP=str(tmp), TMP=str(tmp), TMPDIR=str(tmp), NO_COLOR="1", UV_NO_PROGRESS="1")
    return env


def materialize_environment(declaration: dict[str, Any], closure: Path, venv: Path, tmp: Path,
                            *, online: bool = False, host: dict[str, str] | None = None,
                            deadline: float | None = None) -> tuple[dict[str, Any], bytes]:
    """Create ``venv`` from the frozen lock and describe the result.

    Offline unless ``online`` is requested by an explicit preparation command.
    Returns the realized-environment record and the bounded installer log.
    """
    host = dict(os.environ if host is None else host)
    deadline = time.monotonic() + SETUP_SECONDS if deadline is None else deadline
    tmp.mkdir(parents=True, exist_ok=True)
    uv = find_uv(host)
    observed = uv_version(uv, host)
    if observed != declaration["installer"]["version"]:
        raise SetupError(f"uv {observed} differs from the closure installer "
                         f"{declaration['installer']['version']}")
    # The cache location honours the caller's uv configuration; installation does not.
    cache = subprocess.run([uv, "cache", "dir"], capture_output=True, text=True, cwd=tmp,
                           env={k: v for k, v in host.items() if k != "VIRTUAL_ENV"},
                           timeout=_remaining(deadline))
    if cache.returncode or not cache.stdout.strip():
        raise SetupError(f"cannot resolve the uv cache directory: {cache.stderr.strip()[:200]}")
    cache_dir = cache.stdout.strip()
    env = _installer_env(host, tmp)
    network = [] if online else ["--offline"]
    request = f"{declaration['python']['implementation']}@{declaration['python']['version']}"
    find = [uv, "python", "find", request, "--system", "--no-config", "--no-python-downloads", *network]
    located = subprocess.run(find, capture_output=True, text=True, cwd=tmp,
                             env=dict(env, PATH=host.get("PATH", "")), timeout=_remaining(deadline))
    if located.returncode or not located.stdout.strip():
        raise SetupError(f"no installed interpreter matches {request}: {located.stderr.strip()[:400]}")
    interpreter = located.stdout.strip().splitlines()[-1]
    sync = [uv, "sync", "--frozen", *network, "--no-config", "--no-install-project", "--no-build",
            "--no-python-downloads", "--link-mode", "copy", "--cache-dir", cache_dir,
            "--python", interpreter, "--project", str(closure)]
    started = time.monotonic()
    process = subprocess.run(sync, capture_output=True, cwd=tmp, env=dict(env, UV_PROJECT_ENVIRONMENT=str(venv)),
                             timeout=_remaining(deadline))
    log = (process.stdout + process.stderr)[:LOG_LIMIT]
    if process.returncode:
        raise SetupError("uv sync failed: " + log.decode("utf-8", "replace").strip()[-600:])
    seconds = time.monotonic() - started
    probe = probe_interpreter(venv, declaration, host, tmp, deadline)
    lock = rules.lock_summary((closure / "uv.lock").read_bytes())
    distributions = installed_distributions(venv, probe, lock)
    inventory = inventory_tree(venv)
    record = dict(
        schema_version=1, kind="realized_environment", closure=_hash(_canonical(declaration)),
        installer=dict(tool="uv", version=observed, executable=uv, executable_sha256=_file_sha256(Path(uv)),
                       cache_dir=cache_dir, network="allowed" if online else "offline",
                       python_request=request, python_found=interpreter, command=sync,
                       environment_variables=dict(env, UV_PROJECT_ENVIRONMENT=str(venv)),
                       seconds=round(seconds, 3)),
        interpreter=dict(implementation=probe["implementation"], version=probe["version"],
                         sys_version=probe["sys_version"], executable=probe["executable"],
                         base_executable=probe["base_executable"],
                         base_executable_sha256=_file_sha256(Path(probe["base_executable"]).resolve())),
        platform=dict(**rules.host_platform(), release=probe["release"],
                      payload_view=dict(system=probe["system"], machine=probe["machine"])),
        distributions=distributions, distributions_sha256=_hash(_canonical(distributions)),
        inventory=inventory)
    return record, log


def probe_interpreter(venv: Path, declaration: dict[str, Any], host: dict[str, str], tmp: Path,
                      deadline: float) -> dict[str, Any]:
    env, _ = rules.payload_environment(declaration, host, bin_dir=str(_bin(venv)), tmp=str(tmp),
                                       home=str(tmp))
    process = subprocess.run([str(venv_python(venv)), "-s", "-B", "-c", _PROBE], capture_output=True,
                             text=True, cwd=tmp, env=env, timeout=_remaining(deadline))
    try:
        probe = json.loads(process.stdout)
    except ValueError as exc:
        raise SetupError(f"interpreter probe failed: {process.stderr.strip()[:400]}") from exc
    # The platform is judged from the worker's host view; the payload's view is
    # only recorded, because it depends on the variables the closure passes.
    host_view = rules.host_platform()
    expected = (declaration["python"]["implementation"], declaration["python"]["version"],
                declaration["platform"]["system"], declaration["platform"]["machine"])
    observed = (probe["implementation"], probe["version"], host_view["system"], host_view["machine"])
    if process.returncode or observed != expected:
        raise SetupError(f"realized interpreter {observed} differs from the closure {expected}")
    return probe


def _record_hash(value: str) -> str | None:
    if not value.startswith("sha256="):
        return None
    encoded = value[len("sha256="):]
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).hex()


def installed_distributions(venv: Path, probe: dict[str, Any], lock: dict[str, Any]) -> list[dict[str, Any]]:
    """Every installed dist-info: verified RECORD hashes and a content digest.

    The content digest leaves out installer-written files, so the same wheel
    installed on another host by the same lock yields the same value.
    """
    locked = {(row["name"], row["version"]) for row in lock["packages"]}
    rows = []
    sites = sorted({Path(probe["purelib"]).resolve(), Path(probe["platlib"]).resolve()})
    root = venv.resolve()
    for site in sites:
        if not site.is_dir():
            continue
        for info in sorted(site.glob("*.dist-info")):
            metadata = (info / "METADATA").read_text(encoding="utf-8", errors="replace")
            fields = dict(re.findall(r"^(Name|Version): *(.+?) *$", metadata.split("\n\n")[0], re.M))
            if set(fields) != {"Name", "Version"}:
                raise SetupError(f"installed distribution lacks Name/Version: {info.name}")
            name, version = rules.normalize_name(fields["Name"]), fields["Version"]
            if (name, version) not in locked:
                raise SetupError(f"installed distribution is not in the lock: {name} {version}")
            content, verified = [], True
            for row in csv.reader(io.StringIO((info / "RECORD").read_text(encoding="utf-8"))):
                if not row:
                    continue
                path, value = row[0], row[1] if len(row) > 1 else ""
                target = Path(os.path.normpath(site / path))
                if not target.is_relative_to(root):
                    raise SetupError(f"RECORD path leaves the environment: {path}")
                expected = _record_hash(value)
                if expected is None:
                    continue
                if not target.is_file() or _file_sha256(target) != expected:
                    verified = False
                if Path(path).parent.name != info.name or Path(path).name not in _VOLATILE:
                    content.append([path, expected])
            installer = info / "INSTALLER"
            rows.append(dict(name=name, version=version, dist_info=info.name,
                             installer=installer.read_text(encoding="utf-8").strip() if installer.is_file() else None,
                             record_verified=verified, files=len(content),
                             content_sha256=_hash(_canonical(sorted(content)))))
            if not verified:
                raise SetupError(f"installed files differ from RECORD: {name} {version}")
    return rows


def _portable(path: str) -> bool:
    """Leave out files that embed the venv location or uv cache-entry metadata."""
    parts = path.split("/")
    if len(parts) == 2 and parts[0] in {"Scripts", "bin"} and parts[1].startswith("activate"):
        return False
    if path == "pyvenv.cfg":
        return False
    return not (len(parts) >= 2 and parts[-2].endswith(".dist-info") and parts[-1] in _VOLATILE)


def inventory_tree(root: Path) -> dict[str, Any]:
    """Every file and link below ``root`` with its digest; links are not followed.

    ``sha256`` covers everything and detects changes within one job directory.
    ``portable_sha256`` omits activation scripts, pyvenv.cfg and installer-written
    dist-info files, so equal installed content compares equal across locations.
    """
    entries, total = [], 0
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as scan:
            items = sorted(scan, key=lambda item: item.name)
        for item in items:
            path = Path(item.path)
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                entries.append(dict(path=relative, link=os.readlink(path)))
            elif item.is_dir(follow_symlinks=False):
                pending.append(path)
            else:
                entries.append(dict(path=relative, sha256=_file_sha256(path), bytes=info.st_size))
                total += info.st_size
    entries.sort(key=lambda row: row["path"])
    portable = [row if "sha256" in row else dict(path=row["path"], link=True)
                for row in entries if _portable(row["path"])]
    return dict(files=entries, count=len(entries), bytes=total, sha256=_hash(_canonical(entries)),
                portable_sha256=_hash(_canonical(portable)))


def accelerators(host: dict[str, str]) -> dict[str, Any]:
    """NVIDIA GPU, driver and CUDA version when ``nvidia-smi`` is on the worker PATH.

    Records presence only; whether the program used an accelerator is not observed.
    """
    executable = shutil.which("nvidia-smi", path=host.get("PATH"))
    if executable is None:
        return dict(status="not_available", reason="nvidia-smi not found on the worker PATH")
    try:
        query = subprocess.run([executable, "--query-gpu=index,name,driver_version,memory.total",
                                "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=30)
        banner = subprocess.run([executable], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return dict(status="error", probe="nvidia-smi", reason=str(exc)[:400])
    if query.returncode:
        return dict(status="error", probe="nvidia-smi", reason=query.stderr.strip()[:400])
    return dict(status="available", probe="nvidia-smi", executable=executable,
                **parse_nvidia_smi(query.stdout, banner.stdout))


def parse_nvidia_smi(query: str, banner: str) -> dict[str, Any]:
    gpus = []
    for line in query.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) == 4:
            gpus.append(dict(index=parts[0], name=parts[1], driver_version=parts[2], memory_total_mib=parts[3]))
    match = re.search(r"CUDA Version:\s*([0-9.]+)", banner)
    return dict(gpus=gpus, cuda_version=match.group(1) if match else None)


def observe_inputs(job: Path, manifest: dict[str, Any], spec: dict[str, Any]) -> dict[str, str | None]:
    """Digests of the materialized program tree, input and closure project."""
    def rows(root: Path) -> list[dict[str, Any]]:
        files = []
        pending = [root]
        while pending:
            directory = pending.pop()
            for entry in sorted(directory.iterdir()):
                if rules.is_link(entry):
                    raise ValueError(f"materialized tree contains a link: {entry}")
                if entry.is_dir():
                    pending.append(entry)
                else:
                    files.append(dict(path=entry.relative_to(root).as_posix(), sha256=_file_sha256(entry),
                                      bytes=entry.stat().st_size))
        return sorted(files, key=lambda row: row["path"])
    def single(root: Path) -> str:
        found = rows(root)
        if [row["path"] for row in found] != [manifest["input_name"]]:
            raise ValueError("input directory must hold exactly the frozen input")
        return found[0]["sha256"]
    result: dict[str, str | None] = {}
    for name, compute in (
            ("program", lambda: _hash(_canonical(dict(manifest, files=rows(job / RUN / SOURCE_DIR))))),
            ("input", lambda: single(job / RUN / INPUT_DIR)),
            ("closure_project", lambda: _hash(_canonical(rows(job / CLOSURE))))):
        try:
            result[name] = compute()
        except (OSError, ValueError):
            result[name] = None
    return result


def expected_inputs(spec: dict[str, Any], declaration: dict[str, Any]) -> dict[str, str]:
    return dict(program=spec["program"], input=spec["input"]["sha256"],
                closure_project=rules.project_digest(declaration))


def _run_payload(control: Path, launch: list[str], cwd: Path, env: dict[str, str], spec: dict[str, Any],
                 identity: dict[str, Any], record: dict[str, Any]) -> bool:
    """Gate, run and stop the payload like v1; returns whether termination was confirmed."""
    limit = spec["max_output_bytes"]
    process = None
    job = None
    readers: list[threading.Thread] = []
    overflow = threading.Event()
    io_errors: list[str] = []
    stopped = True
    assigned = False

    def drain(pipe: Any, name: str) -> None:
        try:
            remaining = limit
            with (control / name).open("wb") as output:
                while data := pipe.read(4096):
                    if len(data) > remaining:
                        overflow.set()
                    output.write(data[:remaining])
                    remaining -= min(len(data), remaining)
                output.flush()
                os.fsync(output.fileno())
        except (OSError, ValueError) as exc:
            io_errors.append(str(exc))
            overflow.set()
        finally:
            pipe.close()

    try:
        if os.name == "nt":
            job = _WindowsJob()
            record["process_control"] = "Windows Job Object, payload gated until assignment"
        elif os.name == "posix":
            _prepare_posix()
            record["process_control"] = "POSIX process group; trusted descendants must not leave it"
        else:
            raise RuntimeError("process control unsupported on this platform")
        process = subprocess.Popen(launch, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=os.name == "posix",
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        stopped = False
        if job:
            job.assign(process)
            assigned = True
        for pipe, name in ((process.stdout, "stdout.bin"), (process.stderr, "stderr.bin")):
            thread = threading.Thread(target=drain, args=(pipe, name), daemon=True)
            thread.start()
            readers.append(thread)
        _atomic(control / "started.json", dict(identity=identity, worker_pid=os.getpid(),
                payload_pid=process.pid, started_at=record["started_at"], process_control=record["process_control"]))
        payload_started = time.monotonic()
        record["payload_started_at"] = _now()
        process.stdin.write(b"GO\n")
        process.stdin.close()
        while process.poll() is None:
            _atomic(control / "heartbeat.json", dict(identity=identity, observed_at=_now(), payload_pid=process.pid))
            if overflow.is_set():
                raise RuntimeError("stdout/stderr capture limit exceeded")
            if time.monotonic() - payload_started > spec["wall_seconds"]:
                raise RuntimeError("payload wall time limit exceeded")
            time.sleep(0.05)
        record["returncode"] = process.returncode
        if process.returncode:
            raise RuntimeError(f"payload exited {process.returncode}")
        record.update(status="completed", reason="")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        record.update(status="failed", reason=str(exc))
    finally:
        if process is not None:
            try:
                if job and assigned:
                    job.terminate()
                elif os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait(timeout=5)
                if os.name == "posix":
                    _confirm_posix_group(process.pid)
                stopped = True
                if record["returncode"] is None:
                    record["returncode"] = process.returncode
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                record.update(status="unknown", reason=f"termination unconfirmed: {exc}")
            finally:
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
                if not readers:
                    for pipe in (process.stdout, process.stderr):
                        pipe.close()
        if job:
            job.close()
        for thread in readers:
            thread.join(timeout=5)
        if any(thread.is_alive() for thread in readers) or not stopped:
            record.update(status="unknown", reason="process/log streams have not reached a verified end")
        elif overflow.is_set() or io_errors:
            record.update(status="failed", reason="stdout/stderr capture limit or stream failure: " + "; ".join(io_errors))
    return stopped


def execute(job: Path) -> dict[str, Any]:
    job = job.resolve()
    control, run = job / CONTROL, job / RUN
    identity = json.loads((control / "identity.json").read_bytes())
    data = (control / "spec.json").read_bytes()
    spec = json.loads(data)
    closure_bytes, manifest_bytes = (control / "closure.json").read_bytes(), (control / "program.json").read_bytes()
    if (_hash(data) != identity["specification"] or _canonical(spec) != data
            or _hash(closure_bytes) != spec["environment"] or _hash(manifest_bytes) != spec["program"]):
        raise ValueError("worker specification identity mismatch")
    # The durable marker is never removed. Repeated worker delivery cannot execute again.
    with (control / "launch.lock").open("xb") as marker:
        marker.write(_canonical(identity))
        marker.flush()
        os.fsync(marker.fileno())
    started = time.monotonic()
    record: dict[str, Any] = dict(
        schema_version=2, profile=rules.PROFILE, identity=identity, status="failed",
        reason="worker did not start payload", returncode=None, started_at=_now(), command=spec["command"],
        launch_command=None, working_directory=str(run), environment_variables={}, inherited_missing=[],
        inputs_before={}, inputs_after={}, environment_record=None, environment_after=None,
        installer_log=None, accelerators=None, process_control=None,
        capture_limit="per declared output and stdout/stderr; no OS disk quota",
        bootstrap_sha256=_hash((_SHIM + "\n" + _BOOTSTRAP).encode()), outputs={}, isolation=ISOLATION)
    for filename in ("stdout.bin", "stderr.bin"):
        (control / filename).touch(exist_ok=False)
    stopped = True
    host = dict(os.environ)
    try:
        declaration = rules.check_closure(json.loads(closure_bytes))
        manifest = rules.check_source_manifest(json.loads(manifest_bytes))
        expected = expected_inputs(spec, declaration)
        record["inputs_before"] = observe_inputs(job, manifest, spec)
        if record["inputs_before"] != expected:
            raise ValueError("frozen inputs differ from the specification before execution")
        try:
            environment, log = materialize_environment(
                declaration, job / CLOSURE, job / VENV, job / TMP, host=host,
                deadline=time.monotonic() + spec["setup_seconds"])
        except subprocess.TimeoutExpired as exc:
            raise SetupError(f"environment setup timed out: {exc}") from exc
        except SetupError as exc:
            (control / "installer.log").write_bytes(str(exc).encode("utf-8")[:LOG_LIMIT])
            record["installer_log"] = _entry(control, "installer.log", LOG_LIMIT)
            raise
        (control / "installer.log").write_bytes(log)
        record["installer_log"] = _entry(control, "installer.log", LOG_LIMIT)
        (control / "environment.json").write_bytes(_canonical(environment))
        record["environment_record"] = _entry(control, "environment.json", ENVIRONMENT_LIMIT)
        record["accelerators"] = accelerators(host)
        env, missing = rules.payload_environment(declaration, host, bin_dir=str(_bin(job / VENV)),
                                                 tmp=str(job / TMP), home=str(job / HOME))
        record.update(environment_variables=env, inherited_missing=missing)
        base = environment["interpreter"]["base_executable"]
        launch = [base, "-I", "-S", "-c", _SHIM, str(venv_python(job / VENV)), "-s", "-B", "-c", _BOOTSTRAP,
                  *spec["command"][3:]]
        record["launch_command"] = launch
        stopped = False
        stopped = _run_payload(control, launch, run, env, spec, identity, record)
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        record.update(status="failed", reason=str(exc))
    if record["status"] != "unknown":
        record["inputs_after"] = observe_inputs(job, manifest, spec) if record["inputs_before"] else {}
        if record["status"] == "completed" and record["inputs_after"] != expected:
            record.update(status="failed", reason="frozen inputs changed during execution")
        if record["environment_record"] is not None:
            record["environment_after"] = dict(inventory_sha256=inventory_tree(job / VENV)["sha256"])
            if (record["status"] == "completed"
                    and record["environment_after"]["inventory_sha256"] != environment["inventory"]["sha256"]):
                record.update(status="failed", reason="locked environment changed during execution")
        for name, filename in spec["outputs"].items():
            try:
                record["outputs"][name] = _entry(run, filename, spec["max_output_bytes"])
            except (OSError, ValueError) as exc:
                if record["status"] == "completed":
                    record.update(status="failed", reason=str(exc))
    limit = spec["max_output_bytes"]
    record.update(stdout=_entry(control, "stdout.bin", limit), stderr=_entry(control, "stderr.bin", limit),
                  finished_at=_now(), elapsed_seconds=time.monotonic() - started, termination_confirmed=stopped)
    _atomic(control / "completion.json", record)
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=Path, required=True)
    execute(parser.parse_args().job)
