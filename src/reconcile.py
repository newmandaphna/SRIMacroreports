"""Phase E -- the reconciliation harness (ASSUMPTIONS.md §15).

Every headline is derived from **at least two independent source files**. When
sources disagree by more than the tolerance the harness does exactly one thing:
it **names the variance**. It never averages, never picks a "best" number
silently, and never lets a difference dissolve into rounding.

The rule that governs this module: *a variance with no name is a bug, not a
rounding artifact.* So every disagreement is either

  - RECONCILED          within tolerance,
  - EXPLAINED           fully accounted for by named causes,
  - UNRESOLVED          a residual nobody can explain -> goes in the report, or
  - INSUFFICIENT_SOURCES  fewer than two independent sources exist for it.

That last state is deliberate. Where the four Valant reports genuinely cannot
corroborate a headline twice, the honest outcome is to say so -- not to invent a
second derivation from the same file and call it independent.

Causes are drawn from the fixed vocabulary in §15: add_ons, group_attendees,
voids, roster_exclusions, date_boundary, dedupe.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple, Union

from . import codes
from .config import DEFAULT_CONFIG, Config
from .sessions import Appointment, ChargeLine, NoteRecord, SessionLedger

ZERO = Decimal("0")

Number = Union[int, Decimal]

CAUSE_VOCABULARY = (
    "add_ons",
    "group_attendees",
    "voids",
    "roster_exclusions",
    "date_boundary",
    "dedupe",
)

RECONCILED = "RECONCILED"
EXPLAINED = "EXPLAINED"
UNRESOLVED = "UNRESOLVED"
INSUFFICIENT_SOURCES = "INSUFFICIENT_SOURCES"


# --------------------------------------------------------------------------- #
# Roster snapshot (a runtime input -- ASSUMPTIONS §1, never a code dependency)
# --------------------------------------------------------------------------- #

@dataclass
class RosterSnapshot:
    """Read-only snapshot of the engine roster for the analyzed period."""
    active: frozenset            # normalized provider keys, active
    inactive: frozenset = frozenset()
    source: str = "roster snapshot"

    @property
    def known(self) -> frozenset:
        return self.active | self.inactive


# --------------------------------------------------------------------------- #
# Variance
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SourceValue:
    source: str      # the FILE or artifact this came from
    value: Number
    detail: str = ""


@dataclass
class Variance:
    headline: str
    primary_source: str
    values: List[SourceValue]
    tolerance: Decimal
    status: str
    # Causes for the WIDEST disagreeing pair. Only these enter the arithmetic --
    # a cause may only explain the specific pair it belongs to.
    causes: Dict[str, Number] = field(default_factory=dict)
    # How the primary number was derived within its own file (add-ons collapsed,
    # voids dropped, duplicates removed...). Shown for transparency but NEVER
    # counted against a cross-source gap: it explains our number, not their
    # difference. Conflating the two is how "3 of a 2 gap" nonsense appears.
    derivation: Dict[str, Number] = field(default_factory=dict)
    causes_by_source: Dict[str, Dict[str, Number]] = field(default_factory=dict)
    compared_with: Optional[str] = None
    unexplained: Optional[Number] = None
    notes: List[str] = field(default_factory=list)

    @property
    def reported_value(self) -> Optional[Number]:
        """The value carried forward -- ALWAYS one source's number, never a blend."""
        for sv in self.values:
            if sv.source == self.primary_source:
                return sv.value
        return self.values[0].value if self.values else None

    @property
    def spread(self) -> Number:
        if len(self.values) < 2:
            return 0
        nums = [Decimal(str(v.value)) for v in self.values]
        return max(nums) - min(nums)

    @property
    def is_clean(self) -> bool:
        return self.status == RECONCILED


def _rel_diff(a: Number, b: Number) -> Decimal:
    da, db = Decimal(str(a)), Decimal(str(b))
    scale = max(abs(da), abs(db))
    if scale == ZERO:
        return ZERO
    return abs(da - db) / scale


def _classify(
    headline: str,
    primary: str,
    values: Sequence[SourceValue],
    tolerance: Decimal,
    causes_by_source: Optional[Dict[str, Dict[str, Number]]] = None,
    derivation: Optional[Dict[str, Number]] = None,
    notes: Optional[List[str]] = None,
) -> Variance:
    """Build a Variance, deciding its status. Never averages the sources.

    Causes are PAIR-SCOPED: only causes declared for the source we actually
    disagree with may explain that disagreement. Within-file derivation counts
    (add-ons collapsed, voids dropped) explain OUR number, not their difference,
    and are deliberately kept out of the arithmetic.
    """
    cbs = {
        src: {k: v for k, v in (d or {}).items() if v}
        for src, d in (causes_by_source or {}).items()
    }
    derivation = {k: v for k, v in (derivation or {}).items() if v}
    notes = list(notes or [])
    vals = list(values)

    if len(vals) < 2:
        notes.append(
            "fewer than two independent sources are available for this headline; "
            "it cannot be corroborated and is reported UNCORROBORATED rather than "
            "given a fabricated second derivation"
        )
        return Variance(
            headline=headline, primary_source=primary, values=vals,
            tolerance=tolerance, status=INSUFFICIENT_SOURCES, causes={},
            derivation=derivation, causes_by_source=cbs, compared_with=None,
            unexplained=None, notes=notes,
        )

    prim = next((v for v in vals if v.source == primary), vals[0])
    worst = max(vals, key=lambda v: _rel_diff(prim.value, v.value))
    if _rel_diff(prim.value, worst.value) <= tolerance:
        return Variance(
            headline=headline, primary_source=primary, values=vals,
            tolerance=tolerance, status=RECONCILED, causes={},
            derivation=derivation, causes_by_source=cbs,
            compared_with=worst.source, unexplained=None, notes=notes,
        )

    # Disagreement beyond tolerance: name it. Do NOT average.
    pair_causes = cbs.get(worst.source, {})
    gap = abs(Decimal(str(prim.value)) - Decimal(str(worst.value)))
    explained = sum((abs(Decimal(str(v))) for v in pair_causes.values()), ZERO)
    unexplained = gap - explained
    status = EXPLAINED if unexplained == ZERO and pair_causes else UNRESOLVED
    if status == UNRESOLVED:
        notes.append(
            f"unexplained residual of {unexplained} between `{prim.source}` "
            f"({prim.value}) and `{worst.source}` ({worst.value}); named causes "
            f"account for {explained} of the {gap} gap"
        )
    return Variance(
        headline=headline, primary_source=primary, values=vals,
        tolerance=tolerance, status=status, causes=dict(pair_causes),
        derivation=derivation, causes_by_source=cbs,
        compared_with=worst.source, unexplained=unexplained, notes=notes,
    )


# --------------------------------------------------------------------------- #
# Per-headline reconciliations
# --------------------------------------------------------------------------- #

def reconcile_session_count(
    ledger: SessionLedger,
    config: Config = DEFAULT_CONFIG,
) -> Variance:
    """charges (billable encounters) vs appointments (kept) vs notes (signed)."""
    values = [
        SourceValue("charges", ledger.grains["billable_encounters"],
                    "distinct patient x provider x DOS on billable lines"),
        SourceValue("appointments", ledger.grains["kept_appointments"],
                    "appointments with a kept/arrived status"),
        SourceValue("documentation", ledger.grains["signed_notes"],
                    "notes in a signed state"),
    ]
    # How OUR number was derived inside the charges file (not a cross-source cause).
    derivation = {
        "add_ons": ledger.grains["charge_lines"] - ledger.grains["billable_encounters"],
        "group_attendees": ledger.group["per_attendee"] - ledger.group["one_per_group"],
        "voids": ledger.voids["n_lines"],
        "dedupe": ledger.dedupe["n_duplicate_lines_removed"],
    }
    # Why each OTHER source differs from ours.
    gap = ledger.reconciliation_gap
    causes_by_source = {
        "appointments": {
            "date_boundary": gap["charges_without_appt"] + gap["appts_without_charge"],
        },
        # No §15 cause covers an unsigned note, so a charges-vs-documentation gap
        # is deliberately left UNEXPLAINED rather than mislabelled.
        "documentation": {},
    }
    notes = []
    unsigned = ledger.grains["billable_encounters"] - ledger.grains["signed_notes"]
    if unsigned > 0:
        notes.append(
            f"{unsigned} billable encounter(s) have no signed note. This is the "
            "LEAKAGE signal (sessions held for unsigned notes); it is NOT one of "
            "the §15 causes, so it cannot be used to explain the variance away."
        )
    return _classify("session_count", "charges", values,
                     config.reconciliation_tolerance, causes_by_source,
                     derivation, notes)


def reconcile_expected_revenue(
    charges: Sequence[ChargeLine],
    grand_total_expected: Optional[Decimal] = None,
    config: Config = DEFAULT_CONFIG,
) -> Variance:
    """Row-by-row sum vs Valant's own printed grand total.

    Valant prints per-report grand totals; the engine reconciles its raw sums
    against them (importer.py:335-348). That printed figure is computed by Valant
    independently of our row arithmetic, so it is a genuine second source. When
    the export lacks it, we say so rather than manufacture one.
    """
    billable = [ln for ln in charges
                if not ln.is_void and not codes.is_non_session(ln.cpt)]
    ours = sum((ln.expected or ZERO for ln in billable), ZERO)
    values = [SourceValue("charges (row sum)", ours,
                          f"{len(billable)} billable lines")]
    if grand_total_expected is not None:
        values.append(SourceValue("charges (Valant grand total)",
                                  grand_total_expected,
                                  "printed by Valant on the report"))
    # Valant's printed total includes rows we deliberately exclude, so those
    # exclusions are exactly what should explain a gap against it.
    excluded_voids = sum((ln.expected or ZERO for ln in charges if ln.is_void), ZERO)
    excluded_non_session = sum(
        (ln.expected or ZERO for ln in charges
         if not ln.is_void and codes.is_non_session(ln.cpt)), ZERO
    )
    causes_by_source = {
        "charges (Valant grand total)": {
            "voids": excluded_voids,
            "add_ons": excluded_non_session,
        }
    }
    return _classify("expected_revenue", "charges (row sum)", values,
                     config.reconciliation_tolerance, causes_by_source)


def reconcile_collected_revenue(
    payments: Sequence,
    grand_total_collected: Optional[Decimal] = None,
    config: Config = DEFAULT_CONFIG,
) -> Variance:
    """Statement row sum vs the statement's printed total, when present."""
    ours = sum((p.collected for p in payments), ZERO)
    values = [SourceValue("statements (row sum)", ours,
                          f"{len(payments)} payment rows")]
    if grand_total_collected is not None:
        values.append(SourceValue("statements (printed total)",
                                  grand_total_collected, "printed by Valant"))
    return _classify("collected_revenue", "statements (row sum)", values,
                     config.reconciliation_tolerance)


def reconcile_unique_patients(
    charges: Sequence[ChargeLine],
    appointments: Sequence[Appointment] = (),
    notes: Sequence[NoteRecord] = (),
    config: Config = DEFAULT_CONFIG,
) -> Variance:
    """Distinct patient COUNTS from three files. Keys stay in memory (§17)."""
    billable = [ln for ln in charges
                if not ln.is_void and not codes.is_non_session(ln.cpt)]
    charge_pat = {ln.patient for ln in billable if ln.patient}
    appt_pat = {a.patient for a in appointments
                if a.patient and a.status_category == "kept"}
    note_pat = {n.patient for n in notes if n.patient}

    values = [SourceValue("charges", len(charge_pat), "distinct billed patients")]
    if appointments:
        values.append(SourceValue("appointments", len(appt_pat),
                                  "distinct kept-appointment patients"))
    if notes:
        values.append(SourceValue("documentation", len(note_pat),
                                  "distinct documented patients"))
    causes_by_source = {
        # Patients appearing in one file but not the other -- the honest,
        # symmetric explanation of a distinct-count difference.
        "appointments": {"date_boundary": len(charge_pat ^ appt_pat)},
        "documentation": {"date_boundary": len(charge_pat ^ note_pat)},
    }
    derivation = {
        "voids": len({ln.patient for ln in charges if ln.is_void} - charge_pat),
    }
    return _classify("unique_patients", "charges", values,
                     config.reconciliation_tolerance, causes_by_source, derivation)


def reconcile_active_providers(
    charges: Sequence[ChargeLine],
    appointments: Sequence[Appointment] = (),
    roster: Optional[RosterSnapshot] = None,
    config: Config = DEFAULT_CONFIG,
) -> Variance:
    """Providers billing vs roster vs providers with appointments.

    This is where the engine's 41-vs-43 discrepancy lives (Build Brief:215): more
    providers on the comp workbook than actually billed. The gap is carried by
    name using the engine's taxonomy -- provider_not_in_config, therapist_inactive,
    zero_sessions -- never resolved silently.
    """
    billable = [ln for ln in charges
                if not ln.is_void and not codes.is_non_session(ln.cpt)]
    billing = {ln.provider for ln in billable if ln.provider}
    appt_prov = {a.provider for a in appointments
                 if a.provider and a.status_category == "kept"}

    values = [SourceValue("charges", len(billing), "providers with billed lines")]
    if appointments:
        values.append(SourceValue("appointments", len(appt_prov),
                                  "providers with kept appointments"))
    causes_by_source: Dict[str, Dict[str, Number]] = {
        "appointments": {"date_boundary": len(billing ^ appt_prov)},
    }
    notes: List[str] = []
    if roster is not None:
        values.append(SourceValue(roster.source, len(roster.active),
                                  "active providers on the roster snapshot"))
        not_in_config = sorted(billing - roster.known)
        inactive_billed = sorted(billing & roster.inactive)
        zero_sessions = sorted(roster.active - billing)
        causes_by_source[roster.source] = {
            "roster_exclusions": (
                len(not_in_config) + len(inactive_billed) + len(zero_sessions)
            )
        }
        if not_in_config:
            notes.append(
                f"provider_not_in_config (BLOCK): {len(not_in_config)} provider(s) "
                f"billed but are not on the roster: {', '.join(not_in_config)}"
            )
        if inactive_billed:
            notes.append(
                f"therapist_inactive (BLOCK): {len(inactive_billed)} provider(s) "
                f"billed but are marked inactive: {', '.join(inactive_billed)}"
            )
        if zero_sessions:
            notes.append(
                f"zero_sessions (WARNING): {len(zero_sessions)} active roster "
                f"provider(s) billed nothing: {', '.join(zero_sessions)}"
            )
    return _classify("active_providers", "charges", values,
                     config.reconciliation_tolerance, causes_by_source, None, notes)


# --------------------------------------------------------------------------- #
# The whole harness
# --------------------------------------------------------------------------- #

@dataclass
class ReconciliationReport:
    variances: List[Variance]
    tolerance: Decimal

    @property
    def unresolved(self) -> List[Variance]:
        return [v for v in self.variances if v.status == UNRESOLVED]

    @property
    def uncorroborated(self) -> List[Variance]:
        return [v for v in self.variances if v.status == INSUFFICIENT_SOURCES]

    @property
    def clean(self) -> bool:
        return not self.unresolved and not self.uncorroborated

    def by_headline(self, name: str) -> Optional[Variance]:
        return next((v for v in self.variances if v.headline == name), None)


def reconcile_all(
    ledger: SessionLedger,
    charges: Sequence[ChargeLine],
    payments: Sequence = (),
    appointments: Sequence[Appointment] = (),
    notes: Sequence[NoteRecord] = (),
    roster: Optional[RosterSnapshot] = None,
    grand_total_expected: Optional[Decimal] = None,
    grand_total_collected: Optional[Decimal] = None,
    config: Config = DEFAULT_CONFIG,
) -> ReconciliationReport:
    return ReconciliationReport(
        variances=[
            reconcile_session_count(ledger, config),
            reconcile_expected_revenue(charges, grand_total_expected, config),
            reconcile_collected_revenue(payments, grand_total_collected, config),
            reconcile_unique_patients(charges, appointments, notes, config),
            reconcile_active_providers(charges, appointments, roster, config),
        ],
        tolerance=config.reconciliation_tolerance,
    )


# --------------------------------------------------------------------------- #
# docs/reconciliation.md -- every unresolved variance, named
# --------------------------------------------------------------------------- #

def render_reconciliation_md(report: ReconciliationReport) -> str:
    """Render the reconciliation document. Aggregates only -- no PHI."""
    out: List[str] = [
        "# Reconciliation -- SRI Practice Health Analytics",
        "",
        "_Generated by `src/reconcile.py` (ASSUMPTIONS §15). Aggregate-only._",
        "",
        "Every headline below is derived from **independent source files**. Where "
        "they disagree by more than the tolerance, the difference is **named** -- "
        "never averaged, never absorbed. A variance with no name is a bug, not a "
        "rounding artifact.",
        "",
        f"**Tolerance:** {report.tolerance} (relative)",
        "",
        "| headline | sources | values | spread | status |",
        "|---|---|---|---:|---|",
    ]
    for v in report.variances:
        srcs = "<br>".join(f"`{s.source}`" for s in v.values)
        vals = "<br>".join(str(s.value) for s in v.values)
        out.append(f"| **{v.headline}** | {srcs} | {vals} | {v.spread} | {v.status} |")
    out.append("")

    unresolved = report.unresolved
    out.append("## Unresolved variances")
    out.append("")
    if not unresolved:
        out.append("_None. Every disagreement is either within tolerance or fully "
                   "accounted for by named causes._")
    else:
        for v in unresolved:
            out.append(f"### {v.headline}")
            out.append("")
            for s in v.values:
                out.append(f"- `{s.source}` = **{s.value}** ({s.detail})")
            out.append(f"- widest disagreement is with `{v.compared_with}`")
            if v.causes:
                out.append("- named causes for THAT pair:")
                for cause in CAUSE_VOCABULARY:
                    if v.causes.get(cause):
                        out.append(f"  - `{cause}`: {v.causes[cause]}")
            else:
                out.append("- named causes for that pair: **none apply**")
            if v.derivation:
                out.append(
                    "- how our own number was derived (context only -- these do "
                    "NOT explain the cross-source gap):"
                )
                for cause in CAUSE_VOCABULARY:
                    if v.derivation.get(cause):
                        out.append(f"  - `{cause}`: {v.derivation[cause]}")
            out.append(f"- **unexplained residual: {v.unexplained}**")
            for n in v.notes:
                out.append(f"- {n}")
            out.append("")

    uncorroborated = report.uncorroborated
    out.append("## Uncorroborated headlines (fewer than two independent sources)")
    out.append("")
    if not uncorroborated:
        out.append("_None._")
    else:
        for v in uncorroborated:
            src = v.values[0].source if v.values else "n/a"
            val = v.values[0].value if v.values else "n/a"
            out.append(
                f"- **{v.headline}** = {val} from `{src}` only. No second "
                "independent source exists in the supplied exports, so this "
                "number is **not corroborated**. Supply the report that carries "
                "the corresponding total to close this."
            )
        out.append("")

    out.append("## Explained variances (named causes cover the whole gap)")
    out.append("")
    explained = [v for v in report.variances if v.status == EXPLAINED]
    if not explained:
        out.append("_None._")
    else:
        for v in explained:
            causes = ", ".join(f"`{c}`={v.causes[c]}" for c in CAUSE_VOCABULARY
                               if v.causes.get(c))
            out.append(f"- **{v.headline}**: spread {v.spread}, fully explained by {causes}")
    out.append("")

    # Provider-roster findings use the engine's taxonomy and are always shown.
    prov = report.by_headline("active_providers")
    if prov is not None and prov.notes:
        out.append("## Provider roster findings (engine taxonomy)")
        out.append("")
        for n in prov.notes:
            out.append(f"- {n}")
        out.append("")
    return "\n".join(out)
