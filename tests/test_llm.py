import json
import http.client
import unittest
import urllib.error
from unittest.mock import patch

from evoagent.llm import JsonChatClient


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(
            {
                "choices": [{"message": {"content": '{"status":"ok"}'}}],
                "usage": {},
            }
        ).encode("utf-8")


class JsonChatClientTests(unittest.TestCase):
    def _payload_for(self, provider):
        client = JsonChatClient(
            "https://example.invalid/v1",
            "test-key",
            "test-model",
            provider=provider,
        )
        with patch("evoagent.llm.urllib.request.urlopen", return_value=_Response()) as request:
            self.assertEqual(
                client.complete_json("worker", "Return JSON.", "Return status."),
                {"status": "ok"},
            )
        return json.loads(request.call_args.args[0].data.decode("utf-8"))

    def test_aliyun_disables_default_thinking_for_bounded_json_protocol(self):
        payload = self._payload_for("aliyun-dashscope")
        self.assertIs(payload["enable_thinking"], False)
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_other_openai_compatible_providers_are_unchanged(self):
        payload = self._payload_for("deepseek")
        self.assertNotIn("enable_thinking", payload)

    def test_transient_transport_failure_is_retried(self):
        client = JsonChatClient(
            "https://example.invalid/v1",
            "test-key",
            "test-model",
            max_attempts=3,
            retry_backoff_seconds=0,
        )
        with patch(
            "evoagent.llm.urllib.request.urlopen",
            side_effect=[urllib.error.URLError("connection reset"), _Response()],
        ) as request:
            self.assertEqual(
                client.complete_json("worker", "Return JSON.", "Return status."),
                {"status": "ok"},
            )
        self.assertEqual(request.call_count, 2)

    def test_remote_disconnect_is_retried(self):
        client = JsonChatClient(
            "https://example.invalid/v1",
            "test-key",
            "test-model",
            max_attempts=2,
            retry_backoff_seconds=0,
        )
        with patch(
            "evoagent.llm.urllib.request.urlopen",
            side_effect=[http.client.RemoteDisconnected(), _Response()],
        ) as request:
            self.assertEqual(
                client.complete_json("worker", "Return JSON.", "Return status."),
                {"status": "ok"},
            )
        self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
