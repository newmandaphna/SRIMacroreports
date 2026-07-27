"""Application entry point.

Phases 1 to 3: authentication and administration, the import pipeline, and the
Financial module with the Reports overview dashboard.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

import app.models  # noqa: F401  registers every model on Base.metadata
from app.config import Settings, get_settings
from app.db import Base, DatabaseHandle
from app.logging_setup import configure_logging
from app.middleware import install_middleware
from app.models.enums import Module
from app.routers import (
    admin_audit,
    admin_config,
    admin_sources,
    admin_therapists,
    admin_users,
    auth,
    reports,
    rooms,
    status,
    utilization,
)
from app.security.csrf import CSRFMiddleware
from app.security.deps import (
    AccessDenied,
    FeatureDisabled,
    OptionalUser,
    RedirectRequired,
)
from app.seed import seed_admin
from app.templating import render

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the encrypted database, run migrations metadata, seed the admin."""
    settings: Settings = app.state.settings
    logger.info("Starting SRI Practice Dashboard (environment=%s)", settings.environment)

    app.state.db = DatabaseHandle(settings)

    # Alembic owns the schema in a real deployment. This creates any table that a
    # migration has not yet produced, which keeps development and tests runnable
    # without a migration step for every model change.
    Base.metadata.create_all(app.state.db.engine)

    with app.state.db.session() as db:
        seed_admin(db, settings)

    try:
        yield
    finally:
        app.state.db.dispose()
        logger.info("Shutdown complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory. Tests build their own instance with their own settings."""
    settings = settings or get_settings()
    configure_logging("DEBUG" if settings.debug else "INFO")

    app = FastAPI(
        title="SRI Practice Dashboard",
        description="Internal practice management reporting for SRI Psychological Services.",
        version="0.6.0",
        lifespan=lifespan,
        # No public API docs on a PHI application.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings

    install_middleware(app, settings)
    # Added after the security headers so it runs before them on the way in: a
    # rejected request still gets the headers on the way out.
    app.add_middleware(CSRFMiddleware)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(auth.router)
    app.include_router(admin_users.router)
    app.include_router(admin_audit.router)
    app.include_router(admin_sources.router)
    app.include_router(admin_config.router)
    app.include_router(admin_therapists.router)
    app.include_router(reports.router)
    app.include_router(utilization.router)
    app.include_router(rooms.router)
    app.include_router(status.router)

    register_error_handlers(app)
    register_routes(app)
    return app


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RedirectRequired)
    async def handle_redirect(request: Request, exc: RedirectRequired) -> Response:
        location = exc.location
        # Preserve where they were going, so signing in lands them there.
        if location == "/login" and request.method == "GET" and request.url.path != "/":
            location = f"/login?next={request.url.path}"
        return RedirectResponse(location, status_code=exc.status_code)

    @app.exception_handler(FeatureDisabled)
    async def handle_feature_disabled(request: Request, _exc: FeatureDisabled) -> Response:
        """A disabled module is indistinguishable from one that does not exist.

        A 403 would confirm the feature is there. A 404 is both truer to the state of
        the world and less informative to a scanner.
        """
        return render(
            request,
            "errors/not_found.html",
            {"page_title": "Not found"},
            status_code=404,
        )

    @app.exception_handler(AccessDenied)
    async def handle_access_denied(request: Request, exc: AccessDenied) -> Response:
        logger.warning("Access denied on %s %s", request.method, request.url.path)
        return render(
            request,
            "errors/forbidden.html",
            {"page_title": "Access denied", "message": exc.message},
            status_code=403,
        )


def register_routes(app: FastAPI) -> None:
    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        """Liveness probe. Deliberately carries no data and needs no auth."""
        return JSONResponse({"status": "ok", "version": app.version})

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        """Readiness probe: the encrypted database answers a query."""
        from sqlalchemy import text

        try:
            with request.app.state.db.session() as session:
                session.execute(text("SELECT 1"))
        except Exception:
            logger.exception("Readiness check failed")
            return JSONResponse({"status": "degraded"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request, auth_ctx: OptionalUser) -> Response:
        if auth_ctx is None:
            return RedirectResponse("/login", status_code=303)
        if auth_ctx.user.must_change_password:
            return RedirectResponse("/change-password", status_code=303)
        # Anyone who can see the financial module lands on the dashboard rather than
        # on a placeholder they then have to click through.
        allowed, _ = auth_ctx.user.can_view(Module.FINANCIAL)
        if allowed:
            return RedirectResponse("/reports", status_code=303)
        return render(request, "index.html", {"page_title": "Overview", "auth": auth_ctx})


# Served with the factory form so that importing this module does not require a valid
# environment (tests build their own app):
#   uvicorn app.main:create_app --factory
