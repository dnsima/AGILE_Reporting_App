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
    #: Yes/No indicators, e.g. "is the scholarship programme operational?".
    #: Stored as 1.0 or 0.0 so they aggregate and compare like any other value.
    BOOLEAN = "BOOLEAN"


class AggregationMethod(StrEnum):
    SUM = "SUM"
    AVERAGE = "AVERAGE"
    WEIGHTED_AVERAGE = "WEIGHTED_AVERAGE"
    MAX = "MAX"
    MIN = "MIN"
    LATEST = "LATEST"
    #: Unweighted mean over states that reported a non-zero value. A state
    #: reporting 0% for a completion rate is almost always a non-entry rather
    #: than a true zero, and including it drags the national rate down.
    AVERAGE_NONZERO = "AVERAGE_NONZERO"
    #: National figure for a Yes/No indicator: how many states answered Yes.
    COUNT_YES = "COUNT_YES"


class Direction(StrEnum):
    """Whether a higher or a lower value is better for an indicator."""

    INCREASE = "INCREASE"
    DECREASE = "DECREASE"


class IndicatorDisposition(StrEnum):
    """What became of an indicator when the results framework was revised."""

    RETAINED = "RETAINED"
    MERGED = "MERGED"
    TRANSFORMED = "TRANSFORMED"
    MOVED = "MOVED"
    SPLIT = "SPLIT"
    DROPPED = "DROPPED"
    NEW = "NEW"


#: Dispositions whose figures may be compared period-on-period. MERGED is
#: comparable only once the predecessor's parts are summed, which the analysis
#: engine does; the rest have no like-for-like basis and comparisons against
#: them are suppressed rather than reported as movement.
COMPARABLE_DISPOSITIONS = {
    IndicatorDisposition.RETAINED,
    IndicatorDisposition.MOVED,
    IndicatorDisposition.MERGED,
}


class TargetLevel(StrEnum):
    STATE = "STATE"
    NATIONAL = "NATIONAL"


class QueryStatus(StrEnum):
    """Lifecycle of a data query raised against a reported figure."""

    OPEN = "OPEN"                    # raised, awaiting the state's response
    RESPONDED = "RESPONDED"          # state has responded, awaiting NPCU review
    ACCEPTED = "ACCEPTED"            # NPCU accepted: figure confirmed or restated
    REJECTED = "REJECTED"            # NPCU rejected the response; back to the state
    VERIFICATION = "VERIFICATION"    # escalated to physical verification
    WITHDRAWN = "WITHDRAWN"          # raised in error


#: A query in one of these states is still work for someone.
OPEN_QUERY_STATUSES = {
    QueryStatus.OPEN,
    QueryStatus.RESPONDED,
    QueryStatus.REJECTED,
    QueryStatus.VERIFICATION,
}


class QueryResolution(StrEnum):
    """How a query was settled."""

    CONFIRMED = "CONFIRMED"          # the figure stands as reported, with evidence
    RESTATED = "RESTATED"            # the figure was corrected
    VERIFIED = "VERIFIED"            # settled by physical verification
    WITHDRAWN = "WITHDRAWN"


class TargetStatus(StrEnum):
    """Targets only count once the governance body has cleared them."""

    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    SUPERSEDED = "SUPERSEDED"


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
