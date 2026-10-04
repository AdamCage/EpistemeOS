"""Offline fixtures for execution profile v2: a zipfile wheel, a uv lock, small programs.

Nothing here reaches the network. The wheel is built in the test with
METADATA/WHEEL/RECORD and locked from a relative find-links directory, so the
closure carries its own bytes. uv itself is required; tests that need it skip
with an explicit reason when it is absent.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import shutil
import subprocess
import zipfile


UV = shutil.which("uv")
UV_REASON = "uv is not on PATH; profile v2 environment tests need uv (offline, no downloads)"

PROGRAM = {
    "main.py": b'''import json, pathlib, sys
from helpers.stats import mean
if len(sys.argv) != 4 or sys.argv[2] != "--seed":
    raise SystemExit("expected INPUT --seed N")
raw = pathlib.Path(sys.argv[1]).read_bytes()
values = json.loads(raw)["values"]
pathlib.Path("raw.json").write_bytes(raw)
pathlib.Path("metrics.json").write_text(json.dumps({"mean": mean(values), "seed": int(sys.argv[3])}))
print("multi-file fixture completed")
''',
    "helpers/__init__.py": b"",
    "helpers/stats.py": b"def mean(values):\n    return sum(values) / len(values)\n",
}

REANALYSIS = {
    "reanalyse.py": b'''import json, math, pathlib, sys
from shared import load
raw = pathlib.Path(sys.argv[1]).read_bytes()
pathlib.Path("raw.json").write_bytes(raw)
values = load(raw)
pathlib.Path("metrics.json").write_text(json.dumps({"mean": math.fsum(values) / len(values),
                                                    "seed": int(sys.argv[3])}))
''',
    "shared.py": b"import json\n\ndef load(raw):\n    return json.loads(raw)['values']\n",
}

DEPENDENT = {
    "main.py": b'''import json, pathlib, sys
import episteme_fixture_dep
raw = pathlib.Path(sys.argv[1]).read_bytes()
pathlib.Path("raw.json").write_bytes(raw)
pathlib.Path("metrics.json").write_text(json.dumps({"mean": episteme_fixture_dep.answer(),
    "module": pathlib.Path(episteme_fixture_dep.__file__).parent.parent.name}))
''',
}


def _record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def build_wheel(directory: Path, name: str = "episteme_fixture_dep", version: str = "0.1.0",
                body: bytes = b"VALUE = 41\n\ndef answer():\n    return VALUE + 1\n") -> Path:
    """A minimal valid pure-Python wheel with one console script, built with zipfile."""
    directory.mkdir(parents=True, exist_ok=True)
    dist = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": body,
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode(),
        f"{dist}/WHEEL": b"Wheel-Version: 1.0\nGenerator: episteme-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        # The installer turns this into a launcher that embeds the venv interpreter path.
        f"{dist}/entry_points.txt": f"[console_scripts]\nepisteme-fixture = {name}:answer\n".encode(),
    }
    rows = [f"{path},{_record_hash(data)},{len(data)}" for path, data in files.items()]
    files[f"{dist}/RECORD"] = ("\n".join([*rows, f"{dist}/RECORD,,"]) + "\n").encode()
    wheel = directory / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in files.items():
            archive.writestr(zipfile.ZipInfo(path, date_time=(2020, 1, 1, 0, 0, 0)), data)
    return wheel


def locked_project(directory: Path, *, dependency: bool = False, python: str = ">=3.11") -> Path:
    """pyproject.toml plus a uv.lock produced offline by ``uv lock``."""
    directory.mkdir(parents=True, exist_ok=True)
    dependencies = '["episteme-fixture-dep==0.1.0"]' if dependency else "[]"
    extra = 'no-index = true\nfind-links = ["wheels"]\n' if dependency else ""
    if dependency:
        build_wheel(directory / "wheels")
    (directory / "pyproject.toml").write_text(
        f'[project]\nname = "research-program"\nversion = "0"\nrequires-python = "{python}"\n'
        f"dependencies = {dependencies}\n\n[tool.uv]\npackage = false\n{extra}", encoding="utf-8")
    subprocess.run([UV, "lock", "--offline", "--no-python-downloads"], cwd=directory, check=True,
                   capture_output=True, timeout=120)
    return directory
