import hashlib
import json
import copy
import unittest

from evoagent.text2sql.target_replay import (
    build_replay_identity,
    experience_has_replay_proof,
    evaluate_target_replay,
    result_issue_codes,
    run_target_replay,
    validate_target_replay_artifact,
)
from scripts import run_text2sql_target_replay as replay_script


PARENT_POLICY = "policy-parent"
CANDIDATE_POLICY = "policy-candidate"
SHARED_PINS = {
    "database_snapshot_id": "database-v1",
    "wiki_index_version": "vanna-v1",
    "vanna_index_version": "vanna-v1",
    "memory_snapshot_id": "memory-v1",
}


def _sha_text(value):
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def _plan_revision_proof(code):
    request_fingerprint = "c" * 64
    return {
        "before": {
            "issue_code": code,
            "issue_present": True,
            "revision_request_fingerprint": request_fingerprint,
            "worker_plan_fingerprint": "d" * 64,
        },
        "after": {
            "issue_resolved": True,
            "worker_plan_fingerprint": "e" * 64,
            "approved_plan_fingerprint": "a" * 64,
            "bound_plan_fingerprint": "b" * 64,
        },
        "applicability": {"issue_code": code},
        "evidence": {
            "revision_request_fingerprint": request_fingerprint,
            "initial_binding_conflicts_fingerprint": "f" * 64,
            "approved_plan_fingerprint": "a" * 64,
            "bound_plan_fingerprint": "b" * 64,
        },
    }


def _rehash_artifact(value):
    payload = {key: item for key, item in value.items() if key != "artifact_sha256"}
    value["artifact_sha256"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return value


def _identity():
    runtime = {
        "protocol": "plan-first-v3",
        "build_version": "build-v10",
        "gate_implementation_version": "gate-v1",
        "nodes": ["one", "two"],
        "plan_contracts": ["ApprovedQueryPlan/v1"],
        "max_candidates": 3,
        "max_plan_revisions_per_worker": 1,
        "max_sql_repairs": 1,
        "token_budget": 5000,
        "time_budget": 60,
        "max_rows": 200,
        "timeout_ms": 3000,
    }
    return build_replay_identity(
        parent_version_pins={**SHARED_PINS, "policy_version": PARENT_POLICY},
        candidate_version_pins={
            **SHARED_PINS,
            "policy_version": CANDIDATE_POLICY,
        },
        parent_runtime={
            **runtime,
            "policy_source_memory_ids": ["memory-existing"],
        },
        candidate_runtime={
            **runtime,
            "policy_source_memory_ids": ["memory-existing", "memory-source"],
        },
        model={"provider": "test", "model": "model-v1", "temperature": 0},
        principals=("private-user@example.invalid",),
    )


def _experience(
    memory_id,
    *,
    stage,
    problem_code,
    target_agent="query-planning",
    before=None,
    after=None,
    applicability=None,
    evidence=None,
    source_task_id="source-task-1",
    source_revision=1,
):
    return {
        "memory_id": memory_id,
        "state": "confirmed",
        "rule": {
            "contract": "ExperienceMemory/v1",
            "memory_id": memory_id,
            "state": "confirmed",
            "source_task_id": source_task_id,
            "source_revision": source_revision,
            "target_agent": target_agent,
            "source_stage": stage,
            "problem_code": problem_code,
            "scenario": "bounded scenario",
            "problem": "bounded problem",
            "correction": "bounded correction",
            "before": before or {},
            "after": after or {},
            "applicability": applicability or {},
            "evidence": evidence or {},
            "evidence_grade": "deterministic",
        },
    }


def _trace(task_id="source-task-1", question="统计案例数", source_revision=1):
    return {
        "task_id": task_id,
        "source_revision": source_revision,
        "query_type": "DATA_QUERY",
        "standalone_question": question,
        "version_pins": {**SHARED_PINS, "policy_version": "policy-source"},
        "final_sql": "SELECT secret_column FROM secret_table",
        "result_rows": [["secret-result"]],
    }


def _success(sql="SELECT 1", collaboration=None):
    return {
        "status": "success",
        "final_sql": sql,
        "gates": {"accepted": True, "errors": []},
        "answer": {"rows": [["must-not-leak"]]},
        "collaboration": collaboration or {},
    }


class TargetReplayIdentityTests(unittest.TestCase):
    def test_lanes_must_share_every_non_policy_input(self):
        identity = _identity()

        self.assertEqual(
            identity["shared_version_pins"]["database_snapshot_id"],
            "database-v1",
        )
        self.assertNotIn("private-user@example.invalid", json.dumps(identity))

        with self.assertRaisesRegex(ValueError, "share vanna_index_version"):
            build_replay_identity(
                parent_version_pins={
                    **SHARED_PINS,
                    "policy_version": PARENT_POLICY,
                },
                candidate_version_pins={
                    **SHARED_PINS,
                    "vanna_index_version": "vanna-v2",
                    "policy_version": CANDIDATE_POLICY,
                },
                parent_runtime={"token_budget": 5000},
                candidate_runtime={"token_budget": 5000},
                model={"provider": "test", "model": "model", "temperature": 0},
                principals=("user",),
            )

    def test_unstructured_errors_are_hashed_instead_of_copied(self):
        secret = "SELECT secret_column FROM secret_table WHERE token='secret'"
        codes = result_issue_codes({"gates": {"errors": [secret]}, "collaboration": {}})

        self.assertEqual(len(codes), 1)
        self.assertTrue(codes[0].startswith("unstructured_issue."))
        self.assertNotIn("select", codes[0])


class TargetReplayCliContractTests(unittest.TestCase):
    def test_candidate_metadata_is_authoritative_and_partial_replay_is_rejected(self):
        class Store:
            def policy_source_memory_ids(self, _version):
                raise AssertionError("metadata must be used instead of inference")

        record = {
            "policy_version": CANDIDATE_POLICY,
            "parent_version": PARENT_POLICY,
            "proposal_metadata": {
                "contract": "ExperiencePolicyProposal/v1",
                "source": "confirmed-experiences",
                "memory_ids": ["memory-one", "memory-two"],
                "target_replay_required": True,
            },
        }

        self.assertEqual(
            replay_script._candidate_source_ids(Store(), record, ()),
            ("memory-one", "memory-two"),
        )
        with self.assertRaisesRegex(ValueError, "cover every source"):
            replay_script._candidate_source_ids(Store(), record, ("memory-one",))

    def test_legacy_policy_projection_uses_newly_compiled_source_delta(self):
        class Store:
            def policy_source_memory_ids(self, version):
                return {
                    PARENT_POLICY: ("memory-existing",),
                    CANDIDATE_POLICY: (
                        "memory-existing",
                        "memory-new",
                    ),
                }[version]

        record = {
            "policy_version": CANDIDATE_POLICY,
            "parent_version": PARENT_POLICY,
        }
        self.assertEqual(
            replay_script._candidate_source_ids(Store(), record, ()),
            ("memory-new",),
        )


class TargetReplayJudgementTests(unittest.TestCase):
    def test_reusable_experience_proof_predicate_is_source_specific(self):
        code = "missing_schema_binding"
        self.assertTrue(
            experience_has_replay_proof(
                _experience(
                    "memory-plan",
                    stage="plan-revisions",
                    problem_code=code,
                    **_plan_revision_proof(code),
                )
            )
        )
        corrected_sql = "SELECT COUNT(*) FROM cases"
        self.assertTrue(
            experience_has_replay_proof(
                _experience(
                    "memory-user",
                    stage="user-feedback",
                    problem_code="wrong_result",
                    after={"sql_fingerprint": _sha_text(corrected_sql)},
                )
            )
        )
        self.assertFalse(
            experience_has_replay_proof(
                _experience(
                    "memory-user-missing-proof",
                    stage="user-feedback",
                    problem_code="wrong_result",
                    after={"sql_fingerprint": "not-a-fingerprint"},
                )
            )
        )

    def test_user_feedback_without_valid_correction_fingerprint_fails_closed(self):
        experience = _experience(
            "memory-user-feedback",
            stage="user-feedback",
            problem_code="wrong_result",
            before={"sql_fingerprint": "a" * 64},
            after={"sql_fingerprint": "not-a-sha256"},
        )
        baseline = _success("SELECT private_before FROM t")
        candidate = _success("SELECT private_after FROM t")

        artifact = evaluate_target_replay(
            (experience,),
            {"memory-user-feedback": baseline},
            {"memory-user-feedback": candidate},
            parent_policy_version=PARENT_POLICY,
            candidate_policy_version=CANDIDATE_POLICY,
            replay_identity=_identity(),
        )

        row = artifact["results"][0]
        self.assertEqual(artifact["status"], "failed")
        self.assertIsNone(row["baseline_problem_present"])
        self.assertIsNone(row["candidate_problem_present"])
        self.assertIn("baseline_source_problem_not_verifiable", row["reasons"])
        serialized = json.dumps(artifact, ensure_ascii=False)
        self.assertNotIn("private_before", serialized)
        self.assertNotIn("private_after", serialized)
        self.assertNotIn("must-not-leak", serialized)

    def test_user_feedback_exact_corrected_sql_fingerprint_can_pass(self):
        corrected_sql = "SELECT COUNT(*) FROM cases"
        experience = _experience(
            "memory-user-feedback",
            stage="user-feedback",
            problem_code="wrong_result",
            before={"sql_fingerprint": "a" * 64},
            after={"sql_fingerprint": _sha_text(corrected_sql)},
        )

        artifact = evaluate_target_replay(
            (experience,),
            {"memory-user-feedback": _success("SELECT COUNT(id) FROM cases")},
            {"memory-user-feedback": _success(corrected_sql)},
            parent_policy_version=PARENT_POLICY,
            candidate_policy_version=CANDIDATE_POLICY,
            replay_identity=_identity(),
        )

        self.assertEqual(artifact["status"], "passed")
        self.assertTrue(artifact["results"][0]["baseline_problem_present"])
        self.assertFalse(artifact["results"][0]["candidate_problem_present"])

    def test_user_feedback_accepts_normalized_gate_fingerprint(self):
        normalized_fingerprint = "f" * 64
        experience = _experience(
            "memory-user-feedback-normalized",
            stage="user-feedback",
            problem_code="wrong_result",
            before={"sql_fingerprint": "a" * 64},
            after={"sql_fingerprint": normalized_fingerprint},
        )
        baseline = _success("SELECT COUNT(id) FROM cases")
        candidate = _success("select count(*) from cases")
        candidate["gates"]["ast"] = {
            "fingerprint": normalized_fingerprint,
        }

        artifact = evaluate_target_replay(
            (experience,),
            {"memory-user-feedback-normalized": baseline},
            {"memory-user-feedback-normalized": candidate},
            parent_policy_version=PARENT_POLICY,
            candidate_policy_version=CANDIDATE_POLICY,
            replay_identity=_identity(),
        )

        self.assertEqual(artifact["status"], "passed")
        self.assertFalse(artifact["results"][0]["candidate_problem_present"])

    def test_plan_revision_uses_structured_issue_appearance_and_disappearance(self):
        code = "missing_schema_binding"
        experience = _experience(
            "memory-plan",
            stage="plan-revisions",
            problem_code=code,
            **_plan_revision_proof(code),
        )
        baseline = _success(
            collaboration={
                "revision_requests": [
                    {"worker": "query-planning", "issue_codes": [code]}
                ]
            }
        )

        artifact = evaluate_target_replay(
            (experience,),
            {"memory-plan": baseline},
            {"memory-plan": _success()},
            parent_policy_version=PARENT_POLICY,
            candidate_policy_version=CANDIDATE_POLICY,
            replay_identity=_identity(),
        )

        self.assertEqual(artifact["status"], "passed")
        self.assertEqual(
            artifact["results"][0]["baseline_signal"],
            "revision_issue_present=true",
        )

    def test_plan_revision_without_before_after_fingerprints_fails_closed(self):
        code = "missing_schema_binding"
        experience = _experience(
            "memory-plan",
            stage="plan-revisions",
            problem_code=code,
            before={"issue_code": code, "issue_present": True},
            after={
                "issue_resolved": True,
                "approved_plan_fingerprint": "a" * 64,
                "bound_plan_fingerprint": "b" * 64,
            },
            applicability={"issue_code": code},
        )
        baseline = _success(
            collaboration={
                "initial_binding_conflicts": [
                    {"code": code, "owner": "query-planning"}
                ],
                "revision_requests": [{"issue_codes": [code]}],
            }
        )

        artifact = evaluate_target_replay(
            (experience,),
            {"memory-plan": baseline},
            {"memory-plan": _success()},
            parent_policy_version=PARENT_POLICY,
            candidate_policy_version=CANDIDATE_POLICY,
            replay_identity=_identity(),
        )

        self.assertEqual(artifact["status"], "failed")
        self.assertIsNone(artifact["results"][0]["baseline_problem_present"])
        self.assertEqual(
            artifact["results"][0]["baseline_signal"],
            "plan_revision_proof_unavailable",
        )

    def test_evaluation_rejects_source_revision_metadata_drift(self):
        code = "missing_schema_binding"
        experience = _experience(
            "memory-plan",
            stage="plan-revisions",
            problem_code=code,
            source_revision=1,
            **_plan_revision_proof(code),
        )

        with self.assertRaisesRegex(ValueError, "source revision metadata mismatch"):
            evaluate_target_replay(
                (experience,),
                {
                    "memory-plan": _success(
                        collaboration={
                            "revision_requests": [{"issue_codes": [code]}]
                        }
                    )
                },
                {"memory-plan": _success()},
                parent_policy_version=PARENT_POLICY,
                candidate_policy_version=CANDIDATE_POLICY,
                replay_identity=_identity(),
                source_metadata={
                    "memory-plan": {
                        "source_task_id": "source-task-1",
                        "source_revision": 2,
                    }
                },
            )

    def test_sql_repair_uses_public_repair_count(self):
        experience = _experience(
            "memory-repair",
            stage="candidate-gates",
            problem_code="sql_gate_repair",
            target_agent="sql-generation",
            before={
                "gate_accepted": False,
                "gate_codes": ["distinct_mismatch"],
            },
            after={"gate_accepted": True},
            applicability={
                "single_repair": True,
                "approved_plan_fingerprint": "b" * 64,
            },
        )
        baseline = _success(collaboration={"sql_generation_repairs": 1})
        candidate = _success(collaboration={"sql_generation_repairs": 0})

        artifact = evaluate_target_replay(
            (experience,),
            {"memory-repair": baseline},
            {"memory-repair": candidate},
            parent_policy_version=PARENT_POLICY,
            candidate_policy_version=CANDIDATE_POLICY,
            replay_identity=_identity(),
        )

        self.assertEqual(artifact["status"], "passed")
        self.assertEqual(
            artifact["results"][0]["candidate_signal"],
            "sql_generation_repairs=0",
        )


class TargetReplayExecutionTests(unittest.TestCase):
    def test_shared_source_trace_is_run_once_per_lane_and_artifact_is_redacted(self):
        code_one = "missing_schema_binding"
        code_two = "result_grain_mismatch"
        experiences = (
            _experience(
                "memory-plan-one",
                stage="plan-revisions",
                problem_code=code_one,
                **_plan_revision_proof(code_one),
            ),
            _experience(
                "memory-plan-two",
                stage="plan-revisions",
                problem_code=code_two,
                **_plan_revision_proof(code_two),
            ),
        )
        calls = {"parent": [], "candidate": []}

        def parent(question, task_id):
            calls["parent"].append((question, task_id))
            return _success(
                "SELECT raw_parent_sql FROM private_table",
                {"revision_requests": [{"issue_codes": [code_one, code_two]}]},
            )

        def candidate(question, task_id):
            calls["candidate"].append((question, task_id))
            return _success("SELECT raw_candidate_sql FROM private_table")

        artifact = run_target_replay(
            experiences,
            {"source-task-1": _trace()},
            parent,
            candidate,
            parent_policy_version=PARENT_POLICY,
            candidate_policy_version=CANDIDATE_POLICY,
            replay_identity=_identity(),
        )

        self.assertEqual(artifact["status"], "passed")
        self.assertEqual(len(calls["parent"]), 1)
        self.assertEqual(len(calls["candidate"]), 1)
        serialized = json.dumps(artifact, ensure_ascii=False)
        for private_text in (
            "统计案例数",
            "raw_parent_sql",
            "raw_candidate_sql",
            "secret_column",
            "secret-result",
            "private-user@example.invalid",
        ):
            self.assertNotIn(private_text, serialized)
        self.assertEqual(
            validate_target_replay_artifact(
                artifact, candidate_policy_version=CANDIDATE_POLICY
            )["artifact_sha256"],
            artifact["artifact_sha256"],
        )

    def test_same_task_different_source_revisions_are_replayed_separately(self):
        code_one = "missing_schema_binding"
        code_two = "result_grain_mismatch"
        experiences = (
            _experience(
                "memory-revision-one",
                stage="plan-revisions",
                problem_code=code_one,
                source_revision=1,
                **_plan_revision_proof(code_one),
            ),
            _experience(
                "memory-revision-two",
                stage="plan-revisions",
                problem_code=code_two,
                source_revision=2,
                **_plan_revision_proof(code_two),
            ),
        )
        calls = {"parent": [], "candidate": []}

        def parent(question, task_id):
            calls["parent"].append((question, task_id))
            code = code_one if question == "revision one" else code_two
            return _success(
                collaboration={"revision_requests": [{"issue_codes": [code]}]}
            )

        def candidate(question, task_id):
            calls["candidate"].append((question, task_id))
            return _success()

        artifact = run_target_replay(
            experiences,
            {
                "memory-revision-one": _trace(
                    question="revision one", source_revision=1
                ),
                "memory-revision-two": _trace(
                    question="revision two", source_revision=2
                ),
            },
            parent,
            candidate,
            parent_policy_version=PARENT_POLICY,
            candidate_policy_version=CANDIDATE_POLICY,
            replay_identity=_identity(),
        )

        self.assertEqual(artifact["status"], "passed")
        self.assertEqual(len(calls["parent"]), 2)
        self.assertEqual(len(calls["candidate"]), 2)
        self.assertEqual(
            {row["source_revision"] for row in artifact["results"]}, {1, 2}
        )

    def test_experience_and_trace_source_revision_must_match(self):
        code = "missing_schema_binding"
        experience = _experience(
            "memory-plan",
            stage="plan-revisions",
            problem_code=code,
            source_revision=2,
            **_plan_revision_proof(code),
        )
        calls = []

        with self.assertRaisesRegex(ValueError, "revision.*mismatched"):
            run_target_replay(
                (experience,),
                {"memory-plan": _trace(source_revision=1)},
                lambda question, task_id: calls.append(task_id) or _success(),
                lambda question, task_id: calls.append(task_id) or _success(),
                parent_policy_version=PARENT_POLICY,
                candidate_policy_version=CANDIDATE_POLICY,
                replay_identity=_identity(),
            )
        self.assertEqual(calls, [])

    def test_source_snapshot_drift_fails_before_execution(self):
        code = "missing_schema_binding"
        experience = _experience(
            "memory-plan",
            stage="plan-revisions",
            problem_code=code,
            **_plan_revision_proof(code),
        )
        trace = _trace()
        trace["version_pins"]["database_snapshot_id"] = "database-v2"
        calls = []

        with self.assertRaisesRegex(ValueError, "database_snapshot_id"):
            run_target_replay(
                (experience,),
                {"source-task-1": trace},
                lambda question, task_id: calls.append(task_id) or _success(),
                lambda question, task_id: calls.append(task_id) or _success(),
                parent_policy_version=PARENT_POLICY,
                candidate_policy_version=CANDIDATE_POLICY,
                replay_identity=_identity(),
            )
        self.assertEqual(calls, [])

    def test_runner_exception_is_redacted_and_fails_closed(self):
        code = "missing_schema_binding"
        experience = _experience(
            "memory-plan",
            stage="plan-revisions",
            problem_code=code,
            **_plan_revision_proof(code),
        )

        def parent(_question, _task_id):
            return _success(
                collaboration={"revision_requests": [{"issue_codes": [code]}]}
            )

        def candidate(_question, _task_id):
            raise RuntimeError("SELECT password FROM users; credential=must-not-leak")

        artifact = run_target_replay(
            (experience,),
            {"source-task-1": _trace()},
            parent,
            candidate,
            parent_policy_version=PARENT_POLICY,
            candidate_policy_version=CANDIDATE_POLICY,
            replay_identity=_identity(),
        )

        self.assertEqual(artifact["status"], "failed")
        serialized = json.dumps(artifact, ensure_ascii=False)
        self.assertNotIn("password", serialized)
        self.assertNotIn("must-not-leak", serialized)
        self.assertIn("target_replay_candidate_runtime_failure", serialized)

    def test_tampered_artifact_is_rejected(self):
        corrected_sql = "SELECT COUNT(*) FROM cases"
        experience = _experience(
            "memory-user-feedback",
            stage="user-feedback",
            problem_code="wrong_result",
            before={"sql_fingerprint": "a" * 64},
            after={"sql_fingerprint": _sha_text(corrected_sql)},
        )
        artifact = evaluate_target_replay(
            (experience,),
            {"memory-user-feedback": _success("SELECT id FROM cases")},
            {"memory-user-feedback": _success(corrected_sql)},
            parent_policy_version=PARENT_POLICY,
            candidate_policy_version=CANDIDATE_POLICY,
            replay_identity=_identity(),
        )
        artifact["status"] = "failed"

        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            validate_target_replay_artifact(artifact)

    def test_rehashed_forgery_is_rejected_by_structural_invariants(self):
        corrected_sql = "SELECT COUNT(*) FROM cases"
        experience = _experience(
            "memory-user-feedback",
            stage="user-feedback",
            problem_code="wrong_result",
            before={"sql_fingerprint": "a" * 64},
            after={"sql_fingerprint": _sha_text(corrected_sql)},
        )
        artifact = evaluate_target_replay(
            (experience,),
            {"memory-user-feedback": _success("SELECT id FROM cases")},
            {"memory-user-feedback": _success(corrected_sql)},
            parent_policy_version=PARENT_POLICY,
            candidate_policy_version=CANDIDATE_POLICY,
            replay_identity=_identity(),
        )

        forged_lineage = copy.deepcopy(artifact)
        forged_lineage["candidate_policy_version"] = "policy-other"
        _rehash_artifact(forged_lineage)
        with self.assertRaisesRegex(ValueError, "Policy pins"):
            validate_target_replay_artifact(forged_lineage)

        forged_identity = copy.deepcopy(artifact)
        forged_identity["replay_identity"]["shared_version_pins"][
            "database_snapshot_id"
        ] = "database-other"
        _rehash_artifact(forged_identity)
        with self.assertRaisesRegex(ValueError, "identity hash"):
            validate_target_replay_artifact(forged_identity)

        forged_summary = copy.deepcopy(artifact)
        forged_summary["summary"]["passed_count"] = 0
        _rehash_artifact(forged_summary)
        with self.assertRaisesRegex(ValueError, "summary is inconsistent"):
            validate_target_replay_artifact(forged_summary)

        forged_row = copy.deepcopy(artifact)
        forged_row["results"][0]["sql"] = "SELECT secret FROM private_table"
        _rehash_artifact(forged_row)
        with self.assertRaisesRegex(ValueError, "result contains forbidden"):
            validate_target_replay_artifact(forged_row)

        forged_duplicates = copy.deepcopy(artifact)
        forged_duplicates["memory_ids"].append("memory-user-feedback")
        forged_duplicates["results"].append(
            copy.deepcopy(forged_duplicates["results"][0])
        )
        forged_duplicates["summary"]["source_experience_count"] = 2
        forged_duplicates["summary"]["passed_count"] = 2
        _rehash_artifact(forged_duplicates)
        with self.assertRaisesRegex(ValueError, "Experience ids are invalid"):
            validate_target_replay_artifact(forged_duplicates)


if __name__ == "__main__":
    unittest.main()
