"""Standalone job worker — Phase E of .claude/Addition.md.

Two ways to run this, both consuming `de:v1:stream:jobs` via the same
consumer group so they never double-process each other's work:

- `run()` / `uv run python -m app.worker` — a persistent process (a paid
  Render Background Worker or similar) that blocks on new entries forever.
- `run_once()` / `uv run python -m app.worker --once` — drains whatever is
  currently pending or claimable, then exits. Built for Render's free-tier
  **Cron Job** service type, which invokes a fresh container on a schedule
  rather than keeping one alive — there is no persistent process to block
  in, so this mode processes one batch and returns instead of calling
  `xreadgroup` with a blocking `block=` wait.

Either way: acks only after the durable work succeeds, reclaims entries a
previous run left pending (a cron invocation that got killed mid-job looks
identical to a crashed persistent worker), and retries transient failures
with bounded backoff before dead-lettering.

Keep job payloads minimal (Addition.md §4 Phase E) — this worker re-fetches
canonical state (history, customer) from Supabase rather than trusting
anything beyond `conversation_id` in the envelope.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
import uuid

from app import intelligence, jobs, redis_client
from app.config import settings
from app.logging_config import get_logger, setup_logging

setup_logging()
log = get_logger(__name__)

# analyse_or_raise, not analyse: analyse() itself always returns None rather
# than raising (app/main.py's inline fallback caller must never have a
# scoring failure disturb the customer's turn), which meant a job that did
# genuinely fail — LLM unavailable, bad tool-call JSON — looked identical to
# a successful one from here: no exception, _handle_entry below acks and
# marks it processed regardless. Found via a real customer's lead score
# staying stale through an automated run with zero pending/dead-lettered
# jobs to explain it. analyse_or_raise (app/intelligence.py) wraps the same
# work but raises on that None, so a genuine failure now retries with
# backoff and eventually dead-letters instead of silently vanishing.
_HANDLERS = {
    jobs.JOB_TYPE_INTELLIGENCE: lambda job: intelligence.analyse_or_raise(job.conversation_id),
}


def _consumer_name() -> str:
    if settings.jobs_consumer_name:
        return settings.jobs_consumer_name
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


async def _handle_entry(client, consumer: str, entry_id: str, fields: dict) -> None:
    job = jobs.Job.from_fields(fields)

    if await jobs.already_processed(client, job.job_id):
        log.info("job_already_processed_skipping", extra={"job_id": job.job_id})
        await client.xack(jobs.STREAM_KEY, settings.jobs_consumer_group, entry_id)
        return

    handler = _HANDLERS.get(job.job_type)
    if handler is None:
        log.error("job_unknown_type", extra={"job_id": job.job_id, "job_type": job.job_type})
        await jobs.move_to_dead_letter(client, job, error=f"unknown job_type {job.job_type!r}")
        await client.xack(jobs.STREAM_KEY, settings.jobs_consumer_group, entry_id)
        return

    try:
        await handler(job)
        await jobs.mark_processed(client, job.job_id)
        await client.xack(jobs.STREAM_KEY, settings.jobs_consumer_group, entry_id)
        log.info(
            "job_succeeded",
            extra={"job_id": job.job_id, "job_type": job.job_type, "attempt": job.attempt},
        )
    except Exception as exc:
        job.attempt += 1
        log.exception(
            "job_attempt_failed",
            extra={"job_id": job.job_id, "job_type": job.job_type, "attempt": job.attempt},
        )
        if job.attempt >= settings.jobs_max_attempts:
            await jobs.move_to_dead_letter(client, job, error=str(exc))
            await client.xack(jobs.STREAM_KEY, settings.jobs_consumer_group, entry_id)
            return
        # Bounded exponential backoff before the entry becomes claimable
        # again: re-add as a fresh entry with the incremented attempt count,
        # ack the old one so it doesn't also get reclaimed and double-run.
        backoff = settings.jobs_backoff_base_seconds * (2 ** (job.attempt - 1))
        await asyncio.sleep(min(backoff, 60.0))
        await client.xadd(jobs.STREAM_KEY, job.to_fields())
        await client.xack(jobs.STREAM_KEY, settings.jobs_consumer_group, entry_id)


async def _reclaim_stale(client, consumer: str) -> None:
    """Claim pending entries idle longer than the configured timeout — the
    trace of a worker that died mid-job, per the plan's reclaim requirement."""
    try:
        cursor = "0-0"
        while True:
            cursor, claimed_entries, _deleted = await client.xautoclaim(
                jobs.STREAM_KEY,
                settings.jobs_consumer_group,
                consumer,
                min_idle_time=int(settings.jobs_claim_idle_seconds * 1000),
                start_id=cursor,
                count=settings.jobs_batch_size,
            )
            for entry_id, fields in claimed_entries:
                log.info("job_reclaimed", extra={"entry_id": entry_id})
                await _handle_entry(client, consumer, entry_id, fields)
            if cursor == "0-0" or not claimed_entries:
                break
    except Exception:
        log.exception("job_reclaim_failed")


async def _drain_once(client, consumer: str) -> int:
    """Reclaim stale entries, then read and process whatever is immediately
    available — no blocking wait. Returns the number of entries handled, so
    the caller can log a useful "nothing to do" vs. "processed N" line."""
    await _reclaim_stale(client, consumer)

    handled = 0
    while True:
        response = await client.xreadgroup(
            settings.jobs_consumer_group,
            consumer,
            {jobs.STREAM_KEY: ">"},
            count=settings.jobs_batch_size,
            block=None,  # don't wait — a cron run has no persistent process to block in
        )
        entries = response[0][1] if response else []
        if not entries:
            break
        for entry_id, fields in entries:
            await _handle_entry(client, consumer, entry_id, fields)
            handled += 1
    return handled


async def run_once() -> None:
    """Single drain-and-exit pass — the entry point for a Render Cron Job
    (free tier) invocation. Safe to run on a schedule with no persistent
    process between runs; a run that dies mid-job just leaves its entry
    pending for the next scheduled run's `_reclaim_stale` to pick up."""
    if not settings.redis_jobs_enabled:
        log.warning("worker_disabled_jobs_flag_off")
        return

    client = redis_client.get_client()
    if client is None:
        log.error("worker_redis_unavailable_at_startup")
        return

    await jobs.ensure_group(client)
    consumer = _consumer_name()
    log.info("worker_run_once_started", extra={"consumer": consumer})

    try:
        handled = await _drain_once(client, consumer)
        log.info("worker_run_once_finished", extra={"consumer": consumer, "handled": handled})
    except Exception:
        log.exception("worker_run_once_error")


async def run() -> None:
    """Persistent worker loop for an always-on process (paid Background
    Worker or similar). Blocks on new stream entries; never exits on a
    transient Redis error — logs and retries after a short pause instead."""
    if not settings.redis_jobs_enabled:
        log.warning("worker_disabled_jobs_flag_off")
        return

    client = redis_client.get_client()
    if client is None:
        log.error("worker_redis_unavailable_at_startup")
        return

    await jobs.ensure_group(client)
    consumer = _consumer_name()
    log.info("worker_started", extra={"consumer": consumer})

    while True:
        try:
            await _reclaim_stale(client, consumer)

            response = await client.xreadgroup(
                settings.jobs_consumer_group,
                consumer,
                {jobs.STREAM_KEY: ">"},
                count=settings.jobs_batch_size,
                block=settings.jobs_block_ms,
            )
            if not response:
                continue
            for _stream_key, entries in response:
                for entry_id, fields in entries:
                    await _handle_entry(client, consumer, entry_id, fields)
        except Exception:
            log.exception("worker_loop_error")
            await asyncio.sleep(2.0)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        if "--once" in sys.argv:
            asyncio.run(run_once())
        else:
            asyncio.run(run())
