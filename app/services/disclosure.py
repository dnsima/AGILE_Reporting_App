"""What the platform says about a figure, as distinct from what it does to it.

The platform never removes a reported figure from a total. A figure that fails
a blocking rule is counted and labelled, and the label is carried everywhere
the figure is shown. Three reasons, in the order they matter:

* The national result stops matching what states reported. On Q2 2026 the old
  behaviour cut life-skills completion from 373,529 to 82,820, social safety
  net from 821,726 to 717,150 and life-skills participation from 1,051,564 to
  860,155 -- in each case exactly the figures the NPCU's own technical report
  published, silently contradicted on screen.
* It hides the damage. A wrong figure removed from a total leaves the total
  looking healthier than the data behind it, which is the opposite of what a
  data-quality system is for.
* Changing a figure is a decision, and decisions belong to the change-
  management process. A validation rule may raise a finding; only a query
  resolution, with an NPCU officer behind it, may change or clear one.

``is_valid`` therefore means "fit for use", never "counted". Keeping it in step
with :class:`DisclosureStatus` is this module's job, so no caller has to
remember to set both.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import DisclosureStatus
from app.models import IndicatorValue, Submission


def set_status(
    value: IndicatorValue,
    status: DisclosureStatus,
    *,
    reason: str | None = None,
) -> None:
    """Label a figure, keeping ``is_valid`` in step. Never changes the value."""
    value.disclosure_status = str(status)
    value.is_valid = status is not DisclosureStatus.UNFIT
    if reason is not None:
        value.quarantine_reason = reason[:255]
    elif status in (DisclosureStatus.CLEAN, DisclosureStatus.CORRECTED):
        value.quarantine_reason = None


def mark_unfit(value: IndicatorValue, reason: str) -> None:
    """Flag a figure as unfit for use. It still counts towards every total."""
    set_status(value, DisclosureStatus.UNFIT, reason=reason)


def mark_queried(value: IndicatorValue, reason: str) -> None:
    """Flag a figure as under query without calling it unusable."""
    if value.disclosure_status == str(DisclosureStatus.UNFIT):
        return  # never soften a blocking finding
    set_status(value, DisclosureStatus.QUERIED, reason=reason)


def clear(value: IndicatorValue, *, corrected: bool) -> None:
    """Release a figure, once a query resolution has decided its fate."""
    set_status(
        value,
        DisclosureStatus.CORRECTED if corrected else DisclosureStatus.CLEAN,
    )


def status_of(value: IndicatorValue) -> DisclosureStatus:
    """The figure's label, falling back to ``is_valid`` for pre-existing rows."""
    raw = value.disclosure_status
    if raw:
        try:
            return DisclosureStatus(raw)
        except ValueError:
            pass
    return DisclosureStatus.CLEAN if value.is_valid else DisclosureStatus.UNFIT


def backfill(db: Session, submission: Submission) -> int:
    """Give every figure in a submission a status, for rows written earlier."""
    changed = 0
    for value in db.scalars(
        select(IndicatorValue).where(IndicatorValue.submission_id == submission.id)
    ):
        if not value.disclosure_status:
            set_status(value, status_of(value))
            changed += 1
    return changed


def unfit_count(db: Session, submission_id: int) -> int:
    """How many figures in a submission are marked unfit for use."""
    return int(
        db.scalar(
            select(func.count(IndicatorValue.id)).where(
                IndicatorValue.submission_id == submission_id,
                IndicatorValue.is_valid.is_(False),
            )
        )
        or 0
    )
