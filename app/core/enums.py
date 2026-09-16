"""Shared enumerations used across models, schemas and services."""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


class CohortCode(StrEnum):
    """AGILE financing cohorts / cycles."""

    ORIGINAL = "ORIGINAL"
    ADDITIONAL = "ADDITIONAL"
    LIMITED = "LIMITED"


class Role(StrEnum):
    ADMIN = "ADMIN"
    NPCU = "NPCU"
    ME_OFFICER = "ME_OFFICER"
    STATE_PIU = "STATE_PIU"
    VIEWER = "VIEWER"


class Permission(StrEnum):
    DATA_READ = "data:read"
    DATA_UPLOAD = "data:upload"
    DATA_APPROVE = "data:approve"
    DATA_DELETE = "data:delete"
    ANALYTICS_READ = "analytics:read"
    REPORTS_GENERATE = "reports:generate"
    REFERENCE_MANAGE = "reference:manage"
    USERS_MANAGE = "users:manage"
    APIKEYS_MANAGE = "apikeys:manage"
    AUDIT_READ = "audit:read"


ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.ADMIN: set(Permission),
    Role.NPCU: {
        Permission.DATA_READ,
        Permission.DATA_UPLOAD,
        Permission.DATA_APPROVE,
        Permission.ANALYTICS_READ,
        Permission.REPORTS_GENERATE,
        Permission.REFERENCE_MANAGE,
        Permission.APIKEYS_MANAGE,
        Permission.AUDIT_READ,
    },
    Role.ME_OFFICER: {
        Permission.DATA_READ,
        Permission.DATA_UPLOAD,
        Permission.DATA_APPROVE,
        Permission.ANALYTICS_READ,
        Permission.REPORTS_GENERATE,
        Permission.AUDIT_READ,
    },
    Role.STATE_PIU: {
        Permission.DATA_READ,
        Permission.DATA_UPLOAD,
        Permission.ANALYTICS_READ,
        Permission.REPORTS_GENERATE,
    },
    Role.VIEWER: {
        Permission.DATA_READ,
        Permission.ANALYTICS_READ,
    },
}

#: Roles whose visibility is limited to their own state.
STATE_SCOPED_ROLES = {Role.STATE_PIU}


class PeriodType(StrEnum):
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    SEMI_ANNUAL = "SEMI_ANNUAL"
    ANNUAL = "ANNUAL"


class SubmissionStatus(StrEnum):
    DRAFT = "DRAFT"
    UPLOADED = "UPLOADED"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    APPROVED = "APPROVED"
    SUPERSEDED = "SUPERSEDED"


class Severity(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


class DQADimension(StrEnum):
    """The seven data-quality dimensions assessed for every submission."""

    INTEGRITY = "INTEGRITY"
    TIMELINESS = "TIMELINESS"
    ACCURACY = "ACCURACY"
    COMPLETENESS = "COMPLETENESS"
    CONSISTENCY = "CONSISTENCY"
    VALIDITY = "VALIDITY"
    UNIQUENESS = "UNIQUENESS"


#: Relative weights used to roll the seven dimensions into one score.
DQA_WEIGHTS: dict[DQADimension, float] = {
    DQADimension.INTEGRITY: 1.0,
    DQADimension.TIMELINESS: 1.0,
    DQADimension.ACCURACY: 1.5,
    DQADimension.COMPLETENESS: 1.5,
    DQADimension.CONSISTENCY: 1.0,
    DQADimension.VALIDITY: 1.5,
    DQADimension.UNIQUENESS: 0.5,
}


class IndicatorUnit(StrEnum):
    NUMBER = "NUMBER"
    PERCENT = "PERCENT"
    RATIO = "RATIO"
    CURRENCY_NGN_M = "CURRENCY_NGN_M"
    SCORE = "SCORE"


class AggregationMethod(StrEnum):
    SUM = "SUM"
    AVERAGE = "AVERAGE"
    WEIGHTED_AVERAGE = "WEIGHTED_AVERAGE"
    MAX = "MAX"
    MIN = "MIN"
    LATEST = "LATEST"


class Direction(StrEnum):
    """Whether a higher or a lower value is better for an indicator."""

    INCREASE = "INCREASE"
    DECREASE = "DECREASE"


class TargetLevel(StrEnum):
    STATE = "STATE"
    NATIONAL = "NATIONAL"


class ReportScope(StrEnum):
    STATE = "STATE"
    NATIONAL = "NATIONAL"
    COHORT = "COHORT"


class ReportFormat(StrEnum):
    MARKDOWN = "markdown"
    HTML = "html"
    PDF = "pdf"


#: Grade bands, strongest first.
GRADE_BANDS = ("Excellent", "Good", "Fair", "Weak", "Poor")


def grade_for_score(score: float | None) -> str:
    """Map a 0-100 score onto the platform's performance grade bands."""
    if score is None:
        return "No data"
    if score >= 90:
        return "Excellent"
    if score >= 80:
        return "Good"
    if score >= 70:
        return "Fair"
    if score >= 60:
        return "Weak"
    return "Poor"


#: A dimension at or below this score costs the submission one grade band.
WEAK_DIMENSION_THRESHOLD = 80.0
#: At or below this, it costs two.
CRITICAL_DIMENSION_THRESHOLD = 60.0


def grade_for_submission(score: float | None, dimension_scores: list[float]) -> str:
    """Grade a submission, letting one weak dimension cap the headline grade.

    The overall score is a weighted mean, so a single badly failing dimension
    (a state that reported only two-thirds of its indicators, say) can still
    average out near the top. Capping the grade keeps the headline honest: the
    mean says how much passed, the grade says whether anything needs attention.
    """
    if score is None:
        return "No data"
    base = grade_for_score(score)
    if not dimension_scores:
        return base

    weakest = min(dimension_scores)
    demotion = 0
    if weakest < CRITICAL_DIMENSION_THRESHOLD:
        demotion = 2
    elif weakest < WEAK_DIMENSION_THRESHOLD:
        demotion = 1

    index = min(GRADE_BANDS.index(base) + demotion, len(GRADE_BANDS) - 1)
    return GRADE_BANDS[index]
