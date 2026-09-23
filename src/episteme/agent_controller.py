"""One-shot local model dispatch and explicit proposal application.

An existing dispatch only permits reconciliation. The caller's account and OS
remain trusted; the worker is not an independent or authenticated scientist.
"""

from __future__ import annotations

from pathlib import Path
import stat
import subprocess
import sys
from typing import Any
from uuid import uuid4

from .agents import _completion, _identity, _index, agent_state
from .commands import CommandService
from .execution import _object, _receipt_for
from .kernel import require
from .runner_backend import plain_file
from .store import ConflictError, Store, canonical, digest


def _snapshot(store: Store, request: str) -> tuple[dict[str, Any], int]:
    history = store.events()
    states = _index(store, history)
    require(request in states, "unknown agent request")
    return states[request], len(history)


def _command(store: Store, state: dict[str, Any], revision: int,
             action: str, payload: dict[str, Any]) -> Any:
    request = state["request"]
    context = dict(_receipt_for(store, [request["id"]])["context"],
                   actor=request["payload"]["assignee"], role="planner",
                   command_id=f"agent-{uuid4().hex}", expected_revision=revision,
                   causation_id=request["id"])
    return CommandService(store).execute(dict(context=context,
        request=dict(version=1, action=action, payload=payload)))


def _workspace(store: Store, state: dict[str, Any]) -> Path:
    parent = store.root / "agent-executions"
    path = parent / state["dispatch"]["payload"]["workspace_token"]
    for directory in (parent, path):
        if directory.exists() or directory.is_symlink():
            info = directory.lstat()
            require(stat.S_ISDIR(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400),
                    "agent workspace must be a plain directory")
    return path


def work_agent(store: Store, request: str) -> dict[str, Any]:
    """Fresh intent may launch once, outside SQL; never replay a model call."""
    state, revision = _snapshot(store, request)
    if state["dispatch"] is not None:
        return reconcile_agent(store, request)
    try:
        _command(store, state, revision, "agent.dispatch", dict(request=request, workspace_token=uuid4().hex))
    except ConflictError:
        return agent_state(store, request)
    state, _ = _snapshot(store, request)
    workspace = _workspace(store, state)
    workspace.mkdir(parents=True, exist_ok=False)
    for name, key in state["spec"]["expected_inputs"].items():
        (workspace / name).write_bytes(store.read(key))
    (workspace / "spec.json").write_bytes(store.read(state["request"]["payload"]["specification"]))
    (workspace / "identity.json").write_bytes(canonical(_identity(state)))
    worker = Path(__file__).with_name("runner_backend.py")
    process = subprocess.Popen([sys.executable, str(worker), "--workspace", str(workspace)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    process.wait()
    return reconcile_agent(store, request)


def reconcile_agent(store: Store, request: str) -> dict[str, Any]:
    state, revision = _snapshot(store, request)
    if state["dispatch"] is None or state["response"] is not None:
        return agent_state(store, request)
    workspace = _workspace(store, state)
    path = workspace / "completion.json"
    if not path.is_file():
        return agent_state(store, request)
    require(plain_file(path) and path.stat().st_size <= 1024*1024, "unsafe or oversized agent completion")
    with path.open("rb") as stream:
        raw = stream.read(1024*1024+1)
    require(len(raw) <= 1024*1024, "oversized agent completion")
    record = _object(raw)
    require(record.get("identity") == _identity(state), "agent completion identity mismatch")
    if record.get("status") == "unknown":
        return agent_state(store, request)
    outputs = record.get("outputs")
    require(type(outputs) is dict and set(outputs) <= {"proposal"}, "invalid agent completion outputs")
    limit = state["spec"]["max_output_bytes"]
    for name, item in {**outputs, "stdout": record.get("stdout"), "stderr": record.get("stderr")}.items():
        expected = {"proposal": "proposal.json", "stdout": "stdout.bin", "stderr": "stderr.bin"}[name]
        require(type(item) is dict and item.get("path") == expected, "invalid agent completion path")
        artifact = workspace / expected
        require(plain_file(artifact) and artifact.stat().st_size <= limit, "unsafe or oversized agent output")
        with artifact.open("rb") as stream:
            content = stream.read(limit+1)
        require(len(content) <= limit and len(content) == item.get("bytes")
                and digest(content) == item.get("sha256"), "agent completion output hash mismatch")
        require(store.put(content) == item["sha256"], "agent output changed during capture")
    manifest = store.put(raw)
    _completion(store, state, manifest)
    try:
        _command(store, state, revision, "agent.finalize", dict(request=request, manifest=manifest))
    except ConflictError:
        pass  # Keep the original completion; a later reconcile can import it.
    return agent_state(store, request)


def advance_agent(store: Store, request: str) -> dict[str, Any]:
    """Run/reconcile, then apply a structurally valid proposal to its current question."""
    result = work_agent(store, request)
    if result["status"] != "proposed":
        return result
    state, revision = _snapshot(store, request)
    if state["application"] is not None:
        return agent_state(store, request)
    try:
        _command(store, state, revision, "agent.apply_hypotheses", dict(request=request))
    except ConflictError:
        pass
    return agent_state(store, request)
