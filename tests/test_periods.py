"""Period boundaries are half-open and never double-count (ASSUMPTIONS §2)."""
from datetime import datetime, timedelta

import pytest

from datetime import date

from src.periods import (
    PeriodError,
    half_for_day,
    in_period,
    month_of_pay_period,
    month_pay_periods,
    parse_pay_period_label,
    parse_period,
    pay_period_from_dos,
    pay_period_label,
    pay_period_window,
    pay_period_year,
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


# --- Semi-monthly pay periods (Gate 0 dual-granularity reconciliation) ---

def test_pay_period_label_and_parse_roundtrip():
    assert pay_period_label(2026, 6, 1) == "2026-06 Period 1"
    assert pay_period_label(2026, 6, 2) == "2026-06 Period 2"
    assert parse_pay_period_label("2026-06 Period 1") == (2026, 6, 1)
    assert parse_pay_period_label("2026-06 P2") == (2026, 6, 2)  # short form
    for bad in ("2026-06", "2026-06 Period 3", "June 2026", "2026-13 P1"):
        with pytest.raises(PeriodError):
            parse_pay_period_label(bad)


def test_half_split_at_day_15():
    assert half_for_day(1) == 1
    assert half_for_day(15) == 1  # 15 inclusive in Period 1
    assert half_for_day(16) == 2
    assert half_for_day(31) == 2


def test_pay_period_window_is_inclusive_and_covers_the_month():
    s1, e1 = pay_period_window("2026-06 Period 1")
    assert (s1, e1) == (date(2026, 6, 1), date(2026, 6, 15))
    s2, e2 = pay_period_window("2026-06 Period 2")
    assert (s2, e2) == (date(2026, 6, 16), date(2026, 6, 30))  # June has 30 days
    # February end handled via calendar
    _, feb_end = pay_period_window("2024-02 Period 2")
    assert feb_end == date(2024, 2, 29)  # leap year


def test_two_pay_periods_compose_a_month_with_no_gap_or_overlap():
    labels = month_pay_periods("2026-06")
    assert labels == ["2026-06 Period 1", "2026-06 Period 2"]
    (_, e1) = pay_period_window(labels[0])
    (s2, _) = pay_period_window(labels[1])
    assert (e1 - date(2026, 6, 1)).days + 1 == 15   # period 1 length
    assert s2 == date(2026, 6, 16)                   # contiguous, no gap/overlap
    assert all(month_of_pay_period(x) == "2026-06" for x in labels)


def test_pay_period_from_dos_and_year_from_label():
    assert pay_period_from_dos(date(2026, 6, 3)) == "2026-06 Period 1"
    assert pay_period_from_dos(date(2026, 6, 20)) == "2026-06 Period 2"
    assert pay_period_year("2025-12 Period 2") == 2025
