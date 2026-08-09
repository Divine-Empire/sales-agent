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

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app import prompts, store
from app.agent import handle_message
from app.channels import TelegramAdapter, build_notification
from app.config import settings
from app.enums import Channel, HandoverStatus
from app.logging_config import get_logger, setup_logging
from app.models import OutgoingMessage

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
    await telegram.send(OutgoingMessage(channel=Channel.TELEGRAM, user_id=user_id, text=reply.text))
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


# ---------------------------------------------------------------------------
# Read API — backs the dashboard and reports (BRD §15, §16)
#
# UNAUTHENTICATED. These expose customer PII and lead data and must not reach a
# public deployment without auth. Tracked as the largest known gap in
# docs/build-plan.md.
# ---------------------------------------------------------------------------

api = APIRouter(prefix="/api", tags=["dashboard"])


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


app.include_router(api)
