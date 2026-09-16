"""Test fixtures.

The database URL and storage directories are redirected to a temporary
directory *before* the application modules are imported, because settings are
resolved once at import time.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agile-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_DIR"] = str(_TMP / "reports")
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production-use"
os.environ["ENVIRONMENT"] = "test"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["LOG_JSON"] = "false"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.enums import PeriodType, Role  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import (  # noqa: E402
    Cohort,
    Indicator,
    IndicatorCategory,
    ReportingPeriod,
    State,
    Target,
    User,
)
from app.services.validation import sync_rule_catalog  # noqa: E402

PASSWORD = "TestPass123!"


@lru_cache(maxsize=1)
def _password_hash() -> str:
    """Hash the shared test password once; bcrypt is deliberately slow."""
    return hash_password(PASSWORD)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db():
    """A clean, fully seeded database for each test."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        _seed(session)
        session.commit()
        yield session
    finally:
        session.close()


def _seed(session) -> None:
    cohorts = {
        "ORIGINAL": Cohort(code="ORIGINAL", name="Original Financing States", sort_order=1),
        "ADDITIONAL": Cohort(code="ADDITIONAL", name="Additional Financing States", sort_order=2),
        "LIMITED": Cohort(code="LIMITED", name="Limited Financing States", sort_order=3),
    }
    session.add_all(cohorts.values())
    session.flush()

    states = [
        State(code="KN", name="Kano", geopolitical_zone="North West", cohort_id=cohorts["ORIGINAL"].id),
        State(code="KD", name="Kaduna", geopolitical_zone="North West", cohort_id=cohorts["ORIGINAL"].id),
        State(code="GO", name="Gombe", geopolitical_zone="North East", cohort_id=cohorts["ADDITIONAL"].id),
        State(code="LA", name="Lagos", geopolitical_zone="South West", cohort_id=cohorts["LIMITED"].id),
    ]
    session.add_all(states)

    category = IndicatorCategory(code="PDO", name="Project Development Objective", sort_order=1)
    session.add(category)
    session.flush()

    session.add_all(
        [
            Indicator(
                number=1, code="KPI-001", name="Number of girls benefiting from the project",
                category_id=category.id, unit="NUMBER", aggregation_method="SUM",
                direction="INCREASE", is_cumulative=True, min_value=0, decimal_places=0,
                aliases=["girls benefiting"], is_core=True,
            ),
            Indicator(
                number=2, code="KPI-002", name="Transition rate from primary to junior secondary school",
                category_id=category.id, unit="PERCENT", aggregation_method="WEIGHTED_AVERAGE",
                direction="INCREASE", requires_numerator_denominator=True,
                min_value=0, max_value=100, decimal_places=1, is_core=True,
            ),
            Indicator(
                number=3, code="KPI-003", name="Pupil-classroom ratio in supported schools",
                category_id=category.id, unit="RATIO", aggregation_method="AVERAGE",
                direction="DECREASE", baseline_value=80.0, min_value=0, decimal_places=1,
                is_core=True,
            ),
            Indicator(
                number=4, code="KPI-004", name="Number of teachers recruited under the project",
                category_id=category.id, unit="NUMBER", aggregation_method="SUM",
                direction="INCREASE", min_value=0, decimal_places=0, is_core=True,
            ),
        ]
    )

    today = date.today()
    session.add_all(
        [
            ReportingPeriod(
                code="2025-Q4", label="Q4 2025 (Oct-Dec)", period_type=str(PeriodType.QUARTERLY),
                fiscal_year=2025, sequence=4, start_date=date(2025, 10, 1),
                end_date=date(2025, 12, 31), due_date=date(2026, 1, 15),
            ),
            ReportingPeriod(
                code="2026-Q1", label="Q1 2026 (Jan-Mar)", period_type=str(PeriodType.QUARTERLY),
                fiscal_year=2026, sequence=1, start_date=date(2026, 1, 1),
                end_date=date(2026, 3, 31), due_date=today + timedelta(days=30),
            ),
        ]
    )
    session.flush()

    session.add_all(
        [
            User(
                email="admin@test.gov.ng", full_name="Admin User",
                hashed_password=_password_hash(), role=str(Role.ADMIN),
            ),
            User(
                email="npcu@test.gov.ng", full_name="NPCU Officer",
                hashed_password=_password_hash(), role=str(Role.NPCU),
            ),
            User(
                email="kano@test.gov.ng", full_name="Kano PIU",
                hashed_password=_password_hash(), role=str(Role.STATE_PIU),
                state_id=states[0].id,
            ),
            User(
                email="viewer@test.gov.ng", full_name="Viewer",
                hashed_password=_password_hash(), role=str(Role.VIEWER),
            ),
        ]
    )
    sync_rule_catalog(session)
    session.flush()

    # Targets for the open period, which also define each state's obligation.
    period = session.query(ReportingPeriod).filter_by(code="2026-Q1").one()
    indicators = session.query(Indicator).all()
    for state in states:
        for indicator in indicators:
            session.add(
                Target(
                    indicator_id=indicator.id, period_id=period.id, level="STATE",
                    state_id=state.id,
                    target_value={"NUMBER": 1000.0, "PERCENT": 80.0, "RATIO": 40.0}[indicator.unit],
                    source="test fixture",
                )
            )
    session.flush()


@pytest.fixture()
def client(db):
    with TestClient(fastapi_app) as test_client:
        yield test_client


def _token(client: TestClient, email: str) -> str:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture()
def admin_headers(client):
    return {"Authorization": f"Bearer {_token(client, 'admin@test.gov.ng')}"}


@pytest.fixture()
def npcu_headers(client):
    return {"Authorization": f"Bearer {_token(client, 'npcu@test.gov.ng')}"}


@pytest.fixture()
def state_headers(client):
    return {"Authorization": f"Bearer {_token(client, 'kano@test.gov.ng')}"}


@pytest.fixture()
def viewer_headers(client):
    return {"Authorization": f"Bearer {_token(client, 'viewer@test.gov.ng')}"}
