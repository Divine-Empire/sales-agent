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


# The portal's list endpoint is backed by Supabase, which caps any single
# response at 1000 rows. The portal derives `hasMore` by over-fetching one row
# (`limit + 1`), so asking for exactly 1000 makes that probe row get clamped
# away and `hasMore` reads false even when thousands remain. Staying below the
# ceiling keeps the probe intact, so paging never stalls.
_PORTAL_PAGE_MAX = 500


async def list_conversations(
    limit: int = 30, cursor: str | None = None, filter_: str | None = None
) -> dict[str, Any]:
    """Conversation list, newest activity first.

    `limit` may exceed the portal's 1000-row ceiling: this pages through with
    the portal's own `nextCursor` (a `last_message_at` timestamp) and
    concatenates, so the dashboard can render one growing list rather than
    windowed pages that make earlier rows disappear.

    `has_more`/`next_cursor` describe the position after the last row
    returned, so a caller can continue from there.
    """
    if filter_ == "all":
        filter_ = None

    collected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    next_cursor = cursor
    has_more = False

    try:
        async with httpx.AsyncClient(timeout=settings.whatsapp_portal_timeout_seconds) as client:
            while len(collected) < limit:
                page_size = min(_PORTAL_PAGE_MAX, limit - len(collected))
                params: dict[str, Any] = {"limit": page_size}
                if next_cursor:
                    params["cursor"] = next_cursor
                if filter_:
                    params["filter"] = filter_

                response = await client.get(f"{_base()}/api/conversations/list", params=params)
                if response.status_code != 200:
                    log.warning(
                        "portal_conversations_failed",
                        extra={"status": response.status_code, "body": response.text[:200]},
                    )
                    # Keep whatever earlier pages succeeded rather than
                    # discarding a partly-built list.
                    if collected:
                        break
                    return {
                        "conversations": [],
                        "has_more": False,
                        "next_cursor": None,
                        "available": False,
                    }

                payload = response.json() or {}
                rows = payload.get("conversations") or []
                if not rows:
                    has_more = False
                    break

                for row in rows:
                    # Conversations sharing a last_message_at can straddle a
                    # cursor boundary and repeat; drop duplicates so the UI
                    # never renders the same thread twice.
                    row_id = row.get("id")
                    if row_id and row_id in seen_ids:
                        continue
                    if row_id:
                        seen_ids.add(row_id)
                    collected.append(row)

                has_more = bool(payload.get("hasMore"))
                next_cursor = payload.get("nextCursor")
                if not has_more or not next_cursor:
                    break

        return {
            "conversations": collected,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "available": True,
        }
    except Exception:
        log.exception("portal_conversations_error")
        if collected:
            return {
                "conversations": collected,
                "has_more": False,
                "next_cursor": None,
                "available": True,
            }
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
