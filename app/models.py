"""Channel-agnostic domain models.

The agent core consumes IncomingMessage and produces OutgoingMessage. Neither
carries a Telegram or WhatsApp field — platform payloads are translated by the
adapters and never reach the core. This is what makes the WhatsApp migration an
adapter change rather than a rewrite.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.enums import (
    Channel,
    ConversationStatus,
    CustomerIntent,
    HandoverReason,
    HandoverStatus,
    Language,
    LeadCategory,
    MessageRole,
)

# ---------------------------------------------------------------------------
# Channel boundary
# ---------------------------------------------------------------------------


class IncomingMessage(BaseModel):
    """A normalised inbound message, whatever platform delivered it."""

    channel: Channel
    user_id: str
    text: str
    # display name / phone the platform volunteers, used to seed a customer row
    sender_name: str | None = None
    sender_phone: str | None = None
    # platform-specific extras (message_id, chat_id) kept for adapter use only.
    # The agent core must not read from this.
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def conversation_id(self) -> str:
        """`{channel}:{user_id}` — the universal key. Do not change this scheme."""
        return f"{self.channel.value}:{self.user_id}"


class OutgoingMessage(BaseModel):
    """A reply for an adapter to deliver. Adapters apply their own length limits."""

    channel: Channel
    user_id: str
    text: str

    @property
    def conversation_id(self) -> str:
        return f"{self.channel.value}:{self.user_id}"


def parse_conversation_id(conversation_id: str) -> tuple[Channel, str]:
    """Split `{channel}:{user_id}` back into its parts.

    Splits once only — WhatsApp ids are plain digits, but this keeps any future
    id containing a colon intact.
    """
    channel_part, _, user_id = conversation_id.partition(":")
    if not user_id:
        raise ValueError(f"malformed conversation_id: {conversation_id!r}")
    return Channel(channel_part), user_id


# ---------------------------------------------------------------------------
# Persistence models — mirror the tables in migrations/001_initial_schema.sql
# ---------------------------------------------------------------------------


class Customer(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    id: str | None = None
    channel: Channel
    channel_user_id: str
    name: str | None = None
    company_name: str | None = None
    location: str | None = None
    preferred_language: Language = Language.ENGLISH
    phone: str | None = None
    email: str | None = None
    is_opted_out: bool = False


class Conversation(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    id: str | None = None
    conversation_id: str
    customer_id: str | None = None
    channel: Channel
    status: ConversationStatus = ConversationStatus.ACTIVE
    last_message_at: datetime | None = None


class Message(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    conversation_id: str
    role: MessageRole
    content: str
    created_at: datetime | None = None


class Machine(BaseModel):
    """A catalog entry. Source of record for recommendation and comparison."""

    model_config = ConfigDict(use_enum_values=True)

    id: str | None = None
    machine_code: str
    name: str
    category: str
    description: str | None = None
    specifications: dict[str, Any] = Field(default_factory=dict)
    applications: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    price_range: str | None = None
    lead_time: str | None = None
    is_active: bool = True


class Accessory(BaseModel):
    """A part/accessory catalog entry. Manually maintained, not sourced from
    documents. No machine linkage yet — deliberately deferred until there is
    real data to model the relationship against."""

    model_config = ConfigDict(use_enum_values=True)

    id: str | None = None
    name: str
    category: str | None = None
    description: str | None = None
    is_active: bool = True


class LeadScore(BaseModel):
    """One scoring event (BRD §9). Append-only — never updated in place."""

    model_config = ConfigDict(use_enum_values=True)

    conversation_id: str
    customer_id: str | None = None
    score: int = Field(ge=0, le=100)
    category: LeadCategory
    intent: CustomerIntent | None = None
    # per-factor breakdown; a bare number does not survive contact with a rep
    factors: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ConversationSummary(BaseModel):
    """BRD §14. Upserted on conversation_id — one current version each."""

    model_config = ConfigDict(use_enum_values=True)

    conversation_id: str
    customer_id: str | None = None
    customer_name: str | None = None
    company_name: str | None = None
    location: str | None = None
    preferred_language: Language | None = None
    interested_machines: list[str] = Field(default_factory=list)
    requirements: str | None = None
    budget: str | None = None
    timeline: str | None = None
    lead_score: int | None = Field(default=None, ge=0, le=100)
    lead_category: LeadCategory | None = None
    customer_intent: CustomerIntent | None = None
    summary: str | None = None
    next_action: str | None = None
    handover_status: str = "none"
    ai_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class HandoverRequest(BaseModel):
    """BRD §12. `context` is the point — a handoff without history moves work."""

    model_config = ConfigDict(use_enum_values=True)

    conversation_id: str
    customer_id: str | None = None
    reason: HandoverReason
    context: str | None = None
    status: HandoverStatus = HandoverStatus.PENDING


class OptOutEntry(BaseModel):
    """BRD §13. Honoured immediately, never on a schedule."""

    model_config = ConfigDict(use_enum_values=True)

    channel: Channel
    channel_user_id: str
    customer_id: str | None = None
    conversation_id: str | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# Agent-facing models
# ---------------------------------------------------------------------------


class RetrievedChunk(BaseModel):
    """One RAG hit. `score` is kept so weak retrieval can be logged and the
    agent can decline to answer rather than invent a specification."""

    text: str
    score: float
    machine_id: str | None = None
    machine_code: str | None = None
    category: str | None = None


class AgentReply(BaseModel):
    """The agent core's output for one turn."""

    text: str
    # surfaced for logging and for the post-reply intelligence pass
    used_context: bool = False
    tool_calls: list[str] = Field(default_factory=list)
    handover_triggered: bool = False
    opted_out: bool = False
    model: str | None = None
    # ops alerts for the adapter to fire AFTER the customer has their reply
    notifications: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("text")
    @classmethod
    def text_must_not_be_empty(cls, value: str) -> str:
        """Silence reads as broken. Every path must produce something to send."""
        if not value.strip():
            raise ValueError("agent reply text must not be empty")
        return value
