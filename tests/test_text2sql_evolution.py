import json
import tempfile
import unittest
from pathlib import Path

from evoagent.text2sql.agentic import Text2SQLAgenticEngine
from evoagent.text2sql.database_tools import Text2SQLToolSuite
from evoagent.text2sql.evolution import (
    Text2SQLEvolutionStore,
    evaluate_promotion_gate,
)
from evoagent.text2sql.knowledge_store import KnowledgeStore
from evoagent.text2sql.policy import PolicyArtifact
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
            }
            for index in range(10)
        )
    return {"version_pins": pins, "overall": {}, "splits": splits, "outcomes": outcomes}


class PolicyArtifactTests(unittest.TestCase):
    def test_policy_rejects_unknown_fields_unsafe_sql_and_privilege_expansion(self):
        with self.assertRaisesRegex(ValueError, "forbidden field"):
            PolicyArtifact.from_dict({"deterministic_gates": {"disabled": True}}, SNAPSHOT)
        with self.assertRaisesRegex(ValueError, "safety validation"):
            PolicyArtifact.from_dict(
                {
                    "few_shot_examples": {
                        "sql-strategy": [
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

    def test_policy_is_complete_hashed_and_tool_policy_only_reduces(self):
        value = PolicyArtifact.baseline(SNAPSHOT).as_dict()
        value["prompt_fragments"]["sql-strategy"] = (
            "Before finalizing, explicitly compare COUNT(*) with COUNT(DISTINCT key)."
        )
        value["tool_selection_policy"]["sql-strategy"] = [
            "inspect_schema",
            "validate_sql",
            "explain_sql",
        ]
        artifact = PolicyArtifact.from_dict(value, SNAPSHOT)
        self.assertTrue(artifact.version.startswith("policy-"))
        self.assertEqual(
            artifact.changed_skills(PolicyArtifact.baseline(SNAPSHOT)),
            ("sql-strategy",),
        )
        self.assertNotIn("sample_values", artifact.role_policy("sql-strategy")["allowed_tools"])

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
            "sql-strategy",
            SNAPSHOT,
        )
        candidate = PolicyArtifact.from_dict(generated["artifact"], SNAPSHOT)
        self.assertEqual(
            candidate.changed_skills(PolicyArtifact.baseline(SNAPSHOT)),
            ("sql-strategy",),
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
        value["prompt_fragments"]["sql-strategy"] = (
            "Check duplicate fanout and state the intended result grain."
        )
        return self.store.propose_policy(
            value,
            "sql-strategy",
            "Repeated duplicate-count failures",
            "test-author",
        )

    def test_candidate_must_change_exactly_its_declared_skill(self):
        unchanged = self.store.get_policy().as_dict()
        with self.assertRaisesRegex(ValueError, "change exactly"):
            self.store.propose_policy(
                unchanged, "sql-strategy", "no effective change", "test-author"
            )
        changed = self.store.get_policy().as_dict()
        changed["prompt_fragments"]["sql-strategy"] = "Check result grain."
        changed["prompt_fragments"]["schema-grounding"] = "Check exact names."
        with self.assertRaisesRegex(ValueError, "change exactly"):
            self.store.propose_policy(
                changed, "sql-strategy", "two role changes", "test-author"
            )

    def test_memory_is_candidate_only_until_human_review_and_holdout_is_forbidden(self):
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
        self.store.review_memory(memory_id, "approve", "reviewer", True)
        self.assertEqual(len(self.store.stable_memory("schema-grounding")), 1)
        self.assertNotEqual(before, self.store.memory_snapshot_id)
        with self.assertRaisesRegex(ValueError, "holdout"):
            self.store.add_memory_candidate(
                "schema-grounding", "UNKNOWN_COLUMN", "bad", {}, "sealed_holdout"
            )

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
                "sql-strategy",
                "AGGREGATION_MISMATCH",
                "Compare COUNT(*) and COUNT(DISTINCT key) against the intended grain.",
                {"source": "production-correction"},
                "production_feedback",
            )
            store.review_memory(memory_id, "approve", "reviewer", True)
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
        self.assertEqual(len(engine._stable_memory["sql-strategy"]), 1)
        self.assertEqual(engine._stable_memory["schema-grounding"], ())


if __name__ == "__main__":
    unittest.main()
