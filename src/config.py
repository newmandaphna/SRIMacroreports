"""Pipeline configuration -- the machine-readable form of ASSUMPTIONS.md.

Every value here mirrors a documented default in ASSUMPTIONS.md. The
[RULES.md]-flagged definitions (provider roster, period naming, code handling)
are UNRECONCILED against the compensation engine's RULES.md, which was not
reachable when this was built (the SRIMacroreports repo was empty). The flag
`rules_md_reconciled = False` records that fact: any headline number stays
provisional until it flips True after a real reconciliation.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import FrozenSet, Tuple


@dataclass(frozen=True)
class Config:
    # Target reporting period (ASSUMPTIONS §2, export-request.md).
    target_period: str = "2026-07"

    # §4 -- session grain. Headline uses `session_grain`; all variants are
    # computed every run for comparison so disagreements are visible.
    session_grain: str = "billable_encounters"
    session_grain_variants: Tuple[str, ...] = (
        "kept_appointments",
        "billable_encounters",
        "charge_lines",
        "signed_notes",
    )

    # §5 -- add-on codes seed list. The OPERATIVE list is derived from the data
    # (codes co-occurring with a primary E/M or psychotherapy code) and
    # cross-checked against this seed; discrepancies are reported, not assumed.
    add_on_seed_codes: FrozenSet[str] = frozenset({"90833", "90836", "90838", "90785"})

    # §6 -- group therapy. Headline counts one session per group appointment;
    # the per-attendee view is reported alongside.
    group_therapy_code: str = "90853"
    group_therapy_headline: str = "one_per_group"  # or "per_attendee"

    # §11 -- claim-lag maturity threshold. A period < this fraction mature is
    # labeled INCOMPLETE in every output.
    maturity_threshold: Decimal = Decimal("0.95")

    # §15 -- reconciliation tolerance. Headlines derived from >=2 sources that
    # differ by more than this fraction raise a reconciliation exception rather
    # than being averaged.
    reconciliation_tolerance: Decimal = Decimal("0.01")

    # §0 -- RULES.md reconciliation status. False until roster/period/code
    # definitions are reconciled against the real compensation-engine RULES.md.
    rules_md_reconciled: bool = False


DEFAULT_CONFIG = Config()
