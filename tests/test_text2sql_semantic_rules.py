"""Rule MVP boundaries and three source-case compile/replay integration checks."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from evoagent.text2sql.evolution import Text2SQLEvolutionStore
from evoagent.text2sql.memory_service import (
    build_query_trace,
    extract_sql_gate_repair_experiences,
    extract_user_correction_experiences,
)
from evoagent.text2sql.query_plan import bind_query_plan, approve_query_plan
from evoagent.text2sql.semantic_rules import (
    SemanticRuleGenerator, generate_semantic_rule, load_sql_repair_evidence,
    normalize_rule_content, propose_policy_from_rules,
)
from evoagent.text2sql.target_replay import build_replay_identity, run_target_replay
from evoagent.text2sql.web_service import Text2SQLWebService
from test_text2sql_query_plan import SNAPSHOT, VALID_SQL, grouped_query_spec, grounded_schema_plan


CONTENT = {
    "root_cause": "审批计划中的要求未完整落实到首轮 SQL，修复后保持原计划并通过门禁。",
    "rule": "提交 SQL 前，逐项核对审批计划中的去重、过滤和排序要求，遗漏时先修正。",
    "applicability": ["已冻结的审批计划明确要求相应操作"],
    "exceptions": ["不得为未要求去重的指标统一增加 DISTINCT"],
    "evidence_refs": ["approved_query_plan", "before_sql", "after_sql", "before_gate"],
}


class ScriptedClient:
    provider = "scripted"
    model = "semantic-rule-integration"

    def __init__(self, response=None):
        self.response = response
        self.calls = []

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        request = json.loads(user)
        self.calls.append((role, request))
        if self.response is not None:
            return copy.deepcopy(self.response)
        if role == "text2sql-semantic-rule-extractor":
            if request["evidence"].get("contract") == "AgentSemanticEvidence/v1":
                return {
                    "decision": "candidate",
                    "rule": {
                        "root_cause": "该角色在明确条件下未执行已定义职责。",
                        "rule": "遇到同类条件时，先核对职责边界并完成对应检查。",
                        "applicability": ["存在同类且已确认的纠错证据"],
                        "exceptions": ["未验证其他例外；仅限上述适用范围"],
                        "evidence_refs": ["experience", "query_run", "role_artifacts"],
                    },
                }
            return {"decision": "candidate", "rule": copy.deepcopy(CONTENT)}
        ids = request["constraints"]["source_memory_ids"]
        rules = request["selected_confirmed_semantic_rules"]
        return {
            "clusters": [{"name": "approved plan translation", "memory_ids": ids,
                          "root_cause": rules[0]["root_cause"]}],
            "skill_patch": {"prompt_fragment": request["current_prompt_fragment"]
                            + "\nVerify every required plan clause before submitting SQL."},
            "rationale": "Compile reviewed conditional rules; preserve role and Harness.",
            "memory_ids": ids,
        }


def source_case(store, index=0, *, missing_sql=False, confirm=True):
    """Synthetic fixtures live only in the test's temporary control store."""
    variants = [
        (VALID_SQL.replace("COUNT(DISTINCT c.c_id)", "COUNT(c.c_id)"), "distinct_mismatch"),
        (VALID_SQL.replace("WHERE d.c_level = '强烈' ", ""), "filter_mismatch"),
        (VALID_SQL.replace("ORDER BY case_count DESC ", ""), "order_mismatch"),
    ]
    wrong, code = variants[index % len(variants)]
    pins = {"database_snapshot_id": SNAPSHOT["snapshot_id"], "wiki_index_version": "vanna-rule-fixture",
            "vanna_index_version": "vanna-rule-fixture", "memory_snapshot_id": store.memory_snapshot_id,
            "policy_version": store.active_policy_version}
    plan = approve_query_plan(bind_query_plan(grouped_query_spec(), grounded_schema_plan(), version_pins=pins),
                              approval_reason="fixture reviewed logical requirements")
    collaboration = {
        "approved_query_plan": plan.as_dict(), "sql_generation_repairs": 1,
        "sql_generation_initial": {"output": {"sql_candidates": [{"sql": wrong}]}},
        "candidate_gate_rounds": [
            {"round": 0, "accepted_candidates": [],
             "candidate_gate_results": [{"candidate_id": "before", "accepted": False,
                                         "errors": [code], "validation": {"normalized_sql": wrong}}],
             "gate_issues": [{"code": code}]},
            {"round": 1, "accepted_candidates": [{"candidate_id": "after", "sql": VALID_SQL,
                                                   "bound_plan_fingerprint": plan.bound_plan.fingerprint}],
             "candidate_gate_results": [{"candidate_id": "after", "accepted": True, "errors": []}],
             "gate_issues": []},
        ],
    }
    if missing_sql:
        collaboration.pop("sql_generation_initial")
    terminal = {"task_id": "rule-fixture-%d" % index, "status": "success", "query_type": "DATA_QUERY",
                "question": "按类别统计强烈案例数，按数量降序取前三类", "final_sql": VALID_SQL,
                "gates": {"accepted": True, "errors": []}, "version_pins": pins}
    trace = build_query_trace(terminal, {"collaboration": collaboration}, origin="cli", source_lane="stable")
    store.save_query_trace(trace)
    memory_id = store.add_experience_memory(extract_sql_gate_repair_experiences(trace)[0])
    if confirm:
        store.review_experience_memory(memory_id, "confirm", "fixture-reviewer")
    return memory_id, trace


def role_feedback_case(store, target_agent, index):
    wrong_sql = VALID_SQL.replace("ORDER BY case_count DESC", "ORDER BY case_count ASC")
    pins = {
        "database_snapshot_id": SNAPSHOT["snapshot_id"],
        "wiki_index_version": "vanna-role-fixture",
        "vanna_index_version": "vanna-role-fixture",
        "memory_snapshot_id": store.memory_snapshot_id,
        "policy_version": store.active_policy_version,
    }
    terminal = {
        "task_id": "role-feedback-%d" % index,
        "status": "success",
        "query_type": "DATA_QUERY",
        "question": "按类别统计案例数并按数量降序",
        "final_sql": wrong_sql,
        "gates": {"accepted": True, "errors": []},
        "version_pins": pins,
    }
    trace = build_query_trace(
        terminal, {"collaboration": {}}, origin="cli", source_lane="stable"
    )
    store.save_query_trace(trace)
    note = "人工确认该问题应由 %s 的职责规则修正" % target_agent
    extracted = extract_user_correction_experiences(
        trace,
        {
            "decision": "incorrect",
            "note": note,
            "corrected_sql": VALID_SQL,
            "target_agent": target_agent,
            "problem_code": "role_behavior_mismatch",
            "correction": "按该角色的既定职责完成检查，不改变其他角色边界。",
        },
        snapshot=SNAPSHOT,
    )
    memory_id = store.add_experience_memory(extracted[0])
    store.record_query_feedback(terminal["task_id"], "incorrect", note, "reviewer")
    store.review_experience_memory(memory_id, "confirm", "reviewer")
    return memory_id


class SemanticRuleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Text2SQLEvolutionStore(Path(self.directory.name) / "evolution.sqlite3", SNAPSHOT)
        self.client = ScriptedClient()

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def rule(self, index=0, confirm=False):
        memory_id, trace = source_case(self.store, index)
        result = generate_semantic_rule(self.store, self.client, memory_id, "fixture-author")
        self.assertEqual(result["status"], "candidate", result)
        rule = result["rule"]
        if confirm:
            rule = self.store.review_semantic_rule(rule["rule_id"], "confirm", "fixture-reviewer")
        return rule, memory_id, trace

    def test_three_complete_cases_receive_full_evidence_and_preserve_runtime(self):
        before = (self.store.memory_snapshot_id, self.store.active_policy_version)
        for i in range(3):
            with self.subTest(case=i):
                rule, memory_id, _ = self.rule(i, confirm=True)
                self.assertFalse(rule["runtime_eligible"])
                evidence = self.client.calls[-1][1]["evidence"]
                self.assertTrue(evidence["before_sql"])
                self.assertEqual(evidence["after_sql"], [VALID_SQL])
                self.assertIn("query_spec", evidence["approved_query_plan"]["bound_plan"])
                self.assertEqual(rule["source_memory_ids"], [memory_id])
                self.assertNotIn("result_rows", evidence)
        self.assertEqual(len(self.client.calls), 3)
        self.assertEqual(len(self.store.list_semantic_rules("confirmed")), 3)
        self.assertEqual(before, (self.store.memory_snapshot_id, self.store.active_policy_version))

    def test_all_five_agents_can_generate_and_compile_role_scoped_rules(self):
        agents = (
            "text2sql-lead",
            "schema-grounding",
            "query-planning",
            "sql-generation",
            "text2sql-critic",
        )
        for index, agent in enumerate(agents):
            with self.subTest(agent=agent):
                memory_id = role_feedback_case(self.store, agent, index)
                generated = generate_semantic_rule(
                    self.store, self.client, memory_id, "fixture-author"
                )
                self.assertEqual(generated["status"], "candidate")
                self.assertEqual(generated["rule"]["target_agent"], agent)
                rule = self.store.review_semantic_rule(
                    generated["rule"]["rule_id"], "confirm", "fixture-reviewer"
                )
                policy = propose_policy_from_rules(
                    self.store, self.client, [rule["rule_id"]], "fixture-author"
                )
                self.assertEqual(policy["target_agent"], agent)

    def test_missing_sql_skips_without_model_call_or_rule(self):
        memory_id, _ = source_case(self.store, missing_sql=True)
        result = generate_semantic_rule(self.store, self.client, memory_id, "author")
        self.assertEqual(result["status"], "skipped")
        self.assertIn("before_sql", result["reason"])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.store.list_semantic_rules(), [])

    def test_unconfirmed_and_wrong_agent_sources_do_not_call_model(self):
        memory_id, _ = source_case(self.store, confirm=False)
        self.assertEqual(generate_semantic_rule(self.store, self.client, memory_id, "author")["status"], "skipped")
        self.assertEqual(self.client.calls, [])
        self.store.review_experience_memory(memory_id, "confirm", "reviewer")
        with self.store.connection:
            row = self.store.get_memory(memory_id)
            body = dict(row["rule"])
            body["target_agent"] = "schema-grounding"
            self.store.connection.execute("UPDATE memory_items SET rule_json=? WHERE memory_id=?", (json.dumps(body), memory_id))
        self.assertEqual(generate_semantic_rule(self.store, self.client, memory_id, "author")["status"], "skipped")
        self.assertEqual(self.client.calls, [])

    def test_llm_may_skip_without_saving(self):
        memory_id, _ = source_case(self.store)
        client = ScriptedClient({"decision": "skip", "reason": "本次修复没有可泛化的新规则"})
        self.assertEqual(generate_semantic_rule(self.store, client, memory_id, "author")["status"], "skipped")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.store.list_semantic_rules(), [])

    def test_malformed_or_unsupported_rule_fields_are_rejected(self):
        for bad in ({**CONTENT, "target_agent": "text2sql-lead"},
                    {**CONTENT, "allowed_tools": ["run_sql"]},
                    {**CONTENT, "evidence_refs": ["invented_trace"]},
                    {**CONTENT, "exceptions": []},
                    {**CONTENT, "rule": "x" * 2001}):
            with self.subTest(fields=list(bad)):
                with self.assertRaises(ValueError):
                    normalize_rule_content(bad)

    def test_rule_is_idempotent_and_review_is_terminal(self):
        rule, memory_id, _ = self.rule()
        again = generate_semantic_rule(self.store, self.client, memory_id, "author")
        self.assertEqual(again["rule"]["rule_id"], rule["rule_id"])
        self.assertEqual(len(self.store.list_semantic_rules()), 1)
        with self.assertRaises(ValueError):
            self.store.review_semantic_rule(rule["rule_id"], "reject", "reviewer")
        self.store.review_semantic_rule(rule["rule_id"], "reject", "reviewer", "规则过于宽泛")
        with self.assertRaises(ValueError):
            self.store.review_semantic_rule(rule["rule_id"], "confirm", "reviewer")

    def test_unconfirmed_rules_cannot_compile(self):
        rule, _, _ = self.rule()
        before = len(self.client.calls)
        with self.assertRaises(ValueError):
            propose_policy_from_rules(self.store, self.client, [rule["rule_id"]], "author")
        self.assertEqual(len(self.client.calls), before)

    def test_tampered_content_is_rejected(self):
        rule, _, _ = self.rule()
        with self.store.connection:
            self.store.connection.execute("UPDATE semantic_rules SET rule_json='{}' WHERE rule_id=?", (rule["rule_id"],))
        with self.assertRaisesRegex(ValueError, "hash"):
            self.store.review_semantic_rule(rule["rule_id"], "confirm", "reviewer")

    def test_exact_source_revision_is_used_after_new_trace(self):
        rule, _, trace = self.rule()
        newer = copy.deepcopy(trace)
        newer["source_revision"] = 2
        newer["question"] = "a different later question"
        self.store.save_query_trace(newer)
        reviewed = self.store.review_semantic_rule(rule["rule_id"], "confirm", "reviewer")
        self.assertEqual(reviewed["source_revision"], 1)
        self.assertEqual(reviewed["evidence"]["question"], trace["question"])

    def test_policy_changes_prompt_only_and_keeps_rule_and_experience_lineage(self):
        rule, memory_id, _ = self.rule(confirm=True)
        parent = self.store.get_policy()
        result = propose_policy_from_rules(self.store, self.client, [rule["rule_id"]], "author")
        candidate = self.store.get_policy(result["candidate_policy_version"])
        parent_value, candidate_value = parent.as_dict(), candidate.as_dict()
        candidate_value["prompt_fragments"] = parent_value["prompt_fragments"]
        self.assertEqual(candidate_value, parent_value)
        request = self.client.calls[-1][1]
        self.assertEqual(request["selected_confirmed_semantic_rules"][0]["rule_id"], rule["rule_id"])
        self.assertEqual(result["memory_ids"], [memory_id])
        lineage = self.store.validate_experience_policy_lineage(candidate.version)
        self.assertEqual(lineage["semantic_rule_ids"], [rule["rule_id"]])
        self.assertEqual(self.store.active_policy_version, parent.version)

    def test_materialized_rule_lineage_and_source_coverage_cannot_be_dropped(self):
        rule, _, _ = self.rule(confirm=True)
        result = propose_policy_from_rules(self.store, self.client, [rule["rule_id"]], "author")
        version = result["candidate_policy_version"]
        metadata = self.store.policy_record(version)["proposal_metadata"]
        with self.assertRaisesRegex(ValueError, "every"):
            self.store.validate_semantic_rule_sources(metadata, [], "sql-generation")
        with self.store.connection:
            self.store.connection.execute("DELETE FROM policy_semantic_rule_sources WHERE policy_version=?", (version,))
        with self.assertRaisesRegex(ValueError, "materialized"):
            self.store.validate_experience_policy_lineage(version)

    def test_three_case_target_replay_records_without_activating_policy(self):
        sources = [self.rule(i, confirm=True) for i in range(3)]
        parent = self.store.active_policy_version
        result = propose_policy_from_rules(self.store, self.client, [r[0]["rule_id"] for r in sources], "author")
        candidate = result["candidate_policy_version"]
        pins = sources[0][2]["version_pins"]
        runtime = {"protocol": "plan-first-v3", "build_version": "rule-fixture", "gate_implementation_version": "fixture",
                   "nodes": ["fixture"], "plan_contracts": ["ApprovedQueryPlan/v1"], "max_candidates": 4,
                   "max_plan_revisions_per_worker": 1, "max_sql_repairs": 1, "token_budget": 4000,
                   "time_budget": 60, "max_rows": 200, "timeout_ms": 3000}
        identity = build_replay_identity(
            parent_version_pins={**pins, "policy_version": parent},
            candidate_version_pins={**pins, "policy_version": candidate},
            parent_runtime={**runtime, "policy_source_memory_ids": []},
            candidate_runtime={**runtime, "policy_source_memory_ids": result["memory_ids"]},
            model={"provider": "scripted", "model": "rule-fixture", "temperature": 0}, principals=("fixture",),
        )
        calls = []
        def runner(repairs):
            def run(question, task_id):
                calls.append((repairs, task_id))
                return {"status": "success", "final_sql": VALID_SQL, "gates": {"accepted": True, "errors": []},
                        "collaboration": {"sql_generation_repairs": repairs}}
            return run
        artifact = run_target_replay(
            [self.store.get_memory(mid) for mid in result["memory_ids"]],
            {mid: trace for _, mid, trace in sources}, runner(1), runner(0),
            parent_policy_version=parent, candidate_policy_version=candidate, replay_identity=identity,
        )
        self.assertEqual(artifact["status"], "passed")
        self.assertEqual(len(calls), 6)
        recorded = self.store.record_target_replay(candidate, artifact, created_by="fixture-reviewer")
        self.assertEqual(recorded["status"], "passed")
        self.assertEqual(self.store.active_policy_version, parent)
        self.assertEqual(self.store.policy_record(candidate)["status"], "candidate")

    def test_web_facade_uses_same_store_and_never_publishes_on_review(self):
        memory_id, _ = source_case(self.store)
        service = Text2SQLWebService.__new__(Text2SQLWebService)
        service.client = self.client
        service.llm_config = {"model": self.client.model, "provider": self.client.provider}
        service.settings = SimpleNamespace(agent_token_budget=4000)
        service.evolution_store_path = self.store.path
        service._snapshot = lambda: SNAPSHOT
        result = service.generate_semantic_rule(memory_id, "author")
        rule = service.review_semantic_rule(result["rule"]["rule_id"], "confirm", "reviewer")
        policy = service.propose_policy_from_rules([rule["rule_id"]], "author")
        self.assertEqual(policy["status"], "candidate")
        self.assertNotEqual(self.store.active_policy_version, policy["candidate_policy_version"])


if __name__ == "__main__":
    unittest.main()
