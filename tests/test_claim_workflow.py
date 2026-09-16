"""Claim-relation workflow contracts using explicit output and opinion fixtures.

No experiment or scientific review is performed here. Distinct fixture branches
have distinct protocol inputs and artifacts, so implicit data exposure cannot
stand in for the claim-link propagation that these tests exercise.
"""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from episteme.claim_context import ClaimContextError, resolve_context
from episteme.commands import CommandService
from episteme.graph import GraphIntegrityError, NodeKind, Relation, ResearchGraph
from episteme.kernel import Actor, GateError, Kernel
from episteme.reporting import PaperBuilder
from episteme.store import ConflictError, IntegrityError, Store, canonical, digest


class ClaimWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="episteme-claim-workflow-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.scope = {"population": "isolated synthetic claim-link fixture"}
        self.linker = self.kernel("fixture-link-author", "planner")
        self.reader = self.kernel("fixture-state-reader", "planner")
        self.builder = PaperBuilder(self.store, Actor("fixture-writer", "writer"))
        self.branches = {}

    def kernel(self, actor, role):
        return Kernel(self.store, Actor(actor, role))

    def event(self, id):
        return next(event for event in self.store.events() if event["id"] == id)

    def branch(self, name, *, replicate=True, scope=None):
        scope = self.scope if scope is None else scope
        authors = {role: f"fixture-{name}-{role}" for role in
                   ("hypothesizer", "planner", "executor", "replicator", "analyst")}
        proposer = self.kernel(authors["hypothesizer"], "planner")
        hypotheses = [proposer.hypothesis(f"{name} {label}", "Fixture prediction",
                         "Fixture falsifier", scope) for label in ("signal", "null")]
        blobs = {field: self.store.put(f"Fixture {name}: {field}".encode()) for field in
                 ("implementation", "reimplementation", "environment", "data")}
        protocol = self.kernel(authors["planner"], "planner").preregister(
            hypotheses=hypotheses, scope=scope, design=f"Synthetic branch {name}", metric="mean",
            analysis_plan="All fixture units, no scientific inference", stopping_rule="Fixed fixture",
            seeds=[7], run_limit=20, implementation=blobs["implementation"], environment=blobs["environment"],
            data=blobs["data"], replication_tolerance=0.0)
        record = dict(name=name, authors=authors, scope=scope, protocol=protocol, **blobs)
        original, result = self.attempt(record)
        evidence = [original]
        if replicate:
            replica, _ = self.attempt(record, replica_of=original)
            evidence.append(replica)
        claim = self.kernel(authors["analyst"], "analyst").claim(
            protocol=protocol, statement=f"Synthetic {name} mean is zero; no population inference",
            scope=scope, evidence=evidence, limitations=["Fixture output and declared roles only"],
            outcome="inconclusive")
        record.update(claim=claim, primary=original, primary_result=result, evidence=evidence)
        self.branches[claim] = record
        return claim

    def attempt(self, branch, *, replica_of=None, status="completed"):
        record = self.branches[branch] if isinstance(branch, str) else branch
        role = "replicator" if replica_of else "executor"
        actor = self.kernel(record["authors"][role], role)
        run = actor.start_run(record["protocol"], seed=7,
            implementation=record["reimplementation"] if replica_of else record["implementation"],
            environment=record["environment"], command=["not-executed-fixture", record["name"]],
            replicate_of=replica_of)
        outputs = dict(log=self.store.put(f"Fixture {record['name']} {run}: {status}".encode()))
        if status == "completed":
            outputs.update(raw_data=record["data"], metrics=self.store.put_json(
                {"mean": 0.0, "fixture_branch": record["name"]}))
        result = actor.finish_run(run, status=status, outputs=outputs, reason="Explicit fixture terminal state")
        return run, result

    def bases(self, *claims):
        return {claim: self.reader.gate(claim)["basis_hash"] for claim in claims}

    def link(self, source, target, relation="supports", *, actor=None):
        return (actor or self.linker).link_claims(source=source, target=target, relation=relation,
            rationale="Fixture relation proposal, not verified scientific support",
            expected_bases=self.bases(source, target))

    def assessments(self, claim, *, judgment="accepted", disposition="compatible_as_written"):
        context = resolve_context(self.store.events(), claim)
        return {id: dict(judgment=judgment, disposition=disposition,
                         rationale="Explicit test opinion only; no scientific assessment performed",
                         evidence=[self.event(id)["payload"]["source"], self.event(id)["payload"]["target"]])
                for id in context.link_ids}

    def review(self, claim, *, actor="fixture-independent-reviewer", verdict="approve", assessments=None):
        reviewer = self.kernel(actor, "reviewer")
        args = dict(verdict=verdict, rationale="Explicit fixture opinion, not a scientific approval",
                    actions=[] if verdict == "approve" else ["Fixture control remains required"],
                    expected_basis=self.bases(claim)[claim])
        if resolve_context(self.store.events(), claim).link_ids:
            return reviewer.review_with_links(claim, **args,
                link_assessments=self.assessments(claim) if assessments is None else assessments)
        return reviewer.review(claim, **args)

    def paper(self, claim):
        return self.builder.build(title="Synthetic fixture scaffold", claims=[claim], expected_bases=self.bases(claim))

    def test_transitive_support_propagates_upstream_changes_without_touching_independent_claim(self):
        a, b, c, d = [self.branch(name) for name in "ABCD"]
        local = self.reader._local_evidence(self.store.events(), a)[0]
        self.assertEqual(self.bases(a)[a], digest(canonical(local)))
        independent_basis = self.bases(d)[d]
        ab, bc = self.link(a, b), self.link(b, c)
        context = resolve_context(self.store.events(), c)
        self.assertEqual(context.claim_ids, (a, b, c))
        self.assertEqual(context.link_ids, (ab, bc))
        self.review(b)
        historical_review = self.review(c)
        paper = self.paper(c)
        prior = self.bases(a, b, c)
        self.attempt(a, status="failed")
        current = self.bases(a, b, c)
        self.assertTrue(all(prior[id] != current[id] for id in (a, b, c)))
        self.assertEqual(self.bases(d)[d], independent_basis)
        self.assertTrue(self.reader.gate(c)["passed"])
        self.assertEqual(self.reader.next_action(c)["action"], "scientific_review")
        with self.assertRaisesRegex(GateError, "no longer current"):
            self.builder.materialize(paper)
        upstream = self.bases(a)[a]
        self.attempt(c, status="failed")
        self.assertEqual(self.bases(a)[a], upstream)
        graph = ResearchGraph.from_store(self.store)
        self.assertEqual(graph.node(historical_review).kind, NodeKind.REVIEW)
        self.assertEqual(graph.node(paper).kind, NodeKind.PAPER)

    def test_contradiction_is_symmetric_context_without_automatic_failure_or_approval(self):
        a, b = self.branch("A"), self.branch("B")
        self.review(a)
        self.review(b)
        before = self.bases(a, b)
        link = self.link(a, b, "contradicts")
        for claim in (a, b):
            gate = self.reader.gate(claim)
            self.assertTrue(gate["passed"], gate["failures"])
            self.assertEqual(gate["scientific_validity"], "not_assessed")
            self.assertEqual(set(gate["context_claims"]), {a, b})
            self.assertNotEqual(gate["basis_hash"], before[claim])
            self.assertEqual(self.reader.next_action(claim)["action"], "scientific_review")
        with self.assertRaisesRegex(GateError, "review_with_links"):
            self.kernel("fixture-other-reviewer", "reviewer").review(a, verdict="reject",
                rationale="Fixture rejection", actions=["Fixture control"], expected_basis=self.bases(a)[a])
        self.review(a)
        self.assertEqual(self.reader.next_action(a)["action"], "paper_candidate")
        self.assertEqual(self.reader.next_action(b)["action"], "scientific_review")
        self.assertEqual(self.event(a)["payload"]["outcome"], "inconclusive")
        self.assertEqual(self.event(link)["payload"]["relation"], "contradicts")

    def test_link_admission_rejects_stale_bases_and_concurrent_event_append(self):
        a, b = self.branch("A"), self.branch("B")
        old = self.bases(a, b)
        self.attempt(a, status="failed")
        with self.assertRaisesRegex(GateError, "stale claim link"):
            self.linker.link_claims(source=a, target=b, relation="supports", rationale="Fixture", expected_bases=old)
        append = self.store.append
        def concurrent_append(**kwargs):
            if kwargs["kind"] == "claim_link":
                append(id="fixture-concurrent-event", kind="hypothesis", actor="fixture-concurrent-planner",
                       role="planner", payload=dict(statement="Fixture", prediction="Fixture",
                            falsifier="Fixture", scope=self.scope), expected_revision=len(self.store.events()))
            return append(**kwargs)
        with patch.object(self.store, "append", side_effect=concurrent_append):
            with self.assertRaises(ConflictError):
                self.link(a, b)
        self.assertFalse(any(event["kind"] == "claim_link" for event in self.store.events()))
        self.assertEqual(self.event("fixture-concurrent-event")["kind"], "hypothesis")

    def test_cycles_scope_mismatch_and_older_superseding_source_are_rejected(self):
        a, b, c = [self.branch(name) for name in "ABC"]
        other = self.branch("outside", scope={"population": "different fixture scope"})
        self.link(a, b)
        self.link(b, c, "limits")
        for source, target, relation, message in (
            (c, a, "supports", "cycle"), (a, other, "supports", "scope"),
            (a, b, "supersedes", "newer claim"), (a, b, "supports", "duplicate")):
            before = self.store.events()
            with self.subTest(relation=relation, message=message), self.assertRaisesRegex(ClaimContextError, message):
                self.link(source, target, relation)
            self.assertEqual(self.store.events(), before)
        replacement = self.link(c, a, "supersedes")
        self.assertIn(replacement, resolve_context(self.store.events(), c).link_ids)

    def test_assessments_require_exact_links_compatible_verdict_and_context_evidence(self):
        a, b, outside = self.branch("A"), self.branch("B"), self.branch("outside")
        link = self.link(a, b, "contradicts")
        valid = self.assessments(b)
        invalid = [{}, dict(valid, missing=deepcopy(valid[link]))]
        for changes in (dict(judgment="unresolved"), dict(disposition="requires_claim_revision"),
                        dict(disposition="needs_evidence"), dict(evidence=[]), dict(evidence=["missing"]),
                        dict(evidence=[outside]), dict(evidence=[a, a]), dict(extra="unplanned")):
            case = deepcopy(valid)
            case[link].update(changes)
            invalid.append(case)
        before = self.store.events()
        for index, assessment in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(GateError):
                self.review(b, assessments=assessment)
            self.assertEqual(self.store.events(), before)
        unresolved = self.assessments(b, judgment="unresolved", disposition="needs_evidence")
        self.review(b, verdict="request_changes", assessments=unresolved)
        self.assertEqual(self.reader.next_action(b)["action"], "replan")

    def test_missing_and_failed_source_replication_cannot_become_valid_through_a_link(self):
        a, b = self.branch("unreplicated", replicate=False), self.branch("complete")
        self.link(a, b)
        for after_failed_replica in (False, True):
            if after_failed_replica:
                self.attempt(a, replica_of=self.branches[a]["primary"], status="failed")
            gate = self.reader.gate(b)
            self.assertTrue(gate["passed"], gate["failures"])
            related = next(item for item in gate["related_claims"] if item["claim"] == a)
            self.assertFalse(related["historical_gate"]["passed"])
            self.assertFalse(related["current_gate"]["passed"])
            self.assertTrue(any("reanalysis missing" in failure for failure in related["historical_gate"]["failures"]))
            with self.assertRaisesRegex(GateError, "mechanically unqualified source"):
                self.review(b)
            # An explicit fixture rejection of the proposed support is allowed;
            # it never promotes the unreplicated source claim.
            self.review(b, assessments=self.assessments(b, judgment="rejected"))
            self.assertEqual(self.reader.next_action(b)["action"], "paper_candidate")
            self.assertFalse(self.reader.gate(a)["passed"])
            self.assertEqual(self.reader.next_action(a)["action"], "repair_evidence")

    def test_linked_failed_attempt_artifacts_remain_required_after_its_historical_claim(self):
        a, b = self.branch("A"), self.branch("B")
        self.link(a, b)
        _, failed = self.attempt(a, status="failed")
        self.assertTrue(self.reader.gate(b)["passed"])
        failed_log = self.event(failed)["payload"]["outputs"]["log"]
        (self.store.blobs / failed_log).unlink()
        gate = self.reader.gate(b)
        self.assertFalse(gate["passed"])
        self.assertTrue(any("missing artifact" in failure for failure in gate["failures"]))
        with self.assertRaisesRegex(GateError, "mechanical gate failed"):
            self.review(b)
        with self.assertRaises(IntegrityError):
            ResearchGraph.from_store(self.store)

    def test_all_context_contributors_and_relation_author_cannot_review(self):
        a, b = self.branch("A"), self.branch("B")
        self.link(a, b)
        actors = {self.linker.actor.id, *self.branches[a]["authors"].values(),
                  *self.branches[b]["authors"].values()}
        before = self.store.events()
        for actor in actors:
            with self.subTest(actor=actor), self.assertRaisesRegex(GateError, "independent of contributors"):
                self.review(b, actor=actor)
        self.assertEqual(self.store.events(), before)
        self.review(b)
        self.assertEqual(self.reader.next_action(b)["action"], "paper_candidate")

    def test_prior_negative_veto_survives_changed_basis_and_another_reviewer_approval(self):
        a, b = self.branch("A"), self.branch("B")
        veto = self.review(b, actor="fixture-R1", verdict="request_changes")
        self.link(a, b)
        self.assertEqual(self.reader.next_action(b)["action"], "replan")
        self.review(b, actor="fixture-R2")
        self.assertEqual(self.reader.next_action(b)["action"], "replan")
        self.review(b, actor="fixture-R1")
        self.assertEqual(self.reader.next_action(b)["action"], "paper_candidate")
        self.assertEqual(self.event(veto)["payload"]["verdict"], "request_changes")

    def test_open_veto_owner_cannot_author_a_link_that_would_prevent_its_own_review(self):
        a, b = self.branch("A"), self.branch("B")
        self.review(b, actor=self.linker.actor.id, verdict="reject")
        before = self.store.events()
        with self.assertRaisesRegex(GateError, "open review veto owner"):
            self.link(a, b)
        self.assertEqual(self.store.events(), before)
        self.link(a, b, actor=self.kernel("fixture-independent-link-author", "analyst"))
        self.review(b, actor=self.linker.actor.id)
        self.assertEqual(self.reader.next_action(b)["action"], "paper_candidate")

    def test_foreign_negative_requires_acknowledgement_without_closing_its_original_veto(self):
        a, b = self.branch("A"), self.branch("B")
        link = self.link(a, b)
        self.review(b, actor="fixture-B-reviewer")
        original_basis = self.bases(b)[b]
        negative = self.review(a, actor="fixture-A-reviewer", verdict="request_changes")
        self.assertNotEqual(self.bases(b)[b], original_basis)
        self.assertEqual(self.reader.next_action(b)["action"], "scientific_review")
        with self.assertRaisesRegex(GateError, "acknowledge every open review"):
            self.review(b, actor="fixture-B-reviewer")
        assessments = self.assessments(b)
        assessments[link]["evidence"].append(negative)
        assessments[link]["rationale"] = "Fixture: acknowledge unresolved upstream concern; bounded target assessed separately"
        self.review(b, actor="fixture-B-reviewer", assessments=assessments)
        self.assertEqual(self.reader.next_action(b)["action"], "paper_candidate")
        self.assertEqual(self.reader.next_action(a)["action"], "replan")
        paper = self.event(self.paper(b))["payload"]
        manuscript = self.store.read(paper["manuscript"]).decode()
        self.assertIn(negative, manuscript)
        self.assertIn("open related finding", manuscript)
        self.assertIn("Fixture control remains required", manuscript)
        self.review(a, actor="fixture-A-reviewer")
        after_closure = self.bases(b)[b]
        self.assertNotEqual(after_closure, original_basis)
        self.assertEqual(self.reader.next_action(b)["action"], "scientific_review")
        self.review(b, actor="fixture-B-reviewer")
        self.review(a, actor="fixture-A-reviewer")  # Repeated positive review must not cause a refresh loop.
        self.assertEqual(self.bases(b)[b], after_closure)
        self.assertEqual(self.reader.next_action(b)["action"], "paper_candidate")
        graph = ResearchGraph.from_store(self.store)
        self.assertTrue(any(edge.source == negative and edge.relation == Relation.CONTEXT_FINDING
                            for edge in graph.edges))

    def test_symmetric_review_closures_converge_without_mutual_approval_loop(self):
        a, b = self.branch("A"), self.branch("B")
        link = self.link(a, b, "contradicts")
        self.review(a, actor="fixture-A-reviewer", verdict="request_changes")
        negative_b = self.review(b, actor="fixture-B-reviewer", verdict="request_changes")
        assessments = self.assessments(a)
        assessments[link]["evidence"].append(negative_b)
        self.review(a, actor="fixture-A-reviewer", assessments=assessments)
        self.review(b, actor="fixture-B-reviewer")
        self.assertEqual(self.reader.next_action(a)["action"], "scientific_review")
        self.review(a, actor="fixture-A-reviewer")
        bases = self.bases(a, b)
        for claim, actor in ((a, "fixture-A-reviewer"), (b, "fixture-B-reviewer")):
            self.review(claim, actor=actor)
            self.assertEqual(self.reader.next_action(claim)["action"], "paper_candidate")
        self.assertEqual(self.bases(a, b), bases)
        ResearchGraph.from_store(self.store)

    def test_independent_link_author_cannot_remove_a_veto_owners_review_eligibility(self):
        a, b = self.branch("A"), self.branch("B")
        owner = self.branches[a]["authors"]["executor"]
        self.review(b, actor=owner, verdict="request_changes")
        before = self.store.events()
        with self.assertRaisesRegex(GateError, "open review veto owner"):
            self.link(a, b)
        self.assertEqual(self.store.events(), before)
        self.assertEqual(self.reader.next_action(b)["action"], "replan")

    def test_accepted_supersession_blocks_old_anchor_without_approving_replacement(self):
        old, new = self.branch("old"), self.branch("new")
        link = self.link(new, old, "supersedes")
        accepted = self.review(old, actor="fixture-old-reviewer")
        self.assertEqual(self.reader.next_action(old)["action"], "superseded")
        self.assertEqual(self.reader.next_action(old)["replacement"], new)
        with self.assertRaisesRegex(GateError, "not eligible"):
            self.paper(old)
        self.assertEqual(self.reader.next_action(new)["action"], "scientific_review")
        self.review(new, actor="fixture-new-reviewer")
        self.assertEqual(self.reader.next_action(new)["action"], "paper_candidate")
        self.review(old, actor="fixture-old-reviewer", assessments=self.assessments(old, judgment="rejected"))
        self.assertEqual(self.reader.next_action(old)["action"], "paper_candidate")
        self.assertEqual(self.event(accepted)["payload"]["link_assessments"][link]["judgment"], "accepted")

    def test_same_protocol_replacement_keeps_historical_source_readiness_and_current_artifacts(self):
        old = self.branch("shared-protocol")
        record = self.branches[old]
        second, _ = self.attempt(old)
        second_replica, _ = self.attempt(old, replica_of=second)
        new = self.kernel(record["authors"]["analyst"], "analyst").claim(
            protocol=record["protocol"], statement="Expanded synthetic evidence; no population inference",
            scope=self.scope, evidence=[*record["evidence"], second, second_replica],
            limitations=["Fixture expansion only"], outcome="inconclusive")
        self.assertFalse(self.reader.gate(old)["passed"])
        self.assertTrue(self.reader.gate(new)["passed"])
        self.link(old, new)
        self.link(new, old, "supersedes")
        gate = self.reader.gate(new)
        self.assertTrue(gate["passed"], gate["failures"])
        self.review(new)
        self.assertEqual(self.reader.next_action(new)["action"], "paper_candidate")
        self.assertNotEqual(self.reader.next_action(old)["action"], "paper_candidate")
        self.paper(new)
        _, failed = self.attempt(old, status="failed")
        self.assertTrue(self.reader.gate(new)["passed"])
        (self.store.blobs / self.event(failed)["payload"]["outputs"]["log"]).unlink()
        self.assertFalse(self.reader.gate(new)["passed"])

    def test_accepted_supersession_requires_current_replacement_readiness(self):
        old, new = self.branch("old"), self.branch("new")
        self.link(new, old, "supersedes")
        self.attempt(new)  # New primary result has neither reanalysis nor a new claim's citations.
        gate = self.reader.gate(old)
        self.assertTrue(gate["passed"])
        related = next(item for item in gate["related_claims"] if item["claim"] == new)
        self.assertTrue(related["historical_gate"]["passed"])
        self.assertFalse(related["current_gate"]["passed"])
        with self.assertRaisesRegex(GateError, "mechanically unqualified source"):
            self.review(old)
        self.review(old, assessments=self.assessments(old, judgment="rejected"))
        self.assertEqual(self.reader.next_action(old)["action"], "paper_candidate")
        self.assertEqual(self.reader.next_action(new)["action"], "repair_evidence")

    def test_paper_keeps_accepted_compatible_contradiction_as_unapproved_context(self):
        a, b = self.branch("competing"), self.branch("selected")
        link = self.link(a, b, "contradicts")
        self.review(b)
        paper = self.paper(b)
        payload = self.event(paper)["payload"]
        manuscript = self.store.read(payload["manuscript"]).decode()
        bundle = json.loads(self.store.read(payload["bundle"]))
        self.assertIn(self.event(a)["payload"]["statement"], manuscript)
        self.assertIn(link, manuscript)
        self.assertEqual(bundle["selected_claims"], [b])
        self.assertEqual(set(bundle["selected_context"]["claims"]), {a, b})
        self.assertEqual(bundle["selected_context"]["links"], [link])
        self.assertEqual(self.reader.next_action(a)["action"], "scientific_review")

    def test_command_link_and_link_review_replay_the_original_events_after_reopen(self):
        a, b = self.branch("A"), self.branch("B")
        service = CommandService(self.store)
        def envelope(id, action, payload, actor, role):
            return dict(context=dict(command_id=id, expected_revision=len(self.store.events()),
                actor=actor, role=role, study_id="fixture-study", correlation_id="fixture-cycle", causation_id=None),
                request=dict(version=1, action=action, payload=payload))
        link_request = envelope("fixture-link-command", "kernel.link_claims",
            dict(source=a, target=b, relation="supports", rationale="Fixture relation", expected_bases=self.bases(a, b)),
            self.linker.actor.id, "planner")
        link = service.execute(link_request)
        self.assertEqual(service.execute(link_request), link)
        review_request = envelope("fixture-review-command", "kernel.review_with_links",
            dict(claim=b, verdict="approve", rationale="Fixture opinion, no scientific review",
                 actions=[], expected_basis=self.bases(b)[b], link_assessments=self.assessments(b)),
            "fixture-command-reviewer", "reviewer")
        review = service.execute(review_request)
        self.attempt(a, status="failed")
        before = self.store.events()
        with Store(self.root) as reopened:
            replay = CommandService(reopened)
            self.assertEqual(replay.execute(link_request), link)
            self.assertEqual(replay.execute(review_request), review)
            self.assertEqual(reopened.events(), before)
            self.assertEqual(len(reopened.receipts()), 2)
        self.assertEqual(self.reader.next_action(b)["action"], "scientific_review")

    def test_graph_validates_historical_link_bases_and_projects_review_context_references(self):
        a, b = self.branch("A"), self.branch("B")
        link = self.link(a, b)
        review = self.review(b)
        paper = self.paper(b)
        self.attempt(a, status="failed")
        graph = ResearchGraph.from_store(self.store)
        relations = {(edge.source, edge.target, edge.relation) for edge in graph.edges}
        self.assertIn((a, link, Relation.LINK_SOURCE), relations)
        self.assertIn((b, link, Relation.LINK_TARGET), relations)
        self.assertIn((link, review, Relation.REVIEW_LINK), relations)
        self.assertIn((a, review, Relation.REVIEW_CONTEXT), relations)
        self.assertIn((a, review, Relation.ASSESSMENT_EVIDENCE), relations)
        self.assertEqual(graph.node(paper).kind, NodeKind.PAPER)
        forged = deepcopy(self.event(link)["payload"])
        forged.update(relation="limits", source_basis="0" * 64)
        self.store.append(id="fixture-forged-basis-link", kind="claim_link", actor=self.linker.actor.id,
            role="planner", payload=forged, expected_revision=len(self.store.events()))
        with self.assertRaisesRegex(GraphIntegrityError, "basis"):
            ResearchGraph.from_store(self.store)

    def test_cli_link_and_unresolved_review_persist_replay_and_export_graph(self):
        a, b = self.branch("cli-A"), self.branch("cli-B")
        project = Path(__file__).resolve().parents[1]

        def cli(*args):
            process = subprocess.run([sys.executable, "-m", "episteme", *map(str, args)],
                cwd=project, env=dict(os.environ, PYTHONPATH=str(project / "src")),
                capture_output=True, text=True, encoding="utf-8", timeout=40, check=False)
            self.assertEqual(process.returncode, 0, process.stderr or process.stdout)
            return json.loads(process.stdout)

        def submit(id, action, role, payload):
            envelope = dict(context=dict(command_id=id, expected_revision=len(self.store.events()),
                actor=f"fixture-cli-{role}", role=role, study_id="fixture-study",
                correlation_id="fixture-cli-cycle", causation_id=None),
                request=dict(version=1, action=action, payload=payload))
            path = self.root / f"{id}.json"
            path.write_text(json.dumps(envelope), encoding="utf-8")
            first = cli("command", "--root", self.root, "--input", path)
            self.assertEqual(cli("command", "--root", self.root, "--input", path), first)
            return first["result"]

        link = submit("fixture-cli-link", "kernel.link_claims", "planner", dict(
            source=a, target=b, relation="contradicts", rationale="Unverified fixture conflict",
            expected_bases=self.bases(a, b)))
        review = submit("fixture-cli-review", "kernel.review_with_links", "reviewer", dict(
            claim=b, verdict="request_changes", rationale="Fixture conflict needs evidence",
            actions=["Fixture control"], expected_basis=self.bases(b)[b],
            link_assessments=self.assessments(b, judgment="unresolved", disposition="needs_evidence")))
        self.assertEqual(self.reader.next_action(b)["action"], "replan")
        self.assertEqual(self.event(review)["payload"]["link_assessments"][link]["judgment"], "unresolved")
        self.assertEqual(len(cli("receipts", "--root", self.root)["receipts"]), 2)
        graph = cli("graph", "--root", self.root)
        self.assertEqual(graph["revision"], len(self.store.events()))
        self.assertEqual(graph["node_kinds"]["claim_link"], 1)
        self.assertEqual(graph["node_kinds"]["review"], 1)


if __name__ == "__main__":
    unittest.main()
