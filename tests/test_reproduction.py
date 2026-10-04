"""episteme reproduce (ADR 0019): recorded same-code replays that never count as evidence.

Programs are small offline fixtures. A matched reproduction shows that the
same bytes ran again to the same outputs; it is not independent replication
and not a scientific assessment. No reviewer verdict is created here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import locked_support as fx
from episteme import execution_locked as locked
from episteme.commands import CommandService
from episteme.execution import _index, freeze_environment, work_job
from episteme.graph import ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.recovery import backup, restore
from episteme.reporting import artifact_inventory
from episteme.reproduction import reconcile_reproduction, reproduce
from episteme.runner_locked import execute
from episteme.store import Store


OUTPUTS = {"raw_data": "raw.json", "metrics": "metrics.json"}
RANDOM = {"main.py": b'''import json, os, pathlib, sys
pathlib.Path("raw.json").write_text(json.dumps({"noise": os.urandom(8).hex()}))
pathlib.Path("metrics.json").write_text(json.dumps({"mean": 1.0}))
'''}
V1_SOURCE = b'''import json, pathlib, sys
raw = pathlib.Path(sys.argv[1]).read_bytes()
pathlib.Path("raw.json").write_bytes(raw)
pathlib.Path("metrics.json").write_text(json.dumps({"mean": sum(json.loads(raw)["values"]) / 3}))
'''


@unittest.skipUnless(fx.UV, fx.UV_REASON)
class ReproductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="episteme-reproduce-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        patcher = patch.dict(os.environ, {"EPISTEME_EXECUTION_ROOT": str(self.base / "jobs")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = self.base / "research"
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.executor = Actor("reproduce-executor", "executor")
        self.planner = Kernel(self.store, Actor("reproduce-planner", "planner"))
        self.scope = {"mode": "reproduction_fixture"}
        self.pool = [self.planner.hypothesis(text, "prediction", "falsifier", self.scope)
                     for text in ("fixture mean is preserved", "fixture null alternative")]
        self.data = self.store.put_json({"values": [1, 2, 3]})
        project = fx.locked_project(self.base / "project", dependency=True)
        self.environment = locked.freeze_closure(self.store, locked.project_files(project))

    def plan(self, implementation, environment=None):
        return self.planner.preregister(hypotheses=self.pool, scope=self.scope, design="reproduction fixture",
            metric="mean", analysis_plan="mean of fixture values", stopping_rule="registered attempts",
            seeds=[1], run_limit=4, implementation=implementation,
            environment=environment or self.environment, data=self.data, replication_tolerance=0)

    def enqueue(self, protocol, actor=None, **changes):
        actor = actor or self.executor
        payload = dict(protocol=protocol, seed=1, outputs=OUTPUTS, wall_seconds=60, max_output_bytes=65536)
        payload.update(changes)
        return CommandService(self.store).execute(dict(
            context=dict(command_id=uuid4().hex, expected_revision=len(self.store.events()), actor=actor.id,
                         role=actor.role, study_id="reproduce-study", correlation_id="reproduce-cycle",
                         causation_id=None),
            request=dict(version=1, action="execution.enqueue", payload=payload)))

    def completed(self, files=None, entry="main.py"):
        protocol = self.plan(locked.freeze_source(self.store, files or fx.PROGRAM, entry_point=entry))
        state = work_job(self.store, self.enqueue(protocol))
        self.assertEqual(state["status"], "completed")
        return protocol, state

    def kinds(self):
        return [event["kind"] for event in self.store.events()]

    def test_v2_run_reproduces_identically_and_is_recorded_but_never_evidence(self):
        _, state = self.completed()
        before = self.kinds()
        summary = reproduce(self.store, state["run"])
        self.assertEqual((summary["status"], summary["replay"], summary["counts_as_evidence"]),
                         ("matched", "same_code_fresh_environment", False))
        self.assertEqual(self.kinds()[len(before):], ["execution_reproduction_dispatch", "execution_reproduction"])
        self.assertEqual(before.count("run"), self.kinds().count("run"))
        self.assertEqual(before.count("result"), self.kinds().count("result"))
        comparison = summary["comparison"]
        self.assertTrue(all(row["equal"] for row in comparison["outputs"].values()))
        self.assertTrue(comparison["status"]["equal"])
        self.assertEqual(comparison["environment"]["equal"], dict(
            distributions_sha256=True, installer_version=True, interpreter_sha256=True,
            interpreter_version=True, platform=True, portable_inventory_sha256=True))
        final = self.store.events()[-1]
        self.assertEqual((final["payload"]["replication_mode"], final["payload"]["scientific_validity"]),
                         ("none", "not_assessed"))
        self.assertEqual(final["actor"], self.executor.id)
        graph = ResearchGraph.from_store(self.store)
        self.assertEqual({node.kind.value for node in graph.nodes} & {"execution_reproduction_dispatch",
                                                                      "execution_reproduction"},
                         {"execution_reproduction_dispatch", "execution_reproduction"})
        inventory = {row["sha256"] for row in artifact_inventory(self.store, self.store.events())}
        self.assertIn(final["payload"]["manifest"], inventory)
        self.assertIn(final["payload"]["outputs"]["environment_record"], inventory)
        snapshot, target = self.base / "backup", self.base / "restored"
        backup(self.store, snapshot)
        restore(snapshot, target)
        with Store(target) as restored:
            self.assertEqual(ResearchGraph.from_store(restored).to_dict(), graph.to_dict())

    def test_nondeterministic_outputs_are_recorded_as_a_mismatch(self):
        _, state = self.completed(RANDOM)
        summary = reproduce(self.store, state["run"])
        self.assertEqual(summary["status"], "mismatched")
        self.assertEqual(summary["comparison"]["mismatch_reasons"], ["declared output raw_data differs"])
        self.assertTrue(summary["comparison"]["outputs"]["metrics"]["equal"])
        ResearchGraph.from_store(self.store)

    def test_a_reproduction_enters_the_claim_basis_without_becoming_evidence(self):
        protocol, primary = self.completed()
        reanalysis = locked.freeze_source(self.store, fx.REANALYSIS, entry_point="reanalyse.py")
        replica = work_job(self.store, self.enqueue(protocol, actor=Actor("reproduce-reanalyst", "replicator"),
                                                    implementation=reanalysis, replicate_of=primary["run"]))
        claim = Kernel(self.store, Actor("reproduce-analyst", "analyst")).claim(
            protocol=protocol, statement="fixture mean preserved", scope=self.scope,
            evidence=[primary["run"], replica["run"]], outcome="inconclusive",
            limitations=["synthetic engineering fixture; same OS user, no sandbox"])
        reader = Kernel(self.store, Actor("reader", "observer"))
        before = reader.gate(claim)
        _, contributors_before, _ = reader._review_members(self.store.events(), claim)
        self.assertEqual(reproduce(self.store, primary["run"])["status"], "matched")
        after = reader.gate(claim)
        self.assertTrue(before["passed"] and after["passed"], after)
        self.assertNotEqual(before["basis_hash"], after["basis_hash"],
                            "a decision taken before the reproduction is no longer current")
        evidence = reader._local_evidence(self.store.events(), claim)[0]
        self.assertEqual([e["kind"] for e in evidence if e["kind"].startswith("execution_reproduction")],
                         ["execution_reproduction_dispatch", "execution_reproduction"])
        self.assertEqual(Kernel._get(self.store.events(), claim, "claim")["payload"]["evidence"],
                         [primary["run"], replica["run"]])
        self.assertEqual(reader._review_members(self.store.events(), claim)[1], contributors_before)

    def test_environment_failure_during_reproduction_is_a_mismatch_with_its_reason(self):
        _, state = self.completed()
        with patch.dict(os.environ, {"EPISTEME_UV": str(self.base / "absent" / "uv.exe")}):
            summary = reproduce(self.store, state["run"])
        self.assertEqual((summary["status"], summary["reproduced_status"]), ("mismatched", "failed"))
        reason = summary["comparison"]["mismatch_reasons"][0]
        self.assertIn("terminal status differs", reason)
        self.assertIn("uv executable not found", reason)

    def test_unknown_attempt_stays_visible_and_reconcile_never_relaunches(self):
        _, state = self.completed()
        with patch("episteme.execution_locked.spawn") as spawn:
            summary = reproduce(self.store, state["run"])
            spawn.assert_called_once()
        self.assertEqual((summary["status"], summary["reproduction"]), ("unknown", None))
        self.assertEqual(self.kinds()[-1], "execution_reproduction_dispatch")
        ResearchGraph.from_store(self.store)
        jobs = _index(self.store, self.store.events())
        attempt = jobs[state["job"]]["reproductions"][summary["dispatch"]]
        path = locked.job_directory(self.store, attempt["dispatch"])
        with patch("episteme.execution_locked.spawn") as spawn:
            self.assertEqual(reconcile_reproduction(self.store, summary["dispatch"])["status"], "unknown")
            execute(path)  # A late worker completion, as after a controller crash.
            self.assertEqual(reconcile_reproduction(self.store, summary["dispatch"])["status"], "matched")
            spawn.assert_not_called()
        again = reproduce(self.store, state["run"])
        self.assertNotEqual(again["dispatch"], summary["dispatch"], "a new reproduce is a new attempt")

    def test_v1_runs_reproduce_only_with_the_recorded_interpreter(self):
        environment = freeze_environment(self.store)
        protocol = self.plan(self.store.put(V1_SOURCE), environment)
        state = work_job(self.store, self.enqueue(protocol))
        self.assertEqual(state["status"], "completed")
        summary = reproduce(self.store, state["run"])
        self.assertEqual((summary["status"], summary["replay"]), ("matched", "same_code_same_interpreter"))
        self.assertTrue(summary["comparison"]["environment"]["equal"]["runtime"])
        dispatch = next(e for e in self.store.events() if e["id"] == summary["dispatch"])
        self.assertFalse(Path(dispatch["payload"]["workspace_root"]).is_relative_to(self.root))
        fingerprint = json.loads(self.store.read(environment))["fingerprint"]
        foreign = self.store.put_json(dict(schema_version=1, backend="trusted_local_python_v1",
                                           fingerprint=dict(fingerprint, executable_sha256="0" * 64)))
        failed = work_job(self.store, self.enqueue(self.plan(self.store.put(V1_SOURCE + b"\n"), foreign)))
        self.assertEqual(failed["status"], "failed")
        with self.assertRaisesRegex(ValueError, "not re-materializable"):
            reproduce(self.store, failed["run"])

    def test_unmanaged_unfinished_and_receiptless_reproductions_are_refused(self):
        protocol = self.plan(locked.freeze_source(self.store, fx.PROGRAM, entry_point="main.py"))
        queued = self.enqueue(protocol)
        run = _index(self.store, self.store.events())[queued]["run"]["id"]
        with self.assertRaisesRegex(ValueError, "finalized managed run"):
            reproduce(self.store, run)
        legacy = Kernel(self.store, self.executor).start_run(
            self.plan(self.store.put(V1_SOURCE), freeze_environment(self.store)), seed=1,
            implementation=self.store.put(V1_SOURCE), environment=freeze_environment(self.store),
            command=["python", "program.py"])
        with self.assertRaisesRegex(ValueError, "caller-recorded runs"):
            reproduce(self.store, legacy)
        _, state = self.completed()
        jobs = _index(self.store, self.store.events())
        job = jobs[state["job"]]
        final = job["finalized"]
        result = Kernel._get(self.store.events(), final["payload"]["result"], "result")
        self.store.append(id="forged-reproduction", kind="execution_reproduction_dispatch", actor=self.executor.id,
                          role="executor", expected_revision=len(self.store.events()), payload=dict(
                              schema_version=1, run=state["run"], run_hash=job["run"]["hash"], job=state["job"],
                              job_hash=job["job"]["hash"], finalized=final["id"], finalized_hash=final["hash"],
                              result=result["id"], result_hash=result["hash"], profile=locked.PROFILE,
                              replay="same_code_fresh_environment", workspace_token=uuid4().hex,
                              workspace_root=str(self.base / "jobs"), counts_as_evidence=False))
        with self.assertRaisesRegex(ValueError, "receipt"):
            ResearchGraph.from_store(self.store)

    def test_cli_reports_verdicts_through_exit_codes(self):
        _, state = self.completed()
        _, noisy = self.completed(RANDOM)
        for run, code, verdict in ((state["run"], 0, "matched"), (noisy["run"], 1, "mismatched")):
            process = subprocess.run([sys.executable, "-m", "episteme", "reproduce", run, "--root", str(self.root)],
                                     capture_output=True, text=True, timeout=180)
            self.assertEqual(process.returncode, code, process.stderr)
            report = json.loads(process.stdout)
            self.assertEqual((report["status"], report["counts_as_evidence"]), (verdict, False))
            self.assertIn("not independent replication", report["meaning"])


if __name__ == "__main__":
    unittest.main()
