import hashlib
import json
import unittest
from pathlib import Path

from evoagent.text2sql.database_tools import (
    LEGACY_RUNTIME_ROLE_ALIASES,
    ROLE_TOOL_PERMISSIONS,
    Text2SQLToolSuite,
)
from evoagent.text2sql.memory_attribution import attribute_query_failure
from evoagent.text2sql.policy import (
    POLICY_CONTRACT_VERSION,
    TEXT2SQL_SKILLS,
    PolicyArtifact,
    require_single_skill_change,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = json.loads(
    (PROJECT_ROOT / "artifacts" / "text2sql" / "schema" / "database_snapshot.json").read_text(
        encoding="utf-8"
    )
)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class PolicyRoleMigrationTests(unittest.TestCase):
    def test_only_five_model_roles_are_evolvable(self):
        self.assertEqual(
            TEXT2SQL_SKILLS,
            (
                "text2sql-lead",
                "schema-grounding",
                "query-planning",
                "sql-generation",
                "text2sql-critic",
            ),
        )
        self.assertNotIn("text2sql-harness", TEXT2SQL_SKILLS)
        artifact = PolicyArtifact.baseline(SNAPSHOT).as_dict()
        self.assertEqual(artifact["contract_version"], POLICY_CONTRACT_VERSION)
        for field in (
            "prompt_fragments",
            "field_aliases",
            "value_aliases",
            "few_shot_examples",
            "tool_selection_policy",
            "budget_parameters",
        ):
            self.assertEqual(set(artifact[field]), set(TEXT2SQL_SKILLS))

    def test_persisted_query_planning_prompt_rejects_schema_free_sql(self):
        baseline = PolicyArtifact.baseline(SNAPSHOT).as_dict()
        for fragment in (
            "SELECT 1",
            "Return SELECT lower('x') as the plan.",
            "VALUES (1)",
        ):
            with self.subTest(fragment=fragment):
                candidate = json.loads(json.dumps(baseline, ensure_ascii=False))
                candidate["prompt_fragments"]["query-planning"] = fragment
                with self.assertRaisesRegex(ValueError, "schema-blind"):
                    PolicyArtifact.from_dict(candidate, SNAPSHOT)

    def test_v1_strategy_policy_is_deterministically_split_and_keeps_old_hash(self):
        baseline = PolicyArtifact.baseline(SNAPSHOT).as_dict()
        legacy = {"contract_version": "text2sql-policy-v1"}
        for field in (
            "prompt_fragments",
            "field_aliases",
            "value_aliases",
            "few_shot_examples",
            "tool_selection_policy",
            "budget_parameters",
        ):
            legacy[field] = {
                "text2sql-lead": baseline[field]["text2sql-lead"],
                "schema-grounding": baseline[field]["schema-grounding"],
                "sql-strategy": baseline[field]["query-planning"],
                "text2sql-critic": baseline[field]["text2sql-critic"],
            }
        legacy["prompt_fragments"]["sql-strategy"] = "State the result grain."
        legacy["field_aliases"]["sql-strategy"] = {
            "案例编号": "t_caseinfo.c_caseCode"
        }
        legacy["few_shot_examples"]["sql-strategy"] = [
            {
                "question": "列出一个案例编号",
                "sql": "SELECT c_caseCode FROM t_caseinfo LIMIT 1",
            }
        ]
        legacy["tool_selection_policy"]["sql-strategy"] = [
            "inspect_schema",
            "sample_values",
            "validate_sql",
            "explain_sql",
        ]
        old_version = "policy-%s" % hashlib.sha256(
            _canonical(legacy).encode("utf-8")
        ).hexdigest()[:20]

        migrated = PolicyArtifact.from_dict(legacy, SNAPSHOT)
        value = migrated.as_dict()

        self.assertEqual(migrated.version, old_version)
        self.assertTrue(migrated.was_migrated_from_v1)
        self.assertEqual(value["contract_version"], "text2sql-policy-v2")
        self.assertNotIn("sql-strategy", value["prompt_fragments"])
        self.assertEqual(
            value["prompt_fragments"]["query-planning"], "State the result grain."
        )
        self.assertEqual(
            value["prompt_fragments"]["sql-generation"], "State the result grain."
        )
        self.assertEqual(
            value["field_aliases"]["schema-grounding"],
            {"案例编号": "t_caseinfo.c_caseCode"},
        )
        self.assertEqual(value["field_aliases"]["query-planning"], {})
        self.assertEqual(value["field_aliases"]["sql-generation"], {})
        self.assertEqual(value["few_shot_examples"]["query-planning"], [])
        self.assertEqual(len(value["few_shot_examples"]["sql-generation"]), 1)
        self.assertEqual(
            value["tool_selection_policy"]["query-planning"],
            [],
        )
        self.assertEqual(
            value["tool_selection_policy"]["sql-generation"],
            [],
        )
        self.assertEqual(
            migrated.role_policy("sql-strategy")["skill"], "query-planning"
        )
        with self.assertRaisesRegex(ValueError, "read-only"):
            require_single_skill_change(
                PolicyArtifact.baseline(SNAPSHOT), migrated, "query-planning"
            )


class RoleToolAclTests(unittest.TestCase):
    def test_agent_and_harness_permissions_have_distinct_boundaries(self):
        self.assertEqual(
            ROLE_TOOL_PERMISSIONS["text2sql-lead"],
            {"retrieve_knowledge", "inspect_schema", "sample_values"},
        )
        self.assertEqual(
            ROLE_TOOL_PERMISSIONS["schema-grounding"],
            {"retrieve_knowledge", "inspect_schema", "sample_values"},
        )
        self.assertEqual(
            ROLE_TOOL_PERMISSIONS["query-planning"],
            set(),
        )
        self.assertEqual(
            ROLE_TOOL_PERMISSIONS["sql-generation"],
            set(),
        )
        self.assertEqual(ROLE_TOOL_PERMISSIONS["text2sql-critic"], set())
        self.assertEqual(
            ROLE_TOOL_PERMISSIONS["text2sql-harness"],
            {"validate_sql", "explain_sql", "execute_sql"},
        )
        for role in TEXT2SQL_SKILLS:
            self.assertNotIn("execute_sql", ROLE_TOOL_PERMISSIONS[role])
        self.assertEqual(LEGACY_RUNTIME_ROLE_ALIASES, {"sql-strategy": "query-planning"})

    def test_registry_keeps_a_read_only_legacy_runtime_alias(self):
        suite = object.__new__(Text2SQLToolSuite)
        self.assertEqual(
            suite.registry("sql-strategy").names(),
            suite.registry("query-planning").names(),
        )
        self.assertEqual(
            suite.registry("text2sql-harness").names(),
            ["execute_sql", "explain_sql", "validate_sql"],
        )


class MemoryAttributionRoleTests(unittest.TestCase):
    def test_binding_planning_generation_and_selection_have_separate_owners(self):
        base = {
            "task_id": "task-1",
            "final_sql": "SELECT c_caseCode FROM t_caseinfo LIMIT 1",
            "gates": {"accepted": True, "errors": []},
        }
        schema = attribute_query_failure(
            {**base, "binding_issues": [{"code": "ambiguous_schema_binding"}]},
            SNAPSHOT,
        )
        value = attribute_query_failure(
            {
                **base,
                "binding_conflicts": [{"code": "unverified_value_binding"}],
            },
            SNAPSHOT,
        )
        grain = attribute_query_failure(
            {**base, "binding_conflicts": [{"code": "result_grain_mismatch"}]},
            SNAPSHOT,
        )
        planning = attribute_query_failure(
            base,
            SNAPSHOT,
            feedback_note="Join fanout 导致重复计数和结果粒度错误",
        )
        generation = attribute_query_failure(
            {
                **base,
                "approved_query_plan": {"fingerprint": "bound-plan-1"},
                "gates": {"accepted": False, "errors": ["group_by_mismatch"]},
            },
            SNAPSHOT,
        )
        critic = attribute_query_failure(base, SNAPSHOT)

        self.assertEqual(schema["target_skill"], "schema-grounding")
        self.assertEqual(value["target_skill"], "schema-grounding")
        self.assertEqual(value["failure_kind"], "value_binding_mismatch")
        self.assertEqual(grain["target_skill"], "schema-grounding")
        self.assertEqual(planning["target_skill"], "query-planning")
        self.assertEqual(generation["target_skill"], "sql-generation")
        self.assertEqual(critic["target_skill"], "text2sql-critic")


if __name__ == "__main__":
    unittest.main()
