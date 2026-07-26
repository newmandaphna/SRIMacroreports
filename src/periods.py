"""Reporting-period boundaries (ASSUMPTIONS.md §2).

PERIOD = 'YYYY-MM' names a calendar month by date of service. Boundaries form a
half-open interval [start, next_month_start) so a session at 23:59:59.999 on the
last day of the month lands in exactly one period and is never double-counted.
Same-period-prior-year is the identically named month one year back; no weekday
alignment (calendar-effect normalization is handled separately, §12).
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from typing import List, Tuple

_PERIOD_RE = re.compile(r"^(\d{4})-(\d{2})$")

# Engine pay-period label: "YYYY-MM Period 1|2" (also tolerates "YYYY-MM P1").
# Reconciled with the compensation engine (ASSUMPTIONS §2): the atomic period is
# a semi-monthly pay period split at day 15; analytics reporting windows roll
# whole pay periods up to calendar months.
_PAY_PERIOD_RE = re.compile(r"^\s*(\d{4})-(\d{2})\s*(?:Period|P)\s*([12])\s*$", re.I)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_SPLIT_DAY = 15  # Period 1 = days 1..15 inclusive; Period 2 = 16..end


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


# --- Semi-monthly pay periods (the engine's atomic unit) --------------------

def half_for_day(day: int) -> int:
    """Pay-period half for a day-of-month: 1 for days 1..15, else 2."""
    return 1 if day <= _SPLIT_DAY else 2


def pay_period_label(year: int, month: int, half: int) -> str:
    """Canonical engine label, e.g. pay_period_label(2026, 6, 1) -> '2026-06 Period 1'."""
    if half not in (1, 2):
        raise PeriodError(f"pay-period half must be 1 or 2, got {half!r}")
    if not 1 <= month <= 12:
        raise PeriodError(f"month out of range: {month!r}")
    return f"{year:04d}-{month:02d} Period {half}"


def parse_pay_period_label(label: str) -> Tuple[int, int, int]:
    """'2026-06 Period 1' (or '2026-06 P1') -> (2026, 6, 1). Raises otherwise."""
    m = _PAY_PERIOD_RE.match(str(label))
    if not m:
        raise PeriodError(
            f"pay-period label must be 'YYYY-MM Period 1|2', got {label!r}"
        )
    year, month, half = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not 1 <= month <= 12:
        raise PeriodError(f"month out of range in label {label!r}")
    return year, month, half


def pay_period_window(label: str) -> Tuple[date, date]:
    """Inclusive date-of-service bounds [start, end] for a pay period.

    Matches the engine's inclusive date slice: Period 1 = the 1st..15th; Period 2
    = the 16th..last day of the month.
    """
    year, month, half = parse_pay_period_label(label)
    if half == 1:
        return date(year, month, 1), date(year, month, _SPLIT_DAY)
    last = calendar.monthrange(year, month)[1]
    return date(year, month, _SPLIT_DAY + 1), date(year, month, last)


def month_pay_periods(period: str) -> List[str]:
    """The two pay-period labels that compose a calendar month, in order."""
    year, month = parse_period(period)
    return [pay_period_label(year, month, 1), pay_period_label(year, month, 2)]


def month_of_pay_period(label: str) -> str:
    """'2026-06 Period 2' -> '2026-06'. The calendar month a pay period rolls up to."""
    year, month, _ = parse_pay_period_label(label)
    return f"{year:04d}-{month:02d}"


def pay_period_year(label: str) -> int:
    """Year a pay period belongs to, parsed from the label (engine history.py rule)."""
    year, _, _ = parse_pay_period_label(label)
    return year


def pay_period_from_dos(d: date) -> str:
    """The pay-period label a date of service falls in (day-15 split)."""
    return pay_period_label(d.year, d.month, half_for_day(d.day))


# --- Month arithmetic for comparison windows (Phase D) ----------------------

def shift_month(period: str, delta: int) -> str:
    """Shift a 'YYYY-MM' by whole months. shift_month('2026-07', -12) -> '2025-07'."""
    year, month = parse_period(period)
    total = year * 12 + (month - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def month_range(end_period: str, n_months: int) -> List[str]:
    """The n consecutive months ENDING at end_period, oldest first."""
    if n_months < 1:
        raise PeriodError(f"n_months must be >= 1, got {n_months!r}")
    return [shift_month(end_period, -i) for i in range(n_months - 1, -1, -1)]
