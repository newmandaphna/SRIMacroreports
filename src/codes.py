"""CPT/HCPCS/transaction-code vocabulary, reconciled with the engine (Gate 0).

These are the code rules the session ledger needs. They mirror the compensation
engine (newmandaphna/SRIcompensation) so the two projects agree on what a code
*is*; the engine's *pay* treatments for special codes are out of scope here
(ASSUMPTIONS §3).
"""
from __future__ import annotations

from typing import Iterable

# Never a billable clinical session (engine valant_parse.py:27). Matched on the
# TELE-stripped, upper-cased code.
NON_SESSION_CODES = frozenset({
    "99998", "99999", "BAL FWD", "QBCHK", "ADHD2", "FORM FEE", "ADHD", "",
})

# Group therapy: one appointment = one unit of provider time (ASSUMPTIONS §6).
GROUP_THERAPY_CODE = "90853"

# Primary psychotherapy / diagnostic-eval codes an add-on can attach to. E/M
# codes are detected by numeric range (is_em_code) rather than enumerated.
PRIMARY_PSYCH_CODES = frozenset({
    "90791", "90792",              # diagnostic evaluation
    "90832", "90834", "90837",     # individual psychotherapy
    "90839", "90840",              # crisis psychotherapy
    "90846", "90847",              # family psychotherapy
    GROUP_THERAPY_CODE,            # group therapy is itself a primary session
})


def normalize_code(code: str) -> str:
    """Upper-case, collapse internal whitespace: 'bal  fwd' -> 'BAL FWD'."""
    return " ".join(str(code or "").strip().upper().split())


def strip_tele(code: str) -> str:
    """'TELE90837' -> '90837' (engine valant_parse.py:74). Leaves others alone."""
    c = str(code or "").strip()
    return c[4:] if c.upper().startswith("TELE") else c


def base_code(code: str) -> str:
    """The comparable form of a code: TELE-stripped then normalized."""
    return normalize_code(strip_tele(code))


def is_non_session(code: str) -> bool:
    """True for codes that never count as a billable clinical session."""
    return base_code(code) in NON_SESSION_CODES


def is_group_therapy(code: str) -> bool:
    return base_code(code) == GROUP_THERAPY_CODE


def is_em_code(code: str) -> bool:
    """Evaluation & Management office/outpatient code, by numeric range."""
    c = base_code(code)
    return c.isdigit() and 99202 <= int(c) <= 99499


def is_primary(code: str) -> bool:
    """A primary session code an add-on can attach to (psychotherapy or E/M)."""
    c = base_code(code)
    return c in PRIMARY_PSYCH_CODES or is_em_code(c)
