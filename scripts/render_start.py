"""Render entrypoint: provision once, then supervise two independent processes.

Run only on the disk-owning instance, never in a build/pre-deploy hook. All startup
errors are deliberately redacted: a driver/settings exception can include a DSN.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger("sourcedesk.deploy")
MIGRATION_LOCK = 741093260  # Shared by every SourceDesk Render entrypoint.


class StartupError(RuntimeError):
    """Safe, credential-free startup failure."""


def spawn(command, **kwargs):
    """Separate process groups let shutdown include descendants, not this parent."""
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {
        "start_new_session": True,
    }
    return subprocess.Popen(command, cwd=ROOT, **options, **kwargs)


def stop_processes(processes, timeout=25):
    """Signal every child before waiting; enforce one shared shutdown deadline."""
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            if sys.platform == "win32":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                # Worker catches KeyboardInterrupt; Uvicorn handles SIGINT gracefully.
                os.killpg(process.pid, signal.SIGINT)
        except (OSError, ProcessLookupError):
            pass
    deadline = time.monotonic() + timeout
    while any(process.poll() is None for process in processes) and time.monotonic() < deadline:
        time.sleep(0.05)
    for process in processes:
        try:
            if sys.platform != "win32":
                # Also remove descendants left by an already-exited group leader.
                os.killpg(process.pid, signal.SIGKILL)
            elif process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, check=False)
                if process.poll() is None:
                    process.kill()
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def supervise(commands, stop, *, popen=spawn, shutdown_timeout=25):
    """Any unexpected exit (including exit 0) tears down its peer and fails deploy."""
    processes = []
    try:
        for command in commands:
            if stop.is_set():
                return 0
            processes.append(popen(command))
        while not stop.is_set():
            if any(process.poll() is not None for process in processes):
                logger.error("child_exited; stopping all services")
                return 1
            stop.wait(0.1)
        return 0
    except OSError:
        logger.error("child_start_failed")
        return 1
    finally:
        stop_processes(processes, shutdown_timeout)


def run_migrations(stop, timeout=180):
    # Suppress third-party tracebacks that can reveal connection/settings values.
    # Alembic reads the same normalized environment and its existing env.py.
    process = spawn([sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"),
                     "upgrade", "head"], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if stop.wait(0.1):
                raise StartupError("Startup interrupted")
            if time.monotonic() >= deadline:
                raise StartupError("Migration exceeded startup deadline")
        if process.returncode != 0:
            raise StartupError("Migration failed; services were not started")
    finally:
        stop_processes([process], timeout=5)


def prepare(settings, stop, *, connect=psycopg.connect, migrate=run_migrations,
            attempts=12, retry_seconds=2):
    """Bounded direct-DSN connection, owner extension provisioning, locked migration."""
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    cache = os.getenv("TIKTOKEN_CACHE_DIR")
    if cache:
        Path(cache).mkdir(parents=True, exist_ok=True)
    # Settings validated the exact scheme. Leave password/query contents untouched.
    dsn = "postgresql://" + settings.database_url.split("://", 1)[1]
    connection = None
    for attempt in range(attempts):
        if stop.is_set():
            raise StartupError("Startup interrupted")
        try:
            connection = connect(dsn, connect_timeout=5, autocommit=True)
            break
        except (psycopg.OperationalError, psycopg.InterfaceError):
            logger.warning("database_unavailable attempt=%s", attempt + 1)
            if attempt + 1 == attempts:
                raise StartupError("Database connection retry limit reached") from None
            stop.wait(retry_seconds)
    if connection is None:
        raise StartupError("Database connection retry limit reached")
    try:
        connection.execute("SET statement_timeout = '120s'")
        connection.execute("SET lock_timeout = '10s'")
        for _ in range(attempts):
            if stop.is_set():
                raise StartupError("Startup interrupted")
            locked = connection.execute("SELECT pg_try_advisory_lock(%s)", (MIGRATION_LOCK,)).fetchone()[0]
            if locked:
                break
            stop.wait(retry_seconds)
        else:
            raise StartupError("Migration lock retry limit reached")
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        if stop.is_set():
            raise StartupError("Startup interrupted")
        # Hold a session-scoped advisory lock while Alembic uses its own connection.
        migrate(stop)
        logger.info("database_provisioned schema_upgraded")
    finally:
        connection.close()  # Releases the session advisory lock, including on failure.


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    stop = threading.Event()
    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, lambda *_: stop.set())
    if sys.platform == "win32":
        previous[signal.SIGBREAK] = signal.signal(signal.SIGBREAK, lambda *_: stop.set())
    try:
        from sourcedesk.config import get_settings

        settings = get_settings()
        port = int(os.getenv("PORT", "10000"))
        if not 1 <= port <= 65535:
            raise StartupError("PORT must be between 1 and 65535")
        prepare(settings, stop)
        return supervise([
            [sys.executable, "-m", "uvicorn", "sourcedesk.main:app", "--host", "0.0.0.0",
             "--port", str(port), "--workers", "1", "--timeout-graceful-shutdown", "20",
             "--proxy-headers", "--forwarded-allow-ips", os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1")],
            [sys.executable, "-m", "sourcedesk.worker"],
        ], stop)
    except Exception as exc:
        if stop.is_set():
            return 0
        logger.error("startup_failed category=%s (connection details suppressed)", type(exc).__name__)
        return 1
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
