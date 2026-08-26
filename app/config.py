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
    llm_max_output_tokens: int = 600

    # OCR (scanned-PDF fallback, app/llm.py transcribe_image / app/documents.py).
    # A dense spec-sheet page transcribed verbatim easily exceeds a normal
    # chat reply's token budget.
    ocr_max_output_tokens: int = 2000
    ocr_timeout_seconds: float = 60.0
    ocr_max_pages: int = 30

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

    # WhatsApp Business Cloud API — WhatsAppAdapter is a documented stub
    # (see app/channels.py) until the client provides these. Present here so
    # the settings object is ready the moment the adapter is implemented; an
    # empty value simply means the adapter stays unreachable.
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_business_account_id: str = ""
    # HMAC-verifies X-Hub-Signature-256 on every inbound webhook — unlike
    # Telegram's plain secret header, this is not optional per Meta's docs.
    whatsapp_app_secret: str = ""
    # Matched against hub.verify_token on the one-time GET webhook verification.
    whatsapp_verify_token: str = ""
    whatsapp_api_version: str = "v21.0"
    whatsapp_timeout_seconds: float = 10.0

    # WhatsApp via the existing portal (whatsapp-portal, its own Vercel app +
    # Supabase). We do NOT call Meta directly for WhatsApp: the portal owns
    # the outbound path, and calling its /api/send-message keeps it the single
    # writer of whatsapp_portal_messages — so its inbox, delivery-status
    # matching (which joins on the wa_message_id only the sender sees), and
    # template stats all stay correct. See CLAUDE.md "WhatsApp status".
    whatsapp_portal_base_url: str = "https://whatsapp-portal-divine.vercel.app"
    whatsapp_portal_timeout_seconds: float = 20.0
    # Shared secret the Apps Script sends on the inbound-forward call. Empty
    # means the endpoint is open — set it in both places before going live.
    whatsapp_inbound_secret: str = ""
    # Master switch for the whole WhatsApp path. False = the inbound endpoint
    # accepts and acknowledges but never runs the agent or sends anything, so
    # this can ship dark and be flipped on deliberately.
    whatsapp_agent_enabled: bool = False

    # Redis (optional operational layer — Addition.md).
    #
    # Redis is never the system of record. Every feature here must be safe to
    # disable or fail: the agent reads/writes Supabase either way. Each
    # feature flag defaults to false and is turned on one phase at a time
    # (Addition.md §8) — Phase A only wires the client and instrumentation;
    # nothing below actually changes behavior until its own phase lands.
    redis_url: str = ""
    redis_enabled: bool = False
    redis_connect_timeout_seconds: float = 2.0
    redis_read_timeout_seconds: float = 1.0
    redis_max_connections: int = 20

    # Per-feature flags (Phase B-F). Independently toggleable so a single
    # feature can be rolled back without touching the others.
    redis_dedupe_enabled: bool = False
    redis_locks_enabled: bool = False
    redis_rate_limit_enabled: bool = False
    redis_jobs_enabled: bool = False
    redis_cache_enabled: bool = False

    # Phase C — per-conversation lock lease and bounded acquisition wait.
    redis_lock_lease_seconds: float = 30.0
    redis_lock_wait_seconds: float = 5.0
    redis_lock_retry_interval_seconds: float = 0.1

    # Phase D — rate limiting (Addition.md §4, initial policy table).
    rate_limit_customer_per_minute: int = 10
    rate_limit_customer_burst_per_5min: int = 30
    rate_limit_dashboard_per_minute: int = 120

    # Phase E — durable post-reply jobs (Redis Streams + consumer group).
    jobs_consumer_group: str = "intelligence-workers"
    jobs_consumer_name: str = ""  # blank = derive one at worker startup
    jobs_claim_idle_seconds: float = 60.0
    jobs_max_attempts: int = 5
    jobs_backoff_base_seconds: float = 2.0
    jobs_block_ms: int = 5000
    jobs_batch_size: int = 10

    # Phase F — exact hot-read caching (Addition.md §Phase F candidate table).
    cache_customer_ttl_seconds: int = 300
    cache_summary_ttl_seconds: int = 1800
    cache_machine_ttl_seconds: int = 2400
    cache_rag_ttl_seconds: int = 1200
    cache_dashboard_ttl_seconds: int = 45
    # whatsapp-portal conversation ids never rotate, so this can be long —
    # it exists to skip a 1.7-4.1s get-or-create call on every AI reply.
    cache_wa_conversation_ttl_seconds: int = 86400
    cache_stampede_lock_ttl_seconds: float = 5.0
    cache_stampede_wait_seconds: float = 2.0

    # Dashboard API
    dashboard_api_key: str = ""

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

    @property
    def whatsapp_api_base(self) -> str:
        return (
            f"https://graph.facebook.com/{self.whatsapp_api_version}"
            f"/{self.whatsapp_phone_number_id}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
