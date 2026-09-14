"""Deterministic production-feedback attribution for bounded semantic memory."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping

import sqlglot
from sqlglot import exp

from .sql_safety import validate_sql


MEMORY_RULE_CONTRACT = "AgentSemanticRule/v1"
MEMORY_EVIDENCE_CONTRACT = "AgentSemanticMemoryEvidence/v1"
EXPERIENCE_MEMORY_CONTRACT = "ExperienceMemory/v1"
EXPERIENCE_EVIDENCE_CONTRACT = "ExperienceMemoryEvidence/v1"

EXPERIENCE_MEMORY_STATES = frozenset(
    {"candidate", "confirmed", "rejected", "needs_evidence"}
)
EXPERIENCE_INITIAL_STATES = frozenset({"candidate", "needs_evidence"})
MAX_EXPERIENCE_JSON_BYTES = 64 * 1024
MAX_EXPERIENCE_EVIDENCE_BYTES = 32 * 1024

# These are deterministic role-level invariants.  Attribution selects one
# template from trace/gate/SQL-diff evidence and binds it to the source QueryRun;
# no model-generated reasoning or hidden prompt content enters the memory.
_RULES = {
    "schema_link_mismatch": {
        "target_skill": "schema-grounding",
        "trigger": "问题概念无法唯一映射到 pinned schema，或候选使用了 SchemaPlan 未授权的表列时",
        "action": "依据当前数据库快照逐项绑定实体、表与列，并让 SchemaPlan 覆盖后续计划所需字段；歧义必须显式返回",
        "avoid": "不要猜测表列，也不要把 DraftLinkPack 或历史 SQL 当作 Schema 权威",
        "rationale": "错误表列会污染绑定后的计划、生成与验证步骤",
    },
    "value_binding_mismatch": {
        "target_skill": "schema-grounding",
        "trigger": "业务取值与物理列或实际值域绑定缺失、歧义或未验证时",
        "action": "使用稳定证据和当前快照完成值到物理列与值域的绑定，歧义作为 BindingIssue 返回",
        "avoid": "不要把未验证的取值映射交给后续角色猜测",
        "rationale": "过滤值错绑即使 SQL 语法正确也会返回错误结果",
    },
    "join_semantics_mismatch": {
        "target_skill": "schema-grounding",
        "trigger": "查询需要跨表关联，且 Join 路径、关系证据或结果粒度不完整时",
        "action": "根据 SchemaPlan 关系证据与表间基数绑定 Join 路径，并明确关联后的结果粒度",
        "avoid": "不要提交缺失、歧义或未经证据支持的关联",
        "rationale": "错误关联会遗漏行或产生 fan-out，进而改变结果粒度",
    },
    "aggregation_grain_mismatch": {
        "target_skill": "query-planning",
        "trigger": "问题包含计数、聚合、分组或去重语义，且指标粒度未明确时",
        "action": "在逻辑 QuerySpec 中先确定指标、维度、结果粒度与去重口径，再完成物理绑定",
        "avoid": "不要在粒度未定义时默认 COUNT(*)，或忽略 Join fan-out 对计数的影响",
        "rationale": "计数口径与结果粒度不一致会造成重复计数或聚合层级偏差",
    },
    "filter_value_mismatch": {
        "target_skill": "query-planning",
        "trigger": "用户指定过滤概念、运算符或作用阶段，但 QuerySpec 表达不完整时",
        "action": "在 QuerySpec 中逐项表达过滤概念、运算符与作用阶段，区分行过滤和聚合后过滤",
        "avoid": "不要在逻辑计划中猜测物理字段或将 WHERE 与 HAVING 混用",
        "rationale": "过滤对象或作用阶段错误会改变参与聚合的数据集",
    },
    "ordering_limit_mismatch": {
        "target_skill": "query-planning",
        "trigger": "用户要求排序、最值、Top-K 或数量限制，但 QuerySpec 未完整表达时",
        "action": "在 QuerySpec 中显式写明排序目标、方向、Top-K/LIMIT 和预期结果形状",
        "avoid": "不要依赖默认排序，也不要把最大值误解为任意一行",
        "rationale": "排序目标或限制缺失会让返回行集与用户要求不一致",
    },
    "query_plan_mismatch": {
        "target_skill": "query-planning",
        "trigger": "逻辑 QuerySpec 不完整、不可绑定或混入了用户未提及的物理标识符时",
        "action": "仅从用户意图提取指标、维度、过滤、排序与结果形状，输出完整可绑定的 QuerySpec",
        "avoid": "不要在 Query Planning 阶段混入未经绑定的物理表列或 SQL",
        "rationale": "逻辑计划是后续确定性绑定与一致性验证的语义基线",
    },
    "sql_plan_conformance_mismatch": {
        "target_skill": "sql-generation",
        "trigger": "已有 ApprovedQueryPlan，但 SQL 候选与其表列、关联、聚合、过滤或排序约束不一致时",
        "action": "仅将 ApprovedQueryPlan 翻译为 SQL，并在提交前逐项对照绑定计划与计划指纹",
        "avoid": "不要新增、删除或改写计划未授权的表列、Join、过滤、聚合、排序或 LIMIT",
        "rationale": "SQL Generation 的职责是忠实翻译已批准计划，而不是重新做语义决策",
    },
    "final_selection_mismatch": {
        "target_skill": "text2sql-lead",
        "trigger": "多个 SQL 候选已经门禁与 Critic 评审，但最终候选仍有语义约束未满足时",
        "action": "逐项对照 ApprovedQueryPlan、Critic 决策与用户要求，只选择全部语义约束已满足的候选",
        "avoid": "不要仅根据候选顺序、语法可执行性或单一评分做最终选择",
        "rationale": "终选是执行前最后一层语义责任边界",
    },
    "critic_false_accept": {
        "target_skill": "text2sql-critic",
        "trigger": "SQL 通过 AST/EXPLAIN 等形式门禁，但仍与用户意图或 ApprovedQueryPlan 语义不一致时",
        "action": "盲审时核对候选的指标、维度、过滤、结果粒度、排序与计划一致性",
        "avoid": "不要将 AST 可解析、EXPLAIN 成功或只读门禁通过等同于语义正确",
        "rationale": "形式正确只证明 SQL 可安全执行，不证明它回答了用户问题",
    },
    "sql_gate_failure": {
        "target_skill": "sql-generation",
        "trigger": "SQL 候选无法通过 SQLite 语法、只读、计划一致性或 EXPLAIN 门禁时",
        "action": "输出候选前依据 ApprovedQueryPlan 完成语法、只读与计划一致性自检",
        "avoid": "不要向 Critic 提交无法通过确定性门禁的 SQL",
        "rationale": "不可执行或不安全的候选不应消耗后续审查预算",
    },
}

_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|secret|password|credential|authorization|access[_-]?token|"
    r"refresh[_-]?token|chain[_-]?of[_-]?thought|reasoning|thoughts?|system[_-]?prompt|"
    r"user[_-]?prompt|messages?|agents?|collaboration)",
    re.I,
)
_RAW_EVIDENCE_KEY = re.compile(
    r"^(?:sql|raw_sql|gold_sql|final_sql|original_sql|corrected_sql|feedback[_-]?note)$",
    re.I,
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_ -]?key|secret|password|credential|authorization|bearer|token)"
    r"\b\s*[:=]\s*[^\s,;]+"
)
_SENSITIVE_TOKEN = re.compile(
    r"(?i)(?:\b(?:sk|ak)[-_][a-z0-9][a-z0-9._-]{10,}\b|"
    r"\beyj[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\b|"
    r"\bbearer\s+[a-z0-9._~+/=-]{10,})"
)


def _bounded_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").strip().split())[:limit]


def _safe_text(value: Any, limit: int = 2000) -> str:
    bounded = _bounded_text(value, limit)
    bounded = _SENSITIVE_ASSIGNMENT.sub(r"\1=[REDACTED]", bounded)
    return _SENSITIVE_TOKEN.sub("[REDACTED]", bounded)


def sanitize_memory_evidence(value: Any, *, depth: int = 0) -> Any:
    """Keep a bounded allow-safe audit payload and drop prompts/secrets/CoT."""

    if depth > 6:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        result = {}
        for raw_key, raw_value in list(value.items())[:100]:
            key = _bounded_text(raw_key, 100)
            if not key or _SENSITIVE_KEY.search(key) or _RAW_EVIDENCE_KEY.match(key):
                continue
            result[key] = sanitize_memory_evidence(raw_value, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize_memory_evidence(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return _safe_text(value)
    if type(value) is float and not math.isfinite(value):
        return None
    if value is None or type(value) in {bool, int, float}:
        return value
    return _safe_text(value)


def _safe_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("experience %s must be an object" % field)
    result = sanitize_memory_evidence(value)
    if not isinstance(result, Mapping):
        raise ValueError("experience %s must be an object" % field)
    return dict(result)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def experience_evidence_payload(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the immutable, sanitized proof portion of an Experience."""

    payload = {
        "contract": EXPERIENCE_EVIDENCE_CONTRACT,
        "before": _safe_mapping(value.get("before"), "before"),
        "after": _safe_mapping(value.get("after"), "after"),
        "evidence": _safe_mapping(value.get("evidence"), "evidence"),
        "evidence_grade": _safe_text(value.get("evidence_grade"), 100),
    }
    if len(_canonical_json(payload).encode("utf-8")) > MAX_EXPERIENCE_EVIDENCE_BYTES:
        raise ValueError("experience evidence exceeds size limit")
    return payload


def experience_evidence_sha256(value: Mapping[str, Any]) -> str:
    """Hash immutable proof after redaction and canonicalization."""

    return hashlib.sha256(
        _canonical_json(experience_evidence_payload(value)).encode("utf-8")
    ).hexdigest()


def normalize_experience_memory(
    value: Mapping[str, Any],
    *,
    memory_id: str = "",
    state: str = "",
) -> Mapping[str, Any]:
    """Validate and normalize one bounded ``ExperienceMemory/v1`` record.

    Runtime eligibility deliberately is not part of this contract.  It is a
    server-owned storage invariant and is always false for Experience rows.
    """

    if not isinstance(value, Mapping):
        raise ValueError("experience memory must be an object")
    contract = str(value.get("contract") or EXPERIENCE_MEMORY_CONTRACT).strip()
    if contract != EXPERIENCE_MEMORY_CONTRACT:
        raise ValueError("unsupported experience memory contract")

    normalized_state = _bounded_text(state or value.get("state") or "candidate", 50)
    if normalized_state not in EXPERIENCE_MEMORY_STATES:
        raise ValueError("invalid experience memory state")
    normalized_memory_id = _bounded_text(memory_id or value.get("memory_id"), 200)
    source_task_id = _bounded_text(value.get("source_task_id"), 200)
    source_stage = _bounded_text(value.get("source_stage"), 100)
    target_agent = _bounded_text(
        value.get("target_agent") or value.get("target_skill"), 100
    )
    problem_code = _bounded_text(
        value.get("problem_code") or value.get("failure_kind"), 100
    )
    if type(value.get("source_revision", 1)) is not int:
        raise ValueError("experience source_revision must be a positive integer")
    source_revision = int(value.get("source_revision", 1))
    if source_revision < 1:
        raise ValueError("experience source_revision must be a positive integer")

    scenario = _safe_text(value.get("scenario"), 1200)
    problem = _safe_text(value.get("problem"), 2000)
    correction = _safe_text(value.get("correction"), 2000)
    if not all((source_task_id, source_stage, problem_code, scenario, problem)):
        raise ValueError(
            "experience requires source, stage, problem code, scenario and problem"
        )
    if normalized_state in {"candidate", "confirmed"} and not target_agent:
        raise ValueError("reviewable experience requires a target agent")
    if normalized_state in {"candidate", "confirmed"} and not correction:
        raise ValueError("reviewable experience requires a correction")

    proof = experience_evidence_payload(value)
    if normalized_state in {"candidate", "confirmed"} and (
        not proof["evidence_grade"]
        or not any((proof["before"], proof["after"], proof["evidence"]))
    ):
        raise ValueError("reviewable experience requires structured evidence and grade")
    result = {
        "contract": EXPERIENCE_MEMORY_CONTRACT,
        "memory_id": normalized_memory_id,
        "source_task_id": source_task_id,
        "source_revision": source_revision,
        "target_agent": target_agent,
        "source_stage": source_stage,
        "problem_code": problem_code,
        "scenario": scenario,
        "problem": problem,
        "correction": correction,
        "applicability": _safe_mapping(value.get("applicability"), "applicability"),
        "before": proof["before"],
        "after": proof["after"],
        "evidence": proof["evidence"],
        "evidence_grade": proof["evidence_grade"],
        "state": normalized_state,
    }
    if len(_canonical_json(result).encode("utf-8")) > MAX_EXPERIENCE_JSON_BYTES:
        raise ValueError("experience memory exceeds size limit")
    return result


def decode_memory_payload(
    value: Mapping[str, Any] | str | None,
    *,
    failure_kind: str = "",
    content: str = "",
    source_case_ids: tuple[str, ...] | list[str] = (),
    memory_id: str = "",
    state: str = "",
) -> Mapping[str, Any]:
    """Decode an Experience or legacy semantic rule without changing contract."""

    raw: Any = value
    if isinstance(value, str):
        try:
            raw = json.loads(value or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
    if not isinstance(raw, Mapping):
        raw = {}
    if raw.get("contract") == EXPERIENCE_MEMORY_CONTRACT:
        return normalize_experience_memory(raw, memory_id=memory_id, state=state)
    return normalize_memory_rule(
        raw,
        failure_kind=failure_kind,
        content=content,
        source_case_ids=source_case_ids,
    )


def experience_memory_fingerprint(value: Mapping[str, Any]) -> str:
    """Fingerprint reusable semantics, excluding provenance, evidence and state."""

    normalized = normalize_experience_memory(value)
    semantic = {
        key: normalized[key]
        for key in (
            "target_agent",
            "problem_code",
            "scenario",
            "problem",
            "correction",
            "applicability",
        )
    }
    return hashlib.sha256(_canonical_json(semantic).encode("utf-8")).hexdigest()


def render_experience_memory(value: Mapping[str, Any]) -> str:
    """Render a bounded review projection; proof stays in structured fields."""

    normalized = normalize_experience_memory(value)
    rendered = "场景：%s\n问题：%s" % (
        normalized["scenario"],
        normalized["problem"],
    )
    if normalized["correction"]:
        rendered += "\n修正：%s" % normalized["correction"]
    else:
        rendered += "\n修正：待补充证据"
    return rendered[:3000]


def normalize_memory_rule(
    value: Mapping[str, Any] | None,
    *,
    failure_kind: str,
    content: str = "",
    source_case_ids: tuple[str, ...] | list[str] = (),
) -> Mapping[str, Any]:
    """Normalize both v1 structured rules and legacy free-text memories."""

    raw = value if isinstance(value, Mapping) else {}
    kind = _bounded_text(failure_kind, 100) or "unknown_failure"
    sources = list(
        dict.fromkeys(
            _bounded_text(item, 200)
            for item in (*source_case_ids, *(raw.get("source_case_ids") or ()))
            if _bounded_text(item, 200)
        )
    )[:50]
    trigger = _safe_text(
        raw.get("trigger") or "检测到 %s 类错误时" % kind,
        500,
    )
    action = _safe_text(raw.get("action") or content, 900)
    avoid = _safe_text(
        raw.get("avoid") or "不要重复已审核案例中的 %s 错误" % kind,
        500,
    )
    rationale = _safe_text(
        raw.get("rationale") or "该规则保留旧版记忆的已审核行为约束。",
        600,
    )
    raw_conditions = raw.get("case_conditions")
    case_conditions = (
        sanitize_memory_evidence(raw_conditions)
        if isinstance(raw_conditions, Mapping)
        else {}
    )
    observations = list(
        dict.fromkeys(
            _safe_text(item, 300)
            for item in raw.get("observations") or ()
            if _safe_text(item, 300)
        )
    )[:20]
    if not all((trigger, action, avoid, rationale)):
        raise ValueError("memory rule requires trigger/action/avoid/rationale")
    return {
        "contract": MEMORY_RULE_CONTRACT,
        "trigger": trigger,
        "action": action,
        "avoid": avoid,
        "rationale": rationale,
        "case_conditions": case_conditions,
        "observations": observations,
        "source_case_ids": sources,
    }


def render_memory_rule(rule: Mapping[str, Any]) -> str:
    """Render a compact compatibility string; structured fields remain canonical."""

    observations = "；".join(str(item) for item in rule.get("observations") or ())
    rendered = "触发：%s\n动作：%s\n禁止：%s\n原因：%s" % (
        rule["trigger"],
        rule["action"],
        rule["avoid"],
        rule["rationale"],
    )
    if observations:
        rendered += "\n案例信号：%s" % observations
    return rendered[:3000]


def memory_rule_fingerprint(
    target_skill: str, failure_kind: str, rule: Mapping[str, Any]
) -> str:
    """Content-address rule semantics while excluding mutable provenance."""

    semantic = {
        key: rule.get(key)
        for key in (
            "trigger",
            "action",
            "avoid",
            "rationale",
            "case_conditions",
        )
    }
    payload = json.dumps(
        [target_skill, failure_kind, semantic],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

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


def _approved_plan_fingerprint(trace: Mapping[str, Any]) -> str:
    value = trace.get("approved_query_plan") or trace.get("bound_query_plan")
    if not value and isinstance(trace.get("collaboration"), Mapping):
        collaboration = trace["collaboration"]
        value = collaboration.get("approved_query_plan") or collaboration.get(
            "bound_query_plan"
        )
    if not isinstance(value, Mapping):
        return ""
    bound = value.get("bound_plan")
    if isinstance(bound, Mapping) and bound.get("fingerprint"):
        return _bounded_text(bound.get("fingerprint"), 200)
    return _bounded_text(value.get("fingerprint"), 200)


def _sql_source_summary(sql: str, gate: Any) -> Mapping[str, Any]:
    """Return audit-useful SQL shape without persisting the SQL text itself."""

    text = str(sql or "").strip()
    features = _features(text)
    return {
        "present": bool(text),
        "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "",
        "normalized_fingerprint": str(gate.fingerprint or ""),
        "accepted_by_static_gate": bool(gate.accepted),
        "tables": list(gate.tables)[:20],
        "columns": list(gate.columns)[:50],
        "gate_errors": [str(item)[:200] for item in gate.errors[:20]],
        "shape": dict(features),
    }


def _trace_source_summary(
    trace: Mapping[str, Any], gate_errors: tuple[str, ...], issue_codes: set[str]
) -> Mapping[str, Any]:
    """Allowlist QueryRun provenance; never copy prompts, messages or role reasoning."""

    pins = trace.get("version_pins")
    if not isinstance(pins, Mapping):
        pins = {}
    return {
        "contract": "SafeQueryRunTraceSummary/v1",
        "task_id": _bounded_text(trace.get("task_id"), 200),
        "status": _bounded_text(trace.get("status"), 50),
        "query_type": _bounded_text(trace.get("query_type") or "DATA_QUERY", 50),
        "recorded_at": _bounded_text(trace.get("recorded_at"), 100),
        "gate_accepted": bool((trace.get("gates") or {}).get("accepted")),
        "gate_errors": [_safe_text(item, 200) for item in gate_errors[:20]],
        "binding_issue_codes": sorted(issue_codes)[:20],
        "approved_query_plan_present": _has_approved_plan(trace),
        "approved_query_plan_fingerprint": _approved_plan_fingerprint(trace),
        "version_pins": {
            key: _bounded_text(pins.get(key), 200)
            for key in (
                "database_snapshot_id",
                "wiki_index_version",
                "vanna_index_version",
                "memory_snapshot_id",
                "policy_version",
            )
            if pins.get(key)
        },
    }


def _case_rule_signals(
    *,
    gate_errors: tuple[str, ...],
    issue_codes: set[str],
    original_features: Mapping[str, Any],
    corrected_features: Mapping[str, Any],
    approved_plan_present: bool,
    corrected_sql_present: bool,
    corrected_sql_accepted: bool,
    feedback_category: str,
) -> tuple[Mapping[str, Any], list[str]]:
    """Derive reviewable case conditions from observable, deterministic facts."""

    error_codes = sorted(
        {
            str(value).casefold().split(":", 1)[0].strip()
            for value in gate_errors
            if str(value).strip()
        }
    )[:20]
    feature_delta = (
        {
            key: {
                "original": original_features.get(key),
                "corrected": corrected_features.get(key),
            }
            for key in sorted(set(original_features).union(corrected_features))
            if original_features.get(key) != corrected_features.get(key)
        }
        if corrected_sql_present
        else {}
    )
    conditions = {
        "gate_error_codes": error_codes,
        "binding_issue_codes": sorted(issue_codes)[:20],
        "original_ast_shape": dict(original_features),
        "corrected_ast_shape": (
            dict(corrected_features) if corrected_sql_present else {}
        ),
        "ast_feature_delta": feature_delta,
        "approved_query_plan_present": bool(approved_plan_present),
        "corrected_sql_present": bool(corrected_sql_present),
        "corrected_sql_accepted": bool(corrected_sql_accepted),
        "feedback_category": _bounded_text(
            feedback_category or "semantic_rejection", 100
        ),
    }
    observations = []
    for key, delta in feature_delta.items():
        observations.append(
            "%s 由 %s 变为 %s"
            % (key, str(delta["original"]).lower(), str(delta["corrected"]).lower())
        )
    if original_features and not feature_delta:
        active_shape = [
            key
            for key, value in sorted(original_features.items())
            if value is True or (key == "join_count" and int(value or 0) > 0)
        ]
        if active_shape:
            observations.append("原始 SQL 形状：%s" % ", ".join(active_shape))
    if issue_codes:
        observations.append("绑定信号：%s" % ", ".join(sorted(issue_codes)[:20]))
    if error_codes:
        observations.append("门禁信号：%s" % ", ".join(error_codes))
    if approved_plan_present:
        observations.append("该 QueryRun 已有 ApprovedQueryPlan")
    if corrected_sql_present:
        observations.append(
            "人工修正 SQL %s通过静态门禁"
            % ("已" if corrected_sql_accepted else "未")
        )
    observations.append(
        "人工反馈类别：%s"
        % conditions["feedback_category"]
    )
    return conditions, observations[:20]


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

    template = _RULES[failure_kind]
    target_skill = str(template["target_skill"])
    source_task_id = _bounded_text(trace.get("task_id"), 200)
    case_conditions, observations = _case_rule_signals(
        gate_errors=gate_errors,
        issue_codes=issue_codes,
        original_features=original_features,
        corrected_features=corrected_features,
        approved_plan_present=_has_approved_plan(trace),
        corrected_sql_present=corrected_gate is not None,
        corrected_sql_accepted=bool(corrected_gate and corrected_gate.accepted),
        feedback_category=noted_failure or failure_kind,
    )
    rule = normalize_memory_rule(
        {
            **template,
            "case_conditions": case_conditions,
            "observations": observations,
        },
        failure_kind=failure_kind,
        source_case_ids=([source_task_id] if source_task_id else []),
    )
    content = render_memory_rule(rule)
    safe_original_sql = _sql_source_summary(original_sql, original_gate)
    safe_corrected_sql = (
        _sql_source_summary(corrected_sql, corrected_gate)
        if corrected_gate is not None
        else {
            "present": False,
            "source_sha256": "",
            "normalized_fingerprint": "",
            "accepted_by_static_gate": False,
            "tables": [],
            "columns": [],
            "gate_errors": [],
            "shape": {},
        }
    )
    feedback_digest = (
        hashlib.sha256(feedback_note.strip().encode("utf-8")).hexdigest()
        if feedback_note.strip()
        else ""
    )
    evidence = {
        "contract": "ProductionFeedbackAttribution/v3",
        "source_task_id": source_task_id,
        "source_case_ids": list(rule["source_case_ids"]),
        "query_type": str(trace.get("query_type") or "DATA_QUERY")[:50],
        "original_sql_fingerprint": original_gate.fingerprint,
        "corrected_sql_fingerprint": (
            corrected_gate.fingerprint if corrected_gate is not None else ""
        ),
        "query_run_trace": _trace_source_summary(trace, gate_errors, issue_codes),
        "original_sql_summary": safe_original_sql,
        "corrected_sql_summary": safe_corrected_sql,
        "human_feedback": {
            "decision": "incorrect",
            "note_present": bool(feedback_note.strip()),
            "note_sha256": feedback_digest,
            # A deterministic category is useful for review without retaining
            # arbitrary feedback prose that may contain credentials or SQL.
            "attributed_failure_kind": noted_failure or failure_kind,
        },
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
        "rule": rule,
        "content": content,
        "evidence": sanitize_memory_evidence(evidence),
        "origin_split": "production_feedback",
    }
