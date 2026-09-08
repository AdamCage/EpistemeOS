"""Search decisions are persistent priorities; they are not scientific approval."""

from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
import tempfile
import threading
import unittest
from unittest.mock import patch

from episteme.kernel import Actor, GateError, Kernel
from episteme.search import COMPONENTS, Search
from episteme.store import ConflictError, Store


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="episteme-search-")
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.addCleanup(lambda: self.store.close())
        self.search = Search(self.store, Actor("planner", "planner"))
        self.judge = Search(self.store, Actor("judge-1", "judge"))
        self.planner = Kernel(self.store, Actor("planner", "planner"))
        self.executor = Kernel(self.store, Actor("executor", "executor"))
        self.scope = {"dataset": "fixture", "split": "holdout"}
        self.hypotheses = [self.planner.hypothesis(f"Explanation {i}", f"Prediction {i}",
                                                f"Falsifier {i}", self.scope) for i in range(3)]
        self.blob = self.store.put(b"frozen fixture")
        self.components = dict(discrimination=0.8, uncertainty=0.5, coverage=0.5, invalidity_risk=0.1)
        self.judge_info = dict(model="scripted-fixture", model_version="v1", prompt_version="v1")

    def protocol(self, parent=None, run_limit=30):
        return self.planner.preregister(
            hypotheses=self.hypotheses[:2], scope=self.scope, design="Registered comparison",
            metric="mean", analysis_plan="Mean of raw values", stopping_rule="Registered seeds",
            seeds=[7], run_limit=run_limit, implementation=self.blob, environment=self.blob,
            data=self.blob, replication_tolerance=0.01, parent=parent)

    def tree(self, **changes):
        options = dict(weights={k: 1 for k in COMPONENTS}, cost_weight=0.1, budget=10,
                       cost_unit="fixture-cost", max_nodes=20, max_depth=4, max_width=5,
                       max_selections=20, max_retries=2)
        options.update(changes)
        return self.search.register_tree(**options)

    def node(self, tree, **changes):
        options = dict(protocol=self.protocol(), action="discriminate", components=self.components,
                       estimated_cost=1, rationale="Expected to distinguish explanations")
        options.update(changes)
        return self.search.add_node(tree, **options)

    def execute_run(self, protocol, status="completed", actor=None, replicate_of=None):
        actor = actor or self.executor
        run = actor.start_run(protocol, seed=7, implementation=self.blob if not replicate_of
                              else self.store.put(b"independent implementation"), environment=self.blob,
                              command=["python", "fixture.py"], replicate_of=replicate_of)
        outputs = {} if status != "completed" else dict(
            raw_data=self.blob, metrics=self.store.put_json({"mean": 0}), log=self.blob)
        actor.finish_run(run, status=status, outputs=outputs, reason="Fixture terminal state")
        return run

    def finish(self, selection, status="completed", cost=1, outcome=None):
        run = self.execute_run(selection["protocol"], status)
        claim = None
        if outcome:
            claim = self.executor.claim(protocol=selection["protocol"], statement="Fixture result",
                                        scope=self.scope, evidence=[run], limitations=["Synthetic"],
                                        outcome=outcome)
        self.search.finish_selection(selection["id"], status=status, actual_cost=cost,
                                     reason="Reconciled saved result", run=run, claim=claim)
        return run

    def test_tournament_records_alternating_order_ties_abstentions_and_judge_provenance(self):
        tournament = self.search.register_tournament(candidates=self.hypotheses[:2], rubric="v1")
        first, second = self.search.pairings(tournament)
        self.assertEqual((first["a"], first["b"]), (second["b"], second["a"]))
        for pairing, verdict in [(first, "tie"), (second, "abstain")]:
            self.judge.ballot(tournament, **pairing, verdict=verdict, rationale="No clear preference",
                              judge=self.judge_info, sources=[self.hypotheses[0]])
        ranked = self.search.ranking(tournament)
        self.assertEqual(ranked["scientific_validity"], "not_assessed")
        self.assertEqual(ranked["meaning"], "research_priority_only")
        for row in ranked["ranking"]:
            self.assertEqual((row["priority"], row["ties"], row["abstentions"]), (0.5, 1, 1))
        event = self.store.events()[-1]
        self.assertEqual((event["actor"], event["role"]), ("judge-1", "judge"))
        self.assertEqual(event["payload"]["judge"], self.judge_info)
        self.assertEqual(event["payload"]["sources"], [self.hypotheses[0]])

    def test_tournament_preference_ranking_and_replay_do_not_create_claims(self):
        tournament = self.search.register_tournament(candidates=self.hypotheses, rubric="v1", seed=91)
        pair = self.search.pairings(tournament)[0]
        self.judge.ballot(tournament, **pair, verdict="b", rationale="Higher testability",
                          judge=self.judge_info)
        expected = self.search.ranking(tournament)
        self.assertEqual(expected["ranking"][0]["candidate"], pair["b"])
        self.assertEqual(expected["ranking"][-1]["candidate"], pair["a"])
        with Store(self.directory.name) as reopened:
            self.assertEqual(Search(reopened, Actor("reader", "reviewer")).ranking(tournament), expected)
        self.assertFalse(any(e["kind"] in {"claim", "review"} for e in self.store.events()))

    def test_ballot_rejects_duplicate_swapped_unknown_candidates_and_unknown_sources(self):
        tournament = self.search.register_tournament(candidates=self.hypotheses[:2], rubric="v1")
        pair = self.search.pairings(tournament)[0]
        options = dict(**pair, verdict="a", rationale="Reason", judge=self.judge_info)
        self.judge.ballot(tournament, **options)
        with self.assertRaisesRegex(GateError, "already voted"):
            self.judge.ballot(tournament, **options)
        for changes in [dict(a=pair["b"], b=pair["a"]), dict(a=self.hypotheses[2]),
                        dict(a="missing"), dict(round=True), dict(sources=["missing"]),
                        dict(judge={"model": "missing versions"})]:
            with self.subTest(changes=changes), self.assertRaises(GateError):
                self.judge.ballot(tournament, **dict(options, **changes))

    def test_tournament_requires_real_competing_hypotheses_and_matching_scope(self):
        other = self.planner.hypothesis("Other", "Prediction", "Falsifier", {"dataset": "other"})
        for candidates in [[], self.hypotheses[:1], [self.hypotheses[0]] * 2,
                           ["missing", self.hypotheses[0]], [self.hypotheses[0], other]]:
            with self.subTest(candidates=candidates), self.assertRaises(GateError):
                self.search.register_tournament(candidates=candidates, rubric="v1")

    def test_roles_guard_search_mutations_and_judging(self):
        with self.assertRaisesRegex(GateError, "cannot create tournament"):
            self.judge.register_tournament(candidates=self.hypotheses, rubric="v1")
        tree = self.tree()
        self.node(tree)
        with self.assertRaisesRegex(GateError, "cannot create search_selection"):
            self.judge.select_next(tree)
        tournament = self.search.register_tournament(candidates=self.hypotheses, rubric="v1")
        with self.assertRaisesRegex(GateError, "cannot create tournament_ballot"):
            self.search.ballot(tournament, **self.search.pairings(tournament)[0], verdict="tie",
                               rationale="Reason", judge=self.judge_info)

    def test_best_first_uses_frozen_components_and_selects_affordable_node(self):
        tree = self.tree(budget=2)
        expensive = self.node(tree, estimated_cost=3)
        cheaper = self.node(tree, estimated_cost=1, components=dict(self.components, discrimination=0.1))
        selection = self.search.select_next(tree)
        self.assertEqual(selection["node"], cheaper)
        self.assertEqual(selection["frontier"][0]["node"], expensive)
        self.assertEqual(selection["frontier"][0]["reason"], "insufficient_budget")
        self.assertEqual(self.search.tree_state(tree)["reserved"], "1")
        self.assertEqual(self.search.select_next(tree)["reason"], "inflight_limit")

    def test_score_penalizes_risk_and_cost_without_promoting_truth(self):
        tree = self.tree()
        risky = self.node(tree, components=dict(self.components, invalidity_risk=1))
        safer = self.node(tree)
        self.assertEqual(self.search.select_next(tree)["node"], safer)
        state = self.search.tree_state(tree)
        self.assertLess(state["nodes"][risky]["score"], state["nodes"][safer]["score"])
        self.assertEqual(state["scientific_validity"], "not_assessed")

    def test_deterministic_tie_break_and_unfinished_reservation_survive_reopen(self):
        tree = self.tree(seed=14)
        self.node(tree)
        self.node(tree)
        before = self.search.tree_state(tree)
        with Store(self.directory.name) as reopened:
            twin = Search(reopened, Actor("planner", "planner"))
            self.assertEqual(twin.tree_state(tree), before)
            decision = twin.select_next(tree)
        self.assertEqual(decision["frontier"][0]["node"], decision["node"])
        replay = self.search.tree_state(tree)
        self.assertEqual(replay["reserved"], "1")
        self.assertEqual(replay["nodes"][decision["node"]]["state"], "selected")
        self.assertEqual(self.search.select_next(tree)["reason"], "inflight_limit")

    def test_policy_and_proposals_reject_nonfinite_bool_and_overflow(self):
        for invalid in [float("nan"), float("inf"), float("-inf"), True, 10 ** 1000]:
            with self.subTest(value=str(invalid)):
                with self.assertRaises(GateError):
                    self.tree(budget=invalid)
                tree = self.tree()
                with self.assertRaises(GateError):
                    self.node(tree, estimated_cost=invalid)
                with self.assertRaises(GateError):
                    self.node(tree, components=dict(self.components, coverage=invalid))
                with self.assertRaises(GateError):
                    self.tree(weights=dict.fromkeys(COMPONENTS, invalid))
        tree = self.tree(cost_weight=1e308)
        with self.assertRaisesRegex(GateError, "overflow"):
            self.node(tree, estimated_cost=1e308)

    def test_refs_parent_cycles_and_preexecuted_scientific_proposals_are_rejected(self):
        tree, other = self.tree(), self.tree()
        parent = self.node(tree)
        with self.assertRaisesRegex(GateError, "unknown protocol"):
            self.node(tree, protocol="missing")
        for target, reference in [(tree, "future-self-id"), (other, parent)]:
            with self.subTest(target=target), self.assertRaisesRegex(GateError, "existing node"):
                self.node(target, parent=reference)
        with self.assertRaisesRegex(GateError, "terminal selection"):
            self.node(tree, parent=parent)
        protocol = self.protocol()
        self.execute_run(protocol)
        with self.assertRaisesRegex(GateError, "unexecuted frozen"):
            self.node(tree, protocol=protocol)

    def test_selection_rechecks_execution_and_retains_stale_proposal_reason(self):
        tree = self.tree()
        protocol = self.protocol()
        stale = self.node(tree, protocol=protocol)
        current = self.node(tree, components=dict(self.components, discrimination=0.1))
        self.execute_run(protocol)  # Changed after proposal, before the selection snapshot.
        chosen = self.search.select_next(tree)
        self.assertEqual(chosen["node"], current)
        self.assertEqual(chosen["frontier"][0]["node"], stale)
        self.assertEqual(chosen["frontier"][0]["reason"], "protocol_already_executed")
        self.search.finish_selection(chosen["id"], status="cancelled", actual_cost=0,
                                     reason="Cancelled before dispatch")
        stopped = self.search.select_next(tree)
        self.assertIsNone(stopped["node"])
        self.assertEqual(stopped["reason"], "no_eligible_frontier")
        self.assertEqual(stopped["frontier"][0]["node"], stale)
        state = self.search.tree_state(tree)
        self.assertEqual(state["nodes"][stale]["state"], "pending")
        self.assertEqual(state["reserved"], "0")

    def test_selection_does_not_reserve_one_protocol_for_two_actions(self):
        tree = self.tree(max_inflight=2)
        protocol = self.protocol()
        self.node(tree, protocol=protocol, action="baseline")
        self.node(tree, protocol=protocol, action="discriminate")
        first = self.search.select_next(tree)
        second = self.search.select_next(tree)
        self.assertIsNone(second["node"])
        self.assertEqual(second["frontier"][0]["reason"], "protocol_reserved")
        self.finish(first)
        stopped = self.search.select_next(tree)
        self.assertEqual(stopped["frontier"][0]["reason"], "protocol_already_executed")
        self.assertEqual(self.search.tree_state(tree)["spent"], "1")

    def test_replication_selection_rechecks_run_capacity_and_unfinished_execution(self):
        tree = self.tree()
        protocol = self.protocol(run_limit=2)
        self.execute_run(protocol)
        self.node(tree, protocol=protocol, action="replicate")
        self.execute_run(protocol)
        stopped = self.search.select_next(tree)
        self.assertIsNone(stopped["node"])
        self.assertEqual(stopped["frontier"][0]["reason"], "protocol_run_limit")

        tree = self.tree()
        protocol = self.protocol()
        self.execute_run(protocol)
        self.node(tree, protocol=protocol, action="replicate")
        inflight = self.executor.start_run(protocol, seed=7, implementation=self.blob,
                                            environment=self.blob, command=["python", "fixture.py"])
        waiting = self.search.select_next(tree)
        self.assertIsNone(waiting["node"])
        self.assertEqual(waiting["frontier"][0]["reason"], "protocol_inflight")
        self.executor.finish_run(inflight, status="failed", outputs={}, reason="Process failed")
        self.assertIsNotNone(self.search.select_next(tree)["node"])

    def test_new_execution_during_selection_causes_conflict_then_revalidation(self):
        tree = self.tree()
        protocol = self.protocol()
        self.node(tree, protocol=protocol)
        original_write = self.search._write

        def advance(history, kind, payload, roles=None):
            self.execute_run(protocol)
            return original_write(history, kind, payload, roles)

        with patch.object(self.search, "_write", advance), self.assertRaises(ConflictError):
            self.search.select_next(tree)
        self.assertEqual(self.search.tree_state(tree)["reserved"], "0")
        self.assertEqual(self.search.select_next(tree)["frontier"][0]["reason"],
                         "protocol_already_executed")

    def test_node_width_depth_count_and_selection_bounds_retain_history(self):
        tree = self.tree(max_width=1, max_depth=0, max_nodes=3, max_selections=1)
        parent = self.node(tree)
        with self.assertRaisesRegex(GateError, "width"):
            self.node(tree)
        self.finish(self.search.select_next(tree))
        with self.assertRaisesRegex(GateError, "depth"):
            self.node(tree, parent=parent)
        self.assertEqual(self.search.select_next(tree)["reason"], "selection_limit")
        tree = self.tree(max_nodes=1)
        self.node(tree)
        with self.assertRaisesRegex(GateError, "node bound"):
            self.node(tree)

    def test_null_result_is_retained_as_completed_and_cannot_trigger_technical_retry(self):
        tree = self.tree()
        parent = self.node(tree)
        selection = self.search.select_next(tree)
        self.finish(selection, outcome="inconclusive")
        node = self.search.tree_state(tree)["nodes"][parent]
        self.assertEqual((node["state"], node["scientific_outcome"]), ("completed", "inconclusive"))
        for action in ("retry", "debug"):
            with self.assertRaisesRegex(GateError, "technically failed"):
                self.node(tree, parent=parent, protocol=selection["protocol"], action=action)
        child = self.node(tree, parent=parent)
        self.assertEqual(self.search.select_next(tree)["node"], child)

    def test_failed_retry_retains_failures_consumes_budget_and_can_produce_scientific_evidence(self):
        tree = self.tree(budget=3)
        parent = self.node(tree)
        selection = self.search.select_next(tree)
        self.finish(selection, status="failed")
        retry = self.node(tree, parent=parent, protocol=selection["protocol"], action="retry")
        self.finish(self.search.select_next(tree), outcome="refutes")
        state = self.search.tree_state(tree)
        self.assertEqual(state["spent"], "2")
        self.assertEqual(state["nodes"][parent]["scientific_outcome"], "not_assessed")
        self.assertEqual(state["nodes"][retry]["scientific_outcome"], "refutes")

    def test_retry_bounds_protocol_freeze_and_debug_ineligibility(self):
        tree = self.tree(max_retries=1)
        parent = self.node(tree)
        selected = self.search.select_next(tree)
        self.finish(selected, status="failed")
        with self.assertRaisesRegex(GateError, "keeps protocol"):
            self.node(tree, parent=parent, action="retry")
        with self.assertRaisesRegex(GateError, "registered components"):
            self.node(tree, parent=parent, protocol=selected["protocol"], action="retry",
                      components=dict(self.components, discrimination=0.9))
        amended = self.protocol(parent=selected["protocol"])
        debug = self.node(tree, parent=parent, protocol=amended, action="debug")
        decision = self.search.select_next(tree)
        run = self.execute_run(amended)
        claim = self.executor.claim(protocol=amended, statement="Diagnostic", scope=self.scope,
                                    evidence=[run], limitations=["Debug only"], outcome="supports")
        with self.assertRaisesRegex(GateError, "debug cannot"):
            self.search.finish_selection(decision["id"], status="completed", actual_cost=1,
                                         reason="Diagnostic", run=run, claim=claim)
        self.search.finish_selection(decision["id"], status="completed", actual_cost=1,
                                     reason="Diagnostic", run=run)
        self.assertFalse(self.search.tree_state(tree)["nodes"][debug]["scientific_evidence_eligible"])
        tree = self.tree(max_retries=0)
        parent = self.node(tree)
        selected = self.search.select_next(tree)
        self.finish(selected, status="failed")
        with self.assertRaisesRegex(GateError, "retry bound"):
            self.node(tree, parent=parent, protocol=selected["protocol"], action="retry")

    def test_retry_limit_counts_sibling_and_debug_proposals_in_same_lineage(self):
        tree = self.tree(max_retries=2)
        parent = self.node(tree)
        selected = self.search.select_next(tree)
        self.finish(selected, status="failed")
        retry = self.node(tree, parent=parent, protocol=selected["protocol"], action="retry")
        self.finish(self.search.select_next(tree), status="failed")
        amended = self.protocol(parent=selected["protocol"])
        debug = self.node(tree, parent=parent, protocol=amended, action="debug")
        debug_selection = self.search.select_next(tree)
        self.search.finish_selection(debug_selection["id"], status="cancelled", actual_cost=0,
                                     reason="Retain unused diagnostic proposal")
        with self.assertRaisesRegex(GateError, "retry bound"):
            self.node(tree, parent=parent, protocol=selected["protocol"], action="retry")
        with self.assertRaisesRegex(GateError, "retry bound"):
            self.node(tree, parent=retry, protocol=selected["protocol"], action="retry")
        state = self.search.tree_state(tree)
        self.assertEqual(state["nodes"][retry]["technical_attempt"], 1)
        self.assertEqual(state["nodes"][debug]["technical_attempt"], 2)
        self.assertEqual(state["nodes"][debug]["state"], "cancelled")

        # A new scientific protocol has a separate repair lineage.
        followup = self.node(tree, parent=parent)
        followup_selection = self.search.select_next(tree)
        self.finish(followup_selection, status="failed")
        fresh_retry = self.node(tree, parent=followup, protocol=followup_selection["protocol"],
                                action="retry")
        self.assertEqual(self.search.tree_state(tree)["nodes"][fresh_retry]["technical_attempt"], 1)

    def test_budget_exact_decimal_reservations_release_and_overrun_remain_visible(self):
        tree = self.tree(budget=0.3, max_inflight=2)
        self.node(tree, estimated_cost=0.1)
        self.node(tree, estimated_cost=0.2)
        first, second = self.search.select_next(tree), self.search.select_next(tree)
        self.assertIsNotNone(second["node"])
        self.assertEqual(self.search.tree_state(tree)["remaining"], "0")
        self.search.finish_selection(first["id"], status="cancelled", actual_cost=0,
                                     reason="Cancelled before dispatch")
        self.finish(second, cost=0.5)
        state = self.search.tree_state(tree)
        self.assertEqual(state["spent"], "0.5")
        self.assertEqual(state["reserved"], "0")
        self.node(tree, estimated_cost=0.01)
        stop = self.search.select_next(tree)
        self.assertEqual(stop["reason"], "budget_exhausted")
        self.assertEqual(stop["remaining_before"], "-0.2")
        self.assertEqual(len(stop["frontier"]), 1)

    def test_exact_budget_admission_does_not_round_large_integer_up(self):
        budget = 10 ** 29 - 1
        tree = self.tree(budget=budget)
        self.node(tree, estimated_cost=10 ** 29)
        stop = self.search.select_next(tree)
        self.assertIsNone(stop["node"])
        self.assertEqual(stop["reason"], "budget_exhausted")
        self.assertEqual(stop["remaining_before"], str(budget))
        self.assertEqual(stop["frontier"][0]["reason"], "insufficient_budget")
        self.assertEqual(self.search.tree_state(tree)["reserved"], "0")

    def test_exact_budget_preserves_subnormal_reservation_beside_one_unit(self):
        tree = self.tree(budget=1, max_inflight=2)
        tiny = self.node(tree, estimated_cost=5e-324)
        self.node(tree, estimated_cost=1)
        first = self.search.select_next(tree)
        self.assertEqual(first["node"], tiny)
        self.assertIsNone(self.search.select_next(tree)["node"])
        state = self.search.tree_state(tree)
        self.assertEqual(Fraction(state["remaining"]) + Fraction("5e-324"), 1)
        self.assertEqual(state["reserved"], "0." + "0" * 323 + "5")
        with Store(self.directory.name) as reopened:
            self.assertEqual(Search(reopened, Actor("reader", "reviewer")).tree_state(tree), state)

    def test_exact_subnormal_costs_and_overrun_survive_settlement(self):
        tree = self.tree(budget=1e-323, max_inflight=2)
        self.node(tree, estimated_cost=5e-324)
        self.node(tree, estimated_cost=5e-324)
        first, second = self.search.select_next(tree), self.search.select_next(tree)
        self.assertIsNotNone(second["node"])
        self.assertEqual(self.search.tree_state(tree)["remaining"], "0")
        self.search.finish_selection(first["id"], status="cancelled", actual_cost=0,
                                     reason="Cancelled before dispatch")
        self.finish(second, cost=1e-323)
        terminal = self.store.events()[-1]["payload"]
        self.assertEqual(terminal["cost_overrun_exact"], "0." + "0" * 323 + "5")
        state = self.search.tree_state(tree)
        self.assertEqual(state["spent"], "0." + "0" * 322 + "1")
        self.assertEqual(state["reserved"], "0")
        self.assertEqual(state["remaining"], "0")

    def test_terminal_cost_overrun_has_exact_decimal_field(self):
        tree = self.tree(budget=0.3)
        self.node(tree, estimated_cost=0.1)
        self.finish(self.search.select_next(tree), cost=0.3)
        terminal = self.store.events()[-1]["payload"]
        self.assertEqual(terminal["cost_overrun_exact"], "0.2")
        self.assertIsInstance(terminal["cost_overrun"], float)
        self.assertEqual(self.search.tree_state(tree)["remaining"], "0")

    def test_terminal_requires_matching_real_result_and_never_releases_dispatched_work_by_omission(self):
        tree = self.tree()
        self.node(tree)
        selected = self.search.select_next(tree)
        run = self.executor.start_run(selected["protocol"], seed=7, implementation=self.blob,
                                      environment=self.blob, command=["python", "fixture.py"])
        with self.assertRaisesRegex(GateError, "without terminal run"):
            self.search.finish_selection(selected["id"], status="cancelled", actual_cost=0,
                                         reason="Observation timeout")
        with self.assertRaisesRegex(GateError, "match recorded"):
            self.search.finish_selection(selected["id"], status="failed", actual_cost=1,
                                         reason="Observation timeout", run=run)
        self.assertEqual(self.search.tree_state(tree)["reserved"], "1")
        self.executor.finish_run(run, status="failed", outputs={}, reason="Actual process failure")
        self.search.finish_selection(selected["id"], status="failed", actual_cost=1,
                                     reason="Observed terminal", run=run)
        with self.assertRaisesRegex(GateError, "already terminal"):
            self.search.finish_selection(selected["id"], status="failed", actual_cost=1,
                                         reason="Duplicate", run=run)

    def test_replication_selection_requires_a_real_replication_run(self):
        tree = self.tree()
        primary_node = self.node(tree)
        selected = self.search.select_next(tree)
        original = self.finish(selected)
        self.node(tree, parent=primary_node, protocol=selected["protocol"], action="replicate")
        replication = self.search.select_next(tree)
        wrong = self.execute_run(selected["protocol"])
        with self.assertRaisesRegex(GateError, "replication execution/action"):
            self.search.finish_selection(replication["id"], status="completed", actual_cost=1,
                                         reason="Wrong mode", run=wrong)
        run = self.execute_run(selected["protocol"], actor=Kernel(self.store, Actor("replicator", "replicator")),
                       replicate_of=original)
        self.search.finish_selection(replication["id"], status="completed", actual_cost=1,
                                     reason="Independent reanalysis", run=run)

    def test_tree_tournament_candidate_refs_are_enforced(self):
        tournament = self.search.register_tournament(candidates=self.hypotheses[1:], rubric="v1")
        tree = self.tree(tournament=tournament)
        with self.assertRaisesRegex(GateError, "belong to the tree tournament"):
            self.node(tree)

    def test_concurrent_selection_cannot_double_reserve_node_or_budget(self):
        tree = self.tree(budget=1, max_inflight=2)
        self.node(tree)
        self.node(tree)
        barrier = threading.Barrier(2)
        original_history = Search._history

        def synchronized(search):
            history = original_history(search)
            barrier.wait(timeout=8)
            return history

        def worker(id):
            with Store(self.directory.name) as store:
                try:
                    return Search(store, Actor(id, "planner")).select_next(tree)
                except ConflictError as exc:
                    return exc

        with patch.object(Search, "_history", synchronized):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(worker, id) for id in ("planner-A", "planner-B")]
                results = [future.result(timeout=15) for future in futures]
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1, results)
        self.assertEqual(sum(isinstance(r, ConflictError) for r in results), 1, results)
        state = self.search.tree_state(tree)
        self.assertEqual(state["reserved"], "1")
        self.assertEqual(self.search.select_next(tree)["reason"], "budget_exhausted")


if __name__ == "__main__":
    unittest.main()
