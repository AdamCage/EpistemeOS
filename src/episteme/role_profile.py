"""Role profiles recorded on a run or a review delivery.

``local_subprocess`` is the backend existing commands already use: the same OS
user, no sandbox, no network enforcement. A role name on that backend does not
prove the reviewer lacked the implementation, and it does not prove independent
replication.

``container`` is optional. It starts the payload with ``docker run --network none
--read-only`` and an allowlisted file mount list. If Docker is absent the request
fails before any event is written. It does not fall back to ``local_subprocess``.
The container still runs as this host user. A container result is same-code
execution unless the recorded code digests differ, and neither case is independent
replication. Only a container this process actually started, and only the probe
lines it printed, support the observed root and network fields.
"""

from __future__ import annotations

import json
from pathlib import Path
import stat
import subprocess
from typing import Any
from uuid import uuid4

from .codec import canonical, digest


ROLES = frozenset({"executor", "reviewer", "replicator"})
_DIGEST = r"[0-9a-f]{64}"
_IMAGE_ID = r"sha256:[0-9a-f]{64}"
_MAX_MOUNTS = 16
_MAX_ARGV = 32
_MAX_TEXT = 4096
_MAX_FILE = 1024 * 1024
_MAX_CAPTURE = 64 * 1024
_PROBE = (
    "if touch /episteme-root-probe 2>/dev/null; then echo ROOT_WRITABLE; "
    "else echo ROOT_READONLY; fi\n"
    "if ! command -v wget >/dev/null 2>&1; then echo NET_UNCHECKED\n"
    "elif wget -q -T 3 -O - http://example.com >/dev/null 2>&1; then echo NET_OPEN\n"
    "else echo NET_BLOCKED\n"
    "fi\necho END_PROBE\n"
)
_FORBIDDEN = frozenset({"implementation_tree", "pack_source", "work_directory"})
_EXECUTOR_KINDS = frozenset({"payload", "input_digest", "output_digest"})
_REVIEWER_KINDS = frozenset({"payload", "input_digest", "output_digest"})


def _fail(message: str) -> None:
    from .kernel import GateError
    raise GateError(message)


def local_profile(role: str) -> dict[str, Any]:
    """The profile existing commands record. It describes this OS user, not a sandbox."""
    if role not in ROLES:
        _fail("unknown role profile")
    return {
        "schema_version": 1,
        "role": role,
        "backend": "local_subprocess",
        "os_isolation": "none",
        "host_user": "same",
        "network_enforcement": "not_enforced",
        "filesystem_enforcement": "not_enforced",
        "sandbox": False,
        "code_relation": "not_compared",
        "independent_replication": False,
        "proves_implementation_withheld": False,
    }


def proves_implementation_withheld(profile: dict[str, Any]) -> bool:
    """A role label, a local delivery, or a container mount list is not this proof."""
    return False


def counts_as_independent_replication(profile: dict[str, Any]) -> bool:
    """Same-code and different-code container runs are still not independent replication."""
    return False


def code_relation(code_digest: str, reference: str | None) -> str:
    if reference is None:
        return "not_compared"
    if code_digest == reference:
        return "same_code"
    return "different_code"


def _text(value: Any, label: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > _MAX_TEXT:
        _fail(f"{label} must be bounded nonempty text")
    return value


def _digest_text(value: Any, label: str) -> str:
    import re
    text = _text(value, label)
    if re.fullmatch(_DIGEST, text) is None:
        _fail(f"{label} must be a sha256 hex digest")
    return text


def _optional_digest(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _digest_text(value, label)


def docker_available() -> tuple[bool, str]:
    """Probe the local Docker daemon. This does not pull an image."""
    try:
        completed = subprocess.run(
            ["docker", "info", "-f", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"docker is not available: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "no server").strip()
        return False, f"docker is not available: {detail}"
    version = (completed.stdout or "").strip()
    if not version:
        return False, "docker is not available: empty server version"
    return True, version


def _container_path(value: str) -> str:
    parts = value.split("/")
    if (not value.startswith("/") or "\\" in value or value.startswith("//")
            or len(parts) < 2 or any(part in {"", ".", ".."} for part in parts[1:])):
        _fail("container mount path must be an absolute posix path")
    return value


def _host_path(value: str) -> Path:
    if any(token in value for token in (",", "=", "\n", "\r")):
        _fail("mount host path contains a character the mount syntax cannot quote")
    path = Path(value)
    if not path.is_absolute():
        _fail("mount host path must be absolute")
    return path


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def validate_mount_policy(payload: dict[str, Any]) -> None:
    """Reject a container request whose mount list breaks the role rule.

    This does not start Docker and does not write an event. A reviewer mount
    cannot be the implementation tree or the pack source, including by path
    overlap with the trees the request names. A replicator receives input and
    output digest files, not the executor work directory.
    """
    role = payload.get("role")
    if role not in ROLES:
        _fail("unknown role profile")
    if payload.get("backend") != "container":
        _fail("isolation.execute does not run local_subprocess and does not fall back to it")
    import re
    image = payload.get("image")
    if type(image) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,200}", image) is None:
        _fail("container image name is not a bounded docker reference")
    if ".." in image.split("/"):
        _fail("container image name is not a bounded docker reference")
    argv = payload.get("argv")
    if (type(argv) is not list or not argv or len(argv) > _MAX_ARGV
            or any(type(item) is not str or not item or len(item) > _MAX_TEXT for item in argv)):
        _fail("container argv must be a short list of nonempty strings")
    mounts = payload.get("mounts")
    if type(mounts) is not list or len(mounts) > _MAX_MOUNTS:
        _fail("container mounts must be a short list")
    _digest_text(payload.get("code_digest"), "code_digest")
    _optional_digest(payload.get("reference_code_digest"), "reference_code_digest")
    inputs = _optional_digest(payload.get("inputs_digest"), "inputs_digest")
    outputs = _optional_digest(payload.get("outputs_digest"), "outputs_digest")
    trees = {}
    for label in ("implementation_tree", "pack_source"):
        value = payload.get(label)
        if value is None:
            trees[label] = None
            continue
        trees[label] = _host_path(_text(value, label))
    kinds = []
    for mount in mounts:
        if type(mount) is not dict or set(mount) != {"host", "container", "kind"}:
            _fail("a mount needs host, container and kind")
        kind = mount["kind"]
        if type(kind) is not str or kind not in _EXECUTOR_KINDS | _REVIEWER_KINDS | _FORBIDDEN:
            _fail("unknown mount kind")
        kinds.append(kind)
        host = _host_path(_text(mount["host"], "mount host"))
        _container_path(_text(mount["container"], "mount container"))
        if kind in _FORBIDDEN:
            _fail("this role cannot mount " + kind)
        if role == "reviewer":
            if kind not in _REVIEWER_KINDS:
                _fail("reviewer mounts must not include the implementation tree or the pack source")
            for label, root in trees.items():
                if root is not None and _within(host, root):
                    _fail(f"reviewer mount overlaps the declared {label}")
        elif role == "replicator" and kind not in {"input_digest", "output_digest"}:
            _fail("replicator mounts are only input and output digests")
        elif role == "executor" and kind not in _EXECUTOR_KINDS:
            _fail("executor mount kind is not allowlisted")
    if role == "reviewer" and (trees["implementation_tree"] is None or trees["pack_source"] is None):
        _fail("reviewer container request must name the implementation tree and the pack source")
    if role == "replicator":
        if inputs is None or outputs is None:
            _fail("replicator requires inputs and outputs digests")
        if kinds.count("input_digest") != 1 or kinds.count("output_digest") != 1 or len(kinds) != 2:
            _fail("replicator receives one input digest file and one output digest file")
        if "work_directory" in kinds:
            _fail("replicator must not receive the executor work directory")


def _hash_mounts(payload: dict[str, Any]) -> list[dict[str, str]]:
    prepared = []
    for mount in payload["mounts"]:
        path = _host_path(mount["host"])
        try:
            info = path.lstat()
        except OSError as exc:
            _fail(f"mount host path is not a file: {exc}")
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_size > _MAX_FILE:
            _fail("mount host path must be a regular file within 1 MiB")
        try:
            data = path.read_bytes()
        except OSError as exc:
            _fail(f"mount host path is not readable: {exc}")
        if len(data) != info.st_size:
            _fail("mount host path changed while it was read")
        prepared.append(dict(container=_container_path(mount["container"]), kind=mount["kind"],
                             digest=digest(data)))
    if payload["role"] == "replicator":
        by_kind = {item["kind"]: item["digest"] for item in prepared}
        if (by_kind.get("input_digest") != payload["inputs_digest"]
                or by_kind.get("output_digest") != payload["outputs_digest"]):
            _fail("replicator mount bytes do not match the declared inputs and outputs digests")
    return prepared


def _parse_probe(stdout: str) -> dict[str, str]:
    root, network = "not_observed", "not_observed"
    for line in stdout.splitlines():
        if line == "END_PROBE":
            break
        if line == "ROOT_READONLY":
            root = "read_only"
        elif line == "ROOT_WRITABLE":
            root = "writable"
        elif line == "NET_BLOCKED":
            network = "blocked"
        elif line == "NET_OPEN":
            network = "open"
    return {"observed_root": root, "observed_network": network}


def _flags(args: list[str]) -> None:
    try:
        network = args[args.index("--network") + 1]
        pull = args[args.index("--pull") + 1]
    except (ValueError, IndexError):
        _fail("container invocation does not request read-only root, no network, and no pull")
    if "--read-only" not in args or network != "none" or pull != "never":
        _fail("container invocation does not request read-only root, network none, and pull never")


def profile_from_observation(observation: dict[str, Any], observation_digest: str) -> dict[str, Any]:
    """Rebuild the event profile from the stored observation. Digests decide code_relation."""
    import re
    if type(observation) is not dict or observation.get("schema_version") != 1:
        _fail("container observation has the wrong shape")
    role = observation.get("role")
    if role not in ROLES:
        _fail("container observation has the wrong role")
    image_id = observation.get("image_id")
    if type(image_id) is not str or re.fullmatch(_IMAGE_ID, image_id) is None:
        _fail("container observation has no image id")
    if type(observation.get("exit_code")) is not int:
        _fail("container observation has no exit code")
    mounts = observation.get("mounts")
    if type(mounts) is not list:
        _fail("container observation has no mount list")
    relation = code_relation(observation.get("code_digest"), observation.get("reference_code_digest"))
    observed_root = observation.get("observed_root")
    observed_network = observation.get("observed_network")
    if observed_root not in {"read_only", "writable", "not_observed"}:
        _fail("container observation has no root observation")
    if observed_network not in {"blocked", "open", "not_observed"}:
        _fail("container observation has no network observation")
    return {
        "schema_version": 1,
        "role": role,
        "backend": "container",
        "os_isolation": "container",
        "host_user": "same",
        "sandbox": False,
        "requested_network": "none",
        "requested_root": "read_only",
        "observed_root": observed_root,
        "observed_network": observed_network,
        "network_enforcement": "disabled" if observed_network == "blocked" else "not_observed",
        "filesystem_enforcement": ("container_root_read_only" if observed_root == "read_only"
                                   else "not_observed"),
        "mounts": mounts,
        "image": observation.get("image"),
        "image_id": image_id,
        "code_digest": observation.get("code_digest"),
        "reference_code_digest": observation.get("reference_code_digest"),
        "code_relation": relation,
        "inputs_digest": observation.get("inputs_digest"),
        "outputs_digest": observation.get("outputs_digest"),
        "independent_replication": False,
        "proves_implementation_withheld": False,
        "observation": observation_digest,
    }


def _load_observation(store: Any, payload: dict[str, Any]) -> dict[str, Any]:
    key = payload.get("observation")
    _digest_text(key, "observation")
    try:
        raw = store.read(key)
    except Exception as exc:
        _fail(f"container observation is not in the store: {exc}")
    try:
        observation = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        _fail(f"container observation is not json: {exc}")
    if canonical(observation) != raw:
        _fail("container observation is not canonical")
    prepared = _hash_mounts(payload)
    if observation.get("mounts") != prepared:
        _fail("container observation mounts do not match the request files")
    for field in ("role", "image", "argv", "code_digest", "reference_code_digest",
                  "inputs_digest", "outputs_digest"):
        if observation.get(field) != payload.get(field):
            _fail(f"container observation {field} does not match the request")
    if type(observation.get("docker_args")) is not list:
        _fail("container observation has no invocation")
    _flags([item for item in observation["docker_args"] if type(item) is str])
    parsed = _parse_probe(observation.get("stdout") if type(observation.get("stdout")) is str else "")
    if (observation.get("observed_root") != parsed["observed_root"]
            or observation.get("observed_network") != parsed["observed_network"]):
        _fail("container observation does not match its probe output")
    profile_from_observation(observation, key)
    return observation


def preflight_container(store: Any, payload: dict[str, Any]) -> None:
    """Refuse a new container command before the write transaction when Docker is absent."""
    validate_mount_policy(payload)
    available, reason = docker_available()
    if not available:
        _fail(reason + "; backend=container was not run and did not fall back to local_subprocess")
    _load_observation(store, payload)


def run_container(*, image: str, argv: list[str], mounts: list[dict[str, str]]) -> dict[str, Any]:
    """Start one container. The caller has already checked that Docker is present."""
    name = "episteme-" + uuid4().hex[:16]
    args = ["docker", "run", "--name", name, "--rm", "--network", "none", "--read-only",
            "--pull", "never"]
    for mount in mounts:
        args.extend(["--mount",
                     f"type=bind,source={mount['host']},target={mount['container']},readonly"])
    args.append(image)
    args.extend(["sh", "-c", _PROBE + 'exec "$@"\n', "episteme-payload", *argv])
    try:
        completed = subprocess.run(args, capture_output=True, timeout=60, check=False)
    except subprocess.TimeoutExpired as exc:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20, check=False)
        _fail(f"container timed out; no role-profile event was written: {exc}")
    except OSError as exc:
        _fail(f"docker could not start a container: {exc}")
    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")
    if "END_PROBE" not in stdout.splitlines():
        detail = stderr.strip() or stdout.strip() or f"exit {completed.returncode}"
        _fail("container did not start; no role-profile event was written: " + detail[:500])
    inspect = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        capture_output=True, text=True, timeout=20, check=False)
    image_id = (inspect.stdout or "").strip()
    import re
    if inspect.returncode != 0 or re.fullmatch(_IMAGE_ID, image_id) is None:
        _fail("container image id could not be read; no role-profile event was written")
    truncated = False
    if len(stdout) > _MAX_CAPTURE:
        stdout, truncated = stdout[:_MAX_CAPTURE], True
    if len(stderr) > _MAX_CAPTURE:
        stderr = stderr[:_MAX_CAPTURE]
    return dict(image_id=image_id, exit_code=completed.returncode, stdout=stdout, stderr=stderr,
                docker_args=args, stdout_truncated=truncated)


def _observation(payload: dict[str, Any], mounts: list[dict[str, str]],
                 launched: dict[str, Any]) -> dict[str, Any]:
    parsed = _parse_probe(launched["stdout"])
    return {
        "schema_version": 1,
        "role": payload["role"],
        "image": payload["image"],
        "image_id": launched["image_id"],
        "exit_code": launched["exit_code"],
        "stdout": launched["stdout"],
        "stderr": launched["stderr"],
        "stdout_truncated": launched["stdout_truncated"],
        "argv": payload["argv"],
        "docker_args": launched["docker_args"],
        "code_digest": payload["code_digest"],
        "reference_code_digest": payload["reference_code_digest"],
        "inputs_digest": payload["inputs_digest"],
        "outputs_digest": payload["outputs_digest"],
        "mounts": mounts,
        "observed_root": parsed["observed_root"],
        "observed_network": parsed["observed_network"],
    }


def request_container(store: Any, *, actor: str, role: str, study_id: str, image: str,
                      argv: list[str], mounts: list[dict[str, str]], code_digest: str,
                      reference_code_digest: str | None = None,
                      inputs_digest: str | None = None, outputs_digest: str | None = None,
                      implementation_tree: str | None = None, pack_source: str | None = None,
                      protocol: str | None = None, seed: int | None = None,
                      implementation: str | None = None, environment: str | None = None,
                      command: list[str] | None = None, replicate_of: str | None = None,
                      assignment: str | None = None, provider_id: str | None = None,
                      command_id: str | None = None) -> dict[str, str]:
    """Run a container, then record the profile. Docker absence writes nothing."""
    from .commands import CommandService
    payload = dict(
        role=role, backend="container", image=image, argv=argv, mounts=mounts,
        code_digest=code_digest, reference_code_digest=reference_code_digest,
        inputs_digest=inputs_digest, outputs_digest=outputs_digest,
        implementation_tree=implementation_tree, pack_source=pack_source,
        protocol=protocol, seed=seed, implementation=implementation, environment=environment,
        command=command, replicate_of=replicate_of, assignment=assignment,
        provider_id=provider_id, observation="0" * 64)
    validate_mount_policy(payload)
    available, reason = docker_available()
    if not available:
        _fail(reason + "; backend=container was not run and did not fall back to local_subprocess")
    prepared = _hash_mounts(payload)
    launched = run_container(image=image, argv=argv, mounts=mounts)
    observation = _observation(payload, prepared, launched)
    payload["observation"] = store.put_json(observation)
    command_role = "planner" if role == "reviewer" else role
    envelope = dict(
        context=dict(command_id=command_id or f"isolation-{uuid4().hex}",
                     expected_revision=len(store.events()), actor=actor, role=command_role,
                     study_id=study_id, correlation_id="role-profile", causation_id=None),
        request=dict(version=1, action="isolation.execute", payload=payload))
    return CommandService(store).execute(envelope)


def check_profile_for_run(profile: dict[str, Any], role: str) -> None:
    """Accept the local profile or a container profile that does not claim independence."""
    if profile == local_profile(role):
        return
    if not isinstance(profile, dict) or profile.get("backend") != "container":
        _fail("run role profile is not local_subprocess or container")
    if (profile.get("role") != role or profile.get("os_isolation") != "container"
            or profile.get("host_user") != "same" or profile.get("sandbox") is not False
            or profile.get("independent_replication") is not False
            or profile.get("proves_implementation_withheld") is not False):
        _fail("container run profile overclaims isolation or independence")
    if profile.get("code_relation") != code_relation(
            profile.get("code_digest"), profile.get("reference_code_digest")):
        _fail("container code relation does not match the recorded digests")


def validate_run_profile(store: Any, payload: dict[str, Any]) -> None:
    """Graph check. Historical runs without a profile stay valid and claim no isolation.

    The observation blob is content-addressed. Rebuilding the profile from those
    bytes catches a record that does not match the container that was stored.
    Host paths are not needed again: they are not part of the run profile.
    """
    if "role_profile" not in payload or payload["role_profile"] is None:
        return
    profile = payload["role_profile"]
    role = "replicator" if payload.get("replicate_of") else "executor"
    if not isinstance(profile, dict):
        _fail("run role profile must be an object")
    if profile.get("backend") == "local_subprocess":
        if profile != local_profile(role):
            _fail("local run role profile does not match the recorded role")
        return
    if profile.get("backend") != "container":
        _fail("unknown run role profile backend")
    try:
        raw = store.read(profile.get("observation"))
    except Exception as exc:
        _fail(f"container observation is not in the store: {exc}")
    try:
        observation = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        _fail(f"container observation is not json: {exc}")
    if canonical(observation) != raw:
        _fail("container observation is not canonical")
    if profile != profile_from_observation(observation, profile["observation"]):
        _fail("container run role profile does not match its observation")


class Isolation:
    """Container backend command. The handler does not start Docker; preflight already did."""

    def __init__(self, store: Any, actor: Any):
        self.store, self.actor = store, actor

    def execute(self, *, role: str, backend: str, image: str, argv: list[str],
                mounts: list[dict[str, str]], code_digest: str, observation: str,
                reference_code_digest: str | None = None, inputs_digest: str | None = None,
                outputs_digest: str | None = None, implementation_tree: str | None = None,
                pack_source: str | None = None, protocol: str | None = None,
                seed: int | None = None, implementation: str | None = None,
                environment: str | None = None, command: list[str] | None = None,
                replicate_of: str | None = None, assignment: str | None = None,
                provider_id: str | None = None) -> dict[str, str]:
        from .kernel import Kernel, require
        payload = dict(
            role=role, backend=backend, image=image, argv=argv, mounts=mounts,
            code_digest=code_digest, reference_code_digest=reference_code_digest,
            inputs_digest=inputs_digest, outputs_digest=outputs_digest,
            implementation_tree=implementation_tree, pack_source=pack_source,
            observation=observation)
        validate_mount_policy(payload)
        observed = _load_observation(self.store, payload)
        profile = profile_from_observation(observed, observation)
        require(profile["backend"] == "container", "refusing to record local_subprocess from isolation.execute")
        context = self.store._command_context
        require(context is not None, "isolation.execute requires a command transaction")
        if role == "reviewer":
            require(context["role"] == "planner" and self.actor.role == "planner",
                    "a reviewer container delivery is recorded by the planner")
            require(type(assignment) is str and type(provider_id) is str,
                    "reviewer container delivery needs an assignment and a provider id")
            from .reviewer_controller import ReviewSession
            return ReviewSession(self.store, self.actor)._dispatch(
                assignment=assignment, provider_id=provider_id, profile=profile)
        require(context["role"] == role and self.actor.role == role,
                "container run actor does not match the role profile")
        require(protocol is not None and seed is not None and implementation is not None
                and environment is not None and command is not None,
                "container run needs a protocol, seed, implementation, environment and command")
        return dict(run=Kernel(self.store, self.actor)._start_run(
            protocol, seed=seed, implementation=implementation, environment=environment,
            command=command, replicate_of=replicate_of, batch_slot=None, role_profile=profile))
