import tempfile
import unittest
from pathlib import Path

from evoagent.text2sql.vanna_retriever import VannaRetrieverOnly


class _FakeBackend:
    stores = {}

    def __init__(self, config):
        self.path = str(config["path"])
        self.store = self.stores.setdefault(
            self.path, {"ddl": [], "documentation": [], "sql": []}
        )

    def add_ddl(self, value):
        self.store["ddl"].append(value)

    def add_documentation(self, value):
        self.store["documentation"].append(value)

    def add_question_sql(self, question, sql):
        self.store["sql"].append({"question": question, "sql": sql})

    def get_related_ddl(self, _question):
        return self.store["ddl"]

    def get_related_documentation(self, _question):
        return self.store["documentation"]

    def get_similar_question_sql(self, _question):
        return self.store["sql"]


class VannaRetrieverOnlyTests(unittest.TestCase):
    def setUp(self):
        _FakeBackend.stores = {}

    @staticmethod
    def rows():
        return (
            {
                "evidence_id": "db:cases",
                "knowledge_type": "schema",
                "item_key": "table:cases",
                "title": "cases",
                "content": "案例表",
                "content_sha256": "schema-sha",
                "source_version": "snapshot-1",
                "structured": {
                    "name": "cases",
                    "primary_key": ["id"],
                    "columns": [
                        {
                            "name": "id",
                            "column_type": "INTEGER",
                            "nullable": False,
                        }
                    ],
                },
            },
            {
                "evidence_id": "wiki:level",
                "knowledge_type": "business_glossary",
                "item_key": "wiki:level",
                "title": "岩爆等级",
                "content": "强烈表示等级为强烈。",
                "content_sha256": "wiki-sha",
                "source_version": "wiki-1",
                "structured": {},
            },
            {
                "evidence_id": "example:count",
                "knowledge_type": "verified_example",
                "item_key": "example:count",
                "title": "强烈岩爆有多少个",
                "content": "人工审核问题-SQL",
                "content_sha256": "example-sha",
                "source_version": "review-1",
                "structured": {
                    "question": "强烈岩爆有多少个",
                    "sql": "SELECT COUNT(*) FROM cases",
                },
            },
        )

    def test_build_and_retrieve_without_exposing_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            retriever = VannaRetrieverOnly(
                Path(directory),
                "stable-v1",
                enabled=True,
                backend_factory=_FakeBackend,
            )
            built = retriever.build(self.rows(), "snapshot-1")
            self.assertTrue(built["ready"])
            self.assertEqual(built["counts"], {"ddl": 1, "documentation": 1, "sql": 1})
            self.assertFalse(hasattr(retriever, "generate_sql"))
            self.assertFalse(hasattr(retriever, "run_sql"))

            # The fake store key follows the temporary build directory.  Mirror
            # it after the atomic rename as a real Chroma store lives on disk.
            source = next(iter(_FakeBackend.stores.values()))
            _FakeBackend.stores[str(retriever.index_path)] = source
            result = retriever.retrieve("强烈岩爆数量")
            self.assertEqual(
                result.evidence_ids,
                ("db:cases", "wiki:level", "example:count"),
            )
            self.assertEqual(result.question_sql[0]["sql"], "SELECT COUNT(*) FROM cases")
            self.assertNotIn("EVO_EVIDENCE_ID", result.ddl[0])

    def test_missing_index_falls_back_without_importing_vanna(self):
        with tempfile.TemporaryDirectory() as directory:
            retriever = VannaRetrieverOnly(
                Path(directory),
                "missing",
                enabled=True,
                backend_factory=_FakeBackend,
            )
            result = retriever.retrieve("任意问题")
            self.assertEqual(result.backend, "knowledge-store-fallback")
            self.assertEqual(result.evidence_ids, ())


if __name__ == "__main__":
    unittest.main()
