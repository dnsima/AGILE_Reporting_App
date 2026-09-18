"""Closing a reporting cycle, and what it takes to write into a closed one.

A period closes when the NPCU has consolidated it and published from it. After
that the figures are what was published, and a state must not be able to change
them by uploading a new file: that is precisely the silent overwrite the change
process exists to prevent.

Closing does not freeze the data, though. Two routes still reach a closed
period, and the difference between them is the whole design:

* **Correcting a figure** goes through the query workflow, unchanged. The state
  proposes a correction with evidence, the NPCU accepts it, the original is
  kept and the restatement is reported. A closed period needs no special
  permission for this, because the permission is the acceptance.
* **Re-filing a return** -- the wrong file was uploaded, or a return was never
  filed before the cycle closed -- is what a correction cannot reach, and it
  needs a reopening: granted to one named state, for a stated reason, good for
  one submission, and expiring.

Reopening the whole period so that one state can re-file would let every other
state change figures nobody asked about. That is why the grant is per state.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.events import event_bus
from app.core.logging_config import get_logger
from app.models import PeriodReopening, ReportingPeriod, State, Submission, User
from app.services import audit

logger = get_logger(__name__)

#: How long a reopening stands before it lapses unused.
DEFAULT_GRANT_DAYS = 14


# --------------------------------------------------------------------------
# Closing and reopening
# --------------------------------------------------------------------------
def close_period(
    db: Session,
    period: ReportingPeriod,
    *,
    actor: User | None = None,
    note: str | None = None,
) -> ReportingPeriod:
    """Close the cycle to new submissions, recording who and why."""
    if not period.is_open:
        raise ConflictError(f"{period.code} is already closed.")

    period.is_open = False
    period.locked_at = datetime.now(timezone.utc)
    period.locked_by_id = getattr(actor, "id", None)
    period.lock_note = note
    db.flush()

    audit.record(
        db,
        action="period.close",
        entity_type="reporting_period",
        entity_id=period.id,
        actor=actor,
        period_id=period.id,
        summary=f"Closed {period.code} to new submissions." + (f" {note}" if note else ""),
        after={"is_open": False},
    )
    event_bus.publish("period.closed", {"period": period.code})
    return period


def reopen_period(
    db: Session, period: ReportingPeriod, *, reason: str, actor: User | None = None
) -> ReportingPeriod:
    """Reopen the cycle to every state.

    Deliberately blunt and deliberately loud. Where one state needs to re-file,
    :func:`grant_reopening` is the proportionate act; this one is for a cycle
    closed in error or genuinely resumed.
    """
    if period.is_open:
        raise ConflictError(f"{period.code} is already open.")
    if not (reason or "").strip():
        raise ValidationError("Say why the period is being reopened.")

    period.is_open = True
    period.locked_at = None
    period.locked_by_id = None
    period.lock_note = None
    db.flush()

    audit.record(
        db,
        action="period.reopen",
        entity_type="reporting_period",
        entity_id=period.id,
        actor=actor,
        period_id=period.id,
        summary=f"Reopened {period.code} to all states: {reason}",
        after={"is_open": True},
    )
    event_bus.publish("period.reopened", {"period": period.code})
    return period


# --------------------------------------------------------------------------
# Per-state reopenings
# --------------------------------------------------------------------------
def grant_reopening(
    db: Session,
    period: ReportingPeriod,
    state: State,
    *,
    reason: str,
    actor: User | None = None,
    days: int = DEFAULT_GRANT_DAYS,
    expires_on: date | None = None,
) -> PeriodReopening:
    """Let one state file one more return into a closed period."""
    if period.is_open:
        raise ConflictError(
            f"{period.code} is open, so {state.name} can submit without a reopening."
        )
    if not (reason or "").strip():
        raise ValidationError("Say why this state is being allowed to submit again.")

    existing = active_reopening(db, period, state)
    if existing is not None:
        raise ConflictError(
            f"{state.name} already holds an unused reopening for {period.code} "
            f"(granted {existing.granted_at:%Y-%m-%d}, expires {existing.expires_on})."
        )

    grant = PeriodReopening(
        period_id=period.id,
        state_id=state.id,
        reason=reason.strip(),
        granted_by_id=getattr(actor, "id", None),
        granted_at=datetime.now(timezone.utc),
        expires_on=expires_on or (date.today() + timedelta(days=days)),
    )
    db.add(grant)
    db.flush()

    audit.record(
        db,
        action="period.reopening_grant",
        entity_type="reporting_period",
        entity_id=period.id,
        actor=actor,
        state_id=state.id,
        period_id=period.id,
        summary=(
            f"{state.name} may file one more return for {period.code} until "
            f"{grant.expires_on}: {grant.reason}"
        ),
        after={"reopening_id": grant.id, "expires_on": str(grant.expires_on)},
    )
    event_bus.publish(
        "period.reopening_granted", {"period": period.code, "state": state.code}
    )
    return grant


def revoke_reopening(
    db: Session, grant: PeriodReopening, *, reason: str, actor: User | None = None
) -> PeriodReopening:
    """Withdraw a reopening that has not been used."""
    if grant.consumed_at is not None:
        raise ConflictError(
            f"Reopening #{grant.id} has already been used and cannot be withdrawn."
        )
    if grant.revoked_at is not None:
        raise ConflictError(f"Reopening #{grant.id} was already withdrawn.")

    grant.revoked_at = datetime.now(timezone.utc)
    grant.revoked_by_id = getattr(actor, "id", None)
    grant.revoke_reason = reason
    db.flush()

    audit.record(
        db,
        action="period.reopening_revoke",
        entity_type="reporting_period",
        entity_id=grant.period_id,
        actor=actor,
        state_id=grant.state_id,
        period_id=grant.period_id,
        summary=f"Withdrew {grant.state.name}'s reopening for {grant.period.code}: {reason}",
    )
    return grant


def active_reopening(
    db: Session, period: ReportingPeriod, state: State
) -> PeriodReopening | None:
    """The unused, unexpired reopening this state holds, if any."""
    rows = db.scalars(
        select(PeriodReopening).where(
            PeriodReopening.period_id == period.id,
            PeriodReopening.state_id == state.id,
        )
    )
    return next((row for row in rows if row.is_active()), None)


def reopenings(
    db: Session,
    *,
    period_id: int | None = None,
    state_id: int | None = None,
    active_only: bool = False,
) -> list[PeriodReopening]:
    stmt = select(PeriodReopening)
    if period_id is not None:
        stmt = stmt.where(PeriodReopening.period_id == period_id)
    if state_id is not None:
        stmt = stmt.where(PeriodReopening.state_id == state_id)
    rows = list(db.scalars(stmt.order_by(PeriodReopening.id.desc())))
    return [row for row in rows if row.is_active()] if active_only else rows


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------
def assert_can_submit(
    db: Session, state: State, period: ReportingPeriod
) -> PeriodReopening | None:
    """Raise unless this state may file a return for this period.

    Returns the reopening that permits it, so the caller can mark it used once
    the submission actually lands. An open period returns ``None``.
    """
    if period.is_open:
        return None

    grant = active_reopening(db, period, state)
    if grant is not None:
        return grant

    lapsed = [
        row
        for row in reopenings(db, period_id=period.id, state_id=state.id)
        if not row.is_active()
    ]
    detail = ""
    if lapsed:
        last = lapsed[0]
        detail = (
            f" {state.name}'s last reopening for this period was "
            f"{last.status.lower()}."
        )
    raise ConflictError(
        f"{period.code} is closed, so it takes no new submission from {state.name}."
        f"{detail} To correct a figure, respond to its query with evidence -- that works "
        f"on a closed period and keeps the original on record. To re-file the return "
        f"itself, ask the NPCU to grant a reopening for {state.name}."
    )


def consume_reopening(
    db: Session, grant: PeriodReopening, submission: Submission
) -> PeriodReopening:
    """Mark the grant used by the return that landed on it."""
    grant.consumed_at = datetime.now(timezone.utc)
    grant.consumed_submission_id = submission.id
    db.flush()
    logger.info(
        "reopening used",
        extra={
            "reopening_id": grant.id,
            "period": grant.period.code,
            "state": grant.state.code,
            "submission_id": submission.id,
        },
    )
    return grant


def get_reopening(db: Session, reopening_id: int) -> PeriodReopening:
    grant = db.get(PeriodReopening, reopening_id)
    if grant is None:
        raise NotFoundError(f"Reopening #{reopening_id} does not exist")
    return grant
