"""Durable, finite primary/reanalysis batches; scheduling is not scientific review.

Each slot is bound once to a managed execution. The protocol is reserved while
the batch is open, including ambiguous dispatches. Costs count enqueued attempts,
not observed CPU time, money, or independent scientific observations.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

from .execution import BACKEND, CAPABILITIES, Execution, _index as execution_index, _object, _spec
from .kernel import Actor, Kernel, require
from .search import Search, _amount
from .store import Store


KINDS = {"batch_plan", "batch_slot", "batch_settlement"}
_ACTIONS = {"baseline", "discriminate", "ablate", "robustness"}
_PLAN_FIELDS = {"schema_version", "selection", "selection_hash", "node", "node_hash",
                "tree", "tree_hash", "protocol", "protocol_hash", "executor", "replicator",
                "reanalysis_implementation", "reanalysis_environment", "outputs", "wall_seconds",
                "max_output_bytes", "required_capabilities", "execution_authority", "slots",
                "cost_unit", "reserved_cost"}


def _text(value: Any) -> bool:
    return type(value) is str and bool(value.strip())


def _get(history: list[dict[str, Any]], id: str, kind: str) -> dict[str, Any]:
    return Kernel._get(history, id, kind)


def _roster(seeds: list[int], executor: str, replicator: str) -> list[dict[str, Any]]:
    return [dict(slot=f"{label}:{seed}", seed=seed,
                 mode="primary" if label == "primary" else "independent_reanalysis",
                 depends_on=None if label == "primary" else f"primary:{seed}",
                 actor=executor if label == "primary" else replicator,
                 role="executor" if label == "primary" else "replicator")
            for seed in seeds for label in ("primary", "reanalysis")]


def _recipe(store: Store, p: dict[str, Any], protocol: dict[str, Any]) -> None:
    require(type(p["required_capabilities"]) is list
            and all(_text(item) for item in p["required_capabilities"])
            and len(set(p["required_capabilities"])) == len(p["required_capabilities"])
            and set(p["required_capabilities"]) <= CAPABILITIES, "invalid batch capabilities")
    for implementation, environment in ((protocol["implementation"], protocol["environment"]),
                                        (p["reanalysis_implementation"], p["reanalysis_environment"])):
        store.read(implementation)
        declaration = _object(store.read(environment))
        require(set(declaration) == {"schema_version", "backend", "fingerprint"}
                and type(declaration["schema_version"]) is int and declaration["schema_version"] == 1
                and declaration["backend"] == BACKEND, "batch requires a frozen local Python environment")
        _spec(dict(schema_version=1, command=[sys.executable, "-I", "-S", "program.py", "input.dat", "--seed", "0"],
                   outputs=p["outputs"], wall_seconds=p["wall_seconds"], max_output_bytes=p["max_output_bytes"],
                   expected_inputs={"program.py": implementation, "input.dat": protocol["data"]},
                   environment_fingerprint=declaration["fingerprint"]))
    store.read(protocol["data"])


def _validate_plan(store: Store, history: list[dict[str, Any]], p: dict[str, Any]) -> None:
    require(set(p) == _PLAN_FIELDS and type(p["schema_version"]) is int and p["schema_version"] == 1,
            "invalid batch plan schema")
    chosen = _get(history, p["selection"], "search_selection")
    node = _get(history, p["node"], "experiment_node")
    tree = _get(history, p["tree"], "search_tree")
    protocol = _get(history, p["protocol"], "protocol")
    for name, event in (("selection", chosen), ("node", node), ("tree", tree), ("protocol", protocol)):
        require(p[f"{name}_hash"] == event["hash"], f"batch {name} hash mismatch")
    choice, proposal, plan = chosen["payload"], node["payload"], protocol["payload"]
    require(choice["node"] == node["id"] and choice["tree"] == tree["id"]
            and choice["protocol"] == protocol["id"] and proposal["tree"] == tree["id"]
            and proposal["protocol"] == protocol["id"] and proposal["protocol_hash"] == protocol["hash"],
            "batch selection, node and protocol differ")
    require(proposal["action"] in _ACTIONS and proposal["execution_mode"] == "primary"
            and proposal["scientific_evidence_eligible"] is True, "batch requires a fresh primary scientific node")
    require(not any(e["kind"] == "search_terminal" and e["payload"]["selection"] == chosen["id"]
                    for e in history), "selection already terminal")
    require(not any(e["kind"] == "batch_plan" and e["payload"]["selection"] == chosen["id"]
                    for e in history), "selection already bound to a batch")
    require(not any(e["kind"] == "run" and e["payload"]["protocol"] == protocol["id"] for e in history),
            "batch selection is stale: protocol has already executed")
    closed = {e["payload"]["selection"] for e in history if e["kind"] == "search_terminal"}
    require(not any(e["kind"] == "search_selection" and e["id"] != chosen["id"]
                    and e["payload"]["protocol"] == protocol["id"] and e["payload"]["node"] is not None
                    and e["id"] not in closed for e in history), "protocol has another active selection")
    seeds = plan["seeds"]
    require(type(seeds) is list and seeds and all(type(seed) is int for seed in seeds)
            and len(set(seeds)) == len(seeds), "invalid protocol seed schedule")
    count = 2 * len(seeds)
    require(type(plan["run_limit"]) is int and plan["run_limit"] >= count,
            "protocol lacks slots for the full primary/reanalysis batch")
    require(tree["payload"]["cost_unit"] == p["cost_unit"] == "enqueued_attempt"
            and type(p["reserved_cost"]) is int and p["reserved_cost"] == count
            and _amount(choice["reserved_cost"]) == count, "batch must reserve exactly its roster in enqueued_attempt units")
    require(_text(p["executor"]) and _text(p["replicator"]) and p["executor"] != p["replicator"],
            "batch executor and replicator must differ")
    require(p["reanalysis_implementation"] != plan["implementation"], "reanalysis needs a different implementation")
    require(p["slots"] == _roster(seeds, p["executor"], p["replicator"]), "batch roster differs from frozen seeds/actors")
    require(type(p["execution_authority"]) is str and re.fullmatch(r"[0-9a-f]{64}", p["execution_authority"]),
            "invalid batch execution authority digest")
    _recipe(store, p, plan)


def _cells(state: dict[str, Any], executions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in state["plan"]["payload"]["slots"]:
        binding = state["bindings"].get(spec["slot"])
        row = dict(spec, status="pending", binding=None, run=None, job=None, result=None)
        if binding is not None:
            p = binding["payload"]
            execution = executions[p["job"]]
            final = execution["finalized"]
            row.update(binding=binding["id"], run=p["run"], job=p["job"],
                       status="queued" if execution["dispatch"] is None else "unknown")
            if final is not None:
                row["result"] = final["payload"]["result"]
                row["status"] = execution["terminal_status"]
        elif spec["depends_on"] is not None:
            primary = next(row for row in rows if row["slot"] == spec["depends_on"])
            row["status"] = ("blocked_dependency" if primary["status"] in {"failed", "cancelled"}
                             else "pending" if primary["status"] == "completed" else "waiting_primary")
        rows.append(row)
    return rows


def _outcome(state: dict[str, Any], executions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows = _cells(state, executions)
    require(all(row["status"] in {"completed", "failed", "cancelled", "blocked_dependency"} for row in rows),
            "batch has pending, queued or unknown attempts; reservation remains open")
    status = "completed" if all(row["status"] == "completed" for row in rows) else "failed"
    return dict(status=status, slots=rows, runs=[row["run"] for row in rows if row["run"] is not None],
                results=[row["result"] for row in rows if row["result"] is not None],
                actual_cost=sum(row["job"] is not None for row in rows),
                next_action="awaiting_analysis" if status == "completed" else "repair_evidence")


def _terminal(plan: dict[str, Any], batch: str, settlement: str, outcome: dict[str, Any]) -> dict[str, Any]:
    return dict(tree=plan["tree"], selection=plan["selection"], node=plan["node"], status=outcome["status"],
                actual_cost=outcome["actual_cost"], reserved_cost=plan["reserved_cost"], cost_overrun=0,
                cost_overrun_exact="0", reason="Full batch technical settlement; no scientific review performed",
                run=None, claim=None, scientific_outcome="not_assessed", scientific_validity="not_assessed",
                batch=batch, settlement=settlement, runs=outcome["runs"], results=outcome["results"])


def _index(store: Store, history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Verify batch references at committed boundaries, never require local authority for reads."""
    if not any(e["kind"] in KINDS for e in history):
        return {}
    receipts = store.receipts()
    def receipt(events: list[dict[str, Any]], action: str) -> None:
        ids = [event["id"] for event in events]
        found = next((r for r in receipts if r["event_ids"] == ids), None)
        require(found is not None and found["request"]["action"] == action,
                "batch transition must match its original complete command receipt")

    executions = execution_index(store, history)
    by_id = {event["id"]: event for event in history}
    for execution in executions.values():
        final = execution["finalized"]
        if final is not None:
            execution["terminal_status"] = by_id[final["payload"]["result"]]["payload"]["status"]
    states: dict[str, dict[str, Any]] = {}
    for offset, event in enumerate(history):
        p, kind = event["payload"], event["kind"]
        if kind == "batch_plan":
            require(event["role"] == "planner", "batch planning requires a planner")
            _validate_plan(store, history[:offset], p)
            receipt([event], "batch.plan")
            states[event["id"]] = dict(plan=event, bindings={}, settlement=None, terminal=None)
        elif kind in {"batch_slot", "batch_settlement"}:
            require(p.get("batch") in states, "batch event requires a prior plan")
            state = states[p["batch"]]
            plan = state["plan"]
            require(type(p.get("schema_version")) is int and p["schema_version"] == 1
                    and p.get("batch_hash") == plan["hash"] and state["settlement"] is None,
                    "invalid batch binding/version or already settled")
            if kind == "batch_slot":
                require(set(p) == {"schema_version", "batch", "batch_hash", "slot", "run", "run_hash", "job", "job_hash"},
                        "invalid batch slot fields")
                spec = next((item for item in plan["payload"]["slots"] if item["slot"] == p["slot"]), None)
                require(spec is not None and p["slot"] not in state["bindings"], "unknown or already bound batch slot")
                require((event["actor"], event["role"]) == (spec["actor"], spec["role"]), "wrong assigned slot actor")
                run = _get(history[:offset], p["run"], "run")
                job = _get(history[:offset], p["job"], "execution_job")
                require(run["hash"] == p["run_hash"] and job["hash"] == p["job_hash"]
                        and run["seq"] + 1 == job["seq"] and job["seq"] + 1 == event["seq"]
                        and job["payload"]["run"] == run["id"], "batch slot must immediately bind its new run/job")
                receipt([run, job, event], "batch.enqueue_slot")
                execution = executions[job["id"]]
                r, recipe = run["payload"], execution["spec"]
                prototype = _get(history[:offset], plan["payload"]["protocol"], "protocol")["payload"]
                expected_primary = None
                if spec["depends_on"] is not None:
                    prior = state["bindings"].get(spec["depends_on"])
                    require(prior is not None, "reanalysis lacks its bound primary slot")
                    expected_primary = prior["payload"]["run"]
                    result = Kernel._result(history[:offset - 2], expected_primary)
                    require(result is not None and result["payload"]["status"] == "completed",
                            "reanalysis needs prior completed primary evidence")
                require(r["protocol"] == plan["payload"]["protocol"] and r["seed"] == spec["seed"]
                        and r["replicate_of"] == expected_primary
                        and r["implementation"] == (prototype["implementation"] if spec["mode"] == "primary"
                                                    else plan["payload"]["reanalysis_implementation"])
                        and r["environment"] == (prototype["environment"] if spec["mode"] == "primary"
                                                 else plan["payload"]["reanalysis_environment"]),
                        "batch slot run differs from its frozen recipe")
                require(all(recipe[key] == plan["payload"][key] for key in ("outputs", "wall_seconds", "max_output_bytes"))
                        and job["payload"]["capabilities"] == plan["payload"]["required_capabilities"],
                        "batch execution specification differs from its plan")
                state["bindings"][p["slot"]] = event
            else:
                require(set(p) == {"schema_version", "batch", "batch_hash", "selection", "status", "slots", "runs",
                                    "results", "actual_cost", "cost_unit", "next_action", "scientific_validity"},
                        "invalid batch settlement fields")
                require((event["actor"], event["role"]) == (plan["actor"], "planner"), "only batch planner may settle")
                # Future results must not validate an earlier settlement.
                prior_executions = execution_index(store, history[:offset])
                for execution in prior_executions.values():
                    final = execution["finalized"]
                    if final is not None:
                        execution["terminal_status"] = by_id[final["payload"]["result"]]["payload"]["status"]
                outcome = _outcome(state, prior_executions)
                require(all(p[key] == value for key, value in outcome.items())
                        and p["selection"] == plan["payload"]["selection"] and p["cost_unit"] == "enqueued_attempt"
                        and p["scientific_validity"] == "not_assessed", "batch settlement differs from verified outcomes")
                require(offset + 1 < len(history), "batch settlement lacks its atomic search terminal")
                terminal = history[offset + 1]
                require(terminal["kind"] == "search_terminal" and terminal["seq"] == event["seq"] + 1
                        and terminal["payload"] == _terminal(plan["payload"], plan["id"], event["id"], outcome),
                        "search terminal differs from full batch settlement")
                receipt([event, terminal], "batch.settle")
                state.update(settlement=event, terminal=terminal)
        elif kind == "search_terminal":
            for state in states.values():
                if p["selection"] == state["plan"]["payload"]["selection"]:
                    require(state["terminal"] is not None and state["terminal"]["id"] == event["id"],
                            "batch selection cannot be settled through a single-run terminal")
    for state in states.values():
        plan = state["plan"]
        end = state["settlement"]["seq"] if state["settlement"] else float("inf")
        owned = {binding["payload"]["run"] for binding in state["bindings"].values()}
        require(all(e["id"] in owned for e in history if e["kind"] == "run"
                    and e["payload"]["protocol"] == plan["payload"]["protocol"] and plan["seq"] < e["seq"] < end),
                "unrelated run entered an active batch protocol")
        state["slots"] = _cells(state, executions)
    return states


def validate_start(store: Store, history: list[dict[str, Any]], protocol: str,
                   batch_slot: tuple[str, str] | None) -> None:
    """Admission fence without receipt reads of events in the current transaction."""
    settled = {e["payload"]["batch"] for e in history if e["kind"] == "batch_settlement"}
    active = [e for e in history if e["kind"] == "batch_plan"
              and e["payload"]["protocol"] == protocol and e["id"] not in settled]
    require(len(active) <= 1, "protocol has multiple active batches")
    if not active:
        require(batch_slot is None, "batch slot requires its active protocol reservation")
        return
    plan = active[0]
    require(type(batch_slot) is tuple and len(batch_slot) == 2 and batch_slot[0] == plan["id"],
            "protocol reserved by an active batch; unrelated starts are prohibited")
    require(any(item["slot"] == batch_slot[1] for item in plan["payload"]["slots"])
            and not any(e["kind"] == "batch_slot" and e["payload"]["batch"] == plan["id"]
                        and e["payload"]["slot"] == batch_slot[1] for e in history), "batch slot already bound or unknown")


def batch_state(store: Store, batch: str) -> dict[str, Any]:
    history = store.events()
    states = _index(store, history)
    require(batch in states, "unknown batch")
    return _summary(batch, states[batch], len(history))


def batch_summaries(store: Store, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render the supplied immutable snapshot without rereading event state."""
    return [_summary(id, state, len(history)) for id, state in _index(store, history).items()]


def _summary(batch: str, state: dict[str, Any], revision: int) -> dict[str, Any]:
    plan, rows = state["plan"]["payload"], state["slots"]
    settled = state["settlement"]
    status = (settled["payload"]["status"] if settled else "unknown" if any(row["status"] == "unknown" for row in rows)
              else "active" if state["bindings"] else "planned")
    return dict(batch=batch, revision=revision, plan=plan, selection=plan["selection"], protocol=plan["protocol"], status=status,
                settlement=settled["id"] if settled else None,
                terminal=state["terminal"]["id"] if state["terminal"] else None, slots=rows,
                enqueued_attempts=len(state["bindings"]), reserved_cost=plan["reserved_cost"], cost_unit="enqueued_attempt",
                next_action=settled["payload"]["next_action"] if settled else "reconcile" if status == "unknown" else "execution",
                scientific_validity="not_assessed")


def validate_dispatch(store: Store, history: list[dict[str, Any]], job: str) -> None:
    """The persisted launch boundary must enforce batch ownership too."""
    from .execution_authority import require_authority
    states = _index(store, history)
    for state in states.values():
        if any(binding["payload"]["job"] == job for binding in state["bindings"].values()):
            require_authority(store, state["plan"]["payload"]["execution_authority"])
            return


def batch_context(store: Store, history: list[dict[str, Any]], run_ids: set[str]) -> list[dict[str, Any]]:
    states = _index(store, history)
    batches = {id for id, state in states.items()
               if any(binding["payload"]["run"] in run_ids for binding in state["bindings"].values())}
    context = {states[id]["plan"]["payload"][field] for id in batches
               for field in ("selection", "node", "tree")}
    context.update(event["id"] for event in history if event["kind"] in KINDS
                   and (event["id"] in batches if event["kind"] == "batch_plan"
                        else event["payload"]["batch"] in batches))
    return [event for event in history if event["id"] in context]


def batch_artifacts(store: Store, event: dict[str, Any]) -> set[str]:
    p = event["payload"]
    return {p["reanalysis_implementation"], p["reanalysis_environment"]} if event["kind"] == "batch_plan" else set()


class Batch:
    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def _history(self) -> list[dict[str, Any]]:
        require(self.store._command_context is not None, "batch transitions require CommandService transactions")
        return self.store.events()

    def _write(self, kind: str, payload: dict[str, Any], role: str) -> str:
        return Kernel(self.store, self.actor)._write(self.store.events(), kind, payload, {role})

    def plan(self, *, selection: str, executor: str, replicator: str, reanalysis_implementation: str,
             reanalysis_environment: str, outputs: dict[str, str], wall_seconds: int, max_output_bytes: int,
             required_capabilities: list[str] | None = None) -> str:
        from .execution_authority import establish_authority
        history = self._history()
        require(self.actor.role == "planner", "batch plan requires planner")
        _index(self.store, history)
        choice = _get(history, selection, "search_selection")
        require(choice["payload"]["node"] is not None, "cannot batch a wait/stop selection")
        node = _get(history, choice["payload"]["node"], "experiment_node")
        tree = _get(history, choice["payload"]["tree"], "search_tree")
        protocol = _get(history, choice["payload"]["protocol"], "protocol")
        caps = [] if required_capabilities is None else required_capabilities
        available = CAPABILITIES - {"process_group_timeout", "job_object_timeout"}
        available |= {"job_object_timeout"} if os.name == "nt" else {"process_group_timeout"} if os.name == "posix" else set()
        require(type(caps) is list and all(type(item) is str for item in caps) and set(caps) <= available,
                "required batch capability unavailable on this platform")
        payload = dict(schema_version=1, selection=selection, selection_hash=choice["hash"],
                       node=node["id"], node_hash=node["hash"], tree=tree["id"], tree_hash=tree["hash"],
                       protocol=protocol["id"], protocol_hash=protocol["hash"], executor=executor, replicator=replicator,
                       reanalysis_implementation=reanalysis_implementation, reanalysis_environment=reanalysis_environment,
                       outputs=outputs, wall_seconds=wall_seconds, max_output_bytes=max_output_bytes,
                       required_capabilities=caps, execution_authority=establish_authority(self.store),
                       slots=_roster(protocol["payload"]["seeds"], executor, replicator),
                       cost_unit="enqueued_attempt", reserved_cost=2 * len(protocol["payload"]["seeds"]))
        _validate_plan(self.store, history, payload)
        return self._write("batch_plan", payload, "planner")

    def enqueue_slot(self, *, batch: str, slot: str) -> str:
        from .execution_authority import require_authority
        history = self._history()
        states = _index(self.store, history)
        require(batch in states, "unknown batch")
        state = states[batch]
        plan = state["plan"]
        p = plan["payload"]
        require_authority(self.store, p["execution_authority"])
        require(state["settlement"] is None, "batch already settled")
        spec = next((item for item in p["slots"] if item["slot"] == slot), None)
        require(spec is not None and slot not in state["bindings"], "unknown or already bound batch slot")
        require((self.actor.id, self.actor.role) == (spec["actor"], spec["role"]), "wrong assigned slot actor")
        original = None
        if spec["depends_on"] is not None:
            primary = state["bindings"].get(spec["depends_on"])
            require(primary is not None, "reanalysis waits for its primary")
            original = primary["payload"]["run"]
            result = Kernel._result(history, original)
            require(result is not None and result["payload"]["status"] == "completed", "reanalysis requires completed primary")
        job = Execution(self.store, self.actor)._enqueue(protocol=p["protocol"], seed=spec["seed"],
            outputs=p["outputs"], wall_seconds=p["wall_seconds"], max_output_bytes=p["max_output_bytes"],
            implementation=p["reanalysis_implementation"] if original else None,
            environment=p["reanalysis_environment"] if original else None,
            replicate_of=original, required_capabilities=p["required_capabilities"], batch_slot=(batch, slot))
        # The run/job receipt does not exist yet: inspect only their raw references.
        job_event = _get(self.store.events(), job, "execution_job")
        run = _get(self.store.events(), job_event["payload"]["run"], "run")
        return self._write("batch_slot", dict(schema_version=1, batch=batch, batch_hash=plan["hash"],
            slot=slot, run=run["id"], run_hash=run["hash"], job=job, job_hash=job_event["hash"]), spec["role"])

    def settle(self, *, batch: str) -> str:
        from .execution_authority import require_authority
        history = self._history()
        states = _index(self.store, history)
        require(batch in states, "unknown batch")
        state = states[batch]
        plan, p = state["plan"], state["plan"]["payload"]
        require_authority(self.store, p["execution_authority"])
        require((self.actor.id, self.actor.role) == (plan["actor"], "planner"), "only batch planner may settle")
        require(state["settlement"] is None, "batch already settled")
        executions = execution_index(self.store, history)
        for execution in executions.values():
            final = execution["finalized"]
            if final is not None:
                execution["terminal_status"] = _get(history, final["payload"]["result"], "result")["payload"]["status"]
        outcome = _outcome(state, executions)
        settlement = self._write("batch_settlement", dict(schema_version=1, batch=batch, batch_hash=plan["hash"],
            selection=p["selection"], cost_unit="enqueued_attempt", scientific_validity="not_assessed", **outcome), "planner")
        return self._write("search_terminal", _terminal(p, batch, settlement, outcome), "planner")
