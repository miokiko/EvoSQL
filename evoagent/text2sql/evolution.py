"""Auditable Text2SQL evolution store, memory review, and promotion gates."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .policy import PolicyArtifact, TEXT2SQL_SKILLS, require_single_skill_change


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _metric(report: Mapping[str, Any], split: str, name: str) -> float:
    return float(((report.get("splits") or {}).get(split) or {}).get(name) or 0.0)


def _fixed_regressed(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any], split: str
) -> tuple[int, int]:
    base = {
        str(item["case_id"]): bool(item.get("execution_accuracy"))
        for item in baseline.get("outcomes") or ()
        if item.get("split") == split and item.get("case_id")
    }
    cand = {
        str(item["case_id"]): bool(item.get("execution_accuracy"))
        for item in candidate.get("outcomes") or ()
        if item.get("split") == split and item.get("case_id")
    }
    shared = set(base).intersection(cand)
    fixed = sum(not base[key] and cand[key] for key in shared)
    regressed = sum(base[key] and not cand[key] for key in shared)
    return fixed, regressed


def evaluate_promotion_gate(
    dataset_manifest: Mapping[str, Any],
    baseline_report: Mapping[str, Any],
    candidate_report: Mapping[str, Any],
    dataset_review_evidence: Optional[Mapping[str, Any]] = None,
) -> Mapping[str, Any]:
    """Compare two frozen runs. Holdout can veto but never justify a weak candidate."""

    reasons: list[str] = []
    if not dataset_manifest.get("release_eligible"):
        reasons.append("dataset_not_human_reviewed")
    expected_cases = {
        split: int(item.get("case_count") or 0)
        for split, item in (dataset_manifest.get("files") or {}).items()
    }
    expected_total = sum(expected_cases.values())
    if dataset_manifest.get("release_eligible"):
        if (
            dataset_manifest.get("review_status") not in {"human_reviewed", "approved"}
            or int(dataset_manifest.get("human_reviewed_cases") or 0) != expected_total
        ):
            reasons.append("dataset_review_evidence_incomplete")
        evidence = dict(dataset_review_evidence or {})
        if (
            not evidence.get("verified")
            or evidence.get("dataset_sha256") != dataset_manifest.get("dataset_sha256")
            or int(evidence.get("reviewed_case_count") or 0) != expected_total
            or not evidence.get("certificate_sha256")
        ):
            reasons.append("dataset_review_certificate_unverified")

    baseline_pins = dict(baseline_report.get("version_pins") or {})
    candidate_pins = dict(candidate_report.get("version_pins") or {})
    for name in ("database_snapshot_id", "wiki_index_version", "memory_snapshot_id"):
        if baseline_pins.get(name) != candidate_pins.get(name):
            reasons.append("%s_mismatch" % name)
    if dataset_manifest.get("database_snapshot_id") and baseline_pins.get(
        "database_snapshot_id"
    ) != dataset_manifest.get("database_snapshot_id"):
        reasons.append("dataset_database_snapshot_mismatch")

    validation_gain = _metric(candidate_report, "validation", "execution_accuracy") - _metric(
        baseline_report, "validation", "execution_accuracy"
    )
    fixed, regressed = _fixed_regressed(
        baseline_report, candidate_report, "validation"
    )
    net_fixed = fixed - regressed
    if validation_gain < 0.02:
        reasons.append("validation_execution_gain_below_0.02")
    if net_fixed < 5:
        reasons.append("validation_net_fixed_below_5")

    for split in ("validation", "sealed_holdout"):
        baseline_split = (baseline_report.get("splits") or {}).get(split) or {}
        candidate_split = (candidate_report.get("splits") or {}).get(split) or {}
        if not baseline_split or not candidate_split:
            reasons.append("%s_report_missing" % split)
        expected = expected_cases.get(split)
        if expected and (
            int(baseline_split.get("cases") or 0) != expected
            or int(candidate_split.get("cases") or 0) != expected
        ):
            reasons.append("%s_incomplete" % split)
        base_ids = {
            str(item.get("case_id"))
            for item in baseline_report.get("outcomes") or ()
            if item.get("split") == split and item.get("case_id")
        }
        candidate_ids = {
            str(item.get("case_id"))
            for item in candidate_report.get("outcomes") or ()
            if item.get("split") == split and item.get("case_id")
        }
        if base_ids != candidate_ids or len(base_ids) != int(baseline_split.get("cases") or 0):
            reasons.append("%s_case_set_mismatch" % split)
        if _metric(candidate_report, split, "readonly_safety_rate") < 1.0:
            reasons.append("%s_safety_below_1.0" % split)
        if int(((candidate_report.get("splits") or {}).get(split) or {}).get("framework_errors") or 0):
            reasons.append("%s_framework_errors" % split)
        for name in ("executable_rate", "ast_parse_rate"):
            if _metric(candidate_report, split, name) + 0.01 < _metric(
                baseline_report, split, name
            ):
                reasons.append("%s_%s_regression" % (split, name))
        baseline_p95 = _metric(baseline_report, split, "p95_latency_ms")
        candidate_p95 = _metric(candidate_report, split, "p95_latency_ms")
        if baseline_p95 > 0 and candidate_p95 > baseline_p95 * 1.2:
            reasons.append("%s_p95_latency_over_20_percent" % split)

        base_buckets = ((baseline_report.get("splits") or {}).get(split) or {}).get(
            "skeleton_buckets"
        ) or {}
        cand_buckets = ((candidate_report.get("splits") or {}).get(split) or {}).get(
            "skeleton_buckets"
        ) or {}
        if set(base_buckets) != set(cand_buckets):
            reasons.append("%s_skeleton_set_mismatch" % split)
        for skeleton in set(base_buckets).intersection(cand_buckets):
            base_accuracy = float(base_buckets[skeleton].get("execution_accuracy") or 0.0)
            cand_accuracy = float(cand_buckets[skeleton].get("execution_accuracy") or 0.0)
            if cand_accuracy + 0.03 < base_accuracy:
                reasons.append("%s_skeleton_regression:%s" % (split, skeleton))

    holdout_gain = _metric(candidate_report, "sealed_holdout", "execution_accuracy") - _metric(
        baseline_report, "sealed_holdout", "execution_accuracy"
    )
    if holdout_gain < 0:
        reasons.append("sealed_holdout_execution_regression")

    return {
        "eligible_for_human_approval": not reasons,
        "reasons": sorted(set(reasons)),
        "dataset_review": {
            key: value
            for key, value in dict(dataset_review_evidence or {}).items()
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
            "maximum_executable_or_ast_drop": 0.01,
            "maximum_skeleton_drop": 0.03,
            "maximum_p95_latency_multiplier": 1.2,
            "sealed_holdout_execution_drop": 0.0,
        },
        "observed": {
            "validation_execution_gain": round(validation_gain, 6),
            "validation_fixed": fixed,
            "validation_regressed": regressed,
            "validation_net_fixed": net_fixed,
            "sealed_holdout_execution_gain": round(holdout_gain, 6),
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
                reviewed_at TEXT NOT NULL DEFAULT ''
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
                knowledge_evidence_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_memory_state_skill
                ON memory_items(state, target_skill);
            CREATE INDEX IF NOT EXISTS idx_query_traces_recorded_at
                ON query_traces(recorded_at DESC);
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
        # A query that merely executed successfully is not semantic evidence.
        # Older rows created before the feedback gate are downgraded safely.
        self.connection.execute(
            "UPDATE experience_reviews SET eligible=0,"
            "eligibility_reasons_json=?,state='ineligible' "
            "WHERE source_kind='query_run' AND user_feedback='' "
            "AND state='candidate'",
            (_canonical(["requires_human_feedback"]),),
        )
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
        return {
            "latest_attempt": dict(latest_attempt) if latest_attempt else {},
            "recent_query_runs": runs,
        }

    def query_result_snapshot(
        self, task_id: str, user_id: str, session_id: str
    ) -> Mapping[str, Any]:
        row = self.connection.execute(
            "SELECT task_id,status,original_question,standalone_question,final_sql,"
            "answer_json,result_rows_json,recorded_at FROM query_traces "
            "WHERE task_id=? AND user_id=? AND session_id=?",
            (task_id[:200], user_id[:200], session_id[:200]),
        ).fetchone()
        if not row:
            return {}
        value = dict(row)
        value["answer"] = json.loads(value.pop("answer_json"))
        value["rows"] = json.loads(value.pop("result_rows_json"))
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
                        WHEN experience_reviews.state='promoted' THEN 'promoted'
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
    ) -> None:
        if decision not in {"correct", "incorrect"}:
            raise ValueError("feedback decision must be correct or incorrect")
        with self.connection:
            changed = self.connection.execute(
                "UPDATE query_traces SET feedback_status=? WHERE task_id=?",
                (decision, task_id[:200]),
            ).rowcount
            if not changed:
                raise ValueError("unknown query task")
            self.connection.execute(
                "UPDATE experience_reviews SET user_feedback=?,feedback_note=?,"
                "state=CASE WHEN ?='incorrect' AND state IN ('candidate','ineligible') "
                "THEN 'rejected' "
                "ELSE state END WHERE task_id=?",
                (decision, note[:2000], decision, task_id[:200]),
            )

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
        allowed = {"candidate", "ineligible", "promoted", "rejected"}
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
    ) -> Mapping[str, Any]:
        if decision not in {"approve", "reject"} or not actor.strip():
            raise ValueError("decision and actor are required")
        item = self.get_experience(experience_id)
        if item["state"] not in {"candidate", "promoted"}:
            raise ValueError("experience is not awaiting review")
        if decision == "approve" and not item["eligible"]:
            raise ValueError("ineligible experience cannot be promoted")
        if item["state"] == "promoted":
            if decision == "approve":
                return item
            raise ValueError("promoted experience requires a revoke workflow")
        with self.connection:
            self.connection.execute(
                "UPDATE experience_reviews SET state=?,reviewed_by=?,reviewed_at=?,"
                "knowledge_evidence_id=? WHERE experience_id=?",
                (
                    "promoted" if decision == "approve" else "rejected",
                    actor.strip()[:200],
                    _now(),
                    knowledge_evidence_id[:200],
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
        required = {"validation", "sealed_holdout"}
        if not required.issubset(set(value.get("evaluated_splits") or ())):
            raise ValueError("promotion requires validation and sealed_holdout in one pinned run")
        return report, {
            "model": dict(value.get("model") or {}),
            "evaluated_case_count": int(value.get("evaluated_case_count") or 0),
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

    def review_memory(
        self, memory_id: str, decision: str, actor: str, human_reviewed: bool
    ) -> None:
        if not human_reviewed:
            raise ValueError("explicit human review is required")
        if decision not in {"approve", "reject"} or not actor.strip():
            raise ValueError("decision and actor are required")
        row = self.connection.execute(
            "SELECT state FROM memory_items WHERE memory_id=?", (memory_id,)
        ).fetchone()
        if not row or row["state"] != "candidate":
            raise ValueError("memory item is not awaiting review")
        with self.connection:
            self.connection.execute(
                "UPDATE memory_items SET state=?,reviewed_by=?,reviewed_at=? WHERE memory_id=?",
                ("stable" if decision == "approve" else "rejected", actor.strip()[:200], _now(), memory_id),
            )

    @property
    def memory_snapshot_id(self) -> str:
        rows = self.connection.execute(
            "SELECT memory_id,target_skill,content FROM memory_items WHERE state='stable' ORDER BY memory_id"
        ).fetchall()
        return "memory-%s" % hashlib.sha256(
            _canonical([dict(row) for row in rows]).encode("utf-8")
        ).hexdigest()[:20]

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
        if state and state not in {"candidate", "stable", "rejected"}:
            raise ValueError("invalid memory state")
        sql = (
            "SELECT memory_id,target_skill,origin_split,failure_kind,content,state,created_at,"
            "reviewed_by,reviewed_at FROM memory_items"
        )
        params: tuple[Any, ...] = ()
        if state:
            sql += " WHERE state=?"
            params = (state,)
        sql += " ORDER BY created_at"
        return tuple(dict(row) for row in self.connection.execute(sql, params).fetchall())
