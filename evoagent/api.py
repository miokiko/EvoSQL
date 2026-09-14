"""HTTP interface for Text2SQL queries, feedback and governed memory."""
import json
import mimetypes
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from .config import Settings
from .auth import Principal
from .metrics import metrics
from .application import ApplicationService


TEXT2SQL_QUERY_FEEDBACK = re.compile(
    r"^/v1/text2sql/queries/([A-Za-z0-9_-]+)/feedback$"
)
TEXT2SQL_EXPERIENCE_CONFIRM = re.compile(
    r"^/v1/text2sql/experiences/([A-Za-z0-9_-]+)/confirm$"
)
TEXT2SQL_EXPERIENCE_FEEDBACK = re.compile(
    r"^/v1/text2sql/experiences/([A-Za-z0-9_-]+)/feedback$"
)
TEXT2SQL_MEMORY_REVIEW = re.compile(
    r"^/v1/text2sql/memories/([A-Za-z0-9_-]+)/review$"
)
TEXT2SQL_MEMORY_ACTION = re.compile(
    r"^/v1/text2sql/memories/([A-Za-z0-9_-]+)/(evaluation|activate|rollback)$"
)
WEB_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "web"))


class ApiHandler(BaseHTTPRequestHandler):
    service: ApplicationService
    settings: Settings
    server_version = "EvoSQL/0.4"

    def _text2sql_service(self):
        return self.service.text2sql_web

    def log_message(self, fmt: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()

    def _principal(self, permission: str = "read") -> Principal:
        if not self.settings.auth_required:
            return Principal(
                "local", "local-development", self.settings.default_tenant_id, "admin"
            )
        principal = self.service.auth.authenticate(self.headers.get("Authorization", ""))
        self.service.auth.require(principal, (permission,))
        return principal

    def _authenticate_or_send(self, permission: str = "read"):
        try:
            return self._principal(permission)
        except PermissionError as exc:
            self._send_json(401, {"error": str(exc)})
            return None

    def _send_json(self, status: int, value: Dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _send_text(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self._headers(status, content_type, len(body))
        self.wfile.write(body)

    def _serve_file(self, filename: str) -> None:
        path = os.path.abspath(os.path.join(WEB_ROOT, filename))
        if not path.startswith(WEB_ROOT + os.sep) and path != WEB_ROOT:
            self._send_json(404, {"error": "not found"})
            return
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self._headers(200, content_type, len(body))
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid Content-Length")
        limit = self.settings.max_request_bytes
        if length <= 0 or length > limit:
            raise ValueError("request body is empty or too large")
        return self.rfile.read(length)

    @staticmethod
    def _read_json(body: bytes) -> Dict[str, Any]:
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("request body must be valid UTF-8 JSON")
        if not isinstance(value, dict):
            raise ValueError("JSON root must be an object")
        return value

    def do_GET(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)
        if path == "/":
            self._serve_file("index.html")
            return
        if path == "/assets/app.css":
            self._serve_file("app.css")
            return
        if path == "/assets/theme-cyberpunk.css":
            self._serve_file("theme-cyberpunk.css")
            return
        if path == "/assets/app.js":
            self._serve_file("app.js")
            return
        if path == "/health":
            self._send_json(200, {"status": "ok", "runtime": "plan-first-text2sql-v3",
                                  "auth_required": self.settings.auth_required,
                                  "llm_provider": self.service.llm_config.get("provider", "local"),
                                  "llm_model": self.service.llm_config.get("model", "")})
            return
        principal = self._authenticate_or_send("read")
        if principal is None:
            return
        if path == "/metrics":
            self._send_text(200, metrics.prometheus(), "text/plain; version=0.0.4; charset=utf-8")
            return
        if path == "/api/text2sql/status":
            try:
                self._send_json(200, dict(self._text2sql_service().status()))
            except Exception as exc:
                self._send_json(
                    503,
                    {
                        "ready": False,
                        "error": "Text2SQL status is unavailable",
                        "detail": str(exc)[:500],
                    },
                )
            return
        if path == "/api/text2sql/skills":
            self._send_json(200, dict(self._text2sql_service().skills()))
            return
        if path == "/api/text2sql/traces":
            self._send_json(
                200,
                dict(
                    self._text2sql_service().traces(
                        int(query.get("limit", [20])[0])
                    )
                ),
            )
            return
        if path == "/api/text2sql/memory":
            self._send_json(
                200,
                dict(
                    self._text2sql_service().memory(
                        principal.username,
                        str(query.get("session_id", ["default"])[0]),
                        int(query.get("limit", [12])[0]),
                    )
                ),
            )
            return
        if path == "/api/text2sql/experiences":
            self._send_json(
                200,
                dict(
                    self._text2sql_service().experiences(
                        str(query.get("state", [""])[0]),
                        int(query.get("limit", [50])[0]),
                    )
                ),
            )
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)
        try:
            body = self._read_body()
            if path == "/v1/auth/login":
                if not self.settings.auth_required:
                    self._send_json(409, {"error": "authentication is disabled"})
                    return
                payload = self._read_json(body)
                try:
                    result = self.service.auth.login(
                        str(payload.get("username", "")), str(payload.get("password", "")),
                        str(payload.get("tenant_id", "")),
                    )
                except PermissionError as exc:
                    self._send_json(401, {"error": str(exc)})
                    return
                self._send_json(200, result)
                return
            if path == "/v1/text2sql/query":
                principal = self._principal("query")
                payload = self._read_json(body)
                try:
                    result = self._text2sql_service().query(
                        str(payload.get("question") or ""),
                        principals=(principal.username, principal.tenant_id),
                        task_id=str(payload.get("task_id") or ""),
                        session_id=str(payload.get("session_id") or "default"),
                        clarification_task_id=str(payload.get("clarification_task_id") or ""),
                    )
                except RuntimeError as exc:
                    self._send_json(503, {"error": str(exc)})
                    return
                except ValueError as exc:
                    message = str(exc)
                    if "task_id was reused" in message:
                        self._send_json(409, {
                            "code": "query_identity_conflict",
                            "error": "该任务的请求或运行版本已变化，请重新发起查询。",
                            "retryable": False,
                        })
                    elif "invalid action" in message or "final action" in message:
                        self._send_json(502, {
                            "code": "query_response_contract_error",
                            "error": "模型未返回有效的查询指令，请重试。",
                            "retryable": True,
                        })
                    else:
                        raise
                    return
                self._send_json(200, dict(result))
                return
            if path == "/v1/text2sql/skills/propose":
                principal = self._principal("manage")
                payload = self._read_json(body)
                result = self._text2sql_service().propose_skill(
                    str(payload.get("skill_name") or ""),
                    payload.get("patch") or {},
                    str(payload.get("change_reason") or ""),
                    principal.username,
                )
                self.service.store.audit(
                    principal.tenant_id,
                    principal.username,
                    "text2sql.skill.propose",
                    str(payload.get("skill_name") or ""),
                    {"candidate_policy_version": result["candidate_policy_version"]},
                )
                self._send_json(201, dict(result))
                return
            if path == "/v1/text2sql/semantic-rules/generate":
                principal = self._principal("manage")
                payload = self._read_json(body)
                result = self._text2sql_service().generate_semantic_rule(
                    str(payload.get("memory_id") or ""), principal.username,
                )
                self.service.store.audit(principal.tenant_id, principal.username,
                    "text2sql.semantic_rule.generate", str(payload.get("memory_id") or ""),
                    {"status": result["status"], "rule_id": (result.get("rule") or {}).get("rule_id", "")})
                self._send_json(200 if result["status"] == "skipped" else 201, dict(result))
                return
            rule_review = re.fullmatch(r"/v1/text2sql/semantic-rules/([A-Za-z0-9_-]+)/review", path)
            if rule_review:
                principal = self._principal("manage")
                payload = self._read_json(body)
                result = self._text2sql_service().review_semantic_rule(
                    rule_review.group(1), str(payload.get("decision") or ""),
                    principal.username, str(payload.get("review_note") or ""),
                )
                self.service.store.audit(principal.tenant_id, principal.username,
                    "text2sql.semantic_rule.review", rule_review.group(1), {"state": result["state"]})
                self._send_json(200, dict(result))
                return
            if path == "/v1/text2sql/policies/from-rules":
                principal = self._principal("manage")
                payload = self._read_json(body)
                rule_ids = payload.get("rule_ids")
                if not isinstance(rule_ids, list):
                    raise ValueError("rule_ids must be a list")
                result = self._text2sql_service().propose_policy_from_rules(
                    rule_ids, principal.username, str(payload.get("change_reason") or ""),
                )
                self.service.store.audit(principal.tenant_id, principal.username,
                    "text2sql.policy.propose_from_rules", result["candidate_policy_version"],
                    {"rule_ids": result["semantic_rule_ids"], "memory_ids": result["memory_ids"]})
                self._send_json(201, dict(result))
                return
            if path == "/v1/text2sql/policies/from-experiences":
                principal = self._principal("manage")
                payload = self._read_json(body)
                raw_memory_ids = payload.get("memory_ids") or []
                if not isinstance(raw_memory_ids, list):
                    raise ValueError("memory_ids must be a list")
                result = self._text2sql_service().propose_policy_from_experiences(
                    tuple(str(value) for value in raw_memory_ids),
                    principal.username,
                    str(payload.get("change_reason") or ""),
                )
                self.service.store.audit(
                    principal.tenant_id,
                    principal.username,
                    "text2sql.policy.propose_from_experiences",
                    str(result.get("candidate_policy_version") or ""),
                    {
                        "target_agent": str(result.get("target_agent") or ""),
                        "memory_ids": list(result.get("memory_ids") or ()),
                    },
                )
                self._send_json(201, dict(result))
                return
            feedback_match = TEXT2SQL_QUERY_FEEDBACK.match(path)
            if feedback_match:
                principal = self._principal("query")
                payload = self._read_json(body)
                result = self._text2sql_service().feedback(
                    feedback_match.group(1),
                    str(payload.get("decision") or ""),
                    str(payload.get("note") or ""),
                    str(payload.get("corrected_sql") or ""),
                    user_id=principal.username,
                    session_id=str(payload.get("session_id") or "default")[:200],
                )
                self.service.store.audit(
                    principal.tenant_id,
                    principal.username,
                    "text2sql.query.feedback",
                    feedback_match.group(1),
                    {
                        "decision": result["feedback"],
                        "has_comment": bool(str(payload.get("note") or "").strip()),
                    },
                )
                self._send_json(201, dict(result))
                return
            experience_confirm_match = TEXT2SQL_EXPERIENCE_CONFIRM.match(path)
            if experience_confirm_match:
                principal = self._principal("manage")
                payload = self._read_json(body)
                result = self._text2sql_service().confirm_experience(
                    experience_confirm_match.group(1),
                    principal.username,
                    str(payload.get("note") or ""),
                )
                self.service.store.audit(
                    principal.tenant_id,
                    principal.username,
                    "text2sql.experience.confirm",
                    experience_confirm_match.group(1),
                    {"has_note": bool(str(payload.get("note") or "").strip())},
                )
                self._send_json(200, dict(result))
                return
            experience_feedback_match = TEXT2SQL_EXPERIENCE_FEEDBACK.match(path)
            if experience_feedback_match:
                principal = self._principal("manage")
                payload = self._read_json(body)
                result = self._text2sql_service().feedback_experience(
                    experience_feedback_match.group(1),
                    str(payload.get("decision") or ""),
                    str(payload.get("note") or ""),
                    str(payload.get("corrected_sql") or ""),
                    principal.username,
                )
                self.service.store.audit(
                    principal.tenant_id,
                    principal.username,
                    "text2sql.experience.feedback",
                    experience_feedback_match.group(1),
                    {
                        "decision": str(payload.get("decision") or ""),
                        "has_note": bool(str(payload.get("note") or "").strip()),
                        "has_corrected_sql": bool(
                            str(payload.get("corrected_sql") or "").strip()
                        ),
                    },
                )
                self._send_json(200, dict(result))
                return
            memory_match = TEXT2SQL_MEMORY_REVIEW.match(path)
            if memory_match:
                principal = self._principal("manage")
                payload = self._read_json(body)
                result = self._text2sql_service().review_memory_candidate(
                    memory_match.group(1),
                    str(payload.get("decision") or ""),
                    principal.username,
                    target_skill=str(payload.get("target_skill") or ""),
                    failure_kind=str(payload.get("failure_kind") or ""),
                    content=str(payload.get("content") or ""),
                    rule=(
                        dict(payload["rule"])
                        if isinstance(payload.get("rule"), dict)
                        else None
                    ),
                    review_note=str(payload.get("review_note") or ""),
                )
                self.service.store.audit(
                    principal.tenant_id,
                    principal.username,
                    "text2sql.memory.review",
                    memory_match.group(1),
                    {
                        "decision": str(payload.get("decision") or ""),
                        "has_review_note": bool(str(payload.get("review_note") or "").strip()),
                    },
                )
                self._send_json(200, dict(result))
                return
            memory_action_match = TEXT2SQL_MEMORY_ACTION.match(path)
            if memory_action_match:
                principal = self._principal("manage")
                payload = self._read_json(body)
                memory_id, action = memory_action_match.groups()
                if action == "evaluation":
                    result = self._text2sql_service().start_memory_evaluation(
                        memory_id,
                        principal.username,
                        (principal.username, principal.tenant_id, "local-user"),
                    )
                elif action == "activate":
                    result = self._text2sql_service().activate_memory_candidate(
                        memory_id,
                        principal.username,
                        str(payload.get("reason") or "240-case gate passed"),
                        (principal.username, principal.tenant_id, "local-user"),
                    )
                else:
                    result = self._text2sql_service().rollback_memory(
                        memory_id,
                        principal.username,
                        str(payload.get("reason") or "manual memory rollback"),
                    )
                self.service.store.audit(
                    principal.tenant_id,
                    principal.username,
                    "text2sql.memory.%s" % action,
                    memory_id,
                    {"status": str(result.get("status") or result.get("state") or "")},
                )
                self._send_json(202 if action == "evaluation" else 200, dict(result))
                return
            self._send_json(404, {"error": "not found"})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
        except Exception as exc:
            metrics.inc("http_errors_total")
            self._send_json(500, {"error": "operation failed", "detail": str(exc)})


def run() -> None:
    settings = Settings.from_env()
    service = ApplicationService(settings)
    handler = type("ConfiguredApiHandler", (ApiHandler,), {"service": service, "settings": settings})
    server = ThreadingHTTPServer((settings.host, settings.port), handler)
    print("EvoSQL dashboard: http://%s:%d" % (settings.host, settings.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()
