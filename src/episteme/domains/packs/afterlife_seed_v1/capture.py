"""Read-only capture of one completed historical Afterlife run directory.

The only pack code that touches the filesystem. It reads the declared files
once, rejects links, reparse points and unlisted outputs, and returns their
bytes; the kernel alone writes them to CAS. Hashes identify the captured bytes,
not historical time, prior exposure or completeness of the source beyond the
legacy manifest's own 30 declared outputs.
"""

from pathlib import Path, PurePosixPath, PureWindowsPath
import stat

from episteme.domains import api

from . import inventory


def _unsafe(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _relative(name):
    inventory.require(type(name) is str and bool(name) and "\\" not in name and ":" not in name
                      and "\x00" not in name and not PureWindowsPath(name).is_absolute(),
                      "unsafe historical output path")
    result = PurePosixPath(name)
    inventory.require(not result.is_absolute() and result.as_posix() == name
                      and all(part not in {"", ".", ".."} for part in name.split("/")),
                      "unsafe historical output path")
    return result


def _path(root, name, *, directory=False):
    current = root
    parts = _relative(name).parts
    for index, part in enumerate(parts):
        current = current / part
        inventory.require(not _unsafe(current), f"symlink or reparse point in historical output: {name}")
        mode = current.lstat().st_mode
        expected_directory = directory or index < len(parts) - 1
        inventory.require(stat.S_ISDIR(mode) if expected_directory else stat.S_ISREG(mode),
                          f"historical output is not a regular {'directory' if expected_directory else 'file'}: {name}")
    inventory.require(current.resolve().is_relative_to(root), "historical output escapes source root")
    return current


def _read(root, name, limit):
    path = _path(root, name)
    inventory.require(path.stat().st_size <= limit, f"historical output exceeds size limit: {name}")
    with path.open("rb") as source:
        data = source.read(limit + 1)
    inventory.require(len(data) <= limit, f"historical output exceeds size limit: {name}")
    inventory.require(not _unsafe(path), f"historical output changed to a link: {name}")
    return data


def read_run(source, *, pack_id, pack_version):
    """Audit and read the run before anything is stored; any defect stops capture."""
    run = Path(source).absolute()
    inventory.require(run.is_dir() and not _unsafe(run), "historical run root must be a regular directory")
    for ancestor in run.parents:
        inventory.require(not _unsafe(ancestor), "historical run path traverses a link or reparse point")
    root = run.resolve()
    manifest_bytes = _read(root, inventory.MANIFEST_PATH, inventory.MAX_MANIFEST_BYTES)
    manifest = inventory.strict_json(manifest_bytes, "historical manifest")
    inventory.require(type(manifest) is dict and type(manifest.get("integrity")) is dict
                      and 10 <= len(manifest["integrity"]) <= 256,
                      "historical integrity inventory is incomplete")
    files = {inventory.MANIFEST_PATH: manifest_bytes}
    total = 0
    for name in sorted(manifest["integrity"]):
        data = _read(root, name, inventory.MAX_FILE_BYTES)
        total += len(data)
        inventory.require(total <= inventory.MAX_TOTAL_BYTES, "historical outputs exceed total capture limit")
        files[name] = data
    checked = inventory.verify(files)
    actual = {"config.resolved.yaml"}
    for directory, suffixes in (("data", (".parquet",)),
                                ("data/trajectories", (".steps.jsonl", ".text")),
                                ("requests", (".jsonl",))):
        for item in _path(root, directory, directory=True).iterdir():
            if item.name.endswith(suffixes):
                name = f"{directory}/{item.name}"
                _path(root, name)  # Reject unlisted links and non-regular files too.
                actual.add(name)
    inventory.require(actual == set(checked["integrity"]),
                      "historical run has a missing or unlisted trajectory, request, or parquet output")
    return api.CaptureBundle(
        pack_id=pack_id, pack_version=pack_version, source_label=checked["run_id"], files=files,
        audit=dict(declared_outputs=len(checked["integrity"]), trajectories=len(checked["trajectories"]),
                   steps=checked["observed_steps"], captured_output_bytes=checked["captured_bytes"]))
