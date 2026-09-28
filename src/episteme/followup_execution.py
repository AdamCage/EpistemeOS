"""Reserve a reviewed follow-up's winning search node as a frozen batch.

This planner transition does not dispatch a worker, satisfy the review
obligation, or establish scientific independence of either assigned actor.
"""

from __future__ import annotations

from typing import Any

from .batch import Batch
from .followup import _index as followup_index
from .kernel import Actor, Kernel, require
from .planning import binding_for
from .search import Search
from .store import Store


_REQUEST_FIELDS = {"obligation", "executor", "replicator", "reanalysis_implementation",
                   "reanalysis_environment", "outputs", "wall_seconds", "max_output_bytes",
                   "required_capabilities"}
_RECIPE_FIELDS = {"executor", "replicator", "reanalysis_implementation",
                  "reanalysis_environment", "outputs", "wall_seconds", "max_output_bytes"}


def _origin(store: Store, history: list[dict[str, Any]],
            obligation: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Check the persisted plan and the review/planning basis at this prefix."""
    bound = followup_index(store, history).get(obligation)
    require(bound is not None, "obligation lacks a recorded follow-up plan")
    followup = bound["followup"]
    p = followup["payload"]
    review = Kernel._get(history, p["review"], "review")
    claim = Kernel._get(history, p["claim"], "claim")
    latest = next((event for event in reversed(history) if event["kind"] == "review"
                   and event["actor"] == review["actor"]
                   and event["payload"]["claim"] == claim["id"]), None)
    require(latest is not None and latest["id"] == review["id"],
            "follow-up source is no longer the reviewer's latest opinion")
    reader = Kernel(store, Actor("followup-preparation-reader", "observer"))
    gate = reader._gate(history, claim["id"])
    basis, _ = reader._basis(history, claim["id"])
    require(gate["passed"] and gate["basis_hash"] == basis == p["basis_hash"],
            "follow-up source evidence is stale or mechanically unqualified")
    binding = binding_for(history, p["explanation_set"], current=True)
    require(bound["protocol"]["payload"]["planning"] == binding,
            "follow-up protocol has a stale planning binding")
    return bound, binding


def validate_prepared(store: Store, history_before_selection: list[dict[str, Any]],
                      selection: dict[str, Any], plan: dict[str, Any],
                      request: dict[str, Any], study_id: str) -> None:
    """Replay the exact paired transition against its historical input prefix.

    The caller checks the complete command receipt and supplies its original
    normalized request and study. No later review or adapter revision can
    rewrite the historical decision; any new dispatch needs its own gate.
    """
    require(set(request) == _REQUEST_FIELDS, "invalid follow-up preparation request")
    require(selection["kind"] == "search_selection" and plan["kind"] == "batch_plan"
            and selection["seq"] == len(history_before_selection) + 1
            and plan["seq"] == selection["seq"] + 1
            and plan["previous_hash"] == selection["hash"]
            and (selection["actor"], selection["role"]) == (plan["actor"], "planner"),
            "prepared follow-up selection and batch must be consecutive planner events")
    bound, binding = _origin(store, history_before_selection, request["obligation"])
    node, followup = bound["node"], bound["followup"]
    p = plan["payload"]
    require(selection["payload"] == Search._decision(history_before_selection,
                                                     followup["payload"]["tree"]),
            "prepared follow-up selection differs from the search policy decision")
    require(selection["payload"]["node"] == node["id"]
            and p["selection"] == selection["id"]
            and p["node"] == node["id"]
            and p["protocol"] == bound["protocol"]["id"]
            and p["tree"] == followup["payload"]["tree"],
            "prepared batch differs from its winning follow-up node")
    require(all(p[field] == request[field] for field in _RECIPE_FIELDS)
            and p["required_capabilities"] == (request["required_capabilities"] or []),
            "prepared batch differs from the requested frozen execution recipe")
    require(study_id == binding["study_id"],
            "prepared follow-up batch belongs to another study")


def require_current_for_batch(store: Store, history: list[dict[str, Any]],
                              plan: dict[str, Any]) -> None:
    """Fence new enqueue/dispatch after a source review or planning revision.

    Existing event and receipt history remains valid. A previously dispatched
    worker is reconciled through its recorded dispatch, not re-authorized here.
    """
    receipt = next((item for item in store.receipts()
                    if plan["id"] in item["event_ids"]), None)
    require(receipt is not None, "batch plan lacks its original command receipt")
    if receipt["request"]["action"] != "followup.prepare_next":
        return
    bound, binding = _origin(store, history, receipt["request"]["payload"]["obligation"])
    p = plan["payload"]
    require(p["node"] == bound["node"]["id"]
            and p["protocol"] == bound["protocol"]["id"]
            and p["tree"] == bound["followup"]["payload"]["tree"]
            and receipt["context"]["study_id"] == binding["study_id"],
            "batch no longer matches the obligated follow-up plan")


class FollowupExecution:
    """Select and batch one recorded follow-up in a single command receipt."""

    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def prepare_next(self, *, obligation: str, executor: str, replicator: str,
                     reanalysis_implementation: str, reanalysis_environment: str,
                     outputs: dict[str, str], wall_seconds: int = 120,
                     max_output_bytes: int = 1048576,
                     required_capabilities: list[str] | None = None) -> str:
        require(self.store._command_context is not None and self.actor.role == "planner",
                "follow-up preparation requires a planner CommandService transaction")
        history = self.store.events()
        bound, binding = _origin(self.store, history, obligation)
        require(binding["study_id"] == self.store._command_context["study_id"],
                "follow-up preparation command belongs to another study")
        selected = Search(self.store, self.actor).select_next(bound["followup"]["payload"]["tree"])
        require(selected["node"] == bound["node"]["id"],
                "current search winner is not the obligated follow-up node")
        selection = Kernel._get(self.store.events(), selected["id"], "search_selection")
        batch = Batch(self.store, self.actor).plan(
            selection=selected["id"], executor=executor, replicator=replicator,
            reanalysis_implementation=reanalysis_implementation,
            reanalysis_environment=reanalysis_environment, outputs=outputs,
            wall_seconds=wall_seconds, max_output_bytes=max_output_bytes,
            required_capabilities=required_capabilities)
        request = dict(obligation=obligation, executor=executor, replicator=replicator,
                       reanalysis_implementation=reanalysis_implementation,
                       reanalysis_environment=reanalysis_environment, outputs=outputs,
                       wall_seconds=wall_seconds, max_output_bytes=max_output_bytes,
                       required_capabilities=required_capabilities)
        validate_prepared(self.store, history, selection,
                          Kernel._get(self.store.events(), batch, "batch_plan"),
                          request, binding["study_id"])
        return batch
