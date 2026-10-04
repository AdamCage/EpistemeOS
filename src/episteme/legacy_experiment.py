"""Frozen compiler for experiment-proposal v1 and agent_request schema 2.

New pack-scoped proposals do not use this module. It remains so that the
historical synthetic recipe binding, its catalog bytes and its compilation
manifest stay readable and so that the same admission can still be replayed.
Replay of an already stored application does not call ``compile_recipe``;
a new admission of schema 2 still compiles with this unchanged module.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from . import experiment_proposals
from .domains import synthetic_causal
from .execution import BACKEND, _object
from .kernel import require
from .runner_backend import fingerprint
from .store import Store


COMPILER_SOURCE_BYTES = Path(synthetic_causal.__file__).read_bytes()
DEFAULT_REANALYSIS_TOLERANCE = synthetic_causal.DEFAULT_REANALYSIS_TOLERANCE


def recipe_binding(store: Store, key: str, *, current: bool) -> dict[str, Any]:
    binding = _object(store.read(key))
    require(set(binding) == {"schema_version", "recipe_id", "catalog", "world", "seeds",
                             "environment", "replication_tolerance"}
            and type(binding["schema_version"]) is int and binding["schema_version"] == 1
            and binding["recipe_id"] == experiment_proposals.RECIPE_ID,
            "invalid frozen experiment recipe binding")
    require(type(binding["catalog"]) is dict and binding["catalog"].get("id") == binding["recipe_id"],
            "invalid frozen recipe catalog")
    require(type(binding["world"]) is dict and set(binding["world"]) ==
            {"treatment_effect", "confounding_strength", "noise_std"}, "invalid synthetic world")
    require(type(binding["seeds"]) is list and 1 <= len(binding["seeds"]) <= 64
            and all(type(seed) is int and -(2**31) <= seed < 2**31 for seed in binding["seeds"])
            and len(set(binding["seeds"])) == len(binding["seeds"]), "invalid frozen experiment seeds")
    tolerance = binding["replication_tolerance"]
    require(type(tolerance) in (int, float) and math.isfinite(tolerance) and tolerance >= 0,
            "invalid frozen replication tolerance")
    environment = _object(store.read(binding["environment"]))
    require(set(environment) == {"schema_version", "backend", "fingerprint"}
            and type(environment["schema_version"]) is int and environment["schema_version"] == 1
            and environment["backend"] == BACKEND and type(environment["fingerprint"]) is dict,
            "experiment requires a frozen local Python environment")
    if current:
        require(environment["fingerprint"] == fingerprint(),
                "experiment environment no longer matches this local runner")
        require(binding["catalog"] == synthetic_causal.describe(),
                "experiment recipe catalog changed since it was frozen")
        # Validation is domain-owned.  Do not expose the host world to the model.
        synthetic_causal.compile_recipe(dict(n_samples=32, assignment="randomized",
                                             analysis="difference_in_means"), binding["world"])
    return binding


def freeze_recipe_binding(store: Store, *, world: dict[str, Any], seeds: list[int],
                          environment: str,
                          replication_tolerance: float = DEFAULT_REANALYSIS_TOLERANCE) -> str:
    """Freeze host-owned synthetic world and execution policy before a model call."""
    binding = dict(schema_version=1, recipe_id=experiment_proposals.RECIPE_ID,
                   catalog=synthetic_causal.describe(), world=world, seeds=seeds,
                   environment=environment, replication_tolerance=replication_tolerance)
    key = store.put_json(binding)
    recipe_binding(store, key, current=True)
    return key


def compile_proposal(parameters: dict[str, Any], world: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    """Compile schema 2 parameters with the compiler bytes frozen at import."""
    require(Path(synthetic_causal.__file__).read_bytes() == COMPILER_SOURCE_BYTES,
            "loaded experiment compiler source changed on disk")
    return synthetic_causal.compile_recipe(parameters, world), COMPILER_SOURCE_BYTES
