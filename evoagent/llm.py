"""Small OpenAI-compatible JSON client with auditable usage accounting."""
import json
import http.client
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .telemetry import ExecutionLedger


class JsonChatClient:
    def __init__(
        self, base_url: str, api_key: str, model: str,
        provider: str = "openai-compatible", timeout: int = 60,
        extra_headers: Optional[Dict[str, str]] = None,
        max_attempts: int = 3, retry_backoff_seconds: float = 0.5,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.provider = provider
        self.timeout = timeout
        self.extra_headers = dict(extra_headers or {})
        self.max_attempts = max(1, min(int(max_attempts), 5))
        self.retry_backoff_seconds = max(0.0, min(float(retry_backoff_seconds), 5.0))

    def complete_json(
        self, role: str, system: str, user: str,
        ledger: Optional[ExecutionLedger] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        if self.provider == "aliyun-dashscope":
            # Qwen 3.5+ defaults to thinking mode. EvoAgent's bounded tool
            # protocol needs the response budget reserved for strict JSON.
            payload["enable_thinking"] = False
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        headers.update(self.extra_headers)
        encoded_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        message = "%s JSON request failed" % self.provider
        for attempt in range(1, self.max_attempts + 1):
            request = urllib.request.Request(
                self.base_url + "/chat/completions",
                data=encoded_payload,
                headers=headers, method="POST",
            )
            started = time.monotonic()
            retryable = True
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                content = body["choices"][0]["message"]["content"]
                result = json.loads(content)
                if not isinstance(result, dict):
                    raise ValueError("model JSON root is not an object")
                if ledger:
                    ledger.record_model(
                        role, self.provider, self.model, body.get("usage") or {},
                        int((time.monotonic() - started) * 1000), True,
                    )
                return result
            except urllib.error.HTTPError as exc:
                detail = exc.read(1000).decode("utf-8", errors="replace")
                message = "%s API returned HTTP %d: %s" % (
                    self.provider, exc.code, detail,
                )
                retryable = exc.code in {408, 409, 425, 429} or exc.code >= 500
            except (urllib.error.URLError, http.client.RemoteDisconnected,
                    socket.timeout, ValueError, KeyError, IndexError, TypeError,
                    json.JSONDecodeError) as exc:
                message = "%s JSON request failed: %s" % (self.provider, exc)
            if ledger:
                ledger.record_model(
                    role, self.provider, self.model, {},
                    int((time.monotonic() - started) * 1000), False,
                    "%s (attempt %d/%d)" % (message, attempt, self.max_attempts),
                )
            if not retryable or attempt >= self.max_attempts:
                break
            time.sleep(self.retry_backoff_seconds * (2 ** (attempt - 1)))
        raise RuntimeError(message)
