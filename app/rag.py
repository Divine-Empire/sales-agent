"""Product knowledge retrieval over Qdrant.

Chunked on document structure, not fixed token counts. The knowledge base is
mostly markdown price tables, so a chunk is one `###` sub-section — the whole
"Bar Bending & Cutting Machines" table stays together. Splitting a price table
at an arbitrary token boundary would strand rows from their headers and let the
agent quote a price against the wrong product.

Retrieval failure is never fatal: search returns [] and the agent answers
without context rather than failing the turn. Weak retrieval is handled
explicitly by the caller — saying "let me check with the team" beats inventing
a specification, which is the single worst demo failure.

Ingestion:  uv run python -m app.rag
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path

from qdrant_client import AsyncQdrantClient, models

from app.config import settings
from app.llm import embed
from app.logging_config import get_logger, setup_logging
from app.models import RetrievedChunk

log = get_logger(__name__)

# text-embedding-3-small. Changing either value means re-ingesting everything.
VECTOR_SIZE = 1536
DISTANCE = models.Distance.COSINE

KNOWLEDGE_BASE = Path("docs/divine_empire_knowledge_base.md")

_client: AsyncQdrantClient | None = None


def get_client() -> AsyncQdrantClient | None:
    global _client
    if _client is None:
        if not settings.qdrant_url:
            log.warning("qdrant_not_configured")
            return None
        _client = AsyncQdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
            timeout=int(settings.qdrant_timeout_seconds),
        )
    return _client


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

# Model codes customers actually type: IM-105, B40A, MQ20D-3P, GQ42, BS60-2.
# Pure vector search is weakest exactly here, so we lift them into the payload
# for cheap exact-match filtering alongside the dense search.
_CODE_RE = re.compile(r"\b([A-Z]{1,6}[-\s]?\d{1,4}[A-Z]?(?:[-–]\d[A-Z]?)?)\b")


def extract_codes(text: str) -> list[str]:
    """Pull probable product/model codes out of a chunk."""
    codes = {m.group(1).replace(" ", "-").upper() for m in _CODE_RE.finditer(text)}
    # Drop bare years and prices that survive the pattern
    return sorted(c for c in codes if not c.isdigit())


def chunk_markdown(text: str) -> list[dict[str, str]]:
    """Split on `###` sub-sections, falling back to `##` for prose-only areas.

    Each chunk carries its parent `##` heading so a table about "Safety Items"
    retains that context when retrieved in isolation.
    """
    chunks: list[dict[str, str]] = []
    current_h2 = ""
    current_h3 = ""
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if not body:
            return
        title = current_h3 or current_h2
        if not title:
            return
        # Prefix the heading path so the embedding sees the category, and so a
        # retrieved chunk is self-describing when injected into the prompt.
        header = f"{current_h2} — {current_h3}" if current_h3 and current_h2 else title
        chunks.append({"title": header, "category": current_h2, "text": f"## {header}\n{body}"})

    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            buffer = []
            current_h2 = line[3:].strip()
            current_h3 = ""
        elif line.startswith("### "):
            flush()
            buffer = []
            current_h3 = line[4:].strip()
        else:
            buffer.append(line)
    flush()
    return [c for c in chunks if len(c["text"]) > 60]


def _point_id(title: str) -> str:
    """Deterministic id so re-ingesting updates rather than duplicates."""
    return hashlib.sha1(title.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Collection lifecycle
# ---------------------------------------------------------------------------


async def ensure_collection() -> bool:
    client = get_client()
    if client is None:
        return False
    try:
        if not await client.collection_exists(settings.qdrant_collection):
            await client.create_collection(
                collection_name=settings.qdrant_collection,
                vectors_config=models.VectorParams(size=VECTOR_SIZE, distance=DISTANCE),
            )
            log.info("qdrant_collection_created", extra={"collection": settings.qdrant_collection})
        return True
    except Exception:
        log.exception("qdrant_collection_failed")
        return False


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def search(
    query: str, *, limit: int | None = None, conversation_id: str | None = None
) -> list[RetrievedChunk]:
    """Retrieve product context. Returns [] on any failure — the agent then
    answers without context instead of failing the turn."""
    client = get_client()
    if client is None or not query.strip():
        return []
    limit = limit or settings.rag_top_k

    vectors = await embed([query], conversation_id=conversation_id)
    if not vectors:
        log.warning("rag_embed_unavailable", extra={"conversation_id": conversation_id})
        return []

    try:
        response = await client.query_points(
            collection_name=settings.qdrant_collection,
            query=vectors[0],
            limit=limit,
            score_threshold=settings.rag_score_threshold,
            with_payload=True,
        )
    except Exception:
        log.exception("rag_search_failed", extra={"conversation_id": conversation_id})
        return []

    chunks = [
        RetrievedChunk(
            text=p.payload.get("text", ""),
            score=p.score,
            machine_code=(p.payload.get("codes") or [None])[0],
            category=p.payload.get("category"),
        )
        for p in response.points
        if p.payload
    ]
    log.info(
        "rag_search",
        extra={
            "conversation_id": conversation_id,
            "query": query[:80],
            "hits": len(chunks),
            "top_score": round(chunks[0].score, 3) if chunks else 0.0,
        },
    )
    return chunks


def build_context(chunks: list[RetrievedChunk]) -> str:
    """Format retrieved chunks for injection. Clearly delimited and labeled so
    the model can tell retrieved facts from conversation."""
    if not chunks:
        return ""
    parts = [c.text for c in chunks]
    return "Product context (from the company catalog):\n\n" + "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Ingestion — python -m app.rag
# ---------------------------------------------------------------------------


def load_chunks(path: Path = KNOWLEDGE_BASE) -> list[dict[str, str]]:
    """Read and chunk the knowledge base. Sync on purpose — this runs once at
    ingestion time, never in a request path."""
    if not path.exists():
        log.error("knowledge_base_missing", extra={"path": str(path)})
        return []
    return chunk_markdown(path.read_text())


async def ingest(path: Path = KNOWLEDGE_BASE) -> int:
    """Chunk, embed, and upsert the knowledge base. Idempotent: point ids are
    derived from the heading, so re-running updates in place."""
    chunks = load_chunks(path)
    if not chunks:
        return 0
    if not await ensure_collection():
        return 0
    client = get_client()
    if client is None:
        return 0

    log.info("ingest_chunked", extra={"path": str(path), "chunks": len(chunks)})

    embedded = 0
    batch_size = 32
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        vectors = await embed([c["text"] for c in batch])
        if len(vectors) != len(batch):
            log.error("ingest_embed_mismatch", extra={"expected": len(batch), "got": len(vectors)})
            continue
        points = [
            models.PointStruct(
                id=_point_id(c["title"]),
                vector=v,
                payload={
                    "text": c["text"],
                    "title": c["title"],
                    "category": c["category"],
                    "codes": extract_codes(c["text"]),
                    "source": path.name,
                },
            )
            for c, v in zip(batch, vectors, strict=True)
        ]
        await client.upsert(collection_name=settings.qdrant_collection, points=points)
        embedded += len(points)
        log.info("ingest_batch", extra={"embedded": embedded, "total": len(chunks)})

    return embedded


async def _main() -> None:
    setup_logging()
    count = await ingest()
    print(f"ingested {count} chunks into '{settings.qdrant_collection}'")


if __name__ == "__main__":
    asyncio.run(_main())
