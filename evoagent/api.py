import hashlib
import json
import mimetypes
import os
import re
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

from .config import Settings
from .auth import Principal
from .github import verify_signature
from .metrics import metrics
from .modes import public_taxonomy, resolve_mode
from .report import to_markdown
from .service import ReviewService


TASK = re.compile(r"^/v1/tasks/([0-9a-f-]+)$")
REPORT = re.compile(r"^/v1/tasks/([0-9a-f-]+)/report$")
FIX = re.compile(r"^/v1/tasks/([0-9a-f-]+)/fix$")
FEEDBACK = re.compile(r"^/v1/tasks/([0-9a-f-]+)/feedback$")
CANCEL = re.compile(r"^/v1/tasks/([0-9a-f-]+)/cancel$")
RESUME = re.compile(r"^/v1/tasks/([0-9a-f-]+)/resume$")
ROLLBACK = re.compile(r"^/v1/skills/([A-Za-z0-9_-]+)/versions/(\d+)/activate$")
SKILL_ARTIFACT_VERSIONS = re.compile(r"^/v1/skill-evolution/([a-z0-9_-]+)/versions$")
SKILL_ARTIFACT_ACTIVATE = re.compile(
    r"^/v1/skill-evolution/([a-z0-9_-]+)/versions/(\d+)/activate$"
)
TEXT2SQL_QUERY_FEEDBACK = re.compile(
    r"^/v1/text2sql/queries/([A-Za-z0-9_-]+)/feedback$"
)
TEXT2SQL_EXPERIENCE_REVIEW = re.compile(
    r"^/v1/text2sql/experiences/([A-Za-z0-9_-]+)/review$"
)
TEXT2SQL_EXPERIENCE_CONFIRM = re.compile(
    r"^/v1/text2sql/experiences/([A-Za-z0-9_-]+)/confirm$"
)
TEXT2SQL_EXPERIENCE_FEEDBACK = re.compile(
    r"^/v1/text2sql/experiences/([A-Za-z0-9_-]+)/feedback$"
)
TEXT2SQL_EXPERIENCE_ACTION = re.compile(
    r"^/v1/text2sql/experiences/([A-Za-z0-9_-]+)/(evaluation)$"
)
TEXT2SQL_MEMORY_REVIEW = re.compile(
    r"^/v1/text2sql/memories/([A-Za-z0-9_-]+)/review$"
)
TEXT2SQL_MEMORY_ACTION = re.compile(
    r"^/v1/text2sql/memories/([A-Za-z0-9_-]+)/(evaluation|activate|rollback)$"
)
WEB_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "web"))


class ApiHandler(BaseHTTPRequestHandler):
    service: ReviewService
    settings: Settings
    server_version = "EvoSQL/0.3"

    def _text2sql_service(self):
        instance = getattr(self.service, "text2sql_web", None)
        if instance is None:
            from .text2sql.web_service import Text2SQLWebService

            instance = Text2SQLWebService(
                self.settings,
                client=self.service.chat_client,
                llm_config=self.service.llm_config,
            )
            setattr(self.service, "text2sql_web", instance)
        return instance

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
        limit = self.settings.max_diff_bytes + 256 * 1024
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
        if path == "/assets/login.css":
            self._serve_file("login.css")
            return
        if path == "/assets/app.js":
            self._serve_file("app.js")
            return
        if path == "/health":
            mode = resolve_mode(
                None, bool(self.service.llm_config)
            )
            self._send_json(200, {"status": "ok", "reviewer": self.service.reviewer.name,
                                  "runtime": self.service.harness.name,
                                  "queue": self.service.queue.backend,
                                  "llm_provider": self.service.llm_config.get("provider", "local"),
                                  "llm_model": self.service.llm_config.get("model", ""),
                                  "run_mode": mode.to_dict(),
                                  "taxonomy": public_taxonomy()})
            return
        principal = self._authenticate_or_send("read")
        if principal is None:
            return
        if path == "/metrics":
            self._send_text(200, metrics.prometheus(), "text/plain; version=0.0.4; charset=utf-8")
            return
        if path == "/api/dashboard":
            mode = resolve_mode(
                None, bool(self.service.llm_config)
            )
            self._send_json(200, {"stats": self.service.store.dashboard_stats(principal.tenant_id),
                                  "tasks": self.service.store.list_tasks(10, principal.tenant_id),
                                  "queue": self.service.queue.backend,
                                  "orchestrator": self.service.reviewer.name,
                                  "llm": {
                                      "enabled": bool(self.service.llm_config),
                                      "provider": self.service.llm_config.get("provider", "local"),
                                      "model": self.service.llm_config.get("model", ""),
                                  },
                                  "run_mode": mode.to_dict(),
                                  "taxonomy": public_taxonomy()})
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
        if path == "/api/tasks":
            self._send_json(200, {"tasks": self.service.store.list_tasks(
                int(query.get("limit", [50])[0]), principal.tenant_id)})
            return
        if path == "/api/skills":
            self._send_json(200, {
                "skills": self.service.list_skills(principal.tenant_id),
                "llm": {
                    "enabled": bool(self.service.llm_config),
                    "provider": self.service.llm_config.get("provider", "local"),
                    "model": self.service.llm_config.get("model", ""),
                },
            })
            return
        if path == "/api/failures":
            if not principal.can("audit"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(200, {"cases": self.service.store.list_failure_cases(
                False, 100, principal.tenant_id
            )})
            return
        if path == "/api/audit":
            if not principal.can("audit"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(200, {"events": self.service.store.list_audit(
                principal.tenant_id, int(query.get("limit", [100])[0])
            )})
            return
        if path == "/api/alerts":
            self._send_json(200, {"alerts": self.service.store.list_alerts(principal.tenant_id)})
            return
        if path == "/api/deployments/llm-review":
            self._send_json(200, {"deployment": self.service.store.get_deployment(
                principal.tenant_id, "llm-review"
            )})
            return
        if path == "/api/queue/dead-letters":
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(200, {"messages": self.service.queue.dead_letters(
                int(query.get("limit", [100])[0])
            )})
            return
        if path == "/v1/evaluation/cases":
            split = query.get("split", ["validation"])[0]
            if split == "holdout":
                self._send_json(403, {"error": "holdout cases are not exposed through the API"})
                return
            self._send_json(200, {
                "cases": self.service.store.list_evaluation_cases(split, True, 100)
            })
            return
        if path == "/v1/evolution/runs":
            self._send_json(200, {
                "runs": self.service.store.list_evolution_runs(int(query.get("limit", [50])[0]))
            })
            return
        if path == "/v1/evolution/status":
            status = self.service.evolution.status()
            status["provider"] = self.service.llm_config.get("provider", "local")
            status["model"] = self.service.llm_config.get("model", "")
            self._send_json(200, status)
            return
        if path == "/v1/skill-evolution/status":
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            skill_name = query.get("skill_name", ["evolved-review"])[0]
            self._send_json(200, self.service.skill_evolution.status(
                skill_name, principal.tenant_id
            ))
            return
        if path == "/v1/skill-evolution/runs":
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(200, {"runs": self.service.store.list_skill_evolution_runs(
                int(query.get("limit", [50])[0]), principal.tenant_id
            )})
            return
        match = SKILL_ARTIFACT_VERSIONS.match(path)
        if match:
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(200, {"versions": self.service.store.list_skill_artifact_versions(
                match.group(1), principal.tenant_id
            )})
            return
        if path == "/github/install":
            if not self.settings.github_app_slug:
                self._send_json(503, {"error": "EVOAGENT_GITHUB_APP_SLUG is not configured"})
                return
            self.send_response(302)
            self.send_header("Location", "https://github.com/apps/%s/installations/new" % self.settings.github_app_slug)
            self.end_headers()
            return
        if path == "/github/setup":
            try:
                installation_id = int(query.get("installation_id", [""])[0])
            except ValueError:
                self._send_json(400, {"error": "missing installation_id"})
                return
            self.service.store.save_installation(installation_id, query.get("account", ["github-app"])[0])
            self.send_response(302)
            self.send_header("Location", "/#github")
            self.end_headers()
            return
        report_match = REPORT.match(path)
        task_match = TASK.match(path)
        feedback_match = FEEDBACK.match(path)
        if feedback_match:
            if not principal.can("review"):
                self._send_json(403, {"error": "permission denied"})
                return
            task = self.service.store.get(feedback_match.group(1), principal.tenant_id)
            if not task:
                self._send_json(404, {"error": "task not found"})
                return
            self._send_json(200, {"cases": self.service.store.list_task_failure_cases(
                feedback_match.group(1), principal.tenant_id
            )})
            return
        if report_match:
            task = self.service.store.get(report_match.group(1), principal.tenant_id)
            if not task or not task.get("report"):
                self._send_json(404, {"error": "task or report not found"})
                return
            self._send_text(200, to_markdown(task["report"]), "text/markdown; charset=utf-8")
            return
        if task_match:
            task = self.service.store.get(task_match.group(1), principal.tenant_id)
            if not task:
                self._send_json(404, {"error": "task not found"})
                return
            self._send_json(200, task)
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
                principal = self._principal("review")
                payload = self._read_json(body)
                try:
                    result = self._text2sql_service().query(
                        str(payload.get("question") or ""),
                        principals=(principal.username, principal.tenant_id),
                        task_id=str(payload.get("task_id") or ""),
                        session_id=str(payload.get("session_id") or "default"),
                    )
                except RuntimeError as exc:
                    self._send_json(503, {"error": str(exc)})
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
            feedback_match = TEXT2SQL_QUERY_FEEDBACK.match(path)
            if feedback_match:
                principal = self._principal("review")
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
            experience_match = TEXT2SQL_EXPERIENCE_REVIEW.match(path)
            if experience_match:
                principal = self._principal("manage")
                payload = self._read_json(body)
                result = self._text2sql_service().review_experience(
                    experience_match.group(1),
                    str(payload.get("decision") or ""),
                    principal.username,
                    str(payload.get("review_note") or ""),
                )
                self.service.store.audit(
                    principal.tenant_id,
                    principal.username,
                    "text2sql.experience.review",
                    experience_match.group(1),
                    {
                        "decision": str(payload.get("decision") or ""),
                        "has_review_note": bool(str(payload.get("review_note") or "").strip()),
                    },
                )
                self._send_json(200, dict(result))
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
            experience_action_match = TEXT2SQL_EXPERIENCE_ACTION.match(path)
            if experience_action_match:
                principal = self._principal("manage")
                self._read_json(body)
                result = self._text2sql_service().start_experience_evaluation(
                    experience_action_match.group(1), principal.username
                )
                self.service.store.audit(
                    principal.tenant_id,
                    principal.username,
                    "text2sql.experience.evaluation",
                    experience_action_match.group(1),
                    {"status": str(result.get("status") or "")},
                )
                self._send_json(202, dict(result))
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
                        memory_id, principal.username
                    )
                elif action == "activate":
                    result = self._text2sql_service().activate_memory_candidate(
                        memory_id,
                        principal.username,
                        str(payload.get("reason") or "240-case gate passed"),
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
            if path == "/v1/reviews":
                principal = self._principal("review")
                payload = self._read_json(body)
                pr = payload.get("pull_request")
                if pr is not None and not isinstance(pr, int):
                    raise ValueError("pull_request must be an integer")
                args = (str(payload.get("repository", "")), str(payload.get("diff", "")), pr)
                enabled_agents = payload.get("enabled_agents")
                if enabled_agents is not None and (
                    not isinstance(enabled_agents, list)
                    or not all(isinstance(item, str) for item in enabled_agents)
                ):
                    raise ValueError("enabled_agents must be an array of role names")
                enabled_skills = payload.get("enabled_skills")
                if enabled_skills is not None and (
                    not isinstance(enabled_skills, list)
                    or not all(isinstance(item, str) for item in enabled_skills)
                ):
                    raise ValueError("enabled_skills must be an array of Agent Skill names")
                options = {
                    "tenant_id": principal.tenant_id,
                    "mode": str(payload.get("mode", "")),
                    "repository_root": str(payload.get("repository_root", "")),
                    "enabled_agents": enabled_agents,
                    "enabled_skills": enabled_skills,
                }
                if query.get("async", ["false"])[0].lower() == "true":
                    result = self.service.enqueue_review(*args, **options)
                    self._send_json(202, result)
                else:
                    self._send_json(201, self.service.create_review(
                        *args, **options
                    ))
                self.service.store.audit(
                    principal.tenant_id, principal.username, "review.create",
                    str(payload.get("repository", "")), {
                        "async": query.get("async", ["false"])[0],
                        "mode": str(payload.get("mode", "")),
                    },
                )
                return
            if path == "/webhooks/github":
                if self.headers.get("X-GitHub-Event", "") != "pull_request":
                    self._send_json(202, {"ignored": True, "reason": "unsupported GitHub event"})
                    return
                if not self.settings.github_webhook_secret:
                    self._send_json(503, {"error": "GitHub webhook secret is not configured"})
                    return
                if not verify_signature(self.settings.github_webhook_secret, body,
                                        self.headers.get("X-Hub-Signature-256", "")):
                    self._send_json(401, {"error": "invalid webhook signature"})
                    return
                payload = self._read_json(body)
                updated_at = (payload.get("pull_request") or {}).get("updated_at")
                if updated_at:
                    try:
                        event_time = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                    except ValueError:
                        raise ValueError("invalid pull_request.updated_at")
                    age = abs((datetime.now(timezone.utc) - event_time).total_seconds())
                    if age > self.settings.webhook_max_age_seconds:
                        self._send_json(409, {"error": "webhook is outside the replay window"})
                        return
                delivery_id = self.headers.get("X-GitHub-Delivery", "")
                digest = hashlib.sha256(body).hexdigest()
                self._send_json(202, self.service.handle_github_pull_request(
                    payload, delivery_id, digest
                ))
                return
            match = FIX.match(path)
            if match:
                principal = self._principal("fix")
                payload = self._read_json(body)
                installation_id = payload.get("installation_id")
                if installation_id is not None and not isinstance(installation_id, int):
                    raise ValueError("installation_id must be an integer")
                result = self.service.create_fix(
                    match.group(1), installation_id, principal.tenant_id
                )
                self.service.store.audit(
                    principal.tenant_id, principal.username, "repair.create",
                    match.group(1), {"branch": result.get("branch")},
                )
                self._send_json(201, result)
                return
            match = FEEDBACK.match(path)
            if match:
                principal = self._principal("review")
                payload = self._read_json(body)
                result = self.service.record_feedback(
                    match.group(1), str(payload.get("category", "")), payload.get("finding"),
                    str(payload.get("note", "")), principal.tenant_id,
                )
                self.service.store.audit(
                    principal.tenant_id, principal.username, "feedback.record", match.group(1),
                    {"category": result["category"]},
                )
                self._send_json(201, result)
                return
            match = CANCEL.match(path)
            if match:
                principal = self._principal("review")
                ok = self.service.cancel_task(match.group(1), principal.tenant_id)
                self.service.store.audit(
                    principal.tenant_id, principal.username, "task.cancel", match.group(1)
                )
                self._send_json(202 if ok else 404, {"cancel_requested": ok})
                return
            match = RESUME.match(path)
            if match:
                principal = self._principal("review")
                result = self.service.resume_task(match.group(1), principal.tenant_id)
                self.service.store.audit(
                    principal.tenant_id, principal.username, "task.resume", match.group(1)
                )
                self._send_json(202, result)
                return
            if path == "/v1/skills/reload":
                principal = self._principal("manage")
                self._send_json(200, {"skills": self.service.reload_skills(),
                                      "note": "New tasks now use the reloaded skill set."})
                return
            if path == "/v1/deployments/llm-review":
                principal = self._principal("manage")
                payload = self._read_json(body)
                result = self.service.releases.configure(
                    principal.tenant_id, "llm-review", payload
                )
                self.service.store.audit(
                    principal.tenant_id, principal.username, "deployment.configure",
                    "llm-review", payload,
                )
                self._send_json(201, result)
                return
            if path == "/v1/queue/dead-letters/replay":
                principal = self._principal("manage")
                payload = self._read_json(body)
                ok = self.service.queue.replay_dead_letter(
                    str(payload.get("message_id", ""))
                )
                self._send_json(202 if ok else 404, {"replayed": ok})
                return
            if path == "/v1/evaluation/cases":
                self._principal("manage")
                payload = self._read_json(body)
                result = self.service.evolution.add_evaluation_case(
                    str(payload.get("name", "")),
                    str(payload.get("diff", "")),
                    payload.get("expected_findings", []),
                    str(payload.get("split", "validation")),
                    "api",
                )
                self._send_json(201, result)
                return
            if path == "/v1/evolution/auto":
                principal = self._principal("manage")
                payload = self._read_json(body)
                result = self.service.evolution.auto_propose(
                    str(payload.get("skill_name", "llm-review")), principal.tenant_id
                )
                if result["decision"] == "activated":
                    self.service.reload_skills()
                self._send_json(201, result)
                return
            if path == "/v1/evolution/propose":
                self._principal("manage")
                payload = self._read_json(body)
                result = self.service.evolution.propose(
                    str(payload.get("skill_name", "")), str(payload.get("prompt", "")),
                    float(payload["regression_score"]) if "regression_score" in payload else None,
                )
                if result["decision"] == "activated":
                    self.service.reload_skills()
                self._send_json(201, result)
                return
            if path == "/v1/skill-evolution/auto":
                principal = self._principal("manage")
                payload = self._read_json(body)
                result = self.service.skill_evolution.auto_propose(
                    str(payload.get("skill_name", "evolved-review")), principal.tenant_id
                )
                if result["decision"] == "activated":
                    self.service.reload_skills()
                self.service.store.audit(
                    principal.tenant_id, principal.username, "skill.evolution.auto",
                    str(payload.get("skill_name", "evolved-review")),
                    {"decision": result["decision"], "run_id": result.get("run_id")},
                )
                self._send_json(201, result)
                return
            if path == "/v1/skill-evolution/propose":
                principal = self._principal("manage")
                payload = self._read_json(body)
                artifact = payload.get("artifact")
                if artifact is None and "skill_md" in payload:
                    artifact = {
                        "name": str(payload.get("skill_name", "")),
                        "skill_md": payload.get("skill_md"),
                        "supporting_files": payload.get("supporting_files") or {},
                    }
                result = self.service.skill_evolution.propose(
                    str(payload.get("skill_name", "")), artifact,
                    principal.tenant_id,
                )
                if result["decision"] == "activated":
                    self.service.reload_skills()
                self.service.store.audit(
                    principal.tenant_id, principal.username, "skill.evolution.propose",
                    str(payload.get("skill_name", "")),
                    {"decision": result["decision"], "run_id": result.get("run_id")},
                )
                self._send_json(201, result)
                return
            match = SKILL_ARTIFACT_ACTIVATE.match(path)
            if match:
                principal = self._principal("manage")
                ok = self.service.skill_evolution.rollback(
                    match.group(1), int(match.group(2)), principal.tenant_id
                )
                if ok:
                    self.service.reload_skills()
                self.service.store.audit(
                    principal.tenant_id, principal.username, "skill.evolution.activate",
                    match.group(1), {"version": int(match.group(2)), "activated": ok},
                )
                self._send_json(200 if ok else 404, {"activated": ok})
                return
            match = ROLLBACK.match(path)
            if match:
                self._principal("manage")
                ok = self.service.evolution.rollback(match.group(1), int(match.group(2)))
                if ok:
                    self.service.reload_skills()
                self._send_json(200 if ok else 404, {"activated": ok})
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
    service = ReviewService(settings)
    handler = type("ConfiguredApiHandler", (ApiHandler,), {"service": service, "settings": settings})
    server = ThreadingHTTPServer((settings.host, settings.port), handler)
    print("EvoSQL dashboard: http://%s:%d" % (settings.host, settings.port))
    print("Persistence: %s | Queue: %s | Orchestrator: %s" % (
        "postgresql" if settings.database_url else "sqlite", service.queue.backend, service.reviewer.name
    ))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.queue.close()
        server.server_close()
