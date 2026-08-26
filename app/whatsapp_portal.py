"""Read-only client for the whatsapp-portal's own HTTP API.

The portal (its own Vercel app + Supabase) owns every WhatsApp conversation:
inbound messages arrive at its Meta webhook, and our AI replies are sent
through its `/api/send-message` precisely so it stays the single writer of
`whatsapp_portal_messages`. See `app/channels.py`'s WhatsAppPortalAdapter.

This module is the read side of that relationship, and it exists so the CRM
dashboard never touches the portal's database. The dashboard already speaks to
exactly one backend (this service) with one API key; giving it a second
database credential and a copy of the portal's query logic would trade a clean
boundary for a little latency. So: dashboard -> this service -> portal's API.

Read-only on purpose. Nothing here writes; operator sending is deliberately
not built until permissions and an audit trail are designed.

Every function degrades to an empty/None result rather than raising — a portal
outage should leave the WhatsApp tab empty with an honest "unavailable" state,
never break the whole dashboard page.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)


def _base() -> str:
    return settings.whatsapp_portal_base_url.rstrip("/")


async def list_conversations(
    limit: int = 30, cursor: str | None = None, filter_: str | None = None
) -> dict[str, Any]:
    """Conversation list, newest activity first.

    Mirrors the portal's own pagination contract (`hasMore`/`nextCursor`
    keyed on `last_message_at`) rather than inventing a different one, so the
    dashboard can page without this service holding any state.
    """
    params: dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    if filter_ and filter_ != "all":
        params["filter"] = filter_

    try:
        async with httpx.AsyncClient(timeout=settings.whatsapp_portal_timeout_seconds) as client:
            response = await client.get(f"{_base()}/api/conversations/list", params=params)
        if response.status_code != 200:
            log.warning(
                "portal_conversations_failed",
                extra={"status": response.status_code, "body": response.text[:200]},
            )
            return {"conversations": [], "has_more": False, "next_cursor": None, "available": False}
        payload = response.json() or {}
        return {
            "conversations": payload.get("conversations") or [],
            "has_more": bool(payload.get("hasMore")),
            "next_cursor": payload.get("nextCursor"),
            "available": True,
        }
    except Exception:
        log.exception("portal_conversations_error")
        return {"conversations": [], "has_more": False, "next_cursor": None, "available": False}


async def get_messages(conversation_id: str, limit: int = 100) -> dict[str, Any] | None:
    """One thread's messages, oldest-first, plus the contact for the header.

    Returns None when the portal says the conversation does not exist, so the
    caller can answer 404 rather than showing an empty thread that looks like
    a load failure.
    """
    try:
        async with httpx.AsyncClient(timeout=settings.whatsapp_portal_timeout_seconds) as client:
            response = await client.get(
                f"{_base()}/api/conversations/{conversation_id}/messages",
                params={"limit": limit},
            )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            log.warning(
                "portal_messages_failed",
                extra={
                    "conversation": conversation_id,
                    "status": response.status_code,
                    "body": response.text[:200],
                },
            )
            return {"conversation": None, "messages": [], "available": False}
        payload = response.json() or {}
        return {
            "conversation": payload.get("conversation"),
            "messages": payload.get("messages") or [],
            "available": True,
        }
    except Exception:
        log.exception("portal_messages_error", extra={"conversation": conversation_id})
        return {"conversation": None, "messages": [], "available": False}


async def search_conversations(query: str, limit: int = 30) -> dict[str, Any]:
    """Server-side search over contacts/messages, delegated to the portal."""
    if not query.strip():
        return {"conversations": [], "available": True}
    try:
        async with httpx.AsyncClient(timeout=settings.whatsapp_portal_timeout_seconds) as client:
            response = await client.get(
                f"{_base()}/api/conversations/search",
                params={"q": query, "limit": limit},
            )
        if response.status_code != 200:
            log.warning("portal_search_failed", extra={"status": response.status_code})
            return {"conversations": [], "available": False}
        payload = response.json() or {}
        # The portal's search route answers with `results`, not `conversations`
        # — normalise here so the dashboard sees one shape from both endpoints.
        return {"conversations": payload.get("results") or [], "available": True}
    except Exception:
        log.exception("portal_search_error")
        return {"conversations": [], "available": False}
