"""Deterministic production-feedback attribution for bounded semantic memory."""

from __future__ import annotations

from typing import Any, Mapping

import sqlglot
from sqlglot import exp

from .sql_safety import validate_sql


_RULES = {
    "schema_link_mismatch": (
        "schema-grounding",
        "Grounding 必须依据 pinned DDL 校验问题中的实体、表和字段，并在 SchemaPlan 中完整覆盖最终 SQL 所需列；不得以草案字段替代稳定 Schema 证据。",
    ),
    "value_binding_mismatch": (
        "schema-grounding",
        "Grounding 必须使用稳定证据完成业务值到物理列和值域的绑定，并把歧义作为 BindingIssue 返回；不得把未验证的取值映射交给后续角色猜测。",
    ),
    "join_semantics_mismatch": (
        "schema-grounding",
        "Grounding 必须依据 SchemaPlan 的关系证据与表间基数绑定 Join 路径，并明确结果粒度；不得提交缺失、歧义或未经证据支持的关联。",
    ),
    "aggregation_grain_mismatch": (
        "query-planning",
        "Planning 必须在逻辑 QuerySpec 中先确定指标、维度和结果粒度，并明确聚合与去重语义，避免计数口径或聚合层级偏差。",
    ),
    "filter_value_mismatch": (
        "query-planning",
        "Planning 必须逐项表达用户要求的过滤概念、运算符和作用阶段，并区分 WHERE 与聚合后过滤语义；物理字段和值由绑定阶段解析。",
    ),
    "ordering_limit_mismatch": (
        "query-planning",
        "Planning 必须在逻辑 QuerySpec 中显式表达排序指标、方向、Top-K 和 LIMIT，确保它们与用户问题及预期结果形状一致。",
    ),
    "query_plan_mismatch": (
        "query-planning",
        "Planning 必须输出完整且可绑定的逻辑 QuerySpec；所有指标、维度、过滤、排序和结果形状都必须来自用户意图，不得混入未经绑定的物理表列。",
    ),
    "sql_plan_conformance_mismatch": (
        "sql-generation",
        "Generation 只能把已批准的 BoundQueryPlan 翻译为 SQL；候选必须保持绑定表列、Join、聚合、过滤、排序、LIMIT、结果粒度和计划指纹完全一致。",
    ),
    "final_selection_mismatch": (
        "text2sql-lead",
        "Leader 收敛候选时必须逐项对照 SchemaPlan、QuerySpec、Critic 决策与用户要求，只能选择所有语义约束均已满足的候选。",
    ),
    "critic_false_accept": (
        "text2sql-critic",
        "Critic 不得将 AST/EXPLAIN 通过等同于语义正确；盲审时必须核对候选 SQL 与 QuerySpec 的指标、维度、过滤、结果粒度及排序。",
    ),
    "sql_gate_failure": (
        "sql-generation",
        "Generation 输出候选前必须完成 SQLite 语法和只读约束自检；任何不能通过确定性安全、计划一致性或 EXPLAIN 门禁的 SQL 都不得提交给 Critic。",
    ),
}

_SCHEMA_BINDING_CODES = {
    "missing_schema_binding",
    "ambiguous_schema_binding",
    "invalid_schema_binding",
}
_VALUE_BINDING_CODES = {
    "missing_value_binding",
    "ambiguous_value_binding",
    "invalid_value_binding",
    "unverified_value_binding",
}
_SCHEMA_GRAIN_CODES = {
    "result_grain_mismatch",
}
_QUERY_PLANNING_CODES = {
    "duplicate_slot_id",
    "missing_logical_reference",
    "unsupported_query_contract",
}
_SQL_CONFORMANCE_CODES = {
    "bound_plan_fingerprint_mismatch",
    "missing_bound_plan_fingerprint",
    "unexpected_table",
    "missing_table",
    "unexpected_column",
    "unresolved_sql_column",
    "missing_bound_column",
    "missing_join",
    "unexpected_join",
    "aggregation_mismatch",
    "distinct_mismatch",
    "group_by_mismatch",
    "filter_mismatch",
    "unsupported_filter_expression",
    "order_by_mismatch",
    "unsupported_order_expression",
    "limit_mismatch",
    "result_grain_mismatch",
    "result_shape_mismatch",
    "unsupported_query_shape",
}


def _features(sql: str) -> Mapping[str, Any]:
    if not sql.strip():
        return {}
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except sqlglot.errors.ParseError:
        return {}
    return {
        "join_count": sum(1 for _ in tree.find_all(exp.Join)),
        "has_aggregate": any(True for _ in tree.find_all(exp.AggFunc)),
        "has_group": tree.find(exp.Group) is not None,
        "has_distinct": tree.find(exp.Distinct) is not None,
        "has_filter": tree.find(exp.Where) is not None
        or tree.find(exp.Having) is not None,
        "has_order": tree.find(exp.Order) is not None,
        "has_limit": tree.find(exp.Limit) is not None,
    }


def _note_failure_kind(note: str) -> str:
    compact = note.casefold()
    if any(
        value in compact
        for value in ("sql generation", "sql-generation", "sql翻译", "计划一致性", "conformance")
    ):
        return "sql_plan_conformance_mismatch"
    if any(value in compact for value in ("字段", "列", "表错", "table", "column", "schema")):
        return "schema_link_mismatch"
    if any(value in compact for value in ("取值", "值映射", "value binding", "value mapping")):
        return "value_binding_mismatch"
    if any(
        value in compact
        for value in (
            "聚合",
            "计数",
            "去重",
            "平均",
            "求和",
            "粒度",
            "重复计数",
            "fanout",
            "group",
            "count",
            "distinct",
        )
    ):
        return "aggregation_grain_mismatch"
    if any(value in compact for value in ("关联", "连接", "join")):
        return "join_semantics_mismatch"
    if any(value in compact for value in ("筛选", "过滤", "条件", "取值", "where", "filter")):
        return "filter_value_mismatch"
    if any(value in compact for value in ("排序", "前几", "top", "order", "limit")):
        return "ordering_limit_mismatch"
    if any(value in compact for value in ("选错", "候选", "leader", "收敛")):
        return "final_selection_mismatch"
    return ""


def _binding_issue_codes(trace: Mapping[str, Any]) -> set[str]:
    """Collect structured binder issues without depending on one trace layout."""

    groups = [trace.get("binding_issues"), trace.get("binding_conflicts")]
    for field in ("binding", "bound_query_plan", "approved_query_plan"):
        container = trace.get(field)
        if isinstance(container, Mapping):
            groups.append(container.get("issues") or container.get("binding_issues"))
    collaboration = trace.get("collaboration")
    if isinstance(collaboration, Mapping):
        groups.extend(
            (
                collaboration.get("binding_issues"),
                collaboration.get("binding_conflicts"),
            )
        )
    result = set()
    for group in groups:
        if not isinstance(group, (list, tuple)):
            continue
        for issue in group:
            if isinstance(issue, Mapping):
                code = str(issue.get("code") or issue.get("kind") or "").strip().casefold()
            else:
                code = str(issue).strip().casefold()
            if code:
                result.add(code)
    return result


def _has_approved_plan(trace: Mapping[str, Any]) -> bool:
    value = trace.get("approved_query_plan") or trace.get("bound_query_plan")
    if not value and isinstance(trace.get("collaboration"), Mapping):
        collaboration = trace["collaboration"]
        value = collaboration.get("approved_query_plan") or collaboration.get(
            "bound_query_plan"
        )
    return isinstance(value, Mapping) and bool(value)


def attribute_query_failure(
    trace: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    corrected_sql: str = "",
    feedback_note: str = "",
) -> Mapping[str, Any]:
    """Return one bounded, reviewable memory candidate from rejected feedback."""

    original_sql = str(trace.get("final_sql") or "")
    original_gate = validate_sql(original_sql, snapshot)
    corrected_gate = validate_sql(corrected_sql, snapshot) if corrected_sql.strip() else None
    gate_errors = tuple(
        str(value)
        for value in (
            (trace.get("gates") or {}).get("errors")
            or original_gate.errors
            or ()
        )
    )
    joined_errors = " ".join(gate_errors).casefold()
    error_codes = {
        value.casefold().split(":", 1)[0].strip()
        for value in gate_errors
        if value.strip()
    }
    issue_codes = _binding_issue_codes(trace)
    failure_kind = ""

    if issue_codes.intersection(_VALUE_BINDING_CODES):
        failure_kind = "value_binding_mismatch"
    elif issue_codes.intersection(_SCHEMA_GRAIN_CODES):
        failure_kind = "join_semantics_mismatch"
    elif issue_codes.intersection(_SCHEMA_BINDING_CODES):
        failure_kind = "schema_link_mismatch"
    elif issue_codes.intersection(_QUERY_PLANNING_CODES):
        failure_kind = "query_plan_mismatch"
    elif error_codes.intersection(_SQL_CONFORMANCE_CODES) or any(
        value in joined_errors for value in ("plan_conformance", "bound_plan_fingerprint")
    ):
        failure_kind = "sql_plan_conformance_mismatch"
    elif any(
        value in joined_errors
        for value in (
            "unknown_table",
            "unknown_column",
            "schema_plan",
            "join lacks",
            "join_endpoints",
        )
    ):
        failure_kind = (
            "sql_plan_conformance_mismatch"
            if _has_approved_plan(trace)
            else "schema_link_mismatch"
        )
    elif "invalid_final_candidate_index" in joined_errors:
        failure_kind = "final_selection_mismatch"
    elif "critic" in joined_errors:
        failure_kind = "critic_false_accept"
    elif gate_errors or not original_gate.accepted:
        failure_kind = "sql_gate_failure"

    original_features = _features(original_sql)
    corrected_features = _features(corrected_sql)
    noted_failure = _note_failure_kind(feedback_note)
    if not failure_kind and noted_failure:
        failure_kind = noted_failure
    if not failure_kind and corrected_gate is not None:
        if _has_approved_plan(trace) and (
            original_gate.fingerprint != corrected_gate.fingerprint
        ):
            failure_kind = "sql_plan_conformance_mismatch"
        elif set(original_gate.tables) != set(corrected_gate.tables) or set(
            original_gate.columns
        ) != set(corrected_gate.columns):
            failure_kind = "schema_link_mismatch"
        elif original_features.get("join_count") != corrected_features.get("join_count"):
            failure_kind = "join_semantics_mismatch"
        elif any(
            original_features.get(key) != corrected_features.get(key)
            for key in ("has_aggregate", "has_group", "has_distinct")
        ):
            failure_kind = "aggregation_grain_mismatch"
        elif original_features.get("has_filter") != corrected_features.get("has_filter"):
            failure_kind = "filter_value_mismatch"
        elif any(
            original_features.get(key) != corrected_features.get(key)
            for key in ("has_order", "has_limit")
        ):
            failure_kind = "ordering_limit_mismatch"

    if not failure_kind:
        failure_kind = "critic_false_accept"

    target_skill, content = _RULES[failure_kind]
    evidence = {
        "contract": "ProductionFeedbackAttribution/v2",
        "source_task_id": str(trace.get("task_id") or "")[:200],
        "query_type": str(trace.get("query_type") or "DATA_QUERY")[:50],
        "original_sql_fingerprint": original_gate.fingerprint,
        "corrected_sql_fingerprint": (
            corrected_gate.fingerprint if corrected_gate is not None else ""
        ),
        "gate_errors": list(gate_errors)[:20],
        "binding_issue_codes": sorted(issue_codes)[:20],
        "approved_query_plan_present": _has_approved_plan(trace),
        "feature_delta": {
            key: {
                "original": original_features.get(key),
                "corrected": corrected_features.get(key),
            }
            for key in sorted(set(original_features).union(corrected_features))
            if original_features.get(key) != corrected_features.get(key)
        },
        "feedback_note_present": bool(feedback_note.strip()),
        "attribution_method": "deterministic_trace_and_sql_diff",
    }
    return {
        "target_skill": target_skill,
        "failure_kind": failure_kind,
        "content": content,
        "evidence": evidence,
        "origin_split": "production_feedback",
    }
