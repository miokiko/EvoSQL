import json
import unittest

from evoagent.context_manager import ContextManager, estimate_tokens






class ContextManagerTests(unittest.TestCase):

    def test_old_observations_are_summarized_and_evidence_is_retained(self):
        manager = ContextManager(
            observation_token_budget=350, recent_observations=1,
        )
        observations = [
            {
                "step": index, "tool": "read_file", "ok": True,
                "result": {
                    "evidence_id": "read_file:%d" % index,
                    "tool": "read_file",
                    "output": {"path": "app.py", "content": "x" * 1600},
                },
            }
            for index in range(1, 5)
        ]

        compact, stats = manager.compact_observations(observations, 350)

        self.assertGreater(stats["summarized"], 0)
        rendered = json.dumps(compact)
        self.assertIn("read_file:4", rendered)
        self.assertLess(estimate_tokens(compact), 500)

    def test_managed_context_is_trimmed_before_model_call(self):
        manager = ContextManager(
            context_window_tokens=3000, input_token_budget=1200,
            observation_token_budget=300, recent_observations=1,
        )
        task = json.dumps({
            "phase": "critic", "reasoning_summary": "a" * 12000,
            "candidates": [
                {"candidate_index": index, "explanation": "b" * 1200}
                for index in range(8)
            ],
        })

        managed, stats = manager.build_managed_context(
            task, [], [], 4000, 60, system_prompt="Review Text2SQL candidates", max_output_tokens=512,
        )

        self.assertLessEqual(stats["estimated_input_tokens_after"], stats["input_token_limit"])
        compact_task = json.loads(managed["task"])
        self.assertEqual(8, len(compact_task["candidates"]))
        self.assertEqual(7, compact_task["candidates"][7]["candidate_index"])

    def test_context_compaction_preserves_plan_sql_and_candidate_indices(self):
        sql = "SELECT c_caseCode FROM t_casedesc WHERE c_rockLevel='强烈'"
        plan = {"fingerprint": "approved-plan", "filters": [{"value": "强烈"}]}
        task = json.dumps({
            "approved_query_plan": plan,
            "reasoning_summary": "解释" * 4000,
            "candidates": [{"candidate_index": 0, "sql": sql}],
        }, ensure_ascii=False)
        reduced = json.loads(ContextManager._compact_task(task, 512))
        self.assertEqual(reduced["approved_query_plan"], plan)
        self.assertEqual(reduced["candidates"], [{"candidate_index": 0, "sql": sql}])

    def test_oversized_protected_plan_fails_before_model_call(self):
        task = json.dumps({"approved_query_plan": {"values": list(range(1000))}})
        with self.assertRaisesRegex(ValueError, "context budget"):
            ContextManager._minimal_task(task, 192)




if __name__ == "__main__":
    unittest.main()
