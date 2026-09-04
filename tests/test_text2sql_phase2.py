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
    BUILD_VERSION,
    CRITIC_PROMPT,
    GATE_IMPLEMENTATION_VERSION,
    QUERY_PLANNING_PROMPT,
    SCHEMA_PROMPT,
    SQL_GENERATION_PROMPT,
    STRATEGY_PROMPT,
    TEXT2SQL_OBSERVATION_TOKEN_BUDGET,
    TEXT2SQL_PROTOCOL,
    TEXT2SQL_RUNTIME_NODES,
    Text2SQLAgenticEngine,
    _contains_sql_program,
)
from evoagent.text2sql.checkpoint_store import (
    Text2SQLCheckpointIdentityError,
    Text2SQLRuntimeCheckpointStore,
)
from evoagent.text2sql.database_tools import Text2SQLToolSuite
from evoagent.text2sql.knowledge_store import KnowledgeStore
from evoagent.text2sql.query_plan import bind_query_plan
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
        self.calls = []
        self.plan_workers_active = 0
        self.max_parallel_plan_workers = 0
        schema_plan = {
            "tables": ["t_casedesc"],
            "columns": [
                "t_casedesc.c_caseCode",
                "t_casedesc.c_rockLevel",
            ],
            "joins": [],
            "result_grain": ["t_casedesc.c_caseCode"],
            "bindings": [
                {
                    "logical_name": "案例编号",
                    "column": "t_casedesc.c_caseCode",
                },
                {
                    "logical_name": "岩爆等级",
                    "column": "t_casedesc.c_rockLevel",
                    "value_bindings": [
                        {
                            "logical_value": "强烈",
                            "physical_value": "强烈",
                        }
                    ],
                },
            ],
        }
        query_spec = {
            "intent": "count",
            "subject": "强烈岩爆案例",
            "dimensions": [],
            "measures": [
                {
                    "slot_id": "measure:case_count",
                    "name": "案例数",
                    "aggregation": "count",
                    "field_concept": "案例编号",
                    "distinct": True,
                }
            ],
            "filters": [
                {
                    "slot_id": "filter:rock_level",
                    "field_concept": "岩爆等级",
                    "operator": "eq",
                    "value": "强烈",
                }
            ],
            "order_by": [],
            "limit": 20,
            "expected_shape": "scalar",
            "version": 1,
        }
        generation = {
            "action": "final",
            "sql_candidates": [{"sql": sql}],
            "generation_notes": ["Rendered only from ApprovedQueryPlan."],
        }
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
                            "assignment_id": "planning-1",
                            "worker": "query-planning",
                            "objective": "Produce a logical scalar count plan without SQL.",
                        },
                    ],
                    "risk_level": "normal",
                    "reasoning_summary": "Two independent views are required.",
                },
                {
                    "action": "final",
                    "approve_plan": True,
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
                    "schema_plan": schema_plan,
                    "grounding_notes": ["强烈 is an observed exact value."],
                },
            ],
            "query-planning": [
                {
                    "action": "final",
                    "query_spec": query_spec,
                    "planning_notes": ["No physical identifier or SQL was used."],
                },
            ],
            # A second response is available only for the single bounded repair
            # exercised by rejection tests. Successful runs consume the first.
            "sql-generation": [dict(generation), dict(generation)],
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
        envelope = json.loads(user)
        context = json.loads(envelope.get("task", "{}"))
        initial_plan_worker = False
        with self.lock:
            initial_plan_worker = role in {"schema-grounding", "query-planning"} and not any(
                item["role"] == role for item in self.calls
            )
            self.calls.append(
                {
                    "role": role,
                    "system": system,
                    "context": context,
                    "envelope": envelope,
                }
            )
            response = self.responses[role].pop(0)
            if initial_plan_worker:
                self.plan_workers_active += 1
                self.max_parallel_plan_workers = max(
                    self.max_parallel_plan_workers, self.plan_workers_active
                )
        if initial_plan_worker:
            time.sleep(0.03)
            with self.lock:
                self.plan_workers_active -= 1
        if ledger:
            ledger.record_model(
                role,
                self.provider,
                self.model,
                {"prompt_tokens": 1, "completion_tokens": 1},
                0,
            )
        return response

    def calls_for(self, role):
        return [item for item in self.calls if item["role"] == role]


class DirectGenerationClient(ScriptedClient):
    def __init__(self, sql):
        super().__init__(sql)
        self.responses["sql-generation"] = [self.responses["sql-generation"][0]]


class ResultQAClient:
    model = "scripted"
    provider = "test"

    def __init__(self):
        self.calls = []
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
                "answer_text": "999999",
                "requires_new_query": False,
                "reasoning_summary": "Untrusted Lead answer must be ignored.",
            },
        ]

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        self.assert_role = role
        self.calls.append(role)
        response = self.responses.pop(0)
        if ledger:
            ledger.record_model(role, self.provider, self.model, {}, 0)
        return response


class Text2SQLContractTests(unittest.TestCase):
    def test_role_prompts_require_json_and_plan_first_boundaries(self):
        self.assertIn("JSON", SCHEMA_PROMPT)
        self.assertIn("JSON", QUERY_PLANNING_PROMPT)
        self.assertEqual(STRATEGY_PROMPT, QUERY_PLANNING_PROMPT)
        self.assertIn("ApprovedQueryPlan", SQL_GENERATION_PROMPT)
        self.assertIn("Harness", SQL_GENERATION_PROMPT)
        self.assertIn("JSON", CRITIC_PROMPT)
        self.assertIn("Do not call tools", CRITIC_PROMPT)
        self.assertIn("Do not write SQL", QUERY_PLANNING_PROMPT)
        self.assertIn("Never\nguess a table name", SCHEMA_PROMPT)
        self.assertEqual(TEXT2SQL_OBSERVATION_TOKEN_BUDGET, 1600)
        self.assertEqual(TEXT2SQL_PROTOCOL, "plan-first-text2sql-v3")
        self.assertEqual(BUILD_VERSION, "text2sql-agentic-build-v3")
        self.assertEqual(
            GATE_IMPLEMENTATION_VERSION, "text2sql-harness-gates-v2"
        )
        self.assertEqual(len(TEXT2SQL_RUNTIME_NODES), 11)
        self.assertTrue(_contains_sql_program("SELECT 1"))
        self.assertTrue(_contains_sql_program("VALUES (1)"))
        self.assertEqual(
            TEXT2SQL_RUNTIME_NODES,
            (
                "text2sql-lead-routing",
                "text2sql-evidence-orchestration",
                "text2sql-plan-workers",
                "text2sql-plan-binding",
                "text2sql-lead-plan-assessment",
                "text2sql-plan-revisions-approval",
                "text2sql-sql-generation",
                "text2sql-candidate-gates",
                "text2sql-critic",
                "text2sql-lead-final",
                "text2sql-final-gates-execute",
            ),
        )

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

    def _bind_plans(self, question, schema_plan, query_spec):
        engine = Text2SQLAgenticEngine(
            client=ScriptedClient("SELECT 1"),
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            principals=["local-user"],
            memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v1",
        )
        return engine._bind_worker_plans(
            [
                {
                    "worker": "schema-grounding",
                    "status": "completed",
                    "output": {"schema_plan": schema_plan},
                },
                {
                    "worker": "query-planning",
                    "status": "completed",
                    "output": {"query_spec": query_spec},
                },
            ],
            question,
        )

    def _successful_parent_snapshot(
        self,
        engine,
        *,
        task_id="previous-run",
        user_id="local-user",
        session_id="default",
    ):
        fixture = ScriptedClient(
            "SELECT COUNT(DISTINCT c_caseCode) AS n "
            "FROM t_casedesc WHERE c_rockLevel='强烈'"
        )
        query_spec = json.loads(
            json.dumps(
                fixture.responses["query-planning"][0]["query_spec"],
                ensure_ascii=False,
            )
        )
        schema_plan = json.loads(
            json.dumps(
                fixture.responses["schema-grounding"][0]["schema_plan"],
                ensure_ascii=False,
            )
        )
        bound = bind_query_plan(
            query_spec,
            schema_plan,
            version_pins=engine.version_pins,
        )
        return {
            "task_id": task_id,
            "user_id": user_id,
            "session_id": session_id,
            "status": "success",
            "version_pins": engine.version_pins,
            "gates": {
                "accepted": True,
                "bound_plan_fingerprint": bound.fingerprint,
            },
            "query_spec": query_spec,
            "schema_plan": schema_plan,
            "answer": {"columns": ["n"], "row_count": 1, "truncated": False},
            "rows": [[6]],
        }

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
        lead_tools = suite.registry("text2sql-lead")
        self.assertNotIn("execute_sql", lead_tools.names())
        with self.assertRaises(ToolProtocolError):
            lead_tools.invoke("execute_sql", {"sql": "SELECT 1"})
        harness = suite.registry("text2sql-harness")
        self.assertIn("execute_sql", harness.names())
        executed = harness.invoke(
            "execute_sql",
            {
                "sql": "SELECT COUNT(DISTINCT c_caseCode) AS n "
                "FROM t_casedesc WHERE c_rockLevel='强烈'"
            },
        )
        self.assertEqual(executed["output"]["rows"], [[6]])

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

    def test_plan_first_protocol_runs_all_eleven_nodes_before_harness_execution(self):
        sql = (
            "SELECT COUNT(DISTINCT c_caseCode) AS n "
            "FROM t_casedesc WHERE c_rockLevel='强烈'"
        )
        client = ScriptedClient(sql)
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
        self.assertEqual(
            result["collaboration"]["protocol"], "plan-first-text2sql-v3"
        )
        self.assertEqual(
            {item["worker"] for item in result["collaboration"]["worker_results"]},
            {"schema-grounding", "query-planning"},
        )
        self.assertEqual(client.max_parallel_plan_workers, 2)
        self.assertEqual(result["execution"]["llm_calls"], 7)
        self.assertEqual(
            result["collaboration"]["approved_query_plan"]["contract"],
            "ApprovedQueryPlan/v1",
        )
        self.assertEqual(
            result["collaboration"]["sql_generation_result"]["status"],
            "completed",
        )
        tool_names = [item["tool"] for item in result["execution"]["tool_call_log"]]
        self.assertIn("validate_sql", tool_names)
        self.assertIn("explain_sql", tool_names)
        self.assertEqual(tool_names.count("execute_sql"), 1)

    def test_sql_generation_receives_an_approved_plan_and_never_runs_before_it(self):
        sql = (
            "SELECT COUNT(DISTINCT c_caseCode) AS n "
            "FROM t_casedesc WHERE c_rockLevel='强烈'"
        )
        client = ScriptedClient(sql)
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
        roles = [item["role"] for item in client.calls]
        generation_index = roles.index("sql-generation")
        self.assertGreater(generation_index, roles.index("text2sql-lead", 1))
        generation_context = client.calls[generation_index]["context"]
        approved = generation_context["approved_query_plan"]
        self.assertEqual(approved["contract"], "ApprovedQueryPlan/v1")
        self.assertEqual(approved["bound_plan"]["contract"], "BoundQueryPlan/v1")
        self.assertTrue(approved["bound_plan"]["fingerprint"])

    def test_query_planning_receives_no_ddl_draft_link_pack_or_sql(self):
        sql = (
            "SELECT COUNT(DISTINCT c_caseCode) AS n "
            "FROM t_casedesc WHERE c_rockLevel='强烈'"
        )
        client = ScriptedClient(sql)
        next(
            item
            for item in client.responses["text2sql-lead"][0]["delegations"]
            if item["worker"] == "query-planning"
        )["objective"] = (
            "Use t_casedesc.c_rockLevel and SELECT c_caseCode FROM t_casedesc."
        )
        engine = Text2SQLAgenticEngine(
            client=client,
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            principals=["local-user"],
            memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v1",
            stable_memory_provider=lambda skill, _limit: (
                [
                    {
                        "memory_id": "memory-t_casedesc-planning-leak",
                        "failure_kind": (
                            "aggregation_grain_mismatch: SELECT 1 FROM "
                            "t_casedesc.c_rockLevel"
                        ),
                        "content": (
                            "统计时执行 SELECT c_caseCode FROM t_casedesc，"
                            "并使用 t_casedesc.c_rockLevel。"
                        ),
                    }
                ]
                if skill == "query-planning"
                else []
            ),
        )
        self.assertEqual(engine.run("强烈岩爆案例有多少个")["status"], "success")
        planning_call = client.calls_for("query-planning")[0]
        context = planning_call["context"]
        self.assertEqual(context["draft_link_pack"], {})
        self.assertEqual(planning_call["envelope"]["available_tools"], [])
        self.assertTrue(
            all(
                item.get("knowledge_type") == "business_glossary"
                for item in context["stable_retrieval_pack"].get("evidence", [])
            )
        )
        rendered = json.dumps(context, ensure_ascii=False).casefold()
        self.assertNotIn("full_ddl", rendered)
        self.assertNotIn("draft_sql", rendered)
        self.assertNotIn("sql_candidates", rendered)
        self.assertNotIn("create table", rendered)
        self.assertNotIn("select ", rendered)
        self.assertNotIn("t_casedesc", rendered)
        self.assertNotIn("c_rocklevel", rendered)
        rendered_system = planning_call["system"].casefold()
        self.assertNotIn("select 1", rendered_system)
        self.assertNotIn("values (1)", rendered_system)
        self.assertEqual(context["reviewed_policy_context"]["field_aliases"], {})
        self.assertEqual(context["reviewed_policy_context"]["value_aliases"], {})
        self.assertEqual(context["reviewed_policy_context"]["few_shot_examples"], [])

    def test_missing_or_duplicate_critic_decisions_fail_closed(self):
        sql = (
            "SELECT COUNT(DISTINCT c_caseCode) AS n "
            "FROM t_casedesc WHERE c_rockLevel='强烈'"
        )
        malformed_reviews = (
            {"action": "final", "decisions": [], "summary": "missing"},
            {
                "action": "final",
                "decisions": [
                    {"candidate_index": 0, "accepted": True},
                    {"candidate_index": 0, "accepted": True},
                ],
                "summary": "duplicate",
            },
            {
                "action": "final",
                "decisions": [
                    {
                        "candidate_index": False,
                        "accepted": True,
                        "objections": [],
                    }
                ],
                "summary": "boolean index",
            },
            {
                "action": "final",
                "decisions": [
                    {
                        "candidate_index": "0",
                        "accepted": True,
                        "objections": [],
                    }
                ],
                "summary": "string index",
            },
            {
                "action": "final",
                "decisions": [
                    {
                        "candidate_index": 0,
                        "accepted": True,
                        "objections": ["unresolved semantic objection"],
                    }
                ],
                "summary": "accepted with objections",
            },
        )
        for review in malformed_reviews:
            with self.subTest(summary=review["summary"]):
                client = ScriptedClient(sql)
                client.responses["text2sql-critic"] = [review]
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
                self.assertEqual(result["status"], "rejected")
                critic = result["collaboration"]["critic_result"]
                self.assertIn("invalid_critic_contract", critic["runtime_error"])
                if review["summary"] in {"boolean index", "string index"}:
                    self.assertIn("invalid_candidate_index", critic["runtime_error"])
                if review["summary"] == "accepted with objections":
                    self.assertIn(
                        "accepted_candidate_has_objections",
                        critic["runtime_error"],
                    )
                self.assertEqual(
                    critic["decisions"][0]["objections"],
                    ["invalid_critic_contract"],
                )
                self.assertNotIn(
                    "execute_sql",
                    [item["tool"] for item in result["execution"]["tool_call_log"]],
                )

    def test_harness_always_validates_and_explains_direct_sql_candidates(self):
        sql = (
            "SELECT COUNT(DISTINCT c_caseCode) AS n "
            "FROM t_casedesc WHERE c_rockLevel='强烈'"
        )
        engine = Text2SQLAgenticEngine(
            client=DirectGenerationClient(sql),
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

    def test_harness_requires_strict_integer_leader_index(self):
        sql = (
            "SELECT COUNT(DISTINCT c_caseCode) AS n "
            "FROM t_casedesc WHERE c_rockLevel='强烈'"
        )
        for invalid_index in (False, "0", 9):
            with self.subTest(invalid_index=invalid_index):
                client = ScriptedClient(sql)
                client.responses["text2sql-lead"][-1][
                    "final_candidate_index"
                ] = invalid_index
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
                self.assertEqual(result["status"], "rejected")
                self.assertIn(
                    "invalid_final_candidate_index", result["gates"]["errors"]
                )
                self.assertNotIn(
                    "execute_sql",
                    [
                        item["tool"]
                        for item in result["execution"]["tool_call_log"]
                    ],
                )

    def test_checkpoint_identity_includes_build_and_gate_versions(self):
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
        identity = engine._checkpoint_identity("强烈岩爆案例有多少个", {})
        runtime = identity["runtime"]
        self.assertEqual(runtime["protocol"], "plan-first-text2sql-v3")
        self.assertEqual(runtime["build_version"], BUILD_VERSION)
        self.assertEqual(
            runtime["gate_implementation_version"], GATE_IMPLEMENTATION_VERSION
        )

        checkpoint_path = Path(self.temporary.name) / "identity-versions.sqlite3"
        store = Text2SQLRuntimeCheckpointStore(checkpoint_path)
        session = store.acquire("identity-version-boundary", identity)
        session.fail("release lease for identity drift checks", {})
        for constant, drifted_version in (
            ("BUILD_VERSION", "drifted-build"),
            ("GATE_IMPLEMENTATION_VERSION", "drifted-gates"),
        ):
            with self.subTest(constant=constant), patch(
                "evoagent.text2sql.agentic.%s" % constant, drifted_version
            ):
                drifted_identity = engine._checkpoint_identity(
                    "强烈岩爆案例有多少个", {}
                )
                with self.assertRaises(Text2SQLCheckpointIdentityError):
                    store.acquire("identity-version-boundary", drifted_identity)

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
        explicit_question = "按 t_harm.c_caseCode = t_support.c_caseCode 连接"
        provenance = engine._trusted_query_provenance(
            explicit_question, {"type": "DATA_QUERY"}
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
            explicit_question,
            (),
            provenance["user_explicit_joins"],
        )
        self.assertEqual(plan.joins[0].source, "user_explicit")
        self.assertEqual(plan.joins[0].evidence_id, "")
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
        boundary_attack = (
            "evil_t_harm.c_caseCode=t_support.c_caseCodex"
        )
        attack_provenance = engine._trusted_query_provenance(
            boundary_attack, {"type": "DATA_QUERY"}
        )
        self.assertEqual(attack_provenance["user_explicit_joins"], [])
        with self.assertRaises(ValueError):
            engine._validated_schema_plan(
                plan.as_dict(),
                boundary_attack,
                (),
                attack_provenance["user_explicit_joins"],
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
        client.responses["schema-grounding"].append(
            json.loads(
                json.dumps(client.responses["schema-grounding"][0], ensure_ascii=False)
            )
        )
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
        self.assertEqual(result["status"], "rejected")
        grounding = next(
            item
            for item in result["collaboration"]["worker_results"]
            if item["worker"] == "schema-grounding"
        )
        self.assertEqual(grounding["status"], "completed")
        self.assertIn(
            "t_casedesc.c_caseCode", grounding["output"]["schema_plan"]["columns"]
        )
        self.assertIn(
            "missing_schema_binding",
            {item["code"] for item in result["collaboration"]["binding_conflicts"]},
        )
        self.assertNotIn("sql-generation", [item["role"] for item in client.calls])

    def test_fabricated_value_binding_fails_after_one_revision_without_generation(self):
        sql = (
            "SELECT COUNT(DISTINCT c_caseCode) AS n "
            "FROM t_casedesc WHERE c_rockLevel='强烈'"
        )
        client = ScriptedClient(sql)
        malicious = json.loads(
            json.dumps(client.responses["schema-grounding"][0], ensure_ascii=False)
        )
        malicious["schema_plan"]["bindings"][1]["value_bindings"][0][
            "physical_value"
        ] = "数据库中不存在的等级"
        client.responses["schema-grounding"] = [
            malicious,
            json.loads(json.dumps(malicious, ensure_ascii=False)),
        ]
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
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["collaboration"]["revisions_applied"], 1)
        self.assertEqual(len(client.calls_for("schema-grounding")), 2)
        self.assertNotIn("sql-generation", [item["role"] for item in client.calls])
        tool_names = [item["tool"] for item in result["execution"]["tool_call_log"]]
        self.assertNotIn("execute_sql", tool_names)

    def test_range_boundaries_are_type_checked_without_requiring_existing_rows(self):
        result = self._bind_plans(
            "统计最大深度在 2.25 到 2.75 之间的记录数",
            {
                "tables": ["t_rockdesc"],
                "columns": ["t_rockdesc.d_maxDepth"],
                "joins": [],
                "result_grain": [],
                "bindings": [
                    {
                        "logical_name": "最大深度",
                        "column": "t_rockdesc.d_maxDepth",
                        "value_bindings": [
                            {"logical_value": 2.25, "physical_value": 2.25},
                            {"logical_value": 2.75, "physical_value": 2.75},
                        ],
                    }
                ],
            },
            {
                "intent": "count",
                "subject": "最大深度范围内的记录",
                "measures": [
                    {
                        "slot_id": "measure:rows",
                        "name": "记录数",
                        "aggregation": "count",
                        "count_all": True,
                        "distinct": False,
                    }
                ],
                "filters": [
                    {
                        "slot_id": "filter:depth",
                        "field_concept": "最大深度",
                        "operator": "between",
                        "value": [2.25, 2.75],
                    }
                ],
                "expected_shape": "scalar",
            },
        )
        self.assertTrue(result["bound_query_plan"])
        self.assertEqual(result["binding_conflicts"], [])

    def test_like_pattern_can_be_derived_without_exact_database_membership(self):
        result = self._bind_plans(
            "统计处理过程包含支护的记录数",
            {
                "tables": ["t_casedesc"],
                "columns": ["t_casedesc.c_process"],
                "joins": [],
                "result_grain": [],
                "bindings": [
                    {
                        "logical_name": "处理过程",
                        "column": "t_casedesc.c_process",
                        "value_bindings": [
                            {
                                "logical_value": "支护",
                                "physical_value": "%支护%",
                            }
                        ],
                    }
                ],
            },
            {
                "intent": "count",
                "subject": "包含支护的记录",
                "measures": [
                    {
                        "slot_id": "measure:rows",
                        "name": "记录数",
                        "aggregation": "count",
                        "count_all": True,
                        "distinct": False,
                    }
                ],
                "filters": [
                    {
                        "slot_id": "filter:process",
                        "field_concept": "处理过程",
                        "operator": "like",
                        "value": "支护",
                    }
                ],
                "expected_shape": "scalar",
            },
        )
        self.assertTrue(result["bound_query_plan"])
        self.assertEqual(result["binding_conflicts"], [])

    def test_value_provenance_rejects_numeric_substrings_and_absent_equalities(self):
        base_spec = {
            "intent": "count",
            "subject": "按序号统计记录",
            "measures": [
                {
                    "slot_id": "measure:rows",
                    "name": "记录数",
                    "aggregation": "count",
                    "count_all": True,
                    "distinct": False,
                }
            ],
            "filters": [
                {
                    "slot_id": "filter:serial",
                    "field_concept": "序号",
                    "operator": "eq",
                    "value": 1,
                }
            ],
            "expected_shape": "scalar",
        }
        schema_plan = {
            "tables": ["t_casetimeinfo"],
            "columns": ["t_casetimeinfo.i_serialid"],
            "joins": [],
            "result_grain": [],
            "bindings": [
                {
                    "logical_name": "序号",
                    "column": "t_casetimeinfo.i_serialid",
                    "value_bindings": [
                        {"logical_value": 1, "physical_value": 1}
                    ],
                }
            ],
        }
        substring = self._bind_plans("统计序号10的记录数", schema_plan, base_spec)
        self.assertFalse(substring["bound_query_plan"])
        self.assertEqual(
            {item["code"] for item in substring["binding_conflicts"]},
            {"unverified_value_binding"},
        )

        absent_spec = json.loads(json.dumps(base_spec))
        absent_spec["filters"][0]["value"] = 999999
        absent_plan = json.loads(json.dumps(schema_plan))
        absent_plan["bindings"][0]["value_bindings"] = [
            {"logical_value": 999999, "physical_value": 999999}
        ]
        absent = self._bind_plans(
            "统计序号999999的记录数", absent_plan, absent_spec
        )
        self.assertFalse(absent["bound_query_plan"])
        self.assertEqual(
            {item["code"] for item in absent["binding_conflicts"]},
            {"unverified_value_binding"},
        )

    def test_cjk_value_provenance_rejects_embedded_or_derived_substrings(self):
        client = ScriptedClient("SELECT 1")
        schema_plan = client.responses["schema-grounding"][0]["schema_plan"]
        query_spec = client.responses["query-planning"][0]["query_spec"]
        for question in ("伪强烈值的案例有多少个", "半强烈阶段有多少案例", "超强烈度案例"):
            with self.subTest(question=question):
                result = self._bind_plans(question, schema_plan, query_spec)
                self.assertFalse(result["bound_query_plan"])
                self.assertIn(
                    "unverified_value_binding",
                    {item["code"] for item in result["binding_conflicts"]},
                )

    def test_grounding_reauthorizes_binding_and_join_evidence_through_acl(self):
        engine = Text2SQLAgenticEngine(
            client=ScriptedClient("SELECT 1"),
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            principals=["local-user"],
            memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v1",
        )
        with self.assertRaisesRegex(ValueError, "ACL-authorized observed evidence"):
            engine._validated_schema_plan(
                {
                    "tables": ["t_casedesc"],
                    "columns": ["t_casedesc.c_rockLevel"],
                    "bindings": [
                        {
                            "logical_name": "案例编号",
                            "column": "t_casedesc.c_rockLevel",
                            "evidence_ids": ["totally-fabricated"],
                        }
                    ],
                },
                "统计不同案例编号",
                ["totally-fabricated"],
            )

        with KnowledgeStore(self.knowledge) as store:
            row = store.connection.execute(
                "SELECT evidence_id,structured_json,acl_json FROM knowledge_items "
                "WHERE knowledge_type='relationship' LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(row)
            relation = json.loads(row["structured_json"])
            evidence_id = str(row["evidence_id"])
            original_acl = str(row["acl_json"])
            original_state = str(
                store.connection.execute(
                    "SELECT state FROM knowledge_items WHERE evidence_id=?",
                    (evidence_id,),
                ).fetchone()["state"]
            )
            with store.connection:
                store.connection.execute(
                    "UPDATE knowledge_items SET state='stable',acl_json=? WHERE evidence_id=?",
                    (json.dumps(["secret-only"]), evidence_id),
                )
            self.assertEqual(
                store.resolve_stable_evidence([evidence_id], ["local-user"]), ()
            )
        try:
            with self.assertRaisesRegex(ValueError, "current ACL"):
                engine._validated_schema_plan(
                    {
                        "tables": sorted(
                            {
                                relation["left"].split(".", 1)[0],
                                relation["right"].split(".", 1)[0],
                            }
                        ),
                        "columns": [relation["left"], relation["right"]],
                        "joins": [
                            {
                                "left": relation["left"],
                                "right": relation["right"],
                                "type": "inner",
                                "source": "stable",
                                "evidence_id": evidence_id,
                            }
                        ],
                    },
                    "查询关联记录",
                    [evidence_id],
                )
        finally:
            with KnowledgeStore(self.knowledge) as store:
                with store.connection:
                    store.connection.execute(
                        "UPDATE knowledge_items SET state=?,acl_json=? WHERE evidence_id=?",
                        (original_state, original_acl, evidence_id),
                    )

    def test_follow_up_requires_an_authenticated_parent_before_using_lead_rewrite(self):
        sql = (
            "SELECT COUNT(DISTINCT c_caseCode) AS n "
            "FROM t_casedesc WHERE c_rockLevel='强烈'"
        )
        for parent_state in ("empty", "rejected"):
            with self.subTest(parent_state=parent_state):
                client = ScriptedClient(sql)
                client.responses["text2sql-lead"][0]["route"] = {
                    "type": "FOLLOW_UP_QUERY",
                    "standalone_question": "强烈岩爆案例有多少个",
                    "parent_query_run_id": "previous-run",
                    "reason": "Rewrite the follow-up.",
                }
                parent = {}
                engine = Text2SQLAgenticEngine(
                    client=client,
                    database_path=self.database,
                    snapshot=SNAPSHOT,
                    knowledge_store_path=self.knowledge,
                    principals=["local-user"],
                    memory_snapshot_id="memory-empty-v1",
                    policy_version="policy-v1",
                    result_snapshot_provider=lambda task_id, value=parent: (
                        value if task_id == "previous-run" else {}
                    ),
                )
                if parent_state == "rejected":
                    parent.update(self._successful_parent_snapshot(engine))
                    parent["status"] = "rejected"
                result = engine.run(
                    "继续查一下那个值",
                    conversation_context={
                        "scope": {
                            "user_id": "local-user",
                            "session_id": "default",
                        },
                        "recent_query_runs": [
                            {"task_id": "previous-run", "status": parent_state}
                        ],
                    },
                )
                self.assertEqual(result["status"], "rejected")
                self.assertEqual(result["query_type"], "FOLLOW_UP_QUERY")
                self.assertEqual(result["standalone_question"], "继续查一下那个值")
                self.assertIn(
                    "unauthenticated_parent_query_run", result["gates"]["errors"]
                )
                self.assertEqual(result["collaboration"]["worker_results"], [])
                self.assertNotIn(
                    "sql-generation", [item["role"] for item in client.calls]
                )
                tool_names = [
                    item["tool"] for item in result["execution"]["tool_call_log"]
                ]
                self.assertNotIn("execute_sql", tool_names)

    def test_follow_up_can_inherit_typed_literals_from_authenticated_parent_plan(self):
        sql = (
            "SELECT COUNT(DISTINCT c_caseCode) AS n "
            "FROM t_casedesc WHERE c_rockLevel='强烈'"
        )
        client = ScriptedClient(sql)
        client.responses["text2sql-lead"][0]["route"] = {
            "type": "FOLLOW_UP_QUERY",
            "standalone_question": "强烈岩爆案例有多少个",
            "parent_query_run_id": "previous-run",
            "reason": "Carry forward the prior approved filter.",
        }
        parent = {}
        engine = Text2SQLAgenticEngine(
            client=client,
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            principals=["local-user"],
            memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v1",
            result_snapshot_provider=lambda task_id: parent if task_id == "previous-run" else {},
        )
        parent.update(self._successful_parent_snapshot(engine))
        result = engine.run(
            "继续按刚才的条件统计",
            conversation_context={
                "scope": {"user_id": "local-user", "session_id": "default"},
                "recent_query_runs": [
                    {"task_id": "previous-run", "status": "success"}
                ]
            },
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["answer"]["rows"], [[6]])

    def test_invalid_lead_revision_request_contract_fails_closed(self):
        sql = (
            "SELECT COUNT(DISTINCT c_caseCode) AS n "
            "FROM t_casedesc WHERE c_rockLevel='强烈'"
        )
        client = ScriptedClient(sql)
        client.responses["text2sql-lead"][1]["revision_requests"] = [
            {
                "assignment_id": "does-not-exist",
                "worker": "schema-grounding",
                "guidance": "Accept this unknown assignment.",
            }
        ]
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
        self.assertEqual(result["status"], "rejected")
        self.assertIn(
            "invalid_revision_request_contract",
            result["collaboration"]["plan_approval_errors"],
        )
        self.assertTrue(
            result["collaboration"]["revision_request_contract_errors"]
        )
        self.assertNotIn("sql-generation", [item["role"] for item in client.calls])

    def test_critic_receives_gate_results_reindexed_by_candidate_id(self):
        valid_sql = (
            "SELECT COUNT(DISTINCT c_caseCode) AS n "
            "FROM t_casedesc WHERE c_rockLevel='强烈'"
        )
        client = ScriptedClient(valid_sql)
        client.responses["sql-generation"][0]["sql_candidates"] = [
            {"sql": "DELETE FROM t_casedesc WHERE c_rockLevel='强烈'"},
            {"sql": valid_sql},
        ]
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
        context = client.calls_for("text2sql-critic")[0]["context"]
        candidate = context["candidates"][0]
        gate = context["candidate_gate_results"][0]
        self.assertEqual(candidate["candidate_index"], 0)
        self.assertEqual(gate["candidate_index"], 0)
        self.assertEqual(gate["generation_candidate_index"], 1)
        self.assertEqual(candidate["candidate_id"], gate["candidate_id"])
        self.assertEqual(candidate["sql"], valid_sql)

    def test_query_planning_synonyms_are_normalized_before_binding(self):
        normalized = Text2SQLAgenticEngine._normalized_query_spec(
            {
            "intent": "group",
            "subject": "",
            "expected_shape": "table",
            "limit": 7,
            },
            "统计表 t_casedesc 按 c_rockLevel 分组的记录数",
        )
        self.assertEqual(normalized["intent"], "count")
        self.assertEqual(normalized["expected_shape"], "rows")
        self.assertEqual(normalized["limit"], 7)

    def test_query_planning_numeric_contract_does_not_coerce_invalid_values(self):
        base = {
            "intent": "lookup",
            "subject": "案例",
            "expected_shape": "rows",
        }
        for field, value in (
            ("limit", True),
            ("limit", "7"),
            ("limit", 1.5),
            ("limit", 0),
            ("limit", -1),
            ("version", True),
            ("version", "1"),
            ("version", 1.5),
            ("version", 0),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    Text2SQLAgenticEngine._normalized_query_spec(
                        {**base, field: value},
                        "列出案例",
                    )

    def test_zero_candidates_pass_gates_triggers_exactly_one_generation_repair(self):
        sql = "DELETE FROM t_casedesc WHERE c_rockLevel='强烈'"
        client = ScriptedClient(sql)
        engine = Text2SQLAgenticEngine(
            client=client,
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
        self.assertEqual(result["collaboration"]["sql_generation_repairs"], 1)
        self.assertEqual(len(result["collaboration"]["candidate_gate_rounds"]), 2)
        self.assertEqual(len(client.calls_for("sql-generation")), 2)
        self.assertEqual(client.calls_for("text2sql-critic"), [])
        tool_names = [item["tool"] for item in result["execution"]["tool_call_log"]]
        self.assertNotIn("execute_sql", tool_names)

    def test_leader_routes_result_qa_without_spawning_sql_workers(self):
        parent = {}
        engine = Text2SQLAgenticEngine(
            client=ResultQAClient(),
            database_path=self.database,
            snapshot=SNAPSHOT,
            knowledge_store_path=self.knowledge,
            principals=["local-user"],
            memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v1",
            result_snapshot_provider=lambda task_id: (
                parent if task_id == "previous-run" else {}
            ),
        )
        parent.update(self._successful_parent_snapshot(engine))
        result = engine.run(
            "刚才的结果是多少？",
            conversation_context={
                "scope": {"user_id": "local-user", "session_id": "default"},
                "recent_query_runs": [
                    {"task_id": "previous-run", "status": "success"}
                ]
            },
        )
        self.assertEqual(result["query_type"], "RESULT_QA")
        self.assertEqual(result["answer"]["summary_text"], "6")
        self.assertEqual(result["answer"]["rows"], [[6]])
        self.assertNotIn("999999", json.dumps(result, ensure_ascii=False))
        self.assertEqual(result["collaboration"]["worker_results"], [])
        self.assertEqual(result["execution"]["tool_calls"], 0)

    def test_result_qa_transformation_requires_a_new_query(self):
        for question in (
            "刚才的结果再加一后是多少？",
            "刚才的结果乘 2 是多少？",
            "刚才的结果取平方后是多少？",
        ):
            with self.subTest(question=question):
                parent = {}
                client = ResultQAClient()
                engine = Text2SQLAgenticEngine(
                    client=client,
                    database_path=self.database,
                    snapshot=SNAPSHOT,
                    knowledge_store_path=self.knowledge,
                    principals=["local-user"],
                    memory_snapshot_id="memory-empty-v1",
                    policy_version="policy-v1",
                    result_snapshot_provider=lambda task_id, value=parent: (
                        value if task_id == "previous-run" else {}
                    ),
                )
                parent.update(self._successful_parent_snapshot(engine))
                result = engine.run(
                    question,
                    conversation_context={
                        "scope": {
                            "user_id": "local-user",
                            "session_id": "default",
                        },
                        "recent_query_runs": [
                            {"task_id": "previous-run", "status": "success"}
                        ],
                    },
                )
                self.assertEqual(result["query_type"], "RESULT_QA")
                self.assertEqual(result["status"], "needs_new_query")
                self.assertFalse(result["gates"]["accepted"])
                self.assertIn(
                    "result_qa_not_replay_only", result["gates"]["errors"]
                )
                self.assertEqual(result["final_sql"], "")
                self.assertNotIn("999999", json.dumps(result, ensure_ascii=False))
                self.assertNotIn("sql-generation", client.calls)

    def test_result_qa_replay_classifier_uses_complete_whitelisted_templates(self):
        for question in (
            "请再展示一下上一轮的查询结果。",
            "重复刚才的结果",
        ):
            with self.subTest(question=question):
                self.assertTrue(
                    Text2SQLAgenticEngine._is_replay_only_result_question(question)
                )
        for question in (
            "刚才的结果再加一后是多少？",
            "刚才的结果乘 2 是多少？",
            "刚才的结果取平方后是多少？",
        ):
            with self.subTest(question=question):
                self.assertFalse(
                    Text2SQLAgenticEngine._is_replay_only_result_question(question)
                )

    def test_result_qa_rejects_unauthenticated_or_stale_parent_snapshots(self):
        mutations = {
            "wrong_task": lambda value: value.update(task_id="another-run"),
            "wrong_user": lambda value: value.update(user_id="another-user"),
            "wrong_session": lambda value: value.update(session_id="another-session"),
            "gate_not_accepted": lambda value: value["gates"].update(accepted=False),
            "stale_pins": lambda value: value["version_pins"].update(
                policy_version="policy-stale"
            ),
            "malformed_plan": lambda value: value["query_spec"].update(limit=True),
            "plan_fingerprint_mismatch": lambda value: value["gates"].update(
                bound_plan_fingerprint="0" * 64
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                parent = {}
                engine = Text2SQLAgenticEngine(
                    client=ResultQAClient(),
                    database_path=self.database,
                    snapshot=SNAPSHOT,
                    knowledge_store_path=self.knowledge,
                    principals=["local-user"],
                    memory_snapshot_id="memory-empty-v1",
                    policy_version="policy-v1",
                    result_snapshot_provider=lambda task_id, value=parent: (
                        value if task_id == "previous-run" else {}
                    ),
                )
                parent.update(self._successful_parent_snapshot(engine))
                mutate(parent)
                result = engine.run(
                    "刚才的结果是多少？",
                    conversation_context={
                        "scope": {
                            "user_id": "local-user",
                            "session_id": "default",
                        },
                        "recent_query_runs": [
                            {"task_id": "previous-run", "status": "success"}
                        ],
                    },
                )
                self.assertEqual(result["status"], "needs_new_query")
                self.assertFalse(result["gates"]["accepted"])
                self.assertIn("cached_result_insufficient", result["gates"]["errors"])


if __name__ == "__main__":
    unittest.main()
