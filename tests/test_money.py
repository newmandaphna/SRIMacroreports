"""Money must be Decimal, missing must not become zero (ASSUMPTIONS §10)."""
from decimal import Decimal

import pytest

from src.money import MoneyParseError, parse_money


def test_plain_and_currency_and_commas():
    assert parse_money("180.00") == Decimal("180.00")
    assert parse_money("$1,234.50") == Decimal("1234.50")
    assert parse_money(" $  2,000 ") == Decimal("2000")


def test_parenthesized_and_signed_negatives():
    assert parse_money("(45.00)") == Decimal("-45.00")
    assert parse_money("-45.00") == Decimal("-45.00")
    assert parse_money("+45.00") == Decimal("45.00")


def test_blank_is_missing_not_zero():
    assert parse_money("") is None
    assert parse_money("   ") is None
    assert parse_money(None) is None
    # The critical distinction: missing (None) is NOT the same as a real zero.
    assert parse_money("0.00") == Decimal("0.00")
    assert parse_money("0.00") is not None


def test_result_is_decimal_never_float():
    for raw in ("1", "1.5", "$3,000.25", "(10)"):
        assert isinstance(parse_money(raw), Decimal)


def test_present_but_unparseable_fails_loudly():
    for bad in ("N/A", "abc", "$", "()", "1.2.3"):
        with pytest.raises(MoneyParseError):
            parse_money(bad)
