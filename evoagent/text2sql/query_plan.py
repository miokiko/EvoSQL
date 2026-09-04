"""Deterministic binding and conformance gates for plan-first Text2SQL.

This module is deliberately model-free.  Agents may propose ``QuerySpec`` and
``SchemaPlan`` values, but only this binder can mint a ``BoundQueryPlan`` and it
refuses unresolved or ambiguous logical slots.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional, Sequence, Union

import sqlglot
from sqlglot import exp

from .contracts import (
    ApprovedQueryPlan,
    BindingConflict,
    BoundQueryPlan,
    PlanBinding,
    QueryDimension,
    QueryFilter,
    QueryMeasure,
    QueryOrder,
    QuerySpec,
    SQLCandidate,
    SQLGateResult,
    SchemaBinding,
    SchemaPlan,
)
from .sql_safety import validate_sql


class QueryPlanBindingError(ValueError):
    """Raised when deterministic binding cannot produce a complete plan."""

    def __init__(self, conflicts: Sequence[BindingConflict]) -> None:
        self.conflicts = tuple(conflicts)
        summary = "; ".join("%s:%s" % (item.code, item.slot_id) for item in self.conflicts)
        super().__init__("query plan binding failed: %s" % (summary or "unknown_conflict"))


@dataclass(frozen=True)
class PlanConformanceIssue:
    code: str
    message: str
    slot_id: str = ""
    expected: Any = None
    actual: Any = None

    def as_dict(self) -> Mapping[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlanConformanceResult:
    accepted: bool
    issues: Sequence[PlanConformanceIssue] = field(default_factory=tuple)
    sql_gate: Optional[SQLGateResult] = None

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.issues)

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "accepted": self.accepted,
            "issues": [item.as_dict() for item in self.issues],
            "errors": list(self.errors),
            "sql_gate": self.sql_gate.as_dict() if self.sql_gate is not None else {},
        }


def _logical_key(value: str) -> str:
    return " ".join(str(value).strip().casefold().split())


def _sqlite_identifier_key(value: str) -> str:
    """Match SQLite identifier equality: ASCII case-insensitive, Unicode exact."""

    return "".join(
        chr(ord(character) + 32)
        if "A" <= character <= "Z"
        else character
        for character in str(value)
    )


def _value_key(value: Any) -> str:
    """Type-preserving equality key for JSON-compatible logical values."""

    import json

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _conflict(
    code: str,
    message: str,
    owner: str,
    *,
    slot_id: str = "",
    logical_name: str = "",
    candidates: Sequence[str] = (),
) -> BindingConflict:
    return BindingConflict(code, message, owner, slot_id, logical_name, candidates)


class _SchemaResolver:
    def __init__(self, plan: SchemaPlan) -> None:
        self.plan = plan
        index: dict[str, list[SchemaBinding]] = {}
        for binding in plan.bindings:
            for name in (binding.logical_name, *binding.aliases):
                index.setdefault(_logical_key(name), []).append(binding)
        self.index = index

    def resolve(
        self, logical_name: str, slot_id: str
    ) -> tuple[Optional[str], tuple[str, ...], Optional[BindingConflict]]:
        name = str(logical_name).strip()
        if not name:
            return None, (), _conflict(
                "missing_logical_reference",
                "logical slot does not name a concept to bind",
                "query-planning",
                slot_id=slot_id,
            )

        # Compatibility for an already-qualified legacy reference is exact and
        # snapshot-bounded; no semantic guessing is involved.
        if "." in name:
            if name in self.plan.columns:
                return name, tuple(self.plan.evidence_ids), None
            return None, (), _conflict(
                "missing_schema_binding",
                "qualified logical reference is outside SchemaPlan",
                "schema-grounding",
                slot_id=slot_id,
                logical_name=name,
            )

        matches = self.index.get(_logical_key(name), [])
        columns = tuple(sorted({item.column for item in matches}))
        if len(columns) == 1:
            evidence = tuple(
                dict.fromkeys(
                    evidence_id
                    for item in matches
                    for evidence_id in item.evidence_ids
                    if evidence_id
                )
            )
            return columns[0], evidence, None
        if len(columns) > 1:
            return None, (), _conflict(
                "ambiguous_schema_binding",
                "logical concept maps to more than one physical column",
                "schema-grounding",
                slot_id=slot_id,
                logical_name=name,
                candidates=columns,
            )

        # Legacy physical column names are accepted only when the suffix is
        # unique within the already-approved SchemaPlan.
        suffix_matches = tuple(
            column for column in self.plan.columns if column.split(".", 1)[1] == name
        )
        if len(suffix_matches) == 1:
            return suffix_matches[0], tuple(self.plan.evidence_ids), None
        if len(suffix_matches) > 1:
            return None, (), _conflict(
                "ambiguous_schema_binding",
                "unqualified physical column is ambiguous in SchemaPlan",
                "schema-grounding",
                slot_id=slot_id,
                logical_name=name,
                candidates=suffix_matches,
            )
        return None, (), _conflict(
            "missing_schema_binding",
            "logical concept has no explicit SchemaPlan binding",
            "schema-grounding",
            slot_id=slot_id,
            logical_name=name,
        )

    def resolve_value(
        self,
        logical_name: str,
        column: str,
        logical_value: Any,
        slot_id: str,
    ) -> tuple[Any, tuple[str, ...], Optional[BindingConflict]]:
        matches = [
            item
            for item in self.index.get(_logical_key(logical_name), ())
            if item.column == column
        ]
        wanted = _value_key(logical_value)
        value_matches = [
            value_binding
            for item in matches
            for value_binding in item.value_bindings
            if _value_key(value_binding.logical_value) == wanted
        ]
        physical_values: dict[str, Any] = {
            _value_key(item.physical_value): item.physical_value for item in value_matches
        }
        if len(physical_values) == 1:
            evidence = tuple(
                dict.fromkeys(
                    evidence_id
                    for item in value_matches
                    for evidence_id in item.evidence_ids
                    if evidence_id
                )
            )
            return next(iter(physical_values.values())), evidence, None
        if len(physical_values) > 1:
            return logical_value, (), _conflict(
                "ambiguous_value_binding",
                "logical filter value maps to more than one stored value",
                "schema-grounding",
                slot_id=slot_id,
                logical_name=logical_name,
                candidates=tuple(str(item) for item in physical_values.values()),
            )
        return logical_value, (), _conflict(
            "missing_value_binding",
            "logical filter value has no explicit evidence-backed stored-value binding",
            "schema-grounding",
            slot_id=slot_id,
            logical_name=logical_name,
        )


def _parse_query_spec(value: Union[QuerySpec, Mapping[str, Any]]) -> QuerySpec:
    return value if isinstance(value, QuerySpec) else QuerySpec.from_dict(value)


def _parse_schema_plan(value: Union[SchemaPlan, Mapping[str, Any]]) -> SchemaPlan:
    return value if isinstance(value, SchemaPlan) else SchemaPlan.from_dict(value)


def _has_unmodeled_joined_rows(spec: QuerySpec, plan: SchemaPlan) -> bool:
    """Whether v1 lacks enough semantics to prove a joined row result safe."""

    # QueryPlan/v1 has no relationship cardinality, key uniqueness, or explicit
    # deduplication-preservation contract. A projected (or empty) result_grain
    # therefore cannot prove that a JOIN preserves one output row per entity.
    return spec.expected_shape == "rows" and bool(plan.joins)


def bind_query_plan(
    query_spec: Union[QuerySpec, Mapping[str, Any]],
    schema_plan: Union[SchemaPlan, Mapping[str, Any]],
    *,
    version_pins: Optional[Mapping[str, str]] = None,
) -> BoundQueryPlan:
    """Bind every logical slot exactly once or raise ``QueryPlanBindingError``.

    Resolution is limited to explicit ``SchemaPlan.bindings``, exact qualified
    columns, or a unique legacy column suffix.  Natural-language similarity is
    intentionally absent: ambiguity must go back to the responsible worker.
    """

    try:
        spec = _parse_query_spec(query_spec)
    except (TypeError, ValueError) as exc:
        raise QueryPlanBindingError(
            (_conflict("unsupported_query_contract", str(exc), "query-planning"),)
        ) from exc
    try:
        plan = _parse_schema_plan(schema_plan)
    except (TypeError, ValueError) as exc:
        raise QueryPlanBindingError(
            (_conflict("invalid_schema_binding", str(exc), "schema-grounding"),)
        ) from exc

    conflicts: list[BindingConflict] = []
    try:
        dimensions = spec.dimension_specs()
        measures = spec.measure_specs()
        filters = spec.filter_specs()
        orders = spec.order_specs()
    except (TypeError, ValueError) as exc:
        raise QueryPlanBindingError(
            (_conflict("unsupported_query_contract", str(exc), "query-planning"),)
        ) from exc

    all_slots = [item.slot_id for item in (*dimensions, *measures, *filters, *orders)]
    duplicate_slots = sorted(slot for slot, count in Counter(all_slots).items() if count > 1)
    for slot_id in duplicate_slots:
        conflicts.append(
            _conflict(
                "duplicate_slot_id",
                "logical slot_id must be globally unique",
                "query-planning",
                slot_id=slot_id,
            )
        )

    if spec.expected_shape == "scalar" and dimensions:
        conflicts.append(
            _conflict(
                "unsupported_query_contract",
                "scalar QuerySpec cannot contain dimensions",
                "query-planning",
            )
        )
    aggregate_measures = tuple(
        item for item in measures if item.aggregation != "none"
    )
    if spec.intent == "lookup" and spec.expected_shape != "rows":
        conflicts.append(
            _conflict(
                "unsupported_query_contract",
                "lookup intent requires rows result shape",
                "query-planning",
            )
        )
    if spec.intent == "ranking" and spec.expected_shape not in {
        "rows",
        "grouped_rows",
    }:
        conflicts.append(
            _conflict(
                "unsupported_query_contract",
                "ranking intent requires rows or grouped_rows result shape",
                "query-planning",
            )
        )
    if spec.intent == "ranking" and not orders:
        conflicts.append(
            _conflict(
                "unsupported_query_contract",
                "ranking intent requires at least one order slot",
                "query-planning",
            )
        )
    if spec.intent == "existence" and spec.expected_shape != "scalar":
        conflicts.append(
            _conflict(
                "unsupported_query_contract",
                "existence intent requires scalar result shape",
                "query-planning",
            )
        )
    if spec.intent == "existence" and measures:
        conflicts.append(
            _conflict(
                "unsupported_query_contract",
                "existence intent is represented by EXISTS and cannot contain measures",
                "query-planning",
            )
        )
    if spec.intent in {"count", "aggregate"} and not aggregate_measures:
        conflicts.append(
            _conflict(
                "unsupported_query_contract",
                "%s intent requires an aggregate measure" % spec.intent,
                "query-planning",
            )
        )
    if spec.expected_shape == "rows" and aggregate_measures:
        conflicts.append(
            _conflict(
                "unsupported_query_contract",
                "rows result shape cannot contain aggregate measures",
                "query-planning",
            )
        )
    if spec.expected_shape == "scalar" and spec.intent != "existence" and (
        len(measures) != 1 or len(aggregate_measures) != 1
    ):
        conflicts.append(
            _conflict(
                "unsupported_query_contract",
                "scalar result shape requires exactly one aggregate measure",
                "query-planning",
            )
        )
    if spec.expected_shape == "grouped_rows" and (
        not dimensions or not aggregate_measures or len(aggregate_measures) != len(measures)
    ):
        conflicts.append(
            _conflict(
                "unsupported_query_contract",
                "grouped_rows requires dimensions and only aggregate measures",
                "query-planning",
            )
        )

    resolver = _SchemaResolver(plan)
    bindings: list[PlanBinding] = []

    def resolved(
        logical_name: str, slot_id: str
    ) -> tuple[Optional[str], tuple[str, ...]]:
        column, evidence_ids, issue = resolver.resolve(logical_name, slot_id)
        if issue is not None:
            conflicts.append(issue)
        return column, evidence_ids

    for dimension in dimensions:
        column, evidence_ids = resolved(dimension.concept, dimension.slot_id)
        if column:
            bindings.append(
                PlanBinding(
                    slot_id=dimension.slot_id,
                    kind="dimension",
                    logical_name=dimension.concept,
                    column=column,
                    evidence_ids=evidence_ids,
                )
            )

    for measure in measures:
        column: Optional[str] = None
        evidence_ids: tuple[str, ...] = ()
        if measure.count_all:
            column = ""
        elif measure.field_concept:
            column, evidence_ids = resolved(measure.field_concept, measure.slot_id)
        else:
            conflicts.append(
                _conflict(
                    "missing_logical_reference",
                    "measure must name field_concept or explicitly set count_all",
                    "query-planning",
                    slot_id=measure.slot_id,
                    logical_name=measure.name,
                )
            )
        if measure.aggregation in {"count", "sum", "avg"} and measure.distinct is None:
            conflicts.append(
                _conflict(
                    "unsupported_query_contract",
                    "measure must explicitly state distinct true or false",
                    "query-planning",
                    slot_id=measure.slot_id,
                    logical_name=measure.name,
                )
            )
        if column is not None and not any(item.slot_id == measure.slot_id for item in conflicts):
            bindings.append(
                PlanBinding(
                    slot_id=measure.slot_id,
                    kind="measure",
                    logical_name=measure.name,
                    column=column,
                    aggregation=measure.aggregation,
                    distinct=measure.distinct if measure.distinct is not None else False,
                    evidence_ids=evidence_ids,
                )
            )

    for predicate in filters:
        column, evidence_ids = resolved(predicate.field_concept, predicate.slot_id)
        if column:
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
                    bound_value, observed, issue = resolver.resolve_value(
                        predicate.field_concept,
                        column,
                        logical_value,
                        predicate.slot_id,
                    )
                    physical_values.append(bound_value)
                    value_evidence = tuple(dict.fromkeys((*value_evidence, *observed)))
                    if issue is not None:
                        conflicts.append(issue)
                physical_value = (
                    tuple(physical_values)
                    if predicate.operator in {"in", "not_in", "between"}
                    else physical_values[0]
                )
            if any(item.slot_id == predicate.slot_id for item in conflicts):
                continue
            bindings.append(
                PlanBinding(
                    slot_id=predicate.slot_id,
                    kind="filter",
                    logical_name=predicate.field_concept,
                    column=column,
                    operator=predicate.operator,
                    value=physical_value,
                    logical_value=predicate.value,
                    scope=predicate.scope,
                    evidence_ids=tuple(dict.fromkeys((*evidence_ids, *value_evidence))),
                )
            )

    prior_by_slot = {item.slot_id: item for item in bindings}
    prior_by_name: dict[str, list[PlanBinding]] = {}
    for item in bindings:
        prior_by_name.setdefault(_logical_key(item.logical_name), []).append(item)
    for order in orders:
        targets = []
        if order.target in prior_by_slot:
            targets = [prior_by_slot[order.target]]
        else:
            targets = prior_by_name.get(_logical_key(order.target), [])
        if len(targets) > 1:
            conflicts.append(
                _conflict(
                    "ambiguous_schema_binding",
                    "order target matches more than one logical output slot",
                    "query-planning",
                    slot_id=order.slot_id,
                    logical_name=order.target,
                    candidates=tuple(item.slot_id for item in targets),
                )
            )
            continue
        if len(targets) == 1:
            target = targets[0]
            bindings.append(
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
        column, evidence_ids = resolved(order.target, order.slot_id)
        if column:
            bindings.append(
                PlanBinding(
                    slot_id=order.slot_id,
                    kind="order",
                    logical_name=order.target,
                    column=column,
                    direction=order.direction,
                    evidence_ids=evidence_ids,
                )
            )

    dimension_columns = tuple(
        item.column for item in bindings if item.kind == "dimension" and item.column
    )
    if (
        spec.expected_shape == "grouped_rows"
        and set(dimension_columns) != set(plan.result_grain)
    ):
        conflicts.append(
            _conflict(
                "result_grain_mismatch",
                "grouped dimensions must exactly match SchemaPlan.result_grain",
                "schema-grounding",
                candidates=plan.result_grain,
            )
        )
    if _has_unmodeled_joined_rows(spec, plan):
        conflicts.append(
            _conflict(
                "result_grain_mismatch",
                "QueryPlan/v1 cannot prove row cardinality, uniqueness, or "
                "deduplication preservation across joins",
                "schema-grounding",
                candidates=plan.result_grain,
            )
        )

    planned_columns = set(plan.columns)
    if not set(plan.result_grain).issubset(planned_columns):
        conflicts.append(
            _conflict(
                "invalid_schema_binding",
                "result grain is outside SchemaPlan.columns",
                "schema-grounding",
                candidates=plan.result_grain,
            )
        )
    for join in plan.joins:
        if join.left not in planned_columns or join.right not in planned_columns:
            conflicts.append(
                _conflict(
                    "invalid_schema_binding",
                    "join endpoint is outside SchemaPlan.columns",
                    "schema-grounding",
                    candidates=(join.left, join.right),
                )
            )

    raw_pins = version_pins or {}
    pins: dict[str, str] = {}
    for key, value in raw_pins.items():
        normalized_key = str(key).strip()
        normalized_value = str(value).strip()
        if not normalized_key or not normalized_value:
            conflicts.append(
                _conflict(
                    "version_pin_missing",
                    "version pin keys and values must be non-empty",
                    "text2sql-harness",
                    logical_name=normalized_key,
                )
            )
        else:
            pins[normalized_key] = normalized_value

    if conflicts:
        raise QueryPlanBindingError(tuple(conflicts))
    return BoundQueryPlan(spec, plan, tuple(bindings), pins)


def approve_query_plan(
    bound_plan: BoundQueryPlan,
    *,
    approved_by: str = "text2sql-lead",
    approval_reason: str = "",
    approval_id: str = "",
) -> ApprovedQueryPlan:
    """Mint approval metadata without exposing mutable plan fields to Lead."""

    return ApprovedQueryPlan(
        bound_plan=bound_plan,
        approved_by=approved_by,
        approval_reason=approval_reason,
        approval_id=approval_id,
    )


def _bound_plan(value: Union[BoundQueryPlan, ApprovedQueryPlan]) -> BoundQueryPlan:
    if isinstance(value, ApprovedQueryPlan):
        return value.bound_plan
    if isinstance(value, BoundQueryPlan):
        return value
    raise TypeError("plan must be BoundQueryPlan or ApprovedQueryPlan")


def _literal_signature(value: Any) -> tuple[str, str]:
    if value is None:
        return ("null", "")
    if isinstance(value, bool):
        return ("bool", "true" if value else "false")
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        try:
            number = Decimal(str(value))
            normalized = format(number.normalize(), "f")
            return ("number", "0" if normalized in {"-0", "-0.0"} else normalized)
        except InvalidOperation:
            pass
    return ("string", str(value))


def _sql_literal_signature(node: exp.Expression) -> Optional[tuple[str, str]]:
    if isinstance(node, exp.Null):
        return ("null", "")
    if isinstance(node, exp.Boolean):
        return ("bool", "true" if bool(node.this) else "false")
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        inner = _sql_literal_signature(node.this)
        if inner and inner[0] == "number":
            return ("number", "-" + inner[1])
    if not isinstance(node, exp.Literal):
        return None
    if node.is_string:
        return ("string", str(node.this))
    try:
        number = Decimal(str(node.this))
    except InvalidOperation:
        return ("string", str(node.this))
    normalized = format(number.normalize(), "f")
    return ("number", "0" if normalized in {"-0", "-0.0"} else normalized)


class _SQLResolver:
    def __init__(self, tree: exp.Expression, plan: SchemaPlan) -> None:
        table_nodes = tuple(tree.find_all(exp.Table))
        self.aliases = {
            table.alias_or_name: table.name for table in table_nodes if table.alias_or_name
        }
        self.columns = tuple(plan.columns)

    def column(self, node: exp.Expression) -> Optional[str]:
        if not isinstance(node, exp.Column) or isinstance(node.this, exp.Star):
            return None
        if node.table:
            table = self.aliases.get(node.table, node.table)
            return "%s.%s" % (table, node.name)
        matches = tuple(value for value in self.columns if value.split(".", 1)[1] == node.name)
        return matches[0] if len(matches) == 1 else None


def _aggregate_signature(
    node: exp.Expression, resolver: _SQLResolver
) -> Optional[tuple[str, str, bool]]:
    if not isinstance(node, exp.AggFunc):
        return None
    name = node.key.lower()
    target = node.this
    distinct = isinstance(target, exp.Distinct)
    if distinct:
        expressions = tuple(target.expressions)
        if len(expressions) != 1:
            return None
        target = expressions[0]
    if isinstance(target, exp.Star):
        column = "*"
    else:
        column = resolver.column(target)
        if not column:
            return None
    return (name, column, distinct)


def _expected_aggregate(binding: PlanBinding) -> tuple[str, str, bool]:
    return (binding.aggregation, binding.column or "*", bool(binding.distinct))


def _join_signature(left: str, right: str, join_type: str) -> tuple[str, str, str]:
    if join_type == "inner":
        left, right = sorted((left, right))
    return (join_type, left, right)


def _join_signatures(
    tree: exp.Select, resolver: _SQLResolver
) -> tuple[Counter[tuple[str, str, str]], tuple[PlanConformanceIssue, ...]]:
    """Inspect each physical JOIN as one contract-representable edge.

    QuerySpec/v1 has no alias or composite-join contract.  Consequently a JOIN
    must introduce one previously unseen physical table using exactly one
    cross-table equality.  Anything richer is rejected instead of partially
    extracting a convenient equality and ignoring the rest of the ON clause.
    """

    signatures: Counter[tuple[str, str, str]] = Counter()
    issues: list[PlanConformanceIssue] = []
    from_node = tree.args.get("from_")
    base = from_node.this if isinstance(from_node, exp.From) else None
    if not isinstance(base, exp.Table):
        return signatures, (
            _issue(
                "unsupported_join",
                "FROM must start with one physical table for join conformance",
            ),
        )
    seen_tables = {base.name}
    for join in tuple(tree.args.get("joins") or ()):
        target = join.this
        target_table = target.name if isinstance(target, exp.Table) else ""
        side = str(join.args.get("side") or "").strip().lower()
        kind = str(join.args.get("kind") or "").strip().lower()
        method = str(join.args.get("method") or "").strip().lower()
        if method or not target_table:
            issues.append(
                _issue(
                    "unsupported_join",
                    "NATURAL, derived or unresolved JOIN targets are not representable",
                    actual=join.sql(dialect="sqlite"),
                )
            )
            if target_table:
                seen_tables.add(target_table)
            continue
        if not side and kind in {"", "inner"}:
            join_type = "inner"
        elif side == "left" and kind in {"", "outer"}:
            join_type = "left"
        else:
            issues.append(
                _issue(
                    "unsupported_join",
                    "only explicit INNER and LEFT equality joins are allowed",
                    actual={"side": side, "kind": kind, "sql": join.sql(dialect="sqlite")},
                )
            )
            seen_tables.add(target_table)
            continue
        if target_table in seen_tables:
            issues.append(
                _issue(
                    "unsupported_join",
                    "self joins and repeated physical-table aliases are not representable",
                    actual=target_table,
                )
            )
            continue
        on = join.args.get("on")
        atoms = _flatten_and(on) if isinstance(on, exp.Expression) else None
        if atoms is None or len(atoms) != 1 or not isinstance(atoms[0], exp.EQ):
            issues.append(
                _issue(
                    "unsupported_join",
                    "JOIN ON must contain exactly one direct column equality",
                    actual=(
                        on.sql(dialect="sqlite")
                        if isinstance(on, exp.Expression)
                        else ""
                    ),
                )
            )
            seen_tables.add(target_table)
            continue
        equality = atoms[0]
        left = resolver.column(equality.this)
        right = resolver.column(equality.expression)
        if not left or not right:
            issues.append(
                _issue(
                    "unsupported_join",
                    "JOIN columns could not be resolved uniquely",
                    actual=equality.sql(dialect="sqlite"),
                )
            )
            seen_tables.add(target_table)
            continue
        left_table = left.split(".", 1)[0]
        right_table = right.split(".", 1)[0]
        if left_table == right_table:
            issues.append(
                _issue(
                    "unsupported_join",
                    "JOIN equality must connect two different physical tables",
                    actual=(left, right),
                )
            )
            seen_tables.add(target_table)
            continue
        if left_table == target_table and right_table in seen_tables:
            prior, introduced = right, left
        elif right_table == target_table and left_table in seen_tables:
            prior, introduced = left, right
        else:
            issues.append(
                _issue(
                    "unsupported_join",
                    "JOIN equality must connect the introduced table to prior scope",
                    actual=(left, right),
                )
            )
            seen_tables.add(target_table)
            continue
        signatures[_join_signature(prior, introduced, join_type)] += 1
        seen_tables.add(target_table)
    return signatures, tuple(issues)


def _flatten_and(node: exp.Expression) -> Optional[list[exp.Expression]]:
    if isinstance(node, exp.And):
        left = _flatten_and(node.this)
        right = _flatten_and(node.expression)
        return None if left is None or right is None else left + right
    if isinstance(node, exp.Or):
        return None
    return [node]


_COMPARISON_OPERATORS = {
    exp.EQ: "eq",
    exp.NEQ: "neq",
    exp.GT: "gt",
    exp.GTE: "gte",
    exp.LT: "lt",
    exp.LTE: "lte",
}
_REVERSED_OPERATORS = {"gt": "lt", "gte": "lte", "lt": "gt", "lte": "gte"}


def _predicate_signature(
    node: exp.Expression, resolver: _SQLResolver
) -> Optional[tuple[str, str, Any]]:
    negated = isinstance(node, exp.Not)
    if negated:
        node = node.this
    if isinstance(node, exp.Like):
        # LIKE is not a reversible comparison: only ``column LIKE literal``
        # has the filter semantics represented by QuerySpec/v1. sqlglot stores
        # the canonical ``NOT LIKE`` spelling on the Like node itself.
        column = resolver.column(node.this)
        if not column or resolver.column(node.expression):
            return None
        value = _sql_literal_signature(node.expression)
        if value is None:
            return None
        is_negated = negated ^ bool(node.args.get("negate"))
        return (column, "not_like" if is_negated else "like", value)
    for node_type, operator in _COMPARISON_OPERATORS.items():
        if isinstance(node, node_type):
            left_column = resolver.column(node.this)
            right_column = resolver.column(node.expression)
            if left_column and not right_column:
                value = _sql_literal_signature(node.expression)
                if value is None:
                    return None
                if negated:
                    operator = {"eq": "neq", "like": "not_like"}.get(operator, "")
                return (left_column, operator, value) if operator else None
            if right_column and not left_column:
                value = _sql_literal_signature(node.this)
                if value is None:
                    return None
                operator = _REVERSED_OPERATORS.get(operator, operator)
                if negated:
                    operator = {"eq": "neq", "like": "not_like"}.get(operator, "")
                return (right_column, operator, value) if operator else None
            return None
    if isinstance(node, exp.In):
        column = resolver.column(node.this)
        values = tuple(_sql_literal_signature(item) for item in node.expressions)
        if not column or any(item is None for item in values):
            return None
        return (
            column,
            "not_in" if negated else "in",
            tuple(sorted(values, key=repr)),
        )
    if isinstance(node, exp.Between):
        column = resolver.column(node.this)
        low = _sql_literal_signature(node.args.get("low"))
        high = _sql_literal_signature(node.args.get("high"))
        if not column or low is None or high is None or negated:
            return None
        return (column, "between", (low, high))
    if isinstance(node, exp.Is):
        column = resolver.column(node.this)
        if not column or not isinstance(node.expression, exp.Null):
            return None
        return (column, "is_not_null" if negated else "is_null", ("null", ""))
    return None


def _expected_predicate(binding: PlanBinding) -> tuple[str, str, Any]:
    if binding.operator in {"in", "not_in", "between"}:
        value = tuple(_literal_signature(item) for item in binding.value)
        if binding.operator in {"in", "not_in"}:
            value = tuple(sorted(value, key=repr))
    elif binding.operator in {"is_null", "is_not_null"}:
        value = ("null", "")
    else:
        value = _literal_signature(binding.value)
    return (binding.column, binding.operator, value)


def _projection_aliases(
    tree: exp.Select, resolver: _SQLResolver
) -> Mapping[str, tuple[str, Any]]:
    aliases: dict[str, tuple[str, Any]] = {}
    for projection in tree.expressions:
        alias = projection.alias
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if not alias:
            continue
        aggregate = _aggregate_signature(inner, resolver)
        column = resolver.column(inner)
        if aggregate is not None:
            aliases[_sqlite_identifier_key(alias)] = ("aggregate", aggregate)
        elif column:
            aliases[_sqlite_identifier_key(alias)] = ("column", column)
    return aliases


def _order_signature(
    expression: exp.Expression,
    resolver: _SQLResolver,
    aliases: Mapping[str, tuple[str, Any]],
) -> Optional[tuple[str, Any]]:
    if isinstance(expression, exp.Column) and not expression.table:
        alias = aliases.get(_sqlite_identifier_key(expression.name))
        if alias is not None:
            return alias
    aggregate = _aggregate_signature(expression, resolver)
    if aggregate is not None:
        return ("aggregate", aggregate)
    column = resolver.column(expression)
    if column:
        return ("column", column)
    return None


def _issue(
    code: str,
    message: str,
    *,
    slot_id: str = "",
    expected: Any = None,
    actual: Any = None,
) -> PlanConformanceIssue:
    return PlanConformanceIssue(code, message, slot_id, expected, actual)


def _exists_scalar_select(tree: exp.Select) -> Optional[exp.Select]:
    """Return the inspected SELECT for a strict EXISTS or CASE(EXISTS)->1/0 scalar."""

    # The outer wrapper must produce exactly one scalar row independently of
    # database contents. Any outer FROM/filter/group/order can change its
    # cardinality or suppress the row and is therefore not equivalent.
    if any(
        tree.args.get(name) is not None
        for name in ("from_", "where", "group", "having", "order", "offset", "qualify")
    ) or tree.args.get("joins"):
        return None
    if len(tree.expressions) != 1:
        return None
    projection = tree.expressions[0]
    if isinstance(projection, exp.Alias):
        projection = projection.this
    inner: Optional[exp.Select] = None
    if isinstance(projection, exp.Exists) and isinstance(projection.this, exp.Select):
        inner = projection.this
    elif isinstance(projection, exp.Case) and projection.this is None:
        branches = tuple(projection.args.get("ifs") or ())
        default = projection.args.get("default")
        if len(branches) != 1 or not isinstance(default, exp.Literal):
            return None
        branch = branches[0]
        condition = branch.this
        true_value = branch.args.get("true")
        if not isinstance(condition, exp.Exists) or not isinstance(condition.this, exp.Select):
            return None
        if not isinstance(true_value, exp.Literal):
            return None
        if _sql_literal_signature(true_value) != ("number", "1"):
            return None
        if _sql_literal_signature(default) != ("number", "0"):
            return None
        inner = condition.this
    if inner is None:
        return None

    # EXISTS(SELECT aggregate ...) is not a row-existence test: a scalar
    # aggregate emits one row even for an empty input. Keep v1 deliberately
    # narrow and require the canonical side-effect-free SELECT 1 projection.
    if len(inner.expressions) != 1:
        return None
    inner_projection = inner.expressions[0]
    if isinstance(inner_projection, exp.Alias):
        inner_projection = inner_projection.this
    if _sql_literal_signature(inner_projection) != ("number", "1"):
        return None
    if any(
        inner.args.get(name) is not None
        for name in ("distinct", "group", "having", "order", "limit", "offset", "qualify")
    ):
        return None
    if (
        inner.find(exp.AggFunc) is not None
        or inner.find(exp.Window) is not None
        or inner.find(exp.Exists) is not None
        or inner.find(exp.Subquery) is not None
    ):
        return None
    return inner


def check_plan_conformance(
    sql: str,
    plan: Union[BoundQueryPlan, ApprovedQueryPlan],
    snapshot: Mapping[str, Any],
) -> PlanConformanceResult:
    """Fail closed unless SQL is statically equivalent to the bound plan subset.

    The gate intentionally rejects CTEs, set operations, correlated subqueries,
    OR predicates and expression-based grouping/order when equivalence cannot be
    established safely.  Those cases require a richer future plan contract.
    """

    bound = _bound_plan(plan)
    gate = validate_sql(sql, snapshot)
    if not gate.accepted:
        issues = tuple(
            _issue("sql_safety:%s" % error, "SQL safety gate rejected candidate")
            for error in gate.errors
        )
        return PlanConformanceResult(False, issues, gate)

    try:
        tree = sqlglot.parse_one(gate.normalized_sql, read="sqlite")
    except sqlglot.errors.ParseError:
        return PlanConformanceResult(
            False, (_issue("sql_safety:parse_error", "normalized SQL could not be parsed"),), gate
        )
    issues: list[PlanConformanceIssue] = []
    dimension_columns = tuple(
        item.column
        for item in bound.bindings
        if item.kind == "dimension" and item.column
    )
    if _has_unmodeled_joined_rows(bound.query_spec, bound.schema_plan):
        issues.append(
            _issue(
                "result_grain_mismatch",
                "QueryPlan/v1 cannot prove row cardinality, uniqueness, or "
                "deduplication preservation across joins",
                expected=tuple(bound.schema_plan.result_grain),
                actual=dimension_columns,
            )
        )
    if not isinstance(tree, exp.Select) or tree.find(exp.CTE) is not None:
        issues.append(
            _issue(
                "unsupported_query_shape",
                "conformance cannot prove CTE, set-operation or non-SELECT equivalence",
            )
        )
        return PlanConformanceResult(False, tuple(issues), gate)
    if tree.args.get("distinct") is not None:
        issues.append(
            _issue(
                "unexpected_row_distinct",
                "QuerySpec/v1 has no row-level DISTINCT contract",
            )
        )
    output_aliases = [
        _sqlite_identifier_key(projection.alias)
        for projection in tree.expressions
        if projection.alias
    ]
    duplicate_aliases = sorted(
        alias for alias, count in Counter(output_aliases).items() if count > 1
    )
    if duplicate_aliases:
        issues.append(
            _issue(
                "duplicate_output_alias",
                "duplicate output aliases make ORDER BY binding ambiguous",
                actual=tuple(duplicate_aliases),
            )
        )
    order_alias_node_ids: set[int] = set()
    order_node = tree.args.get("order")
    if isinstance(order_node, exp.Order):
        alias_keys = set(output_aliases)
        for ordered in order_node.expressions:
            expression = ordered.this
            if (
                isinstance(expression, exp.Column)
                and not expression.table
                and _sqlite_identifier_key(expression.name) in alias_keys
            ):
                order_alias_node_ids.add(id(expression))
    # EXISTS is the only supported nested query shape because its scalar shape
    # is explicit and predicates remain inspectable.
    subqueries = tuple(tree.find_all(exp.Subquery))
    if subqueries:
        issues.append(
            _issue(
                "unsupported_query_shape",
                "conformance cannot prove nested subquery equivalence",
            )
        )
        return PlanConformanceResult(False, tuple(issues), gate)

    resolver = _SQLResolver(tree, bound.schema_plan)
    exists_select = (
        _exists_scalar_select(tree) if bound.query_spec.intent == "existence" else None
    )
    semantic_tree = exists_select or tree
    expected_tables = set(bound.schema_plan.tables)
    actual_tables = set(gate.tables)
    for table in sorted(actual_tables - expected_tables):
        issues.append(_issue("unexpected_table", "SQL uses a table outside the bound plan", actual=table))
    for table in sorted(expected_tables - actual_tables):
        issues.append(_issue("missing_table", "SQL omits a table required by the bound plan", expected=table))

    planned_columns = set(bound.schema_plan.columns)
    actual_columns: set[str] = set()
    unresolved_columns: list[str] = []
    for column_node in tree.find_all(exp.Column):
        # Output aliases in ORDER BY are handled below, not schema columns.
        if id(column_node) in order_alias_node_ids:
            continue
        column = resolver.column(column_node)
        if column:
            actual_columns.add(column)
        elif not isinstance(column_node.this, exp.Star):
            unresolved_columns.append(column_node.sql(dialect="sqlite"))
    for column in sorted(actual_columns - planned_columns):
        issues.append(_issue("unexpected_column", "SQL uses a column outside the bound plan", actual=column))
    for column in sorted(set(unresolved_columns)):
        issues.append(
            _issue(
                "unresolved_sql_column",
                "unqualified or derived SQL column cannot be resolved uniquely",
                actual=column,
            )
        )

    for projection in tree.expressions:
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(inner, exp.Star):
            issues.append(
                _issue(
                    "result_shape_mismatch",
                    "wildcard projection cannot conform to an explicit result shape",
                )
            )

    expected_join_signatures = Counter(
        _join_signature(join.left, join.right, join.join_type)
        for join in bound.schema_plan.joins
    )
    actual_join_signatures, join_issues = _join_signatures(
        semantic_tree, resolver
    )
    issues.extend(join_issues)
    for signature, count in sorted(
        (expected_join_signatures - actual_join_signatures).items()
    ):
        join_type, left, right = signature
        issues.append(
            _issue(
                "missing_join",
                "SQL omits a required bound join or uses the wrong join type",
                expected={
                    "left": left,
                    "right": right,
                    "type": join_type,
                    "count": count,
                },
            )
        )
    for signature, count in sorted(
        (actual_join_signatures - expected_join_signatures).items()
    ):
        join_type, left, right = signature
        issues.append(
            _issue(
                "unexpected_join",
                "SQL adds an unapproved cross-table join or join type",
                actual={
                    "left": left,
                    "right": right,
                    "type": join_type,
                    "count": count,
                },
            )
        )

    measure_bindings = tuple(item for item in bound.bindings if item.kind == "measure")
    expected_aggregates = Counter(
        _expected_aggregate(item) for item in measure_bindings if item.aggregation != "none"
    )
    actual_aggregate_values: list[tuple[str, str, bool]] = []
    unsupported_aggregates: list[str] = []
    for projection in tree.expressions:
        aggregate = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(aggregate, exp.AggFunc):
            continue
        signature = _aggregate_signature(aggregate, resolver)
        if signature is None:
            unsupported_aggregates.append(aggregate.sql(dialect="sqlite"))
        else:
            actual_aggregate_values.append(signature)
    actual_aggregates = Counter(actual_aggregate_values)
    if unsupported_aggregates:
        issues.append(
            _issue(
                "aggregation_mismatch",
                "aggregate expression cannot be matched to a bound measure",
                actual=tuple(unsupported_aggregates),
            )
        )

    expected_projections: list[tuple[Any, ...]] = []
    for item in bound.bindings:
        if item.kind == "dimension":
            expected_projections.append(("column", item.column))
    for item in bound.bindings:
        if item.kind != "measure":
            continue
        if item.aggregation == "none":
            expected_projections.append(("column", item.column))
        else:
            expected_projections.append(("aggregate", _expected_aggregate(item)))
    actual_projections: list[tuple[Any, ...]] = []
    unsupported_projections: list[str] = []
    if bound.query_spec.intent == "existence" and exists_select is not None:
        expected_projections.append(("exists",))
        actual_projections.append(("exists",))
    else:
        for projection in tree.expressions:
            inner = projection.this if isinstance(projection, exp.Alias) else projection
            aggregate = _aggregate_signature(inner, resolver)
            column = resolver.column(inner)
            if aggregate is not None:
                actual_projections.append(("aggregate", aggregate))
            elif column:
                actual_projections.append(("column", column))
            else:
                unsupported_projections.append(inner.sql(dialect="sqlite"))
    if unsupported_projections or expected_projections != actual_projections:
        issues.append(
            _issue(
                "result_shape_mismatch",
                "SELECT projections differ from bound dimensions and measures",
                expected=tuple(expected_projections),
                actual={
                    "projections": tuple(actual_projections),
                    "unsupported": tuple(unsupported_projections),
                },
            )
        )
    if expected_aggregates != actual_aggregates:
        expected_without_distinct = Counter((name, column) for name, column, _ in expected_aggregates.elements())
        actual_without_distinct = Counter((name, column) for name, column, _ in actual_aggregates.elements())
        code = "distinct_mismatch" if expected_without_distinct == actual_without_distinct else "aggregation_mismatch"
        issues.append(
            _issue(
                code,
                "SQL aggregate signatures differ from bound measures",
                expected=tuple(sorted(expected_aggregates.elements())),
                actual=tuple(sorted(actual_aggregates.elements())),
            )
        )

    dimension_columns = tuple(
        item.column for item in bound.bindings if item.kind == "dimension"
    )
    group = semantic_tree.args.get("group")
    group_columns: tuple[str, ...] = ()
    if group is not None:
        values: list[str] = []
        for expression in group.expressions:
            column = resolver.column(expression)
            if not column:
                issues.append(
                    _issue(
                        "group_by_mismatch",
                        "GROUP BY expression cannot be resolved to a bound dimension",
                        actual=expression.sql(dialect="sqlite"),
                    )
                )
            else:
                values.append(column)
        group_columns = tuple(values)
    expected_group = set(dimension_columns) if expected_aggregates else set()
    if set(group_columns) != expected_group:
        issues.append(
            _issue(
                "group_by_mismatch",
                "GROUP BY columns differ from bound result dimensions",
                expected=tuple(sorted(expected_group)),
                actual=tuple(sorted(group_columns)),
            )
        )

    expected_predicates: dict[str, Counter[Any]] = {"where": Counter(), "having": Counter()}
    for item in bound.bindings:
        if item.kind == "filter":
            expected_predicates[item.scope][_expected_predicate(item)] += 1
    actual_predicates: dict[str, Counter[Any]] = {"where": Counter(), "having": Counter()}
    for scope in ("where", "having"):
        wrapper = semantic_tree.args.get(scope)
        if wrapper is None:
            continue
        atoms = _flatten_and(wrapper.this)
        if atoms is None:
            issues.append(
                _issue(
                    "unsupported_filter_expression",
                    "OR predicates are not representable by QuerySpec/v1",
                    actual=wrapper.this.sql(dialect="sqlite"),
                )
            )
            continue
        for atom in atoms:
            signature = _predicate_signature(atom, resolver)
            if signature is None:
                issues.append(
                    _issue(
                        "unsupported_filter_expression",
                        "predicate cannot be matched deterministically",
                        actual=atom.sql(dialect="sqlite"),
                    )
                )
            else:
                actual_predicates[scope][signature] += 1
    for scope in ("where", "having"):
        if expected_predicates[scope] != actual_predicates[scope]:
            issues.append(
                _issue(
                    "filter_mismatch",
                    "%s predicates differ from bound filters" % scope.upper(),
                    expected=tuple(sorted(expected_predicates[scope].elements(), key=str)),
                    actual=tuple(sorted(actual_predicates[scope].elements(), key=str)),
                )
            )

    aliases = _projection_aliases(tree, resolver)
    expected_orders: list[tuple[tuple[str, Any], str]] = []
    for item in bound.bindings:
        if item.kind != "order":
            continue
        if item.aggregation not in {"", "none"}:
            target: tuple[str, Any] = ("aggregate", _expected_aggregate(item))
        else:
            target = ("column", item.column)
        expected_orders.append((target, item.direction))
    actual_orders: list[tuple[tuple[str, Any], str]] = []
    order = semantic_tree.args.get("order")
    if order is not None:
        for ordered in order.expressions:
            signature = _order_signature(ordered.this, resolver, aliases)
            if signature is None:
                issues.append(
                    _issue(
                        "unsupported_order_expression",
                        "ORDER BY expression cannot be matched deterministically",
                        actual=ordered.this.sql(dialect="sqlite"),
                    )
                )
            else:
                descending = bool(ordered.args.get("desc"))
                actual_orders.append((signature, "desc" if descending else "asc"))
                nulls_first = ordered.args.get("nulls_first")
                if nulls_first is not None and bool(nulls_first) != (not descending):
                    issues.append(
                        _issue(
                            "null_ordering_mismatch",
                            "QuerySpec/v1 supports only SQLite's default NULL ordering",
                            actual=ordered.sql(dialect="sqlite"),
                        )
                    )
    if expected_orders != actual_orders:
        issues.append(
            _issue(
                "order_by_mismatch",
                "ORDER BY sequence differs from bound order slots",
                expected=tuple(expected_orders),
                actual=tuple(actual_orders),
            )
        )

    offsets = tuple(tree.find_all(exp.Offset))
    if offsets:
        issues.append(
            _issue(
                "offset_not_allowed",
                "QuerySpec/v1 has no OFFSET contract",
                actual=tuple(item.sql(dialect="sqlite") for item in offsets),
            )
        )

    def literal_limit(node: Optional[exp.Expression]) -> Optional[int]:
        if not isinstance(node, exp.Limit):
            return None
        expression = node.expression
        if not isinstance(expression, exp.Literal) or expression.is_string:
            return None
        try:
            return int(str(expression.this))
        except ValueError:
            return None

    limit_nodes = tuple(tree.find_all(exp.Limit))
    if bound.query_spec.expected_shape == "scalar":
        for limit_node in limit_nodes:
            actual_limit = literal_limit(limit_node)
            if actual_limit != 1:
                issues.append(
                    _issue(
                        "limit_mismatch",
                        "scalar SQL may omit LIMIT or use only positive LIMIT 1",
                        expected=1,
                        actual=(
                            actual_limit
                            if actual_limit is not None
                            else limit_node.sql(dialect="sqlite")
                        ),
                    )
                )
    else:
        top_limit = tree.args.get("limit")
        actual_limit = literal_limit(top_limit)
        if actual_limit != bound.query_spec.limit:
            issues.append(
                _issue(
                    "limit_mismatch",
                    "row-producing SQL must use the bound positive LIMIT exactly",
                    expected=bound.query_spec.limit,
                    actual=actual_limit,
                )
            )

    projection_count = len(tree.expressions)
    if bound.query_spec.expected_shape == "scalar":
        scalar_expression = tree.expressions[0] if projection_count == 1 else None
        if isinstance(scalar_expression, exp.Alias):
            scalar_expression = scalar_expression.this
        scalar_supported = isinstance(scalar_expression, (exp.AggFunc, exp.Exists))
        if bound.query_spec.intent == "existence" and exists_select is not None:
            scalar_supported = True
        if projection_count != 1 or not scalar_supported:
            issues.append(
                _issue(
                    "result_shape_mismatch",
                    "scalar plan requires exactly one aggregate or EXISTS projection",
                    expected="scalar",
                    actual=projection_count,
                )
            )
    elif bound.query_spec.expected_shape == "grouped_rows":
        if not dimension_columns or not expected_aggregates or set(group_columns) != set(dimension_columns):
            issues.append(
                _issue(
                    "result_shape_mismatch",
                    "grouped_rows requires dimensions, measures and matching GROUP BY",
                    expected="grouped_rows",
                )
            )
    elif group is not None:
        issues.append(
            _issue(
                "result_shape_mismatch",
                "rows result shape cannot introduce unplanned grouping",
                expected="rows",
            )
        )

    required_columns = {
        item.column for item in bound.bindings if item.column and item.kind != "order"
    }
    for column in sorted(required_columns - actual_columns):
        issues.append(
            _issue(
                "missing_bound_column",
                "SQL does not reference a column required by a bound slot",
                expected=column,
            )
        )

    # Preserve order while avoiding duplicate issue records produced by related
    # structural checks.
    deduplicated: list[PlanConformanceIssue] = []
    seen: set[str] = set()
    for item in issues:
        marker = repr(item.as_dict())
        if marker not in seen:
            deduplicated.append(item)
            seen.add(marker)
    return PlanConformanceResult(not deduplicated, tuple(deduplicated), gate)


def check_candidate_conformance(
    candidate: SQLCandidate,
    plan: Union[BoundQueryPlan, ApprovedQueryPlan],
    snapshot: Mapping[str, Any],
) -> PlanConformanceResult:
    """Check fingerprint freshness before applying SQL plan conformance."""

    bound = _bound_plan(plan)
    version_issues: list[PlanConformanceIssue] = []
    if candidate.query_spec_version != bound.query_spec.version:
        version_issues.append(
            _issue(
                "query_spec_version_mismatch",
                "SQL candidate references a stale QuerySpec version",
                expected=bound.query_spec.version,
                actual=candidate.query_spec_version,
            )
        )
    candidate_pins = {
        "database_snapshot_id": candidate.database_snapshot_id,
        "wiki_index_version": candidate.wiki_index_version,
        "vanna_index_version": candidate.vanna_index_version,
        "memory_snapshot_id": candidate.memory_snapshot_id,
        "policy_version": candidate.policy_version,
    }
    for key in (
        "database_snapshot_id",
        "wiki_index_version",
        "vanna_index_version",
        "memory_snapshot_id",
        "policy_version",
    ):
        if key not in bound.version_pins:
            continue
        expected = bound.version_pins[key]
        actual = candidate_pins[key]
        if actual != expected:
            version_issues.append(
                _issue(
                    "version_pin_mismatch",
                    "SQL candidate %s does not match BoundQueryPlan" % key,
                    slot_id=key,
                    expected=expected,
                    actual=actual,
                )
            )
    if version_issues:
        return PlanConformanceResult(False, tuple(version_issues))
    if not candidate.bound_plan_fingerprint:
        return PlanConformanceResult(
            False,
            (
                _issue(
                    "missing_bound_plan_fingerprint",
                    "SQL candidate is not pinned to a BoundQueryPlan",
                    expected=bound.fingerprint,
                ),
            ),
        )
    if candidate.bound_plan_fingerprint != bound.fingerprint:
        return PlanConformanceResult(
            False,
            (
                _issue(
                    "bound_plan_fingerprint_mismatch",
                    "SQL candidate was generated from a stale or different plan",
                    expected=bound.fingerprint,
                    actual=candidate.bound_plan_fingerprint,
                ),
            ),
        )
    return check_plan_conformance(candidate.sql, bound, snapshot)


# Concise name for deterministic runtime node registration.
plan_conformance = check_plan_conformance


__all__ = [
    "PlanConformanceIssue",
    "PlanConformanceResult",
    "QueryPlanBindingError",
    "approve_query_plan",
    "bind_query_plan",
    "check_candidate_conformance",
    "check_plan_conformance",
    "plan_conformance",
]
