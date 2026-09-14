import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from evoagent.text2sql.evolution import Text2SQLEvolutionStore
from evoagent.text2sql.memory_attribution import (
    EXPERIENCE_MEMORY_CONTRACT,
    MEMORY_EVIDENCE_CONTRACT,
    MEMORY_RULE_CONTRACT,
    attribute_query_failure,
    decode_memory_payload,
    experience_evidence_sha256,
    experience_memory_fingerprint,
    normalize_experience_memory,
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


class StructuredSemanticMemoryTests(unittest.TestCase):
    @staticmethod
    def _experience(**overrides):
        value = {
            "contract": EXPERIENCE_MEMORY_CONTRACT,
            "source_task_id": "query-run-experience-1",
            "source_revision": 1,
            "target_agent": "sql-generation",
            "source_stage": "candidate-gates",
            "problem_code": "sql_gate_repair",
            "scenario": "ApprovedQueryPlan 已固定，但首轮 SQL 未通过门禁",
            "problem": "首轮 SQL 遗漏计划要求的 DISTINCT",
            "correction": "在同一 ApprovedQueryPlan 下补齐 DISTINCT",
            "applicability": {
                "approved_plan_has_distinct": True,
                "approved_plan_fingerprint": "a" * 64,
                "single_repair": True,
            },
            "before": {
                "sql_fingerprint": "before-fingerprint",
                "gate_codes": ["distinct_mismatch"],
                "gate_accepted": False,
            },
            "after": {
                "sql_fingerprint": "after-fingerprint",
                "gate_accepted": True,
            },
            "evidence": {
                "approved_plan_fingerprint": "a" * 64,
                "database_snapshot_id": SNAPSHOT["snapshot_id"],
            },
            "evidence_grade": "deterministic_repair",
            "state": "candidate",
        }
        value.update(overrides)
        return value

    def test_experience_contract_is_bounded_redacted_and_fingerprinted_by_semantics(self):
        raw = self._experience(
            scenario="修复 password=do-not-store 且 sk-ws-abcdefghijklmnop 不应保留",
            evidence={
                "database_snapshot_id": SNAPSHOT["snapshot_id"],
                "api_key": "do-not-store",
                "note": "authorization=do-not-store",
            },
        )
        normalized = normalize_experience_memory(raw)
        rendered = json.dumps(normalized, ensure_ascii=False).lower()
        self.assertEqual(normalized["contract"], EXPERIENCE_MEMORY_CONTRACT)
        self.assertNotIn("do-not-store", rendered)
        self.assertNotIn("abcdefghijklmnop", rendered)
        self.assertNotIn("api_key", normalized["evidence"])
        self.assertEqual(len(experience_evidence_sha256(normalized)), 64)

        changed_provenance = {
            **normalized,
            "memory_id": "memory-other",
            "source_task_id": "query-run-other",
            "source_revision": 9,
            "state": "confirmed",
        }
        self.assertEqual(
            experience_memory_fingerprint(normalized),
            experience_memory_fingerprint(changed_provenance),
        )
        decoded = decode_memory_payload(
            json.dumps(normalized, ensure_ascii=False),
            memory_id="memory-decoded",
            state="confirmed",
        )
        self.assertEqual(decoded["memory_id"], "memory-decoded")
        self.assertEqual(decoded["state"], "confirmed")

    def test_experience_store_is_idempotent_but_never_merges_distinct_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            with Text2SQLEvolutionStore(
                Path(temporary) / "evolution.sqlite3", SNAPSHOT
            ) as store:
                first = self._experience()
                memory_id = store.add_experience_memory(first)
                self.assertEqual(store.add_experience_memory(first), memory_id)

                conflicting = {
                    **first,
                    "correction": "改成另一种未经新证据支持的修正",
                }
                with self.assertRaisesRegex(ValueError, "same evidence"):
                    store.add_experience_memory(conflicting)

                second = self._experience(
                    before={
                        "sql_fingerprint": "other-before",
                        "gate_codes": ["distinct_mismatch"],
                    }
                )
                second_id = store.add_experience_memory(second)
                self.assertNotEqual(memory_id, second_id)
                items = store.list_memory("candidate")
                self.assertEqual(len(items), 2)
                self.assertTrue(all(item["occurrence_count"] == 1 for item in items))
                self.assertTrue(all(not item["runtime_eligible"] for item in items))
                self.assertNotEqual(items[0]["evidence_sha256"], items[1]["evidence_sha256"])

    def test_confirmed_experience_never_changes_runtime_memory_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            with Text2SQLEvolutionStore(
                Path(temporary) / "evolution.sqlite3", SNAPSHOT
            ) as store:
                before = store.memory_snapshot_id
                memory_id = store.add_experience_memory(self._experience())
                created = store.get_memory(memory_id)
                self.assertEqual(created["target_agent"], "sql-generation")
                self.assertEqual(created["problem_code"], "sql_gate_repair")
                self.assertFalse(created["runtime_eligible"])
                self.assertEqual(created["state_version"], 1)

                confirmed = store.review_experience_memory(
                    memory_id, "confirm", "reviewer"
                )
                self.assertEqual(confirmed["state"], "confirmed")
                self.assertEqual(confirmed["rule"]["state"], "confirmed")
                self.assertEqual(confirmed["state_version"], 2)
                self.assertEqual(before, store.memory_snapshot_id)
                self.assertEqual(store.stable_memory("sql-generation"), ())
                self.assertEqual(
                    [item["memory_id"] for item in store.confirmed_experiences()],
                    [memory_id],
                )
                with self.assertRaisesRegex(ValueError, "cannot enter runtime"):
                    store.evaluation_memory("sql-generation", memory_id)

    def test_confirm_experience_without_replay_proof_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            with Text2SQLEvolutionStore(
                Path(temporary) / "evolution.sqlite3", SNAPSHOT
            ) as store:
                memory_id = store.add_experience_memory(
                    self._experience(
                        applicability={}, after={"gate_accepted": True}
                    )
                )

                with self.assertRaisesRegex(ValueError, "replay-verifiable proof"):
                    store.review_experience_memory(
                        memory_id, "confirm", "reviewer"
                    )

                self.assertEqual(store.get_memory(memory_id)["state"], "candidate")

    def test_experience_needs_evidence_does_not_guess_an_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            with Text2SQLEvolutionStore(
                Path(temporary) / "evolution.sqlite3", SNAPSHOT
            ) as store:
                memory_id = store.add_experience_memory(
                    self._experience(
                        target_agent="",
                        correction="",
                        before={},
                        after={},
                        evidence={},
                        evidence_grade="",
                        state="needs_evidence",
                        problem_code="user_correction_unattributed",
                    )
                )
                item = store.get_memory(memory_id)
                self.assertEqual(item["state"], "needs_evidence")
                self.assertEqual(item["target_agent"], "")
                self.assertEqual(store.confirmed_experiences(), ())
                with self.assertRaisesRegex(ValueError, "not awaiting review"):
                    store.review_experience_memory(
                        memory_id, "confirm", "reviewer"
                    )

    def test_experience_evidence_and_semantics_cannot_be_edited_in_place(self):
        with tempfile.TemporaryDirectory() as temporary:
            with Text2SQLEvolutionStore(
                Path(temporary) / "evolution.sqlite3", SNAPSHOT
            ) as store:
                memory_id = store.add_experience_memory(self._experience())
                item = store.get_memory(memory_id)
                with self.assertRaisesRegex(ValueError, "immutable"):
                    store.update_memory_candidate(
                        memory_id,
                        item["target_skill"],
                        item["failure_kind"],
                        "试图覆盖原证据",
                    )

                oversized = self._experience(
                    evidence={"field-%d" % index: "x" * 3000 for index in range(30)}
                )
                with self.assertRaisesRegex(ValueError, "size limit"):
                    store.add_experience_memory(oversized)

    def test_memory_column_migration_is_idempotent_and_keeps_legacy_runtime_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE memory_items (
                    memory_id TEXT PRIMARY KEY,
                    target_skill TEXT NOT NULL,
                    origin_split TEXT NOT NULL,
                    failure_kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    rule_json TEXT NOT NULL DEFAULT '{}',
                    rule_fingerprint TEXT NOT NULL DEFAULT '',
                    source_case_ids_json TEXT NOT NULL DEFAULT '[]',
                    occurrence_count INTEGER NOT NULL DEFAULT 1,
                    evidence_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    reviewed_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                "INSERT INTO memory_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "memory-legacy-stable",
                    "query-planning",
                    "train",
                    "AGGREGATION_MISMATCH",
                    "先明确结果粒度。",
                    "{}",
                    "",
                    "[]",
                    1,
                    json.dumps({"case_id": "legacy-case"}),
                    "stable",
                    "2026-09-06T00:00:00+00:00",
                    "reviewer",
                    "2026-09-06T00:00:01+00:00",
                ),
            )
            connection.commit()
            connection.close()

            required = {
                "source_task_id",
                "source_stage",
                "source_revision",
                "evidence_sha256",
                "runtime_eligible",
                "state_version",
            }
            for _ in range(2):
                with Text2SQLEvolutionStore(path, SNAPSHOT) as store:
                    columns = {
                        row["name"]
                        for row in store.connection.execute(
                            "PRAGMA table_info(memory_items)"
                        ).fetchall()
                    }
                    self.assertTrue(required.issubset(columns))
                    legacy = store.get_memory("memory-legacy-stable")
                    self.assertEqual(legacy["rule"]["contract"], MEMORY_RULE_CONTRACT)
                    self.assertTrue(legacy["runtime_eligible"])
                    self.assertEqual(
                        store.stable_memory("query-planning")[0]["memory_id"],
                        "memory-legacy-stable",
                    )

    def test_attribution_emits_a_safe_traceable_rule_without_raw_trace_or_sql(self):
        original_sql = "SELECT c_caseCode FROM t_caseinfo LIMIT 1"
        corrected_sql = "SELECT DISTINCT c_caseCode FROM t_caseinfo LIMIT 1"
        attribution = attribute_query_failure(
            {
                "task_id": "query-run-123",
                "status": "success",
                "query_type": "DATA_QUERY",
                "recorded_at": "2026-09-05T01:02:03+00:00",
                "final_sql": original_sql,
                "gates": {"accepted": True, "errors": []},
                "version_pins": {
                    "database_snapshot_id": SNAPSHOT["snapshot_id"],
                    "policy_version": "policy-test",
                    "api_key": "must-not-survive",
                },
                "agents": [{"chain_of_thought": "private reasoning"}],
                "collaboration": {
                    "approved_query_plan": {
                        "bound_plan": {"fingerprint": "bound-plan-123"}
                    },
                    "system_prompt": "private prompt",
                },
            },
            SNAPSHOT,
            corrected_sql=corrected_sql,
            feedback_note="去重口径错误 password=must-not-survive",
        )

        rule = attribution["rule"]
        self.assertEqual(rule["contract"], MEMORY_RULE_CONTRACT)
        self.assertEqual(rule["source_case_ids"], ["query-run-123"])
        for field in ("trigger", "action", "avoid", "rationale"):
            self.assertTrue(rule[field])

        evidence = attribution["evidence"]
        self.assertEqual(evidence["contract"], "ProductionFeedbackAttribution/v3")
        self.assertEqual(
            evidence["query_run_trace"]["approved_query_plan_fingerprint"],
            "bound-plan-123",
        )
        self.assertEqual(evidence["human_feedback"]["decision"], "incorrect")
        self.assertEqual(
            evidence["human_feedback"]["note_sha256"],
            hashlib.sha256(
                "去重口径错误 password=must-not-survive".encode("utf-8")
            ).hexdigest(),
        )
        self.assertEqual(
            evidence["original_sql_summary"]["source_sha256"],
            hashlib.sha256(original_sql.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            evidence["corrected_sql_summary"]["source_sha256"],
            hashlib.sha256(corrected_sql.encode("utf-8")).hexdigest(),
        )
        rendered = json.dumps(evidence, ensure_ascii=False).lower()
        self.assertNotIn("must-not-survive", rendered)
        self.assertNotIn("private reasoning", rendered)
        self.assertNotIn("private prompt", rendered)
        self.assertNotIn(original_sql.lower(), rendered)
        self.assertNotIn(corrected_sql.lower(), rendered)

    def test_identical_rules_merge_provenance_and_keep_one_runtime_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evolution.sqlite3"
            with Text2SQLEvolutionStore(path, SNAPSHOT) as store:
                first = attribute_query_failure(
                    {
                        "task_id": "case-1",
                        "final_sql": "SELECT COUNT(*) FROM t_caseinfo",
                        "gates": {"accepted": True, "errors": []},
                    },
                    SNAPSHOT,
                    feedback_note="重复计数，结果粒度错误",
                )
                second = attribute_query_failure(
                    {
                        "task_id": "case-2",
                        "final_sql": "SELECT COUNT(*) FROM t_caseinfo",
                        "gates": {"accepted": True, "errors": []},
                    },
                    SNAPSHOT,
                    feedback_note="聚合口径不正确",
                )
                first_id = store.add_memory_candidate(
                    first["target_skill"],
                    first["failure_kind"],
                    first["content"],
                    first["evidence"],
                    first["origin_split"],
                    rule=first["rule"],
                )
                second_id = store.add_memory_candidate(
                    second["target_skill"],
                    second["failure_kind"],
                    second["content"],
                    second["evidence"],
                    second["origin_split"],
                    rule=second["rule"],
                )
                repeated_id = store.add_memory_candidate(
                    second["target_skill"],
                    second["failure_kind"],
                    second["content"],
                    second["evidence"],
                    second["origin_split"],
                    rule=second["rule"],
                )

                self.assertEqual(first_id, second_id)
                self.assertEqual(first_id, repeated_id)
                item = store.get_memory(first_id)
                self.assertEqual(item["occurrence_count"], 2)
                self.assertEqual(item["source_case_ids"], ["case-1", "case-2"])
                self.assertEqual(item["rule"]["source_case_ids"], ["case-1", "case-2"])
                self.assertEqual(item["evidence"]["contract"], MEMORY_EVIDENCE_CONTRACT)
                self.assertEqual(len(item["evidence"]["sources"]), 2)

                # Stable retrieval remains role-scoped and de-duplicates a
                # rule fingerprint before it can be injected into one Agent.
                with store.connection:
                    store.connection.execute(
                        "UPDATE memory_items SET state='stable' WHERE memory_id=?",
                        (first_id,),
                    )
                self.assertEqual(
                    [value["memory_id"] for value in store.stable_memory("query-planning")],
                    [first_id],
                )
                self.assertEqual(store.stable_memory("schema-grounding"), ())

    def test_same_failure_kind_with_different_ast_deltas_keeps_distinct_rules(self):
        distinct_fix = attribute_query_failure(
            {
                "task_id": "distinct-fix",
                "final_sql": "SELECT COUNT(*) FROM t_caseinfo",
                "gates": {"accepted": True, "errors": []},
            },
            SNAPSHOT,
            corrected_sql="SELECT COUNT(DISTINCT c_caseCode) FROM t_caseinfo",
            feedback_note="重复计数，去重口径错误",
        )
        group_fix = attribute_query_failure(
            {
                "task_id": "group-fix",
                "final_sql": "SELECT COUNT(*) FROM t_caseinfo",
                "gates": {"accepted": True, "errors": []},
            },
            SNAPSHOT,
            corrected_sql=(
                "SELECT c_level, COUNT(*) FROM t_caseinfo GROUP BY c_level"
            ),
            feedback_note="聚合结果粒度错误",
        )
        self.assertEqual(
            distinct_fix["failure_kind"], group_fix["failure_kind"]
        )
        self.assertIn(
            "has_distinct",
            distinct_fix["rule"]["case_conditions"]["ast_feature_delta"],
        )
        self.assertIn(
            "has_group",
            group_fix["rule"]["case_conditions"]["ast_feature_delta"],
        )

        with tempfile.TemporaryDirectory() as temporary:
            with Text2SQLEvolutionStore(
                Path(temporary) / "evolution.sqlite3", SNAPSHOT
            ) as store:
                memory_ids = []
                for attribution in (distinct_fix, group_fix):
                    memory_ids.append(
                        store.add_memory_candidate(
                            attribution["target_skill"],
                            attribution["failure_kind"],
                            attribution["content"],
                            attribution["evidence"],
                            attribution["origin_split"],
                            rule=attribution["rule"],
                        )
                    )
                self.assertNotEqual(memory_ids[0], memory_ids[1])
                self.assertNotEqual(
                    store.get_memory(memory_ids[0])["rule_fingerprint"],
                    store.get_memory(memory_ids[1])["rule_fingerprint"],
                )

    def test_identical_rule_merges_audit_observations_without_changing_semantics(self):
        base_rule = {
            "trigger": "计数问题的结果粒度尚未明确时",
            "action": "先确定粒度与去重口径",
            "avoid": "不要默认 COUNT(*)",
            "rationale": "重复计数会改变答案",
            "case_conditions": {"has_count": True},
        }
        with tempfile.TemporaryDirectory() as temporary:
            with Text2SQLEvolutionStore(
                Path(temporary) / "evolution.sqlite3", SNAPSHOT
            ) as store:
                first = store.add_memory_candidate(
                    "query-planning",
                    "aggregation_grain_mismatch",
                    base_rule["action"],
                    {"case_id": "case-observation-a"},
                    "train",
                    rule={
                        **base_rule,
                        "observations": ["first observation"],
                    },
                )
                second = store.add_memory_candidate(
                    "query-planning",
                    "aggregation_grain_mismatch",
                    base_rule["action"],
                    {"case_id": "case-observation-b"},
                    "train",
                    rule={
                        **base_rule,
                        "observations": ["second observation"],
                    },
                )
                item = store.get_memory(first)

        self.assertEqual(first, second)
        self.assertEqual(
            item["rule"]["observations"],
            ["first observation", "second observation"],
        )

    def test_legacy_free_text_memory_is_upgraded_to_structured_rule(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evolution.sqlite3"
            with Text2SQLEvolutionStore(path, SNAPSHOT) as store:
                memory_id = store.add_memory_candidate(
                    "sql-generation",
                    "GROUP_BY_MISMATCH",
                    "Keep GROUP BY aligned with the approved plan.",
                    {"case_id": "legacy-case"},
                    "train",
                )
                item = store.get_memory(memory_id)

            self.assertEqual(item["rule"]["contract"], MEMORY_RULE_CONTRACT)
            self.assertEqual(item["rule"]["source_case_ids"], ["legacy-case"])
            self.assertEqual(
                item["rule"]["action"],
                "Keep GROUP BY aligned with the approved plan.",
            )
            self.assertTrue(item["rule_fingerprint"])
            self.assertIn("触发：", item["content"])

    def test_policy_exposes_source_memory_ids_for_runtime_double_injection_filter(self):
        with tempfile.TemporaryDirectory() as temporary:
            with Text2SQLEvolutionStore(
                Path(temporary) / "evolution.sqlite3", SNAPSHOT
            ) as store:
                artifact = store.get_policy().as_dict()
                artifact["prompt_fragments"]["query-planning"] = (
                    "State the result grain before choosing a count expression."
                )
                version = store.propose_policy(
                    artifact,
                    "query-planning",
                    "Fold two reviewed rules into one policy fragment.",
                    "reviewer",
                    proposal_metadata={
                        "memory_ids": [
                            "memory-rule-a",
                            "memory-rule-a",
                            "memory-rule-b",
                            "not-a-memory",
                        ]
                    },
                )
                self.assertEqual(
                    store.policy_source_memory_ids(version),
                    ("memory-rule-a", "memory-rule-b"),
                )
                self.assertEqual(store.policy_source_memory_ids(), ())

    def test_policy_descendant_inherits_compiled_memory_and_prompt_replacement_resets_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            with Text2SQLEvolutionStore(
                Path(temporary) / "evolution.sqlite3", SNAPSHOT
            ) as store:
                memory_id = store.add_memory_candidate(
                    "query-planning",
                    "AGGREGATION_MISMATCH",
                    "State the result grain before counting.",
                    {"case_id": "case-policy-lineage"},
                    "train",
                )
                first_artifact = store.get_policy().as_dict()
                first_artifact["prompt_fragments"]["query-planning"] = (
                    "State the result grain before choosing a count expression."
                )
                first = store.propose_policy(
                    first_artifact,
                    "query-planning",
                    "Compile one reviewed rule.",
                    "reviewer",
                    proposal_metadata={"memory_ids": [memory_id]},
                )

                second_artifact = store.get_policy(first).as_dict()
                second_artifact["prompt_fragments"]["text2sql-lead"] = (
                    "Keep final selection bounded to accepted candidates."
                )
                second = store.propose_policy(
                    second_artifact,
                    "text2sql-lead",
                    "Tune an unrelated role budget.",
                    "reviewer",
                    parent_version=first,
                )
                self.assertEqual(
                    store.policy_source_memory_ids(second), (memory_id,)
                )

                replacement_artifact = store.get_policy(second).as_dict()
                replacement_artifact["prompt_fragments"]["query-planning"] = (
                    "Derive all logical dimensions before aggregation."
                )
                replacement = store.propose_policy(
                    replacement_artifact,
                    "query-planning",
                    "Replace the prior planning guidance.",
                    "reviewer",
                    parent_version=second,
                )
                self.assertEqual(store.policy_source_memory_ids(replacement), ())

    def test_structured_review_preserves_server_owned_case_conditions(self):
        with tempfile.TemporaryDirectory() as temporary:
            with Text2SQLEvolutionStore(
                Path(temporary) / "evolution.sqlite3", SNAPSHOT
            ) as store:
                attribution = attribute_query_failure(
                    {
                        "task_id": "case-preserve-conditions",
                        "final_sql": "SELECT COUNT(*) FROM t_caseinfo",
                        "gates": {"accepted": True, "errors": []},
                    },
                    SNAPSHOT,
                    corrected_sql=(
                        "SELECT COUNT(DISTINCT c_caseCode) FROM t_caseinfo"
                    ),
                    feedback_note="重复计数",
                )
                memory_id = store.add_memory_candidate(
                    attribution["target_skill"],
                    attribution["failure_kind"],
                    attribution["content"],
                    attribution["evidence"],
                    attribution["origin_split"],
                    rule=attribution["rule"],
                )
                before = store.get_memory(memory_id)
                updated = store.update_memory_candidate(
                    memory_id,
                    before["target_skill"],
                    before["failure_kind"],
                    "先明确去重口径再生成逻辑指标。",
                    rule={
                        "trigger": before["rule"]["trigger"],
                        "action": "先明确去重口径再生成逻辑指标。",
                        "avoid": before["rule"]["avoid"],
                        "rationale": before["rule"]["rationale"],
                    },
                )

                self.assertEqual(
                    updated["rule"]["case_conditions"],
                    before["rule"]["case_conditions"],
                )
                self.assertEqual(
                    updated["rule"]["observations"],
                    before["rule"]["observations"],
                )
                self.assertIn("动作：先明确去重口径", updated["content"])


if __name__ == "__main__":
    unittest.main()
