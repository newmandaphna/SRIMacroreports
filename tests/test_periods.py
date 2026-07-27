"""Period boundaries and the date range picker."""

from __future__ import annotations

from datetime import date

import pytest

from app.reporting.periods import (
    DateRange,
    Granularity,
    RangePreset,
    format_period,
    month_start,
    period_series,
    period_start,
    quarter_start,
    resolve_range,
    week_start,
)


class TestBoundaries:
    @pytest.mark.parametrize(
        ("day", "expected"),
        [
            (date(2026, 4, 6), date(2026, 4, 6)),  # a Monday
            (date(2026, 4, 8), date(2026, 4, 6)),  # midweek
            (date(2026, 4, 12), date(2026, 4, 6)),  # the Sunday belongs to that week
        ],
    )
    def test_week_starts_monday(self, day, expected):
        assert week_start(day) == expected

    def test_week_can_start_sunday(self):
        assert week_start(date(2026, 4, 8), week_starts_monday=False) == date(2026, 4, 5)

    def test_month_and_quarter(self):
        assert month_start(date(2026, 5, 17)) == date(2026, 5, 1)
        assert quarter_start(date(2026, 5, 17)) == date(2026, 4, 1)
        assert quarter_start(date(2026, 1, 1)) == date(2026, 1, 1)
        assert quarter_start(date(2026, 12, 31)) == date(2026, 10, 1)

    def test_period_start_dispatches(self):
        day = date(2026, 5, 17)
        assert period_start(day, Granularity.WEEK) == date(2026, 5, 11)
        assert period_start(day, Granularity.MONTH) == date(2026, 5, 1)
        assert period_start(day, Granularity.QUARTER) == date(2026, 4, 1)


class TestLabels:
    def test_weeks_are_labelled_by_their_monday(self):
        """A person can find "6 Apr" in a calendar. They cannot find "week 15"."""
        assert format_period(date(2026, 4, 6), Granularity.WEEK) == "6 Apr"

    def test_month_and_quarter_labels(self):
        assert format_period(date(2026, 5, 1), Granularity.MONTH) == "May 2026"
        assert format_period(date(2026, 4, 1), Granularity.QUARTER) == "Q2 2026"
        assert format_period(date(2026, 10, 1), Granularity.QUARTER) == "Q4 2026"


class TestSeries:
    def test_series_is_continuous(self):
        series = period_series(date(2026, 4, 1), date(2026, 4, 30), Granularity.WEEK)
        assert series == [
            date(2026, 3, 30),
            date(2026, 4, 6),
            date(2026, 4, 13),
            date(2026, 4, 20),
            date(2026, 4, 27),
        ]

    def test_single_period(self):
        series = period_series(date(2026, 4, 6), date(2026, 4, 8), Granularity.WEEK)
        assert series == [date(2026, 4, 6)]

    def test_quarter_series_crosses_a_year(self):
        series = period_series(date(2026, 10, 1), date(2027, 3, 31), Granularity.QUARTER)
        assert series == [date(2026, 10, 1), date(2027, 1, 1)]

    def test_reversed_range_is_empty(self):
        assert period_series(date(2026, 5, 1), date(2026, 4, 1), Granularity.WEEK) == []


class TestResolveRange:
    def test_custom_range(self):
        result = resolve_range("custom", "2026-04-01", "2026-06-30")
        assert result.start == date(2026, 4, 1)
        assert result.end == date(2026, 6, 30)
        assert result.preset is RangePreset.CUSTOM

    def test_reversed_custom_range_is_swapped_not_rejected(self):
        result = resolve_range("custom", "2026-06-30", "2026-04-01")
        assert result.start == date(2026, 4, 1)
        assert result.end == date(2026, 6, 30)

    def test_unparseable_input_falls_back_rather_than_erroring(self):
        """A mistyped URL should show a dashboard, not a stack trace."""
        assert resolve_range("not-a-preset").preset is RangePreset.LAST_4_WEEKS
        assert resolve_range("custom", "nonsense", "").preset is RangePreset.LAST_4_WEEKS

    def test_all_time_uses_the_data_it_has(self):
        result = resolve_range("all_time", data_min=date(2026, 4, 1), data_max=date(2026, 6, 30))
        assert result.start == date(2026, 4, 1)
        assert result.end == date(2026, 6, 30)

    def test_this_week_starts_on_monday(self):
        result = resolve_range("this_week")
        assert result.start.weekday() == 0


class TestComparisonWindow:
    def test_previous_window_is_the_same_length(self):
        """Like for like, so a delta is not just the shape of the calendar."""
        current = DateRange(date(2026, 4, 6), date(2026, 4, 12), RangePreset.THIS_WEEK, "This week")
        previous = current.previous()
        assert previous.start == date(2026, 3, 30)
        assert previous.end == date(2026, 4, 5)
        assert previous.days == current.days == 7

    def test_previous_window_of_a_partial_period(self):
        current = DateRange(date(2026, 4, 1), date(2026, 4, 3), RangePreset.QUARTER_TO_DATE, "QTD")
        previous = current.previous()
        assert previous.days == 3
        assert previous.end == date(2026, 3, 31)


class TestGranularitySelection:
    @pytest.mark.parametrize(
        ("days", "expected"),
        [
            (7, Granularity.WEEK),
            (90, Granularity.WEEK),
            (200, Granularity.MONTH),
            (1500, Granularity.QUARTER),
        ],
    )
    def test_bucket_size_scales_with_the_range(self, days, expected):
        from datetime import timedelta

        start = date(2026, 1, 1)
        rng = DateRange(start, start + timedelta(days=days - 1), RangePreset.CUSTOM, "x")
        assert rng.suggested_granularity() is expected
