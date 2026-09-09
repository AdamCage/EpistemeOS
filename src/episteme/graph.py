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

Construction verifies one event snapshot and every referenced artifact, plus
typed statistical declarations and recorded exposure timing. It is
not a lock against later appends or filesystem mutation; reconstruct to observe
current state. Unknown event kinds fail closed until their reference schema is
implemented. No additional database, cache, files, or events are written.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from .kernel import Actor, GateError, Kernel, protocol_exposures
from .protocols import DesignError, StatisticalDesign
from .store import IntegrityError, Store, canonical, digest


class GraphIntegrityError(IntegrityError):
    """The verified event bytes contain unresolved or inconsistent graph links."""


class NodeKind(str, Enum):
    HYPOTHESIS = "hypothesis"
    PROTOCOL = "protocol"
    RUN = "run"
    RESULT = "result"
    CLAIM = "claim"
    REVIEW = "review"
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
        protocol = self.events[claim["payload"]["protocol"]]
        runs = [e for e in self.history if e["seq"] < before and e["kind"] == "run"
                and e["payload"]["protocol"] == protocol["id"]]
        ids = {e["id"] for e in runs}
        results = [e for e in self.history if e["seq"] < before and e["kind"] == "result"
                   and e["payload"]["run"] in ids]
        preceding = [event for event in self.history if event["seq"] < before]
        return digest(canonical([protocol, claim, *runs, *results, *protocol_exposures(preceding, protocol)]))

    def project(self) -> None:
        e, p = self.event, self.event["payload"]
        kind = e["kind"]
        if kind == "hypothesis":
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
        elif kind == "review":
            claim = self.ref(p["claim"], "claim", Relation.REVIEW_TARGET, "claim")
            if self.basis(claim, e["seq"]) != p["basis_hash"]:
                self.fail("review basis does not match the recorded evidence revision")
            plan = self.events[claim["payload"]["protocol"]]
            preceding = [event for event in self.history if event["seq"] < e["seq"]]
            for exposure in protocol_exposures(preceding, plan):
                self.ref(exposure["id"], None, Relation.REVIEW_EXPOSURE, "basis_hash",
                         derivation="resolved_exposure_basis")
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
