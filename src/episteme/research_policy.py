"""Default pure policy for the isolated evolution worker."""

from typing import Any


def recommend(projection: dict[str, Any], instruction: str) -> dict[str, Any]:
    """Return the supplied research priorities for admission by the controller."""
    return {"priorities": projection["priorities"]}
