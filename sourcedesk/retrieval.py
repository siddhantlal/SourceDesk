"""Exact pgvector cosine search: tenant constraints are inside SQL, never post-filtered."""
import time
from difflib import SequenceMatcher
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from sourcedesk.chunking import token_count, tokenizer
from sourcedesk.config import get_settings
from sourcedesk.models import Chunk, Collection, Document
from sourcedesk.providers import get_embedding_provider


def finish_read_transaction(session: Session) -> None:
    """Release read snapshots before external work; never silently commit caller writes."""
    if session.new or session.dirty or session.deleted:
        raise RuntimeError("Commit pending writes before provider calls.")
    if session.in_transaction():
        session.rollback()


def build_search_query(owner_id: UUID, collection_id: UUID, vector: list[float],
                       embedding_model: str, *, top_k: int = 6,
                       document_ids: list[UUID] | None = None):
    distance = Chunk.embedding.cosine_distance(vector).label("distance")
    query = (select(Chunk, Document.filename, distance)
             .join(Document, Chunk.document_id == Document.id)
             .join(Collection, Document.collection_id == Collection.id)
             .where(Chunk.owner_id == owner_id, Document.owner_id == owner_id,
                    Collection.owner_id == owner_id, Collection.id == collection_id,
                    Chunk.collection_id == collection_id, Document.collection_id == collection_id,
                    Document.status == "ready", Chunk.embedding_model == embedding_model))
    if document_ids is not None:
        query = query.where(Document.id.in_(document_ids))
    return query.order_by(distance, Chunk.id).limit(max(1, min(top_k, 200)))


def retrieve_sources(session: Session, owner_id: UUID, collection_id: UUID, question: str, *,
                     settings=None, document_ids: list[UUID] | None = None,
                     embedding_provider=None, timings: dict | None = None) -> list[dict]:
    settings = settings or get_settings()
    provider = embedding_provider or get_embedding_provider(settings)
    finish_read_transaction(session)
    started = time.perf_counter()
    try:
        embedded = provider.embed([question])  # no transaction/locks during HTTP
    finally:
        if timings is not None:
            timings["embedding_ms"] = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    query = build_search_query(owner_id, collection_id, embedded.vectors[0], embedded.model,
                               top_k=min(settings.retrieval_top_k * 4, 200),
                               document_ids=document_ids)
    sources: list[dict] = []
    normalized: list[str] = []
    remaining = settings.context_tokens
    for chunk, filename, distance in session.execute(query):
        text = chunk.text
        plain = " ".join(text.lower().split())
        if any(plain == old or SequenceMatcher(None, plain, old, autojunk=False).ratio() > 0.92
               for old in normalized):
            continue
        available = remaining - 16  # reserve source identifier / serialization overhead
        if available <= 0:
            break
        if token_count(text) > available:
            text = tokenizer().decode(tokenizer().encode(text, disallowed_special=())[:available], errors="ignore")
        if not text.strip():
            continue
        count = token_count(text)
        sources.append({"source_id": f"S{len(sources) + 1}", "document_id": str(chunk.document_id),
                        "chunk_id": str(chunk.id), "filename": filename,
                        "page_number": chunk.page_number, "excerpt": text,
                        "distance": float(distance), "token_count": count})
        normalized.append(plain)
        remaining -= count + 16
        if len(sources) >= settings.retrieval_top_k:
            break
    finish_read_transaction(session)
    if timings is not None:
        timings["retrieval_ms"] = (time.perf_counter() - started) * 1000
    return sources
