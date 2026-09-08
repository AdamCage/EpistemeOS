"""Persistent preference tournaments and bounded experiment search.

The score is a planner's scheduling heuristic, never evidence of scientific truth.
All commands use the same optimistic revision boundary as the research kernel.
Actor identities remain caller-supplied; this module does not authenticate agents.
Cost reservations bound admitted estimates; recorded real overruns stop new work.
Selections are durable reservations, not a process dispatcher or execution lease.
"""

from __future__ import annotations

import math
from fractions import Fraction
from itertools import combinations
from typing import Any
from uuid import uuid4

from .kernel import Actor, GateError, require
from .store import Store, canonical, digest


COMPONENTS = ("discrimination", "uncertainty", "coverage", "invalidity_risk")
ACTIONS = {"baseline", "discriminate", "ablate", "robustness", "replicate", "debug", "retry"}
TECHNICAL_ACTIONS = {"debug", "retry"}


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _number(value: Any, minimum: float = 0) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= minimum
    except OverflowError:
        return False


def _amount(value: int | float) -> Fraction:
    """Use the declared decimal representation, without a precision context."""
    return Fraction(str(value))


def _cost_text(value: Fraction) -> str:
    """Render an exact terminating decimal; normalize trailing zeroes and -0."""
    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    require(denominator == 1, "cost cannot be represented as a finite decimal")
    places = max(twos, fives)
    coefficient = abs(value.numerator) * 2 ** (places - twos) * 5 ** (places - fives)
    digits = str(coefficient).rjust(places + 1, "0")
    rendered = ((digits[:-places] + "." + digits[-places:]).rstrip("0").rstrip(".")
                if places else digits)
    return ("-" if value < 0 else "") + rendered


class Search:
    def __init__(self, store: Store, actor: Actor):
        require(_text(actor.id) and _text(actor.role), "actor id and role are required")
        self.store = store
        self.actor = actor

    def _history(self) -> list[dict[str, Any]]:
        return self.store.events()

    @staticmethod
    def _get(history: list[dict[str, Any]], id: str, kind: str) -> dict[str, Any]:
        require(_text(id), f"invalid {kind} reference")
        for event in history:
            if event["id"] == id and event["kind"] == kind:
                return event
        raise GateError(f"unknown {kind}: {id}")

    def _write(self, history: list[dict[str, Any]], kind: str, payload: dict[str, Any],
               roles: set[str] | None = None) -> str:
        require(self.actor.role in (roles or {"planner"}),
                f"{self.actor.role} cannot create {kind}")
        id = f"{kind}-{uuid4().hex[:16]}"
        self.store.append(id=id, kind=kind, actor=self.actor.id, role=self.actor.role,
                          payload=payload, expected_revision=len(history))
        return id

    def register_tournament(self, *, candidates: list[str], rubric: str,
                            seed: int = 0, rounds: int = 2) -> str:
        """Freeze a same-scope candidate pool and alternating A/B round schedule."""
        history = self._history()
        require(isinstance(candidates, list) and len(candidates) >= 2
                and all(_text(id) for id in candidates)
                and len(set(candidates)) == len(candidates), "unique candidate IDs required")
        require(_text(rubric), "tournament rubric/version required")
        require(type(seed) is int and type(rounds) is int and 1 <= rounds <= 100,
                "integer seed and rounds in [1, 100] required")
        hypotheses = [self._get(history, id, "hypothesis") for id in candidates]
        require(all(h["payload"]["scope"] == hypotheses[0]["payload"]["scope"]
                    for h in hypotheses), "tournament candidate scope mismatch")
        return self._write(history, "tournament", dict(
            candidates=candidates, candidate_hashes={h["id"]: h["hash"] for h in hypotheses},
            rubric=rubric, seed=seed, rounds=rounds, meaning="research_priority_only",
            aggregation="mean_nonabstaining_pairwise_preference_v1"))

    @staticmethod
    def _pairings(plan: dict[str, Any]) -> list[dict[str, Any]]:
        result = []
        for left, right in combinations(sorted(plan["candidates"]), 2):
            flip = int(digest(canonical([plan["seed"], left, right])), 16) % 2
            for round in range(plan["rounds"]):
                a, b = (right, left) if (flip + round) % 2 else (left, right)
                result.append(dict(a=a, b=b, round=round))
        return result

    def pairings(self, tournament: str) -> list[dict[str, Any]]:
        return self._pairings(self._get(self._history(), tournament, "tournament")["payload"])

    def ballot(self, tournament: str, *, a: str, b: str, round: int,
               verdict: str, rationale: str, judge: dict[str, str],
               sources: list[str] | None = None) -> str:
        """Record a judge's scheduled comparison; repeated judge/pair/round is rejected.

        ``verdict`` is a, b, tie, or abstain. Judge metadata requires model,
        model_version and prompt_version; the event actor supplies judge identity.
        Sources are existing event IDs, not unchecked URLs or fabricated citations.
        """
        history = self._history()
        plan = self._get(history, tournament, "tournament")["payload"]
        require(_text(a) and _text(b) and type(round) is int
                and dict(a=a, b=b, round=round) in self._pairings(plan),
                "ballot must match registered candidates, round and A/B order")
        require(isinstance(verdict, str) and verdict in {"a", "b", "tie", "abstain"},
                "invalid ballot verdict")
        require(_text(rationale), "ballot rationale required")
        require(isinstance(judge, dict)
                and {"model", "model_version", "prompt_version"} <= judge.keys()
                and all(_text(k) and _text(v) for k, v in judge.items()),
                "judge model, model_version and prompt_version required")
        sources = [] if sources is None else sources
        require(isinstance(sources, list) and all(_text(s) for s in sources)
                and len(set(sources)) == len(sources), "sources must be unique event IDs")
        known = {e["id"] for e in history}
        require(set(sources) <= known, "unknown ballot source reference")
        require(not any(e["kind"] == "tournament_ballot" and e["actor"] == self.actor.id
                        and e["payload"]["tournament"] == tournament
                        and e["payload"]["round"] == round
                        and {e["payload"]["a"], e["payload"]["b"]} == {a, b}
                        for e in history), "judge already voted on this pair and round")
        return self._write(history, "tournament_ballot", dict(
            tournament=tournament, a=a, b=b, round=round, verdict=verdict,
            winner=a if verdict == "a" else b if verdict == "b" else None,
            rationale=rationale, judge=judge, judge_id=self.actor.id, sources=sources,
            rubric=plan["rubric"], meaning="research_priority_only"), {"judge", "reviewer"})

    def ranking(self, tournament: str) -> dict[str, Any]:
        history = self._history()
        plan = self._get(history, tournament, "tournament")["payload"]
        rows = {id: dict(candidate=id, wins=0, losses=0, ties=0, abstentions=0)
                for id in plan["candidates"]}
        ballots = [e for e in history if e["kind"] == "tournament_ballot"
                   and e["payload"]["tournament"] == tournament]
        for event in ballots:
            ballot = event["payload"]
            for id in (ballot["a"], ballot["b"]):
                key = ("abstentions" if ballot["verdict"] == "abstain" else "ties"
                       if ballot["verdict"] == "tie" else "wins"
                       if ballot["winner"] == id else "losses")
                rows[id][key] += 1
        for row in rows.values():
            compared = row["wins"] + row["losses"] + row["ties"]
            row["comparisons"] = compared
            row["priority"] = (row["wins"] + 0.5 * row["ties"]) / compared if compared else 0.5
        ranked = sorted(rows.values(), key=lambda r: (
            -r["priority"], digest(canonical([plan["seed"], r["candidate"]]))))
        return dict(tournament=tournament, revision=len(history), ranking=ranked,
                    ballot_ids=[e["id"] for e in ballots], meaning="research_priority_only",
                    scientific_validity="not_assessed", complete_schedule_not_required=True)

    def register_tree(self, *, weights: dict[str, float], cost_weight: float,
                      budget: float, cost_unit: str, max_nodes: int, max_depth: int,
                      max_width: int, max_selections: int, max_retries: int = 2,
                      max_inflight: int = 1, seed: int = 0,
                      tournament: str | None = None) -> str:
        """Freeze search policy before node proposals and selections.

        Priority = weighted discrimination + uncertainty + coverage minus weighted
        invalidity risk and cost. Components must be estimates in [0, 1].
        Bounds count retained history, including failed and null-result branches.
        """
        history = self._history()
        require(isinstance(weights, dict) and set(weights) == set(COMPONENTS)
                and all(_number(v) for v in weights.values())
                and any(weights[k] > 0 for k in COMPONENTS[:3]),
                "finite nonnegative weights for all score components required")
        require(_number(cost_weight) and _number(budget) and _text(cost_unit),
                "finite nonnegative cost weight/budget and cost unit required")
        for name, value, minimum in (("max_nodes", max_nodes, 1), ("max_depth", max_depth, 0),
                                      ("max_width", max_width, 1),
                                      ("max_selections", max_selections, 1),
                                      ("max_retries", max_retries, 0),
                                      ("max_inflight", max_inflight, 1)):
            require(type(value) is int and value >= minimum, f"invalid {name}")
        require(type(seed) is int, "seed must be an integer")
        if tournament is not None:
            self._get(history, tournament, "tournament")
        return self._write(history, "search_tree", dict(
            weights=weights, cost_weight=cost_weight, budget=budget, cost_unit=cost_unit,
            max_nodes=max_nodes, max_depth=max_depth, max_width=max_width,
            max_selections=max_selections, max_retries=max_retries,
            max_inflight=max_inflight, seed=seed, tournament=tournament,
            policy="bounded_best_first_v1", meaning="research_priority_only"))

    @staticmethod
    def _projection(history: list[dict[str, Any]], tree: str) -> dict[str, Any]:
        plan = Search._get(history, tree, "search_tree")["payload"]
        nodes = {e["id"]: dict(e["payload"], id=e["id"], state="pending", selection=None,
                              scientific_outcome="not_assessed")
                 for e in history if e["kind"] == "experiment_node" and e["payload"]["tree"] == tree}
        selections = {e["id"]: e for e in history if e["kind"] == "search_selection"
                      and e["payload"]["tree"] == tree and e["payload"]["node"] is not None}
        terminals = {e["payload"]["selection"]: e["payload"] for e in history
                     if e["kind"] == "search_terminal" and e["payload"]["tree"] == tree}
        spent, reserved = Fraction(0), Fraction(0)
        for id, event in selections.items():
            node = nodes[event["payload"]["node"]]
            node["selection"] = id
            if id in terminals:
                terminal = terminals[id]
                node.update(state=terminal["status"], scientific_outcome=terminal["scientific_outcome"])
                spent += _amount(terminal["actual_cost"])
            else:
                node["state"] = "selected"
                reserved += _amount(event["payload"]["reserved_cost"])
        return dict(tree=tree, revision=len(history), policy=plan, nodes=nodes,
                    selections=selections, terminals=terminals, spent=spent, reserved=reserved,
                    remaining=_amount(plan["budget"]) - spent - reserved)

    def tree_state(self, tree: str) -> dict[str, Any]:
        """Replay state; unfinished selections retain their reservations across restarts.

        Cost totals are exact decimal strings, with trailing zeroes normalized.
        """
        state = self._projection(self._history(), tree)
        for key in ("spent", "reserved", "remaining"):
            state[key] = _cost_text(state[key])
        state["scientific_validity"] = "not_assessed"
        return state

    @staticmethod
    def _technical_root(nodes: dict[str, dict[str, Any]], node: str) -> str:
        """Find the scientific/replication attempt that a repair lineage belongs to."""
        while nodes[node]["action"] in TECHNICAL_ACTIONS:
            node = nodes[node]["parent"]
        return node

    def add_node(self, tree: str, *, protocol: str, action: str,
                 components: dict[str, float], estimated_cost: float,
                 rationale: str, parent: str | None = None) -> str:
        """Freeze a proposal before selection; parent links only point backward.

        Retry keeps the same protocol after a technical failure. Debug may amend
        that protocol but never becomes scientific evidence via this search API.
        The retry bound counts all retained repair proposals in that lineage,
        including siblings and cancelled proposals, rather than only its depth.
        Scientific follow-ups use a new unexecuted frozen protocol. Replication
        may reuse a protocol with a completed primary run.
        """
        history = self._history()
        state = self._projection(history, tree)
        policy, nodes = state["policy"], state["nodes"]
        plan = self._get(history, protocol, "protocol")
        require(isinstance(action, str) and action in ACTIONS, "unknown experiment action")
        require(_text(rationale), "node score/cost rationale required")
        require(isinstance(components, dict) and set(components) == set(COMPONENTS)
                and all(_number(v) and v <= 1 for v in components.values()),
                "finite score components in [0, 1] required")
        require(_number(estimated_cost) and estimated_cost > 0,
                "finite positive estimated cost required")
        require(len(nodes) < policy["max_nodes"], "tree node bound exhausted")
        require(sum(n["parent"] == parent for n in nodes.values()) < policy["max_width"],
                "tree width bound exhausted")
        depth, technical_attempt = 0, 0
        evidence_eligible, execution_mode = action != "debug", "replicate" if action == "replicate" else "primary"
        if parent is not None:
            require(_text(parent) and parent in nodes, "parent must be an existing node in this tree")
            depth = nodes[parent]["depth"] + 1
            require(nodes[parent]["state"] in {"completed", "failed", "cancelled"},
                    "parent must have a terminal selection")
        require(depth <= policy["max_depth"], "tree depth bound exhausted")
        if policy["tournament"]:
            pool = self._get(history, policy["tournament"], "tournament")["payload"]["candidates"]
            require(set(plan["payload"]["hypotheses"]) <= set(pool),
                    "protocol hypotheses must belong to the tree tournament")
        runs = [e for e in history if e["kind"] == "run" and e["payload"]["protocol"] == protocol]
        if action in TECHNICAL_ACTIONS:
            require(parent is not None and nodes[parent]["state"] == "failed",
                    "retry/debug requires a technically failed parent, not a null result")
            root = self._technical_root(nodes, parent)
            technical_attempt = 1 + sum(
                n["action"] in TECHNICAL_ACTIONS and self._technical_root(nodes, id) == root
                for id, n in nodes.items())
            require(technical_attempt <= policy["max_retries"], "technical retry bound exhausted")
            prior_protocol = nodes[parent]["protocol"]
            require(protocol == prior_protocol or (action == "debug"
                    and plan["payload"]["parent"] == prior_protocol),
                    "retry keeps protocol; debug changes require a protocol amendment")
            if action == "retry":
                require(components == nodes[parent]["components"], "retry keeps registered components")
                evidence_eligible = nodes[parent]["scientific_evidence_eligible"]
                execution_mode = nodes[parent]["execution_mode"]
        elif action == "replicate":
            results = {e["payload"]["run"]: e["payload"]["status"] for e in history
                       if e["kind"] == "result"}
            require(any(r["payload"]["replicate_of"] is None and results.get(r["id"]) == "completed"
                        for r in runs), "replication requires completed primary execution")
        else:
            require(not runs, "scientific node requires an unexecuted frozen protocol")
        require(not any(n["protocol"] == protocol and n["action"] == action
                        and n["state"] in {"pending", "selected"} for n in nodes.values()),
                "duplicate pending protocol/action")
        score = sum(policy["weights"][k] * components[k] * (-1 if k == "invalidity_risk" else 1)
                    for k in COMPONENTS) - policy["cost_weight"] * estimated_cost
        require(_number(score, -math.inf), "search score overflow")
        return self._write(history, "experiment_node", dict(
            tree=tree, parent=parent, protocol=protocol, protocol_hash=plan["hash"], action=action,
            depth=depth, technical_attempt=technical_attempt, components=components,
            estimated_cost=estimated_cost, rationale=rationale, score=score,
            execution_mode=execution_mode, scientific_evidence_eligible=evidence_eligible))

    @staticmethod
    def _selection_eligibility(history: list[dict[str, Any]], node: dict[str, Any]) -> str:
        """Recheck execution facts when admitting a persisted proposal."""
        plan = Search._get(history, node["protocol"], "protocol")
        require(plan["hash"] == node["protocol_hash"], "node protocol hash mismatch")
        runs = [e for e in history if e["kind"] == "run"
                and e["payload"]["protocol"] == node["protocol"]]
        results = {e["payload"]["run"]: e["payload"]["status"]
                   for e in history if e["kind"] == "result"}
        if node["action"] not in TECHNICAL_ACTIONS | {"replicate"} and runs:
            return "protocol_already_executed"
        if len(runs) >= plan["payload"]["run_limit"]:
            return "protocol_run_limit"
        if any(r["id"] not in results for r in runs):
            return "protocol_inflight"
        terminal_selections = {e["payload"]["selection"] for e in history
                               if e["kind"] == "search_terminal"}
        if any(e["kind"] == "search_selection" and e["payload"]["node"] is not None
               and e["payload"]["protocol"] == node["protocol"]
               and e["id"] not in terminal_selections for e in history):
            return "protocol_reserved"
        if node["execution_mode"] == "replicate" and not any(
                r["payload"]["replicate_of"] is None and results.get(r["id"]) == "completed"
                for r in runs):
            return "replication_requires_completed_primary"
        return "eligible"

    def select_next(self, tree: str) -> dict[str, Any]:
        """Atomically record a best-first decision and reserve its estimated cost.

        Returns decision ID, node/protocol/action or a durable wait/stop reason.
        No automatic retry is safe after a conflict: reload and make a new decision.
        Ineligible proposals remain in the frontier with their current reason.
        """
        history = self._history()
        state = self._projection(history, tree)
        policy, nodes = state["policy"], state["nodes"]
        pending = sorted((n for n in nodes.values() if n["state"] == "pending"), key=lambda n: (
            -n["score"], digest(canonical([policy["seed"], n["id"]]))))
        frontier = []
        for node in pending:
            eligibility = self._selection_eligibility(history, node)
            if eligibility == "eligible" and _amount(node["estimated_cost"]) > state["remaining"]:
                eligibility = "insufficient_budget"
            frontier.append(dict(node=node["id"], score=node["score"],
                                 estimated_cost=node["estimated_cost"], reason=eligibility))
        selected = None
        if len(state["selections"]) - len(state["terminals"]) >= policy["max_inflight"]:
            reason = "inflight_limit"
        elif len(state["selections"]) >= policy["max_selections"]:
            reason = "selection_limit"
        elif not pending:
            reason = "frontier_exhausted"
        else:
            selected = next((nodes[n["node"]] for n in frontier if n["reason"] == "eligible"), None)
            reason = ("best_affordable_priority" if selected else "budget_exhausted"
                      if any(n["reason"] == "insufficient_budget" for n in frontier)
                      else "no_eligible_frontier")
        payload = dict(tree=tree, node=selected["id"] if selected else None,
                       protocol=selected["protocol"] if selected else None,
                       action=selected["action"] if selected else None, reason=reason,
                       reserved_cost=selected["estimated_cost"] if selected else 0,
                       remaining_before=_cost_text(state["remaining"]), frontier=frontier,
                       policy="bounded_best_first_v1", scientific_validity="not_assessed")
        id = self._write(history, "search_selection", payload)
        return dict(id=id, **payload)

    def finish_selection(self, selection: str, *, status: str, actual_cost: float,
                         reason: str, run: str | None = None, claim: str | None = None) -> str:
        """Reconcile a selection against kernel run/result records; retain real overruns.

        Only cancellation before dispatch can omit a run. Scientific outcomes come
        from a cited kernel claim and are not promoted by this scheduler. A claim
        still needs mechanical gates and independent scientific review.
        ``cost_overrun_exact`` is the exact declared decimal difference; the older
        numeric ``cost_overrun`` remains for compatibility and may round floats.
        """
        history = self._history()
        chosen = self._get(history, selection, "search_selection")["payload"]
        require(chosen["node"] is not None, "cannot finish a wait/stop decision")
        require(not any(e["kind"] == "search_terminal" and e["payload"]["selection"] == selection
                        for e in history), "selection already terminal")
        require(isinstance(status, str) and status in {"completed", "failed", "cancelled"},
                "invalid technical status")
        require(_number(actual_cost) and _text(reason), "finite cost and terminal reason required")
        require(run is not None or status == "cancelled", "completed/failed selection needs run evidence")
        outcome = "not_assessed"
        node = self._get(history, chosen["node"], "experiment_node")["payload"]
        if run is None:
            selected_seq = self._get(history, selection, "search_selection")["seq"]
            require(not any(e["kind"] == "run" and e["payload"]["protocol"] == chosen["protocol"]
                            and e["seq"] > selected_seq for e in history),
                    "cannot release a dispatched selection without terminal run evidence")
        else:
            execution = self._get(history, run, "run")
            require(execution["payload"]["protocol"] == chosen["protocol"], "run protocol mismatch")
            require(execution["seq"] > self._get(history, selection, "search_selection")["seq"],
                    "run must start after selection reservation")
            require(not any(e["kind"] == "search_terminal" and e["payload"]["run"] == run
                            for e in history), "run already assigned to another selection")
            result = next((e for e in history if e["kind"] == "result" and e["payload"]["run"] == run), None)
            require(result is not None and result["payload"]["status"] == status,
                    "terminal status must match recorded run result")
            require((execution["payload"]["replicate_of"] is not None) == (node["execution_mode"] == "replicate"),
                    "replication execution/action mismatch")
        if claim is not None:
            require(status == "completed" and node["scientific_evidence_eligible"],
                    "technical failure/debug cannot report a scientific outcome")
            conclusion = self._get(history, claim, "claim")["payload"]
            require(conclusion["protocol"] == chosen["protocol"] and run in conclusion["evidence"],
                    "claim must cite this selected run and protocol")
            outcome = conclusion["outcome"]
        return self._write(history, "search_terminal", dict(
            tree=chosen["tree"], selection=selection, node=chosen["node"], status=status,
            actual_cost=actual_cost, reserved_cost=chosen["reserved_cost"],
            cost_overrun=max(0, actual_cost - chosen["reserved_cost"]),
            cost_overrun_exact=_cost_text(max(Fraction(0),
                                              _amount(actual_cost) - _amount(chosen["reserved_cost"]))),
            reason=reason, run=run, claim=claim, scientific_outcome=outcome,
            scientific_validity="not_assessed"))
