"""Environment-backed configuration. Never hardcode secrets — everything comes from .env."""

from functools import lru_cache

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

    @property
    def telegram_api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.telegram_bot_token}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
