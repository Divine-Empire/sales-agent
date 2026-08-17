"""Redis connection lifecycle — the operational layer described in
.claude/Addition.md.

Redis is never the system of record. Supabase remains canonical for every
customer, conversation, lead, and message; Qdrant remains canonical for
product knowledge. Everything in this module exists to make the agent faster
or safer under concurrency, and every caller must keep working if Redis is
disabled, unreachable, or slow.

Phase A only: this module wires the client, key helpers, and a readiness
check. No feature (dedupe, locks, rate limits, jobs, caches) reads or writes
through this module yet — those land in their own phases, each behind its own
settings flag, so enabling one never silently enables another.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as redis
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

# Bumped on any incompatible key-shape change (Addition.md §3 / §8.7). Changing
# this orphans old keys rather than reinterpreting them under a new schema.
KEY_SCHEMA_VERSION = "v1"
_KEY_PREFIX = f"de:{KEY_SCHEMA_VERSION}"

_client: Redis | None = None
_connect_attempted = False


def build_key(*parts: str) -> str:
    """Colon-separated, versioned, prefixed key (Addition.md §3).

    `de:v1:lock:conversation:{hash}` rather than a bare `conversation:{hash}` —
    the prefix and version make Redis's keyspace greppable and let a future
    schema change bump the version without touching old keys.
    """
    return ":".join((_KEY_PREFIX, *parts))


def get_client() -> Redis | None:
    """Return the shared client, or None if Redis is disabled/misconfigured.

    Construction only — does not verify connectivity, which is why this is
    sync and cheap to call from anywhere. `redis.asyncio.Redis` connections
    are lazy: pool entries are opened on first command, not here.
    """
    global _client, _connect_attempted
    if not settings.redis_enabled:
        return None
    if _client is not None:
        return _client
    if not settings.redis_url:
        if not _connect_attempted:
            log.warning("redis_enabled_but_no_url")
            _connect_attempted = True
        return None

    _connect_attempted = True
    try:
        _client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.redis_connect_timeout_seconds,
            socket_timeout=settings.redis_read_timeout_seconds,
            max_connections=settings.redis_max_connections,
        )
    except Exception:
        # Malformed URL, bad scheme, etc. — construction itself failed, not a
        # network error. Every caller degrades the same way either case.
        log.exception("redis_client_construction_failed")
        _client = None
    return _client


async def close() -> None:
    """Called from the app's lifespan shutdown. Idempotent."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            log.exception("redis_close_failed")
        _client = None


async def ping() -> bool:
    """Used by the /ready diagnostic and by startup logging. Never raises."""
    client = get_client()
    if client is None:
        return False
    try:
        return bool(await client.ping())
    except RedisError:
        log.warning("redis_ping_failed")
        return False
    except Exception:
        log.exception("redis_ping_error")
        return False


@asynccontextmanager
async def safe(operation: str, conversation_id: str | None = None):
    """Wrap a Redis operation so any failure degrades instead of propagating.

    Every feature module (dedupe, locks, rate limits, ...) should use this
    rather than catching redis.RedisError inline — one place decides that a
    Redis error is always a soft failure, never a customer-facing one, and one
    place logs it consistently so `redis_errors_total` is greppable across
    every feature.

    Usage:
        async with safe("dedupe_check", conversation_id) as ok:
            if ok:
                ... do the Redis-backed thing ...
        # falls through with ok=False on any Redis error; caller decides the
        # fail-open/fail-closed behavior for that specific operation.
    """
    try:
        yield True
    except RedisError:
        log.warning(
            "redis_operation_failed",
            extra={"operation": operation, "conversation_id": conversation_id},
        )
    except Exception:
        log.exception(
            "redis_operation_error",
            extra={"operation": operation, "conversation_id": conversation_id},
        )


async def readiness() -> dict[str, Any]:
    """Separate from /health on purpose (Addition.md Phase A acceptance
    criteria): liveness must never depend on Redis, but operators still need
    to see its state."""
    if not settings.redis_enabled:
        return {"enabled": False, "connected": False}
    return {"enabled": True, "connected": await ping()}
