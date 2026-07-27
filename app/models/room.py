"""Rooms and their recorded usage.

The practice has schedules but no confirmed source of actual room usage, so this
module exists behind a feature flag that is off by default, and its only ingestion
path is a manual upload. Nothing here reads from the Q sheet, because the Q sheet does
not contain it.

Usage is recorded as slots rather than as hours, because "how many of the bookable
slots in this room on this day were actually used" is the question the practice asked,
and slot length varies by appointment type in a way that would make an hours figure
quietly wrong.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.types import UTCDateTime, enum_column, utcnow


class UsageSource(StrEnum):
    MANUAL_UPLOAD = "manual_upload"
    MANUAL_ENTRY = "manual_entry"

    @property
    def label(self) -> str:
        return {
            UsageSource.MANUAL_UPLOAD: "Uploaded",
            UsageSource.MANUAL_ENTRY: "Entered by hand",
        }[self]


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)

    # The same short location codes the sessions carry (1, 2, TH), so a room can be
    # tied back to a site without inventing a second vocabulary.
    location_short: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    # Bookable slots on a normal day. Used as the denominator when an uploaded row
    # does not carry its own, which is the common case for a fixed schedule.
    default_slots_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    usage: Mapped[list[RoomUsage]] = relationship(
        back_populates="room", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Room(id={self.id}, name={self.name!r})"


class RoomUsage(Base):
    """One room, one day, how many slots were used out of how many available."""

    __tablename__ = "room_usage"
    __table_args__ = (UniqueConstraint("room_id", "usage_date", name="uq_room_usage_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[int] = mapped_column(
        ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    usage_date: Mapped[date] = mapped_column(nullable=False, index=True)

    slots_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    slots_available: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    source: Mapped[UsageSource] = mapped_column(
        enum_column(UsageSource, length=30), nullable=False, default=UsageSource.MANUAL_UPLOAD
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    recorded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )

    room: Mapped[Room] = relationship(back_populates="usage")

    @property
    def rate(self) -> Decimal | None:
        """Used as a share of available. None when nothing was available.

        None rather than zero: a room that was closed had no utilization to report,
        and showing it as 0 percent would read as a room sitting empty all day.
        """
        if self.slots_available <= 0:
            return None
        return (Decimal(self.slots_used) / Decimal(self.slots_available) * 100).quantize(
            Decimal("0.1")
        )
