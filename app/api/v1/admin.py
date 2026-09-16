"""User administration, API keys and the audit trail."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from app.api.deps import DbSession, client_ip, require
from app.api.v1.auth import user_read
from app.core.enums import Permission, Role
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import generate_api_key, hash_password
from app.models import ApiKey, User
from app.schemas.audit import AuditLogRead
from app.schemas.auth import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyRead,
    UserCreate,
    UserRead,
    UserUpdate,
)
from app.schemas.common import Message
from app.services import audit, reference

router = APIRouter(prefix="/admin", tags=["Administration"])


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
@router.get(
    "/users",
    response_model=list[UserRead],
    summary="List platform users",
    dependencies=[Depends(require(Permission.USERS_MANAGE))],
)
def list_users(
    db: DbSession,
    role: Role | None = None,
    state: str | None = None,
    active_only: bool = False,
    limit: int = Query(default=200, le=1000),
) -> list[UserRead]:
    stmt = select(User).order_by(User.full_name)
    if role:
        stmt = stmt.where(User.role == str(role))
    if state:
        stmt = stmt.where(User.state_id == reference.get_state_by_code(db, state).id)
    if active_only:
        stmt = stmt.where(User.is_active.is_(True))
    return [user_read(user) for user in db.scalars(stmt.limit(limit))]


@router.post(
    "/users",
    response_model=UserRead,
    status_code=201,
    summary="Create a user account",
)
def create_user(
    payload: UserCreate,
    request: Request,
    db: DbSession,
    principal=Depends(require(Permission.USERS_MANAGE)),
) -> UserRead:
    existing = db.scalar(select(User).where(func.lower(User.email) == payload.email.lower()))
    if existing is not None:
        raise ConflictError(f"An account already exists for {payload.email}")

    state = reference.get_state_by_code(db, payload.state_code) if payload.state_code else None
    if payload.role == Role.STATE_PIU and state is None:
        raise ValidationError("State PIU accounts must be assigned to a state")

    user = User(
        email=payload.email.lower(),
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role=str(payload.role),
        state_id=state.id if state else None,
        phone=payload.phone,
    )
    db.add(user)
    db.flush()

    audit.record(
        db,
        action="admin.user_create",
        entity_type="user",
        entity_id=user.id,
        actor=principal.user,
        state_id=user.state_id,
        summary=f"Created {user.role} account for {user.email}.",
        after={"email": user.email, "role": user.role, "state_id": user.state_id},
        ip_address=client_ip(request),
    )
    db.commit()
    return user_read(user)


@router.patch(
    "/users/{user_id}",
    response_model=UserRead,
    summary="Update a user's role, state or status",
)
def update_user(
    user_id: int,
    payload: UserUpdate,
    db: DbSession,
    principal=Depends(require(Permission.USERS_MANAGE)),
) -> UserRead:
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError(f"User #{user_id} does not exist")

    before = {"role": user.role, "state_id": user.state_id, "is_active": user.is_active}
    data = payload.model_dump(exclude_unset=True)

    if "password" in data and data["password"]:
        user.hashed_password = hash_password(data.pop("password"))
    if "state_code" in data:
        state_code = data.pop("state_code")
        user.state_id = reference.get_state_by_code(db, state_code).id if state_code else None
    if "role" in data and data["role"]:
        user.role = str(data.pop("role"))
    for attribute, value in data.items():
        if value is not None:
            setattr(user, attribute, value)

    if user.role == Role.STATE_PIU and user.state_id is None:
        raise ValidationError("State PIU accounts must be assigned to a state")

    db.flush()
    audit.record(
        db,
        action="admin.user_update",
        entity_type="user",
        entity_id=user.id,
        actor=principal.user,
        state_id=user.state_id,
        summary=f"Updated account {user.email}.",
        before=before,
        after={"role": user.role, "state_id": user.state_id, "is_active": user.is_active},
    )
    db.commit()
    return user_read(user)


@router.post(
    "/users/{user_id}/deactivate",
    response_model=Message,
    summary="Deactivate a user account",
)
def deactivate_user(
    user_id: int,
    db: DbSession,
    principal=Depends(require(Permission.USERS_MANAGE)),
) -> Message:
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError(f"User #{user_id} does not exist")
    if principal.id == user.id:
        raise ValidationError("You cannot deactivate your own account")

    user.is_active = False
    audit.record(
        db,
        action="admin.user_deactivate",
        entity_type="user",
        entity_id=user.id,
        actor=principal.user,
        summary=f"Deactivated {user.email}.",
    )
    db.commit()
    return Message(message=f"{user.email} deactivated.")


# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------
@router.get(
    "/api-keys",
    response_model=list[ApiKeyRead],
    summary="List API keys issued to external dashboards",
    dependencies=[Depends(require(Permission.APIKEYS_MANAGE))],
)
def list_api_keys(db: DbSession) -> list[ApiKeyRead]:
    return [
        ApiKeyRead.model_validate(row)
        for row in db.scalars(select(ApiKey).order_by(ApiKey.id.desc()))
    ]


@router.post(
    "/api-keys",
    response_model=ApiKeyCreated,
    status_code=201,
    summary="Issue an API key for an external integration",
)
def create_api_key(
    payload: ApiKeyCreate,
    db: DbSession,
    principal=Depends(require(Permission.APIKEYS_MANAGE)),
) -> ApiKeyCreated:
    if payload.role in {Role.ADMIN, Role.NPCU}:
        raise ValidationError(
            "API keys may not carry ADMIN or NPCU privileges. Use VIEWER, ME_OFFICER or STATE_PIU."
        )

    state = reference.get_state_by_code(db, payload.state_code) if payload.state_code else None
    full_key, prefix, key_hash = generate_api_key()
    key = ApiKey(
        name=payload.name,
        prefix=prefix,
        key_hash=key_hash,
        role=str(payload.role),
        state_id=state.id if state else None,
        created_by_id=principal.id,
        expires_at=(
            datetime.now(timezone.utc) + timedelta(days=payload.expires_in_days)
            if payload.expires_in_days
            else None
        ),
    )
    db.add(key)
    db.flush()

    audit.record(
        db,
        action="admin.api_key_create",
        entity_type="api_key",
        entity_id=key.id,
        actor=principal.user,
        summary=f"Issued API key '{key.name}' with role {key.role}.",
        after={"prefix": key.prefix, "role": key.role, "state_id": key.state_id},
    )
    db.commit()

    return ApiKeyCreated(
        **ApiKeyRead.model_validate(key).model_dump(),
        api_key=full_key,
    )


@router.delete(
    "/api-keys/{key_id}",
    response_model=Message,
    summary="Revoke an API key",
)
def revoke_api_key(
    key_id: int,
    db: DbSession,
    principal=Depends(require(Permission.APIKEYS_MANAGE)),
) -> Message:
    key = db.get(ApiKey, key_id)
    if key is None:
        raise NotFoundError(f"API key #{key_id} does not exist")

    key.is_active = False
    audit.record(
        db,
        action="admin.api_key_revoke",
        entity_type="api_key",
        entity_id=key.id,
        actor=principal.user,
        summary=f"Revoked API key '{key.name}'.",
    )
    db.commit()
    return Message(message=f"API key '{key.name}' revoked.")


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------
@router.get(
    "/audit",
    response_model=list[AuditLogRead],
    summary="Read the audit trail",
    dependencies=[Depends(require(Permission.AUDIT_READ))],
)
def read_audit(
    db: DbSession,
    entity_type: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    actor_email: str | None = None,
    state: str | None = None,
    limit: int = Query(default=100, le=1000),
    offset: int = 0,
) -> list[AuditLogRead]:
    state_id = reference.get_state_by_code(db, state).id if state else None
    rows, _ = audit.history(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor_email=actor_email,
        state_id=state_id,
        limit=limit,
        offset=offset,
    )
    return [AuditLogRead.model_validate(row) for row in rows]
