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


class EmploymentType(StrEnum):
    SALARIED_BENEFITS = "salaried_benefits"
    PERCENTAGE_LEGACY = "percentage_legacy"
    OTHER = "other"

    @property
    def label(self) -> str:
        return {
            EmploymentType.SALARIED_BENEFITS: "Salaried with benefits",
            EmploymentType.PERCENTAGE_LEGACY: "Percentage (legacy)",
            EmploymentType.OTHER: "Other",
        }[self]

    @property
    def counts_against_threshold(self) -> bool:
        """Only salaried therapists are measured against the benefits threshold.

        A percentage based therapist has no session minimum to meet, so showing them
        as "below threshold" would be a false alarm about someone's work.
        """
        return self is EmploymentType.SALARIED_BENEFITS


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
