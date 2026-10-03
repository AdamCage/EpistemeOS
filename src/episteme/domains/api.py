"""Typed JSON envelopes of the DomainPack contract v1 (ADR 0016).

Every envelope is strict JSON: an exact field set, bounded sizes, no duplicate
keys and no non-finite numbers. One declarative schema per envelope drives both
the dependency-free runtime check below and the published JSON Schema files.

A valid envelope is well-formed, not correct. These checks do not establish
that a pack computed a statistic correctly, that its assumptions hold, or that
a claim is scientifically valid; the kernel derives the claim-strength ceiling
from a report separately, and review remains a separate decision.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
import json
import math
from pathlib import PurePosixPath
import re
from types import MappingProxyType
from typing import Any, Callable, ClassVar, Mapping

from ..protocols import StatisticalDesign
from ..store import canonical, digest


CONTRACT_VERSION = 1
ROSTER_SEMANTICS = ("rng_seed", "partition_seed", "frozen_unit_index", "deterministic_single")
SAMPLE_SIZE_SCOPES = ("per_roster_unit", "total")
EXECUTION_PROFILES = ("trusted_local_python_v1",)
CLOSURE_LEVELS = ("interpreter_fingerprint",)
HIDDEN_INPUT_ROLES = ("protocol_data", "captured_artifacts")
CAPABILITIES = ("separate_cwd", "python_isolated_mode", "process_group_timeout",
                "job_object_timeout", "bounded_output_capture")
OUTCOMES = ("supports", "refutes", "inconclusive")
INFERENCE_MODES = ("unclassified", "descriptive", "exploratory", "confirmatory")
STATISTICAL_FIELDS = ("estimand", "estimator", "point_estimate", "uncertainty",
                      "confidence_interval", "effect_size", "assumptions", "sample_size",
                      "multiple_testing", "stopping_rule", "sensitivity_analysis", "deviations")
REQUIRED_SUPPLIED = ("estimator", "point_estimate", "sample_size")
FIELD_STATUSES = ("supplied", "not_supplied", "not_applicable")
MAX_ENVELOPE_BYTES = 1024 * 1024
MAX_DETAILS_BYTES = 256 * 1024
MAX_BLOB_BYTES = 64 * 1024 * 1024


class EnvelopeError(ValueError):
    """A DomainPack envelope is malformed, oversized or contradicts its contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EnvelopeError(message)


# ---------------------------------------------------------------- strict JSON


def strict_json(value: Any) -> Any:
    """Detached strict JSON copy: string keys, finite numbers, no tuples."""
    def check(item: Any, depth: int) -> None:
        _require(depth <= 64, "envelope JSON is nested too deeply")
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float:
            _require(math.isfinite(item), "envelope JSON contains a non-finite number")
            return
        if type(item) is list:
            for child in item:
                check(child, depth + 1)
            return
        if type(item) is dict:
            _require(all(type(key) is str for key in item), "envelope JSON keys must be strings")
            for child in item.values():
                check(child, depth + 1)
            return
        raise EnvelopeError(f"envelope value is not strict JSON: {type(item).__name__}")

    check(value, 0)
    return json.loads(canonical(value))


def strict_loads(data: bytes, label: str = "envelope") -> Any:
    """Parse UTF-8 JSON bytes, rejecting duplicate keys and non-finite constants."""
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            _require(key not in result, f"duplicate key in {label}: {key}")
            result[key] = item
        return result

    def invalid(constant: str) -> None:
        raise EnvelopeError(f"non-finite constant in {label}: {constant}")

    _require(type(data) is bytes, f"{label} must be bytes")
    try:
        return strict_json(json.loads(data.decode("utf-8"), object_pairs_hook=unique,
                                      parse_constant=invalid))
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, EnvelopeError):
            raise
        raise EnvelopeError(f"invalid {label}: {exc}") from exc


def _freeze(value: Any) -> Any:
    if type(value) is dict:
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    """Plain JSON copy of a frozen envelope value."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if type(value) is tuple:
        return [thaw(item) for item in value]
    return value


# ------------------------------------------------ JSON Schema subset validator

_ANNOTATIONS = {"$schema", "$id", "$comment", "$defs", "title", "description", "examples"}
_KEYWORDS = _ANNOTATIONS | {
    "$ref", "type", "const", "enum", "minLength", "maxLength", "pattern", "minimum",
    "maximum", "exclusiveMinimum", "exclusiveMaximum", "items", "minItems", "maxItems",
    "uniqueItems", "properties", "required", "additionalProperties", "propertyNames",
    "minProperties", "maxProperties", "allOf", "anyOf", "oneOf", "if", "then", "else"}


def _type_matches(value: Any, name: str) -> bool:
    if name == "object":
        return type(value) is dict
    if name == "array":
        return type(value) is list
    if name == "string":
        return type(value) is str
    if name == "integer":
        return type(value) is int
    if name == "number":
        return type(value) in (int, float)
    if name == "boolean":
        return type(value) is bool
    if name == "null":
        return value is None
    raise EnvelopeError(f"unsupported schema type: {name}")


def _pattern(pattern: str, text: str) -> bool:
    if pattern.startswith("^") and pattern.endswith("$"):
        return re.fullmatch(pattern[1:-1], text) is not None
    return re.search(pattern, text) is not None


def _check(schema: Mapping[str, Any], value: Any, path: str, defs: Mapping[str, Any]) -> None:
    unknown = set(schema) - _KEYWORDS
    if unknown:
        raise EnvelopeError(f"unsupported schema keywords at {path}: {sorted(unknown)}")
    if "$ref" in schema:
        reference = schema["$ref"]
        _require(type(reference) is str and reference.startswith("#/$defs/")
                 and reference[8:] in defs, f"unresolved schema reference: {reference}")
        _check(defs[reference[8:]], value, path, defs)
    if "type" in schema:
        names = schema["type"] if type(schema["type"]) is list else [schema["type"]]
        _require(any(_type_matches(value, name) for name in names),
                 f"{path}: expected {'/'.join(names)}")
    if "const" in schema:
        _require(canonical(value) == canonical(schema["const"]), f"{path}: unexpected value")
    if "enum" in schema:
        _require(any(canonical(value) == canonical(item) for item in schema["enum"]),
                 f"{path}: value is not one of the allowed options")
    if type(value) is str:
        if "minLength" in schema:
            _require(len(value) >= schema["minLength"], f"{path}: text is too short")
        if "maxLength" in schema:
            _require(len(value) <= schema["maxLength"], f"{path}: text is too long")
        if "pattern" in schema:
            _require(_pattern(schema["pattern"], value), f"{path}: text has an invalid form")
    if type(value) in (int, float):
        if "minimum" in schema:
            _require(value >= schema["minimum"], f"{path}: number is below its minimum")
        if "maximum" in schema:
            _require(value <= schema["maximum"], f"{path}: number is above its maximum")
        if "exclusiveMinimum" in schema:
            _require(value > schema["exclusiveMinimum"], f"{path}: number is not above its bound")
        if "exclusiveMaximum" in schema:
            _require(value < schema["exclusiveMaximum"], f"{path}: number is not below its bound")
    if type(value) is list:
        if "minItems" in schema:
            _require(len(value) >= schema["minItems"], f"{path}: too few items")
        if "maxItems" in schema:
            _require(len(value) <= schema["maxItems"], f"{path}: too many items")
        if schema.get("uniqueItems"):
            encoded = [canonical(item) for item in value]
            _require(len(set(encoded)) == len(encoded), f"{path}: items must be unique")
        if "items" in schema:
            for index, item in enumerate(value):
                _check(schema["items"], item, f"{path}[{index}]", defs)
    if type(value) is dict:
        properties = schema.get("properties", {})
        for name in schema.get("required", ()):
            _require(name in value, f"{path}: missing required field {name}")
        if "minProperties" in schema:
            _require(len(value) >= schema["minProperties"], f"{path}: too few fields")
        if "maxProperties" in schema:
            _require(len(value) <= schema["maxProperties"], f"{path}: too many fields")
        for name, item in value.items():
            if "propertyNames" in schema:
                _check(schema["propertyNames"], name, f"{path}.<key {name!r}>", defs)
            if name in properties:
                _check(properties[name], item, f"{path}.{name}", defs)
            elif "additionalProperties" in schema:
                extra = schema["additionalProperties"]
                _require(extra is not False, f"{path}: unexpected field {name}")
                if isinstance(extra, Mapping):
                    _check(extra, item, f"{path}.{name}", defs)
    for member in schema.get("allOf", ()):
        _check(member, value, path, defs)
    if "anyOf" in schema:
        matched = False
        for member in schema["anyOf"]:
            try:
                _check(member, value, path, defs)
                matched = True
                break
            except EnvelopeError:
                continue
        _require(matched, f"{path}: value matches no permitted form")
    if "oneOf" in schema:
        count = 0
        for member in schema["oneOf"]:
            try:
                _check(member, value, path, defs)
                count += 1
            except EnvelopeError:
                continue
        _require(count == 1, f"{path}: value must match exactly one permitted form")
    if "if" in schema:
        try:
            _check(schema["if"], value, path, defs)
            branch = schema.get("then")
        except EnvelopeError:
            branch = schema.get("else")
        if branch is not None:
            _check(branch, value, path, defs)


def validate_schema(name: str, value: Any) -> None:
    """Check strict JSON against one published envelope schema."""
    schema = SCHEMAS[name]
    _check(schema, value, "$", schema.get("$defs", {}))


def supported_schema(schema: Any, path: str = "$") -> None:
    """Reject schema keywords this dependency-free validator would not enforce."""
    _require(isinstance(schema, Mapping), f"schema at {path} must be an object")
    unknown = set(schema) - _KEYWORDS
    _require(not unknown, f"unsupported schema keywords at {path}: {sorted(unknown)}")
    _require("$ref" not in schema and "$defs" not in schema,
             f"schema references are not supported at {path}")
    for key in ("items", "propertyNames", "if", "then", "else"):
        if key in schema:
            supported_schema(schema[key], f"{path}.{key}")
    if isinstance(schema.get("additionalProperties"), Mapping):
        supported_schema(schema["additionalProperties"], f"{path}.additionalProperties")
    for name, member in schema.get("properties", {}).items():
        supported_schema(member, f"{path}.properties.{name}")
    for key in ("allOf", "anyOf", "oneOf"):
        for index, member in enumerate(schema.get(key, ())):
            supported_schema(member, f"{path}.{key}[{index}]")


# ---------------------------------------------------------- schema building

_TEXT = {"type": "string", "minLength": 1, "maxLength": 4096, "pattern": "\\S"}
_LONG_TEXT = {"type": "string", "minLength": 1, "maxLength": 16384, "pattern": "\\S"}
_SHORT = {"type": "string", "minLength": 1, "maxLength": 256, "pattern": "\\S"}
_SHA256 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_PACK_ID = {"type": "string", "pattern": "^[a-z][a-z0-9_]{1,63}$"}
_PACK_VERSION = {"type": "string", "minLength": 1, "maxLength": 64, "pattern": "^\\S(?:.*\\S)?$"}
_METRIC = {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_.-]{0,127}$"}
_LABEL = {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$"}
_PATH = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"}
_ROSTER_UNIT = {"type": "integer", "minimum": -(2 ** 31), "maximum": 2 ** 31 - 1}
_NUMBER = {"type": "number"}
_DIGESTS = {"type": "array", "maxItems": 4096, "uniqueItems": True, "items": _SHA256}
_OUTPUTS = {"type": "object", "minProperties": 2, "maxProperties": 16,
            "propertyNames": _LABEL, "additionalProperties": _PATH,
            "required": ["raw_data", "metrics"]}


def _object(properties: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False,
            "required": list(properties), "properties": properties, **extra}


def _list(item: dict[str, Any], *, minimum: int = 0, maximum: int = 64,
          unique: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "array", "maxItems": maximum, "items": item}
    if minimum:
        result["minItems"] = minimum
    if unique:
        result["uniqueItems"] = True
    return result


def _schema(name: str, title: str, comment: str, body: dict[str, Any],
            defs: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {"$schema": "https://json-schema.org/draft/2020-12/schema",
              "$id": f"urn:epistemeos:{name}", "title": title, "$comment": comment, **body}
    if defs:
        result["$defs"] = defs
    return result


_BLOB = _object({"sha256": _SHA256, "bytes": {"type": "integer", "minimum": 0,
                                               "maximum": MAX_BLOB_BYTES}})

_FIELD_VALUES: dict[str, dict[str, Any]] = {
    "estimand": _object({"protocol_hash": _SHA256, "text": _LONG_TEXT}),
    "estimator": _object({"name": _SHORT, "description": _TEXT,
                          "hook": {"enum": ["analyse", "recompute_metrics"]}}),
    "point_estimate": _object({
        "metric": _METRIC, "unit": _SHORT, "value": {"type": ["number", "null"]},
        "by_roster_unit": _list(_object({"roster_unit": _ROSTER_UNIT, "value": _NUMBER}),
                                maximum=4096)}),
    "uncertainty": _object({"method": _SHORT, "resampling_unit": {"anyOf": [_SHORT, {"type": "null"}]}}),
    "confidence_interval": _object({
        "level": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1},
        "lower": _NUMBER, "upper": _NUMBER, "method": _SHORT}),
    "effect_size": _object({"measure": _SHORT, "value": _NUMBER, "reference": _TEXT}),
    "assumptions": _list(_object({
        "assumption": _TEXT, "status": {"enum": ["holds", "violated", "unchecked"]},
        "reference": {"anyOf": [_TEXT, {"type": "null"}]}}), minimum=1),
    "sample_size": _object({
        "experimental_unit": _SHORT, "unit_scope": {"enum": list(SAMPLE_SIZE_SCOPES)},
        "planned": {"type": "integer", "minimum": 1},
        "planned_total": {"type": "integer", "minimum": 1},
        "analysed": {"type": "integer", "minimum": 0},
        "exclusions": _list(_object({"rule": _TEXT, "count": {"type": "integer", "minimum": 1},
                                     "preregistered": {"type": "boolean"}})),
        "missing_slots": _list(_SHORT, maximum=8192)}),
    "multiple_testing": _object({
        "family": _list(_SHORT, maximum=256), "claim_position": {"anyOf": [_SHORT, {"type": "null"}]},
        "correction": _SHORT, "adjusted_result": {"anyOf": [_TEXT, {"type": "null"}]}}),
    "stopping_rule": _object({"rule_sha256": _SHA256, "roster_complete": {"type": "boolean"},
                              "interim_looks": {"type": "integer", "minimum": 0}}),
    "sensitivity_analysis": _list(_object({
        "name": _SHORT, "timing": {"enum": ["preregistered", "post_hoc"]}, "result": _TEXT})),
    "deviations": _list(_object({
        "field": {"enum": [*STATISTICAL_FIELDS[:-1], "analysis_plan"]},
        "plan": _TEXT, "actual": _TEXT, "reason": _TEXT})),
}


def _field(name: str) -> dict[str, Any]:
    return {"type": "object", "required": ["status"],
            "properties": {"status": {"enum": list(FIELD_STATUSES)}},
            "if": {"properties": {"status": {"const": "supplied"}}},
            "then": {"additionalProperties": False, "required": ["status", "value"],
                     "properties": {"status": {}, "value": {"$ref": f"#/$defs/{name}"}}},
            "else": {"additionalProperties": False, "required": ["status", "reason"],
                     "properties": {"status": {}, "reason": _TEXT}}}


SCHEMAS: dict[str, dict[str, Any]] = {
    "pack-manifest-v1": _schema(
        "pack-manifest:v1", "EpistemeOS DomainPack manifest v1",
        "Static pack declaration. Metric names, output labels and paths must be unique; outputs "
        "include raw_data and metrics; capabilities include estimator, point_estimate and "
        "sample_size. A declaration is not evidence that the pack computes correctly.",
        _object({
            "contract_version": {"const": CONTRACT_VERSION},
            "pack_id": _PACK_ID, "pack_version": _PACK_VERSION, "description": _TEXT,
            "roster_semantics": _list({"enum": list(ROSTER_SEMANTICS)}, minimum=1, maximum=4),
            "metrics": _list(_object({"name": _METRIC, "unit": _SHORT, "description": _TEXT}),
                             minimum=1, maximum=32),
            "outputs": {"type": "object", "minProperties": 2, "maxProperties": 16,
                        "propertyNames": _LABEL, "required": ["raw_data", "metrics"],
                        "additionalProperties": _object({"path": _PATH, "schema_id": _SHORT})},
            "numeric_tolerance": {"type": "number", "minimum": 0},
            "hidden_inputs": _list({"enum": list(HIDDEN_INPUT_ROLES)}, maximum=2),
            "execution_profiles": _list({"enum": list(EXECUTION_PROFILES)}, minimum=1, maximum=8),
            "statistical_capabilities": _list({"enum": list(STATISTICAL_FIELDS)}, minimum=3,
                                              maximum=12),
            "interpretation_cautions": _list(_TEXT, minimum=1, maximum=32),
            "capture": {"type": "boolean"}})),
    "parameter-catalog-v1": _schema(
        "parameter-catalog:v1", "EpistemeOS DomainPack parameter catalog v1",
        "Model- and planner-visible parameters. It never contains hidden inputs such as a "
        "synthetic world. parameters_schema is a JSON Schema subset checked by the kernel.",
        _object({
            "schema_version": {"const": 1}, "pack_id": _PACK_ID, "pack_version": _PACK_VERSION,
            "description": _TEXT, "parameters_schema": {"type": "object"},
            "metric": _METRIC, "outputs": _OUTPUTS,
            "limitations": _list(_TEXT, minimum=1, maximum=32)})),
    "protocol-draft-v1": _schema(
        "protocol-draft:v1", "EpistemeOS DomainPack protocol draft v1",
        "Compiled preregistration fields. statistical_design must be a normalized "
        "StatisticalDesign v1 whose primary metric and stopping rule equal metric and "
        "stopping_rule. The kernel, not the pack, records the protocol.",
        _object({
            "schema_version": {"const": 1}, "design": _LONG_TEXT, "analysis_plan": _LONG_TEXT,
            "metric": _METRIC, "stopping_rule": _LONG_TEXT, "statistical_design": {"type": "object"},
            "roster": _list(_ROSTER_UNIT, minimum=1, maximum=4096),
            "roster_semantics": {"enum": list(ROSTER_SEMANTICS)},
            "sample_size_scope": {"enum": list(SAMPLE_SIZE_SCOPES)},
            "run_limit": {"type": "integer", "minimum": 2, "maximum": 2 ** 31 - 1},
            "replication_tolerance": {"type": "number", "minimum": 0},
            "seen_data": _DIGESTS})),
    "execution-plan-v1": _schema(
        "execution-plan:v1", "EpistemeOS DomainPack execution plan v1",
        "Frozen form: program and input bytes are referenced by SHA-256 and stored by the "
        "kernel. Only trusted_local_python_v1 is implemented; every reserved environment "
        "field other than closure_level must be null in v1.",
        _object({
            "schema_version": {"const": 1}, "primary_program": _BLOB,
            "reanalysis_program": _BLOB, "input": _BLOB, "outputs": _OUTPUTS,
            "wall_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
            "max_output_bytes": {"type": "integer", "minimum": 1, "maximum": 1024 ** 3},
            "required_capabilities": _list({"enum": list(CAPABILITIES)}, maximum=8),
            "execution_profile": {"enum": list(EXECUTION_PROFILES)},
            "environment_requirements": _object({
                "closure_level": {"enum": list(CLOSURE_LEVELS)}, "image_digest": {"type": "null"},
                "lock_digest": {"type": "null"}, "accelerator": {"type": "null"},
                "network_policy": {"type": "null"}, "env_allowlist": {"type": "null"}})})),
    "capture-bundle-v1": _schema(
        "capture-bundle:v1", "EpistemeOS DomainPack capture bundle v1",
        "Frozen form of a read-only capture of a declared local source. Inventory paths are "
        "sorted, unique, relative POSIX paths; the kernel stores the bytes. Hashes identify "
        "captured bytes, not historical time or completeness of the source.",
        _object({
            "schema_version": {"const": 1}, "pack_id": _PACK_ID, "pack_version": _PACK_VERSION,
            "source": _object({"kind": {"enum": ["local_directory"]}, "label": _SHORT}),
            "inventory": _list(_object({"path": {"type": "string", "minLength": 1,
                                                 "maxLength": 1024},
                                        "sha256": _SHA256,
                                        "bytes": {"type": "integer", "minimum": 0,
                                                  "maximum": MAX_BLOB_BYTES}}),
                               minimum=1, maximum=4096),
            "audit": {"type": "object"}})),
    "output-check-v1": _schema(
        "output-check:v1", "EpistemeOS DomainPack output check v1",
        "One pack check of one completed batch slot. A failed check rejects the analysis.",
        _object({
            "schema_version": {"const": 1},
            "slot": {"type": "string", "pattern": "^(primary|reanalysis):-?[0-9]{1,10}$"},
            "roster_unit": _ROSTER_UNIT,
            "mode": {"enum": ["primary", "independent_reanalysis"]},
            "result": _SHORT, "raw_data": _SHA256, "metrics": _SHA256,
            "status": {"enum": ["passed", "failed"]}, "findings": _list(_TEXT, maximum=32)})),
    "recomputation-v1": _schema(
        "recomputation:v1", "EpistemeOS DomainPack recomputation v1",
        "Metric recomputed from raw data for one roster unit, both recorded metrics and their "
        "absolute differences. The tolerance concerns arithmetic agreement, not uncertainty.",
        _object({
            "schema_version": {"const": 1}, "roster_unit": _ROSTER_UNIT, "metric": _METRIC,
            "recomputed": _NUMBER, "primary_recorded": _NUMBER, "reanalysis_recorded": _NUMBER,
            "primary_absolute_difference": {"type": "number", "minimum": 0},
            "reanalysis_absolute_difference": {"type": "number", "minimum": 0},
            "tolerance": {"type": "number", "minimum": 0}})),
    "analysis-report-v2": _schema(
        "analysis-report:v2", "EpistemeOS DomainPack analysis report v2",
        "Pack proposal for one bounded claim. statistical_report is the SHA-256 of a separate "
        "StatisticalReport v1 artifact. The kernel computes the claim-strength ceiling and "
        "rejects a proposal above it; it never silently downgrades.",
        _object({
            "schema_version": {"const": 2}, "pack_id": _PACK_ID, "pack_version": _PACK_VERSION,
            "protocol_hash": _SHA256, "statement": _TEXT,
            "limitations": _list({"type": "string", "minLength": 1, "maxLength": 1024,
                                  "pattern": "\\S"}, minimum=1, maximum=32),
            "outcome": {"enum": list(OUTCOMES)}, "inference_mode": {"enum": list(INFERENCE_MODES)},
            "details": {"type": "object"}, "statistical_report": _SHA256})),
    "statistical-report-v1": _schema(
        "statistical-report:v1", "EpistemeOS statistical report v1",
        "All twelve fields are required. not_applicable is valid only when the preregistered "
        "design implies it; not_supplied needs a reason and limits claim strength. Presence and "
        "consistency of fields are checked, not correctness of the statistics.",
        _object({"schema_version": {"const": 1}, "protocol_hash": _SHA256,
                 **{name: _field(name) for name in STATISTICAL_FIELDS}}),
        defs=_FIELD_VALUES),
}


# -------------------------------------------------------------- envelopes


def _size(value: Any, limit: int, label: str) -> None:
    _require(len(canonical(value)) <= limit, f"{label} exceeds {limit} bytes")


def _safe_relative(path: str) -> None:
    _require(type(path) is str and "\\" not in path and ":" not in path and "\x00" not in path
             and not path.startswith("/"), f"unsafe relative path: {path!r}")
    parts = path.split("/")
    _require(all(part not in {"", ".", ".."} for part in parts)
             and PurePosixPath(path).as_posix() == path, f"unsafe relative path: {path!r}")


class _Envelope:
    """Frozen dataclass base: nested JSON is frozen, every instance is validated."""

    SCHEMA: ClassVar[str] = ""

    def __post_init__(self) -> None:
        for item in fields(self):  # type: ignore[arg-type]
            object.__setattr__(self, item.name, _freeze(getattr(self, item.name)))
        self.check(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {item.name: thaw(getattr(self, item.name))
                for item in fields(self)}  # type: ignore[arg-type]

    @classmethod
    def check(cls, value: Any) -> None:
        value = strict_json(value)
        _size(value, MAX_ENVELOPE_BYTES, cls.SCHEMA)
        validate_schema(cls.SCHEMA, value)
        cls._semantic(value)

    @classmethod
    def _semantic(cls, value: dict[str, Any]) -> None:
        return None

    @classmethod
    def from_dict(cls, value: Any) -> Any:
        value = strict_json(value)
        cls.check(value)
        return cls(**value)

    def canonical(self) -> bytes:
        return canonical(self.to_dict())

    def digest(self) -> str:
        return digest(self.canonical())


@dataclass(frozen=True)
class PackManifest(_Envelope):
    contract_version: int
    pack_id: str
    pack_version: str
    description: str
    roster_semantics: Any
    metrics: Any
    outputs: Any
    numeric_tolerance: float
    hidden_inputs: Any
    execution_profiles: Any
    statistical_capabilities: Any
    interpretation_cautions: Any
    capture: bool

    SCHEMA: ClassVar[str] = "pack-manifest-v1"

    @classmethod
    def _semantic(cls, value: dict[str, Any]) -> None:
        names = [metric["name"] for metric in value["metrics"]]
        _require(len(set(names)) == len(names), "pack metric names must be unique")
        paths = [output["path"].lower() for output in value["outputs"].values()]
        _require(len(set(paths)) == len(paths), "pack output paths must be distinct")
        _require(set(REQUIRED_SUPPLIED) <= set(value["statistical_capabilities"]),
                 "pack must be able to supply estimator, point_estimate and sample_size")

    def metric(self, name: str) -> Mapping[str, Any]:
        found = [metric for metric in self.metrics if metric["name"] == name]
        _require(len(found) == 1, f"pack does not declare metric {name}")
        return found[0]

    def output_paths(self) -> dict[str, str]:
        return {label: output["path"] for label, output in self.outputs.items()}


@dataclass(frozen=True)
class ParameterCatalog(_Envelope):
    schema_version: int
    pack_id: str
    pack_version: str
    description: str
    parameters_schema: Any
    metric: str
    outputs: Any
    limitations: Any

    SCHEMA: ClassVar[str] = "parameter-catalog-v1"

    @classmethod
    def _semantic(cls, value: dict[str, Any]) -> None:
        _size(value["parameters_schema"], 64 * 1024, "parameters_schema")
        supported_schema(value["parameters_schema"])

    def validate_parameters(self, parameters: Any) -> dict[str, Any]:
        value = strict_json(parameters)
        _require(type(value) is dict, "pack parameters must be a JSON object")
        _check(thaw(self.parameters_schema), value, "$parameters", {})
        return value


@dataclass(frozen=True)
class ProtocolDraft(_Envelope):
    schema_version: int
    design: str
    analysis_plan: str
    metric: str
    stopping_rule: str
    statistical_design: Any
    roster: Any
    roster_semantics: str
    sample_size_scope: str
    run_limit: int
    replication_tolerance: float
    seen_data: Any

    SCHEMA: ClassVar[str] = "protocol-draft-v1"

    @classmethod
    def _semantic(cls, value: dict[str, Any]) -> None:
        try:
            design = StatisticalDesign.from_dict(value["statistical_design"])
            design.validate_metric(value["metric"])
        except ValueError as exc:
            raise EnvelopeError(f"invalid protocol draft statistical design: {exc}") from exc
        _require(design.to_dict() == value["statistical_design"],
                 "protocol draft statistical design is not normalized")
        _require(design.stopping_rule.rule == value["stopping_rule"],
                 "protocol draft stopping rule differs from its statistical design")
        _require(len(set(value["roster"])) == len(value["roster"]),
                 "protocol draft roster units must be unique")
        _require(value["run_limit"] >= 2 * len(value["roster"]),
                 "protocol draft run limit must cover primary runs and reanalyses")
        _require(value["roster_semantics"] != "deterministic_single" or len(value["roster"]) == 1,
                 "deterministic_single roster has exactly one unit")
        _require(value["seen_data"] == sorted(value["seen_data"]),
                 "protocol draft seen_data must be sorted")

    def typed_design(self) -> StatisticalDesign:
        return StatisticalDesign.from_dict(thaw(self.statistical_design))


def _blob(data: bytes) -> dict[str, Any]:
    return dict(sha256=digest(data), bytes=len(data))


_ENVIRONMENT_NULLS = ("image_digest", "lock_digest", "accelerator", "network_policy",
                      "env_allowlist")


@dataclass(frozen=True)
class ExecutionPlan:
    """Runner bytes and limits; the kernel alone writes them to CAS."""

    primary_program: bytes
    reanalysis_program: bytes
    input: bytes
    outputs: Any
    wall_seconds: int
    max_output_bytes: int
    required_capabilities: Any
    execution_profile: str
    environment_requirements: Any

    SCHEMA: ClassVar[str] = "execution-plan-v1"

    def __post_init__(self) -> None:
        for name in ("primary_program", "reanalysis_program", "input"):
            data = getattr(self, name)
            _require(type(data) is bytes and 0 < len(data) <= MAX_BLOB_BYTES,
                     f"execution plan {name} must be nonempty bounded bytes")
        for name in ("outputs", "required_capabilities", "environment_requirements"):
            object.__setattr__(self, name, _freeze(strict_json(thaw(getattr(self, name)))))
        self.check(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return dict(schema_version=1, primary_program=_blob(self.primary_program),
                    reanalysis_program=_blob(self.reanalysis_program), input=_blob(self.input),
                    outputs=thaw(self.outputs), wall_seconds=self.wall_seconds,
                    max_output_bytes=self.max_output_bytes,
                    required_capabilities=thaw(self.required_capabilities),
                    execution_profile=self.execution_profile,
                    environment_requirements=thaw(self.environment_requirements))

    @classmethod
    def check(cls, value: Any) -> None:
        value = strict_json(value)
        _size(value, MAX_ENVELOPE_BYTES, cls.SCHEMA)
        validate_schema(cls.SCHEMA, value)
        _require(value["primary_program"]["sha256"] != value["reanalysis_program"]["sha256"],
                 "reanalysis program must differ from the primary program")
        _require(len({path.lower() for path in value["outputs"].values()}) == len(value["outputs"]),
                 "execution plan output paths must be distinct")

    def blobs(self) -> dict[str, bytes]:
        return {digest(data): data for data in
                (self.primary_program, self.reanalysis_program, self.input)}

    @classmethod
    def from_frozen(cls, value: Any, read: Callable[[str], bytes]) -> ExecutionPlan:
        value = strict_json(value)
        cls.check(value)
        loaded = {}
        for name in ("primary_program", "reanalysis_program", "input"):
            data = read(value[name]["sha256"])
            _require(len(data) == value[name]["bytes"], f"execution plan {name} size mismatch")
            loaded[name] = data
        return cls(**loaded, outputs=value["outputs"], wall_seconds=value["wall_seconds"],
                   max_output_bytes=value["max_output_bytes"],
                   required_capabilities=value["required_capabilities"],
                   execution_profile=value["execution_profile"],
                   environment_requirements=value["environment_requirements"])

    def digest(self) -> str:
        return digest(canonical(self.to_dict()))


def environment_requirements() -> dict[str, Any]:
    """The only v1 closure: an interpreter fingerprint, all reserved fields null."""
    return dict(closure_level="interpreter_fingerprint",
                **{name: None for name in _ENVIRONMENT_NULLS})


@dataclass(frozen=True)
class CaptureBundle:
    """Read-only capture result; ``files`` maps inventory paths to their bytes."""

    pack_id: str
    pack_version: str
    source_label: str
    files: Any
    audit: Any

    SCHEMA: ClassVar[str] = "capture-bundle-v1"

    def __post_init__(self) -> None:
        _require(isinstance(self.files, Mapping) and bool(self.files),
                 "capture bundle needs captured files")
        frozen: dict[str, bytes] = {}
        for path, data in self.files.items():
            _safe_relative(path)
            _require(type(data) is bytes and len(data) <= MAX_BLOB_BYTES,
                     f"captured file must be bounded bytes: {path}")
            frozen[path] = data
        object.__setattr__(self, "files", MappingProxyType(dict(sorted(frozen.items()))))
        object.__setattr__(self, "audit", _freeze(strict_json(thaw(self.audit))))
        self.check(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return dict(schema_version=1, pack_id=self.pack_id, pack_version=self.pack_version,
                    source=dict(kind="local_directory", label=self.source_label),
                    inventory=[dict(path=path, sha256=digest(data), bytes=len(data))
                               for path, data in self.files.items()],
                    audit=thaw(self.audit))

    @classmethod
    def check(cls, value: Any) -> None:
        value = strict_json(value)
        _size(value, MAX_ENVELOPE_BYTES, cls.SCHEMA)
        validate_schema(cls.SCHEMA, value)
        _size(value["audit"], 64 * 1024, "capture audit")
        paths = [row["path"] for row in value["inventory"]]
        _require(paths == sorted(paths) and len(set(paths)) == len(paths),
                 "capture inventory paths must be sorted and unique")
        for path in paths:
            _safe_relative(path)
        _require(sum(row["bytes"] for row in value["inventory"]) <= MAX_BLOB_BYTES,
                 "captured bytes exceed the bundle limit")

    def blobs(self) -> dict[str, bytes]:
        return {digest(data): data for data in self.files.values()}

    @classmethod
    def from_frozen(cls, value: Any, read: Callable[[str], bytes]) -> CaptureBundle:
        value = strict_json(value)
        cls.check(value)
        files = {}
        for row in value["inventory"]:
            data = read(row["sha256"])
            _require(len(data) == row["bytes"], f"captured file size mismatch: {row['path']}")
            files[row["path"]] = data
        return cls(pack_id=value["pack_id"], pack_version=value["pack_version"],
                   source_label=value["source"]["label"], files=files, audit=value["audit"])

    def digest(self) -> str:
        return digest(canonical(self.to_dict()))


@dataclass(frozen=True)
class OutputCheck(_Envelope):
    schema_version: int
    slot: str
    roster_unit: int
    mode: str
    result: str
    raw_data: str
    metrics: str
    status: str
    findings: Any

    SCHEMA: ClassVar[str] = "output-check-v1"

    @classmethod
    def _semantic(cls, value: dict[str, Any]) -> None:
        label, unit = value["slot"].split(":")
        _require(int(unit) == value["roster_unit"]
                 and value["mode"] == ("primary" if label == "primary" else "independent_reanalysis"),
                 "output check slot, roster unit and mode disagree")
        _require(value["status"] == "passed" or bool(value["findings"]),
                 "a failed output check must state its findings")


@dataclass(frozen=True)
class Recomputation(_Envelope):
    schema_version: int
    roster_unit: int
    metric: str
    recomputed: float
    primary_recorded: float
    reanalysis_recorded: float
    primary_absolute_difference: float
    reanalysis_absolute_difference: float
    tolerance: float

    SCHEMA: ClassVar[str] = "recomputation-v1"

    @classmethod
    def _semantic(cls, value: dict[str, Any]) -> None:
        for side in ("primary", "reanalysis"):
            expected = abs(value["recomputed"] - value[f"{side}_recorded"])
            _require(math.isfinite(expected) and value[f"{side}_absolute_difference"] == expected,
                     f"recomputation {side} difference is not |recomputed - recorded|")
            _require(expected <= value["tolerance"],
                     f"recomputed metric disagrees with the {side} record beyond tolerance")


def supplied(value: Any) -> dict[str, Any]:
    return dict(status="supplied", value=value)


def not_supplied(reason: str) -> dict[str, Any]:
    return dict(status="not_supplied", reason=reason)


def not_applicable(reason: str) -> dict[str, Any]:
    return dict(status="not_applicable", reason=reason)


def not_applicable_permitted(field: str, design: StatisticalDesign) -> bool:
    """Whether the preregistered design itself implies that a field does not apply."""
    if field in {"uncertainty", "confidence_interval"}:
        return design.uncertainty.method == "not_applicable"
    if field == "effect_size":
        return design.mode == "descriptive"
    if field == "multiple_testing":
        return design.multiple_testing.correction == "not_applicable"
    return False


@dataclass(frozen=True)
class StatisticalReport(_Envelope):
    schema_version: int
    protocol_hash: str
    estimand: Any
    estimator: Any
    point_estimate: Any
    uncertainty: Any
    confidence_interval: Any
    effect_size: Any
    assumptions: Any
    sample_size: Any
    multiple_testing: Any
    stopping_rule: Any
    sensitivity_analysis: Any
    deviations: Any

    SCHEMA: ClassVar[str] = "statistical-report-v1"

    @classmethod
    def check(cls, value: Any) -> None:
        value = strict_json(value)
        _require(type(value) is dict, "statistical report must be a JSON object")
        missing = [name for name in STATISTICAL_FIELDS if name not in value]
        _require(not missing, "statistical report lacks required fields: " + ", ".join(missing))
        for name in STATISTICAL_FIELDS:
            field = value[name]
            _require(type(field) is dict and field.get("status") in FIELD_STATUSES,
                     f"statistical report field {name} has an invalid status")
            if field["status"] != "supplied":
                reason = field.get("reason")
                _require(type(reason) is str and bool(reason.strip()),
                         f"statistical report field {name} is {field['status']} without a reason")
        super().check(value)

    @classmethod
    def _semantic(cls, value: dict[str, Any]) -> None:
        for name in REQUIRED_SUPPLIED:
            _require(value[name]["status"] == "supplied",
                     f"statistical report field {name} must be supplied")
        estimand = value["estimand"]
        if estimand["status"] == "supplied":
            _require(estimand["value"]["protocol_hash"] == value["protocol_hash"],
                     "estimand refers to another protocol")
        point = value["point_estimate"]["value"]
        units = [row["roster_unit"] for row in point["by_roster_unit"]]
        _require(point["value"] is not None or bool(units),
                 "point estimate needs a value or per-roster-unit values")
        _require(len(set(units)) == len(units), "point estimate roster units must be unique")
        interval = value["confidence_interval"]
        if interval["status"] == "supplied":
            _require(interval["value"]["lower"] <= interval["value"]["upper"],
                     "confidence interval lower bound exceeds its upper bound")
        sizes = value["sample_size"]["value"]
        _require(sizes["planned_total"] >= sizes["planned"],
                 "sample size planned_total cannot be below planned")

    def field(self, name: str) -> Mapping[str, Any]:
        _require(name in STATISTICAL_FIELDS, f"unknown statistical report field {name}")
        return getattr(self, name)

    def validate_design(self, design: StatisticalDesign) -> None:
        """Reject not_applicable that the preregistered design does not imply."""
        for name in STATISTICAL_FIELDS:
            if self.field(name)["status"] == "not_applicable":
                _require(not_applicable_permitted(name, design),
                         f"statistical report field {name} is not_applicable contrary to "
                         "the preregistered design")


@dataclass(frozen=True)
class AnalysisReport:
    """Pack proposal; the kernel stores ``statistical_report`` as its own artifact."""

    pack_id: str
    pack_version: str
    protocol_hash: str
    statement: str
    limitations: Any
    outcome: str
    inference_mode: str
    details: Any
    statistical_report: StatisticalReport

    SCHEMA: ClassVar[str] = "analysis-report-v2"

    def __post_init__(self) -> None:
        _require(type(self.statistical_report) is StatisticalReport,
                 "analysis report needs a StatisticalReport v1")
        for name in ("limitations", "details"):
            object.__setattr__(self, name, _freeze(strict_json(thaw(getattr(self, name)))))
        self.check(self.to_dict())
        _require(self.statistical_report.protocol_hash == self.protocol_hash,
                 "analysis and statistical reports refer to different protocols")

    def to_dict(self) -> dict[str, Any]:
        return dict(schema_version=2, pack_id=self.pack_id, pack_version=self.pack_version,
                    protocol_hash=self.protocol_hash, statement=self.statement,
                    limitations=thaw(self.limitations), outcome=self.outcome,
                    inference_mode=self.inference_mode, details=thaw(self.details),
                    statistical_report=self.statistical_report.digest())

    @classmethod
    def check(cls, value: Any) -> None:
        value = strict_json(value)
        _size(value, MAX_ENVELOPE_BYTES, cls.SCHEMA)
        validate_schema(cls.SCHEMA, value)
        _size(value["details"], MAX_DETAILS_BYTES, "analysis report details")

    @classmethod
    def from_frozen(cls, value: Any, statistical_report: StatisticalReport) -> AnalysisReport:
        value = strict_json(value)
        cls.check(value)
        _require(value["statistical_report"] == statistical_report.digest(),
                 "analysis report refers to another statistical report")
        fields_ = {key: item for key, item in value.items()
                   if key not in {"schema_version", "statistical_report"}}
        return cls(**fields_, statistical_report=statistical_report)

    def digest(self) -> str:
        return digest(canonical(self.to_dict()))


def published_schema(name: str) -> dict[str, Any]:
    """Detached copy of a published JSON Schema."""
    return json.loads(canonical(SCHEMAS[name]))
