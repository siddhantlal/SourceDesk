"""Bounded, idempotent local-file cleanup backed by PostgreSQL deletion intent."""
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from sourcedesk.config import Settings
from sourcedesk.errors import AppError
from sourcedesk.models import FileCleanupTask, utcnow
from sourcedesk.storage import LocalStorage


def drain_file_cleanup(factory: sessionmaker[Session], settings: Settings, limit: int = 5) -> int:
    """Handle at most ``limit`` due tasks, backing failures off for later polls.

    Keep the row lock across the short local unlink and commit. Concurrent API /
    worker drains skip locked rows; a crash rolls back intent removal and retries
    safely because LocalStorage.delete treats an absent original as success.
    """
    handled = 0
    for _ in range(max(0, min(limit, 100))):
        with factory.begin() as session:
            task = session.scalar(select(FileCleanupTask)
                                  .where(FileCleanupTask.available_at <= func.now())
                                  .order_by(FileCleanupTask.available_at, FileCleanupTask.created_at,
                                            FileCleanupTask.storage_key)
                                  .with_for_update(skip_locked=True).limit(1))
            if task is None:
                break
            try:
                LocalStorage(settings.upload_dir).delete(task.storage_key)
            except (OSError, AppError) as exc:
                task.attempts += 1
                task.available_at = utcnow() + timedelta(seconds=min(3600, 30 * 2 ** min(task.attempts - 1, 7)))
                # Persist categories only: OS messages can contain private paths.
                task.error_code = ("invalid_storage_key" if isinstance(exc, AppError)
                                   and exc.code == "invalid_storage_key" else "storage_unavailable")
            else:
                session.delete(task)
            handled += 1
    return handled
