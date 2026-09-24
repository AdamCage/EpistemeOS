"""Durable model proposals. Transport completion is never scientific validity."""

from __future__ import annotations

from datetime import datetime
import math
from pathlib import Path
import re
import sys
from typing import Any

from .agent_profiles import DEFAULT_PROFILE, profile
from . import experiment_proposals
from .codex_provider import PROGRAM, validate_provider
from .domains import synthetic_causal
from .execution import BACKEND, _object
from .execution_authority import establish_authority, require_authority
from .kernel import Actor, Kernel, require
from .planning import Planning, _Index as PlanningIndex, binding_for, planning_context
from .runner_backend import fingerprint
from .search import Search
from .store import Store, canonical


KINDS = {"agent_budget", "agent_request", "agent_dispatch", "agent_response", "agent_application"}
COMPILER_SOURCE_BYTES = Path(synthetic_causal.__file__).read_bytes()


def _text(value: Any) -> bool:
    return type(value) is str and bool(value.strip())


def _get(history: list[dict[str, Any]], id: str, kind: str) -> dict[str, Any]:
    return Kernel._get(history, id, kind)


def _context(history: list[dict[str, Any]], question: str, *, current: bool,
             version: str = DEFAULT_PROFILE) -> dict[str, Any]:
    profile(version)
    index = PlanningIndex(history)
    event = index.get(question, "research_question")
    if current:
        index.head(event)
    return dict(schema_version=1, task="hypothesis_proposal", prompt_version=version, question=event)


def _proposal(store: Store, state: dict[str, Any], raw: bytes) -> dict[str, Any]:
    context = _object(store.read(state["request"]["payload"]["context"]))
    validate = profile(context["prompt_version"])[2]
    if context["task"] == "experiment_proposal":
        return validate(raw, context["hypothesis_ids"])
    return validate(raw)


def _recipe_binding(store: Store, key: str, *, current: bool) -> dict[str, Any]:
    binding = _object(store.read(key))
    require(set(binding) == {"schema_version", "recipe_id", "catalog", "world", "seeds",
                             "environment", "replication_tolerance"}
            and type(binding["schema_version"]) is int and binding["schema_version"] == 1
            and binding["recipe_id"] == experiment_proposals.RECIPE_ID,
            "invalid frozen experiment recipe binding")
    require(type(binding["catalog"]) is dict and binding["catalog"].get("id") == binding["recipe_id"],
            "invalid frozen recipe catalog")
    require(type(binding["world"]) is dict and set(binding["world"]) ==
            {"treatment_effect", "confounding_strength", "noise_std"}, "invalid synthetic world")
    require(type(binding["seeds"]) is list and 1 <= len(binding["seeds"]) <= 64
            and all(type(seed) is int and -(2**31) <= seed < 2**31 for seed in binding["seeds"])
            and len(set(binding["seeds"])) == len(binding["seeds"]), "invalid frozen experiment seeds")
    tolerance = binding["replication_tolerance"]
    require(type(tolerance) in (int, float) and math.isfinite(tolerance) and tolerance >= 0,
            "invalid frozen replication tolerance")
    environment = _object(store.read(binding["environment"]))
    require(set(environment) == {"schema_version", "backend", "fingerprint"}
            and type(environment["schema_version"]) is int and environment["schema_version"] == 1
            and environment["backend"] == BACKEND and type(environment["fingerprint"]) is dict,
            "experiment requires a frozen local Python environment")
    if current:
        require(environment["fingerprint"] == fingerprint(),
                "experiment environment no longer matches this local runner")
        require(binding["catalog"] == synthetic_causal.describe(),
                "experiment recipe catalog changed since it was frozen")
        # Validation is domain-owned.  Do not expose the host world to the model.
        synthetic_causal.compile_recipe(dict(n_samples=32, assignment="randomized",
                                             analysis="difference_in_means"), binding["world"])
    return binding


def freeze_recipe_binding(store: Store, *, world: dict[str, Any], seeds: list[int],
                          environment: str,
                          replication_tolerance: float = synthetic_causal.DEFAULT_REANALYSIS_TOLERANCE) -> str:
    """Freeze host-owned synthetic world and execution policy before a model call."""
    binding = dict(schema_version=1, recipe_id=experiment_proposals.RECIPE_ID,
                   catalog=synthetic_causal.describe(), world=world, seeds=seeds,
                   environment=environment, replication_tolerance=replication_tolerance)
    key = store.put_json(binding)
    _recipe_binding(store, key, current=True)
    return key


def _experiment_context(store: Store, history: list[dict[str, Any]], *,
                        budget: str, explanation_set: str, tree: str,
                        recipe_binding: str, current: bool,
                        adapter_current: bool = False) -> dict[str, Any]:
    frozen = _recipe_binding(store, recipe_binding, current=adapter_current)
    admission = _get(history, budget, "agent_budget")
    binding = binding_for(history, explanation_set, current=current)
    explanations = _get(history, explanation_set, "explanation_set")
    question = _get(history, binding["question"], "research_question")
    policy = _get(history, tree, "search_tree")
    require(admission["payload"]["study_id"] == binding["study_id"]
            and policy["payload"]["cost_unit"] == "enqueued_attempt",
            "experiment budget/study or tree cost unit mismatch")
    for recorded in history:
        if recorded["kind"] == "agent_request" and recorded["payload"].get("schema_version") == 2 \
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
    require(projection["remaining"] >= 2 * len(frozen["seeds"]),
            "experiment attempt budget is exhausted")
    require(len(projection["nodes"]) < policy_data["max_nodes"]
            and sum(node["parent"] is None for node in projection["nodes"].values())
            < policy_data["max_width"], "experiment tree root capacity is exhausted")
    if policy_data["tournament"] is not None:
        tournament = _get(history, policy_data["tournament"], "tournament")
        require(set(explanations["payload"]["hypotheses"])
                <= set(tournament["payload"]["candidates"]),
                "experiment hypotheses are outside the tree tournament")
    nodes = [dict(id=id, parent=n["parent"], protocol=n["protocol"],
                  action=n["action"], state=n["state"],
                  scientific_outcome=n["scientific_outcome"], estimated_cost=n["estimated_cost"])
             for id, n in projection["nodes"].items()]
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
    return dict(schema_version=2, task="experiment_proposal",
                prompt_version=experiment_proposals.PROMPT_VERSION,
                question=question, explanation_set=explanations,
                planning_binding=binding, planning_history=planning_context(history, binding),
                hypothesis_ids=list(explanations["payload"]["hypotheses"]),
                search_tree=policy, search_nodes=nodes, evidence_revisions=evidence,
                recipe=frozen["catalog"],
                seeds=list(frozen["seeds"]), planned_attempts=2*len(frozen["seeds"]),
                cost_unit="enqueued_attempt")


def _experiment_current(store: Store, history: list[dict[str, Any]], p: dict[str, Any],
                        *, adapter_current: bool = True) -> dict[str, Any]:
    current = _experiment_context(store, history, budget=p["budget"],
        explanation_set=p["explanation_set"], tree=p["tree"],
        recipe_binding=p["recipe_binding"], current=True, adapter_current=adapter_current)
    require(_object(store.read(p["context"])) == current,
            "experiment proposal context is stale")
    return current


def _identity(state: dict[str, Any]) -> dict[str, Any]:
    return dict(schema_version=1, request=state["request"]["id"], dispatch=state["dispatch"]["id"],
                specification=state["request"]["payload"]["specification"])


def _specification(store: Store, p: dict[str, Any]) -> dict[str, Any]:
    spec = _object(store.read(p["specification"]))
    require(set(spec) == {"schema_version", "command", "outputs", "wall_seconds", "max_output_bytes",
                          "expected_inputs", "environment_fingerprint"}
            and type(spec["schema_version"]) is int and spec["schema_version"] == 1,
            "invalid agent execution specification")
    require(type(spec["wall_seconds"]) is int and 1 <= spec["wall_seconds"] <= 600,
            "agent wall limit must be 1..600 seconds")
    require(type(spec["max_output_bytes"]) is int and 1024 <= spec["max_output_bytes"] <= 4*1024*1024,
            "agent capture limit must be 1 KiB..4 MiB")
    require(type(spec["command"]) is list and len(spec["command"]) == 7
            and spec["command"][1:] == ["-I", "-S", "program.py", "input.dat", "--seed", "0"]
            and _text(spec["command"][0]), "invalid agent worker command")
    require(spec["outputs"] == {"proposal": "proposal.json"}
            and type(spec["environment_fingerprint"]) is dict
            and set(spec["expected_inputs"]) == {"program.py", "input.dat"}, "invalid agent worker inputs/outputs")
    for key in spec["expected_inputs"].values():
        store.read(key)
    context = _object(store.read(p["context"]))
    provider = _object(store.read(p["provider"]))
    validate_provider(provider)
    prompt, schema, _ = profile(context["prompt_version"])
    supplied = _object(store.read(spec["expected_inputs"]["input.dat"]))
    require(supplied == dict(provider=provider, prompt=prompt + "\n\n" + canonical(context).decode(),
                              output_schema=schema), "agent input differs from frozen context/profile")
    return spec


def _stream(data: bytes) -> tuple[dict[str, Any] | None, list[str]]:
    """Audit observed CLI actions; this detects use, it does not undo a tool call."""
    errors: list[str] = []
    usage = None
    started = 0
    completed = 0
    threads: set[str] = set()
    messages = 0
    phase = "new"
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeError:
        return None, ["provider stream is not UTF-8"]
    for line in lines:
        try:
            event = _object(line.encode())
            kind = event.get("type")
            if kind == "thread.started":
                require(phase == "new", "provider thread event out of order")
                require(_text(event.get("thread_id")), "provider thread identity missing")
                threads.add(event["thread_id"])
                phase = "thread"
            elif kind == "turn.started":
                started += 1
                require(phase == "thread", "provider turn start out of order")
                phase = "turn"
            elif kind == "turn.completed":
                completed += 1
                observed = event.get("usage")
                require(type(observed) is dict and {"input_tokens", "output_tokens"} <= observed.keys(),
                        "provider usage is missing")
                require(all(type(v) is int and v >= 0 for v in observed.values()), "invalid provider usage")
                usage = dict(observed)
                require(phase == "turn", "provider completion out of order")
                phase = "complete"
            elif kind in {"item.started", "item.updated", "item.completed"}:
                require(phase == "turn", "provider item out of order")
                require(type(event.get("item")) is dict and event["item"].get("type") in {"agent_message", "reasoning"},
                        "provider used a tool or an unsupported item type")
                if kind == "item.completed" and event["item"]["type"] == "agent_message":
                    require(_text(event["item"].get("text")), "provider message text missing")
                    messages += 1
            elif kind in {"turn.failed", "error"}:
                errors.append("provider reported an error or failed turn")
            else:
                errors.append("unsupported provider stream event")
        except (ValueError, TypeError, UnicodeError) as exc:
            errors.append(str(exc))
    if not (len(threads) == 1 and started == completed == 1):
        errors.append("exactly one completed provider turn required")
    if not messages:
        errors.append("completed provider message missing")
    return usage, errors


def _last_completed_message(data: bytes) -> bytes:
    """Bind the provider's final JSONL answer to its output-last-message file."""
    final = None
    for line in data.decode("utf-8").splitlines():
        observed = _object(line.encode("utf-8"))
        if observed.get("type") == "item.completed" and observed.get("item", {}).get("type") == "agent_message":
            final = observed["item"].get("text")
    require(_text(final), "final provider message is missing")
    return final.encode("utf-8")


def _completion(store: Store, state: dict[str, Any], manifest: str) -> dict[str, Any]:
    record = _object(store.read(manifest))
    spec = state["spec"]
    require(record.get("identity") == _identity(state) and type(record.get("schema_version")) is int
            and record["schema_version"] == 1,
            "agent completion identity mismatch")
    require(record.get("status") in {"completed", "failed"} and record.get("termination_confirmed") is True,
            "agent completion has no confirmed local termination")
    require(record.get("command") == spec["command"] and type(record.get("reason")) is str,
            "agent completion command/reason mismatch")
    require(type(record.get("elapsed_seconds")) in {int, float} and math.isfinite(record["elapsed_seconds"])
            and record["elapsed_seconds"] >= 0, "invalid agent elapsed time")
    for name in ("started_at", "finished_at"):
        require(type(record.get(name)) is str and datetime.fromisoformat(record[name]).tzinfo is not None,
                "agent timestamps must have a timezone")
    require(record.get("returncode") is None or type(record["returncode"]) is int, "invalid agent return code")
    outputs = record.get("outputs")
    require(type(outputs) is dict and set(outputs) <= {"proposal"}, "invalid agent completion outputs")
    blobs: dict[str, str] = {}
    for name, item in {**outputs, "stdout": record.get("stdout"), "stderr": record.get("stderr")}.items():
        path = {"proposal": "proposal.json", "stdout": "stdout.bin", "stderr": "stderr.bin"}[name]
        require(type(item) is dict and set(item) == {"path", "sha256", "bytes"}
                and item["path"] == path and type(item["bytes"]) is int
                and 0 <= item["bytes"] <= spec["max_output_bytes"], "invalid agent output entry")
        require(len(store.read(item["sha256"])) == item["bytes"], "agent output length mismatch")
        blobs[name] = item["sha256"]
    if record["status"] == "completed":
        require(record["returncode"] == 0 and record.get("runtime") == spec["environment_fingerprint"]
                and record.get("inputs_before") == record.get("inputs_after") == spec["expected_inputs"]
                and set(outputs) == {"proposal"}, "completed agent lacks frozen inputs/runtime/output")
    usage, errors = _stream(store.read(blobs["stdout"]))
    proposal = None
    if record["status"] != "completed":
        status = "failed"
        errors.insert(0, record["reason"] or "agent transport failed")
    elif errors:
        status = "invalid"
    else:
        try:
            proposal = _proposal(store, state, store.read(blobs["proposal"]))
            if state["request"]["payload"]["schema_version"] == 2:
                message = _proposal(store, state, _last_completed_message(store.read(blobs["stdout"])))
                require(message == proposal, "provider message differs from captured proposal file")
            status = proposal["status"]
        except ValueError as exc:
            status = "invalid"
            errors.append(str(exc))
    return dict(transport_status=record["status"], proposal_status=status, errors=errors, usage=usage,
                artifacts=blobs, scientific_validity="not_assessed")


def _validate_experiment_application(store: Store, history: list[dict[str, Any]], offset: int,
                                     state: dict[str, Any], event: dict[str, Any], receipt: Any) -> None:
    p = event["payload"]
    request, response = state["request"], state["response"]
    require(request["payload"]["schema_version"] == 2 and response is not None
            and state["application"] is None and response["payload"]["assessment"]["proposal_status"] == "proposed",
            "experiment application requires one proposed response")
    fields = {"schema_version", "request", "request_hash", "response", "response_hash",
              "compilation", "protocol", "protocol_hash", "experiment_node", "experiment_node_hash",
              "limitations", "scientific_validity"}
    require(set(p) == fields and p["request"] == request["id"] and p["request_hash"] == request["hash"]
            and p["response"] == response["id"] and p["response_hash"] == response["hash"]
            and p["scientific_validity"] == "not_assessed", "invalid experiment application fields")
    require(offset >= 2 and [e["kind"] for e in history[offset-2:offset]] == ["protocol", "experiment_node"],
            "experiment application must immediately follow protocol and node")
    preceding = history[:offset-2]
    context = _experiment_current(store, preceding, request["payload"], adapter_current=False)
    plan, node = history[offset-2:offset]
    require(p["protocol"] == plan["id"] and p["protocol_hash"] == plan["hash"]
            and p["experiment_node"] == node["id"] and p["experiment_node_hash"] == node["hash"],
            "application does not bind its created protocol and node")
    raw = store.read(response["payload"]["assessment"]["artifacts"]["proposal"])
    proposal = _proposal(store, state, raw)
    experiment = proposal["experiment"]
    require(experiment is not None and p["limitations"] == proposal["limitations"],
            "application limitations differ from response")
    manifest = _object(store.read(p["compilation"]))
    require(set(manifest) == {"schema_version", "compiler", "request", "request_hash", "response",
                              "response_hash", "proposal_digest", "recipe_binding", "proposal",
                              "compiled", "sources", "protocol", "experiment_node"}
            and manifest["schema_version"] == 1 and manifest["compiler"] == experiment_proposals.RECIPE_ID
            and manifest["request"] == request["id"] and manifest["request_hash"] == request["hash"]
            and manifest["response"] == response["id"] and manifest["response_hash"] == response["hash"]
            and manifest["proposal_digest"] == response["payload"]["assessment"]["artifacts"]["proposal"]
            and manifest["recipe_binding"] == request["payload"]["recipe_binding"]
            and manifest["proposal"] == proposal and manifest["protocol"] == plan
            and manifest["experiment_node"] == node, "compilation manifest does not match immutable records")
    compiled, sources = manifest["compiled"], manifest["sources"]
    require(type(compiled) is dict and set(compiled) == {"domain", "parameters", "metric", "outputs",
                                                     "design", "analysis_plan", "stopping_rule", "statistical_design"}
            and compiled["domain"] == experiment["recipe_id"]
            and compiled["parameters"] == experiment["parameters"]
            and type(sources) is dict and set(sources) == {"implementation", "reanalysis_implementation",
                                                        "data", "compiler_source"}, "invalid frozen compilation")
    for key in sources.values():
        store.read(key)
    binding = _recipe_binding(store, request["payload"]["recipe_binding"], current=False)
    expected_design = compiled["design"] + "\nFrozen model contrast: " + experiment["discriminating_contrast"]
    expected_analysis = compiled["analysis_plan"] + "\nFrozen model predictions: " + canonical(
        experiment["hypothesis_predictions"]).decode()
    protocol = plan["payload"]
    require(protocol["planning"] == context["planning_binding"]
            and protocol["hypotheses"] == context["hypothesis_ids"]
            and protocol["scope"] == context["question"]["payload"]["scope"]
            and protocol["design"] == expected_design and protocol["analysis_plan"] == expected_analysis
            and protocol["metric"] == compiled["metric"] and protocol["stopping_rule"] == compiled["stopping_rule"]
            and protocol["statistical_design"] == compiled["statistical_design"]
            and protocol["seeds"] == binding["seeds"] and protocol["run_limit"] == 2*len(binding["seeds"])
            and protocol["implementation"] == sources["implementation"]
            and protocol["data"] == sources["data"] and protocol["environment"] == binding["environment"]
            and protocol["replication_tolerance"] == binding["replication_tolerance"]
            and protocol["parent"] is None, "created protocol differs from frozen compilation")
    require(node["payload"]["tree"] == request["payload"]["tree"]
            and node["payload"]["protocol"] == plan["id"]
            and node["payload"]["protocol_hash"] == plan["hash"]
            and node["payload"]["action"] == experiment["action"]
            and node["payload"]["components"] == experiment["components"]
            and node["payload"]["rationale"] == experiment["rationale"]
            and node["payload"]["estimated_cost"] == 2*len(binding["seeds"])
            and node["payload"]["parent"] is None,
            "created search node differs from frozen proposal")
    receipt([plan, node, event], "agent.apply_experiment")


def _index(store: Store, history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not any(event["kind"] in KINDS for event in history):
        return {}
    receipts = store.receipts()
    def receipt(events: list[dict[str, Any]], action: str) -> None:
        ids = [event["id"] for event in events]
        require(any(row["event_ids"] == ids and row["request"]["action"] == action for row in receipts),
                "agent transition lacks its complete original command receipt")
    budgets: dict[str, dict[str, Any]] = {}
    states: dict[str, dict[str, Any]] = {}
    for offset, event in enumerate(history):
        kind, p = event["kind"], event["payload"]
        if kind not in KINDS:
            continue
        version = p.get("schema_version")
        require(event["role"] == "planner" and type(version) is int
                and version in ({1, 2} if kind in {"agent_request", "agent_application"} else {1}),
                "agent transition role/schema mismatch")
        before = history[:offset]
        if kind == "agent_budget":
            require(set(p) == {"schema_version", "study_id", "max_calls"} and _text(p["study_id"])
                    and type(p["max_calls"]) is int and 1 <= p["max_calls"] <= 100,
                    "invalid model call admission budget")
            receipt([event], "agent.register_budget")
            budgets[event["id"]] = event
        elif kind == "agent_request":
            common = {"schema_version", "budget", "budget_hash", "question", "question_hash", "assignee",
                      "provider", "specification", "context", "execution_authority"}
            additional = {"explanation_set", "explanation_set_hash", "tree", "tree_hash", "recipe_binding"}
            require(set(p) == (common | additional if version == 2 else common), "invalid agent request fields")
            require(p["budget"] in budgets and p["budget_hash"] == budgets[p["budget"]]["hash"], "unknown agent budget")
            budget = budgets[p["budget"]]
            require(sum(s["request"]["payload"]["budget"] == p["budget"] for s in states.values()) < budget["payload"]["max_calls"],
                    "model call admission budget exhausted")
            frozen = _object(store.read(p["context"]))
            if version == 2:
                context = _experiment_context(store, before, budget=p["budget"],
                    explanation_set=p["explanation_set"], tree=p["tree"],
                    recipe_binding=p["recipe_binding"], current=True)
                require(context["explanation_set"]["hash"] == p["explanation_set_hash"]
                        and context["search_tree"]["hash"] == p["tree_hash"]
                        and context["planning_binding"]["question"] == p["question"],
                        "experiment request references differ from frozen planning context")
            else:
                context = _context(before, p["question"], current=True, version=frozen.get("prompt_version"))
            require(context["question"]["hash"] == p["question_hash"]
                    and context["question"]["payload"]["study_id"] == budget["payload"]["study_id"]
                    and frozen == context, "request context/study differs from frozen question")
            require(_text(p["assignee"]) and type(p["execution_authority"]) is str
                    and re.fullmatch(r"[0-9a-f]{64}", p["execution_authority"]), "invalid agent assignment/authority")
            receipt([event], "agent.request_experiment" if version == 2 else "agent.request_hypotheses")
            states[event["id"]] = dict(request=event, spec=_specification(store, p), dispatch=None, response=None, application=None)
        else:
            require(p.get("request") in states, "agent event needs a prior request")
            state = states[p["request"]]
            request = state["request"]
            require(p.get("request_hash") == request["hash"] and event["actor"] == request["payload"]["assignee"],
                    "agent request hash or assigned actor mismatch")
            if kind == "agent_dispatch":
                require(set(p) == {"schema_version", "request", "request_hash", "workspace_token"}
                        and state["dispatch"] is None and type(p["workspace_token"]) is str
                        and re.fullmatch(r"[0-9a-f]{32}", p["workspace_token"]), "agent already dispatched or invalid token")
                if request["payload"]["schema_version"] == 2:
                    _experiment_current(store, before, request["payload"], adapter_current=False)
                else:
                    _context(before, request["payload"]["question"], current=True)
                receipt([event], "agent.dispatch")
                state["dispatch"] = event
            elif kind == "agent_response":
                require(set(p) == {"schema_version", "request", "request_hash", "dispatch", "manifest", "assessment"}
                        and state["dispatch"] is not None and state["response"] is None
                        and p["dispatch"] == state["dispatch"]["id"], "invalid/duplicate agent response")
                require(p["assessment"] == _completion(store, state, p["manifest"]), "response assessment differs from captured bytes")
                receipt([event], "agent.finalize")
                state["response"] = event
            else:
                if version == 2:
                    _validate_experiment_application(store, history, offset, state, event, receipt)
                    state["application"] = event
                    continue
                require(request["payload"]["schema_version"] == 1,
                        "hypothesis application requires a hypothesis request")
                require(set(p) == {"schema_version", "request", "request_hash", "response", "response_hash", "mapping",
                                    "explanation_set", "explanation_set_hash", "limitations"}, "invalid agent application fields")
                require(state["response"] is not None and state["application"] is None
                        and p["response"] == state["response"]["id"] and p["response_hash"] == state["response"]["hash"],
                        "invalid or repeated agent application")
                response = state["response"]["payload"]
                require(response["assessment"]["proposal_status"] == "proposed", "agent response is not applicable")
                proposal = _proposal(store, state, store.read(response["assessment"]["artifacts"]["proposal"]))
                count = len(proposal["candidates"])
                created = history[offset-count-1:offset]
                require(len(created) == count+1 and [e["kind"] for e in created] == ["hypothesis"]*count + ["explanation_set"],
                        "application must immediately follow its new hypotheses/set")
                _context(history[:offset-count-1], request["payload"]["question"], current=True)
                context = _object(store.read(request["payload"]["context"]))["question"]["payload"]
                mapping = []
                for index, (candidate, hypothesis) in enumerate(zip(proposal["candidates"], created)):
                    require(hypothesis["payload"] == dict(statement=candidate["statement"], prediction=candidate["prediction"],
                        falsifier=candidate["falsifier"], scope=context["scope"]), "created hypothesis differs from model proposal")
                    mapping.append(dict(index=index, hypothesis=hypothesis["id"], hash=hypothesis["hash"], kind=candidate["kind"]))
                selected = created[-1]
                require(p["mapping"] == mapping and p["limitations"] == proposal["limitations"]
                        and p["explanation_set"] == selected["id"] and p["explanation_set_hash"] == selected["hash"],
                        "application entity mapping differs from its proposal")
                require(selected["payload"]["question"] == request["payload"]["question"]
                        and selected["payload"]["hypotheses"] == [e["id"] for e in created[:-1]]
                        and selected["payload"]["comparison_plan"] == proposal["comparison_plan"]
                        and selected["payload"]["parent"] is None, "application set differs from proposal")
                receipt([*created, event], "agent.apply_hypotheses")
                state["application"] = event
    return states


class Agents:
    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def _history(self) -> list[dict[str, Any]]:
        require(self.store._command_context is not None and self.actor.role == "planner",
                "agent transitions require planner CommandService transactions")
        return self.store.events()

    def _write(self, kind: str, payload: dict[str, Any], *, version: int = 1) -> str:
        return Kernel(self.store, self.actor)._write(self.store.events(), kind,
                                                     dict(schema_version=version, **payload), {"planner"})

    def register_budget(self, *, study_id: str, max_calls: int) -> str:
        self._history()
        require(_text(study_id) and type(max_calls) is int and 1 <= max_calls <= 100, "invalid call budget")
        return self._write("agent_budget", dict(study_id=study_id, max_calls=max_calls))

    def request_hypotheses(self, *, budget: str, question: str, assignee: str, provider: str,
                           wall_seconds: int = 120, max_output_bytes: int = 1048576) -> str:
        history = self._history()
        states = _index(self.store, history)
        bound = _get(history, budget, "agent_budget")
        require(sum(s["request"]["payload"]["budget"] == budget for s in states.values()) < bound["payload"]["max_calls"],
                "model call admission budget exhausted")
        context = _context(history, question, current=True)
        require(context["question"]["payload"]["study_id"] == bound["payload"]["study_id"] and _text(assignee),
                "question/budget study mismatch or empty assignee")
        backend = _object(self.store.read(provider))
        validate_provider(backend)
        context_key = self.store.put_json(context)
        prompt, schema, _ = profile(context["prompt_version"])
        data = self.store.put_json(dict(provider=backend, prompt=prompt+"\n\n"+canonical(context).decode(),
                                       output_schema=schema))
        spec = self.store.put_json(dict(schema_version=1, command=[sys.executable,"-I","-S","program.py","input.dat","--seed","0"],
            outputs={"proposal":"proposal.json"}, wall_seconds=wall_seconds, max_output_bytes=max_output_bytes,
            expected_inputs={"program.py":self.store.put(PROGRAM),"input.dat":data}, environment_fingerprint=fingerprint()))
        p = dict(budget=budget, budget_hash=bound["hash"], question=question, question_hash=context["question"]["hash"],
            assignee=assignee, provider=provider, specification=spec, context=context_key,
            execution_authority=establish_authority(self.store))
        _specification(self.store, p)
        return self._write("agent_request", p)

    def request_experiment(self, *, budget: str, explanation_set: str, tree: str,
                           recipe_binding: str, assignee: str, provider: str,
                           wall_seconds: int = 120, max_output_bytes: int = 1048576) -> str:
        history = self._history()
        states = _index(self.store, history)
        admission = _get(history, budget, "agent_budget")
        require(sum(s["request"]["payload"]["budget"] == budget for s in states.values())
                < admission["payload"]["max_calls"], "model call admission budget exhausted")
        require(_text(assignee), "experiment agent assignee required")
        context = _experiment_context(self.store, history, budget=budget,
            explanation_set=explanation_set, tree=tree, recipe_binding=recipe_binding,
            current=True, adapter_current=True)
        backend = _object(self.store.read(provider))
        validate_provider(backend)
        context_key = self.store.put_json(context)
        prompt, schema, _ = profile(context["prompt_version"])
        data = self.store.put_json(dict(provider=backend, prompt=prompt+"\n\n"+canonical(context).decode(),
                                        output_schema=schema))
        spec = self.store.put_json(dict(schema_version=1,
            command=[sys.executable,"-I","-S","program.py","input.dat","--seed","0"],
            outputs={"proposal":"proposal.json"}, wall_seconds=wall_seconds,
            max_output_bytes=max_output_bytes,
            expected_inputs={"program.py":self.store.put(PROGRAM),"input.dat":data},
            environment_fingerprint=fingerprint()))
        p = dict(budget=budget, budget_hash=_get(history,budget,"agent_budget")["hash"],
                 question=context["question"]["id"], question_hash=context["question"]["hash"],
                 explanation_set=explanation_set, explanation_set_hash=context["explanation_set"]["hash"],
                 tree=tree, tree_hash=context["search_tree"]["hash"], recipe_binding=recipe_binding,
                 assignee=assignee, provider=provider, specification=spec, context=context_key,
                 execution_authority=establish_authority(self.store))
        _specification(self.store, p)
        return self._write("agent_request", p, version=2)

    def _assigned(self, request: str) -> dict[str, Any]:
        states = _index(self.store, self._history())
        require(request in states, "unknown agent request")
        state = states[request]
        require(state["request"]["payload"]["assignee"] == self.actor.id, "wrong assigned agent actor")
        return state

    def dispatch(self, *, request: str, workspace_token: str) -> str:
        state = self._assigned(request)
        require_authority(self.store, state["request"]["payload"]["execution_authority"])
        if state["request"]["payload"]["schema_version"] == 2:
            _experiment_current(self.store, self.store.events(), state["request"]["payload"])
        else:
            _context(self.store.events(), state["request"]["payload"]["question"], current=True)
        require(state["dispatch"] is None and type(workspace_token) is str and re.fullmatch(r"[0-9a-f]{32}",workspace_token),
                "agent already dispatched or invalid token; reconcile instead")
        return self._write("agent_dispatch", dict(request=request, request_hash=state["request"]["hash"], workspace_token=workspace_token))

    def finalize(self, *, request: str, manifest: str) -> str:
        state = self._assigned(request)
        require(state["dispatch"] is not None and state["response"] is None, "agent not dispatched or already finalized")
        assessment = _completion(self.store, state, manifest)
        return self._write("agent_response", dict(request=request, request_hash=state["request"]["hash"],
            dispatch=state["dispatch"]["id"], manifest=manifest, assessment=assessment))

    def apply_hypotheses(self, *, request: str) -> str:
        state = self._assigned(request)
        require(state["request"]["payload"]["schema_version"] == 1,
                "hypothesis application requires a hypothesis request")
        require(state["response"] is not None and state["application"] is None, "agent response missing or already applied")
        response = state["response"]
        require(response["payload"]["assessment"]["proposal_status"] == "proposed", "agent response is not applicable")
        question = state["request"]["payload"]["question"]
        context = _context(self.store.events(), question, current=True)
        proposal = _proposal(self.store, state, self.store.read(response["payload"]["assessment"]["artifacts"]["proposal"]))
        mapping = []
        kernel = Kernel(self.store, self.actor)
        for index, candidate in enumerate(proposal["candidates"]):
            id = kernel.hypothesis(candidate["statement"], candidate["prediction"], candidate["falsifier"],
                                   context["question"]["payload"]["scope"])
            mapping.append(dict(index=index, hypothesis=id, hash=_get(self.store.events(),id,"hypothesis")["hash"],kind=candidate["kind"]))
        selected = Planning(self.store,self.actor).explanation_set(question=question,
            hypotheses=[row["hypothesis"] for row in mapping], comparison_plan=proposal["comparison_plan"])
        return self._write("agent_application", dict(request=request, request_hash=state["request"]["hash"],
            response=response["id"], response_hash=response["hash"], mapping=mapping, explanation_set=selected,
            explanation_set_hash=_get(self.store.events(),selected,"explanation_set")["hash"], limitations=proposal["limitations"]))

    def apply_experiment(self, *, request: str) -> str:
        state = self._assigned(request)
        request_event, response = state["request"], state["response"]
        require(request_event["payload"]["schema_version"] == 2 and response is not None
                and state["application"] is None, "experiment response missing or already applied")
        require(response["payload"]["assessment"]["proposal_status"] == "proposed",
                "experiment response is not applicable")
        p = request_event["payload"]
        _experiment_current(self.store, self.store.events(), p)
        raw_key = response["payload"]["assessment"]["artifacts"]["proposal"]
        proposal = _proposal(self.store, state, self.store.read(raw_key))
        experiment = proposal["experiment"]
        require(experiment is not None, "proposed experiment is missing")
        binding = _recipe_binding(self.store, p["recipe_binding"], current=True)
        require(Path(synthetic_causal.__file__).read_bytes() == COMPILER_SOURCE_BYTES,
                "loaded experiment compiler source changed on disk")
        compiled = synthetic_causal.compile_recipe(experiment["parameters"], binding["world"])
        source_keys = dict(implementation=self.store.put(compiled["implementation"]),
            reanalysis_implementation=self.store.put(compiled["reanalysis_implementation"]),
            data=self.store.put(compiled["data"]),
            compiler_source=self.store.put(COMPILER_SOURCE_BYTES))
        summary = {key:compiled[key] for key in ("domain", "parameters", "metric", "outputs",
                    "design", "analysis_plan", "stopping_rule", "statistical_design")}
        design = compiled["design"] + "\nFrozen model contrast: " + experiment["discriminating_contrast"]
        analysis_plan = compiled["analysis_plan"] + "\nFrozen model predictions: " + canonical(
            experiment["hypothesis_predictions"]).decode()
        protocol = Kernel(self.store,self.actor).preregister_for_set(explanation_set=p["explanation_set"],
            design=design, metric=compiled["metric"], analysis_plan=analysis_plan,
            stopping_rule=compiled["stopping_rule"], seeds=binding["seeds"],
            run_limit=2*len(binding["seeds"]), implementation=source_keys["implementation"],
            environment=binding["environment"], data=source_keys["data"],
            replication_tolerance=binding["replication_tolerance"],
            statistical_design=compiled["statistical_design"])
        node = Search(self.store,self.actor).add_node(p["tree"], protocol=protocol,
            action=experiment["action"], components=experiment["components"],
            estimated_cost=2*len(binding["seeds"]), rationale=experiment["rationale"])
        history = self.store.events()
        plan_event, node_event = _get(history,protocol,"protocol"), _get(history,node,"experiment_node")
        manifest = self.store.put_json(dict(schema_version=1, compiler=experiment_proposals.RECIPE_ID,
            request=request, request_hash=request_event["hash"], response=response["id"],
            response_hash=response["hash"], proposal_digest=raw_key, recipe_binding=p["recipe_binding"],
            proposal=proposal, compiled=summary, sources=source_keys,
            protocol=plan_event, experiment_node=node_event))
        return self._write("agent_application", dict(request=request, request_hash=request_event["hash"],
            response=response["id"], response_hash=response["hash"], compilation=manifest,
            protocol=protocol, protocol_hash=plan_event["hash"], experiment_node=node,
            experiment_node_hash=node_event["hash"], limitations=proposal["limitations"],
            scientific_validity="not_assessed"), version=2)


def agent_context(store: Store, history: list[dict[str, Any]], entity_ids: set[str]) -> list[dict[str, Any]]:
    states = _index(store, history)
    included: set[str] = set()
    for state in states.values():
        applied = state["application"]
        if applied is None:
            continue
        p = applied["payload"]
        if p["schema_version"] == 2:
            if not ({p["protocol"], p["experiment_node"]} & entity_ids):
                continue
            request = state["request"]["payload"]
            included.add(request["budget"])
            included.add(request["tree"])
            included.add(p["protocol"])
            included.add(p["experiment_node"])
            included.update(state[key]["id"] for key in ("request","dispatch","response","application"))
            included.update(event["id"] for event in planning_context(history,
                _object(store.read(request["context"]))["planning_binding"]))
            continue
        if not ({p["explanation_set"], *(row["hypothesis"] for row in p["mapping"])} & entity_ids):
            continue
        included.add(state["request"]["payload"]["budget"])
        included.update(state[key]["id"] for key in ("request","dispatch","response","application"))
        question = state["request"]["payload"]["question"]
        while question is not None:
            included.add(question)
            question = _get(history, question, "research_question")["payload"]["parent"]
    return [event for event in history if event["id"] in included]


def agent_artifacts(store: Store, event: dict[str, Any]) -> set[str]:
    p = event["payload"]
    if event["kind"] == "agent_request":
        spec = _specification(store,p)
        keys = {p["provider"],p["context"],p["specification"],*spec["expected_inputs"].values()}
        if p["schema_version"] == 2:
            keys.add(p["recipe_binding"])
            keys.add(_recipe_binding(store, p["recipe_binding"], current=False)["environment"])
        return keys
    if event["kind"] == "agent_response":
        return {p["manifest"],*p["assessment"]["artifacts"].values()}
    if event["kind"] == "agent_application" and p["schema_version"] == 2:
        compiled = _object(store.read(p["compilation"]))
        return {p["compilation"], *compiled["sources"].values()}
    return set()


def agent_state(store: Store, request: str) -> dict[str, Any]:
    history = store.events()
    states = _index(store, history)
    require(request in states, "unknown agent request")
    state = states[request]
    assessment = state["response"]["payload"]["assessment"] if state["response"] else None
    status = ("applied" if state["application"] else state["response"]["payload"]["assessment"]["proposal_status"]
              if state["response"] else "unknown" if state["dispatch"] else "queued")
    application = state["application"]["payload"] if state["application"] else None
    result = dict(request=request, revision=len(history), status=status,
        response=state["response"]["id"] if state["response"] else None,
        application=state["application"]["id"] if state["application"] else None,
        explanation_set=application.get("explanation_set") if application else None,
        transport_status=assessment["transport_status"] if assessment else None,
        errors=assessment["errors"] if assessment else [], usage=assessment["usage"] if assessment else None,
        isolation="trusted local CLI; fresh context request, not authenticated or clean-room",
        scientific_validity="not_assessed")
    if state["request"]["payload"]["schema_version"] == 2:
        result.update(protocol=application.get("protocol") if application else None,
                      experiment_node=application.get("experiment_node") if application else None)
    return result
