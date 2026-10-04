"""Pack-scoped model proposals. The kernel admits the transition.

A pack may declare a versioned parameter schema and an attempt bound, then
compile an accepted proposal into a protocol draft and an execution plan.
This module freezes that schema before the model call, keeps host inputs out
of the model context, and asks the kernel to record the protocol, pack binding
and search node. The pack does not choose the actor, raise claim strength,
write a review or certify independence. ``scientific_validity`` stays
``not_assessed``. Replay reads the pinned bytes and does not import pack code.
"""

from __future__ import annotations

import re
from typing import Any

from . import domain_packs, experiment_proposals_v2 as proposals
from .domains import api, registry
from .execution import BACKEND, _object
from .kernel import Kernel, require
from .planning import binding_for, planning_context
from .runner_backend import fingerprint
from .search import Search
from .store import Store, canonical


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PACK_ID = re.compile(r"[a-z][a-z0-9_]{1,63}\Z")
_FIELDS = {"schema_version", "pack_id", "pack_version", "pack_code_digest", "catalog",
           "proposal_schema", "host_inputs", "capture", "environment", "planned_attempts"}


def _schema_of(loaded: registry.LoadedPack, catalog: api.ParameterCatalog) -> dict[str, Any]:
    function = getattr(loaded.module, "proposal_schema", None)
    require(callable(function), "pack does not declare a model proposal schema")
    try:
        declared = function()
    except registry.PackError:
        raise
    except Exception as exc:
        raise registry.PackError(
            f"pack {loaded.pack_id} hook proposal_schema failed: {exc}") from exc
    require(type(declared) is dict and set(declared) == {"schema_version", "parameters_schema"}
            and type(declared["schema_version"]) is int and declared["schema_version"] == 1,
            "invalid pack proposal schema")
    parameters = api.thaw(declared["parameters_schema"])
    require(parameters == api.thaw(catalog.parameters_schema),
            "proposal schema differs from the parameter catalog")
    return {"schema_version": 1, "parameters_schema": parameters}


def _attempts(loaded: registry.LoadedPack, host_inputs: Any, capture: api.CaptureBundle | None) -> int:
    function = getattr(loaded.module, "proposal_attempts", None)
    require(callable(function), "pack does not declare a model proposal attempt bound")
    try:
        attempts = function(api.thaw(host_inputs), capture)
    except registry.PackError:
        raise
    except Exception as exc:
        raise registry.PackError(
            f"pack {loaded.pack_id} hook proposal_attempts failed: {exc}") from exc
    require(type(attempts) is int and 2 <= attempts <= 4096,
            "pack proposal attempt bound must be an integer from 2 to 4096")
    return attempts


def _bundle(store: Store, key: str | None, *, pack_id: str, pack_version: str,
            require_capture: bool) -> api.CaptureBundle | None:
    if key is None:
        require(not require_capture, "pack proposal requires a capture bundle")
        return None
    require(type(key) is str and _DIGEST.fullmatch(key) is not None, "invalid proposal capture digest")
    bundle = api.CaptureBundle.from_frozen(_object(store.read(key)), store.read)
    require(bundle.digest() == key and (bundle.pack_id, bundle.pack_version) == (pack_id, pack_version),
            "capture bundle was made by another pack or version")
    return bundle


def _environment(store: Store, key: str, *, live: bool) -> None:
    environment = _object(store.read(key))
    require(set(environment) == {"schema_version", "backend", "fingerprint"}
            and type(environment["schema_version"]) is int and environment["schema_version"] == 1
            and environment["backend"] == BACKEND and type(environment["fingerprint"]) is dict,
            "experiment requires a frozen local Python environment")
    if live:
        require(environment["fingerprint"] == fingerprint(),
                "experiment environment no longer matches this local runner")


def load_proposal_binding(store: Store, key: str, *, live: bool) -> dict[str, Any]:
    """Read a pre-model pack binding. ``live`` re-runs the pack declaration."""
    binding = _object(store.read(key))
    require(type(binding) is dict and set(binding) == _FIELDS
            and type(binding["schema_version"]) is int and binding["schema_version"] == 1
            and type(binding["pack_id"]) is str and _PACK_ID.fullmatch(binding["pack_id"]) is not None
            and type(binding["pack_version"]) is str and binding["pack_version"].strip() == binding["pack_version"]
            and binding["pack_version"]
            and type(binding["pack_code_digest"]) is str
            and _DIGEST.fullmatch(binding["pack_code_digest"]) is not None,
            "invalid frozen pack proposal binding")
    catalog = api.ParameterCatalog.from_dict(_object(store.read(binding["catalog"])))
    require(catalog.digest() == binding["catalog"]
            and (catalog.pack_id, catalog.pack_version) == (binding["pack_id"], binding["pack_version"]),
            "frozen proposal catalog does not match its pack")
    schema = _object(store.read(binding["proposal_schema"]))
    require(type(schema) is dict and set(schema) == {"schema_version", "parameters_schema"}
            and schema["schema_version"] == 1
            and api.thaw(schema["parameters_schema"]) == api.thaw(catalog.parameters_schema),
            "frozen proposal schema does not match its catalog")
    host = _object(store.read(binding["host_inputs"]))
    require(type(host) is dict, "pack host inputs must be a JSON object")
    attempts = binding["planned_attempts"]
    require(type(attempts) is int and 2 <= attempts <= 4096, "invalid frozen proposal attempt bound")
    _environment(store, binding["environment"], live=live)
    bundle = _bundle(store, binding["capture"], pack_id=binding["pack_id"],
                     pack_version=binding["pack_version"], require_capture=False)
    if live:
        loaded = registry.load_pack(binding["pack_id"])
        loaded.require_pin(pack_id=binding["pack_id"], pack_version=binding["pack_version"],
                           pack_code_digest=binding["pack_code_digest"])
        live_catalog = loaded.hook("describe")()
        require(type(live_catalog) is api.ParameterCatalog and live_catalog.digest() == binding["catalog"],
                "pack catalog changed since the proposal binding was frozen")
        require(canonical(_schema_of(loaded, live_catalog)) == store.read(binding["proposal_schema"]),
                "pack proposal schema changed since the binding was frozen")
        if bundle is not None:
            require(loaded.manifest.capture, "pack does not capture external sources")
        require(_attempts(loaded, host, bundle) == attempts,
                "pack proposal attempt bound changed since the binding was frozen")
    return binding


def binding_artifacts(store: Store, key: str) -> set[str]:
    """CAS closure of a pre-model binding, including pinned pack source files."""
    binding = load_proposal_binding(store, key, live=False)
    keys = {key, binding["catalog"], binding["proposal_schema"], binding["host_inputs"],
            binding["environment"], binding["pack_code_digest"]}
    code = api.validate_code_manifest(_object(store.read(binding["pack_code_digest"])))
    keys.update(row["sha256"] for row in code["files"])
    if binding["capture"] is not None:
        keys.add(binding["capture"])
        bundle = _object(store.read(binding["capture"]))
        keys.update(row["sha256"] for row in bundle["inventory"])
    return keys


def freeze_proposal_binding(store: Store, *, pack_id: str, host_inputs: dict[str, Any],
                            capture: str | None, environment: str) -> str:
    """Pin a pack's proposal schema and host inputs before any model call.

    The returned digest is not an event. Host inputs are stored and are not
    copied into the model context.
    """
    require(type(pack_id) is str and type(host_inputs) is dict, "invalid pack proposal binding")
    require(capture is None or (type(capture) is str and _DIGEST.fullmatch(capture) is not None),
            "capture must be null or a CAS digest")
    loaded = registry.load_pack(pack_id)
    catalog = loaded.hook("describe")()
    require(type(catalog) is api.ParameterCatalog
            and (catalog.pack_id, catalog.pack_version) == (loaded.pack_id, loaded.pack_version),
            "pack describe() must return its own ParameterCatalog")
    schema = _schema_of(loaded, catalog)
    for data in loaded.files.values():
        store.put(data)
    require(store.put_json(api.thaw(loaded.code_manifest)) == loaded.pack_code_digest,
            "pack code manifest CAS digest mismatch")
    bundle = _bundle(store, capture, pack_id=loaded.pack_id, pack_version=loaded.pack_version,
                     require_capture=loaded.manifest.capture)
    _environment(store, environment, live=True)
    binding = dict(schema_version=1, pack_id=loaded.pack_id, pack_version=loaded.pack_version,
                   pack_code_digest=loaded.pack_code_digest,
                   catalog=store.put(catalog.canonical()), proposal_schema=store.put_json(schema),
                   host_inputs=store.put_json(host_inputs), capture=capture, environment=environment,
                   planned_attempts=_attempts(loaded, host_inputs, bundle))
    key = store.put_json(binding)
    load_proposal_binding(store, key, live=True)
    return key


def _get(history: list[dict[str, Any]], id: str, kind: str) -> dict[str, Any]:
    return Kernel._get(history, id, kind)


def planning_view(store: Store, history: list[dict[str, Any]], *, budget: str,
                  explanation_set: str, tree: str, planned_attempts: int,
                  current: bool) -> dict[str, Any]:
    """Shared search and planning view. Attempt count is already frozen."""
    admission = _get(history, budget, "agent_budget")
    binding = binding_for(history, explanation_set, current=current)
    explanations = _get(history, explanation_set, "explanation_set")
    question = _get(history, binding["question"], "research_question")
    policy = _get(history, tree, "search_tree")
    require(admission["payload"]["study_id"] == binding["study_id"]
            and policy["payload"]["cost_unit"] == "enqueued_attempt",
            "experiment budget/study or tree cost unit mismatch")
    for recorded in history:
        if recorded["kind"] == "agent_request" and recorded["payload"].get("schema_version") in {2, 3} \
                and recorded["payload"]["tree"] == tree:
            prior = _get(history, recorded["payload"]["explanation_set"], "explanation_set")
            require(prior["payload"]["study_id"] == binding["study_id"],
                    "experiment tree is already bound to another study")
        if recorded["kind"] == "experiment_node" and recorded["payload"]["tree"] == tree:
            prior = _get(history, recorded["payload"]["protocol"], "protocol")
            require(prior["payload"].get("planning", {}).get("study_id") == binding["study_id"],
                    "experiment tree contains an unbound or foreign-study protocol")
    for item in store.receipts():
        if tree in item["event_ids"]:
            require(item["context"]["study_id"] == binding["study_id"],
                    "experiment tree creation receipt belongs to another study")
    projection = Search._projection(history, tree)
    policy_data = policy["payload"]
    require(projection["remaining"] >= planned_attempts, "experiment attempt budget is exhausted")
    require(len(projection["nodes"]) < policy_data["max_nodes"]
            and sum(node["parent"] is None for node in projection["nodes"].values())
            < policy_data["max_width"], "experiment tree root capacity is exhausted")
    if policy_data["tournament"] is not None:
        tournament = _get(history, policy_data["tournament"], "tournament")
        require(set(explanations["payload"]["hypotheses"]) <= set(tournament["payload"]["candidates"]),
                "experiment hypotheses are outside the tree tournament")
    nodes = [dict(id=id, parent=node["parent"], protocol=node["protocol"],
                  action=node["action"], state=node["state"],
                  scientific_outcome=node["scientific_outcome"], estimated_cost=node["estimated_cost"])
             for id, node in projection["nodes"].items()]
    protocols = {node["protocol"] for node in nodes}
    runs = {event["id"] for event in history if event["kind"] == "run"
            and event["payload"]["protocol"] in protocols}
    claims = {event["id"] for event in history if event["kind"] == "claim"
              and event["payload"]["protocol"] in protocols}
    selections = {event["id"] for event in history if event["kind"] == "search_selection"
                  and event["payload"]["tree"] == tree}
    batches = {event["id"] for event in history if event["kind"] == "batch_plan"
               and event["payload"]["selection"] in selections}
    jobs = {event["id"] for event in history if event["kind"] == "execution_job"
            and event["payload"]["run"] in runs}
    evidence = [dict(id=event["id"], kind=event["kind"], hash=event["hash"])
                for event in history if (
                    (event["kind"] in {"run", "result", "execution_job"}
                     and (event["payload"].get("protocol") in protocols
                          or event["payload"].get("run") in runs))
                    or (event["kind"] in {"execution_dispatch", "execution_finalized"}
                        and event["payload"].get("job") in jobs)
                    or (event["kind"] in {"claim", "review"}
                        and (event["payload"].get("protocol") in protocols
                             or event["payload"].get("claim") in claims))
                    or (event["kind"] == "claim_link"
                        and (event["payload"].get("source") in claims
                             or event["payload"].get("target") in claims))
                    or (event["kind"] == "data_exposure"
                        and event["payload"].get("protocol") in protocols)
                    or (event["kind"] == "batch_plan" and event["id"] in batches)
                    or (event["kind"] in {"batch_slot", "batch_settlement"}
                        and event["payload"].get("batch") in batches)
                    or (event["kind"] == "search_selection" and event["id"] in selections)
                    or (event["kind"] == "search_terminal"
                        and event["payload"].get("selection") in selections))]
    return dict(question=question, explanation_set=explanations, planning_binding=binding,
                planning_history=planning_context(history, binding),
                hypothesis_ids=list(explanations["payload"]["hypotheses"]),
                search_tree=policy, search_nodes=nodes, evidence_revisions=evidence)


def context(store: Store, history: list[dict[str, Any]], *, budget: str, explanation_set: str,
            tree: str, proposal_binding: str, current: bool, live: bool) -> dict[str, Any]:
    """Model context. Host inputs and captured bytes are not included."""
    frozen = load_proposal_binding(store, proposal_binding, live=live)
    view = planning_view(store, history, budget=budget, explanation_set=explanation_set, tree=tree,
                         planned_attempts=frozen["planned_attempts"], current=current)
    return dict(schema_version=3, task="experiment_proposal", prompt_version=proposals.PROMPT_VERSION,
                pack_id=frozen["pack_id"], pack_version=frozen["pack_version"],
                catalog=_object(store.read(frozen["catalog"])),
                proposal_schema=_object(store.read(frozen["proposal_schema"])),
                planned_attempts=frozen["planned_attempts"], cost_unit="enqueued_attempt", **view)


def validate_application(store: Store, history: list[dict[str, Any]], offset: int,
                        state: dict[str, Any], event: dict[str, Any]) -> None:
    """Replay one pack-scoped application. Pack code is not imported."""
    p = event["payload"]
    request_event, response = state["request"], state["response"]
    request = request_event["payload"]
    fields = {"schema_version", "request", "request_hash", "response", "response_hash",
              "pack_binding", "protocol", "protocol_hash", "experiment_node",
              "experiment_node_hash", "limitations", "scientific_validity"}
    require(request["schema_version"] == 3 and response is not None and state["application"] is None
            and response["payload"]["assessment"]["proposal_status"] == "proposed",
            "pack experiment application requires one proposed response")
    require(set(p) == fields and p["request"] == request_event["id"] and p["request_hash"] == request_event["hash"]
            and p["response"] == response["id"] and p["response_hash"] == response["hash"]
            and p["scientific_validity"] == "not_assessed",
            "invalid pack experiment application fields")
    require(offset >= 3 and [item["kind"] for item in history[offset - 3:offset]]
            == ["protocol", "pack_binding", "experiment_node"],
            "pack experiment application must follow its protocol, binding and node")
    preceding = history[:offset - 3]
    current = context(store, preceding, budget=request["budget"],
                      explanation_set=request["explanation_set"], tree=request["tree"],
                      proposal_binding=request["proposal_binding"], current=True, live=False)
    require(_object(store.read(request["context"])) == current, "experiment proposal context is stale")
    plan, binding, node = history[offset - 3:offset]
    require(p["protocol"] == plan["id"] and p["protocol_hash"] == plan["hash"]
            and p["pack_binding"] == binding["id"]
            and p["experiment_node"] == node["id"] and p["experiment_node_hash"] == node["hash"],
            "application does not bind its created protocol and node")
    proposal = proposals.validate_proposal(
        store.read(response["payload"]["assessment"]["artifacts"]["proposal"]),
        current["hypothesis_ids"], pack_id=current["pack_id"],
        parameters_schema=current["proposal_schema"]["parameters_schema"])
    experiment = proposal["experiment"]
    require(experiment is not None and p["limitations"] == proposal["limitations"],
            "application limitations differ from response")
    frozen = load_proposal_binding(store, request["proposal_binding"], live=False)
    require(plan["payload"]["planning"] == current["planning_binding"]
            and plan["payload"]["hypotheses"] == current["hypothesis_ids"]
            and plan["payload"]["run_limit"] == frozen["planned_attempts"]
            and binding["payload"]["parameters"] == experiment["parameters"]
            and binding["payload"]["protocol"] == plan["id"]
            and binding["payload"]["explanation_set"] == request["explanation_set"],
            "created protocol differs from the frozen pack proposal")
    require(node["payload"]["tree"] == request["tree"]
            and node["payload"]["protocol"] == plan["id"]
            and node["payload"]["protocol_hash"] == plan["hash"]
            and node["payload"]["action"] == experiment["action"]
            and node["payload"]["components"] == experiment["components"]
            and node["payload"]["rationale"] == experiment["rationale"]
            and node["payload"]["estimated_cost"] == frozen["planned_attempts"]
            and node["payload"]["parent"] is None,
            "created search node differs from frozen proposal")
    domain_packs.pack_bindings(store, history)


def preregister_request(store: Store, agent_payload: dict[str, Any],
                        parameters: dict[str, Any]) -> dict[str, Any]:
    """The pack.preregister-shaped request compiled from a proposed response."""
    frozen = load_proposal_binding(store, agent_payload["proposal_binding"], live=True)
    require(agent_payload["pack_id"] == frozen["pack_id"]
            and agent_payload["pack_version"] == frozen["pack_version"]
            and agent_payload["pack_code_digest"] == frozen["pack_code_digest"]
            and agent_payload["catalog"] == frozen["catalog"],
            "experiment request pin differs from its proposal binding")
    return dict(explanation_set=agent_payload["explanation_set"], pack_id=frozen["pack_id"],
                pack_version=frozen["pack_version"], pack_code_digest=frozen["pack_code_digest"],
                parameters=parameters, host_inputs=_object(store.read(frozen["host_inputs"])),
                capture=frozen["capture"], environment=frozen["environment"])
