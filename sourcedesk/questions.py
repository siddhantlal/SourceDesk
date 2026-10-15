"""Owned question history and synchronous RAG, with no transactions across HTTP.

Answers survive document deletion; stored answer text is retained, but every public
read rechecks source availability and removes excerpts/quotes for deleted chunks.
"""
import logging
import time
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sourcedesk.auth import current_user
from sourcedesk.config import get_settings
from sourcedesk.db import get_session
from sourcedesk.errors import AppError
from sourcedesk.generation import PROMPT_VERSION, AnswerResult, generate_answer
from sourcedesk.models import Answer, Chunk, Collection, Document, Question, User
from sourcedesk.providers import ProviderError, get_embedding_provider, get_generation_provider
from sourcedesk.retrieval import finish_read_transaction, retrieve_sources

router = APIRouter(prefix="/api/v1", tags=["questions and sources"])
logger = logging.getLogger(__name__)


class QuestionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000,
                      validation_alias=AliasChoices("text", "question"))
    document_ids: list[UUID] | None = Field(default=None, max_length=100)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Question must not be blank")
        return value.strip()


class QuestionView(BaseModel):
    id: UUID
    collection_id: UUID
    question: str
    status: str
    answer: str
    citations: list[dict]
    sources: list[dict]
    timings: dict
    token_usage: dict | None
    cost_estimate: dict | None
    model: str
    warnings: list[str]
    created_at: datetime


class QuestionList(BaseModel):
    items: list[QuestionView]
    total: int
    limit: int
    offset: int


class SourcesView(BaseModel):
    question_id: UUID
    sources: list[dict]


def redact_sources(sources: list[dict], available_chunk_ids: set[str]) -> list[dict]:
    """Copy public fields only; never mutate persisted JSON or leak deleted quotes."""
    public = []
    for source in sources:
        item = {key: source[key] for key in
                ("source_id", "document_id", "chunk_id", "filename", "page_number", "excerpt")
                if key in source}
        item["available"] = str(source.get("chunk_id")) in available_chunk_ids
        if item["available"]:
            if source.get("quote"):
                item["quote"] = source["quote"]
        else:
            item["excerpt"] = None
        public.append(item)
    return public


def _owned_collection(session: Session, owner_id: UUID, collection_id: UUID, *, lock=False):
    query = select(Collection).where(Collection.id == collection_id, Collection.owner_id == owner_id)
    if lock:
        query = query.with_for_update()
    collection = session.scalar(query)
    if collection is None:
        raise AppError("not_found", "Collection was not found", 404)
    return collection


def _owned_answer_query(owner_id: UUID):
    return (select(Question, Answer).join(Answer, Answer.question_id == Question.id)
            .join(Collection, Collection.id == Question.collection_id)
            .where(Question.user_id == owner_id, Collection.owner_id == owner_id))


def _serialize(session: Session, question: Question, answer: Answer, owner_id: UUID) -> dict:
    # A single owned query, rather than trusting old JSON, determines availability.
    chunk_ids = []
    for source in answer.retrieved_chunks + answer.citations:
        try:
            chunk_ids.append(UUID(str(source.get("chunk_id"))))
        except (ValueError, TypeError):
            continue
    available = set()
    if chunk_ids:
        available = {str(value) for value in session.scalars(
            select(Chunk.id).join(Document, Document.id == Chunk.document_id)
            .join(Collection, Collection.id == Document.collection_id)
            .where(Chunk.id.in_(chunk_ids), Chunk.owner_id == owner_id,
                   Document.owner_id == owner_id, Collection.owner_id == owner_id,
                   Chunk.collection_id == question.collection_id,
                   Document.collection_id == question.collection_id))}
    citations = redact_sources(answer.citations, available)
    sources = redact_sources(answer.retrieved_chunks, available)
    warnings = list(answer.warnings)
    if any(not item["available"] for item in citations + sources):
        warnings.append("Some sources are unavailable; deleted-source excerpts have been redacted.")
    return {"id": question.id, "collection_id": question.collection_id,
            "question": question.text, "status": answer.status, "answer": answer.text,
            "citations": citations, "sources": sources, "timings": answer.timings,
            "token_usage": answer.token_usage, "cost_estimate": answer.cost_estimate,
            "model": answer.model, "warnings": warnings, "created_at": question.created_at}


def ask_question(session: Session, user: User, collection: Collection | UUID, text: str,
                 document_ids: list[UUID] | None = None, *, settings=None,
                 embedding_provider=None, generation_provider=None) -> dict:
    """Shared API/evaluation service. Caller must commit writes before invoking it."""
    body = QuestionInput(text=text, document_ids=document_ids)
    settings = settings or get_settings()
    owner_id = user.id
    collection_id = collection.id if isinstance(collection, Collection) else collection
    _owned_collection(session, owner_id, collection_id)
    if body.document_ids:
        permitted = set(session.scalars(select(Document.id).where(
            Document.id.in_(body.document_ids), Document.owner_id == owner_id,
            Document.collection_id == collection_id)))
        if permitted != set(body.document_ids):
            raise AppError("not_found", "A selected document was not found in this collection", 404)
    finish_read_transaction(session)
    start = time.perf_counter()
    sources: list[dict] = []
    timings = {"embedding_ms": 0.0, "retrieval_ms": 0.0, "generation_ms": 0.0}
    embedder = embedding_provider or get_embedding_provider(settings)
    generator = generation_provider or get_generation_provider(settings)
    try:
        sources = retrieve_sources(session, owner_id, collection_id, body.text, settings=settings,
                                   document_ids=body.document_ids, embedding_provider=embedder,
                                   timings=timings)
        # Defensive boundary: external providers must never inherit a read snapshot.
        finish_read_transaction(session)
        generation_start = time.perf_counter()
        result = generate_answer(body.text, sources, generator,
                                 max_output_tokens=settings.max_output_tokens)
        timings["generation_ms"] = (time.perf_counter() - generation_start) * 1000
    except ProviderError as exc:
        finish_read_transaction(session)
        result = AnswerResult("provider_unavailable", "The model provider is unavailable; retry later.",
                              model=generator.model, warnings=[exc.code])
    except Exception as exc:
        session.rollback()
        logger.error("question_failed category=%s", type(exc).__name__)
        result = AnswerResult("internal_error", "Question processing failed; retry later.",
                              model=generator.model, warnings=["internal_error"])
    if settings.provider == "fake" and not any("FAKE" in warning for warning in result.warnings):
        result.warnings.append("FAKE deterministic providers; not a real-model quality result.")
    timings["total_ms"] = (time.perf_counter() - start) * 1000
    # Recheck after HTTP. Lock serializes publication against collection deletion.
    _owned_collection(session, owner_id, collection_id, lock=True)
    question = Question(user_id=owner_id, collection_id=collection_id, text=body.text,
                        retrieval_config={"version": "exact-cosine-v1", "top_k": settings.retrieval_top_k,
                                          "context_tokens": settings.context_tokens,
                                          "embedding_model": embedder.model,
                                          "document_ids": [str(value) for value in body.document_ids]
                                          if body.document_ids is not None else None})
    session.add(question)
    session.flush()
    answer = Answer(question_id=question.id, status=result.status, text=result.text,
                    model=result.model, prompt_version=PROMPT_VERSION, retrieved_chunks=sources,
                    citations=result.citations, timings=timings, token_usage=result.token_usage,
                    cost_estimate=None, warnings=result.warnings)
    session.add(answer)
    session.commit()
    logger.info("question_completed question_id=%s status=%s timings=%s", question.id, result.status, timings)
    return _serialize(session, question, answer, owner_id)


@router.post("/collections/{collection_id}/questions", response_model=QuestionView, status_code=201)
def create_question(collection_id: UUID, body: QuestionInput, user: User = Depends(current_user),
                    session: Session = Depends(get_session)):
    return ask_question(session, user, collection_id, body.text, body.document_ids)


@router.get("/collections/{collection_id}/questions", response_model=QuestionList)
def list_questions(collection_id: UUID, limit: int = Query(20, ge=1, le=100),
                   offset: int = Query(0, ge=0, le=1000000), user: User = Depends(current_user),
                   session: Session = Depends(get_session)):
    owner_id = user.id
    _owned_collection(session, owner_id, collection_id)
    query = _owned_answer_query(owner_id).where(Question.collection_id == collection_id)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.execute(query.order_by(Question.created_at.desc(), Question.id)
                           .offset(offset).limit(limit)).all()
    return {"items": [_serialize(session, question, answer, owner_id) for question, answer in rows],
            "total": total, "limit": limit, "offset": offset}


@router.get("/questions/{question_id}", response_model=QuestionView)
def get_question(question_id: UUID, user: User = Depends(current_user),
                 session: Session = Depends(get_session)):
    owner_id = user.id
    row = session.execute(_owned_answer_query(owner_id).where(Question.id == question_id)).first()
    if row is None:
        raise AppError("not_found", "Question was not found", 404)
    return _serialize(session, row[0], row[1], owner_id)


@router.get("/questions/{question_id}/sources", response_model=SourcesView)
def get_sources(question_id: UUID, user: User = Depends(current_user),
                session: Session = Depends(get_session)):
    response = get_question(question_id, user, session)
    return {"question_id": question_id, "sources": response["sources"]}
