"""SQLAlchemy models. Importing this package registers every mapper."""

from app.models.audit import AuditLog
from app.models.reference import (
    Cohort,
    Indicator,
    IndicatorCategory,
    IndicatorLink,
    ReportingPeriod,
    State,
    StateSubcomponent,
    Subcomponent,
)
from app.models.report import GeneratedReport
from app.models.submission import IndicatorValue, Submission, Target
from app.models.user import ApiKey, User
from app.models.validation import DQAScore, ValidationIssue, ValidationRule

__all__ = [
    "ApiKey",
    "AuditLog",
    "Cohort",
    "DQAScore",
    "GeneratedReport",
    "Indicator",
    "IndicatorCategory",
    "IndicatorLink",
    "IndicatorValue",
    "ReportingPeriod",
    "State",
    "StateSubcomponent",
    "Subcomponent",
    "Submission",
    "Target",
    "User",
    "ValidationIssue",
    "ValidationRule",
]
