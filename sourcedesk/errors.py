"""Stable application errors; only explicitly allowlisted metadata enters HTTP output."""


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, *, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}
        self.code = code
        self.message = message
        self.status_code = status_code
