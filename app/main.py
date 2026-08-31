"""FastAPI application — webhooks and the read API behind the dashboard.

Two hard rules in the webhook path:

1. Always return 200 to Telegram, even on failure. A non-200 makes Telegram
   retry the same update, and the customer sees duplicate replies.
2. Always send the customer something. Every exception path ends in a polite
   message; silence reads as broken.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import (
    analytics,
    dedupe,
    documents,
    intelligence,
    jobs,
    prompts,
    rate_limit,
    redis_client,
    store,
    whatsapp_portal,
)
from app.agent import handle_message
from app.channels import TelegramAdapter, WhatsAppPortalAdapter, build_notification
from app.config import settings
from app.enums import Channel, DocumentType, HandoverStatus, LeadCategory
from app.logging_config import get_logger, setup_logging
from app.models import LeadScore, OutgoingMessage

setup_logging()
log = get_logger(__name__)

telegram = TelegramAdapter()
# WhatsApp goes out through the existing portal, never Meta directly — see
# WhatsAppPortalAdapter's docstring. Ops alerts still go to Telegram.
whatsapp = WhatsAppPortalAdapter()

# Strong refs to detached tasks; without this the GC can cancel them mid-flight.
_background: set[asyncio.Task[None]] = set()


@asynccontextmanager
async def lifespan(_: FastAPI):
    redis_ok = await redis_client.ping() if settings.redis_enabled else None
    log.info(
        "startup",
        extra={
            "model": settings.openai_model,
            "fallback": settings.groq_model,
            "collection": settings.qdrant_collection,
            "supabase": bool(settings.supabase_url),
            "telegram": bool(settings.telegram_bot_token),
            "redis_enabled": settings.redis_enabled,
            "redis_connected": redis_ok,
        },
    )
    if settings.redis_enabled and not redis_ok:
        # Loud, not fatal — the whole point of Phase A is that a Redis outage
        # must never stop the agent from serving chats.
        log.warning("redis_unavailable_at_startup")
    yield
    await redis_client.close()
    log.info("shutdown")


app = FastAPI(title="Divine Empire Sales Agent", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Render health check. Deliberately dependency-free: reporting unhealthy
    because Qdrant is briefly slow would take the whole service down."""
    return {"status": "ok", "service": "sales-agent"}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    """Dependency-aware diagnostic, separate from /health on purpose
    (Addition.md Phase A). Render's health check must never fail because
    Redis is down; this endpoint is for operators who want to see it anyway."""
    return {
        "status": "ok",
        "redis": await redis_client.readiness(),
    }


# ---------------------------------------------------------------------------
# Telegram webhook
# ---------------------------------------------------------------------------


async def _deliver(reply: Any, user_id: str, conversation_id: str) -> None:
    """Send the reply, then fire ops alerts. Order matters — the customer waits
    on their answer, the sales team does not."""
    await telegram.send(
        OutgoingMessage(channel=Channel.TELEGRAM, user_id=user_id, text=reply.text),
    )
    for note in reply.notifications:
        text = build_notification(note, conversation_id)
        if text:
            await telegram.notify_ops(text)


async def _process(update: dict[str, Any]) -> None:
    """Handle one update end to end. Never raises — this runs detached."""
    incoming = telegram.parse(update)
    if incoming is None:
        return

    try:
        # Suppress automated replies to anyone who has opted out (BRD §13).
        if await store.is_opted_out(incoming.channel, incoming.user_id):
            log.info("suppressed_opted_out", extra={"conversation_id": incoming.conversation_id})
            return

        limit_result = await rate_limit.check_customer(str(incoming.channel), incoming.user_id)
        if not limit_result.allowed:
            log.info(
                "customer_rate_limited",
                extra={
                    "conversation_id": incoming.conversation_id,
                    "limited_by": limit_result.limited_by,
                },
            )
            await telegram.send(
                OutgoingMessage(
                    channel=Channel.TELEGRAM,
                    user_id=incoming.user_id,
                    text=prompts.RATE_LIMITED_MESSAGE,
                )
            )
            return

        await telegram.send_chat_action(incoming.user_id)
        reply = await handle_message(incoming)
        await _deliver(reply, incoming.user_id, incoming.conversation_id)

        # Scoring, intent and summary (BRD §9-§11, §14) run only after the
        # customer has their reply — they are an LLM call the customer never
        # sees, and inline they would add seconds to every turn. Durable via
        # the job queue (Phase E) when enabled; falls back to the original
        # inline call otherwise, so a queue outage degrades rather than
        # silently drops the analysis.
        if reply.model != "command":
            queued = await jobs.enqueue(jobs.JOB_TYPE_INTELLIGENCE, incoming.conversation_id)
            if not queued:
                await intelligence.analyse(incoming.conversation_id)
    except Exception:
        log.exception(
            "webhook_processing_failed",
            extra={"conversation_id": incoming.conversation_id},
        )
        # The customer must still hear something.
        await telegram.send(
            OutgoingMessage(
                channel=Channel.TELEGRAM,
                user_id=incoming.user_id,
                text=prompts.ERROR_MESSAGE,
            )
        )


@app.post("/webhooks/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> JSONResponse:
    """Receive an update and acknowledge immediately.

    Processing runs detached so Telegram gets its 200 inside the timeout window
    — the LLM round-trip alone can exceed it, and a slow ack means retries and
    duplicate replies.
    """
    if settings.telegram_webhook_secret:
        if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
            log.warning("webhook_bad_secret")
            raise HTTPException(status_code=403, detail="forbidden")

    try:
        update = await request.json()
    except Exception:
        log.warning("webhook_bad_json")
        return JSONResponse({"ok": True})

    # Claim the update before doing anything else. A duplicate delivery (a
    # Telegram retry) acks 200 immediately without being processed again —
    # same response Telegram would get from a fresh delivery, so it has no
    # reason to retry further.
    update_id = update.get("update_id")
    if update_id is not None and not await dedupe.claim_update(update_id):
        return JSONResponse({"ok": True})

    # Record every chat the bot is spoken to in, so group ids are discoverable
    # via /admin/telegram/chats.
    chat = ((update.get("message") or update.get("my_chat_member") or {}).get("chat")) or {}
    if chat.get("id") is not None:
        SEEN_CHATS[str(chat["id"])] = {
            "chat_id": chat["id"],
            "type": chat.get("type"),
            "title": chat.get("title") or chat.get("first_name"),
        }

    # Fire-and-forget: hold a reference so the task is not garbage collected.
    task = asyncio.create_task(_process(update))
    _background.add(task)
    task.add_done_callback(_background.discard)

    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# WhatsApp inbound (forwarded by app_scripts/app.gs, not Meta directly)
# ---------------------------------------------------------------------------


async def _process_whatsapp(payload: dict[str, Any]) -> None:
    """Handle one forwarded WhatsApp message. Never raises — runs detached.

    Mirrors `_process` but through the portal adapter. Deliberate differences:
    no typing indicator (the portal has no such API) and no quick-reply
    keyboard (WhatsApp only offers those on pre-approved templates, not
    free-form replies).
    """
    incoming = whatsapp.parse(payload)
    if incoming is None:
        return

    conversation_id = incoming.conversation_id
    try:
        if await store.is_opted_out(incoming.channel, incoming.user_id):
            log.info("suppressed_opted_out", extra={"conversation_id": conversation_id})
            return

        limit_result = await rate_limit.check_customer(str(incoming.channel), incoming.user_id)
        if not limit_result.allowed:
            log.info(
                "customer_rate_limited",
                extra={
                    "conversation_id": conversation_id,
                    "limited_by": limit_result.limited_by,
                },
            )
            # Deliberately silent: a marketing blast can trip this for many
            # numbers at once, and a flood of "you're too fast" texts on a
            # billable channel is worse than saying nothing. The customer's
            # message is still visible to an operator in the portal inbox.
            return

        reply = await handle_message(incoming)
        await whatsapp.send(
            OutgoingMessage(channel=Channel.WHATSAPP, user_id=incoming.user_id, text=reply.text)
        )
        for note in reply.notifications:
            text = build_notification(note, conversation_id)
            if text:
                await telegram.notify_ops(text)

        if reply.model != "command":
            queued = await jobs.enqueue(jobs.JOB_TYPE_INTELLIGENCE, conversation_id)
            if not queued:
                await intelligence.analyse(conversation_id)
    except Exception:
        log.exception("whatsapp_processing_failed", extra={"conversation_id": conversation_id})
        await whatsapp.send(
            OutgoingMessage(
                channel=Channel.WHATSAPP,
                user_id=incoming.user_id,
                text=prompts.ERROR_MESSAGE,
            )
        )


@app.post("/webhooks/whatsapp-inbound")
async def whatsapp_inbound(
    request: Request,
    x_inbound_secret: str | None = Header(default=None),
) -> JSONResponse:
    """Inbound WhatsApp messages, forwarded by `app_scripts/app.gs`.

    This is NOT Meta's webhook. The Apps Script holds that registration for
    this phone number (Meta allows exactly one per number, and re-pointing it
    would break the live marketing pipeline's reply tracking), so it forwards
    a small JSON body here after doing its own Sheet bookkeeping.

    Always acks 200, even when disabled or on bad input: the caller is a
    fire-and-forget `UrlFetchApp.fetch` inside a live webhook handler, and a
    non-200 there would only add noise to the Apps Script's logs without
    changing anything on our side.
    """
    if settings.whatsapp_inbound_secret:
        if x_inbound_secret != settings.whatsapp_inbound_secret:
            log.warning("whatsapp_inbound_bad_secret")
            raise HTTPException(status_code=403, detail="forbidden")

    if not settings.whatsapp_agent_enabled:
        log.info("whatsapp_inbound_disabled")
        return JSONResponse({"ok": True, "handled": False, "reason": "disabled"})

    try:
        payload = await request.json()
    except Exception:
        log.warning("whatsapp_inbound_bad_json")
        return JSONResponse({"ok": True, "handled": False, "reason": "bad_json"})

    # Valid JSON is not necessarily an object — a bare string or list parses
    # fine and would then blow up on .get(). Apps Script builds this body, so
    # a wrong shape means someone changed one side without the other.
    if not isinstance(payload, dict):
        log.warning("whatsapp_inbound_bad_shape", extra={"type": type(payload).__name__})
        return JSONResponse({"ok": True, "handled": False, "reason": "bad_shape"})

    # Dedupe on Meta's wamid, reusing the Phase B machinery. The Apps Script
    # has its own 5-minute duplicate guard, but Meta also retries the webhook
    # itself, and a duplicate here means a duplicate reply on a billable
    # channel plus a second lead-scoring pass.
    message_id = payload.get("message_id")
    if message_id and not await dedupe.claim_update(f"whatsapp:{message_id}"):
        return JSONResponse({"ok": True, "handled": False, "reason": "duplicate"})

    task = asyncio.create_task(_process_whatsapp(payload))
    _background.add(task)
    task.add_done_callback(_background.discard)

    return JSONResponse({"ok": True, "handled": True})


@app.post("/admin/telegram/set-webhook")
async def set_webhook(url: str | None = None) -> dict[str, Any]:
    """Register the webhook with Telegram. Uses RENDER_EXTERNAL_URL by default."""
    base = url or settings.render_external_url
    if not base:
        raise HTTPException(status_code=400, detail="no url and RENDER_EXTERNAL_URL is unset")
    target = f"{base.rstrip('/')}/webhooks/telegram"
    result = await telegram.set_webhook(target, settings.telegram_webhook_secret or None)
    return {"webhook": target, "telegram": result}


@app.get("/admin/telegram/info")
async def telegram_info() -> dict[str, Any]:
    """Bot identity — a quick way to confirm the token is live."""
    return await telegram.get_me()


# Chats the bot has seen, newest first. Populated by the webhook, so any group
# the bot is in shows up here after one message — which is how you find the
# group id for OPS_CHAT_ID without adding a third-party bot to a channel that
# will carry customer PII.
SEEN_CHATS: dict[str, dict[str, Any]] = {}


@app.get("/admin/telegram/chats")
async def telegram_chats() -> dict[str, Any]:
    return {"count": len(SEEN_CHATS), "chats": list(SEEN_CHATS.values())}


# ---------------------------------------------------------------------------
# Dashboard API (BRD §15, §16)
#
# Authenticated with a shared secret in X-API-Key. These return customer PII —
# names, companies, budgets, full transcripts — so the key is required whenever
# DASHBOARD_API_KEY is configured. The dashboard calls them server-side only,
# so the key never reaches a browser.
# ---------------------------------------------------------------------------


async def require_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    """Reject unauthenticated or rate-limited dashboard requests.

    When no key is configured the API stays open — that keeps local development
    frictionless, and the deployment sets the key. Startup logs loudly if it is
    missing so an unprotected production deploy is visible.

    Rate limiting fails CLOSED (Addition.md §4): an authenticated internal API
    that can't verify its own limits should refuse rather than risk abuse.
    """
    if not settings.dashboard_api_key:
        return
    if x_api_key != settings.dashboard_api_key:
        log.warning("api_unauthorised")
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")

    result = await rate_limit.check_dashboard(x_api_key, request.url.path)
    if not result.allowed:
        log.warning("api_rate_limited", extra={"route": request.url.path})
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded",
            headers={"Retry-After": "60"},
        )


api = APIRouter(prefix="/api", tags=["dashboard"], dependencies=[Depends(require_api_key)])


@api.get("/leads")
async def leads(limit: int = 20, category: str | None = None) -> dict[str, Any]:
    """Ranked leads, highest score first (BRD §11)."""
    rows = await store.get_ranked_leads(limit=limit, category=category)
    return {"count": len(rows), "leads": rows}


@api.get("/handovers")
async def handovers(status: HandoverStatus = HandoverStatus.PENDING) -> dict[str, Any]:
    """The handover queue (BRD §12, §16)."""
    rows = await store.get_handover_queue(status=status)
    return {"count": len(rows), "handovers": rows}


@api.get("/conversations")
async def conversations(limit: int = 50, channel: str | None = None) -> dict[str, Any]:
    """Inbox list: every conversation, newest activity first, with a
    last-message preview, lead score/category, and handover status."""
    rows = await store.list_conversations(limit=limit, channel=channel)
    return {"count": len(rows), "conversations": rows}


@api.get("/conversations/{conversation_id}")
async def conversation(conversation_id: str) -> dict[str, Any]:
    """Full history plus the current summary for one conversation."""
    messages = await store.get_history(conversation_id, limit=200)
    return {
        "conversation_id": conversation_id,
        "summary": await store.get_summary(conversation_id),
        "messages": [m.model_dump(mode="json") for m in messages],
    }


@api.delete("/conversations/{conversation_id}")
async def delete_conversation_route(conversation_id: str) -> dict[str, Any]:
    """Permanently delete a conversation (messages, summary, lead-score
    history) from the inbox — for clearing test/demo conversations, not a
    customer-facing action. Irreversible; the dashboard should confirm
    before calling this."""
    ok = await store.delete_conversation(conversation_id)
    if not ok:
        raise HTTPException(status_code=500, detail="could not delete conversation")
    return {"conversation_id": conversation_id, "deleted": True}


@api.get("/overview")
async def overview() -> dict[str, Any]:
    """Everything the dashboard landing page needs, in one round trip."""
    return await analytics.overview()


@api.get("/reports/{report_type}")
async def report(report_type: str) -> dict[str, Any]:
    """Daily, weekly or monthly aggregate (BRD §15)."""
    return await analytics.report(report_type)


@api.get("/customers")
async def customers(limit: int = 200) -> dict[str, Any]:
    rows = await store.list_customers(limit=limit)
    return {"count": len(rows), "customers": rows}


@api.get("/opt-outs")
async def opt_outs(limit: int = 200) -> dict[str, Any]:
    """BRD §13 — who asked not to be contacted, and when."""
    rows = await store.list_opt_outs(limit=limit)
    return {"count": len(rows), "opt_outs": rows}


@api.get("/summaries")
async def summaries(limit: int = 200) -> dict[str, Any]:
    rows = await store.list_summaries(limit=limit)
    return {"count": len(rows), "summaries": rows}


@api.get("/logs")
async def ai_logs(conversation_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    """AI conversation logs (BRD §16) — model, tokens, latency, retrieval."""
    rows = await store.list_ai_logs(conversation_id=conversation_id, limit=limit)
    return {"count": len(rows), "logs": rows}


@api.get("/jobs/dead-letter-count")
async def jobs_dead_letter_count() -> dict[str, Any]:
    """Exposed per Addition.md Phase E ('dead-letter jobs are observable') —
    a non-zero count means the intelligence worker is failing repeatedly on
    something and needs a look, not that a customer went unanswered."""
    return {"dead_letter_count": await jobs.dead_letter_count()}


# ---------------------------------------------------------------------------
# WhatsApp — read-only proxy over the portal's own API
#
# The portal owns this data; these routes exist so the CRM dashboard reaches it
# through the one backend and one API key it already uses, instead of holding a
# second database credential. Read-only: operator sending is not built until
# permissions and auditing are designed. Under /whatsapp/ rather than
# /conversations/ because /conversations/{conversation_id} would swallow it.
# ---------------------------------------------------------------------------


@api.get("/whatsapp/conversations")
async def whatsapp_conversations(
    limit: int = 30, cursor: str | None = None, filter: str = "all", q: str | None = None
) -> dict[str, Any]:
    """WhatsApp inbox list from the portal, newest activity first.

    `q` switches to the portal's server-side search. `available: false` means
    the portal could not be reached — the dashboard shows that differently
    from a genuinely empty inbox.
    """
    if q:
        result = await whatsapp_portal.search_conversations(q, limit=limit)
        return {
            "count": len(result["conversations"]),
            "conversations": result["conversations"],
            "has_more": False,
            "next_cursor": None,
            "available": result["available"],
            "query": q,
        }

    result = await whatsapp_portal.list_conversations(limit=limit, cursor=cursor, filter_=filter)
    return {
        "count": len(result["conversations"]),
        "conversations": result["conversations"],
        "has_more": result["has_more"],
        "next_cursor": result["next_cursor"],
        "available": result["available"],
    }


@api.get("/whatsapp/conversations/{conversation_id}")
async def whatsapp_conversation(conversation_id: str, limit: int = 100) -> dict[str, Any]:
    """One WhatsApp thread, oldest message first, plus the contact header."""
    result = await whatsapp_portal.get_messages(conversation_id, limit=limit)
    if result is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return {
        "conversation": result["conversation"],
        "count": len(result["messages"]),
        "messages": result["messages"],
        "available": result["available"],
    }


@api.patch("/handovers/{handover_id}")
async def update_handover(handover_id: str, status: HandoverStatus) -> dict[str, Any]:
    """Let a rep acknowledge or resolve a handover from the dashboard."""
    ok = await store.update_handover_status(handover_id, status)
    if not ok:
        raise HTTPException(status_code=500, detail="could not update handover")
    return {"id": handover_id, "status": str(status)}


# Midpoint of each category's scoring band (see intelligence.py's category
# guidance) — a manual drag needs *a* number, and the midpoint reads as
# "typical for this category" rather than an arbitrary boundary value.
_CATEGORY_SCORE_MIDPOINT = {
    LeadCategory.HOT: 85,
    LeadCategory.WARM: 55,
    LeadCategory.COLD: 20,
    LeadCategory.NOT_INTERESTED: 0,
}


@api.patch("/leads/{conversation_id}")
async def override_lead_category(conversation_id: str, category: LeadCategory) -> dict[str, Any]:
    """Manually move a lead to a different category (the kanban drag action).

    Appends a new lead_scores row rather than editing one in place — scoring
    stays append-only throughout the system, so a manual move sits in the same
    history as an AI-generated score and ranking movement stays auditable. The
    next inbound message still triggers ordinary AI re-scoring; this is a
    correction, not a lock.
    """
    existing = await store.get_ranked_leads(limit=1000)
    current = next(
        (lead for lead in existing if lead.get("conversation_id") == conversation_id),
        None,
    )
    score = LeadScore(
        conversation_id=conversation_id,
        customer_id=current.get("customer_id") if current else None,
        score=_CATEGORY_SCORE_MIDPOINT[category],
        category=category,
        intent=current.get("intent") if current else None,
        factors={"manual_override": 1},
        confidence=1.0,
    )
    ok = await store.save_lead_score(score)
    if not ok:
        raise HTTPException(status_code=500, detail="could not save lead override")
    return {
        "conversation_id": conversation_id,
        "category": str(category),
        "score": score.score,
    }


class CustomerUpdate(BaseModel):
    name: str | None = None
    company_name: str | None = None
    location: str | None = None
    phone: str | None = None
    email: str | None = None


@api.patch("/customers/{customer_id}")
async def update_customer(customer_id: str, update: CustomerUpdate) -> dict[str, Any]:
    """Let a rep correct or fill in a customer's own fields."""
    ok = await store.update_customer_fields(customer_id, update.model_dump(exclude_none=True))
    if not ok:
        raise HTTPException(status_code=500, detail="could not update customer")
    return {"id": customer_id, **update.model_dump(exclude_none=True)}


# ---------------------------------------------------------------------------
# Catalog management — how the client adds machines without a developer
# ---------------------------------------------------------------------------


@api.get("/machines")
async def machines(category: str | None = None) -> dict[str, Any]:
    rows = await store.list_machines(category=category)
    return {"count": len(rows), "machines": rows}


@api.get("/machines/documents")
async def machine_documents(machine_id: str | None = None) -> dict[str, Any]:
    rows = await store.list_machine_documents(machine_id=machine_id)
    return {"count": len(rows), "documents": rows}


@api.get("/machines/documents/{document_id}")
async def get_machine_document(document_id: str) -> dict[str, Any]:
    """Full content, for the dashboard's edit form — list_machine_documents
    deliberately omits content since the list view never needed it."""
    row = await store.get_machine_document(document_id)
    if row is None:
        raise HTTPException(status_code=404, detail="document not found")
    return row


class MachineDocumentUpdate(BaseModel):
    content: str


@api.patch("/machines/documents/{document_id}")
async def update_machine_document(
    document_id: str, update: MachineDocumentUpdate
) -> dict[str, Any]:
    """Correct an ingested document's content — most often an AI-structured
    product profile (see documents.structure_product_profile) that needs a
    human fix. Re-ingests into Qdrant so RAG reflects the edit immediately,
    not just the next time someone re-uploads."""
    row = await store.update_machine_document_content(document_id, update.content)
    if row is None:
        raise HTTPException(status_code=500, detail="could not update document")

    machine_id = row.get("machine_id")
    machine = await store.get_machine_by_id(machine_id) if machine_id else None
    reingested = False
    if machine:
        result = await documents.ingest_document(
            machine_name=machine["name"],
            category=machine["category"],
            text=update.content,
            machine_code=machine.get("machine_code"),
            machine_id=machine_id,
            price_range=machine.get("price_range"),
            source_filename=row.get("title"),
        )
        reingested = result.get("embedded", 0) > 0

    return {"id": document_id, "reingested": reingested}


@api.post("/machines/upload")
async def upload_machine_document(
    file: UploadFile = File(...),
    name: str = Form(...),
    category: str = Form(...),
    machine_code: str | None = Form(default=None),
    description: str | None = Form(default=None),
    price_range: str | None = Form(default=None),
    lead_time: str | None = Form(default=None),
    doc_type: DocumentType = Form(default=DocumentType.BROCHURE),
) -> dict[str, Any]:
    """Upload a brochure or spec sheet and make the machine answerable in chat.

    Extracts text, stores the machine and document, then chunks and embeds into
    Qdrant. The agent can answer questions about it immediately afterwards.
    """
    data = await file.read()
    try:
        result = await documents.add_machine_from_document(
            name=name,
            category=category,
            data=data,
            filename=file.filename or "upload",
            content_type=file.content_type,
            machine_code=machine_code,
            description=description,
            price_range=price_range,
            lead_time=lead_time,
            doc_type=doc_type,
        )
    except documents.ExtractionError as exc:
        # These messages are written to be shown to a user, not swallowed.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("upload_failed", extra={"filename": file.filename})
        raise HTTPException(status_code=500, detail="upload failed") from exc
    return result


@api.post("/machines/text")
async def add_machine_from_text(
    name: str = Form(...),
    category: str = Form(...),
    text: str = Form(...),
    machine_code: str | None = Form(default=None),
    price_range: str | None = Form(default=None),
) -> dict[str, Any]:
    """Add a machine by pasting its specifications — the fallback when a PDF is
    a scan, which text extraction cannot read."""
    machine_id = await store.upsert_machine(
        machine_code=machine_code or name.upper().replace(" ", "-")[:40],
        name=name,
        category=category,
        price_range=price_range,
    )
    await store.save_machine_document(
        machine_id=machine_id,
        doc_type=DocumentType.SPEC_SHEET,
        title=f"{name} (pasted)",
        content=text,
    )
    result = await documents.ingest_document(
        machine_name=name,
        category=category,
        text=text,
        machine_code=machine_code,
        machine_id=machine_id,
        price_range=price_range,
        source_filename="pasted",
    )
    return {"machine_id": machine_id, "name": name, **result}


@api.delete("/machines/{machine_id}")
async def delete_machine(machine_id: str) -> dict[str, Any]:
    """Delete a machine and everything that belongs to it: the machines row,
    its machine_documents (Postgres foreign key is ON DELETE CASCADE, so
    those go automatically), and its Qdrant chunks — this last one used to
    be left behind (documented as a known gap: 'delete_machine leaves its
    Qdrant chunks behind, unlike accessories'), which meant a deleted
    machine's specs/price could still surface in a customer's answer via
    RAG even though it no longer existed in the catalog."""
    ok = await store.delete_machine(machine_id)
    if not ok:
        raise HTTPException(status_code=500, detail="could not delete machine")
    await documents.delete_machine_from_index(machine_id)
    return {"id": machine_id, "deleted": True}


class MachineUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    description: str | None = None
    price_range: str | None = None
    lead_time: str | None = None
    is_active: bool | None = None


@api.patch("/machines/{machine_id}")
async def update_machine(machine_id: str, update: MachineUpdate) -> dict[str, Any]:
    """Let a rep correct a machine's own fields (price, description, etc.)
    without re-uploading its source document."""
    ok = await store.update_machine_fields(machine_id, update.model_dump(exclude_none=True))
    if not ok:
        raise HTTPException(status_code=500, detail="could not update machine")
    return {"id": machine_id, **update.model_dump(exclude_none=True)}


# ---------------------------------------------------------------------------
# Accessories/parts — manually maintained, no machine linkage yet (deferred;
# see app/models.py's Accessory docstring). RAG-ingested the same way
# machines are, just without the document-upload step.
# ---------------------------------------------------------------------------


class AccessoryCreate(BaseModel):
    machine_id: str
    name: str
    category: str | None = None
    description: str | None = None


class AccessoryUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    description: str | None = None
    is_active: bool | None = None


@api.get("/accessories")
async def accessories(machine_id: str | None = None) -> dict[str, Any]:
    """Every accessory, or just one machine's — the dashboard's per-machine
    accessories section always passes machine_id; each accessory belongs to
    exactly one machine (see app/models.py's Accessory docstring)."""
    rows = await store.list_accessories(machine_id=machine_id)
    return {"count": len(rows), "accessories": rows}


@api.post("/accessories")
async def create_accessory(payload: AccessoryCreate) -> dict[str, Any]:
    """Add an accessory/part under a specific machine by typing it in
    directly — no document upload, since these are entered manually per
    the current workflow."""
    accessory_id = await store.upsert_accessory(
        machine_id=payload.machine_id,
        name=payload.name,
        category=payload.category,
        description=payload.description,
    )
    if not accessory_id:
        raise HTTPException(status_code=500, detail="could not create accessory")
    result = await documents.ingest_accessory(
        accessory_id=accessory_id,
        name=payload.name,
        category=payload.category,
        description=payload.description,
    )
    return {"id": accessory_id, "name": payload.name, **result}


@api.patch("/accessories/{accessory_id}")
async def update_accessory(accessory_id: str, update: AccessoryUpdate) -> dict[str, Any]:
    """Edit an accessory's fields and re-index it, so RAG never serves a
    stale description after a correction."""
    fields = update.model_dump(exclude_none=True)
    ok = await store.update_accessory_fields(accessory_id, fields)
    if not ok:
        raise HTTPException(status_code=500, detail="could not update accessory")
    if fields.get("is_active") is False:
        # Deactivated — pull it out of RAG so it stops surfacing in chat.
        await documents.delete_accessory_from_index(accessory_id)
    elif any(key in fields for key in ("name", "category", "description")):
        rows = await store.list_accessories()
        row = next((r for r in rows if r.get("id") == accessory_id), None)
        if row:
            await documents.ingest_accessory(
                accessory_id=accessory_id,
                name=row.get("name", ""),
                category=row.get("category"),
                description=row.get("description"),
            )
    return {"id": accessory_id, **fields}


@api.delete("/accessories/{accessory_id}")
async def delete_accessory(accessory_id: str) -> dict[str, Any]:
    ok = await store.delete_accessory(accessory_id)
    if not ok:
        raise HTTPException(status_code=500, detail="could not delete accessory")
    await documents.delete_accessory_from_index(accessory_id)
    return {"id": accessory_id, "deleted": True}


app.include_router(api)
