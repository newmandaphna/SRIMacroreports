"""Application entry point.

Phase 0 scaffold: the app boots, proves its configuration and its encrypted database,
serves a health endpoint and a placeholder shell rendered with the real design tokens.
Auth arrives in Phase 1, the data model and sync in Phase 2.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Settings, get_settings
from app.db import DatabaseHandle
from app.logging_setup import configure_logging
from app.middleware import install_middleware

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the encrypted database at startup, dispose of it at shutdown.

    Failures here stop the process. That is deliberate: a PHI application should not
    serve traffic while its database is misconfigured.
    """
    settings: Settings = app.state.settings
    logger.info("Starting SRI Practice Dashboard (environment=%s)", settings.environment)
    app.state.db = DatabaseHandle(settings)
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
        version="0.1.0",
        lifespan=lifespan,
        # No public API docs on a PHI application.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings

    install_middleware(app, settings)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    register_routes(app, settings)
    return app


def register_routes(app: FastAPI, settings: Settings) -> None:
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
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "page_title": "Overview",
                "environment": settings.environment,
                "room_utilization_enabled": settings.room_utilization_enabled,
                "patient_funnel_enabled": settings.patient_funnel_enabled,
            },
        )


# Served with the factory form so that importing this module does not require a valid
# environment (tests build their own app):
#   uvicorn app.main:create_app --factory
