"""Single-user Text2SQL corpus backed directly by Vanna/Chroma.

The local application has one trusted owner, so factual retrieval does not need a
second relational approval database.  This module materializes the three sources
that are useful at query time into one versioned Vanna index:

* the pinned database schema and observed low-cardinality values;
* repository-owned business Markdown documents;
* Question-SQL pairs explicitly confirmed by the user.

Role-scoped Agent semantic memory remains in ``Text2SQLEvolutionStore`` and is not
part of this corpus.  Inferred Join candidates are also excluded unless the Join
Catalog marks them approved.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .business_documents import BusinessDocument, parse_markdown, validate_and_chunk_page
from .models import Evidence, EvidencePack
from .vanna_retriever import VannaRetrieverOnly


ROLE_VIEWS: Mapping[str, Mapping[str, Any]] = {
    "lead": {
        "limit": 12,
        "weights": {
            "business_glossary": 1.4,
            "relationship": 1.2,
            "schema": 1.0,
            "value": 0.9,
            "verified_example": 0.0,
        },
    },
    "schema-grounding": {
        "limit": 24,
        "weights": {
            "schema": 1.5,
            "value": 1.4,
            "relationship": 1.5,
            "business_glossary": 1.0,
            "verified_example": 0.0,
        },
    },
    "query-planning": {
        "limit": 12,
        "weights": {
            "schema": 0.0,
            "value": 0.0,
            "relationship": 0.0,
            "business_glossary": 1.5,
            "verified_example": 0.0,
        },
    },
    "sql-strategy": {
        "limit": 16,
        "weights": {
            "schema": 1.1,
            "value": 1.0,
            "relationship": 1.4,
            "business_glossary": 1.3,
            "verified_example": 0.0,
        },
    },
    "critic": {
        "limit": 18,
        "weights": {
            "schema": 1.3,
            "value": 1.2,
            "relationship": 1.5,
            "business_glossary": 1.3,
            "verified_example": 0.0,
        },
    },
}


# These two dump artifacts are intentionally absent from retrieval.  The first is
# malformed and the second is an account table rather than business data.  They
# remain in the immutable snapshot so deterministic SQL validation can describe
# the real database, but the model is never encouraged to select them.
DEFAULT_EXCLUDED_TABLES = frozenset({"gale_casefile", "t_operaator"})
QUESTION_SQL_REGISTRY = "confirmed_question_sql.json"
_QUESTION_SQL_WRITE_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _tokens(text: str) -> set[str]:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text).lower().replace("_", " ")
    result: set[str] = set()
    for token in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", normalized):
        result.add(token)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            for width in (2, 3):
                result.update(
                    token[index : index + width]
                    for index in range(len(token) - width + 1)
                )
    return {token for token in result if token}


def _expanded_query(query: str) -> str:
    aliases = {
        "案例": ("案件", "case"),
        "案件": ("案例", "case"),
        "多少": ("数量", "计数", "count"),
        "几个": ("数量", "计数", "count"),
        "数量": ("多少", "计数", "count"),
    }
    additions = [alias for term, values in aliases.items() if term in query for alias in values]
    return " ".join((query, *additions))


def _is_value_knowledge(column: Mapping[str, Any], table_row_count: int) -> bool:
    profile = dict(column.get("profile") or {})
    values = profile.get("low_cardinality_values")
    if not values or int(profile.get("max_length") or 0) > 64:
        return False
    semantic_name = "%s %s" % (column.get("name", ""), column.get("comment", ""))
    if re.search(r"remark|path|process|description|详情|备注|路径|过程", semantic_name, re.I):
        return False
    non_null = table_row_count - int(profile.get("null_count") or 0)
    if non_null <= 0:
        return False
    return int(profile.get("distinct_count") or 0) / non_null <= 0.6


def _schema_items(
    snapshot: Mapping[str, Any], excluded_tables: Sequence[str],
    annotations: Optional[Mapping[str, Any]] = None,
) -> list[Mapping[str, Any]]:
    snapshot_id = str(snapshot["snapshot_id"])
    excluded = {str(value) for value in excluded_tables}
    items: list[Mapping[str, Any]] = []
    annotations = dict(annotations or {})
    source_version = snapshot_id
    if annotations:
        source_version += ":annotations:" + _hash(annotations)[:24]
    for table in snapshot.get("tables") or ():
        table_name = str(table["name"])
        if table_name in excluded:
            continue
        # The immutable snapshot keeps the original comments. Only the search
        # view uses reviewed explanations and labels inferences, with provenance.
        columns = []
        for original in table.get("columns") or ():
            column = dict(original)
            annotation = annotations.get("%s.%s" % (table_name, column["name"]))
            if annotation:
                comment_status = annotation["status"]
                comment = annotation.get("comment") or "注释待确认（原始注释已隔离）"
                if comment_status == "inferred":
                    comment = "推断解释：" + comment
                column.update(raw_comment=column.get("comment") or "",
                              comment=comment,
                              comment_status=comment_status, comment_issue=dict(annotation))
            columns.append(column)
        table = {**table, "columns": columns}
        column_summary = "; ".join(
            "%s（%s，%s）"
            % (
                column["name"],
                column["column_type"],
                column.get("comment") or "无注释",
            )
            for column in table.get("columns") or ()
        )
        content = "表 %s。表说明：%s。主键：%s。字段：%s" % (
            table_name,
            table.get("comment") or "无",
            ", ".join(table.get("primary_key") or ()) or "无声明主键",
            column_summary,
        )
        evidence_id = "db:" + _hash([snapshot_id, "table", table_name])[:24]
        items.append(
            {
                "evidence_id": evidence_id,
                "source_kind": "database",
                "knowledge_type": "schema",
                "item_key": "table:%s" % table_name,
                "title": "表 %s" % table_name,
                "content": content,
                "structured": dict(table),
                "database_snapshot_id": snapshot_id,
                "source_version": source_version,
                "source_url": "",
                "dependencies": [table_name],
                "content_sha256": _content_hash(content),
            }
        )
        for column in table.get("columns") or ():
            qualified = "%s.%s" % (table_name, column["name"])
            column_content = "字段 %s。MySQL 类型：%s；是否可空：%s；字段注释：%s。" % (
                qualified,
                column["column_type"],
                "是" if column.get("nullable") else "否",
                column.get("comment") or "无",
            )
            column_id = "db:" + _hash([snapshot_id, "column", qualified])[:24]
            items.append(
                {
                    "evidence_id": column_id,
                    "source_kind": "database",
                    "knowledge_type": "schema",
                    "item_key": "column:%s" % qualified,
                    "title": "字段 %s" % qualified,
                    "content": column_content,
                    "structured": dict(column),
                    "database_snapshot_id": snapshot_id,
                    "source_version": source_version,
                    "source_url": "",
                    "dependencies": [table_name, qualified],
                    "content_sha256": _content_hash(column_content),
                }
            )
            values = (column.get("profile") or {}).get("low_cardinality_values") or ()
            if not _is_value_knowledge(column, int(table.get("row_count") or 0)):
                continue
            value_content = "字段 %s 的当前快照非 NULL 观测值（JSON 数组）：%s。NULL 行数：%d。" % (
                qualified,
                json.dumps(list(values), ensure_ascii=False),
                int((column.get("profile") or {}).get("null_count") or 0),
            )
            value_id = "db:" + _hash([snapshot_id, "value", qualified, values])[:24]
            items.append(
                {
                    "evidence_id": value_id,
                    "source_kind": "database",
                    "knowledge_type": "value",
                    "item_key": "value:%s" % qualified,
                    "title": "字段值域 %s" % qualified,
                    "content": value_content,
                    "structured": {"column": qualified, "values": list(values)},
                    "database_snapshot_id": snapshot_id,
                    "source_version": source_version,
                    "source_url": "",
                    "dependencies": [table_name, qualified],
                    "content_sha256": _content_hash(value_content),
                }
            )
    return items


def _business_document_items(
    root: Path, snapshot: Mapping[str, Any]
) -> tuple[list[Mapping[str, Any]], list[str]]:
    if not root.exists():
        return [], []
    snapshot_id = str(snapshot["snapshot_id"])
    items: list[Mapping[str, Any]] = []
    skipped: list[str] = []
    page_ids: set[str] = set()
    for path in sorted(root.rglob("*.md")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        metadata, content = parse_markdown(path)
        relative = path.relative_to(root).as_posix()
        page_id = str(metadata.get("page_id") or "doc-" + _hash(relative)[:20])
        if page_id in page_ids:
            raise ValueError("duplicate business document page_id: %s" % page_id)
        page_ids.add(page_id)
        page_version = _content_hash(path.read_text(encoding="utf-8-sig"))
        page = BusinessDocument(
            title=str(metadata.get("title") or path.stem),
            content=content,
            metadata={**metadata, "relative_path": relative},
        )
        validation = validate_and_chunk_page(
            page,
            snapshot,
        )
        if validation.errors:
            raise ValueError(
                "invalid business document %s: %s"
                % (relative, ", ".join(validation.errors))
            )
        # A document that explicitly calls itself a candidate is useful for
        # offline review, but must not silently grant Join authority at runtime.
        if str(metadata.get("business_kind") or "").endswith("candidate"):
            skipped.append(relative)
            continue
        for index, chunk in enumerate(validation.chunks):
            evidence_id = "doc:" + _hash(
                [page_id, page_version, chunk.content_sha256, index]
            )[:24]
            items.append(
                {
                    "evidence_id": evidence_id,
                    "source_kind": "business_document",
                    "knowledge_type": chunk.knowledge_type,
                    "item_key": "document:%s:%d" % (page_id, index),
                    "title": chunk.title,
                    "content": chunk.content,
                    "planning_content": chunk.planning_content,
                    "knowledge_status": chunk.knowledge_status,
                    "structured": {
                        "page_id": page_id,
                        "chunk_index": index,
                        "business_kind": str(metadata.get("business_kind") or ""),
                    },
                    "database_snapshot_id": snapshot_id,
                    "source_version": page_version,
                    "source_url": relative,
                    "dependencies": list(chunk.dependencies),
                    "content_sha256": chunk.content_sha256,
                }
            )
    return items, skipped


def _approved_join_items(
    join_catalog: Optional[Mapping[str, Any]], snapshot: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    if not join_catalog:
        return []
    snapshot_id = str(snapshot["snapshot_id"])
    items: list[Mapping[str, Any]] = []
    for relationship in join_catalog.get("relationships") or ():
        if relationship.get("decision") != "approved":
            continue
        left = str(relationship["left"])
        right = str(relationship["right"])
        candidate_id = str(relationship["candidate_id"])
        content = "Join 关系：%s = %s。基数：%s；结果粒度：%s；fanout 风险：%s。" % (
            left,
            right,
            relationship.get("cardinality") or "unknown",
            relationship.get("result_grain") or "unknown",
            relationship.get("fanout_risk") or "unknown",
        )
        if relationship.get("notes"):
            content += " 使用依据与限制：" + str(relationship["notes"])
        items.append(
            {
                "evidence_id": "join:" + candidate_id,
                "source_kind": "join_catalog",
                "knowledge_type": "relationship",
                "item_key": "relationship:%s" % candidate_id,
                "title": "%s ↔ %s" % (left, right),
                "content": content,
                "structured": dict(relationship),
                "database_snapshot_id": snapshot_id,
                "source_version": snapshot_id + ":relationship:" + _hash(relationship)[:24],
                "source_url": "",
                "dependencies": [
                    left.split(".", 1)[0],
                    left,
                    right.split(".", 1)[0],
                    right,
                ],
                "content_sha256": _content_hash(content),
            }
        )
    return items


def question_sql_registry_path(vanna_root: Path) -> Path:
    return vanna_root.resolve() / QUESTION_SQL_REGISTRY


def load_confirmed_question_sql(
    path: Path, database_snapshot_id: str
) -> list[Mapping[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid confirmed Question-SQL registry: %s" % path) from exc
    rows = payload.get("items") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("confirmed Question-SQL registry must contain an items list")
    return [
        dict(item)
        for item in rows
        if isinstance(item, Mapping)
        and str(item.get("database_snapshot_id") or "") == database_snapshot_id
    ]


def _add_confirmed_question_sql(
    path: Path,
    *,
    database_snapshot_id: str,
    question: str,
    sql: str,
    actor: str,
    source_id: str,
    dependencies: Sequence[str] = (),
) -> Mapping[str, Any]:
    """Persist one user-confirmed pair as Vanna training input, idempotently."""

    question = question.strip()
    sql = sql.strip()
    actor = actor.strip() or "local-user"
    source_id = source_id.strip()
    if not question or not sql or not database_snapshot_id or not source_id:
        raise ValueError("confirmed Question-SQL requires question, SQL, snapshot and source")
    existing: list[Mapping[str, Any]] = []
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping) or not isinstance(payload.get("items"), list):
            raise ValueError("invalid confirmed Question-SQL registry")
        existing = [dict(item) for item in payload["items"] if isinstance(item, Mapping)]
    evidence_id = "example:" + _hash([database_snapshot_id, question, sql])[:24]
    for item in existing:
        if str(item.get("evidence_id") or "") == evidence_id:
            return dict(item)
    confirmed_at = _now()
    item = {
        "evidence_id": evidence_id,
        "database_snapshot_id": database_snapshot_id,
        "question": question,
        "sql": sql,
        "confirmed_by": actor,
        "confirmed_at": confirmed_at,
        "source_id": source_id,
        "dependencies": list(dict.fromkeys(str(value) for value in dependencies if str(value))),
    }
    rows = sorted([*existing, item], key=lambda value: str(value.get("evidence_id") or ""))
    payload = {
        "contract": "evoagent-confirmed-question-sql-v1",
        "updated_at": confirmed_at,
        "items": rows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=".question-sql-",
        suffix=".json",
        dir=path.parent,
        delete=False,
    ) as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        temporary = Path(handle.name)
    temporary.replace(path)
    return item


def add_confirmed_question_sql(
    path: Path,
    *,
    database_snapshot_id: str,
    question: str,
    sql: str,
    actor: str,
    source_id: str,
    dependencies: Sequence[str] = (),
) -> Mapping[str, Any]:
    """Thread-safe public wrapper for the local Question-SQL registry."""

    with _QUESTION_SQL_WRITE_LOCK:
        return _add_confirmed_question_sql(
            path,
            database_snapshot_id=database_snapshot_id,
            question=question,
            sql=sql,
            actor=actor,
            source_id=source_id,
            dependencies=dependencies,
        )


def remove_confirmed_question_sql(
    path: Path,
    *,
    evidence_id: str = "",
    source_id: str = "",
) -> Mapping[str, Any]:
    """Remove one mistaken confirmation; callers must rebuild Vanna afterwards."""

    evidence_id = evidence_id.strip()
    source_id = source_id.strip()
    if not evidence_id and not source_id:
        raise ValueError("evidence_id or source_id is required")
    with _QUESTION_SQL_WRITE_LOCK:
        if not path.exists():
            return {"removed": 0, "evidence_ids": []}
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("items") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            raise ValueError("invalid confirmed Question-SQL registry")
        kept = []
        removed = []
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            matched = (
                bool(evidence_id)
                and str(item.get("evidence_id") or "") == evidence_id
            ) or (
                bool(source_id) and str(item.get("source_id") or "") == source_id
            )
            (removed if matched else kept).append(item)
        if not removed:
            return {"removed": 0, "evidence_ids": []}
        updated = {
            "contract": "evoagent-confirmed-question-sql-v1",
            "updated_at": _now(),
            "items": sorted(
                kept, key=lambda value: str(value.get("evidence_id") or "")
            ),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".question-sql-",
            suffix=".json",
            dir=path.parent,
            delete=False,
        ) as handle:
            handle.write(json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
            temporary = Path(handle.name)
        temporary.replace(path)
        return {
            "removed": len(removed),
            "evidence_ids": [str(item.get("evidence_id") or "") for item in removed],
        }


def _question_sql_items(
    rows: Sequence[Mapping[str, Any]], snapshot: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    snapshot_id = str(snapshot["snapshot_id"])
    items: list[Mapping[str, Any]] = []
    for row in rows:
        question = str(row.get("question") or "").strip()
        sql = str(row.get("sql") or "").strip()
        if not question or not sql:
            continue
        evidence_id = str(row.get("evidence_id") or "") or (
            "example:" + _hash([snapshot_id, question, sql])[:24]
        )
        content = "用户确认问题：%s\n正确 SQL：%s" % (question, sql)
        items.append(
            {
                "evidence_id": evidence_id,
                "source_kind": "user_confirmed",
                "knowledge_type": "verified_example",
                "item_key": "question-sql:%s" % evidence_id,
                "title": question[:300],
                "content": content,
                "structured": {"question": question, "sql": sql},
                "database_snapshot_id": snapshot_id,
                "source_version": str(row.get("confirmed_at") or row.get("source_id") or ""),
                "source_url": "",
                "dependencies": list(row.get("dependencies") or ()),
                "content_sha256": _content_hash(content),
            }
        )
    return items


def collect_vanna_corpus(
    snapshot: Mapping[str, Any],
    *,
    business_root: Path,
    join_catalog: Optional[Mapping[str, Any]] = None,
    question_sql_path: Optional[Path] = None,
    excluded_tables: Sequence[str] = tuple(DEFAULT_EXCLUDED_TABLES),
) -> Mapping[str, Any]:
    """Collect trusted local inputs; invalid documents fail the build."""

    snapshot_id = str(snapshot["snapshot_id"])
    annotation_path = business_root.parent / "schema_annotations.json"
    annotations = {}
    if annotation_path.exists():
        payload = json.loads(annotation_path.read_text(encoding="utf-8"))
        if (payload.get("contract") != "evoagent-schema-annotations-v1"
                or payload.get("database_snapshot_id") != snapshot_id):
            raise ValueError("schema annotations contract or snapshot mismatch")
        annotations = payload.get("columns")
        known_columns = {"%s.%s" % (t["name"], c["name"])
                         for t in snapshot.get("tables") or () for c in t.get("columns") or ()}
        if not isinstance(annotations, dict):
            raise ValueError("schema annotations columns must be a mapping")
        for name, annotation in annotations.items():
            if (name not in known_columns or not isinstance(annotation, dict)
                    or annotation.get("status") not in {"unreliable", "corrected", "inferred"}
                    or not str(annotation.get("reason") or "").strip()):
                raise ValueError("invalid schema annotation: %s" % name)
            if annotation["status"] in {"corrected", "inferred"}:
                if (not isinstance(annotation.get("comment"), str)
                        or not annotation["comment"].strip()
                        or not str(annotation.get("evidence") or "").strip()):
                    raise ValueError("schema interpretation requires comment and evidence: %s" % name)
    documents, skipped_documents = _business_document_items(business_root, snapshot)
    question_sql = load_confirmed_question_sql(question_sql_path, snapshot_id) if question_sql_path else []
    items = [
        *_schema_items(snapshot, excluded_tables, annotations),
        *documents,
        *_approved_join_items(join_catalog, snapshot),
        *_question_sql_items(question_sql, snapshot),
    ]
    items = sorted(items, key=lambda item: str(item["evidence_id"]))
    identifiers = [str(item["evidence_id"]) for item in items]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Vanna corpus contains duplicate evidence ids")
    fingerprint = _hash(
        [
            [item["evidence_id"], item["content_sha256"], item["source_version"]]
            for item in items
        ]
    )
    counts: dict[str, int] = {}
    for item in items:
        kind = str(item["knowledge_type"])
        counts[kind] = counts.get(kind, 0) + 1
    return {
        "database_snapshot_id": snapshot_id,
        "fingerprint": fingerprint,
        "index_version": "vanna-%s" % fingerprint[:12],
        "items": items,
        "counts": counts,
        "business_document_count": len(documents),
        "skipped_documents": skipped_documents,
        "excluded_tables": sorted(set(str(value) for value in excluded_tables)),
    }


def build_vanna_corpus(
    root: Path,
    snapshot: Mapping[str, Any],
    *,
    business_root: Path,
    join_catalog: Optional[Mapping[str, Any]] = None,
    question_sql_path: Optional[Path] = None,
    excluded_tables: Sequence[str] = tuple(DEFAULT_EXCLUDED_TABLES),
    enabled: Optional[bool] = None,
) -> Mapping[str, Any]:
    corpus = collect_vanna_corpus(
        snapshot,
        business_root=business_root,
        join_catalog=join_catalog,
        question_sql_path=question_sql_path or question_sql_registry_path(root),
        excluded_tables=excluded_tables,
    )
    result = VannaRetrieverOnly(
        root,
        str(corpus["index_version"]),
        enabled=enabled,
    ).build(corpus["items"], str(corpus["database_snapshot_id"]))
    return {
        **dict(result),
        "corpus_counts": dict(corpus["counts"]),
        "business_document_count": int(corpus["business_document_count"]),
        "skipped_documents": list(corpus["skipped_documents"]),
        "excluded_tables": list(corpus["excluded_tables"]),
    }


class VannaCorpus:
    """Resolve role-scoped evidence directly from one pinned Vanna index."""

    def __init__(self, root: Path, index_version: str = "") -> None:
        self.root = root.resolve()
        self.index_version = index_version or VannaRetrieverOnly.current_index_version(self.root)
        if not self.index_version:
            raise ValueError("no current Vanna corpus; run scripts/build_text2sql_vanna.py")
        self.retriever = VannaRetrieverOnly(self.root, self.index_version)
        self._items = {
            str(item["evidence_id"]): dict(item)
            for item in self.retriever.corpus_items()
        }
        status = self.retriever.status()
        self.database_snapshot_id = str(status.get("database_snapshot_id") or "")

    def status(self) -> Mapping[str, Any]:
        return self.retriever.status()

    @staticmethod
    def _evidence(item: Mapping[str, Any], score: float = 0.0) -> Evidence:
        return Evidence(
            evidence_id=str(item.get("evidence_id") or ""),
            source_kind=str(item.get("source_kind") or ""),
            knowledge_type=str(item.get("knowledge_type") or ""),
            title=str(item.get("title") or ""),
            content=str(item.get("content") or ""),
            database_snapshot_id=str(item.get("database_snapshot_id") or ""),
            source_version=str(item.get("source_version") or ""),
            source_url=str(item.get("source_url") or ""),
            dependencies=tuple(str(value) for value in item.get("dependencies") or ()),
            score=round(float(score), 6),
            planning_content=str(item.get("planning_content") or ""),
            knowledge_status=str(item.get("knowledge_status") or ""),
        )

    def resolve_evidence(self, evidence_ids: Sequence[str]) -> Sequence[Evidence]:
        return tuple(
            self._evidence(self._items[evidence_id])
            for evidence_id in dict.fromkeys(str(value) for value in evidence_ids)
            if evidence_id in self._items
            and self._items[evidence_id].get("database_snapshot_id")
            == self.database_snapshot_id
        )

    def raw_item(self, evidence_id: str) -> Mapping[str, Any]:
        return dict(self._items.get(evidence_id) or {})

    @staticmethod
    def _score(query: str, item: Mapping[str, Any], type_weight: float) -> float:
        query_tokens = _tokens(_expanded_query(query))
        if not query_tokens or type_weight <= 0:
            return 0.0
        dependencies = " ".join(str(value) for value in item.get("dependencies") or ())
        fields = (
            (str(item.get("title") or ""), 2.4),
            (str(item.get("item_key") or ""), 2.1),
            (dependencies, 2.0),
            (str(item.get("content") or ""), 1.0),
        )
        field_tokens = [(_tokens(text), weight) for text, weight in fields]
        document_tokens = set().union(*(tokens for tokens, _ in field_tokens))
        overlap = query_tokens.intersection(document_tokens)
        if not overlap:
            return 0.0
        coverage = len(overlap) / len(query_tokens)
        specificity = sum(
            (1.0 + math.log1p(len(token)))
            * max(weight for tokens, weight in field_tokens if token in tokens)
            for token in overlap
        )
        haystack = "\n".join(text for text, _ in fields)
        exact_bonus = 3.0 if query.lower() in haystack.lower() else 0.0
        value_bonus = 0.0
        if item.get("knowledge_type") == "value":
            values = (item.get("structured") or {}).get("values") or ()
            normalized_query = re.sub(r"\s+", "", query).lower()
            matches = {
                str(value).strip().lower()
                for value in values
                if value is not None
                and len(str(value).strip()) >= 2
                and str(value).strip().lower() in normalized_query
            }
            value_bonus = min(18.0, 12.0 * len(matches))
        return round(
            type_weight * (coverage * 4.0 + specificity + exact_bonus + value_bonus),
            6,
        )

    def retrieve(
        self,
        query: str,
        role: str,
        memory_snapshot_id: str,
        policy_version: str,
        limit: Optional[int] = None,
    ) -> tuple[EvidencePack, Mapping[str, Any]]:
        if role not in ROLE_VIEWS:
            raise ValueError("unsupported Text2SQL role: %s" % role)
        view = ROLE_VIEWS[role]
        target_limit = max(1, min(int(limit or view["limit"]), 50))
        semantic = self.retriever.retrieve(query, include_question_sql=False)
        semantic_scores = {
            evidence_id: round(5.0 / (index + 1), 6)
            for index, evidence_id in enumerate(semantic.evidence_ids)
        }
        weighted: dict[str, float] = {}
        for evidence_id, item in self._items.items():
            kind = str(item.get("knowledge_type") or "")
            if kind in {"relationship", "verified_example"}:
                continue
            weight = float(view["weights"].get(kind, 0.0))
            lexical = self._score(query, item, weight)
            vector = semantic_scores.get(evidence_id, 0.0) * weight
            score = lexical + vector
            if score > 0:
                weighted[evidence_id] = round(score, 6)

        # A value hit always carries its defining column and table evidence.
        for evidence_id, anchor in sorted(
            weighted.items(), key=lambda pair: -pair[1]
        ):
            item = self._items[evidence_id]
            if item.get("knowledge_type") != "value":
                continue
            dependencies = set(item.get("dependencies") or ())
            seed_tables = {value for value in dependencies if "." not in str(value)}
            seed_columns = {value for value in dependencies if "." in str(value)}
            for schema_id, schema_item in self._items.items():
                if schema_item.get("knowledge_type") != "schema":
                    continue
                item_key = str(schema_item.get("item_key") or "")
                proposed = 0.0
                if item_key in {"column:%s" % value for value in seed_columns}:
                    proposed = anchor * 0.96
                elif item_key in {"table:%s" % value for value in seed_tables}:
                    proposed = anchor * 0.78
                if proposed:
                    weighted[schema_id] = max(
                        weighted.get(schema_id, 0.0), proposed
                    )

        # Only approved Join Catalog rows exist in the corpus. Expand a small
        # number from the top schema/value seeds so Join evidence cannot dominate.
        relation_weight = float(view["weights"].get("relationship", 0.0))
        if relation_weight > 0:
            seed_tables = {
                dependency
                for evidence_id, _ in sorted(weighted.items(), key=lambda pair: -pair[1])[:8]
                for dependency in self._items[evidence_id].get("dependencies") or ()
                if "." not in str(dependency)
            }
            anchor = max(weighted.values(), default=1.0)
            for evidence_id, item in self._items.items():
                if item.get("knowledge_type") != "relationship":
                    continue
                relation_tables = {
                    value for value in item.get("dependencies") or () if "." not in str(value)
                }
                shared = len(seed_tables.intersection(relation_tables))
                if shared:
                    weighted[evidence_id] = round(
                        anchor * (0.32 if shared == 1 else 0.42) * relation_weight,
                        6,
                    )

        selected = sorted(weighted.items(), key=lambda pair: (-pair[1], pair[0]))[
            :target_limit
        ]
        pack = EvidencePack(
            query=query,
            role=role,
            database_snapshot_id=self.database_snapshot_id,
            # Kept as a wire-compatible field name for old checkpoints.  Its
            # value is now the Vanna corpus version, not a separate business index id.
            wiki_index_version=self.index_version,
            memory_snapshot_id=memory_snapshot_id,
            policy_version=policy_version,
            evidence=tuple(
                self._evidence(self._items[evidence_id], score)
                for evidence_id, score in selected
            ),
        )
        return pack, semantic.diagnostics()

    def retrieve_verified_examples(
        self, query: str, limit: int = 8
    ) -> tuple[Sequence[Mapping[str, Any]], Mapping[str, Any]]:
        bounded = max(1, min(int(limit), 20))
        semantic = self.retriever.retrieve(
            query,
            include_ddl=False,
            include_documentation=False,
            include_question_sql=True,
        )
        semantic_rank = {
            evidence_id: index for index, evidence_id in enumerate(semantic.evidence_ids)
        }
        ranked: list[tuple[float, str, Mapping[str, Any], list[str]]] = []
        for evidence_id, item in self._items.items():
            if item.get("knowledge_type") != "verified_example":
                continue
            structured = dict(item.get("structured") or {})
            lexical = self._score(query, item, 1.0)
            sources: list[str] = []
            if lexical > 0:
                sources.append("lexical")
            semantic_score = 0.0
            if evidence_id in semantic_rank:
                semantic_score = round(5.0 / (semantic_rank[evidence_id] + 1), 6)
                sources.append("vanna")
            score = round(lexical + semantic_score, 6)
            if score <= 0:
                continue
            ranked.append((score, evidence_id, structured, sources))
        ranked.sort(key=lambda value: (-value[0], value[1]))
        examples = [
            {
                "evidence_id": evidence_id,
                "knowledge_type": "verified_example",
                "question": str(structured.get("question") or ""),
                "sql": str(structured.get("sql") or ""),
                "database_snapshot_id": self.database_snapshot_id,
                "source_version": str(self._items[evidence_id].get("source_version") or ""),
                "dependencies": list(self._items[evidence_id].get("dependencies") or ()),
                "retrieval_sources": sources,
                "score": score,
            }
            for score, evidence_id, structured, sources in ranked[:bounded]
        ]
        return examples, semantic.diagnostics()
