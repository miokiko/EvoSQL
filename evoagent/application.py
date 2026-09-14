"""Application lifecycle for the Text2SQL console."""

from .auth import AuthManager
from .auth_store import AuthStore
from .config import Settings
from .text2sql.web_service import Text2SQLWebService


class ApplicationService:
    def __init__(self, settings: Settings):
        settings.validate()
        self.settings = settings
        self.store = AuthStore(settings.db_path)
        self.auth = AuthManager(
            self.store, settings.auth_secret, settings.session_ttl_seconds,
            settings.bootstrap_admin_username, settings.bootstrap_admin_password,
            settings.default_tenant_id,
        )
        self.llm_config = settings.resolved_llm()
        self.text2sql_web = Text2SQLWebService(settings, llm_config=self.llm_config)

    def close(self) -> None:
        self.store.close()
