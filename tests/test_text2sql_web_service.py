import json
import tempfile
import unittest
from pathlib import Path

from evoagent.config import Settings
from evoagent.text2sql.evolution import Text2SQLEvolutionStore
from evoagent.text2sql.knowledge_store import KnowledgeStore
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
                        "worker": "sql-strategy",
                        "status": "completed",
                        "observed_evidence_ids": [],
                        "output": {
                            "query_spec": {"intent": "aggregate"},
                            "sql_candidates": [{"sql": "SELECT COUNT(*) FROM cases"}],
                        },
                    },
                ],
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
                "sql-strategy",
                "text2sql-critic",
                "text2sql-lead",
            ],
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
                evolution_store_path=root / "evolution.sqlite3",
            )

            catalog = service.skills()
            self.assertEqual(len(catalog["skills"]), 4)
            self.assertEqual(catalog["candidate_count"], 0)
            self.assertIn("sql-strategy", [item["name"] for item in catalog["skills"]])

            submitted = service.propose_skill(
                "sql-strategy",
                {"prompt_fragment": "State the intended result grain before aggregation."},
                "Improve aggregation reliability",
                "test-author",
            )
            self.assertEqual(submitted["status"], "candidate")
            self.assertEqual(submitted["skill_name"], "sql-strategy")
            self.assertEqual(service.skills()["candidate_count"], 1)

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
                }
            )

            traces = service.traces()["traces"]
            self.assertEqual(traces[0]["task_id"], "trace-1")
            self.assertEqual(traces[0]["answer"]["row_count"], 1)
            self.assertNotIn("rows", traces[0]["answer"])
            self.assertNotIn("result_rows", traces[0])
            self.assertNotIn("collaboration", traces[0])
            self.assertNotIn("user_id", traces[0])
            self.assertNotIn("session_id", traces[0])

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

    def test_data_query_trace_exposes_bounded_vanna_draft_step(self):
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
        self.assertIn(
            "vanna-draft-planner", [item["role"] for item in public["agents"]]
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
            reviewed = service.review_experience(
                feedback["experience_id"], "approve", "human-reviewer"
            )
            self.assertEqual(reviewed["state"], "promoted")
            self.assertTrue(reviewed["vanna_rebuild_required"])
            with KnowledgeStore(knowledge_path) as store:
                self.assertEqual(store.stats()["types"]["verified_example"], 1)


if __name__ == "__main__":
    unittest.main()
