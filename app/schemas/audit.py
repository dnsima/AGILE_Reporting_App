"""Audit trail schemas."""

from __future__ import annotations

from datetime import datetime

from app.schemas.common import ORMModel


class AuditLogRead(ORMModel):
    id: int
    action: str
    entity_type: str
    entity_id: str | None = None
    actor_id: int | None = None
    actor_email: str | None = None
    actor_role: str | None = None
    state_id: int | None = None
    period_id: int | None = None
    summary: str | None = None
    before_state: dict | None = None
    after_state: dict | None = None
    request_id: str | None = None
    ip_address: str | None = None
    status_code: int | None = None
    created_at: datetime
