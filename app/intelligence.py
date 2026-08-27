"""Post-reply analysis: lead scoring, intent classification, summarisation.

BRD §9, §10, §11, §14.

Runs AFTER the customer has their reply, never before. Each of these is an LLM
call; doing them inline would put seconds between a customer's message and the
bot's answer for work the customer never sees.

One call produces all three. The same conversation, read once, yields the
score, the intent and the summary — three calls would cost three times as much
and take three times as long to say the same thing.

Scores are appended, never updated, so ranking movement stays auditable and
"why is this lead hot?" has an answer.
"""

from __future__ import annotations

import json
from typing import Any

from app import store
from app.enums import AiLogEvent, CustomerIntent, Language, LeadCategory
from app.llm import LLMUnavailableError, complete
from app.logging_config import get_logger
from app.models import ConversationSummary, LeadScore, parse_conversation_id

log = get_logger(__name__)

# Weights sum to 100. Made explicit rather than left to the model's judgement so
# that two conversations with the same signals score the same, and so a sales
# manager can argue with the weighting instead of with a black box.
FACTOR_WEIGHTS = {
    "buying_intent": 20,  # explicit intent to purchase
    "budget": 15,  # budget stated or implied
    "timeline": 15,  # when they plan to buy
    "product_fit": 10,  # what they need matches what we sell
    "decision_maker": 10,  # authority to buy
    "business_type": 5,  # contractor/dealer vs student/curious
    "engagement": 10,  # depth of conversation, questions asked
    "quote_request": 5,  # asked for a quotation
    "demo_request": 5,  # asked for a demo or site visit
    "brochure_request": 5,  # asked for specs or literature
}

ANALYSIS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "record_analysis",
        "description": "Record the lead score, intent classification and summary.",
        "parameters": {
            "type": "object",
            "properties": {
                "factors": {
                    "type": "object",
                    "description": (
                        "Score each factor from 0 to its maximum. Award 0 when a factor was "
                        "never discussed — absence is not a negative signal, it is no signal."
                    ),
                    "properties": {
                        name: {
                            "type": "integer",
                            "description": f"0 to {weight}",
                        }
                        for name, weight in FACTOR_WEIGHTS.items()
                    },
                },
                "category": {
                    "type": "string",
                    "enum": [c.value for c in LeadCategory],
                    "description": (
                        "Judge whether a sales rep should call this person — this is a "
                        "separate question from the numeric score, which only measures how "
                        "much information the conversation contains. "
                        "hot: ready to buy — asked for a quote, or gave budget and timeline. "
                        "warm: a real prospect — identified themselves or their company AND "
                        "named a specific product or requirement, even with no budget or "
                        "timeline yet. Most genuine buyers start here. "
                        "cold: anonymous browsing, vague questions, no stated need. "
                        "not_interested: declined, opted out, or spam."
                    ),
                },
                "intent": {
                    "type": "string",
                    "enum": [i.value for i in CustomerIntent],
                    "description": "The dominant purpose of the conversation.",
                },
                "confidence": {
                    "type": "number",
                    "description": "0.0 to 1.0 — how confident you are in this assessment.",
                },
                "customer_name": {"type": "string"},
                "company_name": {"type": "string"},
                "location": {"type": "string"},
                "preferred_language": {
                    "type": "string",
                    "enum": ["en", "hi", "hinglish"],
                },
                "interested_machines": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Products discussed, as named in the catalog.",
                },
                "requirements": {"type": "string", "description": "Technical needs, one line."},
                "budget": {"type": "string"},
                "timeline": {"type": "string"},
                "summary": {
                    "type": "string",
                    "description": (
                        "Two or three sentences a sales rep can read cold and know what "
                        "happened and what the customer wants."
                    ),
                },
                "next_action": {
                    "type": "string",
                    "description": "The single most useful next step for the sales team.",
                },
            },
            "required": ["factors", "category", "intent", "confidence", "summary", "next_action"],
        },
    },
}

ANALYSIS_PROMPT = """You analyse sales conversations for an Indian construction equipment supplier.

Read the conversation and record your assessment by calling record_analysis.

Scoring guidance:
- Score only what the conversation supports. A factor never discussed scores 0 — that is not a penalty, it is the absence of a signal.
- A customer who names a product, a quantity and a deadline is worth more than one who writes long messages without committing to anything. Length is not engagement.
- "Not interested" means they declined or opted out. Someone still asking questions is not cold, however early they are.
- Spam, test messages and abuse are intent=spam, category=not_interested.

Extract only facts the customer actually stated. Never infer a company name from an email address, a budget from a product price, or a location from a dialect. Omit what you do not know."""


def _clamp_factors(raw: Any) -> dict[str, int]:
    """Clamp each factor to its weight. The model will occasionally return 30
    for a factor worth 10; a score above 100 would corrupt the ranking."""
    factors: dict[str, int] = {}
    if not isinstance(raw, dict):
        return factors
    for name, weight in FACTOR_WEIGHTS.items():
        value = raw.get(name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        factors[name] = max(0, min(int(value), weight))
    return factors


def _coerce_enum(value: Any, enum_cls: Any, default: Any) -> Any:
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return default


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


async def analyse(conversation_id: str, customer_id: str | None = None) -> dict[str, Any] | None:
    """Score, classify and summarise a conversation.

    Returns None on any failure — this is background enrichment, and a failure
    here must never surface to the customer or disturb the conversation.
    """
    history = await store.get_history(conversation_id, limit=40)
    if not history:
        return None

    # Resolve the customer so lead_scores joins correctly in current_leads —
    # without it the ranking shows a score with no name against it.
    if customer_id is None:
        channel, user_id = parse_conversation_id(conversation_id)
        customer = await store.get_customer(channel, user_id)
        customer_id = customer.get("id") if customer else None

    transcript = "\n".join(
        f"{'Customer' if m.role == 'user' else 'Agent'}: {m.content}"
        for m in history
        if m.role in ("user", "assistant")
    )
    if not transcript.strip():
        return None

    try:
        response = await complete(
            [
                {"role": "system", "content": ANALYSIS_PROMPT},
                {"role": "user", "content": f"Conversation:\n\n{transcript}"},
            ],
            tools=[ANALYSIS_TOOL],
            temperature=0.0,  # classification, not conversation — determinism wins
            conversation_id=conversation_id,
        )
    except LLMUnavailableError:
        log.warning("analysis_llm_unavailable", extra={"conversation_id": conversation_id})
        await store.log_ai_event(
            conversation_id, AiLogEvent.ERROR, payload={"stage": "analysis_llm_unavailable"}
        )
        return None

    # Logged here, not just in app.llm — this call previously left NO trace
    # anywhere queryable (not /api/logs, which only ever showed the customer-
    # facing turn's own llm_call), which made a genuinely-stale lead score
    # indistinguishable from "the job silently never ran." Same event type
    # the per-turn agent call uses, so a filter on llm_call in /api/logs now
    # shows both, distinguishable by the absence of retrieved_chunks here
    # (this call never runs RAG).
    await store.log_ai_event(
        conversation_id,
        AiLogEvent.LLM_CALL,
        model=response.model,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        latency_ms=response.latency_ms,
        payload={"stage": "intelligence_analyse", "used_fallback": response.used_fallback},
    )

    if not response.tool_calls:
        log.warning("analysis_no_tool_call", extra={"conversation_id": conversation_id})
        return None

    try:
        data = json.loads(response.tool_calls[0].function.arguments)
    except json.JSONDecodeError:
        log.warning("analysis_bad_json", extra={"conversation_id": conversation_id})
        return None
    if not isinstance(data, dict):
        return None

    factors = _clamp_factors(data.get("factors"))
    score = sum(factors.values())
    category = _coerce_enum(data.get("category"), LeadCategory, LeadCategory.COLD)
    intent = _coerce_enum(data.get("intent"), CustomerIntent, CustomerIntent.GENERAL_INQUIRY)

    confidence = data.get("confidence")
    confidence = (
        round(min(max(float(confidence), 0.0), 1.0), 2)
        if isinstance(confidence, int | float)
        else None
    )

    await store.save_lead_score(
        LeadScore(
            conversation_id=conversation_id,
            customer_id=customer_id,
            score=score,
            category=category,
            intent=intent,
            factors=factors,
            confidence=confidence,
        )
    )

    # Preserve fields the agent's save_lead already captured — the analysis may
    # not restate a company name mentioned twenty turns ago.
    existing = await store.get_summary(conversation_id) or {}
    machines = data.get("interested_machines")
    machines = (
        [m for m in machines if isinstance(m, str)]
        if isinstance(machines, list)
        else existing.get("interested_machines") or []
    )

    handover_status = existing.get("handover_status") or "none"
    await store.upsert_summary(
        ConversationSummary(
            conversation_id=conversation_id,
            customer_id=customer_id or existing.get("customer_id"),
            customer_name=_text(data.get("customer_name")) or existing.get("customer_name"),
            company_name=_text(data.get("company_name")) or existing.get("company_name"),
            location=_text(data.get("location")) or existing.get("location"),
            preferred_language=_coerce_enum(data.get("preferred_language"), Language, None)
            or existing.get("preferred_language"),
            interested_machines=machines,
            requirements=_text(data.get("requirements")) or existing.get("requirements"),
            budget=_text(data.get("budget")) or existing.get("budget"),
            timeline=_text(data.get("timeline")) or existing.get("timeline"),
            lead_score=score,
            lead_category=category,
            customer_intent=intent,
            summary=_text(data.get("summary")),
            next_action=_text(data.get("next_action")),
            handover_status=handover_status,
            ai_confidence=confidence,
        )
    )

    log.info(
        "analysis_complete",
        extra={
            "conversation_id": conversation_id,
            "score": score,
            "category": category.value,
            "intent": intent.value,
            "confidence": confidence,
        },
    )
    return {
        "score": score,
        "category": category.value,
        "intent": intent.value,
        "factors": factors,
        "confidence": confidence,
        "summary": _text(data.get("summary")),
        "next_action": _text(data.get("next_action")),
    }


class AnalysisFailedError(RuntimeError):
    """Raised only by analyse_or_raise — see its docstring."""


async def analyse_or_raise(conversation_id: str, customer_id: str | None = None) -> dict[str, Any]:
    """Same work as analyse(), but raises instead of returning None on
    failure — for app/worker.py only.

    analyse() intentionally swallows every failure (LLM unavailable, bad
    tool-call JSON, no history) and returns None, because its OTHER caller
    (app/main.py's inline fallback, used when job enqueueing itself fails)
    must never let a scoring failure disturb the customer's turn. But that
    same contract meant the job worker's _handle_entry saw a clean return
    and called xack + mark_processed regardless — a real conversation's
    scoring job "succeeded" while genuinely writing nothing, and nothing
    told the worker to retry or dead-letter it. Found via a real customer's
    stale lead score that a manual re-run immediately corrected once run
    directly (Hot/75 instead of a stale Cold/15) — the job had clearly
    consumed its stream entry without producing that result automatically.
    """
    result = await analyse(conversation_id, customer_id)
    if result is None:
        raise AnalysisFailedError(f"analyse() returned None for {conversation_id}")
    return result
