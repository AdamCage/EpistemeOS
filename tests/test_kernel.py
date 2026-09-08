"""Behavioral tests of the research kernel; no paid services or LLMs required.

These tests check record integrity and local separation of duties. They cannot
establish that callers' actor IDs are authentic or that reasoning is independent.
"""

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from episteme.kernel import Actor, GateError, Kernel
from episteme.store import ConflictError, IntegrityError, Store, canonical, digest


class KernelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="episteme-test-")
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.addCleanup(lambda: self.store.close())
        self._actors()
        self.scope = {"dataset": "toy-v1", "split": "holdout", "population": "synthetic"}
        self.implementation = self.store.put(b"primary mean implementation v1")
        self.reimplementation = self.store.put(b"separate mean implementation v1")
        self.environment = self.store.put_json({"python": "3.11", "dependencies": []})
        self.data = self.store.put_json({"values": [1, 3]})

    def _actors(self):
        self.planner = Kernel(self.store, Actor("planner-1", "planner"))
        self.executor = Kernel(self.store, Actor("executor-1", "executor"))
        self.replicator = Kernel(self.store, Actor("replicator-1", "replicator"))
        self.analyst = Kernel(self.store, Actor("analyst-1", "analyst"))
        self.reviewer = Kernel(self.store, Actor("reviewer-1", "reviewer"))

    def protocol(self, *, seeds=None, run_limit=12, scope=None, parent=None):
        scope = dict(self.scope if scope is None else scope)
        hypotheses = [self.planner.hypothesis(
            statement, prediction, falsifier, scope
        ) for statement, prediction, falsifier in [
            ("The observed mean is positive", "mean > 0", "mean <= 0"),
            ("The observed mean is nonpositive", "mean <= 0", "mean > 0"),
        ]]
        return self.planner.preregister(
            hypotheses=hypotheses, scope=scope,
            design="Compare the mean to zero on the fixed holdout",
            metric="mean", analysis_plan="Arithmetic mean over all saved values",
            stopping_rule="Execute every registered seed within the run budget",
            seeds=[7] if seeds is None else seeds, run_limit=run_limit,
            implementation=self.implementation, environment=self.environment,
            data=self.data, replication_tolerance=0.0001, parent=parent,
        )

    def start(self, protocol, *, seed=7, actor=None, **changes):
        options = dict(seed=seed, implementation=self.implementation,
                       environment=self.environment, command=["python", "experiment.py"])
        options.update(changes)
        return (self.executor if actor is None else actor).start_run(protocol, **options)

    def outputs(self, *, value=2.0, raw_data=None):
        return dict(
            raw_data=self.data if raw_data is None else raw_data,
            metrics=self.store.put_json({"mean": value}),
            log=self.store.put(b"experiment completed\n"),
        )

    def completed_primary(self, protocol, *, seed=7, value=2.0):
        run = self.start(protocol, seed=seed)
        self.executor.finish_run(run, status="completed", outputs=self.outputs(value=value))
        return run

    def completed_pair(self, protocol, *, seed=7, primary_value=2.0,
                       replica_value=2.0, replica_raw=None):
        run = self.completed_primary(protocol, seed=seed, value=primary_value)
        replica = self.start(protocol, seed=seed, actor=self.replicator,
                             implementation=self.reimplementation, replicate_of=run)
        self.replicator.finish_run(
            replica, status="completed",
            outputs=self.outputs(value=replica_value, raw_data=replica_raw))
        return [run, replica]

    def claim(self, protocol, evidence, **changes):
        options = dict(protocol=protocol, statement="The measured mean is positive",
                       scope=dict(self.scope), evidence=evidence,
                       limitations=["Synthetic data; no external population claim"],
                       outcome="supports")
        options.update(changes)
        return self.analyst.claim(**options)

    def review(self, claim, *, actor=None, verdict="approve", actions=None, basis=None):
        if basis is None:
            basis = self.reviewer.gate(claim)["basis_hash"]
        return (self.reviewer if actor is None else actor).review(
            claim, verdict=verdict, rationale="Reviewed the available evidence bundle",
            actions=[] if actions is None else actions, expected_basis=basis)

    def assert_gate_failure(self, claim, phrase):
        gate = self.analyst.gate(claim)
        self.assertFalse(gate["passed"], gate)
        self.assertIn(phrase, "; ".join(gate["failures"]))

    def test_run_requires_existing_preregistered_protocol(self):
        with self.assertRaisesRegex(GateError, "unknown protocol"):
            self.start("protocol-not-registered")
        self.assertEqual(self.store.events(), [])

    def test_primary_run_binds_frozen_protocol_artifacts_and_hash(self):
        protocol = self.protocol()
        changed = self.store.put(b"changed primary implementation")
        for option in ({"implementation": changed}, {"environment": changed}):
            with self.subTest(option=option):
                with self.assertRaisesRegex(GateError, "frozen protocol"):
                    self.start(protocol, **option)
        run = self.start(protocol)
        events = {e["id"]: e for e in self.store.events()}
        self.assertGreater(events[run]["seq"], events[protocol]["seq"])
        self.assertEqual(events[run]["payload"]["protocol_hash"], events[protocol]["hash"])

    def test_amendment_preserves_original_protocol(self):
        protocol = self.protocol()
        original = next(e for e in self.store.events() if e["id"] == protocol)
        revised = self.protocol(parent=protocol, seeds=[7, 8])
        events = {e["id"]: e for e in self.store.events()}
        self.assertNotEqual(protocol, revised)
        self.assertEqual(events[protocol], original)
        self.assertEqual(events[revised]["payload"]["parent"], protocol)
        with self.assertRaisesRegex(GateError, "seed not preregistered"):
            self.start(protocol, seed=8)

    def test_unregistered_and_boolean_seeds_are_rejected(self):
        protocol = self.protocol()
        for seed in [8, True, 7.0]:
            with self.subTest(seed=seed):
                with self.assertRaisesRegex(GateError, "seed not preregistered"):
                    self.start(protocol, seed=seed)

    def test_run_requires_existing_artifact_and_valid_argv(self):
        protocol = self.protocol()
        with self.assertRaisesRegex(IntegrityError, "missing artifact"):
            self.start(protocol, implementation="0" * 64)
        for command in [[], [""], "python experiment.py", ["python", 1]]:
            with self.subTest(command=command):
                with self.assertRaisesRegex(GateError, "argv"):
                    self.start(protocol, command=command)

    def test_completion_requires_raw_data_metrics_and_logs(self):
        protocol = self.protocol()
        run = self.start(protocol)
        for missing in ["raw_data", "metrics", "log"]:
            with self.subTest(missing=missing):
                outputs = self.outputs()
                del outputs[missing]
                with self.assertRaisesRegex(GateError, "completed run needs"):
                    self.executor.finish_run(run, status="completed", outputs=outputs)
        self.executor.finish_run(run, status="completed", outputs=self.outputs())

    def test_completion_rejects_missing_artifact(self):
        run = self.start(self.protocol())
        outputs = self.outputs()
        outputs["raw_data"] = "0" * 64
        with self.assertRaisesRegex(IntegrityError, "missing artifact"):
            self.executor.finish_run(run, status="completed", outputs=outputs)

    def test_completion_rejects_nonfinite_boolean_and_missing_primary_metric(self):
        run = self.start(self.protocol())
        invalid = [b'{"mean": NaN}', b'{"mean": Infinity}', b'{"mean": -Infinity}',
                   b'{"mean": true}', b'{"mean": "2"}', b'{"other": 2}', b'[]', b'broken']
        for data in invalid:
            with self.subTest(metrics=data):
                outputs = self.outputs()
                outputs["metrics"] = self.store.put(data)
                with self.assertRaises(GateError):
                    self.executor.finish_run(run, status="completed", outputs=outputs)
        self.executor.finish_run(run, status="completed", outputs=self.outputs(value=0.0))

    def test_run_cannot_be_finished_by_other_actor_or_role_or_twice(self):
        run = self.start(self.protocol())
        for actor in [Kernel(self.store, Actor("executor-2", "executor")),
                      Kernel(self.store, Actor("executor-1", "replicator"))]:
            with self.subTest(actor=actor.actor):
                with self.assertRaisesRegex(GateError, "assigned run actor"):
                    actor.finish_run(run, status="completed", outputs=self.outputs())
        self.executor.finish_run(run, status="completed", outputs=self.outputs())
        with self.assertRaisesRegex(GateError, "already terminal"):
            self.executor.finish_run(run, status="completed", outputs=self.outputs())

    def test_failed_and_cancelled_runs_require_reason_and_consume_budget(self):
        protocol = self.protocol(run_limit=2)
        for status in ["failed", "cancelled"]:
            run = self.start(protocol)
            with self.assertRaisesRegex(GateError, "must be explained"):
                self.executor.finish_run(run, status=status, outputs={})
            self.executor.finish_run(run, status=status, outputs={}, reason="Expected fixture failure")
        with self.assertRaisesRegex(GateError, "budget exhausted"):
            self.start(protocol)
        self.assertEqual([e["payload"]["status"] for e in self.store.events()
                          if e["kind"] == "result"], ["failed", "cancelled"])

    def test_concurrent_stale_writers_cannot_overrun_budget(self):
        protocol = self.protocol(run_limit=2)
        self.start(protocol)
        barrier = threading.Barrier(2)
        original_history = Kernel._history

        def synchronized_history(kernel):
            history = original_history(kernel)
            barrier.wait(timeout=8)
            return history

        def worker(actor_id):
            with Store(self.directory.name) as separate_store:
                kernel = Kernel(separate_store, Actor(actor_id, "executor"))
                try:
                    return self.start(protocol, actor=kernel)
                except (ConflictError, GateError) as exc:
                    return exc

        with patch.object(Kernel, "_history", synchronized_history):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(worker, actor_id) for actor_id in ["executor-A", "executor-B"]]
                results = [future.result(timeout=15) for future in futures]
        self.assertEqual(sum(isinstance(result, str) for result in results), 1, results)
        self.assertEqual(sum(isinstance(result, ConflictError) for result in results), 1, results)
        runs = [e for e in self.store.events() if e["kind"] == "run"]
        self.assertEqual(len(runs), 2)
        with self.assertRaisesRegex(GateError, "budget exhausted"):
            self.start(protocol)

    def test_actor_roles_are_enforced(self):
        with self.assertRaisesRegex(GateError, "cannot create hypothesis"):
            self.executor.hypothesis("Statement", "Prediction", "Falsifier", self.scope)
        protocol = self.protocol()
        for actor in [self.planner, self.analyst, self.reviewer, self.replicator]:
            with self.subTest(role=actor.actor.role):
                with self.assertRaisesRegex(GateError, "cannot create run"):
                    self.start(protocol, actor=actor)
        runs = self.completed_pair(protocol)
        with self.assertRaisesRegex(GateError, "cannot create claim"):
            self.reviewer.claim(protocol=protocol, statement="Claim", scope=self.scope,
                                evidence=runs, limitations=["Limited"], outcome="supports")
        claim = self.claim(protocol, runs)
        stranger = Kernel(self.store, Actor("stranger", "planner"))
        with self.assertRaisesRegex(GateError, "cannot create review"):
            self.review(claim, actor=stranger)

    def test_self_replication_rejected_despite_changed_declared_role(self):
        protocol = self.protocol()
        run = self.completed_primary(protocol)
        impersonated_role = Kernel(self.store, Actor("executor-1", "replicator"))
        with self.assertRaisesRegex(GateError, "cannot replicate itself"):
            self.start(protocol, actor=impersonated_role,
                       implementation=self.reimplementation, replicate_of=run)

    def test_reanalysis_requires_different_implementation_and_completed_original(self):
        protocol = self.protocol()
        run = self.start(protocol)
        with self.assertRaisesRegex(GateError, "completed original"):
            self.start(protocol, actor=self.replicator,
                       implementation=self.reimplementation, replicate_of=run)
        self.executor.finish_run(run, status="completed", outputs=self.outputs())
        with self.assertRaisesRegex(GateError, "different implementation"):
            self.start(protocol, actor=self.replicator, replicate_of=run)

    def test_reanalysis_cannot_replicate_a_replica_or_change_seed_or_protocol(self):
        protocol = self.protocol(seeds=[7, 8])
        primary, replica = self.completed_pair(protocol)
        other = self.protocol(seeds=[7, 8])
        for p, seed, original, expected in [
            (protocol, 7, replica, "cannot replicate a reanalysis"),
            (protocol, 8, primary, "protocol/seed mismatch"),
            (other, 7, primary, "protocol/seed mismatch"),
        ]:
            with self.subTest(protocol=p, seed=seed, original=original):
                with self.assertRaisesRegex(GateError, expected):
                    self.start(p, seed=seed, actor=self.replicator,
                               implementation=self.reimplementation, replicate_of=original)

    def test_claim_cannot_leak_scope_or_borrow_cross_protocol_results(self):
        protocol = self.protocol()
        runs = self.completed_pair(protocol)
        with self.assertRaisesRegex(GateError, "scope"):
            self.claim(protocol, runs, scope={"dataset": "all real-world populations"})
        other = self.protocol()
        with self.assertRaisesRegex(GateError, "cross-protocol"):
            self.claim(other, runs)

    def test_claim_cannot_cite_unfinished_failed_or_duplicate_runs(self):
        protocol = self.protocol()
        run = self.start(protocol)
        with self.assertRaisesRegex(GateError, "completed runs"):
            self.claim(protocol, [run])
        self.executor.finish_run(run, status="failed", outputs={}, reason="Expected")
        with self.assertRaisesRegex(GateError, "completed runs"):
            self.claim(protocol, [run])
        valid = self.completed_pair(protocol)
        with self.assertRaisesRegex(GateError, "unique run IDs"):
            self.claim(protocol, [valid[0], valid[0]])

    def test_gate_rejects_unreported_completed_runs(self):
        protocol = self.protocol()
        runs = self.completed_pair(protocol)
        claim = self.claim(protocol, [runs[0]])
        self.assert_gate_failure(claim, "all completed runs")

    def test_gate_rejects_incomplete_scheduled_seed_coverage(self):
        protocol = self.protocol(seeds=[7, 8])
        claim = self.claim(protocol, self.completed_pair(protocol))
        self.assert_gate_failure(claim, "scheduled primary seeds incomplete")

    def test_gate_rejects_missing_reanalysis(self):
        protocol = self.protocol()
        claim = self.claim(protocol, [self.completed_primary(protocol)])
        self.assert_gate_failure(claim, "independent reanalysis missing")

    def test_gate_rejects_reanalysis_metric_disagreement(self):
        protocol = self.protocol()
        claim = self.claim(protocol, self.completed_pair(protocol, replica_value=3.0))
        self.assert_gate_failure(claim, "reanalysis disagreement")

    def test_gate_rejects_reanalysis_using_different_raw_data(self):
        protocol = self.protocol()
        different_raw = self.store.put_json({"values": [2, 2]})
        claim = self.claim(protocol, self.completed_pair(protocol, replica_raw=different_raw))
        self.assert_gate_failure(claim, "original raw data artifact")

    def test_gate_rejects_unterminated_run_even_when_prior_evidence_complete(self):
        protocol = self.protocol()
        claim = self.claim(protocol, self.completed_pair(protocol))
        self.start(protocol)
        self.assert_gate_failure(claim, "unterminated run")
        self.assertEqual(self.reviewer.next_action(claim)["action"], "repair_evidence")

    def test_negative_and_inconclusive_outcomes_can_pass_mechanical_gate(self):
        protocol = self.protocol()
        runs = self.completed_pair(protocol, primary_value=-2.0, replica_value=-2.0)
        for outcome in ["refutes", "inconclusive"]:
            with self.subTest(outcome=outcome):
                claim = self.claim(protocol, runs, outcome=outcome,
                                   statement="The registered positive prediction was not supported")
                gate = self.analyst.gate(claim)
                self.assertTrue(gate["passed"], gate)
                self.assertEqual(gate["scientific_validity"], "not_assessed")
                self.assertEqual(self.reviewer.next_action(claim)["action"], "scientific_review")

    def test_failed_run_is_retained_without_blocking_successful_retry(self):
        protocol = self.protocol()
        failed = self.start(protocol)
        self.executor.finish_run(failed, status="failed", outputs={}, reason="Initial failed attempt")
        claim = self.claim(protocol, self.completed_pair(protocol))
        self.assertTrue(self.analyst.gate(claim)["passed"])
        self.assertTrue(any(e["kind"] == "result" and e["payload"]["run"] == failed
                            for e in self.store.events()))

    def test_self_review_rejects_all_contributors_despite_reviewer_role(self):
        protocol = self.protocol()
        claim = self.claim(protocol, self.completed_pair(protocol))
        for actor_id in ["planner-1", "executor-1", "replicator-1", "analyst-1"]:
            with self.subTest(actor_id=actor_id):
                actor = Kernel(self.store, Actor(actor_id, "reviewer"))
                with self.assertRaisesRegex(GateError, "independent of contributors"):
                    self.review(claim, actor=actor)

    def test_mechanically_invalid_claim_cannot_receive_scientific_review(self):
        protocol = self.protocol()
        claim = self.claim(protocol, [self.completed_primary(protocol)])
        with self.assertRaisesRegex(GateError, "mechanical gate failed"):
            self.review(claim)

    def test_hypothesis_author_cannot_review_when_another_actor_registered_protocol(self):
        original = self.protocol()
        payload = Kernel._get(self.store.events(), original, "protocol")["payload"]
        other_planner = Kernel(self.store, Actor("different-planner", "planner"))
        protocol = other_planner.preregister(**payload)
        claim = self.claim(protocol, self.completed_pair(protocol))
        idea_author = Kernel(self.store, Actor("planner-1", "reviewer"))
        with self.assertRaisesRegex(GateError, "independent of contributors"):
            self.review(claim, actor=idea_author)

    def test_review_basis_becomes_stale_when_new_run_changes_bundle(self):
        protocol = self.protocol()
        claim = self.claim(protocol, self.completed_pair(protocol))
        prior_basis = self.reviewer.gate(claim)["basis_hash"]
        self.review(claim, basis=prior_basis)
        self.assertEqual(self.reviewer.next_action(claim)["action"], "paper_candidate")
        run = self.start(protocol)
        self.executor.finish_run(run, status="cancelled", outputs={}, reason="New cancelled attempt")
        with self.assertRaisesRegex(GateError, "stale review"):
            self.review(claim, basis=prior_basis)
        self.assertEqual(self.reviewer.next_action(claim)["action"], "scientific_review")

    def test_unrelated_hypothesis_does_not_invalidate_claim_review(self):
        protocol = self.protocol()
        claim = self.claim(protocol, self.completed_pair(protocol))
        self.review(claim)
        basis = self.reviewer.gate(claim)["basis_hash"]
        self.planner.hypothesis("Unrelated idea", "Different prediction", "Its falsifier", self.scope)
        self.assertEqual(self.reviewer.gate(claim)["basis_hash"], basis)
        self.assertEqual(self.reviewer.next_action(claim)["action"], "paper_candidate")

    def test_negative_review_veto_persists_despite_another_reviewer_approval(self):
        protocol = self.protocol()
        claim = self.claim(protocol, self.completed_pair(protocol))
        self.review(claim, verdict="request_changes", actions=["Add a discriminating control"])
        reviewer_two = Kernel(self.store, Actor("reviewer-2", "reviewer"))
        self.review(claim, actor=reviewer_two)
        next_action = self.reviewer.next_action(claim)
        self.assertEqual(next_action["action"], "replan")
        self.assertIn("Add a discriminating control", next_action["reasons"])
        self.review(claim)
        self.assertEqual(self.reviewer.next_action(claim)["action"], "paper_candidate")

    def test_nonapproval_requires_actions_and_approval_has_no_unresolved_actions(self):
        protocol = self.protocol()
        claim = self.claim(protocol, self.completed_pair(protocol))
        for verdict in ["reject", "request_changes"]:
            with self.subTest(verdict=verdict):
                with self.assertRaisesRegex(GateError, "replan actions"):
                    self.review(claim, verdict=verdict)
        with self.assertRaisesRegex(GateError, "unresolved actions"):
            self.review(claim, actions=["Unresolved issue"])

    def test_next_action_uses_one_history_snapshot_and_observes_later_changes(self):
        protocol = self.protocol()
        claim = self.claim(protocol, self.completed_pair(protocol))
        self.review(claim)
        snapshot = self.store.events()
        # The gate and review lookup must share one verified event snapshot.
        # next_action is an observation, not a publication lock that prevents
        # another actor from appending after the observation was taken.
        with patch.object(self.reviewer, "_history", return_value=snapshot) as history, \
                patch.object(self.reviewer, "_gate", wraps=self.reviewer._gate) as gate:
            decision = self.reviewer.next_action(claim)
        history.assert_called_once_with()
        gate.assert_called_once_with(snapshot, claim)
        self.assertIs(gate.call_args.args[0], snapshot)
        self.assertEqual(decision["action"], "paper_candidate", decision)

        self.start(protocol)
        subsequent = self.reviewer.next_action(claim)
        self.assertEqual(subsequent["action"], "repair_evidence", subsequent)
        self.assertIn("unterminated run", "; ".join(subsequent["reasons"]))

    def test_reopen_preserves_event_chain_artifacts_gate_and_review(self):
        protocol = self.protocol()
        claim = self.claim(protocol, self.completed_pair(protocol))
        self.review(claim)
        before_events = self.store.events()
        before_gate = self.reviewer.gate(claim)
        self.store.close()
        self.store = Store(self.directory.name)
        self._actors()
        self.assertEqual(self.store.events(), before_events)
        self.assertEqual(self.reviewer.gate(claim), before_gate)
        self.assertEqual(self.store.read(self.data), canonical({"values": [1, 3]}))
        self.assertEqual(self.reviewer.next_action(claim)["action"], "paper_candidate")

    def test_gate_fails_after_artifact_corruption_or_loss(self):
        protocol = self.protocol()
        claim = self.claim(protocol, self.completed_pair(protocol))
        path = self.store.blobs / self.data
        original_bytes = path.read_bytes()
        path.write_bytes(b"corrupted")
        self.assert_gate_failure(claim, "hash mismatch")
        path.write_bytes(original_bytes)
        path.unlink()
        self.assert_gate_failure(claim, "missing artifact")

    def test_gate_verifies_failed_and_cancelled_attempt_artifacts(self):
        protocol = self.protocol()
        failed_logs = []
        for status in ("failed", "cancelled"):
            run = self.start(protocol)
            log = self.store.put(f"{status} attempt log".encode())
            failed_logs.append(log)
            self.executor.finish_run(run, status=status, outputs={"log": log},
                                     reason=f"Recorded {status} attempt before retry")
        claim = self.claim(protocol, self.completed_pair(protocol))
        self.assertTrue(self.reviewer.gate(claim)["passed"])
        for log in failed_logs:
            with self.subTest(log=log):
                path = self.store.blobs / log
                original = path.read_bytes()
                path.write_bytes(b"corrupt failed-attempt provenance")
                self.assert_gate_failure(claim, "hash mismatch")
                self.assertEqual(self.reviewer.next_action(claim)["action"], "repair_evidence")
                path.unlink()
                self.assert_gate_failure(claim, "missing artifact")
                path.write_bytes(original)
        self.assertTrue(self.reviewer.gate(claim)["passed"])

    def test_store_enforces_append_only_updates_and_deletes(self):
        self.protocol()
        before = self.store.events()
        for statement in ["UPDATE events SET actor = 'changed' WHERE seq = 1",
                          "DELETE FROM events WHERE seq = 1"]:
            with self.subTest(sql=statement):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    self.store.db.execute(statement)
                self.store.db.rollback()
        self.assertEqual(self.store.events(), before)

    def test_hash_chain_detects_payload_tampering_after_trigger_bypass(self):
        self.protocol()
        self.store.db.execute("DROP TRIGGER events_no_update")
        self.store.db.execute("UPDATE events SET payload = ? WHERE seq = 1", ('{"forged": true}',))
        self.store.db.commit()
        with self.assertRaisesRegex(IntegrityError, "event chain corrupt"):
            self.store.events()

    def test_store_rejects_path_escape_and_corrupted_existing_content(self):
        for key in ["../state.sqlite3", "A" * 64, "0" * 63, None]:
            with self.subTest(key=key):
                with self.assertRaisesRegex(IntegrityError, "invalid artifact digest"):
                    self.store.read(key)
        data = b"content addressed fixture"
        key = self.store.put(data)
        self.assertEqual(key, digest(data))
        self.assertEqual(self.store.put(data), key)
        (self.store.blobs / key).write_bytes(b"wrong")
        with self.assertRaisesRegex(IntegrityError, "hash mismatch"):
            self.store.put(data)

    def test_store_nonfinite_json_rejected_and_export_round_trips(self):
        for number in [float("nan"), float("inf"), float("-inf")]:
            with self.subTest(number=number):
                with self.assertRaises(ValueError):
                    self.store.put_json({"metric": number})
        self.protocol()
        self.assertEqual([json.loads(line) for line in self.store.export().splitlines()],
                         self.store.events())


if __name__ == "__main__":
    unittest.main()
