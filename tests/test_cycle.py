"""The research-cycle controller stops for science and does not double a step.

Every store here is a synthetic pack fixture. A fixture approval is a recorded
test opinion. Reaching paper_candidate because of one does not make the claim
scientifically valid, and these tests do not treat it as a result.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from episteme.analysis_controller import advance_pack_analysis
from episteme.batch_controller import advance_batch
from episteme.cli import main
from episteme.commands import CommandService
from episteme.cycle import cycle_step
from episteme.kernel import Actor, Kernel
from episteme.store import Store
from review_paths import FIXTURE_RATIONALE, approve, deliver, submit_review
from test_pack_workflow import (
    ANALYST, PLANNER, REVIEWER, STUDY, command, plan_batch, planning, preregistration, search_node)


def _runs(store: Store) -> list[dict]:
    return [event for event in store.events() if event["kind"] == "run"]


def _dispatches(store: Store) -> list[dict]:
    return [event for event in store.events() if event["kind"] == "execution_dispatch"]


class _Dead:
    def wait(self) -> int:
        return 0


_REAL_POPEN = subprocess.Popen


def _failing_worker(argv, **kwargs):
    """Run the real worker after changing a frozen input so the attempt fails."""
    workspace = Path(argv[-1])
    program = workspace / "program.py"
    program.write_bytes(program.read_bytes() + b"\n")
    return _REAL_POPEN(argv, **kwargs)


class CycleControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temporary = TemporaryDirectory(prefix="episteme-cycle-")
        cls.base = Path(cls._temporary.name)
        with Store(cls.base / "planned") as store:
            explanation = planning(store)
            registered = command(store, "pack.preregister", preregistration(store, explanation))
            cls.protocol = registered["protocol"]
            binding = Kernel._get(store.events(), registered["binding"], "pack_binding")
            tree = search_node(store, cls.protocol, 2)
            cls.batch = plan_batch(store, binding, tree)
        shutil.copytree(cls.base / "planned", cls.base / "completed")
        with Store(cls.base / "completed") as store:
            assert advance_batch(store, cls.batch)["status"] == "completed"

    @classmethod
    def tearDownClass(cls):
        cls._temporary.cleanup()

    def copy(self, name: str) -> Store:
        temporary = TemporaryDirectory(prefix="episteme-cycle-case-")
        self.addCleanup(temporary.cleanup)
        target = Path(temporary.name) / "state"
        shutil.copytree(self.base / name, target)
        store = Store(target)
        self.addCleanup(store.close)
        return store

    def test_report_only_writes_nothing(self):
        store = self.copy("planned")
        before = store.export(), store.export_receipts()
        reported = cycle_step(store, study=STUDY, apply=False, budget=1)
        self.assertEqual(reported["recommendation"], "batch.enqueue_slot")
        self.assertTrue(reported["report_only"])
        self.assertFalse(reported["applied"])
        self.assertIsNone(reported["command"])
        self.assertEqual(reported["scientific_validity"], "not_assessed")
        omitted = cycle_step(store, study=STUDY, apply=True, budget=None)
        self.assertEqual(omitted["stop"], "missing_budget")
        self.assertTrue(omitted["report_only"])
        self.assertFalse(omitted["applied"])
        self.assertEqual((store.export(), store.export_receipts()), before)

    def test_apply_one_legal_step_and_a_second_invocation_does_not_double_it(self):
        store = self.copy("completed")
        before = store.export()
        preview = cycle_step(store, study=STUDY, apply=False)
        self.assertEqual(preview["recommendation"], "pack.analyse")
        self.assertEqual(store.export(), before)
        first = cycle_step(store, study=STUDY, apply=True, budget=0,
                           analyst=ANALYST.id, reviewer=REVIEWER)
        self.assertTrue(first["applied"])
        self.assertEqual(first["command"]["action"], "pack.analyse")
        self.assertEqual(first["budget"]["consumed_attempts"], 0)
        self.assertEqual(first["scientific_validity"], "not_assessed")
        self.assertEqual(len([e for e in store.events() if e["kind"] == "pack_analysis"]), 1)
        self.assertTrue(all(event["payload"].get("scientific_validity", "not_assessed") == "not_assessed"
                            for event in store.events()))
        receipt = next(item for item in store.receipts()
                       if item["command_id"] == first["command"]["command_id"])
        frozen = store.export(), store.export_receipts()
        replayed = CommandService(store).execute(dict(context=receipt["context"], request=receipt["request"]))
        self.assertEqual(replayed["analysis"], receipt["result"]["analysis"])
        self.assertEqual((store.export(), store.export_receipts()), frozen)
        second = cycle_step(store, study=STUDY, apply=True, budget=0,
                            analyst=ANALYST.id, reviewer=REVIEWER)
        self.assertTrue(second["applied"])
        self.assertEqual(second["command"]["action"], "review.assign")
        self.assertEqual(len([e for e in store.events() if e["kind"] == "pack_analysis"]), 1)
        self.assertEqual(len([e for e in store.events() if e["kind"] == "review_assignment"]), 1)
        third = cycle_step(store, study=STUDY, apply=True, budget=0,
                           analyst=ANALYST.id, reviewer=REVIEWER)
        self.assertEqual(third["stop"], "human_scientific_input")
        self.assertIn("review verdict", third["reason"])
        self.assertIn("does not submit a review", third["reason"])
        self.assertFalse(third["applied"])
        self.assertEqual(len([e for e in store.events() if e["kind"] == "review_assignment"]), 1)
        self.assertFalse(any(event["kind"] in {"review", "paper"} for event in store.events()))

    def test_budget_exhaustion_stops_before_another_attempt(self):
        store = self.copy("planned")
        blocked = cycle_step(store, study=STUDY, apply=True, budget=0)
        self.assertEqual(blocked["stop"], "budget_exhausted")
        self.assertEqual(blocked["recommendation"], "batch.enqueue_slot")
        self.assertFalse(blocked["applied"])
        self.assertEqual(_runs(store), [])
        started = cycle_step(store, study=STUDY, apply=True, budget=1)
        self.assertTrue(started["applied"])
        self.assertEqual(started["command"]["action"], "batch.enqueue_slot")
        self.assertEqual(started["budget"]["consumed_attempts"], 1)
        self.assertEqual(len(_runs(store)), 1)
        exhausted = cycle_step(store, study=STUDY, apply=True, budget=0)
        self.assertEqual(exhausted["stop"], "budget_exhausted")
        self.assertEqual(exhausted["recommendation"], "execution.dispatch")
        self.assertFalse(exhausted["applied"])
        self.assertEqual(len(_runs(store)), 1)
        self.assertEqual(_dispatches(store), [])

    def test_open_obligation_and_open_veto_stop_the_cycle(self):
        obligation = self.copy("completed")
        admitted = advance_pack_analysis(obligation, self.batch, planner=PLANNER, analyst=ANALYST,
                                         reviewer_actor=REVIEWER)
        submit_review(obligation, admitted["claim"], reviewer="fixture-reviewer", verdict="reject",
                      study=STUDY, findings=[dict(
                          kind="narrow_claim", action="Narrow the fixture statement",
                          closure_criterion="A human revises the recorded statement",
                          evidence_refs=[admitted["claim"]])])
        frozen = obligation.export()
        report = cycle_step(obligation, claim=admitted["claim"], apply=True, budget=1)
        self.assertEqual(report["stop"], "open_obligation")
        self.assertIn("does not design a follow-up", report["reason"])
        self.assertFalse(report["applied"])
        self.assertEqual(report["scientific_validity"], "not_assessed")
        self.assertEqual(obligation.export(), frozen)
        self.assertFalse(any(event["kind"] in {"replan_followup", "paper"} for event in obligation.events()))

        veto = self.copy("completed")
        admitted = advance_pack_analysis(veto, self.batch, planner=PLANNER, analyst=ANALYST,
                                         reviewer_actor=REVIEWER)
        deliver(veto, admitted["assignment"], dict(
            verdict="reject", rationale=FIXTURE_RATIONALE, findings=[], link_assessments=None),
            planner=PLANNER.id, study=STUDY)
        frozen = veto.export()
        report = cycle_step(veto, study=STUDY, apply=True, budget=1)
        self.assertEqual(report["stop"], "open_veto")
        self.assertIn("does not withdraw", report["reason"])
        self.assertFalse(report["applied"])
        self.assertEqual(veto.export(), frozen)

    def test_paper_candidate_stops_and_names_the_synthetic_fixture_approval(self):
        store = self.copy("completed")
        admitted = advance_pack_analysis(store, self.batch, planner=PLANNER, analyst=ANALYST,
                                         reviewer_actor=REVIEWER)
        approve(store, admitted["claim"], reviewer=REVIEWER, study=STUDY)
        claim = Kernel._get(store.events(), admitted["claim"], "claim")
        outcome = claim["payload"]["outcome"]
        frozen = store.export()
        report = cycle_step(store, claim=admitted["claim"], apply=True, budget=0)
        self.assertEqual(report["stop"], "paper_candidate")
        self.assertTrue(report["fixture_approval"])
        self.assertIn("synthetic fixture opinion", report["reason"])
        self.assertIn("not a scientific result", report["reason"])
        self.assertIn("scientific_validity remains not_assessed", report["reason"])
        self.assertEqual(report["scientific_validity"], "not_assessed")
        self.assertFalse(report["applied"])
        self.assertEqual(store.export(), frozen)
        self.assertFalse(any(event["kind"] == "paper" for event in store.events()))
        self.assertEqual(Kernel._get(store.events(), admitted["claim"], "claim")["payload"]["outcome"], outcome)
        self.assertEqual(Kernel(store, Actor("cycle-reader", "observer")).next_action(admitted["claim"])["action"],
                         "paper_candidate")

    def test_unknown_or_failed_attempt_is_not_rerun(self):
        unknown = self.copy("planned")
        enqueued = cycle_step(unknown, study=STUDY, apply=True, budget=1)
        self.assertEqual(enqueued["command"]["action"], "batch.enqueue_slot")
        with patch("episteme.cycle.subprocess.Popen", return_value=_Dead()):
            launched = cycle_step(unknown, study=STUDY, apply=True, budget=1)
        self.assertEqual(launched["command"]["action"], "execution.dispatch")
        self.assertEqual(len(_runs(unknown)), 1)
        self.assertEqual(len(_dispatches(unknown)), 1)
        frozen = unknown.export()
        with patch("episteme.cycle.subprocess.Popen", return_value=_Dead()) as spawn:
            again = cycle_step(unknown, study=STUDY, apply=True, budget=1)
        spawn.assert_not_called()
        self.assertEqual(again["stop"], "blocked")
        self.assertIn("unknown attempt", again["reason"])
        self.assertIn("not rerun", again["reason"])
        self.assertFalse(again["applied"])
        self.assertEqual(unknown.export(), frozen)

        failed = self.copy("planned")
        cycle_step(failed, study=STUDY, apply=True, budget=1)
        with patch("episteme.cycle.subprocess.Popen", side_effect=_failing_worker):
            cycle_step(failed, study=STUDY, apply=True, budget=1)
        self.assertEqual(len(_runs(failed)), 1)
        self.assertEqual(len(_dispatches(failed)), 1)
        with patch("episteme.cycle.subprocess.Popen") as spawn:
            finalized = cycle_step(failed, study=STUDY, apply=True, budget=0)
            settled = cycle_step(failed, study=STUDY, apply=True, budget=1)
            stopped = cycle_step(failed, study=STUDY, apply=True, budget=1)
        spawn.assert_not_called()
        self.assertEqual(finalized["command"]["action"], "execution.finalize")
        self.assertEqual(finalized["budget"]["consumed_attempts"], 0)
        self.assertEqual(settled["command"]["action"], "batch.settle")
        self.assertEqual(stopped["stop"], "blocked")
        self.assertIn("not rerun", stopped["reason"])
        self.assertFalse(stopped["applied"])
        self.assertEqual(len(_runs(failed)), 1)
        self.assertEqual(len(_dispatches(failed)), 1)
        result = next(event for event in failed.events() if event["kind"] == "result")
        settlement = next(event for event in failed.events() if event["kind"] == "batch_settlement")
        self.assertEqual(result["payload"]["status"], "failed")
        self.assertEqual(settlement["payload"]["status"], "failed")
        self.assertEqual(settlement["payload"]["scientific_validity"], "not_assessed")
        self.assertEqual(stopped["scientific_validity"], "not_assessed")

    def test_a_study_without_a_prepared_batch_stops_for_a_human_experiment(self):
        temporary = TemporaryDirectory(prefix="episteme-cycle-question-")
        self.addCleanup(temporary.cleanup)
        with Store(Path(temporary.name) / "state") as store:
            planning(store)
            before = store.export()
            report = cycle_step(store, study=STUDY, apply=True, budget=1)
            self.assertEqual(report["stop"], "human_scientific_input")
            self.assertIn("does not invent a protocol", report["reason"])
            self.assertIn("choose among competing experiments", report["reason"])
            self.assertFalse(report["applied"])
            self.assertEqual(store.export(), before)

    def test_confirmatory_claim_stops_for_human_interpretation(self):
        from test_tabular_pack import ANALYST as tabular_analyst
        from test_tabular_pack import PLANNER as tabular_planner
        from test_tabular_pack import REVIEWER as tabular_reviewer
        from test_tabular_pack import STUDY as tabular_study
        from test_tabular_pack import bind, run_batch
        temporary = TemporaryDirectory(prefix="episteme-cycle-tabular-")
        self.addCleanup(temporary.cleanup)
        with Store(Path(temporary.name) / "state") as store:
            registered, _captured = bind(store, "planted")
            batch = run_batch(store, registered["protocol"])
            admitted = advance_pack_analysis(store, batch, planner=tabular_planner,
                                             analyst=tabular_analyst, reviewer_actor=tabular_reviewer)
            claim = Kernel._get(store.events(), admitted["claim"], "claim")
            self.assertEqual(claim["payload"]["inference_mode"], "confirmatory")
            frozen = store.export()
            report = cycle_step(store, claim=admitted["claim"], apply=True, budget=1)
            self.assertEqual(report["stop"], "human_scientific_input")
            self.assertIn("confirmatory interpretation", report["reason"])
            self.assertIn("does not raise the claim outcome", report["reason"])
            self.assertEqual(report["scientific_validity"], "not_assessed")
            self.assertFalse(report["applied"])
            self.assertEqual(store.export(), frozen)
            self.assertFalse(any(event["kind"] in {"review", "paper"} for event in store.events()))

    def test_cli_report_does_not_write(self):
        temporary = TemporaryDirectory(prefix="episteme-cycle-cli-")
        self.addCleanup(temporary.cleanup)
        target = Path(temporary.name) / "state"
        shutil.copytree(self.base / "planned", target)
        with Store(target) as store:
            before = store.export(), store.export_receipts()
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["cycle", "step", "--root", str(target), "--study", STUDY])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn("batch.enqueue_slot", stdout.getvalue())
        self.assertIn("not_assessed", stdout.getvalue())
        with Store(target) as store:
            self.assertEqual((store.export(), store.export_receipts()), before)


if __name__ == "__main__":
    unittest.main()
