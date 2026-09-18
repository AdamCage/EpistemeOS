"""Actual process lifecycle checks for the trusted local backend."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from episteme.runner_backend import execute, fingerprint
from episteme.store import canonical, digest


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)

    def stage(self, source, wall_seconds=10, limit=65536):
        data = b"fixture input"
        (self.workspace / "program.py").write_bytes(source)
        (self.workspace / "input.dat").write_bytes(data)
        spec = dict(schema_version=1, command=[sys.executable, "-I", "-S", "program.py", "input.dat", "--seed", "1"],
                    outputs={"raw_data": "raw.bin", "metrics": "metrics.json"}, wall_seconds=wall_seconds,
                    max_output_bytes=limit, expected_inputs={"program.py": digest(source), "input.dat": digest(data)},
                    environment_fingerprint=fingerprint())
        (self.workspace / "spec.json").write_bytes(canonical(spec))
        identity = dict(schema_version=1, job="fixture-job", run="fixture-run", dispatch="fixture-dispatch",
                        specification=digest(canonical(spec)))
        (self.workspace / "identity.json").write_bytes(canonical(identity))
        return spec

    def test_payload_and_descendant_terminated_before_late_output(self):
        source = b'''import subprocess,sys,time,pathlib
child = subprocess.Popen([sys.executable,"-I","-S","-c", "import time,pathlib; time.sleep(3); pathlib.Path('late.txt').write_text('escaped')"])
pathlib.Path('child.pid').write_text(str(child.pid))
print('spawned child', flush=True)
time.sleep(30)
'''
        report = execute(self.workspace, self.stage(source, wall_seconds=1))
        self.assertEqual(report["status"], "failed", report)
        self.assertIn("wall time", report["reason"])
        self.assertTrue(report["termination_confirmed"])
        self.assertTrue((self.workspace / "child.pid").exists())
        time.sleep(3.1)
        self.assertFalse((self.workspace / "late.txt").exists())
        self.assertIn(b"spawned child", (self.workspace / "stdout.bin").read_bytes())

    def test_descendant_outliving_successful_parent_is_stopped(self):
        source = b'''import subprocess,sys,pathlib
subprocess.Popen([sys.executable,"-I","-S","-c", "import time,pathlib; time.sleep(2); pathlib.Path('late.txt').write_text('escaped')"])
pathlib.Path('raw.bin').write_bytes(b'fixture')
pathlib.Path('metrics.json').write_text('{"value": 1}')
'''
        report = execute(self.workspace, self.stage(source))
        self.assertEqual(report["status"], "completed", report)
        time.sleep(2.1)
        self.assertFalse((self.workspace / "late.txt").exists())

    def test_second_delivery_never_executes_again(self):
        source = b'''import pathlib
pathlib.Path('executions.txt').open('a').write('executed' + chr(10))
pathlib.Path('raw.bin').write_bytes(b'fixture')
pathlib.Path('metrics.json').write_text('{"value": 1}')
'''
        spec = self.stage(source)
        self.assertEqual(execute(self.workspace, spec)["status"], "completed")
        before = (self.workspace / "completion.json").read_bytes()
        with self.assertRaises(FileExistsError):
            execute(self.workspace, spec)
        self.assertEqual((self.workspace / "executions.txt").read_text(), "executed\n")
        self.assertEqual(before, (self.workspace / "completion.json").read_bytes())

    def test_input_or_runtime_drift_refuses_payload(self):
        for drift in ("input", "runtime"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as directory:
                self.workspace = Path(directory)
                spec = self.stage(b"import pathlib; pathlib.Path('must-not-exist').touch()")
                if drift == "input":
                    (self.workspace / "input.dat").write_bytes(b"different")
                else:
                    spec["environment_fingerprint"]["python"] = "unavailable-version"
                    (self.workspace / "spec.json").write_bytes(canonical(spec))
                    identity = json.loads((self.workspace / "identity.json").read_bytes())
                    identity["specification"] = digest(canonical(spec))
                    (self.workspace / "identity.json").write_bytes(canonical(identity))
                result = execute(self.workspace, spec)
                self.assertEqual(result["status"], "failed")
                self.assertFalse((self.workspace / "must-not-exist").exists())
                self.assertIsNone(result["returncode"])

    def test_capture_cap_does_not_create_unbounded_log(self):
        spec = self.stage(b"import sys; sys.stdout.buffer.write(b'x'*100000); sys.stdout.flush()", limit=1024)
        report = execute(self.workspace, spec)
        self.assertEqual(report["status"], "failed")
        self.assertEqual((self.workspace / "stdout.bin").stat().st_size, 1024)
        self.assertTrue(report["termination_confirmed"])

    def test_symlink_output_is_not_captured(self):
        target = self.workspace / "external.txt"
        target.write_bytes(b"unrelated data")
        try:
            (self.workspace / "probe-link").symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        source = b'''import pathlib
pathlib.Path('raw.bin').symlink_to(pathlib.Path('external.txt').resolve())
pathlib.Path('metrics.json').write_text('{"value": 1}')
'''
        report = execute(self.workspace, self.stage(source))
        self.assertEqual(report["status"], "failed")
        self.assertNotIn("raw_data", report["outputs"])
        self.assertEqual(target.read_bytes(), b"unrelated data")


if __name__ == "__main__":
    unittest.main()
