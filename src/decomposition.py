"""Phase D -- calendar normalization and volume/rate/mix decomposition (§12-§14).

Two jobs:

1. **Calendar normalization (§12).** A window with an extra Monday must not read
   as a real signal. Count business days and CLINIC days (days the practice
   actually delivered care, derived from the appointments export) and report
   volume both raw and per-clinic-day.

2. **Volume / rate / mix decomposition (§13).** Split the year-over-year change
   in expected revenue into three effects that **sum exactly to the total
   change**. This is the point of the exercise: a revenue drop caused by seeing
   fewer patients is a different problem from the same drop caused by a payer-mix
   shift.

   With categories c (CPT and/or payer), sessions S_c and expected-per-session
   y_c = E_c / S_c, and mix weights w_c = S_c / S:

       volume = (S_cur - S_prior) x ybar_prior
       mix    = SUM_c (S_c,cur - S_cur x w_c,prior) x y_c,prior
       rate   = SUM_c (E_c,cur - S_c,cur x y_c,prior)

   These sum identically to E_cur - E_prior (proved in the tests, not assumed).

   **Exactness.** The intermediate yields are exact rationals (Fraction), so the
   identity holds with no floating-point or division rounding. Components are
   presented as Decimal cents; any cent-level rounding residue is folded into the
   MIX component, which §13 already defines as the residual term -- so the three
   Decimals also sum exactly to the total change.

Determinism: categories are iterated in sorted order; no wall clock, no
randomness in the analysis path.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Dict, List, Optional, Sequence, Tuple

from . import codes
from .config import DEFAULT_CONFIG, Config
from .dollars import service_month
from .periods import month_range, shift_month
from .sessions import Appointment, ChargeLine

ZERO = Decimal("0")
CENTS = Decimal("0.01")

# Payer categories are the engine's closed set (models.py:136-139). We reuse the
# engine's payer_map rather than building a second classifier (ASSUMPTIONS §3);
# the map itself is supplied as a runtime input, like the roster snapshot.
UNMAPPED_PAYER = "UNMAPPED"
NO_PAYER = "NO_PAYER"


# --------------------------------------------------------------------------- #
# Encounters -- the session grain, carrying its category and its dollars
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Encounter:
    key: Tuple[str, str, str]
    dos: date
    month: str
    primary_cpt: str
    payer_category: str
    expected: Decimal
    billed: Decimal
    n_lines: int


def map_payer_category(raw_payer: str, payer_map: Optional[Dict[str, str]]) -> str:
    """Engine rule (engine.py:185): exact lookup, then a fee-suffix-stripped
    retry. Unmapped stays UNMAPPED and is counted -- never keyword-guessed."""
    if not raw_payer:
        return NO_PAYER
    if not payer_map:
        return UNMAPPED_PAYER
    if raw_payer in payer_map:
        return payer_map[raw_payer]
    import re
    base = re.sub(r"\s*[-–]?\s*\$\s*\d+(?:\.\d{1,2})?\s*$", "", raw_payer).strip()
    if base and base != raw_payer and base in payer_map:
        return payer_map[base]
    return UNMAPPED_PAYER


def build_encounters(
    charges: Sequence[ChargeLine],
    payer_map: Optional[Dict[str, str]] = None,
) -> Tuple[List[Encounter], Dict[str, int]]:
    """Collapse billable charge lines into encounters with a primary code.

    An encounter's category is its PRIMARY service; add-on lines contribute their
    dollars to the same encounter, so encounter dollars sum to line dollars
    exactly (money conservation carries through Phase D).
    """
    exceptions = {"encounters_without_primary": 0, "multi_primary_encounters": 0,
                  "payer_unmapped": 0}
    billable = [
        ln for ln in charges
        if not ln.is_void and not codes.is_non_session(ln.cpt) and ln.dos is not None
    ]
    grouped: Dict[Tuple[str, str, str], List[ChargeLine]] = {}
    for ln in billable:
        grouped.setdefault(ln.encounter_key, []).append(ln)

    out: List[Encounter] = []
    for key in sorted(grouped):
        lines = grouped[key]
        primaries = [ln for ln in lines if codes.is_primary(ln.cpt)]
        if not primaries:
            exceptions["encounters_without_primary"] += 1
            # Deterministic fallback that is REPORTED, not silent: the
            # highest-expected line names the encounter.
            chosen = max(lines, key=lambda x: ((x.expected or ZERO), x.cpt))
        else:
            if len({p.cpt for p in primaries}) > 1:
                exceptions["multi_primary_encounters"] += 1
            chosen = max(primaries, key=lambda x: ((x.expected or ZERO), x.cpt))
        cat = map_payer_category(chosen.payer, payer_map)
        if cat == UNMAPPED_PAYER:
            exceptions["payer_unmapped"] += 1
        dos = lines[0].dos
        out.append(Encounter(
            key=key,
            dos=dos,
            month=service_month(dos),
            primary_cpt=chosen.cpt,
            payer_category=cat,
            expected=sum((ln.expected or ZERO for ln in lines), ZERO),
            billed=sum((ln.billed or ZERO for ln in lines), ZERO),
            n_lines=len(lines),
        ))
    return out, exceptions


def category_of(enc: Encounter, dimension: str) -> str:
    if dimension == "cpt":
        return enc.primary_cpt
    if dimension == "payer":
        return enc.payer_category
    if dimension == "cpt_payer":
        return f"{enc.primary_cpt}|{enc.payer_category}"
    raise ValueError(f"unknown mix dimension {dimension!r}")


def category_totals(
    encounters: Sequence[Encounter], months: Sequence[str], dimension: str
) -> Dict[str, Tuple[int, Decimal]]:
    """{category: (sessions, expected)} restricted to the given months."""
    wanted = set(months)
    totals: Dict[str, Tuple[int, Decimal]] = {}
    for e in encounters:
        if e.month not in wanted:
            continue
        cat = category_of(e, dimension)
        s, x = totals.get(cat, (0, ZERO))
        totals[cat] = (s + 1, x + e.expected)
    return totals


# --------------------------------------------------------------------------- #
# Calendar normalization (§12)
# --------------------------------------------------------------------------- #

def business_days_in_months(months: Sequence[str]) -> int:
    """Weekdays (Mon-Fri) in the given calendar months.

    NOTE: no holiday calendar is applied -- public holidays would need a holiday
    input, and silently guessing one would be a fabricated adjustment. Clinic
    days (below) are the empirical denominator and already reflect closures.
    """
    n = 0
    for m in months:
        year, month = int(m[:4]), int(m[5:7])
        last = calendar.monthrange(year, month)[1]
        d = date(year, month, 1)
        while d <= date(year, month, last):
            if d.weekday() < 5:
                n += 1
            d += timedelta(days=1)
    return n


@dataclass
class CalendarWindow:
    months: List[str]
    sessions: int
    expected: Decimal
    business_days: int
    clinic_days: int      # days with >= 1 KEPT appointment (care delivered)
    scheduled_days: int   # days with >= 1 appointment of any status

    @property
    def sessions_per_clinic_day(self) -> Optional[Decimal]:
        if self.clinic_days == 0:
            return None
        return (Decimal(self.sessions) / Decimal(self.clinic_days)).quantize(
            Decimal("0.001"), rounding=ROUND_HALF_UP
        )

    @property
    def expected_per_clinic_day(self) -> Optional[Decimal]:
        if self.clinic_days == 0:
            return None
        return (self.expected / Decimal(self.clinic_days)).quantize(
            CENTS, rounding=ROUND_HALF_UP
        )


def build_calendar_window(
    months: Sequence[str],
    encounters: Sequence[Encounter],
    appointments: Sequence[Appointment] = (),
) -> CalendarWindow:
    wanted = set(months)
    in_window = [e for e in encounters if e.month in wanted]
    clinic = {a.dos for a in appointments
              if a.dos and service_month(a.dos) in wanted
              and a.status_category == "kept"}
    scheduled = {a.dos for a in appointments
                 if a.dos and service_month(a.dos) in wanted}
    return CalendarWindow(
        months=list(months),
        sessions=len(in_window),
        expected=sum((e.expected for e in in_window), ZERO),
        business_days=business_days_in_months(months),
        clinic_days=len(clinic),
        scheduled_days=len(scheduled),
    )


# --------------------------------------------------------------------------- #
# Volume / rate / mix decomposition (§13)
# --------------------------------------------------------------------------- #

@dataclass
class Decomposition:
    total_change: Decimal
    volume_effect: Optional[Decimal]
    rate_effect: Optional[Decimal]
    mix_effect: Optional[Decimal]
    prior_sessions: int
    current_sessions: int
    prior_expected: Decimal
    current_expected: Decimal
    prior_yield: Optional[Decimal]
    current_yield: Optional[Decimal]
    dimension: str
    new_categories: List[str] = field(default_factory=list)
    lost_categories: List[str] = field(default_factory=list)
    defined: bool = True
    notes: List[str] = field(default_factory=list)

    def components_sum(self) -> Optional[Decimal]:
        if not self.defined:
            return None
        return self.volume_effect + self.rate_effect + self.mix_effect


def _q(f: Fraction) -> Decimal:
    """Exact rational -> Decimal cents (rounding only at presentation)."""
    return (Decimal(f.numerator) / Decimal(f.denominator)).quantize(
        CENTS, rounding=ROUND_HALF_UP
    )


def decompose(
    prior: Dict[str, Tuple[int, Decimal]],
    current: Dict[str, Tuple[int, Decimal]],
    dimension: str = "cpt",
) -> Decomposition:
    """Split the change in expected revenue into volume, rate and mix effects.

    All intermediate arithmetic is exact (Fraction), so the identity
    volume + rate + mix == total_change holds without rounding drift. The
    cent-level residue from presentation lands in MIX (the residual term).
    """
    S_p = sum(s for s, _ in prior.values())
    S_c = sum(s for s, _ in current.values())
    E_p = sum((x for _, x in prior.values()), ZERO)
    E_c = sum((x for _, x in current.values()), ZERO)
    total_change = E_c - E_p

    notes: List[str] = []
    new_cats = sorted(set(current) - set(prior))
    lost_cats = sorted(set(prior) - set(current))

    if S_p == 0:
        # No prior baseline -> yields are undefined. Report it; do not fabricate
        # a zero or attribute the whole change to an arbitrary component.
        notes.append(
            "no prior-period sessions: volume/rate/mix is undefined without a "
            "baseline yield (reported, not fabricated)"
        )
        return Decomposition(
            total_change=total_change, volume_effect=None, rate_effect=None,
            mix_effect=None, prior_sessions=S_p, current_sessions=S_c,
            prior_expected=E_p, current_expected=E_c, prior_yield=None,
            current_yield=None, dimension=dimension, new_categories=new_cats,
            lost_categories=lost_cats, defined=False, notes=notes,
        )

    ybar_p = Fraction(E_p) / S_p  # exact

    # Prior per-category yield; a category new this year has no prior yield, so
    # it is imputed at the prior overall average. The identity holds for ANY
    # imputed value (the term cancels) -- the choice only shifts attribution
    # between mix and rate, and every such category is listed in new_categories.
    y_prior: Dict[str, Fraction] = {}
    for cat, (s, x) in prior.items():
        y_prior[cat] = Fraction(x) / s if s else Fraction(0)
    for cat in new_cats:
        y_prior[cat] = ybar_p
    if new_cats:
        notes.append(
            f"{len(new_cats)} category(ies) new this period "
            f"({', '.join(new_cats)}); prior yield imputed at the prior overall "
            "average -- attribution only, the identity is unaffected"
        )
    if lost_cats:
        notes.append(
            f"{len(lost_cats)} category(ies) present prior but absent now "
            f"({', '.join(lost_cats)}); their loss lands in the mix effect"
        )

    volume_f = (Fraction(S_c) - Fraction(S_p)) * ybar_p

    mix_f = Fraction(0)
    rate_f = Fraction(0)
    for cat in sorted(set(prior) | set(current)):
        s_cur, e_cur = current.get(cat, (0, ZERO))
        s_pri, _ = prior.get(cat, (0, ZERO))
        yp = y_prior.get(cat, Fraction(0))
        # mix: this category's session share shifted, valued at PRIOR rates
        mix_f += (Fraction(s_cur) - Fraction(S_c) * Fraction(s_pri) / S_p) * yp
        # rate: within-category price change at CURRENT volumes
        rate_f += Fraction(e_cur) - Fraction(s_cur) * yp

    volume_d, rate_d = _q(volume_f), _q(rate_f)
    # Fold the presentation residue into mix (§13 defines mix as the residual),
    # so the three Decimals sum EXACTLY to the total change.
    mix_d = total_change - volume_d - rate_d

    return Decomposition(
        total_change=total_change,
        volume_effect=volume_d,
        rate_effect=rate_d,
        mix_effect=mix_d,
        prior_sessions=S_p,
        current_sessions=S_c,
        prior_expected=E_p,
        current_expected=E_c,
        prior_yield=_q(ybar_p),
        current_yield=_q(Fraction(E_c) / S_c) if S_c else None,
        dimension=dimension,
        new_categories=new_cats,
        lost_categories=lost_cats,
        defined=True,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# Comparison windows (§14) -- all three, every run
# --------------------------------------------------------------------------- #

@dataclass
class WindowSpec:
    name: str
    current_months: List[str]
    prior_months: List[str]


def comparison_windows(period: str) -> List[WindowSpec]:
    """The three windows produced every run (§14)."""
    return [
        WindowSpec(
            "current_vs_same_period_prior_year",
            [period], [shift_month(period, -12)],
        ),
        WindowSpec(
            "rolling_12_vs_prior_rolling_12",
            month_range(period, 12), month_range(shift_month(period, -12), 12),
        ),
        WindowSpec(
            "trailing_3_vs_same_3_prior_year",
            month_range(period, 3), month_range(shift_month(period, -12), 3),
        ),
    ]


@dataclass
class WindowComparison:
    name: str
    current: CalendarWindow
    prior: CalendarWindow
    decomposition: Decomposition
    incomplete_periods: List[str] = field(default_factory=list)
    label: str = "COMPLETE"


def compare_window(
    spec: WindowSpec,
    encounters: Sequence[Encounter],
    appointments: Sequence[Appointment] = (),
    dimension: str = "cpt",
    incomplete: Sequence[str] = (),
) -> WindowComparison:
    cur = build_calendar_window(spec.current_months, encounters, appointments)
    pri = build_calendar_window(spec.prior_months, encounters, appointments)
    dec = decompose(
        category_totals(encounters, spec.prior_months, dimension),
        category_totals(encounters, spec.current_months, dimension),
        dimension,
    )
    # §11: an INCOMPLETE period must carry its label into EVERY output that
    # touches it -- including these comparisons.
    touched = sorted(set(incomplete) & set(spec.current_months + spec.prior_months))
    return WindowComparison(
        name=spec.name, current=cur, prior=pri, decomposition=dec,
        incomplete_periods=touched,
        label="INCOMPLETE" if touched else "COMPLETE",
    )


def compare_all_windows(
    charges: Sequence[ChargeLine],
    appointments: Sequence[Appointment] = (),
    config: Config = DEFAULT_CONFIG,
    payer_map: Optional[Dict[str, str]] = None,
    dimension: str = "cpt",
    incomplete: Sequence[str] = (),
    period: Optional[str] = None,
) -> Tuple[List[WindowComparison], Dict[str, int]]:
    """All three comparison windows, every run (§14)."""
    encounters, exceptions = build_encounters(charges, payer_map)
    target = period or config.target_period
    return (
        [compare_window(spec, encounters, appointments, dimension, incomplete)
         for spec in comparison_windows(target)],
        exceptions,
    )
