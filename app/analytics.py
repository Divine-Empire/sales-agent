"""Aggregates behind the dashboard and reports (BRD §15, §16).

Computed from the rows the agent already writes, so nothing here needs a batch
job or a scheduler. A report for a period is a query over that period.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app import cache, store
from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)


def _period_bounds(report_type: str, reference: date | None = None) -> tuple[date, date]:
    today = reference or datetime.now(UTC).date()
    if report_type == "daily":
        return today, today
    if report_type == "weekly":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6)
    start = today.replace(day=1)
    next_month = (start + timedelta(days=32)).replace(day=1)
    return start, next_month - timedelta(days=1)


async def overview() -> dict[str, Any]:
    """Everything the dashboard's landing page needs, in one call.

    One round trip rather than six: the dashboard renders server-side and each
    extra request is another cold-start-latency hop to Render. Short-TTL
    cached (Addition.md Phase F) — this recomputes from five separate
    Supabase reads every call with no dedicated write path to invalidate
    against, so a short TTL is the whole invalidation story, per the plan's
    own guidance for dashboard aggregates.
    """
    return await cache.get_or_set(
        cache.dashboard_key("overview"), settings.cache_dashboard_ttl_seconds, _overview
    )


async def _overview() -> dict[str, Any]:
    leads, handovers, opt_outs, summaries, conversations = (
        await store.get_ranked_leads(limit=500),
        await store.get_handover_queue(limit=200),
        await store.list_opt_outs(limit=500),
        await store.list_summaries(limit=500),
        await store.count_conversations(),
    )

    categories = Counter(lead.get("category") for lead in leads)
    intents = Counter(lead.get("intent") for lead in leads if lead.get("intent"))

    # Machine interest (BRD §15) comes from the summaries, where the agent
    # records what was actually discussed.
    machines: Counter[str] = Counter()
    for summary in summaries:
        for machine in summary.get("interested_machines") or []:
            if isinstance(machine, str) and machine.strip():
                machines[machine.strip()] += 1

    scored = [lead["score"] for lead in leads if isinstance(lead.get("score"), int)]

    # Conversion funnel (BRD §15). Each stage is a strict subset of the one
    # above it, so the drop-off between stages is meaningful.
    engaged = len([s for s in summaries if s.get("customer_name")])
    qualified = len([lead for lead in leads if lead.get("score", 0) >= 40])
    hot = categories.get("hot", 0)
    handed_over = len([s for s in summaries if (s.get("handover_status") or "none") != "none"])

    return {
        "totals": {
            "conversations": conversations,
            "leads": len(leads),
            "identified_customers": engaged,
            "pending_handovers": len([h for h in handovers if h.get("status") == "pending"]),
            "opt_outs": len(opt_outs),
            "average_score": round(sum(scored) / len(scored), 1) if scored else 0,
        },
        "categories": {
            "hot": categories.get("hot", 0),
            "warm": categories.get("warm", 0),
            "cold": categories.get("cold", 0),
            "not_interested": categories.get("not_interested", 0),
        },
        "intents": [
            {"intent": intent, "count": count} for intent, count in intents.most_common(12)
        ],
        "machine_interest": [
            {"machine": machine, "count": count} for machine, count in machines.most_common(15)
        ],
        "funnel": [
            {"stage": "Conversations", "count": conversations},
            {"stage": "Identified", "count": engaged},
            {"stage": "Qualified (40+)", "count": qualified},
            {"stage": "Hot", "count": hot},
            {"stage": "Handed over", "count": handed_over},
        ],
    }


async def report(report_type: str = "daily") -> dict[str, Any]:
    """Daily / weekly / monthly aggregate (BRD §15).

    Computed live rather than read from the `reports` table, so a report is
    never stale. Persisting a snapshot is the production upgrade once history
    needs to stay fixed.
    """
    if report_type not in ("daily", "weekly", "monthly"):
        report_type = "daily"
    start, end = _period_bounds(report_type)

    leads = await store.get_ranked_leads(limit=1000)
    summaries = await store.list_summaries(limit=1000)
    handovers = await store.get_handover_queue(limit=500)
    opt_outs = await store.list_opt_outs(limit=500)

    def in_period(value: str | None) -> bool:
        if not value:
            return False
        try:
            when = datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return False
        return start <= when <= end

    period_leads = [lead for lead in leads if in_period(lead.get("scored_at"))]
    categories = Counter(lead.get("category") for lead in period_leads)

    machines: Counter[str] = Counter()
    for summary in summaries:
        if not in_period(summary.get("updated_at")):
            continue
        for machine in summary.get("interested_machines") or []:
            if isinstance(machine, str) and machine.strip():
                machines[machine.strip()] += 1

    return {
        "report_type": report_type,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "metrics": {
            "leads": len(period_leads),
            "hot": categories.get("hot", 0),
            "warm": categories.get("warm", 0),
            "cold": categories.get("cold", 0),
            "not_interested": categories.get("not_interested", 0),
            "handovers": len([h for h in handovers if in_period(h.get("notified_at"))]),
            "opt_outs": len([o for o in opt_outs if in_period(o.get("opted_out_at"))]),
            "top_machines": [
                {"machine": machine, "count": count} for machine, count in machines.most_common(10)
            ],
        },
    }
