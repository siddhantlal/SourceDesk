"""Durable single-job PostgreSQL worker; API and worker are separate processes.

All extraction/provider work happens after committing claims. Final publication locks
and rechecks the document and lease token, so retries replace rows atomically and
late workers cannot resurrect deleted documents. Run: python -m sourcedesk.worker --once.
"""
import argparse
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, or_, select

from sourcedesk.chunking import chunk_pages
from sourcedesk.config import get_settings
from sourcedesk.db import session_factory
from sourcedesk.ingestion import ExtractionError, extract_pdf
from sourcedesk.models import Chunk, Collection, Document, IngestionJob, Page
from sourcedesk.providers import ProviderError, get_embedding_provider
from sourcedesk.storage import LocalStorage

logger = logging.getLogger(__name__)


def utcnow():
    return datetime.now(UTC)


@dataclass(frozen=True)
class Claim:
    job_id: UUID
    document_id: UUID
    token: UUID
    storage_key: str
    owner_id: UUID
    collection_id: UUID
    attempts: int
    exhausted: bool = False


def claim_job(factory, settings) -> Claim | None:
    with factory.begin() as session:
        now = utcnow()
        eligible = or_(
            and_(IngestionJob.status.in_(["queued", "retry"]), IngestionJob.available_at <= now),
            and_(IngestionJob.status == "processing",
                 or_(IngestionJob.lease_expires_at <= now, IngestionJob.lease_expires_at.is_(None))),
        )
        # Claims only update non-FK fields and never request a collection lock.
        # Document -> job with SKIP LOCKED therefore cannot close a collection
        # DELETE cycle; publication/renewal/failure use collection -> document -> job.
        document = session.scalar(
            select(Document).join(IngestionJob, IngestionJob.document_id == Document.id)
            .where(eligible).order_by(IngestionJob.available_at, IngestionJob.created_at)
            .with_for_update(skip_locked=True, of=Document).limit(1)
        )
        if document is None:
            return None
        job = session.scalar(select(IngestionJob).where(
            IngestionJob.document_id == document.id, eligible)
            .with_for_update(skip_locked=True))
        if job is None:
            return None
        token = uuid4()
        exhausted = job.attempts >= settings.job_max_attempts
        job.lease_token = token
        job.lease_expires_at = now + timedelta(seconds=settings.job_lease_seconds)
        if not exhausted:
            job.attempts += 1
        job.status = "processing"
        job.started_at = now
        job.completed_at = None
        document.status = "processing"
        document.error_code = document.error_message = None
        claim = Claim(job.id, document.id, token, document.storage_key, document.owner_id,
                      document.collection_id, job.attempts, exhausted)
        logger.info("job_claimed job_id=%s attempt=%s", job.id, job.attempts)
        return claim


def _locked_current(session, claim):
    # Collection DELETE locks its parent before cascading to documents/jobs.
    # Acquire that same parent first, before a Chunk insert requests FK key-share.
    collection = session.scalar(select(Collection).where(
        Collection.id == claim.collection_id).with_for_update())
    if collection is None:
        return None
    document = session.scalar(select(Document).where(
        Document.id == claim.document_id, Document.collection_id == collection.id).with_for_update())
    if document is None:
        return None
    job = session.scalar(select(IngestionJob).where(IngestionJob.id == claim.job_id).with_for_update())
    if (job is None or job.document_id != document.id or job.lease_token != claim.token
            or job.status != "processing" or job.lease_expires_at is None
            or job.lease_expires_at <= utcnow()):
        return None
    return document, job


def renew_lease(factory, claim: Claim, settings) -> bool:
    """Renew only a live token. Never revive an expired/deleted/replaced claim.

    Renew between extraction/chunking and each bounded provider batch. Configure
    lease duration above the provider timeout * attempts plus retry backoff; an
    operation exceeding it is discarded, not published, and recovered next poll.
    """
    with factory.begin() as session:
        current = _locked_current(session, claim)
        if current is None:
            return False
        _, job = current
        job.lease_expires_at = utcnow() + timedelta(seconds=settings.job_lease_seconds)
        return True


def _clear_index(session, document_id):
    session.execute(delete(Chunk).where(Chunk.document_id == document_id))
    session.execute(delete(Page).where(Page.document_id == document_id))


def complete_job(factory, claim, settings, pages, chunks, embedded) -> bool:
    with factory.begin() as session:
        current = _locked_current(session, claim)
        if current is None:
            return False
        document, job = current
        _clear_index(session, document.id)
        session.add_all([Page(document_id=document.id, page_number=page.page_number,
                              text=page.text, extraction_metadata=page.extraction_metadata)
                         for page in pages])
        session.add_all([Chunk(document_id=document.id, collection_id=document.collection_id,
                               owner_id=document.owner_id, page_number=chunk.page_number,
                               position=chunk.position, text=chunk.text, token_count=chunk.token_count,
                               embedding=vector, embedding_model=embedded.model,
                               chunking_version=settings.processing_version)
                         for chunk, vector in zip(chunks, embedded.vectors, strict=True)])
        document.status = "ready"
        document.page_count = len(pages)
        document.processing_version = settings.processing_version
        document.error_code = document.error_message = None
        job.status = "completed"
        job.completed_at = utcnow()
        job.error_code = job.error_message = None
        job.lease_token = job.lease_expires_at = None
        logger.info("job_completed job_id=%s pages=%s chunks=%s", job.id, len(pages), len(chunks))
        return True


def fail_job(factory, claim, settings, code: str, message: str, *, retryable: bool) -> bool:
    with factory.begin() as session:
        current = _locked_current(session, claim)
        if current is None:
            return False
        document, job = current
        retry = retryable and job.attempts < settings.job_max_attempts
        _clear_index(session, document.id)
        document.status = "queued" if retry else "failed"
        document.error_code, document.error_message = code, message
        job.status = "queued" if retry else "failed"
        job.available_at = utcnow() + timedelta(seconds=min(2 ** job.attempts, 60))
        job.completed_at = None if retry else utcnow()
        job.error_code, job.error_message = code, message
        job.lease_token = job.lease_expires_at = None
        logger.warning("job_failed job_id=%s code=%s retry=%s", job.id, code, retry)
        return True


def run_once() -> bool:
    from sourcedesk.cleanup import drain_file_cleanup

    settings = get_settings()
    factory = session_factory()
    cleaned = drain_file_cleanup(factory, settings, limit=5)
    claim = claim_job(factory, settings)
    if claim is None:
        return cleaned > 0
    if claim.exhausted:
        fail_job(factory, claim, settings, "attempts_exhausted", "Ingestion retry limit reached.", retryable=False)
        return True
    stages: dict[str, float] = {}
    stage = "extraction_ms"
    provider_model = "not_called"
    overall_start = start = time.perf_counter()
    try:
        pages = extract_pdf(LocalStorage(settings.upload_dir).path(claim.storage_key), settings.pdf_max_pages)
        stages["extraction_ms"] = (time.perf_counter() - start) * 1000
        if not renew_lease(factory, claim, settings):
            return True
        stage = "chunking_ms"
        start = time.perf_counter()
        chunks = chunk_pages(pages, settings.chunk_tokens, settings.chunk_overlap)
        stages["chunking_ms"] = (time.perf_counter() - start) * 1000
        if not chunks:
            raise ExtractionError("insufficient_text", "Insufficient extractable text; OCR is required.")
        provider = get_embedding_provider(settings)
        provider_model = provider.model
        vectors = []
        stage = "embedding_ms"
        start = time.perf_counter()
        for offset in range(0, len(chunks), settings.embedding_batch_size):
            if not renew_lease(factory, claim, settings):
                return True
            batch = chunks[offset:offset + settings.embedding_batch_size]
            result = provider.embed([chunk.text for chunk in batch])
            if result.model != provider.model or len(result.vectors) != len(batch):
                raise ProviderError("provider_malformed_embedding")
            vectors.extend(result.vectors)
        stages["embedding_ms"] = (time.perf_counter() - start) * 1000
        from sourcedesk.providers import EmbeddingResponse
        complete_job(factory, claim, settings, pages, chunks, EmbeddingResponse(vectors, provider.model))
    except ProviderError as exc:
        fail_job(factory, claim, settings, exc.code, "Embedding provider failed; retry if temporary.", retryable=exc.retryable)
    except ExtractionError as exc:
        fail_job(factory, claim, settings, exc.code, str(exc), retryable=False)
    except (OSError, ValueError):
        fail_job(factory, claim, settings, "processing_error", "Document processing failed.", retryable=False)
    except Exception:
        # Never log document text, provider payloads, or connection strings.
        fail_job(factory, claim, settings, "internal_error", "Ingestion failed unexpectedly.", retryable=True)
    finally:
        stages.setdefault(stage, (time.perf_counter() - start) * 1000)
        stages["total_ms"] = (time.perf_counter() - overall_start) * 1000
        logger.info("ingestion_stages job_id=%s timings=%s provider_model=%s", claim.job_id, stages, provider_model)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Process at most one available job, then exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if args.once:
        run_once()
        return
    try:
        while True:
            if not run_once():
                time.sleep(get_settings().worker_poll_seconds)
    except KeyboardInterrupt:
        logger.info("worker_stopped")


if __name__ == "__main__":
    main()
