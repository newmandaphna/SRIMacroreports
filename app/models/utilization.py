"""Notes that travel with a utilization number.

A session count on its own invites a conclusion about a person's work. "Referral
shortage", "on leave the second week", "three days a week by agreement" are the
difference between a number that informs and a number that misleads, so the note is
part of the record rather than something said in a meeting and forgotten.

One note per therapist per period, editable. Every change is audit logged with its
before and after, so the history is recoverable even though the note itself is not a
thread.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.therapist import Therapist
from app.models.types import UTCDateTime, enum_column, utcnow
from app.reporting.periods import Granularity


class UtilizationNote(Base):
    __tablename__ = "utilization_notes"
    __table_args__ = (
        UniqueConstraint("therapist_id", "period_start", "granularity", name="uq_utilization_note"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    therapist_id: Mapped[int] = mapped_column(
        ForeignKey("therapists.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # The period the note is about, identified the same way the reports identify it.
    period_start: Mapped[date] = mapped_column(nullable=False, index=True)
    granularity: Mapped[Granularity] = mapped_column(
        enum_column(Granularity, length=20), nullable=False, default=Granularity.WEEK
    )

    body: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    updated_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )

    therapist: Mapped[Therapist] = relationship(lazy="selectin")

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"UtilizationNote(therapist_id={self.therapist_id}, period_start={self.period_start})"
        )
