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
from .sql_safety import ReadOnlySQLiteExecutor, validate_sql
from .sqlite_database import open_readonly
from .vanna_corpus import ROLE_VIEWS, VannaCorpus
from .vanna_retriever import VannaRetrieval


# Persistent Chroma clients can race while opening the same local index. The
# evaluation runner shares one engine across worker threads, so serialize this
# short retrieval section while leaving all remote LLM work concurrent.
_VANNA_RETRIEVAL_LOCK = threading.Lock()


ROLE_TOOL_PERMISSIONS = {
    "text2sql-lead": {
        "retrieve_knowledge",
        "inspect_schema",
        "sample_values",
    },
    "schema-grounding": {"retrieve_knowledge", "inspect_schema", "sample_values"},
    # These reasoning roles receive immutable, stage-filtered context. Keeping
    # their maximum ACL empty makes the plan-first separation true even if a
    # caller forgets to apply a stricter stage override.
    "query-planning": set(),
    "sql-generation": set(),
    "text2sql-critic": set(),
    # This is an application-owned principal, not an evolvable model role.  It
    # is the only principal allowed to execute SQL, and the runtime invokes it
    # only after the deterministic final gates have accepted a candidate.
    "text2sql-harness": {"validate_sql", "explain_sql", "execute_sql"},
}


# Old traces and checkpoints may still name the former combined worker.  Keep
# the alias at the runtime boundary only; policy artifacts use the five
# canonical roles and never expose a sixth, evolvable ``sql-strategy`` slot.
LEGACY_RUNTIME_ROLE_ALIASES = {"sql-strategy": "query-planning"}


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
        if not self.vanna_index_root or not self.vanna_index_version:
            raise ValueError("Vanna corpus is not built; run scripts/build_text2sql_vanna.py")
        self.vanna_corpus = VannaCorpus(self.vanna_index_root, self.vanna_index_version)
        if self.vanna_corpus.database_snapshot_id != snapshot["snapshot_id"]:
            raise ValueError("Vanna corpus and database snapshot do not match")
        if not self.vanna_corpus.retriever.corpus_items():
            raise ValueError("Vanna corpus is empty; rebuild the pinned index")

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
        with _VANNA_RETRIEVAL_LOCK:
            pack, diagnostics = self.vanna_corpus.retrieve(
                query, role_view, self.memory_snapshot_id, self.policy_version,
                limit=limit or None,
            )
        return {**dict(pack.as_dict()), "retrieval": dict(diagnostics)}

    def retrieve_for_orchestration(
        self, role_view: str, query: str, limit: int = 0
    ) -> Mapping[str, Any]:
        """Run one Harness-owned, role-specific evidence retrieval."""

        if role_view not in {"schema-grounding", "query-planning"}:
            raise ValueError("unsupported orchestration role view: %s" % role_view)
        call = self._recorded(
            "text2sql-evidence-orchestrator",
            "retrieve_knowledge",
            lambda role_view, query, limit=0: self._retrieve(
                role_view, query, limit
            ),
        )
        return call(role_view=role_view, query=query, limit=limit)

    def retrieve_vanna_draft_context(
        self, query: str
    ) -> tuple[VannaRetrieval, Mapping[str, Any]]:
        """Freeze one Vanna DDL/document/example context for Node 2.

        This is a Harness-owned operation rather than an Agent tool.  The raw
        bodies are returned only to the request-scoped draft generator; the
        persisted tool record contains bounded diagnostics and evidence ids,
        never the retrieved Question-SQL bodies.
        """

        arguments = {
            "query": str(query),
            "include_ddl": True,
            "include_documentation": True,
            "include_question_sql": True,
        }
        started = time.monotonic()
        try:
            with _VANNA_RETRIEVAL_LOCK:
                retrieval = self.vanna_corpus.retriever.retrieve(
                    str(query),
                    include_ddl=True,
                    include_documentation=True,
                    include_question_sql=True,
                )
            output = {
                "contract": "VannaDraftContext/v1",
                **dict(retrieval.diagnostics()),
            }
            result = self._result("retrieve_vanna_draft_context", arguments, output)
            if self.ledger:
                self.ledger.record_tool(
                    "text2sql-evidence-orchestrator",
                    "retrieve_vanna_draft_context",
                    arguments,
                    True,
                    int((time.monotonic() - started) * 1000),
                    result,
                )
            return retrieval, result
        except Exception as exc:
            if self.ledger:
                self.ledger.record_tool(
                    "text2sql-evidence-orchestrator",
                    "retrieve_vanna_draft_context",
                    arguments,
                    False,
                    int((time.monotonic() - started) * 1000),
                    error=str(exc),
                )
            raise

    def retrieve_verified_examples(
        self, query: str, limit: int = 8
    ) -> Mapping[str, Any]:
        """Retrieve user-confirmed Question-SQL after semantic plan approval.

        This is a Harness capability, not a registered Agent tool.  In the
        single-user path the Question-SQL body and its trace metadata are both
        read from the pinned Vanna corpus.
        """

        bounded = max(1, min(int(limit), 20))
        with _VANNA_RETRIEVAL_LOCK:
            examples, diagnostics = self.vanna_corpus.retrieve_verified_examples(
                query, bounded
            )
        if self.ledger:
            self.ledger.trace(
                "text2sql-sql-generation",
                "verified_examples_retrieved",
                candidate_count=len(examples),
                evidence_ids=[item["evidence_id"] for item in examples],
                vanna_ready=True,
            )
        return {
            "contract": "VerifiedExampleCandidates/v1",
            "database_snapshot_id": self.snapshot["snapshot_id"],
            "knowledge_index_version": self.vanna_index_version,
            "vanna_index_version": self.vanna_index_version,
            "examples": [dict(item) for item in examples],
            "retrieval": dict(diagnostics),
        }

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
        canonical_role = LEGACY_RUNTIME_ROLE_ALIASES.get(role, role)
        permissions = ROLE_TOOL_PERMISSIONS.get(canonical_role)
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
            "query-planning": "query-planning",
            "sql-generation": "query-planning",
            "text2sql-critic": "critic",
            "text2sql-harness": "lead",
        }[canonical_role]
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
