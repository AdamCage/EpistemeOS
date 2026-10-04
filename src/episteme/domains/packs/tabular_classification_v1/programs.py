"""Runner programs for the tabular classification pack.

Profile v1 passes one ``input.dat``. That file quotes the training CSV and the
holdout CSV, which remain separate captured artifacts. The fit reads only the
training rows. Holdout labels are read afterwards, and only to score rows.
The reanalysis recounts those scored rows; it does not fit another model.
Both programs are pinned pack code, not independently authored implementations.
"""

PRIMARY_SOURCE = r'''"""Fit on training rows, then score the holdout. Holdout labels are not used to fit."""
import json
import math
import pathlib
import sys


STEPS = 200
LEARNING_RATE = 1
THRESHOLD = 0.5
TIE_CLASS = 0
HEADER = "x1,x2,label"


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def parse_table(data):
    text = data.decode("utf-8")
    if text.startswith("\ufeff"):
        raise ValueError("table must not start with a BOM")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if not lines or lines[0] != HEADER:
        raise ValueError("table header must be x1,x2,label")
    rows = []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) != 3 or parts[2] not in {"0", "1"}:
            raise ValueError("table rows must be x1,x2 and a binary label")
        rows.append((int(parts[0]), int(parts[1]), int(parts[2])))
    if len(rows) < 2:
        raise ValueError("table needs at least two data rows")
    return rows


def sigmoid(value):
    if value >= 0.0:
        exponential = math.exp(-value)
        return 1.0 / (1.0 + exponential)
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


def fit_and_score(train, holdout):
    count = len(train)
    mean1 = math.fsum(row[0] for row in train) / count
    mean2 = math.fsum(row[1] for row in train) / count
    var1 = math.fsum((row[0] - mean1) ** 2 for row in train) / count
    var2 = math.fsum((row[1] - mean2) ** 2 for row in train) / count
    if var1 <= 0.0 or var2 <= 0.0:
        raise ValueError("a training feature has zero variance")
    scale1, scale2 = math.sqrt(var1), math.sqrt(var2)
    features = [((row[0] - mean1) / scale1, (row[1] - mean2) / scale2) for row in train]
    labels = [row[2] for row in train]
    weight0 = weight1 = weight2 = 0.0
    for _ in range(STEPS):
        grad0 = grad1 = grad2 = 0.0
        for (left, right), label in zip(features, labels):
            error = sigmoid(weight0 + weight1 * left + weight2 * right) - label
            grad0 += error
            grad1 += error * left
            grad2 += error * right
        weight0 -= LEARNING_RATE * grad0 / count
        weight1 -= LEARNING_RATE * grad1 / count
        weight2 -= LEARNING_RATE * grad2 / count
    ones = sum(labels)
    baseline = 1 if ones > count - ones else TIE_CLASS
    scored = []
    model_only = baseline_only = 0
    for index, row in enumerate(holdout):
        score = (weight0
                 + weight1 * ((row[0] - mean1) / scale1)
                 + weight2 * ((row[1] - mean2) / scale2))
        logistic = 1 if sigmoid(score) >= THRESHOLD else 0
        if logistic == row[2] and baseline != row[2]:
            model_only += 1
        elif logistic != row[2] and baseline == row[2]:
            baseline_only += 1
        scored.append({"i": index, "label": row[2], "logistic": logistic, "majority": baseline})
    return scored, (model_only - baseline_only) / len(holdout)


def main():
    if len(sys.argv) != 4 or sys.argv[1:3] != ["input.dat", "--seed"]:
        raise ValueError("expected input.dat --seed N")
    seed = int(sys.argv[3])
    if seed != 0:
        raise ValueError("this pack has one deterministic roster unit, 0")
    recipe = json.loads(pathlib.Path("input.dat").read_text(encoding="utf-8"))
    if (recipe["steps"] != STEPS or recipe["learning_rate"] != LEARNING_RATE
            or recipe["threshold"] != THRESHOLD or recipe["tie_class"] != TIE_CLASS):
        raise ValueError("program input does not match the preregistered constants")
    train = parse_table(recipe["train_csv"].encode("utf-8"))
    scored, difference = fit_and_score(train, parse_table(recipe["holdout_csv"].encode("utf-8")))
    pathlib.Path("raw.json").write_bytes(encoded({
        "schema_version": 1, "domain": "tabular_classification_v1", "seed": seed, "rows": scored}))
    pathlib.Path("metrics.json").write_bytes(encoded({"accuracy_difference": difference}))


if __name__ == "__main__":
    main()
'''.encode("utf-8")


REANALYSIS_SOURCE = r'''"""Recount the paired accuracy difference from the primary row list.

This is a second arithmetic pass over the same scored rows. It does not fit a
model and it does not read a new table. It is not independent reanalysis by
another author and not new-data replication.
"""
import json
import pathlib
import sys


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def main():
    if len(sys.argv) != 4 or sys.argv[1:3] != ["input.dat", "--seed"]:
        raise ValueError("expected input.dat --seed N")
    seed = int(sys.argv[3])
    source = pathlib.Path("input.dat").read_bytes()
    raw = json.loads(source)
    rows = raw["rows"]
    if raw["schema_version"] != 1 or raw["domain"] != "tabular_classification_v1" or raw["seed"] != seed:
        raise ValueError("reanalysis input is not the primary raw record")
    if type(rows) is not list or len(rows) < 2:
        raise ValueError("reanalysis row list is incomplete")
    model_only = baseline_only = 0
    for index, row in enumerate(rows):
        if row["i"] != index or row["label"] not in (0, 1):
            raise ValueError("reanalysis row is out of order or unlabeled")
        if row["logistic"] not in (0, 1) or row["majority"] not in (0, 1):
            raise ValueError("reanalysis prediction is not binary")
        if row["logistic"] == row["label"] and row["majority"] != row["label"]:
            model_only += 1
        elif row["logistic"] != row["label"] and row["majority"] == row["label"]:
            baseline_only += 1
    pathlib.Path("raw.json").write_bytes(source)
    pathlib.Path("metrics.json").write_bytes(encoded({
        "accuracy_difference": (model_only - baseline_only) / len(rows)}))


if __name__ == "__main__":
    main()
'''.encode("utf-8")
