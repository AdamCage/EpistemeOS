"""Review obligation -> frozen child plan, without scientific fulfillment."""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from episteme.commands import CommandService
from episteme.cli import main as cli_main
from episteme.followup import Followup, _index, followup_state
from episteme.graph import ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.planning import Planning
from episteme.replanning import Replanning
from episteme.reporting import PaperBuilder
from episteme.recovery import backup, restore
from episteme.search import COMPONENTS, Search
from episteme.store import Store


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="episteme-followup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "research"
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.study = "followup-study"
        self.planner = Actor("followup-planner", "planner")
        self.reviewer = Actor("source-reviewer", "reviewer")
        self.executor = Actor("source-executor", "executor")
        self.replicator = Actor("source-replicator", "replicator")
        self.analyst = Actor("source-analyst", "analyst")
        self.scope = {"population": "synthetic follow-up fixture"}
        self.implementation = self.store.put(b"source fixture computation")
        self.reimplementation = self.store.put(b"separate source fixture reanalysis")
        self.environment = self.store.put_json({"python": "fixture"})
        self.data = self.store.put_json({"values": [1, 3]})
        planning = Planning(self.store, self.planner)
        self.question = planning.question(study_id=self.study, statement="Which mechanism explains the value?",
            objective="Test a review-driven child proposal", scope=self.scope,
            constraints=["Fixture only"], stopping_criteria=["Stop after one control"])
        kernel = Kernel(self.store, self.planner)
        hypotheses = [kernel.hypothesis(text, "distinct prediction", "falsifying observation", self.scope)
                      for text in ("Null mechanism", "Alternative mechanism")]
        self.explanation_set = planning.explanation_set(question=self.question, hypotheses=hypotheses,
            comparison_plan="Compare a control against the observed value")
        self.protocol = kernel.preregister_for_set(explanation_set=self.explanation_set,
            design="Frozen source comparison", metric="mean", analysis_plan="Compute the fixture mean",
            stopping_rule="One primary and one reanalysis", seeds=[7], run_limit=4,
            implementation=self.implementation, environment=self.environment, data=self.data,
            replication_tolerance=0)
        self.search = Search(self.store, self.planner)
        self.tree = self.search.register_tree(weights={key: 1 for key in COMPONENTS},
            cost_weight=0, budget=6, cost_unit="enqueued_attempt", max_nodes=4,
            max_depth=2, max_width=2, max_selections=3, max_retries=0)
        self.node = self.search.add_node(self.tree, protocol=self.protocol,
            action="discriminate", components=self.components(), estimated_cost=2,
            rationale="Source experiment")
        selection = self.search.select_next(self.tree)["id"]
        primary = Kernel(self.store, self.executor).start_run(self.protocol, seed=7,
            implementation=self.implementation, environment=self.environment,
            command=["python", "source.py"])
        Kernel(self.store, self.executor).finish_run(primary, status="completed", outputs=self.outputs())
        reanalysis = Kernel(self.store, self.replicator).start_run(self.protocol, seed=7,
            implementation=self.reimplementation, environment=self.environment,
            command=["python", "reanalysis.py"], replicate_of=primary)
        Kernel(self.store, self.replicator).finish_run(reanalysis, status="completed", outputs=self.outputs())
        self.claim = Kernel(self.store, self.analyst).claim(protocol=self.protocol,
            statement="The fixture value is positive", scope=self.scope,
            evidence=[primary, reanalysis], limitations=["Synthetic fixture only"], outcome="supports")
        self.search.finish_selection(selection, status="completed", actual_cost=2,
            reason="Source fixture completed; scientific review pending", run=primary, claim=self.claim)
        self.basis = Kernel(self.store, self.reviewer).gate(self.claim)["basis_hash"]
        self.assertTrue(Kernel(self.store, self.reviewer).gate(self.claim)["passed"])
        recorded = self.command("replanning.record_review", dict(claim=self.claim,
            verdict="request_changes", rationale="Potential confounding in the source design",
            findings=[dict(kind="discriminating_experiment", action="Add a control",
                closure_criterion="A new reviewed comparison addresses the confound",
                evidence_refs=[self.claim])], expected_basis=self.basis,
            link_assessments=None), self.reviewer,
            lambda p: Replanning(self.store, self.reviewer).record_review(**p))
        self.review = recorded["review"]
        self.obligation = recorded["obligations"][0]
        self.new_implementation = self.store.put(b"follow-up fixture computation")

    @staticmethod
    def components():
        return dict(discrimination=1.0, uncertainty=0.5, coverage=0.5, invalidity_risk=0.0)

    def outputs(self):
        return dict(raw_data=self.data, metrics=self.store.put_json({"mean": 2.0}),
                    log=self.store.put(b"completed fixture"))

    def envelope(self, action, payload, actor):
        return dict(context=dict(command_id=uuid4().hex, expected_revision=len(self.store.events()),
            actor=actor.id, role=actor.role, study_id=self.study,
            correlation_id="followup-fixture", causation_id=None),
            request=dict(version=1, action=action, payload=payload))

    def command(self, action, payload, actor, handler):
        envelope = self.envelope(action, payload, actor)
        result = self.store.command(envelope["context"], envelope["request"],
                                    lambda: handler(payload))
        return result

    def spec(self):
        return dict(design="Frozen follow-up control", metric="mean",
            analysis_plan="Compute the control mean", stopping_rule="One primary and one reanalysis",
            seeds=[8], run_limit=2, implementation=self.new_implementation,
            environment=self.environment, data=self.data, replication_tolerance=0)

    def options(self, **changes):
        options = dict(obligation=self.obligation, parent_node=self.node,
            explanation_set=self.explanation_set, protocol_spec=self.spec(),
            node_spec=dict(action="discriminate", components=self.components(),
                           estimated_cost=2, rationale="Resolve the recorded confound"),
            expected_basis=self.basis)
        options.update(changes)
        return options

    def apply(self, options=None):
        options = self.options() if options is None else options
        return self.command("followup.apply", options, self.planner,
                            lambda p: Followup(self.store, self.planner).apply(**p))

    def test_atomic_child_binding_replay_restart_and_no_false_closure(self):
        options = self.options()
        envelope = self.envelope("followup.apply", options, self.planner)
        followup = self.store.command(envelope["context"], envelope["request"],
            lambda: Followup(self.store, self.planner).apply(**options))
        self.assertEqual(self.store.command(envelope["context"], envelope["request"],
            lambda: self.fail("idempotent replay invoked the handler")), followup)
        receipt = self.store.receipts()[-1]
        events = {event["id"]: event for event in self.store.events()}
        self.assertEqual([events[id]["kind"] for id in receipt["event_ids"]],
                         ["protocol", "experiment_node", "replan_followup"])
        protocol, node, binding = (events[id] for id in receipt["event_ids"])
        self.assertEqual(binding["id"], followup)
        self.assertEqual(protocol["payload"]["parent"], self.protocol)
        self.assertEqual(node["payload"]["parent"], self.node)
        self.assertEqual(node["payload"]["protocol"], protocol["id"])
        self.assertEqual(binding["payload"]["obligation_hash"], events[self.obligation]["hash"])
        self.assertEqual(binding["payload"]["scientific_validity"], "not_assessed")
        self.assertEqual(followup_state(self.store, self.obligation)["obligation_resolution"], "open")
        self.assertEqual(Kernel(self.store, self.reviewer).next_action(self.claim)["action"], "replan")
        self.assertFalse(any(e["kind"] in {"run", "result", "claim", "review"}
                             and e["seq"] > protocol["seq"] for e in self.store.events()))
        self.assertIn(self.obligation, _index(self.store, self.store.events()))
        historical = [event for event in self.store.events() if event["seq"] < protocol["seq"]]
        self.assertEqual(_index(self.store, historical), {})
        with Store(self.root) as reopened:
            self.assertEqual(followup_state(reopened, self.obligation)["followup"], followup)
            self.assertIn(self.obligation, _index(reopened, reopened.events()))

    def test_command_service_and_graph_resolve_the_bound_provenance(self):
        envelope = self.envelope("followup.apply", self.options(), self.planner)
        followup = CommandService(self.store).execute(envelope)
        graph = ResearchGraph.from_store(self.store)
        self.assertEqual(graph.node(followup).kind.value, "replan_followup")
        self.assertEqual(CommandService(self.store).execute(envelope), followup)
        output = StringIO()
        with redirect_stdout(output):
            status = cli_main(["followup", "status", self.obligation, "--root", str(self.root)])
        self.assertEqual(status, 0)
        observed = json.loads(output.getvalue())
        self.assertEqual(observed["followup"], followup)
        self.assertEqual(observed["obligation_resolution"], "open")

    def test_stale_basis_foreign_set_and_budget_reject_without_events(self):
        before = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "basis"):
            self.apply(self.options(expected_basis="0" * 64))
        foreign = Planning(self.store, self.planner).question(study_id="another-study",
            statement="Foreign question", objective="Foreign objective", scope=self.scope,
            constraints=["Fixture"], stopping_criteria=["Stop"])
        kernel = Kernel(self.store, self.planner)
        foreign_set = Planning(self.store, self.planner).explanation_set(question=foreign,
            hypotheses=[kernel.hypothesis("Foreign null", "p", "f", self.scope),
                        kernel.hypothesis("Foreign alternative", "p", "f", self.scope)],
            comparison_plan="Foreign comparison")
        with self.assertRaisesRegex(ValueError, "study"):
            self.apply(self.options(explanation_set=foreign_set))
        self.assertEqual(len(_index(self.store, self.store.events())), 0)
        self.assertEqual(before[0].count('"kind":"replan_followup"'), 0)
        self.assertFalse(any(e["kind"] == "replan_followup" for e in self.store.events()))

    def test_rollback_on_binding_write_and_no_duplicate_plan(self):
        options = self.options()
        before = self.store.export(), self.store.export_receipts()
        original = self.store.append

        def crash(*args, **kwargs):
            if kwargs.get("kind") == "replan_followup":
                raise RuntimeError("binding crash")
            return original(*args, **kwargs)

        with patch.object(self.store, "append", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "binding crash"):
                self.apply(options)
        self.assertEqual((self.store.export(), self.store.export_receipts()), before)
        self.apply(options)
        with self.assertRaisesRegex(ValueError, "already bound"):
            self.apply(options)

    def test_budget_and_parent_state_are_checked_before_preregistration(self):
        other_protocol = Kernel(self.store, self.planner).preregister_for_set(
            explanation_set=self.explanation_set, design="Competing queued fixture",
            metric="mean", analysis_plan="Compute the mean", stopping_rule="Two primary and reanalysis pairs",
            seeds=[9, 10], run_limit=4, implementation=self.new_implementation,
            environment=self.environment, data=self.data, replication_tolerance=0)
        root = self.search.add_node(self.tree, protocol=other_protocol,
            action="baseline", components=self.components(), estimated_cost=4,
            rationale="Another affordable pending root")
        with self.assertRaisesRegex(ValueError, "parent"):
            self.apply(self.options(parent_node=root))
        choice = self.search.select_next(self.tree)
        self.assertEqual(choice["node"], root)
        with self.assertRaisesRegex(ValueError, "budget"):
            self.apply()

    def test_new_source_evidence_stales_followup(self):
        additional = Kernel(self.store, self.executor).start_run(self.protocol, seed=7,
            implementation=self.implementation, environment=self.environment,
            command=["python", "source.py", "--repeat"])
        Kernel(self.store, self.executor).finish_run(additional, status="completed", outputs=self.outputs())
        before = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "stale|mechanically"):
            self.apply()
        self.assertEqual((self.store.export(), self.store.export_receipts()), before)

    def test_revised_source_reviewer_opinion_stales_followup(self):
        Kernel(self.store, self.reviewer).review(self.claim, verdict="approve",
            rationale="Fixture reviewer withdrew the original concern", actions=[],
            expected_basis=self.basis)
        before = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "latest opinion"):
            self.apply()
        self.assertEqual((self.store.export(), self.store.export_receipts()), before)

    def test_only_scientific_child_action_and_exact_attempt_cost(self):
        options = self.options()
        options["node_spec"]["action"] = "debug"
        with self.assertRaisesRegex(ValueError, "scientific"):
            self.apply(options)
        options = self.options()
        options["node_spec"]["estimated_cost"] = 1
        with self.assertRaisesRegex(ValueError, "cost"):
            self.apply(options)
        self.assertFalse(any(e["kind"] == "replan_followup" for e in self.store.events()))

    def test_command_study_cannot_cross_the_source_study(self):
        options = self.options()
        envelope = self.envelope("followup.apply", options, self.planner)
        envelope["context"]["study_id"] = "foreign-study"
        before = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "command study"):
            self.store.command(envelope["context"], envelope["request"],
                lambda: Followup(self.store, self.planner).apply(**options))
        self.assertEqual((self.store.export(), self.store.export_receipts()), before)

    def test_descendant_claim_cannot_paper_over_its_open_source_obligation(self):
        self.apply()
        plan = followup_state(self.store, self.obligation)
        selection = self.search.select_next(self.tree)
        self.assertEqual(selection["node"], plan["experiment_node"])
        primary = Kernel(self.store, self.executor).start_run(plan["protocol"], seed=8,
            implementation=self.new_implementation, environment=self.environment,
            command=["python", "control.py"])
        Kernel(self.store, self.executor).finish_run(primary, status="completed", outputs=self.outputs())
        reanalysis = Kernel(self.store, self.replicator).start_run(plan["protocol"], seed=8,
            implementation=self.reimplementation, environment=self.environment,
            command=["python", "control-reanalysis.py"], replicate_of=primary)
        Kernel(self.store, self.replicator).finish_run(reanalysis, status="completed", outputs=self.outputs())
        successor = Kernel(self.store, self.analyst).claim(protocol=plan["protocol"],
            statement="The synthetic control has a positive value", scope=self.scope,
            evidence=[primary, reanalysis], limitations=["Fixture only"], outcome="supports")
        self.search.finish_selection(selection["id"], status="completed", actual_cost=2,
            reason="Synthetic follow-up completed; review remains local opinion",
            run=primary, claim=successor)
        reviewer = Kernel(self.store, Actor("successor-reviewer", "reviewer"))
        basis = reviewer.gate(successor)["basis_hash"]
        self.assertTrue(reviewer.gate(successor)["passed"])
        reviewer.review(successor, verdict="approve", rationale="Fixture opinion",
                        actions=[], expected_basis=basis)
        decision = reviewer.next_action(successor)
        self.assertEqual(decision["action"], "replan")
        self.assertIn(self.obligation, decision["obligations"])
        with self.assertRaisesRegex(ValueError, "not eligible for paper"):
            PaperBuilder(self.store, Actor("fixture-writer", "writer")).build(
                title="Premature successor draft", claims=[successor],
                expected_bases={successor: basis})

    def test_backup_restores_followup_specification_and_exact_receipts(self):
        self.apply()
        history = self.store.export()
        receipts = self.store.export_receipts()
        graph = ResearchGraph.from_store(self.store).snapshot_hash
        snapshot = self.root.parent / "snapshot"
        restored = self.root.parent / "restored"
        backup(self.store, snapshot)
        restore(snapshot, restored)
        with Store(restored) as reopened:
            self.assertEqual(reopened.export(), history)
            self.assertEqual(reopened.export_receipts(), receipts)
            self.assertEqual(ResearchGraph.from_store(reopened).snapshot_hash, graph)
            self.assertEqual(followup_state(reopened, self.obligation)["status"], "planned")


if __name__ == "__main__":
    unittest.main()
