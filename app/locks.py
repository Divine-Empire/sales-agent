"""Per-conversation distributed lock — Phase C of .claude/Addition.md.

Render can run multiple workers. Two webhook deliveries for the same
conversation arriving close together (a fast double-send, a retry racing the
original) must not read the same history and both act on stale state, or
interleave tool calls out of order. This module serializes the
bootstrap/history/RAG/LLM/tool sequence per conversation_id while leaving
unrelated conversations fully concurrent.

Fail-open: if Redis is unavailable, the turn proceeds without the lock and
the degraded state is logged loudly — losing ordering protection is better
than losing the reply entirely.
"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager

from app import redis_client
from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

# Ownership-checked release: only delete the key if it still holds the token
# we set. Without this a lock whose lease expired mid-turn could be deleted
# by us after some other worker has already acquired it for the next turn.
_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


class LockNotAcquired(Exception):
    """Raised internally when the bounded wait expires without acquiring."""


@asynccontextmanager
async def conversation_lock(conversation_id: str):
    """Acquire the lock for `conversation_id`, yield, then release it.

    Yields True if the lock was actually held (Redis available, acquired
    within the bounded wait) and False if we proceeded without it — either
    because locks are disabled/Redis is down (fail-open) or because another
    worker already holds it and the wait expired (caller should treat this
    as "busy," not "proceed").

    Usage:
        async with conversation_lock(conversation_id) as acquired:
            if not acquired and <some other worker currently owns it>:
                return AgentReply(text=prompts.BUSY_MESSAGE, model="none")
            ... normal turn ...
    """
    if not settings.redis_locks_enabled:
        yield True
        return

    client = redis_client.get_client()
    if client is None:
        log.warning("lock_redis_unavailable", extra={"conversation_id": conversation_id})
        yield True
        return

    key = redis_client.build_key("lock", "conversation", conversation_id)
    token = secrets.token_hex(16)
    lease_ms = int(settings.redis_lock_lease_seconds * 1000)

    held = False
    try:
        deadline = asyncio.get_event_loop().time() + settings.redis_lock_wait_seconds
        while True:
            acquired = await client.set(key, token, nx=True, px=lease_ms)
            if acquired:
                held = True
                break
            if asyncio.get_event_loop().time() >= deadline:
                break
            await asyncio.sleep(settings.redis_lock_retry_interval_seconds)
    except Exception:
        log.exception("lock_acquire_failed", extra={"conversation_id": conversation_id})
        yield True
        return

    if not held:
        log.info("lock_contended", extra={"conversation_id": conversation_id})
        yield False
        return

    try:
        yield True
    finally:
        try:
            await client.eval(_RELEASE_SCRIPT, 1, key, token)
        except Exception:
            log.exception("lock_release_failed", extra={"conversation_id": conversation_id})
