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

import json
from typing import Any

from app import commands, prompts, rag, store
from app.enums import (
    AiLogEvent,
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
                "Escalate to the human sales team. Call this when the customer asks for a formal "
                "quotation, wants to negotiate price, needs a bulk or multi-unit order, asks to "
                "speak to a person, or asks something you cannot answer from the product context. "
                "Always call it rather than guessing at a commercial commitment."
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
    await store.set_conversation_status(ctx.conversation_id, ConversationStatus.HANDED_OVER)
    ctx.handover_triggered = True
    ctx.handover_context = context
    ctx.notifications.append({"type": "handoff", "reason": reason.value, "context": context})
    log.info(
        "tool_handoff",
        extra={"conversation_id": ctx.conversation_id, "reason": reason.value},
    )
    # Give the model the contact block so it delivers them in its own words.
    return (
        "Handoff recorded and the sales team has been notified. "
        f"Share these contact details with the customer: {prompts.SALES_PHONE}, "
        f"WhatsApp {prompts.WHATSAPP_NUMBER}, {prompts.WEBSITE}. "
        "Available Monday to Sunday, 9:30 AM to 6:30 PM."
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


async def handle_message(message: IncomingMessage) -> AgentReply:
    """Process one inbound message and produce a reply.

    Never raises: every failure path returns a sendable message.
    """
    conversation_id = message.conversation_id
    log.info(
        "message_in",
        extra={
            "conversation_id": conversation_id,
            "channel": str(message.channel),
            "chars": len(message.text),
        },
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

    # Retrieval failure is not fatal — answer without context and log it.
    chunks = await rag.search(message.text, conversation_id=conversation_id)
    context_block = rag.build_context(chunks)

    history = [
        {"role": m.role, "content": m.content}
        for m in await store.get_history(conversation_id)
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
            messages.append(
                {
                    "role": "assistant",
                    "content": response.content,
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
        return AgentReply(text=prompts.ERROR_MESSAGE, model="none")

    if not reply_text.strip():
        # The model returned nothing usable; fall back to something sendable.
        reply_text = (
            prompts.OPT_OUT_MESSAGE
            if ctx.opted_out
            else prompts.HANDOFF_MESSAGE
            if ctx.handover_triggered
            else prompts.ERROR_MESSAGE
        )

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
