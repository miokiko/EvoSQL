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
from corpus_fixtures import build_test_corpus
from evoagent.text2sql.vanna_corpus import VannaCorpus, collect_vanna_corpus, add_confirmed_question_sql, question_sql_registry_path
from evoagent.text2sql.policy import TEXT2SQL_SKILLS
from evoagent.text2sql.web_service import Text2SQLWebService


def _settings() -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8080,
        db_path=":memory:",



        llm_base_url="",
        llm_api_key="",
        llm_model="qwen-plus",



        llm_provider="aliyun",
    )


class Text2SQLWebServiceTests(unittest.TestCase):
    def test_retry_uses_frozen_context_but_rejects_identity_drift(self):
        root = Path(__file__).resolve().parents[1]
        snapshot = json.loads((root / "artifacts/text2sql/schema/database_snapshot.json").read_text())
        service = Text2SQLWebService(_settings(), llm_config={})
        initial = {"scope": {"user_id": "u", "session_id": "s"}, "recent_query_runs": []}
        grown = {**initial, "recent_query_runs": [{"task_id": "failed-attempt"}, {"task_id": "other-query"}]}
        pins = {"policy_version": "p1", "memory_snapshot_id": "m1"}
        with tempfile.TemporaryDirectory() as directory:
            with Text2SQLEvolutionStore(Path(directory) / "e.sqlite3", snapshot) as store:
                runtime = service._query_attempt_runtime_identity(pins, initial)
                store.prepare_query_attempt("q", "u", "s", "用户补充：按项目统计", ("u",), initial, runtime)
                store.finish_query_attempt("q", "error", "text2sql_runtime_error")
                latest_runtime = service._query_attempt_runtime_identity(pins, grown)
                retry = store.prepare_query_attempt("q", "u", "s", "用户补充：按项目统计", ("u",), grown, latest_runtime)
                self.assertEqual(retry["conversation_context"], initial)
                self.assertEqual(latest_runtime, service._query_attempt_runtime_identity(pins, grown))
                store.finish_query_attempt("q", "completed", response={"status": "success"})
                cached = store.prepare_query_attempt("q", "u", "s", "用户补充：按项目统计", ("u",), grown, latest_runtime)
                self.assertEqual(cached["cached_response"], {"status": "success"})
                for user, session, question, principals, changed in [
                    ("other", "s", "用户补充：按项目统计", ("u",), latest_runtime),
                    ("u", "other", "用户补充：按项目统计", ("u",), latest_runtime),
                    ("u", "s", "另一个问题", ("u",), latest_runtime),
                    ("u", "s", "用户补充：按项目统计", ("other",), latest_runtime),
                    ("u", "s", "用户补充：按项目统计", ("u",), {**latest_runtime, "build_version": "changed"}),
                    ("u", "s", "用户补充：按项目统计", ("u",), {**latest_runtime, "version_pins": {"policy_version": "p2"}}),
                ]:
                    with self.subTest(user=user, session=session, question=question, runtime=changed):
                        with self.assertRaisesRegex(ValueError, "runtime identity"):
                            store.prepare_query_attempt("q", user, session, question, principals, grown, changed)

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
            self.assertEqual(current["policy_source_memory_ids"], [])
            compiled = service._query_attempt_runtime_identity(
                pins, context, ("memory-compiled",)
            )
            self.assertEqual(
                compiled["policy_source_memory_ids"], ["memory-compiled"]
            )
            self.assertNotEqual(current, compiled)
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

                evolution_store_path=root / "evolution.sqlite3",
                dataset_path=root / "dataset",
            )
            with self.assertRaisesRegex(RuntimeError, "EVOAGENT_DASHSCOPE_API_KEY"):
                service.query("强烈岩爆案例有多少个？")

    def test_query_runtime_failures_persist_only_safe_error_trace(self):
        class StubClient:
            provider = "test"
            model = "stub-model"

        class StubEngine:
            def __init__(self, **kwargs):
                self.policy_version = str(kwargs["policy_version"])
                self.runtime_identity = {"engine": "stub"}
                self.version_pins = {
                    "database_snapshot_id": str(
                        kwargs["snapshot"]["snapshot_id"]
                    ),
                    "wiki_index_version": str(kwargs["vanna_index_version"]),
                    "vanna_index_version": str(kwargs["vanna_index_version"]),
                    "memory_snapshot_id": str(kwargs["memory_snapshot_id"]),
                    "policy_version": self.policy_version,
                }

        def assert_safe_failure(
            service: Text2SQLWebService,
            task_id: str,
            secret: str,
            exception_type: str,
        ) -> None:
            with Text2SQLEvolutionStore(
                service.evolution_store_path, service._snapshot()
            ) as evolution:
                trace = evolution.get_query_trace(task_id)
                self.assertEqual(trace["status"], "error")
                self.assertEqual(trace["final_sql"], "")
                self.assertFalse(trace["gates"]["accepted"])
                self.assertEqual(
                    trace["gates"]["errors"], ["text2sql_runtime_error"]
                )
                self.assertEqual(trace["origin"], "web")
                self.assertEqual(trace["source_lane"], "stable")
                self.assertEqual(
                    trace["collaboration"]["diagnostic"],
                    {"exception_type": exception_type},
                )
                serialized_trace = json.dumps(trace, ensure_ascii=False)
                self.assertNotIn(secret, serialized_trace)
                attempt = evolution.connection.execute(
                    "SELECT status,error FROM query_attempts WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                self.assertEqual(dict(attempt), {
                    "status": "error",
                    "error": "text2sql_runtime_error",
                })
                self.assertEqual(evolution.list_memory(), ())
                self.assertEqual(evolution.list_experiences(), ())
            feedback = service.feedback(
                task_id,
                "incorrect",
                "这是运行异常，不是可验证的 Agent 语义修正",
                "SELECT COUNT(*) FROM t_caseinfo",
                user_id="local-user",
                session_id="failure-session",
            )
            self.assertEqual(
                feedback["experience_skipped_reason"],
                "source_run_not_experience_eligible",
            )
            self.assertEqual(feedback["experience_id"], "")
            self.assertEqual(feedback["memory_id"], "")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine_service = Text2SQLWebService(
                _settings(),
                client=StubClient(),
                llm_config={"provider": "test", "model": "stub-model"},
                evolution_store_path=root / "engine-failure.sqlite3",
                checkpoint_store_path=root / "engine-checkpoints.sqlite3",
            )
            engine_secret = "Authorization: Bearer engine-secret-token"
            with patch.object(
                engine_service,
                "_runtime_vanna_pin",
                return_value=("test-vanna", True),
            ), patch(
                "evoagent.text2sql.web_service.Text2SQLAgenticEngine",
                side_effect=RuntimeError(engine_secret),
            ):
                with self.assertRaises(RuntimeError):
                    engine_service.query(
                        "强烈岩爆案例有多少个？",
                        task_id="engine-construction-failure",
                        session_id="failure-session",
                    )
            assert_safe_failure(
                engine_service,
                "engine-construction-failure",
                engine_secret,
                "RuntimeError",
            )

            release_service = Text2SQLWebService(
                _settings(),
                client=StubClient(),
                llm_config={"provider": "test", "model": "stub-model"},
                evolution_store_path=root / "release-failure.sqlite3",
                checkpoint_store_path=root / "release-checkpoints.sqlite3",
            )
            release_secret = "password=release-secret-value"
            with patch.object(
                release_service,
                "_runtime_vanna_pin",
                return_value=("test-vanna", True),
            ), patch(
                "evoagent.text2sql.web_service.Text2SQLAgenticEngine",
                StubEngine,
            ), patch(
                "evoagent.text2sql.web_service.Text2SQLShadowReleaseManager.execute",
                side_effect=ValueError(release_secret),
            ):
                with self.assertRaises(ValueError):
                    release_service.query(
                        "强烈岩爆案例有多少个？",
                        task_id="release-execute-failure",
                        session_id="failure-session",
                    )
            assert_safe_failure(
                release_service,
                "release-execute-failure",
                release_secret,
                "ValueError",
            )

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
        self.assertEqual(
            public["deterministic_runtime"]["protocol"], TEXT2SQL_PROTOCOL
        )
        self.assertEqual(public["deterministic_runtime"]["node_count"], 11)
        self.assertEqual(
            public["deterministic_runtime"]["nodes"],
            list(TEXT2SQL_RUNTIME_NODES),
        )
        self.assertEqual(
            public["deterministic_runtime"]["build_version"], BUILD_VERSION
        )
        self.assertEqual(
            public["deterministic_runtime"]["gate_implementation_version"],
            GATE_IMPLEMENTATION_VERSION,
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
            self.assertEqual(status["deterministic_runtime"]["node_count"], 11)
            self.assertEqual(
                status["deterministic_runtime"]["nodes"],
                list(TEXT2SQL_RUNTIME_NODES),
            )

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

    def test_status_exposes_experience_counts_and_policy_candidate_lineage(self):
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
                vanna_index_root=root / "vanna",
                evolution_store_path=root / "evolution.sqlite3",
            )
            with Text2SQLEvolutionStore(
                root / "evolution.sqlite3", snapshot
            ) as evolution:
                memory_id = evolution.add_experience_memory(
                    {
                        "contract": "ExperienceMemory/v1",
                        "source_task_id": "task-status-experience",
                        "source_revision": 1,
                        "source_stage": "user-feedback",
                        "target_agent": "query-planning",
                        "problem_code": "ordering_limit_mismatch",
                        "scenario": "用户要求按等级从高到低排序。",
                        "problem": "逻辑计划采用了升序。",
                        "correction": "逻辑计划必须明确采用降序。",
                        "applicability": {"query_intent": "ranking"},
                        "before": {"direction": "ASC"},
                        "after": {
                            "direction": "DESC",
                            "sql_fingerprint": "a" * 64,
                        },
                        "evidence": {"review_note": "用户明确纠正排序方向。"},
                        "evidence_grade": "human_confirmed",
                        "state": "candidate",
                    }
                )
                evolution.review_experience_memory(
                    memory_id, "confirm", "reviewer"
                )
                runtime_snapshot = evolution.runtime_memory_snapshot()
                self.assertTrue(
                    all(
                        memory_id
                        not in {item["memory_id"] for item in items}
                        for items in runtime_snapshot["items"].values()
                    )
                )
                parent = evolution.get_policy()
                artifact = parent.as_dict()
                artifact["prompt_fragments"]["query-planning"] = (
                    "When ranking is requested, make the sort direction explicit."
                )
                candidate_version = evolution.propose_policy(
                    artifact,
                    "query-planning",
                    "Compile confirmed ordering experience",
                    "reviewer",
                    proposal_metadata={
                        "contract": "ExperiencePolicyProposal/v1",
                        "source": "confirmed-experiences",
                        "memory_ids": [memory_id],
                        "memory_field_bindings": {
                            memory_id: ["prompt_fragment"]
                        },
                        "target_replay_required": True,
                    },
                )

            status = service.status()
            self.assertEqual(
                status["evolution"]["semantic_experience_counts"],
                {
                    "candidate": 0,
                    "confirmed": 1,
                    "needs_evidence": 0,
                    "rejected": 0,
                },
            )
            candidates = status["evolution"]["policy_candidates"]
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["policy_version"], candidate_version)
            self.assertEqual(
                candidates[0]["proposal_metadata"]["memory_ids"], [memory_id]
            )
            self.assertEqual(candidates[0]["target_replay"], {})
            self.assertEqual(
                candidates[0]["prompt_fragment_change"],
                {
                    "target_agent": "query-planning",
                    "before": "",
                    "after": (
                        "When ranking is requested, make the sort direction explicit."
                    ),
                    "changed": True,
                },
            )

    def test_shadow_sampled_stable_output_is_not_a_production_experience_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = Text2SQLWebService(
                _settings(),
                llm_config={},
                evolution_store_path=root / "evolution.sqlite3",
            )
            collaboration = {
                "delegations": [
                    {
                        "assignment_id": "ground-shadow",
                        "worker": "schema-grounding",
                    }
                ],
                "worker_results": [
                    {
                        "assignment_id": "ground-shadow",
                        "worker": "schema-grounding",
                        "status": "completed",
                        "output": {"schema_plan": {"tables": ["t_casedesc"]}},
                    }
                ],
                "revision_requests": [
                    {
                        "assignment_id": "ground-shadow",
                        "worker": "schema-grounding",
                        "guidance": "补齐字段绑定",
                        "issue_codes": ["missing_schema_binding"],
                    }
                ],
                "revisions_applied": 1,
                "binding_conflicts": [],
                "plan_approval_errors": [],
                "approved_query_plan": {
                    "fingerprint": "a" * 64,
                    "bound_plan": {"fingerprint": "b" * 64},
                },
            }
            result = {
                "task_id": "trace-shadow-source-lane",
                "status": "success",
                "question": "案例数",
                "standalone_question": "案例数",
                "query_type": "DATA_QUERY",
                "final_sql": "SELECT COUNT(*) FROM t_casedesc",
                "gates": {"accepted": True, "errors": []},
                "answer": {"columns": ["count"], "rows": [[1]], "row_count": 1},
                "version_pins": {},
                "release": {
                    "lane": "stable",
                    "shadow_sampled": True,
                    "candidate_output_used": False,
                },
            }

            write_status = service._remember_trace(
                result,
                {"collaboration": collaboration},
                user_id="reviewer",
                session_id="shadow-session",
            )

            self.assertEqual(write_status["source_lane"], "shadow")
            self.assertEqual(
                write_status["experience_skipped_reason"], "non_production_source"
            )
            with Text2SQLEvolutionStore(
                root / "evolution.sqlite3", service._snapshot()
            ) as evolution:
                trace = evolution.get_query_trace("trace-shadow-source-lane")
                self.assertEqual(trace["source_lane"], "shadow")
                self.assertEqual(evolution.list_memory(), ())

    def test_feedback_cannot_turn_non_production_traces_into_experiences(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = Text2SQLWebService(
                _settings(),
                llm_config={},
                evolution_store_path=root / "evolution.sqlite3",
            )
            snapshot = service._snapshot()
            rejected_sources = (
                ("web", "shadow"),
                ("web", "candidate"),
                ("web", "canary"),
                ("cli", "candidate"),
                ("evaluation", "stable"),
                ("debug", "stable"),
            )
            with Text2SQLEvolutionStore(
                root / "evolution.sqlite3", snapshot
            ) as evolution:
                for index, (origin, source_lane) in enumerate(rejected_sources):
                    evolution.save_query_trace(
                        {
                            "task_id": "non-production-feedback-%d" % index,
                            "status": "success",
                            "question": "按岩爆等级排序 %d" % index,
                            "standalone_question": "按岩爆等级排序 %d" % index,
                            "query_type": "DATA_QUERY",
                            "final_sql": (
                                "SELECT c_rockLevel FROM t_casedesc "
                                "ORDER BY c_rockLevel ASC"
                            ),
                            "gates": {"accepted": True, "errors": []},
                            "answer": {
                                "columns": ["c_rockLevel"],
                                "row_count": 1,
                            },
                            "user_id": "reviewer",
                            "session_id": "lane-guard-session",
                            "origin": origin,
                            "source_lane": source_lane,
                            "source_revision": 1,
                            "version_pins": {},
                        }
                    )
                evolution.save_query_trace(
                    {
                        "task_id": "non-production-correct-feedback",
                        "status": "success",
                        "question": "强烈岩爆案例有多少个？",
                        "standalone_question": "强烈岩爆案例有多少个？",
                        "query_type": "DATA_QUERY",
                        "final_sql": (
                            "SELECT COUNT(DISTINCT c_caseCode) AS n "
                            "FROM t_casedesc WHERE c_rockLevel='强烈'"
                        ),
                        "gates": {"accepted": True, "errors": []},
                        "answer": {"columns": ["n"], "row_count": 1},
                        "user_id": "reviewer",
                        "session_id": "lane-guard-session",
                        "origin": "web",
                        "source_lane": "shadow",
                        "source_revision": 1,
                        "version_pins": {},
                    }
                )

            for index, (origin, source_lane) in enumerate(rejected_sources):
                with self.subTest(origin=origin, source_lane=source_lane):
                    feedback = service.feedback(
                        "non-production-feedback-%d" % index,
                        "incorrect",
                        "排序方向错误，应该从高到低",
                        (
                            "SELECT c_rockLevel FROM t_casedesc "
                            "ORDER BY c_rockLevel DESC"
                        ),
                        user_id="reviewer",
                        session_id="lane-guard-session",
                    )
                    self.assertEqual(feedback["experience_id"], "")
                    self.assertEqual(feedback["memory_id"], "")
                    self.assertEqual(feedback["attribution"], {})
                    self.assertEqual(
                        feedback["experience_skipped_reason"],
                        "non_production_source",
                    )
                    self.assertEqual(feedback["source_origin"], origin)
                    self.assertEqual(feedback["source_lane"], source_lane)
                    self.assertEqual(feedback["next_step"], "feedback_recorded")
                    self.assertEqual(feedback["decision"]["outcome"], "rejected")

            correct_feedback = service.feedback(
                "non-production-correct-feedback",
                "correct",
                "结果与业务含义一致",
                "",
                user_id="reviewer",
                session_id="lane-guard-session",
            )
            self.assertEqual(correct_feedback["experience_id"], "")
            self.assertEqual(correct_feedback["memory_id"], "")
            self.assertEqual(
                correct_feedback["experience_skipped_reason"],
                "non_production_source",
            )
            self.assertEqual(correct_feedback["source_lane"], "shadow")
            self.assertEqual(correct_feedback["decision"]["outcome"], "accepted")
            self.assertEqual(correct_feedback["next_step"], "feedback_recorded")

            with Text2SQLEvolutionStore(
                root / "evolution.sqlite3", snapshot
            ) as evolution:
                self.assertEqual(evolution.list_memory(), ())
                self.assertEqual(evolution.list_experiences(), ())
                for index in range(len(rejected_sources)):
                    self.assertEqual(
                        evolution.connection.execute(
                            "SELECT feedback_status FROM query_traces WHERE task_id=?",
                            ("non-production-feedback-%d" % index,),
                        ).fetchone()[0],
                        "incorrect",
                    )

    def test_experience_feedback_only_records_non_production_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = Text2SQLWebService(
                _settings(),
                llm_config={},
                evolution_store_path=root / "evolution.sqlite3",
            )
            snapshot = service._snapshot()
            task_id = "shadow-experience-feedback"
            with Text2SQLEvolutionStore(
                root / "evolution.sqlite3", snapshot
            ) as evolution:
                evolution.save_query_trace(
                    {
                        "task_id": task_id,
                        "status": "success",
                        "question": "最小累计事件数是多少？",
                        "standalone_question": "最小累计事件数是多少？",
                        "query_type": "DATA_QUERY",
                        "final_sql": "SELECT MAX(d_sumEvent) FROM t_activeinfo",
                        "gates": {"accepted": True, "errors": []},
                        "answer": {"columns": ["max"], "row_count": 1},
                        "user_id": "reviewer",
                        "session_id": "shadow-review-session",
                        "origin": "web",
                        "source_lane": "shadow",
                        "source_revision": 1,
                        "version_pins": {},
                    }
                )
                experience_id = evolution.add_experience_candidate(
                    task_id,
                    "最小累计事件数是多少？",
                    "SELECT MAX(d_sumEvent) FROM t_activeinfo",
                    eligible=False,
                    eligibility_reasons=["requires_human_feedback"],
                )

            pending = service.experiences()["experiences"][0]
            self.assertEqual(pending["experience_id"], experience_id)
            self.assertFalse(pending["confirmable"])
            feedback = service.feedback_experience(
                experience_id,
                "incorrect",
                "聚合口径错误，应该取最小值",
                "SELECT MIN(d_sumEvent) FROM t_activeinfo",
                "reviewer",
            )

            self.assertEqual(feedback["state"], "rejected")
            self.assertEqual(feedback["memory_id"], "")
            self.assertEqual(feedback["corrected_experience_id"], "")
            self.assertEqual(feedback["attribution"], {})
            self.assertEqual(
                feedback["experience_skipped_reason"], "non_production_source"
            )
            self.assertEqual(feedback["source_origin"], "web")
            self.assertEqual(feedback["source_lane"], "shadow")
            self.assertEqual(feedback["next_step"], "feedback_recorded")
            with Text2SQLEvolutionStore(
                root / "evolution.sqlite3", snapshot
            ) as evolution:
                self.assertEqual(evolution.list_memory(), ())
                self.assertEqual(len(evolution.list_experiences()), 1)
                self.assertEqual(
                    evolution.connection.execute(
                        "SELECT feedback_status FROM query_traces WHERE task_id=?",
                        (task_id,),
                    ).fetchone()[0],
                    "incorrect",
                )

    def test_correct_feedback_cannot_promote_shadow_experience(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = Text2SQLWebService(
                _settings(),
                llm_config={},
                evolution_store_path=root / "evolution.sqlite3",
            )
            snapshot = service._snapshot()
            task_id = "shadow-experience-correct-feedback"
            with Text2SQLEvolutionStore(
                root / "evolution.sqlite3", snapshot
            ) as evolution:
                evolution.save_query_trace(
                    {
                        "task_id": task_id,
                        "status": "success",
                        "question": "最大累计事件数是多少？",
                        "standalone_question": "最大累计事件数是多少？",
                        "query_type": "DATA_QUERY",
                        "final_sql": "SELECT MAX(d_sumEvent) FROM t_activeinfo",
                        "gates": {"accepted": True, "errors": []},
                        "answer": {"columns": ["max"], "row_count": 1},
                        "user_id": "reviewer",
                        "session_id": "shadow-review-session",
                        "origin": "web",
                        "source_lane": "shadow",
                        "source_revision": 1,
                        "version_pins": {},
                    }
                )
                experience_id = evolution.add_experience_candidate(
                    task_id,
                    "最大累计事件数是多少？",
                    "SELECT MAX(d_sumEvent) FROM t_activeinfo",
                    eligible=False,
                    eligibility_reasons=["requires_human_feedback"],
                )

            feedback = service.feedback_experience(
                experience_id,
                "correct",
                "结果看起来正确，但来源是 shadow",
                "",
                "reviewer",
            )
            self.assertEqual(feedback["state"], "ineligible")
            self.assertEqual(feedback["user_feedback"], "correct")
            self.assertEqual(feedback["memory_id"], "")
            self.assertEqual(feedback["corrected_experience_id"], "")
            self.assertEqual(
                feedback["experience_skipped_reason"], "non_production_source"
            )
            self.assertEqual(feedback["next_step"], "feedback_recorded")
            with self.assertRaisesRegex(ValueError, "stable Web/CLI"):
                service.confirm_experience(experience_id, "reviewer")
            with Text2SQLEvolutionStore(
                root / "evolution.sqlite3", snapshot
            ) as evolution:
                self.assertEqual(evolution.list_memory(), ())
                self.assertEqual(len(evolution.list_experiences()), 1)
                self.assertEqual(evolution.list_experiences()[0]["state"], "ineligible")

    def test_feedback_evidence_rehydrates_compiled_policy_memory_lineage(self):
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
            with Text2SQLEvolutionStore(
                root / "evolution.sqlite3", snapshot
            ) as evolution:
                source_memory_id = evolution.add_experience_memory(
                    {
                        "contract": "ExperienceMemory/v1",
                        "source_task_id": "task-policy-source",
                        "source_revision": 1,
                        "source_stage": "user-feedback",
                        "target_agent": "query-planning",
                        "problem_code": "ordering_limit_mismatch",
                        "scenario": "排序请求需要明确方向。",
                        "problem": "计划没有确定排序方向。",
                        "correction": "明确排序方向。",
                        "before": {"direction": "unknown"},
                        "after": {
                            "direction": "explicit",
                            "sql_fingerprint": "a" * 64,
                        },
                        "evidence": {"reviewed": True},
                        "evidence_grade": "human_confirmed",
                        "state": "candidate",
                    }
                )
                evolution.review_experience_memory(
                    source_memory_id, "confirm", "reviewer"
                )
                parent = evolution.get_policy()
                artifact = parent.as_dict()
                artifact["prompt_fragments"]["query-planning"] = (
                    "Always state the requested sort direction."
                )
                policy_version = evolution.propose_policy(
                    artifact,
                    "query-planning",
                    "Compile ordering guidance",
                    "reviewer",
                    proposal_metadata={
                        "contract": "ExperiencePolicyProposal/v1",
                        "source": "confirmed-experiences",
                        "memory_ids": [source_memory_id],
                        "memory_field_bindings": {
                            source_memory_id: ["prompt_fragment"]
                        },
                    },
                )

            service._remember_trace(
                {
                    "task_id": "trace-derived-feedback",
                    "status": "success",
                    "question": "按岩爆等级排序",
                    "standalone_question": "按岩爆等级排序",
                    "query_type": "DATA_QUERY",
                    "final_sql": (
                        "SELECT c_rockLevel FROM t_casedesc "
                        "ORDER BY c_rockLevel ASC"
                    ),
                    "gates": {"accepted": True, "errors": []},
                    "answer": {
                        "columns": ["c_rockLevel"],
                        "rows": [["强烈"]],
                        "row_count": 1,
                    },
                    "version_pins": {"policy_version": policy_version},
                },
                user_id="reviewer",
                session_id="lineage-session",
            )
            feedback = service.feedback(
                "trace-derived-feedback",
                "incorrect",
                "排序方向错误，应该从高到低",
                (
                    "SELECT c_rockLevel FROM t_casedesc "
                    "ORDER BY c_rockLevel DESC"
                ),
                user_id="reviewer",
                session_id="lineage-session",
            )
            with Text2SQLEvolutionStore(
                root / "evolution.sqlite3", snapshot
            ) as evolution:
                learned = evolution.get_memory(feedback["memory_id"])
            self.assertEqual(
                learned["rule"]["evidence"]["derived_from_memory_ids"],
                [source_memory_id],
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
            self.assertEqual(dashboard["session_view"]["mode"], "current")
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
            self.assertNotIn("execution", episode)
            self.assertNotIn("version_pins", episode)
            self.assertEqual(episode["temporal_context"]["turn_number"], 1)
            self.assertTrue(episode["temporal_context"]["recorded_at"])
            self.assertIn("database_snapshot_id", episode["version_context"])
            self.assertEqual(
                episode["decisions"]["harness"]["outcome"], "rejected"
            )
            self.assertEqual(episode["decisions"]["human"], {})
            self.assertFalse(dashboard["boundaries"]["raw_model_reasoning_exposed"])
            self.assertTrue(
                dashboard["boundaries"]["stable_semantic_memory_only_injected"]
            )

            historical = service.memory("reader", "new-browser-session", 10)
            self.assertEqual(
                historical["session_view"]["mode"], "latest_history"
            )
            self.assertTrue(
                historical["session_view"]["current_session_empty"]
            )
            self.assertEqual(
                historical["session_view"]["display_session_id"], "session-1"
            )
            self.assertEqual(historical["layers"]["working"]["count"], 2)
            self.assertEqual(historical["layers"]["episodic"]["count"], 1)

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

    def test_confirmed_query_is_persisted_in_vanna_question_sql(self):
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
            evolution_path = root / "evolution.sqlite3"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            build_test_corpus(root / "vanna", snapshot, join_catalog)
            service = Text2SQLWebService(
                _settings(),
                llm_config={},
                snapshot_path=snapshot_path,
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
            self.assertTrue(pending["confirmable"])
            with patch(
                "evoagent.text2sql.web_service.VannaRetrieverOnly.build",
                return_value={
                    "ready": True,
                    "index_version": "stable-test",
                    "item_count": 1,
                    "counts": {"ddl": 0, "documentation": 0, "sql": 1},
                },
            ) as build_vanna:
                feedback = service.feedback(
                    "trace-feedback",
                    "correct",
                    "结果与业务含义一致",
                    "",
                    user_id="reviewer",
                    session_id="session-1",
                )
            self.assertTrue(feedback["experience_id"])
            self.assertEqual(feedback["next_step"], "available_in_vanna")
            self.assertEqual(feedback["experience"]["state"], "promoted")
            self.assertEqual(
                feedback["experience"]["memory_kind"], "vanna_question_sql"
            )
            self.assertFalse(feedback["experience"]["semantic_memory_written"])
            confirmed = service.experiences("promoted")["experiences"][0]
            self.assertEqual(confirmed["experience_id"], feedback["experience_id"])
            self.assertEqual(confirmed["user_feedback"], "correct")
            self.assertTrue(confirmed["knowledge_evidence_id"])
            build_vanna.assert_called_once()
            indexed_items = build_vanna.call_args.args[0]
            self.assertEqual(
                [
                    item["knowledge_type"]
                    for item in indexed_items
                    if item["knowledge_type"] == "verified_example"
                ],
                ["verified_example"],
            )
            from evoagent.text2sql.vanna_corpus import load_confirmed_question_sql
            confirmed_pairs = load_confirmed_question_sql(
                question_sql_registry_path(root / "vanna"), snapshot["snapshot_id"],
            )
            self.assertEqual(len(confirmed_pairs), 1)
            self.assertEqual(confirmed_pairs[0]["sql"], confirmed["sql"])
            dashboard = service.memory("reviewer", "session-1", 10)
            self.assertEqual(dashboard["question_sql"]["counts"]["promoted"], 1)
            self.assertFalse(dashboard["question_sql"]["semantic_memory"])
            self.assertEqual(
                sum(dashboard["layers"]["semantic"]["counts"].values()), 0
            )
            self.assertEqual(
                dashboard["question_sql"]["items"][0]["experience_id"],
                feedback["experience_id"],
            )

    def test_ineligible_experience_can_be_confirmed_from_review_surface(self):
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
            build_test_corpus(root / "vanna", snapshot)
            service = Text2SQLWebService(
                _settings(),
                llm_config={},
                snapshot_path=snapshot_path,
                evolution_store_path=root / "evolution.sqlite3",
                vanna_index_root=root / "vanna",
            )
            service._remember_trace(
                {
                    "task_id": "trace-cross-page-confirm",
                    "status": "success",
                    "question": "最大累计事件数是多少？",
                    "standalone_question": "最大累计事件数是多少？",
                    "query_type": "DATA_QUERY",
                    "final_sql": "SELECT MAX(d_sumEvent) FROM t_activeinfo",
                    "gates": {"accepted": True},
                    "agents": [],
                    "execution": {},
                    "version_pins": {},
                    "answer": {"columns": ["max"], "rows": [[1]], "row_count": 1},
                },
                user_id="reviewer",
                session_id="previous-session",
            )
            pending = service.experiences()["experiences"][0]
            self.assertTrue(pending["confirmable"])

            with patch(
                "evoagent.text2sql.web_service.VannaRetrieverOnly.build",
                return_value={
                    "ready": True,
                    "index_version": "stable-test",
                    "item_count": 1,
                    "counts": {"ddl": 0, "documentation": 0, "sql": 1},
                },
            ):
                confirmed = service.confirm_experience(
                    pending["experience_id"], "reviewer", "结果与业务含义一致"
                )

            self.assertEqual(confirmed["state"], "promoted")
            self.assertTrue(confirmed["eligible"])
            self.assertEqual(confirmed["user_feedback"], "correct")
            self.assertEqual(confirmed["source_kind"], "human_confirmed_query")
            self.assertEqual(
                confirmed["next_step"], "available_in_vanna"
            )
            self.assertEqual(confirmed["memory_kind"], "vanna_question_sql")
            self.assertFalse(confirmed["semantic_memory_written"])
            self.assertTrue(confirmed["knowledge_evidence_id"])

    def test_ineligible_experience_can_be_rejected_from_review_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = Text2SQLWebService(
                _settings(),
                llm_config={},
                evolution_store_path=root / "evolution.sqlite3",
            )
            service._remember_trace(
                {
                    "task_id": "trace-cross-page-reject",
                    "status": "success",
                    "question": "最小累计事件数是多少？",
                    "standalone_question": "最小累计事件数是多少？",
                    "query_type": "DATA_QUERY",
                    "final_sql": "SELECT MAX(d_sumEvent) FROM t_activeinfo",
                    "gates": {"accepted": True, "errors": []},
                    "agents": [],
                    "execution": {},
                    "version_pins": {},
                    "answer": {"columns": ["max"], "rows": [[9]], "row_count": 1},
                },
                user_id="reviewer",
                session_id="previous-session",
            )
            pending = service.experiences()["experiences"][0]

            feedback = service.feedback_experience(
                pending["experience_id"],
                "incorrect",
                "聚合口径错误，应该取最小值",
                "SELECT MIN(d_sumEvent) FROM t_activeinfo",
                "reviewer",
            )

            self.assertEqual(feedback["state"], "rejected")
            self.assertTrue(feedback["memory_id"])
            self.assertTrue(feedback["corrected_experience_id"])
            self.assertEqual(
                feedback["attribution"]["target_skill"], "query-planning"
            )
            with Text2SQLEvolutionStore(
                root / "evolution.sqlite3", service._snapshot()
            ) as evolution:
                corrected = evolution.get_experience(
                    feedback["corrected_experience_id"]
                )
                self.assertEqual(corrected["state"], "candidate")
                self.assertEqual(corrected["source_kind"], "human_corrected_sql")
                self.assertEqual(len(evolution.list_memory("candidate")), 1)

    def test_feedback_without_correction_needs_evidence_and_stays_out_of_runtime(self):
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
                dashboard["layers"]["semantic"]["counts"]["candidate"], 0
            )
            self.assertEqual(
                dashboard["layers"]["semantic"]["counts"]["needs_evidence"], 1
            )
            experience = dashboard["layers"]["semantic"]["items"][0]
            self.assertEqual(experience["state"], "needs_evidence")
            self.assertEqual(experience["origin_split"], "production_feedback")
            self.assertEqual(experience["memory_kind"], "experience")
            self.assertEqual(experience["rule"]["contract"], "ExperienceMemory/v1")
            self.assertEqual(experience["rule"]["evidence_grade"], "human_feedback_only")
            self.assertEqual(experience["target_agent"], "query-planning")
            self.assertEqual(experience["problem_code"], "ordering_limit_mismatch")
            self.assertFalse(experience["runtime_eligible"])
            with Text2SQLEvolutionStore(
                root / "evolution.sqlite3", service._snapshot()
            ) as evolution:
                runtime_before = evolution.runtime_memory_snapshot()
            with self.assertRaisesRegex(ValueError, "not awaiting review"):
                service.review_memory_candidate(
                    feedback["memory_id"],
                    "confirm",
                    "human-reviewer",
                )
            refreshed = service.memory("reviewer", "session-1", 10)
            self.assertEqual(
                refreshed["layers"]["semantic"]["counts"]["candidate"], 0
            )
            self.assertEqual(
                refreshed["layers"]["semantic"]["counts"]["confirmed"], 0
            )
            self.assertEqual(
                refreshed["layers"]["semantic"]["counts"]["needs_evidence"], 1
            )
            self.assertEqual(
                refreshed["layers"]["semantic"]["experience_counts"],
                {
                    "candidate": 0,
                    "confirmed": 0,
                    "needs_evidence": 1,
                    "rejected": 0,
                },
            )
            self.assertFalse(
                refreshed["boundaries"]["experience_direct_runtime_injection"]
            )
            with Text2SQLEvolutionStore(
                root / "evolution.sqlite3", service._snapshot()
            ) as evolution:
                runtime_after = evolution.runtime_memory_snapshot()
            self.assertEqual(
                runtime_after["memory_snapshot_id"],
                runtime_before["memory_snapshot_id"],
            )
            self.assertTrue(
                all(
                    feedback["memory_id"]
                    not in {item["memory_id"] for item in items}
                    for items in runtime_after["items"].values()
                )
            )


if __name__ == "__main__":
    unittest.main()
