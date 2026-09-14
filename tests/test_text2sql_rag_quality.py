import copy
import json
import tempfile
import unittest
from pathlib import Path

from evoagent.text2sql.agentic import Text2SQLAgenticEngine
from evoagent.text2sql.business_documents import BusinessDocument, validate_and_chunk_page
from evoagent.text2sql.vanna_corpus import VannaCorpus, collect_vanna_corpus


SNAPSHOT = {
    "snapshot_id": "quality-test",
    "tables": [{
        "name": "t_cases", "row_count": 6, "primary_key": [], "comment": "",
        "columns": [{
            "name": "c_kind", "column_type": "TEXT", "nullable": True,
            "comment": "错误的支护材料解释",
            "profile": {"null_count": 1, "distinct_count": 3, "max_length": 8,
                        "low_cardinality_values": ["", "砂岩、粉砂岩", "钢拱架/TH梁"]},
        }],
    }],
}


class RAGQualityTests(unittest.TestCase):
    def page(self, content, **metadata):
        return BusinessDocument("案例范围", content, {
            "knowledge_type": "business_glossary",
            "database_snapshot_id": SNAPSHOT["snapshot_id"],
            "knowledge_status": "candidate", **metadata,
        })

    def engine(self):
        engine = Text2SQLAgenticEngine.__new__(Text2SQLAgenticEngine)
        engine._physical_identifiers = {"t_cases", "c_kind", "t_cases.c_kind"}
        return engine

    def test_business_projection_survives_collection_and_role_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "business"
            root.mkdir()
            (root / "cases.md").write_text(
                "---\npage_id: cases\nknowledge_type: business_glossary\n"
                "business_kind: metric\nknowledge_status: candidate\n"
                "database_snapshot_id: quality-test\n---\n# 案例范围\n"
                "<!-- planning -->\n主档范围和明细范围必须区分。\n<!-- /planning -->\n"
                "数据绑定：t_cases.c_kind。"
            )
            corpus = collect_vanna_corpus(SNAPSHOT, business_root=root)
            document = next(i for i in corpus["items"] if i["source_kind"] == "business_document")
            evidence = VannaCorpus._evidence(document).as_dict()
            self.assertIn("t_cases.c_kind", evidence["content"])
            self.assertIn("t_cases.c_kind", evidence["dependencies"])
            visible = self.engine()._schema_blind_business_evidence([evidence])
            self.assertEqual(len(visible), 1)
            self.assertEqual(visible[0]["evidence_id"], evidence["evidence_id"])
            self.assertEqual(visible[0]["knowledge_status"], "candidate")
            self.assertIn("主档范围和明细范围必须区分", visible[0]["content"])
            self.assertNotIn("t_cases", json.dumps(visible))
            self.assertNotIn("dependencies", visible[0])

    def test_build_rejects_physical_names_sql_and_malformed_projections(self):
        for prose, error in [
            ("使用 t_cases.c_kind", "planning_schema_leak"),
            ("使用 c_kind", "planning_schema_leak"),
            ("SELECT 1", "planning_sql_leak"),
            ("", "invalid_planning_content"),
        ]:
            with self.subTest(prose=prose):
                result = validate_and_chunk_page(
                    self.page("<!-- planning -->\n"+prose+"\n<!-- /planning -->"), SNAPSHOT)
                self.assertTrue(any(e.startswith(error) for e in result.errors))
        result = validate_and_chunk_page(self.page("<!-- planning --> 未闭合"), SNAPSHOT)
        self.assertTrue(any(e.startswith("invalid_planning_block") for e in result.errors))

    def test_runtime_rechecks_projection_and_keeps_legacy_boundary(self):
        engine = self.engine()
        base = {"evidence_id": "doc:test", "knowledge_type": "business_glossary",
                "title": "案例范围", "content": "字段 t_cases.c_kind"}
        for projection in ("", "t_cases.c_kind", "SELECT 1"):
            self.assertEqual(engine._schema_blind_business_evidence(
                [{**base, "planning_content": projection}]), [])
        self.assertEqual(len(engine._schema_blind_business_evidence(
            [{**base, "content": "区分案例与明细"}])), 1)

    def write_annotations(self, root, **changes):
        payload = {"contract": "evoagent-schema-annotations-v1",
                   "database_snapshot_id": "quality-test",
                   "columns": {"t_cases.c_kind": {"status": "unreliable", "reason": "注释来源有误"}}}
        payload.update(changes)
        (root.parent / "schema_annotations.json").write_text(json.dumps(payload))

    def test_unreliable_comments_are_not_searchable_but_provenance_survives(self):
        original = copy.deepcopy(SNAPSHOT)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/"business"
            root.mkdir()
            clean = collect_vanna_corpus(SNAPSHOT, business_root=root)
            self.write_annotations(root)
            audited = collect_vanna_corpus(SNAPSHOT, business_root=root)
            self.assertNotEqual(clean["index_version"], audited["index_version"])
            for item in audited["items"]:
                if item["knowledge_type"] != "schema":
                    continue
                self.assertNotIn("支护", item["content"])
                self.assertEqual(VannaCorpus._score("支护", item, 1), 0)
                column = item["structured"] if item["item_key"].startswith("column:") else item["structured"]["columns"][0]
                self.assertEqual(column["raw_comment"], "错误的支护材料解释")
                self.assertEqual(column["comment_status"], "unreliable")
            self.assertEqual(SNAPSHOT, original)

    def test_annotation_drift_and_unknown_columns_fail_the_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/"business"
            root.mkdir()
            for change in [
                {"database_snapshot_id": "wrong"},
                {"columns": {"t_cases.missing": {"status": "unreliable", "reason": "missing"}}},
                {"columns": {"t_cases.c_kind": {"status": "confirmed", "reason": "unsupported"}}},
            ]:
                self.write_annotations(root, **change)
                with self.assertRaises(ValueError):
                    collect_vanna_corpus(SNAPSHOT, business_root=root)

    def test_value_text_preserves_category_boundaries_and_missingness(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus = collect_vanna_corpus(SNAPSHOT, business_root=Path(directory)/"business")
            item = next(i for i in corpus["items"] if i["knowledge_type"] == "value")
            encoded = item["content"].split("：", 1)[1].split("。NULL 行数", 1)[0]
            self.assertEqual(json.loads(encoded), ["", "砂岩、粉砂岩", "钢拱架/TH梁"])
            self.assertIn("NULL 行数：1", item["content"])

    def test_unknown_status_is_rejected_and_missing_status_is_unreviewed(self):
        bad = validate_and_chunk_page(self.page("案例范围", knowledge_status="approved-ish"), SNAPSHOT)
        self.assertIn("invalid_knowledge_status", bad.errors)
        page = self.page("案例范围")
        result = validate_and_chunk_page(BusinessDocument(page.title, page.content,
            {k:v for k,v in page.metadata.items() if k != "knowledge_status"}), SNAPSHOT)
        self.assertEqual(result.chunks[0].knowledge_status, "unreviewed")

    def test_section_status_preserves_fact_inference_and_default_boundaries(self):
        body = "# 事实\n案例记录。\n# 解释\n<!-- planning -->埋深是推断。<!-- /planning -->\n# 口径\n以主档为范围。"
        statuses = {"事实": "observed", "解释": "inferred", "口径": "project_convention"}
        result = validate_and_chunk_page(self.page(body, section_status=statuses), SNAPSHOT)
        self.assertFalse(result.errors)
        self.assertEqual([c.knowledge_status for c in result.chunks], list(statuses.values()))
        self.assertIn("推断", result.chunks[1].planning_content)
        for invalid in ([], {"不存在的标题": "observed"}, {"事实": "made_up"}):
            with self.subTest(invalid=invalid):
                result = validate_and_chunk_page(self.page(body, section_status=invalid), SNAPSHOT)
                self.assertTrue(any(e.startswith("invalid_section_status") for e in result.errors))

    def test_interpretations_are_searchable_versioned_and_keep_raw_ddl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/"business"
            root.mkdir()
            original = copy.deepcopy(SNAPSHOT)
            versions = []
            for status in ("corrected", "inferred"):
                annotation = {"status": status, "comment": "支护类型", "reason": "依据实际值纠正",
                              "evidence": "已检查当前快照的原始类别"}
                self.write_annotations(root, columns={"t_cases.c_kind": annotation})
                corpus = collect_vanna_corpus(SNAPSHOT, business_root=root)
                versions.append(corpus["index_version"])
                col = next(i for i in corpus["items"] if i["item_key"] == "column:t_cases.c_kind")
                self.assertIn("支护类型", col["content"])
                self.assertEqual("推断解释" in col["content"], status == "inferred")
                self.assertEqual(col["structured"]["raw_comment"], original["tables"][0]["columns"][0]["comment"])
                annotation.pop("evidence")
                self.write_annotations(root, columns={"t_cases.c_kind": annotation})
                with self.assertRaises(ValueError):
                    collect_vanna_corpus(SNAPSHOT, business_root=root)
            self.assertNotEqual(*versions)
            self.assertEqual(SNAPSHOT, original)

    def test_join_coverage_notes_reach_retrieval_and_invalidate_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/"business"
            relationship = {"candidate_id": "reviewed", "decision": "approved",
                            "left": "t_cases.c_kind", "right": "t_cases.c_kind",
                            "notes": "仅统计主档范围，孤立记录另计"}
            catalog = {"relationships": [relationship]}
            before = collect_vanna_corpus(SNAPSHOT, business_root=root, join_catalog=catalog)
            item = next(i for i in before["items"] if i["knowledge_type"] == "relationship")
            self.assertIn(relationship["notes"], item["content"])
            relationship["notes"] = "覆盖前提改变，需使用新说明"
            after = collect_vanna_corpus(SNAPSHOT, business_root=root, join_catalog=catalog)
            self.assertNotEqual(before["index_version"], after["index_version"])
