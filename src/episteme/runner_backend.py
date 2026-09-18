"""Trusted local Python worker. No database access and no sandbox claim.

Windows Job Objects contain descendants; POSIX uses a new process group.
Payload starts only after the controller has installed its process control.
The stdout/stderr cap bounds captured bytes, not total disk or memory use.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_BOOTSTRAP = "import sys,runpy; permit=sys.stdin.buffer.readline(); sys.stdin.close(); assert permit==b'GO\\n'; sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint() -> dict[str, Any]:
    if os.name == "nt":
        version = sys.getwindowsversion()
        observed_platform = f"Windows-{version.major}.{version.minor}.{version.build}-{version.service_pack}"
    elif hasattr(os, "uname"):
        version = os.uname()
        observed_platform = f"{version.sysname}-{version.release}-{version.version}"
    else:
        observed_platform = sys.platform
    return dict(python=sys.version, implementation=platform.python_implementation(),
                executable=str(Path(sys.executable).resolve()),
                executable_sha256=_hash(Path(sys.executable).read_bytes()),
                platform=observed_platform, machine=platform.machine(),
                isolation="trusted local; no filesystem/network sandbox",
                environment_closure="interpreter fingerprint only; stdlib/OS not bundled")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic(path: Path, record: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_canonical(record))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def plain_file(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISREG(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400)


def _entry(workspace: Path, name: str, limit: int) -> dict[str, Any]:
    path = workspace / name
    if Path(name).name != name or not plain_file(path) or path.stat().st_size > limit:
        raise ValueError(f"missing, unsafe or oversized output: {name}")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"oversized output: {name}")
    return dict(path=name, sha256=_hash(data), bytes=len(data))


def _prepare_posix() -> None:
    if sys.platform.startswith("linux"):
        # Adopt orphaned descendants so our own process group can be reaped,
        # instead of depending on the host's PID 1 zombie-reaping policy.
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.restype = ctypes.c_int
        if prctl(ctypes.c_int(36), ctypes.c_ulong(1), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)):
            raise OSError(ctypes.get_errno(), "cannot establish worker child subreaper")


def _confirm_posix_group(group: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            while os.waitpid(-group, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            pass
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    raise RuntimeError("could not confirm POSIX process group termination")


class _WindowsJob:
    def __init__(self) -> None:
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [("user", ctypes.c_longlong), ("job", ctypes.c_longlong), ("flags", w.DWORD),
                        ("min", ctypes.c_size_t), ("max", ctypes.c_size_t), ("active", w.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("schedule", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("ro", "wo", "oo", "rb", "wb", "ob")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_mem", ctypes.c_size_t),
                        ("job_mem", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in ("user", "kernel", "period_user", "period_kernel")] + [
                (name, w.DWORD) for name in ("faults", "total", "active", "terminated")]
        self.Accounting = Accounting
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {"CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
                      "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
                      "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
                      "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
                      "QueryInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
                      "CloseHandle": ([w.HANDLE], w.BOOL)}
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process: subprocess.Popen) -> None:
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self) -> None:
        if not self.api.TerminateJobObject(self.handle, 124):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            accounting = self.Accounting()
            if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if accounting.active == 0:
                return
            time.sleep(0.02)
        raise RuntimeError("could not confirm Windows job termination")

    def close(self) -> None:
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def execute(workspace: Path, spec: dict[str, Any]) -> dict[str, Any]:
    workspace = workspace.resolve()
    identity = json.loads((workspace / "identity.json").read_bytes())
    if _hash((workspace / "spec.json").read_bytes()) != identity["specification"] or _canonical(spec) != (workspace / "spec.json").read_bytes():
        raise ValueError("worker specification identity mismatch")
    # The durable marker is never removed. Repeated worker delivery cannot execute again.
    with (workspace / "launch.lock").open("xb") as marker:
        marker.write(_canonical(identity))
        marker.flush()
        os.fsync(marker.fileno())
    started = time.monotonic()
    record = dict(schema_version=1, identity=identity, status="failed", reason="worker did not start payload",
                  returncode=None, started_at=_now(), command=spec["command"], runtime=fingerprint(),
                  outputs={}, inputs_before={}, inputs_after={}, process_control=None,
                  capture_limit="per declared output and stdout/stderr; no OS disk quota",
                  bootstrap_sha256=_hash(_BOOTSTRAP.encode()))
    limit = spec["max_output_bytes"]
    process = None
    job = None
    readers: list[threading.Thread] = []
    overflow = threading.Event()
    io_errors: list[str] = []
    stopped = True
    assigned = False
    for filename in ("stdout.bin", "stderr.bin"):
        (workspace / filename).touch(exist_ok=False)

    def drain(pipe: Any, name: str) -> None:
        try:
            remaining = limit
            with (workspace / name).open("wb") as output:
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
        command = spec["command"]
        if (command[0] != sys.executable or command[1:6] != ["-I", "-S", "program.py", "input.dat", "--seed"]
                or len(command) != 7 or record["runtime"] != spec["environment_fingerprint"]):
            raise ValueError("worker runtime or command differs from frozen specification")
        for name, expected in spec["expected_inputs"].items():
            observed = _entry(workspace, name, 1024**3)["sha256"]
            record["inputs_before"][name] = observed
            if observed != expected:
                raise ValueError("frozen input bytes changed before execution")
        if os.name == "nt":
            job = _WindowsJob()
            record["process_control"] = "Windows Job Object, payload gated until assignment"
        elif os.name == "posix":
            _prepare_posix()
            record["process_control"] = "POSIX process group; trusted descendants must not leave it"
        else:
            raise RuntimeError("process control unsupported on this platform")
        # The bootstrap blocks on stdin before any research code is loaded.
        launch = [command[0], "-I", "-S", "-c", _BOOTSTRAP, *command[3:]]
        record["launch_command"] = launch
        process = subprocess.Popen(launch, cwd=workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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
        _atomic(workspace / "started.json", dict(identity=identity, worker_pid=os.getpid(),
                payload_pid=process.pid, started_at=record["started_at"], process_control=record["process_control"]))
        payload_started = time.monotonic()
        record["payload_started_at"] = _now()
        process.stdin.write(b"GO\n")
        process.stdin.close()
        while process.poll() is None:
            _atomic(workspace / "heartbeat.json", dict(identity=identity, observed_at=_now(), payload_pid=process.pid))
            if overflow.is_set():
                raise RuntimeError("stdout/stderr capture limit exceeded")
            if time.monotonic() - payload_started > spec["wall_seconds"]:
                raise RuntimeError("payload wall time limit exceeded")
            time.sleep(0.05)
        record["returncode"] = process.returncode
        if process.returncode:
            raise RuntimeError(f"payload exited {process.returncode}")
        record.update(status="completed", reason="")
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
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
                # Assignment can fail before readers are installed.
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
    if record["status"] != "unknown":
        for name in spec["expected_inputs"]:
            try:
                record["inputs_after"][name] = _entry(workspace, name, 1024**3)["sha256"]
            except (OSError, ValueError):
                record["inputs_after"][name] = None
        if record["status"] == "completed" and record["inputs_after"] != spec["expected_inputs"]:
            record.update(status="failed", reason="frozen inputs changed during execution")
        for name, filename in spec["outputs"].items():
            try:
                record["outputs"][name] = _entry(workspace, filename, limit)
            except (OSError, ValueError) as exc:
                if record["status"] == "completed":
                    record.update(status="failed", reason=str(exc))
    record.update(stdout=_entry(workspace, "stdout.bin", limit), stderr=_entry(workspace, "stderr.bin", limit),
                  finished_at=_now(), elapsed_seconds=time.monotonic() - started,
                  termination_confirmed=stopped)
    _atomic(workspace / "completion.json", record)
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    execute(args.workspace, json.loads((args.workspace / "spec.json").read_bytes()))
