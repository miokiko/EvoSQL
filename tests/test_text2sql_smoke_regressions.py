import copy
import json
import unittest
import test_text2sql_framework as framework
from evoagent.text2sql.agentic import Text2SQLAgenticEngine
from evoagent.text2sql.contracts import QuerySpec, SchemaPlan
from evoagent.text2sql.query_plan import bind_query_plan, check_plan_conformance
import test_text2sql_query_plan as plans

class SmokeProtocolRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        framework.FrameworkTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        framework.FrameworkTests.tearDownClass.__func__(cls)

    engine = framework.FrameworkTests.engine

    def test_qualified_question_authorizes_its_column_component_only(self):
        engine = self.engine(framework.ScriptedClient(framework.SQL))
        q = "将 t_caseinfo 与 t_stress 按 t_caseinfo.c_caseCode = t_stress.c_caseCode 连接"
        spec = QuerySpec.from_dict({"intent": "count", "subject": "t_caseinfo",
            "measures": [{"name": "案例数", "aggregation": "count",
                          "field_concept": "c_caseCode", "distinct": True}],
            "expected_shape": "scalar", "limit": 1})
        engine._validate_schema_blind_query_spec(spec, q)
        value = spec.as_dict()
        value["subject"] = "t_project.c_projectCode"
        with self.assertRaisesRegex(ValueError, "schema_leak"):
            engine._validate_schema_blind_query_spec(QuerySpec.from_dict(value), q)

    def test_generation_sees_bound_semantics_without_approval_prose(self):
        client = framework.ScriptedClient(framework.SQL)
        client.responses["text2sql-lead"][1]["reasoning_summary"] = "AUDIT_ONLY_UNBOUND_CONDITION"
        result = self.engine(client).run(framework.QUESTION)
        self.assertEqual(result["status"], "success")
        approved = result["collaboration"]["approved_query_plan"]
        context = client.calls_for("sql-generation")[0]["context"]
        view = context["approved_query_plan"]
        self.assertEqual(view["contract"], "ApprovedQueryPlanGenerationView/v1")
        self.assertNotIn("approval_reason", view)
        self.assertEqual(view["bound_plan"], json.loads(json.dumps(approved["bound_plan"])))
        self.assertEqual(view["approved_plan_fingerprint"], approved["fingerprint"])
        self.assertIn("AUDIT_ONLY_UNBOUND_CONDITION", approved["approval_reason"])

    def test_routing_has_no_schema_tools(self):
        result = self.engine(framework.ScriptedClient(framework.SQL)).run(framework.QUESTION)
        self.assertEqual(result["status"], "success")
        started = [x for x in result["execution"]["agent_traces"]["text2sql-lead"]
                   if x["event"] == "started"]
        self.assertEqual(started[0]["tools"], [])

    def test_critic_contract_repair_keeps_candidate_indices(self):
        client = framework.ScriptedClient(framework.SQL)
        valid = copy.deepcopy(client.responses["text2sql-critic"][0])
        invalid = copy.deepcopy(valid)
        invalid["decisions"].append({"candidate_index": 1, "accepted": False,
                                     "objections": ["not a real candidate"]})
        client.responses["text2sql-critic"] = [invalid, valid]
        result = self.engine(client).run(framework.QUESTION)
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(client.calls_for("text2sql-critic")), 2)

    def test_critic_repeated_invalid_output_remains_rejected(self):
        client = framework.ScriptedClient(framework.SQL)
        invalid = {"action": "final", "decisions": [
            {"candidate_index": 99, "accepted": True, "objections": []}]}
        client.responses["text2sql-critic"] = [copy.deepcopy(invalid), copy.deepcopy(invalid)]
        result = self.engine(client).run(framework.QUESTION)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["final_sql"], "")
        self.assertEqual(len(client.calls_for("text2sql-critic")), 2)

    def test_invalid_grounding_repairs_with_same_evidence(self):
        client = framework.ScriptedClient(framework.SQL)
        valid = copy.deepcopy(client.responses["schema-grounding"][0])
        invalid = copy.deepcopy(valid)
        invalid["schema_plan"]["tables"].append("t_activeinfo")
        invalid["schema_plan"]["columns"].append("t_activeinfo.d_sumEvent")
        client.responses["schema-grounding"] = [invalid, valid]
        result = self.engine(client).run(framework.QUESTION)
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(client.calls_for("schema-grounding")), 2)
        worker = next(x for x in result["collaboration"]["worker_results"]
                      if x["worker"] == "schema-grounding")
        self.assertNotIn("t_activeinfo", worker["output"]["schema_plan"]["tables"])

    def test_semantic_critic_rejection_is_not_retried(self):
        client = framework.ScriptedClient(framework.SQL)
        client.responses["text2sql-critic"][0]["decisions"] = [
            {"candidate_index": 0, "accepted": False, "objections": ["wrong business meaning"]}]
        result = self.engine(client).run(framework.QUESTION)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(len(client.calls_for("text2sql-critic")), 1)

class SmokePlanRegressionTests(unittest.TestCase):
    def test_row_counts_do_not_bind_to_filter_columns_or_join_keys(self):
        for question,concept in [
            ("当前事件数量（d_event）为 NULL 的记录有多少条？", "当前事件数量"),
            ("将表 t_caseinfo 与 t_stress 按 c_caseCode 内连接，连接后共有多少行？", "c_caseCode"),
        ]:
            raw = {"intent": "count", "subject": "记录", "expected_shape": "scalar", "limit": 1,
                   "measures": [{"name": "记录数", "aggregation": "count", "field_concept": concept, "distinct": False}]}
            spec = QuerySpec.from_dict(Text2SQLAgenticEngine._normalized_query_spec(raw, question))
            self.assertTrue(spec.measure_specs()[0].count_all)
            self.assertEqual(spec.measure_specs()[0].field_concept, "")
            self.assertFalse(spec.measure_specs()[0].distinct)

    def test_explicit_column_and_distinct_counts_are_preserved(self):
        raw = {"intent": "count", "subject": "记录", "expected_shape": "scalar", "limit": 1,
               "measures": [{"name": "字段计数", "aggregation": "count", "field_concept": "c_caseCode", "distinct": False}]}
        for question in ["计算 COUNT(c_caseCode)，返回记录数", "c_caseCode 非空值有多少个？"]:
            spec = QuerySpec.from_dict(Text2SQLAgenticEngine._normalized_query_spec(raw, question))
            self.assertFalse(spec.measure_specs()[0].count_all)
        raw["measures"][0]["distinct"] = True
        spec = QuerySpec.from_dict(Text2SQLAgenticEngine._normalized_query_spec(raw, "不同 c_caseCode 有多少条？"))
        self.assertFalse(spec.measure_specs()[0].count_all)
        self.assertTrue(spec.measure_specs()[0].distinct)

    def test_sql_generation_prompt_satisfies_json_provider_contract(self):
        from evoagent.text2sql.agentic import SQL_GENERATION_PROMPT
        self.assertIn("json", SQL_GENERATION_PROMPT.casefold())

    def test_unmodeled_null_sorting_is_rejected_before_approval(self):
        from evoagent.text2sql.contracts import QueryOrder
        self.assertEqual(QueryOrder.from_value({"target": "距离", "direction": "desc"}).direction, "desc")
        for key,value in [("nulls_last", True), ("nulls_first", False), ("nulls", "last")]:
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "SQLite default NULL ordering"):
                    QueryOrder.from_value({"target": "距离", "direction": "asc", key: value})

    def test_topk_rows_do_not_become_grouped_rows(self):
        raw = {"intent": "ranking", "subject": "记录",
            "dimensions": ["编号", "距离"], "measures": [],
            "expected_shape": "grouped_rows", "limit": 5,
            "order_by": [{"target": "距离", "direction": "desc"}]}
        spec = Text2SQLAgenticEngine._normalized_query_spec(raw, "列出距离最高的5条记录，返回编号和距离")
        self.assertEqual(spec["expected_shape"], "rows")
        self.assertEqual(spec["dimensions"], raw["dimensions"])
        grouped = plans.grouped_query_spec().as_dict()
        value = Text2SQLAgenticEngine._normalized_query_spec(grouped, "按类别分组统计案例数，取前3名")
        self.assertEqual(value["expected_shape"], "grouped_rows")
        self.assertEqual(len(value["measures"]), 1)

    def test_scalar_null_limit_and_precision_are_preserved(self):
        raw = {"intent": "aggregate", "subject": "指标", "expected_shape": "scalar",
               "limit": None, "measures": [{"name": "最大值", "aggregation": "max",
                                          "field_concept": "案例ID"}]}
        normalized = Text2SQLAgenticEngine._normalized_query_spec(raw, "指标最大值，结果保留 六 位小数。")
        self.assertEqual(normalized["limit"], 1)
        self.assertEqual(QuerySpec.from_dict(normalized).measure_specs()[0].precision, 6)
        for invalid in [True, -1, 13, "6"]:
            broken = copy.deepcopy(normalized)
            broken["measures"][0]["precision"] = invalid
            with self.assertRaises(ValueError):
                QuerySpec.from_dict(broken).measure_specs()

    def bound(self, precision):
        measure = {"name": "最大值", "aggregation": "max", "field_concept": "案例ID"}
        if precision is not None:
            measure["precision"] = precision
        spec = QuerySpec.from_dict({"intent": "aggregate", "subject": "案例",
            "measures": [measure], "expected_shape": "scalar", "limit": 1})
        schema = SchemaPlan.from_dict({"tables": ["t_case"], "columns": ["t_case.c_id"],
            "joins": [], "result_grain": [], "evidence_ids": ["schema:case-id"],
            "bindings": [{"logical_name": "案例ID", "column": "t_case.c_id",
                          "evidence_ids": ["schema:case-id"]}]})
        return bind_query_plan(spec, schema)

    def test_precision_gate_accepts_only_pinned_outer_round(self):
        plan = self.bound(6)
        self.assertTrue(check_plan_conformance("SELECT ROUND(MAX(c_id), 6) FROM t_case", plan, plans.SNAPSHOT).accepted)
        for sql in ["SELECT MAX(c_id) FROM t_case",
                    "SELECT ROUND(MAX(c_id), 2) FROM t_case",
                    "SELECT MAX(ROUND(c_id, 6)) FROM t_case",
                    "SELECT ROUND(MIN(c_id), 6) FROM t_case"]:
            with self.subTest(sql=sql):
                self.assertFalse(check_plan_conformance(sql, plan, plans.SNAPSHOT).accepted)
        self.assertNotEqual(plan.fingerprint, self.bound(2).fingerprint)
        self.assertFalse(check_plan_conformance("SELECT ROUND(MAX(c_id), 6) FROM t_case", self.bound(None), plans.SNAPSHOT).accepted)

if __name__ == "__main__":
    unittest.main()
