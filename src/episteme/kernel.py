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

from .claim_context import resolve_context, validate_link
from .claims import ClaimLink
from .protocols import DesignError, StatisticalDesign
from .planning import binding_for, planning_context
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


def protocol_exposures(history: list[dict[str, Any]], plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Declared and conservatively inferred exposures for a typed review basis.

    Kept shared with the read-only graph so historical review hashes use exactly
    the same subset and ordering. Foreign attempts on the same bytes and newly
    declared seen_data matter; an unrelated plan merely inheriting the global
    seen_data snapshot does not. None of these records attests actual access.
    """
    p = plan["payload"]
    if "statistical_design" not in p:
        return []
    data = {p["data"], *p["seen_data"], *(split["digest"] for split in p["statistical_design"]["data_splits"])}
    run_ids = {event["id"] for event in history if event["kind"] == "run"
               and event["payload"]["protocol"] == plan["id"]}
    for event in history:
        if event["kind"] == "result" and event["payload"]["run"] in run_ids:
            raw = event["payload"]["outputs"].get("raw_data")
            if raw is not None:
                data.add(raw)
    plans = {event["id"]: event for event in history if event["kind"] == "protocol"}
    runs = {event["id"]: event for event in history if event["kind"] == "run"}
    selected: set[str] = set()
    known: set[str] = set()
    for event in history:
        e = event["payload"]
        if event["kind"] == "data_exposure":
            if e["data"] in data or e["protocol"] == plan["id"]:
                selected.add(event["id"])
            known.add(e["data"])
        elif event["kind"] == "protocol":
            declared = set(e.get("seen_data", []))
            if event["id"] != plan["id"] and (declared - known) & data:
                selected.add(event["id"])
            known.update(declared)
        elif event["kind"] == "run":
            source = plans[e["protocol"]]
            inputs = protocol_data(source["payload"])
            if source["id"] != plan["id"] and inputs & data:
                selected.update((source["id"], event["id"]))
            known.update(inputs)
        elif event["kind"] == "result":
            raw = e["outputs"].get("raw_data")
            source_run = runs[e["run"]]
            if source_run["payload"]["protocol"] != plan["id"] and (
                    raw in data or source_run["id"] in selected):
                selected.update((source_run["payload"]["protocol"], source_run["id"], event["id"]))
            if raw is not None:
                known.add(raw)
    return [event for event in history if event["id"] in selected]


def protocol_data(payload: dict[str, Any]) -> set[str]:
    return {payload["data"], *(split["digest"] for split in
                              payload.get("statistical_design", {}).get("data_splits", []))}


def exposure_artifacts(event: dict[str, Any]) -> set[str]:
    """Artifact closure of the extra events used to evaluate exposure context."""
    p = event["payload"]
    if event["kind"] == "data_exposure":
        return {p["data"]}
    if event["kind"] == "protocol":
        return protocol_data(p) | set(p.get("seen_data", [])) | {p["implementation"], p["environment"]}
    if event["kind"] == "run":
        return {p["implementation"], p["environment"]}
    if event["kind"] == "result":
        return set(p["outputs"].values())
    if event["kind"] in {"research_question", "explanation_set", "hypothesis"}:
        return set()  # Frozen planning declarations contain event refs, not blob refs.
    raise GateError("unsupported exposure evidence kind")


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
                    replication_tolerance: float, parent: str | None = None,
                    statistical_design: dict[str, Any] | None = None,
                    amendment_reason: str | None = None, seen_data: list[str] | None = None) -> str:
        return self._preregister(history=self._history(), planning=None, hypotheses=hypotheses,
            scope=scope, design=design, metric=metric, analysis_plan=analysis_plan,
            stopping_rule=stopping_rule, seeds=seeds, run_limit=run_limit,
            implementation=implementation, environment=environment, data=data,
            replication_tolerance=replication_tolerance, parent=parent,
            statistical_design=statistical_design, amendment_reason=amendment_reason, seen_data=seen_data)

    def preregister_for_set(self, *, explanation_set: str, design: str, metric: str,
                            analysis_plan: str, stopping_rule: str, seeds: list[int],
                            run_limit: int, implementation: str, environment: str, data: str,
                            replication_tolerance: float, parent: str | None = None,
                            statistical_design: dict[str, Any] | None = None,
                            amendment_reason: str | None = None, seen_data: list[str] | None = None) -> str:
        history = self._history()
        binding = binding_for(history, explanation_set, current=True)
        question = self._get(history, binding["question"], "research_question")["payload"]
        explanations = self._get(history, explanation_set, "explanation_set")["payload"]
        return self._preregister(history=history, planning=binding,
            hypotheses=list(explanations["hypotheses"]), scope=dict(question["scope"]),
            design=design, metric=metric, analysis_plan=analysis_plan,
            stopping_rule=stopping_rule, seeds=seeds, run_limit=run_limit,
            implementation=implementation, environment=environment, data=data,
            replication_tolerance=replication_tolerance, parent=parent,
            statistical_design=statistical_design, amendment_reason=amendment_reason, seen_data=seen_data)

    def _preregister(self, *, history: list[dict[str, Any]], planning: dict[str, Any] | None,
                     hypotheses: list[str], scope: dict[str, str], design: str,
                     metric: str, analysis_plan: str, stopping_rule: str, seeds: list[int],
                     run_limit: int, implementation: str, environment: str, data: str,
                     replication_tolerance: float, parent: str | None,
                     statistical_design: dict[str, Any] | None,
                     amendment_reason: str | None, seen_data: list[str] | None) -> str:
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
            previous = self._get(history, parent, "protocol")["payload"]
            require("statistical_design" not in previous or statistical_design is not None,
                    "typed protocol amendment requires a statistical design")
        # Each amendment is a NEW protocol ID. The parent remains immutable.
        payload = dict(hypotheses=hypotheses, scope=scope, design=design, metric=metric,
                       analysis_plan=analysis_plan, stopping_rule=stopping_rule,
                       seeds=seeds, run_limit=run_limit, implementation=implementation,
                       environment=environment, data=data,
                       replication_tolerance=replication_tolerance, parent=parent)
        if planning is not None:
            payload["planning"] = planning
        self._validate_planning_protocol(history, payload)
        if statistical_design is not None:
            try:
                typed = StatisticalDesign.from_dict(statistical_design)
            except DesignError as exc:
                raise GateError(str(exc)) from exc
            supplied = [] if seen_data is None else seen_data
            require(isinstance(supplied, list) and all(isinstance(key, str) for key in supplied)
                    and len(set(supplied)) == len(supplied), "seen_data must be unique artifact digests")
            payload.update(statistical_design=typed.to_dict(), protocol_mode=typed.mode,
                           amendment_reason=amendment_reason,
                           seen_data=sorted(set(supplied) | self._known_seen_data(history, parent)))
            self._validate_typed_protocol(history, payload)
        else:
            require(amendment_reason is None and seen_data is None,
                    "amendment/exposure declarations require a statistical design")
        return self._write(history, "protocol", payload, {"planner"})

    def _validate_planning_protocol(self, preceding: list[dict[str, Any]], p: dict[str, Any]) -> None:
        previous = self._get(preceding, p["parent"], "protocol")["payload"] if p["parent"] is not None else {}
        require("planning" not in previous or "planning" in p,
                "planning-bound protocol amendment cannot drop its planning binding")
        if "planning" not in p:
            return
        binding = p["planning"]
        try:
            context = planning_context(preceding, binding)
            require(binding_for(preceding, binding["explanation_set"], current=True) == binding,
                    "protocol planning binding is not current at preregistration")
            question = self._get(preceding, binding["question"], "research_question")["payload"]
            explanations = self._get(preceding, binding["explanation_set"], "explanation_set")["payload"]
            require(p["scope"] == question["scope"] and p["hypotheses"] == explanations["hypotheses"],
                    "protocol scope or hypotheses differ from its frozen explanation set")
            if "planning" in previous:
                prior = planning_context(preceding, previous["planning"])
                roots = lambda events: {event["id"] for event in events
                    if event["kind"] == "research_question" and event["payload"]["parent"] is None}
                require(previous["planning"]["study_id"] == binding["study_id"] and roots(prior) == roots(context),
                        "protocol amendment must retain its study and question lineage")
        except (ValueError, KeyError, TypeError) as exc:
            raise GateError(f"invalid protocol planning binding: {exc}") from exc

    def _planning_evidence(self, history: list[dict[str, Any]], plan: dict[str, Any]) -> list[dict[str, Any]]:
        preceding = [event for event in history if event["seq"] < plan["seq"]]
        self._validate_planning_protocol(preceding, plan["payload"])
        return planning_context(preceding, plan["payload"]["planning"]) if "planning" in plan["payload"] else []

    @staticmethod
    def _known_seen_data(history: list[dict[str, Any]], parent: str | None = None) -> set[str]:
        """Conservative local-store policy: declared reads and attempted inputs.

        An amendment treats all parent inputs/splits as exposed, even without a
        declared read. There is no authenticated access service to prove that an
        old parent holdout remained concealed. This deliberately requires new
        confirmatory bytes and applies to sequential designs as well.
        """
        seen = {event["payload"]["data"] for event in history if event["kind"] == "data_exposure"}
        # A failed attempt can also expose observations that inform a new plan.
        seen.update(event["payload"]["outputs"]["raw_data"] for event in history
                    if event["kind"] == "result" and "raw_data" in event["payload"]["outputs"])
        attempted = {event["payload"]["protocol"] for event in history if event["kind"] == "run"}
        for event in history:
            if event["kind"] != "protocol":
                continue
            previous = event["payload"]
            seen.update(previous.get("seen_data", []))
            if event["id"] in attempted:
                # Omitting a parent link must not relabel an attempted discovery
                # input as a fresh confirmatory holdout in a new root protocol.
                seen.add(previous["data"])
                seen.update(split["digest"] for split in previous.get("statistical_design", {}).get("data_splits", []))
        if parent is not None:
            previous = Kernel._get(history, parent, "protocol")["payload"]
            seen.add(previous["data"])
            seen.update(previous.get("seen_data", []))
            seen.update(split["digest"] for split in previous.get("statistical_design", {}).get("data_splits", []))
        return seen

    def _validate_typed_protocol(self, preceding_history: list[dict[str, Any]], p: dict[str, Any]) -> None:
        if "statistical_design" not in p:
            return
        try:
            typed = StatisticalDesign.from_dict(p["statistical_design"])
            typed.validate_metric(p["metric"])
        except DesignError as exc:
            raise GateError(str(exc)) from exc
        require(p["statistical_design"] == typed.to_dict(), "statistical design is not normalized")
        require(p["protocol_mode"] == typed.mode, "protocol mode differs from statistical design")
        require(p["stopping_rule"] == typed.stopping_rule.rule, "stopping rule differs from statistical design")
        seen = p["seen_data"]
        require(isinstance(seen, list) and all(isinstance(key, str) for key in seen)
                and len(set(seen)) == len(seen), "seen_data must be unique artifact digests")
        required = self._known_seen_data(preceding_history, p["parent"])
        require(required <= set(seen), "seen_data omits known exposure or parent inputs")
        if p["parent"] is not None:
            require(isinstance(p["amendment_reason"], str) and bool(p["amendment_reason"].strip()),
                    "typed protocol amendment requires a reason")
        else:
            require(p["amendment_reason"] is None, "amendment reason requires a parent protocol")
        for key in {*seen, *(split.digest for split in typed.data_splits)}:
            self.store.read(key)
        if typed.mode == "confirmatory":
            for split in typed.data_splits:
                if split.role == "confirmatory":
                    require(split.digest not in seen,
                            f"confirmatory split was already exposed: {split.id}")

    def expose_data(self, *, data: str, purpose: str, protocol: str | None = None) -> str:
        """Record a caller-declared exposure, not an authenticated access log."""
        history = self._history()
        self.store.read(data)
        require(isinstance(purpose, str) and bool(purpose.strip()), "data exposure needs a purpose")
        if protocol is not None:
            self._get(history, protocol, "protocol")
        return self._write(history, "data_exposure", dict(data=data, purpose=purpose, protocol=protocol),
                           {"planner", "executor", "replicator", "analyst", "reviewer"})

    def start_run(self, protocol: str, *, seed: int, implementation: str,
                  environment: str, command: list[str], replicate_of: str | None = None) -> str:
        history = self._history()
        plan = self._get(history, protocol, "protocol")
        p = plan["payload"]
        self._planning_evidence(history, plan)
        self._validate_typed_protocol([event for event in history if event["seq"] < plan["seq"]], p)
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
              evidence: list[str], limitations: list[str], outcome: str,
              inference_mode: str | None = None) -> str:
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
        mode = self._inference_mode(plan, inference_mode)
        payload = dict(protocol=protocol, statement=statement, scope=scope,
                       evidence=evidence, limitations=limitations, outcome=outcome)
        if "statistical_design" in plan or inference_mode is not None:
            payload["inference_mode"] = mode
        return self._write(history, "claim", payload, {"analyst", "executor"})

    @staticmethod
    def _inference_mode(plan: dict[str, Any], requested: str | None) -> str:
        mode = plan.get("protocol_mode", "unclassified") if requested is None else requested
        require(isinstance(mode, str) and mode in {"unclassified", "descriptive", "exploratory", "confirmatory"},
                "invalid claim inference mode")
        require(mode != "confirmatory" or ("statistical_design" in plan
                and plan.get("protocol_mode") == "confirmatory"),
                "confirmatory inference requires a confirmatory statistical protocol")
        return mode

    def _local_evidence(self, history: list[dict[str, Any]], claim: str
                        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        c = self._get(history, claim, "claim")
        protocol = c["payload"]["protocol"]
        plan = self._get(history, protocol, "protocol")
        runs = [e for e in history if e["kind"] == "run" and e["payload"]["protocol"] == protocol]
        ids = {e["id"] for e in runs}
        results = [e for e in history if e["kind"] == "result" and e["payload"]["run"] in ids]
        basis = [plan, c, *runs, *results, *protocol_exposures(history, plan)]
        planning = {event["id"]: event for protocol_event in basis if protocol_event["kind"] == "protocol"
                    for event in self._planning_evidence(history, protocol_event)}
        basis.extend(sorted(planning.values(), key=lambda event: event["seq"]))
        return basis, runs

    def _basis(self, history: list[dict[str, Any]], claim: str) -> tuple[str, list[dict[str, Any]]]:
        local, runs = self._local_evidence(history, claim)
        context = resolve_context(history, claim)
        if not context.link_ids:
            return digest(canonical(local)), runs  # Preserve the published v1 recipe exactly.
        basis = dict(basis_version=2, claim=claim,
                     claims=[dict(claim=id, evidence=self._local_evidence(history, id)[0])
                             for id in context.claim_ids],
                     links=[self._get(history, id, "claim_link") for id in context.link_ids],
                     context_findings=self._context_findings(history, claim)[0])
        return digest(canonical(basis)), runs

    @staticmethod
    def _context_findings(history: list[dict[str, Any]], claim: str
                          ) -> tuple[list[dict[str, Any]], set[str]]:
        """Keep foreign negative findings and their first same-owner closure.

        Own reviews never enter their own evidence basis. Ordinary positive
        reviews also stay out: repeated mutual approvals must not stale each
        other forever. Retaining closed episodes prevents revival of an older
        approval after a finding is raised and subsequently withdrawn.
        """
        others = set(resolve_context(history, claim).claim_ids) - {claim}
        records: list[dict[str, Any]] = []
        pending: dict[tuple[str, str], str] = {}
        for event in history:
            p = event["payload"]
            if event["kind"] != "review" or p["claim"] not in others:
                continue
            key = p["claim"], event["actor"]
            if p["verdict"] != "approve":
                records.append(event)
                pending[key] = event["id"]
            elif key in pending:
                records.append(event)
                del pending[key]
        return records, set(pending.values())

    def link_claims(self, *, source: str, target: str, relation: str, rationale: str,
                    expected_bases: dict[str, str]) -> str:
        """Record a relation proposal at two immutable evidence revisions."""
        require(self.actor.role in {"planner", "analyst"}, "only planner/analyst may propose claim links")
        history = self._history()
        require(isinstance(expected_bases, dict) and set(expected_bases) == {source, target},
                "expected bases must cover exactly both claim endpoints")
        a, b = self._get(history, source, "claim"), self._get(history, target, "claim")
        source_basis, _ = self._basis(history, source)
        target_basis, _ = self._basis(history, target)
        require(expected_bases == {source: source_basis, target: target_basis}, "stale claim link evidence")
        link = ClaimLink(schema_version=1, source=source, target=target, relation=relation,
                         rationale=rationale, source_hash=a["hash"], target_hash=b["hash"],
                         source_basis=source_basis, target_basis=target_basis)
        validate_link(history, link)
        # An author of a new relation cannot subsequently provide its independent
        # review. Avoid stranding a prior veto whose owner must explicitly revise it.
        candidate = dict(id="candidate-" + uuid4().hex, seq=len(history) + 1, kind="claim_link",
                         payload=link.to_dict(), hash=digest(canonical(link.to_dict())), actor=self.actor.id)
        latest = {(e["payload"]["claim"], e["actor"]): e for e in history if e["kind"] == "review"}
        for (id, owner), review in latest.items():
            if review["payload"]["verdict"] != "approve":
                context, contributors, _ = self._review_members([*history, candidate], id)
                require(candidate["id"] not in context.link_ids or owner not in contributors,
                        "claim link would prevent an open review veto owner from independently reviewing its context")
        return self._write(history, "claim_link", link.to_dict(), {"planner", "analyst"})

    def gate(self, claim: str) -> dict[str, Any]:
        """Fail closed on absent/changed evidence; never certifies scientific truth."""
        return self._gate(self._history(), claim)

    def _gate(self, history: list[dict[str, Any]], claim: str) -> dict[str, Any]:
        self._get(history, claim, "claim")
        context = resolve_context(history, claim)
        result = self._gate_local(history, claim)
        if context.link_ids:
            related = []
            for id in context.claim_ids:
                if id != claim:
                    record = self._get(history, id, "claim")
                    original = [e for e in history if e["seq"] <= record["seq"]]
                    related.append(dict(claim=id, historical_gate=self._gate_local(original, id),
                                        current_gate=self._gate_local(history, id)))
                    # A historical claim can be superseded because newer runs
                    # exceed its immutable citations. Verify all current bytes
                    # without mislabelling that old claim as mechanically valid.
                    for event in self._local_evidence(history, id)[0]:
                        if event["kind"] == "claim":
                            continue
                        for key in exposure_artifacts(event):
                            try:
                                self.store.read(key)
                            except IntegrityError as exc:
                                result["failures"].append(f"linked claim {id}: {exc}")
            for id in context.link_ids:
                link = self._get(history, id, "claim_link")
                before = [e for e in history if e["seq"] < link["seq"]]
                p = link["payload"]
                if (self._basis(before, p["source"])[0] != p["source_basis"]
                        or self._basis(before, p["target"])[0] != p["target_basis"]):
                    result["failures"].append(f"claim link basis mismatch: {id}")
            result["basis_hash"] = self._basis(history, claim)[0]
            result["passed"] = not result["failures"]
            result["checks_version"] = "0.2.0"
            result["context_claims"] = list(context.claim_ids)
            result["claim_links"] = list(context.link_ids)
            result["related_claims"] = related
            result["open_context_reviews"] = sorted(self._context_findings(history, claim)[1])
        return result

    def _gate_local(self, history: list[dict[str, Any]], claim: str) -> dict[str, Any]:
        c = self._get(history, claim, "claim")["payload"]
        plan = self._get(history, c["protocol"], "protocol")
        p = plan["payload"]
        evidence, runs = self._local_evidence(history, claim)
        basis = digest(canonical(evidence))
        failures: list[str] = []
        primary: list[dict[str, Any]] = []
        completed: set[str] = set()
        successful_replicas: set[str] = set()
        try:
            self._validate_typed_protocol([event for event in history if event["seq"] < plan["seq"]], p)
            self._inference_mode(p, c.get("inference_mode"))
            for exposure in protocol_exposures(history, plan):
                for key in exposure_artifacts(exposure):
                    self.store.read(key)
        except (GateError, IntegrityError, KeyError) as exc:
            failures.append(str(exc))
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
        return self._record_review(claim, verdict=verdict, rationale=rationale, actions=actions,
                                   expected_basis=expected_basis, link_assessments=None)

    def review_with_links(self, claim: str, *, verdict: str, rationale: str,
                          actions: list[str], expected_basis: str,
                          link_assessments: dict[str, dict[str, Any]]) -> str:
        """Review every declared relation explicitly; no automatic truth inference."""
        require(isinstance(link_assessments, dict), "link assessments must be a mapping")
        return self._record_review(claim, verdict=verdict, rationale=rationale, actions=actions,
                                   expected_basis=expected_basis, link_assessments=link_assessments)

    def _record_review(self, claim: str, *, verdict: str, rationale: str,
                       actions: list[str], expected_basis: str,
                       link_assessments: dict[str, dict[str, Any]] | None) -> str:
        history = self._history()
        gate = self.gate(claim)
        # gate() may observe a newer state; never admit that against stale history.
        basis, runs = self._basis(history, claim)
        require(gate["basis_hash"] == basis == expected_basis, "stale review evidence bundle")
        require(gate["passed"], "mechanical gate failed: " + "; ".join(gate["failures"]))
        context, contributors, admissible_refs = self._review_members(history, claim)
        require(not context.link_ids or link_assessments is not None,
                "linked claim context requires review_with_links")
        require(self.actor.id not in contributors, "reviewer must be independent of contributors")
        require(isinstance(verdict, str) and verdict in {"approve", "request_changes", "reject"},
                "invalid review verdict")
        require(isinstance(rationale, str) and bool(rationale.strip()), "review requires rationale")
        require(isinstance(actions, list) and all(isinstance(a, str) and a.strip() for a in actions),
                "actions must be nonempty strings")
        require(verdict == "approve" or bool(actions), "non-approval requires replan actions")
        require(verdict != "approve" or not actions, "approval cannot have unresolved actions")
        payload = dict(claim=claim, verdict=verdict, rationale=rationale, actions=actions, basis_hash=basis)
        if link_assessments is not None:
            self._validate_assessments(link_assessments, set(context.link_ids), admissible_refs, verdict)
            if verdict == "approve":
                acknowledged = {id for assessment in link_assessments.values() for id in assessment["evidence"]}
                require(self._context_findings(history, claim)[1] <= acknowledged,
                        "approval must explicitly acknowledge every open review in the linked context")
                accepted_replacements = {
                    self._get(history, id, "claim_link")["payload"]["target"]
                    for id, assessment in link_assessments.items()
                    if assessment["judgment"] == "accepted"
                    and self._get(history, id, "claim_link")["payload"]["relation"] == "supersedes"}
                for id, assessment in link_assessments.items():
                    if assessment["judgment"] != "accepted":
                        continue
                    link = self._get(history, id, "claim_link")["payload"]
                    source = self._get(history, link["source"], "claim")
                    # A -> B -> C keeps B's historical replacement decision,
                    # while current coverage belongs to the accepted frontier C.
                    current_source = (link["relation"] == "supersedes"
                                      and source["id"] not in accepted_replacements)
                    basis_history = (history if current_source else
                                     [e for e in history if e["seq"] <= source["seq"]])
                    checked = self._gate_local(basis_history, source["id"])
                    require(checked["passed"], f"accepted link has mechanically unqualified source: {id}")
            payload.update(review_schema_version=2, link_assessments=link_assessments)
        return self._write(history, "review", payload, {"reviewer"})

    def _review_members(self, history: list[dict[str, Any]], claim: str) -> tuple[Any, set[str], set[str]]:
        context = resolve_context(history, claim)
        contributors: set[str] = set()
        admissible_refs = set(context.link_ids)
        for id in context.claim_ids:
            evidence, _ = self._local_evidence(history, id)
            for event in evidence:
                admissible_refs.add(event["id"])
                if event["kind"] != "data_exposure":
                    contributors.add(event["actor"])
                if event["kind"] == "protocol":
                    for hypothesis in event["payload"]["hypotheses"]:
                        admissible_refs.add(hypothesis)
                        contributors.add(self._get(history, hypothesis, "hypothesis")["actor"])
        contributors.update(self._get(history, id, "claim_link")["actor"] for id in context.link_ids)
        admissible_refs.update(event["id"] for event in self._context_findings(history, claim)[0])
        return context, contributors, admissible_refs

    @staticmethod
    def _validate_assessments(assessments: dict[str, Any], links: set[str],
                              admissible_refs: set[str], verdict: str) -> None:
        require(isinstance(assessments, dict) and set(assessments) == links,
                "assessments must cover exactly all claim links in the review context")
        for assessment in assessments.values():
            require(isinstance(assessment, dict) and set(assessment) == {
                "judgment", "disposition", "rationale", "evidence"}, "invalid link assessment fields")
            require(isinstance(assessment["judgment"], str) and assessment["judgment"] in {
                "accepted", "rejected", "unresolved"}, "invalid link judgment")
            require(isinstance(assessment["disposition"], str) and assessment["disposition"] in {
                "compatible_as_written", "requires_claim_revision", "needs_evidence"}, "invalid link disposition")
            require(isinstance(assessment["rationale"], str) and bool(assessment["rationale"].strip()),
                    "link assessment requires a rationale")
            refs = assessment["evidence"]
            require(isinstance(refs, list) and bool(refs) and all(isinstance(id, str) for id in refs)
                    and len(set(refs)) == len(refs) and set(refs) <= admissible_refs,
                    "link assessment needs unique evidence references from its review context")
            if verdict == "approve":
                require(assessment["judgment"] != "unresolved"
                        and assessment["disposition"] == "compatible_as_written",
                        "approval cannot leave an unresolved or incompatible claim link")

    def next_action(self, claim: str) -> dict[str, Any]:
        return self._next_action(self._history(), claim)

    def _next_action(self, history: list[dict[str, Any]], claim: str) -> dict[str, Any]:
        gate = self._gate(history, claim)
        if not gate["passed"]:
            return dict(action="repair_evidence", reasons=gate["failures"])
        all_reviews = [e for e in history if e["kind"] == "review" and e["payload"]["claim"] == claim]
        reviews = [e for e in all_reviews if e["payload"]["basis_hash"] == gate["basis_hash"]]
        # Latest opinion per reviewer; an unresolved negative opinion is a veto.
        latest = {e["actor"]: e["payload"] for e in all_reviews}
        negative = [r for r in latest.values() if r["verdict"] != "approve"]
        if negative:
            return dict(action="replan", reasons=[a for r in negative for a in r["actions"]])
        if not reviews:
            return dict(action="scientific_review", basis_hash=gate["basis_hash"])
        current = {e["actor"]: e["payload"] for e in reviews}
        for link in (self._get(history, id, "claim_link") for id in resolve_context(history, claim).link_ids):
            if link["payload"]["relation"] == "supersedes" and link["payload"]["target"] == claim:
                if any(review.get("link_assessments", {}).get(link["id"], {}).get("judgment") == "accepted"
                       for review in current.values()):
                    return dict(action="superseded", claim=claim, replacement=link["payload"]["source"],
                                reason="reviewed replacement; original claim and findings remain in history")
        return dict(action="paper_candidate", claim=claim, basis_hash=gate["basis_hash"],
                    limitation="local internal approval; human release and venue review still required")
