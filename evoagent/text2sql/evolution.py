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

from .evaluation import FAILURE_KINDS, _percentile as _evaluation_percentile
from .policy import PolicyArtifact, TEXT2SQL_SKILLS, require_single_skill_change


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_RATE_METRICS = {
    "execution_accuracy",
    "executable_rate",
    "ast_parse_rate",
    "readonly_safety_rate",
}
_SPLIT_METRICS = (*sorted(_RATE_METRICS), "p95_latency_ms")
_EXECUTION_ACCURACY_TOLERANCE = 1e-6
_MIN_CANDIDATE_OPERATIONAL_RATE = 1e-6
_NON_EXECUTABLE_NON_AST_FAILURE_KINDS = frozenset(
    {"NO_SQL", "PARSE_ERROR", "UNKNOWN_TABLE", "UNKNOWN_COLUMN", "TIMEOUT"}
)
_NON_EXECUTABLE_FAILURE_KINDS = frozenset(
    {"SCHEMA_LINK_MISMATCH", "FRAMEWORK_ERROR", "EXECUTION_ERROR"}
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
        # The evaluator uses UNSAFE_SQL as the fallback for any unclassified
        # deterministic gate error.  Some of those errors (for example a
        # forbidden projection shape) are read-only and can leave ``safe`` or
        # ``ast_valid`` true, but a gated candidate can never be executable.
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


def evaluate_knowledge_promotion_gate(
    dataset_manifest: Mapping[str, Any],
    baseline_report: Mapping[str, Any],
    candidate_report: Mapping[str, Any],
    dataset_review_evidence: Optional[Mapping[str, Any]] = None,
) -> Mapping[str, Any]:
    """Validate a candidate Question-SQL retrieval snapshot on all 240 cases."""

    reasons: list[str] = []
    splits = ("train", "validation", "sealed_holdout")
    expected_counts = _expected_case_counts(
        dataset_manifest, splits, reasons, required=True
    )
    expected_total = sum(value or 0 for value in expected_counts.values())
    evidence = _mapping(
        dataset_review_evidence or {}, reasons, "dataset_review_evidence"
    )
    if expected_total != 240:
        reasons.append("trusted_dataset_must_contain_240_cases")
    release_eligible = _strict_bool(
        dataset_manifest.get("release_eligible"),
        reasons,
        "manifest.release_eligible",
    )
    verified = _strict_bool(evidence.get("verified"), reasons, "review.verified")
    reviewed_case_count = _strict_count(
        evidence.get("reviewed_case_count"),
        reasons,
        "review.reviewed_case_count",
    )
    if (
        not release_eligible
        or not verified
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
    for name in ("database_snapshot_id", "memory_snapshot_id", "policy_version"):
        if baseline_pins.get(name) != candidate_pins.get(name):
            reasons.append("%s_mismatch" % name)
    if baseline_pins.get("wiki_index_version") == candidate_pins.get(
        "wiki_index_version"
    ):
        reasons.append("candidate_knowledge_snapshot_not_changed")
    if candidate_pins.get("vanna_index_version") != candidate_pins.get(
        "wiki_index_version"
    ):
        reasons.append("candidate_vanna_index_not_aligned")

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
        reasons.append("knowledge_candidate_regressed_cases")
    return {
        "eligible_for_activation": not reasons,
        "reasons": sorted(set(reasons)),
        "dataset_case_count": expected_total,
        "observed": {"fixed": total_fixed, "regressed": total_regressed},
        "thresholds": {
            "required_case_count": 240,
            "minimum_safety_rate": 1.0,
            "minimum_candidate_executable_rate": _MIN_CANDIDATE_OPERATIONAL_RATE,
            "minimum_candidate_ast_parse_rate": _MIN_CANDIDATE_OPERATIONAL_RATE,
            "maximum_execution_regression": 0.0,
            "maximum_p95_latency_multiplier": 1.2,
            "maximum_regressed_cases": 0,
        },
    }


class Text2SQLEvolutionStore:
    """SQLite control plane; model output is never allowed to approve itself."""

    def __init__(self, path: Path, snapshot: Mapping[str, Any]) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot = snapshot
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._initialize()
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
            CREATE TABLE IF NOT EXISTS memory_items (
                memory_id TEXT PRIMARY KEY,
                target_skill TEXT NOT NULL,
                origin_split TEXT NOT NULL,
                failure_kind TEXT NOT NULL,
                content TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reviewed_by TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT NOT NULL DEFAULT '',
                review_note TEXT NOT NULL DEFAULT ''
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
                recorded_at TEXT NOT NULL
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
            CREATE TABLE IF NOT EXISTS experience_evaluation_jobs (
                job_id TEXT PRIMARY KEY,
                experience_id TEXT NOT NULL,
                status TEXT NOT NULL,
                phase TEXT NOT NULL,
                progress_current INTEGER NOT NULL DEFAULT 0,
                progress_total INTEGER NOT NULL DEFAULT 240,
                baseline_artifact TEXT NOT NULL DEFAULT '',
                candidate_artifact TEXT NOT NULL DEFAULT '',
                candidate_knowledge_store TEXT NOT NULL DEFAULT '',
                candidate_vanna_version TEXT NOT NULL DEFAULT '',
                log_path TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                requested_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS experience_evaluation_runs (
                run_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                experience_id TEXT NOT NULL,
                dataset_id TEXT NOT NULL,
                dataset_sha256 TEXT NOT NULL,
                baseline_aggregate_json TEXT NOT NULL,
                candidate_aggregate_json TEXT NOT NULL,
                decision_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memory_state_skill
                ON memory_items(state, target_skill);
            CREATE INDEX IF NOT EXISTS idx_query_traces_recorded_at
                ON query_traces(recorded_at DESC);
            CREATE INDEX IF NOT EXISTS idx_query_decisions_task_source_time
                ON query_decisions(task_id,decision_source,created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_memory_evaluation_memory_time
                ON memory_evaluation_jobs(memory_id,created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_experience_evaluation_item_time
                ON experience_evaluation_jobs(experience_id,created_at DESC);
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
        }
        for column, definition in trace_migrations.items():
            if column not in trace_columns:
                self.connection.execute(
                    "ALTER TABLE query_traces ADD COLUMN %s %s" % (column, definition)
                )
        memory_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(memory_items)").fetchall()
        }
        if "review_note" not in memory_columns:
            self.connection.execute(
                "ALTER TABLE memory_items ADD COLUMN review_note TEXT NOT NULL DEFAULT ''"
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

    def list_policies(self) -> Sequence[Mapping[str, Any]]:
        rows = self.connection.execute(
            "SELECT policy_version,parent_version,target_skill,status,change_reason,created_by,"
            "created_at,reviewed_by,reviewed_at FROM policy_versions ORDER BY created_at"
        ).fetchall()
        return tuple(dict(row) for row in rows)

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
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO query_traces(
                    task_id,status,question,final_sql,gates_json,agents_json,
                    execution_json,version_pins_json,answer_json,recorded_at,
                    user_id,session_id,original_question,standalone_question,
                    query_type,parent_task_id,schema_plan_json,query_spec_json,
                    collaboration_json,retrieval_json,result_rows_json,feedback_status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task_id[:200],
                    str(trace.get("status") or "unknown")[:50],
                    str(trace.get("question") or "")[:2000],
                    str(trace.get("final_sql") or "")[:20000],
                    _canonical(dict(trace.get("gates") or {})),
                    _canonical(list(trace.get("agents") or ())),
                    _canonical(dict(trace.get("execution") or {})),
                    _canonical(dict(trace.get("version_pins") or {})),
                    _canonical(dict(trace.get("answer") or {})),
                    str(trace.get("recorded_at") or _now()),
                    str(trace.get("user_id") or "local-user")[:200],
                    str(trace.get("session_id") or "default")[:200],
                    str(trace.get("original_question") or trace.get("question") or "")[:2000],
                    str(trace.get("standalone_question") or trace.get("question") or "")[:2000],
                    str(trace.get("query_type") or "DATA_QUERY")[:50],
                    str(trace.get("parent_task_id") or "")[:200],
                    _canonical(dict(trace.get("schema_plan") or {})),
                    _canonical(dict(trace.get("query_spec") or {})),
                    _canonical(dict(trace.get("collaboration") or {})),
                    _canonical(list(trace.get("retrieval") or ())),
                    _canonical(list(trace.get("result_rows") or ())[:50]),
                    str(trace.get("feedback_status") or "")[:50],
                ),
            )
            self.connection.execute(
                "DELETE FROM query_traces WHERE task_id NOT IN "
                "(SELECT task_id FROM query_traces ORDER BY recorded_at DESC LIMIT 50)"
            )
            self._store_harness_decision(
                task_id,
                str(trace.get("status") or "unknown"),
                str(trace.get("final_sql") or ""),
                dict(trace.get("gates") or {}),
                str(trace.get("recorded_at") or _now()),
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
            for column in (
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
            ):
                item[column.removesuffix("_json")] = json.loads(item.pop(column))
            values.append(item)
        return tuple(values)

    def get_query_trace(self, task_id: str) -> Mapping[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM query_traces WHERE task_id=?", (task_id[:200],)
        ).fetchone()
        if not row:
            raise ValueError("unknown query task")
        item = dict(row)
        for column in (
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
        ):
            item[column.removesuffix("_json")] = json.loads(item.pop(column))
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
            "final_sql,feedback_status,recorded_at FROM query_traces "
            "WHERE user_id=? AND session_id=? ORDER BY recorded_at DESC LIMIT ?",
            (user_key, session_key, bounded),
        ).fetchall()
        semantic_rows = self.connection.execute(
            "SELECT memory_id,target_skill,origin_split,failure_kind,content,state,"
            "created_at,reviewed_by,reviewed_at,review_note FROM memory_items "
            "ORDER BY CASE state WHEN 'candidate' THEN 0 WHEN 'approved' THEN 1 "
            "WHEN 'evaluating' THEN 2 WHEN 'evaluated' THEN 3 "
            "WHEN 'evaluation_failed' THEN 4 WHEN 'stable' THEN 5 ELSE 6 END,"
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
        episodic_items = [dict(row) for row in episodic_rows]
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
                "retention_limit_per_session": 100,
            },
            "episodic": {
                "items": episodic_items,
                "count": episodic_count,
                "retention_limit": 50,
            },
            "semantic": {
                "items": [dict(row) for row in semantic_rows],
                "counts": semantic_counts,
                "stable_only_injected": True,
            },
            "memory_evaluations": {
                "items": self.list_memory_evaluation_jobs(limit=bounded),
            },
            "question_sql": {
                "items": [dict(row) for row in question_sql_rows],
                "counts": experience_counts,
                "stable_only_injected": True,
            },
        }

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
                if not row["request_sha256"] or row["request_sha256"] != request_sha256:
                    raise ValueError(
                        "query task_id was reused with a different user, session, "
                        "question, principal, or runtime identity"
                    )
                frozen_json = str(row["context_json"] or "{}")
                if hashlib.sha256(frozen_json.encode("utf-8")).hexdigest() != str(
                    row["context_sha256"] or ""
                ):
                    raise ValueError("query attempt context integrity check failed")
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
                "ORDER BY message_id DESC LIMIT 100) AND user_id=? AND session_id=?",
                (user_id[:200], session_id[:200], user_id[:200], session_id[:200]),
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

    def review_experience(
        self,
        experience_id: str,
        decision: str,
        actor: str,
        knowledge_evidence_id: str = "",
        review_note: str = "",
    ) -> Mapping[str, Any]:
        if decision not in {"approve", "reject"} or not actor.strip():
            raise ValueError("decision and actor are required")
        review_note = review_note.strip()
        if decision == "reject" and not review_note:
            raise ValueError("rejection reason is required")
        item = self.get_experience(experience_id)
        if item["state"] not in {"candidate", "approved", "promoted"}:
            raise ValueError("experience is not awaiting review")
        if decision == "approve" and not item["eligible"]:
            raise ValueError("ineligible experience cannot be promoted")
        if item["state"] in {"approved", "promoted"}:
            if decision == "approve":
                return item
            raise ValueError("approved experience requires a revoke workflow")
        with self.connection:
            self.connection.execute(
                "UPDATE experience_reviews SET state=?,reviewed_by=?,reviewed_at=?,"
                "knowledge_evidence_id=?,review_note=? WHERE experience_id=?",
                (
                    "approved" if decision == "approve" else "rejected",
                    actor.strip()[:200],
                    _now(),
                    knowledge_evidence_id[:200],
                    review_note[:2000],
                    experience_id,
                ),
            )
        return self.get_experience(experience_id)

    def create_experience_evaluation_job(
        self,
        experience_id: str,
        requested_by: str,
        baseline_artifact: str,
        candidate_artifact: str,
        candidate_knowledge_store: str,
        log_path: str,
        progress_total: int,
    ) -> Mapping[str, Any]:
        item = self.get_experience(experience_id)
        if item["state"] not in {"approved", "evaluated", "evaluation_failed"}:
            raise ValueError("experience must be approved before evaluation")
        active = self.connection.execute(
            "SELECT job_id FROM experience_evaluation_jobs WHERE experience_id=? "
            "AND status IN ('queued','running')",
            (experience_id,),
        ).fetchone()
        if active:
            raise ValueError("experience evaluation is already running")
        job_id = "experience-eval-%s" % uuid.uuid4().hex
        timestamp = _now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO experience_evaluation_jobs(
                    job_id,experience_id,status,phase,progress_current,progress_total,
                    baseline_artifact,candidate_artifact,candidate_knowledge_store,
                    candidate_vanna_version,log_path,error,requested_by,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    experience_id,
                    "queued",
                    "preparing",
                    0,
                    max(240, int(progress_total)),
                    baseline_artifact,
                    candidate_artifact,
                    candidate_knowledge_store,
                    "",
                    log_path,
                    "",
                    requested_by.strip()[:200],
                    timestamp,
                    timestamp,
                ),
            )
            self.connection.execute(
                "UPDATE experience_reviews SET state='evaluating' WHERE experience_id=?",
                (experience_id,),
            )
        return self.get_experience_evaluation_job(job_id)

    def get_experience_evaluation_job(self, job_id: str) -> Mapping[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM experience_evaluation_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not row:
            raise ValueError("unknown experience evaluation job")
        return dict(row)

    def list_experience_evaluation_jobs(
        self, experience_id: str = "", limit: int = 30
    ) -> Sequence[Mapping[str, Any]]:
        sql = "SELECT * FROM experience_evaluation_jobs"
        params: list[Any] = []
        if experience_id:
            sql += " WHERE experience_id=?"
            params.append(experience_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 100)))
        return tuple(
            dict(row) for row in self.connection.execute(sql, tuple(params)).fetchall()
        )

    def update_experience_evaluation_job(
        self,
        job_id: str,
        *,
        status: str = "",
        phase: str = "",
        progress_current: Optional[int] = None,
        candidate_vanna_version: str = "",
        error: str = "",
    ) -> Mapping[str, Any]:
        item = self.get_experience_evaluation_job(job_id)
        with self.connection:
            self.connection.execute(
                "UPDATE experience_evaluation_jobs SET status=?,phase=?,progress_current=?,"
                "candidate_vanna_version=?,error=?,updated_at=? WHERE job_id=?",
                (
                    status or item["status"],
                    phase or item["phase"],
                    int(progress_current)
                    if progress_current is not None
                    else int(item["progress_current"]),
                    candidate_vanna_version or item["candidate_vanna_version"],
                    error[:2000] if error else item["error"],
                    _now(),
                    job_id,
                ),
            )
            if status == "failed":
                self.connection.execute(
                    "UPDATE experience_reviews SET state='evaluation_failed' "
                    "WHERE experience_id=? AND state='evaluating'",
                    (item["experience_id"],),
                )
        return self.get_experience_evaluation_job(job_id)

    def record_experience_evaluation(
        self,
        experience_id: str,
        job_id: str,
        dataset_manifest: Mapping[str, Any],
        baseline_artifact: Mapping[str, Any],
        candidate_artifact: Mapping[str, Any],
        dataset_review_evidence: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        item = self.get_experience(experience_id)
        if item["state"] != "evaluating":
            raise ValueError("experience is not awaiting an evaluation result")
        job = self.get_experience_evaluation_job(job_id)
        if job["experience_id"] != experience_id:
            raise ValueError("experience evaluation job does not match candidate")
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
            raise ValueError("experience promotion requires the complete 240-case dataset")
        if baseline_meta.get("model") != candidate_meta.get("model"):
            raise ValueError("baseline and candidate model configuration mismatch")
        if candidate_meta.get("experience_candidate_id") != experience_id:
            raise ValueError("candidate evaluation does not identify this experience")
        decision = evaluate_knowledge_promotion_gate(
            dataset_manifest,
            baseline_report,
            candidate_report,
            dataset_review_evidence,
        )
        run_id = "experience-run-%s" % uuid.uuid4().hex
        next_state = (
            "evaluated" if decision["eligible_for_activation"] else "evaluation_failed"
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO experience_evaluation_runs(
                    run_id,job_id,experience_id,dataset_id,dataset_sha256,
                    baseline_aggregate_json,candidate_aggregate_json,
                    decision_json,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    job_id,
                    experience_id,
                    str(dataset_manifest.get("dataset_id") or ""),
                    str(dataset_manifest.get("dataset_sha256") or ""),
                    _canonical(self._aggregates_only(baseline_report)),
                    _canonical(self._aggregates_only(candidate_report)),
                    _canonical(decision),
                    _now(),
                ),
            )
            self.connection.execute(
                "UPDATE experience_reviews SET state=? WHERE experience_id=?",
                (next_state, experience_id),
            )
            self.connection.execute(
                "UPDATE experience_evaluation_jobs SET status=?,phase='complete',"
                "progress_current=progress_total,error=?,updated_at=? WHERE job_id=?",
                (
                    "passed" if decision["eligible_for_activation"] else "blocked",
                    "" if decision["eligible_for_activation"] else ", ".join(decision["reasons"]),
                    _now(),
                    job_id,
                ),
            )
        return {
            "run_id": run_id,
            "experience_id": experience_id,
            "experience_state": next_state,
            **decision,
        }

    def activate_experience(
        self, experience_id: str, knowledge_evidence_id: str
    ) -> Mapping[str, Any]:
        item = self.get_experience(experience_id)
        if item["state"] != "evaluated":
            raise ValueError("only an experience that passed 240-case evaluation can be activated")
        with self.connection:
            self.connection.execute(
                "UPDATE experience_reviews SET state='promoted',knowledge_evidence_id=? "
                "WHERE experience_id=?",
                (knowledge_evidence_id[:200], experience_id),
            )
        return self.get_experience(experience_id)

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
                    _canonical(proposal_metadata or {}),
                ),
            )
        return candidate.version

    @staticmethod
    def _aggregates_only(report: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "version_pins": dict(report.get("version_pins") or {}),
            "overall": dict(report.get("overall") or {}),
            "splits": {
                split: dict(metrics)
                for split, metrics in (report.get("splits") or {}).items()
            },
        }

    @staticmethod
    def _unwrap_evaluation_artifact(
        value: Mapping[str, Any], dataset_manifest: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        if "report" not in value:
            return value, {}
        report = value.get("report")
        if not isinstance(report, Mapping):
            raise ValueError("evaluation artifact report must be an object")
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
        model = value.get("model")
        if model is not None and not isinstance(model, Mapping):
            raise ValueError("evaluation artifact model must be an object")
        return report, {
            "model": dict(model or {}),
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
        if bool(baseline_meta) != bool(candidate_meta):
            raise ValueError("baseline and candidate must use the same evaluation artifact contract")
        if baseline_meta and baseline_meta["model"] != candidate_meta["model"]:
            raise ValueError("baseline and candidate model configuration mismatch")
        required_count = sum(
            int(((dataset_manifest.get("files") or {}).get(split) or {}).get("case_count") or 0)
            for split in ("validation", "sealed_holdout")
        )
        if baseline_meta and (
            baseline_meta["evaluated_case_count"] != required_count
            or candidate_meta["evaluated_case_count"] != required_count
        ):
            raise ValueError("promotion evaluation did not cover the complete required splits")

        row = self.connection.execute(
            "SELECT parent_version,status FROM policy_versions WHERE policy_version=?",
            (candidate_version,),
        ).fetchone()
        if not row or row["status"] not in {"candidate", "evaluated", "shadow_ready"}:
            raise ValueError("policy is not an evaluable candidate")
        if baseline_report.get("version_pins", {}).get("policy_version") != row["parent_version"]:
            raise ValueError("baseline report policy version does not match candidate parent")
        if candidate_report.get("version_pins", {}).get("policy_version") != candidate_version:
            raise ValueError("candidate report policy version mismatch")
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
                    _canonical(self._aggregates_only(baseline_report)),
                    _canonical(self._aggregates_only(candidate_report)),
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
        self, candidate_version: str, actor: str, reason: str, human_approved: bool
    ) -> None:
        if not human_approved:
            raise ValueError("explicit human approval is required")
        if not actor.strip() or not reason.strip():
            raise ValueError("actor and approval reason are required")
        row = self.connection.execute(
            "SELECT status,parent_version FROM policy_versions WHERE policy_version=?", (candidate_version,)
        ).fetchone()
        if not row or row["status"] != "canary_passed":
            raise ValueError("only a candidate that passed shadow and canary can be activated")
        previous = self.active_policy_version
        if row["parent_version"] != previous:
            raise ValueError("candidate parent is no longer the active policy; replay is required")
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

    def add_memory_candidate(
        self,
        target_skill: str,
        failure_kind: str,
        content: str,
        evidence: Mapping[str, Any],
        origin_split: str = "train",
    ) -> str:
        if target_skill not in TEXT2SQL_SKILLS:
            raise ValueError("invalid target skill")
        if origin_split == "sealed_holdout":
            raise ValueError("sealed holdout content may not enter memory")
        if origin_split not in {"train", "production_feedback"}:
            raise ValueError("only train or production feedback may create memory")
        content = content.strip()
        if not failure_kind.strip() or not content or len(content) > 1500:
            raise ValueError("bounded failure_kind and memory content are required")
        memory_id = "memory-%s" % hashlib.sha256(
            _canonical([target_skill, failure_kind, content, evidence, origin_split]).encode("utf-8")
        ).hexdigest()[:24]
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO memory_items(
                    memory_id,target_skill,origin_split,failure_kind,content,evidence_json,state,created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    memory_id,
                    target_skill,
                    origin_split,
                    failure_kind.strip()[:100],
                    content,
                    _canonical(evidence),
                    "candidate",
                    _now(),
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
            "SELECT memory_id,target_skill,origin_split,failure_kind,content,state,"
            "created_at,reviewed_by,reviewed_at,review_note FROM memory_items WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
        if not row:
            raise ValueError("unknown memory item")
        return dict(row)

    def update_memory_candidate(
        self,
        memory_id: str,
        target_skill: str,
        failure_kind: str,
        content: str,
    ) -> Mapping[str, Any]:
        if target_skill not in TEXT2SQL_SKILLS:
            raise ValueError("invalid target skill")
        failure_kind = failure_kind.strip()
        content = content.strip()
        if not failure_kind or not content or len(failure_kind) > 100 or len(content) > 1500:
            raise ValueError("bounded failure_kind and memory content are required")
        with self.connection:
            changed = self.connection.execute(
                "UPDATE memory_items SET target_skill=?,failure_kind=?,content=? "
                "WHERE memory_id=? AND state='candidate'",
                (target_skill, failure_kind, content, memory_id),
            ).rowcount
            if not changed:
                raise ValueError("memory item is not awaiting review")
        return self.get_memory(memory_id)

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
        if decision not in {"approve", "reject"} or not actor.strip():
            raise ValueError("decision and actor are required")
        review_note = review_note.strip()
        if decision == "reject" and not review_note:
            raise ValueError("rejection reason is required")
        row = self.connection.execute(
            "SELECT state FROM memory_items WHERE memory_id=?", (memory_id,)
        ).fetchone()
        if not row or row["state"] != "candidate":
            raise ValueError("memory item is not awaiting review")
        with self.connection:
            self.connection.execute(
                "UPDATE memory_items SET state=?,reviewed_by=?,reviewed_at=?,review_note=? "
                "WHERE memory_id=?",
                (
                    "approved" if decision == "approve" else "rejected",
                    actor.strip()[:200],
                    _now(),
                    review_note[:2000],
                    memory_id,
                ),
            )
        return self.get_memory(memory_id)

    @property
    def memory_snapshot_id(self) -> str:
        return self.memory_snapshot_id_for()

    def memory_snapshot_id_for(self, candidate_memory_id: str = "") -> str:
        rows = self.connection.execute(
            "SELECT memory_id,target_skill,content FROM memory_items WHERE state='stable' ORDER BY memory_id"
        ).fetchall()
        values = [dict(row) for row in rows]
        if candidate_memory_id:
            candidate = self.connection.execute(
                "SELECT memory_id,target_skill,content,state FROM memory_items "
                "WHERE memory_id=?",
                (candidate_memory_id,),
            ).fetchone()
            if not candidate or candidate["state"] not in {
                "approved",
                "evaluating",
                "evaluated",
                "evaluation_failed",
            }:
                raise ValueError("memory candidate is not approved for evaluation")
            values.append(
                {
                    "memory_id": candidate["memory_id"],
                    "target_skill": candidate["target_skill"],
                    "content": candidate["content"],
                }
            )
            values.sort(key=lambda item: str(item["memory_id"]))
        return "memory-%s" % hashlib.sha256(
            _canonical(values).encode("utf-8")
        ).hexdigest()[:20]

    def evaluation_memory(
        self, target_skill: str, candidate_memory_id: str, limit: int = 6
    ) -> Sequence[Mapping[str, Any]]:
        values = list(self.stable_memory(target_skill, limit))
        candidate = self.get_memory(candidate_memory_id)
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
                },
                *[
                    item
                    for item in values
                    if item["memory_id"] != candidate["memory_id"]
                ],
            ][: max(1, min(int(limit), 50))]
        return tuple(values)

    def stable_memory(
        self, target_skill: str, limit: int = 6
    ) -> Sequence[Mapping[str, Any]]:
        if target_skill not in TEXT2SQL_SKILLS:
            raise ValueError("invalid target skill")
        rows = self.connection.execute(
            "SELECT memory_id,failure_kind,content FROM memory_items "
            "WHERE state='stable' AND target_skill=? ORDER BY reviewed_at DESC LIMIT ?",
            (target_skill, max(1, min(int(limit), 50))),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def list_memory(self, state: str = "") -> Sequence[Mapping[str, Any]]:
        if state and state not in {
            "candidate",
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
            "SELECT memory_id,target_skill,origin_split,failure_kind,content,state,created_at,"
            "reviewed_by,reviewed_at,review_note FROM memory_items"
        )
        params: tuple[Any, ...] = ()
        if state:
            sql += " WHERE state=?"
            params = (state,)
        sql += " ORDER BY created_at"
        return tuple(dict(row) for row in self.connection.execute(sql, params).fetchall())

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
        if candidate_meta.get("memory_candidate_id") != memory_id:
            raise ValueError("candidate evaluation does not identify this memory")
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
                    _canonical(self._aggregates_only(baseline_report)),
                    _canonical(self._aggregates_only(candidate_report)),
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
    ) -> Mapping[str, Any]:
        if not human_approved:
            raise ValueError("explicit human approval is required")
        if not actor.strip() or not reason.strip():
            raise ValueError("actor and activation reason are required")
        item = self.get_memory(memory_id)
        if item["state"] != "evaluated":
            raise ValueError("only a memory that passed 240-case evaluation can be activated")
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
