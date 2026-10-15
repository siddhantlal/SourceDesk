"""Relational ownership and durable processing records (v1 vector dimension 1536)."""
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from sourcedesk.db import Base


def utcnow():
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Collection(Base):
    __tablename__ = "collections"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("collection_id", "sha256", name="uq_documents_collection_sha256"),
        CheckConstraint("page_count >= 0", name="ck_documents_page_count"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    collection_id: Mapped[UUID] = mapped_column(ForeignKey("collections.id", ondelete="CASCADE"), index=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(40), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    processing_version: Mapped[str] = mapped_column(String(80), default="v1")


class FileCleanupTask(Base):
    """Original-file deletion intent; deliberately independent of deleted rows."""
    __tablename__ = "file_cleanup_tasks"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="ck_file_cleanup_attempts"),
        Index("ix_file_cleanup_claim", "available_at", "created_at"),
    )
    storage_key: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    error_code: Mapped[str | None] = mapped_column(String(80))


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (
        UniqueConstraint("document_id", "page_number", name="uq_pages_document_page"),
        CheckConstraint("page_number >= 1", name="ck_pages_number"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page_number: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    extraction_metadata: Mapped[dict] = mapped_column(JSON, default=dict)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "page_number", "position", name="uq_chunks_document_page_position"),
        CheckConstraint("page_number >= 1 AND position >= 0 AND token_count > 0", name="ck_chunks_bounds"),
        Index("ix_chunks_tenant_collection", "owner_id", "collection_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    collection_id: Mapped[UUID] = mapped_column(ForeignKey("collections.id", ondelete="CASCADE"))
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    page_number: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536))
    embedding_model: Mapped[str] = mapped_column(String(150))
    chunking_version: Mapped[str] = mapped_column(String(80))


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "available_at", "lease_expires_at"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[UUID | None] = mapped_column(Uuid)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)


class Question(Base):
    __tablename__ = "questions"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    collection_id: Mapped[UUID] = mapped_column(ForeignKey("collections.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    retrieval_config: Mapped[dict] = mapped_column(JSON, default=dict)


class Answer(Base):
    __tablename__ = "answers"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    question_id: Mapped[UUID] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String(40))
    text: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(150))
    prompt_version: Mapped[str] = mapped_column(String(80), default="v1")
    retrieved_chunks: Mapped[list] = mapped_column(JSON, default=list)
    citations: Mapped[list] = mapped_column(JSON, default=list)
    timings: Mapped[dict] = mapped_column(JSON, default=dict)
    token_usage: Mapped[dict | None] = mapped_column(JSON)
    cost_estimate: Mapped[dict | None] = mapped_column(JSON)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
