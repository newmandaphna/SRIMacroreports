"""One imported row per patient visit.

The domain calls this a session, and the table is `sessions` accordingly. The Python
class is `Visit` because `Session` would collide with SQLAlchemy's Session and with
`UserSession`, and three different things called Session in one codebase is how
someone eventually authenticates against a therapy appointment.

This is the single imported dataset every module derives from. There is no separate
ingestion per module.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.therapist import Therapist
from app.models.types import Money, UTCDateTime, utcnow


class Visit(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        # The upsert key. Two deliberate choices in it.
        #
        # Patient name rather than patient code, because Patient Code is blank on 41
        # percent of the real sheet and the specified key collides on 2,467 rows
        # there. See ASSUMPTIONS.md A-020 for the measurement.
        #
        # And NOT source_id. A visit is the same visit whichever quarterly sheet it
        # arrives on, so identity is global. With source_id in the key, a visit
        # appearing in both the Q2 and Q3 sheets, which the practice's own rolling
        # export window makes likely at a quarter boundary, stored twice and was
        # counted twice in every figure. See ASSUMPTIONS.md A-022.
        UniqueConstraint(
            "therapist_id",
            "patient_name_normalized",
            "dos",
            "cpt",
            name="uq_visit_identity",
        ),
        Index("ix_visit_dos", "dos"),
        Index("ix_visit_therapist_dos", "therapist_id", "dos"),
        Index("ix_visit_cpt_base", "cpt_base"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Which source supplied this row. Informational rather than part of identity,
    # since identity is global (A-022).
    source_id: Mapped[int] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Sheet row number, so a row can be found again in the source.
    source_row_ref: Mapped[str | None] = mapped_column(String(40), nullable=True)

    therapist_id: Mapped[int] = mapped_column(
        ForeignKey("therapists.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    # PHI. Only ever selected by the Phase 6 patient funnel; every other module's
    # queries are aggregate and never touch these two columns.
    patient_name: Mapped[str] = mapped_column(String(200), nullable=False)
    patient_name_normalized: Mapped[str] = mapped_column(String(200), nullable=False)
    patient_code: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    dos: Mapped[date] = mapped_column(nullable=False)

    # `cpt` is the normalized value as written; `cpt_base` is the leading numeric code
    # when there is one, so 90791-ADHD and 90837 - ADHD group with 90791 and 90837
    # instead of splintering into their own report lines. See ASSUMPTIONS.md A-033.
    cpt: Mapped[str] = mapped_column(String(40), nullable=False)
    cpt_base: Mapped[str] = mapped_column(String(40), nullable=False)

    insurance_short: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    location_short: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    note_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    recorded_flag: Mapped[str | None] = mapped_column(String(40), nullable=True)

    due_from_pt: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    paid_by_pt: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    pt_amount_due: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    due_from_ins: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    paid_by_ins: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    ins_balance: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    total_due: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    total_paid: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    total_balance: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)

    imported_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    last_sync_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="SET NULL"), nullable=True
    )

    therapist: Mapped[Therapist] = relationship(lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        # No patient identity in a repr: reprs end up in logs and tracebacks.
        return f"Visit(id={self.id}, dos={self.dos}, cpt={self.cpt!r})"

    # The comparable payload, used to decide insert vs update vs unchanged so that a
    # re-sync of identical data does not churn updated_at on nine thousand rows.
    COMPARED_FIELDS = (
        "patient_code",
        "cpt_base",
        "insurance_short",
        "location_short",
        "note_code",
        "recorded_flag",
        "due_from_pt",
        "paid_by_pt",
        "pt_amount_due",
        "due_from_ins",
        "paid_by_ins",
        "ins_balance",
        "total_due",
        "total_paid",
        "total_balance",
        "source_row_ref",
    )

    def differs_from(self, values: dict[str, object]) -> bool:
        return any(getattr(self, field) != values.get(field) for field in self.COMPARED_FIELDS)

    def apply(self, values: dict[str, object]) -> None:
        for field in self.COMPARED_FIELDS:
            setattr(self, field, values.get(field))
