"""Agent provenance fixtures; no model calls, experiments or scientific reviews.

Captured outputs and declared actors below are synthetic admission fixtures.
They test reference closure and contributor rejection, not AI independence.
"""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

from episteme.agents import Agents, agent_artifacts
from episteme.graph import GraphIntegrityError, NodeKind, Relation, ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.planning import Planning
from episteme.recovery import backup, restore
from episteme.reporting import artifact_inventory, review_bundle
from episteme.store import IntegrityError, Store, canonical


class AgentProvenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="episteme-agent-provenance-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.close)
        self.scope = {"population": "synthetic agent provenance fixture"}
        self.study = "agent-provenance-fixture"
        self.requester = "fixture-requester"
        self.assignee = "fixture-proposal-agent"
        self.question = Planning(self.store, Actor("fixture-question-author", "planner")).question(
            study_id=self.study, statement="Which synthetic alternatives should be tested?",
            objective="Exercise model proposal provenance only", scope=self.scope,
            constraints=["Synthetic fixture; no model call or scientific assertion"],
            stopping_criteria=["Stop after fixture application"])
        self.provider = self.store.put_json(dict(schema_version=1, provider="codex_cli_v1",
            profile_version=1, executable=sys.executable, executable_sha256="f"*64,
            cli_version="codex-cli 0.0.0", model="synthetic-provider-fixture", reasoning_effort="low"))

    def event(self, id):
        return next(event for event in self.store.events() if event["id"] == id)

    def command(self, method, *, actor=None, **payload):
        actor = actor or self.assignee
        context = dict(command_id=uuid4().hex, expected_revision=len(self.store.events()),
                       actor=actor, role="planner", study_id=self.study,
                       correlation_id="agent-provenance-fixture-cycle", causation_id=None)
        request = dict(version=1, action="agent." + method, payload=payload)
        service = Agents(self.store, Actor(actor, "planner"))
        return self.store.command(context, request, lambda: getattr(service, method)(**payload))

    def request(self):
        budget = self.command("register_budget", actor="fixture-budget-owner",
                              study_id=self.study, max_calls=1)
        request = self.command("request_hypotheses", actor=self.requester, budget=budget,
                               question=self.question, assignee=self.assignee, provider=self.provider)
        return budget, request

    def propose(self, *, apply=True, abstain=False):
        budget, request = self.request()
        dispatch = self.command("dispatch", request=request, workspace_token=uuid4().hex)
        proposal = dict(status="abstained" if abstain else "proposed", reason="Synthetic fixture only",
            candidates=[] if abstain else [dict(statement="Synthetic " + kind,
                prediction="Fixture prediction for " + kind, falsifier="Fixture falsifier for " + kind,
                kind=kind) for kind in ("null", "mechanism")],
            comparison_plan="" if abstain else "Compare synthetic alternatives with a declared control",
            limitations=["Fabricated test response; no scientific validity or model independence"])
        stream = b"\n".join(canonical(row) for row in [
            {"type": "thread.started", "thread_id": "synthetic-fixture-thread"},
            {"type": "turn.started"}, {"type": "item.completed", "item": {
                "type": "agent_message", "text": canonical(proposal).decode()}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 20}},
        ]) + b"\n"
        def entry(path, data):
            return dict(path=path, sha256=self.store.put(data), bytes=len(data))
        p = self.event(request)["payload"]
        spec = json.loads(self.store.read(p["specification"]))
        manifest = self.store.put_json(dict(schema_version=1,
            identity=dict(schema_version=1, request=request, dispatch=dispatch,
                          specification=p["specification"]),
            status="completed", reason="Synthetic completion fixture, never executed", returncode=0,
            command=spec["command"], elapsed_seconds=0.1, termination_confirmed=True,
            started_at="2026-09-23T10:00:00+00:00", finished_at="2026-09-23T10:00:00.100000+00:00",
            runtime=spec["environment_fingerprint"], inputs_before=spec["expected_inputs"],
            inputs_after=spec["expected_inputs"], outputs={"proposal": entry("proposal.json", canonical(proposal))},
            stdout=entry("stdout.bin", stream), stderr=entry("stderr.bin", b"")))
        response = self.command("finalize", request=request, manifest=manifest)
        application = self.command("apply_hypotheses", request=request) if apply else None
        return dict(budget=budget, request=request, dispatch=dispatch, response=response,
                    application=application)

    def claim(self, hypotheses, *, explanation_set=None):
        planner = Kernel(self.store, Actor("fixture-protocol-author", "planner"))
        code = self.store.put(b"Synthetic primary code; not executed")
        environment = self.store.put(b"Synthetic environment")
        raw = self.store.put_json([0, 0])
        arguments = dict(design="Synthetic provenance checks", metric="mean",
            analysis_plan="Synthetic recorded output only", stopping_rule="Fixed fixture schedule",
            seeds=[7], run_limit=2, implementation=code, environment=environment, data=raw,
            replication_tolerance=0)
        protocol = (planner.preregister_for_set(explanation_set=explanation_set, **arguments)
                    if explanation_set else planner.preregister(hypotheses=hypotheses,
                                                               scope=self.scope, **arguments))
        runs = []
        for role in ("executor", "replicator"):
            kernel = Kernel(self.store, Actor("fixture-" + role, role))
            run = kernel.start_run(protocol, seed=7, implementation=code if not runs else
                self.store.put(b"Synthetic separate reanalysis code; not executed"),
                environment=environment, command=["not-executed-fixture"],
                replicate_of=runs[0] if runs else None)
            kernel.finish_run(run, status="completed", reason="Synthetic result fixture", outputs=dict(
                raw_data=raw, metrics=self.store.put_json({"mean": 0.0}), log=self.store.put(role.encode())))
            runs.append(run)
        return Kernel(self.store, Actor("fixture-analyst", "analyst")).claim(protocol=protocol,
            statement="Synthetic inconclusive claim", scope=self.scope, evidence=runs,
            limitations=["Fixture outputs; no executed science"], outcome="inconclusive")

    def test_graph_records_typed_model_provenance_and_artifact_ancestry(self):
        ids = self.propose()
        applied = self.event(ids["application"])["payload"]
        before = self.store.export(), self.store.export_receipts()
        graph = ResearchGraph.from_store(self.store)
        expected = {"budget": NodeKind.AGENT_BUDGET, "request": NodeKind.AGENT_REQUEST,
                    "dispatch": NodeKind.AGENT_DISPATCH, "response": NodeKind.AGENT_RESPONSE,
                    "application": NodeKind.AGENT_APPLICATION}
        for name, kind in expected.items():
            self.assertEqual(graph.node(ids[name]).kind, kind)
        for generated in [applied["explanation_set"], *(row["hypothesis"] for row in applied["mapping"])]:
            ancestors = {node.id for node in graph.ancestors(generated)}
            self.assertTrue({self.question, ids["budget"], ids["request"], ids["dispatch"], ids["response"]} <= ancestors)
            self.assertIn(ResearchGraph.artifact_id(self.provider), ancestors)
            self.assertTrue(any(edge.source == ids["response"] and edge.target == generated
                                and edge.relation == Relation.AGENT_GENERATED for edge in graph.edges))
        keys = set().union(*(agent_artifacts(self.store, self.event(id)) for id in ids.values()))
        inventory = {row["sha256"] for row in artifact_inventory(self.store, self.store.events())}
        self.assertTrue(keys <= inventory)
        self.assertTrue({ResearchGraph.artifact_id(key) for key in keys} <= {node.id for node in graph.nodes})
        self.assertTrue(all(edge.scientific_validity == "not_assessed" for edge in graph.edges))
        self.assertEqual((self.store.export(), self.store.export_receipts()), before)

    def test_legacy_claim_includes_requester_budget_owner_and_assignee_as_contributors(self):
        ancestor = self.question
        arguments = {key: self.event(ancestor)["payload"][key] for key in
                     ("study_id", "statement", "objective", "scope", "constraints", "stopping_criteria")}
        self.question = Planning(self.store, Actor("fixture-question-reviser", "planner")).question(
            **arguments, parent=ancestor, revision_reason="Exercise original question author provenance")
        ids = self.propose()
        hypotheses = [row["hypothesis"] for row in self.event(ids["application"])["payload"]["mapping"]]
        claim = self.claim(hypotheses)
        kernel = Kernel(self.store, Actor("fixture-reader", "observer"))
        local = {event["id"] for event in kernel._local_evidence(self.store.events(), claim)[0]}
        self.assertTrue(set(ids.values()) <= local)
        self.assertTrue({ancestor, self.question} <= local)
        basis = kernel.gate(claim)["basis_hash"]
        for actor in (self.requester, self.assignee, "fixture-budget-owner",
                      "fixture-question-author", "fixture-question-reviser"):
            with self.subTest(actor=actor), self.assertRaisesRegex(ValueError, "contribut"):
                Kernel(self.store, Actor(actor, "reviewer")).review(claim, verdict="request_changes",
                    rationale="Test contributor rejection", actions=["Fixture action"], expected_basis=basis)
        review = Kernel(self.store, Actor("fixture-independent-opinion", "reviewer")).review(
            claim, verdict="request_changes", rationale="Synthetic opinion only; scientific validity unassessed",
            actions=["Obtain an actual independent scientific review"], expected_basis=basis)
        graph = ResearchGraph.from_store(self.store)
        cited = {edge.source for edge in graph.edges if edge.target == review and edge.relation == Relation.REVIEW_AGENT}
        self.assertEqual(cited, set(ids.values()))
        self.assertEqual(kernel.next_action(claim)["action"], "replan")

    def test_removed_generated_alternatives_remain_in_bound_planning_review_context(self):
        ids = self.propose()
        p = self.event(ids["application"])["payload"]
        planner = Kernel(self.store, Actor("fixture-revised-hypothesis-author", "planner"))
        fresh = [planner.hypothesis("Manual fixture " + str(index), "Fixture prediction", "Fixture falsifier",
                                    self.scope) for index in range(2)]
        revised = Planning(self.store, Actor("fixture-revised-set-author", "planner")).explanation_set(
            question=self.question, hypotheses=fresh, comparison_plan="Compare revised synthetic alternatives",
            parent=p["explanation_set"], revision_reason="Exercise provenance of excluded model candidates",
            excluded_reasons={row["hypothesis"]: "Superseded synthetic fixture, retained in history"
                              for row in p["mapping"]})
        claim = self.claim(fresh, explanation_set=revised)
        kernel = Kernel(self.store, Actor("fixture-reader", "observer"))
        evidence = {event["id"] for event in kernel._local_evidence(self.store.events(), claim)[0]}
        self.assertTrue(set(ids.values()) <= evidence)
        self.assertTrue({row["hypothesis"] for row in p["mapping"]} <= evidence)
        self.assertIn(self.requester, kernel._review_members(self.store.events(), claim)[1])
        self.assertEqual(len(evidence), len(kernel._local_evidence(self.store.events(), claim)[0]))

    def test_unrelated_later_model_proposal_does_not_change_existing_claim_basis(self):
        ids = self.propose()
        hypotheses = [row["hypothesis"] for row in self.event(ids["application"])["payload"]["mapping"]]
        claim = self.claim(hypotheses)
        kernel = Kernel(self.store, Actor("fixture-reader", "observer"))
        before = kernel.gate(claim)["basis_hash"]
        later = self.propose()
        self.assertEqual(kernel.gate(claim)["basis_hash"], before)
        evidence = {event["id"] for event in kernel._local_evidence(self.store.events(), claim)[0]}
        self.assertTrue(set(later.values()).isdisjoint(evidence))

    def test_abstention_exports_without_claims_and_snapshot_does_not_include_later_response(self):
        _, request = self.request()
        history = self.store.events()
        first = review_bundle(self.store, history)
        second = self.propose(apply=False, abstain=True)
        current = review_bundle(self.store, self.store.events())
        self.assertEqual(current["summary"]["claims"], [])
        self.assertEqual(current["summary"]["counts"]["agent_response"], 1)
        self.assertNotIn("hypothesis", current["summary"]["counts"])
        self.assertEqual(review_bundle(self.store, history), first)
        graph = ResearchGraph.from_store(self.store)
        self.assertEqual(graph.node(second["response"]).payload["assessment"]["proposal_status"], "abstained")
        self.assertEqual(graph.node(request).kind, NodeKind.AGENT_REQUEST)

    def test_unreceipted_agent_history_fails_graph_and_bundle_even_without_claims(self):
        self.store.append(id="forged-budget-fixture", kind="agent_budget", actor=self.requester, role="planner",
            payload=dict(schema_version=1, study_id=self.study, max_calls=1), expected_revision=len(self.store.events()))
        with self.assertRaisesRegex(GraphIntegrityError, "receipt"):
            ResearchGraph.from_store(self.store)
        with self.assertRaisesRegex(ValueError, "receipt"):
            review_bundle(self.store, self.store.events())

    def test_recovery_preserves_agent_graph_and_corrupt_proposal_cannot_pass_projection(self):
        ids = self.propose()
        graph = ResearchGraph.from_store(self.store).to_dict()
        backup_path = self.root / "backup"
        backup(self.store, backup_path)
        restored_path = self.root / "restored"
        restore(backup_path, restored_path)
        with Store(restored_path) as restored:
            self.assertEqual(ResearchGraph.from_store(restored).to_dict(), graph)
            self.assertEqual(artifact_inventory(restored, restored.events()),
                             artifact_inventory(self.store, self.store.events()))
        proposal = self.event(ids["response"])["payload"]["assessment"]["artifacts"]["proposal"]
        path = self.store.root / "artifacts" / "sha256" / proposal
        path.write_bytes(b"corrupted synthetic fixture")
        with self.assertRaises(IntegrityError):
            ResearchGraph.from_store(self.store)
        with self.assertRaises(IntegrityError):
            review_bundle(self.store, self.store.events())


if __name__ == "__main__":
    unittest.main()
