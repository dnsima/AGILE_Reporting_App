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
    #: Retained so a database seeded before AGILE settled on monthly and
    #: quarterly reporting still loads. Nothing generates or offers them.
    SEMI_ANNUAL = "SEMI_ANNUAL"
    ANNUAL = "ANNUAL"


#: The period types AGILE actually reports on. States file monthly to the
#: performance tracker and quarterly against the results framework; the
#: half-year and annual calendars were never used and only cluttered every
#: period picker in the platform.
REPORTING_PERIOD_TYPES: tuple[PeriodType, ...] = (
    PeriodType.MONTHLY,
    PeriodType.QUARTERLY,
)


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


#: Methods where the national figure is the sum of what states contributed, so
#: one state's value is a genuine share of it. The averaging methods are not:
#: a state's completion rate is its own performance, not a slice of a national
#: rate, and treating it as one gives a state reporting 88% against a national
#: target of 56% a "contribution" of 157%.
ADDITIVE_METHODS = {AggregationMethod.SUM, AggregationMethod.COUNT_YES}


class TimeBasis(StrEnum):
    """How one period's figure is built from the finer periods inside it.

    This is a different axis from :class:`AggregationMethod`, which says how
    states combine into a national figure. A quarter is built from its months
    along the *time* axis, and the two answers differ: "girls enrolled" sums
    across states but does **not** sum across months, because each month's
    tracker figure is already a running total.

    Getting this wrong in either direction is a real reporting failure -- a
    quarter triple-counted, or a genuine three-month total read as a snapshot.
    """

    #: A running total. The period equals its last constituent period.
    SNAPSHOT = "SNAPSHOT"
    #: A count of what happened within the period. The parts add up.
    SUM = "SUM"
    #: The most recent answer, e.g. a Yes/No status.
    LATEST = "LATEST"
    MAX = "MAX"
    MIN = "MIN"


#: Bases whose verdict needs every constituent period. A partial sum is always
#: below the whole, so judging one before the months are all in manufactures a
#: discrepancy; a snapshot, by contrast, is answered by the final period alone.
TIME_BASES_NEEDING_EVERY_PART = {TimeBasis.SUM, TimeBasis.MAX, TimeBasis.MIN}

#: Units that carry a rate rather than a count, and so tolerate rounding.
RATE_UNITS = {IndicatorUnit.PERCENT, IndicatorUnit.RATIO, IndicatorUnit.SCORE}


class ReconciliationStatus(StrEnum):
    """Verdict on one indicator across two reporting streams."""

    MATCHED = "MATCHED"
    MISMATCH = "MISMATCH"
    #: The coarser stream reported it; the finer one never did.
    TRACKER_MISSING = "TRACKER_MISSING"
    #: The finer stream reported it; the coarser one left it out.
    FRAMEWORK_MISSING = "FRAMEWORK_MISSING"
    #: Not enough of the finer periods are in to reach a verdict yet.
    INCOMPLETE = "INCOMPLETE"


#: Statuses that are somebody's work.
UNRECONCILED_STATUSES = {
    ReconciliationStatus.MISMATCH,
    ReconciliationStatus.TRACKER_MISSING,
    ReconciliationStatus.FRAMEWORK_MISSING,
}


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
    DOCX = "docx"


class ReportKind(StrEnum):
    """Which report to build. The two NPCU quarterlies are fixed documents.

    PERFORMANCE is the platform's own configurable report; the other two
    reproduce the documents the NPCU produces each cycle, so they take a period
    and nothing else.
    """

    PERFORMANCE = "PERFORMANCE"
    VALIDATION = "VALIDATION"
    TECHNICAL = "TECHNICAL"


class DisclosureStatus(StrEnum):
    """What the platform is saying about a single reported figure.

    None of these ever removes a figure from an aggregate. The NPCU reports
    what states reported and discloses what it doubts; a figure changes only
    through the change-management process, never because a rule decided so.
    """

    #: No open finding against it.
    CLEAN = "CLEAN"
    #: An open query, but nothing that makes the figure unusable on its face.
    QUERIED = "QUERIED"
    #: A blocking finding: the figure is counted, and marked unfit for use.
    UNFIT = "UNFIT"
    #: Changed through a query resolution. ``original_value`` keeps what was
    #: first reported, so a published report still reconciles.
    CORRECTED = "CORRECTED"


#: Statuses that put a figure's contribution to the national total in doubt.
EXPOSED_STATUSES = {DisclosureStatus.UNFIT, DisclosureStatus.QUERIED}


class FitnessVerdict(StrEnum):
    """The headline a reader should act on.

    This is the platform's only judgement about a return. It replaced a 0-100
    DQA score and a letter grade, which answered "what share of checks
    passed?" -- about 98 for any plausible return -- while readers took the
    headline as an answer to "can I use this?". Scoring a return is a job for
    a proper DQA process with a field visit behind it, not for a pass rate
    over automated checks.
    """

    FIT = "FIT"
    FIT_WITH_NOTES = "FIT WITH NOTES"
    NOT_FIT = "NOT FIT FOR USE"
    NO_DATA = "NO DATA"


NOT_FIT_EXPOSED_SHARE = 10.0


def verdict_for_submission(
    *,
    has_data: bool,
    exposed_share: float | None,
    material_findings: int,
    open_findings: int,
) -> FitnessVerdict:
    """Say whether a return can be used, which is what a reader actually asks.

    This replaced a 0-100 score and a letter grade. The score was a pass rate
    over every check attempted, so it sat near 100 for any plausible return:
    Gombe scored 99.13 in Q2 2026 while reporting 127 schools where it had
    reported 5,960 the quarter before, an error large enough to move the
    national figure by 89%. A reader who saw "Excellent" had no way to know.

    Materiality decides, not the count of findings. One blocking finding whose
    error is a rounding artefact leaves a return usable; one whose error moves
    the national result does not.
    """
    if not has_data:
        return FitnessVerdict.NO_DATA
    if material_findings > 0:
        return FitnessVerdict.NOT_FIT
    if exposed_share is not None and exposed_share >= NOT_FIT_EXPOSED_SHARE:
        return FitnessVerdict.NOT_FIT
    if open_findings > 0:
        return FitnessVerdict.FIT_WITH_NOTES
    return FitnessVerdict.FIT


def verdict_note(
    verdict: FitnessVerdict,
    *,
    exposed_share: float | None,
    material_findings: int,
    open_findings: int,
) -> str | None:
    """Say why the verdict is what it is. An unexplained verdict is worthless."""
    if verdict is FitnessVerdict.NO_DATA:
        return None
    if verdict is FitnessVerdict.FIT:
        return "No open findings against this return."
    share = (
        ""
        if exposed_share is None
        else f" {exposed_share:.1f}% of this state's contribution to the "
        "national totals rests on them."
    )
    if verdict is FitnessVerdict.NOT_FIT:
        if material_findings > 0:
            return (
                f"{material_findings} finding(s) large enough to move a national "
                f"figure.{share} Every figure is still counted and shown; the "
                "figures themselves must be reconciled with the SPIU."
            )
        return f"Findings against this return are material in aggregate.{share}"
    return (
        f"{open_findings} open finding(s), none large enough on its own to move "
        f"a national figure.{share}"
    )


