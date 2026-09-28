"""Durable review-obligation to child-experiment transition.

This binds an open reviewer request to a new frozen protocol and search node.
It does not select, execute, replicate, resolve, or scientifically approve them.
Actor IDs and roles are declarations from a trusted local caller, not evidence
of independent reasoning or operating-system isolation.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .kernel import Actor, Kernel, finite, require
from .planning import binding_for
from .protocols import StatisticalDesign
from .replanning import _index as review_index
from .search import COMPONENTS, Search, _amount
from .store import Store, canonical


_PROTOCOL_REQUIRED = {"design", "metric", "analysis_plan", "stopping_rule", "seeds",
                      "run_limit", "implementation", "environment", "data",
                      "replication_tolerance"}
_PROTOCOL_OPTIONAL = {"statistical_design", "amendment_reason", "seen_data"}
_NODE_FIELDS = {"action", "components", "estimated_cost", "rationale"}
_SCIENTIFIC_ACTIONS = {"baseline", "discriminate", "ablate", "robustness"}
_FOLLOWUP_FIELDS = {"schema_version", "obligation", "obligation_hash", "review",
                    "review_hash", "claim", "claim_hash", "basis_hash", "tree",
                    "tree_hash", "parent_node", "parent_node_hash", "explanation_set",
                    "explanation_set_hash", "protocol", "protocol_hash", "experiment_node",
                    "experiment_node_hash", "specification", "scientific_validity"}
_COMMAND_FIELDS = {"obligation", "parent_node", "explanation_set", "protocol_spec",
                   "node_spec", "expected_basis"}


def _get(history: list[dict[str, Any]], id: str, kind: str) -> dict[str, Any]:
    return Kernel._get(history, id, kind)


def _specs(protocol_spec: Any, node_spec: Any) -> None:
    require(type(protocol_spec) is dict
            and _PROTOCOL_REQUIRED <= set(protocol_spec) <= _PROTOCOL_REQUIRED | _PROTOCOL_OPTIONAL,
            "follow-up protocol fields must match the allowed preregistration inputs")
    require(type(node_spec) is dict and set(node_spec) == _NODE_FIELDS,
            "follow-up node fields must match the scientific search proposal")
    require(type(node_spec["action"]) is str and node_spec["action"] in _SCIENTIFIC_ACTIONS,
            "follow-up requires a scientific experiment action")
    require(type(protocol_spec["seeds"]) is list and protocol_spec["seeds"]
            and all(type(seed) is int for seed in protocol_spec["seeds"])
            and len(set(protocol_spec["seeds"])) == len(protocol_spec["seeds"]),
            "follow-up needs distinct registered integer seeds")
    require(finite(node_spec["estimated_cost"]) and node_spec["estimated_cost"] > 0
            and _amount(node_spec["estimated_cost"]) == 2 * len(protocol_spec["seeds"]),
            "follow-up cost must cover every primary and reanalysis attempt")
    require(type(protocol_spec["run_limit"]) is int
            and protocol_spec["run_limit"] >= 2 * len(protocol_spec["seeds"]),
            "follow-up run limit must cover its full registered roster")
    require(type(node_spec["rationale"]) is str and bool(node_spec["rationale"].strip()),
            "follow-up needs a rationale")
    require(type(node_spec["components"]) is dict and set(node_spec["components"]) == set(COMPONENTS),
            "follow-up score components are incomplete")


def _obligations(store: Store, history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    states = review_index(store, history)
    return {event["id"]: event for state in states.values() for event in state["obligations"]}


def _question_root(history: list[dict[str, Any]], question: str) -> str:
    current = _get(history, question, "research_question")
    while current["payload"]["parent"] is not None:
        current = _get(history, current["payload"]["parent"], "research_question")
    return current["id"]


def _admit(store: Store, history: list[dict[str, Any]], *, obligation: dict[str, Any],
           parent_node: str, explanation_set: str, protocol_spec: dict[str, Any],
           node_spec: dict[str, Any], expected_basis: str) -> dict[str, Any]:
    """Check the current review, planning and search snapshot before mutation."""
    _specs(protocol_spec, node_spec)
    p = obligation["payload"]
    require(p["kind"] == "discriminating_experiment",
            "only a discriminating-experiment obligation admits this transition")
    require(type(expected_basis) is str and re.fullmatch(r"[0-9a-f]{64}", expected_basis)
            and expected_basis == p["basis_hash"], "follow-up source basis mismatch")
    source_review = _get(history, p["review"], "review")
    source_claim = _get(history, p["claim"], "claim")
    require(source_review["hash"] == p["review_hash"] and source_claim["hash"] == p["claim_hash"]
            and source_review["payload"]["claim"] == source_claim["id"]
            and source_review["payload"]["verdict"] in {"request_changes", "reject"},
            "follow-up obligation does not refer to a negative review of its claim")
    last = next((event for event in reversed(history) if event["kind"] == "review"
                 and event["actor"] == source_review["actor"]
                 and event["payload"]["claim"] == source_claim["id"]), None)
    require(last is not None and last["id"] == source_review["id"],
            "follow-up source is no longer the reviewer's latest opinion")
    reader = Kernel(store, Actor("followup-reader", "observer"))
    gate = reader._gate(history, source_claim["id"])
    basis, _ = reader._basis(history, source_claim["id"])
    require(gate["passed"] and gate["basis_hash"] == basis == expected_basis,
            "follow-up source evidence is stale or mechanically unqualified")

    source_protocol = _get(history, source_claim["payload"]["protocol"], "protocol")
    source_planning = source_protocol["payload"].get("planning")
    require(type(source_planning) is dict,
            "follow-up source claim needs a planning-bound protocol")
    binding = binding_for(history, explanation_set, current=True)
    question = _get(history, binding["question"], "research_question")
    require(binding["study_id"] == source_planning["study_id"]
            and question["payload"]["scope"] == source_claim["payload"]["scope"]
            and _question_root(history, binding["question"])
            == _question_root(history, source_planning["question"]),
            "follow-up explanation set belongs to another study or scope")

    parent = _get(history, parent_node, "experiment_node")
    require(parent["payload"]["protocol"] == source_protocol["id"]
            and parent["payload"]["scientific_evidence_eligible"] is True,
            "follow-up parent must be the reviewed scientific experiment")
    tree = _get(history, parent["payload"]["tree"], "search_tree")
    state = Search._projection(history, tree["id"])
    nodes, policy = state["nodes"], state["policy"]
    require(nodes[parent_node]["state"] == "completed",
            "follow-up parent must have a completed terminal selection")
    require(policy["cost_unit"] == "enqueued_attempt",
            "follow-up tree must budget enqueued attempts")
    require(state["remaining"] >= _amount(node_spec["estimated_cost"]),
            "follow-up does not fit the remaining attempt budget")
    require(len(nodes) < policy["max_nodes"]
            and nodes[parent_node]["depth"] + 1 <= policy["max_depth"]
            and sum(node["parent"] == parent_node for node in nodes.values()) < policy["max_width"],
            "follow-up tree child capacity is exhausted")
    for receipt in store.receipts():
        if tree["id"] in receipt["event_ids"]:
            require(receipt["context"]["study_id"] == binding["study_id"],
                    "follow-up tree creation belongs to another study")
    return dict(review=source_review, claim=source_claim, parent=parent, tree=tree,
                source_protocol=source_protocol, binding=binding)


def _expected_protocol(before: list[dict[str, Any]], protocol_spec: dict[str, Any],
                       source_protocol: str, binding: dict[str, Any]) -> dict[str, Any]:
    question = _get(before, binding["question"], "research_question")
    explanations = _get(before, binding["explanation_set"], "explanation_set")
    payload = {field: protocol_spec[field] for field in _PROTOCOL_REQUIRED}
    payload.update(hypotheses=list(explanations["payload"]["hypotheses"]),
                   scope=dict(question["payload"]["scope"]), parent=source_protocol,
                   planning=binding)
    design = protocol_spec.get("statistical_design")
    if design is not None:
        typed = StatisticalDesign.from_dict(design)
        seen = protocol_spec.get("seen_data")
        seen = [] if seen is None else seen
        require(type(seen) is list and all(type(key) is str for key in seen)
                and len(set(seen)) == len(seen), "invalid follow-up seen_data")
        payload.update(statistical_design=typed.to_dict(), protocol_mode=typed.mode,
                       amendment_reason=protocol_spec.get("amendment_reason"),
                       seen_data=sorted(set(seen) | Kernel._known_seen_data(before, source_protocol)))
    else:
        require(protocol_spec.get("amendment_reason") is None
                and protocol_spec.get("seen_data") is None,
                "untyped follow-up cannot declare amendment/exposure fields")
    return payload


def _expected_node(parent: dict[str, Any], tree: dict[str, Any], protocol: dict[str, Any],
                   spec: dict[str, Any]) -> dict[str, Any]:
    policy = tree["payload"]
    score = sum(policy["weights"][key] * spec["components"][key]
                * (-1 if key == "invalidity_risk" else 1) for key in COMPONENTS)
    score -= policy["cost_weight"] * spec["estimated_cost"]
    require(math.isfinite(score), "follow-up score overflow")
    return dict(tree=tree["id"], parent=parent["id"], protocol=protocol["id"],
                protocol_hash=protocol["hash"], action=spec["action"],
                depth=parent["payload"]["depth"] + 1, technical_attempt=0,
                components=spec["components"], estimated_cost=spec["estimated_cost"],
                rationale=spec["rationale"], score=score, execution_mode="primary",
                scientific_evidence_eligible=True)


def _payload(obligation: dict[str, Any], refs: dict[str, Any],
             explanation_set: dict[str, Any], protocol: dict[str, Any],
             node: dict[str, Any], specification: str) -> dict[str, Any]:
    p = obligation["payload"]
    return dict(schema_version=1, obligation=obligation["id"], obligation_hash=obligation["hash"],
                review=refs["review"]["id"], review_hash=refs["review"]["hash"],
                claim=refs["claim"]["id"], claim_hash=refs["claim"]["hash"],
                basis_hash=p["basis_hash"], tree=refs["tree"]["id"],
                tree_hash=refs["tree"]["hash"], parent_node=refs["parent"]["id"],
                parent_node_hash=refs["parent"]["hash"],
                explanation_set=explanation_set["id"],
                explanation_set_hash=explanation_set["hash"], protocol=protocol["id"],
                protocol_hash=protocol["hash"], experiment_node=node["id"],
                experiment_node_hash=node["hash"], specification=specification,
                scientific_validity="not_assessed")


def _index(store: Store, history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Replay-check exact source snapshots and complete atomic follow-up receipts."""
    all_followups = {event["id"]: event for event in history if event["kind"] == "replan_followup"}
    if not all_followups:
        require(not any(receipt["request"]["action"] == "followup.apply"
                        and receipt["after_revision"] <= len(history)
                        for receipt in store.receipts()),
                "follow-up command receipt lacks its binding event")
        return {}
    obligations = _obligations(store, history)
    seen_events: set[str] = set()
    by_obligation: dict[str, dict[str, Any]] = {}
    for receipt in store.receipts():
        if (receipt["request"]["action"] != "followup.apply"
                or receipt["after_revision"] > len(history)):
            continue
        request = receipt["request"]
        args = request["payload"]
        require(request["version"] == 1 and set(args) == _COMMAND_FIELDS,
                "invalid historical follow-up command arguments")
        before = history[:receipt["before_revision"]]
        events = history[receipt["before_revision"]:receipt["after_revision"]]
        require([event["kind"] for event in events] == ["protocol", "experiment_node", "replan_followup"]
                and [event["id"] for event in events] == receipt["event_ids"],
                "follow-up needs its complete original three-event command receipt")
        protocol, node, followup = events
        require(receipt["context"]["role"] == "planner"
                and all(event["actor"] == receipt["context"]["actor"]
                        and event["role"] == "planner" for event in events),
                "follow-up actor differs from its planner receipt")
        obligation = obligations.get(args["obligation"])
        require(obligation is not None and args["obligation"] not in by_obligation,
                "unknown or already bound review obligation")
        require(obligation["seq"] <= receipt["before_revision"],
                "follow-up obligation must precede its planning command")
        refs = _admit(store, before, obligation=obligation,
                      parent_node=args["parent_node"], explanation_set=args["explanation_set"],
                      protocol_spec=args["protocol_spec"], node_spec=args["node_spec"],
                      expected_basis=args["expected_basis"])
        require(receipt["context"]["study_id"] == refs["binding"]["study_id"],
                "follow-up command study differs from its planning-bound source")
        frozen = store.read(followup["payload"].get("specification"))
        expected_declaration = dict(schema_version=1, protocol_spec=args["protocol_spec"],
                                    node_spec=args["node_spec"], expected_basis=args["expected_basis"])
        require(frozen == canonical(expected_declaration),
                "follow-up specification differs from the original command")
        expected_protocol = _expected_protocol(before, args["protocol_spec"],
                                               refs["source_protocol"]["id"], refs["binding"])
        require(protocol["payload"] == expected_protocol,
                "follow-up protocol differs from its frozen specification")
        require(node["payload"] == _expected_node(refs["parent"], refs["tree"], protocol,
                                                  args["node_spec"]),
                "follow-up node differs from its frozen scientific proposal")
        explanation = _get(before, args["explanation_set"], "explanation_set")
        require(set(followup["payload"]) == _FOLLOWUP_FIELDS
                and followup["payload"] == _payload(obligation, refs, explanation,
                                                    protocol, node, followup["payload"]["specification"]),
                "follow-up binding differs from its frozen references")
        require(receipt["result"] == followup["id"],
                "follow-up receipt result differs from its binding event")
        seen_events.add(followup["id"])
        by_obligation[obligation["id"]] = dict(followup=followup, protocol=protocol, node=node)
    require(seen_events == set(all_followups),
            "follow-up binding lacks its complete original command receipt")
    return by_obligation


def followup_state(store: Store, obligation: str) -> dict[str, Any]:
    """Report the plan and any currently effective reviewer resolution separately."""
    history = store.events()
    obligations = _obligations(store, history)
    require(obligation in obligations, "unknown review obligation")
    state = _index(store, history).get(obligation)
    from .resolution import resolution_states
    resolution = resolution_states(store, history).get(obligation)
    return dict(obligation=obligation, revision=len(history),
                status="planned" if state else "open",
                followup=state["followup"]["id"] if state else None,
                protocol=state["protocol"]["id"] if state else None,
                experiment_node=state["node"]["id"] if state else None,
                obligation_resolution=resolution["status"] if resolution else "open",
                resolution=resolution["resolution"]["id"] if resolution else None,
                scientific_validity="not_assessed")


def followup_artifacts(event: dict[str, Any]) -> set[str]:
    return {event["payload"]["specification"]} if event["kind"] == "replan_followup" else set()


class Followup:
    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def apply(self, *, obligation: str, parent_node: str, explanation_set: str,
              protocol_spec: dict[str, Any], node_spec: dict[str, Any],
              expected_basis: str) -> str:
        """Append protocol, child node, and obligation binding atomically."""
        require(self.store._command_context is not None and self.actor.role == "planner",
                "follow-up application requires a planner CommandService transaction")
        history = self.store.events()
        bound = _index(self.store, history)
        obligations = _obligations(self.store, history)
        require(obligation in obligations and obligation not in bound,
                "unknown or already bound review obligation")
        source = obligations[obligation]
        refs = _admit(self.store, history, obligation=source,
                      parent_node=parent_node, explanation_set=explanation_set,
                      protocol_spec=protocol_spec, node_spec=node_spec,
                      expected_basis=expected_basis)
        require(self.store._command_context["study_id"] == refs["binding"]["study_id"],
                "follow-up command study differs from its planning-bound source")
        specification = self.store.put_json(dict(schema_version=1,
            protocol_spec=protocol_spec, node_spec=node_spec, expected_basis=expected_basis))
        kernel = Kernel(self.store, self.actor)
        protocol = kernel.preregister_for_set(explanation_set=explanation_set,
            parent=refs["source_protocol"]["id"], **protocol_spec)
        node = Search(self.store, self.actor).add_node(refs["tree"]["id"], protocol=protocol,
            parent=parent_node, **node_spec)
        created = self.store.events()
        explanation = _get(history, explanation_set, "explanation_set")
        payload = _payload(source, refs, explanation,
                           _get(created, protocol, "protocol"),
                           _get(created, node, "experiment_node"), specification)
        return kernel._write(created, "replan_followup", payload, {"planner"})
