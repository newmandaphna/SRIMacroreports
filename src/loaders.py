"""Valant raw-export loaders (Phase A).

Valant reports carry quirks the loader must absorb rather than have anyone
hand-clean the file (which would hide the very problems the reconciliation
harness exists to catch). The "NewFeeSchedule" charges variant has a three-line
preamble (a Textbox row, a date row, a blank) before the real header; the header
row is detected **programmatically**, never `skiprows=3`.

No pandas: money columns must stay Decimal (ASSUMPTIONS §10), and pandas would
coerce them to float64 on read -- the exact silent-float bug the spec forbids.
CSV is read with the stdlib; XLSX support is optional and loaded lazily via
openpyxl.
"""
from __future__ import annotations

import csv
import os
from typing import Dict, List, Optional, Sequence, Tuple


class LoaderError(Exception):
    """Base class for loader failures."""


class HeaderNotFoundError(LoaderError):
    """No row matched enough expected header tokens (preamble detection failed)."""


class MissingColumnsError(LoaderError):
    """A required column is absent, blank, or duplicated after header detection."""


# Report family -> filename prefix + the minimal header tokens that identify it.
# Tokens are matched case-insensitively as substrings of a header cell.
REPORT_FAMILIES: Dict[str, Dict[str, object]] = {
    "charges": {
        "prefix": "ChargesHistoryDetailProviderPatientCode",
        "tokens": ("provider", "date of service", "code"),
        "expected_grain": (
            "one row per charge line (patient x provider x DOS x CPT x modifier)"
        ),
    },
    "appointments": {
        "prefix": "AppointmentsPatientInfoByProviderThenDayThenFacility",
        "tokens": ("provider", "date", "status"),
        "expected_grain": "one row per scheduled appointment",
    },
    "statements": {
        "prefix": "PatientStatement",
        "tokens": ("date of service", "insurancepayments", "patientpayments"),
        "expected_grain": "one row per statement grouping line (GroupingLevel3)",
    },
    "documentation": {
        "prefix": "AppointmentDocumentation",
        "tokens": ("provider", "date of service", "status"),
        "expected_grain": "one row per clinical note / encounter",
    },
}


def _norm(s) -> str:
    """Normalize a cell for matching: lowercase, collapse whitespace."""
    return " ".join(str(s).strip().lower().split())


def classify_filename(filename: str) -> Optional[str]:
    """Return the report-family key for a filename, or None if unrecognized."""
    base = os.path.basename(filename)
    for family, spec in REPORT_FAMILIES.items():
        if base.startswith(str(spec["prefix"])):
            return family
    return None


def detect_header_row(
    rows: Sequence[Sequence[object]],
    expected_tokens: Sequence[str],
    min_match: int = 2,
) -> int:
    """Index of the first row containing >= min_match expected tokens.

    This absorbs the preamble quirk without a hardcoded skip. Raises
    HeaderNotFoundError if no row qualifies.
    """
    wanted = [_norm(t) for t in expected_tokens]
    for i, row in enumerate(rows):
        cells = [_norm(c) for c in row]
        matches = sum(1 for t in wanted if any(t in c for c in cells))
        if matches >= min_match:
            return i
    raise HeaderNotFoundError(
        f"no header row matched >= {min_match} of tokens {tuple(expected_tokens)!r}"
    )


def _rows_from_csv(path: str) -> List[List[str]]:
    # utf-8-sig strips a BOM if present; Valant CSVs sometimes carry one.
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return [list(r) for r in csv.reader(f)]


def _rows_from_xlsx(path: str) -> List[List[str]]:
    try:
        from openpyxl import load_workbook
    except ImportError as e:  # pragma: no cover - environment dependent
        raise LoaderError(
            "openpyxl is required to read .xlsx exports; install it or export CSV"
        ) from e
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        rows: List[List[str]] = []
        for row in ws.iter_rows(values_only=True):
            rows.append(["" if c is None else str(c) for c in row])
    finally:
        wb.close()
    return rows


def read_raw_rows(path: str) -> List[List[str]]:
    """Read every row of a raw export as lists of string cells (no parsing yet)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".csv", ".txt", ".tsv"):
        return _rows_from_csv(path)
    if ext in (".xlsx", ".xlsm"):
        return _rows_from_xlsx(path)
    raise LoaderError(f"unsupported export extension: {ext!r} ({path})")


def _assert_clean_header(header: Sequence[str], path: str) -> None:
    blanks = [i for i, h in enumerate(header) if _norm(h) == ""]
    if blanks:
        raise MissingColumnsError(
            f"blank header column(s) at index {blanks} in {os.path.basename(path)}"
        )
    seen: Dict[str, int] = {}
    dupes = set()
    for h in header:
        key = _norm(h)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            dupes.add(h)
    if dupes:
        raise MissingColumnsError(
            f"duplicate header column(s) {sorted(dupes)} in {os.path.basename(path)}"
        )


def assert_columns(header: Sequence[str], required: Sequence[str]) -> bool:
    """Fail loudly if any required column (normalized substring match) is absent."""
    norm_header = [_norm(h) for h in header]
    missing = [req for req in required if not any(_norm(req) in h for h in norm_header)]
    if missing:
        raise MissingColumnsError(f"missing required column(s): {list(missing)}")
    return True


def load_report(
    path: str,
    expected_tokens: Optional[Sequence[str]] = None,
    family: Optional[str] = None,
) -> Tuple[List[str], List[Dict[str, str]]]:
    """Load a Valant export into (header, records).

    - Detects the header row programmatically (preamble-safe).
    - `records` is a list of dict(header_cell -> raw string cell). Values are left
      as raw strings so money/date parsing stays explicit downstream and no
      silent type coercion occurs.

    Raises HeaderNotFoundError / MissingColumnsError / LoaderError loudly; it
    never guesses past a malformed file.
    """
    if family is None:
        family = classify_filename(path)
    if expected_tokens is None:
        if family is None:
            raise LoaderError(
                f"cannot route unrecognized file {os.path.basename(path)!r}; "
                "pass expected_tokens explicitly"
            )
        expected_tokens = REPORT_FAMILIES[family]["tokens"]  # type: ignore[assignment]

    rows = read_raw_rows(path)
    if not rows:
        raise LoaderError(f"empty file: {path}")

    header_idx = detect_header_row(rows, expected_tokens)
    header = [str(c).strip() for c in rows[header_idx]]
    _assert_clean_header(header, path)

    records: List[Dict[str, str]] = []
    for raw in rows[header_idx + 1:]:
        if all(_norm(c) == "" for c in raw):
            continue  # skip fully blank trailing rows
        cells = list(raw)[: len(header)]
        cells += [""] * (len(header) - len(cells))  # pad short rows
        records.append({h: str(c) for h, c in zip(header, cells)})
    return header, records
