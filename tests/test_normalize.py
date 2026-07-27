"""Normalization rules, each tied to something the real Q2 sheet actually contains."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.sync.normalize import (
    ParseError,
    clean_text,
    cpt_base,
    normalize_cpt,
    normalize_patient_name,
    normalize_short_code,
    parse_date,
    parse_money,
)


class TestCleanText:
    def test_strips_excel_float_zeros(self):
        """Excel stores 90837 as the float 90837.0 and location 1 as 1.0."""
        assert clean_text(90837.0) == "90837"
        assert clean_text(1.0) == "1"
        assert clean_text("90837.0") == "90837"

    def test_keeps_genuine_decimals(self):
        assert clean_text(156.05) == "156.05"

    def test_collapses_whitespace(self):
        # The real sheet has one NOTE value of "X " with a trailing space.
        assert clean_text("X ") == "X"
        assert clean_text("  a   b  ") == "a b"

    def test_blank_and_none(self):
        assert clean_text(None) == ""
        assert clean_text("") == ""


class TestCpt:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(90837.0, "90837"), ("90837", "90837"), ("pro bono", "PRO BONO"), ("QBCHK", "QBCHK")],
    )
    def test_normalize(self, raw, expected):
        assert normalize_cpt(raw) == expected

    @pytest.mark.parametrize(
        ("normalized", "expected"),
        [
            ("90837", "90837"),
            # The two suffixed spellings in the real sheet must fold into the code.
            ("90791-ADHD", "90791"),
            ("90837 - ADHD", "90837"),
            # No numeric code at all, so it is its own group.
            ("ADHD", "ADHD"),
            ("ADHD2", "ADHD2"),
            ("QBCHK", "QBCHK"),
            ("PRO BONO", "PRO BONO"),
            ("FORM", "FORM"),
            ("99998", "99998"),
            ("99999", "99999"),
        ],
    )
    def test_base(self, normalized, expected):
        assert cpt_base(normalized) == expected

    def test_suffixed_codes_group_with_their_base(self):
        """The point of cpt_base: 20 rows join their procedure instead of splintering."""
        assert cpt_base(normalize_cpt("90791-ADHD")) == cpt_base(normalize_cpt(90791.0))


class TestShortCodes:
    def test_blank_is_none_not_empty_string(self):
        """Blank insurance means unknown. It must not be coerced into a category."""
        assert normalize_short_code("") is None
        assert normalize_short_code(None) is None
        assert normalize_short_code("   ") is None

    def test_self_pay_is_a_real_value(self):
        assert normalize_short_code("SP") == "SP"

    def test_numeric_location_becomes_a_string(self):
        assert normalize_short_code(1.0) == "1"
        assert normalize_short_code("TH") == "TH"


class TestDates:
    def test_datetime_from_the_sheet(self):
        from datetime import datetime

        assert parse_date(datetime(2026, 4, 1, 0, 0)) == date(2026, 4, 1)

    @pytest.mark.parametrize(
        "raw",
        ["2026-04-01", "4/1/2026", "04/01/2026", "2026-04-01 00:00:00"],
    )
    def test_text_forms(self, raw):
        assert parse_date(raw) == date(2026, 4, 1)

    def test_excel_serial(self):
        assert parse_date(46113) == date(2026, 4, 1)

    def test_blank_is_none(self):
        """Eleven rows in the real sheet have no DOS. None, so the caller rejects."""
        assert parse_date("") is None
        assert parse_date(None) is None

    def test_garbage_raises_rather_than_guessing(self):
        with pytest.raises(ParseError):
            parse_date("sometime last week")


class TestMoney:
    def test_blank_is_zero(self):
        assert parse_money("") == Decimal("0.00")
        assert parse_money(None) == Decimal("0.00")

    def test_floats_are_exact(self):
        assert parse_money(156.05) == Decimal("156.05")
        assert parse_money(191.76) == Decimal("191.76")

    def test_currency_formatting(self):
        assert parse_money("$1,234.56") == Decimal("1234.56")

    def test_parenthesised_negative(self):
        assert parse_money("(50.00)") == Decimal("-50.00")

    def test_unparseable_raises_and_does_not_become_zero(self):
        """A typo must never quietly become a missing payment."""
        with pytest.raises(ParseError) as exc:
            parse_money("see note")
        assert exc.value.raw == "see note"

    def test_sums_are_exact_over_many_rows(self):
        """Nine thousand floats drift; nine thousand Decimals do not."""
        values = [parse_money(0.1) for _ in range(1000)]
        assert sum(values) == Decimal("100.00")


class TestPatientName:
    def test_normalizes_for_the_identity_key(self):
        assert normalize_patient_name("Patient AA") == "PATIENT AA"
        assert normalize_patient_name("  patient  aa ") == "PATIENT AA"
