"""Role-scoped factual tools for the Text2SQL multi-agent runtime."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ..runtime import AgentTool, ToolRegistry
from ..telemetry import ExecutionLedger
from .knowledge_store import KnowledgeStore, ROLE_VIEWS
from .sql_safety import ReadOnlySQLiteExecutor, validate_sql
from .sqlite_database import open_readonly
from .vanna_retriever import VannaRetrieverOnly


# Persistent Chroma clients can race while opening the same local index. The
# evaluation runner shares one engine across worker threads, so serialize this
# short retrieval section while leaving all remote LLM work concurrent.
_VANNA_RETRIEVAL_LOCK = threading.Lock()


ROLE_TOOL_PERMISSIONS = {
    "text2sql-lead": {
        "retrieve_knowledge",
        "inspect_schema",
        "sample_values",
        "validate_sql",
        "explain_sql",
        "execute_sql",
    },
    "schema-grounding": {"retrieve_knowledge", "inspect_schema", "sample_values"},
    "sql-strategy": {
        "retrieve_knowledge",
        "inspect_schema",
        "sample_values",
        "validate_sql",
        "explain_sql",
    },
    "text2sql-critic": {
        "retrieve_knowledge",
        "inspect_schema",
        "validate_sql",
        "explain_sql",
    },
}


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "type": "bytes",
            "length": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _quote_identifier(value: str) -> str:
    return '"%s"' % value.replace('"', '""')


class Text2SQLToolSuite:
    def __init__(
        self,
        *,
        database_path: Path,
        snapshot: Mapping[str, Any],
        knowledge_store_path: Path,
        vanna_index_root: Optional[Path] = None,
        vanna_index_version: str = "",
        principals: Sequence[str],
        memory_snapshot_id: str,
        policy_version: str,
        ledger: Optional[ExecutionLedger] = None,
        max_rows: int = 200,
        timeout_ms: int = 3000,
    ) -> None:
        self.database_path = database_path.resolve()
        self.snapshot = snapshot
        self.knowledge_store_path = knowledge_store_path.resolve()
        self.vanna_index_root = vanna_index_root.resolve() if vanna_index_root else None
        self.vanna_index_version = str(vanna_index_version or "")
        self.principals = tuple(principals)
        self.memory_snapshot_id = memory_snapshot_id
        self.policy_version = policy_version
        self.ledger = ledger
        self.executor = ReadOnlySQLiteExecutor(
            self.database_path, snapshot, max_rows=max_rows, timeout_ms=timeout_ms
        )
        self.tables = {table["name"]: table for table in snapshot["tables"]}
        with KnowledgeStore(self.knowledge_store_path) as store:
            if store.database_snapshot_id() != snapshot["snapshot_id"]:
                raise ValueError("knowledge store and database snapshot do not match")

    def _result(self, tool: str, arguments: Mapping[str, Any], output: Any) -> Mapping[str, Any]:
        safe_output = _json_value(output)
        rendered = json.dumps(
            [tool, arguments, self.snapshot["snapshot_id"], safe_output],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return {
            "evidence_id": "text2sql-tool:%s" % hashlib.sha256(
                rendered.encode("utf-8")
            ).hexdigest()[:20],
            "tool": tool,
            "arguments": _json_value(dict(arguments)),
            "database_snapshot_id": self.snapshot["snapshot_id"],
            "output": safe_output,
        }

    def _recorded(self, role: str, name: str, handler):
        def call(**arguments):
            started = time.monotonic()
            try:
                output = handler(**arguments)
                result = self._result(name, arguments, output)
                if self.ledger:
                    self.ledger.record_tool(
                        role,
                        name,
                        arguments,
                        True,
                        int((time.monotonic() - started) * 1000),
                        result,
                    )
                return result
            except Exception as exc:
                if self.ledger:
                    self.ledger.record_tool(
                        role,
                        name,
                        arguments,
                        False,
                        int((time.monotonic() - started) * 1000),
                        error=str(exc),
                    )
                raise

        return call

    def _retrieve(self, role_view: str, query: str, limit: int = 0) -> Mapping[str, Any]:
        with KnowledgeStore(self.knowledge_store_path) as store:
            pack = store.retrieve(
                query,
                role_view,
                self.principals,
                self.memory_snapshot_id,
                self.policy_version,
                limit=limit or None,
            )
            target_limit = limit or int(ROLE_VIEWS[role_view]["limit"])
            vanna = None
            semantic = ()
            if self.vanna_index_root and self.vanna_index_version:
                with _VANNA_RETRIEVAL_LOCK:
                    retriever = VannaRetrieverOnly(
                        self.vanna_index_root,
                        self.vanna_index_version,
                    )
                    vanna = retriever.retrieve(query)
                # Vanna is a semantic recall signal, not the authority scorer.
                # Keep its reciprocal-rank bonus deliberately below a strong
                # lexical/value match so a loose vector hit cannot displace an
                # exact database fact.
                semantic_scores = {
                    evidence_id: round(5.0 / (index + 1), 6)
                    for index, evidence_id in enumerate(vanna.evidence_ids)
                }
                semantic = store.resolve_stable_evidence(
                    vanna.evidence_ids,
                    self.principals,
                    semantic_scores,
                )

        # Merge vector hits with deterministic lexical/graph retrieval.  The
        # relational store has already re-authorized every semantic hit.
        weighted: dict[str, Mapping[str, Any]] = {
            item.evidence_id: item.as_dict() for item in pack.evidence
        }
        type_weights = ROLE_VIEWS[role_view]["weights"]
        for item in semantic:
            if float(type_weights.get(item.knowledge_type, 0.0)) <= 0:
                continue
            value = item.as_dict()
            value["score"] = round(
                float(value["score"]) * float(type_weights[item.knowledge_type]), 6
            )
            current = weighted.get(item.evidence_id)
            if current is None:
                weighted[item.evidence_id] = value
            else:
                combined = dict(current)
                combined["score"] = round(
                    float(current.get("score") or 0) + float(value["score"]), 6
                )
                weighted[item.evidence_id] = combined
        selected = sorted(
            weighted.values(),
            key=lambda item: (-float(item.get("score") or 0), str(item["evidence_id"])),
        )[:target_limit]
        result = dict(pack.as_dict())
        result["evidence"] = selected
        result["retrieval"] = (
            vanna.diagnostics()
            if vanna is not None
            else {
                "backend": "knowledge-store-only",
                "index_version": self.vanna_index_version,
                "ddl_count": 0,
                "documentation_count": 0,
                "question_sql_count": 0,
                "evidence_ids": [],
            }
        )
        return result

    def _inspect_schema(self, table: str) -> Mapping[str, Any]:
        if table not in self.tables:
            raise ValueError("unknown table: %s" % table)
        return self.tables[table]

    def _sample_values(self, table: str, column: str, limit: int = 20) -> Mapping[str, Any]:
        table_schema = self.tables.get(table)
        if not table_schema:
            raise ValueError("unknown table: %s" % table)
        columns = {item["name"] for item in table_schema["columns"]}
        if column not in columns:
            raise ValueError("unknown column: %s.%s" % (table, column))
        bounded = max(1, min(int(limit), 50))
        sql = "SELECT %s, COUNT(*) AS value_count FROM %s GROUP BY %s ORDER BY value_count DESC LIMIT ?" % (
            _quote_identifier(column),
            _quote_identifier(table),
            _quote_identifier(column),
        )
        connection = open_readonly(self.database_path)
        try:
            rows = connection.execute(sql, (bounded,)).fetchall()
        finally:
            connection.close()
        return {
            "table": table,
            "column": column,
            "values": [
                {"value": _json_value(row[0]), "count": int(row[1])} for row in rows
            ],
        }

    def _validate_sql(self, sql: str) -> Mapping[str, Any]:
        return validate_sql(sql, self.snapshot).as_dict()

    def _explain_sql(self, sql: str) -> Mapping[str, Any]:
        return {"plan": self.executor.explain(sql)}

    def _execute_sql(self, sql: str) -> Mapping[str, Any]:
        return self.executor.execute(sql).as_dict()

    def registry(
        self, role: str, allowed_tools: Optional[Sequence[str]] = None
    ) -> ToolRegistry:
        permissions = ROLE_TOOL_PERMISSIONS.get(role)
        if permissions is None:
            raise ValueError("unsupported Text2SQL role: %s" % role)
        if allowed_tools is not None:
            requested = {str(name) for name in allowed_tools}
            expanded = requested.difference(permissions)
            if expanded:
                raise ValueError(
                    "runtime policy cannot expand role permissions: %s"
                    % ", ".join(sorted(expanded))
                )
            permissions = permissions.intersection(requested)
        role_view = {
            "text2sql-lead": "lead",
            "schema-grounding": "schema-grounding",
            "sql-strategy": "sql-strategy",
            "text2sql-critic": "critic",
        }[role]
        specs = {
            "retrieve_knowledge": AgentTool(
                "retrieve_knowledge",
                "Retrieve ACL-filtered stable evidence for this role and pinned snapshots.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 0, "maximum": 50},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                self._recorded(
                    role,
                    "retrieve_knowledge",
                    lambda query, limit=0: self._retrieve(role_view, query, limit),
                ),
            ),
            "inspect_schema": AgentTool(
                "inspect_schema",
                "Inspect one table from the pinned database schema snapshot.",
                {
                    "type": "object",
                    "properties": {"table": {"type": "string"}},
                    "required": ["table"],
                    "additionalProperties": False,
                },
                self._recorded(role, "inspect_schema", self._inspect_schema),
            ),
            "sample_values": AgentTool(
                "sample_values",
                "Read a bounded value-frequency sample from one whitelisted table column.",
                {
                    "type": "object",
                    "properties": {
                        "table": {"type": "string"},
                        "column": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "required": ["table", "column"],
                    "additionalProperties": False,
                },
                self._recorded(role, "sample_values", self._sample_values),
            ),
            "validate_sql": AgentTool(
                "validate_sql",
                "Parse SQL and apply the deterministic read-only/schema allowlist gate.",
                {
                    "type": "object",
                    "properties": {"sql": {"type": "string"}},
                    "required": ["sql"],
                    "additionalProperties": False,
                },
                self._recorded(role, "validate_sql", self._validate_sql),
            ),
            "explain_sql": AgentTool(
                "explain_sql",
                "Run EXPLAIN QUERY PLAN only after the SQL safety gate accepts the query.",
                {
                    "type": "object",
                    "properties": {"sql": {"type": "string"}},
                    "required": ["sql"],
                    "additionalProperties": False,
                },
                self._recorded(role, "explain_sql", self._explain_sql),
            ),
            "execute_sql": AgentTool(
                "execute_sql",
                "Execute accepted SQL with immutable/query-only SQLite, timeout and row limits.",
                {
                    "type": "object",
                    "properties": {"sql": {"type": "string"}},
                    "required": ["sql"],
                    "additionalProperties": False,
                },
                self._recorded(role, "execute_sql", self._execute_sql),
            ),
        }
        return ToolRegistry(specs[name] for name in sorted(permissions))
