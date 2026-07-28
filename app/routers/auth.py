"""Login, logout, password change, and the session keepalive."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select

from app.models.enums import AuditAction, AuditResult
from app.models.user import User
from app.security import audit
from app.security.deps import (
    AuthContext,
    DbSession,
    SettingsDep,
    require_authenticated_allow_password_change,
)
from app.security.passwords import (
    PasswordPolicyError,
    hash_password,
    needs_rehash,
    validate_password,
    verify_password,
)
from app.security.sessions import (
    SESSION_COOKIE,
    clear_session_cookie,
    create_session,
    load_session,
    revoke,
    revoke_other_sessions,
    set_session_cookie,
)
from app.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

GENERIC_LOGIN_ERROR = "Email or password is incorrect."


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, db: DbSession, next: str = "/") -> Response:
    # Already signed in? Go where you were going.
    if load_session(db, request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(_safe_next(next), status_code=303)
    return render(request, "auth/login.html", {"page_title": "Sign in", "next": next})


@router.post("/login")
async def login(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
) -> Response:
    email_normalized = email.strip().lower()

    user = db.execute(select(User).where(User.email == email_normalized)).scalar_one_or_none()

    failure = _login_failure_reason(user, password)
    if failure is not None:
        _handle_failed_login(db, request, user, email_normalized, failure)
        # The same message and the same status regardless of which check failed, so
        # the form cannot be used to enumerate who has an account here.
        return render(
            request,
            "auth/login.html",
            {
                "page_title": "Sign in",
                "next": next,
                "error": _user_facing_login_error(failure),
            },
            status_code=401,
        )

    assert user is not None  # guaranteed when failure is None

    user.last_login_at = datetime.now(UTC)

    # Opportunistically upgrade a hash whose parameters are now out of date.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    _, token = create_session(db, user, request=request)
    audit.record(
        db,
        action=AuditAction.LOGIN_SUCCESS,
        actor=user,
        request=request,
        detail={"must_change_password": user.must_change_password},
    )

    destination = "/change-password" if user.must_change_password else _safe_next(next)
    response = RedirectResponse(destination, status_code=303)
    set_session_cookie(response, token, settings)
    return response


def _login_failure_reason(user: User | None, password: str) -> str | None:
    """Return a reason string, or None if the login should succeed.

    Verifies a password even when the user does not exist, so that response timing
    does not reveal which addresses have accounts.
    """
    if user is None:
        _burn_timing(password)
        return "unknown_user"
    if not verify_password(password, user.password_hash):
        return "bad_password"
    if not user.is_active:
        return "inactive"
    return None


_DUMMY_HASH = hash_password("timing-equalizer-not-a-real-password")


def _burn_timing(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


def _user_facing_login_error(reason: str) -> str:
    return GENERIC_LOGIN_ERROR


def _handle_failed_login(
    db: DbSession, request: Request, user: User | None, attempted: str, reason: str
) -> None:
    audit.record(
        db,
        action=AuditAction.LOGIN_FAILURE,
        result=AuditResult.FAILURE,
        actor=user,
        actor_label=attempted,
        request=request,
        detail={"reason": reason},
    )


@router.post("/logout")
async def logout(request: Request, db: DbSession, settings: SettingsDep) -> Response:
    user_session = load_session(db, request.cookies.get(SESSION_COOKIE))
    if user_session is not None:
        audit.record(db, action=AuditAction.LOGOUT, actor=user_session.user, request=request)
        revoke(user_session)

    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response, settings)
    return response


@router.get("/change-password", response_class=HTMLResponse)
async def change_password_form(
    request: Request,
    auth: Annotated[AuthContext, Depends(require_authenticated_allow_password_change)],
) -> Response:
    return render(
        request,
        "auth/change_password.html",
        {
            "page_title": "Change password",
            "auth": auth,
            "forced": auth.user.must_change_password,
        },
    )


@router.post("/change-password")
async def change_password(
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, Depends(require_authenticated_allow_password_change)],
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
) -> Response:
    user = auth.user
    error: str | None = None

    if not verify_password(current_password, user.password_hash):
        error = "Your current password is incorrect."
    elif new_password != confirm_password:
        error = "The new passwords do not match."
    elif verify_password(new_password, user.password_hash):
        error = "The new password must be different from your current one."
    else:
        try:
            validate_password(new_password, email=user.email, display_name=user.display_name)
        except PasswordPolicyError as exc:
            error = str(exc)

    if error:
        audit.record(
            db,
            action=AuditAction.PASSWORD_CHANGED,
            result=AuditResult.FAILURE,
            actor=user,
            request=request,
            detail={"reason": "rejected"},
        )
        return render(
            request,
            "auth/change_password.html",
            {
                "page_title": "Change password",
                "auth": auth,
                "forced": user.must_change_password,
                "error": error,
            },
            status_code=400,
        )

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    user.password_changed_at = datetime.now(UTC)

    audit.record(db, action=AuditAction.PASSWORD_CHANGED, actor=user, request=request)

    # Every other session for this user dies, so a stolen cookie does not survive the
    # password change that was meant to shut it out.
    revoked = revoke_other_sessions(db, user.id, keep_session_id=auth.session.id)
    if revoked:
        logger.info("Revoked %d other session(s) after password change", revoked)

    return RedirectResponse("/", status_code=303)


@router.get("/session/status")
async def session_status(request: Request, db: DbSession, settings: SettingsDep) -> JSONResponse:
    """How long until idle expiry, for the client side warning.

    Deliberately does NOT extend the session: polling for the countdown must not be
    the thing that keeps a session alive, or the idle timeout would never fire.
    """
    token = request.cookies.get(SESSION_COOKIE)
    user_session = load_session(db, token)
    if user_session is None:
        return JSONResponse({"authenticated": False, "seconds_remaining": 0})

    remaining = user_session.seconds_until_idle_expiry(settings.session_timeout_minutes)
    return JSONResponse(
        {
            "authenticated": remaining > 0,
            "seconds_remaining": remaining,
            "warn_at_seconds": max(
                0,
                (settings.session_timeout_minutes - settings.session_warning_minutes) * 60,
            ),
        }
    )


@router.post("/session/extend")
async def session_extend(request: Request, db: DbSession, settings: SettingsDep) -> JSONResponse:
    """Explicit "keep me signed in" from the warning dialog."""
    user_session = load_session(db, request.cookies.get(SESSION_COOKIE))
    if user_session is None or user_session.is_idle_expired(settings.session_timeout_minutes):
        return JSONResponse({"extended": False}, status_code=401)

    user_session.last_seen_at = datetime.now(UTC)
    audit.record(
        db,
        action=AuditAction.SESSION_EXTENDED,
        actor=user_session.user,
        target_type="user_session",
        target_id=user_session.id,
        request=request,
    )
    return JSONResponse(
        {
            "extended": True,
            "seconds_remaining": settings.session_timeout_minutes * 60,
        }
    )


def _safe_next(candidate: str) -> str:
    """Only allow same origin relative redirects, so `next` cannot send users offsite."""
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate
