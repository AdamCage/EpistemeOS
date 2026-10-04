"""Same-code replay of a managed run in a fresh directory (ADR 0019).

``episteme reproduce`` re-materializes the frozen program, input and, for
profile v2, the environment closure of one finalized execution job outside
the store, runs the same worker again and compares the declared outputs byte
for byte. Same code, same data, same closure: this is never a run, a result or
independent evidence. Every attempt is recorded, mismatches and attempts
without a verified completion included, and enters the claim evidence basis.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from uuid import uuid4

from .kernel import Actor, Kernel, require
from .store import Store, canonical


DISPATCH, FINAL = "execution_reproduction_dispatch", "execution_reproduction"
KINDS = {DISPATCH, FINAL}
ROLES = {"executor", "replicator"}
REPLAY = {1: "same_code_same_interpreter", 2: "same_code_fresh_environment"}
MEANING = ("same-code replay of a recorded run; not independent replication, not a run, "
           "not evidence for a claim")
_DISPATCH_FIELDS = {"schema_version", "run", "run_hash", "job", "job_hash", "finalized", "finalized_hash",
                    "result", "result_hash", "profile", "replay", "workspace_token", "workspace_root",
                    "counts_as_evidence"}
_FINAL_FIELDS = {"schema_version", "dispatch", "dispatch_hash", "run", "job", "manifest", "status", "reason",
                 "outputs", "comparison", "verdict", "replay", "counts_as_evidence", "replication_mode",
                 "scientific_validity"}


def _profile(state: dict[str, Any]) -> str:
    from .execution import BACKEND
    from .execution_locked import PROFILE
    return PROFILE if state["spec"]["schema_version"] == 2 else BACKEND


def _replayed(state: dict[str, Any], dispatch: dict[str, Any]) -> dict[str, Any]:
    """The job state as seen by its reproduction: same spec, reproduction dispatch."""
    return dict(state, dispatch=dispatch)


def _environment_view(store: Store, outputs: dict[str, str], record: dict[str, Any]) -> dict[str, Any]:
    if record.get("schema_version") != 2:
        return dict(runtime=record.get("runtime"))
    if "environment_record" not in outputs:
        return {}
    from .execution import _object
    value = _object(store.read(outputs["environment_record"]))
    return dict(distributions_sha256=value["distributions_sha256"],
                portable_inventory_sha256=value["inventory"]["portable_sha256"],
                interpreter_sha256=value["interpreter"]["base_executable_sha256"],
                interpreter_version=value["interpreter"]["version"],
                platform=dict(system=value["platform"]["system"], machine=value["platform"]["machine"]),
                installer_version=value["installer"]["version"])


def assess(store: Store, history: list[dict[str, Any]], state: dict[str, Any], dispatch: dict[str, Any],
           manifest: str) -> dict[str, Any]:
    """The deterministic finalization payload of one reproduction completion."""
    from .execution import _check_completion, _object
    status, outputs, reason = _check_completion(store, _replayed(state, dispatch), manifest)
    original = Kernel._get(history, state["finalized"]["payload"]["result"], "result")["payload"]
    labels = sorted(state["spec"]["outputs"])
    compared = {label: dict(original=original["outputs"].get(label), reproduced=outputs.get(label),
                            equal=original["outputs"].get(label) is not None
                            and original["outputs"].get(label) == outputs.get(label))
                for label in labels}
    reasons = []
    if status != original["status"]:
        reasons.append(f"terminal status differs: original {original['status']}, reproduced {status}"
                       + (f" ({reason})" if reason else ""))
    reasons.extend(f"declared output {label} differs" for label, row in compared.items() if not row["equal"])
    before = _object(store.read(original["outputs"]["execution_manifest"]))
    after = _object(store.read(manifest))
    environments = dict(original=_environment_view(store, original["outputs"], before),
                        reproduced=_environment_view(store, outputs, after))
    shared = sorted(set(environments["original"]) & set(environments["reproduced"]))
    logs = {name: before[name]["sha256"] == after[name]["sha256"] for name in ("stdout", "stderr")}
    comparison = dict(status=dict(original=original["status"], reproduced=status,
                                  equal=status == original["status"]),
                      outputs=compared, logs_equal=logs, environment=dict(
                          environments, equal={field: environments["original"][field]
                                               == environments["reproduced"][field] for field in shared}),
                      mismatch_reasons=reasons,
                      verdict_rule="matched: same terminal status and byte-identical declared outputs; "
                                   "logs and environment are reported, not judged")
    p = dispatch["payload"]
    return dict(schema_version=1, dispatch=dispatch["id"], dispatch_hash=dispatch["hash"], run=p["run"],
                job=p["job"], manifest=manifest, status=status, reason=reason, outputs=outputs,
                comparison=comparison, verdict="mismatched" if reasons else "matched", replay=p["replay"],
                counts_as_evidence=False, replication_mode="none", scientific_validity="not_assessed")


def validate(store: Store, history: list[dict[str, Any]], jobs: dict[str, dict[str, Any]]) -> None:
    """Replay reproduction events on their prefixes and attach them to their jobs."""
    from .execution import _receipt_for
    by_run = {state["run"]["id"]: state for state in jobs.values()}
    attempts: dict[str, dict[str, Any]] = {}
    for event in history:
        kind, p = event["kind"], event["payload"]
        if kind not in KINDS:
            continue
        require(event["role"] in ROLES, "reproduction requires an execution role")
        receipt = _receipt_for(store, [event["id"]])
        require(receipt["event_ids"] == [event["id"]]
                and receipt["request"]["action"] == ("reproduction.dispatch" if kind == DISPATCH
                                                     else "reproduction.finalize"),
                "reproduction event must match its own command receipt")
        if kind == DISPATCH:
            require(set(p) == _DISPATCH_FIELDS and p["schema_version"] == 1, "invalid reproduction dispatch fields")
            state = by_run.get(p["run"])
            require(state is not None and state["job"]["id"] == p["job"], "reproduction needs a managed run")
            final = state["finalized"]
            require(final is not None and final["seq"] < event["seq"],
                    "only a finalized managed run can be reproduced")
            result = Kernel._get(history, final["payload"]["result"], "result")
            require((p["run_hash"], p["job_hash"], p["finalized"], p["finalized_hash"], p["result"], p["result_hash"])
                    == (state["run"]["hash"], state["job"]["hash"], final["id"], final["hash"], result["id"],
                        result["hash"]), "reproduction dispatch differs from its recorded run")
            require(p["profile"] == _profile(state) and p["replay"] == REPLAY[state["spec"]["schema_version"]]
                    and p["counts_as_evidence"] is False
                    and type(p["workspace_token"]) is str and re.fullmatch(r"[0-9a-f]{32}", p["workspace_token"])
                    and type(p["workspace_root"]) is str and 0 < len(p["workspace_root"]) <= 1024
                    and "\x00" not in p["workspace_root"], "invalid reproduction dispatch labels")
            attempts[event["id"]] = dict(dispatch=event, job=state, finalized=None)
            state.setdefault("reproductions", {})[event["id"]] = attempts[event["id"]]
        else:
            require(set(p) == _FINAL_FIELDS and p["schema_version"] == 1, "invalid reproduction fields")
            attempt = attempts.get(p["dispatch"])
            require(attempt is not None and attempt["finalized"] is None
                    and p["dispatch_hash"] == attempt["dispatch"]["hash"],
                    "reproduction finalization needs its own unfinished dispatch")
            dispatch = attempt["dispatch"]
            require((event["actor"], event["role"]) == (dispatch["actor"], dispatch["role"]),
                    "reproduction actor differs from its dispatch")
            require(p == assess(store, history, attempt["job"], dispatch, p["manifest"]),
                    "reproduction comparison differs from its verified completion")
            attempt["finalized"] = event


def context(state: dict[str, Any]) -> set[str]:
    return {event["id"] for attempt in state.get("reproductions", {}).values()
            for event in (attempt["dispatch"], attempt["finalized"]) if event is not None}


def artifacts(store: Store, event: dict[str, Any]) -> set[str]:
    if event["kind"] != FINAL:
        return set()
    from .execution import _object
    p = event["payload"]
    record = _object(store.read(p["manifest"]))
    keys = {p["manifest"], record["stdout"]["sha256"], record["stderr"]["sha256"], *p["outputs"].values()}
    if record.get("schema_version") == 2:
        from .execution_locked import completion_artifacts
        keys |= completion_artifacts(record)
    return keys


class Reproduction:
    """Executor or replicator commands; the role label does not prove independence."""

    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def _state(self, run: str) -> dict[str, Any]:
        from .execution import _index
        require(self.store._command_context is not None, "reproduction requires CommandService transactions")
        jobs = _index(self.store, self.store.events())
        state = next((item for item in jobs.values() if item["run"]["id"] == run), None)
        require(state is not None, "caller-recorded runs have no frozen execution specification to reproduce")
        require(state["finalized"] is not None, "only a finalized managed run can be reproduced")
        return state

    def dispatch(self, *, run: str, workspace_token: str, workspace_root: str) -> str:
        from .execution_locked import check_root
        require(self.actor.role in ROLES, "reproduction requires an executor or replicator")
        state = self._state(run)
        require(type(workspace_token) is str and re.fullmatch(r"[0-9a-f]{32}", workspace_token),
                "invalid workspace token")
        root = check_root(self.store, workspace_root)
        version = state["spec"]["schema_version"]
        if version == 1:
            from .runner_backend import fingerprint
            require(state["spec"]["command"][0] == sys.executable
                    and fingerprint() == state["spec"]["environment_fingerprint"],
                    "a v1 environment is not re-materializable; this interpreter differs from the recorded one")
        final = state["finalized"]
        result = Kernel._get(self.store.events(), final["payload"]["result"], "result")
        payload = dict(schema_version=1, run=run, run_hash=state["run"]["hash"], job=state["job"]["id"],
                       job_hash=state["job"]["hash"], finalized=final["id"], finalized_hash=final["hash"],
                       result=result["id"], result_hash=result["hash"], profile=_profile(state),
                       replay=REPLAY[version], workspace_token=workspace_token, workspace_root=str(root),
                       counts_as_evidence=False)
        return Kernel(self.store, self.actor)._write(self.store.events(), DISPATCH, payload, ROLES)

    def finalize(self, *, dispatch: str, manifest: str) -> str:
        from .execution import _index
        require(self.store._command_context is not None, "reproduction requires CommandService transactions")
        history = self.store.events()
        attempt = _attempt(_index(self.store, history), dispatch)
        require(attempt["finalized"] is None, "reproduction already finalized")
        event = attempt["dispatch"]
        require((self.actor.id, self.actor.role) == (event["actor"], event["role"]),
                "only the reproduction actor may finalize it")
        payload = assess(self.store, history, attempt["job"], event, manifest)
        return Kernel(self.store, self.actor)._write(self.store.events(), FINAL, payload, ROLES)


def _attempt(jobs: dict[str, dict[str, Any]], dispatch: str) -> dict[str, Any]:
    attempt = next((item for state in jobs.values() for id, item in state.get("reproductions", {}).items()
                    if id == dispatch), None)
    require(attempt is not None, f"unknown reproduction dispatch: {dispatch}")
    return attempt


def _summary(attempt: dict[str, Any]) -> dict[str, Any]:
    dispatch, final = attempt["dispatch"], attempt["finalized"]
    p = final["payload"] if final is not None else {}
    return dict(run=dispatch["payload"]["run"], job=dispatch["payload"]["job"], dispatch=dispatch["id"],
                reproduction=final["id"] if final else None,
                status=p.get("verdict", "unknown"), reproduced_status=p.get("status"),
                comparison=p.get("comparison"), replay=dispatch["payload"]["replay"],
                counts_as_evidence=False, scientific_validity="not_assessed", meaning=MEANING,
                unknown_meaning="dispatch exists; no verified completion; a new reproduce is a new attempt")


def reproduction_state(store: Store, dispatch: str) -> dict[str, Any]:
    from .execution import _index
    return _summary(_attempt(_index(store, store.events()), dispatch))


def _envelope(store: Store, state: dict[str, Any], action: str, payload: dict[str, Any], actor: str,
              role: str, cause: str) -> dict[str, Any]:
    from .execution import _receipt_for
    receipt = _receipt_for(store, [state["job"]["id"]])
    context = dict(receipt["context"], command_id=f"reproduction-{uuid4().hex}", actor=actor, role=role,
                   expected_revision=len(store.events()), causation_id=cause)
    return dict(context=context, request=dict(version=1, action=action, payload=payload))


def _directory(store: Store, dispatch: dict[str, Any]) -> Path:
    from .execution_locked import job_directory
    return job_directory(store, dispatch)


def reproduce(store: Store, run: str, *, actor: str | None = None, role: str | None = None) -> dict[str, Any]:
    """Record an intent, re-materialize from CAS in a new directory, run once, compare."""
    from .commands import CommandService
    from .execution import _identity, _index
    from . import execution_locked as locked
    state = next((item for item in _index(store, store.events()).values() if item["run"]["id"] == run), None)
    require(state is not None, "caller-recorded runs have no frozen execution specification to reproduce")
    require(state["finalized"] is not None, "only a finalized managed run can be reproduced")
    actor, role = actor or state["job"]["actor"], role or state["job"]["role"]
    root = locked.default_root()
    dispatch_id = CommandService(store).execute(_envelope(
        store, state, "reproduction.dispatch", dict(run=run, workspace_token=uuid4().hex,
                                                    workspace_root=str(root)),
        actor, role, state["finalized"]["id"]))
    attempt = _attempt(_index(store, store.events()), dispatch_id)
    dispatch = attempt["dispatch"]
    path = _directory(store, dispatch)
    identity = _identity(_replayed(state, dispatch))
    specification = state["job"]["payload"]["specification"]
    if state["spec"]["schema_version"] == 2:
        locked.materialize(store, state["spec"], specification, identity, path)
        locked.spawn(path)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir(mode=0o700, exist_ok=False)
        for name, key in state["spec"]["expected_inputs"].items():
            (path / name).write_bytes(store.read(key))
        (path / "spec.json").write_bytes(store.read(specification))
        (path / "identity.json").write_bytes(canonical(identity))
        worker = Path(__file__).with_name("runner_backend.py")
        subprocess.Popen([sys.executable, str(worker), "--workspace", str(path)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).wait()
    return reconcile_reproduction(store, dispatch_id)


def reconcile_reproduction(store: Store, dispatch_id: str) -> dict[str, Any]:
    """Finalize a reproduction from its completion; never relaunches."""
    from .commands import CommandService
    from .execution import _import_completion, _index
    from . import execution_locked as locked
    attempt = _attempt(_index(store, store.events()), dispatch_id)
    if attempt["finalized"] is not None:
        return _summary(attempt)
    state, dispatch = attempt["job"], attempt["dispatch"]
    path = _directory(store, dispatch)
    replayed = _replayed(state, dispatch)
    if state["spec"]["schema_version"] == 2:
        from .execution import _identity
        manifest = locked.import_completion(store, state["spec"], _identity(replayed), path)
    else:
        manifest = _import_completion(store, replayed, path)
    if manifest is None:
        return _summary(attempt)
    CommandService(store).execute(_envelope(store, state, "reproduction.finalize",
                                            dict(dispatch=dispatch_id, manifest=manifest),
                                            dispatch["actor"], dispatch["role"], dispatch_id))
    locked.cleanup(path)
    return _summary(_attempt(_index(store, store.events()), dispatch_id))


def add_arguments(subcommands: Any) -> None:
    parser = subcommands.add_parser("reproduce", help="Same-code replay of a managed run in a fresh "
                                                      "environment; recorded, never evidence")
    parser.add_argument("target", help="Recorded run ID, or a reproduction dispatch ID with --reconcile")
    parser.add_argument("--reconcile", action="store_true",
                        help="Finalize an existing reproduction attempt from its completion; no launch")
    parser.add_argument("--actor", default=None, help="Caller-declared actor (default: the run's executor)")
    parser.add_argument("--role", choices=sorted(ROLES), default=None)
    parser.add_argument("--root", type=Path, required=True)


def run_cli(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    require((args.root / "state.sqlite3").is_file(), "existing research state is required")
    with Store(args.root) as store:
        if args.reconcile:
            result = reconcile_reproduction(store, args.target)
        else:
            result = reproduce(store, args.target, actor=args.actor, role=args.role)
    return result, 0 if result["status"] == "matched" else 1
