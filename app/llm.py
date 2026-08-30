"""LLM gateway — the single place any model call is made.

Business logic never imports `openai` directly. Routing, fallback, and retry
policy live here and nowhere else, which is what keeps the Groq fallback five
lines instead of fifty.

Fallback fires only on *retryable* errors: rate limit, timeout, connection
failure, 5xx. A 400 means the request itself is malformed and would fail
identically on Groq — that is a bug to fix, not to retry.

Both providers speak the OpenAI wire format, so one SDK with two clients is
the whole implementation.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

# Errors where a different provider might plausibly succeed. Everything else
# (400 bad request, 401 auth, 404 unknown model) fails the same way on Groq.
RETRYABLE = (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError)


class LLMUnavailableError(RuntimeError):
    """Both providers failed. Callers must still send the user *something*."""


@dataclass
class LLMResponse:
    """One completion, plus what it cost and who served it."""

    content: str | None
    tool_calls: list[Any] = field(default_factory=list)
    model: str = ""
    provider: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    used_fallback: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


_primary: AsyncOpenAI | None = None
_fallback: AsyncOpenAI | None = None


def _primary_client() -> AsyncOpenAI | None:
    global _primary
    if _primary is None and settings.openai_api_key:
        _primary = AsyncOpenAI(
            api_key=settings.openai_api_key,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,  # retry policy is ours, not the SDK's
        )
    return _primary


def _fallback_client() -> AsyncOpenAI | None:
    global _fallback
    if _fallback is None and settings.groq_api_key:
        _fallback = AsyncOpenAI(
            api_key=settings.groq_api_key,
            base_url=settings.groq_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,
        )
    return _fallback


def _extract(response: Any, model: str, provider: str, started: float) -> LLMResponse:
    choice = response.choices[0]
    usage = getattr(response, "usage", None)
    return LLMResponse(
        content=choice.message.content,
        tool_calls=list(choice.message.tool_calls or []),
        model=model,
        provider=provider,
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


# Matches gpt-5, gpt-5-mini, gpt-5-pro, gpt-5.1, gpt-5.2, gpt-5.6-sol, etc.
# but not gpt-4o/gpt-4.1/gpt-40-something — anchored so a future "gpt-50-x"
# name (unlikely, but not impossible) can't false-match.
_GPT5_FAMILY_RE = re.compile(r"^gpt-5([.\-]|$)")


def _is_gpt5_family(model: str) -> bool:
    return bool(_GPT5_FAMILY_RE.match(model))


def _apply_model_sampling(kwargs: dict[str, Any], model: str, reasoning_effort: str | None) -> None:
    """Swap temperature for reasoning_effort on GPT-5-family models.

    GPT-5-family models reject `temperature` outright (400 Bad Request) on
    every release except gpt-5.1+ with reasoning_effort="none" — simplest
    and most robust is to never send temperature to this family at all, and
    send reasoning_effort instead, which is the parameter this family
    actually uses to trade latency for answer quality. Mutates kwargs in
    place; a no-op for gpt-4o and anything else outside the gpt-5 family, so
    existing behavior for the current default model is unchanged.

    Found live: with `tools` present, gpt-5.6-terra rejected ANY
    reasoning_effort other than "none" on /v1/chat/completions — "Function
    tools with reasoning_effort are not supported ... set reasoning_effort
    to 'none'." This hits every tool-call caller in this codebase (the
    agent's 3 tools, document-profile structuring, lead-scoring analysis),
    so a configured effort is only honored for a plain chat completion;
    whenever `tools` is present, effort is forced to "none" regardless of
    what was requested — reasoning still happens, just not exposed as a
    separate effort dial for this call shape on this API surface.
    """
    if not _is_gpt5_family(model):
        return
    kwargs.pop("temperature", None)
    if kwargs.get("tools"):
        kwargs["reasoning_effort"] = "none"
    else:
        kwargs["reasoning_effort"] = reasoning_effort or settings.llm_reasoning_effort


async def complete(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    reasoning_effort: str | None = None,
    conversation_id: str | None = None,
) -> LLMResponse:
    """Run a chat completion, falling back to Groq on retryable failures.

    Raises LLMUnavailableError only when both providers are exhausted. Callers
    must catch it and still reply to the user — silence reads as broken.

    `reasoning_effort` only applies to a GPT-5-family `openai_model` (see
    _apply_model_sampling) — passed through so a caller doing something
    slower/more-careful (document structuring, lead scoring) can ask for
    more effort than the chat-reply default, without every caller needing
    to know or care whether the configured model is GPT-5-family at all.
    """
    temperature = settings.llm_temperature if temperature is None else temperature
    kwargs: dict[str, Any] = {
        "messages": messages,
        "temperature": temperature,
        # Latency scales with output length far more than input. The prompt
        # asks for short replies; this is the guardrail for when it does not.
        # A caller doing something other than a chat reply (e.g. restructuring
        # a whole document into a product profile) can override this budget —
        # same reasoning transcribe_image has its own ocr_max_output_tokens.
        "max_completion_tokens": (
            max_output_tokens if max_output_tokens is not None else settings.llm_max_output_tokens
        ),
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    log_ctx = {"conversation_id": conversation_id} if conversation_id else {}

    primary = _primary_client()
    primary_error: Exception | None = None

    if primary is not None:
        primary_kwargs = dict(kwargs)
        _apply_model_sampling(primary_kwargs, settings.openai_model, reasoning_effort)
        started = time.perf_counter()
        try:
            raw = await primary.chat.completions.create(
                model=settings.openai_model, **primary_kwargs
            )
            result = _extract(raw, settings.openai_model, "openai", started)
            log.info(
                "llm_call",
                extra={
                    **log_ctx,
                    "provider": "openai",
                    "model": result.model,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                    "latency_ms": result.latency_ms,
                    "tool_calls": len(result.tool_calls),
                },
            )
            return result
        except RETRYABLE as exc:
            primary_error = exc
            # Loud on purpose: if a demo ran on the fallback we want to know.
            log.warning(
                "llm_fallback",
                extra={
                    **log_ctx,
                    "from": "openai",
                    "to": "groq",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:200],
                },
            )
        except Exception as exc:
            # Non-retryable (400/401/404): Groq would fail identically.
            log.exception(
                "llm_error_non_retryable",
                extra={**log_ctx, "provider": "openai", "error_type": type(exc).__name__},
            )
            raise LLMUnavailableError(f"openai: {exc}") from exc
    else:
        log.warning("llm_primary_not_configured", extra=log_ctx)

    fallback = _fallback_client()
    if fallback is None:
        raise LLMUnavailableError(f"no fallback configured; primary failed: {primary_error}")

    fallback_kwargs = dict(kwargs)
    _apply_model_sampling(fallback_kwargs, settings.groq_model, reasoning_effort)
    started = time.perf_counter()
    try:
        raw = await fallback.chat.completions.create(model=settings.groq_model, **fallback_kwargs)
        result = _extract(raw, settings.groq_model, "groq", started)
        result.used_fallback = True
        log.info(
            "llm_call",
            extra={
                **log_ctx,
                "provider": "groq",
                "model": result.model,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "latency_ms": result.latency_ms,
                "tool_calls": len(result.tool_calls),
                "used_fallback": True,
            },
        )
        return result
    except Exception as exc:
        log.exception(
            "llm_error_all_providers_failed",
            extra={**log_ctx, "provider": "groq", "error_type": type(exc).__name__},
        )
        raise LLMUnavailableError(f"groq: {exc}") from exc


async def transcribe_image(image_b64: str, *, prompt: str | None = None) -> str | None:
    """OCR a page image via GPT-4o vision. OpenAI only — Groq's chat models in
    use here are text-only, so there is no fallback path, same as `embed()`.

    Used for scanned/image-only PDF pages (app/documents.py), where pypdf
    extracts nothing because the "text" is actually a picture of text. Returns
    None on failure so the caller can report a clean "couldn't read this
    page" rather than storing a hallucinated transcription.
    """
    client = _primary_client()
    if client is None:
        return None
    started = time.perf_counter()
    ocr_kwargs: dict[str, Any] = {
        "temperature": 0.0,
        "max_completion_tokens": settings.ocr_max_output_tokens,
        "timeout": settings.ocr_timeout_seconds,
    }
    # Same GPT-5-family incompatibility as complete() — a hardcoded
    # temperature=0.0 here would 400 the moment openai_model is switched to
    # any gpt-5* model, silently breaking OCR fallback for scanned PDFs.
    _apply_model_sampling(ocr_kwargs, settings.openai_model, None)
    try:
        raw = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt
                            or (
                                "Transcribe every word of visible text on this page exactly "
                                "as it appears, preserving tables and structure with plain "
                                "text/markdown. If the page is a diagram or photo with no "
                                "readable text, reply with exactly: NO_TEXT_FOUND"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                        },
                    ],
                }
            ],
            **ocr_kwargs,
        )
    except Exception:
        log.exception("vision_transcribe_failed")
        return None

    text = (raw.choices[0].message.content or "").strip()
    # The model sometimes wraps its transcription in a markdown code fence
    # even though the prompt asks for plain text/markdown — strip it so a
    # stray ``` never ends up stored in machine_documents or a RAG chunk.
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text).strip()
    latency = int((time.perf_counter() - started) * 1000)
    log.info("vision_transcribe", extra={"latency_ms": latency, "chars": len(text)})
    if not text or text == "NO_TEXT_FOUND":
        return None
    return text


async def embed(texts: list[str], *, conversation_id: str | None = None) -> list[list[float]]:
    """Embed text for RAG. OpenAI only — Groq has no embeddings endpoint, so
    there is no fallback path here by design.

    Returns [] on failure; the caller degrades to answering without retrieval
    rather than failing the turn.
    """
    client = _primary_client()
    if client is None or not texts:
        return []
    started = time.perf_counter()
    try:
        raw = await client.embeddings.create(model=settings.embedding_model, input=texts)
        latency = int((time.perf_counter() - started) * 1000)
        log.info(
            "embed_call",
            extra={
                "conversation_id": conversation_id,
                "model": settings.embedding_model,
                "count": len(texts),
                "latency_ms": latency,
            },
        )
        return [item.embedding for item in raw.data]
    except Exception:
        log.exception(
            "embed_failed",
            extra={"conversation_id": conversation_id, "count": len(texts)},
        )
        return []
