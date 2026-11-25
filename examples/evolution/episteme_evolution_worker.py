"""Trusted entry point copied outside the allowlisted candidate package."""

from typing import Any


def run(operation: str, payload: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    if operation not in {"policy", "evaluate"}:
        raise ValueError("unsupported evolution worker operation")
    from episteme.research_policy import recommend

    return recommend(payload, profile["role_instructions"].get("orchestrator", ""))
