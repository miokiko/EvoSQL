import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from evoagent.text2sql.agentic import build_runtime_identity
from evoagent.text2sql.auto_evolution import (
    automatic_experience_decision,
    prepare_automatic_policy_candidate,
)
from evoagent.text2sql.evolution import Text2SQLEvolutionStore
from evoagent.text2sql.target_replay import build_replay_identity, run_target_replay
from test_text2sql_query_plan import SNAPSHOT
from test_text2sql_semantic_rules import ScriptedClient, VALID_SQL, source_case


class AutomaticExperienceAdmissionTests(unittest.TestCase):
    def test_only_complete_before_after_evidence_is_admitted(self):
        item = {
            "memory_id": "memory-auto",
            "state": "candidate",
            "runtime_eligible": False,
            "rule": {
                "contract": "ExperienceMemory/v1",
                "source_task_id": "query-1",
                "target_agent": "query-planning",
                "evidence_grade": "deterministic_plan_revision",
                "before": {
                    "issue_present": True,
                    "worker_plan_fingerprint": "before",
                },
                "after": {
                    "issue_resolved": True,
                    "worker_plan_fingerprint": "after",
                    "approved_plan_fingerprint": "approved",
                },
                "evidence": {},
            },
        }
        self.assertTrue(automatic_experience_decision(item)["eligible"])
        item["rule"]["after"]["issue_resolved"] = False
        decision = automatic_experience_decision(item)
        self.assertFalse(decision["eligible"])
        self.assertIn("plan_issue_not_resolved", decision["reasons"])

    def test_prose_only_feedback_cannot_self_promote(self):
        decision = automatic_experience_decision(
            {
                "memory_id": "memory-feedback",
                "state": "candidate",
                "runtime_eligible": False,
                "rule": {
                    "contract": "ExperienceMemory/v1",
                    "source_task_id": "query-2",
                    "target_agent": "text2sql-critic",
                    "evidence_grade": "human_feedback_only",
                    "before": {},
                    "after": {},
                    "evidence": {},
                },
            }
        )
        self.assertFalse(decision["eligible"])
        self.assertIn("evidence_grade_not_automatic", decision["reasons"])


class AutomaticEvolutionFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Text2SQLEvolutionStore(
            Path(self.temporary.name) / "evolution.sqlite3", SNAPSHOT
        )

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    @staticmethod
    def _identity(memory_ids=()):
        return {
            "model": {
                "provider": "scripted",
                "model": "automatic-evolution-test",
                "temperature": 0,
            },
            "runtime": dict(
                build_runtime_identity(
                    token_budget=8000,
                    time_budget=60,
                    policy_source_memory_ids=memory_ids,
                )
            ),
            "principals": ["local-user"],
        }

    def _record_passed_offline_gate(self, candidate_version, parent_version):
        pins = {
            "database_snapshot_id": SNAPSHOT["snapshot_id"],
            "wiki_index_version": "vanna-rule-fixture",
            "vanna_index_version": "vanna-rule-fixture",
            "memory_snapshot_id": self.store.memory_snapshot_id,
            "policy_version": parent_version,
        }
        candidate_pins = {**pins, "policy_version": candidate_version}
        baseline_identity = self._identity(
            self.store.policy_source_memory_ids(parent_version)
        )
        candidate_identity = self._identity(
            self.store.policy_source_memory_ids(candidate_version)
        )
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE policy_versions SET status='shadow_ready' WHERE policy_version=?",
                (candidate_version,),
            )
            self.store.connection.execute(
                "INSERT INTO evolution_runs VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "evolution-run-auto-test",
                    parent_version,
                    candidate_version,
                    "dataset-auto-test",
                    "dataset-sha-auto-test",
                    json.dumps(
                        {
                            "version_pins": pins,
                            "evaluation_identity": baseline_identity,
                        },
                        sort_keys=True,
                    ),
                    json.dumps(
                        {
                            "version_pins": candidate_pins,
                            "evaluation_identity": candidate_identity,
                        },
                        sort_keys=True,
                    ),
                    json.dumps(
                        {
                            "eligible_for_human_approval": True,
                            "reasons": [],
                        },
                        sort_keys=True,
                    ),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return pins, baseline_identity

    def test_machine_verifiable_experience_reaches_prompt_candidate(self):
        memory_id, _ = source_case(self.store, confirm=False)
        client = ScriptedClient()
        result = prepare_automatic_policy_candidate(
            self.store, client, memory_id
        )
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["target_agent"], "sql-generation")
        self.assertEqual(self.store.get_memory(memory_id)["state"], "confirmed")
        self.assertEqual(
            self.store.get_semantic_rule(result["semantic_rule_id"])["state"],
            "confirmed",
        )
        self.assertEqual(
            self.store.validate_experience_policy_lineage(
                result["candidate_policy_version"]
            )["memory_ids"],
            (memory_id,),
        )
        again = prepare_automatic_policy_candidate(
            self.store, client, memory_id
        )
        self.assertTrue(again["reused"])
        self.assertEqual(
            again["candidate_policy_version"],
            result["candidate_policy_version"],
        )
        self.assertEqual(len(client.calls), 2)

    def _record_passed_target_replay(self, result, trace):
        candidate = result["candidate_policy_version"]
        parent = result["parent_policy_version"]
        memory_id = result["memory_id"]
        pins = {
            "database_snapshot_id": SNAPSHOT["snapshot_id"],
            "wiki_index_version": "vanna-rule-fixture",
            "vanna_index_version": "vanna-rule-fixture",
            "memory_snapshot_id": self.store.memory_snapshot_id,
        }
        identity = build_replay_identity(
            parent_version_pins={**pins, "policy_version": parent},
            candidate_version_pins={**pins, "policy_version": candidate},
            parent_runtime=self._identity()["runtime"],
            candidate_runtime=self._identity((memory_id,))["runtime"],
            model=self._identity()["model"],
            principals=("local-user",),
        )

        def runner(repairs):
            def run(_question, _task_id):
                return {
                    "status": "success",
                    "final_sql": VALID_SQL,
                    "gates": {"accepted": True, "errors": []},
                    "collaboration": {"sql_generation_repairs": repairs},
                }

            return run

        artifact = run_target_replay(
            [self.store.get_memory(memory_id)],
            {memory_id: trace},
            runner(1),
            runner(0),
            parent_policy_version=parent,
            candidate_policy_version=candidate,
            replay_identity=identity,
        )
        self.assertEqual(artifact["status"], "passed")
        self.store.record_target_replay(
            candidate, artifact, created_by="text2sql-auto-evolution"
        )

    def test_offline_gate_can_atomically_activate_evolved_candidate(self):
        memory_id, trace = source_case(self.store, confirm=False)
        result = prepare_automatic_policy_candidate(
            self.store, ScriptedClient(), memory_id
        )
        candidate = result["candidate_policy_version"]
        parent = result["parent_policy_version"]
        self._record_passed_target_replay(result, trace)
        pins, identity = self._record_passed_offline_gate(
            candidate, parent
        )
        self.store.activate_policy_automatically(
            candidate,
            "text2sql-auto-evolution",
            "all release gates passed",
            pins,
            identity,
        )
        self.assertEqual(self.store.active_policy_version, candidate)
        policy = self.store.policy_record(candidate)
        self.assertEqual(policy["status"], "approved")

    def test_automatic_activation_rejects_non_experience_candidate(self):
        parent = self.store.get_policy()
        artifact = parent.as_dict()
        artifact["prompt_fragments"]["text2sql-lead"] = "Keep routing bounded."
        candidate = self.store.propose_policy(
            artifact,
            "text2sql-lead",
            "manual prompt candidate",
            "test-author",
            parent.version,
        )
        pins, identity = self._record_passed_offline_gate(candidate, parent.version)
        with self.assertRaisesRegex(ValueError, "SemanticRule-compiled"):
            self.store.activate_policy_automatically(
                candidate,
                "text2sql-auto-evolution",
                "all release gates passed",
                pins,
                identity,
            )

    def test_experience_candidate_still_requires_target_replay(self):
        memory_id, _ = source_case(self.store, confirm=False)
        result = prepare_automatic_policy_candidate(
            self.store, ScriptedClient(), memory_id
        )
        candidate = result["candidate_policy_version"]
        parent = result["parent_policy_version"]
        pins, identity = self._record_passed_offline_gate(candidate, parent)
        with self.assertRaisesRegex(ValueError, "Target Replay"):
            self.store.activate_policy_automatically(
                candidate,
                "text2sql-auto-evolution",
                "all release gates passed",
                pins,
                identity,
            )


if __name__ == "__main__":
    unittest.main()
