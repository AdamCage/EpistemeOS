"""Durable reviewer delivery; fixture responses do not attest scientific review."""

import base64
import json
import subprocess
import sys
import unittest

from episteme.commands import CommandService
from episteme.graph import GraphIntegrityError, NodeKind, Relation, ResearchGraph
from episteme.kernel import Actor, GateError, Kernel
from episteme.replanning import _index as obligations_index, open_obligations
from episteme.reviewer_controller import ReviewerController, _index
from episteme.review_submission import _index as submission_index
from episteme.store import Store

import test_review_assignment as assignment_fixtures


class _Provider:
    def __init__(self, *, fail=False, reply=b'{"fixture":"unadjudicated"}'):
        self.requests = []
        self.fail = fail
        self.reply = reply

    def invoke(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("provider outcome is unknown")
        return self.reply, {"input_tokens": 10, "output_tokens": 3}


class ReviewerControllerTests(unittest.TestCase):
    def setUp(self):
        assignment_fixtures.ReviewAssignmentTests.setUp(self)
        self.assignment = self.service.execute(
            assignment_fixtures.ReviewAssignmentTests.envelope(self))["assignment"]

    def controller(self, provider, *, study_id="fixture-study"):
        return ReviewerController(self.store, Actor("fixture-planner", "planner"),
                                  study_id, "fixture-provider-v1", provider)

    def test_delivers_only_frozen_projection_once_and_replays_after_restart(self):
        provider = _Provider()
        controller = self.controller(provider)
        result = controller.advance(self.assignment)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(provider.requests), 1)
        request = provider.requests[0]
        self.assertEqual(request["assignment"], self.assignment)
        self.assertEqual(request["identity_assurance"], "caller_declared")
        self.assertEqual(request["read_isolation"], "not_enforced")
        self.assertEqual({row["digest"] for row in request["allowed_artifacts"]},
                         {self.raw, self.metrics})
        for row in request["allowed_artifacts"]:
            self.assertEqual(base64.b64decode(row["data_base64"]), self.store.read(row["digest"]))
        encoded = json.dumps(request)
        for hidden in (self.code, self.recode, self.environment, self.log, self.holdout,
                       "secret-command", "secret-reanalysis"):
            self.assertNotIn(hidden, encoded)
        state = _index(self.store, self.store.events())[self.assignment]
        self.assertEqual(state["response"]["id"], result["response_event"])
        self.assertEqual(self.store.read(result["response"]), b'{"fixture":"unadjudicated"}')
        self.assertEqual(self.controller(provider).advance(self.assignment), result)
        self.assertEqual(len(provider.requests), 1)

        graph = ResearchGraph.from_store(self.store)
        self.assertEqual(graph.node(state["dispatch"]["id"]).kind, NodeKind.REVIEW_DISPATCH)
        self.assertEqual(graph.node(result["response_event"]).kind, NodeKind.REVIEW_RESPONSE)
        self.assertTrue(any(edge.source == state["dispatch"]["id"]
                            and edge.target == result["response_event"]
                            and edge.relation == Relation.REVIEW_DELIVERY_REFERENCE
                            for edge in graph.edges))
        self.assertFalse(any(event["kind"] == "review" for event in self.store.events()))
        with Store(self.root) as reopened:
            self.assertEqual(_index(reopened, reopened.events())[self.assignment]
                             ["response"]["id"], result["response_event"])

    def test_unknown_is_not_retried_and_requires_explicit_reconciliation(self):
        provider = _Provider(fail=True)
        controller = self.controller(provider)
        first = controller.advance(self.assignment)
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(controller.advance(self.assignment), first)
        self.assertEqual(len(provider.requests), 1)
        self.assertIsNone(_index(self.store, self.store.events())[self.assignment]["response"])
        raw = self.store.put(b'{"fixture":"late-response"}')
        controller.reconcile(self.assignment, response=raw, status="completed", usage={})
        self.assertEqual(controller.advance(self.assignment)["response"], raw)
        self.assertEqual(len(provider.requests), 1)
        self.assertIsNotNone(ResearchGraph.from_store(self.store))

    def test_oversized_raw_response_stays_unknown(self):
        provider = _Provider(reply=b"x" * (1024 * 1024 + 1))
        controller = self.controller(provider)
        self.assertEqual(controller.advance(self.assignment)["status"], "unknown")
        self.assertEqual(len(provider.requests), 1)
        self.assertIsNone(_index(self.store, self.store.events())[self.assignment]["response"])
        oversized = self.store.put(provider.reply)
        with self.assertRaises((GateError, ValueError)):
            controller.reconcile(self.assignment, response=oversized,
                                 status="completed", usage={})
        self.assertIsNone(_index(self.store, self.store.events())[self.assignment]["response"])

    def test_study_role_and_completion_checks_fail_closed(self):
        provider = _Provider()
        before = len(self.store.events())
        with self.assertRaises((GateError, ValueError)):
            self.controller(provider, study_id="other-study").advance(self.assignment)
        self.assertEqual(len(provider.requests), 0)
        self.assertEqual(len(self.store.events()), before)
        envelope = dict(context=dict(command_id="wrong-role-review", expected_revision=before,
                                     actor="fixture-reviewer", role="reviewer",
                                     study_id="fixture-study", correlation_id="fixture-cycle",
                                     causation_id=None),
                        request=dict(version=1, action="review.dispatch",
                                     payload=dict(assignment=self.assignment,
                                                  provider_id="fixture-provider-v1")))
        with self.assertRaises(ValueError):
            CommandService(self.store).execute(envelope)
        with self.assertRaises((GateError, ValueError)):
            self.controller(provider).reconcile(self.assignment, response=None,
                                                status="completed", usage={})
        self.assertEqual(len(self.store.events()), before)


class ReviewSubmissionTests(unittest.TestCase):
    def setUp(self):
        assignment_fixtures.ReviewAssignmentTests.setUp(self)
        result = self.service.execute(assignment_fixtures.ReviewAssignmentTests.envelope(self))
        self.assignment, self.bundle = result["assignment"], result["bundle"]

    def decision(self, verdict="approve", findings=None, *, actor="fixture-reviewer"):
        return dict(schema_version=1, assignment=self.assignment, bundle=self.bundle,
                    claim=self.claim, basis_hash=self.basis, reviewer_actor=actor,
                    verdict=verdict, rationale="Synthetic fixture opinion; not a scientific assessment",
                    findings=[] if findings is None else findings, link_assessments=None)

    def deliver(self, decision):
        provider = _Provider(reply=json.dumps(decision).encode())
        result = ReviewerController(self.store, Actor("fixture-planner", "planner"),
                                    "fixture-study", "fixture-provider-v1", provider).advance(self.assignment)
        self.assertEqual(result["status"], "completed")
        return result["response"]

    def envelope(self, response, *, actor="fixture-reviewer", command_id="submit-review",
                 basis=None, study_id="fixture-study"):
        return dict(context=dict(command_id=command_id, expected_revision=len(self.store.events()),
                                 actor=actor, role="reviewer", study_id=study_id,
                                 correlation_id="fixture-cycle", causation_id=None),
                    request=dict(version=1, action="review.submit", payload=dict(
                        assignment=self.assignment, response=response,
                        expected_basis=self.basis if basis is None else basis)))

    def test_approval_is_bound_to_delivery_and_exact_receipt(self):
        response = self.deliver(self.decision())
        envelope = self.envelope(response)
        result = self.service.execute(envelope)
        self.assertEqual(result["obligations"], [])
        self.assertEqual([event["kind"] for event in self.store.events()[-2:]],
                         ["review", "review_submission"])
        self.assertEqual(submission_index(self.store, self.store.events())[self.assignment]
                         ["review"]["id"], result["review"])
        self.assertEqual(self.service.execute(envelope), result)
        self.assertEqual(len(submission_index(self.store, self.store.events())), 1)
        with self.assertRaises((GateError, ValueError)):
            self.service.execute(self.envelope(response, command_id="second-verdict"))
        graph = ResearchGraph.from_store(self.store)
        self.assertEqual(graph.node(result["submission"]).kind, NodeKind.REVIEW_SUBMISSION)
        self.assertEqual(graph.node(result["review"]).scientific_validity, "not_assessed")
        self.assertTrue(any(edge.source == result["review"]
                            and edge.target == result["submission"] for edge in graph.edges))
        with Store(self.root) as reopened:
            self.assertEqual(submission_index(reopened, reopened.events())[self.assignment]
                             ["submission"]["id"], result["submission"])

    def test_negative_decision_preserves_typed_open_obligation(self):
        finding = dict(kind="discriminating_experiment", action="Run a control",
                       closure_criterion="New control result addresses the synthetic confound",
                       evidence_refs=[self.claim])
        response = self.deliver(self.decision("request_changes", [finding]))
        result = self.service.execute(self.envelope(response))
        self.assertEqual(len(result["obligations"]), 1)
        self.assertEqual([event["kind"] for event in self.store.events()[-3:]],
                         ["review", "review_obligation", "review_submission"])
        self.assertEqual([e["id"] for e in open_obligations(self.store,
                          self.store.events(), self.claim)], result["obligations"])
        self.assertIn(result["review"], obligations_index(self.store, self.store.events()))
        self.assertNotEqual(Kernel(self.store, Actor("reader", "observer"))
                            .next_action(self.claim)["action"], "paper_candidate")
        self.assertIsNotNone(ResearchGraph.from_store(self.store))

    def test_linked_context_requires_explicit_assessment(self):
        analyst = Kernel(self.store, Actor("fixture-analyst", "analyst"))
        other = analyst.claim(protocol=self.protocol, statement="Alternative fixture interpretation",
                              scope=self.scope, evidence=[self.primary,
                              next(e["id"] for e in self.store.events() if e["kind"] == "run"
                                   and e["payload"]["replicate_of"] == self.primary)],
                              limitations=["Synthetic fixture only"], outcome="inconclusive")
        kernel = Kernel(self.store, Actor("fixture-planner", "planner"))
        bases = {id: kernel.gate(id)["basis_hash"] for id in (self.claim, other)}
        link = kernel.link_claims(source=self.claim, target=other, relation="contradicts",
                                  rationale="Competing fixture interpretation",
                                  expected_bases=bases)
        self.basis = kernel.gate(self.claim)["basis_hash"]
        assignment = self.service.execute(assignment_fixtures.ReviewAssignmentTests.envelope(
            self, basis=self.basis, command_id="linked-assignment"))
        self.assignment, self.bundle = assignment["assignment"], assignment["bundle"]
        response = self.deliver(self.decision())
        with self.assertRaises((GateError, ValueError)):
            self.service.execute(self.envelope(response, command_id="missing-assessment"))
        self.assertFalse(any(e["kind"] == "review_submission" for e in self.store.events()))
        second = self.service.execute(assignment_fixtures.ReviewAssignmentTests.envelope(
            self, reviewer="fixture-reviewer-2", basis=self.basis,
            command_id="linked-second-assignment"))
        self.assignment, self.bundle = second["assignment"], second["bundle"]
        decision = self.decision(actor="fixture-reviewer-2")
        decision["link_assessments"] = {link: dict(
            judgment="rejected", disposition="compatible_as_written",
            rationale="Fixture relation is not established by observed data",
            evidence=[self.claim])}
        response = self.deliver(decision)
        result = self.service.execute(self.envelope(response, actor="fixture-reviewer-2",
                                                    command_id="linked-submission"))
        self.assertIn("link_assessments", Kernel._get(self.store.events(), result["review"],
                                                     "review")["payload"])
        self.assertIsNotNone(ResearchGraph.from_store(self.store))

    def test_wrong_actor_unrecorded_response_and_stale_basis_do_not_submit(self):
        response = self.deliver(self.decision())
        before = len(self.store.events())
        with self.assertRaises((GateError, ValueError)):
            self.service.execute(self.envelope(response, actor="someone-else",
                                               command_id="wrong-reviewer"))
        with self.assertRaises((GateError, ValueError)):
            self.service.execute(self.envelope(self.store.put(b"{}"),
                                               command_id="unrecorded-response"))
        self.assertEqual(len(self.store.events()), before)
        executor = Kernel(self.store, Actor("fixture-executor", "executor"))
        executor.start_run(self.protocol, seed=7, implementation=self.code,
                           environment=self.environment, command=["later attempt"])
        with self.assertRaises((GateError, ValueError)):
            self.service.execute(self.envelope(response, command_id="stale-review"))
        self.assertFalse(any(e["kind"] == "review_submission" for e in self.store.events()))

    def test_invalid_response_does_not_turn_missing_evidence_into_approval(self):
        bad = self.decision()
        bad["basis_hash"] = "0" * 64
        response = self.deliver(bad)
        with self.assertRaises((GateError, ValueError)):
            self.service.execute(self.envelope(response))
        self.assertFalse(any(e["kind"] == "review" for e in self.store.events()))

    def test_failed_delivery_cannot_submit(self):
        failed = _Provider(fail=True)
        controller = ReviewerController(self.store, Actor("fixture-planner", "planner"),
                                        "fixture-study", "fixture-provider-v1", failed)
        self.assertEqual(controller.advance(self.assignment)["status"], "unknown")
        raw = self.store.put(json.dumps(self.decision()).encode())
        controller.reconcile(self.assignment, response=raw, status="failed", usage={})
        with self.assertRaises((GateError, ValueError)):
            self.service.execute(self.envelope(raw, command_id="failed-response"))
        self.assertFalse(any(e["kind"] == "review" for e in self.store.events()))

    def test_duplicate_response_json_keys_cannot_submit(self):
        reply = json.dumps(self.decision()).replace(
            '"verdict": "approve"', '"verdict": "approve", "verdict": "reject"')
        provider = _Provider(reply=reply.encode())
        result = ReviewerController(self.store, Actor("fixture-planner", "planner"),
                                    "fixture-study", "fixture-provider-v1", provider).advance(self.assignment)
        self.assertEqual(result["status"], "completed")
        with self.assertRaisesRegex(ValueError, "duplicate command JSON key"):
            self.service.execute(self.envelope(result["response"]))
        self.assertFalse(any(e["kind"] == "review" for e in self.store.events()))

    def test_orphan_submission_fails_replay_and_graph(self):
        response = self.deliver(self.decision())
        result = self.service.execute(self.envelope(response))
        payload = dict(self.store.events()[-1]["payload"])
        self.assertEqual(self.store.events()[-1]["id"], result["submission"])
        self.store.append(id="review_submission-orphan", kind="review_submission",
                          actor="fixture-reviewer", role="reviewer", payload=payload,
                          expected_revision=len(self.store.events()))
        with self.assertRaisesRegex(GateError, "lacks its original receipt"):
            submission_index(self.store, self.store.events())
        with self.assertRaises(GraphIntegrityError):
            ResearchGraph.from_store(self.store)

    def test_cli_submission_replay_and_graph(self):
        response = self.deliver(self.decision())
        path = self.root.parent / "submit.json"
        path.write_text(json.dumps(self.envelope(response)), encoding="utf-8")

        def cli(*args):
            return subprocess.run([sys.executable, "-m", "episteme.cli", *args],
                                  capture_output=True, text=True, check=False)

        args = ("command", "--input", str(path), "--root", str(self.root))
        first = cli(*args)
        self.assertEqual(first.returncode, 0, first.stderr)
        repeated = cli(*args)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(json.loads(first.stdout), json.loads(repeated.stdout))
        graph = cli("graph", "--root", str(self.root))
        self.assertEqual(graph.returncode, 0, graph.stderr)
        self.assertEqual(json.loads(graph.stdout)["node_kinds"]["review_submission"], 1)


if __name__ == "__main__":
    unittest.main()
