"""Synthetic planning fixtures; declarations do not establish scientific validity."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from episteme.planning import (
    Planning, PlanningError, binding_for, planning_context, validate_planning,
)
from episteme.store import ConflictError, Store


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name) / "state"
        self.store = Store(self.root)
        self.planner = Planning(self.store, SimpleNamespace(id="fixture-planner", role="planner"))
        self.scope = {"domain": "synthetic-planning-fixture"}

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def hypothesis(self, name, scope=None):
        self.store.append(id=name, kind="hypothesis", actor="fixture-hypothesis-author",
                          role="planner", payload=dict(statement=name,
                          prediction="Declared fixture prediction", falsifier="Fixture falsifier",
                          scope=self.scope if scope is None else scope),
                          expected_revision=len(self.store.events()))
        return name

    def question(self, **changes):
        payload = dict(study_id="fixture-study", statement="Fixture research question",
                       objective="Compare declared explanations", scope=self.scope,
                       constraints=["Synthetic fixtures only"],
                       stopping_criteria=["Stop after the declared fixture comparison"])
        payload.update(changes)
        return self.planner.question(**payload)

    def pool(self, question, hypotheses, **changes):
        return self.planner.explanation_set(question=question, hypotheses=hypotheses,
                                            comparison_plan="Compare recorded fixture predictions",
                                            **changes)

    def seed(self):
        hypotheses = [self.hypothesis("alternative-a"), self.hypothesis("alternative-b")]
        question = self.question()
        return question, hypotheses, self.pool(question, hypotheses)

    def event(self, id):
        return next(event for event in self.store.events() if event["id"] == id)

    def test_question_and_pool_freeze_prior_hypotheses_and_hashes(self):
        question, hypotheses, pool = self.seed()
        history = self.store.events()
        before = deepcopy(history)
        validate_planning(history)
        binding = binding_for(history, pool)
        self.assertEqual(binding, dict(schema_version=1, study_id="fixture-study",
                         question=question, question_hash=self.event(question)["hash"],
                         explanation_set=pool, explanation_set_hash=self.event(pool)["hash"]))
        self.assertEqual(self.event(pool)["payload"]["hypothesis_hashes"],
                         {id: self.event(id)["hash"] for id in hypotheses})
        self.assertEqual([event["id"] for event in planning_context(history, binding)],
                         [*hypotheses, question, pool])
        self.assertEqual(history, before)
        self.assertEqual(self.store.receipts(), [])

    def test_question_requires_strict_nonempty_declarations(self):
        invalid = [dict(study_id=" "), dict(statement=False), dict(objective=""),
                   dict(scope={}), dict(scope={"domain": 1}), dict(scope={" ": "value"}),
                   dict(constraints=[]), dict(constraints=("text",)),
                   dict(constraints=[True]), dict(stopping_criteria=[]),
                   dict(stopping_criteria=["\t"]), dict(revision_reason="root cannot revise")]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(PlanningError):
                self.question(**changes)
        self.assertEqual(self.store.events(), [])

    def test_planner_role_required_before_reading_or_writing(self):
        reader = Planning(self.store, SimpleNamespace(id="fixture-reviewer", role="reviewer"))
        with patch.object(self.store, "events", side_effect=AssertionError("must reject before read")):
            with self.assertRaisesRegex(PlanningError, "only planner"):
                reader.question(study_id="study", statement="question", objective="objective",
                                scope=self.scope, constraints=["constraint"],
                                stopping_criteria=["stop"])
            with self.assertRaisesRegex(PlanningError, "only planner"):
                reader.explanation_set(question="missing", hypotheses=[], comparison_plan="plan")

    def test_pool_rejects_bad_references_scope_and_duplicate_hypotheses(self):
        question, hypotheses, _ = self.seed()
        mismatch = self.hypothesis("different-scope", {"domain": "other"})
        before = self.store.export()
        invalid = [[], [hypotheses[0]], [hypotheses[0], hypotheses[0]],
                   [hypotheses[0], "missing"], [hypotheses[0], question],
                   [hypotheses[0], mismatch], [hypotheses[0], False], tuple(hypotheses)]
        for candidates in invalid:
            with self.subTest(candidates=candidates), self.assertRaises(PlanningError):
                self.pool(question, candidates)
        for changes in (dict(question="missing"), dict(comparison_plan=" "),
                        dict(excluded_reasons={hypotheses[0]: "not removed"}),
                        dict(revision_reason="not a revision"), dict(excluded_reasons=[])):
            args = dict(question=question, hypotheses=hypotheses, comparison_plan="Fixture plan")
            args.update(changes)
            with self.subTest(changes=changes), self.assertRaises(PlanningError):
                self.planner.explanation_set(**args)
        self.assertEqual(self.store.export(), before)

    def test_question_revision_preserves_ancestry_and_requires_current_parent(self):
        root = self.question()
        original = deepcopy(self.event(root))
        child = self.question(parent=root, revision_reason="Clarify the stopping declaration")
        self.assertEqual(self.event(child)["payload"]["parent_hash"], original["hash"])
        self.assertEqual(self.event(root), original)
        for changes in (dict(parent=root, revision_reason="Stale parent"),
                        dict(parent=child), dict(parent=child, revision_reason=" "),
                        dict(parent=child, revision_reason="New study", study_id="other-study"),
                        dict(parent="missing", revision_reason="Missing parent")):
            with self.subTest(changes=changes), self.assertRaises(PlanningError):
                self.question(**changes)
        self.assertEqual(len(self.store.events()), 2)

    def test_pool_revision_requires_exact_removed_candidate_reasons(self):
        question, old_hypotheses, original = self.seed()
        new = self.hypothesis("alternative-c")
        hypotheses = [old_hypotheses[0], new]
        for reasons in (None, {}, {old_hypotheses[1]: " "},
                        {old_hypotheses[0]: "wrong candidate"},
                        {old_hypotheses[1]: "reason", "extra": "extra reason"}):
            with self.subTest(reasons=reasons), self.assertRaises(PlanningError):
                self.pool(question, hypotheses, parent=original,
                          revision_reason="Replace fixture candidate", excluded_reasons=reasons)
        revised = self.pool(question, hypotheses, parent=original,
                            revision_reason="Replace fixture candidate",
                            excluded_reasons={old_hypotheses[1]: "Retained historically; no longer selected"})
        context = planning_context(self.store.events(), binding_for(self.store.events(), revised))
        self.assertIn(old_hypotheses[1], [event["id"] for event in context])
        self.assertEqual(self.event(original)["payload"]["hypotheses"], old_hypotheses)
        with self.assertRaisesRegex(PlanningError, "current lineage head"):
            self.pool(question, old_hypotheses, parent=original, revision_reason="Stale pool")

    def test_set_revision_can_bind_new_head_of_same_question_lineage(self):
        question, hypotheses, first = self.seed()
        revised_question = self.question(parent=question, revision_reason="Refine declared objective")
        with self.assertRaisesRegex(PlanningError, "current lineage head"):
            self.pool(question, hypotheses)
        with self.assertRaisesRegex(PlanningError, "current lineage head"):
            binding_for(self.store.events(), first)
        second = self.pool(revised_question, hypotheses, parent=first,
                           revision_reason="Bind refined research question")
        context = planning_context(self.store.events(), binding_for(self.store.events(), second))
        self.assertEqual([e["id"] for e in context],
                         [*hypotheses, question, first, revised_question, second])

    def test_revision_cannot_move_to_other_question_lineage_or_study(self):
        question, hypotheses, first = self.seed()
        for replacement in (self.question(), self.question(study_id="other-study")):
            with self.subTest(replacement=replacement), self.assertRaises(PlanningError):
                self.pool(replacement, hypotheses, parent=first,
                          revision_reason="Invalid cross-question amendment")
        self.assertEqual(binding_for(self.store.events(), first)["question"], question)

    def test_changed_question_scope_requires_new_hypotheses_and_retains_old_ones(self):
        question, old_hypotheses, first = self.seed()
        new_scope = {"domain": "different-synthetic-regime"}
        revised_question = self.question(parent=question, revision_reason="Change declared regime",
                                         scope=new_scope)
        with self.assertRaisesRegex(PlanningError, "scope"):
            self.pool(revised_question, old_hypotheses, parent=first,
                      revision_reason="Cannot reuse hypotheses with a different scope")
        new_hypotheses = [self.hypothesis("new-regime-a", new_scope),
                          self.hypothesis("new-regime-b", new_scope)]
        second = self.pool(revised_question, new_hypotheses, parent=first,
                           revision_reason="Compare hypotheses in the new declared regime",
                           excluded_reasons={id: "Original regime retained historically"
                                             for id in old_hypotheses})
        context = planning_context(self.store.events(), binding_for(self.store.events(), second))
        self.assertEqual([event["id"] for event in context if event["kind"] == "hypothesis"],
                         [*old_hypotheses, *new_hypotheses])

    def test_historical_binding_excludes_future_and_unrelated_context(self):
        question, hypotheses, first = self.seed()
        initial_history = self.store.events()
        old_binding = binding_for(initial_history, first)
        old_context = planning_context(initial_history, old_binding)
        new_hypothesis = self.hypothesis("later-alternative")
        other_question = self.question()
        self.pool(other_question, hypotheses)
        q2 = self.question(parent=question, revision_reason="Later revision")
        second = self.pool(q2, [hypotheses[0], new_hypothesis], parent=first,
                           revision_reason="Later comparison",
                           excluded_reasons={hypotheses[1]: "Retained as a prior alternative"})
        q3 = self.question(parent=q2, revision_reason="Even later question")
        self.pool(q3, [hypotheses[0], new_hypothesis], parent=second,
                  revision_reason="Even later pool")
        history = self.store.events()
        self.assertEqual(binding_for(history, first, current=False), old_binding)
        self.assertEqual(planning_context(history, old_binding), old_context)
        context = planning_context(history, binding_for(history, second, current=False))
        self.assertNotIn(q3, [event["id"] for event in context])
        self.assertNotIn(other_question, [event["id"] for event in context])
        self.assertIn(hypotheses[1], [event["id"] for event in context])

    def test_context_retains_hypotheses_from_all_set_ancestors(self):
        question, hypotheses, first = self.seed()
        third = self.hypothesis("third-alternative")
        second = self.pool(question, [hypotheses[0], third], parent=first,
                           revision_reason="First pool amendment",
                           excluded_reasons={hypotheses[1]: "Historical competitor"})
        fourth = self.hypothesis("fourth-alternative")
        final = self.pool(question, [third, fourth], parent=second,
                          revision_reason="Second pool amendment",
                          excluded_reasons={hypotheses[0]: "Historical competitor"})
        context = planning_context(self.store.events(), binding_for(self.store.events(), final))
        self.assertEqual([event["id"] for event in context if event["kind"] == "hypothesis"],
                         [*hypotheses, third, fourth])

    def test_binding_rejects_changed_or_extra_fields_and_boolean_version(self):
        _, _, pool = self.seed()
        history = self.store.events()
        binding = binding_for(history, pool)
        for changes in (dict(schema_version=True), dict(study_id="other-study"),
                        dict(question_hash="0" * 64), dict(explanation_set_hash="0" * 64),
                        dict(explanation_set="missing"), dict(extra="not supported")):
            with self.subTest(changes=changes), self.assertRaises(PlanningError):
                planning_context(history, {**binding, **changes})
        with self.assertRaises(PlanningError):
            planning_context(history, {k: v for k, v in binding.items() if k != "study_id"})
        with self.assertRaises(PlanningError):
            binding_for(history, pool, current=1)

    def test_pure_validation_rejects_malformed_payloads_and_endpoint_hashes(self):
        question, hypotheses, pool = self.seed()
        history = self.store.events()
        cases = [(question, "schema_version", True), (question, "extra", "unexpected"),
                 (question, "parent_hash", "0" * 64), (question, "constraints", []),
                 (pool, "question_hash", "0" * 64), (pool, "study_id", "other-study"),
                 (pool, "hypothesis_hashes", {id: "0" * 64 for id in hypotheses}),
                 (pool, "excluded_reasons", {hypotheses[0]: "not removed"})]
        for id, field, value in cases:
            modified = deepcopy(history)
            next(event for event in modified if event["id"] == id)["payload"][field] = value
            with self.subTest(field=field, id=id), self.assertRaises(PlanningError):
                validate_planning(modified)
        changed_actor = deepcopy(history)
        changed_actor[-1]["role"] = "reviewer"
        with self.assertRaisesRegex(PlanningError, "planner"):
            validate_planning(changed_actor)

    def test_pure_validation_rejects_forward_self_and_wrong_kind_parents(self):
        question, _, pool = self.seed()
        child = self.question(parent=question, revision_reason="Fixture amendment")
        history = self.store.events()
        for parent in (child, pool, "missing"):
            modified = deepcopy(history)
            modified[-1]["payload"]["parent"] = parent
            with self.subTest(parent=parent), self.assertRaises(PlanningError):
                validate_planning(modified)
        modified = deepcopy(history)
        original = next(event for event in modified if event["id"] == question)
        original["payload"].update(parent=child, parent_hash=modified[-1]["hash"],
                                   revision_reason="Forward reference")
        with self.assertRaises(PlanningError):
            validate_planning(modified)

    def test_pure_validation_rejects_duplicate_or_unordered_history(self):
        self.seed()
        history = self.store.events()
        for modified in ([*history, history[-1]], list(reversed(history)),
                         [*history[:-1], {**history[-1], "seq": True}],
                         [*history[:-1], {**history[-1], "hash": "invalid"}]):
            with self.subTest(modified=modified), self.assertRaises(PlanningError):
                validate_planning(modified)

    def test_pure_validation_checks_revision_hash_and_prior_head_at_recording_time(self):
        question, hypotheses, first = self.seed()
        q2 = self.question(parent=question, revision_reason="Fixture question revision")
        second = self.pool(q2, hypotheses, parent=first, revision_reason="Fixture pool revision")
        third = self.pool(q2, hypotheses, parent=second, revision_reason="Another fixture revision")
        history = self.store.events()
        for id, changes in ((q2, {"parent_hash": "0" * 64}),
                            (second, {"parent_hash": "0" * 64}),
                            (second, {"question": question, "question_hash": self.event(question)["hash"]}),
                            (third, {"parent": first, "parent_hash": self.event(first)["hash"]})):
            modified = deepcopy(history)
            next(event for event in modified if event["id"] == id)["payload"].update(changes)
            with self.subTest(id=id, changes=changes), self.assertRaises(PlanningError):
                validate_planning(modified)

    def test_competing_revisions_commit_only_one_current_head(self):
        parent = self.question()
        barrier = threading.Barrier(2, timeout=10)
        append = Store.append

        def synchronized(store, **kwargs):
            if kwargs["kind"] == "research_question" and kwargs["payload"]["parent"] == parent:
                barrier.wait()
            return append(store, **kwargs)

        def revise(name):
            with Store(self.root) as store:
                planner = Planning(store, SimpleNamespace(id=name, role="planner"))
                try:
                    return planner.question(study_id="fixture-study", statement="Question",
                           objective="Objective", scope=self.scope, constraints=["Fixture only"],
                           stopping_criteria=["Declared stop"], parent=parent,
                           revision_reason=f"Revision proposed by {name}")
                except ConflictError as exc:
                    return exc

        with patch.object(Store, "append", synchronized), ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(revise, ["fixture-planner-a", "fixture-planner-b"]))
        self.assertEqual(sum(isinstance(result, str) for result in results), 1)
        self.assertEqual(sum(isinstance(result, ConflictError) for result in results), 1)
        self.assertEqual(len(self.store.events()), 2)
        validate_planning(self.store.events())


if __name__ == "__main__":
    unittest.main()
