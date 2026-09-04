"""Versioned Text2SQL knowledge store with review and ACL gates."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from .knowledge_ingestion import validate_and_chunk_page
from .models import Evidence, EvidencePack, KNOWLEDGE_TYPES
from .wiki_connector import WikiConnector


ROLE_VIEWS: Mapping[str, Mapping[str, Any]] = {
    "lead": {
        "limit": 12,
        "weights": {
            "business_glossary": 1.4,
            "relationship": 1.2,
            "schema": 1.0,
            "value": 0.9,
            "verified_example": 1.1,
        },
    },
    "schema-grounding": {
        "limit": 24,
        "weights": {
            "schema": 1.5,
            "value": 1.4,
            "relationship": 1.5,
            "business_glossary": 1.0,
            "verified_example": 0.7,
        },
    },
    "sql-strategy": {
        "limit": 16,
        "weights": {
            "schema": 1.1,
            "value": 1.0,
            "relationship": 1.4,
            "business_glossary": 1.3,
            "verified_example": 1.5,
        },
    },
    "critic": {
        "limit": 18,
        "weights": {
            "schema": 1.3,
            "value": 1.2,
            "relationship": 1.5,
            "business_glossary": 1.3,
            "verified_example": 0.8,
        },
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _tokens(text: str) -> set[str]:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text).lower().replace("_", " ")
    result: set[str] = set()
    for token in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", normalized):
        result.add(token)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            # Single-character overlap is far too noisy for schema retrieval.  Chinese
            # bigrams/trigrams preserve useful phrases such as "强烈" and "岩爆" while
            # still matching text that has no whitespace word boundaries.
            for width in (2, 3):
                result.update(
                    token[index : index + width]
                    for index in range(len(token) - width + 1)
                )
    return {token for token in result if token}


def _expanded_query(query: str) -> str:
    """Add a deliberately small, auditable set of Text2SQL retrieval aliases."""

    aliases = {
        "案例": ("案件", "case"),
        "案件": ("案例", "case"),
        "多少": ("数量", "计数", "count"),
        "几个": ("数量", "计数", "count"),
        "数量": ("多少", "计数", "count"),
    }
    additions = [alias for term, values in aliases.items() if term in query for alias in values]
    return " ".join((query, *additions))


def _exact_observed_value_bonus(query: str, row: sqlite3.Row) -> float:
    """Reward categorical values that occur as complete observed DB values."""

    if row["knowledge_type"] != "value":
        return 0.0
    try:
        values = json.loads(row["structured_json"]).get("values", [])
    except (AttributeError, json.JSONDecodeError, TypeError):
        return 0.0
    normalized_query = re.sub(r"\s+", "", query).lower()
    exact_matches = {
        str(value).strip().lower()
        for value in values
        if value is not None
        and len(str(value).strip()) >= 2
        and str(value).strip().lower() in normalized_query
    }
    return min(18.0, 12.0 * len(exact_matches))


def _acl_allows(acl_json: str, principals: set[str]) -> bool:
    acl = set(json.loads(acl_json))
    return "*" in acl or bool(acl & principals)


def _is_value_knowledge(column: Mapping[str, Any], table_row_count: int) -> bool:
    profile = column.get("profile", {})
    values = profile.get("low_cardinality_values")
    if not values or profile.get("max_length", 0) > 64:
        return False
    semantic_name = "%s %s" % (column.get("name", ""), column.get("comment", ""))
    if re.search(r"remark|path|process|description|详情|备注|路径|过程", semantic_name, re.I):
        return False
    non_null = table_row_count - int(profile.get("null_count", 0))
    if non_null <= 0:
        return False
    return int(profile.get("distinct_count", 0)) / non_null <= 0.6


class KnowledgeStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._initialize()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "KnowledgeStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wiki_sources (
                source_id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                space_id TEXT NOT NULL,
                root TEXT NOT NULL,
                sync_cursor TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wiki_pages (
                source_id TEXT NOT NULL,
                page_id TEXT NOT NULL,
                page_version TEXT NOT NULL,
                title TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                acl_json TEXT NOT NULL,
                source_url TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                validation_errors_json TEXT NOT NULL,
                active INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source_id, page_id, page_version)
            );
            CREATE TABLE IF NOT EXISTS wiki_sync_runs (
                run_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                upserted INTEGER NOT NULL DEFAULT 0,
                revoked INTEGER NOT NULL DEFAULT 0,
                quarantined INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS knowledge_items (
                evidence_id TEXT PRIMARY KEY,
                source_kind TEXT NOT NULL,
                knowledge_type TEXT NOT NULL,
                item_key TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                structured_json TEXT NOT NULL,
                state TEXT NOT NULL,
                database_snapshot_id TEXT NOT NULL,
                source_version TEXT NOT NULL,
                source_id TEXT NOT NULL,
                page_id TEXT NOT NULL DEFAULT '',
                page_version TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                acl_json TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                validation_errors_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_knowledge_state_type
                ON knowledge_items(state, knowledge_type, database_snapshot_id);
            CREATE INDEX IF NOT EXISTS ix_knowledge_page
                ON knowledge_items(source_id, page_id, page_version);
            CREATE TABLE IF NOT EXISTS knowledge_dependencies (
                evidence_id TEXT NOT NULL,
                dependency TEXT NOT NULL,
                PRIMARY KEY (evidence_id, dependency),
                FOREIGN KEY (evidence_id) REFERENCES knowledge_items(evidence_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS knowledge_reviews (
                review_id TEXT PRIMARY KEY,
                evidence_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (evidence_id) REFERENCES knowledge_items(evidence_id)
            );
            CREATE TABLE IF NOT EXISTS knowledge_indexes (
                index_kind TEXT NOT NULL,
                version INTEGER NOT NULL,
                version_label TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL,
                item_count INTEGER NOT NULL,
                database_snapshot_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (index_kind, version)
            );
            """
        )
        self.connection.commit()
        self._publish_index("candidate")
        self._publish_index("stable")

    def _metadata(self, key: str) -> str:
        row = self.connection.execute(
            "SELECT value FROM knowledge_metadata WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else ""

    def _set_metadata(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO knowledge_metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def database_snapshot_id(self) -> str:
        return self._metadata("database_snapshot_id")

    def _put_item(
        self,
        *,
        evidence_id: str,
        source_kind: str,
        knowledge_type: str,
        item_key: str,
        title: str,
        content: str,
        structured: Mapping[str, Any],
        state: str,
        database_snapshot_id: str,
        source_version: str,
        source_id: str,
        acl: Sequence[str],
        dependencies: Sequence[str],
        page_id: str = "",
        page_version: str = "",
        source_url: str = "",
        validation_errors: Sequence[str] = (),
    ) -> None:
        if knowledge_type not in KNOWLEDGE_TYPES:
            raise ValueError("invalid knowledge type: %s" % knowledge_type)
        timestamp = _now()
        content_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self.connection.execute(
            """
            INSERT INTO knowledge_items(
                evidence_id, source_kind, knowledge_type, item_key, title, content,
                structured_json, state, database_snapshot_id, source_version,
                source_id, page_id, page_version, source_url, acl_json,
                content_sha256, validation_errors_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(evidence_id) DO UPDATE SET
                title=excluded.title,
                content=excluded.content,
                structured_json=excluded.structured_json,
                state=CASE
                    WHEN knowledge_items.state='stable' AND excluded.state='candidate'
                    THEN knowledge_items.state ELSE excluded.state END,
                acl_json=excluded.acl_json,
                source_url=excluded.source_url,
                validation_errors_json=excluded.validation_errors_json,
                updated_at=excluded.updated_at
            """,
            (
                evidence_id,
                source_kind,
                knowledge_type,
                item_key,
                title,
                content,
                _canonical(structured),
                state,
                database_snapshot_id,
                source_version,
                source_id,
                page_id,
                page_version,
                source_url,
                _canonical(sorted(set(acl))),
                content_sha,
                _canonical(sorted(set(validation_errors))),
                timestamp,
                timestamp,
            ),
        )
        self.connection.execute(
            "DELETE FROM knowledge_dependencies WHERE evidence_id = ?", (evidence_id,)
        )
        self.connection.executemany(
            "INSERT INTO knowledge_dependencies(evidence_id, dependency) VALUES (?, ?)",
            [(evidence_id, dependency) for dependency in sorted(set(dependencies))],
        )

    def ingest_database(
        self,
        snapshot: Mapping[str, Any],
        join_catalog: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, int]:
        snapshot_id = str(snapshot["snapshot_id"])
        previous = self.database_snapshot_id()
        with self.connection:
            if previous and previous != snapshot_id:
                self.connection.execute(
                    "UPDATE knowledge_items SET state='revoked', updated_at=? "
                    "WHERE database_snapshot_id<>? AND state IN ('stable','candidate')",
                    (_now(), snapshot_id),
                )
            self._set_metadata("database_snapshot_id", snapshot_id)
            counts = {"schema": 0, "value": 0, "relationship": 0}
            for table in snapshot["tables"]:
                table_name = table["name"]
                column_summary = "; ".join(
                    "%s（%s，%s）"
                    % (column["name"], column["column_type"], column["comment"] or "无注释")
                    for column in table["columns"]
                )
                table_content = "表 %s。表说明：%s。主键：%s。字段：%s" % (
                    table_name,
                    table["comment"] or "无",
                    ", ".join(table["primary_key"]) or "无声明主键",
                    column_summary,
                )
                table_evidence = "db:" + _hash([snapshot_id, "table", table_name])[:24]
                self._put_item(
                    evidence_id=table_evidence,
                    source_kind="database",
                    knowledge_type="schema",
                    item_key="table:%s" % table_name,
                    title="表 %s" % table_name,
                    content=table_content,
                    structured=table,
                    state="stable",
                    database_snapshot_id=snapshot_id,
                    source_version=snapshot_id,
                    source_id="database",
                    acl=("*",),
                    dependencies=(table_name,),
                )
                counts["schema"] += 1
                for column in table["columns"]:
                    qualified = "%s.%s" % (table_name, column["name"])
                    column_content = (
                        "字段 %s。MySQL 类型：%s；是否可空：%s；字段注释：%s。"
                        % (
                            qualified,
                            column["column_type"],
                            "是" if column["nullable"] else "否",
                            column["comment"] or "无",
                        )
                    )
                    evidence_id = "db:" + _hash([snapshot_id, "column", qualified])[:24]
                    self._put_item(
                        evidence_id=evidence_id,
                        source_kind="database",
                        knowledge_type="schema",
                        item_key="column:%s" % qualified,
                        title="字段 %s" % qualified,
                        content=column_content,
                        structured=column,
                        state="stable",
                        database_snapshot_id=snapshot_id,
                        source_version=snapshot_id,
                        source_id="database",
                        acl=("*",),
                        dependencies=(table_name, qualified),
                    )
                    counts["schema"] += 1
                    values = column.get("profile", {}).get("low_cardinality_values")
                    if _is_value_knowledge(column, int(table.get("row_count", 0))):
                        display_values = ["<EMPTY>" if value == "" else str(value) for value in values]
                        value_evidence = "db:" + _hash(
                            [snapshot_id, "value", qualified, values]
                        )[:24]
                        self._put_item(
                            evidence_id=value_evidence,
                            source_kind="database",
                            knowledge_type="value",
                            item_key="value:%s" % qualified,
                            title="字段值域 %s" % qualified,
                            content="字段 %s 的当前快照观测值：%s"
                            % (qualified, "、".join(display_values)),
                            structured={"column": qualified, "values": values},
                            state="stable",
                            database_snapshot_id=snapshot_id,
                            source_version=snapshot_id,
                            source_id="database",
                            acl=("*",),
                            dependencies=(table_name, qualified),
                        )
                        counts["value"] += 1

            if join_catalog:
                for relationship in join_catalog.get("relationships", []):
                    decision = relationship.get("decision", "pending")
                    state = {
                        "approved": "stable",
                        "rejected": "rejected",
                        "pending": "candidate",
                    }.get(decision, "candidate")
                    left = relationship["left"]
                    right = relationship["right"]
                    evidence_id = "join:" + relationship["candidate_id"]
                    content = (
                        "Join 关系：%s = %s。候选基数：%s；fanout 风险：%s；审核状态：%s。"
                        % (
                            left,
                            right,
                            relationship.get("cardinality", "unknown"),
                            relationship.get("fanout_risk", "unknown"),
                            decision,
                        )
                    )
                    self._put_item(
                        evidence_id=evidence_id,
                        source_kind="database",
                        knowledge_type="relationship",
                        item_key="relationship:%s" % relationship["candidate_id"],
                        title="%s ↔ %s" % (left, right),
                        content=content,
                        structured=relationship,
                        state=state,
                        database_snapshot_id=snapshot_id,
                        source_version=snapshot_id,
                        source_id="database-join-catalog",
                        acl=("*",),
                        dependencies=(left.split(".", 1)[0], left, right.split(".", 1)[0], right),
                    )
                    counts["relationship"] += 1
        self._publish_index("candidate")
        self._publish_index("stable")
        return counts

    def sync_wiki(
        self,
        source_id: str,
        connector: WikiConnector,
        database_snapshot: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        snapshot_id = str(database_snapshot["snapshot_id"])
        if self.database_snapshot_id() and self.database_snapshot_id() != snapshot_id:
            raise ValueError("Wiki sync snapshot does not match KnowledgeStore snapshot")
        spaces = connector.list_spaces()
        if len(spaces) != 1:
            raise ValueError("one Wiki connector must expose exactly one configured space")
        source = self.connection.execute(
            "SELECT sync_cursor FROM wiki_sources WHERE source_id=?", (source_id,)
        ).fetchone()
        cursor = source[0] if source else ""
        run_id = "sync_" + _hash([source_id, cursor, _now()])[:20]
        started = _now()
        with self.connection:
            self.connection.execute(
                "INSERT INTO wiki_sync_runs(run_id,source_id,started_at,status) VALUES(?,?,?,'running')",
                (run_id, source_id, started),
            )
            self.connection.execute(
                """
                INSERT INTO wiki_sources(source_id,platform,space_id,root,sync_cursor,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (source_id, "markdown", spaces[0].space_id, spaces[0].name, cursor, started, started),
            )
        upserted = revoked = quarantined = 0
        next_cursor = cursor
        try:
            changes = connector.list_changes(cursor or None)
            for change in changes:
                next_cursor = change.cursor
                if change.change_type == "revoke":
                    with self.connection:
                        self.connection.execute(
                            "UPDATE wiki_pages SET active=0,status='revoked' "
                            "WHERE source_id=? AND page_id=? AND active=1",
                            (source_id, change.page_id),
                        )
                        self.connection.execute(
                            "UPDATE knowledge_items SET state='revoked',updated_at=? "
                            "WHERE source_id=? AND page_id=? AND state IN ('candidate','stable','quarantined')",
                            (_now(), source_id, change.page_id),
                        )
                    revoked += 1
                    continue

                existing = self.connection.execute(
                    "SELECT 1 FROM wiki_pages WHERE source_id=? AND page_id=? AND page_version=?",
                    (source_id, change.page_id, change.page_version),
                ).fetchone()
                if existing:
                    continue
                page = connector.fetch_page(change.page_id, change.page_version)
                acl = connector.fetch_acl(change.page_id)
                source_url = connector.resolve_link(change.page_id)
                validation = validate_and_chunk_page(page, acl, database_snapshot)
                status = "quarantined" if validation.errors else "candidate"
                with self.connection:
                    self.connection.execute(
                        "UPDATE wiki_pages SET active=0 WHERE source_id=? AND page_id=?",
                        (source_id, page.page_id),
                    )
                    self.connection.execute(
                        "UPDATE knowledge_items SET state='revoked',updated_at=? "
                        "WHERE source_id=? AND page_id=? AND state IN ('candidate','stable','quarantined')",
                        (_now(), source_id, page.page_id),
                    )
                    self.connection.execute(
                        """
                        INSERT INTO wiki_pages(
                            source_id,page_id,page_version,title,owner_id,acl_json,source_url,
                            content_sha256,status,validation_errors_json,active,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,1,?)
                        """,
                        (
                            source_id,
                            page.page_id,
                            page.page_version,
                            page.title,
                            page.owner_id,
                            _canonical(list(acl.principals)),
                            source_url,
                            hashlib.sha256(page.content.encode("utf-8")).hexdigest(),
                            status,
                            _canonical(validation.errors),
                            page.updated_at,
                        ),
                    )
                    for index, chunk in enumerate(validation.chunks):
                        evidence_id = "wiki:" + _hash(
                            [source_id, page.page_id, page.page_version, chunk.content_sha256, index]
                        )[:24]
                        self._put_item(
                            evidence_id=evidence_id,
                            source_kind="wiki",
                            knowledge_type=chunk.knowledge_type,
                            item_key="wiki:%s:%d" % (page.page_id, index),
                            title=chunk.title,
                            content=chunk.content,
                            structured={"metadata": dict(page.metadata), "chunk_index": index},
                            state=status,
                            database_snapshot_id=snapshot_id,
                            source_version=page.page_version,
                            source_id=source_id,
                            page_id=page.page_id,
                            page_version=page.page_version,
                            source_url=source_url,
                            acl=acl.principals,
                            dependencies=chunk.dependencies,
                            validation_errors=validation.errors,
                        )
                upserted += 1
                if validation.errors:
                    quarantined += 1
            with self.connection:
                self.connection.execute(
                    "UPDATE wiki_sources SET sync_cursor=?,updated_at=? WHERE source_id=?",
                    (next_cursor, _now(), source_id),
                )
                self.connection.execute(
                    "UPDATE wiki_sync_runs SET completed_at=?,status='completed',upserted=?,revoked=?,quarantined=? WHERE run_id=?",
                    (_now(), upserted, revoked, quarantined, run_id),
                )
        except Exception as exc:
            with self.connection:
                self.connection.execute(
                    "UPDATE wiki_sync_runs SET completed_at=?,status='failed',error=? WHERE run_id=?",
                    (_now(), str(exc)[:1000], run_id),
                )
            raise
        candidate_version = self._publish_index("candidate")
        stable_version = self._publish_index("stable")
        return {
            "run_id": run_id,
            "upserted": upserted,
            "revoked": revoked,
            "quarantined": quarantined,
            "candidate_index_version": candidate_version,
            "stable_index_version": stable_version,
        }

    def review(self, evidence_id: str, decision: str, reviewer: str, reason: str = "") -> str:
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        if not reviewer.strip():
            raise ValueError("reviewer is required")
        row = self.connection.execute(
            "SELECT state,validation_errors_json FROM knowledge_items WHERE evidence_id=?",
            (evidence_id,),
        ).fetchone()
        if not row:
            raise KeyError(evidence_id)
        if row["state"] != "candidate":
            raise ValueError("only candidate knowledge can be reviewed")
        errors = json.loads(row["validation_errors_json"])
        if decision == "approve" and errors:
            raise ValueError("knowledge with validation errors cannot be approved")
        state = "stable" if decision == "approve" else "rejected"
        review_id = "review_" + _hash([evidence_id, decision, reviewer, _now()])[:20]
        with self.connection:
            self.connection.execute(
                "UPDATE knowledge_items SET state=?,updated_at=? WHERE evidence_id=?",
                (state, _now(), evidence_id),
            )
            self.connection.execute(
                "INSERT INTO knowledge_reviews(review_id,evidence_id,decision,reviewer,reason,created_at) VALUES(?,?,?,?,?,?)",
                (review_id, evidence_id, decision, reviewer, reason, _now()),
            )
        self._publish_index("candidate")
        return self._publish_index("stable")

    def promote_verified_example(
        self,
        question: str,
        sql: str,
        reviewer: str,
        *,
        source_id: str,
        dependencies: Sequence[str] = (),
    ) -> Mapping[str, str]:
        """Idempotently publish one explicitly reviewed question-SQL example."""

        question = question.strip()
        sql = sql.strip()
        reviewer = reviewer.strip()
        if not question or not sql or not reviewer or not source_id.strip():
            raise ValueError("reviewed example requires question, SQL, reviewer and source")
        snapshot_id = self.database_snapshot_id()
        if not snapshot_id:
            raise ValueError("database knowledge has not been ingested")
        evidence_id = "example:" + _hash([snapshot_id, question, sql])[:24]
        existing = self.connection.execute(
            "SELECT state FROM knowledge_items WHERE evidence_id=?", (evidence_id,)
        ).fetchone()
        if not existing or existing["state"] != "stable":
            review_id = "review_" + _hash(
                [evidence_id, "approve", reviewer, source_id]
            )[:20]
            with self.connection:
                self._put_item(
                    evidence_id=evidence_id,
                    source_kind="experience",
                    knowledge_type="verified_example",
                    item_key="question-sql:%s" % evidence_id,
                    title=question[:300],
                    content="人工审核问题：%s\n审核通过 SQL：%s" % (question, sql),
                    structured={"question": question, "sql": sql},
                    state="candidate",
                    database_snapshot_id=snapshot_id,
                    source_version=source_id[:200],
                    source_id=source_id[:200],
                    acl=("*",),
                    dependencies=tuple(dict.fromkeys(dependencies)),
                )
                self.connection.execute(
                    "UPDATE knowledge_items SET state='stable',updated_at=? WHERE evidence_id=?",
                    (_now(), evidence_id),
                )
                self.connection.execute(
                    "INSERT OR IGNORE INTO knowledge_reviews("
                    "review_id,evidence_id,decision,reviewer,reason,created_at"
                    ") VALUES(?,?,?,?,?,?)",
                    (
                        review_id,
                        evidence_id,
                        "approve",
                        reviewer[:200],
                        "Explicit Text2SQL experience promotion",
                        _now(),
                    ),
                )
        self._publish_index("candidate")
        return {
            "evidence_id": evidence_id,
            "stable_index_version": self._publish_index("stable"),
        }

    def _publish_index(self, kind: str) -> str:
        if kind not in {"candidate", "stable"}:
            raise ValueError("invalid index kind")
        state = kind
        rows = self.connection.execute(
            "SELECT evidence_id,content_sha256,source_version,acl_json FROM knowledge_items "
            "WHERE state=? ORDER BY evidence_id",
            (state,),
        ).fetchall()
        fingerprint = _hash([dict(row) for row in rows])
        latest = self.connection.execute(
            "SELECT version,version_label,fingerprint FROM knowledge_indexes "
            "WHERE index_kind=? ORDER BY version DESC LIMIT 1",
            (kind,),
        ).fetchone()
        if latest and latest["fingerprint"] == fingerprint:
            return str(latest["version_label"])
        version = int(latest["version"]) + 1 if latest else 1
        label = "%s-v%d-%s" % (kind, version, fingerprint[:10])
        with self.connection:
            self.connection.execute(
                "INSERT INTO knowledge_indexes(index_kind,version,version_label,fingerprint,item_count,database_snapshot_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (kind, version, label, fingerprint, len(rows), self.database_snapshot_id(), _now()),
            )
        return label

    def current_index_version(self, kind: str = "stable") -> str:
        row = self.connection.execute(
            "SELECT version_label FROM knowledge_indexes WHERE index_kind=? ORDER BY version DESC LIMIT 1",
            (kind,),
        ).fetchone()
        return str(row[0]) if row else self._publish_index(kind)

    def candidates(self) -> list[Mapping[str, Any]]:
        rows = self.connection.execute(
            "SELECT evidence_id,source_kind,knowledge_type,title,content,source_url,validation_errors_json "
            "FROM knowledge_items WHERE state='candidate' ORDER BY source_kind,title"
        ).fetchall()
        return [
            {
                **dict(row),
                "validation_errors": json.loads(row["validation_errors_json"]),
            }
            for row in rows
        ]

    def stable_items_for_index(self) -> Sequence[Mapping[str, Any]]:
        """Return reviewed rows for offline vector-index construction.

        This is intentionally not a retrieval API.  Runtime consumers must still
        resolve Vanna hits through :meth:`resolve_stable_evidence` so ACL, snapshot
        and state checks cannot be bypassed by the vector store.
        """

        snapshot_id = self.database_snapshot_id()
        rows = self.connection.execute(
            "SELECT * FROM knowledge_items WHERE state='stable' "
            "AND database_snapshot_id=? ORDER BY evidence_id",
            (snapshot_id,),
        ).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            item["structured"] = json.loads(item.pop("structured_json"))
            item["acl"] = json.loads(item.pop("acl_json"))
            item["validation_errors"] = json.loads(
                item.pop("validation_errors_json")
            )
            item["dependencies"] = list(self._dependencies(item["evidence_id"]))
            values.append(item)
        return tuple(values)

    def resolve_stable_evidence(
        self,
        evidence_ids: Sequence[str],
        principals: Sequence[str],
        scores: Optional[Mapping[str, float]] = None,
    ) -> Sequence[Evidence]:
        """Re-authorize vector hits against the stable relational source of truth."""

        identifiers = tuple(
            dict.fromkeys(str(value) for value in evidence_ids if str(value).strip())
        )
        if not identifiers:
            return ()
        placeholders = ",".join("?" for _ in identifiers)
        snapshot_id = self.database_snapshot_id()
        rows = self.connection.execute(
            "SELECT * FROM knowledge_items WHERE evidence_id IN (%s) "
            "AND state='stable' AND database_snapshot_id=?" % placeholders,
            (*identifiers, snapshot_id),
        ).fetchall()
        by_id = {str(row["evidence_id"]): row for row in rows}
        principal_set = set(principals)
        resolved = []
        for evidence_id in identifiers:
            row = by_id.get(evidence_id)
            if row is None or not _acl_allows(row["acl_json"], principal_set):
                continue
            resolved.append(
                Evidence(
                    evidence_id=evidence_id,
                    source_kind=row["source_kind"],
                    knowledge_type=row["knowledge_type"],
                    title=row["title"],
                    content=row["content"],
                    database_snapshot_id=row["database_snapshot_id"],
                    source_version=row["source_version"],
                    source_url=row["source_url"],
                    dependencies=self._dependencies(evidence_id),
                    score=float((scores or {}).get(evidence_id, 0.0)),
                )
            )
        return tuple(resolved)

    def _dependencies(self, evidence_id: str) -> tuple[str, ...]:
        return tuple(
            item[0]
            for item in self.connection.execute(
                "SELECT dependency FROM knowledge_dependencies "
                "WHERE evidence_id=? ORDER BY dependency",
                (evidence_id,),
            )
        )

    def _score(self, query: str, row: sqlite3.Row, type_weight: float) -> float:
        query_tokens = _tokens(_expanded_query(query))
        if not query_tokens:
            return 0.0
        dependencies = " ".join(self._dependencies(row["evidence_id"]))
        fields = (
            (row["title"], 2.4),
            (row["item_key"], 2.1),
            (dependencies, 2.0),
            (row["content"], 1.0),
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
        identifier_bonus = 1.5 if any(token.startswith("t_") for token in query.split()) else 0.0
        value_bonus = _exact_observed_value_bonus(query, row)
        return round(
            type_weight
            * (coverage * 4.0 + specificity + exact_bonus + identifier_bonus + value_bonus),
            6,
        )

    def _relationship_expansion(
        self,
        rows: Sequence[sqlite3.Row],
        base_scored: Sequence[tuple[float, sqlite3.Row]],
        principals: set[str],
        type_weight: float,
        target_limit: int,
    ) -> list[tuple[float, sqlite3.Row]]:
        """Expand top schema/value hits through stable, ACL-visible Join edges."""

        graph_limit = min(3, target_limit // 6)
        if graph_limit <= 0:
            return []
        seeds = base_scored[: min(8, target_limit)]
        seed_tables = {
            dependency
            for _, seed in seeds
            for dependency in self._dependencies(seed["evidence_id"])
            if "." not in dependency
        }
        if not seed_tables:
            return []

        expanded: list[tuple[float, sqlite3.Row]] = []
        anchor = max((score for score, _ in seeds), default=1.0)
        for row in rows:
            if row["knowledge_type"] != "relationship":
                continue
            if not _acl_allows(row["acl_json"], principals):
                continue
            relation_tables = {
                dependency
                for dependency in self._dependencies(row["evidence_id"])
                if "." not in dependency
            }
            shared = len(seed_tables.intersection(relation_tables))
            if not shared:
                continue
            # Graph evidence is intentionally placed behind direct lexical hits.  It
            # supplies the approved path without taking over entity grounding.
            score = anchor * (0.32 if shared == 1 else 0.42) * type_weight
            expanded.append((round(score, 6), row))
        expanded.sort(key=lambda item: (-item[0], item[1]["evidence_id"]))
        return expanded[:graph_limit]

    def _schema_context_expansion(
        self,
        rows: Sequence[sqlite3.Row],
        base_scored: Sequence[tuple[float, sqlite3.Row]],
        principals: set[str],
    ) -> list[tuple[float, sqlite3.Row]]:
        """Attach defining schema, table, and identifier fields to value hits."""

        row_scores = {row["evidence_id"]: score for score, row in base_scored}
        row_by_id = {row["evidence_id"]: row for row in rows}
        value_seeds = [item for item in base_scored if item[1]["knowledge_type"] == "value"][:3]
        for anchor, value_row in value_seeds:
            dependencies = self._dependencies(value_row["evidence_id"])
            seed_tables = {item for item in dependencies if "." not in item}
            seed_columns = {item for item in dependencies if "." in item}
            for schema_row in rows:
                if schema_row["knowledge_type"] != "schema":
                    continue
                if not _acl_allows(schema_row["acl_json"], principals):
                    continue
                schema_dependencies = set(self._dependencies(schema_row["evidence_id"]))
                proposed = 0.0
                if seed_columns.intersection(schema_dependencies):
                    proposed = anchor * 0.96
                elif schema_row["item_key"] in {
                    "table:%s" % table for table in seed_tables
                }:
                    proposed = anchor * 0.78
                elif seed_tables.intersection(schema_dependencies) and re.search(
                    r"(?:^|[._])(?:id|.*code)$|编码",
                    "%s %s" % (schema_row["item_key"], schema_row["content"]),
                    re.I,
                ):
                    proposed = anchor * 0.70
                if proposed:
                    row_scores[schema_row["evidence_id"]] = max(
                        row_scores.get(schema_row["evidence_id"], 0.0),
                        round(proposed, 6),
                    )
        expanded = [(score, row_by_id[evidence_id]) for evidence_id, score in row_scores.items()]
        expanded.sort(key=lambda item: (-item[0], item[1]["evidence_id"]))
        return expanded

    def retrieve(
        self,
        query: str,
        role: str,
        principals: Sequence[str],
        memory_snapshot_id: str,
        policy_version: str,
        limit: Optional[int] = None,
    ) -> EvidencePack:
        if role not in ROLE_VIEWS:
            raise ValueError("unsupported Text2SQL role: %s" % role)
        snapshot_id = self.database_snapshot_id()
        if not snapshot_id:
            raise ValueError("database knowledge has not been ingested")
        principal_set = set(principals)
        view = ROLE_VIEWS[role]
        rows = self.connection.execute(
            "SELECT * FROM knowledge_items WHERE state='stable' AND database_snapshot_id=?",
            (snapshot_id,),
        ).fetchall()
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            if not _acl_allows(row["acl_json"], principal_set):
                continue
            weight = float(view["weights"].get(row["knowledge_type"], 0.0))
            score = self._score(query, row, weight)
            if score > 0 and row["knowledge_type"] != "relationship":
                scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], item[1]["evidence_id"]))
        scored = self._schema_context_expansion(rows, scored, principal_set)

        target_limit = limit or int(view["limit"])
        relationship_rows = self._relationship_expansion(
            rows,
            scored,
            principal_set,
            float(view["weights"].get("relationship", 0.0)),
            target_limit,
        )
        selected = scored[: target_limit - len(relationship_rows)] + relationship_rows
        evidence: list[Evidence] = []
        for score, row in selected:
            dependencies = self._dependencies(row["evidence_id"])
            evidence.append(
                Evidence(
                    evidence_id=row["evidence_id"],
                    source_kind=row["source_kind"],
                    knowledge_type=row["knowledge_type"],
                    title=row["title"],
                    content=row["content"],
                    database_snapshot_id=row["database_snapshot_id"],
                    source_version=row["source_version"],
                    source_url=row["source_url"],
                    dependencies=dependencies,
                    score=score,
                )
            )
        return EvidencePack(
            query=query,
            role=role,
            database_snapshot_id=snapshot_id,
            wiki_index_version=self.current_index_version("stable"),
            memory_snapshot_id=memory_snapshot_id,
            policy_version=policy_version,
            evidence=tuple(evidence),
        )

    def stats(self) -> Mapping[str, Any]:
        states = {
            row["state"]: int(row["count"])
            for row in self.connection.execute(
                "SELECT state,COUNT(*) AS count FROM knowledge_items GROUP BY state"
            )
        }
        types = {
            row["knowledge_type"]: int(row["count"])
            for row in self.connection.execute(
                "SELECT knowledge_type,COUNT(*) AS count FROM knowledge_items GROUP BY knowledge_type"
            )
        }
        return {
            "database_snapshot_id": self.database_snapshot_id(),
            "states": states,
            "types": types,
            "candidate_index_version": self.current_index_version("candidate"),
            "stable_index_version": self.current_index_version("stable"),
        }
