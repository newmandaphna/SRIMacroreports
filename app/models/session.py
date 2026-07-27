"""Server side sessions.

The cookie carries an opaque random token and nothing else: no user id, no role claim,
no JWT the client could tamper with. Everything authoritative lives in this table.

Only a hash of the token is stored, so a database read does not hand an attacker a set
of live session cookies.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.types import UTCDateTime, utcnow
from app.models.user import User


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Session bound CSRF token. Bound to the session rather than double submitted, so
    # a subdomain that can write cookies still cannot forge a request.
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # Recorded for the audit trail, not used for authorization: pinning a session to
    # an IP breaks legitimate users on mobile networks.
    source_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    is_admin_elevated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    user: Mapped[User] = relationship(lazy="selectin")

    def is_idle_expired(self, timeout_minutes: int, now: datetime | None = None) -> bool:
        """Idle expiry, evaluated on the server.

        A client that never fires its warning timer still gets logged out, because
        this is what every authenticated request is checked against.
        """
        now = now or utcnow()
        return now - self.last_seen_at > timedelta(minutes=timeout_minutes)

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def seconds_until_idle_expiry(self, timeout_minutes: int, now: datetime | None = None) -> int:
        now = now or utcnow()
        deadline = self.last_seen_at + timedelta(minutes=timeout_minutes)
        return max(0, int((deadline - now).total_seconds()))
