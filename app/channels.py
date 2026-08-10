"""Channel adapters — the only code that knows which chat platform we are on.

Adapters translate platform payloads to and from IncomingMessage/OutgoingMessage.
The agent core never sees a Telegram `update` or a WhatsApp `entry`. That
boundary is what makes the WhatsApp migration an adapter swap plus one route,
rather than a rewrite.

Raw Bot API over httpx — no python-telegram-bot. A framework here would add an
abstraction we cannot debug live during a client demo.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any

import httpx

from app import commands
from app.config import settings
from app.enums import Channel
from app.logging_config import get_logger
from app.models import IncomingMessage, OutgoingMessage

log = get_logger(__name__)

# Telegram's hard limit is 4096; leave headroom for the split suffix.
TELEGRAM_MAX_CHARS = 4000


class ChannelAdapter(ABC):
    """What every channel must provide. Nothing platform-specific escapes this."""

    channel: Channel

    @abstractmethod
    def parse(self, payload: dict[str, Any]) -> IncomingMessage | None:
        """Platform webhook payload -> IncomingMessage, or None to ignore."""

    @abstractmethod
    async def send(self, message: OutgoingMessage) -> bool:
        """Deliver a reply. Returns False on failure rather than raising."""


def to_telegram_html(text: str) -> str:
    """Render model output as Telegram HTML.

    HTML rather than MarkdownV2 on purpose: MarkdownV2 requires escaping
    eighteen characters, and model output will eventually contain an unescaped
    '.' or '-' that makes Telegram reject the entire message. HTML needs three.

    Escaping happens first, so any '<' the model wrote is inert before we add
    tags of our own. Only **bold** is converted — that is the one marker the
    model actually emits, and every tag we do not create is a tag that cannot
    be malformed. `send()` falls back to plain text if Telegram still objects.
    """
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # **bold** and *bold* -> <b>. Non-greedy, single-line, so an unmatched
    # asterisk is left as literal text rather than swallowing the message.
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)", r"<b>\1</b>", escaped)
    return escaped


def split_message(text: str, limit: int = TELEGRAM_MAX_CHARS) -> list[str]:
    """Split a long reply on paragraph, then line, then hard boundaries.

    Splitting mid-sentence looks broken to a customer, so we prefer the largest
    natural boundary that fits.
    """
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


class TelegramAdapter(ChannelAdapter):
    """Telegram Bot API over raw HTTP."""

    channel = Channel.TELEGRAM

    def __init__(self, token: str | None = None):
        self.token = token or settings.telegram_bot_token
        self.api_base = f"https://api.telegram.org/bot{self.token}"

    def parse(self, payload: dict[str, Any]) -> IncomingMessage | None:
        """Extract a text message from an update.

        Non-text updates (photos, stickers, edits, callbacks) are ignored for the
        prototype — logged and skipped, never an error.
        """
        message = payload.get("message") or payload.get("edited_message")
        if not isinstance(message, dict):
            log.info("telegram_update_ignored", extra={"reason": "no_message"})
            return None

        text = message.get("text") or message.get("caption")
        if not text:
            log.info(
                "telegram_update_ignored",
                extra={
                    "reason": "non_text",
                    "kinds": [k for k in ("photo", "sticker", "voice", "document") if k in message],
                },
            )
            return None

        sender = message.get("from") or {}
        user_id = sender.get("id") or (message.get("chat") or {}).get("id")
        if user_id is None:
            log.warning("telegram_update_ignored", extra={"reason": "no_user_id"})
            return None

        name_parts = [sender.get("first_name"), sender.get("last_name")]
        sender_name = " ".join(p for p in name_parts if p) or sender.get("username")

        return IncomingMessage(
            channel=self.channel,
            user_id=str(user_id),
            text=text,
            sender_name=sender_name,
            raw={
                "chat_id": (message.get("chat") or {}).get("id", user_id),
                "message_id": message.get("message_id"),
                "username": sender.get("username"),
            },
        )

    async def send(self, message: OutgoingMessage, keyboard: bool = False) -> bool:
        """Send a reply, splitting anything over the platform limit.

        `keyboard` attaches the quick-reply suggestions above the input box.
        Only the last chunk carries it, or a split message would flash the
        keyboard several times.
        """
        if not self.token:
            log.error("telegram_not_configured")
            return False
        chunks = split_message(message.text)
        markup = (
            {
                "keyboard": [[{"text": t} for t in row] for row in commands.QUICK_REPLIES],
                "resize_keyboard": True,
                "is_persistent": True,
            }
            if keyboard
            else None
        )
        try:
            async with httpx.AsyncClient(timeout=settings.telegram_timeout_seconds) as client:
                for index, chunk in enumerate(chunks):
                    payload: dict[str, Any] = {
                        "chat_id": message.user_id,
                        "text": to_telegram_html(chunk),
                        "parse_mode": "HTML",
                    }
                    if markup and index == len(chunks) - 1:
                        payload["reply_markup"] = markup
                    response = await client.post(
                        f"{self.api_base}/sendMessage",
                        json=payload,
                    )
                    if response.status_code == 400:
                        # Formatting was rejected. A message must never be lost
                        # to a stray tag — resend it as plain text.
                        log.warning(
                            "telegram_html_rejected",
                            extra={"conversation_id": message.conversation_id},
                        )
                        payload["text"] = chunk
                        payload.pop("parse_mode", None)
                        response = await client.post(f"{self.api_base}/sendMessage", json=payload)
                    if response.status_code != 200:
                        log.error(
                            "telegram_send_failed",
                            extra={
                                "conversation_id": message.conversation_id,
                                "status": response.status_code,
                                "body": response.text[:200],
                            },
                        )
                        return False
            log.info(
                "telegram_sent",
                extra={"conversation_id": message.conversation_id, "parts": len(chunks)},
            )
            return True
        except Exception:
            log.exception("telegram_send_error", extra={"conversation_id": message.conversation_id})
            return False

    async def send_chat_action(self, user_id: str, action: str = "typing") -> None:
        """Typing indicator. Best-effort — never blocks or fails a reply."""
        if not self.token:
            return
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{self.api_base}/sendChatAction",
                    json={"chat_id": user_id, "action": action},
                )
        except Exception:
            log.debug("telegram_chat_action_failed", extra={"user_id": user_id})

    async def notify_ops(self, text: str) -> bool:
        """Alert the ops chat about a lead or handoff."""
        if not settings.ops_chat_id:
            log.warning("ops_chat_not_configured")
            return False
        return await self.send(
            OutgoingMessage(channel=self.channel, user_id=settings.ops_chat_id, text=text)
        )

    async def set_webhook(self, url: str, secret: str | None = None) -> dict[str, Any]:
        """Register the webhook. The secret token is echoed back by Telegram on
        every request, which is how we authenticate inbound calls."""
        payload: dict[str, Any] = {
            "url": url,
            "allowed_updates": ["message"],
            "drop_pending_updates": True,
        }
        if secret:
            payload["secret_token"] = secret
        async with httpx.AsyncClient(timeout=settings.telegram_timeout_seconds) as client:
            response = await client.post(f"{self.api_base}/setWebhook", json=payload)
        result: dict[str, Any] = response.json()
        log.info("telegram_webhook_set", extra={"url": url, "ok": result.get("ok")})
        return result

    async def get_me(self) -> dict[str, Any]:
        """Verify the token and return the bot identity."""
        async with httpx.AsyncClient(timeout=settings.telegram_timeout_seconds) as client:
            response = await client.get(f"{self.api_base}/getMe")
        return response.json()


class WhatsAppAdapter(ChannelAdapter):
    """WhatsApp Business Cloud API — documented stub.

    Not implemented: the client releases credentials only after the demo. The
    payload mapping below is the whole job when they arrive; everything else in
    the system already speaks IncomingMessage/OutgoingMessage.

    INBOUND (POST from Meta):
        {
          "entry": [{
            "changes": [{
              "value": {
                "messaging_product": "whatsapp",
                "metadata": {"phone_number_id": "<PHONE_NUMBER_ID>"},
                "contacts": [{"profile": {"name": "Rajesh"}, "wa_id": "919876543210"}],
                "messages": [{
                  "from": "919876543210",     -> IncomingMessage.user_id
                  "id": "wamid.XXX",          -> raw["message_id"]
                  "timestamp": "1234567890",
                  "type": "text",
                  "text": {"body": "..."}     -> IncomingMessage.text
                }]
              }
            }]
          }]
        }

        conversation_id becomes "whatsapp:919876543210" — the same scheme as
        Telegram, so store, agent, and reporting need no changes.

        Non-text types (image, audio, document, location, button, interactive)
        are ignored exactly as Telegram non-text updates are.

    OUTBOUND (POST https://graph.facebook.com/v21.0/<PHONE_NUMBER_ID>/messages):
        headers: {"Authorization": "Bearer <ACCESS_TOKEN>"}
        {
          "messaging_product": "whatsapp",
          "recipient_type": "individual",
          "to": "<user_id>",
          "type": "text",
          "text": {"preview_url": false, "body": "<text>"}
        }

    DIFFERENCES FROM TELEGRAM THAT MATTER:
      - Message limit is 4096 characters for text bodies; split_message applies.
      - Outside a 24-hour customer service window, only pre-approved template
        messages may be sent. Free-form replies to an inbound message are fine,
        which covers this agent's entire flow.
      - Webhook verification is a GET with hub.mode / hub.verify_token /
        hub.challenge; the handler must echo hub.challenge back as plain text.
      - Every POST carries X-Hub-Signature-256, an HMAC-SHA256 of the raw body
        using the app secret. This MUST be verified — unlike Telegram's simple
        secret token header.
      - Meta retries undelivered webhooks, so handlers must be idempotent.

    WHAT TO IMPLEMENT HERE:
      1. parse()  — walk entry[0].changes[0].value.messages[0] per the map above
      2. send()   — POST the outbound body, honouring WHATSAPP_ACCESS_TOKEN and
                    WHATSAPP_PHONE_NUMBER_ID from settings
      3. One route in main.py: GET for verification, POST for messages
    """

    channel = Channel.WHATSAPP

    def parse(self, payload: dict[str, Any]) -> IncomingMessage | None:
        raise NotImplementedError(
            "WhatsAppAdapter is a documented stub — see the class docstring for the "
            "payload mapping. Implement when the client provides Cloud API credentials."
        )

    async def send(self, message: OutgoingMessage) -> bool:
        raise NotImplementedError(
            "WhatsAppAdapter is a documented stub — see the class docstring for the "
            "outbound request shape."
        )


# ---------------------------------------------------------------------------
# Ops notifications
# ---------------------------------------------------------------------------


def format_lead_alert(data: dict[str, Any], conversation_id: str) -> str:
    """Human-readable lead alert for the ops chat."""
    lines = [
        "🔔 NEW LEAD",
        f"Name: {data.get('name') or '—'}",
        f"Company: {data.get('company') or '—'}",
        f"Product: {data.get('product_interest') or '—'}",
    ]
    for label, key in (
        ("Quantity", "quantity"),
        ("Budget", "budget"),
        ("Timeline", "timeline"),
        ("Location", "location"),
        ("Requirements", "requirements"),
    ):
        if data.get(key):
            lines.append(f"{label}: {data[key]}")
    lines.append(f"Conversation: {conversation_id}")
    return "\n".join(lines)


def format_handoff_alert(reason: str, context: str, conversation_id: str) -> str:
    """Handoff alert. The context is the point — a rep must be able to pick this
    up without reading the transcript."""
    return "\n".join(
        [
            "🚨 HUMAN HANDOFF REQUESTED",
            f"Reason: {reason.replace('_', ' ').title()}",
            f"Context: {context or '—'}",
            f"Conversation: {conversation_id}",
        ]
    )


def build_notification(note: dict[str, Any], conversation_id: str) -> str | None:
    """Turn an agent notification into ops-chat text."""
    kind = note.get("type")
    if kind == "lead":
        return format_lead_alert(note.get("data") or {}, conversation_id)
    if kind == "handoff":
        return format_handoff_alert(
            note.get("reason", "other"), note.get("context", ""), conversation_id
        )
    return None


# Registry so main.py can route by channel without importing concrete classes.
ADAPTERS: dict[Channel, ChannelAdapter] = {
    Channel.TELEGRAM: TelegramAdapter(),
    Channel.WHATSAPP: WhatsAppAdapter(),
}


def get_adapter(channel: Channel) -> ChannelAdapter:
    return ADAPTERS[channel]
