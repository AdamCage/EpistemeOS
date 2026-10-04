"""Read-only capture of one training CSV and one holdout CSV.

This is the only module in the pack that touches the filesystem. It checks
that both files are small regular files with the pack's header and binary
labels, and it rejects a holdout that is the training file up to a BOM, CRLF
or trailing whitespace. It does not record the holdout label vector. Hashes
identify the captured bytes, not when the files were written or whether they
are a sample from a population.
"""

from pathlib import Path
import hashlib
import stat

from episteme.domains import api

from .model import parse_table


PACK_ID = "tabular_classification_v1"
PACK_VERSION = "1"
MAX_FILE_BYTES = 256 * 1024
NAMES = ("holdout.csv", "train.csv")


def _unsafe(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _lenient(data):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    text = text.removeprefix("\ufeff").replace("\r\n", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).rstrip().encode("utf-8")


def _read(root, name):
    path = root / name
    if _unsafe(path) or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"capture expects a regular file: {name}")
    if not path.resolve().is_relative_to(root):
        raise ValueError("capture file escapes the source root")
    with path.open("rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"capture file exceeds the size limit: {name}")
    if _unsafe(path):
        raise ValueError(f"capture file changed to a link: {name}")
    return data


def read_directory(source):
    """Return a CaptureBundle for exactly ``train.csv`` and ``holdout.csv``."""
    root = Path(source).absolute()
    if not root.is_dir() or _unsafe(root):
        raise ValueError("capture source must be a regular directory")
    for ancestor in root.parents:
        if _unsafe(ancestor):
            raise ValueError("capture path traverses a link or reparse point")
    root = root.resolve()
    names = []
    for item in root.iterdir():
        if _unsafe(item):
            raise ValueError(f"capture source contains a link: {item.name}")
        if not item.is_file():
            raise ValueError(f"capture source contains a non-file: {item.name}")
        names.append(item.name)
    if sorted(names) != list(NAMES):
        raise ValueError("capture source must contain exactly train.csv and holdout.csv")
    files = {name: _read(root, name) for name in NAMES}
    train_rows = parse_table(files["train.csv"])
    holdout_rows = len(parse_table(files["holdout.csv"]))
    if _lenient(files["train.csv"]) == _lenient(files["holdout.csv"]):
        raise ValueError("holdout file matches the training file up to whitespace and line endings")
    train_hash = hashlib.sha256(files["train.csv"]).hexdigest()
    holdout_hash = hashlib.sha256(files["holdout.csv"]).hexdigest()
    return api.CaptureBundle(
        pack_id=PACK_ID, pack_version=PACK_VERSION,
        source_label=f"csv-{train_hash[:12]}-{holdout_hash[:12]}", files=files,
        audit=dict(train_rows=len(train_rows), holdout_rows=holdout_rows,
                   columns=["x1", "x2", "label"], holdout_label_vector_recorded=False))
