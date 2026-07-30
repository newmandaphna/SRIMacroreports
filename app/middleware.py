"""Security middleware: transport headers, CSP, and safe error responses.

CSRF protection and session handling arrive in Phase 1 with authentication. What is
here now is the transport and header layer, plus the exception handler that keeps
stack traces off the wire.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import Settings

logger = logging.getLogger(__name__)

HSTS_MAX_AGE = 31_536_000  # one year

# Chart.js is the one permitted off origin source. Everything else is same origin.
CHART_CDN = "https://cdn.jsdelivr.net"

CSP = "; ".join(
    (
        "default-src 'self'",
        f"script-src 'self' {CHART_CDN}",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "base-uri 'self'",
        "object-src 'none'",
    )
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply the standard security headers to every response."""

    def __init__(self, app: FastAPI, *, production: bool) -> None:
        super().__init__(app)
        self.production = production

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = CSP
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=(), payment=()"
        )
        # Patient data must never sit in a shared or browser cache.
        response.headers["Cache-Control"] = "no-store, max-age=0"
        if self.production:
            response.headers["Strict-Transport-Security"] = (
                f"max-age={HSTS_MAX_AGE}; includeSubDomains; preload"
            )
        return response


def install_middleware(app: FastAPI, settings: Settings) -> None:
    """Install middleware in the correct order and register error handlers.

    Starlette applies the LAST added first, so this reads inside out. The intended
    order on the way in is:

      TrustedHost, HTTPSRedirect, SecurityHeaders, CSRF

    CSRF used to be added last and so ran outermost, ahead of the host and scheme
    guards. That put a database write (the audit record for a refusal) and a full read
    of the request body behind neither of them: a request with a forged Host, or one
    arriving over plain HTTP that was about to be redirected, had its form parsed and
    could drive an audit write first. Host and scheme are the cheapest possible
    rejections and belong outside everything. Security headers stay outside CSRF, so a
    refused request still carries them on the way out.
    """
    from app.security.csrf import CSRFMiddleware

    app.add_middleware(CSRFMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, production=settings.is_production)

    if settings.is_production:
        app.add_middleware(HTTPSRedirectMiddleware)
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*.replit.app", "*.repl.co"])

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
        """Return a correlation id, never a traceback.

        The traceback goes to the log, where the scrubbing filter and formatter clean
        it. The browser gets an opaque id it can quote to an administrator.
        """
        correlation_id = uuid.uuid4().hex[:12]
        logger.exception(
            "Unhandled error %s on %s %s",
            correlation_id,
            request.method,
            request.url.path,
        )
        message = (
            f"Something went wrong. Quote this reference to an administrator: {correlation_id}"
        )
        accepts = request.headers.get("accept", "")
        if "application/json" in accepts:
            return JSONResponse(
                {"error": "internal_error", "reference": correlation_id},
                status_code=500,
            )
        return PlainTextResponse(message, status_code=500)
