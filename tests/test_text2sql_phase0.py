import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import yaml

from evoagent.text2sql.knowledge_policy import KnowledgeAssertion, QueryVersionPin, resolve_authority
from evoagent.text2sql.schema_catalog import build_snapshot_from_dump, write_snapshot_artifacts
from evoagent.text2sql.sqlite_database import build_sqlite_database, open_readonly


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DUMP_PATH = PROJECT_ROOT / "database" / "test1_full_20241118.sql"


class SchemaSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.artifacts = build_snapshot_from_dump(DUMP_PATH)

    def test_copied_dump_has_expected_fingerprint_and_shape(self):
        snapshot = self.artifacts.snapshot
        self.assertEqual(
            snapshot["source"]["dump_sha256"],
            "f4fff88ef9ba98c368bbe2d9f7daf34d9e943c22a1d344596e4c604bc1ad39c3",
        )
        self.assertEqual(snapshot["table_count"], 20)
        self.assertEqual(snapshot["row_count"], 562)
        self.assertEqual(sum(len(table["columns"]) for table in snapshot["tables"]), 273)

    def test_snapshot_id_is_deterministic(self):
        second = build_snapshot_from_dump(DUMP_PATH)
        self.assertEqual(
            self.artifacts.snapshot["snapshot_id"], second.snapshot["snapshot_id"]
        )
        self.assertEqual(
            self.artifacts.snapshot["schema_fingerprint"],
            second.snapshot["schema_fingerprint"],
        )

    def test_case_code_join_is_only_a_candidate_with_overlap_evidence(self):
        relationship = next(
            item
            for item in self.artifacts.join_candidates
            if {item["left"], item["right"]}
            == {"t_caseinfo.c_caseCode", "t_casedesc.c_caseCode"}
        )
        self.assertGreater(relationship["data_overlap"]["intersection_count"], 0)
        self.assertIn("matching_exact_column_name", relationship["basis"])

        with tempfile.TemporaryDirectory() as directory:
            paths = write_snapshot_artifacts(self.artifacts, Path(directory))
            review = json.loads(paths["join_review"].read_text(encoding="utf-8"))
        reviewed_relationship = next(
            item
            for item in review["relationships"]
            if item["candidate_id"] == relationship["candidate_id"]
        )
        self.assertEqual(review["status"], "pending_review")
        self.assertEqual(reviewed_relationship["decision"], "pending")


class KnowledgeAuthorityTests(unittest.TestCase):
    def test_database_wins_physical_fact(self):
        assertions = [
            KnowledgeAssertion(
                "db:1", "database", "physical", "table.column.type", "varchar(20)", "dbs_1"
            ),
            KnowledgeAssertion(
                "wiki:1", "wiki", "physical", "table.column.type", "integer", "wiki_1"
            ),
            KnowledgeAssertion(
                "memory:1", "memory", "physical", "table.column.type", "date", "mem_1"
            ),
        ]
        decision = resolve_authority("physical", assertions)
        self.assertEqual(decision.status, "resolved")
        self.assertEqual(decision.assertion.evidence_id, "db:1")

    def test_conflicting_stable_wiki_definitions_fail_closed(self):
        assertions = [
            KnowledgeAssertion("wiki:1", "wiki", "business", "严重", "强烈", "wiki_1"),
            KnowledgeAssertion("wiki:2", "wiki", "business", "严重", "极强", "wiki_2"),
        ]
        decision = resolve_authority("business", assertions)
        self.assertEqual(decision.status, "knowledge_conflict")
        self.assertIsNone(decision.assertion)

    def test_all_four_versions_are_required(self):
        pin = QueryVersionPin("dbs_1", "wiki_1", "memory_1", "policy_1")
        self.assertEqual(pin.policy_version, "policy_1")
        with self.assertRaises(ValueError):
            QueryVersionPin("dbs_1", "", "memory_1", "policy_1")


class IsolationConfigurationTests(unittest.TestCase):
    def test_compose_imports_only_copied_dump_and_revokes_write_access(self):
        compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        service = compose["services"]["text2sql-mysql"]
        self.assertEqual(service["environment"]["MYSQL_DATABASE"], "evo_text2sql_eval")
        self.assertEqual(service["environment"]["MYSQL_USER"], "evo_text2sql_ro")
        mounts = "\n".join(service["volumes"])
        self.assertIn("database/test1_full_20241118.sql", mounts)
        self.assertIn("database/init/99-readonly.sql", mounts)

        grant_sql = (PROJECT_ROOT / "database" / "init" / "99-readonly.sql").read_text(
            encoding="utf-8"
        )
        self.assertIn("REVOKE ALL PRIVILEGES", grant_sql)
        self.assertIn("GRANT SELECT, SHOW VIEW", grant_sql)
        self.assertNotIn("GRANT INSERT", grant_sql)


class SQLiteEvaluationDatabaseTests(unittest.TestCase):
    def test_dump_converts_to_complete_readonly_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "eval.sqlite3"
            result = build_sqlite_database(DUMP_PATH, database_path)
            self.assertEqual(result.table_count, 20)
            self.assertEqual(result.row_count, 562)
            self.assertEqual(database_path.stat().st_mode & 0o777, 0o444)

            connection = open_readonly(database_path)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM t_caseinfo").fetchone()[0], 39
                )
                levels = dict(
                    connection.execute(
                        "SELECT c_rockLevel, COUNT(*) FROM t_casedesc GROUP BY c_rockLevel"
                    )
                )
                self.assertEqual(levels["轻微"], 17)
                self.assertEqual(levels["强烈"], 6)
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM t_casetimeinfo WHERE c_evpPath IS NOT NULL"
                    ).fetchone()[0],
                    15,
                )
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("DELETE FROM t_caseinfo")
            finally:
                connection.close()

            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["source_dump_sha256"], self.artifact_dump_sha256())
            self.assertIn("mode=ro", manifest["open_mode"])

    @staticmethod
    def artifact_dump_sha256():
        return "f4fff88ef9ba98c368bbe2d9f7daf34d9e943c22a1d344596e4c604bc1ad39c3"


if __name__ == "__main__":
    unittest.main()
