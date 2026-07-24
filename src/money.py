"""Money parsing for the SRI analytics pipeline.

All monetary values are Decimal, never float (ASSUMPTIONS.md §10). A present but
unparseable money cell fails loudly; a blank cell is *missing* (None), which the
caller must distinguish from a legitimate zero -- it is never silently coerced
to 0.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional


class MoneyParseError(ValueError):
    """A money cell was present but could not be parsed to Decimal."""


def parse_money(raw) -> Optional[Decimal]:
    """Return a Decimal for a money cell, or None if the cell is blank/missing.

    Handles Valant quirks: a leading currency symbol, thousands separators, and
    parenthesized negatives::

        "$1,234.50" -> Decimal('1234.50')
        "(45.00)"   -> Decimal('-45.00')
        ""          -> None            (missing, NOT zero)

    Raises MoneyParseError on a non-empty, non-numeric value so a bad row never
    becomes a silent zero.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "":
        return None

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    if s.startswith("$"):
        s = s[1:].strip()
    if s.startswith("-"):
        negative = True
        s = s[1:].strip()
    elif s.startswith("+"):
        s = s[1:].strip()
    s = s.replace(",", "")

    if s == "":
        raise MoneyParseError(f"unparseable money value: {raw!r}")
    try:
        value = Decimal(s)
    except InvalidOperation:
        raise MoneyParseError(f"unparseable money value: {raw!r}") from None
    return -value if negative else value
