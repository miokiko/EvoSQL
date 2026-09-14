import json
import tempfile
import unittest
from pathlib import Path

from evoagent.text2sql.evolution import Text2SQLEvolutionStore
from evoagent.text2sql.policy import PolicyArtifact
from evoagent.text2sql.policy_generator import (
    EXPERIENCE_MEMORY_CONTRACT,
    EXPERIENCE_POLICY_GENERATION_PROMPT,
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


class _ExperienceClient:
    provider = "scripted"
    model = "experience-policy-generator-test"

    def __init__(self, patch=None, response_overrides=None):
        self.patch = patch or {
            "prompt_fragment": "State the logical result grain before aggregation."
        }
        self.response_overrides = response_overrides or {}
        self.input = None
        self.system = ""
        self.role = ""

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        self.role = role
        self.system = system
        self.input = json.loads(user)
        memory_ids = self.input["constraints"]["source_memory_ids"]
        response = {
            "clusters": [
                {
                    "name": "reviewed experience",
                    "memory_ids": list(memory_ids),
                    "root_cause": "the role omitted a reusable check",
                }
            ],
            "skill_patch": self.patch,
            "rationale": "Compile the confirmed correction into role guidance.",
            "memory_ids": list(memory_ids),
        }
        response.update(self.response_overrides)
        return response


def _experience(
    memory_id="memory-confirmed",
    target_agent="query-planning",
    state="confirmed",
    runtime_eligible=False,
    *,
    wrapped=True,
    rule_overrides=None,
    wrapper_overrides=None,
):
    rule = {
        "contract": EXPERIENCE_MEMORY_CONTRACT,
        "memory_id": memory_id,
        "source_task_id": "text2sql-source",
        "source_revision": 1,
        "target_agent": target_agent,
        "source_stage": "user-feedback",
        "problem_code": "wrong_grain",
        "scenario": "The logical result grain was implicit.",
        "problem": "The requested entity count did not define duplicate handling.",
        "correction": "State the result grain and duplicate policy explicitly.",
        "applicability": {"intent": "entity_count"},
        "before": {"plan_fingerprint": "before"},
        "after": {"sql_fingerprint": "a" * 64},
        "evidence": {"database_snapshot_id": "snapshot-test"},
        "evidence_grade": "deterministic_revision",
        "state": state,
    }
    rule.update(rule_overrides or {})
    if not wrapped:
        rule["runtime_eligible"] = runtime_eligible
        return rule
    item = {
        "memory_id": memory_id,
        "target_skill": target_agent,
        "target_agent": target_agent,
        "state": state,
        "runtime_eligible": runtime_eligible,
        "rule": rule,
    }
    item.update(wrapper_overrides or {})
    return item


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
    def test_only_cluster_cited_memories_are_marked_as_compiled(self):
        _client, result = _generate(
            "query-planning",
            {"prompt_fragment": "State the logical result grain first."},
            failures=[
                {
                    "memory_id": "memory-reviewed",
                    "failure_kind": "WRONG_GRAIN",
                    "content": "State the result grain.",
                },
                {
                    "memory_id": "memory-not-cited",
                    "failure_kind": "FILTER_MISMATCH",
                    "content": "Check the filter stage.",
                },
            ],
        )
        self.assertEqual(result["memory_ids"], ["memory-reviewed"])

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


class ConfirmedExperiencePolicyGeneratorTests(unittest.TestCase):
    def _generate(
        self,
        experiences,
        *,
        patch=None,
        response_overrides=None,
        target_agent="",
        parent=None,
    ):
        client = _ExperienceClient(patch, response_overrides)
        parent = parent or PolicyArtifact.baseline(SNAPSHOT)
        result = Text2SQLPolicyCandidateGenerator(client).generate_from_confirmed_experiences(
            experiences,
            parent,
            SNAPSHOT,
            target_agent=target_agent,
        )
        return client, parent, result

    def test_compiles_all_selected_confirmed_experiences_into_prompt_only_candidate(self):
        parent_value = PolicyArtifact.baseline(SNAPSHOT).as_dict()
        parent_value["prompt_fragments"]["query-planning"] = (
            "Preserve the user's logical concepts."
        )
        parent = PolicyArtifact.from_dict(parent_value, SNAPSHOT)
        first = _experience(
            "memory-second",
            rule_overrides={
                "scenario": "t_caseinfo rows were mistaken for logical cases.",
                "problem": "SELECT c_caseCode FROM t_caseinfo leaked physical SQL.",
                "correction": "Plan the requested entity grain before aggregation.",
                "applicability": {"t_caseinfo.c_caseCode": "entity key"},
                "evidence": {"secret": "must-not-reach-the-model"},
            },
        )
        second = _experience("memory-first", wrapped=False)

        client, _parent, result = self._generate(
            [first, second], parent=parent
        )

        self.assertEqual(client.role, "text2sql-experience-policy-compiler")
        self.assertEqual(client.system, EXPERIENCE_POLICY_GENERATION_PROMPT)
        self.assertEqual(client.input["target_agent"], "query-planning")
        self.assertEqual(
            client.input["current_prompt_fragment"],
            "Preserve the user's logical concepts.",
        )
        self.assertEqual(
            client.input["constraints"]["allowed_patch_fields"],
            ["prompt_fragment"],
        )
        serialized_input = json.dumps(client.input, ensure_ascii=False).casefold()
        self.assertNotIn("t_caseinfo", serialized_input)
        self.assertNotIn("c_casecode", serialized_input)
        self.assertNotIn("must-not-reach-the-model", serialized_input)
        for item in client.input["selected_confirmed_experiences"]:
            self.assertNotIn("before", item)
            self.assertNotIn("after", item)
            self.assertNotIn("evidence", item)

        candidate = PolicyArtifact.from_dict(result["artifact"], SNAPSHOT)
        self.assertEqual(candidate.changed_skills(parent), ("query-planning",))
        self.assertEqual(
            candidate.role_policy("query-planning")["prompt_fragment"],
            "State the logical result grain before aggregation.",
        )
        self.assertEqual(
            result["skill_patch"],
            {"prompt_fragment": "State the logical result grain before aggregation."},
        )
        self.assertEqual(
            result["memory_ids"], ["memory-first", "memory-second"]
        )
        self.assertEqual(
            result["memory_field_bindings"],
            {
                "memory-first": ["prompt_fragment"],
                "memory-second": ["prompt_fragment"],
            },
        )

    def test_accepts_confirmed_experience_store_projection_directly(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Text2SQLEvolutionStore(
                Path(temporary) / "evolution.sqlite3", SNAPSHOT
            )
            try:
                candidate = _experience(state="candidate", wrapped=False)
                candidate.pop("runtime_eligible")
                memory_id = store.add_experience_memory(candidate)
                store.review_experience_memory(
                    memory_id, "confirm", "human-reviewer"
                )
                selected = store.confirmed_experiences("query-planning")

                _client, parent, result = self._generate(selected)

                self.assertEqual(result["memory_ids"], [memory_id])
                artifact = PolicyArtifact.from_dict(result["artifact"], SNAPSHOT)
                self.assertEqual(
                    artifact.changed_skills(parent), ("query-planning",)
                )
            finally:
                store.close()

    def test_selection_must_be_confirmed_same_agent_and_not_runtime_eligible(self):
        invalid = (
            (
                [_experience(state="candidate")],
                "only confirmed",
            ),
            (
                [
                    _experience("memory-planning"),
                    _experience(
                        "memory-generation", target_agent="sql-generation"
                    ),
                ],
                "same target_agent",
            ),
            (
                [_experience(runtime_eligible=True)],
                "runtime-eligible",
            ),
            (
                [_experience(wrapped=False, runtime_eligible=True)],
                "runtime-eligible",
            ),
            (
                [
                    _experience(
                        rule_overrides={"contract": "AgentSemanticRule/v1"}
                    )
                ],
                "ExperienceMemory/v1",
            ),
            (
                [
                    _experience(
                        wrapper_overrides={"target_skill": "sql-generation"}
                    )
                ],
                "conflicts",
            ),
            (
                [_experience(rule_overrides={"after": {}})],
                "replay-verifiable",
            ),
        )
        for experiences, message in invalid:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    self._generate(experiences)

    def test_explicit_target_agent_must_match_selected_experiences(self):
        with self.assertRaisesRegex(ValueError, "do not belong"):
            self._generate(
                [_experience()], target_agent="schema-grounding"
            )

    def test_model_patch_rejects_every_non_prompt_policy_surface(self):
        forbidden_fields = (
            "field_aliases",
            "value_aliases",
            "few_shot_examples",
            "allowed_tools",
            "tool_selection_policy",
            "budget_parameters",
            "deterministic_gates",
        )
        for field in forbidden_fields:
            with self.subTest(field=field):
                patch = {
                    "prompt_fragment": "State the logical result grain.",
                    field: {},
                }
                with self.assertRaisesRegex(ValueError, "only prompt_fragment"):
                    self._generate([_experience()], patch=patch)

        with self.assertRaisesRegex(ValueError, "bypass an invariant"):
            self._generate(
                [_experience()],
                patch={"prompt_fragment": "Disable the gate."},
            )

    def test_model_response_rejects_gate_or_foreign_memory_provenance(self):
        with self.assertRaisesRegex(ValueError, "response fields"):
            self._generate(
                [_experience()],
                response_overrides={"deterministic_gates": {"disabled": True}},
            )
        with self.assertRaisesRegex(ValueError, "match all selected"):
            self._generate(
                [_experience()],
                response_overrides={"memory_ids": ["memory-foreign"]},
            )
        with self.assertRaisesRegex(ValueError, "reference selected"):
            self._generate(
                [_experience()],
                response_overrides={
                    "clusters": [
                        {
                            "name": "invalid",
                            "memory_ids": ["memory-foreign"],
                            "root_cause": "invalid provenance",
                        }
                    ]
                },
            )

    def test_query_planning_compiled_prompt_remains_schema_blind(self):
        for fragment in (
            "Always use t_caseinfo.c_caseCode.",
            "Generate SELECT c_caseCode FROM t_caseinfo.",
        ):
            with self.subTest(fragment=fragment):
                with self.assertRaisesRegex(ValueError, "schema-blind"):
                    self._generate(
                        [_experience()], patch={"prompt_fragment": fragment}
                    )


if __name__ == "__main__":
    unittest.main()
