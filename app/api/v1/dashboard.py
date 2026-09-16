"""Dashboard data endpoints and the server-sent events stream."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentPrincipal, DbSession, require
from app.core.enums import PeriodType, Permission
from app.core.errors import NotFoundError
from app.core.events import event_bus
from app.schemas.analytics import DashboardOverview
from app.services import dashboard as dashboard_service
from app.services import reference

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

#: Heartbeat interval for the SSE stream, so proxies keep the connection open.
SSE_HEARTBEAT_SECONDS = 20


def _resolve_period(db, period_code: str | None):
    if period_code:
        return reference.get_period_by_code(db, period_code)
    period = reference.latest_period(db)
    if period is None:
        raise NotFoundError("No reporting periods have been configured yet")
    return period


@router.get(
    "/overview",
    response_model=DashboardOverview,
    summary="Headline tiles, cohort rows and reporting status",
    dependencies=[Depends(require(Permission.ANALYTICS_READ))],
)
def overview(
    db: DbSession,
    principal: CurrentPrincipal,
    period: str | None = None,
    cohort: str | None = None,
) -> DashboardOverview:
    return dashboard_service.overview(db, _resolve_period(db, period), cohort_code=cohort)


@router.get(
    "/state-rankings",
    response_model=list[dict],
    summary="States ranked by average KPI achievement",
    dependencies=[Depends(require(Permission.ANALYTICS_READ))],
)
def state_rankings(
    db: DbSession,
    principal: CurrentPrincipal,
    period: str | None = None,
    cohort: str | None = None,
) -> list[dict]:
    return dashboard_service.state_rankings(db, _resolve_period(db, period), cohort)


@router.get(
    "/dqa-heatmap",
    response_model=dict,
    summary="States by periods DQA heatmap",
    dependencies=[Depends(require(Permission.ANALYTICS_READ))],
)
def dqa_heatmap(
    db: DbSession,
    principal: CurrentPrincipal,
    periods: int = Query(default=6, ge=1, le=24),
    period_type: PeriodType | None = Query(
        default=None, description="Keep the columns inside one period family"
    ),
) -> dict:
    return dashboard_service.dqa_heatmap(
        db, limit=periods, period_type=str(period_type) if period_type else None
    )


@router.get(
    "/version",
    response_model=dict,
    summary="Cheap poll target: current data version and recent events",
    dependencies=[Depends(require(Permission.ANALYTICS_READ))],
)
def data_version(principal: CurrentPrincipal) -> dict:
    return {"data_version": event_bus.data_version, "recent": event_bus.recent(10)}


@router.get(
    "/stream",
    summary="Server-sent event stream; pushes an event whenever data changes",
    dependencies=[Depends(require(Permission.ANALYTICS_READ))],
)
async def stream(request: Request) -> StreamingResponse:
    """Long-lived SSE connection driving the dashboard's automatic refresh."""

    async def event_source():
        queue = await event_bus.subscribe()
        try:
            yield f"event: hello\ndata: {json.dumps({'data_version': event_bus.data_version})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_SECONDS)
                    yield f"event: change\ndata: {message}\n\n"
                except TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            await event_bus.unsubscribe(queue)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
