import json
import unittest

from evoagent.bounded_role import BoundedRole
from evoagent.runtime import ToolRegistry
from evoagent.telemetry import ExecutionLedger


class _ScriptedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.contexts = []

    def complete_json(
        self, role, system, user, ledger=None, max_tokens=None
    ):
        self.contexts.append(json.loads(user))
        return dict(self.responses.pop(0))


class BoundedRoleResponseContractTests(unittest.TestCase):
    def test_uppercase_final_action_is_canonical_for_the_caller(self):
        client = _ScriptedClient([{"action": " FINAL ", "route": {"type": "DATA_QUERY"}}])
        result = BoundedRole("text2sql-lead", "Return JSON.", client, 1000, 10).run(
            "{}", ToolRegistry(), ExecutionLedger("test"))
        self.assertEqual(result["action"], "final")

    def test_final_shaped_payload_normalizes_action_without_another_call(self):
        client = _ScriptedClient(
            [
                {
                    "action": "clarify",
                    "clarification": {"questions": ["..."]},
                }
            ]
        )
        role = BoundedRole(
            "text2sql-lead",
            "Return JSON.",
            client,
            token_budget=1000,
            time_budget=10,
            max_steps=3,
        )

        result = role.run("{}", ToolRegistry(), ExecutionLedger("test"))

        self.assertEqual(result["action"], "final")
        self.assertEqual(result["clarification"], {"questions": ["..."]})
        self.assertEqual(len(client.contexts), 1)

    def test_invalid_action_gets_one_bounded_contract_repair(self):
        client = _ScriptedClient(
            [
                {"action": "unexpected"},
                {"action": "final", "route": {"type": "DATA_QUERY"}},
            ]
        )
        role = BoundedRole(
            "text2sql-lead",
            "Return JSON.",
            client,
            token_budget=1000,
            time_budget=10,
            max_steps=3,
        )

        result = role.run("{}", ToolRegistry(), ExecutionLedger("test"))

        self.assertEqual(result["action"], "final")
        self.assertEqual(len(client.contexts), 2)
        repair = client.contexts[1]["observations"][-1]
        self.assertEqual(repair["tool"], "response_contract")
        self.assertFalse(repair["ok"])
        self.assertIn('action="final"', repair["error"])

    def test_second_invalid_action_fails_closed(self):
        client = _ScriptedClient(
            [
                {"action": "unexpected"},
                {"unexpected": True},
                {"action": "final"},
            ]
        )
        role = BoundedRole(
            "text2sql-lead",
            "Return JSON.",
            client,
            token_budget=1000,
            time_budget=10,
            max_steps=3,
        )

        with self.assertRaisesRegex(ValueError, "returned an invalid action"):
            role.run("{}", ToolRegistry(), ExecutionLedger("test"))
        self.assertEqual(len(client.contexts), 2)


if __name__ == "__main__":
    unittest.main()
