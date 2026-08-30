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

from app import cache
from app.config import settings
from app.llm import embed
from app.logging_config import get_logger, setup_logging
from app.models import RetrievedChunk

log = get_logger(__name__)

# text-embedding-3-small. Changing either value means re-ingesting everything.
VECTOR_SIZE = 1536
DISTANCE = models.Distance.COSINE

# Lives in data/, not docs/: the app reads this at ingestion time, so it is
# operational data that must ship in the image — docs/ is gitignored.
KNOWLEDGE_BASE = Path("data/knowledge_base.md")

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
        # Needed for _exact_code_matches's payload-filtered scroll — Qdrant
        # requires an explicit index before a field can be used in a filter,
        # and this was missing in production (discovered live: a real
        # customer's exact model-code question got the wrong machine's specs
        # because the correct chunk lost on vector similarity to a longer,
        # richer document). create_payload_index is idempotent — safe to call
        # on every startup, not just once at collection creation.
        await client.create_payload_index(
            collection_name=settings.qdrant_collection,
            field_name="codes",
            field_schema=models.PayloadSchemaType.KEYWORD,
        )
        # Needed for documents.delete_machine_from_index's payload-filtered
        # delete — same "index required but not found" 400 as codes above,
        # caught live: deleting a machine removed its Postgres row but left
        # every Qdrant chunk behind, so a deleted machine's specs/price
        # could still surface in a customer's answer via RAG.
        await client.create_payload_index(
            collection_name=settings.qdrant_collection,
            field_name="machine_id",
            field_schema=models.PayloadSchemaType.KEYWORD,
        )
        return True
    except Exception:
        log.exception("qdrant_collection_failed")
        return False


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


# Greetings and acknowledgements, English + the Hinglish/Hindi forms customers
# actually send. Matched only as a WHOLE message, never as a substring, so
# "hi, price of GW42?" still retrieves.
_SMALLTALK = {
    "hi",
    "hii",
    "hiii",
    "hey",
    "heyy",
    "hello",
    "helo",
    "hlo",
    "yo",
    "hi sir",
    "hello sir",
    "hey sir",
    "hi bro",
    "hello ji",
    "hi ji",
    "namaste",
    "namaskar",
    "salaam",
    "salam",
    "ok",
    "okay",
    "okk",
    "k",
    "kk",
    "hmm",
    "hm",
    "hmmm",
    "yes",
    "yea",
    "yeah",
    "yep",
    "no",
    "nope",
    "nah",
    "haan",
    "haa",
    "ha",
    "ji",
    "ji sir",
    "acha",
    "accha",
    "theek",
    "thik",
    "thik hai",
    "theek hai",
    "ok thanks",
    "okay thanks",
    "ok ji",
    "thanks",
    "thank you",
    "thanku",
    "thankyou",
    "thx",
    "ty",
    "dhanyavad",
    "welcome",
    "good morning",
    "good afternoon",
    "good evening",
    "good night",
    "bye",
    "byee",
    "goodbye",
    "tata",
    "it is working",
    "its working",
    "working",
    "test",
    "testing",
    "who are you",
    "kaun",
    "kaun ho",
    "?",
    "??",
    "...",
}

# Anything that looks like a product code (GW42, iM-62, FX-201, MQ-950, 32mm)
# or carries a number is never smalltalk, however short.
_PRODUCTISH_RE = re.compile(r"\d")


def is_smalltalk(query: str) -> bool:
    """True when retrieval is pointless — a greeting or bare acknowledgement.

    Conservative by design: a false positive means the agent answers a real
    product question with no catalogue context, which is far worse than
    spending 2s on a needless lookup. So this matches the WHOLE normalised
    message against a fixed list, and refuses anything containing a digit
    (model numbers, sizes, quantities) or more than four words.
    """
    normalised = re.sub(r"[^\w\s?.]", "", query.strip().lower())
    normalised = " ".join(normalised.split())
    if not normalised:
        return True
    if _PRODUCTISH_RE.search(normalised):
        return False
    if len(normalised.split()) > 4:
        return False
    return normalised in _SMALLTALK or normalised.rstrip("?.") in _SMALLTALK


async def _exact_code_matches(
    client: AsyncQdrantClient, query: str, conversation_id: str | None
) -> list[RetrievedChunk]:
    """Payload-filter lookup for model codes the customer typed, alongside
    the dense vector search.

    Found in production: a real customer asked "IM-55 aur IM-105 mein kya
    difference hai" and got "I don't have those details" even though both
    codes are in the catalog — a richer, unrelated document (a full spec
    sheet for a different model, same "Total Station"/"Sokkia" vocabulary)
    outscored the sparse-but-correct price-table row on pure vector
    similarity and pushed it out of the top-k. Vector search alone is
    weakest exactly where a customer is most precise — typing an exact model
    number — so codes extracted from the customer's OWN message get an exact
    payload match as a supplement, not a replacement, for the vector search.
    """
    codes = extract_codes(query)
    if not codes:
        return []
    try:
        # A pure payload filter, no vector involved — scroll(), not
        # query_points(), since there is nothing to rank by similarity here;
        # a code either matches or it doesn't.
        points, _ = await client.scroll(
            collection_name=settings.qdrant_collection,
            scroll_filter=models.Filter(
                must=[models.FieldCondition(key="codes", match=models.MatchAny(any=codes))]
            ),
            limit=len(codes) * 2,
            with_payload=True,
        )
    except Exception:
        log.exception(
            "rag_code_match_failed", extra={"conversation_id": conversation_id, "codes": codes}
        )
        return []
    return [
        RetrievedChunk(
            text=p.payload.get("text", ""),
            score=1.0,  # exact code match — treated as maximally relevant
            machine_code=(p.payload.get("codes") or [None])[0],
            category=p.payload.get("category"),
        )
        for p in points
        if p.payload
    ]


async def search(
    query: str, *, limit: int | None = None, conversation_id: str | None = None
) -> list[RetrievedChunk]:
    """Retrieve product context. Returns [] on any failure — the agent then
    answers without context instead of failing the turn.

    Cached by normalized query text (Addition.md Phase F) — a hit skips both
    the embedding call and the Qdrant search, since the whole point is that
    repeated product questions ("what's the price of X") are common and
    identical in substance across customers.
    """
    limit = limit or settings.rag_top_k

    async def _fetch() -> list[dict]:
        client = get_client()
        if client is None or not query.strip():
            return []

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

        # Exact code matches go first and are never dropped for a lower-scoring
        # vector hit — see _exact_code_matches's docstring for the real failure
        # this fixes. Deduped by text so an exact match already surfaced by the
        # vector search isn't repeated, then the combined list is capped back
        # to `limit` so this never grows the context beyond what it was before.
        exact = await _exact_code_matches(client, query, conversation_id)
        if exact:
            seen_text = {c.text for c in exact}
            merged = exact + [c for c in chunks if c.text not in seen_text]
            chunks = merged[:limit]

        log.info(
            "rag_search",
            extra={
                "conversation_id": conversation_id,
                "query": query[:80],
                "hits": len(chunks),
                "exact_code_hits": len(exact),
                "top_score": round(chunks[0].score, 3) if chunks else 0.0,
            },
        )
        return [c.model_dump() for c in chunks]

    if not query.strip():
        return []

    if is_smalltalk(query):
        # Retrieval on "hey" / "ok thanks" cost 1.3-3.1s in production and
        # returned zero hits every time (embedding call + Qdrant round trip,
        # both wasted). Skipping it is the single cheapest latency win on a
        # channel where the customer is watching a typing gap.
        log.info(
            "rag_skipped_smalltalk",
            extra={"conversation_id": conversation_id, "query": query[:40]},
        )
        return []

    raw = await cache.get_or_set(cache.rag_key(query), settings.cache_rag_ttl_seconds, _fetch)
    return [RetrievedChunk(**c) for c in raw]


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

    # A re-ingest replaces the whole collection, so every previously cached
    # RAG result is now potentially stale — clear the namespace rather than
    # try to guess which normalized queries it might affect.
    await cache.invalidate_namespace("rag")

    return embedded


async def _main() -> None:
    setup_logging()
    count = await ingest()
    print(f"ingested {count} chunks into '{settings.qdrant_collection}'")


if __name__ == "__main__":
    asyncio.run(_main())
