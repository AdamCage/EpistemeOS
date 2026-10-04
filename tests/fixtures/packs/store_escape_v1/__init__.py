"""Fixture pack that tries to read a parent-only path and import the store.

Not in the production allowlist. Nothing it does is evidence or a review.
The static import check does not see this attempt; the subprocess must reject it.
"""

from episteme.domains import api


PACK_ID = "store_escape_v1"
PACK_VERSION = "1"
METRIC = "unit_mean"
OUTPUTS = {"raw_data": "raw.json", "metrics": "metrics.json"}

MANIFEST = {
    "contract_version": 1, "pack_id": PACK_ID, "pack_version": PACK_VERSION,
    "description": "Fixture that attempts to reach the kernel store from a hook.",
    "roster_semantics": ["deterministic_single"],
    "metrics": [{"name": METRIC, "unit": "points", "description": "Unused fixture metric"}],
    "outputs": {"raw_data": {"path": "raw.json", "schema_id": "store_escape_v1/raw-v1"},
                "metrics": {"path": "metrics.json", "schema_id": "store_escape_v1/metrics-v1"}},
    "numeric_tolerance": 1e-12, "hidden_inputs": [],
    "execution_profiles": ["trusted_local_python_v1"],
    "statistical_capabilities": ["estimand", "estimator", "point_estimate", "sample_size",
                                 "stopping_rule", "sensitivity_analysis", "deviations"],
    "interpretation_cautions": ["Fixture escape attempt; not evidence."],
    "capture": False,
}


def describe():
    return api.ParameterCatalog(
        schema_version=1, pack_id=PACK_ID, pack_version=PACK_VERSION,
        description="Fixture escape attempt.",
        parameters_schema={"type": "object", "additionalProperties": False,
                           "required": ["scale", "outcome", "inference_mode"],
                           "properties": {"scale": {"type": "integer", "minimum": 1, "maximum": 10},
                                          "outcome": {"enum": ["supports", "refutes", "inconclusive"]},
                                          "inference_mode": {"enum": ["descriptive", "exploratory",
                                                                      "confirmatory"]}}},
        metric=METRIC, outputs=dict(OUTPUTS),
        limitations=["Fixture escape attempt; not evidence."])


def validate_parameters(parameters):
    if set(parameters) != {"scale", "outcome", "inference_mode"}:
        raise ValueError("unexpected escape fixture parameters")


def _steal():
    # A Call named __import__ is rejected by the static check. This form is not.
    builtin = (__builtins__["__import__"] if isinstance(__builtins__, dict)
               else getattr(__builtins__, "__import__"))
    osmod = builtin("os")
    path = osmod.environ.get("EPISTEME_CANARY_PATH")
    stolen = b""
    if path:
        stolen = builtin("pathlib").Path(path).read_bytes()
    store_name = "not-imported"
    try:
        builtin("episteme.store")
        store_name = "imported"
    except Exception as exc:
        store_name = type(exc).__name__
    sql_name = "not-imported"
    try:
        builtin("sqlite3")
        sql_name = "imported"
    except Exception as exc:
        sql_name = type(exc).__name__
    opened = False
    if path:
        try:
            builtin("sqlite3").connect(path)
            opened = True
        except Exception:
            opened = False
    raise RuntimeError(f"stolen={stolen!r} store={store_name} sqlite={sql_name} opened={opened}")


def compile_protocol(request):
    _steal()


def compile_execution(request):
    _steal()


def validate_protocol(context):
    return None


def validate_outputs(context, cas):
    _steal()


def recompute_metrics(context, cas):
    _steal()


def analyse(context, cas, checks, recomputations):
    _steal()
