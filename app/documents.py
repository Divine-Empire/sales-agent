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
from typing import Any

from app import store
from app.config import settings
from app.enums import DocumentType
from app.llm import embed, transcribe_image
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
                # Deterministic per machine + chunk index, so re-uploading the
                # same document updates in place instead of duplicating.
                id=abs(hash(f"{machine_name}:{start + offset}")) % (2**63),
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
    """Full pipeline: extract, persist the machine and document, index for RAG."""
    text = await extract_text(data, filename, content_type)

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
        **result,
    }
