"""Deployment checks are isolated: no live database, fixtures, or parent servers."""
import importlib.util
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError


def test_same_origin_deployment_configuration(tmp_path, monkeypatch):
    from sourcedesk.config import Settings, get_settings

    secret = "deployment-test-only-secret-at-least-32-characters"
    suffix = "user:p%40postgres%3A%2F%2F@db.example:5432/sourcedesk?application_name=postgres://"
    for scheme in ("postgres", "postgresql", "postgresql+psycopg"):
        configured = Settings(_env_file=None, database_url=f"{scheme}://{suffix}", jwt_secret=secret)
        assert configured.database_url == f"postgresql+psycopg://{suffix}"
    for invalid in ("sqlite:///db", "postgresqlfake://host/db", "postgresql://host", "postgresql://host:bad/db"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, database_url=invalid, jwt_secret=secret)

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text('<html><script type="module" src="/assets/app.js"></script></html>')
    (frontend / "app.js").write_text("export const origin = location.origin;")
    (tmp_path / "private.txt").write_text("never expose")
    configured = Settings(_env_file=None, database_url="postgresql://host/sourcedesk", jwt_secret=secret,
                          frontend_dir=frontend, cors_origins=[])
    assert configured.frontend_dir == frontend
    monkeypatch.setenv("SOURCEDESK_DATABASE_URL", configured.database_url)
    monkeypatch.setenv("SOURCEDESK_JWT_SECRET", secret)
    get_settings.cache_clear()
    from sourcedesk import main

    monkeypatch.setattr(main, "get_settings", lambda: configured)
    try:
        with TestClient(main.create_app()) as client:
            index = client.get("/")
            assert index.status_code == 200 and "<html>" in index.text
            assert index.headers["x-content-type-options"] == "nosniff"
            assert index.headers["referrer-policy"] == "no-referrer"
            assert index.headers["cache-control"] == "no-store"
            assert "Content-Security-Policy" not in client.get("/docs").headers
            assert client.get("/docs").status_code == 200
            assert client.get("/health").status_code == 200
            asset = client.get("/assets/app.js")
            assert asset.status_code == 200 and "location.origin" in asset.text
            for path in ("/api/does-not-exist", "/not-a-page", "/assets/missing.js", "/assets/%2e%2e/private.txt"):
                response = client.get(path)
                assert response.status_code == 404
                assert "<html>" not in response.text and "never expose" not in response.text
            auth_error = client.post("/api/v1/auth/login", json={})
            assert auth_error.status_code == 422
            assert auth_error.headers["cache-control"] == "no-store"
        configured.frontend_dir = tmp_path / "not-built"
        with TestClient(main.create_app()) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/").status_code == 503
            assert client.get("/ready").status_code == 503
            assert client.get("/assets/app.js").status_code == 404
    finally:
        get_settings.cache_clear()


def test_render_startup_and_process_lifecycle(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "render_start.py"
    assert script.is_file(), "Render needs an actual startup supervisor"
    spec = importlib.util.spec_from_file_location("render_start", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    settings = SimpleNamespace(database_url="postgresql+psycopg://user:opaque@host/db",
                               upload_dir=tmp_path / "uploads")
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = (True,)
    connect = Mock(side_effect=[module.psycopg.OperationalError("not available"), connection])
    def migrate_while_locked(_stop):
        assert settings.upload_dir.is_dir()
        statements = [call.args[0] for call in connection.execute.call_args_list]
        assert any("CREATE EXTENSION IF NOT EXISTS vector" in statement for statement in statements)
        assert not connection.close.called

    migrations = Mock(side_effect=migrate_while_locked)
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path / "token-cache"))
    module.prepare(settings, threading.Event(), connect=connect, migrate=migrations,
                   attempts=2, retry_seconds=0)
    assert settings.upload_dir.is_dir() and (tmp_path / "token-cache").is_dir()
    assert connect.call_count == 2
    assert connect.call_args.args == ("postgresql://user:opaque@host/db",)
    statements = [call.args[0] for call in connection.execute.call_args_list]
    assert any("pg_try_advisory_lock" in statement for statement in statements)
    assert any("CREATE EXTENSION IF NOT EXISTS vector" in statement for statement in statements)
    migrations.assert_called_once()
    connection.close.assert_called_once()

    real_popen = module.spawn
    processes = []

    def spawn(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    idle = [sys.executable, "-c", "import time; time.sleep(30)"]
    # Actual child failure, not a fake Popen: peer must not survive it.
    assert module.supervise([idle, [sys.executable, "-c", "raise SystemExit(7)"]],
                            threading.Event(), popen=spawn, shutdown_timeout=0.5) != 0
    assert len(processes) == 2 and all(process.poll() is not None for process in processes)

    processes.clear()
    stop = threading.Event()
    marker = tmp_path / "started"
    ignore = "signal.SIGBREAK" if os.name == "nt" else "signal.SIGINT"
    stubborn = [sys.executable, "-c",
                f"import signal,time,pathlib; signal.signal({ignore}, signal.SIG_IGN); "
                f"pathlib.Path({str(marker)!r}).touch(); time.sleep(30)"]

    def request_stop():
        for _ in range(300):
            if marker.exists():
                stop.set()
                return
            time.sleep(0.01)
        stop.set()

    timer = threading.Thread(target=request_stop)
    timer.start()
    started = time.monotonic()
    try:
        assert module.supervise([idle, stubborn], stop, popen=spawn, shutdown_timeout=0.2) == 0
    finally:
        stop.set()
        timer.join(timeout=4)
    assert marker.exists() and time.monotonic() - started < 5
    assert len(processes) == 2 and all(process.poll() is not None for process in processes)

    with pytest.raises(module.StartupError):
        module.prepare(settings, threading.Event(), connect=Mock(side_effect=module.psycopg.OperationalError()),
                       migrate=migrations, attempts=1, retry_seconds=0)
