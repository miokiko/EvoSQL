import unittest

from evoagent.text2sql.query_outcome import defer_routing_clarification, parse_clarification


class RoutingClarificationTests(unittest.TestCase):
    def test_unknown_physical_mapping_reaches_evidence(self):
        request = parse_clarification({"clarification": {
            "reason_code": "missing_business_definition",
            "questions": ["事件对应哪张表？"],
            "missing_concepts": ["累计事件数量"],
        }}, "text2sql-lead", "routing")
        self.assertTrue(defer_routing_clarification(request, {}))

    def test_genuinely_incomplete_intent_can_still_ask(self):
        self.assertFalse(defer_routing_clarification({"reason_code": "ambiguous_intent"}, {}))
        self.assertFalse(defer_routing_clarification({}, {}))

    def test_authenticated_supplement_reaches_planning(self):
        self.assertTrue(defer_routing_clarification({"reason_code": "ambiguous_intent"},
            {"clarification_continuation": {"task_id": "parent", "answer": "按项目统计"}}))
