"""Local original-file storage addressed only by generated opaque keys."""
import os
import re
from pathlib import Path
from uuid import uuid4

from sourcedesk.errors import AppError

_KEY = re.compile(r"[0-9a-f]{32}\.pdf\Z")


class LocalStorage:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise AppError("invalid_storage_key", "Invalid internal storage key", 400)
        candidate = self.root / key
        if candidate.is_symlink() or candidate.resolve().parent != self.root:
            raise AppError("invalid_storage_key", "Invalid internal storage path", 400)
        return candidate

    def put(self, data: bytes) -> str:
        key = uuid4().hex + ".pdf"
        target = self.path(key)
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            target.unlink(missing_ok=True)
            raise AppError("storage_unavailable", "Document storage is unavailable", 503) from None
        return key

    def delete(self, key: str) -> None:
        try:
            self.path(key).unlink(missing_ok=True)
        except OSError:
            raise AppError("storage_unavailable", "Document file cleanup failed", 503) from None
