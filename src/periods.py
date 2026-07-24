"""Reporting-period boundaries (ASSUMPTIONS.md §2).

PERIOD = 'YYYY-MM' names a calendar month by date of service. Boundaries form a
half-open interval [start, next_month_start) so a session at 23:59:59.999 on the
last day of the month lands in exactly one period and is never double-counted.
Same-period-prior-year is the identically named month one year back; no weekday
alignment (calendar-effect normalization is handled separately, §12).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Tuple

_PERIOD_RE = re.compile(r"^(\d{4})-(\d{2})$")


class PeriodError(ValueError):
    """A period string was not a valid 'YYYY-MM'."""


def parse_period(period: str) -> Tuple[int, int]:
    """Return (year, month) for a 'YYYY-MM' string; raise PeriodError otherwise."""
    m = _PERIOD_RE.match(str(period).strip())
    if not m:
        raise PeriodError(f"period must be 'YYYY-MM', got {period!r}")
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        raise PeriodError(f"month out of range in period {period!r}")
    return year, month


def period_bounds(period: str) -> Tuple[datetime, datetime]:
    """Return (start, next_start) naive datetimes for the half-open interval."""
    year, month = parse_period(period)
    start = datetime(year, month, 1)
    if month == 12:
        next_start = datetime(year + 1, 1, 1)
    else:
        next_start = datetime(year, month + 1, 1)
    return start, next_start


def in_period(dt: datetime, period: str) -> bool:
    """True iff dt lands in the period, using the half-open interval."""
    start, next_start = period_bounds(period)
    return start <= dt < next_start


def same_period_prior_year(period: str) -> str:
    """'2026-07' -> '2025-07'. The identically named month one year back."""
    year, month = parse_period(period)
    return f"{year - 1:04d}-{month:02d}"


def prior_year_floor(period: str) -> datetime:
    """Start datetime of the target period shifted back exactly one year.

    Prior-year coverage requires at least this much date-of-service history before
    the target period (ASSUMPTIONS / data-dictionary cross-cutting requirement).
    """
    year, month = parse_period(period)
    return datetime(year - 1, month, 1)
