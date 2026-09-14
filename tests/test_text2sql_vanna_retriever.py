import json
import tempfile
import unittest
from pathlib import Path

from evoagent.text2sql.vanna_retriever import (
    VannaDraftGenerator,
    VannaRetrieverOnly,
)


class _FakeBackend:
    stores = {}
    run_sql_calls = 0

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

    def run_sql(self, _sql):
        type(self).run_sql_calls += 1
        raise AssertionError("draft generation must never execute SQL")


class _FakeJsonClient:
    def __init__(self, response=None, error=None):
        self.response = response or {}
        self.error = error
        self.calls = []

    def complete_json(
        self, role, system, user, ledger=None, max_tokens=None
    ):
        self.calls.append(
            {
                "role": role,
                "system": system,
                "user": user,
                "ledger": ledger,
                "max_tokens": max_tokens,
            }
        )
        if self.error is not None:
            raise self.error
        return dict(self.response)


class VannaRetrieverOnlyTests(unittest.TestCase):
    def setUp(self):
        _FakeBackend.stores = {}
        _FakeBackend.run_sql_calls = 0

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
            self.assertEqual(result.question_sql[0]["evidence_id"], "example:count")
            self.assertNotIn("EVO_EVIDENCE_ID", result.ddl[0])

            schema_only = retriever.retrieve(
                "强烈岩爆数量", include_question_sql=False
            )
            self.assertEqual(schema_only.question_sql, ())
            self.assertNotIn("example:count", schema_only.evidence_ids)

            examples_only = retriever.retrieve(
                "强烈岩爆数量",
                include_ddl=False,
                include_documentation=False,
            )
            self.assertEqual(examples_only.ddl, ())
            self.assertEqual(examples_only.documentation, ())
            self.assertEqual(examples_only.evidence_ids, ("example:count",))

    def test_missing_index_falls_back_without_importing_vanna(self):
        with tempfile.TemporaryDirectory() as directory:
            retriever = VannaRetrieverOnly(
                Path(directory),
                "missing",
                enabled=True,
                backend_factory=_FakeBackend,
            )
            result = retriever.retrieve("任意问题")
            self.assertEqual(result.backend, "vanna-corpus-lexical-fallback")
            self.assertEqual(result.evidence_ids, ())

    def test_request_scoped_draft_uses_frozen_retrieval_and_never_executes(self):
        with tempfile.TemporaryDirectory() as directory:
            retriever = VannaRetrieverOnly(
                Path(directory),
                "stable-v1",
                enabled=True,
                backend_factory=_FakeBackend,
            )
            retriever.build(self.rows(), "snapshot-1")
            source = next(iter(_FakeBackend.stores.values()))
            _FakeBackend.stores[str(retriever.index_path)] = source
            frozen = retriever.retrieve("强烈岩爆数量")
            client = _FakeJsonClient(
                {
                    "sql": "SELECT COUNT(*) FROM cases",
                    "notes": "Use the retrieved cases DDL and confirmed example.",
                }
            )

            result = VannaDraftGenerator().generate(
                "强烈岩爆数量",
                frozen,
                {"keyword_tables": ["cases"], "keyword_columns": ["cases.id"]},
                client,
            )

            self.assertEqual(result.status, "generated")
            self.assertEqual(result.sql, "SELECT COUNT(*) FROM cases")
            self.assertTrue(result.generation_attempted)
            self.assertFalse(result.sql_execution_attempted)
            self.assertEqual(_FakeBackend.run_sql_calls, 0)
            self.assertEqual(len(client.calls), 1)
            request = json.loads(client.calls[0]["user"])
            context = request["frozen_context"]
            self.assertEqual(context["index_version"], "stable-v1")
            self.assertIn('CREATE TABLE "cases"', context["ddl"][0])
            self.assertIn("强烈表示等级为强烈", context["documentation"][0])
            self.assertEqual(
                context["question_sql"][0]["sql"],
                "SELECT COUNT(*) FROM cases",
            )
            self.assertIn("keyword_tables", context["forward_context_json"])
            self.assertEqual(
                result.as_dict()["contract"], "VannaDraftResult/v1"
            )
            # The retrieval-only object remains incapable of generation or execution.
            self.assertFalse(hasattr(retriever, "generate_sql"))
            self.assertFalse(hasattr(retriever, "run_sql"))

    def test_draft_model_failure_degrades_to_structured_empty_result(self):
        retrieval = self._frozen_retrieval()
        client = _FakeJsonClient(error=RuntimeError("provider unavailable"))

        result = VannaDraftGenerator().generate(
            "统计案例数", retrieval, {}, client
        )

        self.assertEqual(result.status, "fallback")
        self.assertEqual(result.sql, "")
        self.assertEqual(result.error_code, "draft_model_failure")
        self.assertIn("provider unavailable", result.error)
        self.assertTrue(result.generation_attempted)
        self.assertFalse(result.sql_execution_attempted)

    def test_non_select_or_multiple_statement_draft_is_rejected_without_execution(self):
        retrieval = self._frozen_retrieval()
        for sql in (
            "DELETE FROM cases",
            "SELECT id FROM cases; SELECT COUNT(*) FROM cases",
        ):
            with self.subTest(sql=sql):
                client = _FakeJsonClient({"sql": sql, "notes": "bad draft"})
                result = VannaDraftGenerator().generate(
                    "统计案例数", retrieval, {}, client
                )
                self.assertEqual(result.status, "fallback")
                self.assertEqual(result.sql, "")
                self.assertTrue(result.error_code)
                self.assertFalse(result.sql_execution_attempted)
        self.assertEqual(_FakeBackend.run_sql_calls, 0)

    def test_empty_frozen_context_falls_back_without_calling_model(self):
        from evoagent.text2sql.vanna_retriever import VannaRetrieval

        client = _FakeJsonClient({"sql": "SELECT 1"})
        result = VannaDraftGenerator().generate(
            "任意问题",
            VannaRetrieval(
                index_version="missing",
                backend="vanna-corpus-lexical-fallback",
            ),
            {},
            client,
        )
        self.assertEqual(result.status, "fallback")
        self.assertEqual(result.error_code, "empty_frozen_context")
        self.assertFalse(result.generation_attempted)
        self.assertEqual(client.calls, [])

    @staticmethod
    def _frozen_retrieval():
        from evoagent.text2sql.vanna_retriever import VannaRetrieval

        return VannaRetrieval(
            evidence_ids=("db:cases",),
            ddl=('CREATE TABLE "cases" ("id" INTEGER);',),
            documentation=("案例表",),
            question_sql=(),
            index_version="stable-v1",
        )


if __name__ == "__main__":
    unittest.main()
