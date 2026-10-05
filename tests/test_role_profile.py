"""Role profiles are records of isolation, not proof of independence.

The default suite does not start Docker and does not use the network. The
container process test is skipped unless EPISTEME_CONTAINER_TEST=1.
"""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from episteme.codec import digest
from episteme.commands import CommandService
from episteme.graph import ResearchGraph
from episteme.kernel import Actor, GateError, Kernel
from episteme.role_profile import (
    code_relation, counts_as_independent_replication, docker_available, local_profile,
    proves_implementation_withheld, request_container)
from episteme.store import Store

import test_review_assignment as assignment_fixtures


def _snapshot(store: Store) -> tuple[str, str, tuple[str, ...]]:
    names = tuple(sorted(path.name for path in store.blobs.iterdir() if path.is_file()))
    return store.export(), store.export_receipts(), names


def _launched(stdout: str = "ROOT_READONLY\nNET_BLOCKED\nEND_PROBE\nPAYLOAD_OK\n") -> dict:
    return dict(image_id="sha256:" + "ab" * 32, exit_code=0, stdout=stdout, stderr="",
                stdout_truncated=False,
                docker_args=["docker", "run", "--name", "episteme-test", "--rm",
                             "--network", "none", "--read-only", "--pull", "never",
                             "busybox:1.36.1"])


class RoleProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="episteme-role-profile-")
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.addCleanup(self.store.close)
        self.scope = {"dataset": "toy-v1", "split": "holdout", "population": "synthetic"}
        self.implementation = self.store.put(b"primary mean implementation v1")
        self.reimplementation = self.store.put(b"separate mean implementation v1")
        self.environment = self.store.put_json({"python": "3.11", "dependencies": []})
        self.data = self.store.put_json({"values": [1, 3]})
        self.note = Path(self.directory.name) / "note.txt"
        self.note.write_bytes(b"note-bytes")
        self.output = Path(self.directory.name) / "output.txt"
        self.output.write_bytes(b"output-bytes")
        self.code = digest(b"same-code")
        self.other = digest(b"other-code")

    def protocol(self) -> str:
        planner = Kernel(self.store, Actor("planner-1", "planner"))
        hypotheses = [planner.hypothesis(statement, prediction, falsifier, self.scope)
                      for statement, prediction, falsifier in (
                          ("The observed mean is positive", "mean > 0", "mean <= 0"),
                          ("The observed mean is nonpositive", "mean <= 0", "mean > 0"))]
        return planner.preregister(
            hypotheses=hypotheses, scope=self.scope,
            design="Compare the mean to zero on the fixed holdout", metric="mean",
            analysis_plan="Arithmetic mean over all saved values",
            stopping_rule="Execute every registered seed within the run budget",
            seeds=[7], run_limit=6, implementation=self.implementation,
            environment=self.environment, data=self.data, replication_tolerance=0.0001)

    def test_recorded_local_run_says_os_isolation_none(self):
        protocol = self.protocol()
        executor = Kernel(self.store, Actor("executor-1", "executor"))
        run = executor.start_run(protocol, seed=7, implementation=self.implementation,
                                  environment=self.environment, command=["python", "experiment.py"])
        profile = self.store.events()[-1]["payload"]["role_profile"]
        self.assertEqual(profile, local_profile("executor"))
        self.assertEqual(profile["backend"], "local_subprocess")
        self.assertEqual(profile["os_isolation"], "none")
        self.assertFalse(counts_as_independent_replication(profile))
        executor.finish_run(run, status="completed", outputs=dict(
            raw_data=self.data, metrics=self.store.put_json({"mean": 2.0}),
            log=self.store.put(b"done\n")))
        replica = Kernel(self.store, Actor("replicator-1", "replicator")).start_run(
            protocol, seed=7, implementation=self.reimplementation, environment=self.environment,
            command=["python", "other.py"], replicate_of=run)
        recorded = next(event["payload"]["role_profile"] for event in self.store.events()
                        if event["id"] == replica)
        self.assertEqual(recorded, local_profile("replicator"))
        self.assertEqual(recorded["os_isolation"], "none")
        self.assertIsNotNone(ResearchGraph.from_store(self.store))

    def test_code_relation_follows_the_digests_and_is_not_independence(self):
        self.assertEqual(code_relation(self.code, self.code), "same_code")
        self.assertEqual(code_relation(self.code, self.other), "different_code")
        self.assertEqual(code_relation(self.code, None), "not_compared")
        self.assertFalse(counts_as_independent_replication(local_profile("replicator")))
        self.assertFalse(proves_implementation_withheld(local_profile("reviewer")))

    def test_container_request_without_docker_writes_nothing(self):
        before = _snapshot(self.store)
        with patch("episteme.role_profile.docker_available", return_value=(False, "docker is not available")), \
                patch("episteme.role_profile.run_container") as launched:
            with self.assertRaisesRegex(GateError, "did not fall back to local_subprocess"):
                request_container(
                    self.store, actor="executor-1", role="executor", study_id="role-profile",
                    image="busybox:1.36.1", argv=["cat", "/note.txt"],
                    mounts=[dict(host=str(self.note), container="/note.txt", kind="payload")],
                    code_digest=self.code, reference_code_digest=self.code)
            launched.assert_not_called()
        self.assertEqual(_snapshot(self.store), before)
        direct = dict(
            context=dict(command_id="container-without-docker", expected_revision=len(self.store.events()),
                         actor="executor-1", role="executor", study_id="role-profile",
                         correlation_id="role-profile", causation_id=None),
            request=dict(version=1, action="isolation.execute", payload=dict(
                role="executor", backend="container", image="busybox:1.36.1",
                argv=["cat", "/note.txt"],
                mounts=[dict(host=str(self.note), container="/note.txt", kind="payload")],
                code_digest=self.code, observation="0" * 64)))
        with patch("episteme.role_profile.docker_available", return_value=(False, "docker is not available")):
            with self.assertRaisesRegex(GateError, "did not fall back to local_subprocess"):
                CommandService(self.store).execute(direct)
        self.assertEqual(_snapshot(self.store), before)

    def test_local_backend_is_not_a_silent_container_fallback(self):
        before = _snapshot(self.store)
        envelope = dict(
            context=dict(command_id="local-on-container-command", expected_revision=len(self.store.events()),
                         actor="executor-1", role="executor", study_id="role-profile",
                         correlation_id="role-profile", causation_id=None),
            request=dict(version=1, action="isolation.execute", payload=dict(
                role="executor", backend="local_subprocess", image="busybox:1.36.1",
                argv=["cat", "/note.txt"],
                mounts=[dict(host=str(self.note), container="/note.txt", kind="payload")],
                code_digest=self.code, observation="0" * 64)))
        with patch("episteme.role_profile.docker_available") as probe:
            with self.assertRaisesRegex(GateError, "does not fall back"):
                CommandService(self.store).execute(envelope)
            probe.assert_not_called()
        self.assertEqual(_snapshot(self.store), before)

    def test_reviewer_mount_cannot_include_implementation_or_pack_source(self):
        impl = Path(self.directory.name) / "implementation"
        pack = Path(self.directory.name) / "pack"
        impl.mkdir()
        pack.mkdir()
        inside = impl / "code.py"
        inside.write_bytes(b"secret implementation")
        before = _snapshot(self.store)
        with patch("episteme.role_profile.docker_available") as probe:
            with self.assertRaisesRegex(GateError, "implementation_tree"):
                request_container(
                    self.store, actor="planner-1", role="reviewer", study_id="role-profile",
                    image="busybox:1.36.1", argv=["cat", "/note.txt"],
                    mounts=[dict(host=str(self.note), container="/note.txt", kind="implementation_tree")],
                    code_digest=self.code, implementation_tree=str(impl), pack_source=str(pack))
            with self.assertRaisesRegex(GateError, "pack_source"):
                request_container(
                    self.store, actor="planner-1", role="reviewer", study_id="role-profile",
                    image="busybox:1.36.1", argv=["cat", "/note.txt"],
                    mounts=[dict(host=str(self.note), container="/note.txt", kind="pack_source")],
                    code_digest=self.code, implementation_tree=str(impl), pack_source=str(pack))
            with self.assertRaisesRegex(GateError, "implementation_tree"):
                request_container(
                    self.store, actor="planner-1", role="reviewer", study_id="role-profile",
                    image="busybox:1.36.1", argv=["cat", "/note.txt"],
                    mounts=[dict(host=str(inside), container="/note.txt", kind="payload")],
                    code_digest=self.code, implementation_tree=str(impl), pack_source=str(pack))
            probe.assert_not_called()
        self.assertEqual(_snapshot(self.store), before)

    def test_replicator_does_not_receive_the_executor_work_directory(self):
        before = _snapshot(self.store)
        with patch("episteme.role_profile.docker_available") as probe:
            with self.assertRaisesRegex(GateError, "work_directory"):
                request_container(
                    self.store, actor="replicator-1", role="replicator", study_id="role-profile",
                    image="busybox:1.36.1", argv=["cat", "/inputs.txt"],
                    mounts=[dict(host=str(self.note), container="/work", kind="work_directory"),
                            dict(host=str(self.output), container="/outputs.txt", kind="output_digest")],
                    code_digest=self.other, reference_code_digest=self.code,
                    inputs_digest=digest(b"note-bytes"), outputs_digest=digest(self.output.read_bytes()))
            with self.assertRaisesRegex(GateError, "inputs and outputs digests"):
                request_container(
                    self.store, actor="replicator-1", role="replicator", study_id="role-profile",
                    image="busybox:1.36.1", argv=["cat", "/inputs.txt"],
                    mounts=[dict(host=str(self.note), container="/inputs.txt", kind="input_digest"),
                            dict(host=str(self.output), container="/outputs.txt", kind="output_digest")],
                    code_digest=self.other, reference_code_digest=self.code)
            probe.assert_not_called()
        self.assertEqual(_snapshot(self.store), before)

    def test_mocked_container_run_records_same_code_without_calling_it_replication(self):
        protocol = self.protocol()
        with patch("episteme.role_profile.docker_available", return_value=(True, "test")), \
                patch("episteme.role_profile.run_container", return_value=_launched()):
            result = request_container(
                self.store, actor="executor-1", role="executor", study_id="role-profile",
                image="busybox:1.36.1", argv=["cat", "/note.txt"],
                mounts=[dict(host=str(self.note), container="/note.txt", kind="payload")],
                code_digest=self.code, reference_code_digest=self.code, protocol=protocol, seed=7,
                implementation=self.implementation, environment=self.environment,
                command=["python", "experiment.py"], command_id="container-same")
        run = next(event for event in self.store.events() if event["id"] == result["run"])
        profile = run["payload"]["role_profile"]
        self.assertEqual(profile["backend"], "container")
        self.assertEqual(profile["os_isolation"], "container")
        self.assertEqual(profile["host_user"], "same")
        self.assertEqual(profile["code_relation"], "same_code")
        self.assertFalse(profile["independent_replication"])
        self.assertFalse(profile["proves_implementation_withheld"])
        self.assertFalse(counts_as_independent_replication(profile))
        self.assertEqual(profile["mounts"], [dict(container="/note.txt", kind="payload",
                                                  digest=digest(b"note-bytes"))])
        self.assertNotIn(str(self.note), json.dumps(profile))
        self.assertIsNotNone(ResearchGraph.from_store(self.store))
        receipt = self.store.receipts()[-1]
        again = CommandService(self.store).execute(dict(context=receipt["context"], request=receipt["request"]))
        self.assertEqual(again, result)
        self.assertEqual(sum(event["kind"] == "run" for event in self.store.events()), 1)

    def test_different_code_digest_is_still_not_independent_replication(self):
        protocol = self.protocol()
        with patch("episteme.role_profile.docker_available", return_value=(True, "test")), \
                patch("episteme.role_profile.run_container", return_value=_launched()):
            result = request_container(
                self.store, actor="executor-1", role="executor", study_id="role-profile",
                image="busybox:1.36.1", argv=["cat", "/note.txt"],
                mounts=[dict(host=str(self.note), container="/note.txt", kind="payload")],
                code_digest=self.other, reference_code_digest=self.code, protocol=protocol, seed=7,
                implementation=self.implementation, environment=self.environment,
                command=["python", "experiment.py"])
        profile = next(event["payload"]["role_profile"] for event in self.store.events()
                       if event["id"] == result["run"])
        self.assertEqual(profile["code_relation"], "different_code")
        self.assertFalse(profile["independent_replication"])
        self.assertFalse(counts_as_independent_replication(profile))

    def test_mocked_replicator_receives_digests_not_a_work_directory(self):
        protocol = self.protocol()
        primary = Kernel(self.store, Actor("executor-1", "executor")).start_run(
            protocol, seed=7, implementation=self.implementation, environment=self.environment,
            command=["python", "experiment.py"])
        Kernel(self.store, Actor("executor-1", "executor")).finish_run(
            primary, status="completed", outputs=dict(
                raw_data=self.data, metrics=self.store.put_json({"mean": 2.0}),
                log=self.store.put(b"done\n")))
        with patch("episteme.role_profile.docker_available", return_value=(True, "test")), \
                patch("episteme.role_profile.run_container", return_value=_launched()):
            result = request_container(
                self.store, actor="replicator-1", role="replicator", study_id="role-profile",
                image="busybox:1.36.1", argv=["cat", "/inputs.txt"],
                mounts=[dict(host=str(self.note), container="/inputs.txt", kind="input_digest"),
                        dict(host=str(self.output), container="/outputs.txt", kind="output_digest")],
                code_digest=self.other, reference_code_digest=self.code,
                inputs_digest=digest(b"note-bytes"), outputs_digest=digest(b"output-bytes"),
                protocol=protocol, seed=7, implementation=self.reimplementation,
                environment=self.environment, command=["python", "other.py"], replicate_of=primary)
        profile = next(event["payload"]["role_profile"] for event in self.store.events()
                       if event["id"] == result["run"])
        self.assertEqual(profile["role"], "replicator")
        self.assertEqual(profile["inputs_digest"], digest(b"note-bytes"))
        self.assertEqual(profile["outputs_digest"], digest(b"output-bytes"))
        self.assertEqual([item["kind"] for item in profile["mounts"]], ["input_digest", "output_digest"])
        self.assertFalse(profile["independent_replication"])
        self.assertIsNotNone(ResearchGraph.from_store(self.store))


class ReviewerRoleProfileTests(unittest.TestCase):
    def setUp(self):
        assignment_fixtures.ReviewAssignmentTests.setUp(self)
        self.assignment = self.service.execute(
            assignment_fixtures.ReviewAssignmentTests.envelope(self))["assignment"]
        self.note = Path(self.root) / "outside.txt"
        self.note.write_bytes(b"outside-bytes")
        self.impl = Path(self.root) / "implementation"
        self.pack = Path(self.root) / "pack-source"
        self.impl.mkdir()
        self.pack.mkdir()
        self.code = digest(b"reviewer-code")

    def test_local_reviewer_delivery_does_not_prove_the_implementation_was_withheld(self):
        before = len(self.store.events())
        envelope = dict(
            context=dict(command_id="local-review-delivery", expected_revision=before,
                         actor="fixture-planner", role="planner", study_id="fixture-study",
                         correlation_id="fixture-cycle", causation_id=None),
            request=dict(version=1, action="review.dispatch", payload=dict(
                assignment=self.assignment, provider_id="fixture-provider-v1")))
        CommandService(self.store).execute(envelope)
        delivery = next(event for event in self.store.events() if event["kind"] == "review_dispatch")
        profile = delivery["payload"]["role_profile"]
        self.assertEqual(profile, local_profile("reviewer"))
        self.assertEqual(profile["backend"], "local_subprocess")
        self.assertEqual(profile["os_isolation"], "none")
        self.assertEqual(delivery["payload"]["read_isolation"], "not_enforced")
        self.assertFalse(profile["proves_implementation_withheld"])
        self.assertFalse(proves_implementation_withheld(profile))
        run = next(event for event in self.store.events()
                   if event["kind"] == "run" and event["payload"]["replicate_of"] is None)
        self.assertEqual(run["payload"]["role_profile"]["os_isolation"], "none")
        self.assertIsNotNone(ResearchGraph.from_store(self.store))

    def test_mocked_container_reviewer_delivery_still_does_not_prove_withholding(self):
        with patch("episteme.role_profile.docker_available", return_value=(True, "test")), \
                patch("episteme.role_profile.run_container", return_value=_launched()):
            result = request_container(
                self.store, actor="fixture-planner", role="reviewer", study_id="fixture-study",
                image="busybox:1.36.1", argv=["cat", "/note.txt"],
                mounts=[dict(host=str(self.note), container="/note.txt", kind="payload")],
                code_digest=self.code, reference_code_digest=self.code,
                implementation_tree=str(self.impl), pack_source=str(self.pack),
                assignment=self.assignment, provider_id="fixture-provider-v1")
        delivery = next(event for event in self.store.events() if event["id"] == result["dispatch"])
        profile = delivery["payload"]["role_profile"]
        self.assertEqual(delivery["kind"], "review_dispatch")
        self.assertEqual(profile["backend"], "container")
        self.assertEqual(profile["role"], "reviewer")
        self.assertEqual(profile["code_relation"], "same_code")
        self.assertFalse(profile["proves_implementation_withheld"])
        self.assertFalse(proves_implementation_withheld(profile))
        self.assertFalse(counts_as_independent_replication(profile))
        self.assertEqual(delivery["payload"]["read_isolation"], "not_enforced")
        self.assertNotIn("implementation_tree", {item["kind"] for item in profile["mounts"]})
        self.assertNotIn("pack_source", {item["kind"] for item in profile["mounts"]})
        self.assertIsNotNone(ResearchGraph.from_store(self.store))


class ContainerRoleProfileTests(unittest.TestCase):
    def test_container_observes_readonly_root_no_network_and_the_mount_list(self):
        if os.environ.get("EPISTEME_CONTAINER_TEST") != "1":
            available, detail = docker_available()
            if not available:
                self.skipTest(detail + ". The container backend test is skipped on this host.")
            self.skipTest("EPISTEME_CONTAINER_TEST is not set; the default suite does not run Docker.")
        directory = tempfile.TemporaryDirectory(prefix="episteme-container-role-")
        self.addCleanup(directory.cleanup)
        store = Store(directory.name)
        self.addCleanup(store.close)
        note = Path(directory.name) / "note.txt"
        note.write_bytes(b"container-note")
        scope = {"dataset": "toy-v1", "split": "holdout", "population": "synthetic"}
        implementation = store.put(b"primary mean implementation v1")
        environment = store.put_json({"python": "3.11", "dependencies": []})
        data = store.put_json({"values": [1, 3]})
        planner = Kernel(store, Actor("planner-1", "planner"))
        hypotheses = [planner.hypothesis(statement, prediction, falsifier, scope)
                      for statement, prediction, falsifier in (
                          ("The observed mean is positive", "mean > 0", "mean <= 0"),
                          ("The observed mean is nonpositive", "mean <= 0", "mean > 0"))]
        protocol = planner.preregister(
            hypotheses=hypotheses, scope=scope, design="Compare the mean to zero on the fixed holdout",
            metric="mean", analysis_plan="Arithmetic mean over all saved values",
            stopping_rule="Execute every registered seed within the run budget", seeds=[7],
            run_limit=4, implementation=implementation, environment=environment, data=data,
            replication_tolerance=0.0001)
        code = digest(b"container-same-code")
        before = len(store.events())
        result = request_container(
            store, actor="executor-1", role="executor", study_id="role-profile",
            image="busybox:1.36.1", argv=["cat", "/etc/note.txt"],
            mounts=[dict(host=str(note), container="/etc/note.txt", kind="payload")],
            code_digest=code, reference_code_digest=code, protocol=protocol, seed=7,
            implementation=implementation, environment=environment, command=["python", "experiment.py"])
        self.assertEqual(len(store.events()), before + 1)
        run = store.events()[-1]
        self.assertEqual(run["id"], result["run"])
        profile = run["payload"]["role_profile"]
        self.assertEqual(profile["backend"], "container")
        self.assertEqual(profile["os_isolation"], "container")
        self.assertEqual(profile["host_user"], "same")
        self.assertEqual(profile["observed_root"], "read_only")
        self.assertEqual(profile["observed_network"], "blocked")
        self.assertEqual(profile["network_enforcement"], "disabled")
        self.assertEqual(profile["filesystem_enforcement"], "container_root_read_only")
        self.assertEqual(profile["requested_network"], "none")
        self.assertEqual(profile["requested_root"], "read_only")
        self.assertEqual(profile["code_relation"], "same_code")
        self.assertFalse(profile["independent_replication"])
        self.assertFalse(profile["sandbox"])
        self.assertFalse(counts_as_independent_replication(profile))
        self.assertEqual(profile["mounts"], [dict(container="/etc/note.txt", kind="payload",
                                                  digest=digest(b"container-note"))])
        observation = json.loads(store.read(profile["observation"]))
        args = observation["docker_args"]
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertIn("--read-only", args)
        self.assertEqual(args[args.index("--pull") + 1], "never")
        self.assertIn("ROOT_READONLY", observation["stdout"])
        self.assertIn("NET_BLOCKED", observation["stdout"])
        self.assertIn("container-note", observation["stdout"])
        self.assertTrue(profile["image_id"].startswith("sha256:"))
        self.assertIsNotNone(ResearchGraph.from_store(store))


if __name__ == "__main__":
    unittest.main()
