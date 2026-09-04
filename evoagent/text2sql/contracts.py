"""Strict domain contracts shared by Text2SQL agents and deterministic gates."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence


_QUALIFIED = re.compile(r"^t_[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
_TABLE = re.compile(r"^t_[A-Za-z0-9_]+$")


def _strings(values: Sequence[Any], limit: int = 100) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))[:limit]


@dataclass(frozen=True)
class QuerySpec:
    intent: str
    subject: str
    dimensions: Sequence[str] = field(default_factory=tuple)
    measures: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    filters: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    order_by: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    limit: int = 20
    expected_shape: str = "rows"
    version: int = 1

    def __post_init__(self) -> None:
        if self.intent not in {"lookup", "count", "aggregate", "ranking", "existence"}:
            raise ValueError("unsupported QuerySpec intent")
        if not self.subject.strip():
            raise ValueError("QuerySpec subject is required")
        if self.expected_shape not in {"scalar", "rows", "grouped_rows"}:
            raise ValueError("unsupported QuerySpec expected_shape")
        if not 1 <= int(self.limit) <= 1000:
            raise ValueError("QuerySpec limit must be between 1 and 1000")
        if int(self.version) < 1:
            raise ValueError("QuerySpec version must be positive")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "QuerySpec":
        return cls(
            intent=str(value.get("intent", "")),
            subject=str(value.get("subject", "")),
            dimensions=_strings(value.get("dimensions") or ()),
            measures=tuple(dict(item) for item in value.get("measures") or () if isinstance(item, Mapping)),
            filters=tuple(dict(item) for item in value.get("filters") or () if isinstance(item, Mapping)),
            order_by=tuple(dict(item) for item in value.get("order_by") or () if isinstance(item, Mapping)),
            limit=int(value.get("limit", 20)),
            expected_shape=str(value.get("expected_shape", "rows")),
            version=int(value.get("version", 1)),
        )

    def as_dict(self) -> Mapping[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class JoinSpec:
    left: str
    right: str
    join_type: str = "inner"
    evidence_id: str = ""
    source: str = "stable"

    def __post_init__(self) -> None:
        if not _QUALIFIED.fullmatch(self.left) or not _QUALIFIED.fullmatch(self.right):
            raise ValueError("JoinSpec endpoints must be qualified table.column identifiers")
        if self.join_type not in {"inner", "left"}:
            raise ValueError("only inner and left joins are supported")
        if self.source not in {"stable", "user_explicit", "draft_inferred"}:
            raise ValueError("unsupported JoinSpec source")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JoinSpec":
        return cls(
            left=str(value.get("left", "")),
            right=str(value.get("right", "")),
            join_type=str(value.get("type", value.get("join_type", "inner"))).lower(),
            evidence_id=str(value.get("evidence_id", "")),
            source=str(value.get("source", "stable")),
        )


@dataclass(frozen=True)
class SchemaPlan:
    tables: Sequence[str]
    columns: Sequence[str]
    joins: Sequence[JoinSpec] = field(default_factory=tuple)
    result_grain: Sequence[str] = field(default_factory=tuple)
    evidence_ids: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.tables:
            raise ValueError("SchemaPlan requires at least one table")
        if any(not _TABLE.fullmatch(value) for value in self.tables):
            raise ValueError("SchemaPlan contains an invalid table identifier")
        if any(not _QUALIFIED.fullmatch(value) for value in self.columns):
            raise ValueError("SchemaPlan contains an invalid column identifier")
        if any(not _QUALIFIED.fullmatch(value) for value in self.result_grain):
            raise ValueError("SchemaPlan contains an invalid result-grain identifier")
        planned_tables = set(self.tables)
        referenced = {value.split(".", 1)[0] for value in self.columns}
        referenced.update(join.left.split(".", 1)[0] for join in self.joins)
        referenced.update(join.right.split(".", 1)[0] for join in self.joins)
        if not referenced.issubset(planned_tables):
            raise ValueError("SchemaPlan references a column outside its planned tables")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SchemaPlan":
        return cls(
            tables=_strings(value.get("tables") or ()),
            columns=_strings(value.get("columns") or ()),
            joins=tuple(
                JoinSpec.from_dict(item)
                for item in value.get("joins") or ()
                if isinstance(item, Mapping)
            ),
            result_grain=_strings(value.get("result_grain") or ()),
            evidence_ids=_strings(value.get("evidence_ids") or value.get("evidence") or ()),
        )

    def as_dict(self) -> Mapping[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SQLCandidate:
    candidate_id: str
    sql: str
    query_spec_version: int
    database_snapshot_id: str
    wiki_index_version: str
    memory_snapshot_id: str
    policy_version: str
    vanna_index_version: str = ""
    revision: int = 0
    evidence_ids: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.candidate_id.strip() or not self.sql.strip():
            raise ValueError("SQLCandidate id and SQL are required")
        if int(self.query_spec_version) < 1 or int(self.revision) < 0:
            raise ValueError("SQLCandidate versions cannot be negative")
        pins = (
            self.database_snapshot_id,
            self.wiki_index_version,
            self.memory_snapshot_id,
            self.policy_version,
        )
        if any(not value for value in pins):
            raise ValueError("SQLCandidate requires all four version pins")

    def as_dict(self) -> Mapping[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SQLGateResult:
    accepted: bool
    normalized_sql: str = ""
    tables: Sequence[str] = field(default_factory=tuple)
    columns: Sequence[str] = field(default_factory=tuple)
    errors: Sequence[str] = field(default_factory=tuple)
    fingerprint: str = ""

    def as_dict(self) -> Mapping[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SQLExecutionResult:
    columns: Sequence[str]
    rows: Sequence[Sequence[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: int
    explain_plan: Sequence[Sequence[Any]]
    sql_fingerprint: str

    def as_dict(self) -> Mapping[str, Any]:
        return asdict(self)
