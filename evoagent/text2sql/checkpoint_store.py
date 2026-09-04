"""Durable, identity-bound checkpoints for Text2SQL runtime nodes.

The Text2SQL release path may execute stable and candidate engines on different
threads.  This store therefore opens a short-lived SQLite connection for every
operation instead of sharing the evolution store's thread-bound connection.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


CHECKPOINT_CONTRACT_VERSION = "text2sql-runtime-checkpoint-v1"
MAX_CHECKPOINT_JSON_BYTES = 4 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_json(value: Any, label: str) -> str:
    rendered = _canonical(value)
    if len(rendered.encode("utf-8")) > MAX_CHECKPOINT_JSON_BYTES:
        raise ValueError("%s exceeds checkpoint size limit" % label)
    return rendered


def _verified_json(value: str, checksum: str, label: str) -> Any:
    if not checksum or _text_digest(value) != checksum:
        raise Text2SQLCheckpointCorruptionError(
            "%s checkpoint integrity check failed" % label
        )
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise Text2SQLCheckpointCorruptionError(
            "%s checkpoint JSON is invalid" % label
        ) from exc


def _run_key(task_id: str) -> str:
    value = str(task_id or "").strip()
    if not value:
        raise ValueError("checkpoint task_id is required")
    return "text2sql-run-%s" % hashlib.sha256(value.encode("utf-8")).hexdigest()


class Text2SQLCheckpointIdentityError(ValueError):
    """A task id was reused with different inputs, permissions, or versions."""


class Text2SQLCheckpointCorruptionError(ValueError):
    """Persisted identity, state, telemetry, or result failed integrity checks."""


class Text2SQLCheckpointBusy(RuntimeError):
    """Another process or thread currently owns the same runtime run."""


class Text2SQLRuntimeCheckpointStore:
    """Thread-safe SQLite persistence for one Text2SQL runtime graph."""

    def __init__(self, path: Path, busy_timeout_ms: int = 30000) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = max(1000, int(busy_timeout_ms))
        self._write_lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = %d" % self.busy_timeout_ms)
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with self._write_lock, closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS text2sql_checkpoint_runs (
                    run_key TEXT PRIMARY KEY,
                    identity_sha256 TEXT NOT NULL,
                    identity_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    owner_token TEXT NOT NULL DEFAULT '',
                    lease_expires_at REAL NOT NULL DEFAULT 0,
                    execution_json TEXT NOT NULL DEFAULT '{}',
                    execution_sha256 TEXT NOT NULL DEFAULT '',
                    result_json TEXT,
                    result_sha256 TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS text2sql_runtime_checkpoints (
                    run_key TEXT NOT NULL,
                    node TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    state_json TEXT NOT NULL,
                    state_sha256 TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(run_key, node),
                    FOREIGN KEY(run_key) REFERENCES text2sql_checkpoint_runs(run_key)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_text2sql_checkpoint_status
                    ON text2sql_checkpoint_runs(status, updated_at);
                """
            )
            run_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(text2sql_checkpoint_runs)"
                ).fetchall()
            }
            if "execution_sha256" not in run_columns:
                connection.execute(
                    "ALTER TABLE text2sql_checkpoint_runs "
                    "ADD COLUMN execution_sha256 TEXT NOT NULL DEFAULT ''"
                )
            if "result_sha256" not in run_columns:
                connection.execute(
                    "ALTER TABLE text2sql_checkpoint_runs "
                    "ADD COLUMN result_sha256 TEXT NOT NULL DEFAULT ''"
                )
            checkpoint_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(text2sql_runtime_checkpoints)"
                ).fetchall()
            }
            if "state_sha256" not in checkpoint_columns:
                connection.execute(
                    "ALTER TABLE text2sql_runtime_checkpoints "
                    "ADD COLUMN state_sha256 TEXT NOT NULL DEFAULT ''"
                )
            self._backfill_hashes(connection)
            connection.commit()

    @staticmethod
    def _backfill_hashes(connection: sqlite3.Connection) -> None:
        for row in connection.execute(
            "SELECT run_key,execution_json,result_json,execution_sha256,result_sha256 "
            "FROM text2sql_checkpoint_runs"
        ).fetchall():
            execution_json = str(row["execution_json"] or "{}")
            result_json = row["result_json"]
            connection.execute(
                "UPDATE text2sql_checkpoint_runs SET execution_sha256=?,result_sha256=? "
                "WHERE run_key=?",
                (
                    str(row["execution_sha256"] or _text_digest(execution_json)),
                    str(
                        row["result_sha256"]
                        or (_text_digest(str(result_json)) if result_json is not None else "")
                    ),
                    row["run_key"],
                ),
            )
        for row in connection.execute(
            "SELECT run_key,node,state_json,state_sha256 "
            "FROM text2sql_runtime_checkpoints"
        ).fetchall():
            state_json = str(row["state_json"])
            connection.execute(
                "UPDATE text2sql_runtime_checkpoints SET state_sha256=? "
                "WHERE run_key=? AND node=?",
                (
                    str(row["state_sha256"] or _text_digest(state_json)),
                    row["run_key"],
                    row["node"],
                ),
            )

    def acquire(
        self,
        task_id: str,
        identity: Mapping[str, Any],
        lease_seconds: int = 900,
    ) -> "Text2SQLRuntimeCheckpointSession":
        run_key = _run_key(task_id)
        identity_value = {
            "contract_version": CHECKPOINT_CONTRACT_VERSION,
            **dict(identity),
        }
        runtime_identity = identity_value.get("runtime")
        node_order = tuple(
            (runtime_identity.get("nodes") or ())
            if isinstance(runtime_identity, Mapping)
            else ()
        )
        identity_json = _bounded_json(identity_value, "checkpoint identity")
        identity_sha256 = _digest(identity_value)
        owner_token = uuid.uuid4().hex
        timestamp = _now()
        now_epoch = time.time()
        lease_expires_at = now_epoch + max(30, int(lease_seconds))

        with self._write_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT identity_sha256,identity_json,status,owner_token,lease_expires_at,"
                "execution_json,execution_sha256,result_json,result_sha256 "
                "FROM text2sql_checkpoint_runs WHERE run_key=?",
                (run_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO text2sql_checkpoint_runs("
                    "run_key,identity_sha256,identity_json,status,owner_token,"
                    "lease_expires_at,execution_sha256,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        run_key,
                        identity_sha256,
                        identity_json,
                        "running",
                        owner_token,
                        lease_expires_at,
                        _text_digest("{}"),
                        timestamp,
                        timestamp,
                    ),
                )
                execution = {}
                cached_result = None
            else:
                if _text_digest(str(row["identity_json"])) != row["identity_sha256"]:
                    connection.rollback()
                    raise Text2SQLCheckpointCorruptionError(
                        "checkpoint identity integrity check failed"
                    )
                if row["identity_sha256"] != identity_sha256:
                    connection.rollback()
                    raise Text2SQLCheckpointIdentityError(
                        "checkpoint task_id was reused with a different question, "
                        "principal, model, runtime configuration, or version pin"
                    )
                execution = _verified_json(
                    str(row["execution_json"] or "{}"),
                    str(row["execution_sha256"] or ""),
                    "execution ledger",
                )
                cached_result = (
                    _verified_json(
                        str(row["result_json"]),
                        str(row["result_sha256"] or ""),
                        "completed result",
                    )
                    if row["status"] == "completed" and row["result_json"]
                    else None
                )
                if row["status"] == "completed" and cached_result is None:
                    connection.rollback()
                    raise Text2SQLCheckpointCorruptionError(
                        "completed checkpoint is missing its result"
                )
                if cached_result is not None:
                    connection.commit()
                    completed_session = Text2SQLRuntimeCheckpointSession(
                        self,
                        task_id,
                        run_key,
                        "",
                        execution,
                        cached_result,
                        max(30, int(lease_seconds)),
                        node_order,
                    )
                    completed_session.load_checkpoints(task_id)
                    return completed_session
                if (
                    row["owner_token"]
                    and float(row["lease_expires_at"] or 0) > now_epoch
                ):
                    connection.rollback()
                    raise Text2SQLCheckpointBusy(
                        "checkpoint run is already executing for this task_id"
                    )
                connection.execute(
                    "UPDATE text2sql_checkpoint_runs SET status='running',owner_token=?,"
                    "lease_expires_at=?,error='',updated_at=? WHERE run_key=?",
                    (owner_token, lease_expires_at, timestamp, run_key),
                )
            connection.commit()

        return Text2SQLRuntimeCheckpointSession(
            self,
            task_id,
            run_key,
            owner_token,
            execution,
            cached_result,
            max(30, int(lease_seconds)),
            node_order,
        )

    def _load_checkpoints(self, run_key: str) -> Dict[str, Dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT node,status,attempt,state_json,state_sha256,error,updated_at "
                "FROM text2sql_runtime_checkpoints WHERE run_key=? ORDER BY updated_at,node",
                (run_key,),
            ).fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            value = dict(row)
            state_json = str(value.pop("state_json"))
            state_sha256 = str(value.pop("state_sha256"))
            value["state"] = _verified_json(
                state_json, state_sha256, "node %s" % value["node"]
            )
            result[value.pop("node")] = value
        return result

    def _save_checkpoint(
        self,
        run_key: str,
        owner_token: str,
        node: str,
        state: Mapping[str, Any],
        status: str,
        attempt: int,
        error: str,
        execution: Mapping[str, Any],
        lease_seconds: int,
    ) -> None:
        timestamp = _now()
        state_json = _bounded_json(dict(state), "checkpoint node state")
        execution_json = _bounded_json(dict(execution), "checkpoint execution ledger")
        with self._write_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_owner(connection, run_key, owner_token)
            existing = connection.execute(
                "SELECT status,attempt,state_json,state_sha256 "
                "FROM text2sql_runtime_checkpoints WHERE run_key=? AND node=?",
                (run_key, str(node)[:200]),
            ).fetchone()
            if existing:
                _verified_json(
                    str(existing["state_json"]),
                    str(existing["state_sha256"]),
                    "existing node %s" % node,
                )
                if existing["status"] == "completed":
                    if str(status) != "completed" or str(existing["state_json"]) != state_json:
                        connection.rollback()
                        raise Text2SQLCheckpointCorruptionError(
                            "completed checkpoint node cannot be overwritten"
                        )
                elif int(attempt) < int(existing["attempt"]):
                    connection.rollback()
                    raise Text2SQLCheckpointCorruptionError(
                        "checkpoint attempt moved backwards"
                    )
            if not existing or existing["status"] != "completed":
                connection.execute(
                    "INSERT INTO text2sql_runtime_checkpoints("
                    "run_key,node,status,attempt,state_json,state_sha256,error,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(run_key,node) DO UPDATE SET "
                    "status=excluded.status,attempt=excluded.attempt,"
                    "state_json=excluded.state_json,state_sha256=excluded.state_sha256,"
                    "error=excluded.error,updated_at=excluded.updated_at",
                    (
                        run_key,
                        str(node)[:200],
                        str(status)[:50],
                        max(1, int(attempt)),
                        state_json,
                        _text_digest(state_json),
                        str(error)[:2000],
                        timestamp,
                    ),
                )
            connection.execute(
                "UPDATE text2sql_checkpoint_runs SET execution_json=?,execution_sha256=?,"
                "lease_expires_at=?,"
                "updated_at=? WHERE run_key=? AND owner_token=?",
                (
                    execution_json,
                    _text_digest(execution_json),
                    time.time() + lease_seconds,
                    timestamp,
                    run_key,
                    owner_token,
                ),
            )
            connection.commit()

    def _finish(
        self,
        run_key: str,
        owner_token: str,
        status: str,
        execution: Mapping[str, Any],
        result: Optional[Mapping[str, Any]] = None,
        error: str = "",
    ) -> None:
        timestamp = _now()
        execution_json = _bounded_json(dict(execution), "checkpoint execution ledger")
        result_json = (
            _bounded_json(dict(result), "checkpoint completed result")
            if result is not None
            else None
        )
        with self._write_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_owner(connection, run_key, owner_token)
            connection.execute(
                "UPDATE text2sql_checkpoint_runs SET status=?,owner_token='',"
                "lease_expires_at=0,execution_json=?,execution_sha256=?,"
                "result_json=?,result_sha256=?,error=?,updated_at=? "
                "WHERE run_key=? AND owner_token=?",
                (
                    status,
                    execution_json,
                    _text_digest(execution_json),
                    result_json,
                    _text_digest(result_json) if result_json is not None else "",
                    str(error)[:2000],
                    timestamp,
                    run_key,
                    owner_token,
                ),
            )
            connection.commit()

    @staticmethod
    def _require_owner(
        connection: sqlite3.Connection, run_key: str, owner_token: str
    ) -> None:
        row = connection.execute(
            "SELECT owner_token,lease_expires_at FROM text2sql_checkpoint_runs WHERE run_key=?",
            (run_key,),
        ).fetchone()
        if (
            row is None
            or not owner_token
            or row["owner_token"] != owner_token
            or float(row["lease_expires_at"] or 0) <= time.time()
        ):
            raise Text2SQLCheckpointBusy("checkpoint execution lease is no longer owned")

    def inspect(self, task_id: str) -> Mapping[str, Any]:
        """Return bounded diagnostics without exposing checkpoint state or results."""
        run_key = _run_key(task_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT status,identity_sha256,lease_expires_at,error,created_at,updated_at "
                "FROM text2sql_checkpoint_runs WHERE run_key=?",
                (run_key,),
            ).fetchone()
            count = connection.execute(
                "SELECT COUNT(*) FROM text2sql_runtime_checkpoints WHERE run_key=?",
                (run_key,),
            ).fetchone()[0]
        return {
            **(dict(row) if row else {"status": "missing"}),
            "checkpoint_count": int(count),
        }


class Text2SQLRuntimeCheckpointSession:
    """One leased, identity-checked view used by :class:`AgentRuntime`."""

    def __init__(
        self,
        store: Text2SQLRuntimeCheckpointStore,
        task_id: str,
        run_key: str,
        owner_token: str,
        execution: Mapping[str, Any],
        cached_result: Optional[Mapping[str, Any]],
        lease_seconds: int,
        node_order: tuple[str, ...],
    ) -> None:
        self.store = store
        self.task_id = task_id
        self.run_key = run_key
        self.owner_token = owner_token
        self.execution = dict(execution)
        self.cached_result = dict(cached_result) if cached_result is not None else None
        self.lease_seconds = lease_seconds
        self.node_order = tuple(str(node) for node in node_order)

    def _check_task(self, task_id: str) -> None:
        if _run_key(task_id) != self.run_key:
            raise Text2SQLCheckpointIdentityError(
                "runtime attempted to use a checkpoint session for another task"
            )

    def load_checkpoints(self, task_id: str) -> Dict[str, Dict[str, Any]]:
        self._check_task(task_id)
        checkpoints = self.store._load_checkpoints(self.run_key)
        self._validate_prefix(checkpoints)
        return checkpoints

    def _validate_prefix(self, checkpoints: Mapping[str, Mapping[str, Any]]) -> None:
        if not self.node_order:
            return
        unknown = set(checkpoints).difference(self.node_order)
        if unknown:
            raise Text2SQLCheckpointCorruptionError(
                "checkpoint contains nodes outside the bound runtime graph"
            )
        gap_seen = False
        for node in self.node_order:
            checkpoint = checkpoints.get(node)
            completed = bool(checkpoint and checkpoint.get("status") == "completed")
            if completed and gap_seen:
                raise Text2SQLCheckpointCorruptionError(
                    "completed checkpoints are not a continuous graph prefix"
                )
            if not completed:
                gap_seen = True

    def save_checkpoint(
        self,
        task_id: str,
        node: str,
        state: Dict[str, Any],
        status: str = "completed",
        attempt: int = 1,
        error: str = "",
        execution: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self._check_task(task_id)
        if not self.owner_token:
            raise Text2SQLCheckpointBusy("completed checkpoint run is read-only")
        if status not in {"completed", "failed"}:
            raise ValueError("checkpoint node status must be completed or failed")
        if self.node_order:
            if node not in self.node_order:
                raise Text2SQLCheckpointCorruptionError(
                    "checkpoint node is outside the bound runtime graph"
                )
            current = self.store._load_checkpoints(self.run_key)
            node_index = self.node_order.index(node)
            if any(
                (current.get(previous) or {}).get("status") != "completed"
                for previous in self.node_order[:node_index]
            ):
                raise Text2SQLCheckpointCorruptionError(
                    "checkpoint node would create a non-continuous graph prefix"
                )
        if execution is not None:
            self.execution = dict(execution)
        self.store._save_checkpoint(
            self.run_key,
            self.owner_token,
            node,
            state,
            status,
            attempt,
            error,
            self.execution,
            self.lease_seconds,
        )

    def complete(
        self, result: Mapping[str, Any], execution: Mapping[str, Any]
    ) -> None:
        if self.node_order:
            checkpoints = self.store._load_checkpoints(self.run_key)
            self._validate_prefix(checkpoints)
            incomplete = [
                node
                for node in self.node_order
                if (checkpoints.get(node) or {}).get("status") != "completed"
            ]
            if incomplete:
                raise Text2SQLCheckpointCorruptionError(
                    "checkpoint run cannot complete before every bound runtime "
                    "node is completed: %s" % ", ".join(incomplete)
                )
        self.execution = dict(execution)
        self.store._finish(
            self.run_key,
            self.owner_token,
            "completed",
            self.execution,
            result=result,
        )
        self.owner_token = ""
        self.cached_result = dict(result)

    def fail(self, error: str, execution: Mapping[str, Any]) -> None:
        if not self.owner_token:
            return
        self.execution = dict(execution)
        self.store._finish(
            self.run_key,
            self.owner_token,
            "failed",
            self.execution,
            error=error,
        )
        self.owner_token = ""
