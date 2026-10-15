"""Real PostgreSQL integration fixtures. Never target development data."""
import os
from pathlib import Path

import pytest
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


@pytest.fixture(scope="session")
def settings(tmp_path_factory):
    root = Path(__file__).resolve().parents[1]
    test_env = dotenv_values(root / ".env.test")
    url = (os.getenv("SOURCEDESK_TEST_DATABASE_URL") or os.getenv("TEST_DATABASE_URL")
           or test_env.get("SOURCEDESK_TEST_DATABASE_URL") or test_env.get("TEST_DATABASE_URL"))
    if not url:
        pytest.skip("Set explicit SOURCEDESK_TEST_DATABASE_URL for real PostgreSQL integration tests")
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or "test" not in (parsed.database or "").lower():
        pytest.fail("Refusing cleanup: dedicated PostgreSQL database name must contain 'test'")
    app_url = os.getenv("SOURCEDESK_DATABASE_URL") or dotenv_values(root / ".env").get("SOURCEDESK_DATABASE_URL")
    if app_url:
        app = make_url(app_url)
        local_hosts = {"localhost", "127.0.0.1", "::1", None}
        same_host = app.host == parsed.host or (app.host in local_hosts and parsed.host in local_hosts)
        if same_host and (app.port or 5432) == (parsed.port or 5432) and app.database == parsed.database:
            pytest.fail("Test database must be different from configured application database")
    from sourcedesk.config import get_settings
    from sourcedesk.db import get_engine, session_factory

    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()
    session_factory.cache_clear()
    get_settings.cache_clear()
    # Never rewrite .env or leak test overrides into the caller's process state.
    with pytest.MonkeyPatch.context() as env:
        env.setenv("SOURCEDESK_DATABASE_URL", url)
        env.setenv("SOURCEDESK_JWT_SECRET", "test-only-signing-key-not-for-production-123456789")
        env.setenv("SOURCEDESK_PROVIDER", "fake")
        env.setenv("SOURCEDESK_UPLOAD_DIR", str(tmp_path_factory.mktemp("uploads")))
        try:
            yield get_settings()
        finally:
            if get_engine.cache_info().currsize:
                get_engine().dispose()
            get_engine.cache_clear()
            session_factory.cache_clear()
            get_settings.cache_clear()


@pytest.fixture
def db_session(settings):
    from sourcedesk import models  # noqa: F401
    from sourcedesk.db import Base

    engine = create_engine(settings.database_url, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        pytest.fail("Explicit test PostgreSQL database is unavailable; integration blocked", pytrace=False)
    with engine.begin() as connection:
        tables = ", ".join('"' + table.name + '"' for table in Base.metadata.sorted_tables)
        connection.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    for path in settings.upload_dir.iterdir():
        if path.is_file():
            path.unlink()
    with Session(engine, expire_on_commit=False) as session:
        yield session
        session.rollback()
    engine.dispose()


@pytest.fixture
def client(settings, db_session):
    from fastapi.testclient import TestClient

    from sourcedesk.main import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def pdf_bytes():
    from tests.helpers import make_pdf
    return make_pdf("SourceDesk is a document research application. Its launch year is 2026.")
