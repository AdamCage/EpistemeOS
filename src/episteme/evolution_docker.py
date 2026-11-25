"""Run evolution candidates in a pinned local Linux container.

The evaluator and its expected answers stay on the host. An image supplies a
trusted worker (an ordinary Python module with run(operation, payload, profile))
and the base EpistemeOS package. Candidate Python files are overlaid in tmpfs.
"""

from __future__ import annotations

import re
import subprocess
import threading
from typing import Any
from uuid import uuid4

from .store import _canonical_json, canonical


_INPUT_BYTES = 1024 * 1024
_OUTPUT_BYTES = 64 * 1024
_BOOTSTRAP = r'''
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile

request = json.load(sys.stdin)
patch = request["patch"]
if request["base_commit"] is not None:
    baseline = Path("/opt/episteme/base-commit").read_text(encoding="utf-8").strip()
    if baseline != request["base_commit"]:
        raise ValueError("image does not contain the pinned Git baseline")
if patch:
    spec = importlib.util.find_spec("episteme")
    root = Path(tempfile.mkdtemp(prefix="candidate-", dir="/tmp"))
    tree = root / "episteme"
    shutil.copytree(spec.submodule_search_locations[0], tree,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name, content in patch.items():
        relative = Path(name).relative_to("src/episteme")
        if ".." in relative.parts:
            raise ValueError("invalid patch path")
        target = tree / relative
        if not target.is_file() or target.resolve() != target:
            raise ValueError("patch target is absent from the pinned base package")
        target.write_text(content, encoding="utf-8", newline="\n")
    sys.path.insert(0, str(root))
worker = importlib.import_module(sys.argv[1])
output = worker.run(request["operation"], request["payload"], request["profile"])
if type(output) is not dict:
    raise ValueError("worker must return a JSON object")
print(json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False))
'''


class EvaluationError(ValueError):
    """The isolated worker was unavailable or returned an unusable result."""


def _bounded_run(args: list[str], data: bytes, timeout: int) -> tuple[int, bytes, bytes]:
    """Bound both attached streams before retaining them in host memory."""
    with subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as child:
        streams = (child.stdout, child.stderr)
        captured = [b"", b""]
        overflow = threading.Event()
        read_error = threading.Event()

        def read(index: int) -> None:
            try:
                captured[index] = streams[index].read(_OUTPUT_BYTES + 1)
                if len(captured[index]) > _OUTPUT_BYTES:
                    overflow.set()
                    child.kill()
            except OSError:
                read_error.set()

        def write() -> None:
            try:
                with child.stdin:
                    child.stdin.write(data)
            except OSError:
                pass  # The subprocess result describes an early exit.

        threads = [threading.Thread(target=read, args=(index,), daemon=True) for index in range(2)]
        threads.append(threading.Thread(target=write, daemon=True))
        for thread in threads:
            thread.start()
        try:
            child.wait(timeout=timeout)
        except BaseException:
            child.kill()
            child.wait()
            raise
        finally:
            for thread in threads:
                thread.join(timeout=1)
        if overflow.is_set() or read_error.is_set() or any(thread.is_alive() for thread in threads):
            raise EvaluationError("Docker worker stream exceeded its bound or could not be read")
        return child.returncode, captured[0], captured[1]


class DockerBackend:
    """Fail closed on a missing image/daemon, timeout, or invalid result."""

    def __init__(self, image: str, worker: str = "episteme_evolution_worker", timeout: int = 15):
        if type(image) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("Docker image must be a local immutable image ID")
        if (type(worker) is not str or not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", worker)
                or worker.split(".")[0] == "episteme"):
            raise ValueError("Docker worker must be a module outside the candidate package")
        if type(timeout) is not int or not 1 <= timeout <= 60:
            raise ValueError("Docker timeout must be between 1 and 60 seconds")
        self.image, self.worker, self.timeout = image, worker, timeout

    @property
    def descriptor(self) -> dict[str, Any]:
        return dict(image=self.image, worker=self.worker, timeout=self.timeout)

    def invoke(self, operation: str, payload: dict[str, Any], pin: dict[str, Any]) -> dict[str, Any]:
        if operation not in {"evaluate", "policy"}:
            raise ValueError("unsupported Docker evolution operation")
        expected = pin.get("backend")
        shortened = (operation == "policy" and type(expected) is dict
                     and type(expected.get("timeout")) is int and self.timeout <= expected["timeout"]
                     and dict(expected, timeout=self.timeout) == self.descriptor)
        if expected not in (None, self.descriptor) and not shortened:
            raise EvaluationError("Docker backend differs from the release pin")
        body = canonical(dict(operation=operation, payload=payload,
                              profile=pin["profile"], patch=pin["patch"], base_commit=pin["base_commit"]))
        if len(body) > _INPUT_BYTES:
            raise EvaluationError("Docker worker request exceeds its byte bound")
        name = "episteme-evolution-" + uuid4().hex
        docker = ["docker", "--context", "default"]
        args = docker + ["run", "--rm", "--name", name, "--pull", "never", "--network", "none",
                         "--read-only", "--log-driver", "none", "--cap-drop", "ALL", "--security-opt",
                         "no-new-privileges", "--user", "65534:65534", "--pids-limit", "32", "--memory",
                         "256m", "--memory-swap", "256m", "--cpus", "1", "--tmpfs",
                         "/tmp:rw,noexec,nosuid,nodev,size=32m", "--entrypoint", "python",
                         "-i", self.image, "-I", "-B", "-c", _BOOTSTRAP, self.worker]
        try:
            code, stdout, stderr = _bounded_run(args, body, self.timeout)
        except BaseException as exc:
            try:
                subprocess.run(docker + ["rm", "-f", name], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
            if isinstance(exc, (EvaluationError, OSError, subprocess.TimeoutExpired)):
                raise EvaluationError(f"Docker worker did not complete ({type(exc).__name__})") from exc
            raise
        if code:
            detail = stderr.decode("utf-8", "replace").strip()[:300]
            raise EvaluationError(f"Docker worker exited with code {code}: {detail}")
        try:
            result = _canonical_json(stdout.decode("utf-8").removesuffix("\n"))
        except (ValueError, UnicodeError) as exc:
            raise EvaluationError("Docker worker did not return canonical JSON") from exc
        if type(result) is not dict:
            raise EvaluationError("Docker worker must return a JSON object")
        return result
