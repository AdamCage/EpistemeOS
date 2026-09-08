"""Validated research commands; agents propose, this kernel admits transitions.

Actor identities are supplied by a trusted caller in this prototype. They are
separation-of-duty checks, not authentication or proof of independent reasoning.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .store import IntegrityError, Store, canonical, digest


class GateError(ValueError):
    """The requested transition lacks its required evidence."""


@dataclass(frozen=True)
class Actor:
    id: str
    role: str


def require(condition: bool, message: str) -> None:
    if not condition:
        raise GateError(message)


def finite(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


class Kernel:
    def __init__(self, store: Store, actor: Actor):
        require(isinstance(actor.id, str) and bool(actor.id.strip()), "actor id is required")
        require(isinstance(actor.role, str) and bool(actor.role.strip()), "actor role is required")
        self.store = store
        self.actor = actor

    def _history(self) -> list[dict[str, Any]]:
        return self.store.events()

    @staticmethod
    def _get(history: list[dict[str, Any]], id: str, kind: str) -> dict[str, Any]:
        matches = [e for e in history if e["id"] == id and e["kind"] == kind]
        require(len(matches) == 1, f"unknown {kind}: {id}")
        return matches[0]

    def _write(self, history: list[dict[str, Any]], kind: str, payload: dict[str, Any],
               roles: set[str]) -> str:
        require(self.actor.role in roles, f"{self.actor.role} cannot create {kind}")
        id = f"{kind}-{uuid4().hex[:16]}"
        self.store.append(id=id, kind=kind, actor=self.actor.id, role=self.actor.role,
                          payload=payload, expected_revision=len(history))
        return id

    def hypothesis(self, statement: str, prediction: str, falsifier: str,
                   scope: dict[str, str]) -> str:
        require(all(isinstance(s, str) and s.strip() for s in
                    (statement, prediction, falsifier)), "hypothesis needs prediction and falsifier")
        self._scope(scope)
        return self._write(self._history(), "hypothesis", dict(statement=statement,
                           prediction=prediction, falsifier=falsifier, scope=scope), {"planner"})

    @staticmethod
    def _scope(scope: dict[str, str]) -> None:
        require(isinstance(scope, dict) and bool(scope) and all(
            isinstance(k, str) and bool(k.strip()) and isinstance(v, str) and bool(v.strip())
            for k, v in scope.items()), "scope must be a nonempty string mapping")

    def preregister(self, *, hypotheses: list[str], scope: dict[str, str], design: str,
                    metric: str, analysis_plan: str, stopping_rule: str, seeds: list[int],
                    run_limit: int, implementation: str, environment: str, data: str,
                    replication_tolerance: float, parent: str | None = None) -> str:
        history = self._history()
        self._scope(scope)
        require(len(set(hypotheses)) >= 2 and len(set(hypotheses)) == len(hypotheses),
                "at least two distinct competing hypotheses required")
        for id in hypotheses:
            hypothesis = self._get(history, id, "hypothesis")
            require(hypothesis["payload"]["scope"] == scope, "hypothesis scope mismatch")
        require(all(isinstance(s, str) and s.strip() for s in
                    (design, metric, analysis_plan, stopping_rule)), "incomplete protocol")
        require(bool(seeds) and all(type(s) is int for s in seeds)
                and len(set(seeds)) == len(seeds), "seeds must be unique integers")
        require(type(run_limit) is int and run_limit >= 2 * len(seeds),
                "run limit must cover primary runs and reanalyses")
        require(finite(replication_tolerance) and replication_tolerance >= 0,
                "replication tolerance must be finite and nonnegative")
        for key in (implementation, environment, data):
            self.store.read(key)
        if parent:
            self._get(history, parent, "protocol")
        # Each amendment is a NEW protocol ID. The parent remains immutable.
        payload = dict(hypotheses=hypotheses, scope=scope, design=design, metric=metric,
                       analysis_plan=analysis_plan, stopping_rule=stopping_rule,
                       seeds=seeds, run_limit=run_limit, implementation=implementation,
                       environment=environment, data=data,
                       replication_tolerance=replication_tolerance, parent=parent)
        return self._write(history, "protocol", payload, {"planner"})

    def start_run(self, protocol: str, *, seed: int, implementation: str,
                  environment: str, command: list[str], replicate_of: str | None = None) -> str:
        history = self._history()
        plan = self._get(history, protocol, "protocol")
        p = plan["payload"]
        require(type(seed) is int and seed in p["seeds"], "seed not preregistered")
        require(isinstance(command, list) and bool(command) and all(
            isinstance(s, str) and s.strip() for s in command), "command must be argv")
        for key in (implementation, environment, p["data"]):
            self.store.read(key)
        runs = [e for e in history if e["kind"] == "run" and e["payload"]["protocol"] == protocol]
        require(len(runs) < p["run_limit"], "protocol run budget exhausted")
        if replicate_of:
            original = self._get(history, replicate_of, "run")
            require(original["payload"]["replicate_of"] is None, "cannot replicate a reanalysis")
            require(original["payload"]["protocol"] == protocol
                    and original["payload"]["seed"] == seed, "replication protocol/seed mismatch")
            require(original["actor"] != self.actor.id, "executor cannot replicate itself")
            require(implementation != original["payload"]["implementation"],
                    "independent reanalysis requires a different implementation artifact")
            result = self._result(history, replicate_of)
            require(result is not None and result["payload"]["status"] == "completed",
                    "replication needs a completed original run")
            roles = {"replicator"}
        else:
            require(implementation == p["implementation"] and environment == p["environment"],
                    "primary implementation/environment differs from frozen protocol")
            roles = {"executor"}
        return self._write(history, "run", dict(protocol=protocol, protocol_hash=plan["hash"],
                           seed=seed, implementation=implementation, environment=environment,
                           command=command, replicate_of=replicate_of), roles)

    @staticmethod
    def _result(history: list[dict[str, Any]], run: str) -> dict[str, Any] | None:
        return next((e for e in history if e["kind"] == "result"
                     and e["payload"]["run"] == run), None)

    def finish_run(self, run: str, *, status: str, outputs: dict[str, str],
                   reason: str = "") -> str:
        history = self._history()
        started = self._get(history, run, "run")
        require(started["actor"] == self.actor.id and started["role"] == self.actor.role,
                "only the assigned run actor can record its terminal result")
        require(self._result(history, run) is None, "run already terminal")
        require(status in {"completed", "failed", "cancelled"}, "invalid run status")
        require(isinstance(outputs, dict), "outputs must be a mapping")
        for key in outputs.values():
            self.store.read(key)
        if status == "completed":
            require({"raw_data", "metrics", "log"} <= outputs.keys(),
                    "completed run needs raw_data, metrics and log")
            p = self._get(history, started["payload"]["protocol"], "protocol")["payload"]
            self._metric(outputs["metrics"], p["metric"])
        else:
            require(bool(reason.strip()), "failure/cancellation must be explained")
        return self._write(history, "result", dict(run=run, status=status, outputs=outputs,
                           reason=reason), {"executor", "replicator"})

    def _metric(self, artifact: str, metric: str) -> float:
        try:
            metrics = json.loads(self.store.read(artifact))
        except (ValueError, UnicodeError) as exc:
            raise GateError("invalid metrics artifact") from exc
        require(isinstance(metrics, dict) and finite(metrics.get(metric)),
                f"missing or non-finite primary metric: {metric}")
        return float(metrics[metric])

    def claim(self, *, protocol: str, statement: str, scope: dict[str, str],
              evidence: list[str], limitations: list[str], outcome: str) -> str:
        history = self._history()
        plan = self._get(history, protocol, "protocol")["payload"]
        require(scope == plan["scope"], "claim scope exceeds/differs from protocol scope")
        require(bool(statement.strip()) and bool(limitations) and all(
            isinstance(s, str) and s.strip() for s in limitations), "claim needs limitations")
        require(outcome in {"supports", "refutes", "inconclusive"}, "invalid claim outcome")
        require(bool(evidence) and len(set(evidence)) == len(evidence), "claim needs unique run IDs")
        for id in evidence:
            run = self._get(history, id, "run")
            require(run["payload"]["protocol"] == protocol, "cross-protocol evidence prohibited")
            result = self._result(history, id)
            require(result is not None and result["payload"]["status"] == "completed",
                    "claim evidence must refer to completed runs")
        return self._write(history, "claim", dict(protocol=protocol, statement=statement,
                           scope=scope, evidence=evidence, limitations=limitations,
                           outcome=outcome), {"analyst", "executor"})

    def _basis(self, history: list[dict[str, Any]], claim: str) -> tuple[str, list[dict[str, Any]]]:
        c = self._get(history, claim, "claim")
        protocol = c["payload"]["protocol"]
        plan = self._get(history, protocol, "protocol")
        runs = [e for e in history if e["kind"] == "run" and e["payload"]["protocol"] == protocol]
        ids = {e["id"] for e in runs}
        results = [e for e in history if e["kind"] == "result" and e["payload"]["run"] in ids]
        basis = [plan, c, *runs, *results]
        return digest(canonical(basis)), runs

    def gate(self, claim: str) -> dict[str, Any]:
        """Fail closed on absent/changed evidence; never certifies scientific truth."""
        return self._gate(self._history(), claim)

    def _gate(self, history: list[dict[str, Any]], claim: str) -> dict[str, Any]:
        c = self._get(history, claim, "claim")["payload"]
        plan = self._get(history, c["protocol"], "protocol")
        p = plan["payload"]
        basis, runs = self._basis(history, claim)
        failures: list[str] = []
        primary: list[dict[str, Any]] = []
        completed: set[str] = set()
        successful_replicas: set[str] = set()
        for key in (p["implementation"], p["environment"], p["data"]):
            try:
                self.store.read(key)
            except IntegrityError as exc:
                failures.append(str(exc))
        for run in runs:
            r = run["payload"]
            if not r["replicate_of"]:
                primary.append(run)
            try:
                require(run["seq"] > plan["seq"] and r["protocol_hash"] == plan["hash"],
                        "preregistration order/hash mismatch")
                self.store.read(r["implementation"])
                self.store.read(r["environment"])
                result = self._result(history, run["id"])
                require(result is not None, f"unterminated run: {run['id']}")
                outputs = result["payload"]["outputs"]
                for key in outputs.values():
                    self.store.read(key)
                if result["payload"]["status"] != "completed":
                    continue  # retained; a successful retry still needs the scheduled seed
                metric = self._metric(outputs["metrics"], p["metric"])
                completed.add(run["id"])
                if r["replicate_of"]:
                    original = self._get(history, r["replicate_of"], "run")
                    original_result = self._result(history, original["id"])
                    require(original_result is not None, "missing original result")
                    original_outputs = original_result["payload"]["outputs"]
                    require(outputs["raw_data"] == original_outputs["raw_data"],
                            "reanalysis must use the original raw data artifact")
                    expected = self._metric(original_outputs["metrics"], p["metric"])
                    require(abs(metric - expected) <= p["replication_tolerance"],
                            f"reanalysis disagreement: {run['id']}")
                    successful_replicas.add(original["id"])
            except (GateError, IntegrityError, KeyError) as exc:
                failures.append(str(exc))
        successful_primary = {e["id"] for e in primary if e["id"] in completed}
        seeds = {e["payload"]["seed"] for e in primary if e["id"] in completed}
        if seeds != set(p["seeds"]):
            failures.append("scheduled primary seeds incomplete")
        if set(c["evidence"]) != completed:
            failures.append("claim must include all completed runs, including reanalyses")
        if not successful_primary or not successful_primary <= successful_replicas:
            failures.append("independent reanalysis missing for primary evidence")
        return dict(passed=not failures, failures=failures, basis_hash=basis,
                    claim=claim, checks_version="0.1.0", scientific_validity="not_assessed")

    def review(self, claim: str, *, verdict: str, rationale: str,
               actions: list[str], expected_basis: str) -> str:
        history = self._history()
        gate = self.gate(claim)
        # gate() may observe a newer state; never admit that against stale history.
        basis, runs = self._basis(history, claim)
        require(gate["basis_hash"] == basis == expected_basis, "stale review evidence bundle")
        require(gate["passed"], "mechanical gate failed: " + "; ".join(gate["failures"]))
        c = self._get(history, claim, "claim")
        plan = self._get(history, c["payload"]["protocol"], "protocol")
        hypotheses = [self._get(history, id, "hypothesis") for id in plan["payload"]["hypotheses"]]
        contributors = {c["actor"], plan["actor"], *(r["actor"] for r in runs),
                        *(h["actor"] for h in hypotheses)}
        require(self.actor.id not in contributors, "reviewer must be independent of contributors")
        require(isinstance(verdict, str) and verdict in {"approve", "request_changes", "reject"},
                "invalid review verdict")
        require(isinstance(rationale, str) and bool(rationale.strip()), "review requires rationale")
        require(isinstance(actions, list) and all(isinstance(a, str) and a.strip() for a in actions),
                "actions must be nonempty strings")
        require(verdict == "approve" or bool(actions), "non-approval requires replan actions")
        require(verdict != "approve" or not actions, "approval cannot have unresolved actions")
        return self._write(history, "review", dict(claim=claim, verdict=verdict,
                           rationale=rationale, actions=actions, basis_hash=basis), {"reviewer"})

    def next_action(self, claim: str) -> dict[str, Any]:
        return self._next_action(self._history(), claim)

    def _next_action(self, history: list[dict[str, Any]], claim: str) -> dict[str, Any]:
        gate = self._gate(history, claim)
        if not gate["passed"]:
            return dict(action="repair_evidence", reasons=gate["failures"])
        reviews = [e for e in history if e["kind"] == "review"
                   and e["payload"]["claim"] == claim
                   and e["payload"]["basis_hash"] == gate["basis_hash"]]
        if not reviews:
            return dict(action="scientific_review", basis_hash=gate["basis_hash"])
        # Latest opinion per reviewer; an unresolved negative opinion is a veto.
        latest = {e["actor"]: e["payload"] for e in reviews}
        negative = [r for r in latest.values() if r["verdict"] != "approve"]
        if negative:
            return dict(action="replan", reasons=[a for r in negative for a in r["actions"]])
        return dict(action="paper_candidate", claim=claim, basis_hash=gate["basis_hash"],
                    limitation="local internal approval; human release and venue review still required")
