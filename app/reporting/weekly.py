"""Session counts per calendar week, over a window counted back from today.

Shared by the dedicated weekly counts page and the overview section, so "a week"
and "the average" can never mean different things on different pages.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.reporting import queries
from app.reporting.periods import Granularity, today_in, week_start

# The week windows offered as one-click choices. Any other count typed into the URL
# or the form works too, clamped to something renderable.
WEEK_WINDOW_CHOICES = (4, 8, 13, 26, 52)
DEFAULT_WEEK_WINDOW = 8
MAX_WEEK_WINDOW = 104


@dataclass
class WeekRow:
    """One calendar week in the weekly counts view."""

    start: date
    end: date
    sessions: int
    collected: Decimal
    in_progress: bool

    @property
    def label(self) -> str:
        """Chart axis label, matching format_period's week style."""
        return self.start.strftime("%-d %b")


@dataclass
class WeeklyCounts:
    week_count: int
    window_start: date
    window_end: date
    rows: list[WeekRow]
    total_sessions: int
    # Over completed weeks only. Including a Tuesday's worth of the current week
    # would drag the average down and read as a real decline. None until one week
    # has actually completed.
    average_per_week: Decimal | None


def parse_week_count(raw: str) -> int:
    """Anything unparseable falls back to the default rather than erroring,
    matching how the report pickers behave everywhere else."""
    try:
        count = int(raw)
    except (TypeError, ValueError):
        count = DEFAULT_WEEK_WINDOW
    return max(1, min(count, MAX_WEEK_WINDOW))


def weekly_counts(
    db: Session,
    filters: queries.Filters,
    *,
    week_count: int,
    timezone: str,
    week_starts_monday: bool,
) -> WeeklyCounts:
    """The last `week_count` calendar weeks, ending with the current one.

    The window deliberately ignores the report date range picker: "the last N
    weeks" is anchored to today, whatever period the rest of the page shows.
    Therapist and location filters do apply.
    """
    today = today_in(timezone)
    this_week = week_start(today, week_starts_monday)
    window_start = this_week - timedelta(weeks=week_count - 1)

    points = queries.by_period(
        db,
        filters.replaced(start=window_start, end=today),
        Granularity.WEEK,
        week_starts_monday=week_starts_monday,
    )

    rows = [
        WeekRow(
            start=p.start,
            end=p.start + timedelta(days=6),
            sessions=p.sessions,
            collected=p.collected,
            in_progress=today < p.start + timedelta(days=6),
        )
        for p in points
    ]

    completed = [w for w in rows if not w.in_progress]
    average = (
        (Decimal(sum(w.sessions for w in completed)) / len(completed)).quantize(Decimal("0.1"))
        if completed
        else None
    )

    return WeeklyCounts(
        week_count=week_count,
        window_start=window_start,
        window_end=today,
        rows=rows,
        total_sessions=sum(w.sessions for w in rows),
        average_per_week=average,
    )
