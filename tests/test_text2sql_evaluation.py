import json
import shutil
import tempfile
import unittest
from collections import Counter, defaultdict
from pathlib import Path

from evoagent.text2sql.dataset_builder import CATEGORY_TARGETS, build_dataset
from evoagent.text2sql.evaluation import (
    Text2SQLEvaluator,
    _failure_from_gate,
    _sql_features,
    classify_result_mismatch,
    load_dataset,
)
from evoagent.text2sql.sql_safety import ReadOnlySQLiteExecutor
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


class Text2SQLDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        cls.database = root / "eval.sqlite3"
        cls.dataset = root / "dataset"
        build_sqlite_database(
            PROJECT_ROOT / "database" / "test1_full_20241118.sql", cls.database
        )
        cls.manifest = build_dataset(
            SNAPSHOT, JOIN_CATALOG, cls.database, cls.dataset
        )

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_dataset_has_exact_independent_category_and_split_counts(self):
        bundle = load_dataset(self.dataset)
        self.assertEqual(len(bundle.cases), 240)
        self.assertEqual(Counter(case.category for case in bundle.cases), CATEGORY_TARGETS)
        self.assertEqual(
            Counter(case.split for case in bundle.cases),
            {"train": 144, "validation": 48, "sealed_holdout": 48},
        )
        skeleton_splits = defaultdict(set)
        for case in bundle.cases:
            skeleton_splits[case.sql_skeleton].add(case.split)
        self.assertTrue(all(len(splits) == 1 for splits in skeleton_splits.values()))
        self.assertEqual(bundle.database_snapshot_id, SNAPSHOT["snapshot_id"])
        self.assertEqual(
            self.manifest["review_status"],
            "machine_validated_pending_human_review",
        )
        self.assertFalse(self.manifest["release_eligible"])
        self.assertFalse(bundle.review_evidence["verified"])

    def test_dataset_build_is_byte_deterministic_and_tamper_evident(self):
        second = Path(self.temporary.name) / "dataset-second"
        manifest = build_dataset(SNAPSHOT, JOIN_CATALOG, self.database, second)
        self.assertEqual(manifest["dataset_sha256"], self.manifest["dataset_sha256"])
        self.assertEqual(
            (second / "validation.jsonl").read_bytes(),
            (self.dataset / "validation.jsonl").read_bytes(),
        )
        tampered = Path(self.temporary.name) / "dataset-tampered"
        shutil.copytree(self.dataset, tampered)
        with (tampered / "train.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("{}\n")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            load_dataset(tampered)

    def test_evaluator_only_passes_question_and_redacts_holdout_details(self):
        bundle = load_dataset(self.dataset)
        chosen = []
        for split in ("train", "validation", "sealed_holdout"):
            chosen.extend([case for case in bundle.cases if case.split == split][:2])
        by_question = {case.question: case for case in chosen}
        executor = ReadOnlySQLiteExecutor(self.database, SNAPSHOT, max_rows=10_000)
        pins = {
            "database_snapshot_id": SNAPSHOT["snapshot_id"],
            "wiki_index_version": "stable-v1-test",
            "memory_snapshot_id": "memory-empty-v1",
            "policy_version": "policy-v1",
        }
        received = []

        def perfect_runner(question):
            received.append(question)
            case = by_question[question]
            answer = executor.execute(case.gold_sql).as_dict()
            return {
                "status": "success",
                "final_sql": case.gold_sql,
                "answer": answer,
                "gates": {"accepted": True, "errors": []},
                "version_pins": pins,
                "collaboration": {
                    "bound_query_plan": {
                        "schema_plan": {
                            "tables": list(case.required_tables),
                            "columns": list(case.required_columns),
                            "joins": [
                                {"evidence_id": value}
                                for value in case.required_relationships
                            ],
                        }
                    },
                    "worker_results": [
                        {
                            "worker": "schema-grounding",
                            "output": {
                                "schema_plan": {
                                    # Canonical BoundQueryPlan must take priority
                                    # over a stale compatibility worker payload.
                                    "tables": [],
                                    "columns": [],
                                    "joins": [],
                                }
                            },
                        }
                    ]
                },
            }

        report = Text2SQLEvaluator(self.database, SNAPSHOT, pins).evaluate(
            chosen, perfect_runner
        )
        self.assertEqual(received, [case.question for case in chosen])
        self.assertEqual(report["overall"]["execution_accuracy"], 1.0)
        self.assertEqual(report["overall"]["table_recall"], 1.0)
        self.assertEqual(report["overall"]["column_recall"], 1.0)
        holdout = next(
            item for item in report["outcomes"] if item["split"] == "sealed_holdout"
        )
        self.assertNotIn("question", holdout)
        self.assertNotIn("candidate_sql", holdout)

    def test_failure_attribution_distinguishes_filters_and_aggregation(self):
        self.assertEqual(
            classify_result_mismatch(
                "SELECT COUNT(*) FROM t_casedesc WHERE c_rockLevel='强烈'",
                "SELECT COUNT(*) FROM t_casedesc WHERE c_rockLevel='轻微'",
                1,
            ),
            "FILTER_MISMATCH",
        )
        self.assertEqual(
            classify_result_mismatch(
                "SELECT COUNT(*) FROM t_casedesc",
                "SELECT MAX(c_caseCode) FROM t_casedesc",
                1,
            ),
            "AGGREGATION_MISMATCH",
        )

    def test_plan_gate_failures_keep_semantic_evaluation_categories(self):
        self.assertEqual(
            _failure_from_gate(["missing_value_binding"]),
            "SCHEMA_LINK_MISMATCH",
        )
        self.assertEqual(
            _failure_from_gate(["filter_mismatch"]),
            "FILTER_MISMATCH",
        )
        self.assertEqual(
            _failure_from_gate(["distinct_mismatch"]),
            "AGGREGATION_MISMATCH",
        )
        self.assertEqual(
            _failure_from_gate(["unexpected_join"]),
            "JOIN_OR_GRAIN_MISMATCH",
        )

    def test_value_grounding_normalizes_equivalent_numeric_literals(self):
        quoted = _sql_features("SELECT * FROM t_activeinfo WHERE c_energy='4.986E+06'")
        numeric = _sql_features("SELECT * FROM t_activeinfo WHERE c_energy=4986000.00")
        self.assertEqual(quoted["literals"], numeric["literals"])

    def test_evaluator_resumes_from_case_outcomes_and_preserves_usage(self):
        cases = list(load_dataset(self.dataset, ["validation"]).cases[:3])
        by_question = {case.question: case for case in cases}
        executor = ReadOnlySQLiteExecutor(self.database, SNAPSHOT, max_rows=10_000)
        pins = {
            "database_snapshot_id": SNAPSHOT["snapshot_id"],
            "wiki_index_version": "wiki-resume-test",
            "memory_snapshot_id": "memory-resume-test",
            "policy_version": "policy-resume-test",
        }
        received = []

        def runner(question):
            received.append(question)
            case = by_question[question]
            return {
                "status": "success",
                "final_sql": case.gold_sql,
                "answer": executor.execute(case.gold_sql).as_dict(),
                "gates": {"accepted": True, "errors": []},
                "version_pins": pins,
                "collaboration": {"worker_results": []},
                "execution": {
                    "llm_calls": 2,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                    "cost_usd": 0.01,
                },
            }

        evaluator = Text2SQLEvaluator(self.database, SNAPSHOT, pins)
        partial = evaluator.evaluate(
            cases,
            runner,
            should_continue=lambda outcomes: len(outcomes) < 1,
        )
        self.assertEqual(len(partial["outcomes"]), 1)
        received.clear()
        resumed = evaluator.evaluate(
            cases, runner, existing_outcomes=partial["outcomes"]
        )
        self.assertEqual(len(received), 2)
        self.assertEqual(resumed["overall"]["cases"], 3)
        self.assertEqual(resumed["overall"]["llm_calls"], 6)
        self.assertEqual(resumed["overall"]["total_tokens"], 45)


if __name__ == "__main__":
    unittest.main()
