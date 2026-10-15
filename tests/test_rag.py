"""Six focused RAG slices; integration uses only the explicit test PostgreSQL DB."""
from datetime import UTC
from io import BytesIO

import pytest
from reportlab.pdfgen.canvas import Canvas


def pdf_bytes(*pages):
    output = BytesIO()
    canvas = Canvas(output)
    for text in pages:
        for i, line in enumerate(text.splitlines()):
            canvas.drawString(50, 780 - i * 15, line)
        canvas.showPage()
    canvas.save()
    return output.getvalue()


@pytest.mark.parametrize("oversized", ["contents_array", "single_stream", "ascii85", "nested_form"])
def test_page_extraction_and_bounded_chunks(tmp_path, monkeypatch, oversized):
    from sourcedesk.chunking import chunk_pages, token_count
    from sourcedesk.ingestion import ExtractionError, extract_pdf

    path = tmp_path / "pages.pdf"
    path.write_bytes(pdf_bytes("ORBITAL NOTES\n" + "Mars has two moons. " * 40,
                               "PAGE TWO\nVenus has no moons."))
    pages = extract_pdf(path, max_pages=10)
    chunks = chunk_pages(pages, target_tokens=32, overlap_tokens=6)
    assert [page.page_number for page in pages] == [1, 2]
    assert chunks and {chunk.page_number for chunk in chunks} == {1, 2}
    assert all(0 < token_count(c.text) == c.token_count <= 32 for c in chunks)
    assert all("Venus" not in c.text for c in chunks if c.page_number == 1)
    path.write_bytes(pdf_bytes(""))
    with pytest.raises(ExtractionError, match="OCR"):
        extract_pdf(path, max_pages=10)

    import zlib

    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (
        ArrayObject,
        ContentStream,
        DictionaryObject,
        EncodedStreamObject,
        NameObject,
    )

    from sourcedesk import ingestion

    # Small encoded files must fail before ContentStream assembles decoded data.
    # Keep the test budget tiny: no real decompression bomb allocation is needed.
    monkeypatch.setattr(ingestion, "PAGE_CONTENT_LIMIT", 4096, raising=False)
    writer = PdfWriter()
    page = writer.add_blank_page(612, 792)
    def stream(data, filter_name="/FlateDecode"):
        result = EncodedStreamObject()
        result._data = data
        result[NameObject("/Filter")] = NameObject(filter_name)
        return writer._add_object(result)

    if oversized == "contents_array":
        page[NameObject("/Contents")] = ArrayObject([
            stream(zlib.compress(b" " * 2500)) for _ in range(3)
        ])
    elif oversized == "single_stream":
        page[NameObject("/Contents")] = stream(zlib.compress(b" " * 12_000))
    elif oversized == "ascii85":
        page[NameObject("/Contents")] = stream(b"z" * 1100 + b"~>", "/ASCII85Decode")
    else:
        page[NameObject("/Contents")] = stream(zlib.compress(b"/Nested Do"))
        form = stream(zlib.compress(b" " * 12_000)).get_object()
        form[NameObject("/Subtype")] = NameObject("/Form")
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/XObject"): DictionaryObject({NameObject("/Nested"): form})
        })
    writer.write(path)
    assert len(path.read_bytes()) < 4096
    assembly_calls = []
    original_init = ContentStream.__init__
    def observed_assembly(self, *args, **kwargs):
        assembly_calls.append(True)
        return original_init(self, *args, **kwargs)
    monkeypatch.setattr(ContentStream, "__init__", observed_assembly)
    with pytest.raises(ExtractionError) as rejected:
        extract_pdf(path)
    assert rejected.value.code == "extraction_limit"
    assert assembly_calls == [], "Oversized streams reached eager content assembly"
    # Context-local pypdf limits must not leak to other readers.
    assert PdfReader(BytesIO(pdf_bytes("Mars has two moons, Phobos and Deimos."))).pages[0].extract_text()


def test_structured_citations_are_backend_mapped_and_invalid_ids_rejected():
    from sourcedesk.generation import CitationValidationError, generate_answer, validate_response
    from sourcedesk.providers import FakeGenerationProvider
    from sourcedesk.questions import redact_sources

    sources = [{"source_id": "S1", "document_id": "doc-a", "chunk_id": "chunk-a",
                "filename": "astronomy.pdf", "page_number": 2,
                "excerpt": "Mars has two moons, Phobos and Deimos."}]
    result = generate_answer("How many moons does Mars have?", sources,
                             FakeGenerationProvider(), max_output_tokens=100)
    assert result.status == "answered"
    assert result.citations[0]["page_number"] == 2
    assert result.citations[0]["document_id"] == "doc-a"
    assert result.model == "fake-extractive-v1" and result.warnings
    deleted = redact_sources(result.citations, set())[0]
    assert deleted["available"] is False and deleted["excerpt"] is None
    assert "quote" not in deleted
    assert redact_sources(result.citations, {"chunk-a"})[0]["available"] is True
    for citation in [{"source_id": "S99"}, {"source_id": "S1", "quote": "Jupiter has rings"}]:
        with pytest.raises(CitationValidationError):
            validate_response({"status": "answered", "answer": "Claim.",
                               "citations": [citation]}, sources, max_output_tokens=100)
    with pytest.raises(CitationValidationError):
        validate_response({"status": "answered", "answer": "Uncited.", "citations": []},
                          sources, max_output_tokens=100)


@pytest.mark.parametrize("invalid_model", ["x" * 151, 123, None, "", " \t"],
                         ids=["overlong", "nonstring", "null", "empty", "blank"])
def test_abstention_is_not_provider_failure_and_http_retries_are_bounded(request, invalid_model):
    import httpx

    from sourcedesk.generation import generate_answer
    from sourcedesk.providers import FakeGenerationProvider, OpenAIProvider

    sources = [{"source_id": "S1", "document_id": "d", "chunk_id": "c",
                "filename": "a.pdf", "page_number": 1, "excerpt": "Mars has two moons."}]
    assert generate_answer("Who invented the telephone?", sources,
                           FakeGenerationProvider()).status == "insufficient_evidence"
    calls = []
    def unavailable(request):
        calls.append(request)
        return httpx.Response(503, json={"error": {"message": "do not expose"}})
    provider = OpenAIProvider(api_key="test-not-a-real-key", base_url="https://example.invalid/v1",
                              embedding_model="test-embedding", generation_model="test-model",
                              max_attempts=2, timeout=0.1,
                              transport=httpx.MockTransport(unavailable), sleep=lambda _: None)
    result = generate_answer("How many moons does Mars have?", sources, provider)
    assert result.status == "provider_unavailable"
    assert len(calls) == 2
    assert "do not expose" not in result.text

    import json

    from sourcedesk.providers import ProviderError

    def malformed_metadata(request):
        calls.append(request)
        return httpx.Response(200, json={
            "model": invalid_model,
            "choices": [{"message": {"content": json.dumps({
                "status": "answered", "answer": "Mars has two moons.",
                "citations": [{"source_id": "S1"}],
            })}}],
        })
    provider.transport = httpx.MockTransport(malformed_metadata)
    with pytest.raises(ProviderError) as rejected:
        provider.generate("Mars moons?", sources)
    assert rejected.value.code == "provider_malformed_generation"
    assert len(calls) == 3, "Invalid metadata is not a transient retry"
    malformed = generate_answer("Mars moons?", sources, provider)
    assert malformed.status == "provider_unavailable"
    assert malformed.model == "test-model"
    assert malformed.warnings == ["provider_malformed_generation"]

    # The controlled failure must remain persistable in Answer.model VARCHAR(150).
    from sourcedesk.models import Chunk
    from sourcedesk.providers import FakeEmbeddingProvider
    from sourcedesk.questions import ask_question, get_question

    db_session = request.getfixturevalue("db_session")
    settings = request.getfixturevalue("settings")
    owner, collection, document, _ = seed_job(db_session, settings)
    document.status = "ready"
    embedder = FakeEmbeddingProvider()
    vector = embedder.embed(["Mars moons"])
    db_session.add(Chunk(document_id=document.id, collection_id=collection.id, owner_id=owner.id,
                         page_number=1, position=0, text="Mars has two moons.", token_count=6,
                         embedding=vector.vectors[0], embedding_model=embedder.model,
                         chunking_version="v1"))
    db_session.commit()
    response = ask_question(db_session, owner, collection, "Mars moons?", settings=settings,
                            embedding_provider=embedder, generation_provider=provider)
    assert response["status"] == "provider_unavailable"
    db_session.expire_all()
    saved = get_question(response["id"], owner, db_session)
    assert saved["status"] == "provider_unavailable" and saved["model"] == "test-model"


def seed_job(db_session, settings, text="Mars has two moons, Phobos and Deimos."):
    import hashlib
    from uuid import uuid4

    from sourcedesk.models import Collection, Document, IngestionJob, User
    from sourcedesk.storage import LocalStorage

    owner = User(email=f"{uuid4()}@example.com", password_hash="not-used-in-service-test")
    db_session.add(owner)
    db_session.flush()
    collection = Collection(owner_id=owner.id, name="Astronomy")
    db_session.add(collection)
    db_session.flush()
    data = pdf_bytes(text)
    key = LocalStorage(settings.upload_dir).put(data)
    document = Document(collection_id=collection.id, owner_id=owner.id, filename="space.pdf",
                        storage_key=key, sha256=hashlib.sha256(data).hexdigest(), status="queued")
    db_session.add(document)
    db_session.flush()
    job = IngestionJob(document_id=document.id)
    db_session.add(job)
    db_session.commit()
    return owner, collection, document, job


@pytest.mark.integration
def test_tenant_ready_model_filters_and_bounded_retrieval(request):
    from uuid import UUID, uuid4

    from sqlalchemy.dialects import postgresql

    from sourcedesk.models import Chunk, Collection, Document, User
    from sourcedesk.providers import FakeEmbeddingProvider
    from sourcedesk.retrieval import build_search_query, retrieve_sources

    provider = FakeEmbeddingProvider()
    vector = provider.embed(["Mars moons"]).vectors[0]
    query = build_search_query(uuid4(), uuid4(), vector, provider.model, top_k=6)
    sql = str(query.compile(dialect=postgresql.dialect()))
    assert "<=>" in sql and "chunks.owner_id" in sql and "documents.owner_id" in sql
    assert "collections.owner_id" in sql and "documents.status" in sql and "chunks.embedding_model" in sql
    db_session = request.getfixturevalue("db_session")
    settings = request.getfixturevalue("settings")
    owner, collection, document, _ = seed_job(db_session, settings)
    document.status = "ready"
    other = User(email="other@example.com", password_hash="unused")
    db_session.add(other)
    db_session.flush()
    alien_collection = Collection(owner_id=other.id, name="Private")
    db_session.add(alien_collection)
    db_session.flush()
    alien = Document(owner_id=other.id, collection_id=alien_collection.id, filename="secret.pdf",
                     storage_key=uuid4().hex, sha256="a" * 64, status="ready")
    pending = Document(owner_id=owner.id, collection_id=collection.id, filename="pending.pdf",
                       storage_key=uuid4().hex, sha256="b" * 64, status="processing")
    db_session.add_all([alien, pending])
    db_session.flush()
    for doc, model, position in [(document, provider.model, 0), (document, "incompatible", 1),
                                 (alien, provider.model, 0), (pending, provider.model, 0)]:
        db_session.add(Chunk(document_id=doc.id, collection_id=doc.collection_id, owner_id=doc.owner_id,
                             page_number=1, position=position, text="Mars has two moons.", token_count=6,
                             embedding=vector, embedding_model=model, chunking_version="v1"))
    db_session.commit()
    sources = retrieve_sources(db_session, owner.id, collection.id, "Mars moons",
                               settings=settings, document_ids=[document.id, alien.id],
                               embedding_provider=provider)
    assert len(sources) == 1 and sources[0]["document_id"] == str(document.id)
    assert retrieve_sources(db_session, owner.id, alien_collection.id, "Mars moons",
                            settings=settings, embedding_provider=provider) == []
    from sourcedesk.errors import AppError
    from sourcedesk.providers import FakeGenerationProvider, ProviderError
    from sourcedesk.questions import ask_question, get_question, get_sources, list_questions

    class CheckedEmbedding(FakeEmbeddingProvider):
        def embed(self, texts):
            assert not db_session.in_transaction(), "Embedding HTTP inherited a transaction"
            return super().embed(texts)

    class CheckedGeneration(FakeGenerationProvider):
        def generate(self, *args, **kwargs):
            assert not db_session.in_transaction(), "Generation HTTP inherited a transaction"
            return super().generate(*args, **kwargs)

    # Capture scalar IDs before the RAG service releases its caller's read snapshot.
    owner_id, collection_id, alien_id = owner.id, collection.id, alien.id
    response = ask_question(db_session, owner, collection_id, "How many moons does Mars have?",
                            settings=settings, embedding_provider=CheckedEmbedding(),
                            generation_provider=CheckedGeneration())
    assert response["status"] == "answered"
    assert response["sources"][0]["available"] is True
    assert {"embedding_ms", "retrieval_ms", "generation_ms", "total_ms"} <= response["timings"].keys()
    assert all(value >= 0 for value in response["timings"].values())
    assert get_question(response["id"], owner, db_session)["answer"] == response["answer"]
    history = list_questions(collection_id, 1, 0, owner, db_session)
    assert history["total"] == 1 and history["items"][0]["id"] == response["id"]
    with pytest.raises(AppError, match="not found"):
        get_sources(response["id"], other, db_session)
    with pytest.raises(AppError, match="not found"):
        ask_question(db_session, owner, collection_id, "Mars?", [alien_id], settings=settings)
    db_session.rollback()

    class UnavailableEmbedding(CheckedEmbedding):
        def embed(self, texts):
            assert not db_session.in_transaction()
            raise ProviderError("provider_timeout", retryable=True)

    failed = ask_question(db_session, db_session.get(User, owner_id), collection_id, "Mars moons?",
                          settings=settings, embedding_provider=UnavailableEmbedding())
    assert failed["status"] == "provider_unavailable"
    assert get_question(failed["id"], db_session.get(User, owner_id), db_session)["status"] == "provider_unavailable"
    db_session.delete(db_session.get(Document, UUID(response["sources"][0]["document_id"])))
    db_session.commit()
    redacted = get_question(response["id"], db_session.get(User, owner_id), db_session)
    assert redacted["answer"] == response["answer"]
    assert redacted["citations"][0]["available"] is False
    assert redacted["sources"][0]["excerpt"] is None
    assert "quote" not in redacted["citations"][0]


@pytest.mark.integration
def test_embedding_failure_retry_is_idempotent(request, monkeypatch, caplog):
    import logging
    from datetime import datetime

    from sqlalchemy import func, select

    from sourcedesk import worker
    from sourcedesk.models import Chunk, Document, IngestionJob, Page
    from sourcedesk.providers import FakeEmbeddingProvider, ProviderError

    db_session = request.getfixturevalue("db_session")
    settings = request.getfixturevalue("settings")
    _, _, document, job = seed_job(db_session, settings)
    document_id, job_id = document.id, job.id
    class FailingEmbedding(FakeEmbeddingProvider):
        def embed(self, texts):
            raise ProviderError("provider_timeout", retryable=True)
    caplog.set_level(logging.INFO, logger="sourcedesk.worker")
    monkeypatch.setattr(worker, "get_embedding_provider", lambda _: FailingEmbedding())
    assert worker.run_once() is True
    timing_logs = [record.message for record in caplog.records if "ingestion_stages" in record.message]
    assert timing_logs and all(stage in timing_logs[-1] for stage in
                              ("extraction_ms", "chunking_ms", "embedding_ms"))
    db_session.expire_all()
    assert db_session.get(Document, document_id).status == "queued"
    assert db_session.scalar(select(func.count()).select_from(Chunk)) == 0
    db_session.get(IngestionJob, job_id).available_at = datetime.now(UTC)
    db_session.commit()
    monkeypatch.setattr(worker, "get_embedding_provider", lambda _: FakeEmbeddingProvider())
    assert worker.run_once() is True
    assert worker.run_once() is False
    db_session.expire_all()
    assert db_session.get(Document, document_id).status == "ready"
    assert db_session.get(IngestionJob, job_id).attempts == 2
    counts = [db_session.scalar(select(func.count()).select_from(model)) for model in (Page, Chunk)]
    assert counts == [1, 1]
    # Explicit reindex uses the same durable row and replaces, never appends.
    job = db_session.get(IngestionJob, job_id)
    job.status, job.attempts = "queued", 0
    job.available_at = datetime.now(UTC)
    db_session.commit()
    assert worker.run_once() is True
    db_session.expire_all()
    assert [db_session.scalar(select(func.count()).select_from(model)) for model in (Page, Chunk)] == counts

    # Deletion recovery runs even when no ingestion work exists, before claiming.
    from sourcedesk.models import FileCleanupTask
    from sourcedesk.storage import LocalStorage

    storage = LocalStorage(settings.upload_dir)
    orphan_key = storage.put(b"previously deleted document original")
    db_session.add(FileCleanupTask(storage_key=orphan_key))
    db_session.commit()
    original_claim = worker.claim_job
    def claim_after_cleanup(factory, settings):
        assert not storage.path(orphan_key).exists(), "Cleanup must precede ingestion claims"
        return original_claim(factory, settings)
    monkeypatch.setattr(worker, "claim_job", claim_after_cleanup)
    assert worker.run_once() is True
    db_session.expire_all()
    assert db_session.get(FileCleanupTask, orphan_key) is None
    assert not storage.path(orphan_key).exists()
    assert worker.run_once() is False


@pytest.mark.integration
@pytest.mark.parametrize("contended_operation", ["complete", "renew", "fail"])
def test_expired_leases_are_fenced_and_deleted_jobs_cannot_publish(
    request, monkeypatch, contended_operation,
):
    from datetime import datetime, timedelta

    from sqlalchemy import func, select

    from sourcedesk import worker
    from sourcedesk.chunking import chunk_pages
    from sourcedesk.db import session_factory
    from sourcedesk.ingestion import extract_pdf
    from sourcedesk.models import Chunk, Document, IngestionJob, Page
    from sourcedesk.providers import FakeEmbeddingProvider
    from sourcedesk.storage import LocalStorage

    db_session = request.getfixturevalue("db_session")
    settings = request.getfixturevalue("settings")
    _, _, document, job = seed_job(db_session, settings)
    document_id, job_id = document.id, job.id
    factory = session_factory()
    stale = worker.claim_job(factory, settings)
    assert stale is not None and stale.document_id == document_id
    db_session.expire_all()
    db_session.get(IngestionJob, job_id).lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    recovered = worker.claim_job(factory, settings)
    assert recovered is not None, "Expired processing leases must be reclaimable"
    assert recovered.token != stale.token and recovered.attempts == 2
    pages = extract_pdf(LocalStorage(settings.upload_dir).path(recovered.storage_key))
    chunks = chunk_pages(pages)
    embedded = FakeEmbeddingProvider().embed([chunk.text for chunk in chunks])
    assert worker.complete_job(factory, stale, settings, pages, chunks, embedded) is False
    assert worker.fail_job(factory, stale, settings, "stale", "stale", retryable=False) is False
    assert worker.renew_lease(factory, stale, settings) is False
    assert worker.renew_lease(factory, recovered, settings) is True
    assert worker.complete_job(factory, recovered, settings, pages, chunks, embedded) is True

    # Recover an exhausted interrupted attempt without ever making a provider call.
    db_session.expire_all()
    row = db_session.get(IngestionJob, job_id)
    row.status, row.attempts = "processing", settings.job_max_attempts
    row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    assert worker.run_once() is True
    db_session.expire_all()
    assert db_session.get(IngestionJob, job_id).status == "failed"
    assert db_session.get(Document, document_id).error_code == "attempts_exhausted"

    row = db_session.get(IngestionJob, job_id)
    row.status, row.attempts = "queued", 0
    row.available_at = datetime.now(UTC)
    db_session.commit()
    doomed = worker.claim_job(factory, settings)
    assert doomed is not None
    db_session.delete(db_session.get(Document, document_id))
    db_session.commit()
    assert worker.complete_job(factory, doomed, settings, pages, chunks, embedded) is False
    assert worker.fail_job(factory, doomed, settings, "deleted", "deleted", retryable=True) is False
    assert worker.renew_lease(factory, doomed, settings) is False
    assert worker.run_once() is False
    assert [db_session.scalar(select(func.count()).select_from(model)) for model in (Page, Chunk, IngestionJob)] == [0, 0, 0]
    db_session.rollback()

    # Reproduce the collection-first DELETE interleaving against real PostgreSQL.
    # SQL events establish the first lock attempt, not timing-dependent sleeps.
    from threading import Event, Thread

    from sqlalchemy import create_engine, event, text
    from sqlalchemy.orm import sessionmaker

    from sourcedesk.models import Collection

    _, collection, _, _ = seed_job(db_session, settings)
    collection_id = collection.id
    concurrent_claim = worker.claim_job(factory, settings)
    assert concurrent_claim is not None
    db_session.rollback()
    contention_engine = create_engine(settings.database_url, connect_args={"connect_timeout": 5})
    contender_factory = sessionmaker(contention_engine, expire_on_commit=False)
    attempted, release = Event(), Event()
    outcomes, errors = [], []

    @event.listens_for(contention_engine, "begin")
    def bounded_locks(connection):
        connection.exec_driver_sql("SET LOCAL lock_timeout = '2s'")

    @event.listens_for(contention_engine, "before_cursor_execute")
    def collection_attempt(connection, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE" in statement and "FROM collections" in statement:
            attempted.set()

    @event.listens_for(contention_engine, "after_cursor_execute")
    def document_acquired(connection, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE" in statement and "FROM documents" in statement:
            attempted.set()
            if not release.wait(5):
                raise AssertionError("Document-lock test barrier timed out")

    def contend():
        try:
            if contended_operation == "complete":
                outcome = worker.complete_job(contender_factory, concurrent_claim, settings,
                                              pages, chunks, embedded)
            elif contended_operation == "renew":
                outcome = worker.renew_lease(contender_factory, concurrent_claim, settings)
            else:
                outcome = worker.fail_job(contender_factory, concurrent_claim, settings,
                                          "deleted", "deleted", retryable=False)
            outcomes.append(outcome)
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=contend, daemon=True)
    try:
        with factory.begin() as deleting:
            deleting.execute(text("SET LOCAL lock_timeout = '2s'"))
            target = deleting.scalar(select(Collection).where(Collection.id == collection_id)
                                     .with_for_update())
            thread.start()
            assert attempted.wait(3), "Worker never attempted its first row lock"
            # A worker waiting for collection DELETE must NOT already hold its
            # document lock, which forms the FK-key-share deadlock on publication.
            assert deleting.scalar(select(Document.id)
                                   .where(Document.id == concurrent_claim.document_id)
                                   .with_for_update(nowait=True)) == concurrent_claim.document_id
            deleting.delete(target)
            deleting.flush()
    finally:
        release.set()
        thread.join(6)
        contention_engine.dispose()
    assert not thread.is_alive(), "Contending worker did not finish within the timeout"
    assert errors == [] and outcomes == [False]
    db_session.expire_all()
    assert db_session.get(Collection, collection_id) is None
    assert [db_session.scalar(select(func.count()).select_from(model))
            for model in (Page, Chunk, IngestionJob)] == [0, 0, 0]

