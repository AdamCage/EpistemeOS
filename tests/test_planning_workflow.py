"""Planning lineage integration with explicit synthetic outputs and review opinions.

No experiment is executed and no scientific approval is manufactured for demos.
The fixture review only exercises admission, frozen context and contributor rules.
"""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from episteme.commands import CommandService
from episteme.graph import GraphIntegrityError, ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.planning import Planning
from episteme.recovery import backup, restore
from episteme.reporting import PaperBuilder, review_bundle
from episteme.search import COMPONENTS, Search
from episteme.store import ConflictError, Store


PROJECT = Path(__file__).resolve().parents[1]


class PlanningWorkflowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="episteme-planning-workflow-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root / "source")
        self.addCleanup(self.store.close)
        self.scope = {"population": "synthetic planning fixture"}
        self.study = "planning-fixture-study"
        self.hypotheses = [self.kernel(f"fixture-hypothesis-{index}", "planner").hypothesis(
            f"Synthetic alternative {index}", "Fixture prediction", "Fixture falsifier", self.scope)
            for index in range(3)]
        self.blobs = {field: self.store.put(f"Planning fixture {field}".encode()) for field in
                      ("implementation", "reimplementation", "environment", "data")}

    def kernel(self, actor="fixture-protocol-author", role="planner"):
        return Kernel(self.store, Actor(actor, role))

    def event(self, id):
        return next(event for event in self.store.events() if event["id"] == id)

    def question_args(self, **changes):
        return dict(dict(study_id=self.study, statement="Which fixture alternatives remain distinguishable?",
                         objective="Exercise immutable planning lineage", scope=self.scope,
                         constraints=["Offline synthetic fixture only"],
                         stopping_criteria=["Stop after the fixed fixture run schedule"]), **changes)

    def question(self, *, actor="fixture-question-author", **changes):
        return Planning(self.store, Actor(actor, "planner")).question(**self.question_args(**changes))

    def explanation_set(self, question, *, actor="fixture-set-author", hypotheses=None, **changes):
        return Planning(self.store, Actor(actor, "planner")).explanation_set(
            question=question, hypotheses=self.hypotheses[:2] if hypotheses is None else hypotheses,
            comparison_plan="Compare every listed explanation using the fixed synthetic control",
            **changes)

    def protocol_args(self, **changes):
        return dict(dict(design="Synthetic planning integration", metric="mean",
                         analysis_plan="Read fixture means; no population inference",
                         stopping_rule="Fixed fixture schedule", seeds=[7], run_limit=20,
                         implementation=self.blobs["implementation"], environment=self.blobs["environment"],
                         data=self.blobs["data"], replication_tolerance=0.0), **changes)

    def protocol(self, explanation_set, **changes):
        return self.kernel().preregister_for_set(
            explanation_set=explanation_set, **self.protocol_args(**changes))

    def envelope(self, action, payload, *, command_id="planning-command", study_id=None,
                 actor="fixture-command-planner", role="planner"):
        return dict(context=dict(command_id=command_id, expected_revision=len(self.store.events()),
                                 actor=actor, role=role, study_id=self.study if study_id is None else study_id,
                                 correlation_id="planning-fixture-cycle", causation_id=None),
                    request=dict(version=1, action=action, payload=payload))

    def claim(self, protocol):
        evidence = []
        for replica in (False, True):
            role = "replicator" if replica else "executor"
            actor = self.kernel(f"fixture-{role}", role)
            run = actor.start_run(protocol, seed=7,
                implementation=self.blobs["reimplementation" if replica else "implementation"],
                environment=self.blobs["environment"], command=["not-executed-fixture"],
                replicate_of=evidence[0] if replica else None)
            actor.finish_run(run, status="completed", reason="Explicit test output fixture", outputs=dict(
                log=self.store.put(f"Synthetic {role} {run}".encode()), raw_data=self.blobs["data"],
                metrics=self.store.put_json({"mean": 0.0})))
            evidence.append(run)
        return self.kernel("fixture-analyst", "analyst").claim(protocol=protocol,
            statement="Synthetic means agree; no population inference", scope=self.scope,
            evidence=evidence, limitations=["Test outputs and caller-declared roles only"], outcome="inconclusive")

    def review(self, claim, actor="fixture-independent-reviewer"):
        return self.kernel(actor, "reviewer").review(claim, verdict="approve",
            rationale="Explicit test opinion only, not an actual scientific assessment", actions=[],
            expected_basis=self.kernel().gate(claim)["basis_hash"])

    def test_question_revisions_are_immutable_and_reject_stale_parent(self):
        original = self.question()
        original_event = deepcopy(self.event(original))
        revised = self.question(parent=original, revision_reason="Clarify the fixture objective",
                                objective="Exercise frozen context after a wording amendment")
        self.assertEqual(self.event(original), original_event)
        self.assertEqual(self.event(revised)["payload"]["parent"], original)
        before = self.store.export()
        for changes in (dict(parent=original, revision_reason="Competing stale revision"),
                        dict(parent=revised), dict(parent=revised, study_id="another-study",
                                                   revision_reason="Attempt a study switch")):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.question(**changes)
            self.assertEqual(self.store.export(), before)
        with self.assertRaises(ValueError):
            self.explanation_set(original)
        self.explanation_set(revised)

    def test_explanation_set_removals_are_exact_and_require_current_same_lineage_heads(self):
        question = self.question()
        original = self.explanation_set(question, hypotheses=self.hypotheses)
        before = self.store.export()
        for reasons in (None, {}, {self.hypotheses[0]: "Wrong excluded alternative"},
                        {self.hypotheses[2]: "Removed", self.hypotheses[0]: "Extra reason"}):
            with self.subTest(reasons=reasons), self.assertRaises(ValueError):
                self.explanation_set(question, parent=original, revision_reason="Remove redundant fixture",
                                     excluded_reasons=reasons)
            self.assertEqual(self.store.export(), before)
        revised = self.explanation_set(question, parent=original, revision_reason="Remove redundant fixture",
            excluded_reasons={self.hypotheses[2]: "Retained in history as a redundant fixture alternative"})
        self.assertEqual(self.event(original)["payload"]["hypotheses"], self.hypotheses)
        self.assertEqual(self.event(revised)["payload"]["hypotheses"], self.hypotheses[:2])
        other_question = self.question(statement="Separate question in the same study")
        for selected_question, parent in ((question, original), (other_question, revised)):
            with self.subTest(parent=parent), self.assertRaises(ValueError):
                self.explanation_set(selected_question, parent=parent, revision_reason="Invalid ancestry")

    def test_set_requires_competing_existing_hypotheses_with_exact_question_scope(self):
        question = self.question()
        foreign = self.kernel().hypothesis("Foreign", "Fixture", "Fixture", {"population": "other"})
        before = self.store.export()
        for hypotheses in ([self.hypotheses[0]], [self.hypotheses[0]] * 2,
                           [self.hypotheses[0], "missing-hypothesis"], [self.hypotheses[0], foreign]):
            with self.subTest(hypotheses=hypotheses), self.assertRaises(ValueError):
                self.explanation_set(question, hypotheses=hypotheses)
            self.assertEqual(self.store.export(), before)

    def test_bound_protocol_derives_exact_hypotheses_scope_and_frozen_hashes(self):
        question = self.question()
        explanation_set = self.explanation_set(question)
        protocol = self.protocol(explanation_set)
        payload = self.event(protocol)["payload"]
        self.assertEqual(payload["hypotheses"], self.hypotheses[:2])
        self.assertEqual(payload["scope"], self.scope)
        self.assertEqual(payload["planning"], dict(schema_version=1, study_id=self.study,
            question=question, question_hash=self.event(question)["hash"],
            explanation_set=explanation_set, explanation_set_hash=self.event(explanation_set)["hash"]))
        graph = ResearchGraph.from_store(self.store)
        self.assertEqual(graph.node(question).kind.value, "research_question")
        self.assertEqual(graph.node(explanation_set).kind.value, "explanation_set")
        edges = graph.to_dict()["edges"]
        self.assertTrue(any(edge["source"] == explanation_set and edge["target"] == protocol for edge in edges))
        with self.assertRaises(TypeError):
            self.kernel().preregister_for_set(explanation_set=explanation_set,
                hypotheses=self.hypotheses, **self.protocol_args())
        before = self.store.export()
        for parent in ("", "missing", question):
            with self.subTest(parent=parent), self.assertRaises(ValueError):
                self.protocol(explanation_set, parent=parent)
            self.assertEqual(self.store.export(), before)

    def test_protocol_amendments_cannot_downgrade_or_cross_question_lineage(self):
        question = self.question()
        explanation_set = self.explanation_set(question)
        legacy = self.kernel().preregister(hypotheses=self.hypotheses[:2], scope=self.scope,
                                          **self.protocol_args())
        planned = self.protocol(explanation_set, parent=legacy)
        question2 = self.question(parent=question, revision_reason="New fixture wording")
        set2 = self.explanation_set(question2, parent=explanation_set, revision_reason="Follow question revision")
        amended = self.protocol(set2, parent=planned)
        self.assertEqual(self.event(amended)["payload"]["parent"], planned)
        with self.assertRaises(ValueError):
            self.kernel().preregister(hypotheses=self.hypotheses[:2], scope=self.scope,
                                     **self.protocol_args(parent=amended))
        for study in (self.study, "separate-study"):
            other_q = self.question(study_id=study, statement="Unrelated lineage")
            other_set = self.explanation_set(other_q)
            with self.subTest(study=study), self.assertRaises(ValueError):
                self.protocol(other_set, parent=amended)
        with self.assertRaises(ValueError):
            self.protocol(explanation_set)

    def test_command_replay_survives_new_heads_but_new_stale_commands_and_study_switches_fail(self):
        service = CommandService(self.store)
        question_envelope = self.envelope("planning.question", self.question_args(), command_id="question-command")
        question = service.execute(question_envelope)
        set_envelope = self.envelope("planning.explanation_set", dict(question=question,
            hypotheses=self.hypotheses[:2], comparison_plan="Synthetic comparison"), command_id="set-command")
        explanation_set = service.execute(set_envelope)
        protocol_envelope = self.envelope("kernel.preregister_for_set",
            dict(explanation_set=explanation_set, **self.protocol_args()), command_id="protocol-command")
        protocol = service.execute(protocol_envelope)
        question2 = self.question(parent=question, revision_reason="A later immutable question")
        self.explanation_set(question2, parent=explanation_set, revision_reason="Follow later question")
        before = self.store.export(), self.store.export_receipts()
        for envelope, result in ((question_envelope, question), (set_envelope, explanation_set),
                                 (protocol_envelope, protocol)):
            self.assertEqual(service.execute(envelope), result)
            changed = deepcopy(envelope)
            changed["context"]["study_id"] = "cross-study-replay"
            with self.assertRaises(ConflictError):
                service.execute(changed)
        with self.assertRaises(ValueError):
            service.execute(self.envelope("kernel.preregister_for_set",
                protocol_envelope["request"]["payload"], command_id="new-command-with-stale-set"))
        self.assertEqual((self.store.export(), self.store.export_receipts()), before)

    def test_command_study_mismatch_rejects_planning_and_bound_run_without_side_effects(self):
        question = self.question()
        explanation_set = self.explanation_set(question)
        protocol = self.protocol(explanation_set)
        cases = [
            ("planning.question", self.question_args(), "planner"),
            ("planning.explanation_set", dict(question=question, hypotheses=self.hypotheses[:2],
                                              comparison_plan="Fixture"), "planner"),
            ("kernel.preregister_for_set", dict(explanation_set=explanation_set, **self.protocol_args()), "planner"),
            ("kernel.start_run", dict(protocol=protocol, seed=7, implementation=self.blobs["implementation"],
                environment=self.blobs["environment"], command=["not-executed-fixture"]), "executor"),
        ]
        before = self.store.export(), self.store.export_receipts()
        for index, (action, payload, role) in enumerate(cases):
            with self.subTest(action=action), self.assertRaises(ValueError):
                CommandService(self.store).execute(self.envelope(action, payload, role=role,
                    command_id=f"wrong-study-{index}", study_id="wrong-study"))
            self.assertEqual((self.store.export(), self.store.export_receipts()), before)

    def test_search_selection_resolves_study_through_tree_nodes_before_reserving_budget(self):
        protocol = self.protocol(self.explanation_set(self.question()))
        search = Search(self.store, Actor("fixture-search-planner", "planner"))
        tree = search.register_tree(weights={key: 1.0 for key in COMPONENTS}, cost_weight=0.1,
            budget=10.0, cost_unit="fixture", max_nodes=10, max_depth=3, max_width=3,
            max_selections=5, max_retries=1)
        node = search.add_node(tree, protocol=protocol, action="discriminate",
            components={key: 0.5 for key in COMPONENTS}, estimated_cost=1.0, rationale="Fixture priority")
        before = self.store.export(), self.store.export_receipts()
        with self.assertRaises(ValueError):
            CommandService(self.store).execute(self.envelope("search.select_next", dict(tree=tree),
                command_id="wrong-study-selection", study_id="wrong-study"))
        self.assertEqual((self.store.export(), self.store.export_receipts()), before)
        selection = CommandService(self.store).execute(self.envelope("search.select_next", dict(tree=tree),
            command_id="matching-study-selection"))
        self.assertEqual((selection["node"], selection["protocol"]), (node, protocol))

    def test_future_planning_revision_preserves_existing_review_basis_and_paper(self):
        question = self.question()
        explanation_set = self.explanation_set(question)
        protocol = self.protocol(explanation_set)
        claim = self.claim(protocol)
        basis = self.kernel().gate(claim)["basis_hash"]
        review = self.review(claim)
        builder = PaperBuilder(self.store, Actor("fixture-writer", "writer"))
        paper = builder.build(title="Synthetic planning fixture", claims=[claim], expected_bases={claim: basis})
        question2 = self.question(parent=question, revision_reason="Future research question refinement")
        set2 = self.explanation_set(question2, parent=explanation_set, revision_reason="Future comparison plan")
        self.assertEqual(self.kernel().gate(claim)["basis_hash"], basis)
        self.assertEqual(self.kernel().next_action(claim)["action"], "paper_candidate")

        self.assertTrue(Path(builder.materialize(paper)["bundle"]).is_file())
        local, _ = self.kernel()._local_evidence(self.store.events(), claim)
        local_ids = {event["id"] for event in local}
        self.assertTrue({question, explanation_set, *self.hypotheses[:2]} <= local_ids)
        self.assertTrue({question2, set2, review}.isdisjoint(local_ids))
        self.assertEqual(ResearchGraph.from_store(self.store).node(review).kind.value, "review")
        with self.assertRaises(ValueError):
            self.protocol(explanation_set)

    def test_review_excludes_question_set_ancestors_and_removed_hypothesis_authors(self):
        question = self.question(actor="fixture-old-question-author")
        original_set = self.explanation_set(question, actor="fixture-old-set-author", hypotheses=self.hypotheses)
        revised_q = self.question(actor="fixture-new-question-author", parent=question,
                                  revision_reason="Question wording refinement")
        revised_set = self.explanation_set(revised_q, actor="fixture-new-set-author", parent=original_set,
            revision_reason="Remove a redundant alternative",
            excluded_reasons={self.hypotheses[2]: "Recorded redundancy, not erasure of the original alternative"})
        claim = self.claim(self.protocol(revised_set))
        local, _ = self.kernel()._local_evidence(self.store.events(), claim)
        self.assertTrue({question, revised_q, original_set, revised_set, *self.hypotheses}
                        <= {event["id"] for event in local})
        before = self.store.export()
        for actor in ("fixture-old-question-author", "fixture-old-set-author", "fixture-new-question-author",
                      "fixture-new-set-author", "fixture-hypothesis-0", "fixture-hypothesis-2"):
            with self.subTest(actor=actor), self.assertRaises(ValueError):
                self.review(claim, actor=actor)
            self.assertEqual(self.store.export(), before)
        self.review(claim)
        bundle = review_bundle(self.store, self.store.events())
        self.assertTrue({question, revised_q, original_set, revised_set, *self.hypotheses}
                        <= {event["id"] for event in bundle["events"]})
        self.assertEqual(self.kernel().next_action(claim)["action"], "paper_candidate")
        paper = PaperBuilder(self.store, Actor("fixture-writer", "writer")).build(
            title="Planning lineage fixture", claims=[claim],
            expected_bases={claim: self.kernel().gate(claim)["basis_hash"]})
        manuscript = self.store.read(self.event(paper)["payload"]["manuscript"]).decode()
        self.assertIn(self.event(self.hypotheses[2])["payload"]["statement"], manuscript)
        self.assertIn("Recorded redundancy, not erasure of the original alternative", manuscript)

    def test_graph_rejects_forged_planning_binding_despite_valid_event_hash_chain(self):
        question = self.question()
        explanation_set = self.explanation_set(question)
        protocol = self.protocol(explanation_set)
        snapshot = self.root / "valid-snapshot"
        backup(self.store, snapshot)
        for field, value in (("question_hash", "0" * 64), ("explanation_set_hash", "0" * 64),
                             ("study_id", "forged-study"), ("question", "missing-question")):
            with self.subTest(field=field):
                destination = self.root / f"tampered-{field}"
                restore(snapshot, destination)
                with Store(destination) as tampered:
                    payload = deepcopy(self.event(protocol)["payload"])
                    payload["planning"][field] = value
                    tampered.append(id="forged-bound-protocol", kind="protocol", actor="fixture-forger",
                        role="planner", payload=payload, expected_revision=len(tampered.events()))
                    self.assertEqual(tampered.events()[-1]["id"], "forged-bound-protocol")
                    with self.assertRaises(GraphIntegrityError):
                        ResearchGraph.from_store(tampered)

    def test_cli_and_recovery_preserve_planning_graph_receipts_and_replay(self):
        environment = dict(os.environ, PYTHONPATH=str(PROJECT / "src"))
        def cli(envelope, filename):
            path = self.root / filename
            path.write_text(json.dumps(envelope), encoding="utf-8")
            result = subprocess.run([sys.executable, "-m", "episteme", "command", "--root",
                str(self.store.root), "--input", str(path)], cwd=PROJECT, env=environment,
                capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)["result"]
        question_envelope = self.envelope("planning.question", self.question_args(), command_id="cli-question")
        question = cli(question_envelope, "question-command.json")
        explanation_set = cli(self.envelope("planning.explanation_set", dict(question=question,
            hypotheses=self.hypotheses[:2], comparison_plan="CLI fixture comparison"), command_id="cli-set"),
            "set-command.json")
        protocol = cli(self.envelope("kernel.preregister_for_set",
            dict(explanation_set=explanation_set, **self.protocol_args()), command_id="cli-protocol"),
            "protocol-command.json")
        claim = self.claim(protocol)
        self.review(claim)
        graph = ResearchGraph.from_store(self.store).to_dict()
        history, receipts = self.store.export(), self.store.export_receipts()
        snapshot, destination = self.root / "snapshot", self.root / "restored"
        backup(self.store, snapshot)
        restore(snapshot, destination)
        with Store(destination) as recovered:
            self.assertEqual(recovered.export(), history)
            self.assertEqual(recovered.export_receipts(), receipts)
            self.assertEqual(ResearchGraph.from_store(recovered).to_dict(), graph)
            self.assertEqual(CommandService(recovered).execute(question_envelope), question)
            self.assertEqual(Kernel(recovered, Actor("reader", "planner")).next_action(claim)["action"],
                             "paper_candidate")


if __name__ == "__main__":
    unittest.main()
