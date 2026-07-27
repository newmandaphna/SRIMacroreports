"""Users and their module grants.

Users are deactivated, never deleted, so that audit log entries always resolve to a
real identity. There is no delete path in the code or the UI.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import Module, Role
from app.models.types import UTCDateTime, utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Identity is the email address, lowercased. Unique per person: no shared or
    # generic accounts, ever (SECURITY.md section 3.1).
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)

    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    role: Mapped[Role] = mapped_column(String(20), nullable=False, default=Role.VIEWER)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Brute force resistance. Cleared on a successful login.
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # foreign_keys is required because ModuleGrant has two paths back to users:
    # the grantee (user_id) and the admin who granted it (granted_by_id).
    grants: Mapped[list[ModuleGrant]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
        foreign_keys="ModuleGrant.user_id",
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        # Never include the hash. Email is not PHI (staff, not patients) but the hash
        # has no business in a log line either way.
        return f"User(id={self.id}, role={self.role}, active={self.is_active})"

    @property
    def role_enum(self) -> Role:
        return Role(self.role)

    @property
    def granted_modules(self) -> set[Module]:
        return {Module(g.module) for g in self.grants}

    @property
    def is_locked(self) -> bool:
        return self.locked_until is not None and self.locked_until > utcnow()

    def has_grant(self, module: Module) -> bool:
        """Whether this user has been explicitly granted a module.

        Deliberately does NOT special case admins. Admin override is a separate,
        separately logged decision made in the dependency layer, so that break glass
        access is distinguishable from routine access in the audit log.
        """
        return module in self.granted_modules

    def can_view(self, module: Module) -> tuple[bool, bool]:
        """Return (allowed, is_emergency_access).

        An admin may reach any module. When they reach one they were not granted,
        that is emergency access and the caller must log it as such.
        """
        if not self.is_active:
            return (False, False)
        if self.has_grant(module):
            return (True, False)
        if self.role_enum.is_admin:
            return (True, True)
        return (False, False)


class ModuleGrant(Base):
    __tablename__ = "module_grants"
    __table_args__ = (UniqueConstraint("user_id", "module", name="uq_grant_user_module"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    module: Mapped[Module] = mapped_column(String(40), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    granted_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="grants", foreign_keys=[user_id])
