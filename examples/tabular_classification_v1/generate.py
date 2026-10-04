"""Write the two generated tables used by ``tabular_classification_v1``.

These files are mechanism-test fixtures, not a scientific dataset and not a
copy of a public table. Each split has 48 rows. ``planted`` sets the label
from the sign of ``x1``. ``null`` sets every label to 0. Training and holdout
use different ``x2`` formulas so the files are not the same bytes.

Run from the repository root: ``python examples/tabular_classification_v1/generate.py``.
"""

from pathlib import Path


def rows(kind, split):
    table = []
    for index in range(48):
        x1 = index - 24
        x2 = (index % 5) - 2 if split == "train" else (index % 7) - 3
        label = (1 if x1 >= 0 else 0) if kind == "planted" else 0
        table.append((x1, x2, label))
    return table


def csv_bytes(table):
    lines = ["x1,x2,label", *(f"{x1},{x2},{label}" for x1, x2, label in table)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def tables():
    return {(kind, split): csv_bytes(rows(kind, split))
            for kind in ("planted", "null") for split in ("train", "holdout")}


def main():
    root = Path(__file__).resolve().parent
    for (kind, split), data in tables().items():
        path = root / kind / f"{split}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


if __name__ == "__main__":
    main()
