"""Data quality endpoints: scorecards, national summary and rule administration."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.api.deps import (
    CurrentPrincipal,
    DbSession,
    enforce_state_scope,
    require,
    visible_state_codes,
)
from app.core.enums import Permission
from app.core.errors import NotFoundError
from app.models import ValidationRule
from app.schemas.validation import (
    DQAScorecard,
    NationalDQASummary,
    ValidationRuleRead,
    ValidationRuleUpdate,
)
from app.services import audit, dqa, reference

router = APIRouter(prefix="/quality", tags=["Data quality"])


def _resolve_period(db, period_code: str | None):
    if period_code:
        return reference.get_period_by_code(db, period_code)
    period = reference.latest_period(db)
    if period is None:
        raise NotFoundError("No reporting periods have been configured yet")
    return period


@router.get(
    "/scorecards/{state_code}",
    response_model=DQAScorecard,
    summary="State-level DQA scorecard",
    dependencies=[Depends(require(Permission.DATA_READ))],
)
def state_scorecard(
    state_code: str,
    db: DbSession,
    principal: CurrentPrincipal,
    period: str | None = None,
) -> DQAScorecard:
    enforce_state_scope(db, principal, state_code)
    state = reference.get_state_by_code(db, state_code)
    return dqa.state_scorecard(db, state, _resolve_period(db, period))


@router.get(
    "/national",
    response_model=NationalDQASummary,
    summary="Consolidated national DQA summary",
    dependencies=[Depends(require(Permission.DATA_READ))],
)
def national(
    db: DbSession,
    principal: CurrentPrincipal,
    period: str | None = None,
    state: str | None = None,
    cohort: str | None = None,
) -> NationalDQASummary:
    if state:
        enforce_state_scope(db, principal, state)
    elif principal.is_state_scoped:
        own = visible_state_codes(db, principal)
        state = own[0] if own else None
    return dqa.national_summary(
        db, _resolve_period(db, period), state_code=state, cohort_code=cohort
    )


@router.get(
    "/rules",
    response_model=list[ValidationRuleRead],
    summary="List the validation rule catalogue",
    dependencies=[Depends(require(Permission.DATA_READ))],
)
def list_rules(
    db: DbSession,
    principal: CurrentPrincipal,
    dimension: str | None = Query(default=None, description="Filter by DQA dimension"),
) -> list[ValidationRuleRead]:
    stmt = select(ValidationRule).order_by(ValidationRule.code)
    if dimension:
        stmt = stmt.where(ValidationRule.dimension == dimension.upper())
    return [ValidationRuleRead.model_validate(row) for row in db.scalars(stmt)]


@router.patch(
    "/rules/{rule_code}",
    response_model=ValidationRuleRead,
    summary="Retune or disable a validation rule",
)
def update_rule(
    rule_code: str,
    payload: ValidationRuleUpdate,
    db: DbSession,
    principal=Depends(require(Permission.REFERENCE_MANAGE)),
) -> ValidationRuleRead:
    rule = db.scalar(select(ValidationRule).where(ValidationRule.code == rule_code.upper()))
    if rule is None:
        raise NotFoundError(f"Unknown validation rule '{rule_code}'")

    before = ValidationRuleRead.model_validate(rule).model_dump(mode="json")
    for attribute, value in payload.model_dump(exclude_unset=True).items():
        setattr(rule, attribute, value)
    db.flush()

    audit.record(
        db,
        action="quality.rule_update",
        entity_type="validation_rule",
        entity_id=rule.id,
        actor=principal.user,
        summary=f"Updated validation rule {rule.code}.",
        before=before,
        after=ValidationRuleRead.model_validate(rule).model_dump(mode="json"),
    )
    db.commit()
    return ValidationRuleRead.model_validate(rule)
