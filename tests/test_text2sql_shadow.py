import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from evoagent.text2sql.evolution import Text2SQLEvolutionStore
from evoagent.text2sql.shadow import (
    Text2SQLShadowReleaseManager,
    compare_shadow_results,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = json.loads(
    (PROJECT_ROOT / "artifacts" / "text2sql" / "schema" / "database_snapshot.json").read_text(
        encoding="utf-8"
    )
)


def _evaluation_report(policy_version, validation_passed):
    pins = {
        "database_snapshot_id": SNAPSHOT["snapshot_id"],
        "wiki_index_version": "stable-wiki-shadow-test",
        "memory_snapshot_id": "memory-shadow-test",
        "policy_version": policy_version,
    }
    outcomes = []
    splits = {}
    for split, passed in (("validation", validation_passed), ("sealed_holdout", 8)):
        accuracy = passed / 10
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
        outcomes.extend(
            {
                "case_id": "%s-%02d" % (split, index),
                "split": split,
                "execution_accuracy": index < passed,
            }
            for index in range(10)
        )
    return {"version_pins": pins, "overall": {}, "splits": splits, "outcomes": outcomes}


def _online_result(sql="SELECT 1", value=1, wiki="wiki:private-page"):
    return {
        "status": "success",
        "final_sql": sql,
        "answer": {"columns": ["value"], "rows": [[value]], "truncated": False},
        "gates": {"accepted": True, "errors": []},
        "collaboration": {
            "worker_results": [
                {
                    "worker": "schema-grounding",
                    "observed_evidence_ids": [wiki],
                    "output": {},
                }
            ]
        },
        "execution": {"duration_ms": 1},
    }


class ShadowDiffTests(unittest.TestCase):
    def test_diff_is_result_aware_and_redacts_raw_sql_results_and_wiki_ids(self):
        stable = _online_result("SELECT 1", 1, "wiki:restricted-stable")
        candidate = _online_result("SELECT 1 AS value", 1, "wiki:restricted-candidate")
        diff = compare_shadow_results(stable, candidate)
        self.assertTrue(diff["result_equivalent"])
        self.assertTrue(diff["sql_changed"])
        self.assertTrue(diff["wiki_refs_changed"])
        rendered = json.dumps(diff, ensure_ascii=False)
        self.assertNotIn("restricted-stable", rendered)
        self.assertNotIn("restricted-candidate", rendered)
        self.assertNotIn("SELECT 1 AS value", rendered)
        self.assertNotIn('[[1]]', rendered)


class ShadowReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Text2SQLEvolutionStore(
            Path(self.temporary.name) / "evolution.sqlite3", SNAPSHOT
        )
        self.release = Text2SQLShadowReleaseManager(self.store)
        self.baseline = self.store.active_policy_version
        artifact = self.store.get_policy().as_dict()
        artifact["prompt_fragments"]["sql-strategy"] = "Always state result grain."
        self.candidate = self.store.propose_policy(
            artifact, "sql-strategy", "shadow test candidate", "test-author"
        )
        manifest = {
            "dataset_id": "shadow-dataset",
            "dataset_sha256": "shadow-sha",
            "database_snapshot_id": SNAPSHOT["snapshot_id"],
            "release_eligible": True,
            "review_status": "human_reviewed",
            "human_reviewed_cases": 20,
            "files": {
                "validation": {"case_count": 10},
                "sealed_holdout": {"case_count": 10},
            },
        }
        self.store.record_evaluation(
            self.candidate,
            manifest,
            _evaluation_report(self.baseline, 4),
            _evaluation_report(self.candidate, 9),
            {
                "verified": True,
                "dataset_sha256": "shadow-sha",
                "reviewed_case_count": 20,
                "certificate_sha256": "shadow-certificate-sha",
            },
        )
        self.pins = {
            "database_snapshot_id": SNAPSHOT["snapshot_id"],
            "wiki_index_version": "stable-wiki-shadow-test",
            "memory_snapshot_id": "memory-shadow-test",
            "policy_version": self.baseline,
        }

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def _configure(self, min_samples=1, shadow_percent=100):
        return self.release.configure_shadow(
            self.candidate,
            self.pins,
            "release-operator",
            shadow_percent=shadow_percent,
            min_samples=min_samples,
            max_candidate_failure_rate=0.0,
            max_result_disagreement_rate=0.2,
            max_p95_latency_multiplier=5.0,
        )

    def test_shadow_review_canary_and_manual_activation_lifecycle(self):
        deployment = self._configure()
        stable = _online_result("SELECT 1", 1, "wiki:stable-private")
        candidate = _online_result("SELECT 1 AS value", 1, "wiki:candidate-private")
        result = self.release.execute(
            "private user question",
            "task-shadow-1",
            lambda _question: stable,
            lambda _version: lambda _question: candidate,
        )
        self.assertEqual(result["final_sql"], "SELECT 1")
        self.assertFalse(result["release"]["candidate_output_used"])
        self.assertEqual(result["release"]["deployment_status"], "shadow_review")
        observations = self.release.list_observations(deployment["deployment_id"])
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["review_state"], "pending")
        persisted = json.dumps(observations[0], ensure_ascii=False)
        self.assertNotIn("private user question", persisted)
        self.assertNotIn("stable-private", persisted)
        self.assertNotIn("candidate-private", persisted)

        self.release.review_observation(
            observations[0]["observation_id"],
            "equivalent",
            "human-reviewer",
            "Same result and acceptable query shape.",
            True,
        )
        self.release.approve_shadow(
            deployment["deployment_id"],
            "human-reviewer",
            "Reviewed every recorded difference.",
            True,
        )
        self.release.start_canary(
            deployment["deployment_id"], "release-operator", 100, 1
        )
        canary_result = self.release.execute(
            "another private question",
            "task-canary-1",
            lambda _question: stable,
            lambda _version: lambda _question: candidate,
        )
        self.assertTrue(canary_result["release"]["candidate_output_used"])
        self.assertEqual(canary_result["release"]["deployment_status"], "canary_passed")
        self.store.activate_policy(
            self.candidate, "human-reviewer", "Canary completed without failures.", True
        )
        self.assertEqual(self.store.active_policy_version, self.candidate)
        self.assertEqual(
            self.release.get_deployment(deployment["deployment_id"])["status"],
            "stable",
        )
        self.store.rollback(
            self.baseline, "human-reviewer", "Verify joint release rollback."
        )
        self.assertEqual(self.store.active_policy_version, self.baseline)
        self.assertEqual(
            self.release.get_deployment(deployment["deployment_id"])["status"],
            "rolled_back",
        )
        actions = {
            row[0]
            for row in self.store.connection.execute(
                "SELECT action FROM activation_audit"
            ).fetchall()
        }
        self.assertTrue(
            {"shadow_configure", "shadow_approve", "canary_start", "approve", "rollback"}
            .issubset(actions)
        )

    def test_candidate_runtime_failure_falls_back_and_rolls_back_immediately(self):
        deployment = self._configure(min_samples=20)

        def failed_candidate(_question):
            raise RuntimeError("provider unavailable with sensitive detail")

        result = self.release.execute(
            "question must not persist",
            "task-failure",
            lambda _question: _online_result(),
            lambda _version: failed_candidate,
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["release"]["deployment_status"], "rolled_back")
        self.assertEqual(self.store.active_policy_version, self.baseline)
        policy = next(
            item for item in self.store.list_policies()
            if item["policy_version"] == self.candidate
        )
        self.assertEqual(policy["status"], "shadow_rejected")
        observation = self.release.list_observations(deployment["deployment_id"])[0]
        persisted = json.dumps(observation, ensure_ascii=False)
        self.assertNotIn("sensitive detail", persisted)
        self.assertNotIn("question must not persist", persisted)

    def test_retried_task_does_not_double_count_shadow_observation(self):
        deployment = self._configure(min_samples=10)
        stable = _online_result("SELECT 1", 1)
        candidate = _online_result("SELECT 1 AS value", 1)
        first = self.release.execute(
            "same question",
            "retryable-task",
            lambda _question: stable,
            lambda _version: lambda _question: candidate,
        )
        second = self.release.execute(
            "same question",
            "retryable-task",
            lambda _question: stable,
            lambda _version: lambda _question: candidate,
        )
        observations = self.release.list_observations(deployment["deployment_id"])
        current = self.release.get_deployment(deployment["deployment_id"])
        self.assertEqual(len(observations), 1)
        self.assertEqual(current["shadow_samples"], 1)
        self.assertEqual(
            first["release"]["observation_id"],
            second["release"]["observation_id"],
        )

    def test_retry_finishes_safety_transition_after_committed_observation(self):
        deployment = self._configure(min_samples=20)
        task_key = "crash-gap-task"
        diff = compare_shadow_results(
            _online_result(), None, "candidate process crashed"
        )
        timestamp = "2026-01-01T00:00:00+00:00"
        with self.store.connection:
            self.store.connection.execute(
                "INSERT INTO shadow_observations("
                "observation_id,deployment_id,task_key_hash,assignment_bucket,lane,"
                "stable_policy_version,candidate_policy_version,diff_json,"
                "stable_latency_ms,candidate_latency_ms,review_state,created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "partially-committed-observation",
                    deployment["deployment_id"],
                    hashlib.sha256(task_key.encode("utf-8")).hexdigest(),
                    1,
                    "stable",
                    self.baseline,
                    self.candidate,
                    json.dumps(diff, sort_keys=True, separators=(",", ":")),
                    1.0,
                    1.0,
                    "pending",
                    timestamp,
                ),
            )
            self.store.connection.execute(
                "UPDATE shadow_deployments SET shadow_samples=1,"
                "shadow_candidate_failures=1 WHERE deployment_id=?",
                (deployment["deployment_id"],),
            )
        recovered = self.release._record_observation(
            deployment, task_key, 1, "stable", diff, 1.0, 1.0
        )
        self.assertEqual(recovered["deployment_status"], "rolled_back")
        self.assertEqual(
            self.release.get_deployment(deployment["deployment_id"])["shadow_samples"],
            1,
        )

    def test_assignment_is_deterministic_and_defaults_to_five_percent_shadow(self):
        self._configure(min_samples=1000, shadow_percent=5)
        first = self.release.assignment("same-task")
        second = self.release.assignment("same-task")
        self.assertEqual(first["bucket"], second["bucket"])
        self.assertEqual(first["shadow"], second["shadow"])
        sampled = sum(
            self.release.assignment("task-%d" % index)["shadow"]
            for index in range(1000)
        )
        self.assertGreater(sampled, 20)
        self.assertLess(sampled, 80)

    def test_version_pin_drift_blocks_shadow_configuration(self):
        drifted = dict(self.pins)
        drifted["memory_snapshot_id"] = "memory-changed"
        with self.assertRaisesRegex(ValueError, "replay is required"):
            self.release.configure_shadow(
                self.candidate, drifted, "release-operator"
            )

    def test_runtime_pin_drift_stops_an_existing_shadow(self):
        deployment = self._configure(min_samples=10)
        drifted = dict(self.pins)
        drifted["wiki_index_version"] = "stable-wiki-changed"
        assignment = self.release.assignment("task-after-wiki-change", drifted)
        self.assertFalse(assignment["shadow"])
        self.assertEqual(
            self.release.get_deployment(deployment["deployment_id"])["status"],
            "rolled_back",
        )


if __name__ == "__main__":
    unittest.main()
