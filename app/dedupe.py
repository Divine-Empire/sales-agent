"""Telegram webhook deduplication — Phase B of .claude/Addition.md.

Telegram retries a webhook call that didn't ack fast enough or returned a
non-200. Our webhook already acks in under a millisecond (main.py schedules
processing as a detached task), so retries should be rare — but "rare" is not
"never," and a duplicate here means a duplicate customer reply plus duplicate
tool side effects (a second save_lead, a second ops alert). This module makes
claiming an update_id atomic, so only the first delivery of a given update
is ever processed.

This is a second layer, not the only guarantee: database tools (save_lead,
etc.) should stay idempotent-ish in their own right. Redis being down must
never block a legitimate message — see the fail-open policy below.
"""

from __future__ import annotations

from app import redis_client
from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

# Comfortably longer than Telegram's own retry window, so a late retry is
# still caught; short enough not to matter for memory at our volume.
DEDUPE_TTL_SECONDS = 24 * 60 * 60


async def claim_update(update_id: int | str) -> bool:
    """Atomically claim this update_id. Returns True if this call is the
    first (and therefore legitimate) processor, False if it is a duplicate.

    Fails open: if dedup is disabled, or Redis is unreachable, this returns
    True — legitimate customer messages must never be silently dropped
    because of a Redis outage. The failure is still logged loudly so an
    operator can tell the difference between "no duplicates happened" and
    "we stopped checking."
    """
    if not settings.redis_dedupe_enabled:
        return True

    client = redis_client.get_client()
    if client is None:
        log.warning("dedupe_redis_unavailable", extra={"update_id": update_id})
        return True

    key = redis_client.build_key("telegram", "update", str(update_id))
    try:
        # SET key value NX EX ttl: succeeds (and returns True) only if the key
        # did not already exist. That single atomic call is the whole
        # claim — no separate GET-then-SET race is possible.
        claimed = await client.set(key, "1", nx=True, ex=DEDUPE_TTL_SECONDS)
    except Exception:
        log.exception("dedupe_check_failed", extra={"update_id": update_id})
        return True

    if not claimed:
        log.info("webhook_duplicate_skipped", extra={"update_id": update_id})
        return False
    return True
