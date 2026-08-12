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

from app import analytics, documents, intelligence, prompts, store
from app.agent import handle_message
from app.channels import TelegramAdapter, build_notification
from app.config import settings
from app.enums import Channel, DocumentType, HandoverStatus, LeadCategory
from app.logging_config import get_logger, setup_logging
from app.models import LeadScore, OutgoingMessage

setup_logging()
log = get_logger(__name__)

telegram = TelegramAdapter()

# Strong refs to detached tasks; without this the GC can cancel them mid-flight.
_background: set[asyncio.Task[None]] = set()


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info(
        "startup",
        extra={
            "model": settings.openai_model,
            "fallback": settings.groq_model,
            "collection": settings.qdrant_collection,
            "supabase": bool(settings.supabase_url),
            "telegram": bool(settings.telegram_bot_token),
        },
    )
    yield
    log.info("shutdown")


app = FastAPI(title="Divine Empire Sales Agent", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Render health check. Deliberately dependency-free: reporting unhealthy
    because Qdrant is briefly slow would take the whole service down."""
    return {"status": "ok", "service": "sales-agent"}


# ---------------------------------------------------------------------------
# Telegram webhook
# ---------------------------------------------------------------------------


async def _deliver(reply: Any, user_id: str, conversation_id: str) -> None:
    """Send the reply, then fire ops alerts. Order matters — the customer waits
    on their answer, the sales team does not."""
    await telegram.send(
        OutgoingMessage(channel=Channel.TELEGRAM, user_id=user_id, text=reply.text),
        keyboard=True,
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

        await telegram.send_chat_action(incoming.user_id)
        reply = await handle_message(incoming)
        await _deliver(reply, incoming.user_id, incoming.conversation_id)

        # Scoring, intent and summary (BRD §9-§11, §14) run only after the
        # customer has their reply — they are an LLM call the customer never
        # sees, and inline they would add seconds to every turn.
        if reply.model != "command":
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


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Reject unauthenticated dashboard requests.

    When no key is configured the API stays open — that keeps local development
    frictionless, and the deployment sets the key. Startup logs loudly if it is
    missing so an unprotected production deploy is visible.
    """
    if not settings.dashboard_api_key:
        return
    if x_api_key != settings.dashboard_api_key:
        log.warning("api_unauthorised")
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


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


@api.get("/conversations/{conversation_id}")
async def conversation(conversation_id: str) -> dict[str, Any]:
    """Full history plus the current summary for one conversation."""
    messages = await store.get_history(conversation_id, limit=200)
    return {
        "conversation_id": conversation_id,
        "summary": await store.get_summary(conversation_id),
        "messages": [m.model_dump(mode="json") for m in messages],
    }


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
async def override_lead_category(
    conversation_id: str, category: LeadCategory
) -> dict[str, Any]:
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
    ok = await store.delete_machine(machine_id)
    if not ok:
        raise HTTPException(status_code=500, detail="could not delete machine")
    return {"id": machine_id, "deleted": True}


app.include_router(api)
