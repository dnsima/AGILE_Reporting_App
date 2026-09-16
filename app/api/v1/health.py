"""Liveness and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select

from app import __version__
from app.api.deps import DbSession
from app.core.config import settings
from app.core.events import event_bus
from app.models import Indicator, ReportingPeriod, State, Submission
from app.schemas.common import HealthResponse

router = APIRouter(tags=["System"])


@router.get("/health", response_model=HealthResponse, summary="Service health and readiness")
def health(db: DbSession) -> HealthResponse:
    checks: dict[str, object] = {}
    database = "ok"
    try:
        checks["states"] = db.scalar(select(func.count(State.id))) or 0
        checks["indicators"] = db.scalar(select(func.count(Indicator.id))) or 0
        checks["periods"] = db.scalar(select(func.count(ReportingPeriod.id))) or 0
        checks["submissions"] = db.scalar(select(func.count(Submission.id))) or 0
    except Exception as exc:  # noqa: BLE001 - health must report, not raise
        database = f"error: {exc}"

    seeded = all(checks.get(key, 0) for key in ("states", "indicators"))
    checks["seeded"] = seeded

    return HealthResponse(
        status="ok" if database == "ok" and seeded else "degraded",
        version=__version__,
        environment=settings.environment,
        database=database,
        data_version=event_bus.data_version,
        checks=checks,
    )
