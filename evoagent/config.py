import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, Optional


_DOTENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_dotenv(paths: Optional[Iterable[str]] = None) -> None:
    """Load local dotenv files without overriding real process environment values.

    The project-root file has priority over ``evoagent/.env``.  This allows the
    latter to remain compatible with existing local setups while keeping the
    conventional root-level ``.env`` as the recommended location.
    """
    package_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(package_dir)
    candidates = list(paths) if paths is not None else [
        os.path.join(project_root, ".env"),
        os.path.join(package_dir, ".env"),
    ]
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if not _DOTENV_KEY.fullmatch(key):
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)


load_dotenv()


def _int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError("%s must be positive" % name)
    return value


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _non_negative_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 0:
        raise ValueError("%s must be non-negative" % name)
    return value


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    db_path: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    max_request_bytes: int = 1024 * 1024
    async_workers: int = 2
    llm_provider: str = "local"
    deepseek_api_key: str = ""
    openrouter_api_key: str = ""
    dashscope_api_key: str = ""
    openrouter_site_url: str = ""
    openrouter_app_name: str = "EvoSQL"
    eval_max_cases: int = 5
    auth_required: bool = False
    auth_secret: str = ""
    bootstrap_admin_username: str = ""
    bootstrap_admin_password: str = ""
    default_tenant_id: str = "default"
    session_ttl_seconds: int = 3600
    agent_token_budget: int = 8000
    agent_time_budget_seconds: int = 60
    llm_input_cost_per_million: float = 0.0
    llm_output_cost_per_million: float = 0.0

    def resolved_llm(self) -> Dict[str, object]:
        """Resolve a named provider to the existing OpenAI-compatible transport."""
        provider = self.llm_provider.strip().lower()
        if provider in {"", "local", "none"}:
            if self.llm_base_url or self.llm_api_key or self.llm_model:
                provider = "custom"
            else:
                return {}

        if provider == "deepseek":
            api_key = self.deepseek_api_key or self.llm_api_key
            if not api_key:
                raise ValueError("DeepSeek requires EVOAGENT_DEEPSEEK_API_KEY")
            return {
                "provider": "deepseek",
                "base_url": self.llm_base_url or "https://api.deepseek.com",
                "api_key": api_key,
                "model": self.llm_model or "deepseek-v4-flash",
                "headers": {},
            }

        if provider in {"aliyun", "dashscope", "alibaba-cloud", "alibaba_cloud"}:
            api_key = self.dashscope_api_key or self.llm_api_key
            if not api_key:
                return {}
            return {
                "provider": "aliyun-dashscope",
                "base_url": self.llm_base_url
                or "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "api_key": api_key,
                "model": self.llm_model or "qwen-plus",
                "headers": {},
            }

        if provider in {"openrouter-deepseek-free", "openrouter_deepseek_free"}:
            api_key = self.openrouter_api_key or self.llm_api_key
            if not api_key:
                raise ValueError("OpenRouter requires EVOAGENT_OPENROUTER_API_KEY")
            headers = {}
            if self.openrouter_site_url:
                headers["HTTP-Referer"] = self.openrouter_site_url
            if self.openrouter_app_name:
                headers["X-Title"] = self.openrouter_app_name
            return {
                "provider": "openrouter-deepseek-free",
                "base_url": self.llm_base_url or "https://openrouter.ai/api/v1",
                "api_key": api_key,
                "model": self.llm_model or "deepseek/deepseek-chat-v3-0324:free",
                "headers": headers,
            }

        if provider == "openrouter-free":
            api_key = self.openrouter_api_key or self.llm_api_key
            if not api_key:
                raise ValueError("OpenRouter requires EVOAGENT_OPENROUTER_API_KEY")
            headers = {}
            if self.openrouter_site_url:
                headers["HTTP-Referer"] = self.openrouter_site_url
            if self.openrouter_app_name:
                headers["X-Title"] = self.openrouter_app_name
            return {
                "provider": "openrouter-free",
                "base_url": self.llm_base_url or "https://openrouter.ai/api/v1",
                "api_key": api_key,
                "model": self.llm_model or "openrouter/free",
                "headers": headers,
            }

        if provider == "custom":
            if not (self.llm_base_url and self.llm_api_key and self.llm_model):
                raise ValueError(
                    "Custom LLM requires EVOAGENT_LLM_BASE_URL, "
                    "EVOAGENT_LLM_API_KEY and EVOAGENT_LLM_MODEL"
                )
            return {
                "provider": "custom",
                "base_url": self.llm_base_url,
                "api_key": self.llm_api_key,
                "model": self.llm_model,
                "headers": {},
            }
        raise ValueError("unsupported EVOAGENT_LLM_PROVIDER: %s" % self.llm_provider)



    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv('EVOAGENT_HOST', '127.0.0.1'),
            port=_int('EVOAGENT_PORT', 8080),
            db_path=os.getenv('EVOAGENT_AUTH_DB_PATH', os.path.join(os.path.dirname(os.path.dirname(__file__)), 'artifacts', 'text2sql', 'auth.sqlite3')),
            llm_base_url=os.getenv('EVOAGENT_LLM_BASE_URL', '').rstrip('/'),
            llm_api_key=os.getenv('EVOAGENT_LLM_API_KEY', ''),
            llm_model=os.getenv('EVOAGENT_LLM_MODEL', ''),
            max_request_bytes=_int("EVOAGENT_MAX_REQUEST_BYTES", 1024 * 1024),
            async_workers=_int('EVOAGENT_ASYNC_WORKERS', 2),
            llm_provider=os.getenv('EVOAGENT_LLM_PROVIDER', 'local'),
            deepseek_api_key=os.getenv('EVOAGENT_DEEPSEEK_API_KEY', ''),
            openrouter_api_key=os.getenv('EVOAGENT_OPENROUTER_API_KEY', ''),
            dashscope_api_key=os.getenv('EVOAGENT_DASHSCOPE_API_KEY', os.getenv('DASHSCOPE_API_KEY', '')),
            openrouter_site_url=os.getenv('EVOAGENT_OPENROUTER_SITE_URL', ''),
            openrouter_app_name=os.getenv('EVOAGENT_OPENROUTER_APP_NAME', 'EvoSQL'),
            eval_max_cases=_int('EVOAGENT_EVAL_MAX_CASES', 5),
            auth_required=_bool('EVOAGENT_AUTH_REQUIRED', False),
            auth_secret=os.getenv('EVOAGENT_AUTH_SECRET', ''),
            bootstrap_admin_username=os.getenv('EVOAGENT_BOOTSTRAP_ADMIN_USERNAME', ''),
            bootstrap_admin_password=os.getenv('EVOAGENT_BOOTSTRAP_ADMIN_PASSWORD', ''),
            default_tenant_id=os.getenv('EVOAGENT_DEFAULT_TENANT_ID', 'default'),
            session_ttl_seconds=_int('EVOAGENT_SESSION_TTL_SECONDS', 3600),
            agent_token_budget=_int('EVOAGENT_AGENT_TOKEN_BUDGET', 8000),
            agent_time_budget_seconds=_int('EVOAGENT_AGENT_TIME_BUDGET_SECONDS', 60),
            llm_input_cost_per_million=float(os.getenv('EVOAGENT_LLM_INPUT_COST_PER_MILLION', '0')),
            llm_output_cost_per_million=float(os.getenv('EVOAGENT_LLM_OUTPUT_COST_PER_MILLION', '0')),
        )

    def validate(self) -> None:
        if self.auth_required and len(self.auth_secret.encode("utf-8")) < 32:
            raise ValueError("EVOAGENT_AUTH_SECRET must contain at least 32 bytes when authentication is enabled")
        if bool(self.bootstrap_admin_username) != bool(self.bootstrap_admin_password):
            raise ValueError("bootstrap admin username and password must be configured together")
        if self.llm_input_cost_per_million < 0 or self.llm_output_cost_per_million < 0:
            raise ValueError("LLM token prices cannot be negative")
