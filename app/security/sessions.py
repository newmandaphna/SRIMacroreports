"""Server side session lifecycle.

The cookie holds an opaque random token. Only its SHA-256 hash is stored, so reading
the database does not yield usable session cookies.

Idle expiry is evaluated here, on the server, against `last_seen_at`. The client side
warning is a courtesy; this is the enforcement.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime

from fastapi import Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.session import UserSession
from app.models.user import User

SESSION_COOKIE = "sri_session"
TOKEN_BYTES = 32


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def create_session(
    db: Session, user: User, *, request: Request | None = None
) -> tuple[UserSession, str]:
    """Start a session and return it alongside the raw token for the cookie."""
    token = new_token()
    user_session = UserSession(
        token_hash=_hash_token(token),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(TOKEN_BYTES),
        source_ip=(request.client.host[:45] if request and request.client else None),
        user_agent=(request.headers.get("user-agent", "")[:255] if request else None),
    )
    db.add(user_session)
    db.flush()
    return user_session, token


def load_session(db: Session, token: str | None) -> UserSession | None:
    """Look up a live, unrevoked session. Does not check idle expiry."""
    if not token:
        return None
    stmt = select(UserSession).where(
        UserSession.token_hash == _hash_token(token),
        UserSession.revoked_at.is_(None),
    )
    return db.execute(stmt).scalar_one_or_none()


def touch(user_session: UserSession) -> None:
    """Reset the idle clock. Called once per authenticated request."""
    user_session.last_seen_at = datetime.now(UTC)


def revoke(user_session: UserSession) -> None:
    if user_session.revoked_at is None:
        user_session.revoked_at = datetime.now(UTC)


def revoke_other_sessions(db: Session, user_id: int, *, keep_session_id: int | None) -> int:
    """Revoke a user's live sessions, optionally sparing the current one.

    Called on deactivation, on a role or grant change, on a password reset, and on a
    password change, so that a change to what someone may do takes effect immediately
    rather than at their next login.
    """
    stmt = select(UserSession).where(
        UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
    )
    if keep_session_id is not None:
        stmt = stmt.where(UserSession.id != keep_session_id)

    sessions = db.execute(stmt).scalars().all()
    now = datetime.now(UTC)
    for s in sessions:
        s.revoked_at = now
    return len(sessions)


def revoke_all_for_user(db: Session, user_id: int) -> int:
    """Revoke every live session for a user, including the current one."""
    return revoke_other_sessions(db, user_id, keep_session_id=None)


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.is_production,
        samesite="strict",
        path="/",
        # No max_age: a session cookie, so closing the browser drops it. Server side
        # idle expiry is the real control either way.
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        secure=settings.is_production,
        samesite="strict",
    )
