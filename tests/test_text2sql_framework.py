import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_text2sql_phase2 import ScriptedClient, SNAPSHOT, JOIN_CATALOG, PROJECT_ROOT
from test_text2sql_web_service import _settings
from corpus_fixtures import build_test_corpus
from evoagent.text2sql.agentic import Text2SQLAgenticEngine
from evoagent.text2sql.checkpoint_store import Text2SQLRuntimeCheckpointStore
from evoagent.text2sql.evolution import Text2SQLEvolutionStore
from evoagent.text2sql.evaluation import EvaluationCase, Text2SQLEvaluator, result_fingerprint
from evoagent.text2sql.shadow import compare_shadow_results
from evoagent.text2sql.query_outcome import (
    clarification_continuation, diagnose_result, parse_clarification,
)
from evoagent.text2sql.sqlite_database import build_sqlite_database
from evoagent.text2sql.web_service import Text2SQLWebService


SQL = "SELECT COUNT(DISTINCT c_caseCode) AS n FROM t_casedesc WHERE c_rockLevel='强烈'"
QUESTION = "强烈岩爆案例有多少个"
CLARIFICATION = {
    "action": "final",
    "clarification": {
        "reason_code": "ambiguous_intent",
        "questions": ["请问需要统计哪个岩爆等级的案例？"],
        "missing_concepts": ["岩爆等级"],
    },
}


class FrameworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.database = cls.root / "data.sqlite3"
        cls.vanna = cls.root / "vanna"
        build_sqlite_database(PROJECT_ROOT / "database/test1_full_20241118.sql", cls.database)
        cls.vanna_version = build_test_corpus(cls.vanna, SNAPSHOT, JOIN_CATALOG)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def engine(self, client, **options):
        return Text2SQLAgenticEngine(
            client=client, database_path=self.database, snapshot=SNAPSHOT,
            vanna_index_root=self.vanna, vanna_index_version=self.vanna_version,
            principals=("local-user",), memory_snapshot_id="memory-empty-v1",
            policy_version="policy-v1", **options,
        )

    def assert_no_sql(self, result):
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["final_sql"], "")
        self.assertFalse(result["gates"]["accepted"])
        self.assertEqual(result["gates"]["errors"], [])
        self.assertEqual(result["answer"]["rows"], [])
        tools = [item["tool"] for item in result["execution"]["tool_call_log"]]
        self.assertNotIn("execute_sql", tools)
        self.assertNotIn("explain_sql", tools)
        roles = [item["role"] for item in result["execution"]["model_call_log"]]
        self.assertNotIn("sql-generation", roles)
        self.assertNotIn("text2sql-critic", roles)
        self.assertEqual(result["diagnostic"]["category"], "intent_ambiguity")

    def test_single_candidate_saves_call_and_still_executes_through_harness(self):
        client = ScriptedClient(SQL)
        result = self.engine(client).run(QUESTION)
        self.assertEqual(result["answer"]["rows"], [[6]])
        self.assertEqual(result["execution"]["llm_calls"], 6)
        self.assertEqual(len(client.calls_for("text2sql-lead")), 2)
        self.assertEqual(result["collaboration"]["lead_final"]["selection_method"],
                         "deterministic_single_candidate")
        final = Text2SQLWebService._public_result(result, "one")["agents"][-1]
        self.assertEqual(final["role"], "text2sql-harness")
        self.assertEqual(final["status"], "completed")

    def test_one_of_two_critic_accepted_candidates_uses_its_actual_index(self):
        client = ScriptedClient(SQL)
        client.responses["sql-generation"][0]["sql_candidates"] = [
            {"sql": SQL}, {"sql": SQL.replace(" AS n ", " AS total ")},
        ]
        client.responses["text2sql-critic"][0]["decisions"] = [
            {"candidate_index": 0, "accepted": False, "objections": ["reject first"]},
            {"candidate_index": 1, "accepted": True, "objections": []},
        ]
        result = self.engine(client).run(QUESTION)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["collaboration"]["lead_final"]["final_candidate_index"], 1)
        self.assertIn("AS total", result["final_sql"])
        self.assertEqual(len(client.calls_for("text2sql-lead")), 2)

    def test_multiple_candidates_still_use_lead_selection(self):
        client = ScriptedClient(SQL)
        client.responses["sql-generation"][0]["sql_candidates"] = [
            {"sql": SQL}, {"sql": SQL.replace(" AS n ", " AS total ")},
        ]
        client.responses["text2sql-critic"][0]["decisions"] = [
            {"candidate_index": index, "accepted": True, "objections": []} for index in (0, 1)
        ]
        client.responses["text2sql-lead"][-1]["final_candidate_index"] = 1
        result = self.engine(client).run(QUESTION)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["execution"]["llm_calls"], 7)
        self.assertEqual(result["collaboration"]["lead_final"]["selection_method"],
                         "lead_multiple_candidates")

    def test_routing_clarification_is_cached_without_model_reexecution(self):
        client = ScriptedClient(SQL)
        client.responses["text2sql-lead"] = [copy.deepcopy(CLARIFICATION)]
        with tempfile.TemporaryDirectory() as directory:
            store = Text2SQLRuntimeCheckpointStore(Path(directory) / "runtime.sqlite3")
            engine = self.engine(client, checkpoint_store=store)
            result = engine.run("案例有多少个", task_id="clarify")
            self.assert_no_sql(result)
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(engine.run("案例有多少个", task_id="clarify"), result)
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(store.inspect("clarify")["status"], "completed")

    def test_missing_schema_mapping_is_resolved_after_routing(self):
        client = ScriptedClient(SQL)
        client.responses["text2sql-lead"][0] = {
            "action": "final", "clarification": {
                "reason_code": "missing_business_definition",
                "questions": ["岩爆案例对应哪张表？"],
                "missing_concepts": ["岩爆案例"],
            },
        }
        result = self.engine(client).run(QUESTION)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["answer"]["rows"], [[6]])
        self.assertEqual(result["query_type"], "DATA_QUERY")
        self.assertGreater(len(client.calls_for("schema-grounding")), 0)

    def test_each_planning_worker_can_request_clarification(self):
        for role in ("schema-grounding", "query-planning"):
            with self.subTest(role=role):
                client = ScriptedClient(SQL)
                client.responses[role] = [copy.deepcopy(CLARIFICATION)]
                result = self.engine(client).run(QUESTION)
                self.assert_no_sql(result)
                self.assertEqual(result["clarification"]["stage"], "planning_workers")
                self.assertEqual(len(client.calls_for("text2sql-lead")), 1)

    def test_lead_can_request_clarification_after_inspecting_plans(self):
        client = ScriptedClient(SQL)
        client.responses["text2sql-lead"][1] = copy.deepcopy(CLARIFICATION)
        result = self.engine(client).run(QUESTION)
        self.assert_no_sql(result)
        self.assertEqual(result["clarification"]["stage"], "plan_approval")
        self.assertFalse(result["collaboration"]["approved_query_plan"])

    def test_revised_worker_can_stop_for_clarification(self):
        client = ScriptedClient(SQL)
        client.responses["text2sql-lead"][1].update({
            "approve_plan": False,
            "revision_requests": [{"assignment_id": "planning-1", "worker": "query-planning",
                                   "guidance": "确认统计口径"}],
        })
        client.responses["query-planning"].append(copy.deepcopy(CLARIFICATION))
        result = self.engine(client).run(QUESTION)
        self.assert_no_sql(result)
        self.assertEqual(result["clarification"]["stage"], "plan_revisions")
        self.assertEqual(len(client.calls_for("query-planning")), 2)

    def test_malformed_clarification_cannot_fall_through_to_sql(self):
        client = ScriptedClient(SQL)
        client.responses["text2sql-lead"][0] = {"action": "final", "route": {"type": "CLARIFICATION"}}
        with self.assertRaisesRegex(ValueError, "invalid_clarification_contract"):
            self.engine(client).run(QUESTION)
        self.assertEqual(len(client.calls), 1)

    def test_web_clarification_round_trip_scope_and_retry_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = ScriptedClient(SQL)
            client.responses["text2sql-lead"].insert(0, copy.deepcopy(CLARIFICATION))
            service = Text2SQLWebService(
                _settings(), client=client, llm_config={"provider": "test", "model": "scripted"}, database_path=self.database,
                vanna_index_root=self.vanna, evolution_store_path=root / "evolution.sqlite3",
                checkpoint_store_path=root / "runtime.sqlite3",
            )
            with patch.object(service, "_runtime_vanna_pin", return_value=(self.vanna_version, True)):
                waiting = service.query("岩爆案例有多少个", principals=("user",), task_id="parent", session_id="s1")
                self.assertEqual(waiting["status"], "needs_clarification")
                history = next(item for item in service.traces()["traces"] if item["task_id"] == "parent")
                self.assertEqual(history["clarification"], waiting["clarification"])
                self.assertEqual(history["diagnostic"]["category"], "intent_ambiguity")
                for user, session, parent in (("other", "s1", "parent"), ("user", "s2", "parent"), ("user", "s1", "missing")):
                    with self.subTest(user=user, session=session, parent=parent):
                        with self.assertRaisesRegex(ValueError, "澄清请求"):
                            service.query("强烈", principals=(user,), session_id=session, clarification_task_id=parent)
                self.assertEqual(len(client.calls), 1)
                result = service.query("强烈", principals=("user",), task_id="reply", session_id="s1", clarification_task_id="parent")
                self.assertEqual(result["status"], "success")
                self.assertEqual(result["answer"]["rows"], [[6]])
                calls = len(client.calls)
                cached = service.query("强烈", principals=("user",), task_id="reply", session_id="s1", clarification_task_id="parent")
                self.assertEqual(cached, json.loads(json.dumps(result)))
                self.assertEqual(len(client.calls), calls)
                with self.assertRaisesRegex(ValueError, "task_id was reused"):
                    service.query("中等", principals=("user",), task_id="reply", session_id="s1", clarification_task_id="parent")
            with Text2SQLEvolutionStore(root / "evolution.sqlite3", SNAPSHOT) as store:
                trace = store.get_query_trace("reply")
                self.assertEqual(trace["collaboration"]["clarification_continuation"]["task_id"], "parent")
                self.assertIn("用户补充：强烈", trace["question"])

    def test_clarification_continuation_repairs_one_invalid_lead_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = ScriptedClient(SQL)
            # Replace the normal reply routing response with: one malformed
            # action followed by a repeated request for physical Schema.  The
            # protocol repair should recover the envelope and the continuation
            # guard should advance to the two planning workers.
            client.responses["text2sql-lead"].pop(0)
            client.responses["text2sql-lead"].insert(
                0, copy.deepcopy(CLARIFICATION)
            )
            client.responses["text2sql-lead"].insert(
                1,
                {
                    "action": "clarify",
                    "clarification": {
                        "reason_code": "missing_business_definition",
                        "questions": ["岩爆案例对应哪张数据库表？"],
                        "missing_concepts": ["物理表映射"],
                    },
                    "reasoning_summary": "The clarification is sufficient.",
                },
            )
            service = Text2SQLWebService(
                _settings(),
                client=client,
                llm_config={"provider": "test", "model": "scripted"},
                database_path=self.database,
                vanna_index_root=self.vanna,
                evolution_store_path=root / "evolution.sqlite3",
                checkpoint_store_path=root / "runtime.sqlite3",
            )
            with patch.object(
                service,
                "_runtime_vanna_pin",
                return_value=(self.vanna_version, True),
            ):
                waiting = service.query(
                    "岩爆案例有多少个",
                    principals=("user",),
                    task_id="parent-invalid-action",
                    session_id="s1",
                )
                self.assertEqual(waiting["status"], "needs_clarification")
                result = service.query(
                    "强烈",
                    principals=("user",),
                    task_id="reply-invalid-action",
                    session_id="s1",
                    clarification_task_id="parent-invalid-action",
                )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["answer"]["rows"], [[6]])
        self.assertEqual(result["query_type"], "DATA_QUERY")
        self.assertFalse(
            any(
                observation.get("tool") == "response_contract"
                for call in client.calls_for("text2sql-lead")
                for observation in call["envelope"].get("observations", [])
            )
        )

    def test_evaluation_counts_unanswered_clarification_without_marking_it_correct(self):
        client = ScriptedClient(SQL)
        client.responses["text2sql-lead"] = [copy.deepcopy(CLARIFICATION)]
        engine = self.engine(client)
        case = EvaluationCase(
            case_id="framework-clarification", question=QUESTION, gold_sql=SQL,
            gold_result_fingerprint=result_fingerprint(["n"], [[6]], False),
            gold_row_count=1, gold_column_count=1, sql_skeleton="count-filter",
            category="count", difficulty="easy", database_snapshot_id=SNAPSHOT["snapshot_id"],
            split="train", ordered=False, required_tables=("t_casedesc",),
            required_columns=("t_casedesc.c_caseCode", "t_casedesc.c_rockLevel"),
            required_relationships=(),
        )
        report = Text2SQLEvaluator(self.database, SNAPSHOT, engine.version_pins).evaluate([case], engine.run)
        self.assertEqual(report["overall"]["execution_accuracy"], 0)
        self.assertEqual(report["overall"]["clarification_count"], 1)
        self.assertEqual(report["outcomes"][0]["failure_kind"], "NEEDS_CLARIFICATION")
        self.assertEqual(report["overall"]["failure_stage_counts"], {"routing": 1})


class DiagnosticTests(unittest.TestCase):
    def test_shadow_compares_the_actual_clarification_request(self):
        stable = {"status": "needs_clarification", "clarification": CLARIFICATION["clarification"]}
        candidate = copy.deepcopy(stable)
        self.assertTrue(compare_shadow_results(stable, candidate)["result_equivalent"])
        candidate["clarification"]["questions"] = ["请选择统计时间范围？"]
        diff = compare_shadow_results(stable, candidate)
        self.assertFalse(diff["result_equivalent"])
        self.assertTrue(diff["review_required"])
        self.assertNotIn("统计时间", json.dumps(diff, ensure_ascii=False))

    def test_contract_rejects_invalid_questions_and_reason(self):
        for replacement in ({"questions": []}, {"questions": "question"},
                            {"reason_code": "approve_unsafe_sql"}, {"questions": ["x" * 501]}):
            request = {**CLARIFICATION["clarification"], **replacement}
            with self.subTest(replacement=replacement):
                with self.assertRaisesRegex(ValueError, "invalid_clarification_contract"):
                    parse_clarification({"clarification": request}, "lead", "routing")

    def test_earliest_binding_failure_wins_over_downstream_errors(self):
        result = {"status": "rejected", "gates": {"errors": ["no_accepted_sql_candidate"]},
                  "collaboration": {"binding_conflicts": [{"code": "duplicate_slot_id"}],
                                    "plan_approval_errors": ["missing_bound_query_plan"],
                                    "sql_generation_result": {"status": "failed", "error": "no plan"}}}
        diagnostic = diagnose_result(result)
        self.assertEqual(diagnostic["stage"], "plan_binding")
        self.assertEqual(diagnostic["category"], "contract_error")
        self.assertEqual(diagnostic["code"], "duplicate_slot_id")

    def test_diagnostics_distinguish_binding_gaps_and_unsupported_contracts(self):
        for code, category in (("missing_schema_binding", "evidence_gap"),
                               ("ambiguous_schema_binding", "binding_ambiguity"),
                               ("unsupported_query_contract", "unsupported_query")):
            with self.subTest(code=code):
                result = {"status": "rejected", "collaboration": {"binding_conflicts": [
                    {"code": code, "message": "requested feature is outside v1" if category == "unsupported_query" else ""}
                ]}}
                self.assertEqual(diagnose_result(result)["category"], category)

    def test_clarification_continuation_does_not_turn_assistant_text_into_user_evidence(self):
        request = parse_clarification(CLARIFICATION, "text2sql-lead", "routing")
        request["questions"] = ["是否查询中等等级？"]
        trace = {"task_id": "p", "user_id": "u", "session_id": "s", "status": "needs_clarification",
                 "question": "岩爆案例有多少个", "collaboration": {"clarification": request}}
        combined, context = clarification_continuation(trace, "p", "u", "s", "强烈")
        self.assertNotIn("中等", combined)
        self.assertIn("强烈", combined)
        self.assertEqual(context["questions"], request["questions"])
        trace["status"] = "success"
        with self.assertRaises(ValueError):
            clarification_continuation(trace, "p", "u", "s", "强烈")
