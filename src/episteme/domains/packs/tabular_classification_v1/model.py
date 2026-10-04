"""Deterministic logistic fit and the preregistered paired comparison.

Constants below are part of pack version 1. They are not estimated from holdout
labels. The comparison is accuracy of this fit minus accuracy of the majority
class of the training labels, on the same holdout rows.
"""

import math


STEPS = 200
LEARNING_RATE = 1
THRESHOLD = 0.5
TIE_CLASS = 0
Z_975 = 1.959963984540054  # Fixed normal quantile for a 95% Wald interval.
ALPHA = 0.05
HEADER = "x1,x2,label"
METRIC = "accuracy_difference"


def parse_table(data):
    """Rows of integer ``x1``, ``x2`` and binary ``label``. Blank lines are rejected."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("table is not UTF-8") from exc
    if text.startswith("\ufeff"):
        raise ValueError("table must not start with a BOM")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if not lines or lines[0] != HEADER:
        raise ValueError("table header must be x1,x2,label")
    rows = []
    for line in lines[1:]:
        if line == "" or line != line.strip():
            raise ValueError("table has a blank or padded line")
        parts = line.split(",")
        if len(parts) != 3 or any(not _integer_token(part) for part in parts[:2]):
            raise ValueError("table rows must be three comma-separated integers")
        if parts[2] not in {"0", "1"}:
            raise ValueError("label must be 0 or 1")
        rows.append((int(parts[0]), int(parts[1]), int(parts[2])))
    if len(rows) < 2:
        raise ValueError("table needs at least two data rows")
    return rows


def _integer_token(token):
    if token in {"", "-", "+"}:
        return False
    body = token[1:] if token[0] == "-" else token
    return body.isdigit()


def sigmoid(value):
    if value >= 0.0:
        exponential = math.exp(-value)
        return 1.0 / (1.0 + exponential)
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


def _standardize(rows):
    count = len(rows)
    mean1 = math.fsum(row[0] for row in rows) / count
    mean2 = math.fsum(row[1] for row in rows) / count
    var1 = math.fsum((row[0] - mean1) ** 2 for row in rows) / count
    var2 = math.fsum((row[1] - mean2) ** 2 for row in rows) / count
    if var1 <= 0.0 or var2 <= 0.0:
        raise ValueError("a training feature has zero variance")
    scale = (math.sqrt(var1), math.sqrt(var2))
    mean = (mean1, mean2)
    transformed = [((row[0] - mean1) / scale[0], (row[1] - mean2) / scale[1]) for row in rows]
    return transformed, mean, scale


def majority_class(labels):
    """Most frequent training label. A tie uses the preregistered class 0."""
    ones = sum(labels)
    if ones > len(labels) - ones:
        return 1
    return TIE_CLASS


def fit(features, labels):
    """Batch gradient descent from the origin for a fixed number of steps."""
    weight0 = weight1 = weight2 = 0.0
    count = len(features)
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
    return (weight0, weight1, weight2)


def predict(weights, mean, scale, row):
    score = (weights[0]
             + weights[1] * ((row[0] - mean[0]) / scale[0])
             + weights[2] * ((row[1] - mean[1]) / scale[1]))
    return 1 if sigmoid(score) >= THRESHOLD else 0


def score_holdout(train, holdout):
    """Fit on ``train`` only, then score every ``holdout`` row with both classifiers."""
    features, mean, scale = _standardize(train)
    labels = [row[2] for row in train]
    weights = fit(features, labels)
    baseline = majority_class(labels)
    scored = []
    discordant_model = discordant_baseline = 0
    for index, row in enumerate(holdout):
        logistic = predict(weights, mean, scale, row)
        if logistic == row[2] and baseline != row[2]:
            discordant_model += 1
        elif logistic != row[2] and baseline == row[2]:
            discordant_baseline += 1
        scored.append({"i": index, "label": row[2], "logistic": logistic, "majority": baseline})
    return dict(weights=weights, mean=mean, scale=scale, majority_class=baseline, rows=scored,
                model_only=discordant_model, baseline_only=discordant_baseline, n=len(holdout))


def mcnemar_p(model_only, baseline_only):
    """Two-sided exact McNemar p-value. No discordant pairs yields 1."""
    total = model_only + baseline_only
    if total == 0:
        return 1.0
    observed = math.comb(total, model_only)
    as_likely = sum(math.comb(total, index) for index in range(total + 1)
                    if math.comb(total, index) <= observed)
    return as_likely / (2 ** total)


def compare(model_only, baseline_only, count):
    """Paired accuracy difference, Wald interval, exact p-value and the decision rule.

    ``supports`` requires the 95% interval to lie entirely above 0 and the exact
    test to fall below alpha. ``refutes`` is the symmetric rule below 0.
    Otherwise the outcome is ``inconclusive``. Alpha and the normal quantile are
    the constants above, not quantities chosen after seeing the interval.
    """
    difference = (model_only - baseline_only) / count
    variance = ((model_only + baseline_only) / (count * count)
                - (model_only - baseline_only) ** 2 / (count * count * count))
    if variance < 0.0:
        variance = 0.0
    margin = Z_975 * math.sqrt(variance)
    lower, upper = difference - margin, difference + margin
    p_value = mcnemar_p(model_only, baseline_only)
    if lower > 0.0 and difference > 0.0 and p_value < ALPHA:
        outcome = "supports"
    elif upper < 0.0 and difference < 0.0 and p_value < ALPHA:
        outcome = "refutes"
    else:
        outcome = "inconclusive"
    return dict(difference=difference, lower=lower, upper=upper, p_value=p_value, outcome=outcome,
                variance=variance)
