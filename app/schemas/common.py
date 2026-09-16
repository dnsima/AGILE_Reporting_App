"""Shared response envelopes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Message(BaseModel):
    message: str
    details: Any | None = None


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
    request_id: str | None = None


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    database: str
    data_version: int
    checks: dict[str, Any] = Field(default_factory=dict)
