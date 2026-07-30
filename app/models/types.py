"""Column types shared across models: UTC timestamps and exact money.

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
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any

from sqlalchemy import DateTime, Integer
from sqlalchemy import Enum as SAEnum
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
            # a genuine local time. Stamped as UTC, not merely described that way: it
            # used to be passed through untouched, and a naive value handed to a
            # timestamptz column is interpreted with the database session's own
            # TimeZone, so the stored instant moved by whatever that happened to be.
            value = value.replace(tzinfo=UTC)
        value = value.astimezone(UTC)
        if dialect.name == "sqlite":
            # SQLite has no native timestamp type and hands back a naive value, so the
            # offset is dropped here on purpose and re-attached on the way out. Every
            # other dialect keeps the aware value, which leaves no room for the session
            # TimeZone to reinterpret it.
            return value.replace(tzinfo=None)
        return value

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def utcnow() -> datetime:
    """The one source of "now" for the model layer. Always aware, always UTC."""
    return datetime.now(UTC)


class Money(TypeDecorator):
    """Currency stored as integer cents, returned as Decimal.

    Money must not be stored as a float. SQLite has no DECIMAL type, so
    SQLAlchemy's Numeric falls back to float there, and summing nine thousand
    floats drifts. The sheet is full of values like 156.05 that have no exact
    binary representation, and a revenue figure that is wrong in the cents is a
    revenue figure nobody trusts.

    Integers are exact, and they port unchanged to PostgreSQL. Callers work in
    Decimal and never see the cents representation.
    """

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> int | None:
        if value is None:
            return None
        if isinstance(value, int) and not isinstance(value, bool):
            value = Decimal(value)
        elif isinstance(value, float):
            # Tolerated, but quantized immediately so the float never reaches storage.
            value = Decimal(str(value))
        elif isinstance(value, str):
            value = Decimal(value)
        return int(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        return (Decimal(value) / 100).quantize(Decimal("0.01"))


ZERO = Decimal("0.00")


def enum_column(enum_class: type[Enum], *, length: int = 40) -> SAEnum:
    """A VARCHAR column that round trips a StrEnum as the enum, not as a string.

    Declaring an enum column as plain String stores the value correctly but hands
    back a bare `str` on read. Anything then reaching for `.value`, `.label`, or a
    property on the member gets an AttributeError, and Jinja swallows that into an
    empty string, so a template quietly renders nothing and a status badge silently
    shows the wrong state. Better to have the type do the conversion.

    `values_callable` stores the member's value rather than its name, so the column
    holds "dry_run" and not "DRY_RUN", and `native_enum=False` keeps it a VARCHAR with
    no database level enum type to migrate.
    """
    return SAEnum(
        enum_class,
        native_enum=False,
        length=length,
        values_callable=lambda e: [member.value for member in e],
        validate_strings=True,
    )
