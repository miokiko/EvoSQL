import unittest

from evoagent.text2sql.sql_safety import validate_sql


SNAPSHOT = {
    "tables": [
        {
            "name": "t_casedesc",
            "columns": [
                {"name": "c_rockLevel"},
                {"name": "c_caseCode"},
            ],
        }
    ]
}


class SQLSafetyProjectionAliasTests(unittest.TestCase):
    def test_order_by_projection_alias_is_not_treated_as_schema_column(self):
        gate = validate_sql(
            "SELECT c_rockLevel, COUNT(*) AS case_count "
            "FROM t_casedesc GROUP BY c_rockLevel ORDER BY case_count DESC",
            SNAPSHOT,
        )

        self.assertTrue(gate.accepted, gate.errors)
        self.assertEqual(gate.columns, ("c_rockLevel",))

    def test_projection_alias_does_not_hide_where_clause_typo(self):
        gate = validate_sql(
            "SELECT c_caseCode AS case_code "
            "FROM t_casedesc WHERE case_code <> ''",
            SNAPSHOT,
        )

        self.assertFalse(gate.accepted)
        self.assertIn("unknown_column:case_code", gate.errors)

    def test_unknown_order_by_alias_remains_rejected(self):
        gate = validate_sql(
            "SELECT c_rockLevel FROM t_casedesc ORDER BY missing_alias",
            SNAPSHOT,
        )

        self.assertFalse(gate.accepted)
        self.assertIn("unknown_column:missing_alias", gate.errors)


if __name__ == "__main__":
    unittest.main()
