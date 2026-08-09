"""Environment-backed configuration. Never hardcode secrets — everything comes from .env."""

from functools import lru_cache
from typing import Any

from pydantic import ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    embedding_model: str = "text-embedding-3-small"
    llm_timeout_seconds: float = 30.0
    llm_temperature: float = 0.3

    # Qdrant
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    qdrant_collection: str = "products"
    qdrant_timeout_seconds: float = 10.0
    rag_top_k: int = 4
    rag_score_threshold: float = 0.25

    # Supabase
    supabase_url: str = ""
    supabase_service_key: str = ""
    supabase_timeout_seconds: float = 10.0
    history_limit: int = 20

    # Telegram
    telegram_bot_token: str = ""
    ops_chat_id: str = ""
    telegram_webhook_secret: str = ""
    telegram_timeout_seconds: float = 10.0

    # Runtime
    port: int = 10000
    log_level: str = "INFO"
    render_external_url: str = ""

    @field_validator("*", mode="before")
    @classmethod
    def _blank_uses_default(cls, value: Any, info: ValidationInfo) -> Any:
        """An empty env var means "unset", not "invalid".

        `PORT=` in a .env is normal — Render injects the real value at runtime.
        Refusing to boot over a blank line is a worse failure than defaulting.
        Only applies to non-string fields; a blank string stays a blank string.
        """
        if isinstance(value, str) and not value.strip() and info.field_name:
            field = cls.model_fields[info.field_name]
            if field.annotation is not str:
                return field.get_default(call_default_factory=True)
        return value

    @property
    def telegram_api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.telegram_bot_token}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
