"""Frozen Codex CLI proposal adapter for a trusted local account.

The fixed profile reduces inherited context and advertised capabilities. It is
not a filesystem sandbox or a proof that no tool can be exposed by a managed
client. The controller must audit the resulting JSONL before accepting a proposal.
No credentials or environment values are stored in the provider descriptor.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
import subprocess
from typing import Any

from .store import Store


_KEYS = {"schema_version", "provider", "profile_version", "executable",
         "executable_sha256", "cli_version", "model", "reasoning_effort"}
_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
_ENVIRONMENT_KEYS = {
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "HOME", "USERPROFILE",
    "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "TEMP", "TMP", "TMPDIR", "LANG",
    "LC_ALL", "LC_CTYPE", "SYSTEMDRIVE", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "CODEX_HOME",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
}

# Keep the security/permission instructions and execpolicy rules. A read-only
# sandbox still permits reads; disabling feature gates does not establish a
# universal no-tools guarantee (notably unified_exec on current Windows builds).
_PROFILE_CONFIG = {
    "approval_policy": "never",
    "web_search": "disabled",
    "project_root_markers": [],
    "project_doc_max_bytes": 0,
    "project_doc_fallback_filenames": [],
    "developer_instructions": "",
    "include_environment_context": False,
    "include_apps_instructions": False,
    "include_collaboration_mode_instructions": False,
    "skills.include_instructions": False,
    "skills.bundled.enabled": False,
    "memories.use_memories": False,
    "memories.generate_memories": False,
    "memories.dedicated_tools": False,
    "agents.enabled": False,
    "tools.update_plan.enabled": False,
    "tools.experimental_request_user_input.enabled": False,
    "features.skip_host_skill_discovery": True,
    **{f"features.{name}": False for name in (
        "shell_tool", "unified_exec", "code_mode", "code_mode_host", "apps",
        "plugins", "remote_plugin", "recommended_plugins", "hooks",
        "multi_agent", "multi_agent_v2", "multi_agent_mode", "browser_use",
        "computer_use", "view_image", "image_generation", "skill_search",
        "skill_mcp_dependency_install", "workspace_dependencies", "memories",
        "external_agent_memory_import", "chronicle", "goals", "sleep_tool",
        "tool_suggest", "default_mode_request_user_input",
    )},
}

# Preserve the first captured profile. Astra's bundled model descriptor forces
# code_mode_only despite feature-list values; its runtime host must be available.
# This enables runtime availability, never a promise of tool isolation.
_PROFILE_CONFIGS = {1: dict(_PROFILE_CONFIG), 2: dict(_PROFILE_CONFIG,
    **{"features.code_mode_host": True, "suppress_unstable_features_warning": True})}
_PROFILE_CONFIG = _PROFILE_CONFIGS[2]


def validate_provider(value: dict[str, Any]) -> None:
    """Validate a portable descriptor without consulting files or processes."""
    if type(value) is not dict or set(value) != _KEYS:
        raise ValueError("provider descriptor requires exactly the supported fields")
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["profile_version"]) is not int or value["profile_version"] not in {1, 2}
            or value["provider"] != "codex_cli_v1"):
        raise ValueError("unsupported Codex provider/profile version")
    for name in ("executable", "cli_version", "model", "reasoning_effort"):
        text = value[name]
        if (type(text) is not str or not text or text != text.strip()
                or any(ord(char) < 32 for char in text)):
            raise ValueError(f"invalid provider {name}")
    path = value["executable"]
    if not (PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()):
        raise ValueError("provider executable must be absolute")
    if (type(value["executable_sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", value["executable_sha256"]) is None):
        raise ValueError("invalid provider executable digest")
    if re.fullmatch(r"codex-cli [0-9][0-9A-Za-z.+_-]*", value["cli_version"]) is None:
        raise ValueError("invalid Codex CLI version")
    if value["reasoning_effort"] not in _EFFORTS:
        raise ValueError("unsupported reasoning effort")


def _environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items()
            if key.upper() in _ENVIRONMENT_KEYS}


def _executable_hash(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Codex executable must be a plain regular file")
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _version(path: Path) -> str:
    result = subprocess.run([str(path), "--version"], capture_output=True,
                            text=True, encoding="utf-8", errors="strict", timeout=10,
                            env=_environment(), check=False)
    if result.returncode != 0 or result.stderr.strip():
        raise ValueError("Codex CLI version probe failed")
    return result.stdout.strip()


def freeze_provider(store: Store, *, model: str, reasoning_effort: str = "low",
                    executable: str | Path | None = None) -> str:
    """Fingerprint an installed CLI; --version makes no model request."""
    selected = str(executable) if executable is not None else shutil.which("codex")
    if not selected:
        raise ValueError("Codex CLI executable was not found")
    # Do not resolve symlinks before checking the selected executable itself.
    path = Path(os.path.abspath(os.path.expanduser(selected)))
    before = _executable_hash(path)
    version = _version(path)
    if _executable_hash(path) != before:
        raise ValueError("Codex executable changed during the version probe")
    value = dict(schema_version=1, provider="codex_cli_v1", profile_version=2,
                 executable=str(path), executable_sha256=before, cli_version=version,
                 model=model, reasoning_effort=reasoning_effort)
    validate_provider(value)
    return store.put_json(value)


# This source is frozen as a runner input. It uses only the Python standard
# library under -I -S and receives no Store or scientific-state write interface.
PROGRAM = (r'''"""Trusted local Codex proposal wrapper; no strict no-tools guarantee."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import subprocess
import sys
import tempfile

KEYS = set(__KEYS__)
EFFORTS = __EFFORTS__
ENVIRONMENT_KEYS = __ENVIRONMENT_KEYS__
PROFILE_CONFIGS = __PROFILE_CONFIGS__


def validate_provider(value):
    if type(value) is not dict or set(value) != KEYS:
        raise ValueError("provider descriptor requires exactly the supported fields")
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["profile_version"]) is not int or value["profile_version"] not in {1, 2}
            or value["provider"] != "codex_cli_v1"):
        raise ValueError("unsupported Codex provider/profile version")
    for name in ("executable", "cli_version", "model", "reasoning_effort"):
        text = value[name]
        if (type(text) is not str or not text or text != text.strip()
                or any(ord(char) < 32 for char in text)):
            raise ValueError("invalid provider " + name)
    path = value["executable"]
    if not (PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()):
        raise ValueError("provider executable must be absolute")
    if (type(value["executable_sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", value["executable_sha256"]) is None):
        raise ValueError("invalid provider executable digest")
    if re.fullmatch(r"codex-cli [0-9][0-9A-Za-z.+_-]*", value["cli_version"]) is None:
        raise ValueError("invalid Codex CLI version")
    if value["reasoning_effort"] not in EFFORTS:
        raise ValueError("unsupported reasoning effort")


def executable_hash(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Codex executable must be a plain regular file")
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def main():
    value = json.loads(Path(sys.argv[1]).read_bytes())
    if type(value) is not dict or set(value) != {"provider", "prompt", "output_schema"}:
        raise ValueError("invalid Codex wrapper input")
    if type(value["prompt"]) is not str or not value["prompt"].strip():
        raise ValueError("proposal prompt must be nonempty text")
    if type(value["output_schema"]) is not dict or not value["output_schema"]:
        raise ValueError("proposal output schema must be a nonempty object")
    provider = value["provider"]
    validate_provider(provider)
    executable = Path(provider["executable"])
    if not executable.is_absolute():
        raise ValueError("frozen executable belongs to another operating system")
    if executable_hash(executable) != provider["executable_sha256"]:
        raise ValueError("frozen Codex executable digest mismatch")
    environment = {key: val for key, val in os.environ.items()
                   if key.upper() in ENVIRONMENT_KEYS}
    result = subprocess.run([str(executable), "--version"], capture_output=True,
                            text=True, encoding="utf-8", errors="strict", timeout=10,
                            env=environment, check=False)
    if (result.returncode != 0 or result.stderr.strip()
            or result.stdout.strip() != provider["cli_version"]):
        raise ValueError("frozen Codex CLI version mismatch")
    if executable_hash(executable) != provider["executable_sha256"]:
        raise ValueError("frozen Codex executable changed during preflight")
    output = Path.cwd() / "proposal.json"
    if os.path.lexists(output):
        raise ValueError("proposal output already exists")
    with tempfile.TemporaryDirectory(prefix="episteme-codex-") as directory:
        work = Path(directory)
        schema = work / "output-schema.json"
        schema.write_text(json.dumps(value["output_schema"], ensure_ascii=False,
                                    allow_nan=False), encoding="utf-8")
        command = [str(executable), "exec", "--ignore-user-config", "--ephemeral",
                   "--skip-git-repo-check", "--sandbox", "read-only", "--json",
                   "--color", "never", "--strict-config", "--model", provider["model"],
                   "--output-schema", str(schema), "--output-last-message", str(output)]
        config = dict(PROFILE_CONFIGS[provider["profile_version"]])
        config["model_reasoning_effort"] = provider["reasoning_effort"]
        # JSON scalar/array syntax used here is also valid TOML for these values.
        for key, val in config.items():
            command.extend(["-c", key + "=" + json.dumps(val, ensure_ascii=False)])
        command.append("-")
        result = subprocess.run(command, input=value["prompt"].encode("utf-8"),
                                cwd=work, env=environment, check=False)
        if result.returncode != 0:
            raise SystemExit(result.returncode)
    info = output.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("proposal output must be a plain regular file")


if __name__ == "__main__":
    main()
'''.replace("__KEYS__", repr(sorted(_KEYS)))
    .replace("__EFFORTS__", repr(sorted(_EFFORTS)))
    .replace("__ENVIRONMENT_KEYS__", repr(sorted(_ENVIRONMENT_KEYS)))
    .replace("__PROFILE_CONFIGS__", repr(_PROFILE_CONFIGS))).encode("utf-8")
