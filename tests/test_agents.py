"""Synthetic model transports run in real tiny workers; no model/API calls.

The patched worker is a fixture, not a Codex response or scientific approval.
These tests establish persistence and admission semantics, not proposal quality.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from episteme.agent_controller import advance_agent, reconcile_agent, work_agent
from episteme.agents import Agents, _index, agent_state
from episteme.commands import CommandService
from episteme.kernel import Actor
from episteme.recovery import backup, restore
from episteme.store import ConflictError, Store, canonical, digest


PROPOSAL = dict(
    status="proposed", reason="Synthetic transport fixture with two declared alternatives",
    candidates=[
        dict(statement="Fixture null: the offset is absent", prediction="Fixture mean is zero",
             falsifier="A stable nonzero fixture mean", kind="null"),
        dict(statement="Fixture mechanism: the offset is present", prediction="Fixture mean is positive",
             falsifier="A stable zero fixture mean", kind="mechanism"),
    ],
    comparison_plan="Compare a synthetic offset against a synthetic null control",
    limitations=["Transport fixture only; no scientific observations or approval"],
)
USAGE = dict(input_tokens=31, cached_input_tokens=0, output_tokens=19)


def stream_events(*, tool=False, completed=True):
    events = [dict(type="thread.started", thread_id="synthetic-fixture-thread"), dict(type="turn.started")]
    if tool:
        events.append(dict(type="item.completed", item=dict(type="command_execution", id="synthetic-tool",
                                                           command="fixture only; never executed")))
    events.append(dict(type="item.completed", item=dict(type="agent_message", id="synthetic-message",
                                                       text="Synthetic fixture proposal")))
    if completed:
        events.append(dict(type="turn.completed", usage=USAGE))
    return events


def fixture_program(proposal=PROPOSAL, *, tool=False, completed=True, exitcode=0):
    raw = canonical(proposal) if isinstance(proposal, dict) else proposal
    stdout = b"\n".join(canonical(event) for event in stream_events(tool=tool, completed=completed)) + b"\n"
    source = (
        "import pathlib, sys\n"
        f"pathlib.Path('proposal.json').write_bytes({raw!r})\n"
        f"sys.stdout.buffer.write({stdout!r})\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stderr.buffer.write(b'synthetic transport diagnostic\\n')\n"
        f"raise SystemExit({exitcode})\n"
    )
    return source.encode(), raw, stdout


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "research"
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.manager = Actor("synthetic-agent-manager", "planner")
        self.assignee = Actor("synthetic-hypothesis-agent", "planner")
        self.scope = dict(mode="synthetic_agent_transport_fixture")
        self.question_payload = dict(study_id="fixture-study", statement="Does the synthetic fixture have an offset?",
                                     objective="Exercise transport and competing hypothesis persistence",
                                     scope=self.scope, constraints=["Synthetic fixture only; no model calls"],
                                     stopping_criteria=["Stop after the bounded fixture proposal"])
        self.question = self.command("planning.question", self.question_payload)
        self.budget = self.command("agent.register_budget", dict(study_id="fixture-study", max_calls=1))
        # The fixture wrapper never launches this descriptor. No real provider is frozen or queried.
        self.provider = self.store.put_json(dict(schema_version=1, provider="codex_cli_v1", profile_version=1,
            executable=str(Path(sys.executable).resolve()), executable_sha256=digest(Path(sys.executable).read_bytes()),
            cli_version="codex-cli 0.0.0", model="synthetic-fixture-no-model", reasoning_effort="low"))

    def envelope(self, action, payload, *, actor=None, study="fixture-study", store=None):
        actor, store = actor or self.manager, store or self.store
        return dict(context=dict(command_id=uuid4().hex, expected_revision=len(store.events()), actor=actor.id,
                                 role=actor.role, study_id=study, correlation_id="synthetic-agent-cycle", causation_id=None),
                    request=dict(version=1, action=action, payload=deepcopy(payload)))

    def command(self, action, payload, **kwargs):
        envelope = self.envelope(action, payload, **kwargs)
        return CommandService(kwargs.get("store") or self.store).execute(envelope)

    def request(self, source=None, *, budget=None, question=None, **options):
        payload = dict(budget=budget or self.budget, question=question or self.question,
                       assignee=self.assignee.id, provider=self.provider, wall_seconds=10, max_output_bytes=65536)
        payload.update(options)
        envelope = self.envelope("agent.request_hypotheses", payload)
        with patch("episteme.agents.PROGRAM", fixture_program()[0] if source is None else source):
            return CommandService(self.store).execute(envelope), envelope

    def event(self, id):
        return next(event for event in self.store.events() if event["id"] == id)

    def workspace(self, request):
        state = _index(self.store, self.store.events())[request]
        return self.root / "agent-executions" / state["dispatch"]["payload"]["workspace_token"]

    def revise_question(self):
        return self.command("planning.question", dict(self.question_payload, parent=self.question,
            revision_reason="Synthetic fixture changes its declared question", statement="Revised synthetic fixture question"))

    def no_generated_research(self):
        self.assertFalse(any(event["kind"] in {"hypothesis", "explanation_set", "claim", "review", "paper"}
                             for event in self.store.events()))

    def test_actual_fixture_proposal_applies_atomically_and_replays_after_question_revision(self):
        request, request_envelope = self.request()
        self.assertEqual(CommandService(self.store).execute(request_envelope), request)
        self.assertEqual(agent_state(self.store, request)["status"], "queued")
        state = work_agent(self.store, request)
        self.assertEqual(state["status"], "proposed", state)
        self.assertEqual(state["usage"], USAGE)
        self.assertEqual(state["scientific_validity"], "not_assessed")
        self.no_generated_research()
        envelope = self.envelope("agent.apply_hypotheses", dict(request=request), actor=self.assignee)
        before = len(self.store.events())
        application = CommandService(self.store).execute(envelope)
        created = self.store.events()[before:]
        self.assertEqual([event["kind"] for event in created],
                         ["hypothesis", "hypothesis", "explanation_set", "agent_application"])
        receipt = next(row for row in self.store.receipts() if row["result"] == application)
        self.assertEqual(receipt["event_ids"], [event["id"] for event in created])
        self.assertEqual(created[-1]["payload"]["limitations"], PROPOSAL["limitations"])
        self.assertEqual([row["kind"] for row in created[-1]["payload"]["mapping"]], ["null", "mechanism"])
        self.assertTrue(all(event["payload"]["scope"] == self.scope for event in created[:2]))
        self.revise_question()
        before = self.store.export(), self.store.export_receipts()
        self.assertEqual(CommandService(self.store).execute(envelope), application)
        with patch("episteme.agent_controller.subprocess.Popen") as spawn:
            self.assertEqual(advance_agent(self.store, request)["status"], "applied")
            spawn.assert_not_called()
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))

    def test_apply_rolls_back_all_hypotheses_and_receipt_on_set_failure(self):
        request, _ = self.request()
        self.assertEqual(work_agent(self.store, request)["status"], "proposed")
        envelope = self.envelope("agent.apply_hypotheses", dict(request=request), actor=self.assignee)
        before = self.store.export(), self.store.export_receipts()
        with patch("episteme.agents.Planning.explanation_set", side_effect=ValueError("synthetic set failure")):
            with self.assertRaisesRegex(ValueError, "synthetic set failure"):
                CommandService(self.store).execute(envelope)
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))
        self.no_generated_research()
        CommandService(self.store).execute(envelope)
        self.assertEqual(agent_state(self.store, request)["status"], "applied")

    def test_concurrent_application_between_work_and_fresh_snapshot_is_idempotent(self):
        request, _ = self.request()
        def competing_controller(store, id):
            observed = work_agent(store, id)
            self.assertEqual(observed["status"], "proposed")
            self.command("agent.apply_hypotheses", dict(request=id), actor=self.assignee)
            return observed
        with patch("episteme.agent_controller.work_agent", side_effect=competing_controller):
            self.assertEqual(advance_agent(self.store, request)["status"], "applied")
        self.assertEqual(len([e for e in self.store.events() if e["kind"] == "agent_application"]), 1)
        self.assertEqual(len([e for e in self.store.events() if e["kind"] == "agent_dispatch"]), 1)

    def test_cli_status_work_reconcile_and_advance_read_applied_request_without_new_call(self):
        request, _ = self.request()
        expected = advance_agent(self.store, request)
        self.assertEqual(expected["status"], "applied")
        before = self.store.export(), self.store.export_receipts()
        for operation in ("status", "work", "reconcile", "advance"):
            with self.subTest(operation=operation):
                result = subprocess.run([sys.executable, "-m", "episteme", "agent", operation, request,
                                         "--root", str(self.root)], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertEqual(json.loads(result.stdout), expected)
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))
        self.assertEqual(len(list((self.root / "agent-executions").iterdir())), 1)

    def test_historical_profile_survives_current_default_and_exported_constant_changes(self):
        from episteme.agent_profiles import profile
        request, _ = self.request()
        expected = advance_agent(self.store, request)
        before = self.store.export(), self.store.export_receipts()
        _, schema, _ = profile("hypothesis-proposal-v1")
        schema.clear()  # Returned mutable schemas must not modify the registered v1 profile.
        with patch("episteme.agents.DEFAULT_PROFILE", "unregistered-future-default"), \
             patch("episteme.agent_profiles.DEFAULT_PROFILE", "unregistered-future-default"), \
             patch("episteme.agent_proposals.SYSTEM_PROMPT", "changed exported constant"), \
             patch("episteme.agent_proposals.STRUCTURED_OUTPUT_SCHEMA", {}):
            self.assertEqual(agent_state(self.store, request), expected)
            self.assertEqual(reconcile_agent(self.store, request), expected)
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))

    def test_stale_question_blocks_application_without_losing_original_response(self):
        request, _ = self.request()
        self.assertEqual(work_agent(self.store, request)["status"], "proposed")
        original = deepcopy(self.event(agent_state(self.store, request)["response"]))
        self.revise_question()
        before = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "head|superseded|current"):
            self.command("agent.apply_hypotheses", dict(request=request), actor=self.assignee)
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))
        self.assertEqual(self.event(original["id"]), original)
        self.assertEqual(agent_state(self.store, request)["status"], "proposed")
        self.no_generated_research()

    def test_stale_question_is_rejected_before_new_request_is_admitted(self):
        self.revise_question()
        before = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "head|superseded|current"):
            self.request()
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))

    def test_stale_question_blocks_queued_dispatch_before_external_work(self):
        request, _ = self.request()
        self.revise_question()
        before = self.store.export(), self.store.export_receipts()
        with patch("episteme.agent_controller.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(ValueError, "head|superseded|current"):
                work_agent(self.store, request)
            spawn.assert_not_called()
        self.assertEqual(agent_state(self.store, request)["status"], "queued")
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))

    def test_wrong_actor_role_and_study_are_rejected_before_dispatch(self):
        request, _ = self.request()
        before = self.store.export(), self.store.export_receipts()
        cases = [dict(actor=self.manager), dict(actor=Actor(self.assignee.id, "executor")),
                 dict(actor=self.assignee, study="other-study")]
        for context in cases:
            with self.subTest(context=context), self.assertRaises(ValueError):
                self.command("agent.dispatch", dict(request=request, workspace_token=uuid4().hex), **context)
        with self.assertRaisesRegex(ValueError, "study"):
            self.command("agent.register_budget", dict(study_id="other-study", max_calls=1))
        with self.assertRaisesRegex(ValueError, "CommandService"):
            Agents(self.store, self.assignee).dispatch(request=request, workspace_token=uuid4().hex)
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))

    def test_wrong_study_request_rejects_matching_foreign_question_and_budget(self):
        foreign = dict(self.question_payload, study_id="other-study")
        question = self.command("planning.question", foreign, study="other-study")
        budget = self.command("agent.register_budget", dict(study_id="other-study", max_calls=1), study="other-study")
        before = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "study"):
            self.request(question=question, budget=budget)
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))

    def test_budget_cannot_be_reused_for_a_failed_or_unknown_attempt(self):
        request, _ = self.request(fixture_program(exitcode=3)[0])
        self.assertEqual(work_agent(self.store, request)["status"], "failed")
        before = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            self.request()
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))
        other = self.command("agent.register_budget", dict(study_id="fixture-study", max_calls=1))
        pending, _ = self.request(budget=other)
        self.command("agent.dispatch", dict(request=pending, workspace_token=uuid4().hex), actor=self.assignee)
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            self.request(budget=other)

    def test_two_writers_cannot_admit_two_calls_under_one_budget(self):
        payload = dict(budget=self.budget, question=self.question, assignee=self.assignee.id,
                       provider=self.provider, wall_seconds=10, max_output_bytes=65536)
        envelopes = [self.envelope("agent.request_hypotheses", payload) for _ in range(2)]
        barrier = threading.Barrier(2)
        def attempt(envelope):
            with Store(self.root) as connection:
                barrier.wait(timeout=10)
                try:
                    return CommandService(connection).execute(envelope)
                except ConflictError:
                    return "conflict"
        with patch("episteme.agents.PROGRAM", fixture_program()[0]), ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(attempt, envelopes))
        self.assertEqual(results.count("conflict"), 1)
        self.assertEqual(len([event for event in self.store.events() if event["kind"] == "agent_request"]), 1)

    def test_abstention_retains_all_bytes_without_placeholder_hypotheses(self):
        abstention = dict(status="abstained", reason="Synthetic fixture lacks information", candidates=[],
                          comparison_plan="", limitations=["Missing fixture observations"])
        source, proposal, stdout = fixture_program(abstention)
        request, _ = self.request(source)
        state = advance_agent(self.store, request)
        self.assertEqual(state["status"], "abstained")
        assessment = self.event(state["response"])["payload"]["assessment"]
        self.assertEqual(self.store.read(assessment["artifacts"]["proposal"]), proposal)
        self.assertEqual(self.store.read(assessment["artifacts"]["stdout"]), stdout)
        self.assertEqual(self.store.read(assessment["artifacts"]["stderr"]), b"synthetic transport diagnostic\n")
        self.assertEqual(assessment["usage"], USAGE)
        with self.assertRaisesRegex(ValueError, "not applicable"):
            self.command("agent.apply_hypotheses", dict(request=request), actor=self.assignee)
        self.no_generated_research()

    def test_malformed_proposal_is_terminal_invalid_and_raw_bytes_survive(self):
        invalid = b'{"status":"proposed","status":"abstained"}'
        request, _ = self.request(fixture_program(invalid)[0])
        state = advance_agent(self.store, request)
        self.assertEqual(state["status"], "invalid")
        assessment = self.event(state["response"])["payload"]["assessment"]
        self.assertEqual(assessment["transport_status"], "completed")
        self.assertTrue(assessment["errors"])
        self.assertEqual(self.store.read(assessment["artifacts"]["proposal"]), invalid)
        self.assertEqual(assessment["usage"], USAGE)
        self.no_generated_research()

    def test_tool_item_invalidates_proposal_but_preserves_later_reported_usage(self):
        request, _ = self.request(fixture_program(tool=True)[0])
        state = advance_agent(self.store, request)
        self.assertEqual(state["status"], "invalid")
        assessment = self.event(state["response"])["payload"]["assessment"]
        self.assertTrue(any("tool" in message for message in assessment["errors"]))
        self.assertEqual(assessment["usage"], USAGE)
        self.no_generated_research()

    def test_failed_transport_without_usage_retains_unknown_usage_as_null(self):
        request, _ = self.request(fixture_program(completed=False, exitcode=4)[0])
        state = advance_agent(self.store, request)
        self.assertEqual(state["status"], "failed")
        self.assertIsNone(state["usage"])
        response = self.event(state["response"])["payload"]
        self.assertEqual(response["assessment"]["transport_status"], "failed")
        manifest = json.loads(self.store.read(response["manifest"]))
        self.assertEqual(manifest["returncode"], 4)
        self.no_generated_research()

    def test_timeout_missing_output_and_capture_overflow_preserve_failed_attempts(self):
        sources = {
            "timeout": b"import time\ntime.sleep(30)\n",
            "missing_output": b"print('Synthetic worker omitted its required proposal')\n",
            "capture_overflow": b"print('x' * 100000)\n",
        }
        for name, source in sources.items():
            with self.subTest(name=name):
                budget = self.command("agent.register_budget", dict(study_id="fixture-study", max_calls=1))
                request, _ = self.request(source, budget=budget, wall_seconds=1, max_output_bytes=4096)
                state = advance_agent(self.store, request)
                self.assertEqual(state["status"], "failed", state)
                response = self.event(state["response"])["payload"]
                self.assertEqual(response["assessment"]["transport_status"], "failed")
                self.assertIsNone(response["assessment"]["usage"])
                self.assertTrue(response["assessment"]["errors"])
                manifest = json.loads(self.store.read(response["manifest"]))
                self.assertTrue(manifest["termination_confirmed"])
                self.assertTrue(manifest["reason"])
                self.assertTrue(all(len(self.store.read(key)) <= 4096
                                    for key in response["assessment"]["artifacts"].values()))
                with self.assertRaisesRegex(ValueError, "budget exhausted"):
                    self.request(budget=budget)
        self.no_generated_research()

    def test_malformed_stream_order_cannot_be_applied_even_with_valid_proposal(self):
        events = stream_events()
        # All expected objects exist, but the final response precedes turn admission.
        events[1], events[2] = events[2], events[1]
        stdout = b"\n".join(canonical(event) for event in events) + b"\n"
        source = ("import pathlib, sys\n"
                  f"pathlib.Path('proposal.json').write_bytes({canonical(PROPOSAL)!r})\n"
                  f"sys.stdout.buffer.write({stdout!r})\n").encode()
        request, _ = self.request(source)
        state = advance_agent(self.store, request)
        self.assertEqual(state["status"], "invalid")
        assessment = self.event(state["response"])["payload"]["assessment"]
        self.assertTrue(assessment["errors"])
        self.assertEqual(assessment["usage"], USAGE)
        self.no_generated_research()

    def test_dispatch_intent_before_spawn_failure_never_authorizes_a_second_launch(self):
        request, _ = self.request()
        with patch("episteme.agent_controller.subprocess.Popen", side_effect=OSError("synthetic launch failure")):
            with self.assertRaisesRegex(OSError, "synthetic launch failure"):
                work_agent(self.store, request)
        self.assertEqual(agent_state(self.store, request)["status"], "unknown")
        before = self.store.export(), self.store.export_receipts()
        with patch("episteme.agent_controller.subprocess.Popen") as spawn:
            self.assertEqual(work_agent(self.store, request)["status"], "unknown")
            self.assertEqual(reconcile_agent(self.store, request)["status"], "unknown")
            spawn.assert_not_called()
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))
        self.assertIsNone(agent_state(self.store, request)["response"])
        self.no_generated_research()

    def test_completion_after_controller_interruption_reconciles_without_respawn(self):
        request, _ = self.request()
        with patch("episteme.agent_controller.reconcile_agent", side_effect=RuntimeError("synthetic controller interruption")):
            with self.assertRaisesRegex(RuntimeError, "synthetic controller interruption"):
                work_agent(self.store, request)
        self.assertEqual(agent_state(self.store, request)["status"], "unknown")
        with Store(self.root) as reopened, patch("episteme.agent_controller.subprocess.Popen") as spawn:
            self.assertEqual(reconcile_agent(reopened, request)["status"], "proposed")
            spawn.assert_not_called()
        self.assertEqual(len([event for event in self.store.events() if event["kind"] == "agent_dispatch"]), 1)

    def test_changed_completion_output_rejected_until_original_bytes_restored(self):
        request, _ = self.request()
        with patch("episteme.agent_controller.reconcile_agent", return_value={}):
            work_agent(self.store, request)
        output = self.workspace(request) / "proposal.json"
        original = output.read_bytes()
        output.write_bytes(original + b" ")
        before = self.store.export(), self.store.export_receipts()
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            reconcile_agent(self.store, request)
        self.assertEqual(before, (self.store.export(), self.store.export_receipts()))
        self.assertEqual(agent_state(self.store, request)["status"], "unknown")
        output.write_bytes(original)
        self.assertEqual(reconcile_agent(self.store, request)["status"], "proposed")

    def test_completion_identity_cannot_be_swapped(self):
        request, _ = self.request()
        with patch("episteme.agent_controller.reconcile_agent", return_value={}):
            work_agent(self.store, request)
        path = self.workspace(request) / "completion.json"
        original = path.read_bytes()
        completion = json.loads(original)
        completion["identity"]["request"] = "different-request"
        path.write_bytes(canonical(completion))
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            reconcile_agent(self.store, request)
        self.assertEqual(agent_state(self.store, request)["status"], "unknown")
        path.write_bytes(original)
        self.assertEqual(reconcile_agent(self.store, request)["status"], "proposed")

    def test_backup_restored_queued_request_has_no_dispatch_authority(self):
        request, _ = self.request()
        snapshot, target = Path(self.temp.name) / "backup", Path(self.temp.name) / "restored"
        backup(self.store, snapshot)
        restore(snapshot, target)
        with Store(target) as recovered, patch("episteme.agent_controller.subprocess.Popen") as spawn:
            before = recovered.export(), recovered.export_receipts()
            self.assertEqual(agent_state(recovered, request)["status"], "queued")
            with self.assertRaisesRegex(ValueError, "authority|origin|marker"):
                work_agent(recovered, request)
            self.assertEqual(before, (recovered.export(), recovered.export_receipts()))
            spawn.assert_not_called()
        self.assertEqual(agent_state(self.store, request)["status"], "queued")


if __name__ == "__main__":
    unittest.main()
