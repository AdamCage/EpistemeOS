"""Retryable local transitions; opinions and outputs here are explicit fixtures."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from episteme.commands import CommandService, parse_command
from episteme.kernel import Actor, GateError, Kernel
from episteme.reporting import PaperBuilder
from episteme.search import Search
from episteme.store import ConflictError, Store


class CommandServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="episteme-command-service-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.service = CommandService(self.store)
        self.scope = {"population": "command test fixture"}

    def envelope(self, action, payload, *, actor="planner", role="planner", id="command-1"):
        return dict(context=dict(command_id=id, expected_revision=len(self.store.events()),
                    actor=actor, role=role, study_id="fixture-study", correlation_id="fixture-cycle",
                    causation_id=None), request=dict(version=1, action=action, payload=payload))

    def protocol(self):
        planner = Kernel(self.store, Actor("planner", "planner"))
        hypotheses = [planner.hypothesis(name, name, name, self.scope) for name in ("signal", "null")]
        self.code = self.store.put(b"primary code fixture")
        self.recode = self.store.put(b"reanalysis code fixture")
        self.environment = self.store.put(b"environment fixture")
        self.raw = self.store.put(b"value\n-1\n1\n")
        self.outputs = dict(raw_data=self.raw, metrics=self.store.put_json({"mean": 0.0}),
                            log=self.store.put(b"Fixture result, no actual computation"))
        return planner.preregister(hypotheses=hypotheses, scope=self.scope,
            design="Two-point mean fixture", metric="mean", analysis_plan="All rows",
            stopping_rule="Fixed fixture", seeds=[7], run_limit=6,
            implementation=self.code, environment=self.environment, data=self.raw,
            replication_tolerance=0.0)

    def run_payload(self, protocol):
        return dict(protocol=protocol, seed=7, implementation=self.code,
                    environment=self.environment, command=["fixture"])

    def test_reopen_replay_and_changed_request_conflict(self):
        envelope = self.envelope("kernel.hypothesis", dict(statement="signal", prediction="mean>0",
                                  falsifier="mean<=0", scope=self.scope))
        result = self.service.execute(envelope)
        Kernel(self.store, Actor("planner", "planner")).hypothesis("null", "null", "signal", self.scope)
        with Store(self.root) as reopened:
            self.assertEqual(CommandService(reopened).execute(envelope), result)
            self.assertEqual(len(reopened.events()), 2)
            self.assertEqual(len(reopened.receipts()), 1)
            changed = copy.deepcopy(envelope)
            changed["request"]["payload"]["statement"] = "another hypothesis"
            with self.assertRaises(ConflictError):
                CommandService(reopened).execute(changed)

    def test_invalid_admission_never_calls_paper_or_writes_blobs(self):
        envelope = self.envelope("paper.build", dict(title="fixture", claims=[], expected_bases={}))
        with patch.object(Store, "put", side_effect=AssertionError("must not write before role admission")):
            with self.assertRaisesRegex(ValueError, "cannot execute"):
                self.service.execute(envelope)
        for change in (dict(action="store.close"), dict(version=True),
                       dict(action="kernel.hypothesis", payload={"unexpected": 1})):
            invalid = copy.deepcopy(envelope)
            invalid["request"].update(change)
            with self.assertRaises(ValueError):
                self.service.execute(invalid)
        self.assertEqual(self.store.events(), [])
        self.assertEqual(self.store.receipts(), [])

    def test_strict_json_rejects_duplicate_keys_and_python_coercions(self):
        for text in ('{"context":{},"context":{}}', '{"number":NaN}', '{"number":1e999}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_command(text)
        for invalid in ({"context": {1: "not a string key"}}, {"request": (1, 2)}):
            with self.assertRaises(ValueError):
                self.service.execute(invalid)

    def test_payload_type_errors_fail_before_kernel_and_exposure_replays(self):
        invalid = self.envelope("kernel.claim", dict(protocol="missing", statement=42,
            scope=self.scope, evidence=[], limitations=["fixture"], outcome="inconclusive"),
            actor="analyst", role="analyst")
        with self.assertRaisesRegex(ValueError, "payload type for statement"):
            self.service.execute(invalid)
        data = self.store.put(b"previewed development fixture")
        exposure = self.envelope("kernel.expose_data", dict(data=data, purpose="Development preview"))
        first = self.service.execute(exposure)
        self.assertEqual(self.service.execute(exposure), first)
        self.assertEqual(len(self.store.events()), 1)
        self.assertEqual(self.store.events()[0]["kind"], "data_exposure")

    def test_public_transport_example_executes_without_external_work(self):
        path = Path(__file__).resolve().parents[1] / "schemas" / "command-v1.schema.json"
        example = json.loads(path.read_text(encoding="utf-8"))["examples"][0]
        result = self.service.execute(example)
        self.assertEqual(self.store.events()[0]["id"], result)
        self.assertEqual(self.store.events()[0]["kind"], "hypothesis")

    def test_start_and_terminal_replays_do_not_retry_finished_run(self):
        protocol = self.protocol()
        start = self.envelope("kernel.start_run", self.run_payload(protocol),
                              actor="executor", role="executor", id="start")
        run = self.service.execute(start)
        finish = self.envelope("kernel.finish_run", dict(run=run, status="completed", outputs=self.outputs),
                               actor="executor", role="executor", id="finish")
        terminal = self.service.execute(finish)
        count = len(self.store.events())
        # Explicit method defaults and omitted defaults are the same normalized request.
        start["request"]["payload"]["replicate_of"] = None
        finish["request"]["payload"]["reason"] = ""
        self.assertEqual(self.service.execute(start), run)
        self.assertEqual(self.service.execute(finish), terminal)
        self.assertEqual(len(self.store.events()), count)
        self.assertEqual(len(self.store.receipts()), 2)

    def test_selection_replay_preserves_one_budget_reservation(self):
        protocol = self.protocol()
        search = Search(self.store, Actor("planner", "planner"))
        components = dict(discrimination=1.0, uncertainty=1.0, coverage=1.0, invalidity_risk=0.0)
        tree = search.register_tree(weights=components, cost_weight=0.0, budget=2.0,
            cost_unit="fixture units", max_nodes=2, max_depth=1, max_width=2, max_selections=2)
        search.add_node(tree, protocol=protocol, action="baseline", components=components,
                        estimated_cost=2.0, rationale="Fixture selection")
        command = self.envelope("search.select_next", dict(tree=tree))
        first = self.service.execute(command)
        self.assertIsNotNone(first["node"])
        state = search.tree_state(tree)
        first["node"] = "caller mutation must not change the receipt"
        repeated = self.service.execute(command)
        self.assertNotEqual(repeated["node"], first["node"])
        self.assertEqual(search.tree_state(tree), state)
        self.assertEqual(len([e for e in self.store.events() if e["kind"] == "search_selection"]), 1)

    def test_historical_review_and_paper_replay_does_not_reapprove_new_evidence(self):
        protocol = self.protocol()
        executor = Kernel(self.store, Actor("executor", "executor"))
        primary = executor.start_run(**self.run_payload(protocol))
        executor.finish_run(primary, status="completed", outputs=self.outputs)
        replicator = Kernel(self.store, Actor("replicator", "replicator"))
        replica = replicator.start_run(protocol, seed=7, implementation=self.recode,
            environment=self.environment, command=["fixture"], replicate_of=primary)
        replicator.finish_run(replica, status="completed", outputs=self.outputs)
        claim = executor.claim(protocol=protocol, statement="Fixture mean is zero", scope=self.scope,
            evidence=[primary, replica], limitations=["Test fixture only"], outcome="inconclusive")
        basis = executor.gate(claim)["basis_hash"]
        review = self.envelope("kernel.review", dict(claim=claim, verdict="approve",
            rationale="Test fixture opinion, not scientific approval", actions=[], expected_basis=basis),
            actor="reviewer", role="reviewer", id="review")
        review_id = self.service.execute(review)
        paper = self.envelope("paper.build", dict(title="Fixture scaffold", claims=[claim],
            expected_bases={claim: basis}), actor="writer", role="writer", id="paper")
        paper_id = self.service.execute(paper)
        executor.start_run(**self.run_payload(protocol))  # New incomplete evidence invalidates eligibility.
        history = self.store.events()
        with patch.object(Store, "put", side_effect=AssertionError("replay must not create artifacts")):
            self.assertEqual(self.service.execute(review), review_id)
            self.assertEqual(self.service.execute(paper), paper_id)
        self.assertEqual(self.store.events(), history)
        self.assertEqual(executor.next_action(claim)["action"], "repair_evidence")
        with self.assertRaises(GateError):
            PaperBuilder(self.store, Actor("writer", "writer")).materialize(paper_id)

    def test_real_cli_reopens_same_command_and_reads_receipts(self):
        command = self.envelope("kernel.hypothesis", dict(statement="signal", prediction="positive",
                                 falsifier="nonpositive", scope=self.scope))
        path = self.root / "command.json"
        path.write_text(json.dumps(command), encoding="utf-8")
        project = Path(__file__).resolve().parents[1]

        def cli(*args):
            process = subprocess.run([sys.executable, "-m", "episteme", *args, "--root", str(self.root)],
                cwd=project, env=dict(os.environ, PYTHONPATH=str(project / "src")),
                text=True, capture_output=True, encoding="utf-8", timeout=30)
            self.assertEqual(process.returncode, 0, process.stderr)
            return json.loads(process.stdout)

        first = cli("command", "--input", str(path))
        self.assertEqual(cli("command", "--input", str(path)), first)
        receipts = cli("receipts")["receipts"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["result"], first["result"])
        self.assertEqual(len(self.store.events()), 1)


if __name__ == "__main__":
    unittest.main()
