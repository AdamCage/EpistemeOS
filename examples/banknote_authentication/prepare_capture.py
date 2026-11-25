"""Prepare the pinned UCI Banknote file for tabular_classification_v1 offline."""

from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from hashlib import sha256
import json
from pathlib import Path
import re


SOURCE_URL = "https://archive.ics.uci.edu/dataset/267/banknote%2Bauthentication"
DATASET_DOI = "10.24432/C55P57"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
ATTRIBUTION = (
    f"Lohweg, V. (2012). Banknote Authentication [Dataset]. UCI Machine Learning "
    f"Repository. https://doi.org/{DATASET_DOI}."
)
EXPECTED_ROWS = 1372
MAX_SOURCE_BYTES = 256 * 1024
SCALE = 100_000
RECIPE = "banknote-projected-pair-hash-v1"
_NUMERIC = re.compile(r"-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,2})?")


def _numeric(token: str) -> Decimal:
    if len(token) > 24 or not _NUMERIC.fullmatch(token):
        raise ValueError("banknote features must be plain decimal numbers")
    value = Decimal(token)
    if abs(value) > 1_000_000:
        raise ValueError("banknote feature magnitude is outside the import range")
    return value


def prepare_capture(raw: bytes, expected_sha256: str, *,
                    collapse_projected_pairs: bool = False) -> tuple[dict[str, bytes], dict]:
    """Return the immutable CSV pair and an attribution manifest for its parent."""
    if not 0 < len(raw) <= MAX_SOURCE_BYTES:
        raise ValueError("banknote file exceeds its input size limit or is empty")
    source_digest = sha256(raw).hexdigest()
    if expected_sha256 != source_digest:
        raise ValueError("banknote file does not match the pinned SHA-256")
    lines = raw.decode("ascii").splitlines()
    if len(lines) != EXPECTED_ROWS:
        raise ValueError(f"banknote file must contain {EXPECTED_ROWS} data rows")

    projected: list[tuple[int, int, str]] = []
    with localcontext() as context:
        context.prec = 40
        for line in lines:
            parts = line.split(",")
            if len(parts) != 5 or parts[-1] not in {"0", "1"}:
                raise ValueError("banknote file needs four features and a binary label per row")
            variance, skewness, _curtosis, _entropy = [_numeric(token) for token in parts[:4]]
            x1 = int((variance * SCALE).to_integral_value(rounding=ROUND_HALF_EVEN))
            x2 = int((skewness * SCALE).to_integral_value(rounding=ROUND_HALF_EVEN))
            projected.append((x1, x2, parts[-1]))
    pair_counts = Counter(row[:2] for row in projected) if collapse_projected_pairs else None
    if pair_counts is not None:
        first_rows: dict[tuple[int, int], tuple[int, int, str]] = {}
        for row in projected:
            first_rows.setdefault(row[:2], row)
        projected = list(first_rows.values())
    splits: dict[str, list[tuple[int, int, str]]] = {"train.csv": [], "holdout.csv": []}
    for x1, x2, label in projected:
        pair_hash = sha256(f"{RECIPE}:{x1},{x2}".encode("ascii")).digest()
        group = "holdout.csv" if pair_hash[0] < 64 else "train.csv"
        splits[group].append((x1, x2, label))
    if any({row[-1] for row in rows} != {"0", "1"} for rows in splits.values()):
        raise ValueError("both fixed splits must have each class")
    if any(len({row[column] for row in splits["train.csv"]}) < 2 for column in (0, 1)):
        raise ValueError("both training features need nonzero variance")
    files = {
        name: ("x1,x2,label\n" + "".join(f"{x1},{x2},{label}\n" for x1, x2, label in rows))
        .encode("ascii")
        for name, rows in splits.items()
    }
    manifest = dict(
        recipe=RECIPE, source_url=SOURCE_URL, source_sha256=source_digest, citation=ATTRIBUTION,
        license="CC-BY-4.0", license_url=LICENSE_URL, rows=EXPECTED_ROWS,
        projection=dict(x1="variance", x2="skewness", scale=SCALE, rounding="half_even"),
        holdout_rule="SHA-256 of recipe and quantized pair, first byte below 64",
        files={name: dict(sha256=sha256(data).hexdigest(), rows=len(splits[name]))
               for name, data in files.items()},
    )
    if pair_counts is not None:
        train_pairs = {row[:2] for row in splits["train.csv"]}
        holdout_pairs = {row[:2] for row in splits["holdout.csv"]}
        group_overlap = len(train_pairs & holdout_pairs)
        if group_overlap:
            raise ValueError("a projected pair crossed the train/holdout boundary")
        split_audit = {}
        for name, rows in splits.items():
            raw_count = sum(pair_counts[row[:2]] for row in rows)
            split_audit[name] = dict(groups=len(rows), source_rows=raw_count,
                                     dropped_rows=raw_count - len(rows))
        manifest["grouping"] = dict(
            policy="first_source_row_per_projected_pair", selection="position_in_pinned_raw_file",
            groups=len(projected), dropped_rows=EXPECTED_ROWS - len(projected),
            group_overlap=group_overlap, split=split_audit,
        )
    return files, manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="extracted data_banknote_authentication.txt")
    parser.add_argument("destination", type=Path, help="new directory for the capture pair")
    parser.add_argument("--sha256", required=True, help="previously recorded SHA-256 of the raw file")
    parser.add_argument("--manifest", type=Path, help="new attribution JSON outside the capture directory")
    args = parser.parse_args()
    try:
        with args.source.open("rb") as stream:
            raw = stream.read(MAX_SOURCE_BYTES + 1)
        files, manifest = prepare_capture(raw, args.sha256)
        if args.manifest is not None:
            if args.manifest.resolve().is_relative_to(args.destination.resolve()):
                raise ValueError("the manifest must be outside the capture directory")
            if not args.manifest.parent.is_dir() or args.manifest.exists():
                raise ValueError("the manifest path must be new with an existing parent")
        args.destination.mkdir()
        for name, data in files.items():
            (args.destination / name).write_bytes(data)
        manifest_json = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        if args.manifest is not None:
            with args.manifest.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(manifest_json)
        print(manifest_json, end="")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"banknote import failed: {exc}\n")


if __name__ == "__main__":
    main()
