"""Dashboard data endpoints and the server-sent events stream."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import (
    CurrentPrincipal,
    DbSession,
    enforce_state_scope,
    require,
    visible_state_codes,
)
from app.core.enums import PeriodType, Permission
from app.core.errors import NotFoundError
from app.core.events import event_bus
from app.schemas.analytics import DashboardOverview
from app.services import analysis_model, reference
from app.services import dashboard as dashboard_service

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
    "/analysis",
    summary="The analysis model: every indicator, every state, one payload",
    dependencies=[Depends(require(Permission.ANALYTICS_READ))],
)
def analysis(
    db: DbSession,
    principal: CurrentPrincipal,
    period: str | None = None,
) -> dict:
    """The one payload every panel of the board reads.

    There is deliberately no cohort or state parameter. The board used to
    take five filters and assemble each panel from its own query under its
    own subset, and the panels disagreed -- a reporting rate once read "18 of
    11 states" because the denominator honoured a cohort filter the numerator
    did not. Cohort is a dimension the charts cut by, and a single state is a
    column in the table, so neither needs to narrow the whole board. The only
    scope is the reporting period.
    """
    resolved = _resolve_period(db, period)
    model = analysis_model.build(db, resolved.code)
    payload = analysis_model.to_payload(model)

    # A state PIU may read its own column and the national picture it sits in,
    # but not another state's figures. Blanking the other columns keeps the
    # national totals honest -- they are still every state's -- while the
    # cells the reader may not see read as unreported.
    if principal.is_state_scoped:
        allowed = set(visible_state_codes(db, principal))
        keep = [code in allowed for code in payload["state_codes"]]
        if not all(keep):
            for row in payload["indicators"]:
                row["states"] = [
                    value if visible else None
                    for value, visible in zip(row["states"], keep, strict=True)
                ]
                row["display"] = [
                    text if visible else "\u2014"
                    for text, visible in zip(row["display"], keep, strict=True)
                ]
            visible_names = {
                name for name, ok in zip(payload["states"], keep, strict=True) if ok
            }
            for row in payload["indicators"]:
                row["flag_severity"] = {
                    name: severity
                    for name, severity in row["flag_severity"].items()
                    if name in visible_names
                }
                row["flagged_states"] = sorted(row["flag_severity"])
            payload["scoped_to"] = sorted(allowed)
    return payload


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
    state: str | None = None,
) -> DashboardOverview:
    if state:
        enforce_state_scope(db, principal, state)
    elif principal.is_state_scoped:
        # A state PIU sees its own state whether or not it asks, so the board
        # never shows it national figures it has no business reading.
        own = visible_state_codes(db, principal)
        state = own[0] if own else None
    return dashboard_service.overview(
        db, _resolve_period(db, period), cohort_code=cohort, state_code=state
    )


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
    "/findings-heatmap",
    response_model=dict,
    summary="States by periods, coloured by open findings",
    dependencies=[Depends(require(Permission.ANALYTICS_READ))],
)
def findings_heatmap(
    db: DbSession,
    principal: CurrentPrincipal,
    periods: int = Query(default=6, ge=1, le=24),
    period_type: PeriodType | None = Query(
        default=None, description="Keep the columns inside one period family"
    ),
) -> dict:
    return dashboard_service.findings_heatmap(
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
