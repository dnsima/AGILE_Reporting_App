"""Aggregates every v1 router behind the API prefix."""

from fastapi import APIRouter

from app.api.v1 import (
    admin,
    analytics,
    auth,
    cohorts,
    dashboard,
    health,
    ingestion,
    quality,
    queries,
    reconciliation,
    reference,
    reports,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(reference.router)
api_router.include_router(ingestion.router)
api_router.include_router(quality.router)
api_router.include_router(queries.router)
api_router.include_router(reconciliation.router)
api_router.include_router(analytics.router)
api_router.include_router(cohorts.router)
api_router.include_router(dashboard.router)
api_router.include_router(reports.router)
api_router.include_router(admin.router)
