"""Typed negative opinions are fixtures, not independent scientific review."""

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from episteme.graph import NodeKind, ResearchGraph
from episteme.kernel import Actor, GateError, Kernel
from episteme.replanning import Replanning, _index, open_obligations
from episteme.reporting import PaperBuilder
from episteme.store import ConflictError, Store
from review_paths import approve, reconsider


class ReplanningTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="episteme-replanning-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.scope = {"population": "synthetic-replanning-test"}
        planner = Kernel(self.store, Actor("fixture-planner", "planner"))
        executor = Kernel(self.store, Actor("fixture-executor", "executor"))
        replicator = Kernel(self.store, Actor("fixture-replicator", "replicator"))
        analyst = Kernel(self.store, Actor("fixture-analyst", "analyst"))
        hypothesis = [planner.hypothesis(name, name, "opposite", self.scope)
                      for name in ("signal", "null")]
        code = self.store.put(b"fixture primary code")
        recode = self.store.put(b"fixture reanalysis code")
        environment = self.store.put(b"fixture environment")
        raw = self.store.put(b"0\n")
        self.protocol = planner.preregister(hypotheses=hypothesis, scope=self.scope,
            design="Synthetic two-explanation test", metric="mean", analysis_plan="Read fixture",
            stopping_rule="One seed", seeds=[7], run_limit=4, implementation=code,
            environment=environment, data=raw, replication_tolerance=0.0)
        outputs = dict(raw_data=raw, metrics=self.store.put_json({"mean": 0.0}),
                       log=self.store.put(b"synthetic fixture result"))
        self.primary = executor.start_run(self.protocol, seed=7, implementation=code,
                                          environment=environment, command=["fixture"])
        executor.finish_run(self.primary, status="completed", outputs=outputs)
        replica = replicator.start_run(self.protocol, seed=7, implementation=recode,
                                       environment=environment, command=["fixture"],
                                       replicate_of=self.primary)
        replicator.finish_run(replica, status="completed", outputs=outputs)
        self.claim = analyst.claim(protocol=self.protocol, statement="Fixture mean is zero",
                                   scope=self.scope, evidence=[self.primary, replica],
                                   limitations=["Synthetic fixture only"], outcome="inconclusive")
        self.basis = Kernel(self.store, Actor("fixture-reviewer", "reviewer")).gate(self.claim)["basis_hash"]
        self.findings = [
            dict(kind="discriminating_experiment", action="Test a held-out contrast",
                 closure_criterion="A frozen contrast and new evidence are reviewed",
                 evidence_refs=[self.claim, self.primary]),
            dict(kind="narrow_claim", action="Limit the population claim",
                 closure_criterion="A bounded revised claim receives independent review",
                 evidence_refs=[self.claim]),
        ]

    def command(self, *, findings=None, basis=None, command_id="typed-review"):
        payload = dict(claim=self.claim, verdict="request_changes",
                       rationale="Fixture opinion identifies unresolved work",
                       findings=copy.deepcopy(self.findings if findings is None else findings),
                       expected_basis=self.basis if basis is None else basis,
                       link_assessments=None)
        context = dict(command_id=command_id, expected_revision=len(self.store.events()),
                       actor="fixture-reviewer", role="reviewer", study_id="fixture-study",
                       correlation_id="fixture-cycle", causation_id=None)
        request = dict(version=1, action="replanning.record_review", payload=payload)
        return context, request

    def record(self, context, request):
        return self.store.command(context, request, lambda: Replanning(
            self.store, Actor(context["actor"], context["role"])).record_review(**request["payload"]))

    def test_negative_review_and_each_obligation_have_one_receipt_and_bound_revisions(self):
        context, request = self.command()
        prior = self.store.events()
        result = self.record(context, request)
        self.assertEqual(open_obligations(self.store, prior, self.claim), [])
        self.assertEqual(len(result["obligations"]), 2)
        events = self.store.events()
        review, first, second = events[-3:]
        self.assertEqual([event["id"] for event in (review, first, second)],
                         [result["review"], *result["obligations"]])
        self.assertEqual([event["kind"] for event in (review, first, second)],
                         ["review", "review_obligation", "review_obligation"])
        self.assertEqual([event["payload"]["finding_index"] for event in (first, second)], [0, 1])
        self.assertEqual(first["payload"]["review_hash"], review["hash"])
        self.assertEqual(first["payload"]["basis_hash"], self.basis)
        self.assertEqual(first["payload"]["evidence_refs"][0]["hash"],
                         next(event["hash"] for event in events if event["id"] == self.claim))
        self.assertEqual(self.store.receipts()[-1]["event_ids"], [event["id"] for event in events[-3:]])
        self.assertEqual([event["id"] for event in open_obligations(self.store, events, self.claim)],
                         result["obligations"])
        self.assertEqual(set(_index(self.store, events)), {result["review"]})
        graph = ResearchGraph.from_store(self.store)
        self.assertEqual(graph.node(first["id"]).kind, NodeKind.REVIEW_OBLIGATION)

        # A later approval is another opinion; it does not satisfy closure criteria.
        reviewer = Kernel(self.store, Actor("fixture-reviewer", "reviewer"))
        reconsider(self.store, self.claim, reviewer="fixture-reviewer", study="fixture-study")
        self.assertEqual(len(open_obligations(self.store, self.store.events(), self.claim)), 2)
        self.assertEqual(self.store.events()[-1]["kind"], "review_submission")
        decision = reviewer.next_action(self.claim)
        self.assertEqual(decision["action"], "replan")
        self.assertEqual(decision["obligations"], result["obligations"])
        with self.assertRaisesRegex(GateError, "not eligible for paper"):
            PaperBuilder(self.store, Actor("fixture-writer", "writer")).build(
                title="Blocked fixture draft", claims=[self.claim],
                expected_bases={self.claim: self.basis})
        self.assertFalse(any(event["kind"] == "paper" for event in self.store.events()))
        self.assertEqual(ResearchGraph.from_store(self.store).node(second["id"]).kind,
                         NodeKind.REVIEW_OBLIGATION)

    def test_replay_after_reopen_is_idempotent_and_does_not_revalidate_against_new_runs(self):
        context, request = self.command()
        result = self.record(context, request)
        count = len(self.store.events())
        with Store(self.root) as reopened:
            self.assertEqual(open_obligations(reopened, reopened.events(), self.claim)[0]["payload"]["review"],
                             result["review"])
        # Incomplete new run stales the current basis, but the historical record remains valid.
        protocol = next(event for event in self.store.events() if event["id"] == self.protocol)
        executor = Kernel(self.store, Actor("fixture-executor", "executor"))
        executor.start_run(self.protocol, seed=7, implementation=protocol["payload"]["implementation"],
                           environment=protocol["payload"]["environment"], command=["fixture"])
        newer = self.store.events()
        self.assertEqual(len(open_obligations(self.store, newer, self.claim)), 2)
        with patch.object(Replanning, "record_review", side_effect=AssertionError("replay invoked handler")):
            self.assertEqual(self.record(context, request), result)
        self.assertEqual(len(self.store.events()), count + 1)
        changed = copy.deepcopy(request)
        changed["payload"]["findings"][0]["action"] = "Different requirement"
        with self.assertRaises(ConflictError):
            self.record(context, changed)

    def test_stale_basis_bad_citation_and_nonreviewer_roll_back(self):
        for context, request in [self.command(basis="0" * 64, command_id="stale"),
                                 self.command(findings=[dict(self.findings[0], evidence_refs=["missing"])],
                                              command_id="foreign")]:
            with self.subTest(context=context["command_id"]):
                before = self.store.events()
                with self.assertRaises(GateError):
                    self.record(context, request)
                self.assertEqual(self.store.events(), before)
        context, request = self.command(command_id="wrong-role")
        context["role"] = "analyst"
        with self.assertRaises(GateError):
            self.record(context, request)
        self.assertFalse(any(event["kind"] in {"review", "review_obligation"}
                             for event in self.store.events()))

    def test_failure_after_review_append_rolls_back_both_events_and_receipt(self):
        context, request = self.command()
        before = self.store.events()
        receipts = self.store.receipts()
        original = Store.append

        def fail_obligation(store, **kwargs):
            if kwargs["kind"] == "review_obligation":
                raise RuntimeError("injected obligation write failure")
            return original(store, **kwargs)

        with patch.object(Store, "append", fail_obligation):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.record(context, request)
        self.assertEqual(self.store.events(), before)
        self.assertEqual(self.store.receipts(), receipts)

    def test_orphaned_or_forged_obligations_fail_replay_validation(self):
        context, request = self.command()
        result = self.record(context, request)
        old = self.store.events()
        self.store.append(id="review_obligation-forged", kind="review_obligation",
                          actor="fixture-reviewer", role="reviewer",
                          payload=copy.deepcopy(old[-1]["payload"]), expected_revision=len(old))
        with self.assertRaisesRegex(GateError, "lacks its original command receipt"):
            open_obligations(self.store, self.store.events(), self.claim)
        self.assertEqual(result["review"], old[-3]["id"])

    def test_duplicate_and_noncontext_references_are_rejected(self):
        unrelated = Kernel(self.store, Actor("fixture-planner", "planner")).hypothesis(
            "Unrelated", "none", "none", self.scope)
        for bad in ([self.claim, self.claim], [unrelated]):
            with self.subTest(bad=bad):
                findings = [dict(self.findings[0], evidence_refs=bad)]
                context, request = self.command(findings=findings, command_id=str(bad))
                with self.assertRaises(GateError):
                    self.record(context, request)
        self.assertFalse(any(event["kind"] == "review" for event in self.store.events()))

    def test_complete_receipt_with_forged_action_is_rejected_on_replay(self):
        context, request = self.command(findings=[self.findings[0]], command_id="forged-action")

        def forge():
            kernel = Kernel(self.store, Actor("fixture-reviewer", "reviewer"))
            review_id = kernel.review(self.claim, verdict="request_changes",
                rationale=request["payload"]["rationale"], actions=["Different action"],
                expected_basis=self.basis)
            events = self.store.events()
            review = next(event for event in events if event["id"] == review_id)
            claim = next(event for event in events if event["id"] == self.claim)
            ref = next(event for event in events if event["id"] == self.primary)
            obligation = kernel._write(events, "review_obligation", dict(
                schema_version=1, review=review_id, review_hash=review["hash"],
                claim=self.claim, claim_hash=claim["hash"], basis_hash=self.basis,
                finding_index=0, kind="discriminating_experiment", action="Different action",
                closure_criterion=self.findings[0]["closure_criterion"],
                evidence_refs=[dict(id=self.primary, hash=ref["hash"])]), {"reviewer"})
            return dict(review=review_id, obligations=[obligation])

        self.store.command(context, request, forge)
        with self.assertRaisesRegex(GateError, "review differs from its typed findings"):
            open_obligations(self.store, self.store.events(), self.claim)

    def test_approval_is_not_a_typed_obligation_transition(self):
        context, request = self.command(command_id="approval-attempt")
        request["payload"]["verdict"] = "approve"
        with self.assertRaisesRegex(GateError, "negative review verdict"):
            self.record(context, request)
        self.assertFalse(any(event["kind"] in {"review", "review_obligation"}
                             for event in self.store.events()))

    def test_open_source_obligation_blocks_a_reviewed_successor_paper(self):
        context, request = self.command(command_id="typed-source-review")
        source_review = self.record(context, request)["review"]
        analyst = Kernel(self.store, Actor("fixture-analyst", "analyst"))
        evidence = next(event for event in self.store.events()
                        if event["id"] == self.claim)["payload"]["evidence"]
        successor = analyst.claim(protocol=self.protocol,
            statement="A narrower fixture mean was observed", scope=self.scope,
            evidence=evidence, limitations=["Synthetic fixture only"],
            outcome="inconclusive")
        planner = Kernel(self.store, Actor("fixture-planner", "planner"))
        reader = Kernel(self.store, Actor("successor-reviewer", "reviewer"))
        link = planner.link_claims(source=successor, target=self.claim,
            relation="supersedes", rationale="Narrower replacement of the old claim",
            expected_bases={self.claim: planner.gate(self.claim)["basis_hash"],
                            successor: planner.gate(successor)["basis_hash"]})
        new_basis = reader.gate(successor)["basis_hash"]
        approve(self.store, successor, reviewer="successor-reviewer", expected_basis=new_basis,
                link_assessments={link: dict(
                    judgment="accepted", disposition="compatible_as_written",
                    rationale="The replacement acknowledges the unresolved source objection",
                    evidence=[source_review, successor])})
        decision = reader.next_action(successor)
        self.assertEqual(decision["action"], "replan")
        self.assertEqual(len(decision["obligations"]), 2)
        with self.assertRaisesRegex(GateError, "not eligible for paper"):
            PaperBuilder(self.store, Actor("fixture-writer", "writer")).build(
                title="Blocked successor draft", claims=[successor],
                expected_bases={successor: new_basis})


if __name__ == "__main__":
    unittest.main()
