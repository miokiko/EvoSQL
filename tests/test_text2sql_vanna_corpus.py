import json
import tempfile
import unittest
from pathlib import Path

from evoagent.text2sql.vanna_corpus import (
    add_confirmed_question_sql,
    collect_vanna_corpus,
    load_confirmed_question_sql,
    remove_confirmed_question_sql,
)
from evoagent.text2sql.vanna_retriever import VannaRetrieverOnly


SNAPSHOT = {
    "snapshot_id": "snapshot-test-1",
    "tables": [
        {
            "name": "t_cases",
            "comment": "岩爆案例",
            "row_count": 2,
            "primary_key": ["id"],
            "columns": [
                {
                    "name": "id",
                    "column_type": "INTEGER",
                    "sqlite_type": "INTEGER",
                    "nullable": False,
                    "comment": "案例主键",
                    "profile": {
                        "null_count": 0,
                        "distinct_count": 2,
                        "max_length": 1,
                    },
                },
                {
                    "name": "c_level",
                    "column_type": "VARCHAR(16)",
                    "sqlite_type": "TEXT",
                    "nullable": True,
                    "comment": "岩爆等级",
                    "profile": {
                        "null_count": 0,
                        "distinct_count": 2,
                        "max_length": 2,
                        "low_cardinality_values": ["强烈", "中等"],
                    },
                },
            ],
        },
        {
            "name": "t_detail",
            "comment": "案例详情",
            "row_count": 2,
            "primary_key": ["id"],
            "columns": [
                {
                    "name": "id",
                    "column_type": "INTEGER",
                    "sqlite_type": "INTEGER",
                    "nullable": False,
                    "comment": "详情主键",
                    "profile": {
                        "null_count": 0,
                        "distinct_count": 2,
                        "max_length": 1,
                    },
                },
                {
                    "name": "case_id",
                    "column_type": "INTEGER",
                    "sqlite_type": "INTEGER",
                    "nullable": False,
                    "comment": "案例主键",
                    "profile": {
                        "null_count": 0,
                        "distinct_count": 2,
                        "max_length": 1,
                    },
                },
            ],
        },
    ],
}


def write_business_document(
    path: Path,
    *,
    page_id: str,
    knowledge_type: str = "business_glossary",
    business_kind: str = "business_rule",
    body: str = "# 岩爆等级\n`t_cases.c_level` 表示岩爆等级。",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """---
page_id: {page_id}
title: 岩爆业务说明
knowledge_type: {knowledge_type}
business_kind: {business_kind}
database_snapshot_id: {snapshot_id}
---
{body}
""".format(
            page_id=page_id,
            knowledge_type=knowledge_type,
            business_kind=business_kind,
            snapshot_id=SNAPSHOT["snapshot_id"],
            body=body,
        ),
        encoding="utf-8",
    )


class _FakeBackend:
    def __init__(self, config):
        self.path = str(config["path"])
        self.ddl = []
        self.documentation = []
        self.question_sql = []

    def add_ddl(self, value):
        self.ddl.append(value)

    def add_documentation(self, value):
        self.documentation.append(value)

    def add_question_sql(self, question, sql):
        self.question_sql.append({"question": question, "sql": sql})


class VannaCorpusTests(unittest.TestCase):
    def test_business_markdown_enters_corpus_without_review_state(self):
        with tempfile.TemporaryDirectory() as directory:
            business_root = Path(directory) / "business"
            write_business_document(business_root / "level.md", page_id="level-rule")

            corpus = collect_vanna_corpus(
                SNAPSHOT,
                business_root=business_root,
                excluded_tables=(),
            )

            documents = [
                item
                for item in corpus["items"]
                if item["source_kind"] == "business_document"
            ]
            self.assertEqual(len(documents), 1)
            self.assertEqual(documents[0]["knowledge_type"], "business_glossary")
            self.assertIn("t_cases.c_level", documents[0]["dependencies"])
            self.assertNotIn("state", documents[0])
            self.assertEqual(corpus["skipped_documents"], [])

    def test_candidate_relationship_document_is_skipped_and_only_approved_join_enters(self):
        with tempfile.TemporaryDirectory() as directory:
            business_root = Path(directory) / "business"
            write_business_document(
                business_root / "relationship_candidate.md",
                page_id="join-proposal",
                knowledge_type="relationship",
                business_kind="relationship_candidate",
                body=(
                    "# 待核验关系\n"
                    "候选关系为 `t_cases.id = t_detail.case_id`，不能直接用于运行时。"
                ),
            )
            join_catalog = {
                "relationships": [
                    {
                        "candidate_id": "approved-link",
                        "left": "t_cases.id",
                        "right": "t_detail.case_id",
                        "decision": "approved",
                        "cardinality": "one_to_one",
                        "result_grain": "case",
                        "fanout_risk": "low",
                    },
                    {
                        "candidate_id": "pending-link",
                        "left": "t_cases.id",
                        "right": "t_detail.case_id",
                        "decision": "candidate",
                    },
                    {
                        "candidate_id": "rejected-link",
                        "left": "t_cases.id",
                        "right": "t_detail.case_id",
                        "decision": "rejected",
                    },
                ]
            }

            corpus = collect_vanna_corpus(
                SNAPSHOT,
                business_root=business_root,
                join_catalog=join_catalog,
                excluded_tables=(),
            )

            relationships = [
                item
                for item in corpus["items"]
                if item["knowledge_type"] == "relationship"
            ]
            self.assertEqual(
                [item["evidence_id"] for item in relationships],
                ["join:approved-link"],
            )
            self.assertEqual(relationships[0]["source_kind"], "join_catalog")
            self.assertEqual(
                corpus["skipped_documents"], ["relationship_candidate.md"]
            )

    def test_confirmed_question_sql_add_is_idempotent_and_remove_is_reversible(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "confirmed_question_sql.json"
            arguments = {
                "database_snapshot_id": SNAPSHOT["snapshot_id"],
                "question": "强烈岩爆案例有多少个？",
                "sql": "SELECT COUNT(*) FROM t_cases WHERE c_level = '强烈'",
                "actor": "local-user",
                "source_id": "query-run-1",
                "dependencies": ("t_cases", "t_cases.c_level"),
            }

            first = add_confirmed_question_sql(registry, **arguments)
            second = add_confirmed_question_sql(
                registry,
                **{**arguments, "actor": "another-display-name"},
            )

            self.assertEqual(second, first)
            stored = load_confirmed_question_sql(registry, SNAPSHOT["snapshot_id"])
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]["evidence_id"], first["evidence_id"])
            self.assertEqual(stored[0]["dependencies"], ["t_cases", "t_cases.c_level"])
            self.assertEqual(load_confirmed_question_sql(registry, "another-snapshot"), [])

            removed = remove_confirmed_question_sql(
                registry, source_id="query-run-1"
            )
            self.assertEqual(removed["removed"], 1)
            self.assertEqual(removed["evidence_ids"], [first["evidence_id"]])
            self.assertEqual(
                load_confirmed_question_sql(registry, SNAPSHOT["snapshot_id"]), []
            )
            self.assertEqual(
                remove_confirmed_question_sql(registry, source_id="query-run-1"),
                {"removed": 0, "evidence_ids": []},
            )

    def test_build_writes_corpus_sidecar_and_atomic_current_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vanna"
            business_root = Path(directory) / "business"
            registry = Path(directory) / "confirmed_question_sql.json"
            write_business_document(business_root / "level.md", page_id="level-rule")
            add_confirmed_question_sql(
                registry,
                database_snapshot_id=SNAPSHOT["snapshot_id"],
                question="强烈岩爆案例有多少个？",
                sql="SELECT COUNT(*) FROM t_cases WHERE c_level = '强烈'",
                actor="local-user",
                source_id="query-run-1",
            )
            corpus = collect_vanna_corpus(
                SNAPSHOT,
                business_root=business_root,
                question_sql_path=registry,
                excluded_tables=(),
            )
            retriever = VannaRetrieverOnly(
                root,
                corpus["index_version"],
                enabled=True,
                backend_factory=_FakeBackend,
            )

            result = retriever.build(corpus["items"], SNAPSHOT["snapshot_id"])

            self.assertTrue(result["ready"])
            self.assertTrue(retriever.corpus_path.exists())
            sidecar = json.loads(retriever.corpus_path.read_text(encoding="utf-8"))
            self.assertEqual(sidecar["contract"], "evoagent-vanna-corpus-v1")
            self.assertEqual(sidecar["database_snapshot_id"], SNAPSHOT["snapshot_id"])
            self.assertEqual(
                [item["evidence_id"] for item in sidecar["items"]],
                [item["evidence_id"] for item in corpus["items"]],
            )
            current = json.loads((root / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(current["contract"], "evoagent-vanna-current-v1")
            self.assertEqual(current["index_version"], corpus["index_version"])
            self.assertEqual(
                VannaRetrieverOnly.current_index_version(root),
                corpus["index_version"],
            )
            self.assertEqual(
                [item["evidence_id"] for item in retriever.corpus_items()],
                [item["evidence_id"] for item in corpus["items"]],
            )

            # An idempotent rebuild republishes a missing mutable pointer while
            # preserving the immutable version directory.
            (root / "current.json").unlink()
            repeated = retriever.build(corpus["items"], SNAPSHOT["snapshot_id"])
            self.assertEqual(
                repeated["added"], {"ddl": 0, "documentation": 0, "sql": 0}
            )
            self.assertTrue((root / "current.json").exists())


if __name__ == "__main__":
    unittest.main()
