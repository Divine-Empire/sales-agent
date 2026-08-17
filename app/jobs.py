"""Durable post-reply jobs — Phase E of .claude/Addition.md.

Today, post-reply intelligence (lead scoring, intent classification,
conversation summary) runs as a plain continuation inside the same detached
asyncio task that handled the webhook. If the dyno restarts or the task is
cancelled between the customer's reply and that continuation finishing, the
work is silently lost — nothing durable ever recorded that it was supposed to
happen.

This module replaces that with one versioned Redis Stream and a consumer
group: `enqueue()` (called from the webhook path, in `app/main.py`) durably
records the job before returning; `app/worker.py` (a separate long-running
process, not part of the web dyno) claims entries, does the work, and only
acks after it durably succeeds. A crashed worker's unacked entries are
reclaimed by the next worker via `reclaim_stale()` instead of being lost.

Only intelligence analysis is queued in this first iteration, per the plan
("do not queue the customer-facing reply") — ops notifications and the reply
itself stay on the immediate synchronous path in app/main.py.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app import redis_client
from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

STREAM_KEY = redis_client.build_key("stream", "jobs")
DEAD_LETTER_KEY = redis_client.build_key("stream", "jobs", "dead-letter")

JOB_TYPE_INTELLIGENCE = "intelligence_analyse"

# Processed-job guard: `lead_scores` is append-only (current_leads keeps the
# latest as a query, not an update), so a naive retry of an already-succeeded
# job would insert a second score row for the same turn. This TTL only needs
# to outlive the claim/retry/reclaim cycle, not forever.
_PROCESSED_TTL_SECONDS = 6 * 60 * 60


def _processed_key(job_id: str) -> str:
    return redis_client.build_key("jobs", "processed", job_id)


async def already_processed(client: Any, job_id: str) -> bool:
    """Read-only check — does NOT claim the slot. Claiming happens in
    `mark_processed`, and only after the handler actually succeeds; claiming
    here (e.g. via SET NX) would mark a job "processed" on its first,
    possibly-failing attempt and then wrongly skip every legitimate retry."""
    return bool(await client.exists(_processed_key(job_id)))


async def mark_processed(client: Any, job_id: str) -> None:
    await client.set(_processed_key(job_id), "1", ex=_PROCESSED_TTL_SECONDS)


@dataclass
class Job:
    job_id: str
    job_type: str
    conversation_id: str
    attempt: int
    created_at: str
    payload: dict[str, Any]

    @classmethod
    def from_fields(cls, fields: dict[str, str]) -> Job:
        return cls(
            job_id=fields["job_id"],
            job_type=fields["job_type"],
            conversation_id=fields["conversation_id"],
            attempt=int(fields["attempt"]),
            created_at=fields["created_at"],
            payload=json.loads(fields["payload"]) if fields.get("payload") else {},
        )

    def to_fields(self) -> dict[str, str]:
        return {
            "job_id": self.job_id,
            "job_type": self.job_type,
            "conversation_id": self.conversation_id,
            "attempt": str(self.attempt),
            "created_at": self.created_at,
            "payload": json.dumps(self.payload),
        }


async def enqueue(
    job_type: str, conversation_id: str, payload: dict[str, Any] | None = None
) -> bool:
    """Durably record a job. Returns True if it was queued.

    Caller decides the fallback when this returns False (Redis disabled or
    down) — for intelligence analysis, `app/main.py` runs it inline instead,
    same as before Phase E, so a Redis outage degrades to "less durable," not
    "silently skipped."
    """
    if not settings.redis_jobs_enabled:
        return False
    client = redis_client.get_client()
    if client is None:
        log.warning("jobs_redis_unavailable", extra={"conversation_id": conversation_id})
        return False

    job = Job(
        job_id=str(uuid.uuid4()),
        job_type=job_type,
        conversation_id=conversation_id,
        attempt=0,
        created_at=datetime.now(UTC).isoformat(),
        payload=payload or {},
    )
    try:
        await client.xadd(STREAM_KEY, job.to_fields())
        log.info(
            "job_enqueued",
            extra={"job_id": job.job_id, "job_type": job_type, "conversation_id": conversation_id},
        )
        return True
    except Exception:
        log.exception("job_enqueue_failed", extra={"conversation_id": conversation_id})
        return False


async def ensure_group(client: Any) -> None:
    """Create the consumer group if it doesn't exist yet. Idempotent."""
    try:
        await client.xgroup_create(STREAM_KEY, settings.jobs_consumer_group, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def move_to_dead_letter(client: Any, job: Job, error: str) -> None:
    fields = job.to_fields()
    fields["error"] = error
    await client.xadd(DEAD_LETTER_KEY, fields)
    log.error(
        "job_dead_lettered",
        extra={
            "job_id": job.job_id,
            "job_type": job.job_type,
            "conversation_id": job.conversation_id,
        },
    )


async def dead_letter_count() -> int:
    client = redis_client.get_client()
    if client is None:
        return 0
    try:
        return int(await client.xlen(DEAD_LETTER_KEY))
    except Exception:
        log.exception("dead_letter_count_failed")
        return 0
