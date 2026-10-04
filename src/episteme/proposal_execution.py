"""Admit a model-proposed experiment to a frozen execution batch.

This is a planner transition only. It reserves a bounded roster and never
dispatches a worker, evaluates a finding, or asserts scientific independence.
Schema 2 keeps the frozen compilation. Schema 3 uses the pinned pack plan.
The originating model application and its compiled sources remain immutable.
"""

from __future__ import annotations

from typing import Any

from .agents import _index as agent_index
from .batch import Batch
from .execution import _object
from .kernel import Actor, Kernel, require
from .planning import binding_for
from .search import Search
from .store import Store


# Signature defaults of proposal.prepare_next. Schema 2 still uses the caller's
# limits. Schema 3 uses the pinned pack plan; these defaults are not an override.
_SCHEMA2_LIMITS = dict(wall_seconds=120, max_output_bytes=1048576, required_capabilities=[])


def _limits(wall_seconds: int, max_output_bytes: int,
            required_capabilities: list[str] | None) -> dict[str, Any]:
    return dict(wall_seconds=wall_seconds, max_output_bytes=max_output_bytes,
                required_capabilities=[] if required_capabilities is None else list(required_capabilities))


def _origin(store: Store, history: list[dict[str, Any]],
            selected: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Resolve the exact applied proposal against the pre-selection snapshot.

    Schema 2 keeps the frozen compilation manifest. Schema 3 reads the pinned
    pack execution plan from CAS and does not import pack code.
    """
    applications = agent_index(store, history)
    choice = selected["payload"]
    require(choice["node"] is not None, "no experiment node can be batched")
    matches = [state for state in applications.values()
               if state["application"] is not None
               and state["request"]["payload"]["schema_version"] in {2, 3}
               and state["application"]["payload"]["experiment_node"] == choice["node"]]
    require(len(matches) == 1,
            "selected node lacks a unique applied experiment proposal")
    state = matches[0]
    request = state["request"]["payload"]
    applied = state["application"]["payload"]
    require(request["tree"] == choice["tree"]
            and applied["protocol"] == choice["protocol"],
            "selected node differs from its applied experiment proposal")
    binding = binding_for(history, request["explanation_set"], current=True)
    protocol = Kernel._get(history, applied["protocol"], "protocol")
    require(protocol["payload"]["planning"] == binding,
            "selected protocol has a stale planning binding")
    if request["schema_version"] == 2:
        manifest = _object(store.read(applied["compilation"]))
        # agent_index validates the full frozen application/manifest against its
        # original response and receipt without recompiling with today's adapter.
        return binding, protocol, dict(
            schema_version=2,
            reanalysis_implementation=manifest["sources"]["reanalysis_implementation"],
            reanalysis_environment=protocol["payload"]["environment"],
            outputs=manifest["compiled"]["outputs"])
    from .domain_packs import execution_fields
    pinned = Kernel._get(history, applied["pack_binding"], "pack_binding")
    require(pinned["payload"]["protocol"] == protocol["id"],
            "pack proposal binding refers to another protocol")
    return binding, protocol, dict(schema_version=3, pack_binding=pinned["id"],
                                   **execution_fields(store, pinned))


def validate_prepared(store: Store, history_before_selection: list[dict[str, Any]],
                      selection: dict[str, Any], plan: dict[str, Any]) -> None:
    """Replay a prepared pair using its original prefix and immutable sources.

    The caller verifies that the complete original receipt has action
    ``proposal.prepare_next`` and exactly these two event IDs. This helper is
    also safe before that receipt exists, inside the new command transaction.
    It deliberately does not inspect today's compiler, current environment
    fingerprint, execution marker, or later scientific/planning revisions.
    """
    require(selection["kind"] == "search_selection" and plan["kind"] == "batch_plan"
            and selection["seq"] == len(history_before_selection) + 1
            and plan["seq"] == selection["seq"] + 1
            and plan["previous_hash"] == selection["hash"]
            and (selection["actor"], selection["role"]) == (plan["actor"], "planner"),
            "prepared selection and batch must be consecutive planner events")
    binding, protocol, recipe = _origin(store, history_before_selection, selection)
    choice, p = selection["payload"], plan["payload"]
    require(p["selection"] == selection["id"] and p["node"] == choice["node"]
            and p["protocol"] == protocol["id"] and p["tree"] == choice["tree"],
            "prepared batch differs from its selected model proposal")
    require(p["reanalysis_implementation"] == recipe["reanalysis_implementation"]
            and p["reanalysis_environment"] == recipe["reanalysis_environment"]
            and p["outputs"] == recipe["outputs"],
            "prepared batch differs from frozen proposal compilation")
    if recipe["schema_version"] == 3:
        require(p["wall_seconds"] == recipe["wall_seconds"]
                and p["max_output_bytes"] == recipe["max_output_bytes"]
                and p["required_capabilities"] == recipe["required_capabilities"],
                "prepared batch differs from the pinned pack execution plan")
    receipt = next((item for item in store.receipts()
                    if item["event_ids"] == [selection["id"], plan["id"]]), None)
    study = (receipt["context"]["study_id"] if receipt is not None
             else store._command_context["study_id"] if store._command_context is not None
             else None)
    require(study == binding["study_id"],
            "prepared batch study differs from the model proposal")


class ProposalExecution:
    """One receipt for search selection and batch reservation.

    A wait/stop decision is rolled back: callers must use ``search.select_next``
    explicitly if they want to retain such a decision without a batch. A manual
    node winning the current search priority also fails closed; this bridge does
    not silently skip it in favor of a lower-ranked model proposal.
    """

    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def prepare_next(self, *, tree: str, executor: str, replicator: str,
                     wall_seconds: int = 120, max_output_bytes: int = 1048576,
                     required_capabilities: list[str] | None = None) -> str:
        require(self.store._command_context is not None and self.actor.role == "planner",
                "proposal preparation requires a planner CommandService transaction")
        history = self.store.events()
        selected = Search(self.store, self.actor).select_next(tree)
        require(selected["node"] is not None,
                f"no experiment node can be batched: {selected['reason']}")
        selection = Kernel._get(self.store.events(), selected["id"], "search_selection")
        binding, protocol, recipe = _origin(self.store, history, selection)
        require(binding["study_id"] == self.store._command_context["study_id"],
                "selected proposal belongs to another study")
        limits = _limits(wall_seconds, max_output_bytes, required_capabilities)
        passed_capabilities = required_capabilities
        if recipe["schema_version"] == 3:
            pinned = {key: recipe[key] for key in _SCHEMA2_LIMITS}
            require(limits == _SCHEMA2_LIMITS or limits == pinned,
                    "schema 3 batch limits differ from the pinned pack execution plan")
            wall_seconds = pinned["wall_seconds"]
            max_output_bytes = pinned["max_output_bytes"]
            passed_capabilities = list(pinned["required_capabilities"])
        # The batch gate rechecks source bytes, declared outputs, environment
        # format and available platform capabilities. It does not prove the
        # frozen environment fingerprint is reproducible on this host.
        batch = Batch(self.store, self.actor).plan(
            selection=selected["id"], executor=executor, replicator=replicator,
            reanalysis_implementation=recipe["reanalysis_implementation"],
            reanalysis_environment=recipe["reanalysis_environment"],
            outputs=recipe["outputs"], wall_seconds=wall_seconds,
            max_output_bytes=max_output_bytes, required_capabilities=passed_capabilities)
        validate_prepared(self.store, history, selection,
                          Kernel._get(self.store.events(), batch, "batch_plan"))
        return batch
