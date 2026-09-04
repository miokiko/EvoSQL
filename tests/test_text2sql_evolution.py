import copy
import json
import tempfile
import unittest
from pathlib import Path

from evoagent.text2sql.agentic import Text2SQLAgenticEngine
from evoagent.text2sql.database_tools import ROLE_TOOL_PERMISSIONS, Text2SQLToolSuite
from evoagent.text2sql.evolution import (
    EPISODIC_MEMORY_RETENTION_PER_SESSION,
    Text2SQLEvolutionStore,
    evaluate_knowledge_promotion_gate,
    evaluate_memory_promotion_gate,
    evaluate_promotion_gate,
)
from evoagent.text2sql.knowledge_store import KnowledgeStore
from evoagent.text2sql.policy import (
    LEGACY_POLICY_CONTRACT_VERSION,
    POLICY_CONTRACT_VERSION,
    TEXT2SQL_SKILLS,
    PolicyArtifact,
    require_single_skill_change,
)
from evoagent.text2sql.policy_generator import Text2SQLPolicyCandidateGenerator
from evoagent.text2sql.sqlite_database import build_sqlite_database


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = json.loads(
    (PROJECT_ROOT / "artifacts" / "text2sql" / "schema" / "database_snapshot.json").read_text(
        encoding="utf-8"
    )
)
JOIN_CATALOG = json.loads(
    (PROJECT_ROOT / "artifacts" / "text2sql" / "schema" / "join_catalog.review.json").read_text(
        encoding="utf-8"
    )
)


def _report(policy_version, validation_accuracy, holdout_accuracy, passed_validation):
    pins = {
        "database_snapshot_id": SNAPSHOT["snapshot_id"],
        "wiki_index_version": "wiki-test",
        "memory_snapshot_id": "memory-test",
        "policy_version": policy_version,
    }
    splits = {}
    outcomes = []
    for split, accuracy in (
        ("validation", validation_accuracy),
        ("sealed_holdout", holdout_accuracy),
    ):
        splits[split] = {
            "cases": 10,
            "execution_accuracy": accuracy,
            "executable_rate": 1.0,
            "ast_parse_rate": 1.0,
            "readonly_safety_rate": 1.0,
            "framework_errors": 0,
            "p95_latency_ms": 100.0,
            "skeleton_buckets": {
                "select-filter": {"cases": 10, "execution_accuracy": accuracy}
            },
        }
        passed = passed_validation if split == "validation" else int(holdout_accuracy * 10)
        outcomes.extend(
            {
                "case_id": "%s-%02d" % (split, index),
                "split": split,
                "execution_accuracy": index < passed,
                "safe": True,
                "executable": True,
                "ast_valid": True,
                "failure_kind": "" if index < passed else "RESULT_MISMATCH",
                "duration_ms": 100.0,
                "sql_skeleton": "select-filter",
            }
            for index in range(10)
        )
    return {"version_pins": pins, "overall": {}, "splits": splits, "outcomes": outcomes}


def _promotion_fixture():
    manifest = {
        "dataset_id": "promotion-dataset",
        "dataset_sha256": "promotion-sha",
        "database_snapshot_id": SNAPSHOT["snapshot_id"],
        "release_eligible": True,
        "review_status": "human_reviewed",
        "human_reviewed_cases": 20,
        "files": {
            "validation": {"case_count": 10},
            "sealed_holdout": {"case_count": 10},
        },
    }
    evidence = {
        "verified": True,
        "dataset_sha256": manifest["dataset_sha256"],
        "reviewed_case_count": 20,
        "certificate_sha256": "certificate",
    }
    return (
        manifest,
        _report("policy-baseline", 0.4, 0.8, 4),
        _report("policy-candidate", 0.9, 0.8, 9),
        evidence,
    )


def _full_release_fixture(kind):
    manifest = {
        "dataset_id": "full-release-dataset",
        "dataset_sha256": "full-release-sha",
        "release_eligible": True,
        "files": {
            "train": {"case_count": 144},
            "validation": {"case_count": 48},
            "sealed_holdout": {"case_count": 48},
        },
    }
    evidence = {
        "verified": True,
        "dataset_sha256": manifest["dataset_sha256"],
        "reviewed_case_count": 240,
        "certificate_sha256": "certificate",
    }

    def report(memory_snapshot_id, wiki_index_version):
        outcomes = []
        splits = {}
        for split, count in (
            ("train", 144),
            ("validation", 48),
            ("sealed_holdout", 48),
        ):
            splits[split] = {
                "cases": count,
                "execution_accuracy": 1.0,
                "executable_rate": 1.0,
                "ast_parse_rate": 1.0,
                "readonly_safety_rate": 1.0,
                "framework_errors": 0,
                "p95_latency_ms": 100.0,
                "skeleton_buckets": {
                    "select-filter": {
                        "cases": count,
                        "execution_accuracy": 1.0,
                    }
                },
            }
            outcomes.extend(
                {
                    "case_id": "%s-%03d" % (split, index),
                    "split": split,
                    "execution_accuracy": True,
                    "safe": True,
                    "executable": True,
                    "ast_valid": True,
                    "failure_kind": "",
                    "duration_ms": 100.0,
                    "sql_skeleton": "select-filter",
                }
                for index in range(count)
            )
        return {
            "version_pins": {
                "database_snapshot_id": SNAPSHOT["snapshot_id"],
                "wiki_index_version": wiki_index_version,
                "vanna_index_version": wiki_index_version,
                "memory_snapshot_id": memory_snapshot_id,
                "policy_version": "policy-stable",
            },
            "overall": {"execution_accuracy": 1.0},
            "splits": splits,
            "outcomes": outcomes,
        }

    if kind == "memory":
        baseline = report("memory-stable", "wiki-stable")
        candidate = report("memory-candidate", "wiki-stable")
    else:
        baseline = report("memory-stable", "wiki-stable")
        candidate = report("memory-stable", "wiki-candidate")
    return manifest, baseline, candidate, evidence


class EvolutionGateValidationTests(unittest.TestCase):
    @staticmethod
    def _fixtures():
        promotion = _promotion_fixture()
        memory = _full_release_fixture("memory")
        knowledge = _full_release_fixture("knowledge")
        return (
            (
                "promotion",
                evaluate_promotion_gate,
                "eligible_for_human_approval",
                promotion,
            ),
            (
                "memory",
                evaluate_memory_promotion_gate,
                "eligible_for_activation",
                memory,
            ),
            (
                "knowledge",
                evaluate_knowledge_promotion_gate,
                "eligible_for_activation",
                knowledge,
            ),
        )

    def test_valid_gate_fixtures_are_eligible(self):
        for gate_name, gate, eligible_key, fixture in self._fixtures():
            with self.subTest(gate=gate_name):
                decision = gate(*copy.deepcopy(fixture))
                self.assertTrue(decision[eligible_key], decision)

    def test_all_gates_fail_closed_on_nonfinite_and_wrong_scalar_types(self):
        mutations = {
            "nan_rate": lambda manifest, candidate, evidence: candidate["splits"][
                "validation"
            ].__setitem__("readonly_safety_rate", float("nan")),
            "infinite_accuracy": lambda manifest, candidate, evidence: candidate[
                "splits"
            ]["validation"].__setitem__("execution_accuracy", float("inf")),
            "string_error_count": lambda manifest, candidate, evidence: candidate[
                "splits"
            ]["validation"].__setitem__("framework_errors", "0"),
            "string_case_boolean": lambda manifest, candidate, evidence: candidate[
                "outcomes"
            ][0].__setitem__("execution_accuracy", "true"),
            "string_release_boolean": lambda manifest, candidate, evidence: manifest.__setitem__(
                "release_eligible", "true"
            ),
            "string_review_boolean": lambda manifest, candidate, evidence: evidence.__setitem__(
                "verified", "true"
            ),
        }
        for gate_name, gate, eligible_key, fixture in self._fixtures():
            for mutation_name, mutate in mutations.items():
                with self.subTest(gate=gate_name, mutation=mutation_name):
                    manifest, baseline, candidate, evidence = copy.deepcopy(fixture)
                    mutate(manifest, candidate, evidence)
                    decision = gate(manifest, baseline, candidate, evidence)
                    self.assertFalse(decision[eligible_key], decision)

    def test_all_gates_require_complete_strict_outcome_fields(self):
        def mutate_field(candidate, field, value, *, remove=False):
            outcome = next(
                item
                for item in candidate["outcomes"]
                if item["split"] == "validation"
            )
            if remove:
                outcome.pop(field)
            else:
                outcome[field] = value

        mutations = (
            ("missing_safe", lambda candidate: mutate_field(candidate, "safe", None, remove=True)),
            ("string_safe", lambda candidate: mutate_field(candidate, "safe", "true")),
            ("nan_duration", lambda candidate: mutate_field(candidate, "duration_ms", float("nan"))),
            ("negative_duration", lambda candidate: mutate_field(candidate, "duration_ms", -1.0)),
            (
                "missing_failure_kind",
                lambda candidate: mutate_field(
                    candidate, "failure_kind", None, remove=True
                ),
            ),
            (
                "numeric_skeleton",
                lambda candidate: mutate_field(candidate, "sql_skeleton", 1),
            ),
        )
        for gate_name, gate, eligible_key, fixture in self._fixtures():
            for mutation_name, mutate in mutations:
                with self.subTest(gate=gate_name, mutation=mutation_name):
                    manifest, baseline, candidate, evidence = copy.deepcopy(fixture)
                    mutate(candidate)
                    decision = gate(manifest, baseline, candidate, evidence)
                    self.assertFalse(decision[eligible_key], decision)

    def test_all_gates_reject_impossible_failure_kind_flag_combinations(self):
        def ensure_failed_executable(report):
            validation = [
                item
                for item in report["outcomes"]
                if item["split"] == "validation"
            ]
            outcome = next(
                (item for item in validation if not item["execution_accuracy"]),
                validation[0],
            )
            outcome["execution_accuracy"] = False
            outcome["failure_kind"] = "RESULT_MISMATCH"
            validation_accuracy = round(
                sum(item["execution_accuracy"] for item in validation)
                / len(validation),
                6,
            )
            report["splits"]["validation"][
                "execution_accuracy"
            ] = validation_accuracy
            report["splits"]["validation"]["skeleton_buckets"][
                "select-filter"
            ]["execution_accuracy"] = validation_accuracy
            if "execution_accuracy" in report["overall"]:
                report["overall"]["execution_accuracy"] = round(
                    sum(item["execution_accuracy"] for item in report["outcomes"])
                    / len(report["outcomes"]),
                    6,
                )
            return outcome

        impossible_while_executable = (
            "UNSAFE_SQL",
            "NO_SQL",
            "PARSE_ERROR",
            "UNKNOWN_TABLE",
            "UNKNOWN_COLUMN",
            "SCHEMA_LINK_MISMATCH",
            "TIMEOUT",
            "FRAMEWORK_ERROR",
            "EXECUTION_ERROR",
            "USER_CORRECTION",
        )
        for gate_name, gate, eligible_key, fixture in self._fixtures():
            for failure_kind in impossible_while_executable:
                with self.subTest(gate=gate_name, failure_kind=failure_kind):
                    manifest, baseline, candidate, evidence = copy.deepcopy(fixture)
                    ensure_failed_executable(baseline)
                    candidate_outcome = ensure_failed_executable(candidate)
                    candidate_outcome["failure_kind"] = failure_kind
                    decision = gate(manifest, baseline, candidate, evidence)
                    self.assertFalse(decision[eligible_key], decision)
                    self.assertTrue(
                        any(
                            "invalid_outcome_invariant" in reason
                            for reason in decision["reasons"]
                        ),
                        decision,
                    )

    def test_readonly_unclassified_gate_error_is_a_valid_unsafe_outcome(self):
        manifest, baseline, candidate, evidence = copy.deepcopy(
            _promotion_fixture()
        )
        outcome = next(
            item
            for item in candidate["outcomes"]
            if item["split"] == "validation" and not item["execution_accuracy"]
        )
        outcome.update(
            {
                "safe": True,
                "executable": False,
                "ast_valid": True,
                "failure_kind": "UNSAFE_SQL",
            }
        )
        candidate["splits"]["validation"]["executable_rate"] = 0.9

        decision = evaluate_promotion_gate(
            manifest, baseline, candidate, evidence
        )

        self.assertFalse(decision["eligible_for_human_approval"], decision)
        self.assertNotIn(
            "invalid_outcome_invariant:candidate.outcomes.validation-09",
            decision["reasons"],
        )

    def test_memory_and_knowledge_reject_zero_operational_rates(self):
        for kind, gate in (
            ("memory", evaluate_memory_promotion_gate),
            ("knowledge", evaluate_knowledge_promotion_gate),
        ):
            with self.subTest(gate=kind):
                manifest, baseline, candidate, evidence = copy.deepcopy(
                    _full_release_fixture(kind)
                )
                for report in (baseline, candidate):
                    for outcome in report["outcomes"]:
                        outcome.update(
                            {
                                "execution_accuracy": False,
                                "executable": False,
                                "ast_valid": False,
                                "safe": True,
                                "failure_kind": "NO_SQL",
                            }
                        )
                    report["overall"]["execution_accuracy"] = 0.0
                    for split in report["splits"].values():
                        split["execution_accuracy"] = 0.0
                        split["executable_rate"] = 0.0
                        split["ast_parse_rate"] = 0.0
                        split["skeleton_buckets"]["select-filter"][
                            "execution_accuracy"
                        ] = 0.0
                decision = gate(manifest, baseline, candidate, evidence)
                self.assertFalse(decision["eligible_for_activation"], decision)
                self.assertTrue(
                    any("operational_rate_zero" in reason for reason in decision["reasons"]),
                    decision,
                )

    def test_all_gates_require_unique_complete_aligned_case_coverage(self):
        for gate_name, gate, eligible_key, fixture in self._fixtures():
            with self.subTest(gate=gate_name, defect="candidate_missing"):
                manifest, baseline, candidate, evidence = copy.deepcopy(fixture)
                candidate["outcomes"].pop()
                decision = gate(manifest, baseline, candidate, evidence)
                self.assertFalse(decision[eligible_key], decision)

            with self.subTest(gate=gate_name, defect="same_duplicate_in_both"):
                manifest, baseline, candidate, evidence = copy.deepcopy(fixture)
                split = "validation"
                for report in (baseline, candidate):
                    split_outcomes = [
                        item for item in report["outcomes"] if item["split"] == split
                    ]
                    split_outcomes[-1]["case_id"] = split_outcomes[0]["case_id"]
                decision = gate(manifest, baseline, candidate, evidence)
                self.assertFalse(decision[eligible_key], decision)

    def test_all_gates_reject_execution_accuracy_aggregate_mismatch(self):
        for gate_name, gate, eligible_key, fixture in self._fixtures():
            with self.subTest(gate=gate_name):
                manifest, baseline, candidate, evidence = copy.deepcopy(fixture)
                if gate_name == "promotion":
                    candidate["splits"]["validation"]["execution_accuracy"] = 0.95
                else:
                    baseline["splits"]["validation"]["execution_accuracy"] = 0.95
                    candidate["splits"]["validation"]["execution_accuracy"] = 0.95
                decision = gate(manifest, baseline, candidate, evidence)
                self.assertFalse(decision[eligible_key], decision)
                self.assertTrue(
                    any(
                        "execution_accuracy_aggregate_mismatch" in reason
                        for reason in decision["reasons"]
                    ),
                    decision,
                )

    def test_six_decimal_execution_accuracy_rounding_is_accepted(self):
        manifest, baseline, candidate, evidence = copy.deepcopy(
            _full_release_fixture("knowledge")
        )
        for report in (baseline, candidate):
            validation = [
                item
                for item in report["outcomes"]
                if item["split"] == "validation"
            ]
            validation[0]["execution_accuracy"] = False
            validation[0]["failure_kind"] = "RESULT_MISMATCH"
            report["splits"]["validation"]["execution_accuracy"] = round(
                47 / 48, 6
            )
            report["splits"]["validation"]["skeleton_buckets"][
                "select-filter"
            ]["execution_accuracy"] = round(47 / 48, 6)

        decision = evaluate_knowledge_promotion_gate(
            manifest, baseline, candidate, evidence
        )

        self.assertTrue(decision["eligible_for_activation"], decision)

    def test_policy_gate_vetoes_any_sealed_holdout_case_regression(self):
        manifest, baseline, candidate, evidence = copy.deepcopy(
            _promotion_fixture()
        )
        holdout = [
            item
            for item in candidate["outcomes"]
            if item["split"] == "sealed_holdout"
        ]
        holdout[0]["execution_accuracy"] = False
        holdout[0]["failure_kind"] = "RESULT_MISMATCH"
        holdout[8]["execution_accuracy"] = True
        holdout[8]["failure_kind"] = ""

        decision = evaluate_promotion_gate(
            manifest, baseline, candidate, evidence
        )

        self.assertFalse(decision["eligible_for_human_approval"], decision)
        self.assertIn("sealed_holdout_case_regression", decision["reasons"])

    def test_all_gates_recompute_safety_errors_and_latency_from_outcomes(self):
        def all_false(candidate):
            for outcome in candidate["outcomes"]:
                if outcome["split"] == "validation":
                    outcome["execution_accuracy"] = False
                    outcome["safe"] = False
                    outcome["executable"] = False
                    outcome["ast_valid"] = False
                    outcome["failure_kind"] = "RESULT_MISMATCH"

        def framework_errors(candidate):
            for outcome in candidate["outcomes"]:
                if outcome["split"] == "validation":
                    outcome["failure_kind"] = "FRAMEWORK_ERROR"

        def high_latency(candidate):
            for outcome in candidate["outcomes"]:
                if outcome["split"] == "validation":
                    outcome["duration_ms"] = 10000.0

        for gate_name, gate, eligible_key, fixture in self._fixtures():
            for mutation_name, mutate in (
                ("all_false", all_false),
                ("framework_errors", framework_errors),
                ("high_latency", high_latency),
            ):
                with self.subTest(gate=gate_name, mutation=mutation_name):
                    manifest, baseline, candidate, evidence = copy.deepcopy(fixture)
                    mutate(candidate)
                    decision = gate(manifest, baseline, candidate, evidence)
                    self.assertFalse(decision[eligible_key], decision)


class PolicyArtifactTests(unittest.TestCase):
    def test_policy_rejects_unknown_fields_unsafe_sql_and_privilege_expansion(self):
        with self.assertRaisesRegex(ValueError, "forbidden field"):
            PolicyArtifact.from_dict({"deterministic_gates": {"disabled": True}}, SNAPSHOT)
        with self.assertRaisesRegex(ValueError, "safety validation"):
            PolicyArtifact.from_dict(
                {
                    "few_shot_examples": {
                        "sql-generation": [
                            {"question": "删数据", "sql": "DELETE FROM t_caseinfo"}
                        ]
                    }
                },
                SNAPSHOT,
            )
        with self.assertRaisesRegex(ValueError, "cannot expand"):
            PolicyArtifact.from_dict(
                {"tool_selection_policy": {"schema-grounding": ["execute_sql"]}},
                SNAPSHOT,
            )
        with self.assertRaisesRegex(ValueError, "owned only by schema-grounding"):
            PolicyArtifact.from_dict(
                {
                    "field_aliases": {
                        "query-planning": {"案例": "t_caseinfo.c_caseCode"}
                    }
                },
                SNAPSHOT,
            )
        with self.assertRaisesRegex(ValueError, "owned only by sql-generation"):
            PolicyArtifact.from_dict(
                {
                    "few_shot_examples": {
                        "query-planning": [
                            {
                                "question": "列出案例",
                                "sql": "SELECT c_caseCode FROM t_caseinfo LIMIT 1",
                            }
                        ]
                    }
                },
                SNAPSHOT,
            )
        with self.assertRaisesRegex(ValueError, "schema-blind"):
            PolicyArtifact.from_dict(
                {
                    "prompt_fragments": {
                        "query-planning": "Always use t_caseinfo.c_caseCode."
                    }
                },
                SNAPSHOT,
            )

    def test_policy_is_complete_hashed_and_tool_policy_only_reduces(self):
        value = PolicyArtifact.baseline(SNAPSHOT).as_dict()
        value["prompt_fragments"]["query-planning"] = (
            "Before finalizing, explicitly compare COUNT(*) with COUNT(DISTINCT key)."
        )
        value["tool_selection_policy"]["query-planning"] = []
        artifact = PolicyArtifact.from_dict(value, SNAPSHOT)
        self.assertTrue(artifact.version.startswith("policy-"))
        self.assertEqual(artifact.as_dict()["contract_version"], POLICY_CONTRACT_VERSION)
        for field in (
            "prompt_fragments",
            "field_aliases",
            "value_aliases",
            "few_shot_examples",
            "tool_selection_policy",
            "budget_parameters",
        ):
            self.assertEqual(set(artifact.as_dict()[field]), set(TEXT2SQL_SKILLS))
        self.assertNotIn("text2sql-harness", TEXT2SQL_SKILLS)
        self.assertEqual(
            ROLE_TOOL_PERMISSIONS["text2sql-harness"],
            {"validate_sql", "explain_sql", "execute_sql"},
        )
        for skill in TEXT2SQL_SKILLS:
            self.assertNotIn("execute_sql", ROLE_TOOL_PERMISSIONS[skill])
        self.assertEqual(
            artifact.changed_skills(PolicyArtifact.baseline(SNAPSHOT)),
            ("query-planning",),
        )
        self.assertEqual(
            artifact.role_policy("query-planning")["allowed_tools"],
            [],
        )
        with self.assertRaisesRegex(ValueError, "unsupported Text2SQL runtime role"):
            artifact.role_policy("text2sql-harness")

    def test_legacy_v1_strategy_policy_is_read_only_migrated_on_load(self):
        baseline = PolicyArtifact.baseline(SNAPSHOT).as_dict()
        legacy = {"contract_version": LEGACY_POLICY_CONTRACT_VERSION}
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
        legacy["prompt_fragments"]["sql-strategy"] = "State result grain first."

        migrated = PolicyArtifact.from_dict(legacy, SNAPSHOT)
        migrated_value = migrated.as_dict()

        self.assertTrue(migrated.was_migrated_from_v1)
        self.assertEqual(migrated_value["contract_version"], POLICY_CONTRACT_VERSION)
        self.assertNotIn("sql-strategy", migrated_value["prompt_fragments"])
        self.assertEqual(
            set(migrated_value["prompt_fragments"]), set(TEXT2SQL_SKILLS)
        )
        self.assertEqual(
            migrated_value["prompt_fragments"]["query-planning"],
            "State result grain first.",
        )
        self.assertEqual(
            migrated_value["prompt_fragments"]["sql-generation"],
            "State result grain first.",
        )
        self.assertEqual(
            migrated.role_policy("sql-strategy")["skill"], "query-planning"
        )
        with self.assertRaisesRegex(ValueError, "read-only"):
            require_single_skill_change(
                PolicyArtifact.baseline(SNAPSHOT), migrated, "query-planning"
            )

    def test_root_cause_generator_can_only_emit_one_validated_skill_patch(self):
        class Client:
            provider = "scripted"
            model = "scripted-generator"

            def complete_json(self, role, system, user, ledger=None, max_tokens=None):
                self.input = json.loads(user)
                return {
                    "clusters": [
                        {
                            "name": "duplicate counting",
                            "memory_ids": ["memory-reviewed"],
                            "root_cause": "result grain was implicit",
                        }
                    ],
                    "skill_patch": {
                        "prompt_fragment": "State result grain before choosing a count expression."
                    },
                    "rationale": "Make count semantics explicit.",
                }

        client = Client()
        generated = Text2SQLPolicyCandidateGenerator(client).generate(
            [
                {
                    "memory_id": "memory-reviewed",
                    "failure_kind": "AGGREGATION_MISMATCH",
                    "content": "Review result grain before counting.",
                }
            ],
            PolicyArtifact.baseline(SNAPSHOT),
            "query-planning",
            SNAPSHOT,
        )
        candidate = PolicyArtifact.from_dict(generated["artifact"], SNAPSHOT)
        self.assertEqual(
            candidate.changed_skills(PolicyArtifact.baseline(SNAPSHOT)),
            ("query-planning",),
        )
        self.assertNotIn("gold_sql", json.dumps(client.input).lower())


class EvolutionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Text2SQLEvolutionStore(self.root / "evolution.sqlite3", SNAPSHOT)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def _candidate(self):
        value = self.store.get_policy().as_dict()
        value["prompt_fragments"]["query-planning"] = (
            "Check duplicate fanout and state the intended result grain."
        )
        return self.store.propose_policy(
            value,
            "query-planning",
            "Repeated duplicate-count failures",
            "test-author",
        )

    def test_episodic_retention_is_per_session_not_global(self):
        self.store.save_query_trace(
            {
                "task_id": "other-session-run",
                "status": "success",
                "question": "其他会话",
                "user_id": "reader",
                "session_id": "session-b",
                "recorded_at": "2026-09-04T00:00:00+00:00",
            }
        )
        for index in range(EPISODIC_MEMORY_RETENTION_PER_SESSION + 1):
            self.store.save_query_trace(
                {
                    "task_id": "session-a-%03d" % index,
                    "status": "success",
                    "question": "当前会话 %d" % index,
                    "user_id": "reader",
                    "session_id": "session-a",
                    "recorded_at": "2026-09-04T00:01:%02d+00:00" % index,
                }
            )
        sessions = {
            item["session_id"]: item
            for item in self.store.list_memory_sessions("reader")
        }
        self.assertEqual(
            sessions["session-a"]["episodic_count"],
            EPISODIC_MEMORY_RETENTION_PER_SESSION,
        )
        self.assertEqual(sessions["session-b"]["episodic_count"], 1)

    def test_recent_query_context_includes_bounded_conversation_messages(self):
        self.store.append_message("user-1", "session-1", "user", "先按等级统计", "task-1")
        self.store.append_message("user-1", "session-1", "assistant", "已经完成统计", "task-1")
        context = self.store.recent_query_context("user-1", "session-1")
        self.assertEqual(
            [(item["role"], item["content"]) for item in context["recent_messages"]],
            [("user", "先按等级统计"), ("assistant", "已经完成统计")],
        )

    def test_query_result_snapshot_returns_scope_and_version_pins(self):
        version_pins = {
            "database_snapshot_id": SNAPSHOT["snapshot_id"],
            "wiki_index_version": "wiki-test",
            "vanna_index_version": "wiki-test",
            "memory_snapshot_id": "memory-test",
            "policy_version": self.store.active_policy_version,
        }
        self.store.save_query_trace(
            {
                "task_id": "parent-run",
                "status": "success",
                "question": "列出案例",
                "final_sql": "SELECT 1",
                "gates": {"accepted": True, "errors": []},
                "answer": {"columns": ["value"], "rows": [[1]]},
                "result_rows": [[1]],
                "schema_plan": {"contract_version": "SchemaPlan/v1"},
                "query_spec": {"contract_version": "QuerySpec/v1"},
                "version_pins": version_pins,
                "user_id": "user-1",
                "session_id": "session-1",
            }
        )

        snapshot = self.store.query_result_snapshot(
            "parent-run", "user-1", "session-1"
        )

        self.assertEqual(snapshot["user_id"], "user-1")
        self.assertEqual(snapshot["session_id"], "session-1")
        self.assertEqual(snapshot["version_pins"], version_pins)
        self.assertEqual(
            self.store.query_result_snapshot("parent-run", "user-2", "session-1"),
            {},
        )

    def test_evaluation_artifact_rejects_string_case_count(self):
        manifest = {
            "dataset_id": "artifact-dataset",
            "dataset_sha256": "artifact-sha",
        }
        artifact = {
            "dataset_id": manifest["dataset_id"],
            "dataset_sha256": manifest["dataset_sha256"],
            "evaluated_splits": ["validation", "sealed_holdout"],
            "evaluated_case_count": "20",
            "model": {},
            "report": {},
        }

        with self.assertRaisesRegex(ValueError, "evaluated_case_count"):
            self.store._unwrap_evaluation_artifact(artifact, manifest)

    def test_query_decisions_keep_harness_and_human_review_separate(self):
        self.store.save_query_trace(
            {
                "task_id": "blocked-run",
                "status": "rejected",
                "question": "按等级统计",
                "gates": {
                    "accepted": False,
                    "errors": ["invalid_final_candidate_index"],
                },
                "user_id": "reviewer",
                "session_id": "session-1",
            }
        )
        first = self.store.memory_dashboard("reviewer", "session-1")[
            "episodic"
        ]["items"][0]
        self.assertEqual(first["decisions"]["harness"]["outcome"], "rejected")
        self.assertEqual(
            first["decisions"]["harness"]["reason_code"],
            "invalid_final_candidate_index",
        )
        self.assertEqual(first["decisions"]["human"], {})
        with self.assertRaisesRegex(ValueError, "rejection reason"):
            self.store.record_query_feedback(
                "blocked-run", "incorrect", "", "reviewer"
            )
        self.store.record_query_feedback(
            "blocked-run", "incorrect", "Leader 不应选择空候选。", "reviewer"
        )
        reviewed = self.store.memory_dashboard("reviewer", "session-1")[
            "episodic"
        ]["items"][0]
        self.assertEqual(reviewed["decisions"]["harness"]["outcome"], "rejected")
        self.assertEqual(reviewed["decisions"]["human"]["outcome"], "rejected")
        self.assertEqual(
            reviewed["decisions"]["human"]["reason_text"],
            "Leader 不应选择空候选。",
        )

    def test_question_sql_rejection_requires_and_persists_review_note(self):
        experience_id = self.store.add_experience_candidate(
            "task-1",
            "统计案例数量",
            "SELECT COUNT(*) FROM t_caseinfo",
            source_kind="human_confirmed_query",
            eligible=True,
        )
        with self.assertRaisesRegex(ValueError, "rejection reason"):
            self.store.review_experience(
                experience_id, "reject", "reviewer"
            )
        rejected = self.store.review_experience(
            experience_id,
            "reject",
            "reviewer",
            review_note="案例口径不正确。",
        )
        self.assertEqual(rejected["state"], "rejected")
        self.assertEqual(rejected["review_note"], "案例口径不正确。")

    def test_candidate_must_change_exactly_its_declared_skill(self):
        unchanged = self.store.get_policy().as_dict()
        with self.assertRaisesRegex(ValueError, "change exactly"):
            self.store.propose_policy(
                unchanged, "query-planning", "no effective change", "test-author"
            )
        changed = self.store.get_policy().as_dict()
        changed["prompt_fragments"]["query-planning"] = "Check result grain."
        changed["prompt_fragments"]["schema-grounding"] = "Check exact names."
        with self.assertRaisesRegex(ValueError, "change exactly"):
            self.store.propose_policy(
                changed, "query-planning", "two role changes", "test-author"
            )

    def test_memory_requires_review_evaluation_and_activation(self):
        memory_id = self.store.add_memory_candidate(
            "schema-grounding",
            "UNKNOWN_COLUMN",
            "Confirm the exact qualified column against the pinned schema.",
            {"case_id": "train-1"},
        )
        self.assertEqual(self.store.stable_memory("schema-grounding"), ())
        before = self.store.memory_snapshot_id
        with self.assertRaisesRegex(ValueError, "human review"):
            self.store.review_memory(memory_id, "approve", "reviewer", False)
        reviewed = self.store.review_memory(
            memory_id, "approve", "reviewer", True
        )
        self.assertEqual(reviewed["state"], "approved")
        self.assertEqual(self.store.stable_memory("schema-grounding"), ())
        self.assertEqual(before, self.store.memory_snapshot_id)
        self.assertNotEqual(
            before, self.store.memory_snapshot_id_for(memory_id)
        )
        with self.assertRaisesRegex(ValueError, "holdout"):
            self.store.add_memory_candidate(
                "schema-grounding", "UNKNOWN_COLUMN", "bad", {}, "sealed_holdout"
            )
        rejected_id = self.store.add_memory_candidate(
            "schema-grounding",
            "UNKNOWN_COLUMN",
            "Do not retain this candidate.",
            {"case_id": "production-2"},
            "production_feedback",
        )
        with self.assertRaisesRegex(ValueError, "rejection reason"):
            self.store.review_memory(
                rejected_id, "reject", "reviewer", True
            )
        rejected = self.store.review_memory(
            rejected_id,
            "reject",
            "reviewer",
            True,
            "该结论只适用于单个案例，不能泛化。",
        )
        self.assertEqual(rejected["review_note"], "该结论只适用于单个案例，不能泛化。")

    def test_memory_240_case_gate_activation_and_rollback(self):
        memory_id = self.store.add_memory_candidate(
            "sql-generation",
            "GROUP_BY_MISMATCH",
            "Keep generated GROUP BY columns aligned with the approved plan.",
            {"case_id": "production-1"},
            "production_feedback",
        )
        stable_before = self.store.memory_snapshot_id
        self.store.review_memory(memory_id, "approve", "reviewer", True)
        job = self.store.create_memory_evaluation_job(
            memory_id,
            "reviewer",
            str(self.root / "baseline.json"),
            str(self.root / "candidate.json"),
            str(self.root / "memory.log"),
            240,
        )
        manifest = {
            "dataset_id": "memory-test-dataset",
            "dataset_sha256": "memory-dataset-sha",
            "release_eligible": True,
            "files": {
                "train": {"case_count": 144},
                "validation": {"case_count": 48},
                "sealed_holdout": {"case_count": 48},
            },
        }

        def artifact(memory_snapshot_id, candidate_id=""):
            outcomes = []
            splits = {}
            for split, count in (
                ("train", 144),
                ("validation", 48),
                ("sealed_holdout", 48),
            ):
                splits[split] = {
                    "cases": count,
                    "execution_accuracy": 1.0,
                    "executable_rate": 1.0,
                    "ast_parse_rate": 1.0,
                    "readonly_safety_rate": 1.0,
                    "framework_errors": 0,
                    "p95_latency_ms": 100.0,
                    "skeleton_buckets": {
                        "select-filter": {
                            "cases": count,
                            "execution_accuracy": 1.0,
                        }
                    },
                }
                outcomes.extend(
                    {
                        "case_id": "%s-%03d" % (split, index),
                        "split": split,
                        "execution_accuracy": True,
                        "safe": True,
                        "executable": True,
                        "ast_valid": True,
                        "failure_kind": "",
                        "duration_ms": 100.0,
                        "sql_skeleton": "select-filter",
                    }
                    for index in range(count)
                )
            return {
                "dataset_id": manifest["dataset_id"],
                "dataset_sha256": manifest["dataset_sha256"],
                "evaluated_splits": ["train", "validation", "sealed_holdout"],
                "evaluated_case_count": 240,
                "memory_candidate_id": candidate_id,
                "model": {
                    "provider": "scripted",
                    "model": "scripted",
                    "temperature": 0,
                },
                "report": {
                    "version_pins": {
                        "database_snapshot_id": SNAPSHOT["snapshot_id"],
                        "wiki_index_version": "wiki-test",
                        "vanna_index_version": "wiki-test",
                        "memory_snapshot_id": memory_snapshot_id,
                        "policy_version": self.store.active_policy_version,
                    },
                    "overall": {"execution_accuracy": 1.0},
                    "splits": splits,
                    "outcomes": outcomes,
                },
            }

        result = self.store.record_memory_evaluation(
            memory_id,
            job["job_id"],
            manifest,
            artifact(stable_before),
            artifact(self.store.memory_snapshot_id_for(memory_id), memory_id),
            {
                "verified": True,
                "dataset_sha256": manifest["dataset_sha256"],
                "reviewed_case_count": 240,
                "certificate_sha256": "certificate",
            },
        )
        self.assertTrue(result["eligible_for_activation"])
        self.assertEqual(self.store.get_memory(memory_id)["state"], "evaluated")
        self.assertEqual(self.store.stable_memory("sql-generation"), ())
        activated = self.store.activate_memory(
            memory_id, "publisher", "240 cases passed", True
        )
        self.assertEqual(activated["state"], "stable")
        self.assertNotEqual(stable_before, self.store.memory_snapshot_id)
        rolled_back = self.store.rollback_memory(
            memory_id, "publisher", "manual rollback"
        )
        self.assertEqual(rolled_back["state"], "retired")
        self.assertEqual(stable_before, self.store.memory_snapshot_id)

    def test_unreviewed_dataset_blocks_and_offline_pass_only_reaches_shadow_ready(self):
        candidate = self._candidate()
        baseline = self.store.active_policy_version
        base_report = _report(baseline, 0.4, 0.8, 4)
        candidate_report = _report(candidate, 0.9, 0.8, 9)
        manifest = {
            "dataset_id": "test-dataset",
            "dataset_sha256": "abc",
            "release_eligible": False,
            "review_status": "machine_validated_pending_human_review",
            "human_reviewed_cases": 0,
        }
        blocked = self.store.record_evaluation(
            candidate, manifest, base_report, candidate_report
        )
        self.assertFalse(blocked["eligible_for_human_approval"])
        self.assertIn("dataset_not_human_reviewed", blocked["reasons"])
        manifest["release_eligible"] = True
        manifest["review_status"] = "human_reviewed"
        ready = self.store.record_evaluation(
            candidate,
            manifest,
            base_report,
            candidate_report,
            {
                "verified": True,
                "dataset_sha256": "abc",
                "reviewed_case_count": 0,
                "certificate_sha256": "certificate-hash",
            },
        )
        self.assertTrue(ready["eligible_for_human_approval"])
        with self.assertRaisesRegex(ValueError, "shadow and canary"):
            self.store.activate_policy(candidate, "reviewer", "looks good", True)
        self.assertEqual(self.store.active_policy_version, baseline)
        stored = self.store.connection.execute(
            "SELECT baseline_aggregate_json FROM evolution_runs LIMIT 1"
        ).fetchone()[0]
        self.assertNotIn("outcomes", stored)
        self.assertNotIn("case_id", stored)

    def test_checked_in_manifest_requires_verified_certificate_evidence(self):
        manifest = json.loads(
            (
                PROJECT_ROOT
                / "evaluation"
                / "datasets"
                / "text2sql_v1"
                / "manifest.json"
            ).read_text(encoding="utf-8")
        )
        decision = evaluate_promotion_gate(
            manifest,
            _report("base", 0.4, 0.8, 4),
            _report("candidate", 0.9, 0.8, 9),
        )
        self.assertFalse(decision["eligible_for_human_approval"])
        self.assertIn("dataset_review_certificate_unverified", decision["reasons"])


class RuntimeRestrictionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        cls.root = root
        cls.database = root / "eval.sqlite3"
        cls.knowledge = root / "knowledge.sqlite3"
        build_sqlite_database(
            PROJECT_ROOT / "database" / "test1_full_20241118.sql", cls.database
        )
        with KnowledgeStore(cls.knowledge) as store:
            store.ingest_database(SNAPSHOT, JOIN_CATALOG)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_runtime_tool_policy_can_remove_but_never_add_permissions(self):
        suite = Text2SQLToolSuite(
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            principals=["local-user"],
            memory_snapshot_id="memory-test",
            policy_version="policy-test",
        )
        registry = suite.registry("schema-grounding", ["inspect_schema"])
        self.assertEqual(registry.names(), ["inspect_schema"])
        with self.assertRaisesRegex(ValueError, "cannot expand"):
            suite.registry("schema-grounding", ["execute_sql"])

    def test_engine_preloads_only_stable_memory_before_worker_threads(self):
        with Text2SQLEvolutionStore(self.root / "runtime-evolution.sqlite3", SNAPSHOT) as store:
            memory_id = store.add_memory_candidate(
                "query-planning",
                "AGGREGATION_MISMATCH",
                "Compare COUNT(*) and COUNT(DISTINCT key) against the intended grain.",
                {"source": "production-correction"},
                "production_feedback",
            )
            store.review_memory(memory_id, "approve", "reviewer", True)
            with store.connection:
                store.connection.execute(
                    "UPDATE memory_items SET state='evaluated' WHERE memory_id=?",
                    (memory_id,),
                )
            store.activate_memory(memory_id, "reviewer", "test release", True)
            policy = store.get_policy()
            engine = Text2SQLAgenticEngine(
                client=object(),
                database_path=self.database,
                snapshot=SNAPSHOT,
                knowledge_store_path=self.knowledge,
                principals=["local-user"],
                memory_snapshot_id=store.memory_snapshot_id,
                policy_version=policy.version,
                policy_artifact=policy,
                stable_memory_provider=store.stable_memory,
            )
        self.assertEqual(len(engine._stable_memory["query-planning"]), 1)
        self.assertEqual(engine._stable_memory["schema-grounding"], ())
        self.assertEqual(
            [item["memory_id"] for item in engine._relevant_memory(
                "query-planning", "按岩爆等级统计案例数量"
            )],
            [memory_id],
        )
        self.assertEqual(
            engine._relevant_memory("query-planning", "列出施工单位名称"), []
        )


if __name__ == "__main__":
    unittest.main()
