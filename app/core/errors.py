"""Application error hierarchy.

Every error carries a machine-readable ``code`` and an HTTP status so the API
layer can translate it into a consistent JSON envelope without each router
re-implementing error handling.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for all expected application failures."""

    status_code: int = 400
    code: str = "app_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class ValidationError(AppError):
    """Raised when user-supplied input is structurally unusable."""

    status_code = 422
    code = "validation_error"


class IngestionError(AppError):
    """Raised when an uploaded file cannot be parsed or mapped."""

    status_code = 422
    code = "ingestion_error"


class AuthenticationError(AppError):
    status_code = 401
    code = "authentication_error"


class PermissionDeniedError(AppError):
    status_code = 403
    code = "permission_denied"
