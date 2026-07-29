"""Therapists and the aliases that resolve to them.

The Q sheet writes therapists as surname capitals (ALEXANDER, HARRIS). Valant writes
them as "Andrea Harris, LMFT (A.Harris)". Both must land on one therapist record.

Aliases match the whole normalized name, never a substring. See ASSUMPTIONS.md A-040a:
the practice's own Config tab uses "contains" semantics, and a rule written as just
`Rosenfeld` would fold Inna Pavlova-Rosenfeld and the unrelated therapist ROSENFELD
into one record, silently corrupting both of their utilization figures. They are
confirmed to be different people.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.types import UTCDateTime, enum_column, utcnow

# Part time is a band, not a floor: fewer than PART_TIME_MIN a week is below, and
# at or over PART_TIME_MAX the arrangement itself is being exceeded, which is a
# different conversation from underperformance. Confirmed with the practice.
PART_TIME_MIN = 5
PART_TIME_MAX = 20

# Full time without benefits carries a fixed expectation, per the practice.
FULL_TIME_NO_BENEFITS_EXPECTED = 25


class EmploymentType(StrEnum):
    SALARIED_BENEFITS = "salaried_benefits"
    FULL_TIME_NO_BENEFITS = "full_time_no_benefits"
    PART_TIME = "part_time"
    PERCENTAGE_LEGACY = "percentage_legacy"
    OTHER = "other"

    @property
    def label(self) -> str:
        return {
            EmploymentType.SALARIED_BENEFITS: "Full time with benefits",
            EmploymentType.FULL_TIME_NO_BENEFITS: "Full time, no benefits",
            EmploymentType.PART_TIME: "Part time",
            EmploymentType.PERCENTAGE_LEGACY: "Percentage (legacy)",
            EmploymentType.OTHER: "Other",
        }[self]

    @property
    def counts_against_threshold(self) -> bool:
        """Whether this arrangement carries a session expectation at all.

        A percentage based therapist has no session minimum to meet, so showing them
        as "below threshold" would be a false alarm about someone's work.
        """
        return self in (
            EmploymentType.SALARIED_BENEFITS,
            EmploymentType.FULL_TIME_NO_BENEFITS,
            EmploymentType.PART_TIME,
        )

    @property
    def expectation_note(self) -> str:
        """What the form shows next to each choice."""
        return {
            EmploymentType.SALARIED_BENEFITS: (
                "Measured against the with-benefits expectation (default 30 a week, "
                "set in Settings)."
            ),
            EmploymentType.FULL_TIME_NO_BENEFITS: (
                f"Measured against {FULL_TIME_NO_BENEFITS_EXPECTED} sessions a week."
            ),
            EmploymentType.PART_TIME: (
                f"Expected between {PART_TIME_MIN} and {PART_TIME_MAX} sessions a week; "
                f"{PART_TIME_MAX} or more flags the arrangement, not the person."
            ),
            EmploymentType.PERCENTAGE_LEGACY: (
                "No session threshold, so no below threshold status is ever shown."
            ),
            EmploymentType.OTHER: (
                "No session threshold, so no below threshold status is ever shown."
            ),
        }[self]


class Discipline(StrEnum):
    THERAPIST = "therapist"
    PSYCHIATRIST = "psychiatrist"

    @property
    def label(self) -> str:
        return {
            Discipline.THERAPIST: "Therapist",
            Discipline.PSYCHIATRIST: "Psychiatrist",
        }[self]


def normalize_therapist_name(raw: str | None) -> str:
    """Uppercase, collapse whitespace, strip punctuation noise.

    Deliberately conservative: it does not strip credentials or parenthetical login
    names, because doing so would make two different people look identical more often
    than it would match one person to themselves.
    """
    if not raw:
        return ""
    text = re.sub(r"\s+", " ", str(raw)).strip().upper()
    return text.strip(" .,;:")


class Therapist(Base):
    __tablename__ = "therapists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    employment_type: Mapped[EmploymentType] = mapped_column(
        enum_column(EmploymentType, length=30), nullable=False, default=EmploymentType.OTHER
    )
    discipline: Mapped[Discipline] = mapped_column(
        enum_column(Discipline, length=20),
        nullable=False,
        default=Discipline.THERAPIST,
        server_default=Discipline.THERAPIST.value,
    )
    # Overrides the employment type's default expectation for this one person,
    # because expectations are individual agreements: one full timer may be on 25
    # while a colleague on insurance panels needs 30.
    weekly_expected_sessions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    aliases: Mapped[list[TherapistAlias]] = relationship(
        back_populates="therapist", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Therapist(id={self.id}, display_name={self.display_name!r})"

    @property
    def alias_values(self) -> set[str]:
        return {a.alias for a in self.aliases}


class AliasSource(StrEnum):
    SHEET_CONFIG_TAB = "sheet_config_tab"
    OBSERVED = "observed"
    MANUAL = "manual"


class TherapistAlias(Base):
    """One normalized string that resolves to exactly one therapist.

    The alias is globally unique, not unique per therapist. That constraint is the
    thing that makes a silent merge impossible: two therapists cannot both claim
    ROSENFELD, and an attempt to add a duplicate fails loudly at the database.
    """

    __tablename__ = "therapist_aliases"
    __table_args__ = (UniqueConstraint("alias", name="uq_therapist_alias"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    therapist_id: Mapped[int] = mapped_column(
        ForeignKey("therapists.id", ondelete="CASCADE"), nullable=False, index=True
    )
    alias: Mapped[str] = mapped_column(String(120), nullable=False)
    source: Mapped[AliasSource] = mapped_column(
        enum_column(AliasSource, length=30), nullable=False, default=AliasSource.MANUAL
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )

    therapist: Mapped[Therapist] = relationship(
        back_populates="aliases", foreign_keys=[therapist_id]
    )
