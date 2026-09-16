"""Request correlation, access logging and the global error envelope."""

from __future__ import annotations

import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging_config import actor_ctx, get_logger, request_id_ctx

logger = get_logger("app.request")

#: Endpoints that are too chatty to access-log at INFO.
QUIET_PATHS = {"/api/v1/health", "/api/v1/dashboard/stream", "/favicon.ico"}


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, times the request and logs the outcome."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request_id_ctx.set(request_id)
        actor_ctx.set("-")
        request.state.request_id = request_id

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "request failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": duration_ms,
                },
            )
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        if request.url.path not in QUIET_PATHS:
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                },
            )
        return response


def _envelope(code: str, message: str, details=None) -> dict:
    body: dict = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return {"error": body, "request_id": request_id_ctx.get()}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "handled application error",
            extra={"code": exc.code, "path": request.url.path, "status": exc.status_code},
        )
        headers = (
            {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message, exc.details),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {
                "field": ".".join(str(part) for part in error.get("loc", [])[1:]) or "body",
                "message": error.get("msg", "Invalid value"),
                "type": error.get("type"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_envelope("request_validation_error", "The request payload is invalid.", details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(f"http_{exc.status_code}", str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception", extra={"path": request.url.path})
        message = (
            f"{type(exc).__name__}: {exc}"
            if settings.debug
            else "An unexpected error occurred. Quote the request id when reporting this."
        )
        return JSONResponse(
            status_code=500, content=_envelope("internal_error", message)
        )
