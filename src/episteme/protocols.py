"""Versioned statistical declarations, independent of storage and execution.

Validation checks completeness and declared relationships, not scientific truth.
Unit labels do not establish independence, a digest does not prove a split is
unseen, and a written rationale does not establish power or valid inference.
Actual data exposure and preregistration timing require the persisted workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal


class DesignError(ValueError):
    """A statistical declaration is incomplete or internally inconsistent."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DesignError(message)


def _text(value: Any, field: str) -> None:
    _require(isinstance(value, str) and bool(value.strip()),
             f"{field} must be a nonempty string")


def _choice(value: Any, choices: set[str], field: str) -> None:
    _require(isinstance(value, str) and value in choices,
             f"{field} must be one of {', '.join(sorted(choices))}")


def _object(value: Any, required: set[str], field: str,
            optional: set[str] | None = None) -> dict[str, Any]:
    _require(isinstance(value, dict), f"{field} must be an object")
    _require(required <= value.keys(), f"{field} is missing required fields")
    _require(value.keys() <= required | (optional or set()),
             f"{field} contains unknown fields")
    return value


def _array(value: Any, field: str) -> list[Any]:
    _require(isinstance(value, list), f"{field} must be an array")
    return value


def _strings(value: Any, field: str, *, nonempty: bool = False) -> None:
    _require(isinstance(value, tuple), f"{field} must be an immutable tuple")
    _require(not nonempty or bool(value), f"{field} must not be empty")
    for item in value:
        _text(item, field)
    _require(len(set(value)) == len(value), f"{field} must contain unique entries")


@dataclass(frozen=True)
class Metric:
    name: str
    unit: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _text(self.name, "metric.name")
        _text(self.unit, "metric.unit")

    @classmethod
    def from_dict(cls, value: Any) -> Metric:
        return cls(**_object(value, {"name", "unit"}, "metric"))

    def to_dict(self) -> dict[str, str]:
        self.validate()
        return dict(name=self.name, unit=self.unit)


@dataclass(frozen=True)
class Uncertainty:
    """Declared independent unit for uncertainty, including analytic estimators.

For v1, a non-exempt estimator must name exactly the experimental unit. More
complex dependence structures need an adapter and a future schema revision.
``not_applicable`` explicitly declares no uncertainty estimate, with a reason.
"""

    method: str
    resampling_unit: str | None
    rationale: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _text(self.method, "uncertainty.method")
        _text(self.rationale, "uncertainty.rationale")
        if self.method == "not_applicable":
            _require(self.resampling_unit is None,
                     "not_applicable uncertainty requires a null resampling_unit")
        else:
            _text(self.resampling_unit, "uncertainty.resampling_unit")

    @classmethod
    def from_dict(cls, value: Any) -> Uncertainty:
        return cls(**_object(value, {"method", "resampling_unit", "rationale"}, "uncertainty"))

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return dict(method=self.method, resampling_unit=self.resampling_unit,
                    rationale=self.rationale)


@dataclass(frozen=True)
class StoppingRule:
    kind: Literal["fixed_sample", "sequential"]
    rule: str
    error_control: str | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _choice(self.kind, {"fixed_sample", "sequential"}, "stopping_rule.kind")
        _text(self.rule, "stopping_rule.rule")
        if self.kind == "sequential":
            _text(self.error_control, "stopping_rule.error_control")
        else:
            _require(self.error_control is None,
                     "fixed_sample stopping cannot declare sequential error_control")

    @classmethod
    def from_dict(cls, value: Any) -> StoppingRule:
        obj = _object(value, {"kind", "rule"}, "stopping_rule", {"error_control"})
        _require("error_control" not in obj or obj["error_control"] is not None,
                 "omit error_control for fixed_sample; null is not an error-control plan")
        return cls(**obj)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        result = dict(kind=self.kind, rule=self.rule)
        if self.error_control is not None:
            result["error_control"] = self.error_control
        return result


@dataclass(frozen=True)
class MultipleTesting:
    """Family entries identify planned comparisons, not observed p-values."""

    family: tuple[str, ...]
    correction: str
    rationale: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _text(self.correction, "multiple_testing.correction")
        _text(self.rationale, "multiple_testing.rationale")
        _strings(self.family, "multiple_testing.family",
                 nonempty=self.correction != "not_applicable")

    @classmethod
    def from_dict(cls, value: Any) -> MultipleTesting:
        obj = _object(value, {"family", "correction", "rationale"}, "multiple_testing")
        return cls(family=tuple(_array(obj["family"], "multiple_testing.family")),
                   correction=obj["correction"], rationale=obj["rationale"])

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return dict(family=list(self.family), correction=self.correction, rationale=self.rationale)


@dataclass(frozen=True)
class DataSplit:
    """Policy describes permitted use, not actual or authenticated access history."""

    id: str
    digest: str
    role: Literal["discovery", "validation", "confirmatory"]
    exposure_policy: Literal["open", "holdout", "new_data", "sequential"]

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _text(self.id, "data_split.id")
        _require(isinstance(self.digest, str)
                 and re.fullmatch(r"[0-9a-f]{64}", self.digest) is not None,
                 "data_split.digest must be a lowercase SHA-256 digest")
        _choice(self.role, {"discovery", "validation", "confirmatory"}, "data_split.role")
        _choice(self.exposure_policy, {"open", "holdout", "new_data", "sequential"},
                "data_split.exposure_policy")

    @classmethod
    def from_dict(cls, value: Any) -> DataSplit:
        return cls(**_object(value, {"id", "digest", "role", "exposure_policy"}, "data_split"))

    def to_dict(self) -> dict[str, str]:
        self.validate()
        return dict(id=self.id, digest=self.digest, role=self.role,
                    exposure_policy=self.exposure_policy)


@dataclass(frozen=True)
class StatisticalDesign:
    """Immutable v1 declaration; sample_size counts experimental units.

An empty exclusions tuple explicitly means no planned exclusions. A
confirmatory mode requires a declared confirmatory holdout/new-data split or a
sequential split with a prespecified sequential error-control rule. Timing,
actual exposure, metric computation and scientific adequacy are checked outside
this pure declaration. JSON Schema covers structure; Python additionally checks
unit equality, unique metric names and unique split IDs/digests.
"""

    schema_version: int
    mode: Literal["descriptive", "exploratory", "confirmatory"]
    experimental_unit: str
    estimand: str
    primary_metric: Metric
    secondary_metrics: tuple[Metric, ...]
    sample_size: int
    sample_size_rationale: str
    uncertainty: Uncertainty
    exclusions: tuple[str, ...]
    stopping_rule: StoppingRule
    multiple_testing: MultipleTesting
    data_splits: tuple[DataSplit, ...]

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _require(type(self.schema_version) is int and self.schema_version == 1,
                 "unsupported statistical design schema_version")
        _choice(self.mode, {"descriptive", "exploratory", "confirmatory"}, "mode")
        for field in ("experimental_unit", "estimand", "sample_size_rationale"):
            _text(getattr(self, field), field)
        _require(type(self.sample_size) is int and self.sample_size > 0,
                 "sample_size must be a positive integer count of experimental units")
        for field, cls in (("primary_metric", Metric), ("uncertainty", Uncertainty),
                           ("stopping_rule", StoppingRule), ("multiple_testing", MultipleTesting)):
            item = getattr(self, field)
            _require(type(item) is cls, f"{field} must be a {cls.__name__}")
            item.validate()
        for field, cls in (("secondary_metrics", Metric), ("data_splits", DataSplit)):
            items = getattr(self, field)
            _require(isinstance(items, tuple) and all(type(item) is cls for item in items),
                     f"{field} must be an immutable tuple of {cls.__name__}")
            for item in items:
                item.validate()
        names = [self.primary_metric.name, *(metric.name for metric in self.secondary_metrics)]
        _require(len(set(names)) == len(names), "primary and secondary metric names must be unique")
        _strings(self.exclusions, "exclusions")
        _require(self.uncertainty.resampling_unit is None
                 or self.uncertainty.resampling_unit == self.experimental_unit,
                 "resampling_unit must equal experimental_unit; declared pseudoreplication")
        _require(bool(self.data_splits), "data_splits must not be empty")
        for field in ("id", "digest"):
            values = [getattr(split, field) for split in self.data_splits]
            _require(len(set(values)) == len(values), f"data_splits must have unique {field} values")
        if any(split.exposure_policy == "sequential" for split in self.data_splits):
            _require(self.stopping_rule.kind == "sequential",
                     "sequential exposure requires a sequential stopping rule")
        if self.mode == "confirmatory":
            _require(any(split.role == "confirmatory" and (
                split.exposure_policy in {"holdout", "new_data"}
                or (split.exposure_policy == "sequential" and self.stopping_rule.kind == "sequential")
            ) for split in self.data_splits),
                "confirmatory design requires a confirmatory holdout/new_data or sequential split")

    def validate_metric(self, name: str) -> None:
        """Reject a requested primary metric that is absent from the frozen plan."""
        self.validate()
        _text(name, "primary metric")
        _require(name == self.primary_metric.name, "primary metric differs from statistical design")

    @classmethod
    def from_dict(cls, value: Any) -> StatisticalDesign:
        obj = _object(value, set(cls.__dataclass_fields__), "statistical_design")
        fields = dict(obj)
        for field, record in (("primary_metric", Metric), ("uncertainty", Uncertainty),
                              ("stopping_rule", StoppingRule), ("multiple_testing", MultipleTesting)):
            fields[field] = record.from_dict(obj[field])
        for field, record in (("secondary_metrics", Metric), ("data_splits", DataSplit)):
            fields[field] = tuple(record.from_dict(item) for item in _array(obj[field], field))
        fields["exclusions"] = tuple(_array(obj["exclusions"], "exclusions"))
        return cls(**fields)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return dict(schema_version=self.schema_version, mode=self.mode,
                    experimental_unit=self.experimental_unit, estimand=self.estimand,
                    primary_metric=self.primary_metric.to_dict(),
                    secondary_metrics=[metric.to_dict() for metric in self.secondary_metrics],
                    sample_size=self.sample_size, sample_size_rationale=self.sample_size_rationale,
                    uncertainty=self.uncertainty.to_dict(), exclusions=list(self.exclusions),
                    stopping_rule=self.stopping_rule.to_dict(),
                    multiple_testing=self.multiple_testing.to_dict(),
                    data_splits=[split.to_dict() for split in self.data_splits])
