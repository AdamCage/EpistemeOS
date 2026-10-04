"""Receipt-backed, mechanical binding of a trusted domain recipe to a protocol.

The planner declares domain-owned recipe bytes and exact execution sources before
the protocol runs. This records provenance and batch compatibility, not scientific
validity, independent authorship, or an approval of the recipe's assumptions.
"""

from __future__ import annotations

import re
from typing import Any

from .batch import _recipe as validate_batch_recipe
from .kernel import Actor, Kernel, require
from .store import Store, canonical, digest


KIND = "domain_binding"
_REQUEST_FIELDS = {"protocol", "adapter_id", "adapter_version", "adapter_source_digest",
                   "recipe", "recipe_artifacts", "reanalysis_implementation",
                   "reanalysis_environment", "outputs", "wall_seconds", "max_output_bytes",
                   "required_capabilities"}
_EVENT_FIELDS = {"schema_version", "protocol", "protocol_hash", "study_id", "adapter_id",
                 "adapter_version", "adapter_source_digest", "recipe_digest", "recipe_artifacts",
                 "primary_implementation", "primary_environment", "data",
                 "reanalysis_implementation", "reanalysis_environment", "outputs",
                 "wall_seconds", "max_output_bytes", "required_capabilities",
                 "scientific_validity"}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ADAPTER = re.compile(r"[a-z][a-z0-9_]{1,63}\Z")


def _expected(store: Store, before: list[dict[str, Any]], request: dict[str, Any],
              study_id: str) -> dict[str, Any]:
    """Recompute a binding from its original command prefix and CAS bytes."""
    require(type(request) is dict and set(request) == _REQUEST_FIELDS,
            "invalid domain binding request fields")
    protocol = Kernel._get(before, request["protocol"], "protocol")
    p = protocol["payload"]
    planning = p.get("planning")
    require(type(planning) is dict and planning.get("study_id") == study_id,
            "domain binding needs a planning-bound protocol in its command study")
    require(not any(event["kind"] == KIND and event["payload"].get("protocol") == protocol["id"]
                    for event in before), "protocol already has a domain binding")
    require(not any(event["kind"] == "pack_binding" and event["payload"].get("protocol") == protocol["id"]
                    for event in before), "protocol already has a pack binding")
    require(not any(event["kind"] == "run" and event["payload"].get("protocol") == protocol["id"]
                    or event["kind"] == "batch_plan" and event["payload"].get("protocol") == protocol["id"]
                    for event in before), "domain binding must precede every run and batch plan")
    require(not any(event["kind"] == "agent_application"
                    and event["payload"].get("protocol") == protocol["id"] for event in before),
            "protocol already has a model-applied domain recipe")
    adapter_id, version = request["adapter_id"], request["adapter_version"]
    require(type(adapter_id) is str and _ADAPTER.fullmatch(adapter_id) is not None,
            "invalid domain adapter ID")
    require(type(version) is str and 0 < len(version) <= 64 and version.strip() == version,
            "invalid domain adapter version")
    source = request["adapter_source_digest"]
    require(type(source) is str and _DIGEST.fullmatch(source) is not None
            and 0 < len(store.read(source)) <= 1024 * 1024,
            "domain adapter source must be a bounded CAS artifact")
    recipe = request["recipe"]
    require(type(recipe) is dict and bool(recipe) and len(canonical(recipe)) <= 1024 * 1024,
            "domain recipe must be a nonempty bounded JSON object")
    artifacts = request["recipe_artifacts"]
    require(type(artifacts) is list and len(artifacts) <= 256
            and all(type(key) is str and _DIGEST.fullmatch(key) is not None for key in artifacts)
            and len(set(artifacts)) == len(artifacts),
            "domain recipe artifacts must be unique CAS digests")
    for key in artifacts:
        store.read(key)
    # The domain adapter interprets recipe and artifact contents. The universal
    # binding only verifies their identity and the local batch execution shape.
    require(request["reanalysis_implementation"] != p["implementation"],
            "domain reanalysis needs a distinct implementation source")
    execution = {key: request[key] for key in
                 ("reanalysis_implementation", "reanalysis_environment", "outputs",
                  "wall_seconds", "max_output_bytes", "required_capabilities")}
    validate_batch_recipe(store, execution, p)
    return dict(schema_version=1, protocol=protocol["id"], protocol_hash=protocol["hash"],
                study_id=study_id, adapter_id=adapter_id, adapter_version=version,
                adapter_source_digest=source, recipe_digest=digest(canonical(recipe)),
                recipe_artifacts=artifacts, primary_implementation=p["implementation"],
                primary_environment=p["environment"], data=p["data"],
                **execution, scientific_validity="not_assessed")


def _index(store: Store, history: list[dict[str, Any]], *,
           receipts: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Verify each exact command/event pair against its historical input prefix."""
    source = store.receipts() if receipts is None else receipts
    relevant = [row for row in source if row["request"]["action"] == "domain.bind"
                and row["after_revision"] <= len(history)]
    events = {event["id"]: event for event in history if event["kind"] == KIND}
    by_protocol: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for row in relevant:
        before = history[:row["before_revision"]]
        created = history[row["before_revision"]:row["after_revision"]]
        context, command = row["context"], row["request"]
        require(command["version"] == 1 and context["role"] == "planner"
                and len(created) == 1 and row["event_ids"] == [created[0]["id"]]
                and created[0]["kind"] == KIND,
                "domain binding needs one complete planner command receipt")
        event = created[0]
        require(event["actor"] == context["actor"] and event["role"] == "planner"
                and row["result"] == event["id"],
                "domain binding actor or receipt result mismatch")
        expected = _expected(store, before, command["payload"], context["study_id"])
        require(set(event["payload"]) == _EVENT_FIELDS and event["payload"] == expected,
                "domain binding differs from its frozen command and protocol")
        require(store.read(expected["recipe_digest"]) == canonical(command["payload"]["recipe"]),
                "domain binding recipe bytes differ from its request")
        seen.add(event["id"])
        require(expected["protocol"] not in by_protocol, "duplicate domain binding for protocol")
        by_protocol[expected["protocol"]] = event
    require(seen == set(events), "domain binding lacks its original command receipt")
    return by_protocol


def binding_artifacts(event: dict[str, Any]) -> set[str]:
    """CAS closure for backup and graph projection."""
    p = event["payload"]
    return {p["adapter_source_digest"], p["recipe_digest"],
            p["primary_implementation"], p["primary_environment"], p["data"],
            p["reanalysis_implementation"], p["reanalysis_environment"],
            *p["recipe_artifacts"]}


class DomainBinding:
    def __init__(self, store: Store, actor: Actor):
        self.store, self.actor = store, actor

    def bind(self, *, protocol: str, adapter_id: str, adapter_version: str,
             adapter_source_digest: str, recipe: dict[str, Any], recipe_artifacts: list[str],
             reanalysis_implementation: str, reanalysis_environment: str,
             outputs: dict[str, str], wall_seconds: int, max_output_bytes: int,
             required_capabilities: list[str]) -> str:
        require(self.store._command_context is not None and self.actor.role == "planner",
                "domain binding requires a planner CommandService transaction")
        history = self.store.events()
        _index(self.store, history)
        request = dict(protocol=protocol, adapter_id=adapter_id, adapter_version=adapter_version,
                       adapter_source_digest=adapter_source_digest, recipe=recipe,
                       recipe_artifacts=recipe_artifacts,
                       reanalysis_implementation=reanalysis_implementation,
                       reanalysis_environment=reanalysis_environment, outputs=outputs,
                       wall_seconds=wall_seconds, max_output_bytes=max_output_bytes,
                       required_capabilities=required_capabilities)
        payload = _expected(self.store, history, request,
                            self.store._command_context["study_id"])
        require(self.store.put_json(recipe) == payload["recipe_digest"],
                "domain recipe CAS digest mismatch")
        return Kernel(self.store, self.actor)._write(history, KIND, payload, {"planner"})
