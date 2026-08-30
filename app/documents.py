"""Machine document ingestion — upload a brochure, get a searchable machine.

This is what makes the catalog the client's to own. Without it, adding a
machine means editing a markdown file in the repo and running a script from a
developer's laptop, which is not a product anyone can operate.

Flow: upload -> extract text -> store in machine_documents -> chunk -> embed ->
upsert into Qdrant. The machine is answerable in chat immediately afterwards.

Memory matters here: Render's free tier has 512MB and a large PDF can exhaust
it. Pages are extracted one at a time and embeddings run in small batches
rather than loading everything at once.
"""

from __future__ import annotations

import base64
import io
import json
import re
from typing import Any

from app import store
from app.config import settings
from app.enums import DocumentType
from app.llm import LLMUnavailableError, complete, embed, transcribe_image
from app.logging_config import get_logger
from app.rag import ensure_collection, extract_codes, get_client

log = get_logger(__name__)

# Beyond this the free tier risks an out-of-memory restart mid-upload.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Roughly 300 words. Long enough to hold a complete spec, short enough that a
# retrieved chunk is mostly relevant.
CHUNK_CHARS = 1800
CHUNK_OVERLAP = 200

SUPPORTED = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/plain": "txt",
    "text/markdown": "txt",
}


class ExtractionError(RuntimeError):
    """The file could not be turned into text. The message reaches the user."""


async def extract_text(data: bytes, filename: str, content_type: str | None) -> str:
    """Pull plain text out of an uploaded document.

    Raises ExtractionError with a message worth showing a user — "this looks
    like a scanned PDF" is actionable, "extraction failed" is not.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise ExtractionError(
            f"File is {len(data) // 1024 // 1024}MB. The limit is "
            f"{MAX_UPLOAD_BYTES // 1024 // 1024}MB."
        )

    kind = SUPPORTED.get(content_type or "")
    if kind is None:
        lower = filename.lower()
        if lower.endswith(".pdf"):
            kind = "pdf"
        elif lower.endswith(".docx"):
            kind = "docx"
        elif lower.endswith((".txt", ".md")):
            kind = "txt"
        else:
            raise ExtractionError(
                "Unsupported file type. Upload a PDF, Word document, or text file."
            )

    if kind == "pdf":
        return await _extract_pdf(data)
    if kind == "docx":
        return _extract_docx(data)
    return data.decode("utf-8", errors="replace").strip()


async def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise ExtractionError(f"Could not read the PDF: {exc}") from exc

    page_count = len(reader.pages)

    # Page by page, so a large document never sits in memory whole. A page
    # with no extractable text is usually a scan (a picture of a page, not
    # real text) rather than an empty page — those get OCR'd individually
    # rather than failing the whole document, since a brochure is often a
    # mix (a photo cover page, then real text pages, or vice versa).
    pages: list[str] = []
    ocr_pages: list[int] = []
    for index, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if text.strip():
            pages.append(text.strip())
        else:
            pages.append("")
            ocr_pages.append(index)

    if ocr_pages:
        if page_count > settings.ocr_max_pages:
            log.warning(
                "ocr_skipped_too_many_pages",
                extra={"page_count": page_count, "limit": settings.ocr_max_pages},
            )
        else:
            ocr_results = await _ocr_pages(data, ocr_pages)
            for index, text in ocr_results.items():
                if text:
                    pages[index] = text

    combined = "\n\n".join(p for p in pages if p).strip()
    if not combined:
        # Every page failed both real extraction and OCR — genuinely nothing
        # to work with, rather than silently storing an empty document that
        # will never answer a question.
        raise ExtractionError(
            "No text could be read from this PDF, even with OCR. The scan "
            "quality may be too low, or the page is a diagram/photo with no "
            "text. Please upload a clearer PDF, or paste the specifications "
            "directly."
        )
    return combined


async def _ocr_pages(data: bytes, page_indices: list[int]) -> dict[int, str | None]:
    """Render specific pages to images and transcribe them via GPT-4o vision.

    Rendered one page at a time (not the whole document at once) to keep
    memory bounded — the same reasoning as the page-by-page text extraction
    above, just for images instead of text.
    """
    import pypdfium2 as pdfium

    results: dict[int, str | None] = {}
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception:
        log.exception("ocr_pdf_open_failed")
        return results

    try:
        for index in page_indices:
            try:
                page = pdf[index]
                # 2x scale (~144 DPI) balances legibility for spec-sheet text
                # against image payload size sent to the vision API.
                bitmap = page.render(scale=2.0)
                pil_image = bitmap.to_pil()
                buffer = io.BytesIO()
                pil_image.save(buffer, format="PNG")
                image_b64 = base64.b64encode(buffer.getvalue()).decode()
            except Exception:
                log.exception("ocr_page_render_failed", extra={"page": index})
                results[index] = None
                continue

            text = await transcribe_image(image_b64)
            results[index] = text
            log.info("ocr_page_done", extra={"page": index, "found_text": bool(text)})
    finally:
        pdf.close()

    return results


def _extract_docx(data: bytes) -> str:
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise ExtractionError(f"Could not read the Word document: {exc}") from exc

    parts = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    # Spec sheets are usually tables; skipping them would drop the useful half.
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))

    combined = "\n".join(parts).strip()
    if not combined:
        raise ExtractionError("The document appears to be empty.")
    return combined


# The product-profile template (data/product_profile_template.md) as a tool
# schema — one nullable string field per section, so the model can genuinely
# omit what a source document doesn't cover rather than being forced to
# invent something to fill every field. `null` here means "not in this
# document", which is exactly the signal _format_profile_markdown needs to
# leave that section out rather than emit an empty heading.
PROFILE_SECTIONS = [
    ("what_it_does", "What it does"),
    ("who_should_buy", "Who should buy it"),
    ("who_should_not_buy", "Who should NOT buy it"),
    ("features", "Features"),
    ("benefits", "Benefits"),
    ("price", "Price"),
    ("competitors", "Competitors"),
    ("advantages", "Advantages"),
    ("limitations", "Limitations"),
    ("common_objections", "Common objections"),
    ("responses", "Responses"),
    ("faqs", "Frequently asked questions"),
    ("upselling_opportunities", "Upselling opportunities"),
]

# Per-field guidance beyond the generic "use null if absent" rule. Only
# `features` needs one today — found live that GPT-4o, left to its own
# summarizing instinct, turned "Reflectorless range: 0.3 to 800m, Accuracy
# (ISO 17123-3:2001): 2\", Battery BDC72 ~20 hours, Weight ~5.7kg" into a
# generic bullet like "Reflectorless laser measurement" with every number
# dropped — exactly the spec a customer asks about by name, and exactly
# what the agent needs verbatim to answer "what's the reflectorless range"
# without falling back to "I'll check with the team" on a spec that was
# genuinely right there in the source document.
_FIELD_GUIDANCE = {
    "features": (
        " List specs with their exact numbers/units as given in the document "
        "(ranges, accuracy figures, weights, battery life, included accessories, "
        "connectivity specs) — do not summarize a number into a generic "
        "description. 'Reflectorless range: 0.3 to 800m' stays exactly that, "
        "never becomes 'reflectorless laser measurement'. If the document gives "
        "you ten specs, keep ten specs."
    ),
}

# A single "properties" shape for one variant's profile, reused both for the
# single-variant case and inside the variants array below — one document can
# genuinely describe several distinct models (e.g. a Sokkia "FX-200 series"
# brochure covering both FX-201 and FX-202 with different accuracy/range
# specs each). Found live: uploading that brochure under one generic machine
# name produced one generic profile that silently dropped which spec
# belonged to which model — the FX-201/FX-202 accuracy difference the source
# document itself calls out ("FX-201 is the higher-precision 1\" variant")
# never made it into the extracted profile at all.
_VARIANT_PROPERTIES = {
    key: {
        "type": ["string", "null"],
        "description": (
            f"{label}. Use null if the document genuinely does not cover this — "
            "never invent content to fill a section." + _FIELD_GUIDANCE.get(key, "")
        ),
    }
    for key, label in PROFILE_SECTIONS
}

PROFILE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "record_product_profile",
        "description": ("Record one product profile per distinct model the document describes."),
        "parameters": {
            "type": "object",
            "properties": {
                "variants": {
                    "type": "array",
                    "description": (
                        "One entry per distinct model/variant the document actually "
                        "describes — most documents cover exactly one, so this will usually "
                        "be a single-item array. Only split into multiple entries when the "
                        "document genuinely gives separate specs for separate model numbers "
                        "(e.g. a 'series' brochure with a spec table listing different "
                        "accuracy/range/price per model). Do not split a document that just "
                        "mentions related products in passing — only when it gives each one "
                        "its own real specs."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "model_name": {
                                "type": "string",
                                "description": (
                                    "This variant's own name/model code as the document gives "
                                    "it, e.g. 'Sokkia FX-201 Total Station' — not the generic "
                                    "series name."
                                ),
                            },
                            **_VARIANT_PROPERTIES,
                        },
                        "required": ["model_name", *(key for key, _ in PROFILE_SECTIONS)],
                    },
                },
            },
            "required": ["variants"],
        },
    },
}

PROFILE_PROMPT = """You turn a raw product document (a brochure, spec sheet, or manual) into a
structured profile for a sales agent to use later.

Extract only what the document actually supports — never invent a feature, a competitor, an
objection, or an FAQ that isn't genuinely there. A section the document doesn't cover should be
left null, not padded with generic or plausible-sounding content. This matters more than
completeness: a missing section costs nothing, a fabricated one costs the sales team's
credibility the moment a customer catches it.

NEVER summarize a number, a range, or a spec into vague prose. If the source says "Reflectorless
range: 0.3 to 800m" or "Accuracy: 2 inch" or "Battery BDC72, approx. 20 hours", that exact figure
must appear in your output — not "long measurement range" or "good battery life". A sales agent
that can't quote the number a customer asked about is worse than one with no answer at all,
because it looks like it's dodging the question. Specs are the single most valuable thing a
brochure gives you; do not compress them away in the name of a tidy summary. When a document is
dense with specs (a full spec sheet), your Features section should be long and specific, not
short and generic — length is not a problem here, vagueness is.

Where the document gives you the raw material but not the finished shape — e.g. it lists specs
but never states the benefit of each one, or it never explicitly poses objections/FAQs but the
content answers ones a reasonable buyer would ask — you may synthesize a reasonable inference
FROM material that is actually present, but do not introduce facts, numbers, or claims that
aren't grounded in the document itself. If in doubt, leave the section null.

MULTIPLE MODELS IN ONE DOCUMENT — READ THIS CAREFULLY, IT IS A COMMON CASE, NOT AN EDGE CASE:
Before writing anything, scan the ENTIRE document text for every distinct model number/name it
mentions (e.g. "FX-201", "FX-202", "iM-55", "iM-65"). Build a mental list of every one you find.
A "series" brochure or comparison spec sheet routinely covers TWO OR MORE models in one document,
each with its own accuracy/range/price/weight numbers — e.g. a "Sokkia FX-200 series" brochure
that gives FX-201 one set of specs and FX-202 a genuinely different set. If your list has more
than one distinct model with its own specs, you MUST output one variants[] entry for EACH ONE —
this is not optional and skipping any of them is a hard failure. A response that names only the
first model and silently drops the rest is exactly as bad as inventing a fake spec: the customer
asking about the model you dropped gets no answer, or worse, gets the other model's numbers by
mistake. Re-check your own output before finishing: count the distinct model numbers in the
source text, then count your variants[] entries — if the source mentions N models, your array
must have N entries, not fewer.

Each entry's model_name must be that specific model as the document names it (e.g. "Sokkia
FX-201", never the generic series name "FX-200 Series"). Never merge two models' different
numbers into one generic profile, and never let one model's specs leak into another's entry.
Content that applies to the whole series (shared accessories, shared warranty, a general
description) may be repeated across each variant's own profile where relevant.

Conversely, do not manufacture variants that aren't really there — a document that just mentions
a sibling product in passing, with no separate specs of its own, describes ONE model; return a
single-item variants[] list in that ordinary case, which is most documents."""


# Same shape as rag.py's _CODE_RE/extract_codes — deliberately not imported
# from there to avoid a documents.py -> rag.py dependency for one regex;
# this is a code-*count* backstop, not a retrieval concern, so it stays
# local. Kept in sync by eye since both patterns are simple and stable.
_MODEL_CODE_RE = re.compile(r"\b([A-Z]{1,6}[-\s]?\d{1,4}[A-Z]?(?:[-–]\d[A-Z]?)?)\b")


def _model_family_prefix(machine_name: str) -> str | None:
    """The letter-prefix of the machine's own code family (e.g. "Sokkia
    FX-200 Series" -> "FX"), used to scope which codes in the raw text
    plausibly refer to a sibling model of THIS product line — a battery
    code (BDC70) or an IP rating (IP66) mentioned in the same brochure is
    not a missed variant just because it's shaped like a code too. Returns
    None if the machine name has no code-shaped token to anchor on, in
    which case the missed-variant check is skipped entirely rather than
    guessed at."""
    match = re.search(r"\b([A-Za-z]{1,6})-?\d{1,4}\b", machine_name)
    return match.group(1).upper() if match else None


def _find_model_codes(text: str, family_prefix: str | None) -> set[str]:
    """Distinct probable model codes in raw source text that share the
    product's own family prefix (see _model_family_prefix) — used only to
    sanity-check the LLM's variant count against what the document itself
    actually mentions for THIS product line, not for retrieval, and
    deliberately not for unrelated codes (battery packs, IP ratings,
    accessory part numbers) that happen to also look code-shaped."""
    if not family_prefix:
        return set()
    codes = {m.group(1).replace(" ", "-").upper() for m in _MODEL_CODE_RE.finditer(text)}
    return {c for c in codes if not c.isdigit() and c.split("-")[0] == family_prefix}


async def _structure_profile_call(
    raw_text: str,
    machine_name: str,
    conversation_id: str | None,
    extra_instruction: str = "",
) -> list[dict[str, str]] | None:
    """One structuring completion call — returns parsed variant profiles or
    None on any failure. Separated from structure_product_profile so a retry
    with a stronger nudge can reuse the same parsing/validation logic."""
    try:
        response = await complete(
            [
                {"role": "system", "content": PROFILE_PROMPT + extra_instruction},
                {
                    "role": "user",
                    "content": f"Machine: {machine_name}\n\nDocument text:\n\n{raw_text}",
                },
            ],
            tools=[PROFILE_TOOL],
            temperature=0.1,  # extraction, not conversation — low variance wins
            max_output_tokens=4000,  # a multi-variant document needs a full profile per model
            conversation_id=conversation_id,
        )
    except LLMUnavailableError:
        log.warning("profile_structuring_llm_unavailable", extra={"machine": machine_name})
        return None

    if not response.tool_calls:
        log.warning("profile_structuring_no_tool_call", extra={"machine": machine_name})
        return None

    try:
        data = json.loads(response.tool_calls[0].function.arguments)
    except json.JSONDecodeError:
        log.warning("profile_structuring_bad_json", extra={"machine": machine_name})
        return None
    if not isinstance(data, dict):
        return None

    raw_variants = data.get("variants")
    if not isinstance(raw_variants, list) or not raw_variants:
        log.warning("profile_structuring_no_variants", extra={"machine": machine_name})
        return None

    profiles: list[dict[str, str]] = []
    for entry in raw_variants:
        if not isinstance(entry, dict):
            continue
        model_name = entry.get("model_name")
        if not isinstance(model_name, str) or not model_name.strip():
            continue
        profile = {
            key: value.strip()
            for key, _ in PROFILE_SECTIONS
            if isinstance(value := entry.get(key), str) and value.strip()
        }
        if not profile:
            continue
        profile["model_name"] = model_name.strip()
        profiles.append(profile)

    return profiles or None


async def structure_product_profile(
    raw_text: str, machine_name: str, conversation_id: str | None = None
) -> list[dict[str, str]] | None:
    """Turn raw extracted document text into one or more rich product-profile
    shapes (data/product_profile_template.md) via one LLM call (plus a
    conditional second call — see below).

    A single uploaded document sometimes covers more than one distinct model
    (e.g. a Sokkia "FX-200 series" brochure with separate FX-201/FX-202 specs
    in the same file) — collapsing those into one generic profile silently
    drops which spec belongs to which model, which is worse than not having
    structured the document at all. So this always returns a *list*: most
    documents genuinely describe one model and come back as a single-item
    list; only a document that actually gives separate models their own
    specs comes back with more than one entry.

    The prompt alone is not a reliable guarantee here — verified live: at
    temperature 0.1, the same two-model source text came back with only one
    variant (silently dropping the second model) in 3 of 5 runs even with an
    explicit "count the models, match the count" instruction in the prompt.
    This matches the established pattern elsewhere in this codebase (the
    catalog-dump and unrequested-price guards in app/agent.py) — a prompt
    instruction on GPT-4o is a strong nudge, never a hard guarantee, and a
    "never do X" rule that actually matters needs a code-level backstop.
    So: _find_model_codes() counts distinct model-shaped codes actually
    present in the raw source text; if the first call returned fewer
    variants than that, one retry runs with an explicit instruction naming
    exactly which codes were missed. If the retry still comes up short, the
    (partial but non-empty) result is still used — this only ever costs one
    extra completion call, and it only fires on the failure case.

    Each list entry is a dict of {"model_name": str, section_key: text, ...}
    with only the sections that entry's source material actually supported —
    a section the model left null is simply absent, never filled with
    placeholder text. Returns None on any failure (LLM unavailable,
    malformed tool call), so the caller can fall back to ingesting the raw
    text unchanged rather than losing the upload entirely.
    """
    profiles = await _structure_profile_call(raw_text, machine_name, conversation_id)
    if profiles is None:
        return None

    family_prefix = _model_family_prefix(machine_name)
    source_codes = _find_model_codes(raw_text, family_prefix)
    found_codes = {
        c for p in profiles for c in _find_model_codes(p.get("model_name", ""), family_prefix)
    }
    missed_codes = source_codes - found_codes

    if missed_codes and len(profiles) < len(source_codes):
        log.warning(
            "profile_structuring_missed_variants",
            extra={
                "machine": machine_name,
                "variant_count": len(profiles),
                "missed_codes": sorted(missed_codes),
            },
        )
        retry_instruction = (
            "\n\nIMPORTANT — A PREVIOUS ATTEMPT AT THIS SAME DOCUMENT MISSED SOME MODELS. The "
            f"following model code(s) appear in the source text but were NOT given their own "
            f"variants[] entry last time: {', '.join(sorted(missed_codes))}. Find each of these "
            "in the document and give every one its own complete variants[] entry with its own "
            "specs, in addition to any other models you find. Do not drop any of them again."
        )
        retried = await _structure_profile_call(
            raw_text, machine_name, conversation_id, extra_instruction=retry_instruction
        )
        if retried and len(retried) > len(profiles):
            profiles = retried

    log.info(
        "profile_structured",
        extra={
            "machine": machine_name,
            "variant_count": len(profiles),
            "sections_filled": sum(len(p) - 1 for p in profiles),
        },
    )
    return profiles or None


def format_profile_markdown(profile: dict[str, str], machine_name: str) -> str:
    """Render a single variant's structured profile as `##`/`###` markdown —
    the same shape `data/knowledge_base.md` uses, so chunk_text's paragraph/
    heading-based splitting handles it with no changes.

    `machine_name` is the heading title — callers pass the variant's own
    model_name when a document produced more than one variant, so each
    resulting document/Qdrant chunk is self-describing rather than sharing a
    generic series title across genuinely different models.
    """
    lines = [f"## {machine_name}", ""]
    for key, label in PROFILE_SECTIONS:
        if key == "model_name":
            continue
        text = profile.get(key)
        if not text:
            continue
        lines.append(f"### {label}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).strip()


def chunk_text(text: str, machine_name: str) -> list[str]:
    """Split on paragraph boundaries, packing up to CHUNK_CHARS.

    Every chunk is prefixed with the machine name so a retrieved fragment is
    self-describing — otherwise a chunk of bare specifications gives the model
    no way to know which machine it belongs to.
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buffer = ""

    for paragraph in paragraphs:
        if len(buffer) + len(paragraph) + 2 <= CHUNK_CHARS:
            buffer = f"{buffer}\n\n{paragraph}" if buffer else paragraph
            continue
        if buffer:
            chunks.append(buffer)
        # A single paragraph larger than the budget gets hard-split.
        while len(paragraph) > CHUNK_CHARS:
            chunks.append(paragraph[:CHUNK_CHARS])
            paragraph = paragraph[CHUNK_CHARS - CHUNK_OVERLAP :]
        buffer = paragraph
    if buffer:
        chunks.append(buffer)

    return [f"## {machine_name}\n{chunk}" for chunk in chunks]


def _stable_point_id(key: str) -> int:
    """A Qdrant point id that is stable across processes and deploys.

    The previous version used Python's builtin `hash()`, which is
    process-randomized for strings by default (PYTHONHASHSEED) — the same
    machine+chunk-index produced a DIFFERENT id every time the backend
    restarted (a new Render deploy, a worker process cycling). The
    "re-uploading the same document updates in place" comment this scheme's
    docstring made was therefore false across restarts: a re-upload or edit
    after a deploy silently added a second, orphaned copy of every chunk
    instead of replacing the first, so a customer's answer could come from
    stale content sitting alongside the corrected version. Found while
    reviewing the multi-variant work, not something that surfaced through a
    customer report — fixed before it did. `hashlib.md5` is deterministic
    for the same input in any process, any process, forever.
    """
    import hashlib

    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return int(digest[:16], 16) % (2**63)


async def ingest_document(
    *,
    machine_name: str,
    category: str,
    text: str,
    machine_code: str | None = None,
    machine_id: str | None = None,
    doc_type: DocumentType = DocumentType.BROCHURE,
    price_range: str | None = None,
    source_filename: str | None = None,
) -> dict[str, Any]:
    """Chunk, embed and upsert a document's text into Qdrant.

    Returns a summary the dashboard can show the user. Raises nothing on
    partial failure — the caller gets counts and can report honestly.
    """
    from qdrant_client import models

    chunks = chunk_text(text, machine_name)
    if not chunks:
        return {"chunks": 0, "embedded": 0, "error": "no text to index"}

    if not await ensure_collection():
        return {"chunks": len(chunks), "embedded": 0, "error": "vector store unavailable"}

    client = get_client()
    if client is None:
        return {"chunks": len(chunks), "embedded": 0, "error": "vector store unavailable"}

    codes = extract_codes(text)
    if machine_code:
        codes = sorted({machine_code.upper(), *codes})

    # A re-ingest (edit, re-upload) must replace this machine's OLD chunks,
    # not just add new ones alongside them — the old scheme's point ids
    # weren't stable enough for upsert-in-place to reliably do that across a
    # process restart (see _stable_point_id). Deleting by machine_id first
    # makes this correct regardless: fewer new chunks than before leaves no
    # orphaned tail, and it degrades safely — a delete failure here just
    # means the upsert below adds alongside whatever's left, no worse than
    # the previous behavior, never a hard failure of the upload itself.
    if machine_id:
        try:
            await client.delete(
                collection_name=settings.qdrant_collection,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="machine_id", match=models.MatchValue(value=machine_id)
                            )
                        ]
                    )
                ),
            )
        except Exception:
            log.exception("ingest_pre_delete_failed", extra={"machine_id": machine_id})

    # id_key is scoped to machine_id when known (a real UUID, so two machines
    # can never collide even if they briefly share a display name) and falls
    # back to machine_name only for the rare caller that doesn't have an id
    # yet — still deterministic, just a narrower guarantee.
    id_key_base = machine_id or machine_name

    embedded = 0
    batch_size = 16  # small batches keep peak memory low on the free tier
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        vectors = await embed(batch)
        if len(vectors) != len(batch):
            log.error(
                "ingest_embed_mismatch",
                extra={"expected": len(batch), "got": len(vectors)},
            )
            break
        points = [
            models.PointStruct(
                # Deterministic per machine + chunk index, and stable across
                # restarts (see _stable_point_id) — combined with the
                # pre-delete above, a re-upload/edit genuinely replaces the
                # old content rather than risking a stale duplicate sitting
                # alongside it.
                id=_stable_point_id(f"{id_key_base}:{start + offset}"),
                vector=vector,
                payload={
                    "text": chunk,
                    "title": machine_name,
                    "category": category,
                    "codes": codes,
                    "machine_id": machine_id,
                    "price_range": price_range,
                    "source": source_filename or "upload",
                    "doc_type": str(doc_type),
                },
            )
            for offset, (chunk, vector) in enumerate(zip(batch, vectors, strict=True))
        ]
        try:
            await client.upsert(collection_name=settings.qdrant_collection, points=points)
            embedded += len(points)
        except Exception:
            log.exception("ingest_upsert_failed", extra={"machine": machine_name})
            break

    log.info(
        "document_ingested",
        extra={
            "machine": machine_name,
            "chunks": len(chunks),
            "embedded": embedded,
            "codes": codes[:5],
        },
    )
    return {"chunks": len(chunks), "embedded": embedded, "codes": codes}


async def ingest_accessory(
    *,
    accessory_id: str,
    name: str,
    category: str | None,
    description: str | None,
) -> dict[str, Any]:
    """Embed one accessory/part as a single Qdrant point so the agent can
    recommend it during a conversation.

    Reuses the same chunk/embed/upsert machinery as machine documents rather
    than a parallel pipeline — accessory text is short (name + description),
    so it rarely needs more than one chunk. The `record_type` payload field
    is the only new thing: a discriminator so retrieval can eventually tell
    machines and accessories apart, if that's ever needed.
    """
    from qdrant_client import models

    text = f"{name}\n{description or ''}".strip()
    if not text:
        return {"chunks": 0, "embedded": 0, "error": "no text to index"}

    if not await ensure_collection():
        return {"chunks": 1, "embedded": 0, "error": "vector store unavailable"}

    client = get_client()
    if client is None:
        return {"chunks": 1, "embedded": 0, "error": "vector store unavailable"}

    chunk = f"## {name} (accessory/part)\n{text}"
    vectors = await embed([chunk])
    if not vectors:
        log.error("accessory_ingest_embed_failed", extra={"accessory_id": accessory_id})
        return {"chunks": 1, "embedded": 0, "error": "embedding failed"}

    point = models.PointStruct(
        # Deterministic per accessory id, so re-editing updates the same
        # point in place instead of duplicating — mirrors ingest_document's
        # deterministic id scheme for machine chunks. Uses _stable_point_id
        # (hashlib, not the builtin hash()) for the same reason documented
        # there: builtin hash() is process-randomized for strings, so this
        # accessory's insert id and delete_accessory_from_index's delete id
        # would silently stop matching after any backend restart, leaving an
        # orphaned point no delete call could ever reach again.
        id=_stable_point_id(f"accessory:{accessory_id}"),
        vector=vectors[0],
        payload={
            "text": chunk,
            "title": name,
            "category": category,
            "codes": [],
            "machine_id": None,
            "accessory_id": accessory_id,
            "price_range": None,
            "source": "manual",
            "record_type": "accessory",
        },
    )
    try:
        await client.upsert(collection_name=settings.qdrant_collection, points=[point])
    except Exception:
        log.exception("accessory_ingest_upsert_failed", extra={"accessory_id": accessory_id})
        return {"chunks": 1, "embedded": 0, "error": "upsert failed"}

    # "name" is a reserved LogRecord attribute (the logger's own name) — using
    # it as an extra key raises KeyError at log time, not at review time, so
    # this was never actually caught until an accessory insert first
    # succeeded end-to-end (earlier attempts failed on a missing table before
    # ever reaching this line). accessory_name avoids the collision.
    log.info("accessory_ingested", extra={"accessory_id": accessory_id, "accessory_name": name})
    return {"chunks": 1, "embedded": 1}


async def delete_accessory_from_index(accessory_id: str) -> None:
    """Remove an accessory's Qdrant point(s).

    Deletes by payload filter on `accessory_id` rather than by computing the
    expected point id and deleting that single id directly. This matters
    because of a real migration edge: any accessory ingested before the
    _stable_point_id fix used the old, process-randomized `hash()` scheme,
    so its actual stored point id does not equal what this function would
    compute today — a by-id delete would silently delete nothing and leave
    that accessory's stale point behind forever. A payload filter finds the
    point regardless of which id scheme created it, at the one-time cost of
    a filtered scan instead of a direct point lookup (accessories are few
    and this call is not on any hot path)."""
    from qdrant_client import models

    client = get_client()
    if client is None:
        return
    try:
        await client.delete(
            collection_name=settings.qdrant_collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="accessory_id", match=models.MatchValue(value=accessory_id)
                        )
                    ]
                )
            ),
        )
    except Exception:
        log.exception("accessory_index_delete_failed", extra={"accessory_id": accessory_id})


async def delete_machine_from_index(machine_id: str) -> None:
    """Remove every Qdrant chunk belonging to a machine, by payload filter
    rather than a deterministic id — unlike an accessory, a machine's
    document can chunk into many points (chunk_text splits on paragraph
    boundaries), so there's no single id to target. Best-effort: deleting
    the Postgres row must not be blocked by a Qdrant hiccup, since the
    machine is already gone from the catalog either way; a stray orphaned
    chunk is a smaller problem than losing the delete entirely."""
    from qdrant_client import models

    client = get_client()
    if client is None:
        return
    try:
        await client.delete(
            collection_name=settings.qdrant_collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="machine_id", match=models.MatchValue(value=machine_id)
                        )
                    ]
                )
            ),
        )
    except Exception:
        log.exception("machine_index_delete_failed", extra={"machine_id": machine_id})


def _variant_machine_code(base_code: str, model_name: str, index: int) -> str:
    """A distinct machine_code per detected variant — exact-code RAG lookup
    (`rag._exact_code_matches`) and Qdrant payload filtering both key off
    this, so two variants sharing one code would blur back together exactly
    the way the original FX-201/FX-202 bug did. Prefer a code pulled from the
    variant's own model name (e.g. "FX-201" out of "Sokkia FX-201 Total
    Station") so it reads sensibly; fall back to base+index only if nothing
    code-shaped is found in the name."""
    match = re.search(r"[A-Za-z]{1,4}-?\d{2,5}[A-Za-z]?", model_name)
    if match:
        return match.group(0).upper().replace(" ", "-")[:40]
    return f"{base_code}-{index + 1}"[:40]


async def add_machine_from_document(
    *,
    name: str,
    category: str,
    data: bytes,
    filename: str,
    content_type: str | None,
    machine_code: str | None = None,
    description: str | None = None,
    price_range: str | None = None,
    lead_time: str | None = None,
    doc_type: DocumentType = DocumentType.BROCHURE,
) -> dict[str, Any]:
    """Full pipeline: extract, restructure into the rich product-profile
    shape, persist the machine(s) and document(s), index for RAG.

    The client only wants to type one machine name and upload one document —
    everything else (What it does / Who should buy it / Objections /
    Responses / FAQs / etc., data/product_profile_template.md's shape) comes
    from structure_product_profile analysing the extracted text. That
    analysis can find more than one distinct model in a single document (a
    "series" brochure listing separate specs per model, e.g. FX-201 vs
    FX-202) — when it does, this creates one `machines` row, one
    machine_document, and one RAG-ingested document PER detected variant,
    each with its own machine_code, rather than merging them into one
    profile. This is what keeps later retrieval from mixing the two models'
    specs together: exact-code lookup and Qdrant payload filtering both key
    off machine_code/machine_id, so distinct codes are what keeps "FX-201
    accuracy" from ever answering with FX-202's number.

    Falls back to one machine using the raw extracted text unchanged if
    structuring fails entirely, so an LLM hiccup costs richness, never the
    whole upload.
    """
    text = await extract_text(data, filename, content_type)
    profiles = await structure_product_profile(text, name)

    if not profiles:
        machine_id = await store.upsert_machine(
            machine_code=machine_code or name.upper().replace(" ", "-")[:40],
            name=name,
            category=category,
            description=description,
            price_range=price_range,
            lead_time=lead_time,
        )
        await store.save_machine_document(
            machine_id=machine_id,
            doc_type=doc_type,
            title=filename,
            content=text,
        )
        result = await ingest_document(
            machine_name=name,
            category=category,
            text=text,
            machine_code=machine_code,
            machine_id=machine_id,
            doc_type=doc_type,
            price_range=price_range,
            source_filename=filename,
        )
        return {
            "machine_id": machine_id,
            "name": name,
            "characters_extracted": len(text),
            "profile_sections_filled": 0,
            "variants_detected": 1,
            **result,
        }

    base_code = machine_code or name.upper().replace(" ", "-")[:40]
    single_variant = len(profiles) == 1
    created: list[dict[str, Any]] = []

    for index, profile in enumerate(profiles):
        variant_name = profile["model_name"] if not single_variant else name
        variant_code = (
            base_code if single_variant else _variant_machine_code(base_code, variant_name, index)
        )
        structured_text = format_profile_markdown(profile, variant_name)

        machine_id = await store.upsert_machine(
            machine_code=variant_code,
            name=variant_name,
            category=category,
            description=description,
            price_range=price_range,
            lead_time=lead_time,
        )
        await store.save_machine_document(
            machine_id=machine_id,
            doc_type=doc_type,
            title=filename if single_variant else f"{filename} — {variant_name}",
            content=structured_text,
        )
        result = await ingest_document(
            machine_name=variant_name,
            category=category,
            text=structured_text,
            machine_code=variant_code,
            machine_id=machine_id,
            doc_type=doc_type,
            price_range=price_range,
            source_filename=filename,
        )
        created.append(
            {
                "machine_id": machine_id,
                "name": variant_name,
                "machine_code": variant_code,
                "profile_sections_filled": len(profile) - 1,
                **result,
            }
        )

    primary = created[0]
    return {
        **primary,
        "characters_extracted": len(text),
        "variants_detected": len(created),
        "variants": created,
    }
