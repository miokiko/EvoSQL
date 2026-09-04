"""Text2SQL-specific shadow/canary release manager built on EvoAgent rollout ideas."""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

import sqlglot
from sqlglot import exp

from .evaluation import result_fingerprint


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return round(float(ordered[index]), 3)


def _sql_skeleton(sql: str) -> str:
    if not sql.strip():
        return ""
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
        tree = tree.transform(
            lambda node: exp.Placeholder() if isinstance(node, exp.Literal) else node
        )
        return tree.sql(dialect="sqlite", pretty=False)[:4000]
    except (sqlglot.errors.ParseError, TypeError, ValueError):
        return "<parse-error>"


def _wiki_refs(result: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()
    collaboration = result.get("collaboration") or {}
    for worker in collaboration.get("worker_results") or ():
        if not isinstance(worker, Mapping):
            continue
        for evidence_id in worker.get("observed_evidence_ids") or ():
            value = str(evidence_id)
            if value.startswith("wiki:"):
                values.add(value)
    return values


def _safe_ref_hash(value: str) -> str:
    return "wiki-ref-%s" % hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _result_signature(result: Mapping[str, Any]) -> str:
    if result.get("status") != "success":
        return ""
    answer = result.get("answer") or {}
    rows = answer.get("rows") or ()
    sql = str(result.get("final_sql") or "")
    try:
        ordered = bool(sqlglot.parse_one(sql, read="sqlite").args.get("order"))
    except (sqlglot.errors.ParseError, TypeError, ValueError):
        ordered = True
    return result_fingerprint(answer.get("columns") or (), rows, ordered)


def compare_shadow_results(
    stable: Mapping[str, Any],
    candidate: Optional[Mapping[str, Any]],
    candidate_error: str = "",
) -> Mapping[str, Any]:
    """Return a persistable diff with no question, raw SQL, result rows, or Wiki ids."""

    candidate = candidate or {}
    stable_sql = str(stable.get("final_sql") or "")
    candidate_sql = str(candidate.get("final_sql") or "")
    stable_result = _result_signature(stable)
    candidate_result = _result_signature(candidate)
    stable_status = str(stable.get("status") or "framework_error")
    candidate_status = str(candidate.get("status") or "framework_error")
    stable_errors = sorted(str(item) for item in (stable.get("gates") or {}).get("errors") or ())
    candidate_errors = sorted(
        str(item) for item in (candidate.get("gates") or {}).get("errors") or ()
    )
    if stable_status == "success" and candidate_status == "success":
        result_equivalent = bool(stable_result and stable_result == candidate_result)
    else:
        result_equivalent = (
            not candidate_error
            and stable_status == candidate_status
            and stable_errors == candidate_errors
        )

    unsafe_markers = {
        "read_only_query_required",
        "write_or_control_statement_forbidden",
        "comments_not_allowed",
        "exactly_one_statement_required",
    }
    unsafe = any(
        item in unsafe_markers or item.startswith("blocked_function")
        for item in candidate_errors
    )
    candidate_failed = bool(
        candidate_error
        or unsafe
        or (stable_status == "success" and candidate_status != "success")
    )
    stable_refs = _wiki_refs(stable)
    candidate_refs = _wiki_refs(candidate)
    stable_skeleton = _sql_skeleton(stable_sql)
    candidate_skeleton = _sql_skeleton(candidate_sql)
    sql_changed = hashlib.sha256(stable_sql.encode("utf-8")).hexdigest() != hashlib.sha256(
        candidate_sql.encode("utf-8")
    ).hexdigest()
    wiki_changed = stable_refs != candidate_refs
    return {
        "stable_status": stable_status,
        "candidate_status": candidate_status,
        "stable_sql_fingerprint": hashlib.sha256(stable_sql.encode("utf-8")).hexdigest(),
        "candidate_sql_fingerprint": hashlib.sha256(candidate_sql.encode("utf-8")).hexdigest(),
        "stable_sql_skeleton": stable_skeleton,
        "candidate_sql_skeleton": candidate_skeleton,
        "stable_result_fingerprint": stable_result,
        "candidate_result_fingerprint": candidate_result,
        "result_equivalent": result_equivalent,
        "sql_changed": sql_changed,
        "wiki_refs_added": sorted(_safe_ref_hash(value) for value in candidate_refs - stable_refs),
        "wiki_refs_removed": sorted(_safe_ref_hash(value) for value in stable_refs - candidate_refs),
        "wiki_refs_changed": wiki_changed,
        "candidate_failed": candidate_failed,
        "candidate_error_fingerprint": hashlib.sha256(
            candidate_error.encode("utf-8")
        ).hexdigest() if candidate_error else "",
        "review_required": bool(
            candidate_failed or not result_equivalent or sql_changed or wiki_changed
        ),
    }


class Text2SQLShadowReleaseManager:
    """Never auto-activates a policy; shadow and canary only produce release evidence."""

    def __init__(self, evolution_store: Any) -> None:
        self.store = evolution_store
        self.connection = evolution_store.connection
        self._initialize()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS shadow_deployments (
                deployment_id TEXT PRIMARY KEY,
                stable_policy_version TEXT NOT NULL,
                candidate_policy_version TEXT NOT NULL,
                version_pins_json TEXT NOT NULL,
                shadow_percent INTEGER NOT NULL,
                canary_percent INTEGER NOT NULL DEFAULT 0,
                min_shadow_samples INTEGER NOT NULL,
                min_canary_samples INTEGER NOT NULL,
                max_candidate_failure_rate REAL NOT NULL,
                max_result_disagreement_rate REAL NOT NULL,
                max_p95_latency_multiplier REAL NOT NULL,
                status TEXT NOT NULL,
                shadow_samples INTEGER NOT NULL DEFAULT 0,
                shadow_candidate_failures INTEGER NOT NULL DEFAULT 0,
                result_disagreements INTEGER NOT NULL DEFAULT 0,
                canary_samples INTEGER NOT NULL DEFAULT 0,
                canary_failures INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_observations (
                observation_id TEXT PRIMARY KEY,
                deployment_id TEXT NOT NULL,
                task_key_hash TEXT NOT NULL,
                assignment_bucket INTEGER NOT NULL,
                lane TEXT NOT NULL,
                stable_policy_version TEXT NOT NULL,
                candidate_policy_version TEXT NOT NULL,
                diff_json TEXT NOT NULL,
                stable_latency_ms REAL NOT NULL,
                candidate_latency_ms REAL NOT NULL,
                review_state TEXT NOT NULL,
                review_verdict TEXT NOT NULL DEFAULT '',
                reviewed_by TEXT NOT NULL DEFAULT '',
                review_reason TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(deployment_id) REFERENCES shadow_deployments(deployment_id)
            );
            CREATE INDEX IF NOT EXISTS idx_shadow_deployment_status
                ON shadow_deployments(status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_shadow_observation_review
                ON shadow_observations(deployment_id, review_state, created_at);
            """
        )
        deployment_columns = {
            str(row["name"])
            for row in self.connection.execute(
                "PRAGMA table_info(shadow_deployments)"
            ).fetchall()
        }
        if "version_pins_json" not in deployment_columns:
            self.connection.execute(
                "ALTER TABLE shadow_deployments ADD COLUMN version_pins_json TEXT NOT NULL DEFAULT '{}'"
            )
        self.connection.commit()

    def configure_shadow(
        self,
        candidate_version: str,
        current_version_pins: Mapping[str, str],
        actor: str,
        shadow_percent: int = 5,
        min_samples: int = 20,
        max_candidate_failure_rate: float = 0.0,
        max_result_disagreement_rate: float = 0.2,
        max_p95_latency_multiplier: float = 1.2,
    ) -> Mapping[str, Any]:
        if not actor.strip():
            raise ValueError("actor is required")
        if not 1 <= int(shadow_percent) <= 100:
            raise ValueError("shadow_percent must be between 1 and 100")
        if not 1 <= int(min_samples) <= 100000:
            raise ValueError("min_samples must be between 1 and 100000")
        if not 0 <= float(max_candidate_failure_rate) <= 1:
            raise ValueError("invalid candidate failure threshold")
        if not 0 <= float(max_result_disagreement_rate) <= 1:
            raise ValueError("invalid result disagreement threshold")
        if not 1.0 <= float(max_p95_latency_multiplier) <= 5.0:
            raise ValueError("invalid P95 latency multiplier")

        policy = self.connection.execute(
            "SELECT parent_version,status FROM policy_versions WHERE policy_version=?",
            (candidate_version,),
        ).fetchone()
        if not policy or policy["status"] != "shadow_ready":
            raise ValueError("candidate must pass offline gates before shadow")
        stable_version = self.store.active_policy_version
        if policy["parent_version"] != stable_version:
            raise ValueError("candidate parent is not the active stable policy")
        run = self.connection.execute(
            "SELECT candidate_aggregate_json,decision_json FROM evolution_runs "
            "WHERE candidate_policy_version=? ORDER BY created_at DESC LIMIT 1",
            (candidate_version,),
        ).fetchone()
        if not run or not json.loads(run["decision_json"]).get("eligible_for_human_approval"):
            raise ValueError("candidate lacks a passing offline evaluation")
        evaluated_pins = json.loads(run["candidate_aggregate_json"]).get("version_pins") or {}
        for name in (
            "database_snapshot_id",
            "wiki_index_version",
            "memory_snapshot_id",
        ):
            if str(evaluated_pins.get(name) or "") != str(current_version_pins.get(name) or ""):
                raise ValueError("%s changed after offline evaluation; replay is required" % name)

        deployment_id = "shadow-%s" % uuid.uuid4().hex
        timestamp = _now()
        with self.connection:
            self.connection.execute(
                "UPDATE policy_versions SET status='shadow_superseded' WHERE policy_version IN "
                "(SELECT candidate_policy_version FROM shadow_deployments WHERE status IN "
                "('shadow_running','shadow_review','shadow_passed','canary_running'))"
            )
            self.connection.execute(
                "UPDATE shadow_deployments SET status='superseded',updated_at=? "
                "WHERE status IN ('shadow_running','shadow_review','shadow_passed','canary_running')",
                (timestamp,),
            )
            self.connection.execute(
                """
                INSERT INTO shadow_deployments(
                    deployment_id,stable_policy_version,candidate_policy_version,version_pins_json,shadow_percent,
                    min_shadow_samples,min_canary_samples,max_candidate_failure_rate,
                    max_result_disagreement_rate,max_p95_latency_multiplier,status,created_by,
                    created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,'shadow_running',?,?,?)
                """,
                (
                    deployment_id,
                    stable_version,
                    candidate_version,
                    _canonical(dict(current_version_pins)),
                    int(shadow_percent),
                    int(min_samples),
                    20,
                    float(max_candidate_failure_rate),
                    float(max_result_disagreement_rate),
                    float(max_p95_latency_multiplier),
                    actor.strip()[:200],
                    timestamp,
                    timestamp,
                ),
            )
            self.connection.execute(
                "UPDATE policy_versions SET status='shadow_running' WHERE policy_version=?",
                (candidate_version,),
            )
            self.connection.execute(
                "INSERT INTO activation_audit VALUES (?,?,?,?,?,?,?)",
                (
                    "activation-%s" % uuid.uuid4().hex,
                    "shadow_configure",
                    stable_version,
                    candidate_version,
                    actor.strip()[:200],
                    "shadow_percent=%d,min_samples=%d" % (
                        int(shadow_percent),
                        int(min_samples),
                    ),
                    timestamp,
                ),
            )
        return self.get_deployment(deployment_id) or {}

    def get_deployment(self, deployment_id: str) -> Optional[Mapping[str, Any]]:
        row = self.connection.execute(
            "SELECT * FROM shadow_deployments WHERE deployment_id=?", (deployment_id,)
        ).fetchone()
        return dict(row) if row else None

    def active_deployment(self) -> Optional[Mapping[str, Any]]:
        row = self.connection.execute(
            "SELECT * FROM shadow_deployments WHERE status IN ('shadow_running','canary_running') "
            "ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def assignment(
        self, task_key: str, current_version_pins: Optional[Mapping[str, str]] = None
    ) -> Mapping[str, Any]:
        deployment = self.active_deployment()
        if not deployment:
            return {"lane": "stable", "shadow": False, "deployment": None, "bucket": None}
        if deployment["stable_policy_version"] != self.store.active_policy_version:
            self._rollback(deployment["deployment_id"], "stable_policy_changed")
            return {"lane": "stable", "shadow": False, "deployment": None, "bucket": None}
        if current_version_pins is not None:
            expected = json.loads(deployment["version_pins_json"])
            for name in (
                "database_snapshot_id",
                "wiki_index_version",
                "memory_snapshot_id",
            ):
                if str(expected.get(name) or "") != str(current_version_pins.get(name) or ""):
                    self._rollback(
                        deployment["deployment_id"], "runtime_%s_changed" % name
                    )
                    return {
                        "lane": "stable",
                        "shadow": False,
                        "deployment": None,
                        "bucket": None,
                    }
        bucket = int(
            hashlib.sha256(
                ("text2sql:%s:%s" % (deployment["deployment_id"], task_key)).encode("utf-8")
            ).hexdigest()[:8],
            16,
        ) % 100
        if deployment["status"] == "canary_running":
            return {
                "lane": "canary" if bucket < deployment["canary_percent"] else "stable",
                "shadow": bucket < deployment["canary_percent"],
                "deployment": deployment,
                "bucket": bucket,
            }
        return {
            "lane": "stable",
            "shadow": bucket < deployment["shadow_percent"],
            "deployment": deployment,
            "bucket": bucket,
        }

    @staticmethod
    def _run(runner: Callable[[str], Mapping[str, Any]], question: str) -> tuple[Mapping[str, Any], str, float]:
        started = time.monotonic()
        try:
            return dict(runner(question)), "", (time.monotonic() - started) * 1000
        except Exception as exc:
            return {}, str(exc)[:1000], (time.monotonic() - started) * 1000

    def execute(
        self,
        question: str,
        task_key: str,
        stable_runner: Callable[[str], Mapping[str, Any]],
        candidate_runner_factory: Callable[[str], Callable[[str], Mapping[str, Any]]],
        current_version_pins: Optional[Mapping[str, str]] = None,
    ) -> Mapping[str, Any]:
        assignment = self.assignment(task_key, current_version_pins)
        deployment = assignment["deployment"]
        if not deployment or not assignment["shadow"]:
            stable, error, _ = self._run(stable_runner, question)
            if error:
                raise RuntimeError(error)
            stable["release"] = {
                "lane": "stable",
                "shadow_sampled": False,
                "candidate_output_used": False,
            }
            return stable

        candidate_runner = candidate_runner_factory(deployment["candidate_policy_version"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            stable_future = pool.submit(self._run, stable_runner, question)
            candidate_future = pool.submit(self._run, candidate_runner, question)
            stable, stable_error, stable_latency = stable_future.result()
            candidate, candidate_error, candidate_latency = candidate_future.result()
        if stable_error:
            raise RuntimeError(stable_error)
        diff = compare_shadow_results(stable, candidate, candidate_error)
        observation = self._record_observation(
            deployment,
            task_key,
            int(assignment["bucket"]),
            str(assignment["lane"]),
            diff,
            stable_latency,
            candidate_latency,
        )
        use_candidate = assignment["lane"] == "canary" and not diff["candidate_failed"]
        primary = dict(candidate if use_candidate else stable)
        primary["release"] = {
            "lane": assignment["lane"],
            "shadow_sampled": True,
            "candidate_output_used": use_candidate,
            "fallback_to_stable": assignment["lane"] == "canary" and not use_candidate,
            "deployment_id": deployment["deployment_id"],
            "observation_id": observation["observation_id"],
            "deployment_status": observation["deployment_status"],
        }
        return primary

    def _record_observation(
        self,
        deployment: Mapping[str, Any],
        task_key: str,
        bucket: int,
        lane: str,
        diff: Mapping[str, Any],
        stable_latency: float,
        candidate_latency: float,
    ) -> Mapping[str, Any]:
        task_key_hash = hashlib.sha256(task_key.encode("utf-8")).hexdigest()
        existing = self.connection.execute(
            "SELECT observation_id,diff_json FROM shadow_observations WHERE deployment_id=? "
            "AND task_key_hash=? AND lane=? ORDER BY created_at LIMIT 1",
            (deployment["deployment_id"], task_key_hash, lane),
        ).fetchone()
        if existing:
            return {
                "observation_id": existing["observation_id"],
                "deployment_status": self._advance_or_rollback(
                    deployment["deployment_id"], json.loads(existing["diff_json"])
                ),
            }
        observation_id = "shadow-observation-%s" % _hash(
            [deployment["deployment_id"], task_key_hash, lane]
        )[:32]
        review_state = (
            "pending" if lane == "stable" and diff["review_required"] else "not_required"
        )
        timestamp = _now()
        with self.connection:
            inserted = self.connection.execute(
                """
                INSERT OR IGNORE INTO shadow_observations(
                    observation_id,deployment_id,task_key_hash,assignment_bucket,lane,
                    stable_policy_version,candidate_policy_version,diff_json,stable_latency_ms,
                    candidate_latency_ms,review_state,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    observation_id,
                    deployment["deployment_id"],
                    task_key_hash,
                    bucket,
                    lane,
                    deployment["stable_policy_version"],
                    deployment["candidate_policy_version"],
                    _canonical(diff),
                    round(stable_latency, 3),
                    round(candidate_latency, 3),
                    review_state,
                    timestamp,
                ),
            )
            if inserted.rowcount != 1:
                persisted = self.connection.execute(
                    "SELECT diff_json FROM shadow_observations WHERE observation_id=?",
                    (observation_id,),
                ).fetchone()
                return {
                    "observation_id": observation_id,
                    "deployment_status": self._advance_or_rollback(
                        deployment["deployment_id"],
                        json.loads(persisted["diff_json"]) if persisted else diff,
                    ),
                }
            if lane == "canary":
                self.connection.execute(
                    "UPDATE shadow_deployments SET canary_samples=canary_samples+1,"
                    "canary_failures=canary_failures+?,updated_at=? WHERE deployment_id=?",
                    (int(diff["candidate_failed"]), timestamp, deployment["deployment_id"]),
                )
            else:
                self.connection.execute(
                    "UPDATE shadow_deployments SET shadow_samples=shadow_samples+1,"
                    "shadow_candidate_failures=shadow_candidate_failures+?,"
                    "result_disagreements=result_disagreements+?,updated_at=? WHERE deployment_id=?",
                    (
                        int(diff["candidate_failed"]),
                        int(not diff["result_equivalent"]),
                        timestamp,
                        deployment["deployment_id"],
                    ),
                )
            # Keep the sample/counters and its safety transition in one SQLite
            # transaction so a process crash cannot strand a failed candidate.
            status = self._advance_or_rollback(deployment["deployment_id"], diff)
        return {"observation_id": observation_id, "deployment_status": status}

    def _latencies(self, deployment_id: str, lane: str) -> tuple[list[float], list[float]]:
        rows = self.connection.execute(
            "SELECT stable_latency_ms,candidate_latency_ms FROM shadow_observations "
            "WHERE deployment_id=? AND lane=?",
            (deployment_id, lane),
        ).fetchall()
        return (
            [float(row["stable_latency_ms"]) for row in rows],
            [float(row["candidate_latency_ms"]) for row in rows],
        )

    def _advance_or_rollback(
        self, deployment_id: str, latest_diff: Mapping[str, Any]
    ) -> str:
        deployment = self.get_deployment(deployment_id)
        if not deployment:
            raise ValueError("unknown shadow deployment")
        if latest_diff["candidate_failed"]:
            return self._rollback(deployment_id, "candidate_runtime_or_safety_failure")
        if deployment["status"] == "shadow_running":
            samples = int(deployment["shadow_samples"])
            if samples < int(deployment["min_shadow_samples"]):
                return "shadow_running"
            failure_rate = deployment["shadow_candidate_failures"] / max(1, samples)
            disagreement_rate = deployment["result_disagreements"] / max(1, samples)
            stable_latency, candidate_latency = self._latencies(deployment_id, "stable")
            latency_failed = bool(
                stable_latency
                and _percentile(candidate_latency, 0.95)
                > _percentile(stable_latency, 0.95)
                * float(deployment["max_p95_latency_multiplier"])
            )
            if (
                failure_rate > float(deployment["max_candidate_failure_rate"])
                or disagreement_rate > float(deployment["max_result_disagreement_rate"])
                or latency_failed
            ):
                return self._rollback(deployment_id, "shadow_threshold_exceeded")
            with self.connection:
                self.connection.execute(
                    "UPDATE shadow_deployments SET status='shadow_review',shadow_percent=0,updated_at=? "
                    "WHERE deployment_id=?",
                    (_now(), deployment_id),
                )
                self.connection.execute(
                    "UPDATE policy_versions SET status='shadow_review' WHERE policy_version=?",
                    (deployment["candidate_policy_version"],),
                )
            return "shadow_review"
        if deployment["status"] == "canary_running":
            samples = int(deployment["canary_samples"])
            if samples < int(deployment["min_canary_samples"]):
                return "canary_running"
            failure_rate = deployment["canary_failures"] / max(1, samples)
            if failure_rate > float(deployment["max_candidate_failure_rate"]):
                return self._rollback(deployment_id, "canary_error_budget_exceeded")
            with self.connection:
                self.connection.execute(
                    "UPDATE shadow_deployments SET status='canary_passed',canary_percent=0,updated_at=? "
                    "WHERE deployment_id=?",
                    (_now(), deployment_id),
                )
                self.connection.execute(
                    "UPDATE policy_versions SET status='canary_passed' WHERE policy_version=?",
                    (deployment["candidate_policy_version"],),
                )
            return "canary_passed"
        return str(deployment["status"])

    def _rollback(self, deployment_id: str, reason: str) -> str:
        deployment = self.get_deployment(deployment_id)
        if not deployment:
            return "rolled_back"
        with self.connection:
            self.connection.execute(
                "UPDATE shadow_deployments SET status='rolled_back',shadow_percent=0,"
                "canary_percent=0,updated_at=? WHERE deployment_id=?",
                (_now(), deployment_id),
            )
            self.connection.execute(
                "UPDATE policy_versions SET status='shadow_rejected' WHERE policy_version=?",
                (deployment["candidate_policy_version"],),
            )
            self.connection.execute(
                "INSERT INTO activation_audit VALUES (?,?,?,?,?,?,?)",
                (
                    "activation-%s" % uuid.uuid4().hex,
                    "automatic_release_rollback",
                    deployment["candidate_policy_version"],
                    deployment["stable_policy_version"],
                    "text2sql-release-manager",
                    reason[:2000],
                    _now(),
                ),
            )
        return "rolled_back"

    def review_observation(
        self,
        observation_id: str,
        verdict: str,
        actor: str,
        reason: str,
        human_reviewed: bool,
    ) -> None:
        if not human_reviewed:
            raise ValueError("explicit human review is required")
        if verdict not in {"equivalent", "candidate_better", "stable_better", "reject"}:
            raise ValueError("invalid shadow review verdict")
        if not actor.strip() or not reason.strip():
            raise ValueError("review actor and reason are required")
        row = self.connection.execute(
            "SELECT o.review_state,d.status AS deployment_status FROM shadow_observations o "
            "JOIN shadow_deployments d ON d.deployment_id=o.deployment_id "
            "WHERE o.observation_id=?",
            (observation_id,),
        ).fetchone()
        if not row or row["review_state"] != "pending":
            raise ValueError("shadow observation is not awaiting review")
        if row["deployment_status"] != "shadow_review":
            raise ValueError("deployment is not in human shadow review")
        with self.connection:
            self.connection.execute(
                "UPDATE shadow_observations SET review_state='reviewed',review_verdict=?,"
                "reviewed_by=?,review_reason=?,reviewed_at=? WHERE observation_id=?",
                (verdict, actor.strip()[:200], reason.strip()[:2000], _now(), observation_id),
            )

    def approve_shadow(
        self, deployment_id: str, actor: str, reason: str, human_approved: bool
    ) -> None:
        if not human_approved:
            raise ValueError("explicit human approval is required")
        if not actor.strip() or not reason.strip():
            raise ValueError("approval actor and reason are required")
        deployment = self.get_deployment(deployment_id)
        if not deployment or deployment["status"] != "shadow_review":
            raise ValueError("deployment is not awaiting shadow approval")
        pending = self.connection.execute(
            "SELECT COUNT(*) FROM shadow_observations WHERE deployment_id=? AND review_state='pending'",
            (deployment_id,),
        ).fetchone()[0]
        adverse = self.connection.execute(
            "SELECT COUNT(*) FROM shadow_observations WHERE deployment_id=? "
            "AND review_verdict IN ('stable_better','reject')",
            (deployment_id,),
        ).fetchone()[0]
        if pending:
            raise ValueError("all shadow differences require human review")
        if adverse:
            self._rollback(deployment_id, "human_shadow_review_rejected_candidate")
            raise ValueError("shadow review contains an adverse verdict")
        with self.connection:
            self.connection.execute(
                "UPDATE shadow_deployments SET status='shadow_passed',updated_at=? WHERE deployment_id=?",
                (_now(), deployment_id),
            )
            self.connection.execute(
                "UPDATE policy_versions SET status='canary_ready',reviewed_by=?,reviewed_at=? "
                "WHERE policy_version=?",
                (actor.strip()[:200], _now(), deployment["candidate_policy_version"]),
            )
            self.connection.execute(
                "INSERT INTO activation_audit VALUES (?,?,?,?,?,?,?)",
                (
                    "activation-%s" % uuid.uuid4().hex,
                    "shadow_approve",
                    deployment["stable_policy_version"],
                    deployment["candidate_policy_version"],
                    actor.strip()[:200],
                    reason.strip()[:2000],
                    _now(),
                ),
            )

    def start_canary(
        self,
        deployment_id: str,
        actor: str,
        canary_percent: int = 5,
        min_samples: int = 20,
    ) -> None:
        if not actor.strip():
            raise ValueError("actor is required")
        if not 1 <= int(canary_percent) <= 100 or not 1 <= int(min_samples) <= 100000:
            raise ValueError("invalid canary percentage or sample count")
        deployment = self.get_deployment(deployment_id)
        if not deployment or deployment["status"] != "shadow_passed":
            raise ValueError("shadow must pass before canary")
        with self.connection:
            self.connection.execute(
                "UPDATE shadow_deployments SET status='canary_running',canary_percent=?,"
                "min_canary_samples=?,canary_samples=0,canary_failures=0,updated_at=? "
                "WHERE deployment_id=?",
                (int(canary_percent), int(min_samples), _now(), deployment_id),
            )
            self.connection.execute(
                "UPDATE policy_versions SET status='canary_running' WHERE policy_version=?",
                (deployment["candidate_policy_version"],),
            )
            self.connection.execute(
                "INSERT INTO activation_audit VALUES (?,?,?,?,?,?,?)",
                (
                    "activation-%s" % uuid.uuid4().hex,
                    "canary_start",
                    deployment["stable_policy_version"],
                    deployment["candidate_policy_version"],
                    actor.strip()[:200],
                    "canary_percent=%d,min_samples=%d" % (
                        int(canary_percent),
                        int(min_samples),
                    ),
                    _now(),
                ),
            )

    def list_observations(
        self, deployment_id: str, review_state: str = "", limit: int = 100
    ) -> Sequence[Mapping[str, Any]]:
        if review_state and review_state not in {"pending", "reviewed", "not_required"}:
            raise ValueError("invalid review_state")
        sql = "SELECT * FROM shadow_observations WHERE deployment_id=?"
        params: list[Any] = [deployment_id]
        if review_state:
            sql += " AND review_state=?"
            params.append(review_state)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        values = []
        for row in self.connection.execute(sql, tuple(params)).fetchall():
            item = dict(row)
            item["diff"] = json.loads(item.pop("diff_json"))
            values.append(item)
        return tuple(values)

    def status(self, deployment_id: str = "") -> Mapping[str, Any]:
        if deployment_id:
            deployment = self.get_deployment(deployment_id)
        else:
            row = self.connection.execute(
                "SELECT * FROM shadow_deployments ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            deployment = dict(row) if row else None
        if not deployment:
            return {"status": "inactive"}
        observations = self.list_observations(deployment["deployment_id"], limit=500)
        shadow = [item for item in observations if item["lane"] == "stable"]
        canary = [item for item in observations if item["lane"] == "canary"]
        return {
            **deployment,
            "shadow_candidate_failure_rate": round(
                deployment["shadow_candidate_failures"] / max(1, deployment["shadow_samples"]),
                6,
            ),
            "shadow_result_disagreement_rate": round(
                deployment["result_disagreements"] / max(1, deployment["shadow_samples"]),
                6,
            ),
            "canary_failure_rate": round(
                deployment["canary_failures"] / max(1, deployment["canary_samples"]), 6
            ),
            "pending_human_reviews": sum(item["review_state"] == "pending" for item in observations),
            "shadow_p95_latency_ms": {
                "stable": _percentile([item["stable_latency_ms"] for item in shadow], 0.95),
                "candidate": _percentile([item["candidate_latency_ms"] for item in shadow], 0.95),
            },
            "canary_p95_latency_ms": {
                "stable": _percentile([item["stable_latency_ms"] for item in canary], 0.95),
                "candidate": _percentile([item["candidate_latency_ms"] for item in canary], 0.95),
            },
        }
