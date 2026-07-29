"""Year over year comparison: this period against the same period last year.

Seasonal slumps only show up against the same season, not against last month, so
each row compares a period to its own shadow twelve months earlier. Rows whose
last-year window holds no data say so instead of showing a fake zero, and point at
the historical upload as the way to light them up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config_store import PracticeConfig
from app.reporting import queries
from app.reporting.periods import month_start, quarter_start, today_in

ZERO = Decimal(0)


@dataclass
class YoYRow:
    label: str
    current_span: str
    previous_span: str
    current: queries.Totals
    previous: queries.Totals
    has_comparison: bool
    sessions_change: Decimal | None
    collected_change: Decimal | None


def _last_year(day: date) -> date:
    """The same calendar date a year earlier. 29 Feb maps to 28 Feb."""
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=28)


def _pct(current, previous) -> Decimal | None:
    previous = Decimal(str(previous))
    if previous <= ZERO:
        return None
    return ((Decimal(str(current)) - previous) / previous * 100).quantize(Decimal("0.1"))


def _span(start: date, end: date) -> str:
    return f"{start.strftime('%-d %b %Y')} to {end.strftime('%-d %b %Y')}"


def _month_label(day: date) -> str:
    return day.strftime("%B %Y")


def _quarter_label(day: date) -> str:
    return f"Q{(day.month - 1) // 3 + 1} {day.year}"


def year_over_year(
    db: Session,
    *,
    config: PracticeConfig,
    cpt_exclusions: tuple[str, ...],
) -> list[YoYRow]:
    today = today_in(config.timezone)

    this_month = month_start(today)
    last_month_end = this_month - timedelta(days=1)
    last_month = month_start(last_month_end)
    this_quarter = quarter_start(today)
    last_quarter_end = this_quarter - timedelta(days=1)
    last_quarter = quarter_start(last_quarter_end)

    periods: list[tuple[str, date, date]] = [
        (f"{_month_label(today)}, month to date", this_month, today),
        (f"{_month_label(last_month)}, full month", last_month, last_month_end),
        (f"{_quarter_label(today)}, quarter to date", this_quarter, today),
        (f"{_quarter_label(last_quarter)}, full quarter", last_quarter, last_quarter_end),
        (f"{today.year} year to date", date(today.year, 1, 1), today),
    ]

    rows: list[YoYRow] = []
    for label, start, end in periods:
        prev_start, prev_end = _last_year(start), _last_year(end)
        current = queries.totals(
            db, queries.Filters(start=start, end=end, cpt_exclusions=cpt_exclusions)
        )
        previous = queries.totals(
            db, queries.Filters(start=prev_start, end=prev_end, cpt_exclusions=cpt_exclusions)
        )
        has_comparison = current.visits > 0 and previous.visits > 0
        rows.append(
            YoYRow(
                label=label,
                current_span=_span(start, end),
                previous_span=_span(prev_start, prev_end),
                current=current,
                previous=previous,
                has_comparison=has_comparison,
                sessions_change=(
                    _pct(current.sessions, previous.sessions) if has_comparison else None
                ),
                collected_change=(
                    _pct(current.collected, previous.collected) if has_comparison else None
                ),
            )
        )
    return rows
