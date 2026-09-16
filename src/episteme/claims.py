"""Versioned claim-link declarations without storage or scientific promotion.

A relation records an actor's assertion about two claims, not verified support
or contradiction. The kernel must resolve claim IDs, verify event/basis hashes,
and apply scope, cycle, review and transition policies against persisted state.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal


class ClaimLinkError(ValueError):
    """A claim-link declaration is malformed or refers to itself."""


@dataclass(frozen=True)
class ClaimLink:
    """An immutable v1 assertion: source supports/contradicts/limits/supersedes target.

    Event hashes identify the claimed revisions; basis hashes identify the
    evidence snapshots considered for each endpoint. Their actual identities,
    existence and currency cannot be established by this pure declaration.
    In particular, ``supersedes`` does not delete the target or close findings.
    """

    schema_version: int
    source: str
    target: str
    relation: Literal["supports", "contradicts", "limits", "supersedes"]
    rationale: str
    source_hash: str
    target_hash: str
    source_basis: str
    target_basis: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ClaimLinkError("unsupported claim-link schema_version")
        for field in ("source", "target", "rationale"):
            value = getattr(self, field)
            if type(value) is not str or not value.strip():
                raise ClaimLinkError(f"{field} must be a nonempty string")
        if self.source == self.target:
            raise ClaimLinkError("source and target must be distinct claim IDs")
        if type(self.relation) is not str or self.relation not in {
                "supports", "contradicts", "limits", "supersedes"}:
            raise ClaimLinkError("unsupported claim-link relation")
        for field in ("source_hash", "target_hash", "source_basis", "target_basis"):
            value = getattr(self, field)
            if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ClaimLinkError(f"{field} must be a lowercase SHA-256 digest")

    @classmethod
    def from_dict(cls, value: Any) -> ClaimLink:
        if type(value) is not dict:
            raise ClaimLinkError("claim link must be an object")
        required = set(cls.__dataclass_fields__)
        if not required <= value.keys():
            raise ClaimLinkError("claim link is missing required fields")
        if value.keys() != required:
            raise ClaimLinkError("claim link contains unknown fields")
        return cls(**value)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {field: getattr(self, field) for field in self.__dataclass_fields__}
