"""Application factory, bounded requests, consistent safe errors, and operational probes."""
import logging
import time
from pathlib import Path
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException

from sourcedesk.auth import router as auth_router
from sourcedesk.config import get_settings
from sourcedesk.db import get_engine
from sourcedesk.errors import AppError

logger = logging.getLogger("sourcedesk.http")


def error_response(request, code, message, status, details=None):
    correlation_id = getattr(request.state, "correlation_id", str(uuid4()))
    headers = {"X-Correlation-ID": correlation_id, "Cache-Control": "no-store",
               "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    error = {"code": code, "message": message, "correlation_id": correlation_id}
    # Only the duplicate response exposes an explicitly allowlisted, owned UUID.
    # Never merge arbitrary exception/provider details into a public response.
    if code == "duplicate_document" and details:
        from uuid import UUID

        try:
            error["document_id"] = str(UUID(str(details.get("document_id"))))
        except (ValueError, TypeError):
            pass
    return JSONResponse({"error": error}, status_code=status, headers=headers)


def create_app() -> FastAPI:
    settings = get_settings()
    # Uvicorn configures its own loggers, not the root/application INFO level.
    # Own one named handler, without reconfiguring global or provider logging.
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    migration_config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    schema_heads = set(ScriptDirectory.from_config(migration_config).get_heads())
    app = FastAPI(title="SourceDesk", version="1.0.0", description=(
        "Page-cited document research. Fake providers are deterministic test/demo components, "
        "not real-model quality evidence. Use the Authorize button after registration."))
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_credentials=False, allow_methods=["GET", "POST", "DELETE"],
                       allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
                       expose_headers=["X-Correlation-ID"])

    @app.middleware("http")
    async def request_bounds(request: Request, call_next):
        request.state.correlation_id = str(uuid4())
        started = time.perf_counter()
        if request.method in {"POST", "PUT", "PATCH"}:
            multipart = request.headers.get("content-type", "").startswith("multipart/form-data")
            limit = settings.upload_max_bytes + 65536 if multipart else 65536
            try:
                declared = int(request.headers.get("content-length", "0"))
            except ValueError:
                return error_response(request, "invalid_request", "Invalid Content-Length", 400)
            if declared < 0 or declared > limit:
                return error_response(request, "request_too_large", "Request body exceeds the configured limit", 413)
            buffered = bytearray()
            async for piece in request.stream():
                if len(buffered) + len(piece) > limit:
                    return error_response(request, "request_too_large", "Request body exceeds the configured limit", 413)
                buffered.extend(piece)
            request._body = bytes(buffered)
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = request.state.correlation_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        # Includes auth responses, user documents and non-fingerprinted frontend assets.
        response.headers["Cache-Control"] = "no-store"
        logger.info("request correlation_id=%s method=%s status=%s duration_ms=%.2f",
                    request.state.correlation_id, request.method, response.status_code,
                    (time.perf_counter() - started) * 1000)
        return response

    @app.exception_handler(AppError)
    async def application_error(request, exc):
        return error_response(request, exc.code, exc.message, exc.status_code, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Never echo input values: validation errors may contain passwords or document text.
        return error_response(request, "validation_error", "Request fields are invalid or out of bounds", 422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response(request, "http_error", "Request could not be completed", exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logger.error("request failed correlation_id=%s category=%s",
                     getattr(request.state, "correlation_id", "unknown"), type(exc).__name__)
        return error_response(request, "internal_error", "An internal error occurred", 500)

    @app.get("/health", tags=["operations"])
    def health():
        return {"status": "ok", "provider": settings.provider,
                "fake_provider": settings.provider == "fake"}

    @app.get("/ready", tags=["operations"])
    def readiness():
        if not (settings.frontend_dir / "index.html").is_file():
            raise AppError("not_ready", "Frontend index is unavailable", 503)
        try:
            with get_engine().connect() as connection:
                connection.execute(text("SELECT 1"))
                extension = connection.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
                revisions = set(connection.scalars(text("SELECT version_num FROM alembic_version")))
                if not extension or not schema_heads or revisions != schema_heads:
                    raise RuntimeError("Schema or pgvector unavailable")
        except Exception:
            raise AppError("not_ready", "Database, schema or pgvector is unavailable", 503) from None
        return {"status": "ready"}

    # These are required components: a missing module must fail application startup.
    from sourcedesk.api import router as api_router
    from sourcedesk.questions import router as questions_router

    app.include_router(auth_router)
    app.include_router(api_router)
    app.include_router(questions_router)

    @app.get("/", include_in_schema=False)
    def frontend_index():
        index = settings.frontend_dir / "index.html"
        if not index.is_file():
            raise AppError("frontend_unavailable", "Frontend index is unavailable", 503)
        return FileResponse(index, media_type="text/html")

    # No SPA fallback: unknown API paths remain JSON 404s. StaticFiles bounds paths
    # to this directory (including symlink resolution); a source-only API can boot
    # even when the separately maintained frontend has not been checked out yet.
    if settings.frontend_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=settings.frontend_dir), name="assets")
    return app


app = create_app()
