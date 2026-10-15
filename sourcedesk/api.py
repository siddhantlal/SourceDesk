"""Owned collection/document HTTP operations and pre-queue PDF validation."""
import logging
import re
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sourcedesk.auth import current_user
from sourcedesk.cleanup import drain_file_cleanup
from sourcedesk.config import get_settings
from sourcedesk.db import get_session, session_factory
from sourcedesk.errors import AppError
from sourcedesk.models import Collection, Document, FileCleanupTask, IngestionJob, Page, User
from sourcedesk.storage import LocalStorage

router = APIRouter(prefix="/api/v1", tags=["collections and documents"])
logger = logging.getLogger("sourcedesk.http")


def best_effort_file_cleanup():
    # DB deletion is already committed. A cleanup/connection failure must not
    # turn it into an HTTP error; durable intent is retried by the worker.
    try:
        drain_file_cleanup(session_factory(), get_settings())
    except Exception:
        logger.warning("file cleanup deferred category=cleanup_unavailable")


class CollectionInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class CollectionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    owner_id: UUID
    name: str
    description: str
    created_at: datetime


class DocumentView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    collection_id: UUID
    filename: str
    status: str
    page_count: int
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    processing_version: str


class PageView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    document_id: UUID
    page_number: int
    text: str
    extraction_metadata: dict


class CollectionList(BaseModel):
    items: list[CollectionView]
    total: int
    limit: int
    offset: int


class DocumentList(BaseModel):
    items: list[DocumentView]
    total: int
    limit: int
    offset: int


def owned_collection(session, user, collection_id, *, lock=False):
    statement = select(Collection).where(Collection.id == collection_id, Collection.owner_id == user.id)
    if lock:
        statement = statement.with_for_update()
    collection = session.scalar(statement)
    if not collection:
        raise AppError("not_found", "Collection was not found", 404)
    return collection


def owned_document(session, user, document_id, *, lock=False):
    statement = select(Document).where(Document.id == document_id, Document.owner_id == user.id)
    if lock:
        statement = statement.with_for_update()
    document = session.scalar(statement)
    if not document:
        raise AppError("not_found", "Document was not found", 404)
    return document


def validate_pdf(data: bytes, *, max_bytes: int, max_pages: int) -> int:
    if len(data) > max_bytes:
        raise AppError("file_too_large", "PDF exceeds the configured upload size limit", 413)
    if not data.startswith(b"%PDF-"):
        raise AppError("invalid_pdf", "The uploaded file is not a readable PDF", 400)
    try:
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise AppError("encrypted_pdf", "Encrypted or password-protected PDFs are not supported", 400)
        count = len(reader.pages)
        if count < 1:
            raise AppError("invalid_pdf", "PDF must contain at least one page", 400)
        if count > max_pages:
            raise AppError("too_many_pages", "PDF exceeds the configured page count limit", 400)
        # Resolve the bounded page tree now; text extraction remains durable worker work.
        for page in reader.pages:
            _ = page.mediabox
        return count
    except AppError:
        raise
    except Exception:
        raise AppError("invalid_pdf", "PDF is malformed or cannot be parsed", 400) from None


def display_filename(original: str | None) -> str:
    name = (original or "document.pdf").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip(" .")
    return name[:255] or "document.pdf"


@router.post("/collections", response_model=CollectionView, status_code=201)
def create_collection(body: CollectionInput, user: User = Depends(current_user), session: Session = Depends(get_session)):
    if not body.name.strip():
        raise AppError("validation_error", "Collection name must not be blank", 422)
    collection = Collection(owner_id=user.id, name=body.name.strip(), description=body.description)
    session.add(collection)
    session.commit()
    return collection


@router.get("/collections", response_model=CollectionList)
def list_collections(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=1000000),
                     user: User = Depends(current_user), session: Session = Depends(get_session)):
    statement = select(Collection).where(Collection.owner_id == user.id)
    total = session.scalar(select(func.count()).select_from(statement.subquery()))
    items = session.scalars(statement.order_by(Collection.created_at.desc(), Collection.id).offset(offset).limit(limit)).all()
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/collections/{collection_id}", response_model=CollectionView)
def get_collection(collection_id: UUID, user: User = Depends(current_user), session: Session = Depends(get_session)):
    return owned_collection(session, user, collection_id)


@router.delete("/collections/{collection_id}", status_code=204)
def delete_collection(collection_id: UUID, user: User = Depends(current_user), session: Session = Depends(get_session)):
    collection = owned_collection(session, user, collection_id, lock=True)
    # Lock the snapshot before enqueueing: a concurrent document DELETE may
    # otherwise remove one row and enqueue its key between this read and commit.
    keys = session.scalars(select(Document.storage_key).where(Document.collection_id == collection.id,
                                                            Document.owner_id == user.id)
                           .order_by(Document.id).with_for_update()).all()
    session.add_all(FileCleanupTask(storage_key=key) for key in keys)
    session.delete(collection)
    session.commit()
    best_effort_file_cleanup()
    return Response(status_code=204)


@router.post("/collections/{collection_id}/documents", response_model=DocumentView, status_code=202)
def upload_document(collection_id: UUID, file: UploadFile = File(...), user: User = Depends(current_user),
                    session: Session = Depends(get_session)):
    owned_collection(session, user, collection_id)
    session.commit()
    settings = get_settings()
    try:
        data = file.file.read(settings.upload_max_bytes + 1)
    finally:
        file.file.close()
    page_count = validate_pdf(data, max_bytes=settings.upload_max_bytes, max_pages=settings.pdf_max_pages)
    digest = sha256(data).hexdigest()
    storage = LocalStorage(settings.upload_dir)
    key = storage.put(data)
    try:
        # Collection deletion during parser/storage work cannot create an orphan record.
        owned_collection(session, user, collection_id, lock=True)
        existing = session.scalar(select(Document.id).where(Document.collection_id == collection_id,
                                                            Document.owner_id == user.id,
                                                            Document.sha256 == digest))
        if existing:
            raise AppError("duplicate_document", "This PDF already exists in the collection", 409,
                           details={"document_id": str(existing)})
        document = Document(collection_id=collection_id, owner_id=user.id,
                            filename=display_filename(file.filename), storage_key=key, sha256=digest,
                            status="queued", page_count=page_count, processing_version=settings.processing_version)
        session.add(document)
        session.flush()
        session.add(IngestionJob(document_id=document.id, status="queued"))
        session.commit()
    except Exception:
        session.rollback()
        storage.delete(key)
        raise
    return document


@router.get("/collections/{collection_id}/documents", response_model=DocumentList)
def list_documents(collection_id: UUID, limit: int = Query(20, ge=1, le=100),
                   offset: int = Query(0, ge=0, le=1000000), user: User = Depends(current_user),
                   session: Session = Depends(get_session)):
    owned_collection(session, user, collection_id)
    statement = select(Document).where(Document.collection_id == collection_id, Document.owner_id == user.id)
    total = session.scalar(select(func.count()).select_from(statement.subquery()))
    documents = session.scalars(statement.order_by(Document.created_at.desc(), Document.id).offset(offset).limit(limit)).all()
    return {"items": documents, "total": total, "limit": limit, "offset": offset}


@router.get("/documents/{document_id}", response_model=DocumentView)
def get_document(document_id: UUID, user: User = Depends(current_user), session: Session = Depends(get_session)):
    return owned_document(session, user, document_id)


@router.get("/documents/{document_id}/download", response_class=Response,
            responses={200: {"content": {"application/pdf": {}}}})
def download_document(document_id: UUID, user: User = Depends(current_user), session: Session = Depends(get_session)):
    # Snapshot the size-bounded original while holding its deletion lock. Delayed
    # FileResponse opens race with DELETE (and open handles block unlink on Windows).
    # Downloads use at most one accepted PDF in memory, rather than an open stream.
    document = owned_document(session, user, document_id, lock=True)
    path = LocalStorage(get_settings().upload_dir).path(document.storage_key)
    try:
        data = path.read_bytes()
    except OSError:
        raise AppError("file_unavailable", "Original document is unavailable", 404) from None
    filename = document.filename
    session.commit()
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename*=utf-8''{quote(filename)}",
                             "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@router.get("/documents/{document_id}/pages/{page_number}", response_model=PageView)
def get_page(document_id: UUID, page_number: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    owned_document(session, user, document_id)
    page = session.scalar(select(Page).where(Page.document_id == document_id, Page.page_number == page_number))
    if page_number < 1 or not page:
        raise AppError("not_found", "Extracted page was not found; check processing status", 404)
    return page


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(document_id: UUID, user: User = Depends(current_user), session: Session = Depends(get_session)):
    document = owned_document(session, user, document_id, lock=True)
    session.add(FileCleanupTask(storage_key=document.storage_key))
    session.delete(document)
    session.commit()
    best_effort_file_cleanup()
    return Response(status_code=204)
