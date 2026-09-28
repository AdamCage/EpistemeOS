"""Local reviewer opinions are fixtures; they do not prove scientific independence."""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from episteme.commands import CommandService
from episteme.execution import _identity, _index as execution_index
from episteme.followup import followup_state
from episteme.graph import ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.recovery import backup, restore
from episteme.replanning import open_obligations
from episteme.reporting import PaperBuilder, review_bundle
from episteme.resolution import _admit, _index, _payload, resolution_states
from episteme.store import Store

from tests import test_followup, test_followup_execution


class ResolutionTests(unittest.TestCase):
    # Reuse the existing small scientific fixture without inheriting its tests.
    components = staticmethod(test_followup.FollowupTests.components)
    outputs = test_followup.FollowupTests.outputs
    envelope = test_followup.FollowupTests.envelope
    command = test_followup.FollowupTests.command
    spec = test_followup.FollowupTests.spec
    options = test_followup.FollowupTests.options
    apply = test_followup.FollowupTests.apply

    def setUp(self):
        test_followup.FollowupTests.setUp(self)
        self.sibling_obligation = next(event["id"] for event in self.store.events()
            if event["kind"] == "review_obligation" and event["id"] != self.obligation)

    def complete_child(self, *, outcome="inconclusive"):
        self.apply()
        plan = followup_state(self.store, self.obligation)
        selection = self.search.select_next(self.tree)
        self.assertEqual(selection["node"], plan["experiment_node"])
        primary = Kernel(self.store, self.executor).start_run(plan["protocol"], seed=8,
            implementation=self.new_implementation, environment=self.environment,
            command=["python", "control.py"])
        primary_result = Kernel(self.store, self.executor).finish_run(
            primary, status="completed", outputs=self.outputs())
        replica = Kernel(self.store, self.replicator).start_run(plan["protocol"], seed=8,
            implementation=self.reimplementation, environment=self.environment,
            command=["python", "control-reanalysis.py"], replicate_of=primary)
        replica_result = Kernel(self.store, self.replicator).finish_run(
            replica, status="completed", outputs=self.outputs())
        claim = Kernel(self.store, self.analyst).claim(protocol=plan["protocol"],
            statement="The synthetic control remains bounded and inconclusive",
            scope=self.scope, evidence=[primary, replica],
            limitations=["Fixture only; no new-data replication"], outcome=outcome)
        terminal = self.search.finish_selection(selection["id"], status="completed",
            actual_cost=2, reason="Technical completion; no automatic scientific verdict",
            run=primary, claim=claim)
        basis = Kernel(self.store, self.reviewer).gate(claim)["basis_hash"]
        return dict(plan=plan, claim=claim, basis=basis, terminal=terminal,
                    runs=[primary, replica], results=[primary_result, replica_result])

    def resolution_payload(self, child):
        return dict(obligation=self.obligation, claim=child["claim"],
            expected_basis=child["basis"],
            review_rationale="The bounded follow-up claim is acceptable as a fixture opinion",
            resolution_rationale="The registered control addresses the original confound in this fixture",
            evidence_refs=[child["claim"], *child["results"]], link_assessments=None)

    def resolve(self, child, actor=None):
        return CommandService(self.store).execute(self.envelope(
            "replanning.resolve_obligation", self.resolution_payload(child),
            self.reviewer if actor is None else actor))

    def test_atomic_resolution_replay_and_bounded_child_eligibility(self):
        child = self.complete_child()
        payload = self.resolution_payload(child)
        envelope = self.envelope("replanning.resolve_obligation", payload, self.reviewer)
        resolution = CommandService(self.store).execute(envelope)
        self.assertEqual(CommandService(self.store).execute(envelope), resolution)
        history = self.store.events()
        receipt = self.store.receipts()[-1]
        self.assertEqual([history[id_index - 1]["kind"] for id_index in
                          range(receipt["before_revision"] + 1, receipt["after_revision"] + 1)],
                         ["review", "review_obligation_resolution"])
        self.assertEqual(history[-1]["id"], resolution)
        self.assertEqual(history[-1]["payload"]["disposition"], "reviewer_satisfied")
        self.assertEqual([ref["id"] for ref in history[-1]["payload"]["evidence_refs"]],
                         payload["evidence_refs"])
        self.assertEqual([event["id"] for event in open_obligations(self.store, history, self.claim)],
                         [self.sibling_obligation])
        self.assertEqual(followup_state(self.store, self.obligation)["obligation_resolution"],
                         "reviewer_satisfied")
        self.assertEqual(Kernel(self.store, self.reviewer).next_action(self.claim)["action"], "replan")
        self.assertEqual(Kernel(self.store, self.reviewer).next_action(child["claim"])["action"],
                         "replan")
        graph = ResearchGraph.from_store(self.store)
        self.assertEqual(graph.node(resolution).kind.value, "review_obligation_resolution")
        bundle = review_bundle(self.store, history)
        self.assertEqual(bundle["obligation_resolution_status"][self.obligation], "reviewer_satisfied")
        self.assertEqual(bundle["review_obligation_resolutions"][0]["id"], resolution)
        self.assertIn(self.obligation, _index(self.store, history))
        self.assertEqual(_index(self.store, history[:receipt["before_revision"]]), {})

    def test_late_evidence_stales_resolution_without_erasing_history(self):
        child = self.complete_child()
        resolution = self.resolve(child)
        new_run = Kernel(self.store, self.executor).start_run(self.protocol, seed=7,
            implementation=self.implementation, environment=self.environment,
            command=["python", "late-source.py"])
        history = self.store.events()
        self.assertEqual(resolution_states(self.store, history)[self.obligation]["status"],
                         "stale_resolution")
        self.assertEqual(followup_state(self.store, self.obligation)["obligation_resolution"],
                         "stale_resolution")
        self.assertEqual([event["id"] for event in open_obligations(self.store, history, self.claim)],
                         [self.obligation, self.sibling_obligation])
        self.assertNotEqual(Kernel(self.store, self.reviewer).next_action(child["claim"])["action"],
                            "paper_candidate")
        self.assertIn(resolution, {event["id"] for event in history})
        self.assertIn(new_run, {event["id"] for event in history})

    def test_wrong_reviewer_and_missing_result_citations_roll_back(self):
        child = self.complete_child()
        prior = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "original negative reviewer"):
            self.resolve(child, Actor("other-reviewer", "reviewer"))
        payload = self.resolution_payload(child)
        payload["evidence_refs"].pop()
        with self.assertRaisesRegex(ValueError, "citations"):
            CommandService(self.store).execute(self.envelope(
                "replanning.resolve_obligation", payload, self.reviewer))
        self.assertEqual((self.store.export(), self.store.export_receipts()), prior)

    def test_changed_source_basis_rejects_the_old_finding_at_commit(self):
        child = self.complete_child()
        Kernel(self.store, self.executor).start_run(self.protocol, seed=7,
            implementation=self.implementation, environment=self.environment,
            command=["python", "late-source.py"])
        prior = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "source evidence differs"):
            self.resolve(child)
        self.assertEqual((self.store.export(), self.store.export_receipts()), prior)
        self.assertFalse(any(event["kind"] == "review_obligation_resolution"
                             for event in self.store.events()))

    def test_forged_resolution_event_fails_replay_and_graph(self):
        child = self.complete_child()
        resolution = self.resolve(child)
        event = self.store.events()[-1]
        self.store.append(id="forged-resolution", kind="review_obligation_resolution",
            actor=self.reviewer.id, role="reviewer", payload=dict(event["payload"]),
            expected_revision=len(self.store.events()))
        with self.assertRaisesRegex(ValueError, "original command receipt"):
            resolution_states(self.store, self.store.events())
        with self.assertRaisesRegex(ValueError, "resolution history"):
            ResearchGraph.from_store(self.store)
        self.assertNotEqual(resolution, "forged-resolution")

    def test_complete_receipt_with_forged_citation_fails_historical_replay(self):
        child = self.complete_child()
        args = self.resolution_payload(child)
        envelope = self.envelope("replanning.resolve_obligation", args, self.reviewer)

        def forge():
            refs = _admit(self.store, self.store.events(), **args,
                          actor=self.reviewer.id, study_id=self.study)
            kernel = Kernel(self.store, self.reviewer)
            review_id = kernel.review(child["claim"], verdict="approve",
                rationale=args["review_rationale"], actions=[], expected_basis=child["basis"])
            review = Kernel._get(self.store.events(), review_id, "review")
            event_payload = _payload(refs, review, args["resolution_rationale"])
            event_payload["evidence_refs"][0]["hash"] = "0" * 64
            return kernel._write(self.store.events(), "review_obligation_resolution",
                                 event_payload, {"reviewer"})

        self.store.command(envelope["context"], envelope["request"], forge)
        with self.assertRaisesRegex(ValueError, "citations|frozen evidence"):
            resolution_states(self.store, self.store.events())

    def test_backup_restores_resolution_and_current_status(self):
        child = self.complete_child()
        resolution = self.resolve(child)
        history, receipts = self.store.export(), self.store.export_receipts()
        graph = ResearchGraph.from_store(self.store).snapshot_hash
        snapshot = self.root.parent / "snapshot"
        restored = self.root.parent / "restored"
        backup(self.store, snapshot)
        restore(snapshot, restored)
        with Store(restored) as reopened:
            self.assertEqual((reopened.export(), reopened.export_receipts()), (history, receipts))
            self.assertEqual(ResearchGraph.from_store(reopened).snapshot_hash, graph)
            self.assertEqual(resolution_states(reopened, reopened.events())[self.obligation]
                             ["resolution"]["id"], resolution)
            self.assertEqual(followup_state(reopened, self.obligation)["obligation_resolution"],
                             "reviewer_satisfied")


class SoleObligationResolutionTests(unittest.TestCase):
    """One source finding can be addressed without approving the original claim."""

    components = staticmethod(test_followup_execution.FollowupExecutionTests.components)
    _finish = test_followup_execution.FollowupExecutionTests._finish
    envelope = test_followup_execution.FollowupExecutionTests.envelope
    command = test_followup_execution.FollowupExecutionTests.command
    prepare = test_followup_execution.FollowupExecutionTests.prepare

    def setUp(self):
        test_followup_execution.FollowupExecutionTests.setUp(self)

    def complete_child(self):
        selection = self.search.select_next(self.tree)
        protocol = followup_state(self.store, self.obligation)["protocol"]
        primary = Kernel(self.store, self.executor).start_run(protocol, seed=8,
            implementation=self.child_source, environment=self.environment,
            command=["python", "control.py"])
        first = Kernel(self.store, self.executor).finish_run(primary, status="completed",
            outputs=dict(raw_data=self.data, metrics=self.store.put_json({"mean": 2.0}),
                         log=self.store.put(b"synthetic control completed")))
        replica = Kernel(self.store, self.replicator).start_run(protocol, seed=8,
            implementation=self.reanalysis, environment=self.environment,
            command=["python", "control-reanalysis.py"], replicate_of=primary)
        second = Kernel(self.store, self.replicator).finish_run(replica, status="completed",
            outputs=dict(raw_data=self.data, metrics=self.store.put_json({"mean": 2.0}),
                         log=self.store.put(b"synthetic reanalysis completed")))
        child = Kernel(self.store, self.analyst).claim(protocol=protocol,
            statement="This synthetic control is inconclusive about the broad mechanism",
            scope=self.scope, evidence=[primary, replica],
            limitations=["Synthetic fixture and same-data reanalysis only"], outcome="inconclusive")
        self.search.finish_selection(selection["id"], status="completed", actual_cost=2,
            reason="Control technically complete", run=primary, claim=child)
        basis = Kernel(self.store, self.reviewer).gate(child)["basis_hash"]
        return child, basis, [first, second]

    def resolve(self, child, basis, results):
        return self.command("replanning.resolve_obligation", self.reviewer,
            dict(obligation=self.obligation, claim=child, expected_basis=basis,
                 review_rationale="The bounded inconclusive claim is acceptable in this fixture",
                 resolution_rationale="The new control addresses the recorded confound",
                 evidence_refs=[child, *results], link_assessments=None))

    def test_effective_resolution_unblocks_only_the_reviewed_child(self):
        child, basis, results = self.complete_child()
        self.assertEqual(Kernel(self.store, self.reviewer).next_action(child)["action"], "replan")
        self.resolve(child, basis, results)
        self.assertEqual(open_obligations(self.store, self.store.events(), self.claim), [])
        self.assertEqual(Kernel(self.store, self.reviewer).next_action(child)["action"],
                         "paper_candidate")
        self.assertEqual(Kernel(self.store, self.reviewer).next_action(self.claim)["action"],
                         "replan")
        paper = PaperBuilder(self.store, Actor("fixture-writer", "writer")).build(
            title="Bounded inconclusive fixture draft", claims=[child],
            expected_bases={child: basis})
        self.assertEqual(Kernel._get(self.store.events(), paper, "paper")["payload"]["claims"],
                         [child])

    def test_later_reviewer_reversal_stales_resolution(self):
        child, basis, results = self.complete_child()
        self.resolve(child, basis, results)
        Kernel(self.store, self.reviewer).review(child, verdict="request_changes",
            rationale="Revised local opinion", actions=["Check another control"],
            expected_basis=basis)
        self.assertEqual(resolution_states(self.store, self.store.events())[self.obligation]["status"],
                         "stale_resolution")
        self.assertEqual([event["id"] for event in open_obligations(
            self.store, self.store.events(), self.claim)], [self.obligation])
        self.assertEqual(Kernel(self.store, self.reviewer).next_action(child)["action"], "replan")

    def test_later_source_approval_cannot_promote_the_old_or_child_claim(self):
        child, basis, results = self.complete_child()
        self.resolve(child, basis, results)
        Kernel(self.store, self.reviewer).review(self.claim, verdict="approve",
            rationale="Later local opinion about the original fixture claim",
            actions=[], expected_basis=self.basis)
        self.assertEqual(resolution_states(self.store, self.store.events())[self.obligation]["status"],
                         "stale_resolution")
        self.assertEqual(Kernel(self.store, self.reviewer).next_action(self.claim)["action"],
                         "replan")
        self.assertEqual(Kernel(self.store, self.reviewer).next_action(child)["action"],
                         "replan")
        with self.assertRaisesRegex(ValueError, "not eligible for paper"):
            PaperBuilder(self.store, Actor("fixture-writer", "writer")).build(
                title="Premature fixture draft", claims=[child], expected_bases={child: basis})

    def test_completed_batch_with_null_terminal_claim_can_be_reviewed(self):
        # All completion manifests here are synthetic fixture records. No worker
        # or scientific experiment is launched by this contract test.
        batch = CommandService(self.store).execute(self.prepare())
        all_runs: list[str] = []
        all_results: list[str] = []
        blank = self.store.put(b"")
        for slot, actor in (("primary:8", Actor("child-executor", "executor")),
                            ("reanalysis:8", Actor("child-replicator", "replicator"))):
            binding = self.command("batch.enqueue_slot", actor, dict(batch=batch, slot=slot))
            job = Kernel._get(self.store.events(), binding, "batch_slot")["payload"]["job"]
            self.command("execution.dispatch", actor,
                dict(job=job, workspace_token=("1" if actor.role == "executor" else "2") * 32))
            state = execution_index(self.store, self.store.events())[job]
            spec = state["spec"]
            raw = self.data
            metrics = self.store.put_json({"mean": 2.0})
            def entry(path, digest):
                return dict(path=path, sha256=digest, bytes=len(self.store.read(digest)))
            now = datetime.now(timezone.utc).isoformat()
            manifest = self.store.put_json(dict(schema_version=1, identity=_identity(state),
                command=spec["command"], status="completed", elapsed_seconds=0,
                started_at=now, finished_at=now, returncode=0, reason="",
                termination_confirmed=True, runtime=spec["environment_fingerprint"],
                inputs_before=spec["expected_inputs"], inputs_after=spec["expected_inputs"],
                process_control="synthetic fixture only",
                outputs={"raw_data": entry(spec["outputs"]["raw_data"], raw),
                         "metrics": entry(spec["outputs"]["metrics"], metrics)},
                stdout=entry("stdout.bin", blank), stderr=entry("stderr.bin", blank)))
            self.command("execution.finalize", actor, dict(job=job, manifest=manifest))
            all_runs.append(state["run"]["id"])
            all_results.append(Kernel._result(self.store.events(), state["run"]["id"])["id"])
        with patch("episteme.execution.subprocess.Popen") as worker:
            terminal_id = self.command("batch.settle", self.planner, dict(batch=batch))
            worker.assert_not_called()
        terminal = Kernel._get(self.store.events(), terminal_id, "search_terminal")
        self.assertIsNone(terminal["payload"]["claim"])
        self.assertEqual(set(terminal["payload"]["runs"]), set(all_runs))
        protocol = followup_state(self.store, self.obligation)["protocol"]
        child = Kernel(self.store, self.analyst).claim(protocol=protocol,
            statement="Synthetic batch control remains bounded", scope=self.scope,
            evidence=all_runs, limitations=["Fabricated fixture completion manifests"],
            outcome="inconclusive")
        basis = Kernel(self.store, self.reviewer).gate(child)["basis_hash"]
        self.assertTrue(Kernel(self.store, self.reviewer).gate(child)["passed"])
        self.resolve(child, basis, all_results)
        self.assertEqual(resolution_states(self.store, self.store.events())[self.obligation]["status"],
                         "reviewer_satisfied")
        self.assertEqual(Kernel(self.store, self.reviewer).next_action(child)["action"],
                         "paper_candidate")


if __name__ == "__main__":
    unittest.main()
