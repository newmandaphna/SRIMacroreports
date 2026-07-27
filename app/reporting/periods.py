"""Weeks, months, quarters, and the date range picker.

Every period boundary in the application comes from here, so that a week means the
same thing on the overview, in the financial module, and in a CSV export.

Weeks start Monday and are labelled by their Monday date (ASSUMPTIONS.md A-036).
"Today" is evaluated in the practice's timezone, not the server's: a report run at
9pm Eastern must not already be showing tomorrow because the container runs in UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo


class Granularity(StrEnum):
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"

    @property
    def label(self) -> str:
        return {
            Granularity.WEEK: "Weekly",
            Granularity.MONTH: "Monthly",
            Granularity.QUARTER: "Quarterly",
        }[self]


class RangePreset(StrEnum):
    THIS_WEEK = "this_week"
    LAST_4_WEEKS = "last_4_weeks"
    LAST_12_WEEKS = "last_12_weeks"
    MONTH_TO_DATE = "month_to_date"
    QUARTER_TO_DATE = "quarter_to_date"
    YEAR_TO_DATE = "year_to_date"
    ALL_TIME = "all_time"
    CUSTOM = "custom"

    @property
    def label(self) -> str:
        return {
            RangePreset.THIS_WEEK: "This week",
            RangePreset.LAST_4_WEEKS: "Last 4 weeks",
            RangePreset.LAST_12_WEEKS: "Last 12 weeks",
            RangePreset.MONTH_TO_DATE: "Month to date",
            RangePreset.QUARTER_TO_DATE: "Quarter to date",
            RangePreset.YEAR_TO_DATE: "Year to date",
            RangePreset.ALL_TIME: "All time",
            RangePreset.CUSTOM: "Custom",
        }[self]


# Presets offered in the picker, in the order they appear.
PICKER_PRESETS: tuple[RangePreset, ...] = (
    RangePreset.THIS_WEEK,
    RangePreset.LAST_4_WEEKS,
    RangePreset.LAST_12_WEEKS,
    RangePreset.MONTH_TO_DATE,
    RangePreset.QUARTER_TO_DATE,
    RangePreset.YEAR_TO_DATE,
    RangePreset.ALL_TIME,
)

DEFAULT_PRESET = RangePreset.LAST_4_WEEKS

# The earliest date the app will treat as real. Guards an "all time" range against a
# stray 1899 date turning every chart into one spike at the right hand edge.
FLOOR_DATE = date(2000, 1, 1)


def today_in(timezone: str) -> date:
    """The current date in the practice's timezone."""
    from datetime import datetime

    return datetime.now(ZoneInfo(timezone)).date()


def week_start(day: date, week_starts_monday: bool = True) -> date:
    """The Monday (or Sunday) on or before `day`."""
    if week_starts_monday:
        return day - timedelta(days=day.weekday())
    return day - timedelta(days=(day.weekday() + 1) % 7)


def month_start(day: date) -> date:
    return day.replace(day=1)


def quarter_start(day: date) -> date:
    first_month = 3 * ((day.month - 1) // 3) + 1
    return date(day.year, first_month, 1)


def period_start(day: date, granularity: Granularity, *, week_starts_monday: bool = True) -> date:
    if granularity is Granularity.WEEK:
        return week_start(day, week_starts_monday)
    if granularity is Granularity.MONTH:
        return month_start(day)
    return quarter_start(day)


def next_period(start: date, granularity: Granularity) -> date:
    if granularity is Granularity.WEEK:
        return start + timedelta(days=7)
    if granularity is Granularity.MONTH:
        return (
            date(start.year + 1, 1, 1)
            if start.month == 12
            else date(start.year, start.month + 1, 1)
        )
    return date(start.year + 1, 1, 1) if start.month >= 10 else date(start.year, start.month + 3, 1)


def format_period(start: date, granularity: Granularity) -> str:
    """Axis and table label for a period.

    Weeks are labelled by their Monday, per the build specification, because "week of
    13 Apr" is something a person can locate in a calendar and "week 16" is not.
    """
    if granularity is Granularity.WEEK:
        return start.strftime("%-d %b")
    if granularity is Granularity.MONTH:
        return start.strftime("%b %Y")
    return f"Q{(start.month - 1) // 3 + 1} {start.year}"


def period_series(
    start: date, end: date, granularity: Granularity, *, week_starts_monday: bool = True
) -> list[date]:
    """Every period start between start and end, inclusive of both ends' periods.

    Returns a continuous run. A week with no sessions has to appear as a zero rather
    than be missing, or a trend line silently closes the gap and reads as though
    nothing happened.
    """
    if end < start:
        return []
    cursor = period_start(start, granularity, week_starts_monday=week_starts_monday)
    last = period_start(end, granularity, week_starts_monday=week_starts_monday)
    out: list[date] = []
    # Bounded so a bad custom range cannot spin: 40 years of weeks.
    for _ in range(2200):
        out.append(cursor)
        if cursor >= last:
            break
        cursor = next_period(cursor, granularity)
    return out


@dataclass(frozen=True)
class DateRange:
    """A resolved, inclusive date range plus the label to show for it."""

    start: date
    end: date
    preset: RangePreset
    label: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def previous(self) -> DateRange:
        """The immediately preceding window of the same length, for deltas.

        Same length rather than "the previous calendar period", so a delta compares
        like with like. Comparing a three day quarter to date against a full previous
        quarter would show a collapse that is only the calendar.
        """
        length = self.days
        new_end = self.start - timedelta(days=1)
        new_start = new_end - timedelta(days=length - 1)
        return DateRange(
            start=new_start,
            end=new_end,
            preset=self.preset,
            label=f"previous {length} days",
        )

    def suggested_granularity(self) -> Granularity:
        """Pick a sensible bucket size so a chart has neither 2 bars nor 400."""
        if self.days <= 120:
            return Granularity.WEEK
        if self.days <= 800:
            return Granularity.MONTH
        return Granularity.QUARTER


def resolve_range(
    preset: str | None,
    start: str | date | None = None,
    end: str | date | None = None,
    *,
    timezone: str = "America/New_York",
    week_starts_monday: bool = True,
    data_min: date | None = None,
    data_max: date | None = None,
) -> DateRange:
    """Turn picker input into a concrete range.

    Anything unparseable falls back to the default preset rather than erroring: a
    mistyped URL should show a dashboard, not a stack trace.
    """
    now = today_in(timezone)

    try:
        chosen = RangePreset(preset) if preset else DEFAULT_PRESET
    except ValueError:
        chosen = DEFAULT_PRESET

    if chosen is RangePreset.CUSTOM:
        parsed_start = _coerce_date(start)
        parsed_end = _coerce_date(end)
        if parsed_start is None or parsed_end is None:
            chosen = DEFAULT_PRESET
        else:
            if parsed_end < parsed_start:
                parsed_start, parsed_end = parsed_end, parsed_start
            return DateRange(
                start=max(parsed_start, FLOOR_DATE),
                end=parsed_end,
                preset=RangePreset.CUSTOM,
                label=f"{parsed_start.isoformat()} to {parsed_end.isoformat()}",
            )

    this_week = week_start(now, week_starts_monday)

    if chosen is RangePreset.THIS_WEEK:
        return DateRange(this_week, now, chosen, chosen.label)
    if chosen is RangePreset.LAST_4_WEEKS:
        return DateRange(this_week - timedelta(weeks=3), now, chosen, chosen.label)
    if chosen is RangePreset.LAST_12_WEEKS:
        return DateRange(this_week - timedelta(weeks=11), now, chosen, chosen.label)
    if chosen is RangePreset.MONTH_TO_DATE:
        return DateRange(month_start(now), now, chosen, chosen.label)
    if chosen is RangePreset.QUARTER_TO_DATE:
        return DateRange(quarter_start(now), now, chosen, chosen.label)
    if chosen is RangePreset.YEAR_TO_DATE:
        return DateRange(date(now.year, 1, 1), now, chosen, chosen.label)

    # All time: the span of what has actually been imported, which is more useful
    # than an arbitrary floor and keeps charts to a readable number of buckets.
    return DateRange(
        start=data_min or FLOOR_DATE,
        end=data_max or now,
        preset=RangePreset.ALL_TIME,
        label=RangePreset.ALL_TIME.label,
    )


def _coerce_date(value: str | date | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None
