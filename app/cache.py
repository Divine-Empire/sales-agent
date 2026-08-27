"""Exact hot-read caching — Phase F of .claude/Addition.md.

Cache-aside: Redis -> source -> Redis. Every helper here degrades to a plain
uncached read when the cache is disabled, Redis is unreachable, or a payload
fails to (de)serialize — a cache is a latency optimization, never a
dependency. Nothing here ever decides authorization; do not put anything
auth-shaped (API keys, session state, opt-out status) through this module —
opt-outs in particular are deliberately excluded, per the plan ("never allow
a stale negative cache to message an opted-out customer").

Stampede protection: a short single-flight lock so N concurrent misses for
the same key don't all hit Supabase/Qdrant at once — only the lock holder
fetches; everyone else waits briefly and retries the cache before falling
back to a direct (uncached) fetch if the wait times out.

Invalidation is direct (delete-on-write from the mutating call site), not a
version counter — this system has no multi-instance cache-consistency need
yet (single Redis, one small deployment), so the plan's `catalog_version`
suggestion would be complexity with no present payoff. If that changes,
version keys are a natural upgrade from here without touching call sites
that only ever call `get_or_set`/`invalidate`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable

from app import redis_client
from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)


def _normalize_query(query: str) -> str:
    return " ".join(query.strip().lower().split())


def _hash(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode()).hexdigest()[:20]


async def get_or_set[T](
    key: str,
    ttl_seconds: int,
    fetch: Callable[[], Awaitable[T]],
    *,
    enabled: bool = True,
) -> T:
    """Cache-aside read with single-flight stampede protection.

    `fetch` is only ever called 0 or 1 times per call to `get_or_set` on a
    cache hit or successful lock acquisition; on lock contention this polls
    the cache briefly and falls back to an uncached `fetch()` if the other
    fetcher hasn't finished in time — correctness never depends on the cache.
    """
    if not enabled or not settings.redis_cache_enabled:
        return await fetch()

    client = redis_client.get_client()
    if client is None:
        return await fetch()

    try:
        cached = await client.get(key)
    except Exception:
        log.warning("cache_read_failed", extra={"key": key})
        return await fetch()

    if cached is not None:
        try:
            return json.loads(cached)
        except (json.JSONDecodeError, TypeError):
            log.warning("cache_corrupt_value", extra={"key": key})
            # fall through to a normal fetch-and-repopulate below

    lock_key = redis_client.build_key("cache", "lock", key)
    lock_ttl_ms = max(1, int(settings.cache_stampede_lock_ttl_seconds * 1000))
    try:
        got_lock = await client.set(lock_key, "1", nx=True, px=lock_ttl_ms)
    except Exception:
        log.warning("cache_lock_acquire_failed", extra={"key": key})
        got_lock = False

    if not got_lock:
        # Someone else is already fetching this key — wait briefly for them
        # rather than also hitting the source.
        deadline = asyncio.get_event_loop().time() + settings.cache_stampede_wait_seconds
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.1)
            try:
                cached = await client.get(key)
            except Exception:
                break
            if cached is not None:
                try:
                    return json.loads(cached)
                except (json.JSONDecodeError, TypeError):
                    break
        return await fetch()

    try:
        value = await fetch()
        try:
            await client.set(key, json.dumps(value), ex=ttl_seconds)
        except Exception:
            log.warning("cache_write_failed", extra={"key": key})
        return value
    finally:
        try:
            await client.delete(lock_key)
        except Exception:
            pass


async def invalidate(*keys: str) -> None:
    """Delete-on-write. Best-effort — a failed invalidation just means the
    TTL is the backstop instead of an immediate refresh."""
    if not settings.redis_cache_enabled:
        return
    client = redis_client.get_client()
    if client is None:
        return
    try:
        await client.delete(*keys)
    except Exception:
        log.warning("cache_invalidate_failed", extra={"keys": keys})


async def invalidate_namespace(*namespace_parts: str) -> None:
    """Clear every key under a namespace via SCAN (never KEYS — Addition.md
    §4 explicitly forbids it as a blocking full-keyspace scan). Reserved for
    rare, wholesale, offline events — the only current caller is RAG
    ingestion (`uv run python -m app.rag`), which already replaces the whole
    Qdrant collection at once, so there is no finer-grained invalidation to
    do here; a full re-ingest naturally invalidates every cached RAG result.
    """
    if not settings.redis_cache_enabled:
        return
    client = redis_client.get_client()
    if client is None:
        return
    prefix = redis_client.build_key("cache", *namespace_parts)
    try:
        cursor = 0
        cleared = 0
        while True:
            cursor, keys = await client.scan(cursor, match=f"{prefix}*", count=200)
            if keys:
                await client.delete(*keys)
                cleared += len(keys)
            if cursor == 0:
                break
        log.info("cache_namespace_invalidated", extra={"prefix": prefix, "cleared": cleared})
    except Exception:
        log.warning("cache_namespace_invalidate_failed", extra={"prefix": prefix})


# ---------------------------------------------------------------------------
# Key builders — one place per candidate so call sites and invalidation sites
# never have to independently agree on a key shape.
# ---------------------------------------------------------------------------


def customer_key(channel: str, channel_user_id: str) -> str:
    return redis_client.build_key("cache", "customer", channel, channel_user_id)


def summary_key(conversation_id: str) -> str:
    return redis_client.build_key("cache", "summary", conversation_id)


def machine_key(machine_code: str) -> str:
    return redis_client.build_key("cache", "machine", machine_code.lower())


def machines_list_key(category: str | None) -> str:
    return redis_client.build_key("cache", "machines", category or "all")


def accessories_list_key(category: str | None) -> str:
    return redis_client.build_key("cache", "accessories", category or "all")


def rag_key(query: str) -> str:
    return redis_client.build_key("cache", "rag", _hash(_normalize_query(query)))


def dashboard_key(name: str) -> str:
    return redis_client.build_key("cache", "dashboard", name)


def wa_conversation_key(phone: str) -> str:
    """whatsapp-portal's conversation id for a phone number.

    Safe to cache for a long time: the portal creates one conversation row per
    (user, contact) and never rotates its id — `get-or-create` returns the same
    uuid forever. Caching it removes a 1.7-4.1s round trip from every AI reply.
    """
    return redis_client.build_key("cache", "wa", "conv", phone)
