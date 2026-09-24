"""Append-only proposal contract registry.

agent_proposals.py defines v1. Keep its prompt/schema/validator semantics stable
after release; introduce a new module and registry entry for a new contract.
Historical requests select their saved version, never the current default.
"""

from copy import deepcopy
from types import MappingProxyType

from . import agent_proposals as v1
from . import experiment_proposals as experiment_v1

DEFAULT_PROFILE = "hypothesis-proposal-v1"
_PROFILES = MappingProxyType({
    "hypothesis-proposal-v1": (
        v1.SYSTEM_PROMPT, deepcopy(v1.STRUCTURED_OUTPUT_SCHEMA), v1.validate_proposal),
    "experiment-proposal-v1": (
        experiment_v1.SYSTEM_PROMPT, deepcopy(experiment_v1.STRUCTURED_OUTPUT_SCHEMA),
        experiment_v1.validate_proposal),
})


def profile(version: str):
    if type(version) is not str or version not in _PROFILES:
        raise ValueError("unsupported proposal profile version")
    prompt, schema, validate = _PROFILES[version]
    return prompt, deepcopy(schema), validate
