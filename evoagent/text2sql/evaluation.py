"""Independent Text2SQL execution-accuracy evaluation and failure attribution."""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal, InvalidOperation
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import sqlglot
from sqlglot import exp

from .sql_safety import ReadOnlySQLiteExecutor, validate_sql


FAILURE_KINDS = frozenset(
    {
        "NO_SQL",
        "PARSE_ERROR",
        "UNSAFE_SQL",
        "UNKNOWN_TABLE",
        "UNKNOWN_COLUMN",
        "SCHEMA_LINK_MISMATCH",
        "FILTER_MISMATCH",
        "AGGREGATION_MISMATCH",
        "JOIN_OR_GRAIN_MISMATCH",
        "EXECUTION_ERROR",
        "TIMEOUT",
        "UNEXPECTED_EMPTY",
        "RESULT_MISMATCH",
        "USER_CORRECTION",
        "FRAMEWORK_ERROR",
    }
)


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    question: str
    gold_sql: str
    gold_result_fingerprint: str
    gold_row_count: int
    gold_column_count: int
    sql_skeleton: str
    category: str
    difficulty: str
    database_snapshot_id: str
    split: str
    ordered: bool
    required_tables: Sequence[str]
    required_columns: Sequence[str]
    required_relationships: Sequence[str]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvaluationCase":
        return cls(
            case_id=str(value["case_id"]),
            question=str(value["question"]),
            gold_sql=str(value["gold_sql"]),
            gold_result_fingerprint=str(value["gold_result_fingerprint"]),
            gold_row_count=int(value["gold_row_count"]),
            gold_column_count=int(value["gold_column_count"]),
            sql_skeleton=str(value["sql_skeleton"]),
            category=str(value["category"]),
            difficulty=str(value["difficulty"]),
            database_snapshot_id=str(value["database_snapshot_id"]),
            split=str(value["split"]),
            ordered=bool(value["ordered"]),
            required_tables=tuple(value.get("required_tables") or ()),
            required_columns=tuple(value.get("required_columns") or ()),
            required_relationships=tuple(value.get("required_relationships") or ()),
        )

    def as_dict(self) -> Mapping[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DatasetBundle:
    dataset_id: str
    database_snapshot_id: str
    dataset_sha256: str
    cases: Sequence[EvaluationCase]
    split_counts: Mapping[str, int]
    review_evidence: Mapping[str, Any]


def _canonical_value(value: Any) -> Any:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bytes):
        return {
            "type": "bytes",
            "sha256": hashlib.sha256(value).hexdigest(),
            "length": len(value),
        }
    if isinstance(value, float):
        if math.isnan(value):
            return {"type": "float", "value": "nan"}
        if math.isinf(value):
            return {"type": "float", "value": "inf" if value > 0 else "-inf"}
        return {"type": "number", "value": round(value, 6)}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"type": "number", "value": value}
    return {"type": type(value).__name__, "value": value}


def canonical_result(
    columns: Sequence[str], rows: Sequence[Sequence[Any]], ordered: bool
) -> Mapping[str, Any]:
    normalized_rows = [
        [_canonical_value(value) for value in row]
        for row in rows
    ]
    if not ordered:
        normalized_rows.sort(
            key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True)
        )
    return {
        "column_count": len(columns),
        "rows": normalized_rows,
    }


def result_fingerprint(
    columns: Sequence[str], rows: Sequence[Sequence[Any]], ordered: bool
) -> str:
    rendered = json.dumps(
        canonical_result(columns, rows, ordered),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def load_dataset(
    root: Path,
    splits: Optional[Iterable[str]] = None,
    review_signing_key: Optional[bytes] = None,
) -> DatasetBundle:
    root = root.resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    selected = set(splits or manifest["files"])
    unknown = selected.difference(manifest["files"])
    if unknown:
        raise ValueError("unknown dataset split(s): %s" % ", ".join(sorted(unknown)))
    all_cases: list[EvaluationCase] = []
    file_hashes = {}
    seen: set[str] = set()
    all_case_values: list[Mapping[str, Any]] = []
    actual_split_counts: dict[str, int] = {}
    for split, item in sorted(manifest["files"].items()):
        path = root / item["path"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item["sha256"]:
            raise ValueError("dataset split hash mismatch: %s" % split)
        file_hashes[split] = digest
        actual_split_counts[split] = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            case_value = json.loads(line)
            case = EvaluationCase.from_dict(case_value)
            if case.case_id in seen:
                raise ValueError("duplicate evaluation case_id: %s" % case.case_id)
            if case.split != split:
                raise ValueError("case split does not match containing file")
            if case.database_snapshot_id != manifest["database_snapshot_id"]:
                raise ValueError("case database snapshot mismatch")
            seen.add(case.case_id)
            actual_split_counts[split] += 1
            all_case_values.append(case.as_dict())
            if split in selected:
                all_cases.append(case)
        if actual_split_counts[split] != int(item.get("case_count") or 0):
            raise ValueError("dataset split case count mismatch: %s" % split)
    fingerprint_payload = {
        "contract_version": manifest["contract_version"],
        "database_snapshot_id": manifest["database_snapshot_id"],
        "files": {
            split: manifest["files"][split]["sha256"]
            for split in sorted(manifest["files"])
        },
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    if fingerprint != manifest["dataset_sha256"]:
        raise ValueError("dataset manifest fingerprint mismatch")
    review_evidence: Mapping[str, Any] = {"verified": False}
    if manifest.get("release_eligible"):
        expected_total = sum(
            int(item.get("case_count") or 0) for item in manifest["files"].values()
        )
        if manifest.get("review_status") not in {"human_reviewed", "approved"} or int(
            manifest.get("human_reviewed_cases") or 0
        ) != expected_total:
            raise ValueError("dataset human review manifest evidence is incomplete")
        certificate_meta = manifest.get("review_certificate") or {}
        certificate_relative = str(certificate_meta.get("path") or "")
        if not certificate_relative:
            raise ValueError("release-eligible dataset is missing its review certificate")
        certificate_path = (root / certificate_relative).resolve()
        try:
            certificate_path.relative_to(root)
        except ValueError as exc:
            raise ValueError("dataset review certificate path escapes dataset root") from exc
        certificate_digest = hashlib.sha256(certificate_path.read_bytes()).hexdigest()
        if certificate_digest != certificate_meta.get("sha256"):
            raise ValueError("dataset review certificate file hash mismatch")
        from .dataset_review import read_review_signing_key, verify_review_certificate

        key = review_signing_key if review_signing_key is not None else read_review_signing_key()
        certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
        review_evidence = {
            **verify_review_certificate(
                certificate,
                key,
                dataset_id=str(manifest["dataset_id"]),
                dataset_sha256=fingerprint,
                database_snapshot_id=str(manifest["database_snapshot_id"]),
                cases=all_case_values,
            ),
            "certificate_sha256": certificate_digest,
        }
    return DatasetBundle(
        dataset_id=manifest["dataset_id"],
        database_snapshot_id=manifest["database_snapshot_id"],
        dataset_sha256=fingerprint,
        cases=tuple(sorted(all_cases, key=lambda item: item.case_id)),
        split_counts={split: int(item["case_count"]) for split, item in manifest["files"].items()},
        review_evidence=review_evidence,
    )


def _failure_from_gate(errors: Sequence[str]) -> str:
    if any(value == "parse_error" for value in errors):
        return "PARSE_ERROR"
    if any(value.startswith("unknown_table") for value in errors):
        return "UNKNOWN_TABLE"
    if any(value.startswith("unknown_column") for value in errors):
        return "UNKNOWN_COLUMN"
    if any(value in {"sql_tables_outside_schema_plan", "sql_columns_outside_schema_plan"} for value in errors):
        return "SCHEMA_LINK_MISMATCH"
    if errors:
        return "UNSAFE_SQL"
    return "RESULT_MISMATCH"


def _sql_features(sql: str) -> Mapping[str, Any]:
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except (sqlglot.errors.ParseError, TypeError):
        return {}
    return {
        "tables": tuple(sorted(table.name for table in tree.find_all(exp.Table))),
        "joins": len(tuple(tree.find_all(exp.Join))),
        "where": tuple(node.sql(dialect="sqlite") for node in tree.find_all(exp.Where)),
        "aggregates": tuple(
            sorted(type(node).__name__ for node in tree.walk() if isinstance(node, exp.AggFunc))
        ),
        "group": tuple(node.sql(dialect="sqlite") for node in tree.find_all(exp.Group)),
        "literals": tuple(
            sorted(_normalized_literal(node) for node in tree.find_all(exp.Literal))
        ),
    }


def _normalized_literal(node: exp.Literal) -> str:
    """Compare grounded values, not equivalent SQL quoting choices."""

    value = str(node.this)
    try:
        number = Decimal(value)
    except InvalidOperation:
        return value
    if not number.is_finite():
        return value.lower()
    normalized = format(number.normalize(), "f")
    return "0" if normalized in {"-0", "-0.0"} else normalized


def classify_result_mismatch(gold_sql: str, candidate_sql: str, actual_rows: int) -> str:
    gold = _sql_features(gold_sql)
    candidate = _sql_features(candidate_sql)
    if not candidate:
        return "PARSE_ERROR"
    if gold.get("tables") != candidate.get("tables") or gold.get("joins") != candidate.get("joins"):
        return "JOIN_OR_GRAIN_MISMATCH"
    if gold.get("aggregates") != candidate.get("aggregates") or gold.get("group") != candidate.get("group"):
        return "AGGREGATION_MISMATCH"
    if gold.get("where") != candidate.get("where"):
        return "FILTER_MISMATCH"
    if actual_rows == 0:
        return "UNEXPECTED_EMPTY"
    return "RESULT_MISMATCH"


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return round(float(ordered[index]), 3)


class Text2SQLEvaluator:
    """Run agents without ever passing Gold SQL or Gold results into their input."""

    def __init__(
        self,
        database_path: Path,
        snapshot: Mapping[str, Any],
        expected_version_pins: Mapping[str, str],
        max_rows: int = 10_000,
        timeout_ms: int = 10_000,
    ) -> None:
        self.snapshot = snapshot
        self.expected_version_pins = dict(expected_version_pins)
        self.executor = ReadOnlySQLiteExecutor(
            database_path, snapshot, max_rows=max_rows, timeout_ms=timeout_ms
        )

    def evaluate(
        self,
        cases: Sequence[EvaluationCase],
        runner: Callable[[str], Mapping[str, Any]],
        redact_holdout: bool = True,
        existing_outcomes: Sequence[Mapping[str, Any]] = (),
        progress_callback: Optional[Callable[[Mapping[str, Any]], None]] = None,
        should_continue: Optional[
            Callable[[Sequence[Mapping[str, Any]]], bool]
        ] = None,
        case_runner: Optional[
            Callable[[EvaluationCase], Mapping[str, Any]]
        ] = None,
    ) -> Mapping[str, Any]:
        outcomes = [dict(item) for item in existing_outcomes]
        case_ids = {case.case_id for case in cases}
        completed_ids = {str(item.get("case_id") or "") for item in outcomes}
        if "" in completed_ids or len(completed_ids) != len(outcomes):
            raise ValueError("existing evaluation outcomes contain invalid or duplicate case_id")
        if not completed_ids.issubset(case_ids):
            raise ValueError("existing evaluation outcomes do not belong to this case set")
        for case in cases:
            if case.case_id in completed_ids:
                continue
            if should_continue is not None and not should_continue(tuple(outcomes)):
                break
            if case.database_snapshot_id != self.snapshot["snapshot_id"]:
                raise ValueError("evaluation case snapshot mismatch")
            gold = self.executor.execute(case.gold_sql)
            if gold.truncated:
                raise ValueError("Gold result exceeded evaluation row budget: %s" % case.case_id)
            gold_fingerprint = result_fingerprint(gold.columns, gold.rows, case.ordered)
            if gold_fingerprint != case.gold_result_fingerprint:
                raise ValueError("Gold result fingerprint drift: %s" % case.case_id)

            started = time.monotonic()
            framework_error = ""
            try:
                result = dict(
                    case_runner(case) if case_runner is not None else runner(case.question)
                )
            except TimeoutError as exc:
                result = {}
                framework_error = "TIMEOUT:%s" % exc
            except Exception as exc:
                result = {}
                framework_error = "FRAMEWORK_ERROR:%s" % exc
            duration_ms = (time.monotonic() - started) * 1000

            candidate_sql = str(result.get("final_sql") or "")
            gate_errors = list((result.get("gates") or {}).get("errors") or ())
            failure = ""
            exact = False
            executable = result.get("status") == "success"
            ast_valid = bool(candidate_sql and validate_sql(candidate_sql, self.snapshot).accepted)
            actual = result.get("answer") or {}
            actual_rows = list(actual.get("rows") or ())
            schema_worker = next(
                (
                    item
                    for item in (result.get("collaboration") or {}).get("worker_results") or ()
                    if isinstance(item, Mapping) and item.get("worker") == "schema-grounding"
                ),
                {},
            )
            schema_plan = (schema_worker.get("output") or {}).get("schema_plan") or {}
            planned_tables = set(schema_plan.get("tables") or ())
            planned_columns = set(schema_plan.get("columns") or ())
            planned_relationships = {
                str(item.get("evidence_id"))
                for item in schema_plan.get("joins") or ()
                if isinstance(item, Mapping) and item.get("evidence_id")
            }
            if framework_error.startswith("TIMEOUT"):
                failure = "TIMEOUT"
            elif framework_error:
                failure = "FRAMEWORK_ERROR"
            elif dict(result.get("version_pins") or {}) != self.expected_version_pins:
                failure = "FRAMEWORK_ERROR"
                framework_error = "version_pin_mismatch"
            elif not candidate_sql:
                failure = _failure_from_gate(gate_errors) if gate_errors else "NO_SQL"
            elif not executable:
                failure = _failure_from_gate(gate_errors)
            elif actual.get("truncated"):
                failure = "RESULT_MISMATCH"
            else:
                candidate_fingerprint = result_fingerprint(
                    actual.get("columns") or (), actual_rows, case.ordered
                )
                exact = candidate_fingerprint == gold_fingerprint
                if not exact:
                    failure = classify_result_mismatch(
                        case.gold_sql, candidate_sql, len(actual_rows)
                    )
            if failure and failure not in FAILURE_KINDS:
                failure = "FRAMEWORK_ERROR"
            safe = not any(
                error in {
                    "read_only_query_required",
                    "write_or_control_statement_forbidden",
                    "comments_not_allowed",
                    "exactly_one_statement_required",
                }
                or str(error).startswith("blocked_function")
                for error in gate_errors
            )
            required_tables = set(case.required_tables)
            required_columns = set(case.required_columns)
            required_relationships = set(case.required_relationships)
            candidate_features = _sql_features(candidate_sql) if candidate_sql else {}
            gold_features = _sql_features(case.gold_sql)
            gold_literals = set(gold_features.get("literals") or ())
            candidate_literals = set(candidate_features.get("literals") or ())
            execution = result.get("execution") or {}
            outcome = {
                "case_id": case.case_id,
                "split": case.split,
                "category": case.category,
                "difficulty": case.difficulty,
                "sql_skeleton": case.sql_skeleton,
                "execution_accuracy": exact,
                "executable": executable,
                "ast_valid": ast_valid,
                "safe": safe,
                "table_recall": round(
                    len(required_tables.intersection(planned_tables)) / max(1, len(required_tables)),
                    6,
                ),
                "column_recall": round(
                    len(required_columns.intersection(planned_columns)) / max(1, len(required_columns)),
                    6,
                ),
                "join_edge_recall": (
                    round(
                        len(required_relationships.intersection(planned_relationships))
                        / len(required_relationships),
                        6,
                    )
                    if required_relationships
                    else None
                ),
                "value_grounding_accuracy": (
                    round(
                        len(gold_literals.intersection(candidate_literals)) / len(gold_literals),
                        6,
                    )
                    if gold_literals
                    else None
                ),
                "failure_kind": failure,
                "duration_ms": round(duration_ms, 3),
                "candidate_sql_fingerprint": hashlib.sha256(
                    candidate_sql.encode("utf-8")
                ).hexdigest() if candidate_sql else "",
                "framework_error": framework_error[:500],
                "llm_calls": int(execution.get("llm_calls") or 0),
                "input_tokens": int(execution.get("input_tokens") or 0),
                "output_tokens": int(execution.get("output_tokens") or 0),
                "total_tokens": int(execution.get("total_tokens") or 0),
                "reported_cost_usd": round(float(execution.get("cost_usd") or 0.0), 8),
            }
            if not (redact_holdout and case.split == "sealed_holdout"):
                outcome.update(
                    {
                        "question": case.question,
                        "candidate_sql": candidate_sql,
                        "gate_errors": gate_errors,
                        "schema_worker_status": str(schema_worker.get("status") or "missing"),
                        "schema_worker_error": str(schema_worker.get("error") or "")[:500],
                        "planned_relationships": sorted(planned_relationships),
                    }
                )
            outcomes.append(outcome)
            completed_ids.add(case.case_id)
            if progress_callback is not None:
                progress_callback(dict(outcome))

        outcomes.sort(key=lambda item: str(item["case_id"]))

        def metrics_for(values: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
            total = len(values)
            failures = Counter(item["failure_kind"] for item in values if item["failure_kind"])
            buckets = defaultdict(list)
            for item in values:
                buckets[item["sql_skeleton"]].append(item)
            join_values = [item["join_edge_recall"] for item in values if item["join_edge_recall"] is not None]
            value_values = [
                item["value_grounding_accuracy"]
                for item in values
                if item["value_grounding_accuracy"] is not None
            ]
            return {
                "cases": total,
                "execution_accuracy": round(sum(item["execution_accuracy"] for item in values) / max(1, total), 6),
                "executable_rate": round(sum(item["executable"] for item in values) / max(1, total), 6),
                "ast_parse_rate": round(sum(item["ast_valid"] for item in values) / max(1, total), 6),
                "readonly_safety_rate": round(sum(item["safe"] for item in values) / max(1, total), 6),
                "table_recall": round(sum(item["table_recall"] for item in values) / max(1, total), 6),
                "column_recall": round(sum(item["column_recall"] for item in values) / max(1, total), 6),
                "join_edge_recall": round(sum(join_values) / len(join_values), 6) if join_values else None,
                "value_grounding_accuracy": round(sum(value_values) / len(value_values), 6) if value_values else None,
                "framework_errors": int(failures.get("FRAMEWORK_ERROR", 0)),
                "llm_calls": sum(int(item.get("llm_calls") or 0) for item in values),
                "input_tokens": sum(int(item.get("input_tokens") or 0) for item in values),
                "output_tokens": sum(int(item.get("output_tokens") or 0) for item in values),
                "total_tokens": sum(int(item.get("total_tokens") or 0) for item in values),
                "reported_cost_usd": round(
                    sum(float(item.get("reported_cost_usd") or 0.0) for item in values), 8
                ),
                "failure_counts": dict(sorted(failures.items())),
                "p50_latency_ms": round(statistics.median([item["duration_ms"] for item in values]), 3) if values else 0.0,
                "p95_latency_ms": _percentile([item["duration_ms"] for item in values], 0.95),
                "skeleton_buckets": {
                    name: {
                        "cases": len(items),
                        "execution_accuracy": round(
                            sum(item["execution_accuracy"] for item in items) / len(items), 6
                        ),
                    }
                    for name, items in sorted(buckets.items())
                },
            }

        split_metrics = {
            split: metrics_for([item for item in outcomes if item["split"] == split])
            for split in sorted({item["split"] for item in outcomes})
        }
        return {
            "version_pins": dict(self.expected_version_pins),
            "overall": metrics_for(outcomes),
            "splits": split_metrics,
            "outcomes": outcomes,
        }
