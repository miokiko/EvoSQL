"""Auditable Text2SQL evolution store, memory review, and promotion gates."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .agentic import validate_runtime_identity
from .evaluation import (
    EVALUATION_ARTIFACT_CONTRACT_VERSION,
    FAILURE_KINDS,
    _percentile as _evaluation_percentile,
)
from .memory_attribution import (
    EXPERIENCE_INITIAL_STATES,
    EXPERIENCE_MEMORY_CONTRACT,
    MEMORY_EVIDENCE_CONTRACT,
    decode_memory_payload,
    experience_evidence_payload,
    experience_evidence_sha256,
    experience_memory_fingerprint,
    memory_rule_fingerprint,
    normalize_experience_memory,
    normalize_memory_rule,
    render_experience_memory,
    render_memory_rule,
    sanitize_memory_evidence,
)
from .policy import PolicyArtifact, TEXT2SQL_SKILLS, require_single_skill_change
from .semantic_rules import SemanticRuleStoreMixin
from .target_replay import (
    experience_has_replay_proof,
    validate_target_replay_artifact,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_QUERY_TRACE_JSON_COLUMNS = (
    "gates_json",
    "agents_json",
    "execution_json",
    "version_pins_json",
    "answer_json",
    "schema_plan_json",
    "query_spec_json",
    "collaboration_json",
    "retrieval_json",
    "result_rows_json",
    "draft_link_pack_json",
)


def _decode_query_trace_row(row: Mapping[str, Any]) -> Mapping[str, Any]:
    item = dict(row)
    for column in _QUERY_TRACE_JSON_COLUMNS:
        if column not in item:
            continue
        try:
            decoded = json.loads(str(item.pop(column) or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = (
                []
                if column in {"agents_json", "retrieval_json", "result_rows_json"}
                else {}
            )
        item[column.removesuffix("_json")] = decoded
    item["source_revision"] = max(1, int(item.get("source_revision") or 1))
    return item


def _query_trace_evidence_sha256(trace: Mapping[str, Any]) -> str:
    """Fingerprint immutable run evidence, excluding display/feedback metadata."""

    evidence = {
        str(key): value
        for key, value in trace.items()
        if key not in {"recorded_at", "feedback_status", "trace_sha256"}
    }
    return hashlib.sha256(_canonical(evidence).encode("utf-8")).hexdigest()


def validate_evaluation_identity(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate the model, runtime, and authorization identity of an evaluation."""

    if not isinstance(value, Mapping):
        raise ValueError("evaluation identity must be an object")
    model = value.get("model")
    if not isinstance(model, Mapping) or set(model) != {
        "provider",
        "model",
        "temperature",
    }:
        raise ValueError("evaluation artifact model contract is invalid")
    if (
        type(model.get("provider")) is not str
        or not str(model["provider"]).strip()
        or type(model.get("model")) is not str
        or not str(model["model"]).strip()
        or type(model.get("temperature")) not in (int, float)
        or not math.isfinite(float(model["temperature"]))
        or float(model["temperature"]) != 0.0
    ):
        raise ValueError("evaluation artifact model identity is invalid")
    runtime = value.get("runtime")
    if not isinstance(runtime, Mapping) or not runtime:
        raise ValueError("evaluation artifact runtime must be a non-empty object")
    canonical_runtime = validate_runtime_identity(runtime)
    principals = value.get("principals")
    if (
        not isinstance(principals, Sequence)
        or isinstance(principals, (str, bytes, bytearray))
        or not principals
        or any(
            type(principal) is not str or not principal.strip()
            for principal in principals
        )
    ):
        raise ValueError(
            "evaluation artifact principals must be a non-empty list of strings"
        )
    normalized_principals = tuple(
        sorted(principal.strip() for principal in principals)
    )
    if len(set(normalized_principals)) != len(normalized_principals):
        raise ValueError("evaluation artifact principals must not contain duplicates")
    return {
        "model": dict(model),
        "runtime": dict(canonical_runtime),
        "principals": list(normalized_principals),
    }


def _memory_source_case_ids(
    evidence: Mapping[str, Any], explicit: Sequence[str] = ()
) -> tuple[str, ...]:
    """Extract bounded, non-secret case identifiers from known provenance fields."""

    values: list[Any] = [*explicit]
    for key in ("source_case_ids", "case_ids"):
        raw = evidence.get(key)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            values.extend(raw)
    values.extend((evidence.get("source_task_id"), evidence.get("case_id")))
    trace = evidence.get("query_run_trace")
    if isinstance(trace, Mapping):
        values.append(trace.get("task_id"))
    return tuple(
        list(
            dict.fromkeys(
                str(item or "").strip()[:200]
                for item in values
                if str(item or "").strip()
            )
        )[:50]
    )


def _memory_evidence_events(value: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if value.get("contract") == MEMORY_EVIDENCE_CONTRACT:
        sources = value.get("sources")
        if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes)):
            return [dict(item) for item in sources if isinstance(item, Mapping)][:50]
    return [dict(value)] if value else []


def _memory_evidence_bundle(
    existing: Mapping[str, Any],
    incoming: Mapping[str, Any],
    source_case_ids: Sequence[str],
) -> Mapping[str, Any]:
    """Merge identical rule occurrences without changing the reviewed semantics."""

    events = []
    seen = set()
    for item in [
        *_memory_evidence_events(existing),
        *_memory_evidence_events(incoming),
    ]:
        safe = sanitize_memory_evidence(item)
        if not isinstance(safe, Mapping):
            continue
        marker = hashlib.sha256(_canonical(safe).encode("utf-8")).hexdigest()
        if marker in seen:
            continue
        seen.add(marker)
        events.append(dict(safe))
        if len(events) >= 50:
            break
    return {
        "contract": MEMORY_EVIDENCE_CONTRACT,
        "source_case_ids": list(dict.fromkeys(str(item) for item in source_case_ids))[:50],
        "sources": events,
    }


def _decode_memory_row(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = dict(row)
    for column, public_name, fallback in (
        ("rule_json", "rule", {}),
        ("source_case_ids_json", "source_case_ids", []),
        ("evidence_json", "evidence", {}),
    ):
        if column not in value:
            continue
        try:
            decoded = json.loads(str(value.pop(column) or _canonical(fallback)))
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = fallback
        value[public_name] = decoded
    value["runtime_eligible"] = bool(value.get("runtime_eligible", False))
    value["state_version"] = max(1, int(value.get("state_version") or 1))
    rule = value.get("rule")
    if isinstance(rule, Mapping) and rule.get("contract") == EXPERIENCE_MEMORY_CONTRACT:
        # Lifecycle and identity columns are server-owned.  Project them into
        # the decoded contract without trusting a stale value in rule_json.
        normalized = decode_memory_payload(
            rule,
            memory_id=str(value.get("memory_id") or ""),
            state=str(value.get("state") or "candidate"),
        )
        value["rule"] = normalized
        value["target_agent"] = str(value.get("target_skill") or "")
        value["problem_code"] = str(value.get("failure_kind") or "")
        value["memory_kind"] = "experience"
    else:
        value["memory_kind"] = "legacy_rule"
    return value


_RATE_METRICS = {
    "execution_accuracy",
    "executable_rate",
    "ast_parse_rate",
    "readonly_safety_rate",
}
_SPLIT_METRICS = (*sorted(_RATE_METRICS), "p95_latency_ms")
_EXECUTION_ACCURACY_TOLERANCE = 1e-6
_MIN_CANDIDATE_OPERATIONAL_RATE = 1e-6
WORKING_MEMORY_RETENTION_PER_SESSION = 100
EPISODIC_MEMORY_RETENTION_PER_SESSION = 50
_NON_EXECUTABLE_NON_AST_FAILURE_KINDS = frozenset(
    {"NO_SQL", "PARSE_ERROR", "UNKNOWN_TABLE", "UNKNOWN_COLUMN", "TIMEOUT"}
)
_NON_EXECUTABLE_FAILURE_KINDS = frozenset(
    {
        "SCHEMA_LINK_MISMATCH",
        "FRAMEWORK_ERROR",
        "EXECUTION_ERROR",
        "PLANNING_FAILURE",
        "VALUE_GROUNDING_MISMATCH",
        "CANDIDATE_GENERATION_FAILURE",
        "CRITIC_REJECTION",
        "LEAD_SELECTION_FAILURE",
    }
)
_RESULT_FAILURE_KINDS = frozenset(
    {
        "FILTER_MISMATCH",
        "AGGREGATION_MISMATCH",
        "JOIN_OR_GRAIN_MISMATCH",
        "RESULT_MISMATCH",
    }
)


def _mapping(value: Any, reasons: list[str], path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        reasons.append("invalid_object_field:%s" % path)
        return {}
    return value


def _strict_bool(value: Any, reasons: list[str], path: str) -> bool:
    if type(value) is not bool:
        reasons.append("invalid_boolean_field:%s" % path)
        return False
    return value


def _strict_count(value: Any, reasons: list[str], path: str) -> Optional[int]:
    if type(value) is not int or value < 0:
        reasons.append("invalid_numeric_field:%s" % path)
        return None
    return value


def _strict_number(
    value: Any,
    reasons: list[str],
    path: str,
    *,
    rate: bool = False,
) -> Optional[float]:
    if (
        type(value) not in {int, float}
        or not math.isfinite(float(value))
        or float(value) < 0.0
        or (rate and float(value) > 1.0)
    ):
        reasons.append("invalid_numeric_field:%s" % path)
        return None
    return float(value)


def _expected_case_counts(
    manifest: Mapping[str, Any],
    splits: Sequence[str],
    reasons: list[str],
    *,
    required: bool,
) -> Mapping[str, Optional[int]]:
    raw_files = manifest.get("files")
    if raw_files is None and not required:
        return {split: None for split in splits}
    files = _mapping(raw_files, reasons, "manifest.files")
    values: dict[str, Optional[int]] = {}
    for split in splits:
        raw = files.get(split)
        if raw is None and not required:
            values[split] = None
            continue
        item = _mapping(raw, reasons, "manifest.files.%s" % split)
        values[split] = _strict_count(
            item.get("case_count"),
            reasons,
            "manifest.files.%s.case_count" % split,
        )
    return values


def _validated_split_metrics(
    report: Mapping[str, Any],
    splits: Sequence[str],
    label: str,
    reasons: list[str],
) -> Mapping[str, Mapping[str, Optional[float | int]]]:
    raw_splits = _mapping(report.get("splits"), reasons, "%s.splits" % label)
    values: dict[str, Mapping[str, Optional[float | int]]] = {}
    for split in splits:
        item = _mapping(
            raw_splits.get(split), reasons, "%s.splits.%s" % (label, split)
        )
        metrics: dict[str, Optional[float | int]] = {
            "cases": _strict_count(
                item.get("cases"), reasons, "%s.%s.cases" % (label, split)
            ),
            "framework_errors": _strict_count(
                item.get("framework_errors"),
                reasons,
                "%s.%s.framework_errors" % (label, split),
            ),
        }
        for name in _SPLIT_METRICS:
            metrics[name] = _strict_number(
                item.get(name),
                reasons,
                "%s.%s.%s" % (label, split, name),
                rate=name in _RATE_METRICS,
            )
        values[split] = metrics
    return values


def _outcome_matches_evaluator_semantics(
    values: Mapping[str, bool], failure_kind: str
) -> bool:
    accuracy = values["execution_accuracy"]
    safe = values["safe"]
    executable = values["executable"]
    ast_valid = values["ast_valid"]
    if accuracy:
        return safe and executable and ast_valid and failure_kind == ""
    if not failure_kind:
        return False
    if failure_kind == "UNSAFE_SQL":
        # A gated candidate can leave ``ast_valid`` true, but it can never be
        # executable after a deterministic read-only safety rejection.
        return not executable
    if failure_kind in _NON_EXECUTABLE_NON_AST_FAILURE_KINDS:
        return not executable and not ast_valid
    if failure_kind in _NON_EXECUTABLE_FAILURE_KINDS:
        return not executable
    if failure_kind == "UNEXPECTED_EMPTY":
        return safe and executable and ast_valid
    if failure_kind in _RESULT_FAILURE_KINDS:
        return not executable or (safe and ast_valid)
    # USER_CORRECTION is an experience-memory label, not an evaluator outcome.
    return False


def _validated_outcomes(
    report: Mapping[str, Any],
    splits: Sequence[str],
    label: str,
    reasons: list[str],
) -> Mapping[str, Mapping[str, Mapping[str, Any]]]:
    raw = report.get("outcomes")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        reasons.append("invalid_outcomes:%s" % label)
        return {split: {} for split in splits}
    expected_splits = set(splits)
    values: dict[str, dict[str, Mapping[str, Any]]] = {
        split: {} for split in splits
    }
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            reasons.append("invalid_outcome:%s:%d" % (label, index))
            continue
        split = item.get("split")
        case_id = item.get("case_id")
        if type(split) is not str or split not in expected_splits:
            reasons.append("invalid_outcome_split:%s:%d" % (label, index))
            continue
        if type(case_id) is not str or not case_id.strip():
            reasons.append("invalid_case_id:%s:%s:%d" % (label, split, index))
            continue
        case_id = case_id.strip()
        if case_id in seen:
            reasons.append("duplicate_case_id:%s:%s" % (label, case_id))
            continue
        seen.add(case_id)
        valid = True
        boolean_values: dict[str, bool] = {}
        for field in ("execution_accuracy", "safe", "executable", "ast_valid"):
            field_value = item.get(field)
            if type(field_value) is not bool:
                reasons.append(
                    "invalid_boolean_field:%s.outcomes.%s.%s"
                    % (label, case_id, field)
                )
                valid = False
            else:
                boolean_values[field] = field_value
        failure_kind = item.get("failure_kind")
        if (
            type(failure_kind) is not str
            or (failure_kind and failure_kind not in FAILURE_KINDS)
        ):
            reasons.append(
                "invalid_failure_kind:%s.outcomes.%s" % (label, case_id)
            )
            valid = False
        duration_ms = _strict_number(
            item.get("duration_ms"),
            reasons,
            "%s.outcomes.%s.duration_ms" % (label, case_id),
        )
        if duration_ms is None:
            valid = False
        sql_skeleton = item.get("sql_skeleton")
        if type(sql_skeleton) is not str:
            reasons.append(
                "invalid_sql_skeleton:%s.outcomes.%s" % (label, case_id)
            )
            valid = False
        if valid and not _outcome_matches_evaluator_semantics(
            boolean_values, failure_kind
        ):
            reasons.append(
                "invalid_outcome_invariant:%s.outcomes.%s" % (label, case_id)
            )
            valid = False
        if not valid:
            continue
        values[split][case_id] = {
            **boolean_values,
            "failure_kind": failure_kind,
            "duration_ms": duration_ms,
            "sql_skeleton": sql_skeleton,
        }
    return values


def _validated_skeleton_buckets(
    report: Mapping[str, Any], split: str, label: str, reasons: list[str]
) -> Mapping[str, Mapping[str, float | int]]:
    raw_splits = report.get("splits")
    split_value = raw_splits.get(split) if isinstance(raw_splits, Mapping) else None
    raw = split_value.get("skeleton_buckets") if isinstance(split_value, Mapping) else None
    if raw is None:
        reasons.append(
            "invalid_object_field:%s.%s.skeleton_buckets" % (label, split)
        )
        return {}
    buckets = _mapping(raw, reasons, "%s.%s.skeleton_buckets" % (label, split))
    values: dict[str, Mapping[str, float | int]] = {}
    for name, item in buckets.items():
        if type(name) is not str or not name:
            reasons.append("invalid_skeleton_name:%s:%s" % (label, split))
            continue
        bucket = _mapping(
            item, reasons, "%s.%s.skeleton_buckets.%s" % (label, split, name)
        )
        cases = _strict_count(
            bucket.get("cases"),
            reasons,
            "%s.%s.skeleton_buckets.%s.cases" % (label, split, name),
        )
        accuracy = _strict_number(
            bucket.get("execution_accuracy"),
            reasons,
            "%s.%s.skeleton_buckets.%s.execution_accuracy"
            % (label, split, name),
            rate=True,
        )
        if cases is not None and accuracy is not None:
            values[name] = {"cases": cases, "execution_accuracy": accuracy}
    return values


def _check_case_coverage(
    split: str,
    expected: Optional[int],
    baseline_cases: Optional[int],
    candidate_cases: Optional[int],
    baseline_outcomes: Mapping[str, Mapping[str, Any]],
    candidate_outcomes: Mapping[str, Mapping[str, Any]],
    reasons: list[str],
) -> None:
    if expected is not None and (
        baseline_cases != expected or candidate_cases != expected
    ):
        reasons.append("%s_incomplete" % split)
    if (
        baseline_cases is None
        or candidate_cases is None
        or len(baseline_outcomes) != baseline_cases
        or len(candidate_outcomes) != candidate_cases
    ):
        reasons.append("%s_outcome_coverage_mismatch" % split)
    if set(baseline_outcomes) != set(candidate_outcomes):
        reasons.append("%s_case_set_mismatch" % split)


def _aggregate_outcomes(
    outcomes: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    values = list(outcomes.values())
    total = len(values)
    denominator = max(1, total)
    failures = Counter(
        str(item["failure_kind"])
        for item in values
        if item["failure_kind"]
    )
    buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in values:
        buckets[str(item["sql_skeleton"])].append(item)
    return {
        "cases": total,
        "execution_accuracy": round(
            sum(item["execution_accuracy"] for item in values) / denominator, 6
        ),
        "executable_rate": round(
            sum(item["executable"] for item in values) / denominator, 6
        ),
        "ast_parse_rate": round(
            sum(item["ast_valid"] for item in values) / denominator, 6
        ),
        "readonly_safety_rate": round(
            sum(item["safe"] for item in values) / denominator, 6
        ),
        "framework_errors": int(failures.get("FRAMEWORK_ERROR", 0)),
        "p95_latency_ms": _evaluation_percentile(
            [float(item["duration_ms"]) for item in values], 0.95
        ),
        "skeleton_buckets": {
            name: {
                "cases": len(items),
                "execution_accuracy": round(
                    sum(item["execution_accuracy"] for item in items) / len(items),
                    6,
                ),
            }
            for name, items in sorted(buckets.items())
        },
    }


def _check_declared_split_metrics(
    split: str,
    label: str,
    declared: Mapping[str, Optional[float | int]],
    declared_buckets: Mapping[str, Mapping[str, float | int]],
    recomputed: Mapping[str, Any],
    reasons: list[str],
) -> None:
    for name in ("cases", "framework_errors"):
        value = declared.get(name)
        if value is not None and value != recomputed[name]:
            reasons.append("%s_%s_%s_aggregate_mismatch" % (split, label, name))
    for name in _RATE_METRICS:
        value = declared.get(name)
        if value is not None and not math.isclose(
            float(value),
            float(recomputed[name]),
            rel_tol=0.0,
            abs_tol=_EXECUTION_ACCURACY_TOLERANCE,
        ):
            reasons.append("%s_%s_%s_aggregate_mismatch" % (split, label, name))
    latency = declared.get("p95_latency_ms")
    if latency is not None and not math.isclose(
        float(latency),
        float(recomputed["p95_latency_ms"]),
        rel_tol=0.0,
        abs_tol=1e-3,
    ):
        reasons.append("%s_%s_p95_latency_ms_aggregate_mismatch" % (split, label))
    recomputed_buckets = recomputed["skeleton_buckets"]
    if set(declared_buckets) != set(recomputed_buckets):
        reasons.append("%s_%s_skeleton_set_aggregate_mismatch" % (split, label))
    for name in set(declared_buckets).intersection(recomputed_buckets):
        declared_bucket = declared_buckets[name]
        recomputed_bucket = recomputed_buckets[name]
        if declared_bucket["cases"] != recomputed_bucket["cases"]:
            reasons.append(
                "%s_%s_skeleton_cases_aggregate_mismatch:%s"
                % (split, label, name)
            )
        if not math.isclose(
            float(declared_bucket["execution_accuracy"]),
            float(recomputed_bucket["execution_accuracy"]),
            rel_tol=0.0,
            abs_tol=_EXECUTION_ACCURACY_TOLERANCE,
        ):
            reasons.append(
                "%s_%s_skeleton_accuracy_aggregate_mismatch:%s"
                % (split, label, name)
            )


def _check_declared_overall_accuracy(
    report: Mapping[str, Any],
    label: str,
    recomputed: Mapping[str, Any],
    reasons: list[str],
) -> None:
    overall = _mapping(report.get("overall"), reasons, "%s.overall" % label)
    declared = _strict_number(
        overall.get("execution_accuracy"),
        reasons,
        "%s.overall.execution_accuracy" % label,
        rate=True,
    )
    if declared is not None and not math.isclose(
        declared,
        float(recomputed["execution_accuracy"]),
        rel_tol=0.0,
        abs_tol=_EXECUTION_ACCURACY_TOLERANCE,
    ):
        reasons.append("%s_overall_execution_accuracy_aggregate_mismatch" % label)


def _check_candidate_operational_floor(
    split: str, recomputed: Mapping[str, Any], reasons: list[str]
) -> None:
    for name in ("executable_rate", "ast_parse_rate"):
        if float(recomputed[name]) < _MIN_CANDIDATE_OPERATIONAL_RATE:
            reasons.append("%s_%s_operational_rate_zero" % (split, name))


def _fixed_regressed(
    baseline: Mapping[str, Mapping[str, Any]],
    candidate: Mapping[str, Mapping[str, Any]],
) -> tuple[int, int]:
    shared = set(baseline).intersection(candidate)
    fixed = sum(
        not baseline[key]["execution_accuracy"]
        and candidate[key]["execution_accuracy"]
        for key in shared
    )
    regressed = sum(
        baseline[key]["execution_accuracy"]
        and not candidate[key]["execution_accuracy"]
        for key in shared
    )
    return fixed, regressed


def evaluate_promotion_gate(
    dataset_manifest: Mapping[str, Any],
    baseline_report: Mapping[str, Any],
    candidate_report: Mapping[str, Any],
    dataset_review_evidence: Optional[Mapping[str, Any]] = None,
) -> Mapping[str, Any]:
    """Compare two frozen runs. Holdout can veto but never justify a weak candidate."""

    reasons: list[str] = []
    splits = ("validation", "sealed_holdout")
    release_eligible = _strict_bool(
        dataset_manifest.get("release_eligible"),
        reasons,
        "manifest.release_eligible",
    )
    if not release_eligible:
        reasons.append("dataset_not_human_reviewed")
    expected_cases = _expected_case_counts(
        dataset_manifest, splits, reasons, required=False
    )
    expected_total = sum(value or 0 for value in expected_cases.values())
    evidence = _mapping(
        dataset_review_evidence or {}, reasons, "dataset_review_evidence"
    )
    if release_eligible:
        human_reviewed_cases = _strict_count(
            dataset_manifest.get("human_reviewed_cases"),
            reasons,
            "manifest.human_reviewed_cases",
        )
        if (
            dataset_manifest.get("review_status") not in {"human_reviewed", "approved"}
            or human_reviewed_cases != expected_total
        ):
            reasons.append("dataset_review_evidence_incomplete")
        verified = _strict_bool(
            evidence.get("verified"), reasons, "review.verified"
        )
        reviewed_case_count = _strict_count(
            evidence.get("reviewed_case_count"),
            reasons,
            "review.reviewed_case_count",
        )
        if (
            not verified
            or evidence.get("dataset_sha256") != dataset_manifest.get("dataset_sha256")
            or reviewed_case_count != expected_total
            or not evidence.get("certificate_sha256")
        ):
            reasons.append("dataset_review_certificate_unverified")

    baseline_pins = _mapping(
        baseline_report.get("version_pins"), reasons, "baseline.version_pins"
    )
    candidate_pins = _mapping(
        candidate_report.get("version_pins"), reasons, "candidate.version_pins"
    )
    for name in ("database_snapshot_id", "wiki_index_version", "memory_snapshot_id"):
        if baseline_pins.get(name) != candidate_pins.get(name):
            reasons.append("%s_mismatch" % name)
    if dataset_manifest.get("database_snapshot_id") and baseline_pins.get(
        "database_snapshot_id"
    ) != dataset_manifest.get("database_snapshot_id"):
        reasons.append("dataset_database_snapshot_mismatch")

    baseline_metrics = _validated_split_metrics(
        baseline_report, splits, "baseline", reasons
    )
    candidate_metrics = _validated_split_metrics(
        candidate_report, splits, "candidate", reasons
    )
    baseline_outcomes = _validated_outcomes(
        baseline_report, splits, "baseline", reasons
    )
    candidate_outcomes = _validated_outcomes(
        candidate_report, splits, "candidate", reasons
    )
    baseline_recomputed = {
        split: _aggregate_outcomes(baseline_outcomes[split]) for split in splits
    }
    candidate_recomputed = {
        split: _aggregate_outcomes(candidate_outcomes[split]) for split in splits
    }
    baseline_declared_buckets = {
        split: _validated_skeleton_buckets(
            baseline_report, split, "baseline", reasons
        )
        for split in splits
    }
    candidate_declared_buckets = {
        split: _validated_skeleton_buckets(
            candidate_report, split, "candidate", reasons
        )
        for split in splits
    }
    for split in splits:
        _check_declared_split_metrics(
            split,
            "baseline",
            baseline_metrics[split],
            baseline_declared_buckets[split],
            baseline_recomputed[split],
            reasons,
        )
        _check_declared_split_metrics(
            split,
            "candidate",
            candidate_metrics[split],
            candidate_declared_buckets[split],
            candidate_recomputed[split],
            reasons,
        )

    validation_gain = float(
        candidate_recomputed["validation"]["execution_accuracy"]
    ) - float(baseline_recomputed["validation"]["execution_accuracy"])
    fixed, regressed = _fixed_regressed(
        baseline_outcomes["validation"], candidate_outcomes["validation"]
    )
    net_fixed = fixed - regressed
    if validation_gain < 0.02:
        reasons.append("validation_execution_gain_below_0.02")
    if net_fixed < 5:
        reasons.append("validation_net_fixed_below_5")

    for split in splits:
        expected = expected_cases.get(split)
        _check_case_coverage(
            split,
            expected,
            baseline_recomputed[split]["cases"],
            candidate_recomputed[split]["cases"],
            baseline_outcomes[split],
            candidate_outcomes[split],
            reasons,
        )
        _check_candidate_operational_floor(
            split, candidate_recomputed[split], reasons
        )
        if candidate_recomputed[split]["readonly_safety_rate"] < 1.0:
            reasons.append("%s_safety_below_1.0" % split)
        if candidate_recomputed[split]["framework_errors"]:
            reasons.append("%s_framework_errors" % split)
        for name in ("executable_rate", "ast_parse_rate"):
            if (
                candidate_recomputed[split][name] + 0.01
                < baseline_recomputed[split][name]
            ):
                reasons.append("%s_%s_regression" % (split, name))
        baseline_p95 = float(baseline_recomputed[split]["p95_latency_ms"])
        candidate_p95 = float(candidate_recomputed[split]["p95_latency_ms"])
        if baseline_p95 > 0 and candidate_p95 > baseline_p95 * 1.2:
            reasons.append("%s_p95_latency_over_20_percent" % split)

        base_buckets = baseline_recomputed[split]["skeleton_buckets"]
        cand_buckets = candidate_recomputed[split]["skeleton_buckets"]
        if set(base_buckets) != set(cand_buckets):
            reasons.append("%s_skeleton_set_mismatch" % split)
        for skeleton in set(base_buckets).intersection(cand_buckets):
            base_accuracy = float(base_buckets[skeleton]["execution_accuracy"])
            cand_accuracy = float(cand_buckets[skeleton]["execution_accuracy"])
            if cand_accuracy + 0.03 < base_accuracy:
                reasons.append("%s_skeleton_regression:%s" % (split, skeleton))

    holdout_gain = float(
        candidate_recomputed["sealed_holdout"]["execution_accuracy"]
    ) - float(baseline_recomputed["sealed_holdout"]["execution_accuracy"])
    holdout_fixed, holdout_regressed = _fixed_regressed(
        baseline_outcomes["sealed_holdout"],
        candidate_outcomes["sealed_holdout"],
    )
    if holdout_regressed:
        reasons.append("sealed_holdout_case_regression")
    if holdout_gain < 0:
        reasons.append("sealed_holdout_execution_regression")

    return {
        "eligible_for_human_approval": not reasons,
        "reasons": sorted(set(reasons)),
        "dataset_review": {
            key: value
            for key, value in evidence.items()
            if key in {
                "verified",
                "certificate_kind",
                "certificate_sha256",
                "key_id",
                "reviewed_case_count",
                "dataset_sha256",
                "chain_head",
            }
        },
        "thresholds": {
            "validation_execution_gain": 0.02,
            "validation_net_fixed": 5,
            "minimum_safety_rate": 1.0,
            "minimum_candidate_executable_rate": _MIN_CANDIDATE_OPERATIONAL_RATE,
            "minimum_candidate_ast_parse_rate": _MIN_CANDIDATE_OPERATIONAL_RATE,
            "maximum_executable_or_ast_drop": 0.01,
            "maximum_skeleton_drop": 0.03,
            "maximum_p95_latency_multiplier": 1.2,
            "sealed_holdout_execution_drop": 0.0,
            "maximum_sealed_holdout_regressed_cases": 0,
        },
        "observed": {
            "validation_execution_gain": round(validation_gain, 6),
            "validation_fixed": fixed,
            "validation_regressed": regressed,
            "validation_net_fixed": net_fixed,
            "sealed_holdout_execution_gain": round(holdout_gain, 6),
            "sealed_holdout_fixed": holdout_fixed,
            "sealed_holdout_regressed": holdout_regressed,
        },
    }


def evaluate_memory_promotion_gate(
    dataset_manifest: Mapping[str, Any],
    baseline_report: Mapping[str, Any],
    candidate_report: Mapping[str, Any],
    dataset_review_evidence: Optional[Mapping[str, Any]] = None,
) -> Mapping[str, Any]:
    """Require a complete 240-case non-regression before memory activation."""

    reasons: list[str] = []
    splits = ("train", "validation", "sealed_holdout")
    expected_counts = _expected_case_counts(
        dataset_manifest, splits, reasons, required=True
    )
    expected_total = sum(value or 0 for value in expected_counts.values())
    if expected_total != 240:
        reasons.append("trusted_dataset_must_contain_240_cases")
    if not _strict_bool(
        dataset_manifest.get("release_eligible"),
        reasons,
        "manifest.release_eligible",
    ):
        reasons.append("dataset_not_human_reviewed")
    evidence = _mapping(
        dataset_review_evidence or {}, reasons, "dataset_review_evidence"
    )
    verified = _strict_bool(evidence.get("verified"), reasons, "review.verified")
    reviewed_case_count = _strict_count(
        evidence.get("reviewed_case_count"),
        reasons,
        "review.reviewed_case_count",
    )
    if (
        not verified
        or evidence.get("dataset_sha256") != dataset_manifest.get("dataset_sha256")
        or reviewed_case_count != expected_total
        or not evidence.get("certificate_sha256")
    ):
        reasons.append("dataset_review_certificate_unverified")

    baseline_pins = _mapping(
        baseline_report.get("version_pins"), reasons, "baseline.version_pins"
    )
    candidate_pins = _mapping(
        candidate_report.get("version_pins"), reasons, "candidate.version_pins"
    )
    for name in (
        "database_snapshot_id",
        "wiki_index_version",
        "vanna_index_version",
        "policy_version",
    ):
        if baseline_pins.get(name) != candidate_pins.get(name):
            reasons.append("%s_mismatch" % name)
    if baseline_pins.get("memory_snapshot_id") == candidate_pins.get(
        "memory_snapshot_id"
    ):
        reasons.append("candidate_memory_snapshot_not_changed")

    baseline_metrics = _validated_split_metrics(
        baseline_report, splits, "baseline", reasons
    )
    candidate_metrics = _validated_split_metrics(
        candidate_report, splits, "candidate", reasons
    )
    baseline_outcomes = _validated_outcomes(
        baseline_report, splits, "baseline", reasons
    )
    candidate_outcomes = _validated_outcomes(
        candidate_report, splits, "candidate", reasons
    )
    baseline_recomputed = {
        split: _aggregate_outcomes(baseline_outcomes[split]) for split in splits
    }
    candidate_recomputed = {
        split: _aggregate_outcomes(candidate_outcomes[split]) for split in splits
    }
    for split in splits:
        _check_declared_split_metrics(
            split,
            "baseline",
            baseline_metrics[split],
            _validated_skeleton_buckets(
                baseline_report, split, "baseline", reasons
            ),
            baseline_recomputed[split],
            reasons,
        )
        _check_declared_split_metrics(
            split,
            "candidate",
            candidate_metrics[split],
            _validated_skeleton_buckets(
                candidate_report, split, "candidate", reasons
            ),
            candidate_recomputed[split],
            reasons,
        )
    baseline_all = {
        case_id: outcome
        for split in splits
        for case_id, outcome in baseline_outcomes[split].items()
    }
    candidate_all = {
        case_id: outcome
        for split in splits
        for case_id, outcome in candidate_outcomes[split].items()
    }
    baseline_overall_recomputed = _aggregate_outcomes(baseline_all)
    candidate_overall_recomputed = _aggregate_outcomes(candidate_all)
    _check_declared_overall_accuracy(
        baseline_report, "baseline", baseline_overall_recomputed, reasons
    )
    _check_declared_overall_accuracy(
        candidate_report, "candidate", candidate_overall_recomputed, reasons
    )

    total_fixed = 0
    total_regressed = 0
    for split, expected in expected_counts.items():
        _check_case_coverage(
            split,
            expected,
            baseline_recomputed[split]["cases"],
            candidate_recomputed[split]["cases"],
            baseline_outcomes[split],
            candidate_outcomes[split],
            reasons,
        )
        _check_candidate_operational_floor(
            split, candidate_recomputed[split], reasons
        )
        if candidate_recomputed[split]["readonly_safety_rate"] < 1.0:
            reasons.append("%s_safety_below_1.0" % split)
        if candidate_recomputed[split]["framework_errors"]:
            reasons.append("%s_framework_errors" % split)
        if (
            candidate_recomputed[split]["execution_accuracy"]
            < baseline_recomputed[split]["execution_accuracy"]
        ):
            reasons.append("%s_execution_regression" % split)
        for name in ("executable_rate", "ast_parse_rate"):
            if (
                candidate_recomputed[split][name] + 0.01
                < baseline_recomputed[split][name]
            ):
                reasons.append("%s_%s_regression" % (split, name))
        baseline_p95 = float(baseline_recomputed[split]["p95_latency_ms"])
        candidate_p95 = float(candidate_recomputed[split]["p95_latency_ms"])
        if baseline_p95 > 0 and candidate_p95 > baseline_p95 * 1.2:
            reasons.append("%s_p95_latency_over_20_percent" % split)
        fixed, regressed = _fixed_regressed(
            baseline_outcomes[split], candidate_outcomes[split]
        )
        total_fixed += fixed
        total_regressed += regressed
    if total_regressed:
        reasons.append("memory_candidate_regressed_cases")

    return {
        "eligible_for_activation": not reasons,
        "reasons": sorted(set(reasons)),
        "dataset_case_count": expected_total,
        "observed": {
            "fixed": total_fixed,
            "regressed": total_regressed,
            "baseline_execution_accuracy": baseline_overall_recomputed[
                "execution_accuracy"
            ],
            "candidate_execution_accuracy": candidate_overall_recomputed[
                "execution_accuracy"
            ],
        },
        "thresholds": {
            "required_case_count": 240,
            "minimum_safety_rate": 1.0,
            "minimum_candidate_executable_rate": _MIN_CANDIDATE_OPERATIONAL_RATE,
            "minimum_candidate_ast_parse_rate": _MIN_CANDIDATE_OPERATIONAL_RATE,
            "maximum_execution_regression": 0.0,
            "maximum_executable_or_ast_drop": 0.01,
            "maximum_p95_latency_multiplier": 1.2,
            "maximum_regressed_cases": 0,
        },
    }




class Text2SQLEvolutionStore(SemanticRuleStoreMixin):
    """SQLite control plane; model output is never allowed to approve itself."""

    def __init__(self, path: Path, snapshot: Mapping[str, Any]) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot = snapshot
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._initialize()
        self._initialize_semantic_rules()
        stored = self._metadata("database_snapshot_id")
        if stored and stored != snapshot["snapshot_id"]:
            raise ValueError("evolution store belongs to a different database snapshot")
        if not stored:
            self._set_metadata("database_snapshot_id", str(snapshot["snapshot_id"]))
        self.bootstrap()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Text2SQLEvolutionStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS evolution_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS policy_versions (
                policy_version TEXT PRIMARY KEY,
                parent_version TEXT NOT NULL,
                target_skill TEXT NOT NULL,
                artifact_json TEXT NOT NULL,
                status TEXT NOT NULL,
                change_reason TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reviewed_by TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT NOT NULL DEFAULT '',
                proposal_metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS evolution_runs (
                run_id TEXT PRIMARY KEY,
                baseline_policy_version TEXT NOT NULL,
                candidate_policy_version TEXT NOT NULL,
                dataset_id TEXT NOT NULL,
                dataset_sha256 TEXT NOT NULL,
                baseline_aggregate_json TEXT NOT NULL,
                candidate_aggregate_json TEXT NOT NULL,
                decision_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS policy_target_replays (
                replay_id TEXT PRIMARY KEY,
                candidate_policy_version TEXT NOT NULL,
                parent_policy_version TEXT NOT NULL,
                memory_ids_json TEXT NOT NULL,
                artifact_json TEXT NOT NULL,
                artifact_sha256 TEXT NOT NULL,
                artifact_path TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(candidate_policy_version, artifact_sha256)
            );
            CREATE TABLE IF NOT EXISTS memory_items (
                memory_id TEXT PRIMARY KEY,
                target_skill TEXT NOT NULL,
                origin_split TEXT NOT NULL,
                failure_kind TEXT NOT NULL,
                content TEXT NOT NULL,
                rule_json TEXT NOT NULL DEFAULT '{}',
                rule_fingerprint TEXT NOT NULL DEFAULT '',
                source_case_ids_json TEXT NOT NULL DEFAULT '[]',
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                evidence_json TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reviewed_by TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT NOT NULL DEFAULT '',
                review_note TEXT NOT NULL DEFAULT '',
                source_task_id TEXT NOT NULL DEFAULT '',
                source_stage TEXT NOT NULL DEFAULT '',
                source_revision INTEGER NOT NULL DEFAULT 1,
                evidence_sha256 TEXT NOT NULL DEFAULT '',
                runtime_eligible INTEGER NOT NULL DEFAULT 0,
                state_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS memory_evaluation_jobs (
                job_id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                status TEXT NOT NULL,
                phase TEXT NOT NULL,
                progress_current INTEGER NOT NULL DEFAULT 0,
                progress_total INTEGER NOT NULL DEFAULT 240,
                baseline_artifact TEXT NOT NULL DEFAULT '',
                candidate_artifact TEXT NOT NULL DEFAULT '',
                log_path TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                requested_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_evaluation_runs (
                run_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                dataset_id TEXT NOT NULL,
                dataset_sha256 TEXT NOT NULL,
                baseline_aggregate_json TEXT NOT NULL,
                candidate_aggregate_json TEXT NOT NULL,
                decision_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_activation_audit (
                event_id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activation_audit (
                event_id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                previous_policy_version TEXT NOT NULL,
                new_policy_version TEXT NOT NULL,
                actor TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS query_traces (
                task_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                question TEXT NOT NULL,
                final_sql TEXT NOT NULL,
                gates_json TEXT NOT NULL,
                agents_json TEXT NOT NULL,
                execution_json TEXT NOT NULL,
                version_pins_json TEXT NOT NULL,
                answer_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                origin TEXT NOT NULL DEFAULT 'legacy',
                source_lane TEXT NOT NULL DEFAULT 'stable',
                source_revision INTEGER NOT NULL DEFAULT 1,
                draft_link_pack_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS query_trace_revisions (
                task_id TEXT NOT NULL,
                source_revision INTEGER NOT NULL,
                trace_json TEXT NOT NULL,
                trace_sha256 TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY(task_id, source_revision)
            );
            CREATE TABLE IF NOT EXISTS query_decisions (
                decision_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                decision_source TEXT NOT NULL,
                decision_type TEXT NOT NULL,
                outcome TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                reason_text TEXT NOT NULL,
                actor TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS query_attempts (
                task_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                question TEXT NOT NULL,
                request_sha256 TEXT NOT NULL DEFAULT '',
                context_json TEXT NOT NULL DEFAULT '{}',
                context_sha256 TEXT NOT NULL DEFAULT '',
                response_json TEXT NOT NULL DEFAULT '{}',
                response_sha256 TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS memory_messages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                task_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS experience_reviews (
                experience_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                question TEXT NOT NULL,
                sql TEXT NOT NULL,
                sql_fingerprint TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                eligible INTEGER NOT NULL,
                eligibility_reasons_json TEXT NOT NULL,
                state TEXT NOT NULL,
                user_feedback TEXT NOT NULL DEFAULT '',
                feedback_note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                reviewed_by TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT NOT NULL DEFAULT '',
                review_note TEXT NOT NULL DEFAULT '',
                knowledge_evidence_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_memory_state_skill
                ON memory_items(state, target_skill);
            CREATE INDEX IF NOT EXISTS idx_query_traces_recorded_at
                ON query_traces(recorded_at DESC);
            CREATE INDEX IF NOT EXISTS idx_query_trace_revisions_task_revision
                ON query_trace_revisions(task_id,source_revision DESC);
            CREATE INDEX IF NOT EXISTS idx_query_decisions_task_source_time
                ON query_decisions(task_id,decision_source,created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_memory_evaluation_memory_time
                ON memory_evaluation_jobs(memory_id,created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_policy_target_replay_candidate_time
                ON policy_target_replays(candidate_policy_version,created_at DESC);
            """
        )
        policy_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(policy_versions)").fetchall()
        }
        if "proposal_metadata_json" not in policy_columns:
            self.connection.execute(
                "ALTER TABLE policy_versions ADD COLUMN proposal_metadata_json TEXT NOT NULL DEFAULT '{}'"
            )
        trace_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(query_traces)").fetchall()
        }
        trace_migrations = {
            "user_id": "TEXT NOT NULL DEFAULT 'local-user'",
            "session_id": "TEXT NOT NULL DEFAULT 'default'",
            "original_question": "TEXT NOT NULL DEFAULT ''",
            "standalone_question": "TEXT NOT NULL DEFAULT ''",
            "query_type": "TEXT NOT NULL DEFAULT 'DATA_QUERY'",
            "parent_task_id": "TEXT NOT NULL DEFAULT ''",
            "schema_plan_json": "TEXT NOT NULL DEFAULT '{}'",
            "query_spec_json": "TEXT NOT NULL DEFAULT '{}'",
            "collaboration_json": "TEXT NOT NULL DEFAULT '{}'",
            "retrieval_json": "TEXT NOT NULL DEFAULT '[]'",
            "result_rows_json": "TEXT NOT NULL DEFAULT '[]'",
            "feedback_status": "TEXT NOT NULL DEFAULT ''",
            "origin": "TEXT NOT NULL DEFAULT 'legacy'",
            "source_lane": "TEXT NOT NULL DEFAULT 'stable'",
            "source_revision": "INTEGER NOT NULL DEFAULT 1",
            "draft_link_pack_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for column, definition in trace_migrations.items():
            if column not in trace_columns:
                self.connection.execute(
                    "ALTER TABLE query_traces ADD COLUMN %s %s" % (column, definition)
                )
        # ``query_traces`` remains the latest-read projection for existing UI
        # queries.  This append-only table is the canonical source used by
        # Experience replay, so a later source revision can never erase the
        # exact QueryRun that justified an earlier Experience.
        for row in self.connection.execute("SELECT * FROM query_traces").fetchall():
            trace = _decode_query_trace_row(row)
            trace.setdefault("policy_source_memory_ids", [])
            revision = max(1, int(trace.get("source_revision") or 1))
            self.connection.execute(
                "INSERT OR IGNORE INTO query_trace_revisions("
                "task_id,source_revision,trace_json,trace_sha256,recorded_at) "
                "VALUES (?,?,?,?,?)",
                (
                    str(trace.get("task_id") or "")[:200],
                    revision,
                    _canonical(trace),
                    _query_trace_evidence_sha256(trace),
                    str(trace.get("recorded_at") or _now())[:100],
                ),
            )
        memory_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(memory_items)").fetchall()
        }
        if "review_note" not in memory_columns:
            self.connection.execute(
                "ALTER TABLE memory_items ADD COLUMN review_note TEXT NOT NULL DEFAULT ''"
            )
        memory_migrations = {
            "rule_json": "TEXT NOT NULL DEFAULT '{}'",
            "rule_fingerprint": "TEXT NOT NULL DEFAULT ''",
            "source_case_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "occurrence_count": "INTEGER NOT NULL DEFAULT 1",
            "source_task_id": "TEXT NOT NULL DEFAULT ''",
            "source_stage": "TEXT NOT NULL DEFAULT ''",
            "source_revision": "INTEGER NOT NULL DEFAULT 1",
            "evidence_sha256": "TEXT NOT NULL DEFAULT ''",
            "runtime_eligible": "INTEGER NOT NULL DEFAULT 0",
            "state_version": "INTEGER NOT NULL DEFAULT 1",
        }
        for column, definition in memory_migrations.items():
            if column not in memory_columns:
                self.connection.execute(
                    "ALTER TABLE memory_items ADD COLUMN %s %s" % (column, definition)
                )
        experience_columns = {
            str(row["name"])
            for row in self.connection.execute(
                "PRAGMA table_info(experience_reviews)"
            ).fetchall()
        }
        if "review_note" not in experience_columns:
            self.connection.execute(
                "ALTER TABLE experience_reviews ADD COLUMN review_note TEXT NOT NULL DEFAULT ''"
            )
        attempt_columns = {
            str(row["name"])
            for row in self.connection.execute(
                "PRAGMA table_info(query_attempts)"
            ).fetchall()
        }
        attempt_migrations = {
            "request_sha256": "TEXT NOT NULL DEFAULT ''",
            "context_json": "TEXT NOT NULL DEFAULT '{}'",
            "context_sha256": "TEXT NOT NULL DEFAULT ''",
            "response_json": "TEXT NOT NULL DEFAULT '{}'",
            "response_sha256": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in attempt_migrations.items():
            if column not in attempt_columns:
                self.connection.execute(
                    "ALTER TABLE query_attempts ADD COLUMN %s %s"
                    % (column, definition)
                )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_query_traces_session_time "
            "ON query_traces(user_id,session_id,recorded_at DESC)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_query_attempts_session_time "
            "ON query_attempts(user_id,session_id,created_at DESC)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_messages_session_time "
            "ON memory_messages(user_id,session_id,message_id DESC)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_experience_state_time "
            "ON experience_reviews(state,created_at DESC)"
        )
        # Protocol v2 split the former sql-strategy memory slot into logical
        # Planning and physical SQL Generation.  Preserve content/provenance
        # while moving each reviewed item to its new single owner.
        self.connection.execute(
            "UPDATE memory_items SET target_skill=CASE "
            "WHEN failure_kind IN ('sql_gate_failure','sql_plan_conformance_mismatch') "
            "THEN 'sql-generation' "
            "WHEN failure_kind IN ('schema_link_mismatch','value_binding_mismatch',"
            "'join_semantics_mismatch') THEN 'schema-grounding' "
            "WHEN failure_kind='final_selection_mismatch' THEN 'text2sql-lead' "
            "WHEN failure_kind='critic_false_accept' THEN 'text2sql-critic' "
            "ELSE 'query-planning' END WHERE target_skill='sql-strategy'"
        )
        # Upgrade legacy free-text rows to the structured semantic-rule
        # contract.  The old content remains available as a compatibility
        # rendering, while runtime behavior is hashed from the rule fields.
        for row in self.connection.execute(
            "SELECT memory_id,target_skill,failure_kind,content,rule_json,"
            "source_case_ids_json,evidence_json,occurrence_count,state,"
            "source_task_id,source_stage,source_revision,evidence_sha256,"
            "runtime_eligible,state_version FROM memory_items"
        ).fetchall():
            try:
                raw_rule = json.loads(str(row["rule_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                raw_rule = {}
            try:
                raw_evidence = json.loads(str(row["evidence_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                raw_evidence = {}
            if not isinstance(raw_evidence, Mapping):
                raw_evidence = {}
            try:
                stored_sources = json.loads(
                    str(row["source_case_ids_json"] or "[]")
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                stored_sources = []
            if not isinstance(stored_sources, list):
                stored_sources = []
            if (
                isinstance(raw_rule, Mapping)
                and raw_rule.get("contract") == EXPERIENCE_MEMORY_CONTRACT
            ):
                experience = normalize_experience_memory(
                    raw_rule,
                    memory_id=str(row["memory_id"]),
                    state=str(row["state"]),
                )
                evidence = experience_evidence_payload(experience)
                evidence_sha256 = experience_evidence_sha256(experience)
                fingerprint = experience_memory_fingerprint(experience)
                self.connection.execute(
                    "UPDATE memory_items SET target_skill=?,failure_kind=?,content=?,"
                    "rule_json=?,rule_fingerprint=?,source_case_ids_json='[]',"
                    "occurrence_count=1,evidence_json=?,source_task_id=?,source_stage=?,"
                    "source_revision=?,evidence_sha256=?,runtime_eligible=0,"
                    "state_version=? WHERE memory_id=?",
                    (
                        experience["target_agent"],
                        experience["problem_code"],
                        render_experience_memory(experience),
                        _canonical(experience),
                        fingerprint,
                        _canonical(evidence),
                        experience["source_task_id"],
                        experience["source_stage"],
                        experience["source_revision"],
                        evidence_sha256,
                        max(1, int(row["state_version"] or 1)),
                        row["memory_id"],
                    ),
                )
                continue

            source_case_ids = _memory_source_case_ids(
                raw_evidence, tuple(str(item) for item in stored_sources)
            )
            rule = normalize_memory_rule(
                raw_rule if isinstance(raw_rule, Mapping) else {},
                failure_kind=str(row["failure_kind"]),
                content=str(row["content"]),
                source_case_ids=list(source_case_ids),
            )
            fingerprint = memory_rule_fingerprint(
                str(row["target_skill"]), str(row["failure_kind"]), rule
            )
            evidence = _memory_evidence_bundle({}, raw_evidence, source_case_ids)
            compatibility_content = (
                str(row["content"])
                if isinstance(raw_rule, Mapping)
                and raw_rule.get("contract") == rule["contract"]
                else render_memory_rule(rule)
            )
            self.connection.execute(
                "UPDATE memory_items SET content=?,rule_json=?,rule_fingerprint=?,"
                "source_case_ids_json=?,occurrence_count=?,evidence_json=?,"
                "runtime_eligible=1,state_version=? WHERE memory_id=?",
                (
                    compatibility_content,
                    _canonical(rule),
                    fingerprint,
                    _canonical(source_case_ids),
                    max(1, int(row["occurrence_count"] or 1)),
                    _canonical(evidence),
                    max(1, int(row["state_version"] or 1)),
                    row["memory_id"],
                ),
            )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_rule_state ON "
            "memory_items(target_skill,rule_fingerprint,state)"
        )
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_experience_source_evidence "
            "ON memory_items(source_task_id,failure_kind,evidence_sha256) "
            "WHERE source_task_id<>'' AND evidence_sha256<>''"
        )
        # Backfill source-aware decisions for historical QueryRuns. Deterministic
        # ids make this migration safe to run on every startup.
        for row in self.connection.execute(
            "SELECT task_id,status,final_sql,gates_json,recorded_at FROM query_traces"
        ).fetchall():
            self._store_harness_decision(
                str(row["task_id"]),
                str(row["status"]),
                str(row["final_sql"]),
                json.loads(row["gates_json"]),
                str(row["recorded_at"]),
            )
        for row in self.connection.execute(
            "SELECT q.task_id,q.feedback_status,q.recorded_at,"
            "COALESCE(e.feedback_note,'') AS feedback_note "
            "FROM query_traces q LEFT JOIN experience_reviews e ON e.task_id=q.task_id "
            "WHERE q.feedback_status IN ('correct','incorrect') "
            "GROUP BY q.task_id"
        ).fetchall():
            exists = self.connection.execute(
                "SELECT 1 FROM query_decisions WHERE task_id=? "
                "AND decision_source='human' LIMIT 1",
                (row["task_id"],),
            ).fetchone()
            if not exists:
                self._store_human_decision(
                    str(row["task_id"]),
                    str(row["feedback_status"]),
                    str(row["feedback_note"])
                    or (
                        "历史记录未保存拒绝理由。"
                        if row["feedback_status"] == "incorrect"
                        else ""
                    ),
                    "historical-reviewer",
                    str(row["recorded_at"]),
                    deterministic=True,
                )
        # A query that merely executed successfully is not semantic evidence.
        # Older rows created before the feedback gate are downgraded safely.
        self.connection.execute(
            "UPDATE experience_reviews SET eligible=0,"
            "eligibility_reasons_json=?,state='ineligible' "
            "WHERE source_kind='query_run' AND user_feedback='' "
            "AND state='candidate'",
            (_canonical(["requires_human_feedback"]),),
        )
        self.connection.execute("PRAGMA optimize")
        self.connection.commit()

    def _metadata(self, key: str) -> str:
        row = self.connection.execute(
            "SELECT value FROM evolution_metadata WHERE key=?", (key,)
        ).fetchone()
        return str(row["value"]) if row else ""

    def _set_metadata(self, key: str, value: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO evolution_metadata(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def bootstrap(self) -> str:
        baseline = PolicyArtifact.baseline(self.snapshot)
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO policy_versions(
                    policy_version,parent_version,target_skill,artifact_json,status,
                    change_reason,created_by,created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    baseline.version,
                    "",
                    "baseline",
                    _canonical(baseline.as_dict()),
                    "approved",
                    "Immutable empty baseline",
                    "system",
                    _now(),
                ),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO evolution_metadata(key,value) VALUES ('active_policy_version',?)",
                (baseline.version,),
            )
        return baseline.version

    @property
    def active_policy_version(self) -> str:
        return self._metadata("active_policy_version")

    def get_policy(self, version: Optional[str] = None) -> PolicyArtifact:
        selected = version or self.active_policy_version
        row = self.connection.execute(
            "SELECT artifact_json FROM policy_versions WHERE policy_version=?", (selected,)
        ).fetchone()
        if not row:
            raise ValueError("unknown policy version: %s" % selected)
        artifact = PolicyArtifact.from_dict(json.loads(row["artifact_json"]), self.snapshot)
        if artifact.version != selected:
            raise ValueError("stored policy artifact hash mismatch")
        return artifact

    def ensure_current_policy_contract(
        self, actor: str = "text2sql-policy-migration"
    ) -> Mapping[str, Any]:
        """Atomically activate the behavior-equivalent v2 form of a v1 Policy.

        Legacy artifacts retain their v1 content hash when read so old traces
        and checkpoints remain verifiable. They are intentionally read-only for
        evolution. This migration materializes the already validated v2 form as
        a new content-addressed Policy and switches the active pin without
        changing any role behavior.
        """

        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("policy contract migration actor is required")
        previous = self.active_policy_version
        row = self.connection.execute(
            "SELECT artifact_json,status FROM policy_versions WHERE policy_version=?",
            (previous,),
        ).fetchone()
        if not row:
            raise ValueError("active Policy row is missing")
        migrated = PolicyArtifact.from_dict(
            json.loads(row["artifact_json"]), self.snapshot
        )
        if not migrated.was_migrated_from_v1:
            return {
                "migrated": False,
                "previous_policy_version": previous,
                "active_policy_version": previous,
            }
        current = PolicyArtifact.from_dict(migrated.as_dict(), self.snapshot)
        if current.was_migrated_from_v1 or current.changed_skills(migrated):
            raise ValueError("legacy Policy migration must preserve every role field")
        existing = self.connection.execute(
            "SELECT artifact_json,status FROM policy_versions WHERE policy_version=?",
            (current.version,),
        ).fetchone()
        if existing:
            existing_artifact = PolicyArtifact.from_dict(
                json.loads(existing["artifact_json"]), self.snapshot
            )
            if existing_artifact.as_dict() != current.as_dict() or str(
                existing["status"] or ""
            ) not in {"approved", "retired"}:
                raise ValueError("current Policy contract row is not safely reusable")

        timestamp = _now()
        metadata = {
            "contract": "PolicyContractMigration/v1",
            "behavior_preserving": True,
            "source_policy_version": previous,
            "source_contract": "text2sql-policy-v1",
            "target_contract": "text2sql-policy-v2",
        }
        with self.connection:
            if not existing:
                self.connection.execute(
                    """
                    INSERT INTO policy_versions(
                        policy_version,parent_version,target_skill,artifact_json,status,
                        change_reason,created_by,created_at,reviewed_by,reviewed_at,
                        proposal_metadata_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        current.version,
                        previous,
                        "contract-migration",
                        _canonical(current.as_dict()),
                        "approved",
                        "Behavior-preserving PolicyArtifact v1 to v2 migration",
                        actor[:200],
                        timestamp,
                        actor[:200],
                        timestamp,
                        _canonical(metadata),
                    ),
                )
            else:
                self.connection.execute(
                    "UPDATE policy_versions SET status='approved' WHERE policy_version=?",
                    (current.version,),
                )
            self.connection.execute(
                "UPDATE policy_versions SET status='retired' WHERE policy_version=?",
                (previous,),
            )
            self.connection.execute(
                "UPDATE evolution_metadata SET value=? WHERE key='active_policy_version'",
                (current.version,),
            )
            self.connection.execute(
                "INSERT INTO activation_audit VALUES (?,?,?,?,?,?,?)",
                (
                    "activation-%s" % uuid.uuid4().hex,
                    "policy_contract_migration",
                    previous,
                    current.version,
                    actor[:200],
                    "Behavior-preserving PolicyArtifact v1 to v2 migration",
                    timestamp,
                ),
            )
        return {
            "migrated": True,
            "previous_policy_version": previous,
            "active_policy_version": current.version,
        }

    def policy_record(self, version: Optional[str] = None) -> Mapping[str, Any]:
        """Return one Policy control-plane row with decoded proposal lineage."""

        selected = version or self.active_policy_version
        row = self.connection.execute(
            "SELECT policy_version,parent_version,target_skill,status,change_reason,"
            "created_by,created_at,reviewed_by,reviewed_at,proposal_metadata_json "
            "FROM policy_versions WHERE policy_version=?",
            (selected,),
        ).fetchone()
        if not row:
            raise ValueError("unknown policy version: %s" % selected)
        value = dict(row)
        try:
            metadata = json.loads(str(value.pop("proposal_metadata_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        value["proposal_metadata"] = (
            dict(metadata) if isinstance(metadata, Mapping) else {}
        )
        return value

    def policy_source_memory_bindings(
        self, version: Optional[str] = None
    ) -> Mapping[str, tuple[str, ...]]:
        """Return materialized Memory-to-Policy field provenance for one version."""

        def memory_ids(value: Any) -> tuple[str, ...]:
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                return ()
            return tuple(
                dict.fromkeys(
                    str(item).strip()[:100]
                    for item in value
                    if str(item).strip().startswith("memory-")
                )
            )

        def bindings(value: Any) -> dict[str, set[str]]:
            if not isinstance(value, Mapping):
                return {}
            result: dict[str, set[str]] = {}
            for raw_memory_id, raw_slots in value.items():
                memory_id = str(raw_memory_id).strip()[:100]
                if not memory_id.startswith("memory-"):
                    continue
                if not isinstance(raw_slots, Sequence) or isinstance(
                    raw_slots, (str, bytes)
                ):
                    continue
                slots = {
                    str(slot).strip()[:200]
                    for slot in raw_slots
                    if str(slot).strip()
                }
                if slots:
                    result[memory_id] = slots
            return result

        selected = version or self.active_policy_version
        chain = []
        seen_versions = set()
        while selected:
            if selected in seen_versions or len(chain) >= 100:
                raise ValueError("invalid policy parent lineage")
            seen_versions.add(selected)
            row = self.connection.execute(
                "SELECT parent_version,target_skill,proposal_metadata_json "
                "FROM policy_versions "
                "WHERE policy_version=?",
                (selected,),
            ).fetchone()
            if not row:
                raise ValueError("unknown policy version: %s" % selected)
            try:
                metadata = json.loads(
                    str(row["proposal_metadata_json"] or "{}")
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            chain.append(
                (
                    str(row["target_skill"] or ""),
                    metadata if isinstance(metadata, Mapping) else {},
                )
            )
            selected = str(row["parent_version"] or "")

        compiled: dict[str, set[str]] = {}
        for target_skill, metadata in reversed(chain):
            materialized = metadata.get("compiled_memory_fields")
            if isinstance(materialized, Mapping):
                compiled = bindings(materialized)
            else:
                legacy_slot = "%s.*" % (target_skill or "unknown")
                for memory_id in memory_ids(metadata.get("memory_ids")):
                    compiled.setdefault(memory_id, set()).add(legacy_slot)
            for memory_id in memory_ids(metadata.get("drop_memory_ids")):
                compiled.pop(memory_id, None)
        return {
            memory_id: tuple(sorted(slots))
            for memory_id, slots in sorted(compiled.items())
            if slots
        }

    def policy_source_memory_ids(
        self, version: Optional[str] = None
    ) -> tuple[str, ...]:
        """Return every reviewed memory still compiled into a Policy lineage."""

        return tuple(self.policy_source_memory_bindings(version))

    def validate_experience_policy_lineage(
        self,
        candidate_policy_version: str,
        memory_ids: Sequence[str] = (),
    ) -> Mapping[str, Any]:
        """Prove that direct Experience sources changed only their Agent prompt.

        Proposal metadata is not sufficient evidence by itself: callers of the
        generic ``propose_policy`` API can name a Memory without binding it to a
        changed field.  Recompute the Policy diff and compare both the persisted
        compile attestation and the materialized Policy lineage before any
        source-case replay or release decision is accepted.
        """

        candidate = self.policy_record(candidate_policy_version)
        metadata = candidate.get("proposal_metadata")
        if not isinstance(metadata, Mapping):
            raise ValueError("Experience Policy proposal metadata is required")
        if (
            metadata.get("contract") != "ExperiencePolicyProposal/v1"
            or metadata.get("source") != "confirmed-experiences"
            or metadata.get("target_replay_required") is not True
        ):
            raise ValueError("Policy candidate is not an Experience-driven proposal")

        raw_ids = metadata.get("memory_ids")
        if (
            not isinstance(raw_ids, Sequence)
            or isinstance(raw_ids, (str, bytes, bytearray))
            or not raw_ids
            or any(
                type(memory_id) is not str
                or not memory_id.startswith("memory-")
                for memory_id in raw_ids
            )
        ):
            raise ValueError("Experience Policy source ids are invalid")
        direct_ids = tuple(sorted(raw_ids))
        if len(set(direct_ids)) != len(direct_ids):
            raise ValueError("Experience Policy source ids must not contain duplicates")

        if memory_ids:
            if (
                not isinstance(memory_ids, Sequence)
                or isinstance(memory_ids, (str, bytes, bytearray))
                or any(type(memory_id) is not str for memory_id in memory_ids)
            ):
                raise ValueError("target replay Experience ids are invalid")
            requested_ids = tuple(sorted(memory_ids))
            if (
                len(set(requested_ids)) != len(requested_ids)
                or requested_ids != direct_ids
            ):
                raise ValueError("target replay Experience lineage mismatch")

        parent_version = str(candidate.get("parent_version") or "")
        target_skill = str(candidate.get("target_skill") or "")
        if not parent_version or target_skill not in TEXT2SQL_SKILLS:
            raise ValueError("Experience Policy lineage is incomplete")
        parent_artifact = self.get_policy(parent_version)
        candidate_artifact = self.get_policy(candidate_policy_version)
        require_single_skill_change(parent_artifact, candidate_artifact, target_skill)

        field_map = {
            "prompt_fragment": "prompt_fragments",
            "field_aliases": "field_aliases",
            "value_aliases": "value_aliases",
            "few_shot_examples": "few_shot_examples",
            "allowed_tools": "tool_selection_policy",
            "budget_parameters": "budget_parameters",
        }
        parent_value = parent_artifact.as_dict()
        candidate_value = candidate_artifact.as_dict()
        changed_fields = {
            public_name
            for public_name, artifact_name in field_map.items()
            if parent_value[artifact_name][target_skill]
            != candidate_value[artifact_name][target_skill]
        }
        if changed_fields != {"prompt_fragment"}:
            raise ValueError(
                "Experience-driven Policy may change prompt_fragment only"
            )

        compiled_metadata = metadata.get("compiled_memory_fields")
        if not isinstance(compiled_metadata, Mapping):
            raise ValueError("Experience Policy compile attestation is missing")
        materialized = self.policy_source_memory_bindings(candidate_policy_version)
        expected_slot = "%s.prompt_fragment" % target_skill
        for memory_id in direct_ids:
            raw_slots = compiled_metadata.get(memory_id)
            metadata_slots = (
                set(str(slot) for slot in raw_slots)
                if isinstance(raw_slots, Sequence)
                and not isinstance(raw_slots, (str, bytes, bytearray))
                else set()
            )
            if metadata_slots != {expected_slot} or set(
                materialized.get(memory_id) or ()
            ) != {expected_slot}:
                raise ValueError(
                    "source Experience is not compiled into target prompt_fragment"
                )
        rule_rows = self.connection.execute(
            "SELECT rule_id,content_sha256 FROM policy_semantic_rule_sources WHERE policy_version=?",
            (candidate_policy_version,),
        ).fetchall()
        if rule_rows or any(key.startswith("semantic_rule") for key in metadata):
            rules = self.validate_semantic_rule_sources(metadata, direct_ids, target_skill)
            if {row["rule_id"]: row["content_sha256"] for row in rule_rows} != {
                rule["rule_id"]: rule["content_sha256"] for rule in rules
            }:
                raise ValueError("materialized SemanticRule Policy lineage mismatch")
        return {
            "candidate_policy_version": candidate_policy_version,
            "parent_policy_version": parent_version,
            "target_skill": target_skill,
            "memory_ids": direct_ids,
            "semantic_rule_ids": sorted(row["rule_id"] for row in rule_rows),
        }

    def list_policies(self) -> Sequence[Mapping[str, Any]]:
        rows = self.connection.execute(
            "SELECT policy_version,parent_version,target_skill,status,change_reason,created_by,"
            "created_at,reviewed_by,reviewed_at,proposal_metadata_json "
            "FROM policy_versions ORDER BY created_at"
        ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            try:
                metadata = json.loads(
                    str(value.pop("proposal_metadata_json") or "{}")
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            value["proposal_metadata"] = (
                dict(metadata) if isinstance(metadata, Mapping) else {}
            )
            values.append(value)
        return tuple(values)

    def record_target_replay(
        self,
        candidate_policy_version: str,
        artifact: Mapping[str, Any],
        *,
        created_by: str = "",
        artifact_path: str = "",
    ) -> Mapping[str, Any]:
        """Persist an immutable, content-addressed source-case replay result."""

        candidate = self.policy_record(candidate_policy_version)
        if candidate["status"] not in {
            "candidate",
            "evaluated",
            "shadow_ready",
        }:
            raise ValueError("policy is not awaiting target replay")
        validated = validate_target_replay_artifact(
            artifact, candidate_policy_version=candidate_policy_version
        )
        if validated.get("parent_policy_version") != candidate["parent_version"]:
            raise ValueError("target replay parent Policy mismatch")
        actual_memory_ids = sorted(str(value) for value in validated["memory_ids"])
        self.validate_experience_policy_lineage(
            candidate_policy_version,
            actual_memory_ids,
        )
        for memory_id in actual_memory_ids:
            memory = self.get_memory(memory_id)
            rule = memory.get("rule") or {}
            if (
                memory.get("state") != "confirmed"
                or memory.get("runtime_eligible") is not False
                or rule.get("contract") != EXPERIENCE_MEMORY_CONTRACT
                or rule.get("target_agent") != candidate["target_skill"]
            ):
                raise ValueError("target replay source Experience is no longer valid")
        artifact_sha256 = str(validated["artifact_sha256"])
        replay_id = "target-replay-%s" % hashlib.sha256(
            _canonical(
                [candidate_policy_version, artifact_sha256]
            ).encode("utf-8")
        ).hexdigest()[:24]
        existing = self.connection.execute(
            "SELECT replay_id FROM policy_target_replays "
            "WHERE candidate_policy_version=? AND artifact_sha256=?",
            (candidate_policy_version, artifact_sha256),
        ).fetchone()
        if existing:
            return self.get_target_replay(str(existing["replay_id"]))
        with self.connection:
            self.connection.execute(
                "INSERT INTO policy_target_replays("
                "replay_id,candidate_policy_version,parent_policy_version,"
                "memory_ids_json,artifact_json,artifact_sha256,artifact_path,status,"
                "created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    replay_id,
                    candidate_policy_version,
                    str(candidate["parent_version"]),
                    _canonical(actual_memory_ids),
                    _canonical(validated),
                    artifact_sha256,
                    str(artifact_path or "")[:2000],
                    str(validated["status"]),
                    str(created_by or "")[:200],
                    _now(),
                ),
            )
        return self.get_target_replay(replay_id)

    @staticmethod
    def _decode_target_replay_row(row: Mapping[str, Any]) -> Mapping[str, Any]:
        value = dict(row)
        value["memory_ids"] = json.loads(value.pop("memory_ids_json"))
        value["artifact"] = json.loads(value.pop("artifact_json"))
        return value

    def get_target_replay(self, replay_id: str) -> Mapping[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM policy_target_replays WHERE replay_id=?",
            (str(replay_id)[:200],),
        ).fetchone()
        if not row:
            raise ValueError("unknown target replay")
        return self._decode_target_replay_row(row)

    def latest_target_replay(
        self, candidate_policy_version: str
    ) -> Mapping[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM policy_target_replays WHERE candidate_policy_version=? "
            "ORDER BY created_at DESC,replay_id DESC LIMIT 1",
            (candidate_policy_version,),
        ).fetchone()
        return self._decode_target_replay_row(row) if row else {}

    # Compatibility spelling used by the web presentation layer.
    def get_latest_target_replay(
        self, candidate_policy_version: str
    ) -> Mapping[str, Any]:
        return self.latest_target_replay(candidate_policy_version)

    @staticmethod
    def _harness_reason_text(reason_code: str, errors: Sequence[str]) -> str:
        friendly = {
            "all_gates_passed": "候选 SQL 通过确定性门禁并完成只读执行。",
            "invalid_final_candidate_index": "Leader 选择的候选不存在，Harness 已失败关闭。",
            "needs_new_query": "现有结果不足以回答，需发起新的数据库查询。",
            "rejected": "候选未通过 Harness 放行条件。",
            "failed": "运行时失败，未进入安全执行。",
            "error": "运行时异常，未进入安全执行。",
        }
        if reason_code in friendly:
            return friendly[reason_code]
        if errors:
            return "；".join(str(item) for item in errors)[:2000]
        return reason_code or "未提供门禁原因"

    def _store_harness_decision(
        self,
        task_id: str,
        status: str,
        final_sql: str,
        gates: Mapping[str, Any],
        created_at: str,
    ) -> Mapping[str, Any]:
        errors = [str(item) for item in gates.get("errors") or () if item]
        accepted = status == "success" and bool(gates.get("accepted"))
        if accepted:
            outcome = "accepted"
            reason_code = "all_gates_passed"
        elif status == "needs_new_query":
            outcome = "deferred"
            reason_code = "needs_new_query"
        elif status == "rejected" or errors:
            outcome = "rejected"
            reason_code = errors[0].split(":", 1)[0] if errors else "rejected"
        else:
            outcome = "failed"
            reason_code = status or "failed"
        decision_id = "decision-harness-%s" % hashlib.sha256(
            task_id.encode("utf-8")
        ).hexdigest()[:24]
        evidence = {
            "gate_errors": errors,
            "gate_accepted": bool(gates.get("accepted")),
            "query_status": status,
            "sql_generated": bool(final_sql.strip()),
        }
        self.connection.execute(
            """
            INSERT INTO query_decisions(
                decision_id,task_id,decision_source,decision_type,outcome,
                reason_code,reason_text,actor,evidence_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(decision_id) DO UPDATE SET
                decision_type=excluded.decision_type,outcome=excluded.outcome,
                reason_code=excluded.reason_code,reason_text=excluded.reason_text,
                actor=excluded.actor,evidence_json=excluded.evidence_json,
                created_at=excluded.created_at
            """,
            (
                decision_id,
                task_id[:200],
                "harness",
                "execution_gate",
                outcome,
                reason_code[:200],
                self._harness_reason_text(reason_code, errors),
                "text2sql-harness",
                _canonical(evidence),
                created_at or _now(),
            ),
        )
        return self.get_query_decision(decision_id)

    def _store_human_decision(
        self,
        task_id: str,
        decision: str,
        note: str,
        actor: str,
        created_at: str = "",
        *,
        deterministic: bool = False,
    ) -> Mapping[str, Any]:
        if decision not in {"correct", "incorrect"}:
            raise ValueError("feedback decision must be correct or incorrect")
        note = note.strip()
        if decision == "incorrect" and not note:
            raise ValueError("rejection reason is required")
        if deterministic:
            suffix = hashlib.sha256(
                (task_id + ":human").encode("utf-8")
            ).hexdigest()[:24]
        else:
            suffix = uuid.uuid4().hex
        decision_id = "decision-human-%s" % suffix
        outcome = "accepted" if decision == "correct" else "rejected"
        reason_code = "human_confirmed" if decision == "correct" else "human_rejected"
        reason_text = note or "人工确认结果与业务语义一致。"
        self.connection.execute(
            "INSERT OR IGNORE INTO query_decisions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                decision_id,
                task_id[:200],
                "human",
                "result_review",
                outcome,
                reason_code,
                reason_text[:2000],
                actor.strip()[:200] or "historical-reviewer",
                _canonical({"feedback": decision}),
                created_at or _now(),
            ),
        )
        return self.get_query_decision(decision_id)

    def get_query_decision(self, decision_id: str) -> Mapping[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM query_decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        if not row:
            raise ValueError("unknown query decision")
        value = dict(row)
        value["evidence"] = json.loads(value.pop("evidence_json"))
        return value

    def query_decisions(
        self, task_ids: Sequence[str]
    ) -> Sequence[Mapping[str, Any]]:
        bounded = tuple(dict.fromkeys(str(item)[:200] for item in task_ids if item))[:50]
        if not bounded:
            return ()
        placeholders = ",".join("?" for _ in bounded)
        rows = self.connection.execute(
            "SELECT * FROM query_decisions WHERE task_id IN (%s) "
            "ORDER BY created_at,decision_id" % placeholders,
            bounded,
        ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["evidence"] = json.loads(value.pop("evidence_json"))
            values.append(value)
        return tuple(values)

    def save_query_trace(self, trace: Mapping[str, Any]) -> None:
        task_id = str(trace.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("query trace task_id is required")
        source_revision = max(1, int(trace.get("source_revision") or 1))
        stored_trace = {
            "task_id": task_id[:200],
            "status": str(trace.get("status") or "unknown")[:50],
            "question": str(trace.get("question") or "")[:2000],
            "final_sql": str(trace.get("final_sql") or "")[:20000],
            "gates": dict(trace.get("gates") or {}),
            "agents": list(trace.get("agents") or ()),
            "execution": dict(trace.get("execution") or {}),
            "version_pins": dict(trace.get("version_pins") or {}),
            "answer": dict(trace.get("answer") or {}),
            "recorded_at": str(trace.get("recorded_at") or _now())[:100],
            "user_id": str(trace.get("user_id") or "local-user")[:200],
            "session_id": str(trace.get("session_id") or "default")[:200],
            "original_question": str(
                trace.get("original_question") or trace.get("question") or ""
            )[:2000],
            "standalone_question": str(
                trace.get("standalone_question") or trace.get("question") or ""
            )[:2000],
            "query_type": str(trace.get("query_type") or "DATA_QUERY")[:50],
            "parent_task_id": str(trace.get("parent_task_id") or "")[:200],
            "schema_plan": dict(trace.get("schema_plan") or {}),
            "query_spec": dict(trace.get("query_spec") or {}),
            "collaboration": dict(trace.get("collaboration") or {}),
            "retrieval": list(trace.get("retrieval") or ()),
            "result_rows": list(trace.get("result_rows") or ())[:50],
            "feedback_status": str(trace.get("feedback_status") or "")[:50],
            "origin": str(trace.get("origin") or "legacy")[:50],
            "source_lane": str(trace.get("source_lane") or "stable")[:50],
            "source_revision": source_revision,
            "draft_link_pack": dict(trace.get("draft_link_pack") or {}),
            "policy_source_memory_ids": list(
                dict.fromkeys(
                    str(value)[:200]
                    for value in trace.get("policy_source_memory_ids") or ()
                    if str(value).strip()
                )
            )[:50],
        }
        trace_sha256 = _query_trace_evidence_sha256(stored_trace)
        with self.connection:
            existing_revision = self.connection.execute(
                "SELECT trace_sha256 FROM query_trace_revisions "
                "WHERE task_id=? AND source_revision=?",
                (stored_trace["task_id"], source_revision),
            ).fetchone()
            if existing_revision:
                if str(existing_revision["trace_sha256"]) != trace_sha256:
                    raise ValueError(
                        "query trace revision conflicts with immutable evidence"
                    )
                return
            latest_revision = self.connection.execute(
                "SELECT MAX(source_revision) FROM query_trace_revisions WHERE task_id=?",
                (stored_trace["task_id"],),
            ).fetchone()[0]
            if latest_revision is not None and source_revision <= int(latest_revision):
                raise ValueError("query trace source_revision must increase monotonically")
            inserted = self.connection.execute(
                "INSERT OR IGNORE INTO query_trace_revisions("
                "task_id,source_revision,trace_json,trace_sha256,recorded_at) "
                "VALUES (?,?,?,?,?)",
                (
                    stored_trace["task_id"],
                    source_revision,
                    _canonical(stored_trace),
                    trace_sha256,
                    stored_trace["recorded_at"],
                ),
            ).rowcount
            if not inserted:
                raced = self.connection.execute(
                    "SELECT trace_sha256 FROM query_trace_revisions "
                    "WHERE task_id=? AND source_revision=?",
                    (stored_trace["task_id"], source_revision),
                ).fetchone()
                if not raced or str(raced["trace_sha256"]) != trace_sha256:
                    raise ValueError(
                        "query trace revision conflicts with immutable evidence"
                    )
                return
            previous_projection = self.connection.execute(
                "SELECT feedback_status FROM query_traces WHERE task_id=?",
                (stored_trace["task_id"],),
            ).fetchone()
            projection_feedback = (
                str(previous_projection["feedback_status"] or "")
                if previous_projection
                else stored_trace["feedback_status"]
            )
            self.connection.execute(
                """
                INSERT OR REPLACE INTO query_traces(
                    task_id,status,question,final_sql,gates_json,agents_json,
                    execution_json,version_pins_json,answer_json,recorded_at,
                    user_id,session_id,original_question,standalone_question,
                    query_type,parent_task_id,schema_plan_json,query_spec_json,
                    collaboration_json,retrieval_json,result_rows_json,feedback_status,
                    origin,source_lane,source_revision,draft_link_pack_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    stored_trace["task_id"],
                    stored_trace["status"],
                    stored_trace["question"],
                    stored_trace["final_sql"],
                    _canonical(stored_trace["gates"]),
                    _canonical(stored_trace["agents"]),
                    _canonical(stored_trace["execution"]),
                    _canonical(stored_trace["version_pins"]),
                    _canonical(stored_trace["answer"]),
                    stored_trace["recorded_at"],
                    stored_trace["user_id"],
                    stored_trace["session_id"],
                    stored_trace["original_question"],
                    stored_trace["standalone_question"],
                    stored_trace["query_type"],
                    stored_trace["parent_task_id"],
                    _canonical(stored_trace["schema_plan"]),
                    _canonical(stored_trace["query_spec"]),
                    _canonical(stored_trace["collaboration"]),
                    _canonical(stored_trace["retrieval"]),
                    _canonical(stored_trace["result_rows"]),
                    projection_feedback,
                    stored_trace["origin"],
                    stored_trace["source_lane"],
                    source_revision,
                    _canonical(stored_trace["draft_link_pack"]),
                ),
            )
            self._store_harness_decision(
                stored_trace["task_id"],
                stored_trace["status"],
                stored_trace["final_sql"],
                stored_trace["gates"],
                stored_trace["recorded_at"],
            )
            self.connection.execute(
                "DELETE FROM query_decisions WHERE task_id NOT IN "
                "(SELECT task_id FROM query_traces)"
            )

    def list_query_traces(self, limit: int = 20) -> Sequence[Mapping[str, Any]]:
        bounded = max(1, min(int(limit), 50))
        rows = self.connection.execute(
            "SELECT * FROM query_traces ORDER BY recorded_at DESC LIMIT ?", (bounded,)
        ).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            values.append(_decode_query_trace_row(row))
        return tuple(values)

    def next_query_trace_revision(self, task_id: str) -> int:
        """Allocate the next immutable Trace revision for a retried request."""

        normalized = str(task_id or "").strip()[:200]
        if not normalized:
            raise ValueError("query trace task_id is required")
        row = self.connection.execute(
            "SELECT MAX(source_revision) FROM query_trace_revisions WHERE task_id=?",
            (normalized,),
        ).fetchone()
        latest = int(row[0] or 0) if row else 0
        return latest + 1

    def get_query_trace(
        self, task_id: str, source_revision: Optional[int] = None
    ) -> Mapping[str, Any]:
        if source_revision is None:
            revision_row = self.connection.execute(
                "SELECT trace_json,trace_sha256 FROM query_trace_revisions "
                "WHERE task_id=? ORDER BY source_revision DESC LIMIT 1",
                (task_id[:200],),
            ).fetchone()
        else:
            revision_row = self.connection.execute(
                "SELECT trace_json,trace_sha256 FROM query_trace_revisions "
                "WHERE task_id=? AND source_revision=?",
                (task_id[:200], max(1, int(source_revision))),
            ).fetchone()
        if revision_row:
            try:
                revision = json.loads(str(revision_row["trace_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("stored query trace revision is invalid") from exc
            if (
                not isinstance(revision, Mapping)
                or _query_trace_evidence_sha256(revision)
                != str(revision_row["trace_sha256"])
            ):
                raise ValueError("stored query trace revision hash mismatch")
            return dict(revision)
        row = self.connection.execute(
            "SELECT * FROM query_traces WHERE task_id=?", (task_id[:200],)
        ).fetchone()
        if not row:
            raise ValueError("unknown query task")
        item = _decode_query_trace_row(row)
        if source_revision is not None and int(item["source_revision"]) != max(
            1, int(source_revision)
        ):
            raise ValueError("unknown query task revision")
        return item

    def memory_dashboard(
        self, user_id: str, session_id: str, limit: int = 12
    ) -> Mapping[str, Any]:
        """Return bounded, display-safe Working/Episodic/Semantic memory."""
        bounded = max(1, min(int(limit), 50))
        user_key = user_id[:200]
        session_key = session_id[:200]
        working_rows = self.connection.execute(
            "SELECT role,content,task_id,created_at FROM memory_messages "
            "WHERE user_id=? AND session_id=? ORDER BY message_id DESC LIMIT ?",
            (user_key, session_key, bounded),
        ).fetchall()
        episodic_rows = self.connection.execute(
            "SELECT task_id,status,original_question,standalone_question,query_type,"
            "parent_task_id,final_sql,feedback_status,recorded_at,execution_json,"
            "version_pins_json,origin,source_lane,source_revision FROM query_traces "
            "WHERE user_id=? AND session_id=? ORDER BY recorded_at DESC LIMIT ?",
            (user_key, session_key, bounded),
        ).fetchall()
        semantic_rows = self.connection.execute(
            "SELECT memory_id,target_skill,origin_split,failure_kind,content,rule_json,"
            "rule_fingerprint,source_case_ids_json,occurrence_count,evidence_json,state,"
            "created_at,reviewed_by,reviewed_at,review_note,source_task_id,source_stage,"
            "source_revision,evidence_sha256,runtime_eligible,state_version "
            "FROM memory_items "
            "ORDER BY CASE state WHEN 'candidate' THEN 0 WHEN 'needs_evidence' THEN 1 "
            "WHEN 'confirmed' THEN 2 WHEN 'approved' THEN 3 "
            "WHEN 'evaluating' THEN 4 WHEN 'evaluated' THEN 5 "
            "WHEN 'evaluation_failed' THEN 6 WHEN 'stable' THEN 7 ELSE 8 END,"
            "created_at DESC LIMIT ?",
            (bounded,),
        ).fetchall()
        question_sql_rows = self.connection.execute(
            "SELECT experience_id,task_id,question,sql,source_kind,state,created_at,"
            "reviewed_by,reviewed_at,review_note,knowledge_evidence_id "
            "FROM experience_reviews WHERE state='promoted' "
            "ORDER BY reviewed_at DESC,created_at DESC LIMIT ?",
            (bounded,),
        ).fetchall()
        working_count = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM memory_messages WHERE user_id=? AND session_id=?",
                (user_key, session_key),
            ).fetchone()[0]
        )
        episodic_count = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM query_traces WHERE user_id=? AND session_id=?",
                (user_key, session_key),
            ).fetchone()[0]
        )
        semantic_counts = {
            "candidate": 0,
            "confirmed": 0,
            "needs_evidence": 0,
            "approved": 0,
            "evaluating": 0,
            "evaluated": 0,
            "evaluation_failed": 0,
            "stable": 0,
            "rejected": 0,
            "retired": 0,
        }
        for row in self.connection.execute(
            "SELECT state,COUNT(*) AS count FROM memory_items GROUP BY state"
        ).fetchall():
            semantic_counts[str(row["state"])] = int(row["count"])
        experience_counts = {
            "candidate": 0,
            "ineligible": 0,
            "promoted": 0,
            "rejected": 0,
        }
        for row in self.connection.execute(
            "SELECT state,COUNT(*) AS count FROM experience_reviews GROUP BY state"
        ).fetchall():
            experience_counts[str(row["state"])] = int(row["count"])
        episodic_items = []
        for index, row in enumerate(episodic_rows):
            item = dict(row)
            execution = json.loads(item.pop("execution_json"))
            version_pins = json.loads(item.pop("version_pins_json"))
            item["temporal_context"] = {
                "recorded_at": str(item["recorded_at"]),
                "turn_number": max(1, episodic_count - index),
                "duration_ms": int(execution.get("duration_ms") or 0),
            }
            item["version_context"] = {
                key: str(version_pins.get(key) or "")
                for key in (
                    "database_snapshot_id",
                    "wiki_index_version",
                    "vanna_index_version",
                    "memory_snapshot_id",
                    "policy_version",
                )
            }
            episodic_items.append(item)
        decisions_by_task: dict[str, dict[str, Mapping[str, Any]]] = {}
        for decision in self.query_decisions(
            [str(item["task_id"]) for item in episodic_items]
        ):
            decisions_by_task.setdefault(str(decision["task_id"]), {})[
                str(decision["decision_source"])
            ] = decision
        for item in episodic_items:
            sources = decisions_by_task.get(str(item["task_id"]), {})
            item["decisions"] = {
                "harness": dict(sources.get("harness") or {}),
                "human": dict(sources.get("human") or {}),
            }
            item["human_review_required"] = not bool(sources.get("human"))
        return {
            "working": {
                "items": [dict(row) for row in working_rows],
                "count": working_count,
                "retention_limit_per_session": WORKING_MEMORY_RETENTION_PER_SESSION,
            },
            "episodic": {
                "items": episodic_items,
                "count": episodic_count,
                "display_limit": EPISODIC_MEMORY_RETENTION_PER_SESSION,
                "physical_retention": "unbounded_mvp",
            },
            "semantic": {
                "items": [_decode_memory_row(row) for row in semantic_rows],
                "counts": semantic_counts,
                "stable_only_injected": True,
            },
            "memory_evaluations": {
                "items": self.list_memory_evaluation_jobs(limit=bounded),
            },
            "question_sql": {
                "items": [dict(row) for row in question_sql_rows],
                "counts": experience_counts,
                "retrieval_backend": "vanna",
                "user_confirmed_only": True,
                "semantic_memory": False,
            },
        }

    def list_memory_sessions(
        self, user_id: str, limit: int = 20
    ) -> Sequence[Mapping[str, Any]]:
        """List bounded session summaries for one user, newest activity first."""

        user_key = user_id[:200]
        bounded = max(1, min(int(limit), 50))
        rows = self.connection.execute(
            "SELECT session_id,MAX(last_activity) AS last_activity FROM ("
            "SELECT session_id,MAX(created_at) AS last_activity FROM memory_messages "
            "WHERE user_id=? GROUP BY session_id UNION ALL "
            "SELECT session_id,MAX(recorded_at) AS last_activity FROM query_traces "
            "WHERE user_id=? GROUP BY session_id) "
            "WHERE session_id<>'' GROUP BY session_id "
            "ORDER BY last_activity DESC LIMIT ?",
            (user_key, user_key, bounded),
        ).fetchall()
        sessions = []
        for row in rows:
            session_key = str(row["session_id"])
            working_count = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM memory_messages WHERE user_id=? AND session_id=?",
                    (user_key, session_key),
                ).fetchone()[0]
            )
            episodic_count = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM query_traces WHERE user_id=? AND session_id=?",
                    (user_key, session_key),
                ).fetchone()[0]
            )
            sessions.append(
                {
                    "session_id": session_key,
                    "last_activity": str(row["last_activity"] or ""),
                    "working_count": working_count,
                    "episodic_count": episodic_count,
                }
            )
        return tuple(sessions)

    def prepare_query_attempt(
        self,
        task_id: str,
        user_id: str,
        session_id: str,
        question: str,
        principals: Sequence[str],
        conversation_context: Mapping[str, Any],
        runtime_identity: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        """Insert once, freeze context, and return a completed response on retry."""

        task_id = task_id.strip()[:200]
        if not task_id:
            raise ValueError("query attempt task_id is required")
        request_identity = {
            "user_id": user_id[:200],
            "session_id": session_id[:200],
            "question": question[:2000],
            "principals": sorted(set(str(item) for item in principals)),
            "runtime": dict(runtime_identity or {}),
        }
        request_sha256 = hashlib.sha256(
            _canonical(request_identity).encode("utf-8")
        ).hexdigest()
        context_json = _canonical(dict(conversation_context))
        context_sha256 = hashlib.sha256(context_json.encode("utf-8")).hexdigest()
        timestamp = _now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT request_sha256,context_json,context_sha256,response_json,"
                "response_sha256,status FROM query_attempts WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO query_attempts("
                    "task_id,user_id,session_id,question,request_sha256,context_json,"
                    "context_sha256,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        task_id,
                        user_id[:200],
                        session_id[:200],
                        question[:2000],
                        request_sha256,
                        context_json,
                        context_sha256,
                        "pending",
                        timestamp,
                    ),
                )
                frozen_context = dict(conversation_context)
                cached_response = None
                status = "pending"
            else:
                frozen_json = str(row["context_json"] or "{}")
                if hashlib.sha256(frozen_json.encode("utf-8")).hexdigest() != str(
                    row["context_sha256"] or ""
                ):
                    raise ValueError("query attempt context integrity check failed")
                # Conversation history grows after a failure or another query.
                # Retry the original frozen context, while still comparing every
                # caller, permission, question, model and release identity field.
                if "conversation_context_sha256" in request_identity["runtime"]:
                    request_identity["runtime"]["conversation_context_sha256"] = str(
                        row["context_sha256"]
                    )
                    request_sha256 = hashlib.sha256(
                        _canonical(request_identity).encode("utf-8")
                    ).hexdigest()
                if not row["request_sha256"] or row["request_sha256"] != request_sha256:
                    raise ValueError(
                        "query task_id was reused with a different user, session, "
                        "question, principal, or runtime identity"
                    )
                frozen_context = json.loads(frozen_json)
                status = str(row["status"])
                cached_response = None
                if status == "completed":
                    response_json = str(row["response_json"] or "")
                    if (
                        not response_json
                        or hashlib.sha256(response_json.encode("utf-8")).hexdigest()
                        != str(row["response_sha256"] or "")
                    ):
                        raise ValueError("query attempt response integrity check failed")
                    cached_response = json.loads(response_json)
                else:
                    self.connection.execute(
                        "UPDATE query_attempts SET status='pending',error='',completed_at='' "
                        "WHERE task_id=?",
                        (task_id,),
                    )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return {
            "status": status,
            "conversation_context": frozen_context,
            "cached_response": cached_response,
        }

    def start_query_attempt(
        self, task_id: str, user_id: str, session_id: str, question: str
    ) -> None:
        self.prepare_query_attempt(
            task_id,
            user_id,
            session_id,
            question,
            (user_id,),
            {},
            {},
        )

    def finish_query_attempt(
        self,
        task_id: str,
        status: str,
        error: str = "",
        response: Optional[Mapping[str, Any]] = None,
    ) -> None:
        with self.connection:
            if response is None:
                self.connection.execute(
                    "UPDATE query_attempts SET status=?,error=?,completed_at=? "
                    "WHERE task_id=?",
                    (status[:50], error[:1000], _now(), task_id[:200]),
                )
            else:
                response_json = _canonical(dict(response))
                self.connection.execute(
                    "UPDATE query_attempts SET status=?,error=?,response_json=?,"
                    "response_sha256=?,completed_at=? WHERE task_id=?",
                    (
                        status[:50],
                        error[:1000],
                        response_json,
                        hashlib.sha256(response_json.encode("utf-8")).hexdigest(),
                        _now(),
                        task_id[:200],
                    ),
                )

    def append_message(
        self,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
        task_id: str = "",
    ) -> None:
        if role not in {"user", "assistant"} or not content.strip():
            return
        with self.connection:
            self.connection.execute(
                "INSERT INTO memory_messages(user_id,session_id,role,content,task_id,created_at) "
                "SELECT ?,?,?,?,?,? WHERE NOT EXISTS ("
                "SELECT 1 FROM memory_messages WHERE task_id=? AND role=? AND task_id<>'')",
                (
                    user_id[:200],
                    session_id[:200],
                    role,
                    content.strip()[:4000],
                    task_id[:200],
                    _now(),
                    task_id[:200],
                    role,
                ),
            )
            self.connection.execute(
                "DELETE FROM memory_messages WHERE message_id NOT IN ("
                "SELECT message_id FROM memory_messages WHERE user_id=? AND session_id=? "
                "ORDER BY message_id DESC LIMIT ?) AND user_id=? AND session_id=?",
                (
                    user_id[:200],
                    session_id[:200],
                    WORKING_MEMORY_RETENTION_PER_SESSION,
                    user_id[:200],
                    session_id[:200],
                ),
            )

    def recent_query_context(
        self, user_id: str, session_id: str, limit: int = 3
    ) -> Mapping[str, Any]:
        bounded = max(1, min(int(limit), 5))
        latest_attempt = self.connection.execute(
            "SELECT task_id,question,status,error,created_at,completed_at "
            "FROM query_attempts WHERE user_id=? AND session_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id[:200], session_id[:200]),
        ).fetchone()
        rows = self.connection.execute(
            "SELECT task_id,status,original_question,standalone_question,query_type,"
            "parent_task_id,final_sql,answer_json,recorded_at,feedback_status "
            "FROM query_traces WHERE user_id=? AND session_id=? "
            "ORDER BY recorded_at DESC LIMIT ?",
            (user_id[:200], session_id[:200], bounded),
        ).fetchall()
        runs = []
        for row in rows:
            item = dict(row)
            item["answer"] = json.loads(item.pop("answer_json"))
            runs.append(item)
        message_rows = self.connection.execute(
            "SELECT role,content,task_id,created_at FROM memory_messages "
            "WHERE user_id=? AND session_id=? ORDER BY message_id DESC LIMIT 8",
            (user_id[:200], session_id[:200]),
        ).fetchall()
        return {
            "latest_attempt": dict(latest_attempt) if latest_attempt else {},
            "recent_query_runs": runs,
            # Keep chat context in conversation order; SQL result payloads stay
            # in the separately bounded QueryRun snapshots.
            "recent_messages": [dict(row) for row in reversed(message_rows)],
        }

    def query_result_snapshot(
        self, task_id: str, user_id: str, session_id: str
    ) -> Mapping[str, Any]:
        row = self.connection.execute(
            "SELECT task_id,status,original_question,standalone_question,final_sql,"
            "answer_json,result_rows_json,gates_json,schema_plan_json,query_spec_json,"
            "version_pins_json,user_id,session_id,recorded_at FROM query_traces "
            "WHERE task_id=? AND user_id=? AND session_id=?",
            (task_id[:200], user_id[:200], session_id[:200]),
        ).fetchone()
        if not row:
            return {}
        value = dict(row)
        value["answer"] = json.loads(value.pop("answer_json"))
        value["rows"] = json.loads(value.pop("result_rows_json"))
        value["gates"] = json.loads(value.pop("gates_json"))
        value["schema_plan"] = json.loads(value.pop("schema_plan_json"))
        value["query_spec"] = json.loads(value.pop("query_spec_json"))
        value["version_pins"] = json.loads(value.pop("version_pins_json"))
        return value

    def add_experience_candidate(
        self,
        task_id: str,
        question: str,
        sql: str,
        *,
        source_kind: str = "query_run",
        eligible: bool,
        eligibility_reasons: Sequence[str] = (),
    ) -> str:
        question = question.strip()
        sql = sql.strip()
        if not task_id.strip() or not question or not sql:
            raise ValueError("experience requires task_id, question and SQL")
        sql_fingerprint = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        experience_id = "experience-%s" % hashlib.sha256(
            _canonical([question, sql, self.snapshot["snapshot_id"]]).encode("utf-8")
        ).hexdigest()[:24]
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO experience_reviews(
                    experience_id,task_id,question,sql,sql_fingerprint,source_kind,
                    eligible,eligibility_reasons_json,state,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(experience_id) DO UPDATE SET
                    eligible=MAX(experience_reviews.eligible,excluded.eligible),
                    eligibility_reasons_json=CASE WHEN excluded.eligible=1 THEN '[]'
                        ELSE experience_reviews.eligibility_reasons_json END,
                    state=CASE
                        WHEN experience_reviews.state IN (
                            'approved','evaluating','evaluated','promoted'
                        ) THEN experience_reviews.state
                        WHEN excluded.eligible=1 THEN 'candidate'
                        ELSE experience_reviews.state END,
                    source_kind=CASE WHEN excluded.eligible=1 THEN excluded.source_kind
                        ELSE experience_reviews.source_kind END
                """,
                (
                    experience_id,
                    task_id[:200],
                    question[:2000],
                    sql[:20000],
                    sql_fingerprint,
                    source_kind[:50],
                    1 if eligible else 0,
                    _canonical(list(dict.fromkeys(eligibility_reasons))),
                    "candidate" if eligible else "ineligible",
                    _now(),
                ),
            )
        return experience_id

    def record_query_feedback(
        self,
        task_id: str,
        decision: str,
        note: str,
        actor: str = "human-reviewer",
    ) -> Mapping[str, Any]:
        if decision not in {"correct", "incorrect"}:
            raise ValueError("feedback decision must be correct or incorrect")
        note = note.strip()
        if decision == "incorrect" and not note:
            raise ValueError("rejection reason is required")
        with self.connection:
            changed = self.connection.execute(
                "UPDATE query_traces SET feedback_status=? WHERE task_id=?",
                (decision, task_id[:200]),
            ).rowcount
            if not changed:
                raise ValueError("unknown query task")
            self.connection.execute(
                "UPDATE experience_reviews SET user_feedback=?,feedback_note=?,"
                "state=CASE WHEN ?='incorrect' AND state IN "
                "('candidate','ineligible','approved','evaluation_failed') "
                "THEN 'rejected' "
                "ELSE state END WHERE task_id=?",
                (decision, note[:2000], decision, task_id[:200]),
            )
            human_decision = self._store_human_decision(
                task_id, decision, note, actor
            )
        return human_decision

    def get_experience(self, experience_id: str) -> Mapping[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM experience_reviews WHERE experience_id=?",
            (experience_id,),
        ).fetchone()
        if not row:
            raise ValueError("unknown experience")
        value = dict(row)
        value["eligible"] = bool(value["eligible"])
        value["eligibility_reasons"] = json.loads(
            value.pop("eligibility_reasons_json")
        )
        return value

    def list_experiences(self, state: str = "", limit: int = 50) -> Sequence[Mapping[str, Any]]:
        allowed = {
            "candidate",
            "ineligible",
            "approved",
            "evaluating",
            "evaluated",
            "evaluation_failed",
            "promoted",
            "rejected",
        }
        if state and state not in allowed:
            raise ValueError("invalid experience state")
        sql = "SELECT * FROM experience_reviews"
        params: list[Any] = []
        if state:
            sql += " WHERE state=?"
            params.append(state)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 100)))
        values = []
        for row in self.connection.execute(sql, tuple(params)).fetchall():
            value = dict(row)
            value["eligible"] = bool(value["eligible"])
            value["eligibility_reasons"] = json.loads(
                value.pop("eligibility_reasons_json")
            )
            values.append(value)
        return tuple(values)








    def promote_confirmed_experience(
        self,
        experience_id: str,
        knowledge_evidence_id: str,
        actor: str,
        review_note: str = "",
    ) -> Mapping[str, Any]:
        """Publish a human-confirmed Question-SQL pair as stable retrieval memory."""

        item = self.get_experience(experience_id)
        if item["state"] == "promoted":
            return item
        if (
            item["state"] != "candidate"
            or not item["eligible"]
            or item["user_feedback"] != "correct"
        ):
            raise ValueError("only a human-confirmed candidate can be promoted")
        if not knowledge_evidence_id.strip() or not actor.strip():
            raise ValueError("knowledge evidence and actor are required")
        with self.connection:
            self.connection.execute(
                "UPDATE experience_reviews SET state='promoted',knowledge_evidence_id=?,"
                "reviewed_by=?,reviewed_at=?,review_note=? WHERE experience_id=?",
                (
                    knowledge_evidence_id[:200],
                    actor.strip()[:200],
                    _now(),
                    review_note.strip()[:2000],
                    experience_id,
                ),
            )
        return self.get_experience(experience_id)

    def revoke_confirmed_experience(
        self,
        experience_id: str,
        actor: str,
        reason: str,
    ) -> Mapping[str, Any]:
        """Retain audit history while removing a mistaken Q-SQL confirmation."""

        item = self.get_experience(experience_id)
        if item["state"] != "promoted":
            raise ValueError("only a promoted Question-SQL can be revoked")
        if not actor.strip() or not reason.strip():
            raise ValueError("actor and revocation reason are required")
        with self.connection:
            self.connection.execute(
                "UPDATE experience_reviews SET state='rejected',user_feedback='incorrect',"
                "feedback_note=?,reviewed_by=?,reviewed_at=?,review_note=? "
                "WHERE experience_id=?",
                (
                    reason.strip()[:2000],
                    actor.strip()[:200],
                    _now(),
                    reason.strip()[:2000],
                    experience_id,
                ),
            )
        return self.get_experience(experience_id)

    def propose_policy(
        self,
        artifact: Mapping[str, Any],
        target_skill: str,
        change_reason: str,
        created_by: str,
        parent_version: str = "",
        proposal_metadata: Optional[Mapping[str, Any]] = None,
    ) -> str:
        if target_skill not in TEXT2SQL_SKILLS:
            raise ValueError("invalid target skill")
        if not change_reason.strip() or not created_by.strip():
            raise ValueError("change_reason and created_by are required")
        parent_version = parent_version or self.active_policy_version
        parent = self.get_policy(parent_version)
        candidate = PolicyArtifact.from_dict(artifact, self.snapshot)
        require_single_skill_change(parent, candidate, target_skill)
        if proposal_metadata is not None and not isinstance(
            proposal_metadata, Mapping
        ):
            raise ValueError("proposal metadata must be an object")
        metadata = dict(proposal_metadata or {})

        def valid_memory_ids(value: Any) -> set[str]:
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                return set()
            return {
                str(item).strip()[:100]
                for item in value
                if str(item).strip().startswith("memory-")
            }

        field_map = {
            "prompt_fragment": "prompt_fragments",
            "field_aliases": "field_aliases",
            "value_aliases": "value_aliases",
            "few_shot_examples": "few_shot_examples",
            "allowed_tools": "tool_selection_policy",
            "budget_parameters": "budget_parameters",
        }
        parent_value = parent.as_dict()
        candidate_value = candidate.as_dict()
        changed_fields = {
            public_name
            for public_name, artifact_name in field_map.items()
            if parent_value[artifact_name][target_skill]
            != candidate_value[artifact_name][target_skill]
        }
        changed_slots = {
            "%s.%s" % (target_skill, field) for field in changed_fields
        }
        inherited_bindings = {
            memory_id: set(slots)
            for memory_id, slots in self.policy_source_memory_bindings(
                parent_version
            ).items()
        }
        explicit_ids = valid_memory_ids(metadata.get("memory_ids"))
        dropped_ids = valid_memory_ids(metadata.get("drop_memory_ids"))
        role_wildcard = "%s.*" % target_skill
        for memory_id in tuple(inherited_bindings):
            slots = inherited_bindings[memory_id]
            slots.difference_update(changed_slots)
            if changed_fields:
                slots.discard(role_wildcard)
            if not slots:
                inherited_bindings.pop(memory_id, None)

        raw_field_bindings = metadata.get("memory_field_bindings")
        field_bindings = (
            raw_field_bindings if isinstance(raw_field_bindings, Mapping) else {}
        )
        for memory_id in explicit_ids:
            raw_fields = field_bindings.get(memory_id)
            requested_fields = (
                {
                    str(field)
                    for field in raw_fields
                    if str(field) in field_map
                }
                if isinstance(raw_fields, Sequence)
                and not isinstance(raw_fields, (str, bytes))
                else set(changed_fields)
            )
            inherited_bindings.setdefault(memory_id, set()).update(
                "%s.%s" % (target_skill, field)
                for field in requested_fields.intersection(changed_fields)
            )
        for memory_id in dropped_ids:
            inherited_bindings.pop(memory_id, None)
        inherited_bindings = {
            memory_id: slots
            for memory_id, slots in inherited_bindings.items()
            if slots
        }
        metadata["memory_ids"] = sorted(explicit_ids)
        metadata["compiled_memory_ids"] = sorted(inherited_bindings)
        metadata["compiled_memory_fields"] = {
            memory_id: sorted(slots)
            for memory_id, slots in sorted(inherited_bindings.items())
        }
        if dropped_ids:
            metadata["drop_memory_ids"] = sorted(dropped_ids)
        semantic_rules = []
        if any(key.startswith("semantic_rule") for key in metadata):
            if (changed_fields != {"prompt_fragment"}
                    or metadata.get("contract") != "ExperiencePolicyProposal/v1"
                    or metadata.get("source") != "confirmed-experiences"
                    or metadata.get("target_replay_required") is not True):
                raise ValueError("SemanticRule Policy requires prompt-only change and target replay")
            semantic_rules = self.validate_semantic_rule_sources(metadata, sorted(explicit_ids), target_skill)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO policy_versions(
                    policy_version,parent_version,target_skill,artifact_json,status,
                    change_reason,created_by,created_at,proposal_metadata_json
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    candidate.version,
                    parent_version,
                    target_skill,
                    _canonical(candidate.as_dict()),
                    "candidate",
                    change_reason.strip()[:2000],
                    created_by.strip()[:200],
                    _now(),
                    _canonical(metadata),
                ),
            )
            for rule in semantic_rules:
                self.connection.execute(
                    "INSERT INTO policy_semantic_rule_sources(policy_version,rule_id,content_sha256) VALUES (?,?,?)",
                    (candidate.version, rule["rule_id"], rule["content_sha256"]),
                )
        return candidate.version

    @staticmethod
    def _aggregates_only(
        report: Mapping[str, Any], meta: Optional[Mapping[str, Any]] = None
    ) -> Mapping[str, Any]:
        value: dict[str, Any] = {
            "version_pins": dict(report.get("version_pins") or {}),
            "overall": dict(report.get("overall") or {}),
            "splits": {
                split: dict(metrics)
                for split, metrics in (report.get("splits") or {}).items()
            },
        }
        if meta:
            value["evaluation_identity"] = {
                "model": dict(meta.get("model") or {}),
                "runtime": dict(meta.get("runtime") or {}),
                "principals": list(meta.get("principals") or ()),
            }
        return value

    @staticmethod
    def _unwrap_evaluation_artifact(
        value: Mapping[str, Any], dataset_manifest: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        if "report" not in value:
            return value, {}
        if value.get("contract_version") != EVALUATION_ARTIFACT_CONTRACT_VERSION:
            raise ValueError("evaluation artifact contract_version mismatch")
        if value.get("status") != "complete":
            raise ValueError("evaluation artifact must have complete status")
        report = value.get("report")
        if not isinstance(report, Mapping):
            raise ValueError("evaluation artifact report must be an object")
        version_pins = report.get("version_pins")
        required_pin_keys = {
            "database_snapshot_id",
            "wiki_index_version",
            "vanna_index_version",
            "memory_snapshot_id",
            "policy_version",
        }
        if (
            not isinstance(version_pins, Mapping)
            or set(version_pins) != required_pin_keys
            or any(
                type(version_pins.get(name)) is not str
                or not str(version_pins[name]).strip()
                for name in required_pin_keys
            )
        ):
            raise ValueError("evaluation report version_pins contract is invalid")
        if value.get("dataset_id") != dataset_manifest.get("dataset_id"):
            raise ValueError("evaluation artifact dataset_id mismatch")
        if value.get("dataset_sha256") != dataset_manifest.get("dataset_sha256"):
            raise ValueError("evaluation artifact dataset hash mismatch")
        evaluated_splits = value.get("evaluated_splits")
        if (
            not isinstance(evaluated_splits, Sequence)
            or isinstance(evaluated_splits, (str, bytes, bytearray))
            or any(type(split) is not str for split in evaluated_splits)
        ):
            raise ValueError("evaluation artifact evaluated_splits must be a list of strings")
        required = {"validation", "sealed_holdout"}
        if not required.issubset(set(evaluated_splits)):
            raise ValueError("promotion requires validation and sealed_holdout in one pinned run")
        evaluated_case_count = value.get("evaluated_case_count")
        if type(evaluated_case_count) is not int or evaluated_case_count < 0:
            raise ValueError(
                "evaluation artifact evaluated_case_count must be a non-negative native integer"
            )
        identity = validate_evaluation_identity(value)
        return report, {
            "model": dict(identity["model"]),
            "runtime": dict(identity["runtime"]),
            "principals": tuple(identity["principals"]),
            "evaluated_case_count": evaluated_case_count,
            "memory_candidate_id": str(value.get("memory_candidate_id") or ""),
            "experience_candidate_id": str(
                value.get("experience_candidate_id") or ""
            ),
        }

    def record_evaluation(
        self,
        candidate_version: str,
        dataset_manifest: Mapping[str, Any],
        baseline_report: Mapping[str, Any],
        candidate_report: Mapping[str, Any],
        dataset_review_evidence: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        baseline_report, baseline_meta = self._unwrap_evaluation_artifact(
            baseline_report, dataset_manifest
        )
        candidate_report, candidate_meta = self._unwrap_evaluation_artifact(
            candidate_report, dataset_manifest
        )
        if not baseline_meta or not candidate_meta:
            raise ValueError("policy promotion requires pinned evaluation artifacts")
        if baseline_meta["model"] != candidate_meta["model"]:
            raise ValueError("baseline and candidate model configuration mismatch")
        if baseline_meta["principals"] != candidate_meta["principals"]:
            raise ValueError("baseline and candidate principal set mismatch")
        required_count = sum(
            int(((dataset_manifest.get("files") or {}).get(split) or {}).get("case_count") or 0)
            for split in ("validation", "sealed_holdout")
        )
        if (
            baseline_meta["evaluated_case_count"] != required_count
            or candidate_meta["evaluated_case_count"] != required_count
        ):
            raise ValueError("promotion evaluation did not cover the complete required splits")

        row = self.connection.execute(
            "SELECT parent_version,status,proposal_metadata_json "
            "FROM policy_versions WHERE policy_version=?",
            (candidate_version,),
        ).fetchone()
        if not row or row["status"] not in {"candidate", "evaluated", "shadow_ready"}:
            raise ValueError("policy is not an evaluable candidate")
        try:
            proposal_metadata = json.loads(
                str(row["proposal_metadata_json"] or "{}")
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            proposal_metadata = {}
        target_replay: Mapping[str, Any] = {}
        if (
            isinstance(proposal_metadata, Mapping)
            and proposal_metadata.get("target_replay_required") is True
        ):
            target_replay = self.latest_target_replay(candidate_version)
            if not target_replay or target_replay.get("status") != "passed":
                raise ValueError(
                    "Experience-driven Policy requires a passed target replay"
                )
            validated_replay = validate_target_replay_artifact(
                dict(target_replay.get("artifact") or {}),
                candidate_policy_version=candidate_version,
            )
            self.validate_experience_policy_lineage(
                candidate_version,
                tuple(validated_replay.get("memory_ids") or ()),
            )

        def split_runtime(
            meta: Mapping[str, Any],
        ) -> tuple[Mapping[str, Any], tuple[str, ...]]:
            runtime = dict(meta["runtime"])
            raw_ids = runtime.pop("policy_source_memory_ids", None)
            if (
                not isinstance(raw_ids, Sequence)
                or isinstance(raw_ids, (str, bytes, bytearray))
                or any(type(memory_id) is not str for memory_id in raw_ids)
            ):
                raise ValueError(
                    "evaluation runtime policy_source_memory_ids must be a list of strings"
                )
            ids = tuple(sorted(raw_ids))
            if len(set(ids)) != len(ids):
                raise ValueError(
                    "evaluation runtime policy_source_memory_ids must not contain duplicates"
                )
            return runtime, ids

        baseline_runtime, baseline_memory_ids = split_runtime(baseline_meta)
        candidate_runtime, candidate_memory_ids = split_runtime(candidate_meta)
        if baseline_runtime != candidate_runtime:
            raise ValueError("baseline and candidate runtime identity mismatch")
        expected_baseline_memory_ids = self.policy_source_memory_ids(
            str(row["parent_version"])
        )
        expected_candidate_memory_ids = self.policy_source_memory_ids(candidate_version)
        if baseline_memory_ids != expected_baseline_memory_ids:
            raise ValueError("baseline runtime Policy-Memory lineage mismatch")
        if candidate_memory_ids != expected_candidate_memory_ids:
            raise ValueError("candidate runtime Policy-Memory lineage mismatch")
        if any(
            baseline_meta.get(field) or candidate_meta.get(field)
            for field in ("memory_candidate_id", "experience_candidate_id")
        ):
            raise ValueError(
                "policy promotion cannot use Memory or Q-SQL candidate artifacts"
            )
        baseline_pins = dict(baseline_report.get("version_pins") or {})
        candidate_pins = dict(candidate_report.get("version_pins") or {})
        for name in (
            "database_snapshot_id",
            "wiki_index_version",
            "vanna_index_version",
            "memory_snapshot_id",
        ):
            if baseline_pins.get(name) != candidate_pins.get(name):
                raise ValueError(
                    "%s changed between Policy baseline and candidate" % name
                )
        if baseline_report.get("version_pins", {}).get("policy_version") != row["parent_version"]:
            raise ValueError("baseline report policy version does not match candidate parent")
        if candidate_report.get("version_pins", {}).get("policy_version") != candidate_version:
            raise ValueError("candidate report policy version mismatch")
        if target_replay:
            replay_artifact = dict(target_replay.get("artifact") or {})
            replay_identity = dict(replay_artifact.get("replay_identity") or {})
            replay_pins = dict(replay_identity.get("shared_version_pins") or {})
            for name in (
                "database_snapshot_id",
                "wiki_index_version",
                "vanna_index_version",
                "memory_snapshot_id",
            ):
                if replay_pins.get(name) != candidate_pins.get(name):
                    raise ValueError(
                        "target replay %s does not match release evaluation" % name
                    )
        decision = evaluate_promotion_gate(
            dataset_manifest,
            baseline_report,
            candidate_report,
            dataset_review_evidence,
        )
        run_id = "evolution-run-%s" % uuid.uuid4().hex
        next_status = "shadow_ready" if decision["eligible_for_human_approval"] else "evaluated"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO evolution_runs(
                    run_id,baseline_policy_version,candidate_policy_version,dataset_id,
                    dataset_sha256,baseline_aggregate_json,candidate_aggregate_json,
                    decision_json,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    row["parent_version"],
                    candidate_version,
                    str(dataset_manifest.get("dataset_id") or ""),
                    str(dataset_manifest.get("dataset_sha256") or ""),
                    _canonical(self._aggregates_only(baseline_report, baseline_meta)),
                    _canonical(self._aggregates_only(candidate_report, candidate_meta)),
                    _canonical(decision),
                    _now(),
                ),
            )
            self.connection.execute(
                "UPDATE policy_versions SET status=? WHERE policy_version=?",
                (next_status, candidate_version),
            )
        return {"run_id": run_id, "candidate_status": next_status, **decision}

    def activate_policy(
        self,
        candidate_version: str,
        actor: str,
        reason: str,
        human_approved: bool,
        current_version_pins: Optional[Mapping[str, str]] = None,
        current_evaluation_identity: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if not human_approved:
            raise ValueError("explicit human approval is required")
        if not actor.strip() or not reason.strip():
            raise ValueError("actor and approval reason are required")
        row = self.connection.execute(
            "SELECT status,parent_version,proposal_metadata_json "
            "FROM policy_versions WHERE policy_version=?",
            (candidate_version,),
        ).fetchone()
        if not row or row["status"] != "canary_passed":
            raise ValueError("only a candidate that passed shadow and canary can be activated")
        previous = self.active_policy_version
        if row["parent_version"] != previous:
            raise ValueError("candidate parent is no longer the active policy; replay is required")
        deployment = self.connection.execute(
            "SELECT stable_policy_version,version_pins_json,evaluation_identity_json "
            "FROM shadow_deployments WHERE candidate_policy_version=? "
            "AND status='canary_passed' ORDER BY updated_at DESC LIMIT 1",
            (candidate_version,),
        ).fetchone()
        if not deployment:
            raise ValueError("candidate lacks a completed canary deployment")
        if current_version_pins is None or current_evaluation_identity is None:
            raise ValueError("current release identity is required for Policy activation")
        if dict(current_version_pins) != json.loads(deployment["version_pins_json"]):
            raise ValueError("version pins changed after canary; replay is required")
        try:
            proposal_metadata = json.loads(
                str(row["proposal_metadata_json"] or "{}")
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            proposal_metadata = {}
        if (
            isinstance(proposal_metadata, Mapping)
            and proposal_metadata.get("target_replay_required") is True
        ):
            replay = self.latest_target_replay(candidate_version)
            if not replay or replay.get("status") != "passed":
                raise ValueError("passed target replay is required for Policy activation")
            artifact = validate_target_replay_artifact(
                dict(replay.get("artifact") or {}),
                candidate_policy_version=candidate_version,
            )
            self.validate_experience_policy_lineage(
                candidate_version,
                tuple(artifact.get("memory_ids") or ()),
            )
            replay_pins = dict(
                (artifact.get("replay_identity") or {}).get(
                    "shared_version_pins"
                )
                or {}
            )
            for name in (
                "database_snapshot_id",
                "wiki_index_version",
                "vanna_index_version",
                "memory_snapshot_id",
            ):
                if replay_pins.get(name) != current_version_pins.get(name):
                    raise ValueError(
                        "target replay identity changed before activation"
                    )
        current_identity = validate_evaluation_identity(
            current_evaluation_identity
        )
        expected_identities = json.loads(deployment["evaluation_identity_json"])
        if current_identity != validate_evaluation_identity(
            expected_identities.get("stable") or {}
        ):
            raise ValueError("stable release identity changed after canary")
        candidate_identity = {
            "model": dict(current_identity["model"]),
            "runtime": dict(current_identity["runtime"]),
            "principals": list(current_identity["principals"]),
        }
        candidate_identity["runtime"]["policy_source_memory_ids"] = list(
            self.policy_source_memory_ids(candidate_version)
        )
        if validate_evaluation_identity(candidate_identity) != (
            validate_evaluation_identity(
                expected_identities.get("candidate") or {}
            )
        ):
            raise ValueError("candidate release identity changed after canary")
        timestamp = _now()
        with self.connection:
            self.connection.execute(
                "UPDATE policy_versions SET status='retired' WHERE policy_version=? AND status='approved'",
                (previous,),
            )
            self.connection.execute(
                "UPDATE policy_versions SET status='approved',reviewed_by=?,reviewed_at=? "
                "WHERE policy_version=?",
                (actor.strip()[:200], timestamp, candidate_version),
            )
            self.connection.execute(
                "UPDATE evolution_metadata SET value=? WHERE key='active_policy_version'",
                (candidate_version,),
            )
            self.connection.execute(
                "UPDATE shadow_deployments SET status='stable',canary_percent=0,shadow_percent=0,"
                "updated_at=? WHERE candidate_policy_version=? AND status='canary_passed'",
                (timestamp, candidate_version),
            )
            self.connection.execute(
                "INSERT INTO activation_audit VALUES (?,?,?,?,?,?,?)",
                (
                    "activation-%s" % uuid.uuid4().hex,
                    "approve",
                    previous,
                    candidate_version,
                    actor.strip()[:200],
                    reason.strip()[:2000],
                    timestamp,
                ),
            )

    def activate_policy_automatically(
        self,
        candidate_version: str,
        actor: str,
        reason: str,
        current_version_pins: Mapping[str, str],
        current_evaluation_identity: Mapping[str, Any],
    ) -> None:
        """Activate a prompt-only candidate after the complete offline gate.

        This is intentionally a separate path from human approval.  It accepts
        only ``shadow_ready`` candidates produced by ``record_evaluation`` and
        rechecks the immutable evaluation identity, current pins, single-role
        Policy lineage, and mandatory Target Replay immediately before the
        atomic switch.  It does not treat an LLM decision as approval.
        """

        if not actor.strip() or not reason.strip():
            raise ValueError("actor and automatic activation reason are required")
        row = self.connection.execute(
            "SELECT status,parent_version,proposal_metadata_json "
            "FROM policy_versions WHERE policy_version=?",
            (candidate_version,),
        ).fetchone()
        if not row or row["status"] != "shadow_ready":
            raise ValueError(
                "automatic activation requires a candidate that passed the full offline gate"
            )
        previous = self.active_policy_version
        if row["parent_version"] != previous:
            raise ValueError(
                "candidate parent is no longer active; replay and evaluation are stale"
            )
        run = self.connection.execute(
            "SELECT baseline_aggregate_json,candidate_aggregate_json,decision_json "
            "FROM evolution_runs WHERE candidate_policy_version=? "
            "ORDER BY created_at DESC,run_id DESC LIMIT 1",
            (candidate_version,),
        ).fetchone()
        if not run:
            raise ValueError("automatic activation requires a recorded release evaluation")
        baseline = json.loads(run["baseline_aggregate_json"])
        candidate = json.loads(run["candidate_aggregate_json"])
        decision = json.loads(run["decision_json"])
        if (
            decision.get("eligible_for_human_approval") is not True
            or list(decision.get("reasons") or ())
        ):
            raise ValueError("recorded release evaluation is not eligible")

        baseline_pins = dict(baseline.get("version_pins") or {})
        candidate_pins = dict(candidate.get("version_pins") or {})
        if dict(current_version_pins) != baseline_pins:
            raise ValueError("runtime pins changed after release evaluation")
        if candidate_pins.get("policy_version") != candidate_version:
            raise ValueError("candidate evaluation Policy version mismatch")
        for name in (
            "database_snapshot_id",
            "wiki_index_version",
            "vanna_index_version",
            "memory_snapshot_id",
        ):
            if candidate_pins.get(name) != baseline_pins.get(name):
                raise ValueError("candidate evaluation %s drifted" % name)

        current_identity = validate_evaluation_identity(
            current_evaluation_identity
        )
        baseline_identity = validate_evaluation_identity(
            baseline.get("evaluation_identity") or {}
        )
        if current_identity != baseline_identity:
            raise ValueError("runtime identity changed after release evaluation")
        expected_candidate_identity = {
            "model": dict(current_identity["model"]),
            "runtime": dict(current_identity["runtime"]),
            "principals": list(current_identity["principals"]),
        }
        expected_candidate_identity["runtime"][
            "policy_source_memory_ids"
        ] = list(self.policy_source_memory_ids(candidate_version))
        if validate_evaluation_identity(expected_candidate_identity) != (
            validate_evaluation_identity(
                candidate.get("evaluation_identity") or {}
            )
        ):
            raise ValueError("candidate evaluation identity mismatch")

        try:
            proposal_metadata = json.loads(
                str(row["proposal_metadata_json"] or "{}")
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            proposal_metadata = {}
        if (
            not isinstance(proposal_metadata, Mapping)
            or proposal_metadata.get("contract")
            != "ExperiencePolicyProposal/v1"
            or proposal_metadata.get("source") != "confirmed-experiences"
            or proposal_metadata.get("target_replay_required") is not True
            or proposal_metadata.get("semantic_rule_compilation")
            != "SemanticRulePolicyCompilation/v1"
        ):
            raise ValueError(
                "automatic activation accepts only SemanticRule-compiled "
                "Experience Policy candidates"
            )
        replay = self.latest_target_replay(candidate_version)
        if not replay or replay.get("status") != "passed":
            raise ValueError("passed Target Replay is required")
        artifact = validate_target_replay_artifact(
            dict(replay.get("artifact") or {}),
            candidate_policy_version=candidate_version,
        )
        self.validate_experience_policy_lineage(
            candidate_version,
            tuple(artifact.get("memory_ids") or ()),
        )
        replay_pins = dict(
            (artifact.get("replay_identity") or {}).get(
                "shared_version_pins"
            )
            or {}
        )
        for name in (
            "database_snapshot_id",
            "wiki_index_version",
            "vanna_index_version",
            "memory_snapshot_id",
        ):
            if replay_pins.get(name) != baseline_pins.get(name):
                raise ValueError("Target Replay identity changed before activation")

        timestamp = _now()
        with self.connection:
            self.connection.execute(
                "UPDATE policy_versions SET status='retired' "
                "WHERE policy_version=? AND status='approved'",
                (previous,),
            )
            self.connection.execute(
                "UPDATE policy_versions SET status='approved',reviewed_by=?,reviewed_at=? "
                "WHERE policy_version=?",
                (actor.strip()[:200], timestamp, candidate_version),
            )
            self.connection.execute(
                "UPDATE evolution_metadata SET value=? WHERE key='active_policy_version'",
                (candidate_version,),
            )
            self.connection.execute(
                "INSERT INTO activation_audit VALUES (?,?,?,?,?,?,?)",
                (
                    "activation-%s" % uuid.uuid4().hex,
                    "automatic_offline_gate_approve",
                    previous,
                    candidate_version,
                    actor.strip()[:200],
                    reason.strip()[:2000],
                    timestamp,
                ),
            )

    def rollback(self, target_version: str, actor: str, reason: str) -> None:
        if not actor.strip() or not reason.strip():
            raise ValueError("actor and rollback reason are required")
        row = self.connection.execute(
            "SELECT status FROM policy_versions WHERE policy_version=?", (target_version,)
        ).fetchone()
        if not row or row["status"] not in {"approved", "retired"}:
            raise ValueError("rollback target must be a previously approved policy")
        previous = self.active_policy_version
        if previous == target_version:
            return
        timestamp = _now()
        has_shadow_table = bool(
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shadow_deployments'"
            ).fetchone()
        )
        with self.connection:
            self.connection.execute(
                "UPDATE policy_versions SET status='retired' WHERE policy_version=?",
                (previous,),
            )
            self.connection.execute(
                "UPDATE policy_versions SET status='approved',reviewed_by=?,reviewed_at=? WHERE policy_version=?",
                (actor.strip()[:200], timestamp, target_version),
            )
            self.connection.execute(
                "UPDATE evolution_metadata SET value=? WHERE key='active_policy_version'",
                (target_version,),
            )
            if has_shadow_table:
                self.connection.execute(
                    "UPDATE shadow_deployments SET status='rolled_back',shadow_percent=0,"
                    "canary_percent=0,updated_at=? WHERE candidate_policy_version=?",
                    (timestamp, previous),
                )
            self.connection.execute(
                "INSERT INTO activation_audit VALUES (?,?,?,?,?,?,?)",
                (
                    "activation-%s" % uuid.uuid4().hex,
                    "rollback",
                    previous,
                    target_version,
                    actor.strip()[:200],
                    reason.strip()[:2000],
                    timestamp,
                ),
            )

    def add_experience_memory(
        self,
        experience: Mapping[str, Any],
        *,
        origin_split: str = "production_feedback",
    ) -> str:
        """Persist one immutable, non-runtime semantic Experience.

        The idempotency key is the reviewed source identity plus its sanitized
        proof hash.  Replaying the exact extraction returns the original id;
        trying to attach different semantics to the same proof fails closed.
        """

        if origin_split == "sealed_holdout":
            raise ValueError("sealed holdout content may not enter memory")
        if origin_split not in {"train", "production_feedback"}:
            raise ValueError("only train or production feedback may create memory")
        normalized = normalize_experience_memory(experience)
        state = str(normalized["state"])
        if state not in EXPERIENCE_INITIAL_STATES:
            raise ValueError("new experience must be candidate or needs_evidence")
        target_agent = str(normalized["target_agent"])
        if target_agent and target_agent not in TEXT2SQL_SKILLS:
            raise ValueError("invalid target agent")
        if state == "candidate" and target_agent not in TEXT2SQL_SKILLS:
            raise ValueError("candidate experience requires a valid target agent")

        evidence_sha256 = experience_evidence_sha256(normalized)
        fingerprint = experience_memory_fingerprint(normalized)
        source_task_id = str(normalized["source_task_id"])
        problem_code = str(normalized["problem_code"])
        existing = self.connection.execute(
            "SELECT memory_id,target_skill,failure_kind,rule_json,rule_fingerprint,"
            "evidence_json,state,source_stage,source_revision,evidence_sha256,"
            "runtime_eligible,state_version FROM memory_items "
            "WHERE source_task_id=? AND failure_kind=? AND evidence_sha256=? LIMIT 1",
            (source_task_id, problem_code, evidence_sha256),
        ).fetchone()
        if existing:
            decoded = _decode_memory_row(existing)
            same_payload = (
                str(decoded.get("rule_fingerprint") or "") == fingerprint
                and str(decoded.get("source_stage") or "")
                == str(normalized["source_stage"])
                and int(decoded.get("source_revision") or 1)
                == int(normalized["source_revision"])
                and not bool(decoded.get("runtime_eligible"))
            )
            if not same_payload:
                raise ValueError(
                    "same evidence already belongs to a different experience; "
                    "create a new evidence revision"
                )
            return str(existing["memory_id"])

        memory_id = "memory-%s" % hashlib.sha256(
            _canonical([source_task_id, problem_code, evidence_sha256]).encode("utf-8")
        ).hexdigest()[:24]
        normalized = normalize_experience_memory(
            normalized,
            memory_id=memory_id,
            state=state,
        )
        evidence_payload = experience_evidence_payload(normalized)
        content = render_experience_memory(normalized)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO memory_items(
                    memory_id,target_skill,origin_split,failure_kind,content,rule_json,
                    rule_fingerprint,source_case_ids_json,occurrence_count,evidence_json,
                    state,created_at,source_task_id,source_stage,source_revision,
                    evidence_sha256,runtime_eligible,state_version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    memory_id,
                    target_agent,
                    origin_split,
                    problem_code,
                    content,
                    _canonical(normalized),
                    fingerprint,
                    "[]",
                    1,
                    _canonical(evidence_payload),
                    state,
                    _now(),
                    source_task_id,
                    str(normalized["source_stage"]),
                    int(normalized["source_revision"]),
                    evidence_sha256,
                    0,
                    1,
                ),
            )
        return memory_id

    def add_memory_candidate(
        self,
        target_skill: str,
        failure_kind: str,
        content: str,
        evidence: Mapping[str, Any],
        origin_split: str = "train",
        *,
        rule: Optional[Mapping[str, Any]] = None,
    ) -> str:
        if target_skill not in TEXT2SQL_SKILLS:
            raise ValueError("invalid target skill")
        if origin_split == "sealed_holdout":
            raise ValueError("sealed holdout content may not enter memory")
        if origin_split not in {"train", "production_feedback"}:
            raise ValueError("only train or production feedback may create memory")
        failure_kind = failure_kind.strip()[:100]
        content = content.strip()
        if not failure_kind or not content or len(content) > 3000:
            raise ValueError("bounded failure_kind and memory content are required")
        safe_evidence = sanitize_memory_evidence(evidence)
        if not isinstance(safe_evidence, Mapping):
            safe_evidence = {}
        explicit_sources = (
            tuple(str(item) for item in rule.get("source_case_ids") or ())
            if isinstance(rule, Mapping)
            else ()
        )
        source_case_ids = _memory_source_case_ids(safe_evidence, explicit_sources)
        normalized_rule = normalize_memory_rule(
            rule,
            failure_kind=failure_kind,
            content=content,
            source_case_ids=list(source_case_ids),
        )
        content = render_memory_rule(normalized_rule)
        rule_fingerprint = memory_rule_fingerprint(
            target_skill, failure_kind, normalized_rule
        )
        evidence_bundle = _memory_evidence_bundle(
            {}, safe_evidence, source_case_ids
        )
        # Identical semantics are one rule with multiple source cases.  Merge
        # only provenance; a reviewed/stable rule's behavior never changes.
        existing = self.connection.execute(
            "SELECT memory_id,rule_json,evidence_json,source_case_ids_json,occurrence_count "
            "FROM memory_items WHERE target_skill=? AND failure_kind=? "
            "AND rule_fingerprint=? AND state NOT IN "
            "('rejected','retired','evaluation_failed') "
            "ORDER BY CASE state WHEN 'stable' THEN 0 WHEN 'evaluated' THEN 1 "
            "WHEN 'evaluating' THEN 2 WHEN 'approved' THEN 3 ELSE 4 END,created_at LIMIT 1",
            (target_skill, failure_kind, rule_fingerprint),
        ).fetchone()
        if existing:
            prior_evidence = json.loads(str(existing["evidence_json"] or "{}"))
            if not isinstance(prior_evidence, Mapping):
                prior_evidence = {}
            prior_sources = json.loads(
                str(existing["source_case_ids_json"] or "[]")
            )
            merged_sources = tuple(
                list(
                    dict.fromkeys(
                        [
                            *(str(item) for item in prior_sources),
                            *source_case_ids,
                        ]
                    )
                )[:50]
            )
            try:
                prior_rule = json.loads(str(existing["rule_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                prior_rule = {}
            if not isinstance(prior_rule, Mapping):
                prior_rule = {}
            prior_rule = dict(prior_rule)
            prior_rule["observations"] = list(
                dict.fromkeys(
                    [
                        *(str(item) for item in prior_rule.get("observations") or ()),
                        *(
                            str(item)
                            for item in normalized_rule.get("observations") or ()
                        ),
                    ]
                )
            )[:20]
            merged_rule = normalize_memory_rule(
                prior_rule,
                failure_kind=failure_kind,
                content=content,
                source_case_ids=list(merged_sources),
            )
            merged_evidence = _memory_evidence_bundle(
                prior_evidence, safe_evidence, merged_sources
            )
            prior_event_count = len(_memory_evidence_events(prior_evidence))
            merged_event_count = len(_memory_evidence_events(merged_evidence))
            added_occurrences = max(0, merged_event_count - prior_event_count)
            with self.connection:
                self.connection.execute(
                    "UPDATE memory_items SET rule_json=?,source_case_ids_json=?,"
                    "occurrence_count=occurrence_count+?,evidence_json=? WHERE memory_id=?",
                    (
                        _canonical(merged_rule),
                        _canonical(merged_sources),
                        added_occurrences,
                        _canonical(merged_evidence),
                        existing["memory_id"],
                    ),
                )
            return str(existing["memory_id"])
        memory_id = "memory-%s" % hashlib.sha256(
            _canonical(
                [
                    target_skill,
                    failure_kind,
                    rule_fingerprint,
                    source_case_ids,
                    evidence_bundle,
                    origin_split,
                ]
            ).encode("utf-8")
        ).hexdigest()[:24]
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO memory_items(
                    memory_id,target_skill,origin_split,failure_kind,content,rule_json,
                    rule_fingerprint,source_case_ids_json,occurrence_count,
                    evidence_json,state,created_at,runtime_eligible,state_version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    memory_id,
                    target_skill,
                    origin_split,
                    failure_kind,
                    content,
                    _canonical(normalized_rule),
                    rule_fingerprint,
                    _canonical(source_case_ids),
                    1,
                    _canonical(evidence_bundle),
                    "candidate",
                    _now(),
                    1,
                    1,
                ),
            )
        return memory_id

    def capture_training_failures(
        self, report: Mapping[str, Any], target_skill: str
    ) -> Sequence[str]:
        ids = []
        for item in report.get("outcomes") or ():
            if item.get("split") != "train" or not item.get("failure_kind"):
                continue
            content = (
                "Failure pattern %s in category %s; review schema grounding, result grain, "
                "filters, aggregation, and deterministic gates before proposing a reusable correction."
                % (item["failure_kind"], item.get("category") or "unknown")
            )
            ids.append(
                self.add_memory_candidate(
                    target_skill,
                    str(item["failure_kind"]),
                    content,
                    {
                        "case_id": str(item.get("case_id") or ""),
                        "sql_skeleton": str(item.get("sql_skeleton") or ""),
                        "candidate_sql_fingerprint": str(
                            item.get("candidate_sql_fingerprint") or ""
                        ),
                    },
                    "train",
                )
            )
        return tuple(ids)

    def get_memory(self, memory_id: str) -> Mapping[str, Any]:
        row = self.connection.execute(
            "SELECT memory_id,target_skill,origin_split,failure_kind,content,rule_json,"
            "rule_fingerprint,source_case_ids_json,occurrence_count,evidence_json,state,"
            "created_at,reviewed_by,reviewed_at,review_note,source_task_id,source_stage,"
            "source_revision,evidence_sha256,runtime_eligible,state_version "
            "FROM memory_items WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
        if not row:
            raise ValueError("unknown memory item")
        return _decode_memory_row(row)

    def update_memory_candidate(
        self,
        memory_id: str,
        target_skill: str,
        failure_kind: str,
        content: str,
        *,
        rule: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        if target_skill not in TEXT2SQL_SKILLS:
            raise ValueError("invalid target skill")
        failure_kind = failure_kind.strip()
        content = content.strip()
        if not failure_kind or not content or len(failure_kind) > 100 or len(content) > 3000:
            raise ValueError("bounded failure_kind and memory content are required")
        current = self.get_memory(memory_id)
        if current.get("rule", {}).get("contract") == EXPERIENCE_MEMORY_CONTRACT:
            raise ValueError(
                "Experience evidence and semantics are immutable; create a new evidence revision"
            )
        if current["state"] != "candidate":
            raise ValueError("memory item is not awaiting review")
        if rule is None and (
            target_skill != current["target_skill"]
            or failure_kind != current["failure_kind"]
        ):
            raise ValueError(
                "changing memory ownership or failure kind requires a structured rule"
            )
        # Provenance-derived conditions are server-owned. The review surface
        # may edit the four semantic fields, but cannot erase AST deltas,
        # observations, or source-case lineage by omitting them from payload.
        raw_rule = dict(current["rule"])
        if isinstance(rule, Mapping):
            for field in ("trigger", "action", "avoid", "rationale"):
                if field in rule:
                    raw_rule[field] = rule[field]
        elif content != current["content"]:
            # Backward-compatible review surfaces edit one text field. Treat
            # that field as the reviewed action while preserving the contract.
            raw_rule["action"] = content
        normalized_rule = normalize_memory_rule(
            raw_rule,
            failure_kind=failure_kind,
            content=content,
            source_case_ids=list(current["source_case_ids"]),
        )
        rendered = render_memory_rule(normalized_rule)
        fingerprint = memory_rule_fingerprint(
            target_skill, failure_kind, normalized_rule
        )
        duplicate = self.connection.execute(
            "SELECT memory_id FROM memory_items WHERE memory_id<>? AND target_skill=? "
            "AND failure_kind=? AND rule_fingerprint=? AND state NOT IN "
            "('rejected','retired','evaluation_failed') LIMIT 1",
            (memory_id, target_skill, failure_kind, fingerprint),
        ).fetchone()
        if duplicate:
            raise ValueError(
                "equivalent memory rule already exists: %s" % duplicate["memory_id"]
            )
        with self.connection:
            changed = self.connection.execute(
                "UPDATE memory_items SET target_skill=?,failure_kind=?,content=?,"
                "rule_json=?,rule_fingerprint=? "
                "WHERE memory_id=? AND state='candidate'",
                (
                    target_skill,
                    failure_kind,
                    rendered,
                    _canonical(normalized_rule),
                    fingerprint,
                    memory_id,
                ),
            ).rowcount
            if not changed:
                raise ValueError("memory item is not awaiting review")
        return self.get_memory(memory_id)

    def review_experience_memory(
        self,
        memory_id: str,
        decision: str,
        actor: str,
        review_note: str = "",
    ) -> Mapping[str, Any]:
        """Apply an explicit human lifecycle decision to one Experience."""

        if decision not in {"confirm", "reject", "needs_evidence"}:
            raise ValueError("invalid experience review decision")
        actor = actor.strip()
        review_note = review_note.strip()
        if not actor:
            raise ValueError("experience review actor is required")
        if decision in {"reject", "needs_evidence"} and not review_note:
            raise ValueError("experience review reason is required")
        current = self.get_memory(memory_id)
        rule = current.get("rule")
        if not isinstance(rule, Mapping) or rule.get("contract") != EXPERIENCE_MEMORY_CONTRACT:
            raise ValueError("memory item is not an ExperienceMemory/v1")
        if current["state"] != "candidate":
            raise ValueError("experience is not awaiting review")
        if bool(current.get("runtime_eligible")):
            raise ValueError("ExperienceMemory/v1 cannot be runtime eligible")
        if decision == "confirm":
            # Revalidate the persisted server-owned contract before promotion.
            normalized = normalize_experience_memory(rule, state="confirmed")
            if normalized["target_agent"] not in TEXT2SQL_SKILLS:
                raise ValueError("confirmed experience requires a valid target agent")
            if experience_evidence_sha256(normalized) != str(
                current.get("evidence_sha256") or ""
            ):
                raise ValueError("experience evidence integrity mismatch")
            if experience_memory_fingerprint(normalized) != str(
                current.get("rule_fingerprint") or ""
            ):
                raise ValueError("experience semantic integrity mismatch")
            if not experience_has_replay_proof(normalized):
                raise ValueError(
                    "confirmed experience requires replay-verifiable proof"
                )
            next_state = "confirmed"
        elif decision == "reject":
            next_state = "rejected"
        else:
            next_state = "needs_evidence"
        with self.connection:
            changed = self.connection.execute(
                "UPDATE memory_items SET state=?,reviewed_by=?,reviewed_at=?,review_note=?,"
                "runtime_eligible=0,state_version=state_version+1 "
                "WHERE memory_id=? AND state='candidate' AND runtime_eligible=0",
                (
                    next_state,
                    actor[:200],
                    _now(),
                    review_note[:2000],
                    memory_id,
                ),
            ).rowcount
            if not changed:
                raise ValueError("experience is not awaiting review")
        return self.get_memory(memory_id)

    def confirmed_experiences(
        self,
        target_agent: str = "",
        limit: int = 50,
    ) -> Sequence[Mapping[str, Any]]:
        """Return bounded, confirmed non-runtime Experiences for Policy work."""

        if target_agent and target_agent not in TEXT2SQL_SKILLS:
            raise ValueError("invalid target agent")
        sql = (
            "SELECT memory_id,target_skill,origin_split,failure_kind,content,rule_json,"
            "rule_fingerprint,source_case_ids_json,occurrence_count,evidence_json,state,"
            "created_at,reviewed_by,reviewed_at,review_note,source_task_id,source_stage,"
            "source_revision,evidence_sha256,runtime_eligible,state_version "
            "FROM memory_items WHERE state='confirmed' AND runtime_eligible=0"
        )
        params: list[Any] = []
        if target_agent:
            sql += " AND target_skill=?"
            params.append(target_agent)
        sql += " ORDER BY reviewed_at DESC,created_at DESC,memory_id LIMIT ?"
        params.append(max(1, min(int(limit), 100)))
        values = []
        for row in self.connection.execute(sql, tuple(params)).fetchall():
            item = _decode_memory_row(row)
            rule = item.get("rule")
            if isinstance(rule, Mapping) and rule.get("contract") == EXPERIENCE_MEMORY_CONTRACT:
                values.append(item)
        return tuple(values)

    def review_memory(
        self,
        memory_id: str,
        decision: str,
        actor: str,
        human_reviewed: bool,
        review_note: str = "",
    ) -> Mapping[str, Any]:
        if not human_reviewed:
            raise ValueError("explicit human review is required")
        current = self.get_memory(memory_id)
        if current.get("rule", {}).get("contract") == EXPERIENCE_MEMORY_CONTRACT:
            mapped_decision = {
                "approve": "confirm",
                "confirm": "confirm",
                "reject": "reject",
                "needs_evidence": "needs_evidence",
            }.get(decision)
            if not mapped_decision:
                raise ValueError("invalid experience review decision")
            return self.review_experience_memory(
                memory_id,
                mapped_decision,
                actor,
                review_note,
            )
        if decision not in {"approve", "reject"} or not actor.strip():
            raise ValueError("decision and actor are required")
        review_note = review_note.strip()
        if decision == "reject" and not review_note:
            raise ValueError("rejection reason is required")
        if current["state"] != "candidate":
            raise ValueError("memory item is not awaiting review")
        with self.connection:
            changed = self.connection.execute(
                "UPDATE memory_items SET state=?,reviewed_by=?,reviewed_at=?,review_note=? "
                "WHERE memory_id=? AND state='candidate'",
                (
                    "approved" if decision == "approve" else "rejected",
                    actor.strip()[:200],
                    _now(),
                    review_note[:2000],
                    memory_id,
                ),
            ).rowcount
            if not changed:
                raise ValueError("memory item is not awaiting review")
        return self.get_memory(memory_id)

    @property
    def memory_snapshot_id(self) -> str:
        return self.memory_snapshot_id_for()

    def runtime_memory_snapshot(
        self, candidate_memory_id: str = ""
    ) -> Mapping[str, Any]:
        """Atomically materialize the Memory version and every role's runtime pool."""

        rows = self.connection.execute(
            "SELECT memory_id,target_skill,failure_kind,content,rule_json,"
            "rule_fingerprint,state,reviewed_at,runtime_eligible,state_version "
            "FROM memory_items WHERE (state='stable' AND runtime_eligible=1) "
            "OR memory_id=? "
            "ORDER BY target_skill,reviewed_at DESC,memory_id",
            (candidate_memory_id,),
        ).fetchall()
        stable_rows = [row for row in rows if str(row["state"]) == "stable"]
        candidate = next(
            (
                row
                for row in rows
                if candidate_memory_id
                and str(row["memory_id"]) == candidate_memory_id
            ),
            None,
        )
        if candidate_memory_id and (
            candidate is None
            or not bool(candidate["runtime_eligible"])
            or str(candidate["state"])
            not in {"approved", "evaluating", "evaluated", "evaluation_failed"}
        ):
            raise ValueError("memory candidate is not approved for evaluation")

        snapshot_values = []
        snapshot_seen = set()
        for row in sorted(stable_rows, key=lambda item: str(item["memory_id"])):
            marker = (str(row["target_skill"]), str(row["rule_fingerprint"]))
            if marker in snapshot_seen:
                continue
            snapshot_seen.add(marker)
            snapshot_values.append(
                {
                    "memory_id": row["memory_id"],
                    "target_skill": row["target_skill"],
                    "rule_fingerprint": row["rule_fingerprint"],
                    "content": row["content"],
                }
            )
        if candidate is not None:
            snapshot_values.append(
                {
                    "memory_id": candidate["memory_id"],
                    "target_skill": candidate["target_skill"],
                    "rule_fingerprint": candidate["rule_fingerprint"],
                    "content": candidate["content"],
                }
            )
            snapshot_values.sort(key=lambda item: str(item["memory_id"]))

        pools: dict[str, list[Mapping[str, Any]]] = {
            skill: [] for skill in TEXT2SQL_SKILLS
        }
        seen_by_skill: dict[str, set[str]] = {
            skill: set() for skill in TEXT2SQL_SKILLS
        }
        if candidate is not None:
            skill = str(candidate["target_skill"])
            pools[skill].append(_decode_memory_row(candidate))
            seen_by_skill[skill].add(str(candidate["rule_fingerprint"]))
        for row in stable_rows:
            skill = str(row["target_skill"])
            fingerprint = str(row["rule_fingerprint"])
            if fingerprint in seen_by_skill[skill] or len(pools[skill]) >= 50:
                continue
            seen_by_skill[skill].add(fingerprint)
            pools[skill].append(_decode_memory_row(row))
        return {
            "memory_snapshot_id": "memory-%s"
            % hashlib.sha256(
                _canonical(snapshot_values).encode("utf-8")
            ).hexdigest()[:20],
            "items": {
                skill: tuple(values) for skill, values in pools.items()
            },
        }

    def memory_snapshot_id_for(self, candidate_memory_id: str = "") -> str:
        return str(
            self.runtime_memory_snapshot(candidate_memory_id)[
                "memory_snapshot_id"
            ]
        )

    def evaluation_memory(
        self, target_skill: str, candidate_memory_id: str, limit: int = 6
    ) -> Sequence[Mapping[str, Any]]:
        values = list(self.stable_memory(target_skill, limit))
        candidate = self.get_memory(candidate_memory_id)
        if not candidate.get("runtime_eligible"):
            raise ValueError("ExperienceMemory/v1 cannot enter runtime Memory")
        if candidate["state"] not in {
            "approved",
            "evaluating",
            "evaluated",
            "evaluation_failed",
        }:
            raise ValueError("memory candidate is not approved for evaluation")
        if candidate["target_skill"] == target_skill:
            values = [
                {
                    "memory_id": candidate["memory_id"],
                    "failure_kind": candidate["failure_kind"],
                    "content": candidate["content"],
                    "rule": candidate["rule"],
                    "rule_fingerprint": candidate["rule_fingerprint"],
                },
                *[
                    item
                    for item in values
                    if item["memory_id"] != candidate["memory_id"]
                    and item.get("rule_fingerprint")
                    != candidate["rule_fingerprint"]
                ],
            ][: max(1, min(int(limit), 50))]
        return tuple(values)

    def stable_memory(
        self, target_skill: str, limit: int = 6
    ) -> Sequence[Mapping[str, Any]]:
        if target_skill not in TEXT2SQL_SKILLS:
            raise ValueError("invalid target skill")
        rows = self.connection.execute(
            "SELECT memory_id,failure_kind,content,rule_json,rule_fingerprint,"
            "runtime_eligible,state_version FROM memory_items "
            "WHERE state='stable' AND runtime_eligible=1 AND target_skill=? "
            "ORDER BY reviewed_at DESC,memory_id",
            (target_skill,),
        ).fetchall()
        values = []
        seen = set()
        for row in rows:
            fingerprint = str(row["rule_fingerprint"])
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            values.append(_decode_memory_row(row))
            if len(values) >= max(1, min(int(limit), 50)):
                break
        return tuple(values)

    def list_memory(self, state: str = "") -> Sequence[Mapping[str, Any]]:
        if state and state not in {
            "candidate",
            "confirmed",
            "needs_evidence",
            "approved",
            "evaluating",
            "evaluated",
            "evaluation_failed",
            "stable",
            "rejected",
            "retired",
        }:
            raise ValueError("invalid memory state")
        sql = (
            "SELECT memory_id,target_skill,origin_split,failure_kind,content,rule_json,"
            "rule_fingerprint,source_case_ids_json,occurrence_count,evidence_json,state,"
            "created_at,reviewed_by,reviewed_at,review_note,source_task_id,source_stage,"
            "source_revision,evidence_sha256,runtime_eligible,state_version "
            "FROM memory_items"
        )
        params: tuple[Any, ...] = ()
        if state:
            sql += " WHERE state=?"
            params = (state,)
        sql += " ORDER BY created_at"
        return tuple(
            _decode_memory_row(row)
            for row in self.connection.execute(sql, params).fetchall()
        )

    def create_memory_evaluation_job(
        self,
        memory_id: str,
        requested_by: str,
        baseline_artifact: str,
        candidate_artifact: str,
        log_path: str,
        progress_total: int,
    ) -> Mapping[str, Any]:
        item = self.get_memory(memory_id)
        if not item.get("runtime_eligible"):
            raise ValueError("ExperienceMemory/v1 does not use Memory evaluation")
        if item["state"] not in {"approved", "evaluation_failed"}:
            raise ValueError("memory must be approved before evaluation")
        active = self.connection.execute(
            "SELECT job_id FROM memory_evaluation_jobs WHERE memory_id=? "
            "AND status IN ('queued','running')",
            (memory_id,),
        ).fetchone()
        if active:
            raise ValueError("memory evaluation is already running")
        job_id = "memory-eval-%s" % uuid.uuid4().hex
        timestamp = _now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO memory_evaluation_jobs(
                    job_id,memory_id,status,phase,progress_current,progress_total,
                    baseline_artifact,candidate_artifact,log_path,error,
                    requested_by,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    memory_id,
                    "queued",
                    "preparing",
                    0,
                    max(240, int(progress_total)),
                    baseline_artifact,
                    candidate_artifact,
                    log_path,
                    "",
                    requested_by.strip()[:200],
                    timestamp,
                    timestamp,
                ),
            )
            self.connection.execute(
                "UPDATE memory_items SET state='evaluating' WHERE memory_id=?",
                (memory_id,),
            )
        return self.get_memory_evaluation_job(job_id)

    def get_memory_evaluation_job(self, job_id: str) -> Mapping[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM memory_evaluation_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not row:
            raise ValueError("unknown memory evaluation job")
        return dict(row)

    def list_memory_evaluation_jobs(
        self, memory_id: str = "", limit: int = 20
    ) -> Sequence[Mapping[str, Any]]:
        sql = "SELECT * FROM memory_evaluation_jobs"
        params: list[Any] = []
        if memory_id:
            sql += " WHERE memory_id=?"
            params.append(memory_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 100)))
        return tuple(
            dict(row) for row in self.connection.execute(sql, tuple(params)).fetchall()
        )

    def update_memory_evaluation_job(
        self,
        job_id: str,
        *,
        status: str = "",
        phase: str = "",
        progress_current: Optional[int] = None,
        error: str = "",
    ) -> Mapping[str, Any]:
        item = self.get_memory_evaluation_job(job_id)
        values = {
            "status": status or item["status"],
            "phase": phase or item["phase"],
            "progress_current": (
                int(progress_current)
                if progress_current is not None
                else int(item["progress_current"])
            ),
            "error": error[:2000] if error else item["error"],
        }
        with self.connection:
            self.connection.execute(
                "UPDATE memory_evaluation_jobs SET status=?,phase=?,progress_current=?,"
                "error=?,updated_at=? WHERE job_id=?",
                (
                    values["status"],
                    values["phase"],
                    values["progress_current"],
                    values["error"],
                    _now(),
                    job_id,
                ),
            )
            if values["status"] == "failed":
                self.connection.execute(
                    "UPDATE memory_items SET state='evaluation_failed' WHERE memory_id=? "
                    "AND state='evaluating'",
                    (item["memory_id"],),
                )
        return self.get_memory_evaluation_job(job_id)

    def record_memory_evaluation(
        self,
        memory_id: str,
        job_id: str,
        dataset_manifest: Mapping[str, Any],
        baseline_artifact: Mapping[str, Any],
        candidate_artifact: Mapping[str, Any],
        dataset_review_evidence: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        item = self.get_memory(memory_id)
        if item["state"] != "evaluating":
            raise ValueError("memory is not awaiting an evaluation result")
        job = self.get_memory_evaluation_job(job_id)
        if job["memory_id"] != memory_id:
            raise ValueError("memory evaluation job does not match candidate")
        baseline_report, baseline_meta = self._unwrap_evaluation_artifact(
            baseline_artifact, dataset_manifest
        )
        candidate_report, candidate_meta = self._unwrap_evaluation_artifact(
            candidate_artifact, dataset_manifest
        )
        required_count = sum(
            int((value or {}).get("case_count") or 0)
            for value in (dataset_manifest.get("files") or {}).values()
        )
        if (
            baseline_meta.get("evaluated_case_count") != required_count
            or candidate_meta.get("evaluated_case_count") != required_count
        ):
            raise ValueError("memory promotion requires the complete 240-case dataset")
        if baseline_meta.get("model") != candidate_meta.get("model"):
            raise ValueError("baseline and candidate model configuration mismatch")
        if baseline_meta.get("runtime") != candidate_meta.get("runtime"):
            raise ValueError("baseline and candidate runtime identity mismatch")
        if baseline_meta.get("principals") != candidate_meta.get("principals"):
            raise ValueError("baseline and candidate principal set mismatch")
        if baseline_meta.get("memory_candidate_id") or baseline_meta.get(
            "experience_candidate_id"
        ):
            raise ValueError("baseline evaluation must not contain a release candidate")
        if candidate_meta.get("experience_candidate_id"):
            raise ValueError("Memory evaluation cannot contain a Q-SQL candidate")
        if candidate_meta.get("memory_candidate_id") != memory_id:
            raise ValueError("candidate evaluation does not identify this memory")
        baseline_pins = dict(baseline_report.get("version_pins") or {})
        candidate_pins = dict(candidate_report.get("version_pins") or {})
        for name in (
            "database_snapshot_id",
            "wiki_index_version",
            "vanna_index_version",
            "policy_version",
        ):
            if baseline_pins.get(name) != candidate_pins.get(name):
                raise ValueError(
                    "%s changed between Memory baseline and candidate" % name
                )
        policy_version = str(baseline_pins.get("policy_version") or "")
        if not policy_version or policy_version != self.active_policy_version:
            raise ValueError(
                "active Policy changed during Memory evaluation; replay required"
            )
        expected_policy_memory_ids = self.policy_source_memory_ids(policy_version)
        if tuple(baseline_meta["runtime"]["policy_source_memory_ids"]) != (
            expected_policy_memory_ids
        ):
            raise ValueError("Memory evaluation runtime Policy-Memory lineage mismatch")
        if baseline_pins.get("memory_snapshot_id") != self.memory_snapshot_id:
            raise ValueError("stable Memory changed during evaluation; replay required")
        expected_snapshot = self.memory_snapshot_id_for(memory_id)
        if (
            candidate_report.get("version_pins", {}).get("memory_snapshot_id")
            != expected_snapshot
        ):
            raise ValueError("candidate evaluation memory snapshot mismatch")
        decision = evaluate_memory_promotion_gate(
            dataset_manifest,
            baseline_report,
            candidate_report,
            dataset_review_evidence,
        )
        run_id = "memory-run-%s" % uuid.uuid4().hex
        next_state = (
            "evaluated" if decision["eligible_for_activation"] else "evaluation_failed"
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO memory_evaluation_runs(
                    run_id,job_id,memory_id,dataset_id,dataset_sha256,
                    baseline_aggregate_json,candidate_aggregate_json,
                    decision_json,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    job_id,
                    memory_id,
                    str(dataset_manifest.get("dataset_id") or ""),
                    str(dataset_manifest.get("dataset_sha256") or ""),
                    _canonical(self._aggregates_only(baseline_report, baseline_meta)),
                    _canonical(self._aggregates_only(candidate_report, candidate_meta)),
                    _canonical(decision),
                    _now(),
                ),
            )
            self.connection.execute(
                "UPDATE memory_items SET state=? WHERE memory_id=?",
                (next_state, memory_id),
            )
            self.connection.execute(
                "UPDATE memory_evaluation_jobs SET status=?,phase='complete',"
                "progress_current=progress_total,error=?,updated_at=? WHERE job_id=?",
                (
                    "passed" if decision["eligible_for_activation"] else "failed",
                    "" if decision["eligible_for_activation"] else ", ".join(decision["reasons"]),
                    _now(),
                    job_id,
                ),
            )
        return {
            "run_id": run_id,
            "memory_id": memory_id,
            "memory_state": next_state,
            **decision,
        }

    def activate_memory(
        self,
        memory_id: str,
        actor: str,
        reason: str,
        human_approved: bool,
        current_version_pins: Optional[Mapping[str, str]] = None,
        current_evaluation_identity: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        if not human_approved:
            raise ValueError("explicit human approval is required")
        if not actor.strip() or not reason.strip():
            raise ValueError("actor and activation reason are required")
        item = self.get_memory(memory_id)
        if not item.get("runtime_eligible"):
            raise ValueError("ExperienceMemory/v1 cannot be activated as runtime Memory")
        if item["state"] != "evaluated":
            raise ValueError("only a memory that passed 240-case evaluation can be activated")
        run = self.connection.execute(
            "SELECT baseline_aggregate_json,candidate_aggregate_json,decision_json "
            "FROM memory_evaluation_runs WHERE memory_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (memory_id,),
        ).fetchone()
        if not run or not json.loads(run["decision_json"]).get(
            "eligible_for_activation"
        ):
            raise ValueError("memory lacks a passing evaluation")
        if current_version_pins is None or current_evaluation_identity is None:
            raise ValueError("current release identity is required for Memory activation")
        baseline_aggregate = json.loads(run["baseline_aggregate_json"])
        candidate_aggregate = json.loads(run["candidate_aggregate_json"])
        baseline_pins = dict(baseline_aggregate.get("version_pins") or {})
        candidate_pins = dict(candidate_aggregate.get("version_pins") or {})
        if dict(current_version_pins) != baseline_pins:
            raise ValueError("release inputs changed after Memory evaluation; replay required")
        expected_candidate_pins = {
            **baseline_pins,
            "memory_snapshot_id": self.memory_snapshot_id_for(memory_id),
        }
        if candidate_pins != expected_candidate_pins:
            raise ValueError("Memory candidate snapshot changed; replay required")
        current_identity = validate_evaluation_identity(
            current_evaluation_identity
        )
        if current_identity != validate_evaluation_identity(
            baseline_aggregate.get("evaluation_identity") or {}
        ) or current_identity != validate_evaluation_identity(
            candidate_aggregate.get("evaluation_identity") or {}
        ):
            raise ValueError("release identity changed after Memory evaluation; replay required")
        with self.connection:
            self.connection.execute(
                "UPDATE memory_items SET state='stable' WHERE memory_id=?",
                (memory_id,),
            )
            self.connection.execute(
                "INSERT INTO memory_activation_audit VALUES (?,?,?,?,?,?)",
                (
                    "memory-activation-%s" % uuid.uuid4().hex,
                    memory_id,
                    "activate",
                    actor.strip()[:200],
                    reason.strip()[:2000],
                    _now(),
                ),
            )
        return self.get_memory(memory_id)

    def rollback_memory(
        self, memory_id: str, actor: str, reason: str
    ) -> Mapping[str, Any]:
        if not actor.strip() or not reason.strip():
            raise ValueError("actor and rollback reason are required")
        item = self.get_memory(memory_id)
        if item["state"] != "stable":
            raise ValueError("only stable memory can be rolled back")
        with self.connection:
            self.connection.execute(
                "UPDATE memory_items SET state='retired' WHERE memory_id=?",
                (memory_id,),
            )
            self.connection.execute(
                "INSERT INTO memory_activation_audit VALUES (?,?,?,?,?,?)",
                (
                    "memory-activation-%s" % uuid.uuid4().hex,
                    memory_id,
                    "rollback",
                    actor.strip()[:200],
                    reason.strip()[:2000],
                    _now(),
                ),
            )
        return self.get_memory(memory_id)
