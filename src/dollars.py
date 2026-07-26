"""Phase C -- the dollar ladder and claim-lag maturity (ASSUMPTIONS.md §10-§11).

Three measures that are NEVER substituted for one another:

  1. billed_charges      gross list price. CONTEXT ONLY, not a health metric.
  2. expected_collection contracted/expected at date of service. THE HEADLINE.
  3. collected           actual cash from PatientStatement (insurance + patient).

Everything buckets by DATE OF SERVICE. The posting/payment date is used for one
thing only: measuring claim lag. A period whose collections are not yet mature is
labeled INCOMPLETE in every output, so a recent period is never silently compared
against a fully-matured prior-year one.

Money is Decimal end to end; rounding happens only at presentation.

Sign convention (reconciled with the engine, statements.py:49-53): Valant stores
payments as NEGATIVE credits, so collected = -(InsurancePayments + PatientPayments).
Rows that violate this convention are counted and reported, never silently flipped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from . import codes, loaders
from .config import DEFAULT_CONFIG, Config
from .money import parse_money
from .sessions import ChargeLine, _col, _to_date

ZERO = Decimal("0")

# A service month is treated as a maturity reference once it is at least this old.
DEFAULT_MATURITY_HORIZON_MONTHS = 12


# --------------------------------------------------------------------------- #
# Payment records (the only source of actual cash)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PaymentLine:
    provider: str
    dos: Optional[date]           # bucketing basis
    posting_date: Optional[date]  # lag basis ONLY, never bucketing
    insurance: Decimal
    patient: Decimal
    payer: str

    @property
    def collected(self) -> Decimal:
        """Actual cash. Payments arrive as negative credits, hence the negation."""
        return -(self.insurance + self.patient)


def build_payment_lines(
    header: Sequence[str], records: Sequence[Dict[str, str]]
) -> List[PaymentLine]:
    prov_c = _col(header, "GroupingLevel1", "ProviderID", "Provider")
    dos_c = loaders.resolve_dos_column("statements", header)
    post_c = loaders.resolve_posting_column("statements", header)
    ins_c = _col(header, "InsurancePayments")
    pat_c = _col(header, "PatientPayments")
    payer_c = _col(header, "Insurance123", "Payer", "Insurance")

    out: List[PaymentLine] = []
    for rec in records:
        ins = parse_money(rec.get(ins_c, "") if ins_c else "") or ZERO
        pat = parse_money(rec.get(pat_c, "") if pat_c else "") or ZERO
        out.append(PaymentLine(
            provider=loaders._norm_alnum(rec.get(prov_c, "") if prov_c else ""),
            dos=_to_date(rec.get(dos_c, "") if dos_c else ""),
            posting_date=_to_date(rec.get(post_c, "") if post_c else ""),
            insurance=ins,
            patient=pat,
            payer=loaders._norm(rec.get(payer_c, "") if payer_c else ""),
        ))
    return out


def load_payments(path: str) -> List[PaymentLine]:
    header, records = loaders.load_report(path, family="statements")
    return build_payment_lines(header, records)


# --------------------------------------------------------------------------- #
# Periods and lag
# --------------------------------------------------------------------------- #

def service_month(d: date) -> str:
    """The calendar month a date of service rolls up to ('2026-07')."""
    return f"{d.year:04d}-{d.month:02d}"


def months_between(start: date, end: date) -> int:
    """Whole months from start to end (month granularity, deterministic)."""
    return (end.year - start.year) * 12 + (end.month - start.month)


def lag_months(dos: date, posting: date) -> int:
    """Months from date of service to posting. Never negative-clamped silently:
    a payment posted before its DOS is a data anomaly and is reported."""
    return months_between(dos, posting)


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #

@dataclass
class PeriodDollars:
    period: str
    billed_charges: Decimal
    expected_collection: Decimal
    collected: Decimal
    maturity: Optional[Decimal] = None   # modeled fraction of ultimate collected
    is_complete: Optional[bool] = None   # None = unknown (no curve)
    label: str = "UNKNOWN"               # COMPLETE | INCOMPLETE | UNKNOWN

    @property
    def collection_rate(self) -> Optional[Decimal]:
        """collected / expected. Meaningless on its own for an INCOMPLETE period."""
        if self.expected_collection == ZERO:
            return None
        return self.collected / self.expected_collection


@dataclass
class DollarLadder:
    periods: List[PeriodDollars]
    totals: Dict[str, Decimal]
    maturity_curve: Dict[int, Decimal]
    reference_periods: List[str]
    as_of: Optional[date]
    threshold: Decimal
    exceptions: Dict[str, int] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def by_period(self, period: str) -> Optional[PeriodDollars]:
        for p in self.periods:
            if p.period == period:
                return p
        return None

    def incomplete_periods(self) -> List[str]:
        return [p.period for p in self.periods if p.label != "COMPLETE"]


# --------------------------------------------------------------------------- #
# The maturity model
# --------------------------------------------------------------------------- #

def build_maturity_curve(
    charges: Sequence[ChargeLine],
    payments: Sequence[PaymentLine],
    as_of: date,
    horizon: int = DEFAULT_MATURITY_HORIZON_MONTHS,
) -> Tuple[Dict[int, Decimal], List[str]]:
    """Fit cumulative-collection-by-lag from FULLY MATURED service months.

    A service month qualifies as a reference once it is at least `horizon` months
    old at `as_of` and actually collected something. For each reference month the
    cumulative collected at lag L is divided by that month's ultimate collected;
    the curve is the mean across reference months at each lag.

    Returns (curve, reference_period_labels). An empty curve means the data cannot
    support a maturity judgement -- callers must then label periods UNKNOWN rather
    than assume completeness.
    """
    by_month_lag: Dict[str, Dict[int, Decimal]] = {}
    for p in payments:
        if p.dos is None or p.posting_date is None:
            continue
        m = service_month(p.dos)
        lag = max(lag_months(p.dos, p.posting_date), 0)
        by_month_lag.setdefault(m, {})
        by_month_lag[m][lag] = by_month_lag[m].get(lag, ZERO) + p.collected

    references = sorted(
        m for m, lags in by_month_lag.items()
        if months_between(date(int(m[:4]), int(m[5:7]), 1), as_of) >= horizon
        and sum(lags.values(), ZERO) > ZERO
    )
    if not references:
        return ({}, [])

    curve: Dict[int, Decimal] = {}
    for lag in range(0, horizon + 1):
        ratios: List[Decimal] = []
        for m in references:
            lags = by_month_lag[m]
            ultimate = sum(lags.values(), ZERO)
            if ultimate <= ZERO:
                continue
            cum = sum((v for L, v in sorted(lags.items()) if L <= lag), ZERO)
            ratios.append(cum / ultimate)
        if ratios:
            curve[lag] = sum(ratios, ZERO) / Decimal(len(ratios))
    return (curve, references)


def maturity_for_age(curve: Dict[int, Decimal], age: int) -> Optional[Decimal]:
    """Modeled fraction of ultimate collections in hand for a period of this age."""
    if not curve:
        return None
    if age < 0:
        return ZERO
    max_lag = max(curve)
    return curve[min(age, max_lag)]


# --------------------------------------------------------------------------- #
# The ladder
# --------------------------------------------------------------------------- #

def compute_ladder(
    charges: Sequence[ChargeLine],
    payments: Sequence[PaymentLine] = (),
    config: Config = DEFAULT_CONFIG,
    as_of: Optional[date] = None,
    horizon: int = DEFAULT_MATURITY_HORIZON_MONTHS,
) -> DollarLadder:
    """Build the three-measure ladder by date of service, with maturity labels.

    `as_of` defaults to the latest posting date observed in the payments (a
    deterministic function of the input -- no wall clock).
    """
    notes: List[str] = []
    exceptions = {
        "charge_lines_missing_dos": 0,
        "payments_missing_dos": 0,
        "payments_missing_posting_date": 0,
        "payments_posted_before_service": 0,
        "payment_sign_anomalies": 0,
    }

    # --- charges side: billed + expected, by DOS. Voids and non-session codes
    # are excluded from dollars (§8/§3) exactly as they are from session counts.
    billable = [
        ln for ln in charges
        if not ln.is_void and not codes.is_non_session(ln.cpt)
    ]
    per_period: Dict[str, Dict[str, Decimal]] = {}
    for ln in billable:
        if ln.dos is None:
            exceptions["charge_lines_missing_dos"] += 1
            continue
        m = service_month(ln.dos)
        row = per_period.setdefault(
            m, {"billed": ZERO, "expected": ZERO, "collected": ZERO}
        )
        row["billed"] += ln.billed or ZERO
        row["expected"] += ln.expected or ZERO

    # --- cash side: collected, ALSO bucketed by DOS (never by posting date).
    for p in payments:
        if p.dos is None:
            exceptions["payments_missing_dos"] += 1
            continue
        if p.posting_date is None:
            exceptions["payments_missing_posting_date"] += 1
        elif p.posting_date < p.dos:
            exceptions["payments_posted_before_service"] += 1
        if p.collected < ZERO:
            exceptions["payment_sign_anomalies"] += 1
        m = service_month(p.dos)
        row = per_period.setdefault(
            m, {"billed": ZERO, "expected": ZERO, "collected": ZERO}
        )
        row["collected"] += p.collected

    # --- as_of: latest posting date in the data (deterministic, no wall clock).
    if as_of is None:
        posting_dates = [p.posting_date for p in payments if p.posting_date]
        as_of = max(posting_dates) if posting_dates else None

    curve: Dict[int, Decimal] = {}
    references: List[str] = []
    if as_of is not None:
        curve, references = build_maturity_curve(charges, payments, as_of, horizon)
    if not curve:
        notes.append(
            "no fully-matured reference months -- maturity cannot be modeled; "
            "every period is labeled UNKNOWN rather than assumed complete"
        )

    threshold = config.maturity_threshold
    rows: List[PeriodDollars] = []
    for m in sorted(per_period):
        vals = per_period[m]
        pd_row = PeriodDollars(
            period=m,
            billed_charges=vals["billed"],
            expected_collection=vals["expected"],
            collected=vals["collected"],
        )
        if as_of is not None and curve:
            age = months_between(date(int(m[:4]), int(m[5:7]), 1), as_of)
            mat = maturity_for_age(curve, age)
            pd_row.maturity = mat
            pd_row.is_complete = (mat is not None and mat >= threshold)
            pd_row.label = "COMPLETE" if pd_row.is_complete else "INCOMPLETE"
        rows.append(pd_row)

    totals = {
        "billed_charges": sum((r.billed_charges for r in rows), ZERO),
        "expected_collection": sum((r.expected_collection for r in rows), ZERO),
        "collected": sum((r.collected for r in rows), ZERO),
    }

    incomplete = [r.period for r in rows if r.label != "COMPLETE"]
    if incomplete:
        notes.append(
            f"{len(incomplete)} period(s) not fully mature: {', '.join(incomplete)} "
            f"(threshold {threshold}). Never compare these against a matured "
            "period without carrying the label."
        )
    for k, v in sorted(exceptions.items()):
        if v:
            notes.append(f"{v} {k.replace('_', ' ')}")

    return DollarLadder(
        periods=rows,
        totals=totals,
        maturity_curve=curve,
        reference_periods=references,
        as_of=as_of,
        threshold=threshold,
        exceptions=exceptions,
        notes=notes,
    )
