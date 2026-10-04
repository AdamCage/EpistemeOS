"""Execution profile v2 (ADR 0019): source trees, uv-locked closures, allowlisted runs.

Every program is a small stdlib or locally built fixture; no test reaches the
network. Completed runs are mechanical execution results, not scientific
evidence of anything, and no reviewer verdict is created here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import locked_support as fx
from episteme import environment_closure as rules
from episteme.commands import CommandService
from episteme.execution import _index, _workspace, freeze_environment, job_state, reconcile_job, work_job
from episteme import execution_locked as locked
from episteme.graph import ResearchGraph
from episteme.kernel import Actor, Kernel
from episteme.recovery import backup, restore
from episteme.reporting import artifact_inventory, export_store
from episteme.runner_locked import accelerators, parse_nvidia_smi
from episteme.store import IntegrityError, Store, canonical, digest


OUTPUTS = {"raw_data": "raw.json", "metrics": "metrics.json"}
REMOTE_LOCK = b'''version = 1
revision = 3
requires-python = ">=3.11"

[[package]]
name = "episteme-absent-dep"
version = "9.9.9"
source = { registry = "https://example.invalid/simple" }
wheels = [
    { url = "https://example.invalid/files/episteme_absent_dep-9.9.9-py3-none-any.whl", hash = "sha256:%s", size = 1000 },
]

[[package]]
name = "research-program"
version = "0"
source = { virtual = "." }
dependencies = [
    { name = "episteme-absent-dep" },
]

[package.metadata]
requires-dist = [{ name = "episteme-absent-dep", specifier = "==9.9.9" }]
''' % (b"1" * 64)
REMOTE_PYPROJECT = (b'[project]\nname = "research-program"\nversion = "0"\nrequires-python = ">=3.11"\n'
                    b'dependencies = ["episteme-absent-dep==9.9.9"]\n\n[tool.uv]\npackage = false\n')


def lock(*packages: str) -> bytes:
    return ('version = 1\nrevision = 3\nrequires-python = ">=3.11"\n\n'
            '[[package]]\nname = "root"\nversion = "0"\nsource = { virtual = "." }\n\n'
            + "\n".join(packages)).encode()


class FormatTests(unittest.TestCase):
    """Pure manifest, lock and variable rules; no uv and no Store needed."""

    def test_source_manifest_is_canonical_and_rejects_unportable_trees(self):
        manifest = rules.source_manifest("main.py", "input.dat", fx.PROGRAM)
        self.assertEqual([row["path"] for row in manifest["files"]],
                         ["helpers/__init__.py", "helpers/stats.py", "main.py"])
        for files, entry in (({"main.py": b""}, "other.py"), ({"data.txt": b""}, "data.txt"),
                             ({"../x.py": b""}, "../x.py"), ({"/abs.py": b""}, "/abs.py"),
                             ({"a\\b.py": b""}, "a\\b.py"), ({"CON.py": b""}, "CON.py"),
                             ({"a/x.py": b"", "A/y.py": b""}, "a/x.py"),
                             ({"a": b"", "a/b.py": b""}, "a/b.py"), ({"x..py.": b""}, "x..py.")):
            with self.subTest(files=sorted(files)):
                with self.assertRaises(rules.ClosureError):
                    rules.source_manifest(entry, "input.dat", files)
        with self.assertRaises(rules.ClosureError):
            rules.source_manifest("main.py", "../input.dat", fx.PROGRAM)

    def test_directory_capture_skips_hidden_and_bytecode_and_refuses_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative, data in {"main.py": b"x = 1\n", ".env": b"SECRET=1", ".git/config": b"[core]",
                                   "pkg/__pycache__/m.cpython-311.pyc": b"\0", "pkg/m.py": b""}.items():
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_bytes(data)
            self.assertEqual(sorted(rules.directory_files(root)), ["main.py", "pkg/m.py"])
            try:
                (root / "linked.py").symlink_to(root / "main.py")
            except OSError:
                self.skipTest("this Windows account cannot create symbolic links")
            with self.assertRaisesRegex(rules.ClosureError, "link"):
                rules.directory_files(root)

    def test_lock_summary_accepts_hashed_remote_and_relative_local_wheels_only(self):
        good = rules.lock_summary(lock(
            '[[package]]\nname = "Remote_Pkg"\nversion = "1.0"\nsource = { registry = "https://pypi.org/simple" }\n'
            f'wheels = [{{ url = "https://x/r.whl", hash = "sha256:{"a" * 64}", size = 1 }}]\n',
            '[[package]]\nname = "local"\nversion = "2.0"\nsource = { registry = "./wheels" }\n'
            'wheels = [{ path = "local-2.0-py3-none-any.whl" }]\n'))
        self.assertEqual(good["local_files"], ["wheels/local-2.0-py3-none-any.whl"])
        self.assertEqual([row["name"] for row in good["packages"]], ["local", "remote-pkg"])
        refused = {
            "unhashed remote": '[[package]]\nname = "r"\nversion = "1"\nsource = { registry = "https://x" }\n'
                               'wheels = [{ url = "https://x/r.whl" }]\n',
            "absolute local": '[[package]]\nname = "l"\nversion = "1"\nsource = { registry = "C:/wheels" }\n'
                              'wheels = [{ path = "l.whl" }]\n',
            "escaping local": '[[package]]\nname = "l"\nversion = "1"\nsource = { registry = "../wheels" }\n'
                              'wheels = [{ path = "l.whl" }]\n',
            "local sdist": '[[package]]\nname = "l"\nversion = "1"\nsource = { registry = "wheels" }\n'
                           'sdist = { path = "l-1.tar.gz" }\n',
            "git": '[[package]]\nname = "g"\nversion = "1"\nsource = { git = "https://x/g.git" }\n',
            "path": '[[package]]\nname = "p"\nversion = "1"\nsource = { path = "deps/p" }\n',
            "member": '[[package]]\nname = "m"\nversion = "1"\nsource = { editable = "member" }\n',
        }
        for label, package in refused.items():
            with self.subTest(label=label), self.assertRaises(rules.ClosureError):
                rules.lock_summary(lock(package))
        with self.assertRaises(rules.ClosureError):
            rules.lock_summary(b"not = [toml")

    def test_variables_refuse_controlled_python_uv_and_credential_names(self):
        rules.check_variables(dict(inherit=["CUDA_VISIBLE_DEVICES"],
                                   set={"OMP_NUM_THREADS": "1", "PYTHONHASHSEED": "0"}))
        refused = [dict(inherit=["PATH"], set={}), dict(inherit=[], set={"HOME": "/x"}),
                   dict(inherit=["PYTHONPATH"], set={}), dict(inherit=[], set={"PYTHONPATH": "x"}),
                   dict(inherit=["PYTHONHASHSEED"], set={}), dict(inherit=["UV_CACHE_DIR"], set={}),
                   dict(inherit=["GITHUB_TOKEN"], set={}), dict(inherit=["AWS_SECRET_ACCESS_KEY"], set={}),
                   dict(inherit=[], set={"OPENAI_API_KEY": "x"}), dict(inherit=["A", "a"], set={}),
                   dict(inherit=["B", "A"], set={}), dict(inherit=["X"], set={"X": "1"}),
                   dict(inherit=[], set={"BAD-NAME": "1"}), dict(inherit=[], set={"NUL": "a\x00b"}),
                   dict(inherit=["__PYVENV_LAUNCHER__"], set={})]
        for variables in refused:
            with self.subTest(variables=variables), self.assertRaises(rules.ClosureError):
                rules.check_variables(variables)

    def test_payload_environment_contains_only_declared_and_controlled_values(self):
        declaration = dict(platform=dict(system=platform.system(), machine=platform.machine()),
                           variables=dict(inherit=["EPISTEME_INHERITED", "EPISTEME_ABSENT"],
                                          set={"PYTHONHASHSEED": "0"}))
        host = {"EPISTEME_INHERITED": "kept", "EPISTEME_CANARY": "secret value", "PYTHONPATH": "x",
                "SYSTEMROOT": r"C:\Windows"}
        env, missing = rules.payload_environment(declaration, host, bin_dir="B", tmp="T", home="H")
        self.assertEqual(missing, ["EPISTEME_ABSENT"])
        self.assertEqual(env["EPISTEME_INHERITED"], "kept")
        self.assertNotIn("EPISTEME_CANARY", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertEqual(env["HOME"], "H")
        self.assertTrue(env["PATH"].startswith("B"))
        self.assertTrue(set(env) <= {"EPISTEME_INHERITED", "PYTHONHASHSEED", *rules.CONTROLLED})

    def test_closure_project_must_be_exactly_pyproject_lock_and_local_wheels(self):
        files = {"pyproject.toml": b"[project]\n", "uv.lock": lock()}
        kwargs = dict(implementation="cpython", version="3.11.15", system="Linux", machine="x86_64",
                      installer_version="0.11.28")
        declaration = rules.closure_declaration(files, **kwargs)
        self.assertEqual(declaration["variables"]["set"], rules.DEFAULT_SET)
        with self.assertRaisesRegex(rules.ClosureError, "exactly"):
            rules.closure_declaration({**files, "notes.txt": b"x"}, **kwargs)
        wheel_lock = lock('[[package]]\nname = "l"\nversion = "1"\nsource = { registry = "wheels" }\n'
                          'wheels = [{ path = "l-1-py3-none-any.whl" }]\n')
        with self.assertRaisesRegex(rules.ClosureError, "exactly"):
            rules.closure_declaration({**files, "uv.lock": wheel_lock}, **kwargs)
        for change in (dict(version="3.11"), dict(implementation="jython"), dict(system="Plan9"),
                       dict(installer_version="latest")):
            with self.subTest(change=change), self.assertRaises(rules.ClosureError):
                rules.closure_declaration(files, **{**kwargs, **change})

    def test_accelerator_probe_records_absence_and_parses_nvidia_smi(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(accelerators({"PATH": empty})["status"], "not_available")
        parsed = parse_nvidia_smi("0, NVIDIA Fixture GPU, 555.42, 24576\n",
                                  "| NVIDIA-SMI 555.42  Driver Version: 555.42  CUDA Version: 12.5 |")
        self.assertEqual(parsed, dict(cuda_version="12.5", gpus=[dict(
            index="0", name="NVIDIA Fixture GPU", driver_version="555.42", memory_total_mib="24576")]))


@unittest.skipUnless(fx.UV, fx.UV_REASON)
class LockedExecutionTests(unittest.TestCase):
    """Real offline uv environments and small subprocess payloads."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="episteme-locked-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.jobs = self.base / "jobs"
        patcher = patch.dict(os.environ, {"EPISTEME_EXECUTION_ROOT": str(self.jobs),
                                          "EPISTEME_TEST_CANARY": "controller-only secret"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = self.base / "research"
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.actor = Actor("locked-executor", "executor")
        self.planner = Kernel(self.store, Actor("locked-planner", "planner"))
        self.scope = {"mode": "locked_execution_fixture"}
        self.pool = [self.planner.hypothesis(text, "prediction", "falsifier", self.scope)
                     for text in ("fixture mean is preserved", "fixture null alternative")]
        self.data = self.store.put_json({"values": [1, 2, 3]})
        self.environment = self.closure()
        self.program = locked.freeze_source(self.store, fx.PROGRAM, entry_point="main.py")

    def closure(self, *, dependency=False, **changes):
        name = f"project-{uuid4().hex[:8]}"
        project = fx.locked_project(self.base / name, dependency=dependency)
        return locked.freeze_closure(self.store, locked.project_files(project), **changes)

    def plan(self, program=None, environment=None, run_limit=4):
        return self.planner.preregister(hypotheses=self.pool, scope=self.scope, design="locked fixture",
            metric="mean", analysis_plan="mean of fixture values", stopping_rule="registered attempts",
            seeds=[1], run_limit=run_limit, implementation=program or self.program,
            environment=environment or self.environment, data=self.data, replication_tolerance=0)

    def envelope(self, action, payload, actor=None):
        actor = actor or self.actor
        return dict(context=dict(command_id=uuid4().hex, expected_revision=len(self.store.events()),
                                 actor=actor.id, role=actor.role, study_id="locked-study",
                                 correlation_id="locked-cycle", causation_id=None),
                    request=dict(version=1, action=action, payload=payload))

    def enqueue(self, protocol=None, actor=None, **changes):
        payload = dict(protocol=protocol or self.plan(), seed=1, outputs=OUTPUTS, wall_seconds=60,
                       max_output_bytes=65536)
        payload.update(changes)
        return CommandService(self.store).execute(self.envelope("execution.enqueue", payload, actor))

    def run_program(self, files, *, entry="main.py", environment=None, **changes):
        program = locked.freeze_source(self.store, files, entry_point=entry)
        state = work_job(self.store, self.enqueue(self.plan(program, environment), **changes))
        result = Kernel._get(self.store.events(), state["result"], "result")["payload"]
        return state, result, json.loads(self.store.read(result["outputs"]["log"]))

    def test_multi_file_program_runs_in_a_fresh_environment_outside_the_store(self):
        state, result, record = self.run_program(fx.PROGRAM)
        self.assertEqual((state["status"], state["isolation"], state["scientific_validity"]),
                         ("completed", locked.PROFILE, "not_assessed"), result["reason"])
        self.assertEqual(json.loads(self.store.read(result["outputs"]["metrics"])), {"mean": 2.0, "seed": 1})
        self.assertEqual(result["outputs"]["raw_data"], self.data)
        job = state["job"]
        workspace = _workspace(self.store, _index(self.store, self.store.events())[job])
        self.assertTrue(workspace.is_relative_to(self.jobs.resolve()))
        self.assertFalse(workspace.resolve().is_relative_to(self.root))
        self.assertFalse((workspace / "venv").exists(), "the venv copy is dropped after finalization")
        self.assertEqual(record["command"], ["python", "-s", "-B", "src/main.py", "inputs/input.dat",
                                             "--seed", "1"])
        self.assertEqual(record["launch_command"][-4:], record["command"][3:])
        self.assertEqual(record["isolation"]["filesystem"], "not_enforced")
        self.assertEqual(record["environment_variables"]["PYTHONHASHSEED"], "0")
        self.assertNotIn("EPISTEME_TEST_CANARY", record["environment_variables"])
        self.assertIn(record["accelerators"]["status"], {"not_available", "available", "error"})
        environment = json.loads(self.store.read(result["outputs"]["environment_record"]))
        self.assertEqual(environment["closure"], self.environment)
        self.assertEqual(environment["interpreter"]["version"], platform.python_version())
        self.assertRegex(environment["interpreter"]["base_executable_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(environment["installer"]["network"], "offline")
        self.assertIn("--offline", environment["installer"]["command"])
        self.assertEqual(environment["distributions"], [])
        graph = ResearchGraph.from_store(self.store)
        manifest, files = locked.source_parts(self.store, self.program)
        artifacts = {node.payload["sha256"] for node in graph.nodes if node.kind.value == "artifact"}
        self.assertTrue({self.program, self.environment, *map(digest, files.values()),
                         result["outputs"]["environment_record"]} <= artifacts)
        self.assertTrue(artifacts <= {row["sha256"] for row in artifact_inventory(self.store, self.store.events())})
        self.assertTrue(export_store(self.store))
        snapshot, target = self.base / "backup", self.base / "restored"
        backup(self.store, snapshot)
        restore(snapshot, target)
        with Store(target) as restored, patch("episteme.execution_locked.spawn") as spawn:
            self.assertEqual(ResearchGraph.from_store(restored).to_dict(), graph.to_dict())
            self.assertEqual(work_job(restored, job), state)
            spawn.assert_not_called()

    def test_payload_sees_only_allowlisted_variables_and_no_store_by_relative_path(self):
        database = str(self.root / "state.sqlite3").encode("unicode_escape")
        probe = {"main.py": b'''import json, os, pathlib, sys
cwd = pathlib.Path.cwd()
view = dict(keys=sorted(os.environ), canary=os.environ.get("EPISTEME_TEST_CANARY"),
            hashseed=os.environ.get("PYTHONHASHSEED"), sys_flags_no_user_site=sys.flags.no_user_site,
            store_two_up=(cwd / ".." / ".." / "state.sqlite3").exists(),
            store_three_up=(cwd / ".." / ".." / ".." / "state.sqlite3").exists(),
            store_by_absolute_path=pathlib.Path("''' + database + b'''").is_file())
pathlib.Path("raw.json").write_text(json.dumps(view))
pathlib.Path("metrics.json").write_text(json.dumps({"mean": 2.0}))
'''}
        state, result, record = self.run_program(probe)
        self.assertEqual(state["status"], "completed", result["reason"])
        view = json.loads(self.store.read(result["outputs"]["raw_data"]))
        self.assertIsNone(view["canary"])
        self.assertEqual(view["hashseed"], "0")
        self.assertEqual(view["sys_flags_no_user_site"], 1)
        self.assertFalse(view["store_two_up"] or view["store_three_up"])
        # No sandbox: the same OS user still reaches the store when it knows the path.
        self.assertTrue(view["store_by_absolute_path"])
        self.assertEqual(set(view["keys"]), set(record["environment_variables"]))

    def test_vendored_wheel_dependency_is_installed_offline_and_recorded(self):
        environment = self.closure(dependency=True)
        state, result, _ = self.run_program(fx.DEPENDENT, environment=environment)
        self.assertEqual(state["status"], "completed", result["reason"])
        metrics = json.loads(self.store.read(result["outputs"]["metrics"]))
        self.assertEqual((metrics["mean"], metrics["module"]), (42, "site-packages"))
        record = json.loads(self.store.read(result["outputs"]["environment_record"]))
        (dist,) = record["distributions"]
        self.assertEqual((dist["name"], dist["version"], dist["record_verified"], dist["installer"]),
                         ("episteme-fixture-dep", "0.1.0", True, "uv"))
        declaration, project = locked.closure_parts(self.store, environment)
        self.assertIn("wheels/episteme_fixture_dep-0.1.0-py3-none-any.whl", project)

    def test_offline_cache_miss_fails_the_attempt_without_downloading(self):
        environment = locked.freeze_closure(self.store, {"pyproject.toml": REMOTE_PYPROJECT,
                                                         "uv.lock": REMOTE_LOCK})
        state, result, record = self.run_program(fx.PROGRAM, environment=environment)
        self.assertEqual(state["status"], "failed")
        self.assertIn("uv sync failed", result["reason"])
        self.assertIsNone(record["launch_command"], "the payload never started")
        self.assertIsNotNone(record["installer_log"])
        self.assertNotIn("environment_record", result["outputs"])
        ResearchGraph.from_store(self.store)

    def test_installer_or_interpreter_mismatch_fails_before_the_payload(self):
        for change, message in ((dict(installer_version="0.0.1"), "differs from the closure installer"),
                                (dict(version="3.99.0"), "no installed interpreter")):
            with self.subTest(change=change):
                state, result, record = self.run_program(fx.PROGRAM, environment=self.closure(**change))
                self.assertEqual(state["status"], "failed")
                self.assertIn(message, result["reason"])
                self.assertIsNone(record["launch_command"])

    def test_changes_to_the_program_tree_input_or_environment_fail_the_run(self):
        cases = {
            "tree": b"import pathlib\npathlib.Path('src/extra.py').write_text('x = 1')\n",
            "input": b"import pathlib\npathlib.Path('inputs/other.dat').write_text('x')\n",
            "environment": b"import pathlib, sysconfig\n"
                           b"pathlib.Path(sysconfig.get_path('purelib'), 'planted.pth').write_text('')\n",
        }
        tail = b"pathlib.Path('raw.json').write_text('{}')\npathlib.Path('metrics.json').write_text('{\"mean\": 1}')\n"
        for label, body in cases.items():
            with self.subTest(label=label):
                state, result, _ = self.run_program({"main.py": body + tail})
                self.assertEqual(state["status"], "failed")
                self.assertRegex(result["reason"], "frozen inputs changed|locked environment changed")

    def test_failures_keep_logs_and_never_turn_into_completion(self):
        cases = {"exit": b"print('failure log'); raise SystemExit(3)",
                 "missing": b"print('outputs omitted')",
                 "timeout": b"import time\ntime.sleep(30)"}
        for label, body in cases.items():
            with self.subTest(label=label):
                state, result, record = self.run_program({"main.py": body}, wall_seconds=1)
                self.assertEqual(state["status"], "failed", result)
                self.assertTrue(result["reason"])
                self.assertIn("log", result["outputs"])
                self.assertTrue(record["termination_confirmed"])

    def test_second_tree_reanalysis_is_managed_evidence_for_the_gate(self):
        protocol = self.plan()
        primary = work_job(self.store, self.enqueue(protocol))
        reanalysis = locked.freeze_source(self.store, fx.REANALYSIS, entry_point="reanalyse.py")
        replica = work_job(self.store, self.enqueue(protocol, actor=Actor("locked-reanalyst", "replicator"),
                                                    implementation=reanalysis, replicate_of=primary["run"]))
        self.assertEqual((primary["status"], replica["status"]), ("completed", "completed"))
        claim = Kernel(self.store, Actor("locked-analyst", "analyst")).claim(
            protocol=protocol, statement="fixture mean preserved", scope=self.scope,
            evidence=[primary["run"], replica["run"]], outcome="inconclusive",
            limitations=["synthetic engineering fixture; same OS user, no sandbox"])
        reader = Kernel(self.store, Actor("reader", "observer"))
        gate = reader.gate(claim)
        self.assertTrue(gate["passed"], gate)
        context = reader._local_evidence(self.store.events(), claim)[0]
        self.assertEqual(len([e for e in context if e["kind"].startswith("execution_")]), 6)
        self.assertFalse(any(e["kind"] == "review" for e in self.store.events()))

    def test_dispatch_actions_are_profile_specific_and_roots_stay_outside_the_store(self):
        job = self.enqueue()
        for action, payload, message in (
                ("execution.dispatch", dict(job=job, workspace_token=uuid4().hex), "execution.dispatch_v2"),
                ("execution.dispatch_v2", dict(job=job, workspace_token=uuid4().hex,
                                               workspace_root=str(self.root / "jobs")), "outside the store"),
                ("execution.dispatch_v2", dict(job=job, workspace_token=uuid4().hex,
                                               workspace_root="relative/jobs"), "absolute")):
            with self.subTest(action=action, payload=payload):
                with self.assertRaisesRegex(ValueError, message):
                    CommandService(self.store).execute(self.envelope(action, payload))
        v1 = self.planner.preregister(hypotheses=self.pool, scope=self.scope, design="v1", metric="mean",
            analysis_plan="a", stopping_rule="s", seeds=[1], run_limit=2,
            implementation=self.store.put(b"print(1)"), environment=freeze_environment(self.store),
            data=self.data, replication_tolerance=0)
        v1_job = self.enqueue(v1)
        with self.assertRaisesRegex(ValueError, "v1 jobs"):
            CommandService(self.store).execute(self.envelope("execution.dispatch_v2", dict(
                job=v1_job, workspace_token=uuid4().hex, workspace_root=str(self.jobs))))

    def test_unknown_dispatch_is_never_relaunched_and_corrupt_tree_bytes_fail_replay(self):
        job = self.enqueue()
        CommandService(self.store).execute(self.envelope("execution.dispatch_v2", dict(
            job=job, workspace_token=uuid4().hex, workspace_root=str(self.jobs))))
        with patch("episteme.execution_locked.spawn") as spawn, patch("episteme.execution.subprocess.Popen") as popen:
            self.assertEqual(work_job(self.store, job)["status"], "unknown")
            spawn.assert_not_called()
            popen.assert_not_called()
        manifest, files = locked.source_parts(self.store, self.program)
        target = self.store.blobs / digest(files["helpers/stats.py"])
        target.write_bytes(b"def mean(values):\n    return 0\n")
        with self.assertRaises(IntegrityError):
            job_state(self.store, job)
        with self.assertRaises(ValueError):
            ResearchGraph.from_store(self.store)

    def test_cli_freezes_sources_and_closures_prepares_offline_and_works_a_job(self):
        tree = self.base / "tree"
        for relative, data in fx.PROGRAM.items():
            (tree / relative).parent.mkdir(parents=True, exist_ok=True)
            (tree / relative).write_bytes(data)
        project = fx.locked_project(self.base / "cli-project", dependency=True)

        def cli(*args):
            process = subprocess.run([sys.executable, "-m", "episteme", *args, "--root", str(self.root)],
                                     capture_output=True, text=True, timeout=180)
            self.assertEqual(process.returncode, 0, process.stderr)
            return json.loads(process.stdout)
        source = cli("execution", "source", "--tree", str(tree), "--entry", "main.py")
        self.assertEqual((source["implementation"], source["events_written"]), (self.program, 0))
        closure = cli("execution", "closure", "--project", str(project), "--set", "OMP_NUM_THREADS=1")
        self.assertEqual(closure["variables"]["set"], {"OMP_NUM_THREADS": "1"})
        prepared = cli("execution", "prepare", closure["environment"])
        self.assertEqual((prepared["status"], prepared["network"], prepared["events_written"]),
                         ("prepared", "offline", 0))
        self.assertEqual([row["name"] for row in prepared["distributions"]], ["episteme-fixture-dep"])
        job = self.enqueue(self.plan(environment=closure["environment"]))
        worked = cli("execution", "work", job)
        self.assertEqual(worked["status"], "completed")
        result = Kernel._get(self.store.events(), worked["result"], "result")["payload"]
        record = json.loads(self.store.read(result["outputs"]["log"]))
        self.assertEqual(record["environment_variables"]["OMP_NUM_THREADS"], "1")
        self.assertNotIn("PYTHONHASHSEED", record["environment_variables"])


if __name__ == "__main__":
    unittest.main()
