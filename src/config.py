"""Pipeline configuration -- the machine-readable form of ASSUMPTIONS.md.

Every value here mirrors a documented default in ASSUMPTIONS.md. The
[RULES.md]-flagged definitions (provider roster, period naming, code handling)
were reconciled in Gate 0 against the compensation engine (newmandaphna/
SRIcompensation -- there is no literal RULES.md file). `rules_reconciled` records
which sections are **definitionally** reconciled; `rules_md_reconciled` is True
only when all three are. Note: definition-reconciled is not the same as
implemented -- the canonical-name loaders, dual-granularity periods, and
non-session-code exclusion are wired in Gates 1-3.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import FrozenSet, Tuple

# The three [RULES.md]-flagged areas that must be reconciled against the engine.
RULES_SECTIONS: FrozenSet[str] = frozenset({"roster", "periods", "codes"})


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

    # §0 -- reconciliation status against the compensation engine. Gate 0
    # reconciled all three [RULES.md]-flagged areas (roster, periods, codes) at
    # the DEFINITION level with source lines cited in ASSUMPTIONS.md §1-§3, §10.
    # A section is listed here once its definition is settled against the engine;
    # implementation of that definition lands in Gates 1-3.
    rules_reconciled: FrozenSet[str] = RULES_SECTIONS

    @property
    def rules_md_reconciled(self) -> bool:
        """True only when every [RULES.md]-flagged section is reconciled."""
        return RULES_SECTIONS <= self.rules_reconciled

    def unreconciled_sections(self) -> Tuple[str, ...]:
        """Flagged sections not yet reconciled (deterministic order)."""
        return tuple(sorted(RULES_SECTIONS - self.rules_reconciled))


DEFAULT_CONFIG = Config()
