"""Provider admission/wrapper checks with mocked CLI; no model requests."""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from episteme.codex_provider import PROGRAM, freeze_provider, validate_provider
from episteme.store import Store, canonical


class CodexProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binary = self.root / "codex.exe"
        self.binary.write_bytes(b"fake CLI bytes; never executed")
        self.provider = dict(schema_version=1, provider="codex_cli_v1", profile_version=2,
                             executable=str(self.binary),
                             executable_sha256=hashlib.sha256(self.binary.read_bytes()).hexdigest(),
                             cli_version="codex-cli 0.153.4", model="test-model", reasoning_effort="low")
        self.store = Store(self.root / "store")
        self.addCleanup(self.store.close)

    @staticmethod
    def version():
        return subprocess.CompletedProcess([], 0, "codex-cli 0.153.4\n", "")

    def test_portable_validation_never_checks_local_files_or_processes(self):
        for path in ("/missing/on/another/host/codex", r"Z:\another\host\codex.exe"):
            value = dict(self.provider, executable=path)
            with patch("subprocess.run", side_effect=AssertionError("process access")), \
                    patch.object(Path, "lstat", side_effect=AssertionError("filesystem access")):
                self.assertIsNone(validate_provider(value))

    def test_descriptor_rejects_unknown_fields_types_and_unfrozen_argv(self):
        cases = [dict(self.provider, extra=True), dict(self.provider, command=["unsafe"])]
        for name, bad in (("schema_version", True), ("profile_version", 3),
                          ("provider", "other"), ("model", " "), ("model", "x\ny"),
                          ("reasoning_effort", "invented"), ("executable", "codex"),
                          ("executable_sha256", "A" * 64), ("cli_version", "unknown")):
            cases.append(dict(self.provider, **{name: bad}))
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_provider(value)

    def test_freeze_records_only_fingerprint_and_makes_version_probe(self):
        with patch("episteme.codex_provider.subprocess.run", return_value=self.version()) as run:
            key = freeze_provider(self.store, model="test-model", executable=self.binary)
        self.assertEqual(json.loads(self.store.read(key)), self.provider)
        self.assertEqual(run.call_args.args[0], [str(self.binary), "--version"])
        self.assertEqual(self.store.events(), [])

    def test_freeze_detects_executable_change_during_probe(self):
        def probe(*args, **kwargs):
            self.binary.write_bytes(b"different executable")
            return self.version()
        with patch("episteme.codex_provider.subprocess.run", side_effect=probe):
            with self.assertRaisesRegex(ValueError, "changed"):
                freeze_provider(self.store, model="test-model", executable=self.binary)
        self.assertEqual(list(self.store.blobs.iterdir()), [])

    def test_failed_version_probe_does_not_freeze_provider(self):
        result = subprocess.CompletedProcess([], 1, "", "failed")
        with patch("episteme.codex_provider.subprocess.run", return_value=result):
            with self.assertRaisesRegex(ValueError, "version probe failed"):
                freeze_provider(self.store, model="test-model", executable=self.binary)
        self.assertEqual(list(self.store.blobs.iterdir()), [])

    def namespace(self):
        namespace = {"__name__": "fixture"}
        exec(compile(PROGRAM, "frozen-provider.py", "exec"), namespace)
        return namespace

    def input(self, **overrides):
        value = dict(provider=self.provider, prompt="Предложи два объяснения.",
                     output_schema={"type": "object"})
        value.update(overrides)
        path = self.root / "input.dat"
        path.write_bytes(canonical(value))
        return path

    def invoke(self, callback, **overrides):
        path = self.input(**overrides)
        namespace = self.namespace()
        with patch.object(sys, "argv", ["program.py", str(path)]), \
                patch.object(Path, "cwd", return_value=self.root), \
                patch("subprocess.run", side_effect=callback) as run:
            namespace["main"]()
        return run

    def test_wrapper_passes_stdin_schema_fixed_profile_and_clean_environment(self):
        seen = []
        def process(command, **kwargs):
            seen.append((command, kwargs))
            if command[-1] == "--version":
                return self.version()
            child = kwargs["cwd"]
            self.assertNotEqual(child, self.root)
            self.assertFalse(child.is_relative_to(self.root))
            schema = Path(command[command.index("--output-schema") + 1])
            self.assertEqual(json.loads(schema.read_bytes()), {"type": "object"})
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_bytes(b'{"hypotheses":[]}')
            return subprocess.CompletedProcess(command, 0)
        environment = {"PATH": "safe-path", "HOME": "existing-home", "CODEX_HOME": "existing-auth-root",
                       "OPENAI_API_KEY": "never-inherit", "CODEX_THREAD_ID": "never-inherit",
                       "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "never-inherit", "PRIVATE_SECRET": "never-inherit"}
        with patch.dict(os.environ, environment, clear=True):
            self.invoke(process)
        command, call = seen[-1]
        self.assertEqual(call["input"], "Предложи два объяснения.".encode())
        self.assertEqual(call["env"], {key: environment[key] for key in ("PATH", "HOME", "CODEX_HOME")})
        self.assertNotIn("stdout", call)
        self.assertNotIn("stderr", call)
        for flag in ("--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "--strict-config"):
            self.assertIn(flag, command)
        self.assertNotIn("--ignore-rules", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        for config in ("project_doc_max_bytes=0", "project_root_markers=[]",
                       "skills.include_instructions=false", "features.shell_tool=false",
                       'web_search="disabled"', "memories.use_memories=false",
                       "features.code_mode_host=true", "suppress_unstable_features_warning=true"):
            self.assertIn(config, command)
        self.assertFalse(call["cwd"].exists())
        self.assertTrue((self.root / "proposal.json").is_file())

    def test_profile_one_remains_readable_and_preserves_original_cli_settings(self):
        legacy = dict(self.provider, profile_version=1)
        validate_provider(legacy)
        calls = []
        def process(command, **kwargs):
            calls.append(command)
            if command[-1] == "--version":
                return self.version()
            Path(command[command.index("--output-last-message")+1]).write_bytes(b"{}")
            return subprocess.CompletedProcess(command, 0)
        self.invoke(process, provider=legacy)
        self.assertIn("features.code_mode_host=false", calls[-1])
        self.assertNotIn("suppress_unstable_features_warning=true", calls[-1])

    def test_wrapper_rejects_digest_mismatch_without_starting_cli(self):
        value = dict(self.provider, executable_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            self.invoke(lambda *a, **k: self.fail("must not launch"), provider=value)

    def test_wrapper_rejects_version_mismatch_before_model_request(self):
        calls = []
        def process(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "codex-cli 9.0\n", "")
        with self.assertRaisesRegex(ValueError, "version mismatch"):
            self.invoke(process)
        self.assertEqual(len(calls), 1)

    def test_wrapper_rejects_executable_change_between_version_and_launch(self):
        def process(*args, **kwargs):
            self.binary.write_bytes(b"changed")
            return self.version()
        with self.assertRaisesRegex(ValueError, "changed during preflight"):
            self.invoke(process)

    def test_wrapper_preserves_nonzero_cli_exit_and_creates_no_success_output(self):
        def process(command, **kwargs):
            return self.version() if command[-1] == "--version" else subprocess.CompletedProcess(command, 7)
        with self.assertRaises(SystemExit) as caught:
            self.invoke(process)
        self.assertEqual(caught.exception.code, 7)
        self.assertFalse((self.root / "proposal.json").exists())

    def test_wrapper_does_not_accept_missing_output_or_overwrite_existing_output(self):
        def process(command, **kwargs):
            return self.version() if command[-1] == "--version" else subprocess.CompletedProcess(command, 0)
        with self.assertRaises(FileNotFoundError):
            self.invoke(process)
        output = self.root / "proposal.json"
        output.write_bytes(b"prior")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.invoke(process)
        self.assertEqual(output.read_bytes(), b"prior")

    def test_wrapper_rejects_extra_input_fields_and_empty_prompt_before_process(self):
        for overrides in ({"command": ["unsafe"]}, {"prompt": ""}, {"output_schema": {}}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.invoke(lambda *a, **k: self.fail("must not launch"), **overrides)

    def test_host_and_wrapper_validate_same_descriptor_cases(self):
        validate = self.namespace()["validate_provider"]
        validate(copy.deepcopy(self.provider))
        for key in self.provider:
            value = {name: val for name, val in self.provider.items() if name != key}
            for function in (validate, validate_provider):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    function(value)


if __name__ == "__main__":
    unittest.main()
