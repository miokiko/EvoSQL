import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from evoagent.runtime import ToolProtocolError
from evoagent.text2sql.contracts import QuerySpec, SQLCandidate, SchemaPlan
from evoagent.text2sql.agentic import (
    CRITIC_PROMPT,
    SCHEMA_PROMPT,
    STRATEGY_PROMPT,
    TEXT2SQL_OBSERVATION_TOKEN_BUDGET,
    Text2SQLAgenticEngine,
)
from evoagent.text2sql.database_tools import Text2SQLToolSuite
from evoagent.text2sql.checkpoint_store import (
    Text2SQLCheckpointIdentityError,
    Text2SQLRuntimeCheckpointStore,
)
from evoagent.text2sql.knowledge_store import KnowledgeStore
from evoagent.text2sql.sql_safety import ReadOnlySQLiteExecutor, validate_sql
from evoagent.text2sql.sqlite_database import build_sqlite_database
from evoagent.text2sql.vanna_retriever import VannaRetrieval


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


class ScriptedClient:
    def __init__(self, sql):
        self.model = "scripted"
        self.provider = "test"
        self.lock = threading.Lock()
        self.responses = {
            "text2sql-lead": [
                {
                    "action": "final",
                    "delegations": [
                        {
                            "assignment_id": "grounding-1",
                            "worker": "schema-grounding",
                            "objective": "Ground the case level and counting key.",
                        },
                        {
                            "assignment_id": "strategy-1",
                            "worker": "sql-strategy",
                            "objective": "Produce a scalar count query.",
                        },
                    ],
                    "risk_level": "normal",
                    "reasoning_summary": "Two independent views are required.",
                },
                {
                    "action": "final",
                    "revision_requests": [],
                    "critic_objective": "Check value grounding and distinct count semantics.",
                    "reasoning_summary": "Worker outputs agree.",
                },
                {
                    "action": "final",
                    "final_candidate_index": 0,
                    "resolved_objections": [],
                    "resolution_summary": "The candidate is grounded and executable.",
                },
            ],
            "schema-grounding": [
                {
                    "action": "final",
                    "schema_plan": {
                        "tables": ["t_casedesc"],
                        "columns": [
                            "t_casedesc.c_caseCode",
                            "t_casedesc.c_rockLevel",
                        ],
                        "joins": [],
                        "result_grain": ["t_casedesc.c_caseCode"],
                    },
                    "grounding_notes": ["强烈 is an observed exact value."],
                },
            ],
            "sql-strategy": [
                {
                    "action": "final",
                    "query_spec": {
                        "intent": "count",
                        "subject": "强烈岩爆案例",
                        "dimensions": [],
                        "measures": [{"name": "案例数", "aggregation": "count"}],
                        "filters": [
                            {"field_concept": "岩爆等级", "operator": "eq", "value": "强烈"}
                        ],
                        "order_by": [],
                        "limit": 20,
                        "expected_shape": "scalar",
                        "version": 1,
                    },
                    "sql_candidates": [{"candidate_id": "candidate-1", "sql": sql}],
                },
            ],
            "text2sql-critic": [
                {
                    "action": "final",
                    "decisions": [
                        {"candidate_index": 0, "accepted": True, "objections": []}
                    ],
                    "summary": "No unresolved objection.",
                }
            ],
        }

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        with self.lock:
            response = self.responses[role].pop(0)
        if ledger:
            ledger.record_model(
                role,
                self.provider,
                self.model,
                {"prompt_tokens": 1, "completion_tokens": 1},
                0,
            )
        return response


class DirectStrategyClient(ScriptedClient):
    def __init__(self, sql):
        super().__init__(sql)
        self.responses["sql-strategy"] = [self.responses["sql-strategy"][-1]]


class ResultQAClient:
    model = "scripted"
    provider = "test"

    def __init__(self):
        self.responses = [
            {
                "action": "final",
                "route": {
                    "type": "RESULT_QA",
                    "standalone_question": "刚才的结果是多少？",
                    "parent_query_run_id": "previous-run",
                    "reason": "The question explicitly references the previous result.",
                },
                "delegations": [],
                "risk_level": "low",
                "reasoning_summary": "Use cached result only.",
            },
            {
                "action": "final",
                "answer_text": "上一轮结果是 6 个案例。",
                "requires_new_query": False,
                "reasoning_summary": "The cached scalar directly answers the question.",
            },
        ]

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        self.assert_role = role
        response = self.responses.pop(0)
        if ledger:
            ledger.record_model(role, self.provider, self.model, {}, 0)
        return response


class FailAtAssessmentClient(ScriptedClient):
    def __init__(self, sql):
        super().__init__(sql)
        self.lead_calls = 0

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        if role == "text2sql-lead":
            self.lead_calls += 1
            if self.lead_calls == 2:
                raise RuntimeError("simulated process interruption")
        return super().complete_json(role, system, user, ledger, max_tokens)


class ResumeAfterAssessmentClient(ScriptedClient):
    def __init__(self, sql):
        super().__init__(sql)
        self.responses["text2sql-lead"] = self.responses["text2sql-lead"][1:]
        self.called_roles = []

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        self.called_roles.append(role)
        return super().complete_json(role, system, user, ledger, max_tokens)


class NoCallClient:
    model = "scripted"
    provider = "test"

    def complete_json(self, *args, **kwargs):
        raise AssertionError("a completed checkpoint must not call the model")


class Text2SQLContractTests(unittest.TestCase):
    def test_role_prompts_require_json_and_strategy_explain_gate(self):
        self.assertIn("JSON", SCHEMA_PROMPT)
        self.assertIn("JSON", STRATEGY_PROMPT)
        self.assertIn("JSON", CRITIC_PROMPT)
        self.assertIn("Do not call tools", CRITIC_PROMPT)
        self.assertIn("Harness will always", STRATEGY_PROMPT)
        self.assertIn("Never\nguess a table name", SCHEMA_PROMPT)
        self.assertEqual(TEXT2SQL_OBSERVATION_TOKEN_BUDGET, 1600)

    def test_contracts_require_valid_shapes_and_all_version_pins(self):
        spec = QuerySpec.from_dict(
            {
                "intent": "count",
                "subject": "强烈岩爆案例",
                "expected_shape": "scalar",
            }
        )
        self.assertEqual(spec.limit, 20)
        with self.assertRaises(ValueError):
            SchemaPlan.from_dict(
                {"tables": ["t_caseinfo"], "columns": ["t_missing.c_fake"]}
            )
        with self.assertRaises(ValueError):
            SQLCandidate("c1", "SELECT 1", 1, SNAPSHOT["snapshot_id"], "", "m1", "p1")


class Text2SQLSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
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

    def test_ast_gate_accepts_select_and_cte_but_rejects_unsafe_sql(self):
        accepted = validate_sql(
            "SELECT COUNT(DISTINCT c_caseCode) FROM t_casedesc WHERE c_rockLevel='强烈'",
            SNAPSHOT,
        )
        self.assertTrue(accepted.accepted)
        self.assertEqual(accepted.tables, ("t_casedesc",))
        self.assertTrue(
            validate_sql(
                "WITH x AS (SELECT c_caseCode FROM t_casedesc) SELECT COUNT(*) FROM x",
                SNAPSHOT,
            ).accepted
        )
        rejected = (
            "DELETE FROM t_caseinfo",
            "SELECT * FROM unknown_table",
            "SELECT 1; SELECT 2",
            "SELECT * FROM t_caseinfo -- hide a second intent",
        )
        self.assertTrue(all(not validate_sql(sql, SNAPSHOT).accepted for sql in rejected))

    def test_executor_is_read_only_bounded_and_returns_explain(self):
        executor = ReadOnlySQLiteExecutor(self.database, SNAPSHOT, max_rows=2)
        result = executor.execute(
            "SELECT COUNT(DISTINCT c_caseCode) AS n FROM t_casedesc WHERE c_rockLevel='强烈'"
        )
        self.assertEqual(result.rows, ((6,),))
        self.assertTrue(result.explain_plan)
        many = executor.execute("SELECT c_caseCode FROM t_caseinfo ORDER BY c_caseCode")
        self.assertEqual(many.row_count, 2)
        self.assertTrue(many.truncated)
        with self.assertRaises(ValueError):
            executor.execute("UPDATE t_caseinfo SET c_mark='0'")

    def test_tools_are_role_scoped_and_return_traceable_evidence(self):
        suite = Text2SQLToolSuite(
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            principals=["local-user"],
            memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v1",
        )
        schema_tools = suite.registry("schema-grounding")
        self.assertNotIn("execute_sql", schema_tools.names())
        with self.assertRaises(ToolProtocolError):
            schema_tools.invoke("execute_sql", {"sql": "SELECT 1"})
        sample = schema_tools.invoke(
            "sample_values", {"table": "t_casedesc", "column": "c_rockLevel"}
        )
        self.assertTrue(sample["evidence_id"].startswith("text2sql-tool:"))
        self.assertIn(
            {"value": "强烈", "count": 6}, sample["output"]["values"]
        )
        lead = suite.registry("text2sql-lead").invoke(
            "execute_sql",
            {
                "sql": "SELECT COUNT(DISTINCT c_caseCode) AS n "
                "FROM t_casedesc WHERE c_rockLevel='强烈'"
            },
        )
        self.assertEqual(lead["output"]["rows"], [[6]])

    def test_vanna_rank_is_a_bonus_and_cannot_displace_exact_value_evidence(self):
        with KnowledgeStore(self.knowledge) as store:
            irrelevant = store.connection.execute(
                "SELECT evidence_id FROM knowledge_items "
                "WHERE state='stable' AND item_key='table:t_activeinfoevent'"
            ).fetchone()["evidence_id"]
        with patch(
            "evoagent.text2sql.database_tools.VannaRetrieverOnly"
        ) as retriever_class:
            retriever_class.return_value.retrieve.return_value = VannaRetrieval(
                evidence_ids=(irrelevant,),
                index_version="stable-test",
            )
            suite = Text2SQLToolSuite(
                database_path=self.database,
                snapshot=SNAPSHOT,
                knowledge_store_path=self.knowledge,
                vanna_index_root=Path(self.temporary.name) / "vanna",
                vanna_index_version="stable-test",
                principals=["local-user"],
                memory_snapshot_id="memory-empty-v1",
                policy_version="policy-v1",
            )
            output = suite.registry("schema-grounding").invoke(
                "retrieve_knowledge",
                {"query": "强烈岩爆案例有多少个", "limit": 5},
            )["output"]

        titles = [item["title"] for item in output["evidence"]]
        self.assertIn("字段值域 t_casedesc.c_rockLevel", titles)
        self.assertNotEqual(titles[0], "表 t_activeinfoevent")

    def test_vanna_local_index_access_is_serialized_across_evaluation_threads(self):
        class RacingRetriever:
            active = 0
            maximum = 0
            lock = threading.Lock()

            def __init__(self, *_args, **_kwargs):
                pass

            def retrieve(self, _query):
                with self.lock:
                    type(self).active += 1
                    type(self).maximum = max(type(self).maximum, type(self).active)
                time.sleep(0.02)
                with self.lock:
                    type(self).active -= 1
                return VannaRetrieval(index_version="stable-test")

        suite = Text2SQLToolSuite(
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            vanna_index_root=Path(self.temporary.name) / "vanna",
            vanna_index_version="stable-test",
            principals=["local-user"],
            memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v1",
        )
        with patch(
            "evoagent.text2sql.database_tools.VannaRetrieverOnly", RacingRetriever
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                calls = [
                    pool.submit(
                        suite.registry("schema-grounding").invoke,
                        "retrieve_knowledge",
                        {"query": "岩爆案例", "limit": 5},
                    )
                    for _ in range(2)
                ]
                for call in calls:
                    call.result()
        self.assertEqual(RacingRetriever.maximum, 1)

    def test_original_hierarchical_protocol_runs_before_deterministic_execution(self):
        sql = (
            "SELECT COUNT(DISTINCT c_caseCode) AS n "
            "FROM t_casedesc WHERE c_rockLevel='强烈'"
        )
        engine = Text2SQLAgenticEngine(
            client=ScriptedClient(sql),
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            principals=["local-user"],
            memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v1",
        )
        result = engine.run("强烈岩爆案例有多少个")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["answer"]["rows"], [[6]])
        self.assertEqual(
            result["collaboration"]["protocol"], "lead-workers-text2sql-v1"
        )
        self.assertEqual(
            {item["worker"] for item in result["collaboration"]["worker_results"]},
            {"schema-grounding", "sql-strategy"},
        )
        self.assertEqual(result["execution"]["llm_calls"], 6)

    def test_harness_always_validates_and_explains_direct_sql_candidates(self):
        sql = (
            "SELECT COUNT(DISTINCT c_caseCode) AS n "
            "FROM t_casedesc WHERE c_rockLevel='强烈'"
        )
        engine = Text2SQLAgenticEngine(
            client=DirectStrategyClient(sql),
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            principals=["local-user"],
            memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v1",
        )
        result = engine.run("强烈岩爆案例有多少个")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["answer"]["rows"], [[6]])
        tools = [item["tool"] for item in result["execution"]["tool_call_log"]]
        self.assertIn("validate_sql", tools)
        self.assertIn("explain_sql", tools)

    def test_harness_recovers_one_critic_accepted_candidate_from_bad_leader_index(self):
        sql = (
            "SELECT COUNT(DISTINCT c_caseCode) AS n "
            "FROM t_casedesc WHERE c_rockLevel='强烈'"
        )
        client = ScriptedClient(sql)
        client.responses["text2sql-lead"][-1]["final_candidate_index"] = 9
        engine = Text2SQLAgenticEngine(
            client=client,
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            principals=["local-user"],
            memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v1",
        )
        result = engine.run("强烈岩爆案例有多少个")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["answer"]["rows"], [[6]])
        self.assertTrue(
            any(
                item["event"] == "single_accepted_candidate_recovered"
                for item in result["execution"]["agent_traces"]["text2sql-harness"]
            )
        )

    def test_user_explicit_join_can_be_grounded_without_promoting_inferred_relation(self):
        engine = Text2SQLAgenticEngine(
            client=ScriptedClient("SELECT 1"),
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            principals=["local-user"],
            memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v1",
        )
        plan = engine._validated_schema_plan(
            {
                "tables": ["t_harm", "t_support"],
                "columns": ["t_harm.c_caseCode", "t_support.c_caseCode"],
                "joins": [
                    {
                        "left": "t_harm.c_caseCode",
                        "right": "t_support.c_caseCode",
                        "type": "inner",
                        "source": "user_explicit",
                        "evidence_id": "join:user_explicit_fake_model_id",
                    }
                ],
                "result_grain": [],
            },
            "按 t_harm.c_caseCode = t_support.c_caseCode 连接",
        )
        self.assertEqual(plan.joins[0].source, "user_explicit")
        self.assertTrue(plan.joins[0].evidence_id.startswith("join:"))
        self.assertNotEqual(
            plan.joins[0].evidence_id, "join:user_explicit_fake_model_id"
        )
        normalized = engine._grounding_plan_value(
            {
                "schema_plan": {
                    "tables": ["t_harm", "t_support"],
                    "columns": ["t_harm.c_caseCode", "t_support.c_caseCode"],
                    "joins": [
                        {
                            "left": "t_harm.c_caseCode",
                            "right": "t_support.c_caseCode",
                            "type": "inner",
                            "source": "stable",
                        }
                    ],
                }
            },
            {
                "joins": [
                    {
                        "left": "t_harm.c_caseCode",
                        "right": "t_support.c_caseCode",
                        "type": "inner",
                        "source": "user_explicit",
                    }
                ]
            },
        )
        self.assertEqual(normalized["joins"][0]["source"], "user_explicit")
        with self.assertRaises(ValueError):
            engine._validated_schema_plan(
                plan.as_dict(),
                "查询危害和支护信息",
            )

    def test_invalid_grounding_output_falls_back_to_snapshot_checked_direct_links(self):
        sql = (
            "SELECT COUNT(DISTINCT c_caseCode) AS n "
            "FROM t_casedesc WHERE c_rockLevel='强烈'"
        )
        client = ScriptedClient(sql)
        client.responses["schema-grounding"][0]["schema_plan"] = {
            "tables": ["t_casedesc"],
            "columns": ["t_casedesc.invented_column"],
            "joins": [],
            "result_grain": [],
        }
        engine = Text2SQLAgenticEngine(
            client=client,
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            principals=["local-user"],
            memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v1",
        )
        result = engine.run(
            "在表 t_casedesc 中统计 c_rockLevel='强烈' 的不同 c_caseCode 数量"
        )
        self.assertEqual(result["status"], "success")
        grounding = next(
            item
            for item in result["collaboration"]["worker_results"]
            if item["worker"] == "schema-grounding"
        )
        self.assertEqual(grounding["status"], "completed")
        self.assertIn(
            "t_casedesc.c_caseCode", grounding["output"]["schema_plan"]["columns"]
        )

    def test_strategy_query_spec_synonyms_do_not_discard_valid_group_sql(self):
        sql = (
            "SELECT c_rockLevel, COUNT(*) AS n FROM t_casedesc "
            "GROUP BY c_rockLevel ORDER BY c_rockLevel"
        )
        client = ScriptedClient(sql)
        client.responses["schema-grounding"][0]["schema_plan"] = {
            "tables": ["t_casedesc"],
            "columns": ["t_casedesc.c_rockLevel"],
            "joins": [],
            "result_grain": ["t_casedesc.c_rockLevel"],
        }
        client.responses["sql-strategy"][0]["query_spec"] = {
            "intent": "group",
            "subject": "",
            "expected_shape": "table",
            "limit": 0,
        }
        engine = Text2SQLAgenticEngine(
            client=client,
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            principals=["local-user"],
            memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v1",
        )
        result = engine.run(
            "统计表 t_casedesc 按 c_rockLevel 分组的记录数，并按该字段升序排列"
        )
        self.assertEqual(result["status"], "success")
        strategy = next(
            item
            for item in result["collaboration"]["worker_results"]
            if item["worker"] == "sql-strategy"
        )
        self.assertEqual(strategy["output"]["query_spec"]["intent"], "count")
        self.assertEqual(
            strategy["output"]["query_spec"]["expected_shape"], "rows"
        )
        self.assertEqual(strategy["output"]["query_spec"]["limit"], 1)

    def test_write_candidate_is_rejected_even_if_agents_and_critic_accept_it(self):
        sql = "DELETE FROM t_casedesc WHERE c_rockLevel='强烈'"
        engine = Text2SQLAgenticEngine(
            client=ScriptedClient(sql),
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            principals=["local-user"],
            memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v1",
        )
        result = engine.run("删除强烈岩爆案例")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("invalid_final_candidate_index", result["gates"]["errors"])

    def test_leader_routes_result_qa_without_spawning_sql_workers(self):
        engine = Text2SQLAgenticEngine(
            client=ResultQAClient(),
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            principals=["local-user"],
            memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v1",
            result_snapshot_provider=lambda task_id: {
                "task_id": task_id,
                "status": "success",
                "answer": {"columns": ["n"], "row_count": 1, "truncated": False},
                "rows": [[6]],
            },
        )
        result = engine.run(
            "刚才的结果是多少？",
            conversation_context={
                "recent_query_runs": [
                    {"task_id": "previous-run", "status": "success"}
                ]
            },
        )
        self.assertEqual(result["query_type"], "RESULT_QA")
        self.assertEqual(result["answer"]["summary_text"], "上一轮结果是 6 个案例。")
        self.assertEqual(result["collaboration"]["worker_results"], [])
        self.assertEqual(result["execution"]["tool_calls"], 0)

    def test_runtime_resumes_nodes_after_restart_and_caches_final_result(self):
        sql = (
            "SELECT COUNT(DISTINCT c_caseCode) AS n "
            "FROM t_casedesc WHERE c_rockLevel='强烈'"
        )
        checkpoint_path = Path(self.temporary.name) / "runtime-checkpoints.sqlite3"
        task_id = "resume-after-assessment"
        first = Text2SQLAgenticEngine(
            client=FailAtAssessmentClient(sql),
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            principals=["local-user"],
            memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v1",
            checkpoint_store=Text2SQLRuntimeCheckpointStore(checkpoint_path),
        )
        with self.assertRaisesRegex(RuntimeError, "simulated process interruption"):
            first.run("强烈岩爆案例有多少个", task_id=task_id)
        failed = first.checkpoint_store.inspect(task_id)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["checkpoint_count"], 4)

        resumed_client = ResumeAfterAssessmentClient(sql)
        resumed = Text2SQLAgenticEngine(
            client=resumed_client,
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            principals=["local-user"],
            memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v1",
            checkpoint_store=Text2SQLRuntimeCheckpointStore(checkpoint_path),
        )
        result = resumed.run("强烈岩爆案例有多少个", task_id=task_id)
        self.assertEqual(result["answer"]["rows"], [[6]])
        self.assertEqual(result["execution"]["llm_calls"], 6)
        self.assertNotIn("schema-grounding", resumed_client.called_roles)
        self.assertNotIn("sql-strategy", resumed_client.called_roles)
        self.assertEqual(resumed.checkpoint_store.inspect(task_id)["checkpoint_count"], 8)

        cached = Text2SQLAgenticEngine(
            client=NoCallClient(),
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            principals=["local-user"],
            memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v1",
            checkpoint_store=Text2SQLRuntimeCheckpointStore(checkpoint_path),
        ).run("强烈岩爆案例有多少个", task_id=task_id)
        self.assertEqual(
            cached,
            json.loads(json.dumps(result, ensure_ascii=False, default=str)),
        )

        drifted = Text2SQLAgenticEngine(
            client=NoCallClient(),
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            principals=["different-user"],
            memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v1",
            checkpoint_store=Text2SQLRuntimeCheckpointStore(checkpoint_path),
        )
        with self.assertRaises(Text2SQLCheckpointIdentityError):
            drifted.run("强烈岩爆案例有多少个", task_id=task_id)

        version_drifted = Text2SQLAgenticEngine(
            client=NoCallClient(),
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            principals=["local-user"],
            memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v2",
            checkpoint_store=Text2SQLRuntimeCheckpointStore(checkpoint_path),
        )
        with self.assertRaises(Text2SQLCheckpointIdentityError):
            version_drifted.run("强烈岩爆案例有多少个", task_id=task_id)


if __name__ == "__main__":
    unittest.main()
