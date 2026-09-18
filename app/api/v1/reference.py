"""Reference data: cohorts, states, indicators, categories, periods and targets."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.api.deps import CurrentPrincipal, DbSession, require
from app.core.enums import PeriodType, Permission, TargetLevel
from app.core.errors import NotFoundError
from app.models import (
    Cohort,
    Indicator,
    IndicatorCategory,
    PeriodReopening,
    ReportingPeriod,
    State,
    Target,
    User,
)
from app.schemas.common import Message
from app.schemas.reference import (
    CohortRead,
    IndicatorCategoryRead,
    IndicatorRead,
    IndicatorUpdate,
    PeriodCloseRequest,
    PeriodCreate,
    PeriodGenerate,
    PeriodRead,
    PeriodReopenRequest,
    ReopeningCreate,
    ReopeningRead,
    ReopeningRevoke,
    StateRead,
    StateUpdate,
    TargetBulkUpsert,
    TargetRead,
)
from app.services import audit, reconciliation, reference
from app.services import periods as period_service

router = APIRouter(prefix="/reference", tags=["Reference data"])


def _state_read(state: State) -> StateRead:
    return StateRead(
        id=state.id,
        code=state.code,
        name=state.name,
        geopolitical_zone=state.geopolitical_zone,
        cohort_id=state.cohort_id,
        cohort_code=state.cohort.code if state.cohort else None,
        cohort_name=state.cohort.name if state.cohort else None,
        piu_contact_email=state.piu_contact_email,
        is_active=state.is_active,
    )


def _indicator_read(indicator: Indicator) -> IndicatorRead:
    return IndicatorRead(
        id=indicator.id,
        number=indicator.number,
        code=indicator.code,
        name=indicator.name,
        definition=indicator.definition,
        category_id=indicator.category_id,
        category_code=indicator.category.code if indicator.category else None,
        category_name=indicator.category.name if indicator.category else None,
        unit=indicator.unit,
        aggregation_method=indicator.aggregation_method,
        direction=indicator.direction,
        is_cumulative=indicator.is_cumulative,
        time_basis=indicator.time_basis,
        effective_time_basis=str(reconciliation.time_basis(indicator)),
        requires_numerator_denominator=indicator.requires_numerator_denominator,
        baseline_value=indicator.baseline_value,
        min_value=indicator.min_value,
        max_value=indicator.max_value,
        decimal_places=indicator.decimal_places,
        disaggregations=indicator.disaggregations or [],
        aliases=indicator.aliases or [],
        is_core=indicator.is_core,
        is_active=indicator.is_active,
    )


# --------------------------------------------------------------------------
# Cohorts and states
# --------------------------------------------------------------------------
@router.get("/cohorts", response_model=list[CohortRead], summary="List financing cohorts")
def list_cohorts(db: DbSession, principal: CurrentPrincipal) -> list[CohortRead]:
    counts = dict(
        db.execute(
            select(State.cohort_id, func.count(State.id))
            .where(State.is_active.is_(True))
            .group_by(State.cohort_id)
        ).all()
    )
    return [
        CohortRead(
            id=cohort.id,
            code=cohort.code,
            name=cohort.name,
            description=cohort.description,
            financing_window=cohort.financing_window,
            start_year=cohort.start_year,
            sort_order=cohort.sort_order,
            is_active=cohort.is_active,
            state_count=counts.get(cohort.id, 0),
        )
        for cohort in sorted(db.scalars(select(Cohort)), key=lambda c: c.sort_order)
    ]


@router.get("/states", response_model=list[StateRead], summary="List states and their cohorts")
def list_states(
    db: DbSession,
    principal: CurrentPrincipal,
    cohort: str | None = Query(default=None, description="Filter by cohort code"),
    zone: str | None = Query(default=None, description="Filter by geopolitical zone"),
    active_only: bool = True,
) -> list[StateRead]:
    stmt = select(State).order_by(State.name)
    if active_only:
        stmt = stmt.where(State.is_active.is_(True))
    if zone:
        stmt = stmt.where(func.lower(State.geopolitical_zone) == zone.lower())
    states = list(db.scalars(stmt))
    if cohort:
        wanted = cohort.strip().upper()
        states = [s for s in states if s.cohort and s.cohort.code.upper() == wanted]
    return [_state_read(state) for state in states]


@router.patch(
    "/states/{state_code}",
    response_model=StateRead,
    summary="Reassign a state's cohort or contact details",
)
def update_state(
    state_code: str,
    payload: StateUpdate,
    db: DbSession,
    principal=Depends(require(Permission.REFERENCE_MANAGE)),
) -> StateRead:
    state = reference.get_state_by_code(db, state_code)
    before = {
        "cohort": state.cohort.code if state.cohort else None,
        "zone": state.geopolitical_zone,
        "is_active": state.is_active,
    }

    if payload.cohort_code is not None:
        state.cohort_id = reference.get_cohort_by_code(db, str(payload.cohort_code)).id
    if payload.geopolitical_zone is not None:
        state.geopolitical_zone = payload.geopolitical_zone
    if payload.piu_contact_email is not None:
        state.piu_contact_email = payload.piu_contact_email
    if payload.is_active is not None:
        state.is_active = payload.is_active

    db.flush()
    audit.record(
        db,
        action="reference.state_update",
        entity_type="state",
        entity_id=state.id,
        actor=principal.user,
        state_id=state.id,
        summary=f"Updated reference data for {state.name}.",
        before=before,
        after={
            "cohort": state.cohort.code if state.cohort else None,
            "zone": state.geopolitical_zone,
            "is_active": state.is_active,
        },
    )
    db.commit()
    return _state_read(state)


# --------------------------------------------------------------------------
# Indicators
# --------------------------------------------------------------------------
@router.get(
    "/indicator-categories",
    response_model=list[IndicatorCategoryRead],
    summary="List results-framework categories",
)
def list_categories(db: DbSession, principal: CurrentPrincipal) -> list[IndicatorCategoryRead]:
    counts = dict(
        db.execute(
            select(Indicator.category_id, func.count(Indicator.id)).group_by(Indicator.category_id)
        ).all()
    )
    return [
        IndicatorCategoryRead(
            id=category.id,
            code=category.code,
            name=category.name,
            description=category.description,
            sort_order=category.sort_order,
            indicator_count=counts.get(category.id, 0),
        )
        for category in sorted(db.scalars(select(IndicatorCategory)), key=lambda c: c.sort_order)
    ]


@router.get("/indicators", response_model=list[IndicatorRead], summary="List the 70 AGILE KPIs")
def list_indicators(
    db: DbSession,
    principal: CurrentPrincipal,
    category: str | None = Query(default=None, description="Filter by category code"),
    search: str | None = Query(default=None, description="Match against code or name"),
    active_only: bool = True,
) -> list[IndicatorRead]:
    stmt = select(Indicator).order_by(Indicator.number)
    if active_only:
        stmt = stmt.where(Indicator.is_active.is_(True))
    if search:
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(
            func.lower(Indicator.name).like(pattern) | func.lower(Indicator.code).like(pattern)
        )
    indicators = list(db.scalars(stmt))
    if category:
        wanted = category.strip().upper()
        indicators = [
            i for i in indicators if i.category and i.category.code.upper() == wanted
        ]
    return [_indicator_read(indicator) for indicator in indicators]


@router.get(
    "/indicators/{indicator_code}",
    response_model=IndicatorRead,
    summary="Describe one indicator",
)
def get_indicator(indicator_code: str, db: DbSession, principal: CurrentPrincipal) -> IndicatorRead:
    return _indicator_read(reference.get_indicator_by_code(db, indicator_code))


@router.patch(
    "/indicators/{indicator_code}",
    response_model=IndicatorRead,
    summary="Retune an indicator's definition or validation bounds",
)
def update_indicator(
    indicator_code: str,
    payload: IndicatorUpdate,
    db: DbSession,
    principal=Depends(require(Permission.REFERENCE_MANAGE)),
) -> IndicatorRead:
    indicator = reference.get_indicator_by_code(db, indicator_code)
    before = _indicator_read(indicator).model_dump(mode="json")

    for attribute, value in payload.model_dump(exclude_unset=True).items():
        if attribute == "time_basis" and value in ("", None):
            # An empty string clears the override so the basis is derived again.
            indicator.time_basis = None
            continue
        setattr(indicator, attribute, str(value) if hasattr(value, "value") else value)
    db.flush()

    audit.record(
        db,
        action="reference.indicator_update",
        entity_type="indicator",
        entity_id=indicator.id,
        actor=principal.user,
        summary=f"Updated indicator {indicator.code}.",
        before=before,
        after=_indicator_read(indicator).model_dump(mode="json"),
    )
    db.commit()
    return _indicator_read(indicator)


# --------------------------------------------------------------------------
# Reporting periods
# --------------------------------------------------------------------------
def _period_read(db, period: ReportingPeriod) -> PeriodRead:
    return PeriodRead(
        **{
            field: getattr(period, field)
            for field in (
                "id", "code", "label", "period_type", "fiscal_year", "sequence",
                "start_date", "end_date", "due_date", "is_open", "locked_at", "lock_note",
            )
        },
        reopened_for=sorted(
            grant.state.code
            for grant in period_service.reopenings(
                db, period_id=period.id, active_only=True
            )
        ),
    )


def _reopening_read(db, grant: PeriodReopening) -> ReopeningRead:
    granted_by = db.get(User, grant.granted_by_id) if grant.granted_by_id else None
    return ReopeningRead(
        id=grant.id,
        period_id=grant.period_id,
        period_code=grant.period.code if grant.period else None,
        state_id=grant.state_id,
        state_code=grant.state.code if grant.state else None,
        state_name=grant.state.name if grant.state else None,
        reason=grant.reason,
        status=grant.status,
        granted_by=(granted_by.full_name or granted_by.email) if granted_by else None,
        granted_at=grant.granted_at,
        expires_on=grant.expires_on,
        consumed_at=grant.consumed_at,
        consumed_submission_id=grant.consumed_submission_id,
        revoked_at=grant.revoked_at,
        revoke_reason=grant.revoke_reason,
    )



@router.get("/periods", response_model=list[PeriodRead], summary="List reporting periods")
def list_periods(
    db: DbSession,
    principal: CurrentPrincipal,
    period_type: PeriodType | None = None,
    fiscal_year: int | None = None,
) -> list[PeriodRead]:
    periods = reference.ordered_periods(db, str(period_type) if period_type else None)
    if fiscal_year:
        periods = [p for p in periods if p.fiscal_year == fiscal_year]
    return [_period_read(db, period) for period in periods]


@router.get(
    "/periods/current",
    response_model=PeriodRead,
    summary="The most recently closed reporting period",
)
def current_period(
    db: DbSession, principal: CurrentPrincipal, period_type: PeriodType | None = None
) -> PeriodRead:
    period = reference.latest_period(db, str(period_type) if period_type else None)
    if period is None:
        raise NotFoundError("No reporting periods have been configured yet")
    return _period_read(db, period)


@router.post(
    "/periods",
    response_model=PeriodRead,
    status_code=201,
    summary="Create a single reporting period",
)
def create_period(
    payload: PeriodCreate,
    db: DbSession,
    principal=Depends(require(Permission.REFERENCE_MANAGE)),
) -> PeriodRead:
    period = reference.ensure_period(
        db, payload.period_type, payload.fiscal_year, payload.sequence, is_open=payload.is_open
    )
    if payload.due_date:
        period.due_date = payload.due_date
    audit.record(
        db,
        action="reference.period_create",
        entity_type="reporting_period",
        entity_id=period.id,
        actor=principal.user,
        summary=f"Created reporting period {period.code}.",
    )
    db.commit()
    return _period_read(db, period)


@router.post(
    "/periods/generate",
    response_model=list[PeriodRead],
    summary="Generate a full reporting calendar for a fiscal year",
)
def generate_periods(
    payload: PeriodGenerate,
    db: DbSession,
    principal=Depends(require(Permission.REFERENCE_MANAGE)),
) -> list[PeriodRead]:
    periods = reference.generate_year(
        db, payload.fiscal_year, payload.period_types, payload.due_days_after_close
    )
    audit.record(
        db,
        action="reference.period_generate",
        entity_type="reporting_period",
        entity_id=payload.fiscal_year,
        actor=principal.user,
        summary=f"Generated the {payload.fiscal_year} reporting calendar ({len(periods)} periods).",
    )
    db.commit()
    return [_period_read(db, period) for period in periods]


@router.post(
    "/periods/{period_code}/close",
    response_model=PeriodRead,
    summary="Close a period to further submissions",
)
def close_period(
    period_code: str,
    payload: PeriodCloseRequest,
    db: DbSession,
    principal=Depends(require(Permission.REFERENCE_MANAGE)),
) -> PeriodRead:
    """Close the cycle. Corrections still land through the query workflow."""
    period = reference.get_period_by_code(db, period_code)
    period_service.close_period(db, period, actor=principal.user, note=payload.note)
    db.commit()
    return _period_read(db, period)


@router.post(
    "/periods/{period_code}/reopen",
    response_model=PeriodRead,
    summary="Reopen a closed period to every state",
)
def reopen_period(
    period_code: str,
    payload: PeriodReopenRequest,
    db: DbSession,
    principal=Depends(require(Permission.REFERENCE_MANAGE)),
) -> PeriodRead:
    """Reopen the whole cycle.

    Blunt on purpose. Where one state needs to re-file, grant that state a
    reopening instead: reopening the period lets every other state change
    figures nobody asked about.
    """
    period = reference.get_period_by_code(db, period_code)
    period_service.reopen_period(db, period, reason=payload.reason, actor=principal.user)
    db.commit()
    return _period_read(db, period)


@router.get(
    "/periods/{period_code}/reopenings",
    response_model=list[ReopeningRead],
    summary="Reopenings granted for a closed period",
    dependencies=[Depends(require(Permission.DATA_READ))],
)
def list_reopenings(
    period_code: str,
    db: DbSession,
    principal: CurrentPrincipal,
    active_only: bool = False,
) -> list[ReopeningRead]:
    period = reference.get_period_by_code(db, period_code)
    state_id = principal.state_id if principal.is_state_scoped else None
    return [
        _reopening_read(db, grant)
        for grant in period_service.reopenings(
            db, period_id=period.id, state_id=state_id, active_only=active_only
        )
    ]


@router.post(
    "/periods/{period_code}/reopenings",
    response_model=ReopeningRead,
    status_code=201,
    summary="Let one state file one more return into a closed period",
)
def grant_reopening(
    period_code: str,
    payload: ReopeningCreate,
    db: DbSession,
    principal=Depends(require(Permission.REFERENCE_MANAGE)),
) -> ReopeningRead:
    period = reference.get_period_by_code(db, period_code)
    state = reference.get_state_by_code(db, payload.state_code)
    grant = period_service.grant_reopening(
        db,
        period,
        state,
        reason=payload.reason,
        actor=principal.user,
        days=payload.days,
    )
    db.commit()
    return _reopening_read(db, grant)


@router.post(
    "/reopenings/{reopening_id}/revoke",
    response_model=ReopeningRead,
    summary="Withdraw a reopening that has not been used",
)
def revoke_reopening(
    reopening_id: int,
    payload: ReopeningRevoke,
    db: DbSession,
    principal=Depends(require(Permission.REFERENCE_MANAGE)),
) -> ReopeningRead:
    grant = period_service.get_reopening(db, reopening_id)
    period_service.revoke_reopening(db, grant, reason=payload.reason, actor=principal.user)
    db.commit()
    return _reopening_read(db, grant)


# --------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------
@router.get("/targets", response_model=list[TargetRead], summary="List targets")
def list_targets(
    db: DbSession,
    principal: CurrentPrincipal,
    period: str | None = None,
    state: str | None = None,
    indicator: str | None = None,
    level: TargetLevel | None = None,
    limit: int = Query(default=500, le=20000),
) -> list[TargetRead]:
    stmt = select(Target)
    if period:
        stmt = stmt.where(Target.period_id == reference.get_period_by_code(db, period).id)
    if state:
        stmt = stmt.where(Target.state_id == reference.get_state_by_code(db, state).id)
    if indicator:
        stmt = stmt.where(
            Target.indicator_id == reference.get_indicator_by_code(db, indicator).id
        )
    if level:
        stmt = stmt.where(Target.level == str(level))

    return [
        TargetRead(
            id=row.id,
            indicator_id=row.indicator_id,
            indicator_code=row.indicator.code if row.indicator else None,
            period_id=row.period_id,
            period_code=row.period.code if row.period else None,
            level=row.level,
            state_id=row.state_id,
            state_code=row.state.code if row.state else None,
            target_value=row.target_value,
            source=row.source,
        )
        for row in db.scalars(stmt.limit(limit))
    ]


@router.post("/targets", response_model=Message, summary="Create or update targets in bulk")
def upsert_targets(
    payload: TargetBulkUpsert,
    db: DbSession,
    principal=Depends(require(Permission.REFERENCE_MANAGE)),
) -> Message:
    created = updated = 0
    for entry in payload.targets:
        indicator = reference.get_indicator_by_code(db, entry.indicator_code)
        period = reference.get_period_by_code(db, entry.period_code)
        state = (
            reference.get_state_by_code(db, entry.state_code)
            if entry.level == TargetLevel.STATE and entry.state_code
            else None
        )
        existing = db.scalar(
            select(Target).where(
                Target.indicator_id == indicator.id,
                Target.period_id == period.id,
                Target.level == str(entry.level),
                Target.state_id == (state.id if state else None),
            )
        )
        if existing is None:
            db.add(
                Target(
                    indicator_id=indicator.id,
                    period_id=period.id,
                    level=str(entry.level),
                    state_id=state.id if state else None,
                    target_value=entry.target_value,
                    source=entry.source,
                    notes=entry.notes,
                )
            )
            created += 1
        else:
            existing.target_value = entry.target_value
            existing.source = entry.source or existing.source
            updated += 1

    audit.record(
        db,
        action="reference.targets_upsert",
        entity_type="target",
        actor=principal.user,
        summary=f"Loaded targets: {created} created, {updated} updated.",
        after={"created": created, "updated": updated},
    )
    db.commit()
    return Message(
        message=f"{created} target(s) created and {updated} updated.",
        details={"created": created, "updated": updated},
    )
