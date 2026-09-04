"""Retriever-only Vanna/Chroma adapter for trusted Text2SQL knowledge.

Vanna's legacy API combines vector retrieval, LLM generation and SQL execution in
one base class.  This module deliberately exposes only retrieval and offline index
construction.  The wrapped backend rejects every LLM or SQL execution entry point.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence


EVIDENCE_MARKER = "EVO_EVIDENCE_ID"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _enabled_from_env() -> bool:
    return os.getenv("EVOAGENT_TEXT2SQL_VANNA_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _marker(evidence_id: str) -> str:
    return "-- %s: %s" % (EVIDENCE_MARKER, evidence_id)


def _extract_marker(value: str) -> str:
    prefix = "%s:" % EVIDENCE_MARKER
    for line in str(value or "").splitlines()[:4]:
        normalized = line.strip().removeprefix("--").strip()
        if normalized.startswith(prefix):
            return normalized[len(prefix) :].strip()
    return ""


def _strip_marker(value: str) -> str:
    return "\n".join(
        line
        for line in str(value or "").splitlines()
        if EVIDENCE_MARKER not in line
    ).strip()


def _table_ddl(item: Mapping[str, Any]) -> str:
    structured = dict(item.get("structured") or {})
    table = str(structured.get("name") or "").strip()
    columns = structured.get("columns") or ()
    if not table or not columns:
        return str(item.get("content") or "")
    definitions = []
    primary_key = {str(value) for value in structured.get("primary_key") or ()}
    for column in columns:
        name = str(column.get("name") or "").strip()
        column_type = str(
            column.get("sqlite_type") or column.get("column_type") or "TEXT"
        ).strip()
        definition = '  "%s" %s' % (name.replace('"', '""'), column_type)
        if not column.get("nullable", True):
            definition += " NOT NULL"
        if len(primary_key) == 1 and name in primary_key:
            definition += " PRIMARY KEY"
        definitions.append(definition)
    if len(primary_key) > 1:
        definitions.append(
            "  PRIMARY KEY (%s)"
            % ", ".join('"%s"' % value.replace('"', '""') for value in sorted(primary_key))
        )
    return 'CREATE TABLE "%s" (\n%s\n);' % (
        table.replace('"', '""'),
        ",\n".join(definitions),
    )


def _load_chroma_backend_class():
    try:
        from vanna.legacy.chromadb.chromadb_vector import ChromaDB_VectorStore
    except ImportError:
        # Vanna 0.x compatibility.  Runtime code remains pinned to 2.x, but the
        # fallback keeps the adapter testable with an existing legacy install.
        from vanna.chromadb import ChromaDB_VectorStore

    class RetrieverBackend(ChromaDB_VectorStore):
        """Chroma retrieval with every generative/execution entry point disabled."""

        @staticmethod
        def _generation_disabled(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("Vanna generation and SQL execution are disabled")

        system_message = _generation_disabled
        user_message = _generation_disabled
        assistant_message = _generation_disabled
        submit_prompt = _generation_disabled
        ask = _generation_disabled
        generate_sql = _generation_disabled
        run_sql = _generation_disabled

    return RetrieverBackend


@dataclass(frozen=True)
class VannaRetrieval:
    evidence_ids: Sequence[str] = field(default_factory=tuple)
    ddl: Sequence[str] = field(default_factory=tuple)
    documentation: Sequence[str] = field(default_factory=tuple)
    question_sql: Sequence[Mapping[str, str]] = field(default_factory=tuple)
    index_version: str = ""
    backend: str = "vanna-chromadb"

    def diagnostics(self) -> Mapping[str, Any]:
        return {
            "backend": self.backend,
            "index_version": self.index_version,
            "ddl_count": len(self.ddl),
            "documentation_count": len(self.documentation),
            "question_sql_count": len(self.question_sql),
            "evidence_ids": list(self.evidence_ids),
        }


class VannaRetrieverOnly:
    """Small capability object; Agent code can retrieve but cannot generate SQL."""

    def __init__(
        self,
        root: Path,
        index_version: str,
        *,
        enabled: Optional[bool] = None,
        backend_factory: Optional[Callable[[Mapping[str, Any]], Any]] = None,
        n_results_ddl: int = 4,
        n_results_documentation: int = 8,
        n_results_sql: int = 4,
    ) -> None:
        self.root = root.resolve()
        self.index_version = str(index_version)
        self.enabled = _enabled_from_env() if enabled is None else bool(enabled)
        self.backend_factory = backend_factory
        self.n_results_ddl = max(1, min(int(n_results_ddl), 20))
        self.n_results_documentation = max(
            1, min(int(n_results_documentation), 30)
        )
        self.n_results_sql = max(1, min(int(n_results_sql), 20))

    @property
    def index_path(self) -> Path:
        return self.root / self.index_version

    @property
    def manifest_path(self) -> Path:
        return self.index_path / "manifest.json"

    @staticmethod
    def dependency_available() -> bool:
        return importlib.util.find_spec("vanna") is not None and importlib.util.find_spec(
            "chromadb"
        ) is not None

    def _backend(self, path: Path):
        config = {
            "path": str(path),
            "n_results_ddl": self.n_results_ddl,
            "n_results_documentation": self.n_results_documentation,
            "n_results_sql": self.n_results_sql,
        }
        if self.backend_factory is not None:
            return self.backend_factory(config)
        backend_class = _load_chroma_backend_class()
        return backend_class(config=config)

    def status(self) -> Mapping[str, Any]:
        manifest: Mapping[str, Any] = {}
        if self.manifest_path.exists():
            try:
                manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                manifest = {}
        dependency = self.backend_factory is not None or self.dependency_available()
        ready = bool(
            self.enabled
            and dependency
            and manifest.get("index_version") == self.index_version
            and manifest.get("state") == "stable"
        )
        return {
            "enabled": self.enabled,
            "dependency_available": dependency,
            "ready": ready,
            "mode": "retriever_only",
            "generation_enabled": False,
            "sql_execution_enabled": False,
            "index_version": self.index_version,
            "item_count": int(manifest.get("item_count") or 0),
            "counts": dict(manifest.get("counts") or {}),
            "source_fingerprint": str(manifest.get("source_fingerprint") or ""),
        }

    def build(
        self,
        stable_items: Sequence[Mapping[str, Any]],
        database_snapshot_id: str,
    ) -> Mapping[str, Any]:
        """Build an immutable stable index from already-reviewed knowledge."""

        if not self.enabled:
            raise RuntimeError("Vanna retrieval is disabled")
        if self.backend_factory is None and not self.dependency_available():
            raise RuntimeError("Vanna/Chroma dependencies are not installed")
        rows = sorted(
            (dict(item) for item in stable_items),
            key=lambda item: str(item.get("evidence_id") or ""),
        )
        source_fingerprint = _fingerprint(
            [
                [
                    item.get("evidence_id"),
                    item.get("content_sha256"),
                    item.get("source_version"),
                ]
                for item in rows
            ]
        )
        current = self.status()
        if current.get("ready") and current.get("source_fingerprint") == source_fingerprint:
            return {**current, "added": {"ddl": 0, "documentation": 0, "sql": 0}}

        self.root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".building-", dir=self.root))
        counts = {"ddl": 0, "documentation": 0, "sql": 0}
        try:
            backend = self._backend(temporary)
            for item in rows:
                evidence_id = str(item.get("evidence_id") or "")
                kind = str(item.get("knowledge_type") or "")
                item_key = str(item.get("item_key") or "")
                if kind == "schema" and item_key.startswith("table:"):
                    backend.add_ddl("%s\n%s" % (_marker(evidence_id), _table_ddl(item)))
                    counts["ddl"] += 1
                elif kind == "verified_example":
                    structured = dict(item.get("structured") or {})
                    question = str(structured.get("question") or item.get("title") or "")
                    sql = str(structured.get("sql") or "")
                    if question and sql:
                        backend.add_question_sql(
                            question=question,
                            sql="%s\n%s" % (_marker(evidence_id), sql),
                        )
                        counts["sql"] += 1
                elif kind != "schema" or item_key.startswith("column:"):
                    document = "%s\n%s\n%s" % (
                        _marker(evidence_id),
                        str(item.get("title") or ""),
                        str(item.get("content") or ""),
                    )
                    backend.add_documentation(document.strip())
                    counts["documentation"] += 1

            manifest = {
                "contract": "evoagent-vanna-retriever-v1",
                "state": "stable",
                "index_version": self.index_version,
                "database_snapshot_id": database_snapshot_id,
                "source_fingerprint": source_fingerprint,
                "item_count": sum(counts.values()),
                "counts": counts,
                "generation_enabled": False,
                "sql_execution_enabled": False,
            }
            (temporary / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            if self.index_path.exists():
                shutil.rmtree(self.index_path)
            temporary.replace(self.index_path)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return {**self.status(), "added": counts}

    def retrieve(self, question: str) -> VannaRetrieval:
        status = self.status()
        if not status.get("ready"):
            return VannaRetrieval(
                index_version=self.index_version,
                backend="knowledge-store-fallback",
            )
        backend = self._backend(self.index_path)
        raw_examples = backend.get_similar_question_sql(question) or []
        raw_ddl = backend.get_related_ddl(question) or []
        raw_docs = backend.get_related_documentation(question) or []
        evidence_ids: list[str] = []

        ddl = []
        for value in raw_ddl:
            rendered = str(value or "")
            evidence_id = _extract_marker(rendered)
            if evidence_id:
                evidence_ids.append(evidence_id)
            cleaned = _strip_marker(rendered)
            if cleaned:
                ddl.append(cleaned)

        documentation = []
        for value in raw_docs:
            rendered = str(value or "")
            evidence_id = _extract_marker(rendered)
            if evidence_id:
                evidence_ids.append(evidence_id)
            cleaned = _strip_marker(rendered)
            if cleaned:
                documentation.append(cleaned)

        examples = []
        for value in raw_examples:
            if not isinstance(value, Mapping):
                continue
            sql = str(value.get("sql") or "")
            evidence_id = _extract_marker(sql)
            if evidence_id:
                evidence_ids.append(evidence_id)
            question_value = str(value.get("question") or "").strip()
            sql_value = _strip_marker(sql)
            if question_value and sql_value:
                examples.append({"question": question_value, "sql": sql_value})

        return VannaRetrieval(
            evidence_ids=tuple(dict.fromkeys(evidence_ids)),
            ddl=tuple(ddl),
            documentation=tuple(documentation),
            question_sql=tuple(examples),
            index_version=self.index_version,
        )
