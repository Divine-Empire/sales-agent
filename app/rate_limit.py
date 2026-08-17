"""Rate limiting — Phase D of .claude/Addition.md.

Two independent scopes, both atomic fixed-window counters (INCR + EXPIRE in
one Lua call, so there's no race between the increment and setting the
window's TTL — no `KEYS` scan involved either):

- Per-customer/channel: protects LLM cost and stops a stuck client loop from
  hammering the agent. Fails OPEN — a Redis outage must never block a real
  customer message, per the plan's failure policy for the chat path.
- Per-dashboard-API-key/route: protects the operator API from abuse. Fails
  CLOSED — if Redis is down we can't enforce the limit, and this endpoint's
  own auth/rate posture matters more than availability for an internal tool.

Two windows for the customer scope (10/min steady, 30/5min burst) match the
plan's initial policy table; either one tripping blocks the turn.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app import redis_client
from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

# INCR then, only on the first hit in the window, set the expiry — avoids a
# second round trip and avoids ever leaving a key without a TTL.
_INCR_SCRIPT = """
local count = redis.call("incr", KEYS[1])
if count == 1 then
    redis.call("expire", KEYS[1], ARGV[1])
end
return count
"""


@dataclass
class RateLimitResult:
    allowed: bool
    limited_by: str | None = None  # which window tripped, for logging/metrics


async def _incr_window(key: str, ttl_seconds: int) -> int | None:
    """Returns the post-increment count, or None if Redis is unavailable."""
    client = redis_client.get_client()
    if client is None:
        return None
    try:
        return int(await client.eval(_INCR_SCRIPT, 1, key, str(ttl_seconds)))
    except Exception:
        log.exception("rate_limit_check_failed", extra={"key": key})
        return None


async def check_customer(channel: str, user_id: str) -> RateLimitResult:
    """10 messages/minute steady, 30/5 minutes burst (Addition.md §4 policy
    table). Fails open: a Redis outage lets the message through rather than
    silently dropping a real customer."""
    if not settings.redis_rate_limit_enabled:
        return RateLimitResult(allowed=True)

    minute_key = redis_client.build_key("rate", "customer", channel, user_id, "1m")
    burst_key = redis_client.build_key("rate", "customer", channel, user_id, "5m")

    minute_count = await _incr_window(minute_key, 60)
    if minute_count is None:
        log.warning("rate_limit_redis_unavailable", extra={"scope": "customer"})
        return RateLimitResult(allowed=True)
    if minute_count > settings.rate_limit_customer_per_minute:
        return RateLimitResult(allowed=False, limited_by="per_minute")

    burst_count = await _incr_window(burst_key, 300)
    if burst_count is None:
        return RateLimitResult(allowed=True)
    if burst_count > settings.rate_limit_customer_burst_per_5min:
        return RateLimitResult(allowed=False, limited_by="burst_5min")

    return RateLimitResult(allowed=True)


async def check_dashboard(api_key: str, route: str) -> RateLimitResult:
    """Route-scoped limit for the dashboard API. Fails CLOSED: if Redis is
    unavailable we can't verify the caller is within limits, and this is an
    authenticated internal surface, not a customer-facing chat — availability
    is not the priority the plan assigns it (Addition.md §4 failure policy)."""
    if not settings.redis_rate_limit_enabled:
        return RateLimitResult(allowed=True)

    # Never put the live API key itself into a Redis key (Addition.md §3/§5)
    # — hash it, same as any other secret-derived identifier.
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:16]
    key = redis_client.build_key("rate", "api", key_hash, route, "1m")
    count = await _incr_window(key, 60)
    if count is None:
        log.warning("rate_limit_redis_unavailable", extra={"scope": "dashboard", "route": route})
        return RateLimitResult(allowed=False, limited_by="redis_unavailable")
    if count > settings.rate_limit_dashboard_per_minute:
        return RateLimitResult(allowed=False, limited_by="per_minute")
    return RateLimitResult(allowed=True)
