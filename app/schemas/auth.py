"""Authentication, user and API key schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.enums import Role
from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserRead


class UserBase(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=255)
    role: Role = Role.VIEWER
    state_code: str | None = None
    phone: str | None = None


class UserCreate(UserBase):
    password: str = Field(min_length=8, max_length=128)

    @field_validator("password")
    @classmethod
    def _strength(cls, value: str) -> str:
        if value.isalpha() or value.isdigit():
            raise ValueError("Password must mix letters with digits or symbols")
        return value


class UserUpdate(BaseModel):
    full_name: str | None = None
    role: Role | None = None
    state_code: str | None = None
    phone: str | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)


class UserRead(ORMModel):
    id: int
    email: str
    full_name: str
    role: str
    state_id: int | None = None
    state_code: str | None = None
    state_name: str | None = None
    phone: str | None = None
    is_active: bool
    last_login_at: datetime | None = None
    permissions: list[str] = []


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=3, max_length=255)
    role: Role = Role.VIEWER
    state_code: str | None = None
    expires_in_days: int | None = Field(default=365, ge=1, le=3650)


class ApiKeyRead(ORMModel):
    id: int
    name: str
    prefix: str
    role: str
    state_id: int | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    is_active: bool
    created_at: datetime


class ApiKeyCreated(ApiKeyRead):
    api_key: str = Field(description="Shown once; store it securely.")


TokenResponse.model_rebuild()
