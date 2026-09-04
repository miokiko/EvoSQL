"""Trust and version rules shared by Text2SQL retrieval and execution.

Database, Wiki and Memory are deliberately separate trust domains:

* Database is authoritative for physical schema and observed values.
* Approved Wiki content is authoritative for business semantics.
* Memory is historical evidence only and can never override either source.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Tuple


VALID_SOURCES = frozenset({"database", "wiki", "memory"})
VALID_CLAIM_KINDS = frozenset({"physical", "business", "experience"})
VALID_STATES = frozenset({"candidate", "stable", "revoked"})


@dataclass(frozen=True)
class KnowledgeAssertion:
    evidence_id: str
    source: str
    claim_kind: str
    key: str
    value: Any
    version: str
    state: str = "stable"

    def __post_init__(self) -> None:
        if self.source not in VALID_SOURCES:
            raise ValueError("unsupported knowledge source: %s" % self.source)
        if self.claim_kind not in VALID_CLAIM_KINDS:
            raise ValueError("unsupported claim kind: %s" % self.claim_kind)
        if self.state not in VALID_STATES:
            raise ValueError("unsupported knowledge state: %s" % self.state)
        if not self.evidence_id or not self.key or not self.version:
            raise ValueError("evidence_id, key and version are required")


@dataclass(frozen=True)
class AuthorityDecision:
    status: str
    assertion: Optional[KnowledgeAssertion]
    reason: str
    conflicts: Tuple[str, ...] = ()


@dataclass(frozen=True)
class QueryVersionPin:
    """The four immutable versions needed to replay one Text2SQL task."""

    database_snapshot_id: str
    wiki_index_version: str
    memory_snapshot_id: str
    policy_version: str

    def __post_init__(self) -> None:
        values = (
            self.database_snapshot_id,
            self.wiki_index_version,
            self.memory_snapshot_id,
            self.policy_version,
        )
        if any(not value.strip() for value in values):
            raise ValueError("all Text2SQL version pins are required")


def _stable(assertions: Iterable[KnowledgeAssertion]) -> list[KnowledgeAssertion]:
    return [item for item in assertions if item.state == "stable"]


def _single_value_or_conflict(
    assertions: Iterable[KnowledgeAssertion], reason: str
) -> AuthorityDecision:
    items = list(assertions)
    if not items:
        return AuthorityDecision("unresolved", None, reason)
    grouped: dict[str, list[KnowledgeAssertion]] = {}
    for item in items:
        marker = repr(item.value)
        grouped.setdefault(marker, []).append(item)
    if len(grouped) > 1:
        return AuthorityDecision(
            "knowledge_conflict",
            None,
            "authoritative sources disagree",
            tuple(sorted(item.evidence_id for item in items)),
        )
    selected = sorted(items, key=lambda item: (item.version, item.evidence_id))[-1]
    return AuthorityDecision("resolved", selected, reason)


def resolve_authority(
    claim_kind: str, assertions: Iterable[KnowledgeAssertion]
) -> AuthorityDecision:
    """Resolve a claim without allowing lower-trust sources to override truth.

    A conflict among equally authoritative stable assertions fails closed. The
    caller must ask for review instead of selecting whichever item was recalled
    first.
    """

    if claim_kind not in VALID_CLAIM_KINDS:
        raise ValueError("unsupported claim kind: %s" % claim_kind)
    items = [item for item in _stable(assertions) if item.claim_kind == claim_kind]

    if claim_kind == "physical":
        return _single_value_or_conflict(
            (item for item in items if item.source == "database"),
            "database is authoritative for physical facts",
        )
    if claim_kind == "business":
        return _single_value_or_conflict(
            (item for item in items if item.source == "wiki"),
            "approved Wiki is authoritative for business semantics",
        )
    return _single_value_or_conflict(
        (item for item in items if item.source == "memory"),
        "memory is authoritative only for historical experience",
    )
