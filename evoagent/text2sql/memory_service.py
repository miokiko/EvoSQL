"""Shared, best-effort QueryTrace and Experience finalization.

The query path owns the answer.  This module owns only the side effects that
record a terminal QueryRun and turn *observable* repairs into reviewable
``ExperienceMemory/v1`` candidates.  Consequently ``finalize_run`` never
raises a persistence error to its caller: a failed write is reported as
``memory_status=degraded`` and must not replace an already-produced answer.

The extractors are intentionally deterministic.  They consume public runtime
artifacts (revision requests, immutable plan fingerprints and gate results),
not model reasoning, prompts, or a second model call.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Protocol, Sequence, Tuple

from .memory_attribution import (
    EXPERIENCE_MEMORY_CONTRACT,
    attribute_query_failure,
    sanitize_memory_evidence,
)


PRODUCTION_EXPERIENCE_ORIGINS = frozenset({"web", "cli"})
PRODUCTION_EXPERIENCE_LANE = "stable"

_AGENTS = frozenset(
    {
        "text2sql-lead",
        "schema-grounding",
        "query-planning",
        "sql-generation",
        "text2sql-critic",
    }
)
_COLLABORATION_FIELDS = (
    "protocol",
    "route",
    "route_gate_errors",
    "clarification",
    "clarification_continuation",
    "lead_delegation",
    "draft_link_pack",
    "delegations",
    "initial_worker_results",
    "worker_results",
    "bound_query_plan",
    "initial_binding_conflicts",
    "binding_conflicts",
    "lead_assessment",
    "revision_requests",
    "revision_request_contract_errors",
    "revisions_applied",
    "lead_plan_approval",
    "approved_query_plan",
    "plan_approval_errors",
    "sql_generation_initial",
    "sql_generation_result",
    "sql_generation_repairs",
    "candidate_gate_results",
    "candidate_gate_rounds",
    "critic_result",
    "lead_final",
    "diagnostic",
)
_PIN_FIELDS = (
    "database_snapshot_id",
    "wiki_index_version",
    "vanna_index_version",
    "memory_snapshot_id",
    "policy_version",
)
_PLAN_CODES = frozenset(
    {
        "ambiguous_schema_binding",
        "duplicate_slot_id",
        "invalid_schema_binding",
        "missing_logical_reference",
        "missing_schema_binding",
        "result_grain_mismatch",
        "unsupported_query_contract",
        "unverified_value_binding",
        "ambiguous_value_binding",
        "invalid_value_binding",
        "missing_value_binding",
    }
)
_INFRASTRUCTURE_GATE_CODES = frozenset(
    {
        "candidate_gate_runtime_failure",
        "database_unavailable",
        "llm_runtime_failure",
        "sql_generation_failure",
        "storage_failure",
        "timeout",
    }
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_ -]?key|secret|password|credential|authorization|bearer|token)"
    r"\b\s*[:=]\s*[^\s,;]+"
)
_BEARER_SECRET = re.compile(
    r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}"
)
_BARE_SECRET = re.compile(
    r"(?i)(?<![a-z0-9])(?:"
    r"sk-[a-z0-9._\\-]{8,}|"
    r"(?:access[_-]?key|ak)[_-]?[a-z0-9._-]{12,}|"
    r"eyj[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}"
    r")(?![a-z0-9])"
)
_SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "password",
        "credential",
        "credentials",
        "authorization",
        "access_token",
        "refresh_token",
        "id_token",
    }
)
_HIDDEN_REASONING_FIELDS = frozenset(
    {
        "prompt",
        "system_prompt",
        "user_prompt",
        "messages",
        "chain_of_thought",
        "hidden_reasoning",
        "reasoning_content",
    }
)
_SENSITIVE_RESULT_COLUMN = re.compile(
    r"(?i)(?:^|[._-])(password|passwd|secret|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|authorization|credential)(?:$|[._-])"
)
_PLAN_CODE_IN_GUIDANCE = re.compile(
    r"(?:^|;\s*)([a-z][a-z0-9_]{2,100})(?:\[[^\]]*\])?\s*:", re.I
)


class MemoryFinalizationStore(Protocol):
    """Minimal duck-typed store surface required by :func:`finalize_run`."""

    snapshot: Mapping[str, Any]

    def save_query_trace(self, trace: Mapping[str, Any]) -> None:
        ...

    def add_experience_memory(
        self,
        experience: Mapping[str, Any],
        *,
        origin_split: str = "production_feedback",
    ) -> str:
        ...


@dataclass(frozen=True)
class MemoryWriteStatus:
    """Bounded status returned to Web/CLI without changing the query result."""

    status: str
    trace_recorded: bool
    task_id: str = ""
    origin: str = ""
    source_lane: str = ""
    experience_ids: Tuple[str, ...] = ()
    experience_states: Tuple[str, ...] = ()
    experience_skipped_reason: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"recorded", "degraded"}:
            raise ValueError("memory write status must be recorded or degraded")

    @property
    def experience_count(self) -> int:
        return len(self.experience_ids)

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "memory_status": self.status,
            "trace_recorded": self.trace_recorded,
            "task_id": self.task_id,
            "origin": self.origin,
            "source_lane": self.source_lane,
            "experience_count": self.experience_count,
            "experience_ids": list(self.experience_ids),
            "experience_states": list(self.experience_states),
            "experience_skipped_reason": self.experience_skipped_reason,
            "error": self.error,
        }


def _bounded_text(value: Any, limit: int = 2_000) -> str:
    compact = " ".join(str(value or "").strip().split())[:limit]
    compact = _SECRET_ASSIGNMENT.sub(r"\1=[REDACTED]", compact)
    compact = _BEARER_SECRET.sub("Bearer [REDACTED]", compact)
    return _BARE_SECRET.sub("[REDACTED]", compact)


def _sanitize_trace_payload(value: Any, *, depth: int = 0) -> Any:
    """Recursively redact credentials before any QueryTrace reaches SQLite."""

    if depth > 10:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        sanitized = {}
        for raw_key, raw_value in list(value.items())[:300]:
            key = str(raw_key)[:200]
            normalized = key.strip().casefold().replace("-", "_").replace(" ", "_")
            if normalized in _SECRET_FIELD_NAMES or normalized in _HIDDEN_REASONING_FIELDS:
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = _sanitize_trace_payload(
                    raw_value, depth=depth + 1
                )
        return sanitized
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [
            _sanitize_trace_payload(item, depth=depth + 1)
            for item in list(value)[:300]
        ]
    if isinstance(value, str):
        return _bounded_text(value, 20_000)
    if value is None or type(value) in {bool, int, float}:
        return value
    return _bounded_text(value, 2_000)


def _redact_sensitive_result_columns(
    columns: Sequence[Any], rows: Sequence[Any]
) -> Sequence[Any]:
    sensitive = {
        index
        for index, column in enumerate(columns)
        if _SENSITIVE_RESULT_COLUMN.search(str(column or ""))
    }
    if not sensitive:
        return rows
    redacted = []
    for row in rows:
        if not isinstance(row, Sequence) or isinstance(
            row, (str, bytes, bytearray)
        ):
            redacted.append(row)
            continue
        values = list(row)
        for index in sensitive:
            if index < len(values):
                values[index] = "[REDACTED]"
        redacted.append(values)
    return redacted


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text_fingerprint(value: Any) -> str:
    text = str(value or "").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return value
    return ()


def _revision(value: Any) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _recorded_at(value: Optional[str]) -> str:
    return str(value or datetime.now(timezone.utc).isoformat())[:100]


def _safe_collaboration(value: Any) -> Mapping[str, Any]:
    source = _mapping(value)
    return _sanitize_trace_payload({
        key: copy.deepcopy(source[key])
        for key in _COLLABORATION_FIELDS
        if key in source
    })


def _worker_results(collaboration: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    values = {}
    for item in _sequence(collaboration.get("worker_results")):
        if not isinstance(item, Mapping):
            continue
        worker = str(item.get("worker") or "")
        if worker:
            values[worker] = item
    return values


def _initial_worker_results(
    collaboration: Mapping[str, Any],
) -> Mapping[str, Mapping[str, Any]]:
    return _worker_results(
        {"worker_results": collaboration.get("initial_worker_results")}
    )


def _worker_plan_fingerprint(value: Mapping[str, Any]) -> str:
    """Fingerprint only the structured Plan, excluding free-form worker notes."""

    worker = str(value.get("worker") or "")
    output = _mapping(value.get("output"))
    field = {
        "schema-grounding": "schema_plan",
        "query-planning": "query_spec",
        "sql-strategy": "query_spec",
    }.get(worker, "")
    plan = _mapping(output.get(field)) if field else {}
    return _fingerprint(plan) if plan else ""


def _public_draft_link_pack(value: Any) -> Mapping[str, Any]:
    draft = _mapping(value)
    if not draft:
        return {}
    fields = (
        "contract",
        "trust",
        "draft_sql",
        "draft_valid",
        "draft_error",
        "tables",
        "columns",
        "projection_columns",
        "filter_columns",
        "group_columns",
        "order_columns",
        "join_columns",
        "unresolved_columns",
        "ambiguous_columns",
        "column_owners",
        "has_star",
        "joins",
        "links",
        "logical_concepts",
        "draft_output",
        "forward_linking",
        "semantic_completion",
        "coverage",
    )
    return _sanitize_trace_payload(
        {key: copy.deepcopy(draft.get(key)) for key in fields if key in draft}
    )


def _retrieval_trace(
    collaboration: Mapping[str, Any],
    workers: Mapping[str, Mapping[str, Any]],
) -> Sequence[Mapping[str, Any]]:
    values = []
    for role in ("schema-grounding", "query-planning"):
        worker = workers.get(role)
        if worker is None and role == "query-planning":
            worker = workers.get("sql-strategy")
        for item in _sequence(_mapping(worker).get("retrieval")):
            if isinstance(item, Mapping):
                values.append({"role": role, **copy.deepcopy(dict(item))})

    planning = workers.get("query-planning") or workers.get("sql-strategy") or {}
    generation = _mapping(collaboration.get("sql_generation_result"))
    sources = (
        ("text2sql-lead", "delegation", collaboration.get("lead_delegation")),
        ("text2sql-lead", "assessment", collaboration.get("lead_assessment")),
        ("text2sql-critic", "critique", collaboration.get("critic_result")),
        ("text2sql-lead", "selection", collaboration.get("lead_final")),
        ("schema-grounding", "worker", workers.get("schema-grounding")),
        ("query-planning", "worker", planning),
        ("sql-generation", "worker", generation),
    )
    for role, phase, payload in sources:
        if not isinstance(payload, Mapping):
            continue
        memory_ids = list(
            dict.fromkeys(
                str(item)[:200]
                for item in _sequence(payload.get("memory_evidence_ids"))
                if str(item).strip()
            )
        )[:50]
        if memory_ids:
            values.append(
                {
                    "role": role,
                    "phase": phase,
                    "backend": "semantic-memory",
                    "memory_ids": memory_ids,
                }
            )
    return _sanitize_trace_payload(values[:100])


def build_query_trace(
    result: Mapping[str, Any],
    internal: Optional[Mapping[str, Any]] = None,
    *,
    task_id: str = "",
    user_id: str = "local-user",
    session_id: str = "default",
    origin: str,
    source_lane: str = PRODUCTION_EXPERIENCE_LANE,
    source_revision: int = 1,
    recorded_at: Optional[str] = None,
) -> Mapping[str, Any]:
    """Build the common Web/CLI QueryTrace projection from one terminal result."""

    if not isinstance(result, Mapping):
        raise TypeError("terminal Text2SQL result must be a mapping")
    terminal = dict(result)
    internal_value = dict(internal or {})
    resolved_task_id = _bounded_text(
        task_id or terminal.get("task_id") or internal_value.get("task_id"), 200
    )
    if not resolved_task_id:
        raise ValueError("query trace task_id is required")
    resolved_origin = _bounded_text(origin, 50).casefold()
    resolved_lane = _bounded_text(source_lane, 50).casefold()
    if not resolved_origin:
        raise ValueError("query trace origin is required")
    if not resolved_lane:
        raise ValueError("query trace source_lane is required")

    collaboration = _safe_collaboration(
        internal_value.get("collaboration") or terminal.get("collaboration")
    )
    workers = _worker_results(collaboration)
    grounding = _mapping(_mapping(workers.get("schema-grounding")).get("output"))
    planning_worker = workers.get("query-planning") or workers.get("sql-strategy") or {}
    planning = _mapping(_mapping(planning_worker).get("output"))
    answer = _mapping(terminal.get("answer") or internal_value.get("answer"))
    rows = list(_sequence(answer.get("rows")))[:50]
    row_count = int(answer.get("row_count") or len(rows))

    trace = {
        "task_id": resolved_task_id,
        "recorded_at": _recorded_at(recorded_at),
        "status": _bounded_text(terminal.get("status") or "unknown", 50),
        "question": _bounded_text(terminal.get("question"), 2_000),
        "original_question": _bounded_text(
            terminal.get("original_question") or terminal.get("question"), 2_000
        ),
        "standalone_question": _bounded_text(
            terminal.get("standalone_question") or terminal.get("question"), 2_000
        ),
        "query_type": _bounded_text(terminal.get("query_type") or "DATA_QUERY", 50),
        "parent_task_id": _bounded_text(
            terminal.get("parent_task_id")
            or terminal.get("parent_query_run_id"),
            200,
        ),
        "user_id": _bounded_text(user_id or "local-user", 200),
        "session_id": _bounded_text(session_id or "default", 200),
        "origin": resolved_origin,
        "source_lane": resolved_lane,
        "source_revision": _revision(source_revision),
        "final_sql": str(terminal.get("final_sql") or "")[:20_000],
        "gates": copy.deepcopy(dict(_mapping(terminal.get("gates")))),
        "agents": copy.deepcopy(list(_sequence(terminal.get("agents"))))[:20],
        "execution": copy.deepcopy(dict(_mapping(terminal.get("execution")))),
        "version_pins": copy.deepcopy(dict(_mapping(terminal.get("version_pins")))),
        "schema_plan": copy.deepcopy(dict(_mapping(grounding.get("schema_plan")))),
        "query_spec": copy.deepcopy(dict(_mapping(planning.get("query_spec")))),
        "draft_link_pack": _public_draft_link_pack(
            collaboration.get("draft_link_pack")
        ),
        "collaboration": collaboration,
        "retrieval": list(_retrieval_trace(collaboration, workers)),
        "result_rows": rows,
        "answer": {
            "columns": list(_sequence(answer.get("columns")))[:200],
            "row_count": row_count,
            "truncated": bool(answer.get("truncated")) or row_count > len(rows),
            "summary_text": _bounded_text(answer.get("summary_text"), 2_000),
        },
    }
    trace = _sanitize_trace_payload(trace)
    trace["result_rows"] = list(
        _redact_sensitive_result_columns(
            _sequence(_mapping(trace.get("answer")).get("columns")),
            _sequence(trace.get("result_rows")),
        )
    )
    return trace


def production_experience_source(origin: str, source_lane: str) -> bool:
    """Only real Web/CLI traffic on the frozen stable lane may teach Policy."""

    return (
        str(origin or "").strip().casefold() in PRODUCTION_EXPERIENCE_ORIGINS
        and str(source_lane or "").strip().casefold() == PRODUCTION_EXPERIENCE_LANE
    )


def _pins(trace: Mapping[str, Any]) -> Mapping[str, str]:
    values = _mapping(trace.get("version_pins"))
    return {
        key: _bounded_text(values.get(key), 200)
        for key in _PIN_FIELDS
        if values.get(key)
    }


def _approved_plan(collaboration: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(collaboration.get("approved_query_plan"))


def _approved_plan_fingerprints(
    collaboration: Mapping[str, Any],
) -> Tuple[str, str]:
    approved = _approved_plan(collaboration)
    bound = _mapping(approved.get("bound_plan"))
    return (
        _bounded_text(approved.get("fingerprint"), 200),
        _bounded_text(bound.get("fingerprint"), 200),
    )


def _derived_memory_ids(trace: Mapping[str, Any]) -> Sequence[str]:
    values = list(_sequence(trace.get("policy_source_memory_ids")))
    for item in _sequence(trace.get("retrieval")):
        if not isinstance(item, Mapping) or item.get("backend") != "semantic-memory":
            continue
        values.extend(_sequence(item.get("memory_ids")))
    return list(
        dict.fromkeys(
            _bounded_text(item, 200) for item in values if _bounded_text(item, 200)
        )
    )[:50]


def _experience(
    trace: Mapping[str, Any],
    *,
    target_agent: str,
    source_stage: str,
    problem_code: str,
    scenario: str,
    problem: str,
    correction: str,
    applicability: Optional[Mapping[str, Any]] = None,
    before: Optional[Mapping[str, Any]] = None,
    after: Optional[Mapping[str, Any]] = None,
    evidence: Optional[Mapping[str, Any]] = None,
    evidence_grade: str,
    state: str,
) -> Mapping[str, Any]:
    safe_evidence = sanitize_memory_evidence(
        {
            "source_task_id": _bounded_text(trace.get("task_id"), 200),
            "query_type": _bounded_text(trace.get("query_type") or "DATA_QUERY", 50),
            "version_pins": _pins(trace),
            "derived_from_memory_ids": list(_derived_memory_ids(trace)),
            **dict(evidence or {}),
        }
    )
    return {
        "contract": EXPERIENCE_MEMORY_CONTRACT,
        "source_task_id": _bounded_text(trace.get("task_id"), 200),
        "source_revision": _revision(trace.get("source_revision")),
        "target_agent": _bounded_text(target_agent, 100),
        "source_stage": _bounded_text(source_stage, 100),
        "problem_code": _bounded_text(problem_code, 100),
        "scenario": _bounded_text(scenario, 600),
        "problem": _bounded_text(problem, 900),
        "correction": _bounded_text(correction, 900),
        "applicability": sanitize_memory_evidence(dict(applicability or {})),
        "before": sanitize_memory_evidence(dict(before or {})),
        "after": sanitize_memory_evidence(dict(after or {})),
        "evidence": safe_evidence,
        "evidence_grade": _bounded_text(evidence_grade, 100),
        "state": state,
    }


def _feedback_value(
    trace: Mapping[str, Any], feedback: Optional[Mapping[str, Any]]
) -> Mapping[str, Any]:
    if isinstance(feedback, Mapping):
        return feedback
    for key in ("user_feedback", "feedback"):
        if isinstance(trace.get(key), Mapping):
            return _mapping(trace.get(key))
    return {}


def _incorrect_decision(value: Mapping[str, Any]) -> bool:
    decision = str(
        value.get("decision") or value.get("feedback") or value.get("status") or ""
    ).strip().casefold()
    return decision in {"incorrect", "rejected", "wrong"}


def _corrected_sql_proof(
    corrected_sql: str,
    feedback: Mapping[str, Any],
    snapshot: Optional[Mapping[str, Any]],
) -> Tuple[bool, str]:
    if not corrected_sql:
        return False, ""
    if snapshot:
        try:
            from .sql_safety import validate_sql

            validation = validate_sql(corrected_sql, snapshot)
            return (
                bool(validation.accepted),
                str(validation.fingerprint or "")
                if validation.accepted
                else "",
            )
        except Exception:
            return False, ""
    gate = _mapping(
        feedback.get("corrected_sql_gate") or feedback.get("corrected_gate")
    )
    accepted = bool(gate.get("accepted") or feedback.get("corrected_sql_accepted"))
    fingerprint = str(gate.get("fingerprint") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint, re.I):
        fingerprint = _text_fingerprint(corrected_sql) if accepted else ""
    return accepted, fingerprint.casefold()


def extract_user_correction_experiences(
    trace: Mapping[str, Any],
    feedback: Optional[Mapping[str, Any]] = None,
    *,
    snapshot: Optional[Mapping[str, Any]] = None,
) -> Sequence[Mapping[str, Any]]:
    """Extract one user-backed correction, or an unattributed needs-evidence row."""

    value = _feedback_value(trace, feedback)
    if not value or not _incorrect_decision(value):
        return ()

    note = _bounded_text(
        value.get("note") or value.get("reason") or value.get("review_note"), 900
    )
    corrected_sql = str(value.get("corrected_sql") or "").strip()[:20_000]
    corrected_accepted, corrected_fingerprint = _corrected_sql_proof(
        corrected_sql, value, snapshot
    )
    explicit_agent = _bounded_text(
        value.get("target_agent") or value.get("target_skill"), 100
    )
    explicit_code = _bounded_text(
        value.get("problem_code") or value.get("failure_kind"), 100
    )
    explicit_correction = _bounded_text(
        value.get("correction") or value.get("correction_summary"), 900
    )
    attributed: Mapping[str, Any] = {}
    if corrected_accepted and snapshot:
        try:
            attributed = attribute_query_failure(
                trace,
                snapshot,
                corrected_sql=corrected_sql,
                feedback_note=note,
            )
        except Exception:
            attributed = {}

    target_agent = explicit_agent or _bounded_text(
        attributed.get("target_skill"), 100
    )
    problem_code = explicit_code or _bounded_text(
        attributed.get("failure_kind"), 100
    )
    # A generic attribution fallback must not silently blame the Critic.  It is
    # reviewable evidence, but not enough evidence for an owner.
    if (
        target_agent == "text2sql-critic"
        and explicit_agent != "text2sql-critic"
        and explicit_code != "critic_false_accept"
    ):
        target_agent = ""
        problem_code = ""

    # Target Replay can deterministically verify user feedback only against a
    # corrected SQL fingerprint that passed the current read-only Schema gate.
    # A prose-only correction remains useful evidence, but cannot become a
    # confirmable Policy source yet.
    candidate_ready = bool(
        note
        and target_agent in _AGENTS
        and problem_code
        and corrected_accepted
    )
    state = "candidate" if candidate_ready else "needs_evidence"
    if not problem_code:
        problem_code = "user_correction_unattributed"
    correction = explicit_correction
    if not correction and corrected_accepted:
        correction = "人工修正 SQL 已通过当前快照的确定性只读与 Schema 门禁。"
    if not correction:
        correction = "需要补充可验证的修正、差异或明确责任边界后再归因。"
    problem = note or "用户明确判定结果不正确，但尚未提供可归因的结构化原因。"
    original_sql = str(trace.get("final_sql") or "")
    gates = _mapping(trace.get("gates"))
    return (
        _experience(
            trace,
            target_agent=target_agent,
            source_stage="user-feedback",
            problem_code=problem_code,
            scenario="用户对终态 QueryRun 明确提交了错误反馈。",
            problem=problem,
            correction=correction,
            applicability={"human_feedback_required": True},
            before={
                "sql_fingerprint": _text_fingerprint(original_sql),
                "gate_accepted": bool(gates.get("accepted")),
                "gate_codes": _gate_codes(gates.get("errors")),
            },
            after={
                # The deterministic SQL Gate hashes normalized SQLite AST output,
                # so harmless formatting differences during Target Replay do not
                # invalidate an otherwise identical correction.
                "sql_fingerprint": corrected_fingerprint,
                "gate_accepted": corrected_accepted,
            },
            evidence={
                "feedback_decision": "incorrect",
                "feedback_note_present": bool(note),
                "feedback_note_sha256": _text_fingerprint(note),
                "corrected_sql_present": bool(corrected_sql),
                "corrected_sql_accepted": corrected_accepted,
            },
            evidence_grade=(
                "human_correction_with_gate"
                if candidate_ready and corrected_accepted
                else "human_explicit_correction"
                if candidate_ready
                else "human_feedback_only"
            ),
            state=state,
        ),
    )


def _revision_requests(collaboration: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    direct = collaboration.get("revision_requests")
    if not _sequence(direct):
        direct = _mapping(collaboration.get("lead_assessment")).get(
            "revision_requests"
        )
    return [dict(item) for item in _sequence(direct) if isinstance(item, Mapping)]


def _request_codes(request: Mapping[str, Any]) -> Sequence[str]:
    values = []
    for key in ("problem_code", "code", "issue_code"):
        if request.get(key):
            values.append(request.get(key))
    values.extend(_sequence(request.get("issue_codes")))
    for issue in _sequence(request.get("issues")):
        if isinstance(issue, Mapping):
            values.append(issue.get("code"))
        else:
            values.append(issue)
    guidance = str(request.get("guidance") or "")
    values.extend(
        match.group(1)
        for match in _PLAN_CODE_IN_GUIDANCE.finditer(guidance)
        if match.group(1).casefold() in _PLAN_CODES
    )
    return list(
        dict.fromkeys(
            _bounded_text(value, 100).casefold()
            for value in values
            if _bounded_text(value, 100)
        )
    )[:20]


def _resolved_plan_revision(
    request: Mapping[str, Any],
    collaboration: Mapping[str, Any],
    code: str,
) -> bool:
    if request.get("resolved") is False:
        return False
    remaining = {
        _bounded_text(item.get("code"), 100).casefold()
        for item in _sequence(collaboration.get("binding_conflicts"))
        if isinstance(item, Mapping) and item.get("code")
    }
    errors = {
        _bounded_text(item, 100).casefold()
        for item in _sequence(collaboration.get("plan_approval_errors"))
        if _bounded_text(item, 100)
    }
    approved, bound = _approved_plan_fingerprints(collaboration)
    return bool(
        code
        and (approved or bound)
        and code not in remaining
        and code not in errors
        and not remaining
        and not errors
    )


def extract_plan_revision_experiences(
    trace: Mapping[str, Any],
    internal: Optional[Mapping[str, Any]] = None,
) -> Sequence[Mapping[str, Any]]:
    """Extract resolved, single-owner Plan Worker revision evidence."""

    collaboration = _safe_collaboration(
        _mapping(internal).get("collaboration") or trace.get("collaboration")
    )
    requests = _revision_requests(collaboration)
    if not requests or int(collaboration.get("revisions_applied") or 0) <= 0:
        return ()
    approved_fingerprint, bound_fingerprint = _approved_plan_fingerprints(
        collaboration
    )
    if not (approved_fingerprint or bound_fingerprint):
        return ()

    workers = _worker_results(collaboration)
    initial_workers = _initial_worker_results(collaboration)
    assignments = {
        str(item.get("assignment_id") or ""): str(item.get("worker") or "")
        for item in _sequence(collaboration.get("delegations"))
        if isinstance(item, Mapping)
    }
    values = []
    for request in requests:
        worker = str(
            request.get("worker")
            or assignments.get(str(request.get("assignment_id") or ""))
            or ""
        )
        if worker not in {"schema-grounding", "query-planning"}:
            continue
        codes = list(_request_codes(request))
        if not codes:
            codes = [""]
        final_worker = _mapping(workers.get(worker))
        initial_worker = _mapping(initial_workers.get(worker))
        initial_plan_fingerprint = _worker_plan_fingerprint(initial_worker)
        final_plan_fingerprint = _worker_plan_fingerprint(final_worker)
        for raw_code in codes:
            code = raw_code
            initial_conflicts = [
                dict(item)
                for item in _sequence(
                    collaboration.get("initial_binding_conflicts")
                )
                if isinstance(item, Mapping)
                and _bounded_text(item.get("code"), 100).casefold() == code
                and _bounded_text(item.get("owner"), 100) == worker
            ]
            initial_issue_present = bool(code and initial_conflicts)
            plan_changed = bool(
                initial_plan_fingerprint
                and final_plan_fingerprint
                and initial_plan_fingerprint != final_plan_fingerprint
            )
            resolved = bool(
                initial_worker.get("status") == "completed"
                and final_worker.get("status") == "completed"
                and initial_issue_present
                and plan_changed
                and _resolved_plan_revision(request, collaboration, code)
            )
            state = "candidate" if resolved else "needs_evidence"
            problem_code = code or "%s_plan_revision" % worker
            guidance = _bounded_text(request.get("guidance"), 900)
            values.append(
                _experience(
                    trace,
                    target_agent=worker,
                    source_stage="plan-revisions",
                    problem_code=problem_code,
                    scenario=(
                        "Binder/Lead 向 %s 发出一次有界计划修订，最终计划进入批准态。"
                        % worker
                    ),
                    problem=guidance or "计划修订请求缺少可确定的问题代码。",
                    correction=(
                        "该问题在修订后的 Worker 输出中消失，且 Harness 铸造了 ApprovedQueryPlan。"
                        if resolved
                        else "需要补充修订前后结构化差异和问题消失证据。"
                    ),
                    applicability={
                        "worker": worker,
                        "issue_code": code,
                    },
                    before={
                        "issue_code": code,
                        "issue_present": initial_issue_present,
                        "revision_request_fingerprint": _fingerprint(request),
                        "worker_plan_fingerprint": initial_plan_fingerprint,
                    },
                    after={
                        "worker_plan_fingerprint": final_plan_fingerprint,
                        "approved_plan_fingerprint": approved_fingerprint,
                        "bound_plan_fingerprint": bound_fingerprint,
                        "issue_resolved": resolved,
                    },
                    evidence={
                        "assignment_id": _bounded_text(
                            request.get("assignment_id"), 100
                        ),
                        "revision_request_fingerprint": _fingerprint(request),
                        "initial_binding_conflicts_fingerprint": (
                            _fingerprint(initial_conflicts)
                            if initial_conflicts
                            else ""
                        ),
                        "approved_plan_fingerprint": approved_fingerprint,
                        "bound_plan_fingerprint": bound_fingerprint,
                    },
                    evidence_grade=(
                        "deterministic_plan_revision"
                        if resolved
                        else "incomplete_plan_revision"
                    ),
                    state=state,
                )
            )
    return tuple(values)


def _gate_codes(value: Any) -> Sequence[str]:
    values = []
    for item in _sequence(value):
        if isinstance(item, Mapping):
            raw = item.get("code") or item.get("error")
        else:
            raw = item
        code = _bounded_text(raw, 200).casefold().split(":", 1)[0]
        if code:
            values.append(code)
    return list(dict.fromkeys(values))[:40]


def _round_accepted(round_value: Mapping[str, Any]) -> bool:
    if _sequence(round_value.get("accepted_candidates")):
        return True
    return any(
        isinstance(item, Mapping) and item.get("accepted") is True
        for item in _sequence(round_value.get("candidate_gate_results"))
    )


def _round_sql_fingerprints(round_value: Mapping[str, Any]) -> Sequence[str]:
    values = []
    for item in _sequence(round_value.get("accepted_candidates")):
        if isinstance(item, Mapping) and item.get("sql"):
            values.append(_text_fingerprint(item.get("sql")))
    for item in _sequence(round_value.get("candidate_gate_results")):
        if not isinstance(item, Mapping):
            continue
        validation = _mapping(item.get("validation"))
        if validation.get("fingerprint"):
            values.append(str(validation.get("fingerprint")))
        elif validation.get("normalized_sql"):
            values.append(_text_fingerprint(validation.get("normalized_sql")))
    return list(dict.fromkeys(value for value in values if value))[:20]


def _initial_generation_fingerprints(collaboration: Mapping[str, Any]) -> Sequence[str]:
    initial = _mapping(collaboration.get("sql_generation_initial"))
    values = []
    for item in _sequence(_mapping(initial.get("output")).get("sql_candidates")):
        if isinstance(item, Mapping) and item.get("sql"):
            values.append(_text_fingerprint(item.get("sql")))
    return list(dict.fromkeys(values))[:20]


def _accepted_bound_plan_matches(
    round_value: Mapping[str, Any], bound_fingerprint: str
) -> bool:
    if not bound_fingerprint:
        return False
    candidates = [
        item
        for item in _sequence(round_value.get("accepted_candidates"))
        if isinstance(item, Mapping)
    ]
    return bool(candidates) and all(
        str(item.get("bound_plan_fingerprint") or "") == bound_fingerprint
        for item in candidates
    )


def extract_sql_gate_repair_experiences(
    trace: Mapping[str, Any],
    internal: Optional[Mapping[str, Any]] = None,
) -> Sequence[Mapping[str, Any]]:
    """Extract one reject -> single repair -> accept SQL Generation event."""

    collaboration = _safe_collaboration(
        _mapping(internal).get("collaboration") or trace.get("collaboration")
    )
    rounds = [
        dict(item)
        for item in _sequence(collaboration.get("candidate_gate_rounds"))
        if isinstance(item, Mapping)
    ]
    if len(rounds) != 2 or int(collaboration.get("sql_generation_repairs") or 0) != 1:
        return ()
    first, repaired = rounds
    if _round_accepted(first) or not _round_accepted(repaired):
        return ()
    approved_fingerprint, bound_fingerprint = _approved_plan_fingerprints(
        collaboration
    )
    if not (approved_fingerprint and bound_fingerprint):
        return ()
    if not _accepted_bound_plan_matches(repaired, bound_fingerprint):
        return ()
    if str(trace.get("status") or "") != "success" or not bool(
        _mapping(trace.get("gates")).get("accepted")
    ):
        return ()

    codes = _gate_codes(first.get("gate_issues"))
    if not codes:
        codes = _gate_codes(
            [
                error
                for item in _sequence(first.get("candidate_gate_results"))
                if isinstance(item, Mapping)
                for error in _sequence(item.get("errors"))
            ]
        )
    if any(code in _INFRASTRUCTURE_GATE_CODES for code in codes):
        return ()
    deterministic_codes = list(codes)
    if not deterministic_codes:
        # Infrastructure failures are episodes, not Agent experience.
        return ()

    before_fingerprints = list(_initial_generation_fingerprints(collaboration))
    if not before_fingerprints:
        before_fingerprints = list(_round_sql_fingerprints(first))
    after_fingerprints = list(_round_sql_fingerprints(repaired))
    return (
        _experience(
            trace,
            target_agent="sql-generation",
            source_stage="candidate-gates",
            problem_code="sql_gate_repair",
            scenario=(
                "ApprovedQueryPlan 已冻结，首轮 SQL 被确定性门禁拒绝，一次有界修复后通过。"
            ),
            problem="首轮 SQL 未通过门禁：%s。" % ", ".join(deterministic_codes),
            correction="在同一 ApprovedQueryPlan 下按 Gate code 修复，第二轮候选通过门禁并完成查询。",
            applicability={
                "approved_plan_fingerprint": approved_fingerprint,
                "gate_codes": deterministic_codes,
                "single_repair": True,
            },
            before={
                "sql_fingerprints": before_fingerprints,
                "gate_codes": deterministic_codes,
                "gate_accepted": False,
            },
            after={
                "sql_fingerprints": after_fingerprints,
                "gate_accepted": True,
            },
            evidence={
                "approved_plan_fingerprint": approved_fingerprint,
                "bound_plan_fingerprint": bound_fingerprint,
                "round_count": 2,
            },
            evidence_grade="deterministic_repair",
            state="candidate",
        ),
    )


def extract_experiences(
    trace: Mapping[str, Any],
    internal: Optional[Mapping[str, Any]] = None,
    *,
    user_feedback: Optional[Mapping[str, Any]] = None,
    snapshot: Optional[Mapping[str, Any]] = None,
) -> Sequence[Mapping[str, Any]]:
    """Return all deterministic Experience rows for one eligible terminal run."""

    if not production_experience_source(
        str(trace.get("origin") or ""), str(trace.get("source_lane") or "")
    ):
        return ()
    # Cached-result QA, clarification and ordinary failures are useful episodes
    # but cannot establish a new Agent method.
    if str(trace.get("query_type") or "DATA_QUERY") != "DATA_QUERY":
        return ()
    values = [
        *extract_user_correction_experiences(
            trace, user_feedback, snapshot=snapshot
        ),
        *extract_plan_revision_experiences(trace, internal),
        *extract_sql_gate_repair_experiences(trace, internal),
    ]
    seen = set()
    unique = []
    for value in values:
        key = (
            value.get("source_task_id"),
            value.get("problem_code"),
            _fingerprint(value.get("evidence") or {}),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)
    return tuple(unique)


def _safe_error(exc: BaseException) -> str:
    return "%s: %s" % (type(exc).__name__, _bounded_text(exc, 500))


def finalize_run(
    result: Mapping[str, Any],
    internal: Optional[Mapping[str, Any]] = None,
    *,
    store: MemoryFinalizationStore,
    task_id: str = "",
    user_id: str = "local-user",
    session_id: str = "default",
    origin: str,
    source_lane: str = PRODUCTION_EXPERIENCE_LANE,
    source_revision: int = 1,
    user_feedback: Optional[Mapping[str, Any]] = None,
    recorded_at: Optional[str] = None,
) -> MemoryWriteStatus:
    """Persist one terminal run as a best-effort side effect.

    ``store`` intentionally uses a tiny protocol so an already-open Web store
    and a CLI-owned store can share the exact same path.  Store-level natural
    keys provide idempotency for repeated ``task_id + source_revision`` calls.
    """

    resolved_task_id = _bounded_text(
        task_id
        or (_mapping(result).get("task_id"))
        or (_mapping(internal).get("task_id")),
        200,
    )
    resolved_origin = _bounded_text(origin, 50).casefold()
    resolved_lane = _bounded_text(source_lane, 50).casefold()
    try:
        trace = build_query_trace(
            result,
            internal,
            task_id=resolved_task_id,
            user_id=user_id,
            session_id=session_id,
            origin=resolved_origin,
            source_lane=resolved_lane,
            source_revision=source_revision,
            recorded_at=recorded_at,
        )
        policy_sources = getattr(store, "policy_source_memory_ids", None)
        if callable(policy_sources):
            policy_version = str(
                _mapping(trace.get("version_pins")).get("policy_version") or ""
            )
            try:
                compiled_sources = list(policy_sources(policy_version))
            except (TypeError, ValueError):
                compiled_sources = []
            if compiled_sources:
                trace = {
                    **dict(trace),
                    "policy_source_memory_ids": compiled_sources,
                }
        store.save_query_trace(trace)
    except Exception as exc:
        return MemoryWriteStatus(
            status="degraded",
            trace_recorded=False,
            task_id=resolved_task_id,
            origin=resolved_origin,
            source_lane=resolved_lane,
            error=_safe_error(exc),
        )

    if not production_experience_source(resolved_origin, resolved_lane):
        return MemoryWriteStatus(
            status="recorded",
            trace_recorded=True,
            task_id=resolved_task_id,
            origin=resolved_origin,
            source_lane=resolved_lane,
            experience_skipped_reason="non_production_source",
        )

    snapshot = getattr(store, "snapshot", None)
    try:
        experiences = extract_experiences(
            trace,
            internal,
            user_feedback=user_feedback,
            snapshot=snapshot if isinstance(snapshot, Mapping) else None,
        )
    except Exception as exc:
        return MemoryWriteStatus(
            status="degraded",
            trace_recorded=True,
            task_id=resolved_task_id,
            origin=resolved_origin,
            source_lane=resolved_lane,
            error=_safe_error(exc),
        )

    if not experiences:
        return MemoryWriteStatus(
            status="recorded",
            trace_recorded=True,
            task_id=resolved_task_id,
            origin=resolved_origin,
            source_lane=resolved_lane,
            experience_skipped_reason="no_experience_signal",
        )

    ids = []
    states = []
    errors = []
    for experience in experiences:
        try:
            memory_id = store.add_experience_memory(
                experience, origin_split="production_feedback"
            )
            ids.append(str(memory_id))
            states.append(str(experience.get("state") or "candidate"))
        except Exception as exc:
            errors.append(_safe_error(exc))
    return MemoryWriteStatus(
        status="degraded" if errors else "recorded",
        trace_recorded=True,
        task_id=resolved_task_id,
        origin=resolved_origin,
        source_lane=resolved_lane,
        experience_ids=tuple(ids),
        experience_states=tuple(states),
        error="; ".join(errors)[:1_000],
    )


__all__ = [
    "EXPERIENCE_MEMORY_CONTRACT",
    "MemoryFinalizationStore",
    "MemoryWriteStatus",
    "PRODUCTION_EXPERIENCE_LANE",
    "PRODUCTION_EXPERIENCE_ORIGINS",
    "build_query_trace",
    "extract_experiences",
    "extract_plan_revision_experiences",
    "extract_sql_gate_repair_experiences",
    "extract_user_correction_experiences",
    "finalize_run",
    "production_experience_source",
]
