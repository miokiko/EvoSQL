import http.client
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from evoagent.api import ApiHandler
from evoagent.application import ApplicationService
from evoagent.config import Settings


class Text2SQLApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings.from_env()
        self.settings = replace(
            settings, db_path=str(Path(self.directory.name) / "auth.sqlite3"),
            auth_required=True, auth_secret="test-only-signing-secret-32-characters",
            bootstrap_admin_username="owner", bootstrap_admin_password="test-password-1234",
        )
        self.service = ApplicationService(self.settings)
        handler = type("TestApiHandler", (ApiHandler,), {
            "service": self.service, "settings": self.settings,
            "log_message": lambda *_args: None,
        })
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.service.close()
        self.directory.cleanup()

    def request(self, path, body=None, token=""):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        connection.request("GET" if body is None else "POST", path,
                           None if body is None else json.dumps(body), headers)
        response = connection.getresponse()
        content = response.read().decode()
        status = response.status
        connection.close()
        return status, json.loads(content) if content.startswith("{") else content

    def login(self):
        status, payload = self.request("/v1/auth/login", {
            "username": "owner", "password": "test-password-1234",
        })
        self.assertEqual(status, 200)
        return payload["access_token"]

    def test_starts_text2sql_without_pr_store_or_queue(self):
        status, health = self.request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["runtime"], "plan-first-text2sql-v3")
        self.assertTrue(health["auth_required"])
        self.assertFalse(hasattr(self.service, "reviewer"))
        self.assertFalse(hasattr(self.service, "queue"))
        tables = {row[0] for row in self.service.store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertNotIn("tasks", tables)
        self.assertIn("users", tables)
        status, html = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("问答工作台", html)

    def test_retired_pr_and_knowledge_endpoints_are_unavailable(self):
        token = self.login()
        for path in ("/api/tasks", "/github/install", "/v1/evolution/status"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, token=token)[0], 404)
        for path in ("/v1/reviews", "/github/webhook", "/api/text2sql/knowledge/sync",
                     "/v1/text2sql/experiences/old/evaluation"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, {}, token)[0], 404)

    def test_query_requires_login_and_preserves_user_session(self):
        self.service.text2sql_web.query = Mock(return_value={"status": "success"})
        self.assertEqual(self.request("/api/text2sql/status")[0], 401)
        self.assertEqual(self.request("/v1/text2sql/query", {"question": "案例数"})[0], 403)
        self.service.text2sql_web.query.assert_not_called()
        token = self.login()
        status, result = self.request("/v1/text2sql/query", {
            "question": "案例数", "session_id": "session-1", "task_id": "query-1",
        }, token)
        self.assertEqual((status, result["status"]), (200, "success"))
        self.service.text2sql_web.query.assert_called_once_with(
            "案例数", principals=("owner", "default"), task_id="query-1", session_id="session-1",
            clarification_task_id="",
        )

    def test_query_errors_distinguish_new_task_from_retry(self):
        token = self.login()
        for message, expected_status, code, retryable in [
            ("query task_id was reused with a different user, session, question, principal, or runtime identity", 409, "query_identity_conflict", False),
            ("text2sql-lead returned an invalid action", 502, "query_response_contract_error", True),
        ]:
            self.service.text2sql_web.query = Mock(side_effect=ValueError(message))
            status, result = self.request("/v1/text2sql/query", {"question": "案例数"}, token)
            self.assertEqual(status, expected_status)
            self.assertEqual(result["code"], code)
            self.assertEqual(result["retryable"], retryable)
            self.assertNotIn(message, result["error"])

    def test_feedback_still_writes_audit_without_review_service(self):
        self.service.text2sql_web.feedback = Mock(return_value={"feedback": "correct"})
        status, _ = self.request("/v1/text2sql/queries/query-1/feedback", {
            "decision": "correct", "note": "已核对", "session_id": "session-1",
        }, self.login())
        self.assertEqual(status, 201)
        row = self.service.store.connection.execute(
            "SELECT actor,action,resource_id FROM audit_events"
        ).fetchone()
        self.assertEqual(tuple(row), ("owner", "text2sql.query.feedback", "query-1"))

    def test_confirmed_experiences_can_propose_one_policy_candidate(self):
        self.service.text2sql_web.propose_policy_from_experiences = Mock(
            return_value={
                "candidate_policy_version": "policy-candidate",
                "target_agent": "query-planning",
                "memory_ids": ["memory-1", "memory-2"],
            }
        )
        status, result = self.request(
            "/v1/text2sql/policies/from-experiences",
            {
                "memory_ids": ["memory-1", "memory-2"],
                "change_reason": "修复去重规划",
            },
            self.login(),
        )
        self.assertEqual(status, 201)
        self.assertEqual(result["candidate_policy_version"], "policy-candidate")
        self.service.text2sql_web.propose_policy_from_experiences.assert_called_once_with(
            ("memory-1", "memory-2"), "owner", "修复去重规划"
        )
        row = self.service.store.connection.execute(
            "SELECT actor,action,resource_id FROM audit_events"
        ).fetchone()
        self.assertEqual(
            tuple(row),
            (
                "owner",
                "text2sql.policy.propose_from_experiences",
                "policy-candidate",
            ),
        )

    def test_semantic_rule_endpoints_require_manage_and_use_authenticated_actor(self):
        web = self.service.text2sql_web
        web.generate_semantic_rule = Mock(return_value={"status": "candidate", "rule": {"rule_id": "semantic-rule-one"}})
        web.review_semantic_rule = Mock(return_value={"state": "confirmed"})
        web.propose_policy_from_rules = Mock(return_value={"candidate_policy_version": "policy-rule", "semantic_rule_ids": ["semantic-rule-one"], "memory_ids": ["memory-one"]})
        requests = [
            ("/v1/text2sql/semantic-rules/generate", {"memory_id": "memory-one", "actor": "spoofed"}),
            ("/v1/text2sql/semantic-rules/semantic-rule-one/review", {"decision": "confirm", "actor": "spoofed"}),
            ("/v1/text2sql/policies/from-rules", {"rule_ids": ["semantic-rule-one"], "actor": "spoofed"}),
        ]
        for path, body in requests:
            self.assertEqual(self.request(path, body)[0], 403)
        web.generate_semantic_rule.assert_not_called()
        web.review_semantic_rule.assert_not_called()
        web.propose_policy_from_rules.assert_not_called()
        token = self.login()
        for (path, body), expected in zip(requests, (201, 200, 201)):
            self.assertEqual(self.request(path, body, token)[0], expected)
        web.generate_semantic_rule.assert_called_once_with("memory-one", "owner")
        web.review_semantic_rule.assert_called_once_with("semantic-rule-one", "confirm", "owner", "")
        web.propose_policy_from_rules.assert_called_once_with(["semantic-rule-one"], "owner", "")
        rows = self.service.store.connection.execute("SELECT actor,action FROM audit_events").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["actor"] == "owner" for row in rows))

    def test_semantic_rule_skip_and_invalid_policy_selection(self):
        web = self.service.text2sql_web
        web.generate_semantic_rule = Mock(return_value={"status": "skipped", "reason": "missing evidence"})
        web.propose_policy_from_rules = Mock()
        token = self.login()
        status, result = self.request("/v1/text2sql/semantic-rules/generate", {"memory_id": "missing"}, token)
        self.assertEqual((status, result["status"]), (200, "skipped"))
        self.assertEqual(self.request("/v1/text2sql/policies/from-rules", {"rule_ids": "not-a-list"}, token)[0], 400)
        web.propose_policy_from_rules.assert_not_called()

    def test_auth_configuration_is_validated_on_startup(self):
        with self.assertRaisesRegex(ValueError, "32 bytes"):
            ApplicationService(replace(self.settings, auth_secret="short"))
