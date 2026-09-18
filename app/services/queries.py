"""The data query workflow.

A validation finding becomes a query against the figure that caused it, owned
by the state that reported it. The state responds with evidence and either
confirms the figure or proposes a correction; the NPCU accepts or rejects.
Only an accepted correction changes the stored figure, and the original is
kept, so a published report and the live dashboard can always be reconciled.

Nothing here rejects a submission. A figure the rules cannot use is
*quarantined* -- excluded from aggregation but still on record and visible --
rather than taking the whole return down with it. A downward restatement backed
by evidence is a data improvement, not a failure.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import (
    OPEN_QUERY_STATUSES,
    QueryResolution,
    QueryStatus,
    Severity,
)
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.events import event_bus
from app.core.logging_config import get_logger
from app.models import (
    DataQuery,
    EvidenceFile,
    IndicatorValue,
    QueryResponse,
    Submission,
    User,
    ValidationIssue,
    ValueRevision,
)
from app.services import audit

logger = get_logger(__name__)

#: How long a state has to respond, from the day the query is raised.
DEFAULT_RESPONSE_DAYS = 14

#: Severities that raise a query. INFO findings are informational only.
QUERYABLE = {Severity.ERROR, Severity.WARNING}


# --------------------------------------------------------------------------
# Raising queries
# --------------------------------------------------------------------------
def raise_queries(
    db: Session,
    submission: Submission,
    *,
    actor: User | None = None,
    response_days: int = DEFAULT_RESPONSE_DAYS,
    severities: set[Severity] | None = None,
) -> list[DataQuery]:
    """Open a query for every queryable finding on ``submission``.

    Re-running validation does not duplicate queries: a finding that already
    has an open query against the same indicator and rule is left alone.
    """
    wanted = severities or QUERYABLE
    issues = list(
        db.scalars(select(ValidationIssue).where(ValidationIssue.submission_id == submission.id))
    )
    existing = {
        (query.rule_code, query.indicator_id)
        for query in db.scalars(
            select(DataQuery).where(
                DataQuery.state_id == submission.state_id,
                DataQuery.period_id == submission.period_id,
            )
        )
        if query.is_open
    }

    values = {
        value.indicator_id: value
        for value in db.scalars(
            select(IndicatorValue).where(IndicatorValue.submission_id == submission.id)
        )
    }

    opened: list[DataQuery] = []
    now = datetime.now(timezone.utc)
    for issue in issues:
        if Severity(issue.severity) not in wanted:
            continue
        if (issue.rule_code, issue.indicator_id) in existing:
            continue

        value = values.get(issue.indicator_id) if issue.indicator_id else None
        query = DataQuery(
            submission_id=submission.id,
            state_id=submission.state_id,
            period_id=submission.period_id,
            indicator_id=issue.indicator_id,
            rule_code=issue.rule_code,
            dimension=issue.dimension,
            severity=issue.severity,
            title=(issue.message or "")[:400],
            detail=(
                f"Raised by rule {issue.rule_code} ({issue.dimension}). "
                f"Observed: {issue.observed or '—'}. Expected: {issue.expected or '—'}."
            ),
            reported_value=value.effective_value if value else None,
            status=str(QueryStatus.OPEN),
            due_date=date.today() + timedelta(days=response_days),
            opened_by_id=getattr(actor, "id", None),
            opened_at=now,
            verification_required=False,
        )
        db.add(query)
        opened.append(query)
        existing.add((issue.rule_code, issue.indicator_id))

        # A figure the rules cannot use is quarantined rather than rejected:
        # kept on record and visible, but out of every aggregation until settled.
        if issue.is_blocking and value is not None:
            value.is_valid = False
            value.quarantine_reason = f"{issue.rule_code}: {(issue.message or '')[:180]}"

    db.flush()
    _refresh_counts(db, submission)

    if opened:
        audit.record(
            db,
            action="query.raise",
            entity_type="submission",
            entity_id=submission.id,
            actor=actor,
            state_id=submission.state_id,
            period_id=submission.period_id,
            summary=f"Opened {len(opened)} data quer(ies) against submission #{submission.id}.",
            after={"queries": len(opened), "quarantined": submission.quarantined_count},
        )
        event_bus.publish(
            "queries.raised",
            {"submission_id": submission.id, "count": len(opened)},
        )
    return opened


def _refresh_counts(db: Session, submission: Submission) -> None:
    open_queries = db.scalar(
        select(func.count(DataQuery.id)).where(
            DataQuery.submission_id == submission.id,
            DataQuery.status.in_([str(s) for s in OPEN_QUERY_STATUSES]),
        )
    )
    quarantined = db.scalar(
        select(func.count(IndicatorValue.id)).where(
            IndicatorValue.submission_id == submission.id,
            IndicatorValue.is_valid.is_(False),
        )
    )
    submission.open_query_count = int(open_queries or 0)
    submission.quarantined_count = int(quarantined or 0)
    db.flush()


# --------------------------------------------------------------------------
# Responding
# --------------------------------------------------------------------------
def respond(
    db: Session,
    query: DataQuery,
    *,
    narrative: str,
    proposed_value: float | None = None,
    evidence_summary: str | None = None,
    restates_period_code: str | None = None,
    actor: User | None = None,
) -> QueryResponse:
    """Record a state's response: evidence, and a figure confirmed or corrected.

    ``restates_period_code`` names the period the correction applies to, which
    is not always the period queried: a cumulative figure that appears to fall
    is usually put right by restating the earlier period.
    """
    if not query.is_open:
        raise ConflictError(
            f"Query #{query.id} is {query.status} and no longer accepts responses."
        )
    if not narrative.strip():
        raise ValidationError("A response must explain the figure and cite its evidence.")

    restates_period_id = query.period_id
    if restates_period_code:
        from app.services import reference

        restates_period_id = reference.get_period_by_code(db, restates_period_code).id

    response = QueryResponse(
        query_id=query.id,
        responder_id=getattr(actor, "id", None),
        submitted_at=datetime.now(timezone.utc),
        narrative=narrative.strip(),
        evidence_summary=evidence_summary,
        proposed_value=proposed_value,
        restates_period_id=restates_period_id,
    )
    db.add(response)
    query.status = str(QueryStatus.RESPONDED)
    db.flush()

    audit.record(
        db,
        action="query.respond",
        entity_type="data_query",
        entity_id=query.id,
        actor=actor,
        state_id=query.state_id,
        period_id=query.period_id,
        summary=(
            f"Response submitted for query #{query.id}"
            + (
                f", proposing {proposed_value:g} in place of {query.reported_value:g}."
                if proposed_value is not None and query.reported_value is not None
                else ", confirming the figure as reported."
            )
        ),
        after={"proposed_value": proposed_value, "status": query.status},
    )
    event_bus.publish("query.responded", {"query_id": query.id, "state_id": query.state_id})
    return response


def attach_evidence(
    db: Session,
    response: QueryResponse,
    *,
    filename: str,
    stored_path: str,
    content_hash: str,
    size_bytes: int,
    content_type: str | None = None,
    description: str | None = None,
    actor: User | None = None,
) -> EvidenceFile:
    evidence = EvidenceFile(
        response_id=response.id,
        filename=filename,
        stored_path=stored_path,
        content_hash=content_hash,
        size_bytes=size_bytes,
        content_type=content_type,
        description=description,
        uploaded_by_id=getattr(actor, "id", None),
    )
    db.add(evidence)
    db.flush()
    return evidence


# --------------------------------------------------------------------------
# Review
# --------------------------------------------------------------------------
def accept(
    db: Session,
    query: DataQuery,
    *,
    actor: User | None = None,
    note: str | None = None,
) -> DataQuery:
    """Accept the state's response, restating the figure if one was proposed."""
    response = _latest_response(db, query)
    if response is None:
        raise ConflictError(f"Query #{query.id} has no response to accept.")

    restated = False
    if response.proposed_value is not None:
        _apply_restatement(db, query, response, actor=actor, note=note)
        restated = True

    # Whatever period the correction landed on, the figure this query
    # quarantined is settled and belongs back in the aggregations.
    _release_quarantine(db, query)

    response.reviewed_by_id = getattr(actor, "id", None)
    response.reviewed_at = datetime.now(timezone.utc)
    response.review_outcome = "ACCEPTED"
    response.review_note = note

    query.status = str(QueryStatus.ACCEPTED)
    query.resolution = str(QueryResolution.RESTATED if restated else QueryResolution.CONFIRMED)
    query.resolution_note = note
    query.closed_by_id = getattr(actor, "id", None)
    query.closed_at = datetime.now(timezone.utc)
    db.flush()

    submission = db.get(Submission, query.submission_id) if query.submission_id else None
    if submission is not None:
        _refresh_counts(db, submission)

    audit.record(
        db,
        action="query.accept",
        entity_type="data_query",
        entity_id=query.id,
        actor=actor,
        state_id=query.state_id,
        period_id=query.period_id,
        summary=(
            f"Query #{query.id} accepted; figure "
            + ("restated." if restated else "confirmed as reported.")
        ),
        after={"resolution": query.resolution},
    )
    event_bus.publish("query.accepted", {"query_id": query.id, "restated": restated})
    return query


def reject(
    db: Session, query: DataQuery, *, reason: str, actor: User | None = None
) -> DataQuery:
    """Send the query back to the state for a better response."""
    response = _latest_response(db, query)
    if response is not None:
        response.reviewed_by_id = getattr(actor, "id", None)
        response.reviewed_at = datetime.now(timezone.utc)
        response.review_outcome = "REJECTED"
        response.review_note = reason

    query.status = str(QueryStatus.REJECTED)
    query.resolution_note = reason
    db.flush()

    audit.record(
        db,
        action="query.reject",
        entity_type="data_query",
        entity_id=query.id,
        actor=actor,
        state_id=query.state_id,
        period_id=query.period_id,
        summary=f"Query #{query.id} returned to {query.state.name}: {reason}",
    )
    event_bus.publish("query.rejected", {"query_id": query.id})
    return query


def withdraw(
    db: Session, query: DataQuery, *, reason: str, actor: User | None = None
) -> DataQuery:
    """Close a query raised in error, releasing whatever it held.

    A rule can be wrong, or retuned after the fact. Withdrawing says so on the
    record rather than leaving the state to answer for a finding nobody stands
    behind -- and it releases the figure, which is the part that matters: a
    quarantined figure sits out of every national total until something settles
    it, and "we should not have asked" settles it.
    """
    if not query.is_open:
        raise ConflictError(f"Query #{query.id} is already {query.status}.")

    _release_quarantine(db, query)
    query.status = str(QueryStatus.WITHDRAWN)
    query.resolution = str(QueryResolution.WITHDRAWN)
    query.resolution_note = reason
    query.closed_by_id = getattr(actor, "id", None)
    query.closed_at = datetime.now(timezone.utc)
    db.flush()

    submission = db.get(Submission, query.submission_id) if query.submission_id else None
    if submission is not None:
        _refresh_counts(db, submission)

    audit.record(
        db,
        action="query.withdraw",
        entity_type="data_query",
        entity_id=query.id,
        actor=actor,
        state_id=query.state_id,
        period_id=query.period_id,
        summary=f"Query #{query.id} withdrawn: {reason}",
        after={"status": query.status},
    )
    event_bus.publish("query.withdrawn", {"query_id": query.id})
    return query


def escalate_to_verification(
    db: Session, query: DataQuery, *, reason: str, actor: User | None = None
) -> DataQuery:
    """Park the query for physical checking on a supervision or DQA visit."""
    query.status = str(QueryStatus.VERIFICATION)
    query.verification_required = True
    query.resolution_note = reason
    db.flush()

    audit.record(
        db,
        action="query.escalate",
        entity_type="data_query",
        entity_id=query.id,
        actor=actor,
        state_id=query.state_id,
        period_id=query.period_id,
        summary=f"Query #{query.id} referred for physical verification: {reason}",
    )
    return query


def _release_quarantine(db: Session, query: DataQuery) -> None:
    """Return the figure this query quarantined to the aggregations.

    Only if no other open query still holds it out: two rules can flag the same
    figure, and settling one of them does not settle the other.
    """
    if query.submission_id is None or query.indicator_id is None:
        return

    value = db.scalar(
        select(IndicatorValue).where(
            IndicatorValue.submission_id == query.submission_id,
            IndicatorValue.indicator_id == query.indicator_id,
        )
    )
    if value is None or value.is_valid:
        return

    others = [
        row
        for row in db.scalars(
            select(DataQuery).where(
                DataQuery.submission_id == query.submission_id,
                DataQuery.indicator_id == query.indicator_id,
                DataQuery.id != query.id,
            )
        )
        if row.is_open
    ]
    if others:
        logger.info(
            "figure stays quarantined",
            extra={"query_id": query.id, "other_open_queries": [q.id for q in others]},
        )
        return

    value.is_valid = True
    value.quarantine_reason = None
    db.flush()


def _latest_response(db: Session, query: DataQuery) -> QueryResponse | None:
    return db.scalar(
        select(QueryResponse)
        .where(QueryResponse.query_id == query.id)
        .order_by(QueryResponse.id.desc())
        .limit(1)
    )


def _apply_restatement(
    db: Session,
    query: DataQuery,
    response: QueryResponse,
    *,
    actor: User | None,
    note: str | None,
) -> ValueRevision:
    """Change the stored figure, keeping what was first reported."""
    # The correction may land on an earlier period than the one queried.
    target_period_id = response.restates_period_id or query.period_id
    target_submission = db.scalar(
        select(Submission).where(
            Submission.state_id == query.state_id,
            Submission.period_id == target_period_id,
            Submission.is_current.is_(True),
        )
    )
    if target_submission is None:
        raise NotFoundError(
            f"No current submission for query #{query.id}'s target period."
        )

    value = db.scalar(
        select(IndicatorValue).where(
            IndicatorValue.submission_id == target_submission.id,
            IndicatorValue.indicator_id == query.indicator_id,
        )
    )
    if value is None:
        raise NotFoundError(
            f"No stored figure for query #{query.id}; the submission may have been replaced."
        )

    previous = value.value
    if value.original_value is None:
        value.original_value = previous

    value.value = response.proposed_value
    value.is_restated = True

    revision = ValueRevision(
        indicator_value_id=value.id,
        query_id=query.id,
        response_id=response.id,
        previous_value=previous,
        new_value=response.proposed_value,
        reason=(note or response.narrative)[:4000],
        proposed_by_id=response.responder_id,
        approved_by_id=getattr(actor, "id", None),
        approved_at=datetime.now(timezone.utc),
    )
    db.add(revision)
    db.flush()

    logger.info(
        "figure restated",
        extra={
            "query_id": query.id,
            "indicator_id": query.indicator_id,
            "period_id": target_period_id,
            "previous": previous,
            "restated": response.proposed_value,
        },
    )
    # A restatement changes history, so anything derived from it is stale.
    from app.services.analytics import clear_analysis_cache

    clear_analysis_cache(db)
    return revision


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------
def open_queries(
    db: Session,
    *,
    state_id: int | None = None,
    period_id: int | None = None,
    overdue_only: bool = False,
    verification_only: bool = False,
) -> list[DataQuery]:
    stmt = select(DataQuery).where(
        DataQuery.status.in_([str(s) for s in OPEN_QUERY_STATUSES])
    )
    if state_id:
        stmt = stmt.where(DataQuery.state_id == state_id)
    if period_id:
        stmt = stmt.where(DataQuery.period_id == period_id)
    if verification_only:
        stmt = stmt.where(DataQuery.verification_required.is_(True))

    rows = list(db.scalars(stmt.order_by(DataQuery.due_date, DataQuery.id)))
    if overdue_only:
        rows = [row for row in rows if row.is_overdue()]
    return rows


def verification_worklist(db: Session, state_id: int | None = None) -> list[DataQuery]:
    """Figures still unconfirmed, for checking on the next supervision visit."""
    return open_queries(db, state_id=state_id, verification_only=True)


def query_summary(
    db: Session, period_id: int, *, state_ids: list[int] | None = None
) -> dict:
    """Counts for the dashboard and for the report's open-query section.

    ``state_ids`` narrows the count to what the caller may see. A state PIU
    shown the national figure would read 106 open queries against its own 21
    and have no way to tell which were its to answer.
    """
    stmt = select(DataQuery).where(DataQuery.period_id == period_id)
    if state_ids is not None:
        stmt = stmt.where(DataQuery.state_id.in_(state_ids or [0]))
    rows = list(db.scalars(stmt))
    open_rows = [row for row in rows if row.is_open]
    return {
        "total": len(rows),
        "open": len(open_rows),
        "overdue": sum(1 for row in open_rows if row.is_overdue()),
        "awaiting_review": sum(
            1 for row in rows if row.status == QueryStatus.RESPONDED
        ),
        "for_verification": sum(1 for row in open_rows if row.verification_required),
        "resolved": sum(1 for row in rows if not row.is_open),
        "restated": sum(1 for row in rows if row.resolution == QueryResolution.RESTATED),
        "states_with_open_queries": len({row.state_id for row in open_rows}),
    }
