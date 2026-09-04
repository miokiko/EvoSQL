import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evoagent.config import Settings
from evoagent.text2sql.agentic import (
    BUILD_VERSION,
    GATE_IMPLEMENTATION_VERSION,
    TEXT2SQL_PROTOCOL,
    TEXT2SQL_RUNTIME_NODES,
)
from evoagent.text2sql.evolution import Text2SQLEvolutionStore
from evoagent.text2sql.knowledge_store import KnowledgeStore
from evoagent.text2sql.policy import TEXT2SQL_SKILLS
from evoagent.text2sql.web_service import Text2SQLWebService


def _settings() -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8080,
        db_path=":memory:",
        max_diff_bytes=10000,
        max_steps=8,
        timeout_seconds=10,
        llm_base_url="",
        llm_api_key="",
        llm_model="qwen-plus",
        github_webhook_secret="",
        github_token="",
        auto_post_review=False,
        llm_provider="aliyun",
    )


class Text2SQLWebServiceTests(unittest.TestCase):
    def test_query_attempt_freezes_context_caches_response_and_deduplicates_messages(self):
        project_root = Path(__file__).resolve().parents[1]
        snapshot = json.loads(
            (project_root / "artifacts/text2sql/schema/database_snapshot.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            with Text2SQLEvolutionStore(
                Path(directory) / "evolution.sqlite3", snapshot
            ) as evolution:
                original_context = {
                    "scope": {"user_id": "user", "session_id": "session"},
                    "recent_query_runs": [{"task_id": "previous"}],
                }
                first = evolution.prepare_query_attempt(
                    "task-1",
                    "user",
                    "session",
                    "问题",
                    ("user", "tenant"),
                    original_context,
                )
                self.assertEqual(first["conversation_context"], original_context)
                evolution.append_message("user", "session", "user", "问题", "task-1")
                evolution.append_message("user", "session", "user", "问题", "task-1")
                response = {"task_id": "task-1", "status": "success"}
                evolution.finish_query_attempt(
                    "task-1", "completed", response=response
                )

                retried = evolution.prepare_query_attempt(
                    "task-1",
                    "user",
                    "session",
                    "问题",
                    ("user", "tenant"),
                    {"recent_query_runs": [{"task_id": "newer"}]},
                )
                self.assertEqual(retried["conversation_context"], original_context)
                self.assertEqual(retried["cached_response"], response)
                message_count = evolution.connection.execute(
                    "SELECT COUNT(*) FROM memory_messages WHERE task_id='task-1' "
                    "AND role='user'"
                ).fetchone()[0]
                self.assertEqual(message_count, 1)
                with self.assertRaisesRegex(ValueError, "runtime identity"):
                    evolution.prepare_query_attempt(
                        "task-1",
                        "user",
                        "session",
                        "问题",
                        ("user", "tenant"),
                        original_context,
                        {"policy_version": "changed"},
                    )
                with self.assertRaisesRegex(ValueError, "different user"):
                    evolution.prepare_query_attempt(
                        "task-1",
                        "other-user",
                        "session",
                        "问题",
                        ("other-user", "tenant"),
                        original_context,
                    )

    def test_web_cache_identity_rejects_old_build_gate_and_topology(self):
        project_root = Path(__file__).resolve().parents[1]
        snapshot = json.loads(
            (project_root / "artifacts/text2sql/schema/database_snapshot.json").read_text(
                encoding="utf-8"
            )
        )

        class StubClient:
            provider = "test"
            model = "stub-model"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = Text2SQLWebService(
                _settings(),
                client=StubClient(),
                llm_config={"provider": "test", "model": "stub-model"},
                evolution_store_path=root / "evolution.sqlite3",
            )
            pins = {
                "database_snapshot_id": snapshot["snapshot_id"],
                "wiki_index_version": "wiki-v1",
                "vanna_index_version": "wiki-v1",
                "memory_snapshot_id": "memory-v1",
                "policy_version": "policy-v1",
            }
            context = {
                "scope": {"user_id": "user", "session_id": "session"},
                "recent_messages": [],
                "recent_query_runs": [{"task_id": "previous"}],
            }
            current = service._query_attempt_runtime_identity(pins, context)
            self.assertEqual(current["version_pins"], pins)
            self.assertEqual(current["protocol"], TEXT2SQL_PROTOCOL)
            self.assertEqual(current["nodes"], list(TEXT2SQL_RUNTIME_NODES))
            self.assertEqual(len(current["nodes"]), 11)
            self.assertEqual(current["build_version"], BUILD_VERSION)
            self.assertEqual(
                current["gate_implementation_version"],
                GATE_IMPLEMENTATION_VERSION,
            )
            changed_context = {
                **context,
                "recent_query_runs": [{"task_id": "another-run"}],
            }
            self.assertNotEqual(
                current["conversation_context_sha256"],
                service._query_attempt_runtime_identity(
                    pins, changed_context
                )["conversation_context_sha256"],
            )

            drifted_identities = {
                "old-build": {**current, "build_version": "old-build"},
                "old-gates": {
                    **current,
                    "gate_implementation_version": "old-gates",
                },
                "old-topology": {
                    **current,
                    "nodes": list(TEXT2SQL_RUNTIME_NODES[:-1]),
                },
            }
            with Text2SQLEvolutionStore(
                root / "evolution.sqlite3", snapshot
            ) as evolution:
                for task_id, old_identity in drifted_identities.items():
                    with self.subTest(task_id=task_id):
                        evolution.prepare_query_attempt(
                            task_id,
                            "user",
                            "session",
                            "问题",
                            ("user",),
                            context,
                            old_identity,
                        )
                        evolution.finish_query_attempt(
                            task_id,
                            "completed",
                            response={"task_id": task_id, "status": "old-cache"},
                        )
                        with self.assertRaisesRegex(ValueError, "runtime identity"):
                            evolution.prepare_query_attempt(
                                task_id,
                                "user",
                                "session",
                                "问题",
                                ("user",),
                                context,
                                current,
                            )

    def test_query_requires_configured_cloud_model_before_opening_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = Text2SQLWebService(
                _settings(),
                llm_config={},
                database_path=root / "database.sqlite3",
                snapshot_path=root / "snapshot.json",
                knowledge_store_path=root / "knowledge.sqlite3",
                evolution_store_path=root / "evolution.sqlite3",
                dataset_path=root / "dataset",
            )
            with self.assertRaisesRegex(RuntimeError, "EVOAGENT_DASHSCOPE_API_KEY"):
                service.query("强烈岩爆案例有多少个？")

    def test_public_result_contains_bounded_answer_and_agent_trace(self):
        result = {
            "status": "success",
            "question": "有多少条记录？",
            "final_sql": "SELECT COUNT(*) AS total FROM cases",
            "answer": {
                "columns": ["total"],
                "rows": [[12]],
                "row_count": 1,
                "truncated": False,
            },
            "gates": {"accepted": True, "errors": []},
            "version_pins": {"policy_version": "policy-v1"},
            "release": {"lane": "stable"},
            "collaboration": {
                "lead_assessment": {"reasoning_summary": "并行定位 Schema 与查询策略"},
                "worker_results": [
                    {
                        "worker": "schema-grounding",
                        "status": "completed",
                        "observed_evidence_ids": ["schema:cases"],
                        "output": {
                            "schema_plan": {
                                "tables": ["cases"],
                                "columns": ["id"],
                                "joins": [],
                            }
                        },
                    },
                    {
                        "worker": "query-planning",
                        "status": "completed",
                        "observed_evidence_ids": [],
                        "output": {
                            "query_spec": {"intent": "aggregate"},
                        },
                    },
                ],
                "bound_query_plan": {"fingerprint": "bound-plan-1"},
                "approved_query_plan": {
                    "bound_plan_fingerprint": "bound-plan-1",
                    "approved_by": "text2sql-lead",
                },
                "binding_conflicts": [],
                "lead_plan_approval": {
                    "approve_plan": True,
                    "reasoning_summary": "语义计划完整且绑定无冲突",
                },
                "sql_generation_result": {
                    "worker": "sql-generation",
                    "status": "completed",
                    "observed_evidence_ids": [],
                    "output": {
                        "sql_candidates": [
                            {
                                "candidate_id": "candidate-1",
                                "sql": "SELECT COUNT(*) FROM cases",
                            }
                        ],
                        "generation_notes": ["严格翻译 ApprovedQueryPlan"],
                    },
                },
                "candidate_gate_rounds": [
                    {
                        "round": 0,
                        "accepted_candidates": [
                            {
                                "candidate_id": "candidate-1",
                                "sql": "SELECT COUNT(*) FROM cases",
                            }
                        ],
                        "candidate_gate_results": [
                            {
                                "candidate_index": 0,
                                "candidate_id": "candidate-1",
                                "accepted": True,
                                "validation": {
                                    "accepted": True,
                                    "normalized_sql": "SELECT COUNT(*) FROM cases",
                                },
                                "plan_conformance": {"accepted": True},
                                "explain": {"plan": ["SCAN cases"]},
                                "errors": [],
                            }
                        ],
                        "gate_issues": [],
                    }
                ],
                "sql_generation_repairs": 0,
                "critic_result": {
                    "summary": "候选通过盲审",
                    "decisions": [{"accepted": True}],
                },
                "lead_final": {
                    "resolution_summary": "选择通过门禁的候选",
                    "final_candidate_index": 0,
                },
            },
            "execution": {
                "llm_calls": 4,
                "tool_calls": 6,
                "total_tokens": 1200,
                "duration_ms": 420,
            },
        }

        public = Text2SQLWebService._public_result(result, "web-task")

        self.assertEqual(public["task_id"], "web-task")
        self.assertEqual(public["answer"]["rows"], [[12]])
        self.assertEqual(public["execution"]["llm_calls"], 4)
        self.assertEqual(
            [item["role"] for item in public["agents"]],
            [
                "text2sql-lead",
                "schema-grounding",
                "query-planning",
                "text2sql-lead",
                "sql-generation",
                "text2sql-critic",
                "text2sql-lead",
            ],
        )
        self.assertEqual(
            {item["role"] for item in public["agents"]}, set(TEXT2SQL_SKILLS)
        )
        self.assertEqual(public["bound_query_plan"]["fingerprint"], "bound-plan-1")
        self.assertEqual(public["sql_generation"]["candidate_count"], 1)
        self.assertEqual(public["candidate_gate_results"][0]["candidate_id"], "candidate-1")
        self.assertNotIn(
            "normalized_sql", public["candidate_gate_results"][0]["validation"]
        )
        self.assertNotIn(
            "accepted_candidates", public["candidate_gate_rounds"][0]
        )
        self.assertFalse(public["deterministic_runtime"]["is_skill"])
        self.assertEqual(
            public["deterministic_runtime"]["role"], "text2sql-harness"
        )
        self.assertNotIn("collaboration", public)

    def test_text2sql_skills_can_be_listed_and_submitted_as_isolated_candidate(self):
        project_root = Path(__file__).resolve().parents[1]
        snapshot = json.loads(
            (project_root / "artifacts/text2sql/schema/database_snapshot.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            service = Text2SQLWebService(
                _settings(),
                llm_config={},
                snapshot_path=snapshot_path,
                knowledge_store_path=root / "knowledge.sqlite3",
                vanna_index_root=root / "vanna",
                evolution_store_path=root / "evolution.sqlite3",
            )

            catalog = service.skills()
            self.assertEqual(len(catalog["skills"]), 5)
            self.assertEqual(catalog["candidate_count"], 0)
            self.assertEqual(
                [item["name"] for item in catalog["skills"]],
                list(TEXT2SQL_SKILLS),
            )
            self.assertNotIn(
                "text2sql-harness", [item["name"] for item in catalog["skills"]]
            )
            status = service.status()
            self.assertEqual(status["roles"], list(TEXT2SQL_SKILLS))
            self.assertFalse(status["deterministic_runtime"]["is_skill"])

            submitted = service.propose_skill(
                "query-planning",
                {"prompt_fragment": "State the intended result grain before aggregation."},
                "Improve aggregation reliability",
                "test-author",
            )
            self.assertEqual(submitted["status"], "candidate")
            self.assertEqual(submitted["skill_name"], "query-planning")
            self.assertEqual(service.skills()["candidate_count"], 1)
            with self.assertRaisesRegex(ValueError, "unsupported"):
                service.propose_skill(
                    "text2sql-harness",
                    {"prompt_fragment": "change deterministic runtime"},
                    "must remain immutable",
                    "test-author",
                )

    def test_trace_history_is_bounded_and_excludes_answer_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = Text2SQLWebService(
                _settings(),
                llm_config={},
                evolution_store_path=root / "evolution.sqlite3",
            )
            service._remember_trace(
                {
                    "task_id": "trace-1",
                    "status": "success",
                    "question": "有多少条？",
                    "final_sql": "SELECT COUNT(*) FROM cases",
                    "gates": {"accepted": True},
                    "agents": [{"role": "text2sql-lead"}],
                    "execution": {"duration_ms": 10},
                    "version_pins": {"policy_version": "policy-v1"},
                    "answer": {"columns": ["count"], "rows": [[12]], "row_count": 1},
                },
                {
                    "collaboration": {
                        "lead_delegation": {
                            "memory_evidence_ids": ["memory-lead-1"]
                        },
                        "worker_results": [
                            {
                                "worker": "query-planning",
                                "memory_evidence_ids": ["memory-plan-1"],
                                "retrieval": [],
                                "output": {"query_spec": {"intent": "count"}},
                            }
                        ],
                        "bound_query_plan": {"fingerprint": "bound-plan-trace"},
                        "approved_query_plan": {
                            "bound_plan_fingerprint": "bound-plan-trace"
                        },
                        "binding_conflicts": [],
                        "sql_generation_result": {
                            "worker": "sql-generation",
                            "status": "completed",
                            "memory_evidence_ids": ["memory-generation-1"],
                            "output": {
                                "sql_candidates": [
                                    {
                                        "candidate_id": "candidate-trace",
                                        "sql": "SELECT COUNT(*) FROM cases",
                                    }
                                ],
                                "generation_notes": [],
                            },
                        },
                        "candidate_gate_rounds": [
                            {
                                "round": 0,
                                "accepted_candidates": [{"candidate_id": "candidate-trace"}],
                                "candidate_gate_results": [
                                    {
                                        "candidate_index": 0,
                                        "candidate_id": "candidate-trace",
                                        "accepted": True,
                                        "errors": [],
                                    }
                                ],
                                "gate_issues": [],
                            }
                        ],
                        "sql_generation_repairs": 0,
                    }
                },
            )

            traces = service.traces()["traces"]
            self.assertEqual(traces[0]["task_id"], "trace-1")
            self.assertEqual(traces[0]["answer"]["row_count"], 1)
            self.assertNotIn("rows", traces[0]["answer"])
            self.assertNotIn("result_rows", traces[0])
            self.assertNotIn("collaboration", traces[0])
            self.assertNotIn("user_id", traces[0])
            self.assertNotIn("session_id", traces[0])
            self.assertEqual(
                traces[0]["bound_query_plan"]["fingerprint"], "bound-plan-trace"
            )
            self.assertEqual(traces[0]["sql_generation"]["candidate_count"], 1)
            self.assertEqual(
                traces[0]["candidate_gate_results"][0]["candidate_id"],
                "candidate-trace",
            )
            self.assertFalse(traces[0]["deterministic_runtime"]["is_skill"])
            memory_usage = [
                item
                for item in traces[0]["retrieval"]
                if item.get("backend") == "semantic-memory"
            ]
            self.assertEqual(
                {item["memory_ids"][0] for item in memory_usage},
                {"memory-lead-1", "memory-plan-1", "memory-generation-1"},
            )

    def test_memory_dashboard_separates_three_layers_and_hides_private_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = Text2SQLWebService(
                _settings(),
                llm_config={},
                evolution_store_path=root / "evolution.sqlite3",
            )
            service._remember_trace(
                {
                    "task_id": "memory-trace-1",
                    "status": "rejected",
                    "question": "按等级统计数量",
                    "original_question": "按等级统计数量",
                    "standalone_question": "按等级统计数量",
                    "query_type": "DATA_QUERY",
                    "final_sql": "",
                    "gates": {"accepted": False},
                    "agents": [],
                    "execution": {},
                    "version_pins": {},
                    "answer": {"columns": [], "rows": [], "row_count": 0},
                },
                user_id="reader",
                session_id="session-1",
            )
            with Text2SQLEvolutionStore(
                root / "evolution.sqlite3", service._snapshot()
            ) as evolution:
                evolution.append_message(
                    "reader", "session-1", "user", "按等级统计数量", "memory-trace-1"
                )
                evolution.add_memory_candidate(
                    "query-planning",
                    "aggregation_grain_mismatch",
                    "聚合前明确指标、维度和去重口径。",
                    {"case_id": "case-1"},
                    "production_feedback",
                )

            dashboard = service.memory("reader", "session-1", 10)
            self.assertEqual(dashboard["contract"], "Text2SQLMemoryDashboard/v1")
            self.assertEqual(dashboard["layers"]["working"]["count"], 2)
            self.assertEqual(
                {item["role"] for item in dashboard["layers"]["working"]["items"]},
                {"assistant", "user"},
            )
            self.assertEqual(dashboard["layers"]["episodic"]["count"], 1)
            self.assertEqual(
                dashboard["layers"]["semantic"]["counts"]["candidate"], 1
            )
            episode = dashboard["layers"]["episodic"]["items"][0]
            self.assertNotIn("result_rows", episode)
            self.assertNotIn("collaboration", episode)
            self.assertEqual(
                episode["decisions"]["harness"]["outcome"], "rejected"
            )
            self.assertEqual(episode["decisions"]["human"], {})
            self.assertFalse(dashboard["boundaries"]["raw_model_reasoning_exposed"])
            self.assertTrue(
                dashboard["boundaries"]["stable_semantic_memory_only_injected"]
            )

    def test_cached_result_qa_trace_does_not_claim_sql_workers_ran(self):
        public = Text2SQLWebService._public_result(
            {
                "status": "success",
                "question": "其中最大的是哪个？",
                "query_type": "RESULT_QA",
                "parent_query_run_id": "previous-run",
                "answer": {
                    "columns": ["level", "count"],
                    "rows": [["强烈", 12]],
                    "row_count": 1,
                    "summary_text": "强烈等级最多，共 12 条。",
                },
                "gates": {"accepted": True, "mode": "cached_result"},
                "collaboration": {
                    "route": {
                        "type": "RESULT_QA",
                        "parent_query_run_id": "previous-run",
                        "reason": "可由上次结果回答",
                    },
                    "worker_results": [],
                    "critic_result": {"decisions": []},
                    "lead_final": {"reasoning_summary": "读取缓存结果"},
                },
            },
            "result-qa-run",
        )

        self.assertEqual(public["query_type"], "RESULT_QA")
        self.assertEqual(
            [item["stage"] for item in public["agents"]],
            ["query-routing", "cached-result-answer"],
        )
        self.assertEqual(public["final_sql"], "")

    def test_data_query_keeps_bounded_draft_pack_without_fake_agent_role(self):
        public = Text2SQLWebService._public_result(
            {
                "status": "rejected",
                "question": "查询案例",
                "query_type": "DATA_QUERY",
                "collaboration": {
                    "route": {"type": "DATA_QUERY"},
                    "draft_link_pack": {
                        "contract": "DraftLinkPack/v1",
                        "trust": "untrusted_candidate_input_to_grounding",
                        "draft_sql": "SELECT c_caseCode FROM t_caseinfo",
                        "draft_valid": True,
                        "tables": ["t_caseinfo"],
                        "columns": ["t_caseinfo.c_caseCode"],
                        "full_ddl": ["must-not-be-public"],
                        "coverage": {"has_full_ddl": True},
                    },
                    "worker_results": [],
                    "critic_result": {"decisions": []},
                    "lead_final": {},
                },
            },
            "draft-trace",
        )
        self.assertEqual(public["draft_link_pack"]["contract"], "DraftLinkPack/v1")
        self.assertNotIn("full_ddl", public["draft_link_pack"])
        self.assertNotIn(
            "vanna-draft-planner", [item["role"] for item in public["agents"]]
        )
        self.assertEqual(
            {item["role"] for item in public["agents"]}, set(TEXT2SQL_SKILLS)
        )

    def test_confirmed_query_becomes_reviewed_knowledge_not_direct_vanna_write(self):
        project_root = Path(__file__).resolve().parents[1]
        snapshot = json.loads(
            (project_root / "artifacts/text2sql/schema/database_snapshot.json").read_text(
                encoding="utf-8"
            )
        )
        join_catalog = json.loads(
            (project_root / "artifacts/text2sql/schema/join_catalog.review.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path = root / "snapshot.json"
            knowledge_path = root / "knowledge.sqlite3"
            evolution_path = root / "evolution.sqlite3"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            with KnowledgeStore(knowledge_path) as store:
                store.ingest_database(snapshot, join_catalog)
            service = Text2SQLWebService(
                _settings(),
                llm_config={},
                snapshot_path=snapshot_path,
                knowledge_store_path=knowledge_path,
                evolution_store_path=evolution_path,
                vanna_index_root=root / "vanna",
            )
            service._remember_trace(
                {
                    "task_id": "trace-feedback",
                    "status": "success",
                    "question": "强烈岩爆案例有多少个？",
                    "standalone_question": "强烈岩爆案例有多少个？",
                    "query_type": "DATA_QUERY",
                    "final_sql": (
                        "SELECT COUNT(DISTINCT c_caseCode) AS n "
                        "FROM t_casedesc WHERE c_rockLevel='强烈'"
                    ),
                    "gates": {"accepted": True},
                    "agents": [],
                    "execution": {},
                    "version_pins": {},
                    "answer": {"columns": ["n"], "rows": [[6]], "row_count": 1},
                },
                user_id="reviewer",
                session_id="session-1",
            )
            pending = service.experiences()["experiences"][0]
            self.assertEqual(pending["state"], "ineligible")
            self.assertIn("requires_human_feedback", pending["eligibility_reasons"])
            feedback = service.feedback(
                "trace-feedback",
                "correct",
                "结果与业务含义一致",
                "",
                user_id="reviewer",
                session_id="session-1",
            )
            self.assertTrue(feedback["experience_id"])
            confirmed = service.experiences("candidate")["experiences"][0]
            self.assertEqual(confirmed["experience_id"], feedback["experience_id"])
            with KnowledgeStore(knowledge_path) as store:
                stable_before = store.current_index_version("stable")
            with patch.object(
                service,
                "start_experience_evaluation",
                return_value={
                    "job_id": "experience-eval-test",
                    "status": "queued",
                    "background": True,
                },
            ):
                reviewed = service.review_experience(
                    feedback["experience_id"], "approve", "human-reviewer"
                )
            self.assertEqual(reviewed["state"], "approved")
            self.assertEqual(
                reviewed["next_step"],
                "candidate_vanna_build_and_240_case_regression_started",
            )
            with KnowledgeStore(knowledge_path) as store:
                self.assertEqual(store.stats()["types"]["verified_example"], 1)
                self.assertEqual(
                    store.current_index_version("stable"), stable_before
                )
                self.assertIn(
                    "verified_example",
                    {
                        item["knowledge_type"]
                        for item in store.candidates()
                    },
                )

    def test_incorrect_production_feedback_creates_reviewable_semantic_memory(self):
        project_root = Path(__file__).resolve().parents[1]
        snapshot = json.loads(
            (project_root / "artifacts/text2sql/schema/database_snapshot.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            service = Text2SQLWebService(
                _settings(),
                llm_config={},
                snapshot_path=snapshot_path,
                evolution_store_path=root / "evolution.sqlite3",
            )
            service._remember_trace(
                {
                    "task_id": "trace-incorrect-feedback",
                    "status": "success",
                    "question": "按岩爆等级排序",
                    "standalone_question": "按岩爆等级排序",
                    "query_type": "DATA_QUERY",
                    "final_sql": (
                        "SELECT c_rockLevel FROM t_casedesc "
                        "ORDER BY c_rockLevel ASC"
                    ),
                    "gates": {"accepted": True, "errors": []},
                    "agents": [],
                    "execution": {},
                    "version_pins": {},
                    "answer": {
                        "columns": ["c_rockLevel"],
                        "rows": [["强烈"]],
                        "row_count": 1,
                    },
                },
                user_id="reviewer",
                session_id="session-1",
            )

            with self.assertRaisesRegex(ValueError, "rejection reason"):
                service.feedback(
                    "trace-incorrect-feedback",
                    "incorrect",
                    "",
                    "",
                    user_id="reviewer",
                    session_id="session-1",
                )
            self.assertEqual(
                service.memory("reviewer", "session-1", 10)["layers"]["semantic"]["counts"]["candidate"],
                0,
            )

            feedback = service.feedback(
                "trace-incorrect-feedback",
                "incorrect",
                "排序方向错误，应该从高到低",
                "",
                user_id="reviewer",
                session_id="session-1",
            )

            self.assertTrue(feedback["memory_id"])
            self.assertEqual(feedback["next_step"], "human_memory_review")
            self.assertEqual(
                feedback["attribution"]["failure_kind"],
                "ordering_limit_mismatch",
            )
            self.assertEqual(
                feedback["attribution"]["target_skill"], "query-planning"
            )
            self.assertEqual(feedback["decision"]["decision_source"], "human")
            self.assertEqual(feedback["decision"]["outcome"], "rejected")
            dashboard = service.memory("reviewer", "session-1", 10)
            self.assertEqual(
                dashboard["layers"]["semantic"]["counts"]["candidate"], 1
            )
            candidate = dashboard["layers"]["semantic"]["items"][0]
            self.assertEqual(candidate["state"], "candidate")
            self.assertEqual(candidate["origin_split"], "production_feedback")
            reviewed = service.review_memory_candidate(
                feedback["memory_id"],
                "approve",
                "human-reviewer",
                target_skill="query-planning",
                failure_kind="ordering_limit_mismatch",
                content="Planning 必须明确排序指标、方向与 LIMIT。",
            )
            self.assertEqual(reviewed["state"], "approved")
            self.assertEqual(
                reviewed["content"], "Planning 必须明确排序指标、方向与 LIMIT。"
            )
            refreshed = service.memory("reviewer", "session-1", 10)
            self.assertEqual(
                refreshed["layers"]["semantic"]["counts"]["candidate"], 0
            )
            self.assertEqual(
                refreshed["layers"]["semantic"]["counts"]["approved"], 1
            )


if __name__ == "__main__":
    unittest.main()
