"""The audit log.

Append only, enforced in code and not merely by convention. There is no update path
and no delete path: the ORM events below raise if anything tries. Admins read the log
and export it. Nobody edits it. Retention is 6 years, met by never deleting.

The log itself contains no PHI (SECURITY.md section 4). A patient level view read is
recorded as the view name plus its filter parameters, never as the rows returned.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text, event
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base
from app.models.enums import AuditAction, AuditResult
from app.models.types import UTCDateTime, enum_column, utcnow


class AuditLogImmutableError(RuntimeError):
    """Raised on any attempt to modify or remove an audit record."""


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    occurred_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=utcnow,
        index=True,
    )

    # Null actor with an attempted identifier covers a failed login for an address
    # that does not resolve to a user.
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    actor_label: Mapped[str] = mapped_column(String(320), nullable=False)

    action: Mapped[AuditAction] = mapped_column(
        enum_column(AuditAction), nullable=False, index=True
    )
    result: Mapped[AuditResult] = mapped_column(enum_column(AuditResult, length=10), nullable=False)

    target_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(60), nullable=True)

    source_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    # Scrubbed before it gets here. Filter parameters and counts, never patient rows.
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"AuditLog(id={self.id}, action={self.action}, result={self.result})"


def _reject(_mapper: object, _connection: object, target: AuditLog) -> None:
    raise AuditLogImmutableError(
        "The audit log is append only. Records cannot be updated or deleted "
        "(HIPAA 164.312(b), SECURITY.md section 4)."
    )


event.listen(AuditLog, "before_update", _reject, propagate=True)
event.listen(AuditLog, "before_delete", _reject, propagate=True)


@event.listens_for(Session, "before_flush")
def _block_bulk_audit_mutation(session: Session, _ctx: object, _instances: object) -> None:
    """Catch what the mapper level events cannot see.

    The before_update and before_delete events fire per instance. A session that has
    marked an AuditLog dirty or deleted is caught here first, with a clearer error,
    before SQLAlchemy gets as far as emitting anything.
    """
    for obj in session.dirty:
        if isinstance(obj, AuditLog) and session.is_modified(obj):
            raise AuditLogImmutableError(
                "The audit log is append only. An audit record was modified in this "
                "session (HIPAA 164.312(b), SECURITY.md section 4)."
            )
    for obj in session.deleted:
        if isinstance(obj, AuditLog):
            raise AuditLogImmutableError(
                "The audit log is append only. An audit record was deleted in this "
                "session (HIPAA 164.312(b), SECURITY.md section 4)."
            )
