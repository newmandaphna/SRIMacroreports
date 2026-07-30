"""CSRF protection on every state changing route.

The token is bound to the server side session rather than double submitted in a
cookie, so an attacker who can write cookies for a sibling subdomain still cannot
forge a request.

Enforcement is centralized in the middleware below rather than left to each route, so
a new POST handler is protected by default instead of by remembering.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Request
from fastapi.responses import PlainTextResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.security.sessions import SESSION_COOKIE, load_session

logger = logging.getLogger(__name__)

CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Paths that change no state and hold no session. Everything else is protected.
EXEMPT_PATHS = frozenset({"/healthz", "/readyz"})


class CSRFMiddleware(BaseHTTPMiddleware):
    """Reject state changing requests without a valid session bound CSRF token."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method in SAFE_METHODS or request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        # The session lookup comes first, deliberately. Extracting the token reads the
        # request body, and for a multipart upload that means buffering the whole thing
        # in memory. Doing that before knowing whether there is even a session to
        # compare against let an unauthenticated caller make the server hold an
        # arbitrary body for a request it was going to wave through anyway.
        expected = _expected_token(request)

        # No session means no token to compare against. The login POST is the normal
        # case here: it is state changing but pre authentication, so it is allowed
        # through and protected instead by SameSite=Strict plus its own credentials.
        if expected is None:
            return await call_next(request)

        submitted = await _extract_token(request)

        if not submitted or not hmac.compare_digest(submitted, expected):
            logger.warning("CSRF rejection on %s %s", request.method, request.url.path)
            _record_rejection(request)
            return PlainTextResponse(
                "Your session could not be verified. Reload the page and try again.",
                status_code=403,
            )

        return await call_next(request)


def _record_rejection(request: Request) -> None:
    """Write the refusal to the audit log.

    A CSRF failure is either a bug or an attack, and both are worth a record. Best
    effort: a logging failure must never turn a clean 403 into a 500.
    """
    from app.models.enums import AuditAction, AuditResult
    from app.security import audit

    db_handle = getattr(request.app.state, "db", None)
    if db_handle is None:  # pragma: no cover
        return
    try:
        with db_handle.session() as db:
            user_session = load_session(db, request.cookies.get(SESSION_COOKIE))
            audit.record(
                db,
                action=AuditAction.ACCESS_DENIED,
                result=AuditResult.FAILURE,
                actor=user_session.user if user_session else None,
                request=request,
                detail={
                    "reason": "csrf_token_invalid",
                    "path": request.url.path,
                    "method": request.method,
                },
            )
    except Exception:  # pragma: no cover - never fail the response over an audit write
        logger.exception("Could not record the CSRF rejection")


async def _extract_token(request: Request) -> str | None:
    header = request.headers.get(CSRF_HEADER)
    if header:
        return header

    content_type = request.headers.get("content-type", "")
    if not content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        return None

    # Read the raw body first. Starlette only replays a consumed body to the
    # downstream app when `body()` was the call that consumed it, so parsing the
    # form on its own would leave the route handler with an empty request.
    await request.body()

    form = await request.form()
    value = form.get(CSRF_FIELD)
    return value if isinstance(value, str) else None


def _expected_token(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    db_handle = getattr(request.app.state, "db", None)
    if db_handle is None:  # pragma: no cover - only before startup completes
        return None
    with db_handle.session() as db:
        user_session = load_session(db, token)
        return user_session.csrf_token if user_session else None
