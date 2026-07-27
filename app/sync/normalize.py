"""Turning what the sheet says into what the database stores.

Every rule here is recorded in ASSUMPTIONS.md. Sheet reality that these handle:
  Excel writes numeric codes as floats, so 90837 arrives as "90837.0"
  the same procedure is written several ways, 90791-ADHD and 90837 - ADHD
  location 1 arrives as the float 1.0
  53 rows carry a CPT that is not a code at all (QBCHK, FORM, pro bono, ADHD)
  money cells are blank, or floats, or strings with currency symbols
  dates arrive as datetimes, as serial numbers, or as text
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# Excel's day zero. Dates in a sheet read through the API are usually already typed,
# but a cell formatted as a number comes through as a serial and must not be silently
# dropped.
_EXCEL_EPOCH = date(1899, 12, 30)

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%m-%d-%Y",
    "%b %d, %Y",
    "%d %b %Y",
)

_MONEY_STRIP = re.compile(r"[$,\s]")
_TRAILING_ZERO_FLOAT = re.compile(r"^(-?\d+)\.0+$")
_LEADING_CPT_CODE = re.compile(r"^(\d{4,5})\b")


class ParseError(ValueError):
    """Raised when a value cannot be interpreted. Carries the offending raw text."""

    def __init__(self, message: str, raw: object) -> None:
        self.raw = raw
        super().__init__(message)


def clean_text(value: object) -> str:
    """Trim, collapse internal whitespace, and drop Excel's trailing float zeros.

    `90837.0` becomes `90837`, `1.0` becomes `1`. Applied before anything else, so
    every downstream rule sees the same shape regardless of how Excel typed the cell.
    """
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)

    text = re.sub(r"\s+", " ", str(value)).strip()
    match = _TRAILING_ZERO_FLOAT.match(text)
    if match:
        return match.group(1)
    return text


def normalize_cpt(value: object) -> str:
    """Uppercase the cleaned text. `pro bono` becomes `PRO BONO`."""
    return clean_text(value).upper()


def cpt_base(normalized_cpt: str) -> str:
    """The leading numeric code when there is one, otherwise the value itself.

    This is what the exclusion list is compared against and what reports group by, so
    that the suffixed spellings of a procedure land on the procedure.
    """
    match = _LEADING_CPT_CODE.match(normalized_cpt)
    return match.group(1) if match else normalized_cpt


def normalize_patient_name(value: object) -> str:
    """Uppercase and whitespace collapsed, for the identity key.

    The display value is kept separately: this is only ever the comparison form.
    """
    return clean_text(value).upper()


def normalize_short_code(value: object) -> str | None:
    """Insurance, location, and note codes. Blank becomes None, not empty string.

    Blank insurance means unknown, not self pay (`SP` is a real code with 421 rows in
    the Q2 data), so it must not be coerced into any category. See ASSUMPTIONS.md
    A-043.
    """
    text = clean_text(value).upper()
    return text or None


def parse_date(value: object) -> date | None:
    """Return a date, None for blank, or raise ParseError for unreadable text."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    if isinstance(value, int | float) and not isinstance(value, bool):
        serial = int(value)
        if serial <= 0 or serial > 200_000:
            raise ParseError(f"Date serial out of range: {value!r}", value)
        from datetime import timedelta

        return _EXCEL_EPOCH + timedelta(days=serial)

    text = clean_text(value)
    if not text:
        return None

    # A datetime that arrived as text, which is how the xlsx copy writes DOS.
    if " " in text:
        text = text.split(" ", 1)[0]

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    raise ParseError(f"Unrecognized date: {text!r}", value)


def parse_money(value: object) -> Decimal:
    """Return an exact Decimal.

    Blank reads as 0. A value that will not parse does NOT read as 0: it raises, and
    the caller sends the row to import_errors with the raw string preserved, so a
    typo never quietly becomes a missing payment. See ASSUMPTIONS.md A-038.
    """
    if value is None:
        return Decimal("0.00")
    if isinstance(value, bool):
        raise ParseError(f"Not an amount: {value!r}", value)
    if isinstance(value, int):
        return Decimal(value).quantize(Decimal("0.01"))
    if isinstance(value, float):
        return Decimal(str(value)).quantize(Decimal("0.01"))
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"))

    text = str(value).strip()
    if not text:
        return Decimal("0.00")

    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]

    text = _MONEY_STRIP.sub("", text)
    if not text or text == "-":
        return Decimal("0.00")

    try:
        amount = Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ArithmeticError) as exc:
        raise ParseError(f"Unrecognized amount: {value!r}", value) from exc

    return -amount if negative else amount
