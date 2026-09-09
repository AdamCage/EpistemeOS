"""Typed scientific declarations and exposure timing; all results are fixtures."""

from copy import deepcopy
import tempfile
import unittest
from unittest.mock import patch

from episteme.graph import GraphIntegrityError, NodeKind, Relation, ResearchGraph
from episteme.kernel import Actor, GateError, Kernel
from episteme.reporting import artifact_inventory
from episteme.store import ConflictError, IntegrityError, Store


class ScientificWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="episteme-scientific-workflow-")
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.addCleanup(self.store.close)
        self.planner = Kernel(self.store, Actor("fixture-planner", "planner"))
        self.executor = Kernel(self.store, Actor("fixture-executor", "executor"))
        self.replicator = Kernel(self.store, Actor("fixture-replicator", "replicator"))
        self.reviewer = Kernel(self.store, Actor("fixture-reviewer", "reviewer"))
        self.scope = {"dataset": "synthetic", "population": "two fixture units"}
        self.hypotheses = [self.planner.hypothesis("Mean difference is zero", "Zero", "Nonzero", self.scope),
                           self.planner.hypothesis("Mean difference is nonzero", "Nonzero", "Zero", self.scope)]
        self.code = self.store.put(b"fixture primary implementation")
        self.replica_code = self.store.put(b"fixture independent implementation declaration")
        self.environment = self.store.put(b"fixture environment")
        self.data = self.store.put(b"unit,difference\nu1,-1\nu2,1\n")
        self.holdout = self.store.put(b"unit,difference\nh1,-2\nh2,2\n")

    def design(self, mode="exploratory", split=None, *, sequential=False):
        return dict(
            schema_version=1, mode=mode, experimental_unit="unit", estimand="Mean difference over units",
            primary_metric={"name": "mean_difference", "unit": "score"}, secondary_metrics=[],
            sample_size=2, sample_size_rationale="Two controlled fixture units; no population inference",
            uncertainty={"method": "not_applicable", "resampling_unit": None,
                         "rationale": "Deterministic fixture equality only"}, exclusions=[],
            stopping_rule={"kind": "sequential", "rule": "Registered fixture stopping",
                           "error_control": "Prespecified fixture error-spending declaration"} if sequential else
                          {"kind": "fixed_sample", "rule": "Registered fixture stopping"},
            multiple_testing={"family": ["mean_difference"], "correction": "not_applicable",
                              "rationale": "No significance testing on this fixture"},
            data_splits=[{"id": "evaluation", "digest": split or self.holdout,
                          "role": "confirmatory" if mode == "confirmatory" else "discovery",
                          "exposure_policy": "sequential" if sequential else
                                             "holdout" if mode == "confirmatory" else "open"}])

    def protocol(self, design=None, **changes):
        options = dict(hypotheses=self.hypotheses, scope=self.scope, design="Synthetic difference experiment",
                       metric="mean_difference", analysis_plan="Arithmetic mean of the two unit differences",
                       stopping_rule="Registered fixture stopping", seeds=[7], run_limit=10,
                       implementation=self.code, environment=self.environment, data=self.data,
                       replication_tolerance=0.0, statistical_design=design or self.design())
        options.update(changes)
        return self.planner.preregister(**options)

    def execute_run(self, protocol, *, replica_of=None):
        actor = self.replicator if replica_of else self.executor
        run = actor.start_run(protocol, seed=7, implementation=self.replica_code if replica_of else self.code,
                              environment=self.environment, command=["fixture-declaration"], replicate_of=replica_of)
        actor.finish_run(run, status="completed", outputs={"raw_data": self.data,
                         "metrics": self.store.put_json({"mean_difference": 0}),
                         "log": self.store.put(b"Fixture result: -1 + 1 = 0")})
        return run

    def claim_fixture(self, protocol, *, outcome="inconclusive", **changes):
        original = self.execute_run(protocol)
        replica = self.execute_run(protocol, replica_of=original)
        return self.executor.claim(protocol=protocol, statement="Fixture mean is zero; population behavior unknown",
                                   scope=self.scope, evidence=[original, replica],
                                   limitations=["Synthetic fixture, declared actor separation only"], outcome=outcome, **changes)

    def event(self, id):
        return next(event for event in self.store.events() if event["id"] == id)

    def test_typed_null_result_preserves_mode_and_remains_scientifically_unassessed(self):
        protocol = self.protocol()
        claim = self.claim_fixture(protocol)
        gate = self.planner.gate(claim)
        self.assertTrue(gate["passed"], gate["failures"])
        self.assertEqual(gate["scientific_validity"], "not_assessed")
        self.assertEqual(self.event(claim)["payload"]["inference_mode"], "exploratory")
        self.assertEqual(self.event(claim)["payload"]["outcome"], "inconclusive")
        self.assertEqual(self.planner.next_action(claim)["action"], "scientific_review")
        graph = ResearchGraph.from_store(self.store)
        self.assertEqual(graph.node(claim).scientific_validity, "not_assessed")
        with Store(self.directory.name) as reopened:
            self.assertEqual(Kernel(reopened, Actor("reader", "planner")).gate(claim), gate)

    def test_primary_metric_stopping_rule_and_missing_split_blobs_are_rejected_before_write(self):
        for changes, expected in [({"metric": "selected_later"}, "primary metric"),
                                  ({"stopping_rule": "Stop once favorable"}, "stopping rule")]:
            with self.subTest(changes=changes), self.assertRaisesRegex(GateError, expected):
                self.protocol(**changes)
        with self.assertRaises(IntegrityError):
            self.protocol(design=self.design(split="0" * 64))
        with self.assertRaises(IntegrityError):
            self.protocol(seen_data=["0" * 64])
        self.assertFalse(any(event["kind"] == "protocol" for event in self.store.events()))

    def test_previously_exposed_holdout_and_sequential_data_cannot_be_confirmatory(self):
        exposure = self.planner.expose_data(data=self.holdout, purpose="Inspected discovery outcomes")
        self.assertIsNone(self.event(exposure)["payload"]["protocol"])
        for sequential in (False, True):
            with self.subTest(sequential=sequential), self.assertRaisesRegex(GateError, "already exposed"):
                self.protocol(design=self.design("confirmatory", sequential=sequential))
        fresh = self.store.put(b"new,registered,data")
        with self.assertRaisesRegex(GateError, "already exposed"):
            self.protocol(design=self.design("confirmatory", split=fresh), seen_data=[fresh])

    def test_completed_raw_data_counts_as_seen_even_without_explicit_exposure(self):
        original = self.protocol()
        self.execute_run(original)
        with self.assertRaisesRegex(GateError, "already exposed"):
            self.protocol(design=self.design("confirmatory", split=self.data))
        next_protocol = self.protocol()
        self.assertIn(self.data, self.event(next_protocol)["payload"]["seen_data"])

    def test_omitting_parent_cannot_relabel_attempted_discovery_split_as_unseen(self):
        original = self.protocol()
        run = self.executor.start_run(original, seed=7, implementation=self.code, environment=self.environment,
                                      command=["fixture-attempt"])
        self.executor.finish_run(run, status="failed", outputs={}, reason="Fixture crashed after reading inputs")
        with self.assertRaisesRegex(GateError, "already exposed"):
            self.protocol(design=self.design("confirmatory"))
        later = self.protocol()
        self.assertTrue({self.data, self.holdout} <= set(self.event(later)["payload"]["seen_data"]))

    def test_seen_data_declaration_in_earlier_plan_remains_known_without_parent(self):
        self.protocol(seen_data=[self.holdout])
        with self.assertRaisesRegex(GateError, "already exposed"):
            self.protocol(design=self.design("confirmatory"))

    def test_typed_amendment_freezes_exposure_snapshot_and_cannot_reset_to_legacy(self):
        original = self.protocol(design=self.design("confirmatory"))
        old = deepcopy(self.event(original))
        with self.assertRaisesRegex(GateError, "requires a statistical design"):
            self.protocol(parent=original, statistical_design=None)
        with self.assertRaisesRegex(GateError, "requires a reason"):
            self.protocol(parent=original)
        with self.assertRaisesRegex(GateError, "already exposed"):
            self.protocol(design=self.design("confirmatory"), parent=original, amendment_reason="Change analysis")
        fresh = self.store.put(b"new holdout after protocol amendment")
        amended = self.protocol(design=self.design("confirmatory", split=fresh), parent=original,
                                amendment_reason="Use new held-out units for revised estimand")
        self.assertEqual(self.event(original), old)
        p = self.event(amended)["payload"]
        self.assertEqual(p["parent"], original)
        self.assertEqual(p["seen_data"], sorted([self.data, self.holdout]))
        self.assertEqual(p["protocol_mode"], "confirmatory")
        self.assertNotIn(fresh, p["seen_data"])

    def test_confirmatory_claim_cannot_upgrade_exploratory_descriptive_or_legacy_protocol(self):
        for mode in ("exploratory", "descriptive", "legacy"):
            with self.subTest(mode=mode):
                protocol = self.protocol(design=self.design(mode if mode != "legacy" else "descriptive"),
                                         **({"statistical_design": None} if mode == "legacy" else {}))
                with self.assertRaisesRegex(GateError, "confirmatory inference"):
                    self.claim_fixture(protocol, inference_mode="confirmatory")
                p = self.event(protocol)["payload"]
                if mode == "legacy":
                    self.assertNotIn("statistical_design", p)
                    self.assertNotIn("protocol_mode", p)
                    self.assertNotIn("seen_data", p)

    def test_postprotocol_planned_exposure_changes_basis_without_failing_design(self):
        protocol = self.protocol(design=self.design("confirmatory"))
        self.executor.expose_data(data=self.holdout, purpose="Execute frozen holdout analysis", protocol=protocol)
        claim = self.claim_fixture(protocol)
        first_gate = self.planner.gate(claim)
        self.assertTrue(first_gate["passed"], first_gate["failures"])
        self.assertEqual(self.event(claim)["payload"]["inference_mode"], "confirmatory")
        review = self.reviewer.review(claim, verdict="request_changes", rationale="Fixture requires domain review",
                                     actions=["Obtain independent scientific assessment"], expected_basis=first_gate["basis_hash"])
        self.assertEqual(self.planner.next_action(claim)["action"], "replan")
        new_exposure = self.reviewer.expose_data(data=self.holdout, purpose="Read additional holdout details", protocol=protocol)
        updated = self.planner.gate(claim)
        self.assertTrue(updated["passed"], updated["failures"])
        self.assertNotEqual(updated["basis_hash"], first_gate["basis_hash"])
        self.assertEqual(self.planner.next_action(claim)["action"], "scientific_review")
        with self.assertRaisesRegex(GateError, "stale review"):
            self.reviewer.review(claim, verdict="request_changes", rationale="Old basis",
                                 actions=["Review"], expected_basis=first_gate["basis_hash"])
        latest = self.reviewer.review(claim, verdict="request_changes", rationale="New fixture basis assessed",
                                     actions=["Obtain scientific assessment"], expected_basis=updated["basis_hash"])
        graph = ResearchGraph.from_store(self.store)
        self.assertEqual(graph.node(new_exposure).kind, NodeKind.DATA_EXPOSURE)
        self.assertEqual(graph.node(review).payload["basis_hash"], first_gate["basis_hash"])
        self.assertTrue(any(edge.source == new_exposure and edge.target == latest
                            and edge.relation == Relation.REVIEW_EXPOSURE for edge in graph.edges))

    def test_unrelated_later_exposure_does_not_invalidate_typed_basis(self):
        claim = self.claim_fixture(self.protocol())
        first = self.planner.gate(claim)
        self.planner.expose_data(data=self.store.put(b"unrelated data"), purpose="Different task")
        self.assertEqual(self.planner.gate(claim)["basis_hash"], first["basis_hash"])

    def test_foreign_attempt_on_same_bytes_invalidates_review_and_retains_failed_context(self):
        protocol = self.protocol()
        claim = self.claim_fixture(protocol)
        initial = self.planner.gate(claim)
        self.reviewer.review(claim, verdict="request_changes", rationale="Fixture context assessment",
            actions=["Check alternate analyses"], expected_basis=initial["basis_hash"])
        foreign = self.protocol()
        # Merely inheriting already known seen_data does not create a new exposure.
        self.assertEqual(self.planner.gate(claim)["basis_hash"], initial["basis_hash"])
        attempt = self.executor.start_run(foreign, seed=7, implementation=self.code,
            environment=self.environment, command=["fixture-foreign-attempt"])
        after_start = self.planner.gate(claim)
        self.assertTrue(after_start["passed"], after_start["failures"])
        self.assertNotEqual(after_start["basis_hash"], initial["basis_hash"])
        self.assertEqual(self.planner.next_action(claim)["action"], "scientific_review")
        log = self.store.put(b"Foreign fixture failed after viewing data")
        terminal = self.executor.finish_run(attempt, status="failed", outputs={"log": log}, reason="Injected failure")
        after_finish = self.planner.gate(claim)
        self.assertNotEqual(after_finish["basis_hash"], after_start["basis_hash"])
        latest = self.reviewer.review(claim, verdict="request_changes", rationale="Inspected retained failure",
            actions=["Investigate alternative explanation"], expected_basis=after_finish["basis_hash"])
        graph = ResearchGraph.from_store(self.store)
        self.assertTrue(any(edge.source == terminal and edge.target == latest
                            and edge.relation == Relation.REVIEW_EXPOSURE for edge in graph.edges))
        (self.store.blobs / log).unlink()
        self.assertFalse(self.planner.gate(claim)["passed"])

    def test_unrelated_plan_with_inherited_snapshot_and_new_inputs_does_not_invalidate_review(self):
        claim = self.claim_fixture(self.protocol())
        initial = self.planner.gate(claim)["basis_hash"]
        other = self.store.put(b"unrelated dataset")
        protocol = self.protocol(design=self.design(split=other), data=other)
        self.executor.start_run(protocol, seed=7, implementation=self.code,
                                environment=self.environment, command=["unrelated-fixture"])
        self.assertEqual(self.planner.gate(claim)["basis_hash"], initial)

    def test_reviewer_cannot_review_own_foreign_attempt_in_exposure_basis(self):
        claim = self.claim_fixture(self.protocol())
        foreign = self.protocol()
        contributor = Kernel(self.store, Actor(self.reviewer.actor.id, "executor"))
        contributor.start_run(foreign, seed=7, implementation=self.code,
                              environment=self.environment, command=["foreign-fixture"])
        gate = self.planner.gate(claim)
        self.assertTrue(gate["passed"], gate["failures"])
        with self.assertRaisesRegex(GateError, "independent of contributors"):
            self.reviewer.review(claim, verdict="request_changes", rationale="Self review fixture",
                                 actions=["Fix"], expected_basis=gate["basis_hash"])

    def test_bundle_inventory_includes_split_seen_and_exposure_closure(self):
        seen = self.store.put(b"prior discovery data")
        self.planner.expose_data(data=seen, purpose="Discovery")
        protocol = self.protocol()
        extra = self.store.put(b"later context")
        self.reviewer.expose_data(data=extra, purpose="Additional context", protocol=protocol)
        inventory = artifact_inventory(self.store, self.store.events())
        self.assertTrue({self.data, self.holdout, seen, extra} <= {row["sha256"] for row in inventory})

    def test_later_related_exposure_blob_is_required_even_when_outside_original_splits(self):
        protocol = self.protocol()
        claim = self.claim_fixture(protocol)
        extra = self.store.put(b"additional inspected evidence")
        self.reviewer.expose_data(data=extra, purpose="Read additional analysis", protocol=protocol)
        self.assertTrue(self.planner.gate(claim)["passed"])
        (self.store.blobs / extra).unlink()
        gate = self.planner.gate(claim)
        self.assertFalse(gate["passed"])
        self.assertTrue(any("missing artifact" in failure for failure in gate["failures"]))
        with self.assertRaisesRegex(GateError, "mechanical gate failed"):
            self.reviewer.review(claim, verdict="request_changes", rationale="Check missing data",
                                 actions=["Restore evidence"], expected_basis=gate["basis_hash"])

    def test_graph_rejects_forged_typed_amendment_without_recorded_reason(self):
        protocol = self.protocol()
        payload = deepcopy(self.event(protocol)["payload"])
        payload.update(parent=protocol, seen_data=sorted([self.data, self.holdout]), amendment_reason=None)
        self.store.append(id="forged-amendment", kind="protocol", actor="fixture-planner", role="planner",
                          payload=payload, expected_revision=len(self.store.events()))
        with self.assertRaisesRegex(GraphIntegrityError, "requires a reason"):
            ResearchGraph.from_store(self.store)

    def test_split_or_seen_blob_corruption_blocks_gate_start_and_graph_closure(self):
        seen = self.store.put(b"discovery data viewed before the protocol")
        self.planner.expose_data(data=seen, purpose="Discovery")
        protocol = self.protocol()
        claim = self.claim_fixture(protocol)
        for key in (seen, self.holdout):
            with self.subTest(key=key):
                path = self.store.blobs / key
                original = path.read_bytes()
                path.write_bytes(b"corrupted")
                try:
                    self.assertFalse(self.planner.gate(claim)["passed"])
                    with self.assertRaises(IntegrityError):
                        self.execute_run(protocol)
                    with self.assertRaises(IntegrityError):
                        ResearchGraph.from_store(self.store)
                finally:
                    path.write_bytes(original)

    def test_exposure_requires_valid_artifact_protocol_purpose_and_role(self):
        for kwargs in ({"data": "0" * 64, "purpose": "Read"},
                       {"data": self.data, "purpose": ""},
                       {"data": self.data, "purpose": "Read", "protocol": "missing"}):
            with self.subTest(kwargs=kwargs), self.assertRaises((GateError, IntegrityError)):
                self.planner.expose_data(**kwargs)
        with self.assertRaisesRegex(GateError, "cannot create"):
            Kernel(self.store, Actor("writer", "writer")).expose_data(data=self.data, purpose="Read")
        self.assertFalse(any(event["kind"] == "data_exposure" for event in self.store.events()))

    def test_concurrent_exposure_cannot_cross_preregistration_commit_boundary(self):
        append = self.store.append
        injected = False
        def expose_before_write(**kwargs):
            nonlocal injected
            if kwargs["kind"] == "protocol" and not injected:
                injected = True
                append(id="concurrent-exposure", kind="data_exposure", actor="observer", role="analyst",
                       payload={"data": self.holdout, "purpose": "Concurrent observation", "protocol": None},
                       expected_revision=len(self.store.events()))
            return append(**kwargs)
        with patch.object(self.store, "append", side_effect=expose_before_write):
            with self.assertRaises(ConflictError):
                self.protocol(design=self.design("confirmatory"))
        self.assertFalse(any(event["kind"] == "protocol" for event in self.store.events()))
        with self.assertRaisesRegex(GateError, "already exposed"):
            self.protocol(design=self.design("confirmatory"))


if __name__ == "__main__":
    unittest.main()
