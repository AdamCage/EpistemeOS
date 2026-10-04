"""Receipt-backed admission of one frozen analysis proposal for a completed batch.

The adapter is trusted local code. Since ADR 0018 the command recomputes the
proposal with the registered adapter on the same snapshot and admits only an
identical one; this shows which code produced the claim, not that it is right.
Its report is provenance for a bounded claim, not a scientific verdict. A
separate planner command assigns review.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .agents import _index as agent_index
from .batch import _index as batch_index
from .domain_binding import _index as binding_index
from .kernel import Actor, Kernel, independent_of, require
from .store import Store, canonical, digest


KIND = "batch_analysis"
_PROPOSAL_FIELDS = {"schema_version", "adapter_id", "adapter_version", "statement",
                    "limitations", "outcome", "inference_mode", "details"}
_EVENT_FIELDS = {"schema_version", "batch", "batch_hash", "settlement", "settlement_hash",
                 "terminal", "terminal_hash", "protocol", "protocol_hash", "claim", "claim_hash",
                 "proposal_digest", "adapter_id", "adapter_version", "adapter_source_digest",
                 "task_id", "reviewer_actor", "runs", "results", "scientific_validity"}
_REQUEST_FIELDS = {"batch", "expected_settlement", "proposal", "adapter_source_digest",
                   "reviewer_actor"}
# Schema 2 records that the command recomputed the proposal; schema 1 did not.
PROPOSAL_ORIGIN = "recomputed_by_registered_adapter_at_admission"
UNVERIFIED_ORIGIN = "caller_submitted_unverified"


def analysis_provenance(event: dict[str, Any]) -> str:
    """How the admitted proposal was established; historical schema 1 was never recomputed."""
    return event["payload"].get("proposal_origin", UNVERIFIED_ORIGIN)


def _proposal(value: Any) -> dict[str, Any]:
    require(type(value) is dict and set(value) == _PROPOSAL_FIELDS,
            "invalid analysis proposal fields")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1,
            "unsupported analysis proposal schema")
    require(type(value["adapter_id"]) is str and re.fullmatch(r"[a-z][a-z0-9_]{1,63}", value["adapter_id"]),
            "invalid analysis adapter ID")
    require(type(value["adapter_version"]) is str and 0 < len(value["adapter_version"]) <= 64
            and value["adapter_version"].strip() == value["adapter_version"],
            "invalid analysis adapter version")
    require(type(value["statement"]) is str and 0 < len(value["statement"].strip()) <= 4096,
            "analysis statement is required and bounded")
    require(type(value["limitations"]) is list and 0 < len(value["limitations"]) <= 32
            and all(type(item) is str and 0 < len(item.strip()) <= 1024 for item in value["limitations"]),
            "analysis limitations are required and bounded")
    require(type(value["outcome"]) is str
            and value["outcome"] in {"supports", "refutes", "inconclusive"},
            "invalid analysis outcome")
    require(type(value["inference_mode"]) is str
            and value["inference_mode"] in {"unclassified", "descriptive", "exploratory", "confirmatory"},
            "invalid analysis inference mode")
    require(type(value["details"]) is dict and len(canonical(value)) <= 1024 * 1024,
            "analysis proposal details must be a bounded JSON object")
    return value


def _origin(store: Store, history: list[dict[str, Any]], plan: dict[str, Any],
            adapter_id: str, adapter_version: str, adapter_source_digest: str,
            study_id: str) -> None:
    """Require exactly one replay-verified domain recipe source for this batch."""
    protocol = plan["payload"]["protocol"]
    applications = agent_index(store, history)
    matches = [state["application"] for state in applications.values()
               if state["application"] is not None
               and state["application"]["payload"].get("protocol") == protocol]
    binding = binding_index(store, history).get(protocol)
    require(len(matches) + (binding is not None) == 1,
            "analysis requires exactly one frozen applied or manually bound domain recipe")
    if matches:
        manifest = store.read(matches[0]["payload"]["compilation"])
        compiled = json.loads(manifest)["compiled"]
        require(compiled["domain"] == adapter_id,
                "analysis adapter differs from frozen model domain")
        return
    require(binding is not None and binding["seq"] < plan["seq"],
            "manual domain binding must precede the batch plan")
    p, batch = binding["payload"], plan["payload"]
    protocol_event = Kernel._get(history, protocol, "protocol")
    require(p["study_id"] == study_id and p["protocol_hash"] == protocol_event["hash"],
            "manual domain binding differs from the analysis study or protocol")
    require((p["adapter_id"], p["adapter_version"], p["adapter_source_digest"])
            == (adapter_id, adapter_version, adapter_source_digest),
            "analysis adapter identity or source differs from frozen domain binding")
    require(all(batch[field] == p[field] for field in
                ("reanalysis_implementation", "reanalysis_environment", "outputs",
                 "wall_seconds", "max_output_bytes", "required_capabilities")),
            "batch execution recipe differs from frozen domain binding")
    require(all(protocol_event["payload"][field] == p[target] for field, target in
                (("implementation", "primary_implementation"),
                 ("environment", "primary_environment"), ("data", "data"))),
            "primary execution source differs from frozen domain binding")


def _context(store: Store, history: list[dict[str, Any]], *, batch: str,
             expected_settlement: str, proposal: dict[str, Any],
             adapter_source_digest: str, reviewer_actor: str,
             study_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, str]:
    states = batch_index(store, history)
    require(batch in states, "unknown batch")
    state = states[batch]
    plan, settlement, terminal = state["plan"], state["settlement"], state["terminal"]
    require(settlement is not None and terminal is not None
            and settlement["id"] == expected_settlement
            and settlement["payload"]["status"] == "completed"
            and settlement["payload"]["next_action"] == "awaiting_analysis"
            and terminal["payload"]["status"] == "completed",
            "analysis requires an exactly matched completed batch settlement")
    originals = [r for r in store.receipts() if batch in r["event_ids"]]
    require(len(originals) == 1 and originals[0]["context"]["study_id"] == study_id,
            "analysis study differs from batch planning study")
    p = _proposal(proposal)
    protocol = plan["payload"]["protocol"]
    _origin(store, history, plan, p["adapter_id"], p["adapter_version"],
            adapter_source_digest, study_id)
    require(type(adapter_source_digest) is str and re.fullmatch(r"[0-9a-f]{64}", adapter_source_digest),
            "invalid analysis adapter source digest")
    require(0 < len(store.read(adapter_source_digest)) <= 1024 * 1024,
            "analysis adapter source must be a bounded frozen artifact")
    require(type(reviewer_actor) is str and bool(reviewer_actor.strip()) and len(reviewer_actor) <= 128,
            "analysis reviewer actor is required")
    runs, results = settlement["payload"]["runs"], settlement["payload"]["results"]
    require(len(runs) == len(results) == len(plan["payload"]["slots"])
            and all(row["status"] == "completed" for row in settlement["payload"]["slots"]),
            "analysis requires the entire completed batch roster")
    proposal_digest = digest(canonical(p))
    observed_artifacts = {key for result_id in results
                          for key in Kernel._get(history, result_id, "result")["payload"]["outputs"].values()}
    require(not {proposal_digest, adapter_source_digest} & observed_artifacts,
            "analysis proposal/source cannot alias observed output artifacts")
    binding = binding_index(store, history).get(protocol)
    if binding is not None:
        require(binding["payload"]["recipe_digest"] not in observed_artifacts,
                "domain recipe cannot alias observed output artifacts")
    task_id = digest(canonical(dict(batch=batch, settlement=expected_settlement,
                                    adapter_id=p["adapter_id"], adapter_version=p["adapter_version"],
                                    adapter_source_digest=adapter_source_digest,
                                    proposal_digest=proposal_digest)))
    return state, plan, settlement, proposal_digest, task_id


def _payload(state: dict[str, Any], claim: dict[str, Any], proposal: dict[str, Any],
             proposal_digest: str, adapter_source_digest: str, task_id: str,
             reviewer_actor: str, *, schema_version: int = 2) -> dict[str, Any]:
    plan, settlement, terminal = state["plan"], state["settlement"], state["terminal"]
    origin = {} if schema_version == 1 else dict(proposal_origin=PROPOSAL_ORIGIN)
    return dict(schema_version=schema_version, **origin, batch=plan["id"], batch_hash=plan["hash"],
                settlement=settlement["id"], settlement_hash=settlement["hash"],
                terminal=terminal["id"], terminal_hash=terminal["hash"],
                protocol=plan["payload"]["protocol"], protocol_hash=plan["payload"]["protocol_hash"],
                claim=claim["id"], claim_hash=claim["hash"], proposal_digest=proposal_digest,
                adapter_id=proposal["adapter_id"], adapter_version=proposal["adapter_version"],
                adapter_source_digest=adapter_source_digest, task_id=task_id,
                reviewer_actor=reviewer_actor, runs=settlement["payload"]["runs"],
                results=settlement["payload"]["results"], scientific_validity="not_assessed")


def _claim_payload(plan: dict[str, Any], proposal: dict[str, Any], runs: list[str]) -> dict[str, Any]:
    result = dict(protocol=plan["id"], statement=proposal["statement"],
                  scope=plan["payload"]["scope"], evidence=runs,
                  limitations=proposal["limitations"], outcome=proposal["outcome"])
    result["inference_mode"] = proposal["inference_mode"]
    return result


def _index(store: Store, history: list[dict[str, Any]], *,
           receipts: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Recompute each committed admission on its historical prefix."""
    events = {e["id"]: e for e in history if e["kind"] == KIND}
    relevant = [r for r in (store.receipts() if receipts is None else receipts)
                if r["request"]["action"] == "analysis.apply" and r["after_revision"] <= len(history)]
    if not events and not relevant:
        return {}
    seen: set[str] = set()
    tasks: set[str] = set()
    for receipt in relevant:
        context, request = receipt["context"], receipt["request"]
        require(context["role"] == "analyst" and request["version"] == 1
                and set(request["payload"]) == _REQUEST_FIELDS,
                "invalid historical analysis command")
        before = history[:receipt["before_revision"]]
        created = history[receipt["before_revision"]:receipt["after_revision"]]
        require(len(created) == 2 and [e["kind"] for e in created] == ["claim", KIND]
                and receipt["event_ids"] == [e["id"] for e in created],
                "analysis requires its exact claim and provenance receipt")
        claim, analysis = created
        require((claim["actor"], claim["role"]) == (context["actor"], "analyst")
                and (analysis["actor"], analysis["role"]) == (context["actor"], "analyst"),
                "analysis actor differs from command")
        args = request["payload"]
        state, _, _, proposal_digest, task_id = _context(
            store, before, study_id=context["study_id"], **args)
        proposal = _proposal(args["proposal"])
        protocol = Kernel._get(before, state["plan"]["payload"]["protocol"], "protocol")
        require(claim["payload"] == _claim_payload(protocol, proposal, state["settlement"]["payload"]["runs"]),
                "analysis claim differs from frozen proposal and full roster")
        # Schema 1 events predate recomputation at admission and stay valid
        # history; replay checks structure only and never reruns adapter code.
        version = analysis["payload"].get("schema_version")
        require(version in (1, 2), "unsupported batch analysis schema")
        expected = _payload(state, claim, proposal, proposal_digest,
                            args["adapter_source_digest"], task_id, args["reviewer_actor"],
                            schema_version=version)
        fields = _EVENT_FIELDS if version == 1 else _EVENT_FIELDS | {"proposal_origin"}
        require(set(analysis["payload"]) == fields and analysis["payload"] == expected,
                "analysis event differs from frozen evidence")
        require(store.read(proposal_digest) == canonical(proposal),
                "analysis proposal artifact differs from original command")
        gate = Kernel(store, Actor("analysis-validator", "observer"))._gate(
            history[:receipt["after_revision"]], claim["id"])
        require(gate["passed"], "historical analysis mechanical gate failed")
        contributors = Kernel(store, Actor("analysis-validator", "observer"))._review_members(
            history[:receipt["after_revision"]], claim["id"])[1]
        require(args["reviewer_actor"] not in contributors,
                "historical analysis reviewer contributed to evidence")
        require(receipt["result"] == dict(analysis=analysis["id"], claim=claim["id"],
                                          basis_hash=gate["basis_hash"], proposal_digest=proposal_digest,
                                          task_id=task_id, scientific_validity="not_assessed"),
                "analysis receipt result differs from historical gate")
        require(task_id not in tasks, "duplicate historical analysis task")
        tasks.add(task_id)
        seen.add(analysis["id"])
    require(seen == set(events), "batch analysis lacks its original command receipt")
    return {id: events[id] for id in seen}


class BatchAnalysis:
    """Admit a bounded analyst claim; mechanical passage is not scientific review."""

    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def apply(self, *, batch: str, expected_settlement: str, proposal: dict[str, Any],
              adapter_source_digest: str, reviewer_actor: str) -> dict[str, str]:
        require(self.store._command_context is not None and self.actor.role == "analyst",
                "analysis requires an analyst CommandService transaction")
        history = self.store.events()
        prior = _index(self.store, history)
        study_id = self.store._command_context["study_id"]
        state, _, _, proposal_digest, task_id = _context(
            self.store, history, batch=batch, expected_settlement=expected_settlement,
            proposal=proposal, adapter_source_digest=adapter_source_digest,
            reviewer_actor=reviewer_actor, study_id=study_id)
        require(not any(e["payload"]["task_id"] == task_id for e in prior.values()),
                "analysis task already applied")
        protocol = Kernel._get(history, state["plan"]["payload"]["protocol"], "protocol")
        from .domain_packs import pack_lineage
        require(pack_lineage(history, protocol["id"]) is None,
                "a pack-bound protocol lineage admits analyses only through pack.analyse")
        validated = _proposal(proposal)
        # ADR 0018: admit only what the registered adapter computes on this snapshot,
        # as pack.analyse does for pinned pack hooks.
        from .domains.registry import legacy_analysis_adapter
        adapter, source = legacy_analysis_adapter(validated["adapter_id"])
        require((adapter.adapter_id, adapter.adapter_version)
                == (validated["adapter_id"], validated["adapter_version"]),
                "analysis adapter identity differs from the registered adapter")
        require(digest(source) == adapter_source_digest,
                "analysis adapter source differs from the registered adapter module")
        recomputed = _proposal(adapter.propose(self.store, state))
        require(canonical(recomputed) == canonical(validated),
                "submitted proposal differs from what the registered adapter computes on this snapshot")
        require(self.store.put_json(validated) == proposal_digest,
                "analysis proposal digest mismatch")
        kernel = Kernel(self.store, self.actor)
        claim_id = kernel.claim(protocol=protocol["id"], statement=validated["statement"],
            scope=protocol["payload"]["scope"], evidence=state["settlement"]["payload"]["runs"],
            limitations=validated["limitations"], outcome=validated["outcome"],
            inference_mode=validated["inference_mode"])
        claim = Kernel._get(self.store.events(), claim_id, "claim")
        analysis_id = kernel._write(self.store.events(), KIND,
            _payload(state, claim, validated, proposal_digest, adapter_source_digest,
                     task_id, reviewer_actor), {"analyst"})
        updated = self.store.events()
        gate = kernel._gate(updated, claim_id)
        require(gate["passed"], "analysis mechanical gate failed: " + "; ".join(gate["failures"]))
        contributors = kernel._review_members(updated, claim_id)[1]
        require(independent_of(reviewer_actor, contributors), "analysis reviewer contributed to evidence")
        return dict(analysis=analysis_id, claim=claim_id, basis_hash=gate["basis_hash"],
                    proposal_digest=proposal_digest, task_id=task_id,
                    scientific_validity="not_assessed")
