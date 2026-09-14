import unittest
from types import SimpleNamespace

from evoagent.text2sql.agentic import Text2SQLAgenticEngine


class ObservedSchemaCoverageTests(unittest.TestCase):
    def setUp(self):
        self.engine = Text2SQLAgenticEngine.__new__(Text2SQLAgenticEngine)
        self.engine.snapshot = {"snapshot_id": "snapshot-1"}
        self.engine._allowed_columns = {"cases.project_id", "cases.id", "other.id"}
        self.item = {"item_key": "table:cases", "knowledge_type": "schema",
                     "database_snapshot_id": "snapshot-1",
                     "structured": {"name": "cases", "columns": [{"name": "id"}, {"name": "project_id"}]}}
        self.engine.vanna_corpus = SimpleNamespace(raw_item=lambda _: self.item)
        self.evidence = SimpleNamespace(evidence_id="table-evidence", knowledge_type="schema", dependencies=("cases",))

    def test_pinned_observed_table_definition_covers_its_columns(self):
        self.assertTrue(self.engine._observed_schema_covers_column(self.evidence, "cases.project_id"))
        self.assertFalse(self.engine._observed_schema_covers_column(self.evidence, "cases.hallucinated"))
        self.assertFalse(self.engine._observed_schema_covers_column(self.evidence, "other.id"))

    def test_wrong_snapshot_and_non_table_documents_cannot_expand(self):
        self.item["database_snapshot_id"] = "stale"
        self.assertFalse(self.engine._observed_schema_covers_column(self.evidence, "cases.project_id"))
        self.item["database_snapshot_id"] = "snapshot-1"
        self.item["item_key"] = "document:cases"
        self.assertFalse(self.engine._observed_schema_covers_column(self.evidence, "cases.project_id"))

    def test_explicit_column_dependencies_still_work(self):
        self.evidence.dependencies = ("cases.id",)
        self.assertTrue(self.engine._observed_schema_covers_column(self.evidence, "cases.id"))

    def test_revision_names_the_missing_business_concept(self):
        conflicts = [{"owner": "schema-grounding", "code": "missing_schema_binding",
                      "slot_id": "measure:case_count", "logical_name": "案例编号",
                      "message": "logical concept has no explicit SchemaPlan binding"}]
        assignment = {"worker": "schema-grounding", "assignment_id": "schema-1"}
        for requested in ([], [{**assignment, "guidance": "修订绑定"}]):
            revisions = self.engine._binding_revision_requests(conflicts, [assignment], requested)
            self.assertIn("logical_name=案例编号", revisions[0]["guidance"])
            self.assertIn("measure:case_count", revisions[0]["guidance"])
