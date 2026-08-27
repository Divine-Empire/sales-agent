"""Supabase persistence layer.

The single rule in this module: **a database failure must never cost the
customer a reply.** Every public function catches its own exceptions and logs
them. Reads return an empty default, writes return None or False. Callers may
check the result, but nothing here raises into a request path.

The app is stateless; this module is the memory. Any instance can serve any
webhook, which is what makes Render's sleep/wake cycle survivable.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from supabase import AsyncClient, AsyncClientOptions, acreate_client

from app import cache
from app.config import settings
from app.enums import (
    AiLogEvent,
    Channel,
    ConversationStatus,
    HandoverStatus,
    MessageRole,
)
from app.logging_config import get_logger
from app.models import (
    ConversationSummary,
    Customer,
    HandoverRequest,
    LeadScore,
    Message,
    OptOutEntry,
    parse_conversation_id,
)

log = get_logger(__name__)

_client: AsyncClient | None = None
_client_lock = asyncio.Lock()


async def get_client() -> AsyncClient | None:
    """Lazily create the shared client. Returns None if unconfigured or if
    construction fails, so an unconfigured deployment degrades to no-persistence
    rather than refusing to boot."""
    global _client
    if _client is not None:
        return _client
    if not settings.supabase_url or not settings.supabase_service_key:
        log.warning("supabase_not_configured")
        return None
    async with _client_lock:
        if _client is not None:
            return _client
        try:
            _client = await acreate_client(
                settings.supabase_url,
                settings.supabase_service_key,
                options=AsyncClientOptions(
                    postgrest_client_timeout=int(settings.supabase_timeout_seconds),
                ),
            )
        except Exception:
            log.exception("supabase_client_init_failed")
            return None
    return _client


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# customers
# ---------------------------------------------------------------------------


async def upsert_customer(customer: Customer) -> str | None:
    """Insert or update by (channel, channel_user_id). Returns the customer id.

    Only non-null fields are written, so a later turn that learns just the
    company name cannot blank out a name captured earlier.
    """
    client = await get_client()
    if client is None:
        return None
    payload = customer.model_dump(exclude_none=True, exclude={"id"})
    try:
        result = (
            await client.table("customers")
            .upsert(payload, on_conflict="channel,channel_user_id")
            .execute()
        )
        customer_id = result.data[0]["id"] if result.data else None
        log.info(
            "customer_upserted",
            extra={"customer_id": customer_id, "channel": str(customer.channel)},
        )
        await cache.invalidate(cache.customer_key(str(customer.channel), customer.channel_user_id))
        return customer_id
    except Exception:
        log.exception("customer_upsert_failed", extra={"channel": str(customer.channel)})
        return None


async def update_customer_fields(customer_id: str, fields: dict[str, Any]) -> bool:
    """Edit a customer's own fields from the dashboard (name, company, etc).

    Unlike upsert_customer, this targets a known row by id — a rep editing a
    lead already has the id, not a (channel, channel_user_id) pair.
    """
    client = await get_client()
    if client is None:
        return False
    payload = {k: v for k, v in fields.items() if v is not None}
    if not payload:
        return True
    try:
        result = await client.table("customers").update(payload).eq("id", customer_id).execute()
        log.info("customer_fields_updated", extra={"customer_id": customer_id})
        if result.data:
            row = result.data[0]
            await cache.invalidate(cache.customer_key(row["channel"], row["channel_user_id"]))
        return True
    except Exception:
        log.exception("customer_update_failed", extra={"customer_id": customer_id})
        return False


async def get_customer(channel: Channel, channel_user_id: str) -> dict[str, Any] | None:
    async def _fetch() -> dict[str, Any] | None:
        client = await get_client()
        if client is None:
            return None
        try:
            result = (
                await client.table("customers")
                .select("*")
                .eq("channel", str(channel))
                .eq("channel_user_id", channel_user_id)
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception:
            log.exception("customer_fetch_failed")
            return None

    return await cache.get_or_set(
        cache.customer_key(str(channel), channel_user_id),
        settings.cache_customer_ttl_seconds,
        _fetch,
    )


# ---------------------------------------------------------------------------
# conversations
# ---------------------------------------------------------------------------


async def ensure_conversation(conversation_id: str, customer_id: str | None = None) -> None:
    """Create the conversation row if absent, otherwise bump last_message_at.

    last_message_at drives follow-up queues and recency ranking, so it is
    refreshed on every turn.
    """
    client = await get_client()
    if client is None:
        return
    try:
        channel, _ = parse_conversation_id(conversation_id)
    except ValueError:
        log.error("bad_conversation_id", extra={"conversation_id": conversation_id})
        return
    payload: dict[str, Any] = {
        "conversation_id": conversation_id,
        "channel": str(channel),
        "last_message_at": _now(),
    }
    if customer_id:
        payload["customer_id"] = customer_id
    try:
        await client.table("conversations").upsert(payload, on_conflict="conversation_id").execute()
    except Exception:
        log.exception("conversation_upsert_failed", extra={"conversation_id": conversation_id})


async def set_conversation_status(conversation_id: str, status: ConversationStatus) -> None:
    client = await get_client()
    if client is None:
        return
    try:
        await (
            client.table("conversations")
            .update({"status": str(status)})
            .eq("conversation_id", conversation_id)
            .execute()
        )
        log.info(
            "conversation_status_set",
            extra={"conversation_id": conversation_id, "status": str(status)},
        )
    except Exception:
        log.exception("conversation_status_failed", extra={"conversation_id": conversation_id})


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------


async def save_message(conversation_id: str, role: MessageRole, content: str) -> bool:
    client = await get_client()
    if client is None:
        return False
    try:
        await (
            client.table("messages")
            .insert({"conversation_id": conversation_id, "role": str(role), "content": content})
            .execute()
        )
        return True
    except Exception:
        log.exception(
            "message_save_failed",
            extra={"conversation_id": conversation_id, "role": str(role)},
        )
        return False


async def clear_history(conversation_id: str) -> bool:
    """Delete the conversation's message history so the agent starts fresh.

    Deliberately narrow: messages only. Leads, summaries, handovers, opt-outs
    and telemetry all survive. A customer tidying their chat is not withdrawing
    consent and must not silently destroy a captured lead — and an opt-out that
    disappeared here would be a compliance failure.
    """
    client = await get_client()
    if client is None:
        return False
    try:
        await client.table("messages").delete().eq("conversation_id", conversation_id).execute()
        log.info("history_cleared", extra={"conversation_id": conversation_id})
        return True
    except Exception:
        log.exception("history_clear_failed", extra={"conversation_id": conversation_id})
        return False


async def has_prior_messages(conversation_id: str) -> bool:
    """True if this conversation already has at least one stored message.

    Used to detect a customer's genuine first contact (for the WhatsApp
    auto-greeting — see app/agent.py) without loading full history. Call
    this BEFORE saving the current turn's message, or it always returns
    True. Fails closed (returns True) on a lookup error, so a transient
    Supabase issue never causes a repeat customer to get the greeting again
    — a missed greeting is a smaller problem than a duplicated one.
    """
    client = await get_client()
    if client is None:
        return True
    try:
        result = (
            await client.table("messages")
            .select("id")
            .eq("conversation_id", conversation_id)
            .limit(1)
            .execute()
        )
        return bool(result.data)
    except Exception:
        log.exception("has_prior_messages_failed", extra={"conversation_id": conversation_id})
        return True


async def get_history(conversation_id: str, limit: int | None = None) -> list[Message]:
    """Last N messages in chronological order, ready for the LLM.

    Fetched newest-first so the index is used, then reversed — the window we
    want is the most recent, but the model needs oldest-first.
    """
    client = await get_client()
    if client is None:
        return []
    limit = limit or settings.history_limit
    try:
        result = (
            await client.table("messages")
            .select("conversation_id, role, content, created_at")
            .eq("conversation_id", conversation_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = list(reversed(result.data or []))
        return [Message(**row) for row in rows]
    except Exception:
        log.exception("history_fetch_failed", extra={"conversation_id": conversation_id})
        return []


# ---------------------------------------------------------------------------
# opt-out (BRD §13) — compliance-critical
# ---------------------------------------------------------------------------


async def record_opt_out(entry: OptOutEntry) -> bool:
    """Honour an opt-out immediately: write the list entry and flip the
    denormalised customer flag that outbound checks read."""
    client = await get_client()
    if client is None:
        return False
    payload = entry.model_dump(exclude_none=True)
    try:
        await (
            client.table("opt_out_list")
            .upsert(payload, on_conflict="channel,channel_user_id")
            .execute()
        )
        await (
            client.table("customers")
            .update({"is_opted_out": True})
            .eq("channel", str(entry.channel))
            .eq("channel_user_id", entry.channel_user_id)
            .execute()
        )
        log.info(
            "opt_out_recorded",
            extra={"channel": str(entry.channel), "conversation_id": entry.conversation_id},
        )
        return True
    except Exception:
        log.exception("opt_out_failed", extra={"channel": str(entry.channel)})
        return False


async def is_opted_out(channel: Channel, channel_user_id: str) -> bool:
    """Checked before every outbound automated message.

    Fails closed: if the check itself errors we report opted-out, because
    messaging someone who asked us to stop is worse than missing a reply.
    """
    client = await get_client()
    if client is None:
        return False
    try:
        result = (
            await client.table("opt_out_list")
            .select("id")
            .eq("channel", str(channel))
            .eq("channel_user_id", channel_user_id)
            .limit(1)
            .execute()
        )
        return bool(result.data)
    except Exception:
        log.exception("opt_out_check_failed", extra={"channel": str(channel)})
        return True


# ---------------------------------------------------------------------------
# lead scores (BRD §9, §11) — append-only
# ---------------------------------------------------------------------------


async def save_lead_score(score: LeadScore) -> bool:
    client = await get_client()
    if client is None:
        return False
    try:
        await client.table("lead_scores").insert(score.model_dump(exclude_none=True)).execute()
        log.info(
            "lead_scored",
            extra={
                "conversation_id": score.conversation_id,
                "score": score.score,
                "category": str(score.category),
            },
        )
        return True
    except Exception:
        log.exception("lead_score_save_failed", extra={"conversation_id": score.conversation_id})
        return False


async def get_ranked_leads(limit: int = 20, category: str | None = None) -> list[dict[str, Any]]:
    """Top leads by current score (BRD §11). Reads the current_leads view, so
    ranking is a query rather than a batch job."""
    client = await get_client()
    if client is None:
        return []
    try:
        query = client.table("current_leads").select("*")
        if category:
            query = query.eq("category", category)
        result = await query.order("score", desc=True).limit(limit).execute()
        return result.data or []
    except Exception:
        log.exception("ranked_leads_failed")
        return []


# ---------------------------------------------------------------------------
# summaries (BRD §14)
# ---------------------------------------------------------------------------


async def upsert_summary(summary: ConversationSummary) -> bool:
    client = await get_client()
    if client is None:
        return False
    payload = summary.model_dump(exclude_none=True)
    payload["updated_at"] = _now()
    try:
        await (
            client.table("conversation_summaries")
            .upsert(payload, on_conflict="conversation_id")
            .execute()
        )
        log.info("summary_upserted", extra={"conversation_id": summary.conversation_id})
        await cache.invalidate(cache.summary_key(summary.conversation_id))
        return True
    except Exception:
        log.exception("summary_upsert_failed", extra={"conversation_id": summary.conversation_id})
        return False


async def get_summary(conversation_id: str) -> dict[str, Any] | None:
    async def _fetch() -> dict[str, Any] | None:
        client = await get_client()
        if client is None:
            return None
        try:
            result = (
                await client.table("conversation_summaries")
                .select("*")
                .eq("conversation_id", conversation_id)
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception:
            log.exception("summary_fetch_failed", extra={"conversation_id": conversation_id})
            return None

    return await cache.get_or_set(
        cache.summary_key(conversation_id), settings.cache_summary_ttl_seconds, _fetch
    )


# ---------------------------------------------------------------------------
# handover (BRD §12)
# ---------------------------------------------------------------------------


async def save_handover(request: HandoverRequest) -> str | None:
    client = await get_client()
    if client is None:
        return None
    try:
        result = (
            await client.table("handover_requests")
            .insert(request.model_dump(exclude_none=True))
            .execute()
        )
        handover_id = result.data[0]["id"] if result.data else None
        log.info(
            "handover_saved",
            extra={
                "conversation_id": request.conversation_id,
                "reason": str(request.reason),
                "handover_id": handover_id,
            },
        )
        return handover_id
    except Exception:
        log.exception("handover_save_failed", extra={"conversation_id": request.conversation_id})
        return None


async def get_handover_queue(
    status: HandoverStatus = HandoverStatus.PENDING, limit: int = 50
) -> list[dict[str, Any]]:
    client = await get_client()
    if client is None:
        return []
    try:
        result = (
            await client.table("handover_requests")
            .select("*")
            .eq("status", str(status))
            .order("notified_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []
    except Exception:
        log.exception("handover_queue_failed")
        return []


# ---------------------------------------------------------------------------
# machines — catalog reads for recommendation (BRD §6, §7)
# ---------------------------------------------------------------------------


async def get_machine_by_code(machine_code: str) -> dict[str, Any] | None:
    """Exact-match lookup. Complements vector search, which is weakest on
    model numbers like VMC-850 — precisely what customers type."""

    async def _fetch() -> dict[str, Any] | None:
        client = await get_client()
        if client is None:
            return None
        try:
            result = (
                await client.table("machines")
                .select("*")
                .ilike("machine_code", machine_code)
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception:
            log.exception("machine_fetch_failed", extra={"machine_code": machine_code})
            return None

    return await cache.get_or_set(
        cache.machine_key(machine_code), settings.cache_machine_ttl_seconds, _fetch
    )


async def upsert_machine(
    *,
    machine_code: str,
    name: str,
    category: str,
    description: str | None = None,
    price_range: str | None = None,
    lead_time: str | None = None,
    specifications: dict[str, Any] | None = None,
) -> str | None:
    """Insert or update a catalog entry by machine_code."""
    client = await get_client()
    if client is None:
        return None
    payload = {
        "machine_code": machine_code,
        "name": name,
        "category": category,
        "description": description,
        "price_range": price_range,
        "lead_time": lead_time,
        "specifications": specifications or {},
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    try:
        result = (
            await client.table("machines").upsert(payload, on_conflict="machine_code").execute()
        )
        machine_id = result.data[0]["id"] if result.data else None
        log.info("machine_upserted", extra={"machine_code": machine_code, "id": machine_id})
        await cache.invalidate(
            cache.machine_key(machine_code),
            cache.machines_list_key(None),
            cache.machines_list_key(category),
        )
        return machine_id
    except Exception:
        log.exception("machine_upsert_failed", extra={"machine_code": machine_code})
        return None


async def update_machine_fields(machine_id: str, fields: dict[str, Any]) -> bool:
    """Edit an existing machine's own fields (price, description, etc.) from
    the dashboard, without touching machine_code/name/category or requiring a
    re-upload — the counterpart to update_customer_fields."""
    client = await get_client()
    if client is None:
        return False
    payload = {k: v for k, v in fields.items() if v is not None}
    if not payload:
        return True
    try:
        result = await client.table("machines").update(payload).eq("id", machine_id).execute()
        log.info("machine_fields_updated", extra={"machine_id": machine_id})
        if result.data:
            row = result.data[0]
            await cache.invalidate(
                cache.machine_key(row["machine_code"]),
                cache.machines_list_key(None),
                cache.machines_list_key(row.get("category")),
            )
        return True
    except Exception:
        log.exception("machine_update_failed", extra={"machine_id": machine_id})
        return False


async def save_machine_document(
    *,
    machine_id: str | None,
    doc_type: Any,
    title: str,
    content: str,
    source_url: str | None = None,
) -> str | None:
    """Store extracted document text. Keeping it in Postgres means re-embedding
    never requires re-parsing the original file."""
    client = await get_client()
    if client is None:
        return None
    try:
        result = (
            await client.table("machine_documents")
            .insert(
                {
                    "machine_id": machine_id,
                    "doc_type": str(doc_type),
                    "title": title,
                    "content": content,
                    "source_url": source_url,
                    "indexed_at": _now(),
                }
            )
            .execute()
        )
        return result.data[0]["id"] if result.data else None
    except Exception:
        log.exception("machine_document_save_failed", extra={"machine_id": machine_id})
        return None


async def delete_machine(machine_id: str) -> bool:
    client = await get_client()
    if client is None:
        return False
    try:
        result = await client.table("machines").delete().eq("id", machine_id).execute()
        log.info("machine_deleted", extra={"machine_id": machine_id})
        if result.data:
            row = result.data[0]
            await cache.invalidate(
                cache.machine_key(row["machine_code"]),
                cache.machines_list_key(None),
                cache.machines_list_key(row.get("category")),
            )
        return True
    except Exception:
        log.exception("machine_delete_failed", extra={"machine_id": machine_id})
        return False


async def list_machine_documents(machine_id: str | None = None) -> list[dict[str, Any]]:
    client = await get_client()
    if client is None:
        return []
    try:
        query = client.table("machine_documents").select(
            "id, machine_id, doc_type, title, indexed_at, created_at"
        )
        if machine_id:
            query = query.eq("machine_id", machine_id)
        result = await query.order("created_at", desc=True).execute()
        return result.data or []
    except Exception:
        log.exception("machine_documents_list_failed")
        return []


async def list_machines(category: str | None = None) -> list[dict[str, Any]]:
    async def _fetch() -> list[dict[str, Any]]:
        client = await get_client()
        if client is None:
            return []
        try:
            query = client.table("machines").select("*").eq("is_active", True)
            if category:
                query = query.eq("category", category)
            result = await query.execute()
            return result.data or []
        except Exception:
            log.exception("machine_list_failed")
            return []

    return await cache.get_or_set(
        cache.machines_list_key(category), settings.cache_machine_ttl_seconds, _fetch
    )


# ---------------------------------------------------------------------------
# accessories — parts/accessories catalog, manually maintained (no machine
# linkage yet — deferred, see app/models.py's Accessory docstring)
# ---------------------------------------------------------------------------


async def upsert_accessory(
    *,
    accessory_id: str | None = None,
    name: str,
    category: str | None = None,
    description: str | None = None,
) -> str | None:
    """Insert a new accessory, or update an existing one when accessory_id is
    given. The counterpart to upsert_machine, minus the machine_code
    conflict key since accessories aren't keyed by a catalog code."""
    client = await get_client()
    if client is None:
        return None
    payload = {
        "name": name,
        "category": category,
        "description": description,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    try:
        if accessory_id:
            result = (
                await client.table("accessories").update(payload).eq("id", accessory_id).execute()
            )
        else:
            result = await client.table("accessories").insert(payload).execute()
        row_id = result.data[0]["id"] if result.data else accessory_id
        log.info("accessory_upserted", extra={"id": row_id})
        await cache.invalidate(
            cache.accessories_list_key(None),
            cache.accessories_list_key(category),
        )
        return row_id
    except Exception:
        log.exception("accessory_upsert_failed", extra={"id": accessory_id})
        return None


async def update_accessory_fields(accessory_id: str, fields: dict[str, Any]) -> bool:
    """Edit an existing accessory's fields — the counterpart to
    update_machine_fields."""
    client = await get_client()
    if client is None:
        return False
    payload = {k: v for k, v in fields.items() if v is not None}
    if not payload:
        return True
    try:
        result = await client.table("accessories").update(payload).eq("id", accessory_id).execute()
        log.info("accessory_fields_updated", extra={"accessory_id": accessory_id})
        if result.data:
            row = result.data[0]
            await cache.invalidate(
                cache.accessories_list_key(None),
                cache.accessories_list_key(row.get("category")),
            )
        return True
    except Exception:
        log.exception("accessory_update_failed", extra={"accessory_id": accessory_id})
        return False


async def delete_accessory(accessory_id: str) -> bool:
    client = await get_client()
    if client is None:
        return False
    try:
        result = await client.table("accessories").delete().eq("id", accessory_id).execute()
        log.info("accessory_deleted", extra={"accessory_id": accessory_id})
        if result.data:
            row = result.data[0]
            await cache.invalidate(
                cache.accessories_list_key(None),
                cache.accessories_list_key(row.get("category")),
            )
        return True
    except Exception:
        log.exception("accessory_delete_failed", extra={"accessory_id": accessory_id})
        return False


async def list_accessories(category: str | None = None) -> list[dict[str, Any]]:
    async def _fetch() -> list[dict[str, Any]]:
        client = await get_client()
        if client is None:
            return []
        try:
            query = client.table("accessories").select("*").eq("is_active", True)
            if category:
                query = query.eq("category", category)
            result = await query.execute()
            return result.data or []
        except Exception:
            log.exception("accessory_list_failed")
            return []

    return await cache.get_or_set(
        cache.accessories_list_key(category), settings.cache_machine_ttl_seconds, _fetch
    )


# ---------------------------------------------------------------------------
# ai_logs — telemetry. Fire-and-forget: never awaited in a reply path.
# ---------------------------------------------------------------------------


async def log_ai_event(
    conversation_id: str,
    event_type: AiLogEvent,
    *,
    model: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    latency_ms: int | None = None,
    retrieved_chunks: list[dict[str, Any]] | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    client = await get_client()
    if client is None:
        return
    row = {
        "conversation_id": conversation_id,
        "event_type": str(event_type),
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "latency_ms": latency_ms,
        "retrieved_chunks": retrieved_chunks,
        "payload": payload,
    }
    try:
        await (
            client.table("ai_logs")
            .insert({k: v for k, v in row.items() if v is not None})
            .execute()
        )
    except Exception:
        log.exception("ai_log_failed", extra={"conversation_id": conversation_id})


# ---------------------------------------------------------------------------
# conversation bootstrap — one call per inbound turn
# ---------------------------------------------------------------------------


async def list_customers(limit: int = 200) -> list[dict[str, Any]]:
    """Customer management (BRD §16)."""
    client = await get_client()
    if client is None:
        return []
    try:
        result = (
            await client.table("customers")
            .select("*")
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []
    except Exception:
        log.exception("customers_list_failed")
        return []


async def list_opt_outs(limit: int = 200) -> list[dict[str, Any]]:
    """Opt-out management (BRD §13, §16)."""
    client = await get_client()
    if client is None:
        return []
    try:
        result = (
            await client.table("opt_out_list")
            .select("*")
            .order("opted_out_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []
    except Exception:
        log.exception("opt_outs_list_failed")
        return []


async def list_summaries(limit: int = 200) -> list[dict[str, Any]]:
    """Conversation summaries — the source for machine-interest analytics."""
    client = await get_client()
    if client is None:
        return []
    try:
        result = (
            await client.table("conversation_summaries")
            .select("*")
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []
    except Exception:
        log.exception("summaries_list_failed")
        return []


async def list_ai_logs(
    conversation_id: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """AI conversation logs (BRD §16) — model, tokens, latency per turn."""
    client = await get_client()
    if client is None:
        return []
    try:
        query = client.table("ai_logs").select("*")
        if conversation_id:
            query = query.eq("conversation_id", conversation_id)
        result = await query.order("created_at", desc=True).limit(limit).execute()
        return result.data or []
    except Exception:
        log.exception("ai_logs_list_failed")
        return []


async def update_handover_status(handover_id: str, status: Any) -> bool:
    """Let a rep acknowledge or resolve a handover from the dashboard."""
    client = await get_client()
    if client is None:
        return False
    payload: dict[str, Any] = {"status": str(status)}
    if str(status) == "resolved":
        payload["resolved_at"] = _now()
    try:
        await client.table("handover_requests").update(payload).eq("id", handover_id).execute()
        log.info(
            "handover_status_updated",
            extra={"handover_id": handover_id, "status": str(status)},
        )
        return True
    except Exception:
        log.exception("handover_update_failed", extra={"handover_id": handover_id})
        return False


async def count_conversations() -> int:
    client = await get_client()
    if client is None:
        return 0
    try:
        result = await client.table("conversations").select("id", count="exact").execute()
        return result.count or 0
    except Exception:
        log.exception("conversation_count_failed")
        return 0


async def list_conversations(limit: int = 50, channel: str | None = None) -> list[dict[str, Any]]:
    """Inbox view: every conversation, newest activity first, with a last-
    message preview and (if the agent has analysed it) summary/lead fields.

    Built from `conversations` rather than `conversation_summaries` so a
    brand-new thread the intelligence pass hasn't touched yet still shows up
    — `conversation_summaries`/`current_leads` are enrichment, not the
    membership list.
    """
    client = await get_client()
    if client is None:
        return []
    try:
        query = client.table("conversations").select(
            "conversation_id, channel, status, started_at, last_message_at, "
            "customers(name, company_name, channel_user_id, phone)"
        )
        if channel:
            query = query.eq("channel", channel)
        result = await query.order("last_message_at", desc=True).limit(limit).execute()
        conversations = result.data or []
        if not conversations:
            return []

        conversation_ids = [row["conversation_id"] for row in conversations]

        # PostgREST has no DISTINCT ON embed, so pull recent messages for
        # just these conversations and keep the first (newest) per id —
        # same reverse-then-limit trick as get_history.
        messages_result = (
            await client.table("messages")
            .select("conversation_id, role, content, created_at")
            .in_("conversation_id", conversation_ids)
            .order("created_at", desc=True)
            .limit(len(conversation_ids) * 5)
            .execute()
        )
        last_message: dict[str, dict[str, Any]] = {}
        for row in messages_result.data or []:
            cid = row["conversation_id"]
            if cid not in last_message:
                last_message[cid] = row

        summaries_result = (
            await client.table("conversation_summaries")
            .select(
                "conversation_id, lead_score, lead_category, handover_status, "
                "customer_intent, updated_at"
            )
            .in_("conversation_id", conversation_ids)
            .execute()
        )
        summary_by_id = {row["conversation_id"]: row for row in summaries_result.data or []}

        rows = []
        for row in conversations:
            cid = row["conversation_id"]
            customer = row.pop("customers", None) or {}
            summary = summary_by_id.get(cid)
            last = last_message.get(cid)
            rows.append(
                {
                    "conversation_id": cid,
                    "channel": row["channel"],
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "last_message_at": row["last_message_at"],
                    "customer_name": customer.get("name"),
                    "company_name": customer.get("company_name"),
                    "channel_user_id": customer.get("channel_user_id"),
                    "phone": customer.get("phone"),
                    "last_message": last.get("content") if last else None,
                    "last_message_role": last.get("role") if last else None,
                    "lead_score": summary.get("lead_score") if summary else None,
                    "lead_category": summary.get("lead_category") if summary else None,
                    "handover_status": summary.get("handover_status") if summary else "none",
                    "customer_intent": summary.get("customer_intent") if summary else None,
                }
            )
        return rows
    except Exception:
        log.exception("conversations_list_failed")
        return []


async def bootstrap_turn(
    channel: Channel,
    user_id: str,
    conversation_id: str,
    sender_name: str | None = None,
    sender_phone: str | None = None,
) -> str | None:
    """Ensure customer and conversation rows exist for an inbound message.

    Returns the customer id, or None if persistence is unavailable — callers
    continue regardless, since a missing row must not block a reply.
    """
    customer_id = await upsert_customer(
        Customer(
            channel=channel,
            channel_user_id=user_id,
            name=sender_name,
            phone=sender_phone,
        )
    )
    await ensure_conversation(conversation_id, customer_id)
    return customer_id
