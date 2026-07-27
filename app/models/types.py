"""Column types shared across models.

SQLite has no native timestamp type: SQLAlchemy stores a string and hands it back as a
naive datetime, so `DateTime(timezone=True)` silently loses the offset on a round trip.
Comparing a value read back from the database against an aware `datetime.now(UTC)`
then raises, and, worse, a comparison that happens to involve two naive values would
quietly be wrong by whatever the offset was.

UTCDateTime makes the contract explicit: everything is stored as UTC and everything
comes back aware. Under PostgreSQL the driver already returns aware values and this
becomes a no op, so the same models port without change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """A timezone aware datetime that is always UTC on the way in and out."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # Naive input is taken as UTC rather than rejected: the alternative is a
            # 500 on a code path that is almost always a missed `tz=UTC` rather than
            # a genuine local time.
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def utcnow() -> datetime:
    """The one source of "now" for the model layer. Always aware, always UTC."""
    return datetime.now(UTC)
