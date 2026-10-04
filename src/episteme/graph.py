"""Read-only, typed provenance projection of the current event vocabulary.

Edges point from a dependency to the event that refers to it. Artifact nodes
identify bytes, not independent observations or proven producer processes. In
particular ``recorded_output`` points artifact -> result: a result records those
bytes; this projection cannot attest that the run actually produced them.

Evidence edges mean recorded citations, never verified scientific support. A
review verdict, search score, or mechanically intact artifact cannot change the
``not_assessed`` scientific validity of this projection. Caller-supplied actor
IDs and implementation digests do not establish independent reasoning or OS
isolation. Graph traversal does not perform claim promotion or paper eligibility.

Construction verifies one event snapshot and every referenced artifact, each
hashed once in its read scope, plus typed statistical declarations and recorded
exposure timing. It is not a lock against later appends or filesystem mutation;
reconstruct to observe current state. Unknown event kinds fail closed until their
reference schema is implemented. No database rows, files, or events are written;
verified bytes stay in memory only for the read scope.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from .kernel import Actor, GateError, Kernel, protocol_exposures
from .claim_context import resolve_context, validate_link
from .claims import ClaimLink
from .planning import validate_planning
from .protocols import DesignError, StatisticalDesign
from .store import IntegrityError, Store, canonical, digest


class GraphIntegrityError(IntegrityError):
    """The verified event bytes contain unresolved or inconsistent graph links."""


class NodeKind(str, Enum):
    AGENT_BUDGET = "agent_budget"
    AGENT_REQUEST = "agent_request"
    AGENT_DISPATCH = "agent_dispatch"
    AGENT_RESPONSE = "agent_response"
    AGENT_APPLICATION = "agent_application"
    BATCH_PLAN = "batch_plan"
    BATCH_SLOT = "batch_slot"
    BATCH_SETTLEMENT = "batch_settlement"
    BATCH_ANALYSIS = "batch_analysis"
    DOMAIN_BINDING = "domain_binding"
    PACK_BINDING = "pack_binding"
    PACK_ANALYSIS = "pack_analysis"
    EXECUTION_JOB = "execution_job"
    EXECUTION_DISPATCH = "execution_dispatch"
    EXECUTION_FINALIZED = "execution_finalized"
    RESEARCH_QUESTION = "research_question"
    EXPLANATION_SET = "explanation_set"
    HYPOTHESIS = "hypothesis"
    PROTOCOL = "protocol"
    RUN = "run"
    RESULT = "result"
    CLAIM = "claim"
    CLAIM_LINK = "claim_link"
    REVIEW = "review"
    REVIEW_ASSIGNMENT = "review_assignment"
    REVIEW_DISPATCH = "review_dispatch"
    REVIEW_RESPONSE = "review_response"
    REVIEW_SUBMISSION = "review_submission"
    REVIEW_OBLIGATION = "review_obligation"
    REVIEW_OBLIGATION_RESOLUTION = "review_obligation_resolution"
    REPLAN_FOLLOWUP = "replan_followup"
    PAPER = "paper"
    TOURNAMENT = "tournament"
    BALLOT = "tournament_ballot"
    SEARCH_TREE = "search_tree"
    EXPERIMENT_NODE = "experiment_node"
    SELECTION = "search_selection"
    TERMINAL = "search_terminal"
    ARTIFACT = "artifact"
    AFTERLIFE_SNAPSHOT = "afterlife_snapshot"
    DATA_EXPOSURE = "data_exposure"


class Relation(str, Enum):
    AGENT_REFERENCE = "agent_reference"
    AGENT_ARTIFACT = "agent_artifact"
    AGENT_GENERATED = "agent_generated"
    REVIEW_AGENT = "review_agent"
    BATCH_REFERENCE = "batch_reference"
    BATCH_ARTIFACT = "batch_artifact"
    BATCH_ANALYSIS_REFERENCE = "batch_analysis_reference"
    BATCH_ANALYSIS_ARTIFACT = "batch_analysis_artifact"
    DOMAIN_REFERENCE = "domain_binding_reference"
    DOMAIN_ARTIFACT = "domain_binding_artifact"
    PACK_REFERENCE = "pack_binding_reference"
    PACK_ARTIFACT = "pack_binding_artifact"
    PACK_ANALYSIS_REFERENCE = "pack_analysis_reference"
    PACK_ANALYSIS_ARTIFACT = "pack_analysis_artifact"
    PACK_ANALYSIS_INPUT = "pack_analysis_allowed_input"
    REVIEW_PACK = "review_pack"
    REVIEW_BATCH = "review_batch"
    EXECUTION_RUN = "execution_run"
    EXECUTION_JOB = "execution_job"
    EXECUTION_DISPATCH = "execution_dispatch"
    EXECUTION_RESULT = "execution_result"
    EXECUTION_ARTIFACT = "execution_artifact"
    REVIEW_EXECUTION = "review_execution"
    QUESTION_PARENT = "question_parent"
    EXPLANATION_QUESTION = "explanation_question"
    EXPLANATION_PARENT = "explanation_parent"
    EXPLANATION_HYPOTHESIS = "explanation_hypothesis"
    PROTOCOL_QUESTION = "protocol_question"
    PROTOCOL_EXPLANATIONS = "protocol_explanation_set"
    REVIEW_PLANNING_CONTEXT = "review_planning_context"
    REGISTERED_HYPOTHESIS = "registered_hypothesis"
    PROTOCOL_PARENT = "protocol_parent"
    RUN_PROTOCOL = "run_protocol"
    DECLARED_REANALYSIS = "declared_reanalysis_of"
    RUN_RESULT = "run_result"
    ARTIFACT_INPUT = "artifact_input"
    RECORDED_OUTPUT = "recorded_output"
    CLAIM_PROTOCOL = "claim_protocol"
    EVIDENCE_RUN = "evidence_run_reference"
    EVIDENCE_RESULT = "evidence_result_reference"
    EVIDENCE_ARTIFACT = "evidence_artifact_reference"
    REVIEW_TARGET = "review_target"
    REVIEW_ASSIGNMENT_TARGET = "review_assignment_target"
    REVIEW_ASSIGNMENT_CONTEXT = "review_assignment_context"
    REVIEW_ASSIGNMENT_ARTIFACT = "review_assignment_allowed_artifact"
    REVIEW_DELIVERY_REFERENCE = "review_delivery_reference"
    REVIEW_DELIVERY_ARTIFACT = "review_delivery_artifact"
    OBLIGATION_REVIEW = "obligation_review"
    OBLIGATION_CLAIM = "obligation_claim"
    OBLIGATION_EVIDENCE = "obligation_evidence"
    RESOLUTION_REFERENCE = "resolution_reference"
    RESOLUTION_EVIDENCE = "resolution_evidence"
    FOLLOWUP_REFERENCE = "followup_reference"
    FOLLOWUP_ARTIFACT = "followup_artifact"
    PAPER_CLAIM = "paper_claim"
    PAPER_ARTIFACT = "paper_artifact"
    SHARED_REVIEW_BASIS = "shared_review_basis"
    SOURCE_SNAPSHOT = "source_snapshot"
    TOURNAMENT_CANDIDATE = "tournament_candidate"
    BALLOT_TOURNAMENT = "ballot_tournament"
    BALLOT_CANDIDATE = "ballot_candidate"
    BALLOT_SOURCE = "ballot_source"
    TREE_TOURNAMENT = "tree_tournament"
    TREE_NODE = "tree_node"
    NODE_PROTOCOL = "node_protocol"
    NODE_PARENT = "node_parent"
    SELECTION_TREE = "selection_tree"
    SELECTION_NODE = "selection_node"
    SELECTION_PROTOCOL = "selection_protocol"
    SELECTION_FRONTIER = "selection_frontier"
    TERMINAL_TREE = "terminal_tree"
    TERMINAL_SELECTION = "terminal_selection"
    TERMINAL_NODE = "terminal_node"
    TERMINAL_RUN = "terminal_run"
    TERMINAL_CLAIM = "terminal_claim"
    SELECTION_RUN = "selection_run_reference"
    HISTORICAL_SNAPSHOT = "historical_snapshot"
    HISTORICAL_ARTIFACT = "historical_artifact"
    EXPOSED_DATA = "exposed_data"
    EXPOSURE_PROTOCOL = "exposure_protocol"
    REVIEW_EXPOSURE = "review_exposure_basis"
    LINK_SOURCE = "claim_link_source"
    LINK_TARGET = "claim_link_target"
    REVIEW_LINK = "review_claim_link"
    REVIEW_CONTEXT = "review_context_claim"
    ASSESSMENT_EVIDENCE = "assessment_evidence"
    CONTEXT_FINDING = "context_review_finding"


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


@dataclass(frozen=True)
class GraphNode:
    id: str
    kind: NodeKind
    payload: Mapping[str, Any]
    seq: int | None = None
    event_hash: str | None = None
    actor: str | None = None
    role: str | None = None
    created_at: str | None = None
    scientific_validity: str = "not_assessed"

    def to_dict(self) -> dict[str, Any]:
        return dict(id=self.id, kind=self.kind.value, payload=_thaw(self.payload),
                    seq=self.seq, event_hash=self.event_hash, actor=self.actor,
                    role=self.role, created_at=self.created_at,
                    scientific_validity=self.scientific_validity)


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    relation: Relation
    reference_event: str
    field: str
    derivation: str = "recorded_reference"
    scientific_validity: str = "not_assessed"

    def to_dict(self) -> dict[str, Any]:
        return dict(source=self.source, target=self.target, relation=self.relation.value,
                    reference_event=self.reference_event, field=self.field,
                    derivation=self.derivation, scientific_validity=self.scientific_validity)


@dataclass(frozen=True)
class ResearchGraph:
    """Immutable snapshot; traversal returns nodes in stable event/artifact order.

    ``claims(scope=...)`` requires exact scope equality, not subset matching or
    inferred scope transfer. ``ancestors`` / ``descendants`` exclude the queried
    node and optionally follow only the given relation types. A missing node ID
    raises KeyError. ``to_dict`` returns a detached, JSON-compatible copy.
    """

    revision: int
    snapshot_hash: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]

    @classmethod
    def from_store(cls, store: Store) -> ResearchGraph:
        with store.reading():
            return _Projection(store, store.events()).build()

    @staticmethod
    def artifact_id(sha256: str) -> str:
        return "artifact:sha256:" + sha256

    def node(self, id: str) -> GraphNode:
        for node in self.nodes:
            if node.id == id:
                return node
        raise KeyError(id)

    def claims(self, *, scope: Mapping[str, str] | None = None) -> tuple[GraphNode, ...]:
        if scope is not None and (not isinstance(scope, Mapping) or not scope or not all(
                isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
                for k, v in scope.items())):
            raise ValueError("scope must be a nonempty string mapping")
        return tuple(n for n in self.nodes if n.kind == NodeKind.CLAIM
                     and (scope is None or n.payload["scope"] == scope))

    def _walk(self, id: str, *, reverse: bool,
              relations: frozenset[Relation] | None) -> tuple[GraphNode, ...]:
        self.node(id)
        adjacency: dict[str, set[str]] = {}
        for edge in self.edges:
            if relations is None or edge.relation in relations:
                a, b = (edge.target, edge.source) if reverse else (edge.source, edge.target)
                adjacency.setdefault(a, set()).add(b)
        seen, pending = {id}, [id]
        while pending:
            for linked in adjacency.get(pending.pop(), ()):
                if linked not in seen:
                    seen.add(linked)
                    pending.append(linked)
        return tuple(n for n in self.nodes if n.id in seen and n.id != id)

    def ancestors(self, id: str, *, relations: frozenset[Relation] | None = None
                  ) -> tuple[GraphNode, ...]:
        return self._walk(id, reverse=True, relations=relations)

    def descendants(self, id: str, *, relations: frozenset[Relation] | None = None
                    ) -> tuple[GraphNode, ...]:
        return self._walk(id, reverse=False, relations=relations)

    def to_dict(self) -> dict[str, Any]:
        return dict(graph_version=1, revision=self.revision, snapshot_hash=self.snapshot_hash,
                    direction="dependency_to_referrer", scientific_validity="not_assessed",
                    validation="event_chain_references_and_artifact_bytes_at_construction",
                    nodes=[n.to_dict() for n in self.nodes], edges=[e.to_dict() for e in self.edges])

    def to_json(self) -> str:
        return canonical(self.to_dict()).decode("utf-8") + "\n"

    def to_dot(self) -> str:
        """DOT text only; identifiers and labels are quoted as JSON strings."""
        quote = lambda text: json.dumps(text, ensure_ascii=False)
        lines = ["digraph research {", '  label="Recorded references; scientific validity: not_assessed";']
        for node in self.nodes:
            lines.append(f"  {quote(node.id)} [label={quote(node.kind.value + ': ' + node.id)}];")
        for edge in self.edges:
            lines.append(f"  {quote(edge.source)} -> {quote(edge.target)} "
                         f"[label={quote(edge.relation.value + ': ' + edge.field)}];")
        return "\n".join([*lines, "}", ""])


class _Projection:
    def __init__(self, store: Store, history: list[dict[str, Any]]):
        self.store, self.history = store, history
        self.events = {e["id"]: e for e in history}
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []
        self.results: dict[str, dict[str, Any]] = {}
        self.event: dict[str, Any] = {}

    def fail(self, message: str) -> None:
        raise GraphIntegrityError(f"{self.event.get('id', 'graph')}: {message}")

    def ref(self, id: Any, kind: str | None, relation: Relation, field: str,
            *, target: str | None = None, derivation: str = "recorded_reference") -> dict[str, Any]:
        event = self.events.get(id) if isinstance(id, str) else None
        if event is None:
            self.fail(f"dangling event reference in {field}: {id!r}")
        if kind is not None and event["kind"] != kind:
            self.fail(f"wrong reference kind in {field}: expected {kind}, got {event['kind']}")
        if event["seq"] >= self.event["seq"]:
            self.fail(f"non-prior event reference in {field}: {id}")
        self.edges.append(GraphEdge(id, target or self.event["id"], relation,
                                    self.event["id"], field, derivation))
        return event

    def blob(self, key: str, relation: Relation, field: str,
             *, derivation: str = "recorded_reference") -> None:
        id = ResearchGraph.artifact_id(key) if isinstance(key, str) else ""
        if id in self.events:
            self.fail(f"event ID collides with artifact namespace: {id}")
        if id not in self.nodes:
            data = self.store.read(key)
            self.nodes[id] = GraphNode(id, NodeKind.ARTIFACT,
                                      _freeze(dict(sha256=key, bytes=len(data), integrity="verified")))
        self.edges.append(GraphEdge(id, self.event["id"], relation, self.event["id"], field, derivation))

    def refs(self, values: Any, kind: str | None, relation: Relation, field: str) -> None:
        if (not isinstance(values, list) or not all(isinstance(v, str) for v in values)
                or len(set(values)) != len(values)):
            self.fail(f"{field} must be a list of unique event IDs")
        for index, id in enumerate(values):
            self.ref(id, kind, relation, f"{field}[{index}]")

    def hash_ref(self, reference: dict[str, Any], expected: Any, field: str) -> None:
        if reference["hash"] != expected:
            self.fail(f"reference hash mismatch in {field}")

    def basis(self, claim: dict[str, Any], before: int) -> str:
        preceding = [event for event in self.history if event["seq"] < before]
        return Kernel(self.store, Actor("graph-reader", "observer"))._basis(preceding, claim["id"])[0]

    def project(self) -> None:
        e, p = self.event, self.event["payload"]
        kind = e["kind"]
        if kind.startswith("agent_"):
            from .agents import agent_artifacts
            fields = {
                "agent_budget": {},
                "agent_request": {"budget": "agent_budget", "question": "research_question"},
                "agent_dispatch": {"request": "agent_request"},
                "agent_response": {"request": "agent_request", "dispatch": "agent_dispatch"},
                "agent_application": {"request": "agent_request", "response": "agent_response",
                                      "explanation_set": "explanation_set"},
            }[kind]
            version = p.get("schema_version")
            if kind == "agent_request" and version == 2:
                fields.update(explanation_set="explanation_set", tree="search_tree")
            elif kind == "agent_application" and version == 2:
                fields = {"request": "agent_request", "response": "agent_response",
                          "protocol": "protocol", "experiment_node": "experiment_node"}
            for field, target_kind in fields.items():
                referenced = self.ref(p[field], target_kind, Relation.AGENT_REFERENCE, field)
                if field + "_hash" in p:
                    self.hash_ref(referenced, p[field + "_hash"], field + "_hash")
            if kind == "agent_application":
                if version == 2:
                    request = self.events[p["request"]]
                    protocol = self.events[p["protocol"]]
                    node = self.events[p["experiment_node"]]
                    if (request["payload"].get("schema_version") != 2
                            or protocol["payload"].get("planning", {}).get("explanation_set")
                            != request["payload"]["explanation_set"]
                            or node["payload"].get("tree") != request["payload"]["tree"]
                            or node["payload"].get("protocol") != p["protocol"]):
                        self.fail("agent experiment application differs from its frozen request")
                    for field in ("protocol", "experiment_node"):
                        self.ref(p["response"], "agent_response", Relation.AGENT_GENERATED,
                                 field, target=p[field], derivation="resolved_agent_application")
                else:
                    for index, row in enumerate(p["mapping"]):
                        reference = self.ref(row["hypothesis"], "hypothesis", Relation.AGENT_REFERENCE,
                                             f"mapping[{index}].hypothesis")
                        self.hash_ref(reference, row["hash"], f"mapping[{index}].hash")
                        self.ref(p["response"], "agent_response", Relation.AGENT_GENERATED,
                                 f"mapping[{index}].hypothesis", target=row["hypothesis"],
                                 derivation="resolved_agent_application")
                    self.ref(p["response"], "agent_response", Relation.AGENT_GENERATED,
                             "explanation_set", target=p["explanation_set"],
                             derivation="resolved_agent_application")
            for key in sorted(agent_artifacts(self.store, e)):
                self.blob(key, Relation.AGENT_ARTIFACT, "agent_provenance")
            return
        if kind == "domain_binding":
            from .domain_binding import binding_artifacts
            protocol = self.ref(p["protocol"], "protocol", Relation.DOMAIN_REFERENCE, "protocol")
            self.hash_ref(protocol, p["protocol_hash"], "protocol_hash")
            for key in sorted(binding_artifacts(e)):
                self.blob(key, Relation.DOMAIN_ARTIFACT, "frozen_domain_recipe")
            return
        if kind == "pack_binding":
            from .domain_packs import binding_artifacts
            protocol = self.ref(p["protocol"], "protocol", Relation.PACK_REFERENCE, "protocol")
            self.hash_ref(protocol, p["protocol_hash"], "protocol_hash")
            self.ref(p["explanation_set"], "explanation_set", Relation.PACK_REFERENCE, "explanation_set")
            for key in sorted(binding_artifacts(self.store, e)):
                self.blob(key, Relation.PACK_ARTIFACT, "pinned_pack")
            return
        if kind == "pack_analysis":
            fields = {"batch": "batch_plan", "settlement": "batch_settlement",
                      "terminal": "search_terminal", "protocol": "protocol",
                      "binding": "pack_binding", "claim": "claim"}
            for field, target_kind in fields.items():
                reference = self.ref(p[field], target_kind, Relation.PACK_ANALYSIS_REFERENCE, field)
                self.hash_ref(reference, p[field + "_hash"], field + "_hash")
            self.refs(p["runs"], "run", Relation.PACK_ANALYSIS_REFERENCE, "runs")
            self.refs(p["results"], "result", Relation.PACK_ANALYSIS_REFERENCE, "results")
            for field in ("report", "statistical_report", "checks", "recomputations"):
                self.blob(p[field], Relation.PACK_ANALYSIS_ARTIFACT, field)
            for index, key in enumerate(p["cas_allowlist"]):
                self.blob(key, Relation.PACK_ANALYSIS_INPUT, f"cas_allowlist[{index}]")
            return
        if kind == "batch_analysis":
            fields = {"batch": "batch_plan", "settlement": "batch_settlement",
                      "terminal": "search_terminal", "protocol": "protocol",
                      "claim": "claim"}
            for field, target_kind in fields.items():
                reference = self.ref(p[field], target_kind,
                                     Relation.BATCH_ANALYSIS_REFERENCE, field)
                self.hash_ref(reference, p[field + "_hash"], field + "_hash")
            self.refs(p["runs"], "run", Relation.BATCH_ANALYSIS_REFERENCE, "runs")
            self.refs(p["results"], "result", Relation.BATCH_ANALYSIS_REFERENCE, "results")
            self.blob(p["proposal_digest"], Relation.BATCH_ANALYSIS_ARTIFACT,
                      "proposal_digest")
            self.blob(p["adapter_source_digest"], Relation.BATCH_ANALYSIS_ARTIFACT,
                      "adapter_source_digest")
            return
        if kind in {"batch_plan", "batch_slot", "batch_settlement"}:
            from .batch import batch_artifacts
            fields = ({"selection": "search_selection", "node": "experiment_node",
                       "tree": "search_tree", "protocol": "protocol"} if kind == "batch_plan"
                      else {"batch": "batch_plan", "run": "run", "job": "execution_job"}
                      if kind == "batch_slot" else {"batch": "batch_plan"})
            for field, target_kind in fields.items():
                self.ref(p[field], target_kind, Relation.BATCH_REFERENCE, field)
            if kind == "batch_settlement":
                for index, slot in enumerate(p["slots"]):
                    for field, target_kind in {"binding": "batch_slot", "run": "run",
                                               "job": "execution_job", "result": "result"}.items():
                        if slot[field] is not None:
                            self.ref(slot[field], target_kind, Relation.BATCH_REFERENCE,
                                     f"slots[{index}].{field}")
            for key in sorted(batch_artifacts(self.store, e)):
                self.blob(key, Relation.BATCH_ARTIFACT, "batch_recipe")
            return
        if kind.startswith("execution_"):
            from .execution import execution_artifacts
            if kind == "execution_job":
                self.ref(p["run"], "run", Relation.EXECUTION_RUN, "run")
            else:
                self.ref(p["job"], "execution_job", Relation.EXECUTION_JOB, "job")
                if kind == "execution_finalized":
                    self.ref(p["dispatch"], "execution_dispatch", Relation.EXECUTION_DISPATCH, "dispatch")
                    self.ref(p["result"], "result", Relation.EXECUTION_RESULT, "result")
            for key in sorted(execution_artifacts(self.store, e)):
                self.blob(key, Relation.EXECUTION_ARTIFACT, "execution_provenance")
            return
        if kind == "hypothesis":
            return
        if kind in {"research_question", "explanation_set"}:
            try:
                validate_planning([event for event in self.history if event["seq"] <= e["seq"]])
            except ValueError as exc:
                self.fail(str(exc))
            if p["parent"] is not None:
                parent = self.ref(p["parent"], kind,
                    Relation.QUESTION_PARENT if kind == "research_question" else Relation.EXPLANATION_PARENT, "parent")
                self.hash_ref(parent, p["parent_hash"], "parent_hash")
            if kind == "explanation_set":
                question = self.ref(p["question"], "research_question", Relation.EXPLANATION_QUESTION, "question")
                self.hash_ref(question, p["question_hash"], "question_hash")
                self.refs(p["hypotheses"], "hypothesis", Relation.EXPLANATION_HYPOTHESIS, "hypotheses")
            return
        if kind == "afterlife_snapshot":
            if p["adapter"] != "afterlife" or p["trust"] != "historical_unverified":
                self.fail("invalid historical snapshot trust/adapter")
            self.blob(p["snapshot"], Relation.HISTORICAL_SNAPSHOT, "snapshot")
            try:
                snapshot = json.loads(self.store.read(p["snapshot"]))
            except (ValueError, UnicodeError) as exc:
                self.fail(f"invalid historical snapshot JSON: {exc}")
            if (snapshot["schema"] != "afterlife-historical-snapshot-v1"
                    or snapshot["adapter"] != "afterlife"
                    or snapshot["trust"] != "historical_unverified"):
                self.fail("unsupported historical snapshot schema/adapter/trust")
            keys = snapshot["blob_digests"]
            if (not isinstance(keys, list) or not all(isinstance(k, str) for k in keys)
                    or len(set(keys)) != len(keys)):
                self.fail("historical blob_digests must be a list of unique digests")
            for index, key in enumerate(keys):
                self.blob(key, Relation.HISTORICAL_ARTIFACT, f"snapshot.blob_digests[{index}]",
                          derivation="historical_snapshot_blob_reference")
        if kind == "protocol":
            self.refs(p["hypotheses"], "hypothesis", Relation.REGISTERED_HYPOTHESIS, "hypotheses")
            if p["parent"] is not None:
                parent = self.ref(p["parent"], "protocol", Relation.PROTOCOL_PARENT, "parent")
                if "statistical_design" in parent["payload"] and "statistical_design" not in p:
                    self.fail("typed protocol amendment cannot drop statistical design")
            preceding = [event for event in self.history if event["seq"] < e["seq"]]
            try:
                Kernel(self.store, Actor("graph-validation", "reader"))._validate_planning_protocol(preceding, p)
            except ValueError as exc:
                self.fail(str(exc))
            if "planning" in p:
                self.ref(p["planning"]["question"], "research_question", Relation.PROTOCOL_QUESTION, "planning.question")
                self.ref(p["planning"]["explanation_set"], "explanation_set", Relation.PROTOCOL_EXPLANATIONS,
                         "planning.explanation_set")
            for field in ("implementation", "environment", "data"):
                self.blob(p[field], Relation.ARTIFACT_INPUT, field)
            if "statistical_design" in p:
                try:
                    Kernel(self.store, Actor("graph-validation", "reader"))._validate_typed_protocol(
                        [event for event in self.history if event["seq"] < e["seq"]], p)
                    design = StatisticalDesign.from_dict(p["statistical_design"])
                except (DesignError, GateError) as exc:
                    self.fail(str(exc))
                for index, split in enumerate(design.data_splits):
                    self.blob(split.digest, Relation.ARTIFACT_INPUT, f"statistical_design.data_splits[{index}].digest")
                seen = p["seen_data"]
                if (not isinstance(seen, list) or not all(isinstance(key, str) for key in seen)
                        or len(set(seen)) != len(seen)):
                    self.fail("seen_data must be unique artifact digests")
                for index, key in enumerate(seen):
                    self.blob(key, Relation.EXPOSED_DATA, f"seen_data[{index}]")
        elif kind == "data_exposure":
            self.blob(p["data"], Relation.EXPOSED_DATA, "data")
            if not isinstance(p["purpose"], str) or not p["purpose"].strip():
                self.fail("data exposure lacks a purpose")
            if p["protocol"] is not None:
                self.ref(p["protocol"], "protocol", Relation.EXPOSURE_PROTOCOL, "protocol")
        elif kind == "run":
            plan = self.ref(p["protocol"], "protocol", Relation.RUN_PROTOCOL, "protocol")
            self.hash_ref(plan, p["protocol_hash"], "protocol_hash")
            if p["replicate_of"] is not None:
                self.ref(p["replicate_of"], "run", Relation.DECLARED_REANALYSIS, "replicate_of")
            for field in ("implementation", "environment"):
                self.blob(p[field], Relation.ARTIFACT_INPUT, field)
        elif kind == "result":
            self.ref(p["run"], "run", Relation.RUN_RESULT, "run")
            if p["run"] in self.results:
                self.fail("multiple terminal results for one run")
            self.results[p["run"]] = e
            for label, key in p["outputs"].items():
                self.blob(key, Relation.RECORDED_OUTPUT, f"outputs.{label}")
        elif kind == "claim":
            plan = self.ref(p["protocol"], "protocol", Relation.CLAIM_PROTOCOL, "protocol")
            if p["scope"] != plan["payload"]["scope"]:
                self.fail("claim scope differs from protocol scope")
            mode = p.get("inference_mode", plan["payload"].get("protocol_mode", "unclassified"))
            if mode not in {"unclassified", "descriptive", "exploratory", "confirmatory"}:
                self.fail("invalid claim inference mode")
            if mode == "confirmatory" and ("statistical_design" not in plan["payload"]
                                            or plan["payload"].get("protocol_mode") != "confirmatory"):
                self.fail("confirmatory inference requires a confirmatory statistical protocol")
            self.refs(p["evidence"], "run", Relation.EVIDENCE_RUN, "evidence")
            for index, run in enumerate(p["evidence"]):
                result = self.results.get(run)
                if self.events[run]["payload"]["protocol"] != plan["id"]:
                    self.fail("cross-protocol evidence reference")
                if result is None or result["payload"]["status"] != "completed":
                    self.fail("evidence reference lacks a prior completed result")
                field = f"evidence[{index}]"
                self.ref(result["id"], "result", Relation.EVIDENCE_RESULT, field,
                         derivation="resolved_run_result")
                for label, key in result["payload"]["outputs"].items():
                    self.blob(key, Relation.EVIDENCE_ARTIFACT, field + f".outputs.{label}",
                              derivation="resolved_run_result_output")
        elif kind == "claim_link":
            source = self.ref(p["source"], "claim", Relation.LINK_SOURCE, "source")
            target = self.ref(p["target"], "claim", Relation.LINK_TARGET, "target")
            preceding = [event for event in self.history if event["seq"] < e["seq"]]
            try:
                validate_link(preceding, ClaimLink.from_dict(p))
            except ValueError as exc:
                self.fail(str(exc))
            if (self.basis(source, e["seq"]) != p["source_basis"]
                    or self.basis(target, e["seq"]) != p["target_basis"]):
                self.fail("claim link basis does not match its preceding evidence revisions")
        elif kind == "review_assignment":
            claim = self.ref(p["claim"], "claim", Relation.REVIEW_ASSIGNMENT_TARGET, "claim")
            self.hash_ref(claim, p["claim_hash"], "claim_hash")
            self.blob(p["bundle"], Relation.REVIEW_ASSIGNMENT_ARTIFACT, "bundle")
            manifest = json.loads(self.store.read(p["bundle"]))
            sections = {"questions": "research_question", "explanation_sets": "explanation_set",
                        "hypotheses": "hypothesis", "claims": "claim", "claim_links": "claim_link",
                        "protocols": "protocol"}
            for section, target_kind in sections.items():
                for index, row in enumerate(manifest["context"][section]):
                    reference = self.ref(row["id"], target_kind,
                                         Relation.REVIEW_ASSIGNMENT_CONTEXT,
                                         f"bundle.context.{section}[{index}]")
                    self.hash_ref(reference, row["hash"], f"bundle.context.{section}[{index}].hash")
            for index, row in enumerate(manifest["context"]["observed_runs"]):
                run = self.ref(row["run"], "run", Relation.REVIEW_ASSIGNMENT_CONTEXT,
                               f"bundle.context.observed_runs[{index}].run")
                self.hash_ref(run, row["run_hash"], f"bundle.context.observed_runs[{index}].run_hash")
                if row["result"] is not None:
                    result = self.ref(row["result"]["id"], "result",
                                      Relation.REVIEW_ASSIGNMENT_CONTEXT,
                                      f"bundle.context.observed_runs[{index}].result")
                    self.hash_ref(result, row["result"]["hash"],
                                  f"bundle.context.observed_runs[{index}].result.hash")
            for index, key in enumerate(manifest["allowed_artifact_digests"]):
                self.blob(key, Relation.REVIEW_ASSIGNMENT_ARTIFACT,
                          f"bundle.allowed_artifact_digests[{index}]")
            for section in ("opinions", "obligations"):
                for index, row in enumerate(manifest.get("own_findings", {}).get(section, [])):
                    reference = self.ref(row["id"], None, Relation.REVIEW_ASSIGNMENT_CONTEXT,
                                         f"bundle.own_findings.{section}[{index}]")
                    self.hash_ref(reference, row["hash"], f"bundle.own_findings.{section}[{index}].hash")
        elif kind == "review_dispatch":
            assignment = self.ref(p["assignment"], "review_assignment",
                                  Relation.REVIEW_DELIVERY_REFERENCE, "assignment")
            self.hash_ref(assignment, p["assignment_hash"], "assignment_hash")
            self.ref(p["claim"], "claim", Relation.REVIEW_DELIVERY_REFERENCE, "claim")
            self.blob(p["request"], Relation.REVIEW_DELIVERY_ARTIFACT, "request")
        elif kind == "review_response":
            self.ref(p["assignment"], "review_assignment",
                     Relation.REVIEW_DELIVERY_REFERENCE, "assignment")
            dispatch = self.ref(p["dispatch"], "review_dispatch",
                                Relation.REVIEW_DELIVERY_REFERENCE, "dispatch")
            self.hash_ref(dispatch, p["dispatch_hash"], "dispatch_hash")
            self.ref(p["claim"], "claim", Relation.REVIEW_DELIVERY_REFERENCE, "claim")
            if p["response"] is not None:
                self.blob(p["response"], Relation.REVIEW_DELIVERY_ARTIFACT, "response")
        elif kind == "review_submission":
            assignment = self.ref(p["assignment"], "review_assignment",
                                  Relation.REVIEW_DELIVERY_REFERENCE, "assignment")
            self.hash_ref(assignment, p["assignment_hash"], "assignment_hash")
            dispatch = self.ref(p["dispatch"], "review_dispatch",
                                Relation.REVIEW_DELIVERY_REFERENCE, "dispatch")
            self.hash_ref(dispatch, p["dispatch_hash"], "dispatch_hash")
            completed = self.ref(p["response_event"], "review_response",
                                 Relation.REVIEW_DELIVERY_REFERENCE, "response_event")
            self.hash_ref(completed, p["response_event_hash"], "response_event_hash")
            review = self.ref(p["review"], "review",
                              Relation.REVIEW_DELIVERY_REFERENCE, "review")
            self.hash_ref(review, p["review_hash"], "review_hash")
            self.ref(p["claim"], "claim", Relation.REVIEW_DELIVERY_REFERENCE, "claim")
            self.refs(p["obligations"], "review_obligation",
                      Relation.REVIEW_DELIVERY_REFERENCE, "obligations")
            self.blob(p["response"], Relation.REVIEW_DELIVERY_ARTIFACT, "response")
            if p.get("schema_version") == 2:
                self.refs(p["resolutions"], "review_obligation_resolution",
                          Relation.REVIEW_DELIVERY_REFERENCE, "resolutions")
                for index, row in enumerate(p["withdrawals"]):
                    opinion = self.ref(row["opinion"], None, Relation.REVIEW_DELIVERY_REFERENCE,
                                       f"withdrawals[{index}].opinion")
                    self.hash_ref(opinion, row["opinion_hash"], f"withdrawals[{index}].opinion_hash")
                    if opinion["kind"] not in {"review", "review_response"}:
                        self.fail("withdrawal names an event that is not a review opinion")
        elif kind == "review":
            claim = self.ref(p["claim"], "claim", Relation.REVIEW_TARGET, "claim")
            if self.basis(claim, e["seq"]) != p["basis_hash"]:
                self.fail("review basis does not match the recorded evidence revision")
            plan = self.events[claim["payload"]["protocol"]]
            preceding = [event for event in self.history if event["seq"] < e["seq"]]
            for exposure in protocol_exposures(preceding, plan):
                self.ref(exposure["id"], None, Relation.REVIEW_EXPOSURE, "basis_hash",
                         derivation="resolved_exposure_basis")
            context = resolve_context(preceding, claim["id"])
            reader = Kernel(self.store, Actor("graph-reader", "observer"))
            for context_claim in context.claim_ids:
                for record in reader._local_evidence(preceding, context_claim)[0]:
                    if record["kind"] in {"research_question", "explanation_set", "hypothesis"}:
                        self.ref(record["id"], record["kind"], Relation.REVIEW_PLANNING_CONTEXT, "basis_hash",
                                 derivation="resolved_frozen_planning_ancestry")
                    elif record["kind"].startswith("execution_"):
                        self.ref(record["id"], record["kind"], Relation.REVIEW_EXECUTION, "basis_hash",
                                 derivation="resolved_execution_provenance")
                    elif record["kind"].startswith("agent_"):
                        self.ref(record["id"], record["kind"], Relation.REVIEW_AGENT, "basis_hash",
                                 derivation="resolved_agent_provenance")
                    elif record["kind"].startswith("batch_") or record["kind"] in {
                            "search_selection", "experiment_node", "search_tree"}:
                        self.ref(record["id"], record["kind"], Relation.REVIEW_BATCH, "basis_hash",
                                 derivation="resolved_batch_provenance")
                    elif record["kind"] in {"pack_binding", "pack_analysis"}:
                        self.ref(record["id"], record["kind"], Relation.REVIEW_PACK, "basis_hash",
                                 derivation="resolved_pack_provenance")
            version = p.get("review_schema_version", 1)
            if type(version) is not int or version not in {1, 2} or (context.link_ids and version != 2):
                self.fail("unsupported review schema or linked context lacks explicit assessments")
            if version == 2:
                admissible = set(context.link_ids)
                reader = Kernel(self.store, Actor("graph-reader", "observer"))
                for id in context.claim_ids:
                    self.ref(id, "claim", Relation.REVIEW_CONTEXT, "basis_hash",
                             derivation="resolved_claim_context")
                    for record in reader._local_evidence(preceding, id)[0]:
                        admissible.add(record["id"])
                        if record["kind"] == "protocol":
                            admissible.update(record["payload"]["hypotheses"])
                findings, open_findings = reader._context_findings(preceding, claim["id"])
                for finding in findings:
                    admissible.add(finding["id"])
                    self.ref(finding["id"], "review", Relation.CONTEXT_FINDING, "basis_hash",
                             derivation="resolved_foreign_review_context")
                try:
                    reader._validate_assessments(p["link_assessments"], set(context.link_ids), admissible, p["verdict"])
                    if p["verdict"] == "approve" and not open_findings <= {
                            id for assessment in p["link_assessments"].values() for id in assessment["evidence"]}:
                        self.fail("approval omits open reviews in its linked context")
                except GateError as exc:
                    self.fail(str(exc))
                for id, assessment in p["link_assessments"].items():
                    self.ref(id, "claim_link", Relation.REVIEW_LINK, f"link_assessments.{id}")
                    self.refs(assessment["evidence"], None, Relation.ASSESSMENT_EVIDENCE,
                              f"link_assessments.{id}.evidence")
        elif kind == "review_obligation":
            review = self.ref(p["review"], "review", Relation.OBLIGATION_REVIEW, "review")
            claim = self.ref(p["claim"], "claim", Relation.OBLIGATION_CLAIM, "claim")
            self.hash_ref(review, p["review_hash"], "review_hash")
            self.hash_ref(claim, p["claim_hash"], "claim_hash")
            if review["payload"]["claim"] != claim["id"] or review["payload"]["basis_hash"] != p["basis_hash"]:
                self.fail("obligation review, claim and evidence basis differ")
            for index, citation in enumerate(p["evidence_refs"]):
                cited = self.ref(citation["id"], None, Relation.OBLIGATION_EVIDENCE,
                                 f"evidence_refs[{index}]")
                self.hash_ref(cited, citation["hash"], f"evidence_refs[{index}].hash")
        elif kind == "replan_followup":
            from .followup import followup_artifacts
            fields = {"obligation": "review_obligation", "review": "review", "claim": "claim",
                      "tree": "search_tree", "parent_node": "experiment_node",
                      "explanation_set": "explanation_set", "protocol": "protocol",
                      "experiment_node": "experiment_node"}
            for field, target_kind in fields.items():
                reference = self.ref(p[field], target_kind, Relation.FOLLOWUP_REFERENCE, field)
                self.hash_ref(reference, p[field + "_hash"], field + "_hash")
            if (self.events[p["obligation"]]["payload"]["review"] != p["review"]
                    or self.events[p["obligation"]]["payload"]["claim"] != p["claim"]
                    or self.events[p["experiment_node"]]["payload"]["parent"] != p["parent_node"]
                    or self.events[p["experiment_node"]]["payload"]["protocol"] != p["protocol"]):
                self.fail("follow-up binding differs from its obligation or child experiment")
            for key in sorted(followup_artifacts(e)):
                self.blob(key, Relation.FOLLOWUP_ARTIFACT, "specification")
        elif kind == "review_obligation_resolution":
            fields = {"obligation": "review_obligation", "followup": "replan_followup",
                      "source_review": "review", "source_claim": "claim", "claim": "claim",
                      "review": "review", "terminal": "search_terminal"}
            if p.get("schema_version") == 2:
                fields["assignment"] = "review_assignment"
            for field, target_kind in fields.items():
                if p[field] is None and p.get("schema_version") == 2 and field in {"followup", "terminal"}:
                    if p["kind"] != "narrow_claim" or p[field + "_hash"] is not None:
                        self.fail("resolution omits the follow-up its finding kind requires")
                    continue
                reference = self.ref(p[field], target_kind, Relation.RESOLUTION_REFERENCE, field)
                self.hash_ref(reference, p[field + "_hash"], field + "_hash")
            if ((p["followup"] is not None
                 and self.events[p["followup"]]["payload"]["obligation"] != p["obligation"])
                    or self.events[p["obligation"]]["payload"]["review"] != p["source_review"]
                    or self.events[p["obligation"]]["payload"]["claim"] != p["source_claim"]
                    or self.events[p["review"]]["payload"]["claim"] != p["claim"]
                    or self.events[p["review"]]["payload"]["basis_hash"] != p["basis_hash"]):
                self.fail("resolution references disagree with the source finding or child review")
            for index, citation in enumerate(p["evidence_refs"]):
                cited = self.ref(citation["id"], None, Relation.RESOLUTION_EVIDENCE,
                                 f"evidence_refs[{index}]")
                self.hash_ref(cited, citation["hash"], f"evidence_refs[{index}].hash")
        elif kind == "paper":
            self.refs(p["claims"], "claim", Relation.PAPER_CLAIM, "claims")
            if set(p["reviewed_bases"]) != set(p["claims"]):
                self.fail("paper reviewed bases do not cover exactly its claims")
            if p["source_snapshot"] != e["previous_hash"]:
                self.fail("paper source snapshot does not match its prior revision")
            source = self.history[e["seq"] - 2]
            self.ref(source["id"], None, Relation.SOURCE_SNAPSHOT, "source_snapshot")
            for field in ("manuscript", "bundle"):
                self.blob(p[field], Relation.PAPER_ARTIFACT, field)
            for claim_id, basis in p["reviewed_bases"].items():
                if self.basis(self.events[claim_id], e["seq"]) != basis:
                    self.fail(f"paper basis mismatch for {claim_id}")
                for review in self.history[:e["seq"] - 1]:
                    if (review["kind"] == "review" and review["payload"]["claim"] == claim_id
                            and review["payload"]["basis_hash"] == basis):
                        self.ref(review["id"], "review", Relation.SHARED_REVIEW_BASIS,
                                 f"reviewed_bases.{claim_id}", derivation="shared_basis_not_approval")
        elif kind == "tournament":
            self.refs(p["candidates"], "hypothesis", Relation.TOURNAMENT_CANDIDATE, "candidates")
            if set(p["candidate_hashes"]) != set(p["candidates"]):
                self.fail("candidate hash references do not match candidates")
            for id, expected in p["candidate_hashes"].items():
                self.hash_ref(self.events[id], expected, f"candidate_hashes.{id}")
        elif kind == "tournament_ballot":
            plan = self.ref(p["tournament"], "tournament", Relation.BALLOT_TOURNAMENT, "tournament")
            for field in ("a", "b"):
                self.ref(p[field], "hypothesis", Relation.BALLOT_CANDIDATE, field)
                if p[field] not in plan["payload"]["candidates"]:
                    self.fail(f"ballot {field} is outside the tournament candidate pool")
            if p["winner"] not in (None, p["a"], p["b"]):
                self.fail("ballot winner is not one of the compared candidates")
            self.refs(p["sources"], None, Relation.BALLOT_SOURCE, "sources")
        elif kind == "search_tree":
            if p["tournament"] is not None:
                self.ref(p["tournament"], "tournament", Relation.TREE_TOURNAMENT, "tournament")
        elif kind == "experiment_node":
            self.ref(p["tree"], "search_tree", Relation.TREE_NODE, "tree")
            plan = self.ref(p["protocol"], "protocol", Relation.NODE_PROTOCOL, "protocol")
            self.hash_ref(plan, p["protocol_hash"], "protocol_hash")
            if p["parent"] is not None:
                parent = self.ref(p["parent"], "experiment_node", Relation.NODE_PARENT, "parent")
                if parent["payload"]["tree"] != p["tree"]:
                    self.fail("node parent belongs to another tree")
        elif kind == "search_selection":
            self.ref(p["tree"], "search_tree", Relation.SELECTION_TREE, "tree")
            if p["node"] is not None:
                node = self.ref(p["node"], "experiment_node", Relation.SELECTION_NODE, "node")
                self.ref(p["protocol"], "protocol", Relation.SELECTION_PROTOCOL, "protocol")
                if (node["payload"]["tree"] != p["tree"]
                        or node["payload"]["protocol"] != p["protocol"]):
                    self.fail("selection tree/protocol does not match its node")
            elif p["protocol"] is not None:
                self.fail("wait/stop selection cannot reference a protocol")
            for index, item in enumerate(p["frontier"]):
                node = self.ref(item["node"], "experiment_node", Relation.SELECTION_FRONTIER,
                                f"frontier[{index}].node")
                if node["payload"]["tree"] != p["tree"]:
                    self.fail("frontier node belongs to another tree")
        elif kind == "search_terminal":
            if "batch" in p:
                self.ref(p["batch"], "batch_plan", Relation.BATCH_REFERENCE, "batch")
                self.ref(p["settlement"], "batch_settlement", Relation.BATCH_REFERENCE, "settlement")
                self.refs(p["runs"], "run", Relation.TERMINAL_RUN, "runs")
                self.refs(p["results"], "result", Relation.EXECUTION_RESULT, "results")
            self.ref(p["tree"], "search_tree", Relation.TERMINAL_TREE, "tree")
            selected = self.ref(p["selection"], "search_selection", Relation.TERMINAL_SELECTION,
                                "selection")
            self.ref(p["node"], "experiment_node", Relation.TERMINAL_NODE, "node")
            if p["tree"] != selected["payload"]["tree"] or p["node"] != selected["payload"]["node"]:
                self.fail("terminal tree/node does not match its selection")
            if p["run"] is not None:
                run = self.ref(p["run"], "run", Relation.TERMINAL_RUN, "run")
                if run["payload"]["protocol"] != selected["payload"]["protocol"]:
                    self.fail("terminal run protocol does not match selection")
                if run["seq"] <= selected["seq"]:
                    self.fail("terminal run precedes selection")
                self.ref(selected["id"], "search_selection", Relation.SELECTION_RUN, "run",
                         target=run["id"], derivation="resolved_terminal_selection_run")
            if p["claim"] is not None:
                claim = self.ref(p["claim"], "claim", Relation.TERMINAL_CLAIM, "claim")
                if (claim["payload"]["protocol"] != selected["payload"]["protocol"]
                        or p["run"] not in claim["payload"]["evidence"]):
                    self.fail("terminal claim does not cite selected run/protocol")

    def build(self) -> ResearchGraph:
        # A forged review.assign receipt can point to another supported event
        # kind; checking only for assignment events would silently skip it.
        from .review_assignment import _index as assignment_index
        from .batch_analysis import _index as analysis_index
        from .domain_binding import _index as binding_index
        from .reviewer_controller import _index as delivery_index
        from .review_submission import _index as submission_index
        receipts = self.store._verified_receipts(self.history)
        try:
            binding_index(self.store, self.history, receipts=receipts)
        except (ValueError, KeyError, TypeError) as exc:
            self.fail(f"invalid domain binding history: {exc}")
        try:
            analysis_index(self.store, self.history, receipts=receipts)
        except (ValueError, KeyError, TypeError) as exc:
            self.fail(f"invalid batch analysis history: {exc}")
        if (any(e["kind"] in {"pack_binding", "pack_analysis"} for e in self.history)
                or any(r["request"]["action"] in {"pack.preregister", "pack.analyse"} for r in receipts)):
            from .domain_packs import _analysis_index as pack_analysis_index
            from .domain_packs import _binding_index as pack_binding_index
            try:
                pack_binding_index(self.store, self.history, receipts=receipts)
                pack_analysis_index(self.store, self.history, receipts=receipts)
            except (ValueError, KeyError, TypeError) as exc:
                self.fail(f"invalid pack history: {exc}")
        try:
            assignment_index(self.store, self.history, receipts=receipts)
        except (ValueError, KeyError, TypeError) as exc:
            self.fail(f"invalid review assignment history: {exc}")
        try:
            delivery_index(self.store, self.history, receipts=receipts)
        except (ValueError, KeyError, TypeError) as exc:
            self.fail(f"invalid review delivery history: {exc}")
        try:
            submission_index(self.store, self.history, receipts=receipts)
        except (ValueError, KeyError, TypeError) as exc:
            self.fail(f"invalid review submission history: {exc}")
        if any(e["kind"] == "review_obligation_resolution" for e in self.history):
            from .resolution import _index as resolution_index
            try:
                resolution_index(self.store, self.history)
            except (ValueError, KeyError, TypeError) as exc:
                self.fail(f"invalid review obligation resolution history: {exc}")
        if any(e["kind"] == "replan_followup" for e in self.history):
            from .followup import _index as followup_index
            try:
                followup_index(self.store, self.history)
            except (ValueError, KeyError, TypeError) as exc:
                self.fail(f"invalid follow-up history: {exc}")
        if any(e["kind"] == "review_obligation" for e in self.history):
            from .replanning import _index as replanning_index
            try:
                replanning_index(self.store, self.history, receipts=receipts)
            except (ValueError, KeyError, TypeError) as exc:
                self.fail(f"invalid replanning history: {exc}")
        if any(e["kind"].startswith("agent_") for e in self.history):
            from .agents import agent_context
            try:
                agent_context(self.store, self.history, set())
            except (ValueError, KeyError, TypeError) as exc:
                self.fail(f"invalid agent history: {exc}")
        if any(e["kind"].startswith("batch_") for e in self.history):
            from .batch import batch_context
            try:
                batch_context(self.store, self.history, set())
            except (ValueError, KeyError, TypeError) as exc:
                self.fail(f"invalid batch history: {exc}")
        if any(e["kind"].startswith("execution_") for e in self.history):
            from .execution import execution_context
            try:
                execution_context(self.store, self.history, set())
            except (ValueError, KeyError, TypeError) as exc:
                self.fail(f"invalid execution history: {exc}")
        for event in self.history:
            self.event = event
            try:
                kind = NodeKind(event["kind"])
            except ValueError:
                self.fail(f"unsupported event kind: {event['kind']}")
            if kind == NodeKind.ARTIFACT or not isinstance(event["payload"], dict):
                self.fail("unsupported event payload or artifact event")
            self.nodes[event["id"]] = GraphNode(event["id"], kind, _freeze(event["payload"]),
                                               event["seq"], event["hash"], event["actor"],
                                               event["role"], event["created_at"])
            try:
                self.project()
            except (KeyError, TypeError, AttributeError) as exc:
                self.fail(f"malformed {event['kind']} reference schema: {exc}")
        nodes = tuple(sorted(self.nodes.values(), key=lambda n: (
            n.seq is None, n.seq or 0, n.id)))
        return ResearchGraph(len(self.history), self.history[-1]["hash"] if self.history else "0" * 64,
                             nodes, tuple(self.edges))
