"""Audit trail writer.

Every ingestion, approval, rejection, reference edit and administrative action
lands here. Rows are append-only: the API exposes reads and no update/delete.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging_config import get_logger, request_id_ctx
from app.models import AuditLog, User

logger = get_logger(__name__)


def record(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: str | int | None = None,
    actor: User | None = None,
    state_id: int | None = None,
    period_id: int | None = None,
    summary: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    status_code: int | None = None,
) -> AuditLog:
    entry = AuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        actor_id=getattr(actor, "id", None),
        actor_email=getattr(actor, "email", None),
        actor_role=getattr(actor, "role", None),
        state_id=state_id,
        period_id=period_id,
        summary=summary,
        before_state=before,
        after_state=after,
        request_id=request_id_ctx.get(),
        ip_address=ip_address,
        user_agent=(user_agent or "")[:512] or None,
        status_code=status_code,
    )
    db.add(entry)
    db.flush()
    logger.info(
        "audit",
        extra={
            "audit_action": action,
            "entity_type": entity_type,
            "entity_id": str(entity_id) if entity_id is not None else None,
            "actor": getattr(actor, "email", None),
        },
    )
    return entry


def history(
    db: Session,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    actor_email: str | None = None,
    state_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[AuditLog], int]:
    stmt = select(AuditLog)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(AuditLog.entity_id == str(entity_id))
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if actor_email:
        stmt = stmt.where(AuditLog.actor_email == actor_email)
    if state_id:
        stmt = stmt.where(AuditLog.state_id == state_id)

    total = len(list(db.scalars(stmt)))
    rows = list(
        db.scalars(stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit).offset(offset))
    )
    return rows, total
