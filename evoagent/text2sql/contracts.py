"""Strict domain contracts shared by Text2SQL agents and deterministic gates."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional, Sequence


_QUALIFIED = re.compile(r"^t_[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
_TABLE = re.compile(r"^t_[A-Za-z0-9_]+$")


def _strings(values: Sequence[Any], limit: int = 100) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, Mapping)) or not isinstance(values, Sequence):
        raise ValueError("string collection fields must be a sequence")
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))[:limit]


def _dimension_values(values: Sequence[Any], limit: int = 100) -> tuple[Any, ...]:
    if isinstance(values, (str, bytes, Mapping)) or not isinstance(values, Sequence):
        raise ValueError("QuerySpec dimensions must be a sequence")
    normalized: list[Any] = []
    for value in values:
        if isinstance(value, Mapping):
            normalized.append(dict(value))
        elif isinstance(value, str) and value.strip():
            normalized.append(value.strip())
        else:
            raise ValueError("QuerySpec dimensions must contain strings or mappings")
    return tuple(normalized[:limit])


def _mapping_values(values: Sequence[Any], label: str, limit: int = 100) -> tuple[Mapping[str, Any], ...]:
    if isinstance(values, (str, bytes, Mapping)) or not isinstance(values, Sequence):
        raise ValueError("QuerySpec %s must be a sequence" % label)
    if any(not isinstance(value, Mapping) for value in values):
        raise ValueError("QuerySpec %s entries must be mappings" % label)
    return tuple(dict(value) for value in values[:limit])


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("%s is required" % label)
    return text


def _fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _logical_key(value: Any) -> str:
    return " ".join(str(value).strip().casefold().split())


def _value_key(value: Any) -> str:
    """Return a type-preserving key while treating JSON arrays and tuples alike."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


@dataclass(frozen=True)
class QueryDimension:
    """One logical result dimension, before it is bound to a physical column."""

    slot_id: str
    concept: str

    def __post_init__(self) -> None:
        if not self.slot_id.strip() or not self.concept.strip():
            raise ValueError("QueryDimension slot_id and concept are required")

    @classmethod
    def from_value(cls, value: Any, index: int = 0) -> "QueryDimension":
        if isinstance(value, str):
            return cls("dimension-%d" % (index + 1), _required_text(value, "dimension concept"))
        if not isinstance(value, Mapping):
            raise ValueError("QuerySpec dimension must be a string or mapping")
        concept = (
            value.get("concept")
            or value.get("field_concept")
            or value.get("field")
            or value.get("column")
            or value.get("name")
        )
        return cls(
            _required_text(value.get("slot_id") or value.get("id") or "dimension-%d" % (index + 1), "dimension slot_id"),
            _required_text(concept, "dimension concept"),
        )


@dataclass(frozen=True)
class QueryMeasure:
    """A logical metric with explicit aggregation and cardinality semantics."""

    slot_id: str
    name: str
    aggregation: str
    field_concept: str = ""
    distinct: Optional[bool] = None
    count_all: bool = False

    def __post_init__(self) -> None:
        if not self.slot_id.strip() or not self.name.strip():
            raise ValueError("QueryMeasure slot_id and name are required")
        if type(self.count_all) is not bool:
            raise ValueError("QueryMeasure count_all must be boolean")
        if self.aggregation not in {"none", "count", "sum", "avg", "min", "max"}:
            raise ValueError("unsupported QueryMeasure aggregation")
        if self.count_all and self.aggregation != "count":
            raise ValueError("count_all is only valid for count measures")
        if self.count_all and self.field_concept:
            raise ValueError("count_all cannot also name a field")
        if self.count_all and self.distinct is True:
            raise ValueError("COUNT(*) cannot use DISTINCT")
        if self.aggregation not in {"none", "count"} and not self.field_concept:
            raise ValueError("aggregate measure requires a field_concept")
        if self.aggregation == "none" and self.distinct is not None:
            raise ValueError("distinct is only valid for aggregate measures")

    @classmethod
    def from_value(cls, value: Any, index: int = 0) -> "QueryMeasure":
        if isinstance(value, str):
            value = {"name": value, "aggregation": "none", "field_concept": value}
        if not isinstance(value, Mapping):
            raise ValueError("QuerySpec measure must be a string or mapping")
        aggregation = str(value.get("aggregation") or value.get("function") or "none").strip().lower()
        distinct_value = value.get("distinct")
        if aggregation in {"count_distinct", "distinct_count"}:
            aggregation = "count"
            distinct_value = True
        if distinct_value is not None and not isinstance(distinct_value, bool):
            raise ValueError("QueryMeasure distinct must be boolean when provided")
        field_concept = str(
            value.get("field_concept")
            or value.get("field")
            or value.get("column")
            or value.get("concept")
            or ""
        ).strip()
        count_all = value.get("count_all", False)
        return cls(
            slot_id=_required_text(value.get("slot_id") or value.get("id") or "measure-%d" % (index + 1), "measure slot_id"),
            name=_required_text(value.get("name") or field_concept or aggregation, "measure name"),
            aggregation=aggregation,
            field_concept=field_concept,
            distinct=distinct_value,
            count_all=count_all,
        )


@dataclass(frozen=True)
class QueryFilter:
    """A logical predicate. Multiple filters are an AND conjunction."""

    slot_id: str
    field_concept: str
    operator: str
    value: Any = None
    scope: str = "where"

    def __post_init__(self) -> None:
        if not self.slot_id.strip() or not self.field_concept.strip():
            raise ValueError("QueryFilter slot_id and field_concept are required")
        if self.operator not in {
            "eq",
            "neq",
            "gt",
            "gte",
            "lt",
            "lte",
            "in",
            "not_in",
            "like",
            "not_like",
            "between",
            "is_null",
            "is_not_null",
        }:
            raise ValueError("unsupported QueryFilter operator")
        if self.scope != "where":
            raise ValueError("QuerySpec/v1 supports WHERE filters only")
        if self.operator not in {"is_null", "is_not_null"} and self.value is None:
            raise ValueError("QueryFilter value is required for this operator")
        if self.operator in {"in", "not_in", "between"} and (
            isinstance(self.value, (str, bytes)) or not isinstance(self.value, Sequence)
        ):
            raise ValueError("QueryFilter operator requires a sequence value")
        if self.operator == "between" and len(self.value) != 2:
            raise ValueError("between requires exactly two values")
        if self.operator in {"in", "not_in"} and not self.value:
            raise ValueError("IN predicates require at least one value")

    @classmethod
    def from_value(cls, value: Any, index: int = 0) -> "QueryFilter":
        if not isinstance(value, Mapping):
            raise ValueError("QuerySpec filter must be a mapping")
        aliases = {
            "=": "eq",
            "==": "eq",
            "!=": "neq",
            "<>": "neq",
            ">": "gt",
            ">=": "gte",
            "<": "lt",
            "<=": "lte",
            "is null": "is_null",
            "is not null": "is_not_null",
            "not in": "not_in",
            "not like": "not_like",
        }
        operator = str(value.get("operator") or value.get("op") or "eq").strip().lower()
        operator = aliases.get(operator, operator)
        field_concept = (
            value.get("field_concept")
            or value.get("field")
            or value.get("column")
            or value.get("concept")
        )
        return cls(
            slot_id=_required_text(value.get("slot_id") or value.get("id") or "filter-%d" % (index + 1), "filter slot_id"),
            field_concept=_required_text(field_concept, "filter field_concept"),
            operator=operator,
            value=value.get("value"),
            scope=str(value.get("scope") or "where").strip().lower(),
        )


@dataclass(frozen=True)
class QueryOrder:
    """One ordered logical output slot or field."""

    slot_id: str
    target: str
    direction: str = "asc"

    def __post_init__(self) -> None:
        if not self.slot_id.strip() or not self.target.strip():
            raise ValueError("QueryOrder slot_id and target are required")
        if self.direction not in {"asc", "desc"}:
            raise ValueError("unsupported QueryOrder direction")

    @classmethod
    def from_value(cls, value: Any, index: int = 0) -> "QueryOrder":
        if isinstance(value, str):
            value = {"target": value}
        if not isinstance(value, Mapping):
            raise ValueError("QuerySpec order_by entry must be a string or mapping")
        target = (
            value.get("target")
            or value.get("slot")
            or value.get("field_concept")
            or value.get("field")
            or value.get("column")
            or value.get("name")
        )
        direction = str(value.get("direction") or value.get("order") or "asc").strip().lower()
        return cls(
            slot_id=_required_text(value.get("slot_id") or value.get("id") or "order-%d" % (index + 1), "order slot_id"),
            target=_required_text(target, "order target"),
            direction=direction,
        )


@dataclass(frozen=True)
class QuerySpec:
    intent: str
    subject: str
    dimensions: Sequence[Any] = field(default_factory=tuple)
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
        if type(self.limit) is not int:
            raise ValueError("QuerySpec limit must be an integer")
        if not 1 <= self.limit <= 1000:
            raise ValueError("QuerySpec limit must be between 1 and 1000")
        if type(self.version) is not int:
            raise ValueError("QuerySpec version must be an integer")
        if self.version < 1:
            raise ValueError("QuerySpec version must be positive")
        object.__setattr__(self, "dimensions", _dimension_values(self.dimensions))
        object.__setattr__(self, "measures", _mapping_values(self.measures, "measures"))
        object.__setattr__(self, "filters", _mapping_values(self.filters, "filters"))
        object.__setattr__(self, "order_by", _mapping_values(self.order_by, "order_by"))
        if any(
            str(item.get("scope") or "where").strip().lower() != "where"
            for item in self.filters
        ):
            raise ValueError("QuerySpec/v1 supports WHERE filters only")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "QuerySpec":
        return cls(
            intent=str(value.get("intent", "")),
            subject=str(value.get("subject", "")),
            dimensions=_dimension_values(value.get("dimensions") or ()),
            measures=_mapping_values(value.get("measures") or (), "measures"),
            filters=_mapping_values(value.get("filters") or (), "filters"),
            order_by=_mapping_values(value.get("order_by") or (), "order_by"),
            limit=value.get("limit", 20),
            expected_shape=str(value.get("expected_shape", "rows")),
            version=value.get("version", 1),
        )

    def as_dict(self) -> Mapping[str, Any]:
        return asdict(self)

    def dimension_specs(self) -> tuple[QueryDimension, ...]:
        return tuple(QueryDimension.from_value(item, index) for index, item in enumerate(self.dimensions))

    def measure_specs(self) -> tuple[QueryMeasure, ...]:
        return tuple(QueryMeasure.from_value(item, index) for index, item in enumerate(self.measures))

    def filter_specs(self) -> tuple[QueryFilter, ...]:
        return tuple(QueryFilter.from_value(item, index) for index, item in enumerate(self.filters))

    def order_specs(self) -> tuple[QueryOrder, ...]:
        return tuple(QueryOrder.from_value(item, index) for index, item in enumerate(self.order_by))


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
class SchemaValueBinding:
    """Evidence-backed mapping from a logical literal to its stored value."""

    logical_value: Any
    physical_value: Any
    evidence_ids: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_ids", _strings(self.evidence_ids))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SchemaValueBinding":
        if "logical_value" not in value or "physical_value" not in value:
            raise ValueError(
                "SchemaValueBinding requires logical_value and physical_value keys"
            )
        return cls(
            logical_value=value["logical_value"],
            physical_value=value["physical_value"],
            evidence_ids=_strings(value.get("evidence_ids") or value.get("evidence") or ()),
        )


@dataclass(frozen=True)
class SchemaBinding:
    """Evidence-backed mapping from a logical concept to one physical column."""

    logical_name: str
    column: str
    aliases: Sequence[str] = field(default_factory=tuple)
    evidence_ids: Sequence[str] = field(default_factory=tuple)
    value_bindings: Sequence[SchemaValueBinding] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.logical_name.strip():
            raise ValueError("SchemaBinding logical_name is required")
        if not _QUALIFIED.fullmatch(self.column):
            raise ValueError("SchemaBinding column must be a qualified table.column identifier")
        object.__setattr__(self, "aliases", _strings(self.aliases))
        object.__setattr__(self, "evidence_ids", _strings(self.evidence_ids))
        values = tuple(self.value_bindings)
        if any(not isinstance(item, SchemaValueBinding) for item in values):
            raise ValueError(
                "SchemaBinding value_bindings must contain SchemaValueBinding values"
            )
        object.__setattr__(self, "value_bindings", values)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SchemaBinding":
        raw_value_bindings = value.get("value_bindings") or ()
        if isinstance(raw_value_bindings, Mapping):
            raw_value_bindings = tuple(
                {
                    "logical_value": logical_value,
                    "physical_value": physical_value,
                }
                for logical_value, physical_value in raw_value_bindings.items()
            )
        elif isinstance(raw_value_bindings, (str, bytes)) or not isinstance(
            raw_value_bindings, Sequence
        ):
            raise ValueError("SchemaBinding value_bindings must be a sequence or mapping")
        if any(not isinstance(item, Mapping) for item in raw_value_bindings):
            raise ValueError("SchemaBinding value binding entries must be mappings")
        return cls(
            logical_name=str(
                value.get("logical_name")
                or value.get("concept")
                or value.get("field_concept")
                or value.get("slot_id")
                or ""
            ).strip(),
            column=str(value.get("column") or value.get("physical_column") or "").strip(),
            aliases=_strings(value.get("aliases") or ()),
            evidence_ids=_strings(value.get("evidence_ids") or value.get("evidence") or ()),
            value_bindings=tuple(
                SchemaValueBinding.from_dict(item) for item in raw_value_bindings
            ),
        )


@dataclass(frozen=True)
class SchemaPlan:
    tables: Sequence[str]
    columns: Sequence[str]
    joins: Sequence[JoinSpec] = field(default_factory=tuple)
    result_grain: Sequence[str] = field(default_factory=tuple)
    evidence_ids: Sequence[str] = field(default_factory=tuple)
    bindings: Sequence[SchemaBinding] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tables", _strings(self.tables))
        object.__setattr__(self, "columns", _strings(self.columns))
        object.__setattr__(self, "joins", tuple(self.joins))
        object.__setattr__(self, "result_grain", _strings(self.result_grain))
        object.__setattr__(self, "evidence_ids", _strings(self.evidence_ids))
        object.__setattr__(self, "bindings", tuple(self.bindings))
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
        if any(not isinstance(join, JoinSpec) for join in self.joins):
            raise ValueError("SchemaPlan joins must contain JoinSpec values")
        if any(not isinstance(binding, SchemaBinding) for binding in self.bindings):
            raise ValueError("SchemaPlan bindings must contain SchemaBinding values")
        referenced.update(join.left.split(".", 1)[0] for join in self.joins)
        referenced.update(join.right.split(".", 1)[0] for join in self.joins)
        referenced.update(binding.column.split(".", 1)[0] for binding in self.bindings)
        if not referenced.issubset(planned_tables):
            raise ValueError("SchemaPlan references a column outside its planned tables")
        if any(
            join.left not in self.columns or join.right not in self.columns
            for join in self.joins
        ):
            raise ValueError("SchemaPlan join endpoint is outside its planned columns")
        if any(binding.column not in self.columns for binding in self.bindings):
            raise ValueError("SchemaPlan binding references a column outside its planned columns")
        graph = {table: set() for table in planned_tables}
        for join in self.joins:
            left_table = join.left.split(".", 1)[0]
            right_table = join.right.split(".", 1)[0]
            if left_table == right_table:
                raise ValueError("SchemaPlan self joins are not representable without alias bindings")
            graph[left_table].add(right_table)
            graph[right_table].add(left_table)
        if len(planned_tables) > 1:
            uncovered = sorted(table for table, neighbors in graph.items() if not neighbors)
            if uncovered:
                raise ValueError(
                    "SchemaPlan join graph does not cover planned tables: %s"
                    % ", ".join(uncovered)
                )
            if len(self.joins) != len(planned_tables) - 1:
                raise ValueError(
                    "SchemaPlan join graph must contain one edge per introduced table"
                )
            start = next(iter(planned_tables))
            reached = {start}
            pending = [start]
            while pending:
                current = pending.pop()
                for neighbor in graph[current] - reached:
                    reached.add(neighbor)
                    pending.append(neighbor)
            if reached != planned_tables:
                raise ValueError("SchemaPlan join graph must be connected")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SchemaPlan":
        raw_joins = value.get("joins", ())
        if raw_joins is None:
            raw_joins = ()
        if isinstance(raw_joins, (str, bytes, Mapping)) or not isinstance(
            raw_joins, Sequence
        ):
            raise ValueError("SchemaPlan joins must be a sequence")
        if any(not isinstance(item, Mapping) for item in raw_joins):
            raise ValueError("SchemaPlan join entries must be mappings")
        raw_bindings = value.get("bindings") or value.get("field_bindings") or ()
        if isinstance(raw_bindings, Mapping):
            raw_bindings = tuple(
                {"logical_name": logical_name, "column": column}
                for logical_name, column in raw_bindings.items()
            )
        elif isinstance(raw_bindings, (str, bytes)) or not isinstance(raw_bindings, Sequence):
            raise ValueError("SchemaPlan bindings must be a sequence or mapping")
        if any(not isinstance(item, Mapping) for item in raw_bindings):
            raise ValueError("SchemaPlan binding entries must be mappings")
        return cls(
            tables=_strings(value.get("tables") or ()),
            columns=_strings(value.get("columns") or ()),
            joins=tuple(
                JoinSpec.from_dict(item)
                for item in raw_joins
            ),
            result_grain=_strings(value.get("result_grain") or ()),
            evidence_ids=_strings(value.get("evidence_ids") or value.get("evidence") or ()),
            bindings=tuple(
                SchemaBinding.from_dict(item)
                for item in raw_bindings
                if isinstance(item, Mapping)
            ),
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
    bound_plan_fingerprint: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_ids", _strings(self.evidence_ids))
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
        if self.bound_plan_fingerprint and not re.fullmatch(
            r"[0-9a-f]{64}", self.bound_plan_fingerprint
        ):
            raise ValueError("SQLCandidate bound_plan_fingerprint must be a SHA-256 digest")

    def as_dict(self) -> Mapping[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SQLCandidate":
        return cls(
            candidate_id=str(value.get("candidate_id") or ""),
            sql=str(value.get("sql") or ""),
            query_spec_version=int(value.get("query_spec_version", 0)),
            database_snapshot_id=str(value.get("database_snapshot_id") or ""),
            wiki_index_version=str(value.get("wiki_index_version") or ""),
            memory_snapshot_id=str(value.get("memory_snapshot_id") or ""),
            policy_version=str(value.get("policy_version") or ""),
            vanna_index_version=str(value.get("vanna_index_version") or ""),
            revision=int(value.get("revision", 0)),
            evidence_ids=_strings(value.get("evidence_ids") or ()),
            bound_plan_fingerprint=str(value.get("bound_plan_fingerprint") or ""),
        )


@dataclass(frozen=True)
class BindingConflict:
    """A deterministic, attributable reason that a plan cannot be bound."""

    code: str
    message: str
    owner: str
    slot_id: str = ""
    logical_name: str = ""
    candidates: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.message.strip() or not self.owner.strip():
            raise ValueError("BindingConflict code, message and owner are required")
        object.__setattr__(self, "candidates", _strings(self.candidates))

    def as_dict(self) -> Mapping[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlanBinding:
    """A resolved logical slot in an immutable bound query plan."""

    slot_id: str
    kind: str
    logical_name: str
    column: str = ""
    aggregation: str = ""
    distinct: Optional[bool] = None
    operator: str = ""
    value: Any = None
    logical_value: Any = None
    direction: str = ""
    scope: str = ""
    evidence_ids: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.kind not in {"dimension", "measure", "filter", "order"}:
            raise ValueError("unsupported PlanBinding kind")
        if not self.slot_id.strip() or not self.logical_name.strip():
            raise ValueError("PlanBinding slot_id and logical_name are required")
        if self.column and not _QUALIFIED.fullmatch(self.column):
            raise ValueError("PlanBinding column must be qualified")
        if self.distinct is not None and not isinstance(self.distinct, bool):
            raise ValueError("PlanBinding distinct must be boolean when provided")
        if self.kind in {"dimension", "filter", "order"} and not self.column:
            raise ValueError("this PlanBinding kind requires a physical column")
        if self.kind == "measure" and self.aggregation not in {
            "none",
            "count",
            "sum",
            "avg",
            "min",
            "max",
        }:
            raise ValueError("PlanBinding measure has an unsupported aggregation")
        if self.kind == "filter" and (
            not self.operator or self.scope not in {"where", "having"}
        ):
            raise ValueError("PlanBinding filter requires operator and scope")
        if self.kind == "order" and self.direction not in {"asc", "desc"}:
            raise ValueError("PlanBinding order requires asc or desc direction")
        object.__setattr__(self, "evidence_ids", _strings(self.evidence_ids))

    def as_dict(self) -> Mapping[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanBinding":
        return cls(
            slot_id=str(value.get("slot_id") or ""),
            kind=str(value.get("kind") or ""),
            logical_name=str(value.get("logical_name") or ""),
            column=str(value.get("column") or ""),
            aggregation=str(value.get("aggregation") or ""),
            distinct=value.get("distinct"),
            operator=str(value.get("operator") or ""),
            value=value.get("value"),
            logical_value=value.get("logical_value"),
            direction=str(value.get("direction") or ""),
            scope=str(value.get("scope") or ""),
            evidence_ids=_strings(value.get("evidence_ids") or ()),
        )


def _schema_binding_index(plan: SchemaPlan) -> Mapping[str, list[SchemaBinding]]:
    index: dict[str, list[SchemaBinding]] = {}
    for binding in plan.bindings:
        for name in (binding.logical_name, *binding.aliases):
            index.setdefault(_logical_key(name), []).append(binding)
    return index


def _resolve_schema_column(
    plan: SchemaPlan,
    index: Mapping[str, Sequence[SchemaBinding]],
    logical_name: str,
) -> tuple[str, tuple[str, ...]]:
    name = str(logical_name).strip()
    if not name:
        raise ValueError("BoundQueryPlan logical reference cannot be empty")
    if "." in name:
        if name not in plan.columns:
            raise ValueError("BoundQueryPlan qualified reference is outside SchemaPlan")
        return name, tuple(plan.evidence_ids)
    matches = tuple(index.get(_logical_key(name), ()))
    columns = tuple(sorted({item.column for item in matches}))
    if len(columns) == 1:
        evidence_ids = tuple(
            dict.fromkeys(
                evidence_id
                for item in matches
                for evidence_id in item.evidence_ids
                if evidence_id
            )
        )
        return columns[0], evidence_ids
    if len(columns) > 1:
        raise ValueError("BoundQueryPlan logical reference is ambiguous")
    suffix_matches = tuple(
        column for column in plan.columns if column.split(".", 1)[1] == name
    )
    if len(suffix_matches) == 1:
        return suffix_matches[0], tuple(plan.evidence_ids)
    if len(suffix_matches) > 1:
        raise ValueError("BoundQueryPlan physical-column suffix is ambiguous")
    raise ValueError("BoundQueryPlan logical reference has no SchemaPlan binding")


def _resolve_schema_value(
    index: Mapping[str, Sequence[SchemaBinding]],
    logical_name: str,
    column: str,
    logical_value: Any,
) -> tuple[Any, tuple[str, ...]]:
    matches = tuple(
        item
        for item in index.get(_logical_key(logical_name), ())
        if item.column == column
    )
    wanted = _value_key(logical_value)
    value_matches = tuple(
        value_binding
        for item in matches
        for value_binding in item.value_bindings
        if _value_key(value_binding.logical_value) == wanted
    )
    physical_values = {
        _value_key(item.physical_value): item.physical_value for item in value_matches
    }
    if len(physical_values) != 1:
        raise ValueError(
            "BoundQueryPlan filter value must have one explicit SchemaPlan value binding"
        )
    evidence_ids = tuple(
        dict.fromkeys(
            evidence_id
            for item in value_matches
            for evidence_id in item.evidence_ids
            if evidence_id
        )
    )
    return next(iter(physical_values.values())), evidence_ids


def _canonical_plan_bindings(
    query_spec: QuerySpec, schema_plan: SchemaPlan
) -> tuple[PlanBinding, ...]:
    """Replay the model-free slot binding inside the contract boundary.

    This deliberately mirrors the deterministic binder.  It prevents a caller
    or checkpoint from changing a resolved operator/value/aggregation while
    recomputing an otherwise self-consistent fingerprint.
    """

    index = _schema_binding_index(schema_plan)
    values: list[PlanBinding] = []
    for dimension in query_spec.dimension_specs():
        column, evidence_ids = _resolve_schema_column(
            schema_plan, index, dimension.concept
        )
        values.append(
            PlanBinding(
                slot_id=dimension.slot_id,
                kind="dimension",
                logical_name=dimension.concept,
                column=column,
                evidence_ids=evidence_ids,
            )
        )

    for measure in query_spec.measure_specs():
        if measure.count_all:
            column, evidence_ids = "", ()
        elif measure.field_concept:
            column, evidence_ids = _resolve_schema_column(
                schema_plan, index, measure.field_concept
            )
        else:
            raise ValueError(
                "BoundQueryPlan measure requires field_concept or count_all"
            )
        if measure.aggregation in {"count", "sum", "avg"} and measure.distinct is None:
            raise ValueError("BoundQueryPlan aggregate distinct semantics are missing")
        values.append(
            PlanBinding(
                slot_id=measure.slot_id,
                kind="measure",
                logical_name=measure.name,
                column=column,
                aggregation=measure.aggregation,
                distinct=(
                    measure.distinct if measure.distinct is not None else False
                ),
                evidence_ids=evidence_ids,
            )
        )

    for predicate in query_spec.filter_specs():
        column, evidence_ids = _resolve_schema_column(
            schema_plan, index, predicate.field_concept
        )
        physical_value = predicate.value
        value_evidence: tuple[str, ...] = ()
        if predicate.operator not in {"is_null", "is_not_null"}:
            logical_values = (
                tuple(predicate.value)
                if predicate.operator in {"in", "not_in", "between"}
                else (predicate.value,)
            )
            physical_values = []
            for logical_value in logical_values:
                bound_value, observed = _resolve_schema_value(
                    index, predicate.field_concept, column, logical_value
                )
                physical_values.append(bound_value)
                value_evidence = tuple(
                    dict.fromkeys((*value_evidence, *observed))
                )
            physical_value = (
                tuple(physical_values)
                if predicate.operator in {"in", "not_in", "between"}
                else physical_values[0]
            )
        values.append(
            PlanBinding(
                slot_id=predicate.slot_id,
                kind="filter",
                logical_name=predicate.field_concept,
                column=column,
                operator=predicate.operator,
                value=physical_value,
                logical_value=predicate.value,
                scope=predicate.scope,
                evidence_ids=tuple(
                    dict.fromkeys((*evidence_ids, *value_evidence))
                ),
            )
        )

    prior_by_slot = {item.slot_id: item for item in values}
    prior_by_name: dict[str, list[PlanBinding]] = {}
    for item in values:
        prior_by_name.setdefault(_logical_key(item.logical_name), []).append(item)
    for order in query_spec.order_specs():
        if order.target in prior_by_slot:
            targets = [prior_by_slot[order.target]]
        else:
            targets = prior_by_name.get(_logical_key(order.target), [])
        if len(targets) > 1:
            raise ValueError("BoundQueryPlan order target is ambiguous")
        if targets:
            target = targets[0]
            values.append(
                PlanBinding(
                    slot_id=order.slot_id,
                    kind="order",
                    logical_name=order.target,
                    column=target.column,
                    aggregation=target.aggregation,
                    distinct=target.distinct,
                    direction=order.direction,
                    evidence_ids=target.evidence_ids,
                )
            )
            continue
        column, evidence_ids = _resolve_schema_column(
            schema_plan, index, order.target
        )
        values.append(
            PlanBinding(
                slot_id=order.slot_id,
                kind="order",
                logical_name=order.target,
                column=column,
                direction=order.direction,
                evidence_ids=evidence_ids,
            )
        )
    return tuple(values)


def _plan_binding_commitment(binding: PlanBinding) -> tuple[Any, ...]:
    return (
        binding.slot_id,
        binding.kind,
        binding.logical_name,
        binding.column,
        binding.aggregation,
        binding.distinct,
        binding.operator,
        _value_key(binding.value),
        _value_key(binding.logical_value),
        binding.direction,
        binding.scope,
        tuple(binding.evidence_ids),
    )


def _validate_bound_query_contract(
    query_spec: QuerySpec,
    schema_plan: SchemaPlan,
    bindings: Sequence[PlanBinding],
) -> None:
    dimensions = query_spec.dimension_specs()
    measures = query_spec.measure_specs()
    aggregate_measures = tuple(
        item for item in measures if item.aggregation != "none"
    )
    if query_spec.expected_shape == "scalar" and dimensions:
        raise ValueError("scalar QuerySpec cannot contain dimensions")
    if query_spec.intent == "lookup" and query_spec.expected_shape != "rows":
        raise ValueError("lookup intent requires rows result shape")
    if query_spec.intent == "ranking" and query_spec.expected_shape not in {
        "rows",
        "grouped_rows",
    }:
        raise ValueError("ranking intent requires rows or grouped_rows result shape")
    if query_spec.intent == "ranking" and not query_spec.order_specs():
        raise ValueError("ranking intent requires at least one order slot")
    if query_spec.intent == "existence" and query_spec.expected_shape != "scalar":
        raise ValueError("existence intent requires scalar result shape")
    if query_spec.intent == "existence" and measures:
        raise ValueError("existence intent cannot contain measures")
    if query_spec.intent in {"count", "aggregate"} and not aggregate_measures:
        raise ValueError("count and aggregate intents require an aggregate measure")
    if query_spec.expected_shape == "rows" and aggregate_measures:
        raise ValueError("rows result shape cannot contain aggregate measures")
    if query_spec.expected_shape == "scalar" and query_spec.intent != "existence" and (
        len(measures) != 1 or len(aggregate_measures) != 1
    ):
        raise ValueError("scalar result shape requires exactly one aggregate measure")
    if query_spec.expected_shape == "grouped_rows" and (
        not dimensions
        or not aggregate_measures
        or len(aggregate_measures) != len(measures)
    ):
        raise ValueError("grouped_rows requires dimensions and only aggregate measures")
    dimension_columns = {
        item.column for item in bindings if item.kind == "dimension"
    }
    if not set(schema_plan.result_grain).issubset(set(schema_plan.columns)):
        raise ValueError("SchemaPlan result grain is outside its planned columns")
    if (
        query_spec.expected_shape == "grouped_rows"
        and dimension_columns != set(schema_plan.result_grain)
    ):
        raise ValueError(
            "BoundQueryPlan grouped dimensions must exactly match SchemaPlan.result_grain"
        )


@dataclass(frozen=True)
class BoundQueryPlan:
    """The only plan contract an SQL generator may consume."""

    query_spec: QuerySpec
    schema_plan: SchemaPlan
    bindings: Sequence[PlanBinding]
    version_pins: Mapping[str, str] = field(default_factory=dict)
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.query_spec, QuerySpec) or not isinstance(self.schema_plan, SchemaPlan):
            raise ValueError("BoundQueryPlan requires QuerySpec and SchemaPlan instances")
        bindings = tuple(self.bindings)
        if any(not isinstance(item, PlanBinding) for item in bindings):
            raise ValueError("BoundQueryPlan bindings must contain PlanBinding values")
        if len({item.slot_id for item in bindings}) != len(bindings):
            raise ValueError("BoundQueryPlan binding slot_ids must be unique")
        expected_entries = [
            *((item.slot_id, "dimension") for item in self.query_spec.dimension_specs()),
            *((item.slot_id, "measure") for item in self.query_spec.measure_specs()),
            *((item.slot_id, "filter") for item in self.query_spec.filter_specs()),
            *((item.slot_id, "order") for item in self.query_spec.order_specs()),
        ]
        if len({slot_id for slot_id, _ in expected_entries}) != len(expected_entries):
            raise ValueError("QuerySpec slot_ids must be globally unique")
        expected_slots = dict(expected_entries)
        actual_slots = {item.slot_id: item.kind for item in bindings}
        if expected_slots != actual_slots:
            raise ValueError("BoundQueryPlan must bind every QuerySpec slot exactly once")
        if any(item.column and item.column not in self.schema_plan.columns for item in bindings):
            raise ValueError("BoundQueryPlan contains a column outside SchemaPlan")
        canonical_bindings = _canonical_plan_bindings(
            self.query_spec, self.schema_plan
        )
        if tuple(map(_plan_binding_commitment, bindings)) != tuple(
            map(_plan_binding_commitment, canonical_bindings)
        ):
            raise ValueError(
                "BoundQueryPlan bindings do not match deterministic QuerySpec/SchemaPlan binding"
            )
        _validate_bound_query_contract(
            self.query_spec, self.schema_plan, bindings
        )
        pins = {
            str(key).strip(): str(value).strip()
            for key, value in self.version_pins.items()
            if str(key).strip() and str(value).strip()
        }
        object.__setattr__(self, "bindings", bindings)
        object.__setattr__(self, "version_pins", pins)
        canonical = {
            "contract": "BoundQueryPlan/v1",
            "query_spec": self.query_spec.as_dict(),
            "schema_plan": self.schema_plan.as_dict(),
            "bindings": [item.as_dict() for item in bindings],
            "version_pins": pins,
        }
        expected = _fingerprint(canonical)
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("BoundQueryPlan fingerprint does not match its contents")
        object.__setattr__(self, "fingerprint", expected)

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "contract": "BoundQueryPlan/v1",
            "query_spec": self.query_spec.as_dict(),
            "schema_plan": self.schema_plan.as_dict(),
            "bindings": [item.as_dict() for item in self.bindings],
            "version_pins": dict(self.version_pins),
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BoundQueryPlan":
        if value.get("contract") != "BoundQueryPlan/v1":
            raise ValueError("unsupported or missing BoundQueryPlan contract version")
        if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("fingerprint") or "")):
            raise ValueError("BoundQueryPlan checkpoint requires a SHA-256 fingerprint")
        query_spec = value.get("query_spec")
        schema_plan = value.get("schema_plan")
        if not isinstance(query_spec, Mapping) or not isinstance(schema_plan, Mapping):
            raise ValueError("BoundQueryPlan checkpoint requires nested query_spec and schema_plan")
        raw_bindings = value.get("bindings") or ()
        if isinstance(raw_bindings, (str, bytes)) or not isinstance(raw_bindings, Sequence):
            raise ValueError("BoundQueryPlan bindings must be a sequence")
        if any(not isinstance(item, Mapping) for item in raw_bindings):
            raise ValueError("BoundQueryPlan binding entries must be mappings")
        raw_pins = value.get("version_pins") or {}
        if not isinstance(raw_pins, Mapping):
            raise ValueError("BoundQueryPlan version_pins must be a mapping")
        return cls(
            query_spec=QuerySpec.from_dict(query_spec),
            schema_plan=SchemaPlan.from_dict(schema_plan),
            bindings=tuple(PlanBinding.from_dict(item) for item in raw_bindings),
            version_pins={str(key): str(pin) for key, pin in raw_pins.items()},
            fingerprint=str(value.get("fingerprint") or ""),
        )


@dataclass(frozen=True)
class ApprovedQueryPlan:
    """An immutable bound plan plus a Lead approval decision."""

    bound_plan: BoundQueryPlan
    approved_by: str
    approval_reason: str = ""
    approval_id: str = ""
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.bound_plan, BoundQueryPlan):
            raise ValueError("ApprovedQueryPlan requires a BoundQueryPlan")
        if self.approved_by != "text2sql-lead":
            raise ValueError("ApprovedQueryPlan must be approved by text2sql-lead")
        canonical = {
            "contract": "ApprovedQueryPlan/v1",
            "bound_plan_fingerprint": self.bound_plan.fingerprint,
            "approved_by": self.approved_by,
            "approval_reason": self.approval_reason,
            "approval_id": self.approval_id,
        }
        expected = _fingerprint(canonical)
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("ApprovedQueryPlan fingerprint does not match its contents")
        object.__setattr__(self, "fingerprint", expected)

    @property
    def query_spec(self) -> QuerySpec:
        return self.bound_plan.query_spec

    @property
    def schema_plan(self) -> SchemaPlan:
        return self.bound_plan.schema_plan

    @property
    def bindings(self) -> Sequence[PlanBinding]:
        return self.bound_plan.bindings

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "contract": "ApprovedQueryPlan/v1",
            "bound_plan": self.bound_plan.as_dict(),
            "approved_by": self.approved_by,
            "approval_reason": self.approval_reason,
            "approval_id": self.approval_id,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ApprovedQueryPlan":
        if value.get("contract") != "ApprovedQueryPlan/v1":
            raise ValueError("unsupported or missing ApprovedQueryPlan contract version")
        if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("fingerprint") or "")):
            raise ValueError("ApprovedQueryPlan checkpoint requires a SHA-256 fingerprint")
        raw_bound_plan = value.get("bound_plan")
        if not isinstance(raw_bound_plan, Mapping):
            raise ValueError("ApprovedQueryPlan checkpoint requires nested bound_plan")
        return cls(
            bound_plan=BoundQueryPlan.from_dict(raw_bound_plan),
            approved_by=str(value.get("approved_by") or ""),
            approval_reason=str(value.get("approval_reason") or ""),
            approval_id=str(value.get("approval_id") or ""),
            fingerprint=str(value.get("fingerprint") or ""),
        )


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
