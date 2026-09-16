"""Server-rendered shell for the dashboard.

The pages are thin: they render a layout and let the browser call the same JSON
API that external consumers use, so the dashboard can never drift from the API.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app import __version__
from app.core.config import settings

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

router = APIRouter(include_in_schema=False)


def _context(request: Request, **extra) -> dict:
    return {
        "request": request,
        "app_name": settings.app_name,
        "version": __version__,
        "api_prefix": settings.api_prefix,
        "environment": settings.environment,
        **extra,
    }


@router.get("/", response_class=RedirectResponse)
def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard", status_code=307)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name="login.html", context=_context(request, page="login")
    )


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name="dashboard.html", context=_context(request, page="dashboard")
    )


@router.get("/favicon.ico")
def favicon() -> Response:
    # A tiny inline mark keeps the browser from logging a 404 on every page load.
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="7" fill="#0f3d61"/>'
        '<text x="16" y="22" font-family="Helvetica,Arial" font-size="16" font-weight="bold" '
        'fill="#7ec8f5" text-anchor="middle">A</text></svg>'
    )
    return Response(content=svg, media_type="image/svg+xml")
