"""Period boundaries are half-open and never double-count (ASSUMPTIONS §2)."""
from datetime import datetime, timedelta

import pytest

from src.periods import (
    PeriodError,
    in_period,
    parse_period,
    period_bounds,
    prior_year_floor,
    same_period_prior_year,
)


def test_parse_and_validate():
    assert parse_period("2026-07") == (2026, 7)
    for bad in ("2026-13", "2026/07", "26-7", "2026-00", "nope"):
        with pytest.raises(PeriodError):
            parse_period(bad)


def test_bounds_are_half_open():
    start, nxt = period_bounds("2026-07")
    assert start == datetime(2026, 7, 1)
    assert nxt == datetime(2026, 8, 1)


def test_december_rolls_year():
    start, nxt = period_bounds("2026-12")
    assert start == datetime(2026, 12, 1)
    assert nxt == datetime(2027, 1, 1)


def test_last_instant_lands_in_exactly_one_period():
    last = datetime(2026, 7, 31, 23, 59, 59)
    assert in_period(last, "2026-07")
    assert not in_period(last, "2026-08")
    first_next = datetime(2026, 8, 1, 0, 0, 0)
    assert not in_period(first_next, "2026-07")
    assert in_period(first_next, "2026-08")


def test_no_datetime_belongs_to_two_periods():
    # Property-ish sweep across a boundary at minute granularity.
    t = datetime(2026, 7, 31, 23, 0, 0)
    end = datetime(2026, 8, 1, 1, 0, 0)
    while t < end:
        hits = [p for p in ("2026-07", "2026-08") if in_period(t, p)]
        assert len(hits) == 1, (t, hits)
        t += timedelta(minutes=1)


def test_same_period_prior_year_and_floor():
    assert same_period_prior_year("2026-07") == "2025-07"
    assert same_period_prior_year("2026-01") == "2025-01"
    assert prior_year_floor("2026-07") == datetime(2025, 7, 1)
