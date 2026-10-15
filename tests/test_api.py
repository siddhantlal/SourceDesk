"""Focused API contract tests, added one vertical slice at a time."""
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from tests.helpers import create_collection, register_and_login, upload_document


def test_cross_user_endpoints_are_hidden(client, db_session, pdf_bytes):
    owner = register_and_login(client)
    other = register_and_login(client, "other@example.com")
    collection = create_collection(client, owner)
    collection_id = collection["id"]
    uploaded = upload_document(client, owner, collection_id, pdf_bytes)
    assert uploaded.status_code == 202, uploaded.text
    document_id = uploaded.json()["id"]
    from uuid import UUID

    from sourcedesk.models import Answer, Page, Question
    page = Page(document_id=UUID(document_id), page_number=1, text="Private owner-only text", extraction_metadata={})
    question = Question(user_id=UUID(client.get("/api/v1/users/me", headers=owner).json()["id"]),
                        collection_id=UUID(collection_id), text="Private question", retrieval_config={})
    db_session.add_all([page, question])
    db_session.flush()
    answer = Answer(question_id=question.id, status="answered", text="Private answer",
                    model="fake-extractive-v1", prompt_version="v1", retrieved_chunks=[], citations=[], timings={}, warnings=[])
    db_session.add(answer)
    db_session.commit()
    paths = [f"/collections/{collection_id}", f"/collections/{collection_id}/documents",
             f"/documents/{document_id}", f"/documents/{document_id}/download",
             f"/documents/{document_id}/pages/1", f"/collections/{collection_id}/questions",
             f"/questions/{question.id}", f"/questions/{question.id}/sources"]
    for path in paths:
        result = client.get("/api/v1" + path, headers=other)
        assert result.status_code == 404, (path, result.text)
        assert result.json()["error"]["correlation_id"]
    assert upload_document(client, other, collection_id, pdf_bytes).status_code == 404
    assert client.post(f"/api/v1/collections/{collection_id}/questions", headers=other,
                       json={"question": "What is private?"}).status_code == 404
    assert client.delete(f"/api/v1/documents/{document_id}", headers=other).status_code == 404
    assert client.delete(f"/api/v1/collections/{collection_id}", headers=other).status_code == 404
    assert client.get("/api/v1/collections", headers=other).json()["items"] == []
    assert client.get(f"/api/v1/documents/{document_id}/download", headers=owner).content == pdf_bytes
    assert client.get(f"/api/v1/documents/{document_id}/pages/1", headers=owner).json()["text"] == page.text
    assert client.get("/api/v1/collections").status_code == 401


def test_registration_and_token_lifecycle(client, settings, db_session, monkeypatch):
    # Fresh interpreter: pytest's logging capture must not hide missing default
    # handlers/levels. Repeated factories must emit exactly one safe HTTP record.
    import subprocess
    import sys

    probe = subprocess.run([sys.executable, "-c", """
from fastapi.testclient import TestClient
from sourcedesk.main import create_app
create_app()
with TestClient(create_app()) as client:
    response = client.post('/api/v1/auth/register', json={'email': 'private-invalid-email', 'password': 'do-not-echo'})
    assert response.status_code == 422
    print(response.headers['x-correlation-id'])
"""], capture_output=True, text=True, timeout=30)
    assert probe.returncode == 0, "Default HTTP logging subprocess failed"
    correlation = probe.stdout.strip()
    assert probe.stderr.count(f"request correlation_id={correlation} method=POST status=422") == 1
    assert "duration_ms=" in probe.stderr
    assert "private-invalid-email" not in probe.stderr and "do-not-echo" not in probe.stderr

    invalid = client.post("/api/v1/auth/register", json={"email": "private-invalid-email", "password": "do-not-echo"})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"
    assert "private-invalid-email" not in invalid.text and "do-not-echo" not in invalid.text
    assert invalid.json()["error"]["correlation_id"] == invalid.headers["x-correlation-id"]
    oversized = client.post("/api/v1/auth/register", content=iter([b"x" * 65536, b"tail"]),
                            headers={"Content-Type": "application/json"})
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "request_too_large"
    headers = register_and_login(client, email="Owner@Example.COM")
    me = client.get("/api/v1/users/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["email"] == "owner@example.com"
    assert "password_hash" not in me.json()
    assert client.post("/api/v1/auth/register", json={"email": "owner@example.com", "password": "long-enough-password"}).status_code == 409
    bad = client.post("/api/v1/auth/login", data={"username": "owner@example.com", "password": "wrong"})
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "invalid_credentials"
    assert bad.json()["error"]["correlation_id"] == bad.headers["x-correlation-id"]
    assert client.get("/api/v1/users/me").status_code == 401
    expired = jwt.encode({"sub": me.json()["id"], "exp": datetime.now(UTC) - timedelta(seconds=1)}, settings.jwt_secret, algorithm="HS256")
    assert client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    assert client.get("/api/v1/users/me", headers={"Authorization": "Bearer invalid"}).status_code == 401
    from uuid import UUID

    from sourcedesk.models import User
    user = db_session.get(User, UUID(me.json()["id"]))
    user.is_active = False
    db_session.commit()
    assert client.get("/api/v1/users/me", headers=headers).status_code == 401
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/ready").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").json()["components"]["securitySchemes"]
    # A deployment missing a critical router must fail rather than advertise health.
    import builtins

    from sourcedesk.main import create_app

    real_import = builtins.__import__

    def missing_questions(name, *args, **kwargs):
        if name == "sourcedesk.questions":
            raise ModuleNotFoundError("Missing required questions module", name=name)
        return real_import(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", missing_questions)
        with pytest.raises(ModuleNotFoundError):
            create_app()


@pytest.mark.parametrize("case,code", [("signature", "invalid_pdf"), ("malformed", "invalid_pdf"),
                                      ("encrypted", "encrypted_pdf"), ("pages", "too_many_pages"),
                                      ("size", "file_too_large")])
def test_invalid_pdf_representative_rejection(request, case, code):
    # The pure validation slice runs even while native PostgreSQL is provisioning.
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter

    from sourcedesk.api import validate_pdf
    from sourcedesk.errors import AppError
    from tests.helpers import make_pdf
    data = make_pdf("A legitimate text PDF.")
    max_bytes, max_pages = 10485760, 100
    if case == "signature":
        data = b"not a pdf"
    elif case == "malformed":
        data = b"%PDF-1.7\nmalformed content\n%%EOF"
    elif case == "encrypted":
        writer = PdfWriter()
        writer.append(PdfReader(BytesIO(data)))
        writer.encrypt("password")
        output = BytesIO()
        writer.write(output)
        data = output.getvalue()
    elif case == "pages":
        max_pages = 1
        data = make_pdf(pages=["First page", "Second page"])
    elif case == "size":
        max_bytes = len(data) - 1
    with pytest.raises(AppError) as exc:
        validate_pdf(data, max_bytes=max_bytes, max_pages=max_pages)
    assert exc.value.code == code
    client = request.getfixturevalue("client")
    settings = request.getfixturevalue("settings")
    old_bytes, old_pages = settings.upload_max_bytes, settings.pdf_max_pages
    settings.upload_max_bytes, settings.pdf_max_pages = max_bytes, max_pages
    try:
        headers = register_and_login(client)
        collection = create_collection(client, headers)
        response = upload_document(client, headers, collection["id"], data)
        assert response.status_code in (400, 413, 422), response.text
        assert response.json()["error"]["code"] == code
        assert client.get(f"/api/v1/collections/{collection['id']}/documents", headers=headers).json()["items"] == []
    finally:
        settings.upload_max_bytes, settings.pdf_max_pages = old_bytes, old_pages


def test_duplicate_content_returns_existing_document_without_extra_job(client, db_session, pdf_bytes, settings, monkeypatch):
    from sqlalchemy import func, select

    from sourcedesk.models import Document, IngestionJob
    headers = register_and_login(client)
    collection = create_collection(client, headers)
    first = upload_document(client, headers, collection["id"], pdf_bytes, "../../untrusted.pdf")
    assert first.status_code == 202, first.text
    duplicate = upload_document(client, headers, collection["id"], pdf_bytes, "renamed.pdf")
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["error"]["code"] == "duplicate_document"
    assert duplicate.json()["error"]["document_id"] == first.json()["id"]
    assert db_session.scalar(select(func.count()).select_from(Document)) == 1
    assert db_session.scalar(select(func.count()).select_from(IngestionJob)) == 1
    assert len(list(settings.upload_dir.iterdir())) == 1
    assert first.json()["filename"] == "untrusted.pdf"
    assert "storage_key" not in first.json()
    other_collection = create_collection(client, headers, "Separate research")
    assert upload_document(client, headers, other_collection["id"], pdf_bytes).status_code == 202

    # A committed delete must remain successful when Windows holds the original open.
    from pathlib import Path
    from uuid import UUID

    first_key = db_session.get(Document, UUID(first.json()["id"])).storage_key
    original_unlink = Path.unlink

    def permission_denied(path, *args, **kwargs):
        if path.name == first_key:
            raise PermissionError("private filesystem detail must not be stored")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", permission_denied)
        deleted = client.delete(f"/api/v1/documents/{first.json()['id']}", headers=headers)
    assert deleted.status_code == 204, deleted.text

    from sourcedesk.cleanup import drain_file_cleanup
    from sourcedesk.db import session_factory
    from sourcedesk.models import FileCleanupTask

    db_session.expire_all()
    assert db_session.get(Document, UUID(first.json()["id"])) is None
    pending = db_session.get(FileCleanupTask, first_key)
    assert pending is not None and pending.attempts == 1
    assert pending.available_at > datetime.now(UTC)
    assert pending.error_code == "storage_unavailable"
    assert (settings.upload_dir / first_key).is_file()
    assert drain_file_cleanup(session_factory(), settings) == 0  # No busy retry.
    pending.available_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    assert drain_file_cleanup(session_factory(), settings, limit=0) == 0
    with session_factory().begin() as claimant:
        assert claimant.scalar(select(FileCleanupTask).where(FileCleanupTask.storage_key == first_key)
                               .with_for_update()) is not None
        # Another drainer must skip a claimed row rather than block or unlink it.
        assert drain_file_cleanup(session_factory(), settings) == 0
        assert (settings.upload_dir / first_key).is_file()
    assert drain_file_cleanup(session_factory(), settings) == 1
    db_session.expire_all()
    assert db_session.get(FileCleanupTask, first_key) is None
    assert not (settings.upload_dir / first_key).exists()

    # All collection keys are enqueued before deletion; one failed unlink cannot
    # abandon another original. A different collection's original is untouched.
    from tests.helpers import make_pdf

    batch = create_collection(client, headers, "Batch cleanup")
    for text in ("First collection original", "Second collection original"):
        assert upload_document(client, headers, batch["id"], make_pdf(text)).status_code == 202
    keys = db_session.scalars(select(Document.storage_key).where(Document.collection_id == UUID(batch["id"]))).all()
    assert len(keys) == 2
    first_key = keys[0]
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", permission_denied)
        deleted = client.delete(f"/api/v1/collections/{batch['id']}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    db_session.expire_all()
    assert list(db_session.scalars(select(FileCleanupTask.storage_key))) == [first_key]
    assert (settings.upload_dir / first_key).is_file()
    assert not (settings.upload_dir / keys[1]).exists()
    pending = db_session.get(FileCleanupTask, first_key)
    pending.available_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    # Simulate unlink succeeding just before a cleanup transaction crashes.
    (settings.upload_dir / first_key).unlink()
    assert drain_file_cleanup(session_factory(), settings, limit=1) == 1
    db_session.expire_all()
    assert db_session.get(FileCleanupTask, first_key) is None
    assert len(list(settings.upload_dir.iterdir())) == 1
    remaining = client.get(f"/api/v1/collections/{other_collection['id']}/documents", headers=headers).json()
    assert remaining["total"] == 1
    remaining_id = remaining["items"][0]["id"]
    remaining_key = db_session.get(Document, UUID(remaining_id)).storage_key

    # Even an unavailable post-commit drainer cannot invalidate the DB delete.
    def unavailable_drainer(*args, **kwargs):
        raise RuntimeError("private connection detail must not be logged")

    with monkeypatch.context() as patch:
        patch.setattr("sourcedesk.api.drain_file_cleanup", unavailable_drainer)
        assert client.delete(f"/api/v1/documents/{remaining_id}", headers=headers).status_code == 204
    db_session.expire_all()
    assert db_session.get(Document, UUID(remaining_id)) is None
    assert db_session.get(FileCleanupTask, remaining_key).attempts == 0
    assert (settings.upload_dir / remaining_key).is_file()
    assert drain_file_cleanup(session_factory(), settings) == 1
    db_session.expire_all()
    assert db_session.get(FileCleanupTask, remaining_key) is None
    assert not (settings.upload_dir / remaining_key).exists()
