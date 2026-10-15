"""Validated environment configuration; no production credentials are defaulted."""
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SOURCEDESK_", env_file=".env", extra="ignore",
                                      hide_input_in_errors=True)
    database_url: str = Field(repr=False)
    jwt_secret: str = Field(min_length=32, repr=False)
    access_token_minutes: int = Field(default=30, ge=1, le=1440)
    upload_dir: Path = Path("var/uploads")
    frontend_dir: Path = Path(__file__).resolve().parents[1] / "frontend"
    provider: Literal["fake", "openai"] = "fake"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    generation_model: str = "gpt-4.1-mini"
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    upload_max_bytes: int = Field(default=10485760, ge=1024, le=104857600)
    pdf_max_pages: int = Field(default=100, ge=1, le=1000)
    chunk_tokens: int = Field(default=400, ge=16, le=2000)
    chunk_overlap: int = Field(default=60, ge=0)
    context_tokens: int = Field(default=3000, ge=64, le=32000)
    retrieval_top_k: int = Field(default=6, ge=1, le=50)
    max_output_tokens: int = Field(default=1000, ge=32, le=8000)
    provider_timeout_seconds: float = Field(default=30, gt=0, le=120)
    provider_max_attempts: int = Field(default=3, ge=1, le=5)
    embedding_batch_size: int = Field(default=32, ge=1, le=128)
    job_max_attempts: int = Field(default=3, ge=1, le=10)
    job_lease_seconds: int = Field(default=120, ge=5, le=3600)
    worker_poll_seconds: float = Field(default=1, gt=0, le=60)
    processing_version: str = "v1"
    cors_origins: list[str] = []

    @field_validator("database_url", mode="before")
    @classmethod
    def postgres_url(cls, value):
        # Replace the scheme only: credentials and query values are opaque.
        message = "A PostgreSQL URL with a host and database is required"
        if not isinstance(value, str) or value != value.strip() or any(c in value for c in "\r\n\t"):
            raise ValueError(message)
        scheme, separator, remainder = value.partition("://")
        if not separator or scheme not in {"postgres", "postgresql", "postgresql+psycopg"}:
            raise ValueError(message)
        try:
            parsed = urlsplit(value)
            port = parsed.port
            if (not parsed.hostname or not parsed.path.strip("/") or parsed.fragment
                    or (port is not None and not 1 <= port <= 65535)):
                raise ValueError(message)
        except ValueError:
            raise ValueError(message) from None
        return "postgresql+psycopg://" + remainder

    @model_validator(mode="after")
    def consistent_index(self):
        if self.embedding_dimensions != 1536:
            raise ValueError("v1 requires 1536 embedding dimensions; migrate the vector column and reindex before changing dimensions")
        if self.chunk_overlap >= self.chunk_tokens:
            raise ValueError("chunk_overlap must be smaller than chunk_tokens")
        return self


@lru_cache
def get_settings() -> Settings:
    # Required fields come from the environment, which mypy cannot infer.
    return Settings()  # type: ignore[call-arg]
