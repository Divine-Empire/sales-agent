"""Controlled vocabularies shared by the database, the agent, and the API.

These mirror the CHECK constraints in migrations/001_initial_schema.sql. A value
added here without a matching migration will be rejected at write time.

All inherit from str so they serialise as plain strings into Supabase and JSON.
"""

from enum import StrEnum


class Channel(StrEnum):
    """Chat platform. The first half of conversation_id."""

    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    HANDED_OVER = "handed_over"
    CLOSED = "closed"
    OPTED_OUT = "opted_out"


class Language(StrEnum):
    """Detected conversation language (BRD §4). Hinglish is code-mixed
    Hindi-English and is treated as its own language, not as either parent."""

    ENGLISH = "en"
    HINDI = "hi"
    HINGLISH = "hinglish"


class LeadCategory(StrEnum):
    """BRD §9."""

    HOT = "hot"
    WARM = "warm"
    COLD = "cold"
    NOT_INTERESTED = "not_interested"


class CustomerIntent(StrEnum):
    """BRD §10 — the full twelve values, including spam."""

    PRODUCT_INQUIRY = "product_inquiry"
    TECHNICAL_INQUIRY = "technical_inquiry"
    PRICE_INQUIRY = "price_inquiry"
    PRODUCT_COMPARISON = "product_comparison"
    PURCHASE_INTENT = "purchase_intent"
    DEALER_INQUIRY = "dealer_inquiry"
    DISTRIBUTOR_INQUIRY = "distributor_inquiry"
    EXISTING_CUSTOMER = "existing_customer"
    NEW_CUSTOMER = "new_customer"
    SERVICE_REQUEST = "service_request"
    GENERAL_INQUIRY = "general_inquiry"
    SPAM = "spam"


class HandoverReason(StrEnum):
    """BRD §12. Formal quotes, negotiation, and bulk orders always escalate."""

    FORMAL_QUOTE = "formal_quote"
    PRICE_NEGOTIATION = "price_negotiation"
    BULK_ORDER = "bulk_order"
    CUSTOMER_REQUEST = "customer_request"
    LOW_CONFIDENCE = "low_confidence"
    OTHER = "other"


class HandoverStatus(StrEnum):
    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class DocumentType(StrEnum):
    """BRD §5 source document kinds."""

    BROCHURE = "brochure"
    MANUAL = "manual"
    SPEC_SHEET = "spec_sheet"
    CATALOG = "catalog"
    FAQ = "faq"
    PRICE_SHEET = "price_sheet"


class AiLogEvent(StrEnum):
    LLM_CALL = "llm_call"
    RAG_SEARCH = "rag_search"
    TOOL_CALL = "tool_call"
    FALLBACK = "fallback"
    ERROR = "error"


class ReportType(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
