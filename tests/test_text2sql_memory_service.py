import json
import tempfile
import unittest
from pathlib import Path

from evoagent.text2sql.evolution import Text2SQLEvolutionStore
from evoagent.text2sql.memory_service import (
    EXPERIENCE_MEMORY_CONTRACT,
    build_query_trace,
    extract_plan_revision_experiences,
    extract_sql_gate_repair_experiences,
    extract_user_correction_experiences,
    finalize_run,
    production_experience_source,
)


APPROVED_PLAN = {
    "contract": "ApprovedQueryPlan/v1",
    "fingerprint": "a" * 64,
    "bound_plan": {"fingerprint": "b" * 64},
}
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


class _Store:
    def __init__(self, *, fail_trace=False, fail_experience=False):
        self.snapshot = {}
        self.fail_trace = fail_trace
        self.fail_experience = fail_experience
        self.traces = []
        self.experiences = []

    def save_query_trace(self, trace):
        if self.fail_trace:
            raise RuntimeError("trace sink unavailable password=do-not-leak")
        self.traces.append(dict(trace))

    def add_experience_memory(self, experience, *, origin_split):
        if self.fail_experience:
            raise RuntimeError("experience sink unavailable")
        self.experiences.append((dict(experience), origin_split))
        return "memory-%d" % len(self.experiences)


def _terminal(collaboration=None):
    return {
        "task_id": "query-1",
        "status": "success",
        "question": "按项目统计案例数",
        "standalone_question": "按项目统计案例数",
        "query_type": "DATA_QUERY",
        "answer": {
            "columns": ["project", "count"],
            "rows": [["A", 2]],
            "row_count": 1,
            "summary_text": "返回 1 行",
        },
        "final_sql": "SELECT project, COUNT(*) FROM cases GROUP BY project",
        "gates": {"accepted": True, "errors": []},
        "version_pins": {
            "database_snapshot_id": "snapshot-1",
            "wiki_index_version": "wiki-1",
            "vanna_index_version": "vanna-1",
            "memory_snapshot_id": "memory-1",
            "policy_version": "policy-1",
        },
        "execution": {"duration_ms": 10},
        "collaboration": collaboration or {},
    }


def _plan_revision_collaboration():
    revision_requests = [
        {
            "assignment_id": "ground-1",
            "worker": "schema-grounding",
            "guidance": "补齐项目字段的物理绑定",
            "issue_codes": ["missing_schema_binding"],
        },
        {
            "assignment_id": "plan-1",
            "worker": "query-planning",
            "guidance": "明确案例标识的去重口径",
            "issue_codes": ["unsupported_query_contract"],
        },
    ]
    return {
        "protocol": "test",
        "delegations": [
            {"assignment_id": "ground-1", "worker": "schema-grounding"},
            {"assignment_id": "plan-1", "worker": "query-planning"},
        ],
        "initial_worker_results": [
            {
                "assignment_id": "ground-1",
                "worker": "schema-grounding",
                "status": "completed",
                "output": {"schema_plan": {"tables": []}},
            },
            {
                "assignment_id": "plan-1",
                "worker": "query-planning",
                "status": "completed",
                "output": {"query_spec": {"intent": "list"}},
            },
        ],
        "worker_results": [
            {
                "assignment_id": "ground-1",
                "worker": "schema-grounding",
                "status": "completed",
                "output": {"schema_plan": {"tables": ["cases"]}},
                "memory_evidence_ids": ["memory-old"],
            },
            {
                "assignment_id": "plan-1",
                "worker": "query-planning",
                "status": "completed",
                "output": {"query_spec": {"intent": "count"}},
            },
        ],
        "lead_assessment": {"revision_requests": []},
        "revision_requests": revision_requests,
        "revisions_applied": 2,
        "initial_binding_conflicts": [
            {
                "code": "missing_schema_binding",
                "owner": "schema-grounding",
            },
            {
                "code": "unsupported_query_contract",
                "owner": "query-planning",
            },
        ],
        "binding_conflicts": [],
        "plan_approval_errors": [],
        "approved_query_plan": APPROVED_PLAN,
    }


def _sql_repair_collaboration():
    return {
        "approved_query_plan": APPROVED_PLAN,
        "sql_generation_repairs": 1,
        "sql_generation_initial": {
            "worker": "sql-generation",
            "status": "completed",
            "output": {
                "sql_candidates": [
                    {
                        "sql": "SELECT COUNT(case_id) FROM cases",
                        "bound_plan_fingerprint": "b" * 64,
                        "revision": 0,
                    }
                ]
            },
        },
        "candidate_gate_rounds": [
            {
                "round": 0,
                "accepted_candidates": [],
                "candidate_gate_results": [
                    {
                        "candidate_id": "initial",
                        "accepted": False,
                        "errors": ["distinct_mismatch"],
                    }
                ],
                "gate_issues": [
                    {"candidate_id": "initial", "code": "distinct_mismatch"}
                ],
            },
            {
                "round": 1,
                "accepted_candidates": [
                    {
                        "candidate_id": "repaired",
                        "sql": "SELECT COUNT(DISTINCT case_id) FROM cases",
                        "bound_plan_fingerprint": "b" * 64,
                        "revision": 1,
                    }
                ],
                "candidate_gate_results": [
                    {"candidate_id": "repaired", "accepted": True, "errors": []}
                ],
                "gate_issues": [],
            },
        ],
    }


class QueryTraceBuilderTests(unittest.TestCase):
    def test_builder_projects_common_trace_and_drops_unknown_internal_fields(self):
        collaboration = _plan_revision_collaboration()
        collaboration["system_prompt"] = "must-not-be-persisted"
        trace = build_query_trace(
            _terminal(),
            {"collaboration": collaboration},
            origin="web",
            source_lane="stable",
            source_revision=3,
            user_id="user-1",
            session_id="session-1",
            recorded_at="2026-09-06T00:00:00+00:00",
        )

        self.assertEqual(trace["task_id"], "query-1")
        self.assertEqual(trace["origin"], "web")
        self.assertEqual(trace["source_lane"], "stable")
        self.assertEqual(trace["source_revision"], 3)
        self.assertEqual(trace["schema_plan"], {"tables": ["cases"]})
        self.assertEqual(trace["query_spec"], {"intent": "count"})
        self.assertNotIn("system_prompt", trace["collaboration"])
        self.assertEqual(trace["answer"]["row_count"], 1)
        self.assertEqual(
            trace["retrieval"][0]["memory_ids"], ["memory-old"]
        )

    def test_only_real_stable_web_or_cli_runs_can_produce_experience(self):
        self.assertTrue(production_experience_source("web", "stable"))
        self.assertTrue(production_experience_source("CLI", "STABLE"))
        for origin, lane in (
            ("evaluation", "stable"),
            ("shadow", "stable"),
            ("debug", "stable"),
            ("web", "candidate"),
            ("cli", "shadow"),
        ):
            with self.subTest(origin=origin, lane=lane):
                self.assertFalse(production_experience_source(origin, lane))


class DeterministicExperienceExtractorTests(unittest.TestCase):
    def test_plan_revisions_create_one_single_owner_experience_per_issue(self):
        collaboration = _plan_revision_collaboration()
        trace = build_query_trace(
            _terminal(collaboration), origin="web", source_lane="stable"
        )

        values = extract_plan_revision_experiences(trace)

        self.assertEqual(len(values), 2)
        by_agent = {item["target_agent"]: item for item in values}
        self.assertEqual(
            by_agent["schema-grounding"]["problem_code"],
            "missing_schema_binding",
        )
        self.assertEqual(
            by_agent["query-planning"]["problem_code"],
            "unsupported_query_contract",
        )
        self.assertTrue(all(item["state"] == "candidate" for item in values))
        self.assertTrue(
            all(item["contract"] == EXPERIENCE_MEMORY_CONTRACT for item in values)
        )
        self.assertTrue(
            all(
                item["before"]["worker_plan_fingerprint"]
                != item["after"]["worker_plan_fingerprint"]
                for item in values
            )
        )
        self.assertEqual(
            by_agent["schema-grounding"]["evidence"]["derived_from_memory_ids"],
            ["memory-old"],
        )

    def test_plan_revision_without_a_structured_issue_needs_evidence(self):
        collaboration = _plan_revision_collaboration()
        collaboration["revision_requests"] = [
            {
                "assignment_id": "ground-1",
                "worker": "schema-grounding",
                "guidance": "请再检查一次",
            }
        ]
        trace = build_query_trace(
            _terminal(collaboration), origin="web", source_lane="stable"
        )

        values = extract_plan_revision_experiences(trace)

        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["state"], "needs_evidence")
        self.assertEqual(values[0]["target_agent"], "schema-grounding")

    def test_plan_revision_without_initial_worker_output_fails_closed(self):
        collaboration = _plan_revision_collaboration()
        collaboration.pop("initial_worker_results")
        trace = build_query_trace(
            _terminal(collaboration), origin="web", source_lane="stable"
        )

        values = extract_plan_revision_experiences(trace)

        self.assertEqual(len(values), 2)
        self.assertTrue(all(item["state"] == "needs_evidence" for item in values))
        self.assertTrue(
            all(not item["after"]["issue_resolved"] for item in values)
        )

    def test_single_sql_gate_repair_creates_generation_experience(self):
        collaboration = _sql_repair_collaboration()
        trace = build_query_trace(
            _terminal(collaboration), origin="web", source_lane="stable"
        )

        values = extract_sql_gate_repair_experiences(trace)

        self.assertEqual(len(values), 1)
        value = values[0]
        self.assertEqual(value["target_agent"], "sql-generation")
        self.assertEqual(value["problem_code"], "sql_gate_repair")
        self.assertEqual(value["state"], "candidate")
        self.assertEqual(value["before"]["gate_codes"], ["distinct_mismatch"])
        self.assertTrue(value["after"]["gate_accepted"])
        rendered = str(value)
        self.assertNotIn("SELECT COUNT", rendered)

    def test_runtime_gate_failure_is_not_agent_experience(self):
        collaboration = _sql_repair_collaboration()
        collaboration["candidate_gate_rounds"][0]["gate_issues"] = [
            {"code": "candidate_gate_runtime_failure"}
        ]
        collaboration["candidate_gate_rounds"][0]["candidate_gate_results"][0][
            "errors"
        ] = ["candidate_gate_runtime_failure"]
        trace = build_query_trace(
            _terminal(collaboration), origin="web", source_lane="stable"
        )

        self.assertEqual(extract_sql_gate_repair_experiences(trace), ())

    def test_generic_user_rejection_is_unattributed_needs_evidence(self):
        trace = build_query_trace(
            _terminal(), origin="web", source_lane="stable"
        )

        values = extract_user_correction_experiences(
            trace,
            {
                "decision": "incorrect",
                "note": "结果不对 password=must-not-survive",
            },
        )

        self.assertEqual(len(values), 1)
        value = values[0]
        self.assertEqual(value["state"], "needs_evidence")
        self.assertEqual(value["target_agent"], "")
        self.assertEqual(value["problem_code"], "user_correction_unattributed")
        self.assertNotIn("must-not-survive", str(value))

    def test_explicit_prose_correction_needs_replayable_evidence(self):
        trace = build_query_trace(
            _terminal(), origin="cli", source_lane="stable"
        )

        values = extract_user_correction_experiences(
            trace,
            {
                "decision": "incorrect",
                "note": "逻辑计划遗漏去重口径",
                "target_agent": "query-planning",
                "problem_code": "aggregation_grain_mismatch",
                "correction": "案例数按案例标识去重",
            },
        )

        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["state"], "needs_evidence")
        self.assertEqual(values[0]["target_agent"], "query-planning")

    def test_gate_validated_corrected_sql_creates_replayable_candidate(self):
        trace = build_query_trace(
            _terminal(), origin="web", source_lane="stable"
        )

        values = extract_user_correction_experiences(
            trace,
            {
                "decision": "incorrect",
                "note": "逻辑计划遗漏去重口径",
                "target_agent": "query-planning",
                "problem_code": "aggregation_grain_mismatch",
                "correction": "案例数按案例标识去重",
                "corrected_sql": "SELECT 1",
            },
            snapshot=SNAPSHOT,
        )

        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["state"], "candidate")
        self.assertTrue(values[0]["evidence"]["corrected_sql_accepted"])
        self.assertEqual(len(values[0]["after"]["sql_fingerprint"]), 64)

    def test_query_trace_recursively_redacts_credentials_and_sensitive_columns(self):
        secret = "sk-ws-super-secret-value"
        terminal = _terminal()
        terminal["question"] = "检查 %s" % secret
        terminal["standalone_question"] = terminal["question"]
        terminal["final_sql"] = "SELECT '%s' AS access_token" % secret
        terminal["gates"] = {"accepted": False, "errors": ["Bearer abcdefghijklmnop"]}
        terminal["agents"] = [{"detail": {"authorization": secret}}]
        terminal["execution"] = {"nested": {"password": secret}}
        terminal["answer"] = {
            "columns": ["user", "access_token"],
            "rows": [["alice", "Bearer abcdefghijklmnop"]],
            "row_count": 1,
            "summary_text": "credential=%s" % secret,
        }
        terminal["collaboration"] = {
            "critic_result": {
                "error": secret,
                "system_prompt": "do not persist",
            }
        }

        trace = build_query_trace(terminal, origin="web", source_lane="stable")
        serialized = json.dumps(trace, ensure_ascii=False)

        self.assertNotIn(secret, serialized)
        self.assertNotIn("abcdefghijklmnop", serialized)
        self.assertNotIn("do not persist", serialized)
        self.assertEqual(trace["result_rows"][0][1], "[REDACTED]")
        self.assertIn("[REDACTED]", serialized)


class FinalizeRunTests(unittest.TestCase):
    def test_normal_success_records_trace_without_inventing_experience(self):
        store = _Store()

        status = finalize_run(
            _terminal(), store=store, origin="web", source_lane="stable"
        )

        self.assertEqual(status.status, "recorded")
        self.assertTrue(status.trace_recorded)
        self.assertEqual(status.experience_count, 0)
        self.assertEqual(status.experience_skipped_reason, "no_experience_signal")
        self.assertEqual(len(store.traces), 1)
        self.assertEqual(store.experiences, [])
        self.assertEqual(status.as_dict()["memory_status"], "recorded")

    def test_trace_failure_is_degraded_and_never_raises(self):
        store = _Store(fail_trace=True)

        status = finalize_run(_terminal(), store=store, origin="web")

        self.assertEqual(status.status, "degraded")
        self.assertFalse(status.trace_recorded)
        self.assertIn("RuntimeError", status.error)
        self.assertNotIn("do-not-leak", status.error)

    def test_experience_failure_is_degraded_after_trace_is_kept(self):
        store = _Store(fail_experience=True)

        status = finalize_run(
            _terminal(_sql_repair_collaboration()),
            store=store,
            origin="web",
            source_lane="stable",
        )

        self.assertEqual(status.status, "degraded")
        self.assertTrue(status.trace_recorded)
        self.assertEqual(len(store.traces), 1)
        self.assertEqual(status.experience_count, 0)

    def test_evaluation_shadow_and_candidate_never_write_experience(self):
        for origin, lane in (
            ("evaluation", "stable"),
            ("shadow", "stable"),
            ("web", "candidate"),
        ):
            with self.subTest(origin=origin, lane=lane):
                store = _Store()
                status = finalize_run(
                    _terminal(_sql_repair_collaboration()),
                    store=store,
                    origin=origin,
                    source_lane=lane,
                )
                self.assertEqual(status.status, "recorded")
                self.assertTrue(status.trace_recorded)
                self.assertEqual(
                    status.experience_skipped_reason, "non_production_source"
                )
                self.assertEqual(store.experiences, [])

    def test_finalize_persists_all_deterministic_candidates(self):
        collaboration = _plan_revision_collaboration()
        collaboration.update(_sql_repair_collaboration())
        store = _Store()

        status = finalize_run(
            _terminal(collaboration),
            store=store,
            origin="cli",
            source_lane="stable",
            source_revision=2,
        )

        self.assertEqual(status.status, "recorded")
        self.assertEqual(status.experience_count, 3)
        self.assertEqual(status.experience_ids, ("memory-1", "memory-2", "memory-3"))
        self.assertEqual(
            {item[0]["target_agent"] for item in store.experiences},
            {"schema-grounding", "query-planning", "sql-generation"},
        )
        self.assertTrue(
            all(item[1] == "production_feedback" for item in store.experiences)
        )

    def test_finalize_integrates_with_real_evolution_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evolution.sqlite3"
            terminal = _terminal(_sql_repair_collaboration())
            terminal["version_pins"]["database_snapshot_id"] = SNAPSHOT["snapshot_id"]
            with Text2SQLEvolutionStore(path, SNAPSHOT) as store:
                status = finalize_run(
                    terminal,
                    store=store,
                    origin="web",
                    source_lane="stable",
                )

                self.assertEqual(status.status, "recorded")
                self.assertEqual(status.experience_count, 1)
                persisted = store.get_memory(status.experience_ids[0])
                self.assertEqual(persisted["rule"]["contract"], EXPERIENCE_MEMORY_CONTRACT)
                self.assertEqual(persisted["state"], "candidate")
                self.assertFalse(persisted["runtime_eligible"])


if __name__ == "__main__":
    unittest.main()
