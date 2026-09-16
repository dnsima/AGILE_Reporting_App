"""Authentication and account self-service."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response
from sqlalchemy import func, select

from app.api.deps import SESSION_COOKIE, CurrentPrincipal, DbSession, client_ip
from app.core.config import settings
from app.core.enums import ROLE_PERMISSIONS, Role
from app.core.errors import AuthenticationError, ValidationError
from app.core.security import create_access_token, hash_password, verify_password
from app.models import State, User
from app.schemas.auth import LoginRequest, PasswordChange, TokenResponse, UserRead
from app.schemas.common import Message
from app.services import audit

router = APIRouter(prefix="/auth", tags=["Authentication"])


def user_read(user: User) -> UserRead:
    return UserRead(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        state_id=user.state_id,
        state_code=user.state.code if user.state else None,
        state_name=user.state.name if user.state else None,
        phone=user.phone,
        is_active=user.is_active,
        last_login_at=user.last_login_at,
        permissions=sorted(str(p) for p in ROLE_PERMISSIONS.get(Role(user.role), set())),
    )


@router.post("/login", response_model=TokenResponse, summary="Exchange credentials for a token")
def login(
    payload: LoginRequest, request: Request, response: Response, db: DbSession
) -> TokenResponse:
    user = db.scalar(select(User).where(func.lower(User.email) == payload.email.lower()))
    if user is None or not verify_password(payload.password, user.hashed_password):
        # Same message either way so the endpoint cannot be used to enumerate accounts.
        raise AuthenticationError("Incorrect email or password")
    if not user.is_active:
        raise AuthenticationError("This account has been deactivated")

    user.last_login_at = datetime.now(timezone.utc)
    audit.record(
        db,
        action="auth.login",
        entity_type="user",
        entity_id=user.id,
        actor=user,
        summary=f"{user.email} signed in.",
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()

    token = create_access_token(
        str(user.id),
        {"role": user.role, "email": user.email, "state_id": user.state_id},
    )
    # The dashboard's SSE stream cannot send an Authorization header, so the same
    # token is also issued as an HttpOnly cookie.
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.access_token_ttl_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        path="/",
    )
    return TokenResponse(
        access_token=token,
        expires_in=settings.access_token_ttl_minutes * 60,
        user=user_read(user),
    )


@router.post("/logout", response_model=Message, summary="Clear the dashboard session cookie")
def logout(response: Response) -> Message:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return Message(message="Signed out.")


@router.get("/me", response_model=UserRead, summary="Describe the authenticated caller")
def me(principal: CurrentPrincipal, db: DbSession) -> UserRead:
    if principal.user is not None:
        return user_read(principal.user)

    state = db.get(State, principal.state_id) if principal.state_id else None
    return UserRead(
        id=0,
        email=principal.email,
        full_name=principal.full_name,
        role=str(principal.role),
        state_id=principal.state_id,
        state_code=state.code if state else None,
        state_name=state.name if state else None,
        is_active=True,
        permissions=sorted(str(p) for p in principal.permissions),
    )


@router.post("/change-password", response_model=Message, summary="Change your own password")
def change_password(
    payload: PasswordChange, principal: CurrentPrincipal, db: DbSession
) -> Message:
    if principal.user is None:
        raise ValidationError("API keys cannot change a password")
    if not verify_password(payload.current_password, principal.user.hashed_password):
        raise AuthenticationError("Current password is incorrect")
    if payload.new_password == payload.current_password:
        raise ValidationError("The new password must differ from the current one")

    principal.user.hashed_password = hash_password(payload.new_password)
    audit.record(
        db,
        action="auth.password_change",
        entity_type="user",
        entity_id=principal.user.id,
        actor=principal.user,
        summary=f"{principal.email} changed their password.",
    )
    db.commit()
    return Message(message="Password updated.")
