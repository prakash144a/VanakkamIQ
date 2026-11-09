from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:///./vanakkamiq.db"
    # PostgreSQL only: keep VanakkamIQ's tables in their own schema inside a shared database.
    # Empty = the database's default schema (public).
    database_schema: str = ""
    public_base_url: str = "http://localhost:8000"
    secret_key: str = "change-me"

    # OpenAI Realtime over SIP
    openai_api_key: str = ""
    openai_project_id: str = ""
    openai_webhook_secret: str = ""
    openai_realtime_model: str = "gpt-realtime-2.1"
    openai_transcription_model: str = "gpt-4o-transcribe"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_realtime_ws_url: str = "wss://api.openai.com/v1/realtime"

    # Telnyx
    telnyx_api_key: str = ""
    telnyx_base_url: str = "https://api.telnyx.com/v2"

    # SIP headers searched, in order, for the number the caller dialed.
    sip_called_number_headers: list[str] = ["X-Called-Number", "Diversion", "P-Called-Party-ID", "To"]
    # If no header holds the dialed number and exactly one active restaurant has a number, use it.
    single_restaurant_fallback: bool = True

    # Seconds to wait after the AI's last words before hanging up / transferring,
    # so the goodbye or "connecting you now" is heard.
    hangup_delay_seconds: float = 4.0
    transfer_delay_seconds: float = 2.5
    max_call_seconds: float = 1200.0  # safety limit: hang up after 20 minutes

    # On shutdown, how long to let active calls finish.
    shutdown_grace_seconds: float = 300.0

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
