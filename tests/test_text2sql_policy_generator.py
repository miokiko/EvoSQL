import json
import unittest
from pathlib import Path

from evoagent.text2sql.policy import PolicyArtifact
from evoagent.text2sql.policy_generator import (
    TEXT2SQL_EVOLUTION_PROMPT,
    Text2SQLPolicyCandidateGenerator,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = json.loads(
    (
        PROJECT_ROOT
        / "artifacts"
        / "text2sql"
        / "schema"
        / "database_snapshot.json"
    ).read_text(encoding="utf-8")
)


class _Client:
    provider = "scripted"
    model = "policy-generator-test"

    def __init__(self, patch):
        self.patch = patch
        self.input = None
        self.system = ""

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        self.system = system
        self.input = json.loads(user)
        return {
            "clusters": [
                {
                    "name": "reviewed failure",
                    "memory_ids": ["memory-reviewed"],
                    "root_cause": "bounded test cause",
                }
            ],
            "skill_patch": self.patch,
            "rationale": "bounded test rationale",
        }


def _generate(target_skill, patch, failures=None):
    client = _Client(patch)
    result = Text2SQLPolicyCandidateGenerator(client).generate(
        failures
        or [
            {
                "memory_id": "memory-reviewed",
                "failure_kind": "AGGREGATION_MISMATCH",
                "content": "The logical result grain was implicit.",
            }
        ],
        PolicyArtifact.baseline(SNAPSHOT),
        target_skill,
        SNAPSHOT,
    )
    return client, result


class PolicyGeneratorRoleBoundaryTests(unittest.TestCase):
    def test_evolution_prompt_declares_role_ownership_and_empty_tool_acl(self):
        self.assertIn(
            "only schema-grounding may own non-empty field_aliases or value_aliases",
            TEXT2SQL_EVOLUTION_PROMPT,
        )
        self.assertIn(
            "only sql-generation may own non-empty SQL few_shot_examples",
            TEXT2SQL_EVOLUTION_PROMPT,
        )
        self.assertIn("query-planning is schema-blind", TEXT2SQL_EVOLUTION_PROMPT)
        self.assertIn(
            "query-planning, sql-generation, and text2sql-critic have an empty maximum Tool ACL",
            TEXT2SQL_EVOLUTION_PROMPT,
        )

    def test_query_planning_input_and_candidate_remain_schema_blind(self):
        client, result = _generate(
            "query-planning",
            {"prompt_fragment": "State the logical result grain before aggregation."},
            failures=[
                {
                    "memory_id": "memory-reviewed",
                    "failure_kind": "WRONG_GRAIN",
                    "content": (
                        "SELECT c_caseCode FROM t_caseinfo exposed a physical plan."
                    ),
                }
            ],
        )

        self.assertNotIn("schema_columns", client.input)
        self.assertEqual(client.input["current_role_tools"], [])
        self.assertEqual(
            client.input["target_role_contract"],
            {
                "schema_visibility": "business-semantics-only",
                "may_change_field_aliases": False,
                "may_change_value_aliases": False,
                "may_change_sql_few_shot_examples": False,
                "maximum_allowed_tools": [],
            },
        )
        serialized = json.dumps(client.input, ensure_ascii=False).casefold()
        self.assertNotIn("t_caseinfo", serialized)
        self.assertNotIn("c_casecode", serialized)
        self.assertEqual(
            client.input["stable_failure_memory"][0]["content"],
            "[schema-specific SQL evidence redacted]",
        )
        artifact = PolicyArtifact.from_dict(result["artifact"], SNAPSHOT)
        self.assertEqual(
            artifact.changed_skills(PolicyArtifact.baseline(SNAPSHOT)),
            ("query-planning",),
        )

    def test_query_planning_rejects_physical_or_sql_prompt_fragments(self):
        for fragment in (
            "Always use t_caseinfo.c_caseCode.",
            "Generate SELECT c_caseCode FROM some_table.",
            "Return SELECT 1.",
            "Return VALUES (1).",
        ):
            with self.subTest(fragment=fragment):
                with self.assertRaisesRegex(ValueError, "schema-blind"):
                    _generate(
                        "query-planning", {"prompt_fragment": fragment}
                    )

    def test_alias_patches_are_owned_only_by_schema_grounding(self):
        invalid = (
            (
                "query-planning",
                {"field_aliases": {"案例编号": "t_caseinfo.c_caseCode"}},
                "field_aliases",
            ),
            (
                "text2sql-lead",
                {
                    "value_aliases": {
                        "强烈": {"column": "t_caseinfo.c_level", "value": "强烈"}
                    }
                },
                "value_aliases",
            ),
        )
        for role, patch, message in invalid:
            with self.subTest(role=role):
                with self.assertRaisesRegex(ValueError, message):
                    _generate(role, patch)

        client, result = _generate(
            "schema-grounding",
            {"field_aliases": {"案例编号": "t_caseinfo.c_caseCode"}},
        )
        self.assertIn("schema_columns", client.input)
        self.assertEqual(
            result["artifact"]["field_aliases"]["schema-grounding"],
            {"案例编号": "t_caseinfo.c_caseCode"},
        )

    def test_sql_few_shots_are_owned_only_by_sql_generation(self):
        example = {
            "question": "列出一个案例编号",
            "sql": "SELECT c_caseCode FROM t_caseinfo LIMIT 1",
        }
        with self.assertRaisesRegex(ValueError, "sql-generation"):
            _generate("schema-grounding", {"few_shot_examples": [example]})

        client, result = _generate(
            "sql-generation", {"few_shot_examples": [example]}
        )
        self.assertIn("schema_columns", client.input)
        self.assertEqual(client.input["current_role_tools"], [])
        self.assertEqual(
            result["artifact"]["few_shot_examples"]["sql-generation"],
            [example],
        )

    def test_reasoning_roles_cannot_add_tools_to_empty_maximum_acl(self):
        for role in (
            "query-planning",
            "sql-generation",
            "text2sql-critic",
        ):
            with self.subTest(role=role):
                with self.assertRaisesRegex(ValueError, "empty maximum Tool ACL"):
                    _generate(role, {"allowed_tools": ["validate_sql"]})


if __name__ == "__main__":
    unittest.main()
