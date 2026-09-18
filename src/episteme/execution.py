"""One-shot local dispatch and receipt-backed admission of execution evidence.

The controller owns Store; the worker receives files in a separate directory.
This is trusted local execution, not an OS/network or actor security boundary.
An ambiguous dispatch is never retried automatically, even after a restart.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from datetime import datetime
from typing import Any
from uuid import uuid4

from .kernel import Actor, Kernel, require
from .store import Store, canonical, digest


BACKEND = "trusted_local_python_v1"
CAPABILITIES = {"separate_cwd", "python_isolated_mode", "process_group_timeout", "job_object_timeout", "bounded_output_capture"}
KINDS = {"execution_job", "execution_dispatch", "execution_finalized"}
_RESERVED = {"program.py", "input.dat", "spec.json", "identity.json", "completion.json",
             "started.json", "heartbeat.json", "launch.lock", "stdout.bin", "stderr.bin"}


def freeze_environment(store: Store) -> str:
    """Record the available interpreter fingerprint, not a portable environment."""
    from .runner_backend import fingerprint
    return store.put_json(dict(schema_version=1, backend=BACKEND, fingerprint=fingerprint()))


def _object(data: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON key in execution artifact")
            result[key] = value
        return result
    def invalid(value: str) -> None:
        raise ValueError(f"nonfinite JSON value: {value}")
    result = json.loads(data, object_pairs_hook=unique, parse_constant=invalid)
    require(type(result) is dict, "execution artifact must be a JSON object")
    return result


def _spec(spec: dict[str, Any]) -> None:
    require(set(spec) == {"schema_version", "command", "outputs", "wall_seconds",
                          "max_output_bytes", "expected_inputs", "environment_fingerprint"}
            and type(spec["schema_version"]) is int and spec["schema_version"] == 1,
            "unsupported execution specification")
    require(type(spec["wall_seconds"]) is int and 1 <= spec["wall_seconds"] <= 86400,
            "wall_seconds must be an integer from 1 to 86400")
    require(type(spec["max_output_bytes"]) is int and 1 <= spec["max_output_bytes"] <= 1024**3,
            "max_output_bytes must be an integer from 1 to 1 GiB")
    outputs = spec["outputs"]
    require(type(outputs) is dict and {"raw_data", "metrics"} <= outputs.keys()
            and "log" not in outputs and "execution_manifest" not in outputs,
            "declared outputs need raw_data and metrics; controller output names are reserved")
    require(all(type(k) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", k)
                and type(v) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", v)
                and not v.endswith((".", " ")) and v.lower() not in _RESERVED
                and v.split(".")[0].upper() not in {"CON", "PRN", "AUX", "NUL",
                    *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}
                for k, v in outputs.items()), "output paths must be portable, distinct plain basenames")
    require(len({v.lower() for v in outputs.values()}) == len(outputs), "output paths collide")
    require(type(spec["command"]) is list and len(spec["command"]) == 7
            and spec["command"][1:6] == ["-I", "-S", "program.py", "input.dat", "--seed"],
            "unsupported local Python invocation")
    require(type(spec["environment_fingerprint"]) is dict, "environment fingerprint required")
    require(type(spec["expected_inputs"]) is dict
            and set(spec["expected_inputs"]) == {"program.py", "input.dat"}
            and all(type(key) is str and re.fullmatch(r"[0-9a-f]{64}", key)
                    for key in spec["expected_inputs"].values()), "invalid frozen input hashes")


def _receipt_for(store: Store, ids: list[str]) -> dict[str, Any]:
    receipt = next((item for item in store.receipts() if all(id in item["event_ids"] for id in ids)), None)
    require(receipt is not None, "managed execution events must share their original command receipt")
    return receipt


def _index(store: Store, history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Validate typed execution links at each prefix and their frozen artifacts."""
    events = {event["id"]: event for event in history}
    jobs: dict[str, dict[str, Any]] = {}
    runs: set[str] = set()
    def ref(event: dict[str, Any], id: str, kind: str) -> dict[str, Any]:
        prior = events.get(id)
        require(prior is not None and prior["kind"] == kind and prior["seq"] < event["seq"],
                "invalid prior execution reference")
        require((prior["actor"], prior["role"]) == (event["actor"], event["role"]),
                "execution actor differs from its assigned run")
        return prior
    for event in history:
        kind, p = event["kind"], event["payload"]
        if kind not in KINDS:
            continue
        require(event["role"] in {"executor", "replicator"}, "execution requires an assigned execution role")
        require(type(p.get("schema_version")) is int and p["schema_version"] == 1,
                "unsupported execution event version")
        if kind == "execution_job":
            require(set(p) == {"schema_version", "run", "run_hash", "specification", "mode", "capabilities"},
                    "invalid execution job fields")
            run = ref(event, p["run"], "run")
            require(run["id"] not in runs and run["seq"] + 1 == event["seq"] and p["run_hash"] == run["hash"],
                    "job must immediately bind one new run")
            _receipt_for(store, [run["id"], event["id"]])
            r = run["payload"]
            plan = Kernel._get(history, r["protocol"], "protocol")["payload"]
            spec = _object(store.read(p["specification"]))
            _spec(spec)
            expected_data = plan["data"]
            if r["replicate_of"] is not None:
                result = Kernel._result([e for e in history if e["seq"] < run["seq"]], r["replicate_of"])
                require(result is not None and result["payload"]["status"] == "completed",
                        "reanalysis job requires completed original data")
                expected_data = result["payload"]["outputs"]["raw_data"]
            require(spec["command"] == r["command"] and spec["command"][-1] == str(r["seed"])
                    and spec["expected_inputs"] == {"program.py": r["implementation"], "input.dat": expected_data},
                    "execution specification differs from frozen run inputs")
            environment = _object(store.read(r["environment"]))
            require(environment == dict(schema_version=1, backend=BACKEND, fingerprint=spec["environment_fingerprint"]),
                    "execution environment differs from its frozen declaration")
            require(p["mode"] == ("independent_reanalysis" if r["replicate_of"] else "primary")
                    and type(p["capabilities"]) is list and set(p["capabilities"]) <= CAPABILITIES,
                    "unsupported execution mode/capability")
            for key in spec["expected_inputs"].values():
                store.read(key)
            runs.add(run["id"])
            jobs[event["id"]] = dict(job=event, run=run, spec=spec, dispatch=None, finalized=None)
        else:
            job = ref(event, p.get("job"), "execution_job")
            state = jobs[job["id"]]
            require(p.get("job_hash") == job["hash"], "execution job hash mismatch")
            if kind == "execution_dispatch":
                require(set(p) == {"schema_version", "job", "job_hash", "workspace_token"}
                        and type(p["workspace_token"]) is str and re.fullmatch(r"[0-9a-f]{32}", p["workspace_token"]),
                        "invalid execution dispatch fields")
                require(state["dispatch"] is None and Kernel._result(
                    [e for e in history if e["seq"] < event["seq"]], state["run"]["id"]) is None,
                        "execution already dispatched or terminal")
                _receipt_for(store, [event["id"]])
                state["dispatch"] = event
            else:
                require(set(p) == {"schema_version", "job", "job_hash", "dispatch", "manifest", "result"},
                        "invalid execution finalization fields")
                require(state["dispatch"] is not None and state["finalized"] is None
                        and p["dispatch"] == state["dispatch"]["id"], "invalid execution finalization dispatch")
                result = ref(event, p["result"], "result")
                require(result["payload"]["run"] == state["run"]["id"] and result["seq"] + 1 == event["seq"],
                        "execution finalization must follow its own result")
                _receipt_for(store, [result["id"], event["id"]])
                _check_completion(store, state, p["manifest"], result["payload"])
                state["finalized"] = event
    # A result for a managed run can only come from the paired finalization.
    for state in jobs.values():
        result = Kernel._result(history, state["run"]["id"])
        require(result is None or state["finalized"] is not None,
                "managed run result has no verified execution finalization")
    return jobs


def _identity(state: dict[str, Any]) -> dict[str, Any]:
    return dict(schema_version=1, job=state["job"]["id"], run=state["run"]["id"],
                dispatch=state["dispatch"]["id"], specification=state["job"]["payload"]["specification"])


def _check_completion(store: Store, state: dict[str, Any], manifest: str,
                      result: dict[str, Any] | None = None) -> tuple[str, dict[str, str], str]:
    record = _object(store.read(manifest))
    require(record.get("schema_version") == 1 and record.get("identity") == _identity(state),
            "completion belongs to a different execution")
    require(record.get("command") == state["spec"]["command"], "completion command differs from specification")
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
    if status == "completed":
        require(record.get("runtime") == state["spec"]["environment_fingerprint"], "completion environment mismatch")
        require(record.get("inputs_before") == state["spec"]["expected_inputs"]
                and record.get("inputs_after") == state["spec"]["expected_inputs"],
                "completion inputs differ from frozen specification")
        control = record.get("process_control", "")
        caps = state["job"]["payload"]["capabilities"]
        require(("job_object_timeout" not in caps or control.startswith("Windows Job Object"))
                and ("process_group_timeout" not in caps or control.startswith("POSIX process group")),
                "completion did not provide the requested process control")
    outputs: dict[str, str] = {}
    entries = record.get("outputs")
    require(type(entries) is dict and set(entries) <= set(state["spec"]["outputs"]), "unexpected completion output")
    require(status != "completed" or set(entries) == set(state["spec"]["outputs"]), "completion misses expected outputs")
    for name, item in {**entries, "_stdout": record.get("stdout"), "_stderr": record.get("stderr")}.items():
        require(type(item) is dict and set(item) == {"path", "sha256", "bytes"}, "invalid completion output entry")
        expected = {"_stdout": "stdout.bin", "_stderr": "stderr.bin"}.get(name, state["spec"]["outputs"].get(name))
        require(item["path"] == expected and type(item["bytes"]) is int
                and 0 <= item["bytes"] <= state["spec"]["max_output_bytes"], "invalid completion output path/size")
        data = store.read(item["sha256"])
        require(len(data) == item["bytes"], "completion output size mismatch")
        if name not in {"_stdout", "_stderr"}:
            outputs[name] = item["sha256"]
    outputs["log"] = manifest
    outputs["execution_manifest"] = manifest
    if status == "completed":
        plan = Kernel._get(store.events(), state["run"]["payload"]["protocol"], "protocol")["payload"]
        try:
            Kernel(store, Actor("execution-validator", "observer"))._metric(outputs["metrics"], plan["metric"])
        except ValueError as exc:
            status, reason = "failed", f"execution output validation failed: {exc}"
    if result is not None:
        require(result["status"] == status and result["outputs"] == outputs and result["reason"] == reason,
                "terminal result differs from verified completion")
    return status, outputs, reason


def execution_context(store: Store, history: list[dict[str, Any]], run_ids: set[str]) -> list[dict[str, Any]]:
    states = _index(store, history)
    selected = {e["id"] for state in states.values() if state["run"]["id"] in run_ids
                for e in (state["job"], state["dispatch"], state["finalized"]) if e is not None}
    return [event for event in history if event["id"] in selected]


def execution_artifacts(store: Store, event: dict[str, Any]) -> set[str]:
    p = event["payload"]
    if event["kind"] == "execution_job":
        spec = _object(store.read(p["specification"]))
        return {p["specification"], *spec["expected_inputs"].values()}
    if event["kind"] == "execution_finalized":
        record = _object(store.read(p["manifest"]))
        return {p["manifest"], record["stdout"]["sha256"], record["stderr"]["sha256"],
                *(item["sha256"] for item in record["outputs"].values())}
    return set()


class Execution:
    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def _history(self) -> list[dict[str, Any]]:
        require(self.store._command_context is not None,
                "execution transitions require CommandService transactions")
        return self.store.events()

    def _write(self, kind: str, payload: dict[str, Any]) -> str:
        return Kernel(self.store, self.actor)._write(self.store.events(), kind, payload, {"executor", "replicator"})

    def enqueue(self, *, protocol: str, seed: int, outputs: dict[str, str], wall_seconds: int,
                max_output_bytes: int, implementation: str | None = None,
                environment: str | None = None, replicate_of: str | None = None,
                required_capabilities: list[str] | None = None) -> str:
        history = self._history()
        _index(self.store, history)
        plan = Kernel._get(history, protocol, "protocol")["payload"]
        implementation = plan["implementation"] if implementation is None else implementation
        environment = plan["environment"] if environment is None else environment
        declared = _object(self.store.read(environment))
        require(set(declared) == {"schema_version", "backend", "fingerprint"}
                and type(declared["schema_version"]) is int and declared["schema_version"] == 1 and declared["backend"] == BACKEND,
                "local runner requires an explicitly frozen local Python environment")
        capabilities = [] if required_capabilities is None else required_capabilities
        available = CAPABILITIES - {"process_group_timeout", "job_object_timeout"}
        available |= {"job_object_timeout"} if os.name == "nt" else {"process_group_timeout"} if os.name == "posix" else set()
        require(len(set(capabilities)) == len(capabilities) and set(capabilities) <= available,
                "required execution capability is unsupported")
        data = plan["data"]
        if replicate_of is not None:
            original = Kernel._result(history, replicate_of)
            require(original is not None and original["payload"]["status"] == "completed", "completed original required")
            data = original["payload"]["outputs"]["raw_data"]
        self.store.read(data)
        command = [sys.executable, "-I", "-S", "program.py", "input.dat", "--seed", str(seed)]
        spec = dict(schema_version=1, command=command, outputs=outputs,
                    wall_seconds=wall_seconds, max_output_bytes=max_output_bytes,
                    expected_inputs={"program.py": implementation, "input.dat": data},
                    environment_fingerprint=declared["fingerprint"])
        _spec(spec)
        key = self.store.put_json(spec)
        run = Kernel(self.store, self.actor).start_run(protocol, seed=seed, implementation=implementation,
                                                      environment=environment, command=command, replicate_of=replicate_of)
        event = Kernel._get(self.store.events(), run, "run")
        return self._write("execution_job", dict(schema_version=1, run=run, run_hash=event["hash"],
                    specification=key, mode="independent_reanalysis" if replicate_of else "primary",
                    capabilities=capabilities))

    def dispatch(self, *, job: str, workspace_token: str) -> str:
        state = _index(self.store, self._history())[job]
        require(state["job"]["actor"] == self.actor.id and state["job"]["role"] == self.actor.role,
                "only assigned execution actor may dispatch")
        require(state["dispatch"] is None, "execution already dispatched; reconcile instead of launching again")
        require(type(workspace_token) is str and re.fullmatch(r"[0-9a-f]{32}", workspace_token), "invalid workspace token")
        return self._write("execution_dispatch", dict(schema_version=1, job=job,
                            job_hash=state["job"]["hash"], workspace_token=workspace_token))

    def finalize(self, *, job: str, manifest: str) -> str:
        state = _index(self.store, self._history())[job]
        require(state["dispatch"] is not None and state["finalized"] is None, "execution not dispatched or already finalized")
        require(state["job"]["actor"] == self.actor.id and state["job"]["role"] == self.actor.role,
                "only assigned execution actor may finalize")
        status, outputs, reason = _check_completion(self.store, state, manifest)
        result = Kernel(self.store, self.actor)._finish_run(state["run"]["id"], status=status,
                    outputs=outputs, reason=reason, managed_job=job)
        return self._write("execution_finalized", dict(schema_version=1, job=job,
                    job_hash=state["job"]["hash"], dispatch=state["dispatch"]["id"], manifest=manifest, result=result))


def job_state(store: Store, job: str) -> dict[str, Any]:
    state = _index(store, store.events())[job]
    status = "queued" if state["dispatch"] is None else "unknown"
    result = None
    if state["finalized"] is not None:
        result = state["finalized"]["payload"]["result"]
        status = Kernel._get(store.events(), result, "result")["payload"]["status"]
    return dict(job=job, run=state["run"]["id"], status=status, result=result,
                dispatch=state["dispatch"]["id"] if state["dispatch"] else None,
                scientific_validity="not_assessed", isolation=BACKEND,
                unknown_meaning="dispatch exists; no verified terminal evidence; never auto-relaunch")


def _envelope(store: Store, state: dict[str, Any], action: str, payload: dict[str, Any]) -> dict[str, Any]:
    receipt = _receipt_for(store, [state["job"]["id"]])
    context = dict(receipt["context"], command_id=f"execution-{uuid4().hex}",
                   expected_revision=len(store.events()), causation_id=state["job"]["id"])
    return dict(context=context, request=dict(version=1, action=action, payload=payload))


def _workspace(store: Store, state: dict[str, Any]) -> Path:
    parent = store.root / "executions"
    path = parent / state["dispatch"]["payload"]["workspace_token"]
    for directory in (parent, path):
        if directory.exists() or directory.is_symlink():
            info = directory.lstat()
            require(stat.S_ISDIR(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400),
                    "execution workspace must be a plain directory")
    return path


def work_job(store: Store, job: str) -> dict[str, Any]:
    """Fresh admission may spawn once. Observing an old dispatch never spawns."""
    from .commands import CommandService
    state = _index(store, store.events())[job]
    if state["dispatch"] is not None:
        return reconcile_job(store, job)
    envelope = _envelope(store, state, "execution.dispatch", dict(job=job, workspace_token=uuid4().hex))
    # Each invocation uses a fresh command ID; no replay acknowledgement grants launch authority.
    CommandService(store).execute(envelope)
    state = _index(store, store.events())[job]
    workspace = _workspace(store, state)
    workspace.mkdir(parents=True, exist_ok=False)
    for name, key in state["spec"]["expected_inputs"].items():
        (workspace / name).write_bytes(store.read(key))
    (workspace / "spec.json").write_bytes(store.read(state["job"]["payload"]["specification"]))
    (workspace / "identity.json").write_bytes(canonical(_identity(state)))
    worker = Path(__file__).with_name("runner_backend.py")
    process = subprocess.Popen([sys.executable, str(worker), "--workspace", str(workspace)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # The worker may outlive this controller. Its durable completion remains reconcilable.
    process.wait()
    return reconcile_job(store, job)


def reconcile_job(store: Store, job: str) -> dict[str, Any]:
    from .commands import CommandService
    state = _index(store, store.events())[job]
    if state["dispatch"] is None or state["finalized"] is not None:
        return job_state(store, job)
    workspace = _workspace(store, state)
    completion = workspace / "completion.json"
    if not completion.is_file():
        return job_state(store, job)
    from .runner_backend import plain_file
    require(plain_file(completion) and completion.stat().st_size <= 1024 * 1024, "unsafe or oversized completion")
    with completion.open("rb") as stream:
        data = stream.read(1024 * 1024 + 1)
    require(len(data) <= 1024 * 1024, "oversized completion")
    record = _object(data)
    require(record.get("identity") == _identity(state), "completion identity mismatch")
    if record.get("status") == "unknown":
        return job_state(store, job)
    # Hash and import bytes from only the registered basenames; no manifest-controlled traversal.
    entries = record.get("outputs")
    require(type(entries) is dict and set(entries) <= set(state["spec"]["outputs"]), "invalid completion outputs")
    for name, item in {**entries, "_stdout": record.get("stdout"), "_stderr": record.get("stderr")}.items():
        expected = {"_stdout": "stdout.bin", "_stderr": "stderr.bin"}.get(name, state["spec"]["outputs"].get(name))
        require(type(item) is dict and item.get("path") == expected, "invalid completion path")
        path = workspace / expected
        require(plain_file(path), "unsafe completion output")
        require(path.stat().st_size <= state["spec"]["max_output_bytes"], "completion output exceeds limit")
        with path.open("rb") as stream:
            content = stream.read(state["spec"]["max_output_bytes"] + 1)
        require(len(content) == item.get("bytes") and digest(content) == item.get("sha256"), "completion output hash mismatch")
        self_key = store.put(content)
        require(self_key == item["sha256"], "output changed during capture")
    manifest = store.put(data)
    _check_completion(store, state, manifest)
    CommandService(store).execute(_envelope(store, state, "execution.finalize", dict(job=job, manifest=manifest)))
    return job_state(store, job)
