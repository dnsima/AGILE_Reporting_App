"""Application factory and entry point.

Run locally with::

    uvicorn app.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.middleware import RequestContextMiddleware, register_error_handlers
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging_config import configure_logging, get_logger
from app.db.session import init_db, session_scope
from app.schemas.common import ErrorResponse
from app.services.validation import sync_rule_catalog
from app.web.routes import router as web_router

logger = get_logger(__name__)

DESCRIPTION = """
Ingests, validates, analyses and visualises standardised reporting data from all
AGILE state offices in Nigeria.

* **Ingestion** - upload state templates; headers are auto-mapped to a unified schema.
* **Validation** - rule-based checks plus a seven-dimension DQA that gates the pipeline.
* **Analysis** - 70 KPIs across three layers: state vs target, national vs target, and
  each state's percentage contribution to the national result.
* **Cohorts** - every result is disaggregated by financing cohort.
* **Reporting** - monthly, quarterly, semi-annual and annual reports in Markdown, HTML and PDF.

Authenticate with `POST /api/v1/auth/login` and send the returned token as
`Authorization: Bearer <token>`, or use an `X-API-Key` header for machine integrations.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level, settings.log_json)
    logger.info(
        "starting",
        extra={"version": __version__, "environment": settings.environment},
    )
    settings.ensure_directories()
    init_db()
    with session_scope() as db:
        created = sync_rule_catalog(db)
    if created:
        logger.info("registered validation rules", extra={"count": created})

    if settings.is_production and settings.secret_key.startswith("change-me"):
        logger.error(
            "SECRET_KEY is still the development default; set a strong value before serving "
            "production traffic."
        )

    yield
    logger.info("shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        # Every failure returns the same envelope; declare it once so the
        # generated schema documents it on every endpoint.
        responses={
            400: {"model": ErrorResponse, "description": "Bad request"},
            401: {"model": ErrorResponse, "description": "Authentication required"},
            403: {"model": ErrorResponse, "description": "Permission denied"},
            404: {"model": ErrorResponse, "description": "Not found"},
            409: {"model": ErrorResponse, "description": "Conflict"},
            422: {"model": ErrorResponse, "description": "Invalid input"},
            500: {"model": ErrorResponse, "description": "Unexpected error"},
        },
    )

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
    register_error_handlers(app)

    app.include_router(api_router, prefix=settings.api_prefix)
    app.include_router(web_router)

    static_dir = Path(__file__).parent / "web" / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app


app = create_app()
