"""Agent core — channel-agnostic.

One agent, one job: qualify -> answer -> capture -> hand off. No router agent,
no second LLM in the conversation path.

Per turn: load history -> retrieve product context -> call the LLM with three
tools -> run any tool calls -> reply. The tool loop is hard-capped; an uncapped
autonomous loop eventually burns an API budget.

Every failure path still produces a message for the customer. Silence reads as
broken.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from app import commands, locks, prompts, rag, store
from app.enums import (
    AiLogEvent,
    Channel,
    ConversationStatus,
    HandoverReason,
    MessageRole,
)
from app.llm import LLMUnavailableError, complete
from app.logging_config import get_logger
from app.models import (
    AgentReply,
    HandoverRequest,
    IncomingMessage,
    OptOutEntry,
    parse_conversation_id,
)

log = get_logger(__name__)

MAX_TOOL_ITERATIONS = 3

# Post-generation guard for the "bulleted catalog dump" failure mode: prompt
# instructions and a lower temperature (app/config.py) both reduce how often
# GPT-4o ignores the "short, no bullets" rules, but neither is a hard
# guarantee — confirmed live: the same deployed code, same low temperature,
# still produced a 346-completion-token bulleted reply on a fresh
# conversation. Rather than capping llm_max_output_tokens (which would risk
# truncating a genuinely long reply mid-sentence when the customer actually
# asked for a comparison or full list), a reply is only compressed AFTER
# generation, and only when it's unambiguously bot-shaped — several bullet
# lines AND long. A short reply, or one with a single bullet, is left alone.
_BULLET_LINE_RE = re.compile(r"^\s*[•\-*]\s+|^\s*\d+[.)]\s+", re.MULTILINE)
CATALOG_DUMP_MIN_BULLET_LINES = 3
CATALOG_DUMP_MIN_CHARS = 400

# Second post-generation guard: the client does not want a price volunteered
# unless the customer actually asked for one — confirmed the prompt-only
# version of this rule (an explicit hard rule plus a worked example matching
# the exact failing query) still failed 5/5 in testing, the same pattern as
# the catalog-dump case above. A rupee amount in the reply is unambiguous —
# unlike bullet-vs-no-bullet, there's no legitimate reason a price-less
# question should ever get one back, so this check is a plain regex on the
# CUSTOMER's message, not a fuzzy content judgement.
_RUPEE_RE = re.compile(r"₹|\brs\.?\s*\d|\bINR\b", re.IGNORECASE)
_PRICE_INTENT_RE = re.compile(
    r"price|cost|rate|quote|quotation|budget|kitna|kitne|kimat|keemat|daam|rupees?",
    re.IGNORECASE,
)


def _asked_for_price(customer_text: str) -> bool:
    return bool(_PRICE_INTENT_RE.search(customer_text))


# Tool schemas. Descriptions tell the model WHEN to call, not just what it does
# — these are part of the prompt and are written with the same care.
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "save_lead",
            "description": (
                "Save the customer as a sales lead. Call this IN THE SAME TURN that you first "
                "learn their name, company, and product interest — even if they told you all "
                "three in their opening message, and even if the conversation is clearly going "
                "to continue. Waiting until the conversation feels finished is wrong: most "
                "customers stop replying without warning, and an unsaved lead is a lost one. "
                "Do not ask permission, and do not mention that you saved anything. Call it "
                "again later if you learn more (budget, timeline, quantity)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Customer's name"},
                    "company": {"type": "string", "description": "Company or firm name"},
                    "product_interest": {
                        "type": "string",
                        "description": (
                            "Product or category they asked about, "
                            "e.g. 'Sokkia IM-105 total station'"
                        ),
                    },
                    "quantity": {"type": "string", "description": "Quantity needed, if mentioned"},
                    "budget": {"type": "string", "description": "Budget range, if mentioned"},
                    "timeline": {
                        "type": "string",
                        "description": "When they plan to buy, if mentioned",
                    },
                    "location": {"type": "string", "description": "City or site location"},
                    "requirements": {
                        "type": "string",
                        "description": (
                            "Technical needs in one line, e.g. '32mm TMT bar, RCC building site'"
                        ),
                    },
                },
                "required": ["name", "company", "product_interest"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_human_handoff",
            "description": (
                "Notify the sales team about a specific commercial request: a formal quotation, "
                "price negotiation, a bulk or multi-unit order, an explicit ask to speak to a "
                "person, or a question you genuinely cannot answer from the product context. "
                "Call it once for that request, then KEEP TALKING to the customer yourself — "
                "this notifies the team in the background, it does not end the conversation or "
                "hand control away from you. Simply mentioning another product afterward is NOT "
                "a new commercial request — answer normally from the product context, with a "
                "regular price like any other question. Only call this again if the customer "
                "explicitly asks for a formal quote, negotiates price, or places a bulk order on "
                "a DIFFERENT product than the one already escalated. Never guess at a price, "
                "discount, or delivery date yourself — that is what makes this necessary — but "
                "everything else about the conversation is still your job."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": [r.value for r in HandoverReason],
                        "description": "Why this needs a human",
                    },
                    "context": {
                        "type": "string",
                        "description": (
                            "What the sales rep needs to know to pick this up cold: who the "
                            "customer is, what they want, and what has been discussed."
                        ),
                    },
                },
                "required": ["reason", "context"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_opt_out",
            "description": (
                "Record that the customer does not want to be contacted again. Call this "
                "immediately when someone says stop, unsubscribe, remove my number, or do not "
                "contact me. Never argue or try to retain them first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "The customer's own words requesting this",
                    }
                },
                "required": [],
            },
        },
    },
]


class ToolContext:
    """Per-turn state shared with tool handlers."""

    def __init__(self, message: IncomingMessage, customer_id: str | None):
        self.message = message
        self.customer_id = customer_id
        self.conversation_id = message.conversation_id
        self.handover_triggered = False
        self.handover_context: str | None = None
        self.opted_out = False
        self.lead_saved = False
        self.notifications: list[dict[str, Any]] = []


_PLACEHOLDERS = {
    "unknown",
    "n/a",
    "na",
    "none",
    "not provided",
    "not specified",
    "not mentioned",
    "customer",
    "-",
    "",
}


def _real_value(value: Any) -> str | None:
    """Return the value only if it is a genuine answer, not a placeholder."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned.lower() not in _PLACEHOLDERS else None


async def _handle_save_lead(args: dict[str, Any], ctx: ToolContext) -> str:
    """Persist the lead as a conversation summary and queue an ops alert."""
    from app.models import ConversationSummary

    # The model will fill required fields with placeholders rather than skip the
    # call. A lead row reading "Unknown | Unknown" teaches a sales rep nothing,
    # so reject it and tell the model to go ask.
    name = _real_value(args.get("name"))
    company = _real_value(args.get("company"))
    if not name and not company:
        return (
            "Error: no real customer name or company was provided. Do not guess or use "
            "placeholders — ask the customer for their name and company first, then call "
            "save_lead again."
        )

    interested = [args["product_interest"]] if args.get("product_interest") else []
    ok = await store.upsert_summary(
        ConversationSummary(
            conversation_id=ctx.conversation_id,
            customer_id=ctx.customer_id,
            customer_name=name,
            company_name=company,
            location=_real_value(args.get("location")),
            interested_machines=interested,
            requirements=args.get("requirements"),
            budget=args.get("budget"),
            timeline=args.get("timeline"),
        )
    )
    # Keep the customer record in step so the dashboard and future turns agree.
    if ctx.customer_id:
        from app.enums import Channel
        from app.models import Customer

        channel, user_id = parse_conversation_id(ctx.conversation_id)
        await store.upsert_customer(
            Customer(
                channel=Channel(channel),
                channel_user_id=user_id,
                name=name,
                company_name=company,
                location=_real_value(args.get("location")),
            )
        )
    ctx.lead_saved = True
    # Filter placeholders out of the ops alert too, not just the stored row.
    # A rep reading "Location: unknown" learns nothing the omission would not
    # have told them, and it makes the alert look like it malfunctioned.
    ctx.notifications.append(
        {"type": "lead", "data": {k: v for k, v in args.items() if _real_value(v)}}
    )
    log.info(
        "tool_save_lead",
        extra={
            "conversation_id": ctx.conversation_id,
            "company": args.get("company"),
            "product": args.get("product_interest"),
            "persisted": ok,
        },
    )
    return "Lead saved." if ok else "Lead noted (storage unavailable)."


async def _handle_handoff(args: dict[str, Any], ctx: ToolContext) -> str:
    raw_reason = args.get("reason", HandoverReason.OTHER.value)
    try:
        reason = HandoverReason(raw_reason)
    except ValueError:
        reason = HandoverReason.OTHER
    context = args.get("context", "")
    await store.save_handover(
        HandoverRequest(
            conversation_id=ctx.conversation_id,
            customer_id=ctx.customer_id,
            reason=reason,
            context=context,
        )
    )
    ctx.handover_triggered = True
    ctx.handover_context = context
    ctx.notifications.append({"type": "handoff", "reason": reason.value, "context": context})
    log.info(
        "tool_handoff",
        extra={"conversation_id": ctx.conversation_id, "reason": reason.value},
    )
    # Deliberately does NOT instruct the model to recite contact details — that
    # phrasing was what caused the model to answer every subsequent message
    # with only a contact card, as if the handoff had ended the conversation.
    # It has not: this is a background notification to the sales team, and the
    # model should keep selling. If the customer separately asks how to reach
    # a person, that is a normal question to answer, not a scripted response.
    return (
        "The sales team has been notified about this specific request and will follow up "
        "with exact pricing. Continue the conversation normally — keep answering questions, "
        "recommending products, and helping them. Do not repeat this notification or mention "
        "contact details unless the customer asks how to reach someone directly."
    )


async def _handle_opt_out(args: dict[str, Any], ctx: ToolContext) -> str:
    channel, user_id = parse_conversation_id(ctx.conversation_id)
    await store.record_opt_out(
        OptOutEntry(
            channel=channel,
            channel_user_id=user_id,
            customer_id=ctx.customer_id,
            conversation_id=ctx.conversation_id,
            reason=args.get("reason"),
        )
    )
    await store.set_conversation_status(ctx.conversation_id, ConversationStatus.OPTED_OUT)
    ctx.opted_out = True
    log.info("tool_opt_out", extra={"conversation_id": ctx.conversation_id})
    return (
        "Opt-out recorded. Confirm politely that they will not be contacted again, "
        "and do not ask any further questions."
    )


HANDLERS = {
    "save_lead": _handle_save_lead,
    "request_human_handoff": _handle_handoff,
    "record_opt_out": _handle_opt_out,
}


async def _run_tool(name: str, raw_args: str, ctx: ToolContext) -> str:
    """Execute one tool call. Malformed arguments are returned to the model as
    an error string — the model will eventually emit bad JSON, and that must
    never crash the request."""
    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError:
        log.warning(
            "tool_bad_json",
            extra={"conversation_id": ctx.conversation_id, "tool": name, "raw": raw_args[:200]},
        )
        return "Error: arguments were not valid JSON. Retry with a valid JSON object."
    if not isinstance(args, dict):
        return "Error: arguments must be a JSON object."

    handler = HANDLERS.get(name)
    if handler is None:
        log.warning("tool_unknown", extra={"conversation_id": ctx.conversation_id, "tool": name})
        return f"Error: unknown tool '{name}'."

    try:
        return await handler(args, ctx)
    except KeyError as exc:
        return f"Error: missing required argument {exc}. Retry with all required fields."
    except Exception:
        log.exception("tool_failed", extra={"conversation_id": ctx.conversation_id, "tool": name})
        return "Error: that action could not be completed. Continue the conversation."


def _enrich_search_query(message_text: str, summary: dict[str, Any] | None) -> str:
    """Fold the customer's already-collected requirement (from save_lead's
    conversation_summaries row) into the RAG query, not just this one
    message.

    Pure keyword/vector similarity on a single short message ("total station
    chahiye") can't distinguish between machines in the same category — it
    has no way to weigh a customer's stated project type, location, or
    requirements against a rich product profile's "Who should (not) buy it"
    section. Once qualifying has captured that context, feeding it into
    every subsequent search lets retrieval do that matching instead of
    leaving it to chance on whichever single message triggered this turn's
    search. Degrades to the plain message when there's no summary yet (a
    conversation's first few turns, or if Supabase is unreachable) — this is
    an enrichment, not a requirement.
    """
    if not summary:
        return message_text
    extra = [
        summary.get("requirements"),
        " ".join(summary.get("interested_machines") or []),
        summary.get("location"),
    ]
    extra_text = " ".join(part for part in extra if isinstance(part, str) and part.strip())
    return f"{message_text} {extra_text}".strip() if extra_text else message_text


def _looks_like_catalog_dump(text: str) -> bool:
    """Conservative detector for the specific failure this guards against —
    several bullet/numbered lines AND real length. A single bullet, or a
    short reply, is left alone; the customer may have genuinely asked for a
    comparison or full list, which the prompt explicitly allows."""
    bullet_lines = len(_BULLET_LINE_RE.findall(text))
    return bullet_lines >= CATALOG_DUMP_MIN_BULLET_LINES and len(text) >= CATALOG_DUMP_MIN_CHARS


async def _compress_reply(text: str, conversation_id: str) -> str:
    """One extra, cheap completion that rewrites an over-long bulleted reply
    into 1-3 plain sentences — a self-correction pass, not a blind
    truncation, so no information is lost mid-sentence the way capping
    llm_max_output_tokens would risk. Returns the original text on any
    failure, so a compression-call error never costs the customer their
    (long but complete) answer."""
    try:
        response = await complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Rewrite the message below as 1-3 short plain sentences, the way a "
                        "person would type it in chat. No bullet points, no numbered lists, "
                        "no headings. Keep only the single most relevant item or two — drop "
                        "the rest rather than summarizing everything. End with the same "
                        "question the original message asked, if it asked one. Reply with "
                        "only the rewritten message, nothing else."
                    ),
                },
                {"role": "user", "content": text},
            ],
            temperature=0.1,
            conversation_id=conversation_id,
        )
        compressed = (response.content or "").strip()
        if not compressed:
            log.warning("reply_guard_compression_empty", extra={"conversation_id": conversation_id})
            return text
        return compressed
    except LLMUnavailableError:
        log.warning(
            "reply_guard_compression_call_failed", extra={"conversation_id": conversation_id}
        )
        return text


async def _strip_unrequested_price(text: str, conversation_id: str) -> str:
    """Rewrite a reply to remove a price the customer didn't ask for.

    A plain regex removal of the ₹ amount would leave broken grammar behind
    ("which is available at ." after deleting "₹2,89,000") — a rewrite call
    keeps everything else about the reply (specs, the qualifying question)
    intact and just drops the pricing sentence/clause entirely. Returns the
    original text on any failure, matching _compress_reply's fail-open
    behavior — a price the customer didn't ask for is a smaller problem than
    losing an otherwise-correct reply to a guard-call error."""
    try:
        response = await complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Rewrite the message below to remove any price, cost, or rupee amount "
                        "— the customer did not ask for pricing, so it should not be mentioned "
                        "at all. Keep everything else (specs, recommendations, the question at "
                        "the end) exactly as it is, just remove the price and any sentence that "
                        "exists only to state it. Reply with only the rewritten message, "
                        "nothing else."
                    ),
                },
                {"role": "user", "content": text},
            ],
            temperature=0.1,
            conversation_id=conversation_id,
        )
        rewritten = (response.content or "").strip()
        if not rewritten:
            log.warning("reply_guard_price_strip_empty", extra={"conversation_id": conversation_id})
            return text
        return rewritten
    except LLMUnavailableError:
        log.warning(
            "reply_guard_price_strip_call_failed", extra={"conversation_id": conversation_id}
        )
        return text


async def handle_message(message: IncomingMessage) -> AgentReply:
    """Process one inbound message and produce a reply.

    Never raises: every failure path returns a sendable message.

    Wrapped in a per-conversation lock (Phase C, .claude/Addition.md) so two
    near-simultaneous deliveries for the same conversation — across Render
    workers — can't both read the same history and act on stale state. Other
    conversations proceed fully concurrently; lock acquisition fails open
    when Redis is disabled or unreachable.
    """
    async with locks.conversation_lock(message.conversation_id) as acquired:
        if not acquired:
            return AgentReply(text=prompts.BUSY_MESSAGE, model="none")
        return await _handle_message_locked(message)


async def _handle_message_locked(message: IncomingMessage) -> AgentReply:
    conversation_id = message.conversation_id
    log.info(
        "message_in",
        extra={
            "conversation_id": conversation_id,
            "channel": str(message.channel),
            "chars": len(message.text),
        },
    )

    # WhatsApp has no slash-command concept in this flow (the forwarded
    # payload is just {from, text, ...} — see WhatsAppPortalAdapter.parse),
    # so unlike Telegram's /start there is no way for a customer to ask for
    # the greeting explicitly. Detect it instead: genuinely no prior message
    # for this conversation_id. Must run BEFORE this turn's own
    # save_message calls below (bootstrap_turn itself only touches
    # customers/conversations, not messages, so it's safe either side) — or
    # every check from here on would see this turn's own message as "prior".
    is_whatsapp_first_message = message.channel == Channel.WHATSAPP and not (
        await store.has_prior_messages(conversation_id)
    )

    customer_id = await store.bootstrap_turn(
        message.channel,
        message.user_id,
        conversation_id,
        sender_name=message.sender_name,
        sender_phone=message.sender_phone,
    )
    ctx = ToolContext(message, customer_id)

    # /clear wipes history, so it runs before this turn is written — otherwise
    # the confirmation would be deleted along with everything else.
    if commands.parse_command(message.text) == "/clear":
        await store.clear_history(conversation_id)
        await store.save_message(conversation_id, MessageRole.ASSISTANT, commands.CLEARED_MESSAGE)
        log.info("command_handled", extra={"conversation_id": conversation_id, "command": "/clear"})
        return AgentReply(text=commands.CLEARED_MESSAGE, model="command")

    await store.save_message(conversation_id, MessageRole.USER, message.text)

    # Slash commands answer from constants — instant, identical every time, and
    # no LLM cost. /stop deliberately falls through so opt-out is persisted.
    canned = commands.handle_command(message.text)
    if canned is not None:
        await store.save_message(conversation_id, MessageRole.ASSISTANT, canned)
        log.info(
            "command_handled",
            extra={"conversation_id": conversation_id, "command": message.text.split()[0]},
        )
        return AgentReply(text=canned, model="command")

    # History and the existing summary (if save_lead has already captured
    # one) are independent of each other, so fetched together — the summary
    # is needed before retrieval can run (see _enrich_search_query), which is
    # why it can't join the same gather as rag.search below.
    history_rows, summary = await asyncio.gather(
        store.get_history(conversation_id),
        store.get_summary(conversation_id),
    )
    search_query = _enrich_search_query(message.text, summary)
    chunks = await rag.search(search_query, conversation_id=conversation_id)
    context_block = rag.build_context(chunks)

    history = [
        {"role": m.role, "content": m.content}
        for m in history_rows
        if m.role in ("user", "assistant")
    ]
    if not history:
        history = [{"role": "user", "content": message.text}]

    messages = prompts.build_messages(history, context_block)

    reply_text = ""
    model_used = ""
    tool_names: list[str] = []

    try:
        for iteration in range(MAX_TOOL_ITERATIONS):
            response = await complete(messages, tools=TOOLS, conversation_id=conversation_id)
            model_used = response.model

            await store.log_ai_event(
                conversation_id,
                AiLogEvent.LLM_CALL,
                model=response.model,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                latency_ms=response.latency_ms,
                retrieved_chunks=[
                    {"score": round(c.score, 3), "category": c.category} for c in chunks
                ]
                or None,
                payload={"iteration": iteration, "used_fallback": response.used_fallback},
            )

            if not response.tool_calls:
                reply_text = response.content or ""
                break

            # Echo the assistant's tool-call turn back before the results.
            # content is deliberately dropped, not echoed as response.content: GPT-4o
            # sometimes narrates the call in prose alongside it (e.g. "I'll notify the
            # team — request_human_handoff"), and that narration, once in history, has
            # leaked into a LATER reply almost verbatim (a real customer saw the literal
            # tool name "request_human_handoff" appended to an otherwise normal
            # message). The customer-facing reply always comes from the next
            # completion after tool results are fed back, never from this field, so
            # there is nothing lost by not persisting it.
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in response.tool_calls
                    ],
                }
            )
            for tc in response.tool_calls:
                tool_names.append(tc.function.name)
                result = await _run_tool(tc.function.name, tc.function.arguments, ctx)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        else:
            # Loop cap hit with tool calls still pending. Ask for prose only.
            log.warning("tool_loop_capped", extra={"conversation_id": conversation_id})
            final = await complete(messages, conversation_id=conversation_id)
            reply_text = final.content or ""
            model_used = final.model

    except LLMUnavailableError:
        log.exception("agent_llm_unavailable", extra={"conversation_id": conversation_id})
        await store.log_ai_event(
            conversation_id, AiLogEvent.ERROR, payload={"stage": "llm_unavailable"}
        )
        error_text = prompts.ERROR_MESSAGE
        if is_whatsapp_first_message:
            # A first-time customer still gets the company intro even if the
            # LLM itself is down for their very first message — the outage
            # shouldn't cost them the greeting too. Note this path does not
            # persist an assistant message, matching this function's existing
            # behavior on LLM failure (unchanged by this addition).
            error_text = f"{commands.START_MESSAGE}\n\n{error_text}"
        return AgentReply(text=error_text, model="none")

    if not reply_text.strip():
        # The model returned nothing usable; fall back to something sendable.
        reply_text = (
            prompts.OPT_OUT_MESSAGE
            if ctx.opted_out
            else prompts.HANDOFF_MESSAGE
            if ctx.handover_triggered
            else prompts.ERROR_MESSAGE
        )
    else:
        # Two independent post-generation guards — a reply could in theory
        # trip both, so these run sequentially rather than as elif branches.
        # See _looks_like_catalog_dump / _compress_reply and
        # _asked_for_price / _strip_unrequested_price above: the prompt's
        # rules (short/no-bullets, price-only-if-asked) and a lower
        # temperature both help, but neither guarantees it — both confirmed
        # live against production, the price rule failing 5/5 in testing
        # even with an explicit worked example. These are the hard backstop.
        # Logged before AND after so each is verifiable from Render's logs
        # alone: "_triggered" always fires the moment a violation is caught,
        # "_result" reports what the rewrite call actually did — the
        # still-violating flag is the direct answer to "did it work", rather
        # than requiring a human to read the rewritten text and judge.
        if _looks_like_catalog_dump(reply_text):
            original_chars = len(reply_text)
            log.info(
                "reply_guard_triggered",
                extra={"conversation_id": conversation_id, "original_chars": original_chars},
            )
            compressed = await _compress_reply(reply_text, conversation_id)
            still_flagged = _looks_like_catalog_dump(compressed)
            log.info(
                "reply_guard_result",
                extra={
                    "conversation_id": conversation_id,
                    "original_chars": original_chars,
                    "compressed_chars": len(compressed),
                    "unchanged": compressed == reply_text,
                    "still_flagged": still_flagged,
                },
            )
            if still_flagged:
                log.warning(
                    "reply_guard_did_not_fix",
                    extra={
                        "conversation_id": conversation_id,
                        "compressed_chars": len(compressed),
                    },
                )
            reply_text = compressed

        if _RUPEE_RE.search(reply_text) and not _asked_for_price(message.text):
            original_chars = len(reply_text)
            log.info(
                "reply_price_guard_triggered",
                extra={"conversation_id": conversation_id, "original_chars": original_chars},
            )
            stripped = await _strip_unrequested_price(reply_text, conversation_id)
            still_has_price = bool(_RUPEE_RE.search(stripped))
            log.info(
                "reply_price_guard_result",
                extra={
                    "conversation_id": conversation_id,
                    "original_chars": original_chars,
                    "stripped_chars": len(stripped),
                    "unchanged": stripped == reply_text,
                    "still_has_price": still_has_price,
                },
            )
            if still_has_price:
                log.warning(
                    "reply_price_guard_did_not_fix",
                    extra={"conversation_id": conversation_id, "stripped_chars": len(stripped)},
                )
            reply_text = stripped

    if is_whatsapp_first_message:
        # Lead with the company intro, then the model's actual answer to
        # whatever the customer asked — one message, so the customer isn't
        # left waiting through two separate sends before their question is
        # addressed. split_message (app/channels.py) handles the case where
        # the combination runs past the platform's character limit.
        reply_text = f"{commands.START_MESSAGE}\n\n{reply_text}"
        log.info("whatsapp_first_message_greeted", extra={"conversation_id": conversation_id})

    await store.save_message(conversation_id, MessageRole.ASSISTANT, reply_text)

    log.info(
        "message_out",
        extra={
            "conversation_id": conversation_id,
            "chars": len(reply_text),
            "tools": tool_names,
            "rag_hits": len(chunks),
            "model": model_used,
        },
    )

    return AgentReply(
        text=reply_text,
        used_context=bool(chunks),
        tool_calls=tool_names,
        handover_triggered=ctx.handover_triggered,
        opted_out=ctx.opted_out,
        model=model_used,
        # Adapters fire these AFTER the customer has their reply.
        notifications=ctx.notifications,
    )


__all__ = ["handle_message", "TOOLS", "MAX_TOOL_ITERATIONS"]
