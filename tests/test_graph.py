"""Reference projection tests; fixture verdicts are not scientific assessments."""

import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from episteme.graph import GraphIntegrityError, NodeKind, Relation, ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.reporting import PaperBuilder
from episteme.search import COMPONENTS, Search
from episteme.store import IntegrityError, Store


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="episteme-graph-")
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.addCleanup(self.store.close)
        self.planner = Kernel(self.store, Actor("planner", "planner"))
        self.executor = Kernel(self.store, Actor("executor", "executor"))
        self.analyst = Kernel(self.store, Actor("analyst", "analyst"))
        self.scope = {"population": "synthetic", "dataset": "fixture-v1"}
        self.code = self.store.put(b"fixture primary source")
        self.environment = self.store.put(b"fixture environment")
        self.raw = self.store.put_json({"values": [-1, 1]})
        self.hypotheses = [self.planner.hypothesis(x, x, x, self.scope) for x in ("positive", "null")]

    def protocol(self, *, parent=None, scope=None):
        scope = self.scope if scope is None else scope
        hypotheses = self.hypotheses if scope == self.scope else [
            self.planner.hypothesis(x, x, x, scope) for x in ("positive", "null")]
        return self.planner.preregister(
            hypotheses=hypotheses, scope=scope, design="Fixture comparison", metric="mean",
            analysis_plan="Mean of all rows", stopping_rule="Fixed schedule", seeds=[7],
            run_limit=10, implementation=self.code, environment=self.environment, data=self.raw,
            replication_tolerance=0.01, parent=parent)

    def execute(self, protocol, *, replicate_of=None, status="completed"):
        actor = (Kernel(self.store, Actor("replicator", "replicator"))
                 if replicate_of else self.executor)
        run = actor.start_run(protocol, seed=7, implementation=(self.store.put(b"fixture reanalysis")
            if replicate_of else self.code), environment=self.environment,
            command=["not-executed-test-fixture"], replicate_of=replicate_of)
        outputs = (dict(raw_data=self.raw, metrics=self.store.put_json({"mean": 0.0}),
                        log=self.store.put(b"fixture completed")) if status == "completed"
                   else dict(log=self.store.put(b"fixture failed attempt log")))
        result = actor.finish_run(run, status=status, outputs=outputs, reason="Fixture terminal state")
        return run, result

    def claim(self, protocol, evidence, *, outcome="inconclusive", scope=None):
        return self.analyst.claim(protocol=protocol, statement="Fixture estimate is inconclusive",
            evidence=evidence, scope=self.scope if scope is None else scope,
            limitations=["Synthetic fixture; no population inference"], outcome=outcome)

    def paper_fixture(self):
        protocol = self.protocol()
        primary, result = self.execute(protocol)
        replica, replica_result = self.execute(protocol, replicate_of=primary)
        claim = self.claim(protocol, [primary, replica])
        reviewer = Kernel(self.store, Actor("reviewer", "reviewer"))
        basis = reviewer.gate(claim)["basis_hash"]
        review = reviewer.review(claim, verdict="approve", rationale="Test-only reference fixture",
                                 actions=[], expected_basis=basis)
        paper = PaperBuilder(self.store, Actor("writer", "writer")).build(
            title="Reference graph fixture", claims=[claim], expected_bases={claim: basis})
        return protocol, primary, result, replica, replica_result, claim, review, paper

    def append_raw(self, kind, payload, *, id="malformed-fixture"):
        return self.store.append(id=id, kind=kind, actor="fixture", role="fixture", payload=payload,
                                 expected_revision=len(self.store.events()))

    def edge_exists(self, graph, source, target, relation):
        self.assertTrue(any(e.source == source and e.target == target and e.relation == relation
                            for e in graph.edges), (source, target, relation))

    def test_paper_paths_are_typed_recorded_citations_without_scientific_promotion(self):
        protocol, primary, result, replica, replica_result, claim, review, paper = self.paper_fixture()
        graph = ResearchGraph.from_store(self.store)
        expected = [*self.hypotheses, protocol, primary, result, replica, replica_result, claim, review]
        self.assertTrue(set(expected) <= {n.id for n in graph.ancestors(paper)})
        self.edge_exists(graph, self.hypotheses[0], protocol, Relation.REGISTERED_HYPOTHESIS)
        self.edge_exists(graph, protocol, primary, Relation.RUN_PROTOCOL)
        self.edge_exists(graph, primary, result, Relation.RUN_RESULT)
        self.edge_exists(graph, result, claim, Relation.EVIDENCE_RESULT)
        self.edge_exists(graph, claim, review, Relation.REVIEW_TARGET)
        self.edge_exists(graph, review, paper, Relation.SHARED_REVIEW_BASIS)
        self.edge_exists(graph, primary, replica, Relation.DECLARED_REANALYSIS)
        self.assertEqual(graph.node(claim).payload["outcome"], "inconclusive")
        self.assertEqual(graph.node(review).payload["verdict"], "approve")
        self.assertTrue(all(n.scientific_validity == "not_assessed" for n in graph.nodes))
        self.assertTrue(all(e.scientific_validity == "not_assessed" for e in graph.edges))
        self.assertEqual(next(e for e in graph.edges if e.relation == Relation.SHARED_REVIEW_BASIS
                              ).derivation, "shared_basis_not_approval")
        raw = ResearchGraph.artifact_id(self.raw)
        self.assertEqual(graph.node(raw).kind, NodeKind.ARTIFACT)
        self.assertEqual(graph.ancestors(raw), ())
        self.assertIn(paper, {n.id for n in graph.descendants(raw)})
        self.assertEqual(sum(n.id == raw for n in graph.nodes), 1)

    def test_graph_keeps_missing_replication_and_failed_negative_history_visible(self):
        protocol = self.protocol()
        failed, result = self.execute(protocol, status="failed")
        primary, _ = self.execute(protocol)
        claim = self.claim(protocol, [primary], outcome="refutes")
        graph = ResearchGraph.from_store(self.store)
        self.assertEqual(graph.node(result).payload["status"], "failed")
        self.assertIn(result, {n.id for n in graph.descendants(failed)})
        self.assertFalse(self.analyst.gate(claim)["passed"])
        self.assertEqual(graph.claims()[0].payload["outcome"], "refutes")
        self.assertEqual(graph.claims()[0].scientific_validity, "not_assessed")

    def test_claim_scope_requires_exact_equality_without_generalization(self):
        protocol = self.protocol()
        primary, _ = self.execute(protocol)
        claim = self.claim(protocol, [primary])
        other_scope = dict(self.scope, dataset="fixture-v2")
        other_protocol = self.protocol(scope=other_scope)
        other_run, _ = self.execute(other_protocol)
        other_claim = self.claim(other_protocol, [other_run], scope=other_scope)
        graph = ResearchGraph.from_store(self.store)
        self.assertEqual([n.id for n in graph.claims(scope=dict(reversed(list(self.scope.items()))))], [claim])
        self.assertEqual([n.id for n in graph.claims(scope=other_scope)], [other_claim])
        self.assertEqual(graph.claims(scope={"population": "synthetic"}), ())
        self.assertEqual(graph.claims(scope=dict(self.scope, context="new")), ())
        for scope in ({}, {"population": 1}, "synthetic"):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                graph.claims(scope=scope)

    def test_historical_review_and_paper_remain_when_later_evidence_changes_basis(self):
        protocol, *_, claim, review, paper = self.paper_fixture()
        previous = ResearchGraph.from_store(self.store)
        later, _ = self.execute(protocol, status="cancelled")
        current = ResearchGraph.from_store(self.store)
        self.assertEqual(previous.node(paper), current.node(paper))
        self.assertEqual(previous.node(review), current.node(review))
        self.assertNotIn(later, {n.id for n in current.ancestors(paper)})
        self.assertNotEqual(previous.snapshot_hash, current.snapshot_hash)
        self.assertEqual(self.analyst.next_action(claim)["action"], "scientific_review")

    def test_reconstruction_and_exports_are_deterministic_detached_and_read_only(self):
        self.paper_fixture()
        history = self.store.events()
        graph = ResearchGraph.from_store(self.store)
        with Store(self.directory.name, read_only=True) as reader:
            with patch.object(reader, "append", side_effect=AssertionError("write")), \
                    patch.object(reader, "put", side_effect=AssertionError("write")):
                reopened = ResearchGraph.from_store(reader)
        self.assertEqual(graph.to_json(), reopened.to_json())
        self.assertEqual(graph.to_dict(), json.loads(graph.to_json()))
        self.assertEqual(self.store.events(), history)
        exported = graph.to_dict()
        exported["nodes"][0]["payload"]["scope"]["dataset"] = "mutated export"
        self.assertEqual(graph.node(self.hypotheses[0]).payload["scope"]["dataset"], "fixture-v1")
        with self.assertRaises(TypeError):
            graph.node(self.hypotheses[0]).payload["scope"]["dataset"] = "changed"
        with self.assertRaises(FrozenInstanceError):
            graph.revision = 0
        self.assertTrue(graph.to_dot().startswith("digraph research {"))
        self.assertIn("scientific validity: not_assessed", graph.to_dot())

    def test_single_snapshot_ignores_concurrent_append_and_requires_explicit_rebuild(self):
        self.protocol()
        snapshot = self.store.events()
        original_read = self.store.read
        appended = False

        def read_and_append(key):
            nonlocal appended
            if not appended:
                appended = True
                with Store(self.directory.name) as writer:
                    Kernel(writer, Actor("planner-2", "planner")).hypothesis(
                        "Later idea", "Later prediction", "Later falsifier", self.scope)
            return original_read(key)

        with patch.object(self.store, "events", return_value=snapshot) as read_events, \
                patch.object(self.store, "read", side_effect=read_and_append):
            graph = ResearchGraph.from_store(self.store)
        read_events.assert_called_once_with()
        self.assertEqual(graph.revision, len(snapshot))
        self.assertEqual(graph.snapshot_hash, snapshot[-1]["hash"])
        self.assertEqual(ResearchGraph.from_store(self.store).revision, graph.revision + 1)

    def test_artifact_loss_and_corruption_fail_even_for_failed_attempts(self):
        protocol = self.protocol()
        self.execute(protocol, status="failed")
        key = self.store.events()[-1]["payload"]["outputs"]["log"]
        path = self.store.blobs / key
        content = path.read_bytes()
        path.write_bytes(b"corrupt")
        with self.assertRaisesRegex(IntegrityError, "hash mismatch"):
            ResearchGraph.from_store(self.store)
        path.unlink()
        with self.assertRaisesRegex(IntegrityError, "missing artifact"):
            ResearchGraph.from_store(self.store)
        path.write_bytes(content)
        ResearchGraph.from_store(self.store)

    def test_dangling_reference_fails_despite_valid_event_hash_chain(self):
        self.append_raw("result", dict(run="missing-run", status="failed", outputs={}, reason="fixture"))
        self.assertEqual(len(self.store.events()), 3)
        with self.assertRaisesRegex(GraphIntegrityError, "dangling.*run"):
            ResearchGraph.from_store(self.store)

    def test_wrong_reference_kind_is_not_treated_as_a_run(self):
        self.append_raw("result", dict(run=self.hypotheses[0], outputs={}))
        with self.assertRaisesRegex(GraphIntegrityError, "wrong reference kind.*expected run"):
            ResearchGraph.from_store(self.store)

    def test_changed_preregistration_hash_is_rejected(self):
        protocol = self.protocol()
        self.append_raw("run", dict(protocol=protocol, protocol_hash="0" * 64))
        with self.assertRaisesRegex(GraphIntegrityError, "reference hash mismatch"):
            ResearchGraph.from_store(self.store)

    def test_forward_parent_or_cycle_is_rejected(self):
        protocol = self.protocol()
        payload = dict(self.store.events()[-1]["payload"], parent="later-protocol")
        self.append_raw("protocol", payload)
        self.append_raw("protocol", dict(payload, parent=protocol), id="later-protocol")
        with self.assertRaisesRegex(GraphIntegrityError, "non-prior event reference.*parent"):
            ResearchGraph.from_store(self.store)

    def test_forged_review_basis_is_rejected(self):
        protocol = self.protocol()
        primary, _ = self.execute(protocol)
        claim = self.claim(protocol, [primary])
        self.append_raw("review", dict(claim=claim, basis_hash="0" * 64))
        with self.assertRaisesRegex(GraphIntegrityError, "review basis"):
            ResearchGraph.from_store(self.store)

    def test_duplicate_terminal_results_and_unknown_kinds_fail_closed(self):
        protocol = self.protocol()
        self.execute(protocol)
        self.append_raw("result", self.store.events()[-1]["payload"])
        with self.assertRaisesRegex(GraphIntegrityError, "multiple terminal"):
            ResearchGraph.from_store(self.store)
        with tempfile.TemporaryDirectory() as root, Store(root) as store:
            store.append(id="future", kind="unknown-schema", actor="fixture", role="fixture",
                         payload={}, expected_revision=0)
            with self.assertRaisesRegex(GraphIntegrityError, "unsupported event kind"):
                ResearchGraph.from_store(store)

    def test_tournament_search_lineage_and_selected_run_references(self):
        search = Search(self.store, Actor("planner", "planner"))
        tournament = search.register_tournament(candidates=self.hypotheses, rubric="fixture-v1")
        pairing = search.pairings(tournament)[0]
        ballot = Search(self.store, Actor("judge", "judge")).ballot(
            tournament, **pairing, verdict="tie", rationale="Fixture tie", sources=[self.hypotheses[0]],
            judge=dict(model="fixture", model_version="v1", prompt_version="v1"))
        tree = search.register_tree(weights={k: 1 for k in COMPONENTS}, cost_weight=0.1,
            budget=10, cost_unit="fixture", max_nodes=5, max_depth=2, max_width=2,
            max_selections=5, tournament=tournament)
        protocol = self.protocol()
        components = {k: 0.5 for k in COMPONENTS}
        node = search.add_node(tree, protocol=protocol, action="discriminate", components=components,
                               estimated_cost=1, rationale="Fixture discrimination")
        selection = search.select_next(tree)
        failed, _ = self.execute(protocol, status="failed")
        terminal = search.finish_selection(selection["id"], status="failed", actual_cost=1,
                                           reason="Recorded failed fixture", run=failed)
        retry = search.add_node(tree, protocol=protocol, action="retry", parent=node,
                                components=components, estimated_cost=1, rationale="Fixture retry")
        second = search.select_next(tree)
        primary, _ = self.execute(protocol)
        claim = self.claim(protocol, [primary])
        search.finish_selection(second["id"], status="completed", actual_cost=1,
                                reason="Recorded inconclusive fixture", run=primary, claim=claim)
        stop = search.select_next(tree)
        graph = ResearchGraph.from_store(self.store)
        self.edge_exists(graph, tournament, ballot, Relation.BALLOT_TOURNAMENT)
        self.edge_exists(graph, tournament, tree, Relation.TREE_TOURNAMENT)
        self.edge_exists(graph, tree, node, Relation.TREE_NODE)
        self.edge_exists(graph, node, retry, Relation.NODE_PARENT)
        self.edge_exists(graph, selection["id"], failed, Relation.SELECTION_RUN)
        self.edge_exists(graph, failed, terminal, Relation.TERMINAL_RUN)
        self.assertIn(claim, {n.id for n in graph.descendants(tree)})
        self.assertIsNone(graph.node(stop["id"]).payload["node"])
        self.assertEqual([n.id for n in graph.ancestors(retry, relations=frozenset({Relation.NODE_PARENT}))],
                         [node])
        self.assertNotIn("supports", {edge.relation.value for edge in graph.edges})
        with self.assertRaises(KeyError):
            graph.ancestors("unknown")

    def test_empty_store_has_stable_empty_projection(self):
        with tempfile.TemporaryDirectory() as root, Store(root) as store:
            graph = ResearchGraph.from_store(store)
            self.assertEqual((graph.revision, graph.snapshot_hash, graph.nodes, graph.edges),
                             (0, "0" * 64, (), ()))

    def test_afterlife_import_projects_only_historical_node_and_verifies_blob_closure(self):
        from episteme.domains.afterlife import import_snapshot, inspect

        source = Path(self.directory.name) / "legacy"
        document = source / "docs" / "stages" / "stage-1" / "REPORT.md"
        document.parent.mkdir(parents=True)
        document.write_text("Fixture negative result; historical unverified report", encoding="utf-8")
        with patch("episteme.domains.afterlife._git", return_value={"sha": None, "dirty": None}):
            snapshot = inspect(source)
        imported = import_snapshot(self.store, snapshot)
        graph = ResearchGraph.from_store(self.store)
        historical = graph.node(imported["event_id"])
        self.assertEqual(historical.kind, NodeKind.AFTERLIFE_SNAPSHOT)
        self.assertEqual(historical.payload["trust"], "historical_unverified")
        self.assertEqual(historical.scientific_validity, "not_assessed")
        self.assertEqual(graph.claims(), ())
        self.assertTrue(all(n.kind == NodeKind.ARTIFACT for n in graph.ancestors(historical.id)))
        self.assertEqual(len(graph.ancestors(historical.id)), len(snapshot.blobs) + 1)
        self.assertTrue(snapshot.blobs)
        (self.store.blobs / snapshot.blobs[0][0]).write_bytes(b"corrupt nested historical record")
        with self.assertRaisesRegex(IntegrityError, "hash mismatch"):
            ResearchGraph.from_store(self.store)

    def test_artifact_namespace_collision_and_dot_identifier_escaping(self):
        escaped = 'fixture"\\with\nnewline'
        self.append_raw("hypothesis", dict(statement="fixture", scope=self.scope), id=escaped)
        self.assertIn(json.dumps(escaped, ensure_ascii=False), ResearchGraph.from_store(self.store).to_dot())
        self.append_raw("hypothesis", dict(statement="collision", scope=self.scope),
                        id=ResearchGraph.artifact_id(self.code))
        self.protocol()
        with self.assertRaisesRegex(GraphIntegrityError, "collides with artifact namespace"):
            ResearchGraph.from_store(self.store)


if __name__ == "__main__":
    unittest.main()
