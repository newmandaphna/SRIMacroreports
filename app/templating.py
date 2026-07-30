"""Template rendering with the shared context every page needs.

One entry point, `render`, so that the CSRF token, the signed in user, and the module
navigation are present on every page without each route remembering to pass them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.models.enums import Module, Role

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _currency(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"${value:,.2f}"


def _money(value: float | int | None) -> str:
    """Whole dollars, for headline tiles. Cents on a KPI card are noise that can
    also overflow the card; the exact figure lives in the tables and exports."""
    if value is None:
        return "-"
    return f"${value:,.0f}"


def _count(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.0f}"


def _datetime(value: Any, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if value is None:
        return "-"
    return value.strftime(fmt)


templates.env.filters["currency"] = _currency
templates.env.filters["money"] = _money
templates.env.filters["count"] = _count
templates.env.filters["datetime"] = _datetime
templates.env.filters["urlencode"] = quote_plus


def render(
    request: Request,
    template: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    settings = request.app.state.settings
    auth = context.get("auth") if context else None
    if auth is None:
        auth = getattr(request.state, "auth", None)

    merged: dict[str, Any] = {
        "environment": settings.environment,
        "settings": settings,
        "auth": auth,
        "csrf_token": auth.csrf_token if auth else "",
        "nav_modules": auth.visible_modules() if auth else [],
        "all_modules": list(Module),
        "all_roles": list(Role),
        "current_path": request.url.path,
        # Set by the auth dependency from the stored setting. The environment value
        # is the fallback for pages reached without a session, such as login.
        "session_timeout_minutes": getattr(
            request.state, "session_timeout_minutes", settings.session_timeout_minutes
        ),
        "session_warning_minutes": settings.session_warning_minutes,
    }
    merged.update(context or {})

    return templates.TemplateResponse(request, template, merged, status_code=status_code)
