"""Shared FastAPI dependencies: authentication, RBAC and request scoping."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Cookie, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import ROLE_PERMISSIONS, STATE_SCOPED_ROLES, Permission, Role
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.logging_config import actor_ctx
from app.core.security import decode_access_token, hash_api_key
from app.db.base import ensure_utc
from app.db.session import get_db
from app.models import ApiKey, State, User

DbSession = Annotated[Session, Depends(get_db)]


@dataclass
class Principal:
    """Whoever is making the request: a signed-in user or an API key."""

    id: int | None
    email: str
    full_name: str
    role: Role
    state_id: int | None = None
    is_api_key: bool = False
    user: User | None = None

    @property
    def permissions(self) -> set[Permission]:
        return ROLE_PERMISSIONS.get(self.role, set())

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    @property
    def is_state_scoped(self) -> bool:
        return self.role in STATE_SCOPED_ROLES and self.state_id is not None


def _principal_from_user(user: User) -> Principal:
    return Principal(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=Role(user.role),
        state_id=user.state_id,
        user=user,
    )


def _principal_from_api_key(key: ApiKey) -> Principal:
    return Principal(
        id=None,
        email=f"api-key:{key.prefix}",
        full_name=key.name,
        role=Role(key.role),
        state_id=key.state_id,
        is_api_key=True,
    )


#: Name of the cookie the dashboard uses. EventSource cannot set headers, so the
#: browser session also travels as an HttpOnly cookie.
SESSION_COOKIE = "agile_session"


def get_current_principal(
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
    agile_session: Annotated[str | None, Cookie()] = None,
) -> Principal:
    """Resolve the caller from a bearer token, an API key or the session cookie."""
    if x_api_key:
        key = db.scalar(
            select(ApiKey).where(
                ApiKey.key_hash == hash_api_key(x_api_key), ApiKey.is_active.is_(True)
            )
        )
        if key is None:
            raise AuthenticationError("Invalid API key")
        expires_at = ensure_utc(key.expires_at)
        if expires_at is not None and expires_at < datetime.now(timezone.utc):
            raise AuthenticationError("API key has expired")
        key.last_used_at = datetime.now(timezone.utc)
        db.flush()
        principal = _principal_from_api_key(key)
        actor_ctx.set(principal.email)
        return principal

    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthenticationError("Authorization header must use the Bearer scheme")
    elif agile_session:
        token = agile_session
    else:
        raise AuthenticationError(
            "Authentication required. Send an Authorization: Bearer token, an X-API-Key "
            "header, or sign in to the dashboard."
        )

    payload = decode_access_token(token)
    user = db.get(User, int(payload.get("sub", 0)))
    if user is None or not user.is_active:
        raise AuthenticationError("Account not found or deactivated")

    principal = _principal_from_user(user)
    actor_ctx.set(principal.email)
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


def require(*permissions: Permission) -> Callable[[Principal], Principal]:
    """Dependency factory enforcing that the caller holds every permission."""

    def dependency(principal: CurrentPrincipal) -> Principal:
        missing = [p for p in permissions if not principal.has(p)]
        if missing:
            raise PermissionDeniedError(
                f"Role {principal.role} lacks the required permission(s): "
                + ", ".join(str(p) for p in missing)
            )
        return principal

    return dependency


def enforce_state_scope(db: Session, principal: Principal, state_code: str | None) -> None:
    """Stop a state-scoped caller from touching another state's data."""
    if not principal.is_state_scoped:
        return
    own = db.get(State, principal.state_id)
    if state_code is None:
        raise PermissionDeniedError(
            f"{principal.role} accounts must specify their own state "
            f"({own.code if own else 'unassigned'})."
        )
    if own is None or state_code.strip().upper() not in {own.code.upper(), own.name.upper()}:
        raise PermissionDeniedError(
            f"{principal.role} accounts may only access data for "
            f"{own.name if own else 'their assigned state'}."
        )


def visible_state_codes(db: Session, principal: Principal) -> list[str] | None:
    """State codes the caller may see; ``None`` means unrestricted."""
    if not principal.is_state_scoped:
        return None
    state = db.get(State, principal.state_id)
    return [state.code] if state else []


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None
