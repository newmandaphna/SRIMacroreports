"""Phase B -- the session ledger (ASSUMPTIONS.md §4-§9c).

Turns charge lines (plus appointments and note status) into session counts, with
every headline accompanied by the disagreeing views and the reconciliation
categories, so nothing is silently absorbed:

- all four session grains every run (headline = billable_encounters);
- the operative add-on list DERIVED from the data and cross-checked vs the seed;
- group therapy shown both one-per-group and per-attendee;
- voids/reversals excluded from counts but reported as their own category;
- duplicate charge lines deduped deterministically, with the count first-class;
- no-shows / cancellations kept as their own status, never as sessions;
- the appointments-vs-charges gap named and classified.

Money stays Decimal; nothing here uses a wall clock or dict ordering.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from . import codes, loaders
from .config import DEFAULT_CONFIG, Config
from .money import parse_money
from .periods import pay_period_from_dos

ZERO = Decimal("0")


# --------------------------------------------------------------------------- #
# Normalized records
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ChargeLine:
    provider: str          # normalized provider key (alnum); display name dropped
    patient: str           # in-memory grain key only, NEVER emitted
    dos: Optional[date]
    cpt: str               # TELE-stripped, normalized
    txn: str               # TELE-stripped, normalized transaction code
    modifiers: str
    units: Optional[Decimal]
    billed: Optional[Decimal]
    expected: Optional[Decimal]
    status: str            # normalized status text
    encounter_id: str

    @property
    def is_void(self) -> bool:
        s = self.status
        return "void" in s or "revers" in s

    @property
    def encounter_key(self) -> Tuple[str, str, str]:
        """One billable encounter = one patient x provider x date of service."""
        return (self.provider, self.patient, self.dos.isoformat() if self.dos else "")

    @property
    def full_key(self) -> tuple:
        """Identity of a whole charge line, for deterministic dedupe."""
        return (
            self.provider, self.patient,
            self.dos.isoformat() if self.dos else "",
            self.cpt, self.txn, self.modifiers,
            str(self.units), str(self.billed), str(self.expected),
            self.status, self.encounter_id,
        )


@dataclass(frozen=True)
class Appointment:
    provider: str
    patient: str
    dos: Optional[date]
    status_category: str   # kept | no_show | cancelled | rescheduled | other

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.provider, self.patient, self.dos.isoformat() if self.dos else "")


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #

@dataclass
class SessionLedger:
    grains: Dict[str, int]
    headline_grain: str
    headline_value: int
    voids: Dict[str, object]
    dedupe: Dict[str, int]
    addons: Dict[str, List[str]]
    group: Dict[str, int]
    appointment_status: Dict[str, int]
    reconciliation_gap: Dict[str, int]
    dollar_flags: Dict[str, int]
    pay_periods: List[str]
    notes: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Building normalized records from loader output
# --------------------------------------------------------------------------- #

def _col(header: Sequence[str], *tokens: str) -> Optional[str]:
    for tok in tokens:
        t = loaders._norm_alnum(tok)
        for h in header:
            if t and t in loaders._norm_alnum(h):
                return h
    return None


def _to_date(value: str) -> Optional[date]:
    # Reuse the profiler's tolerant, deterministic date parser.
    from .profile_raw import parse_date
    dt = parse_date(value)
    return dt.date() if dt is not None else None


def build_charge_lines(header: Sequence[str], records: Sequence[Dict[str, str]]) -> List[ChargeLine]:
    prov_c = _col(header, "ProviderID", "Provider")
    pat_c = _col(header, "PatientID", "Patient")
    dos_c = loaders.resolve_dos_column("charges", header)
    cpt_c = _col(header, "CPTCode", "CPT")
    txn_c = _col(header, "TransactionCode")
    mod_c = _col(header, "Modifiers", "Modifier")
    units_c = _col(header, "Units", "Unit")
    billed_c = _col(header, "Amount", "Billed Amount", "Billed", "Charge Amount")
    exp_c = _col(header, "ExpectedCollectionAmount", "Expected Amount",
                 "Expected", "Contracted Rate", "Allowed Amount")
    status_c = _col(header, "Charge Status", "ChargeStatus", "Status")
    enc_c = _col(header, "Encounter ID", "EncounterID", "Encounter/Charge ID",
                 "Charge ID", "ChargeID")

    def g(rec, c):
        return rec.get(c, "") if c else ""

    lines: List[ChargeLine] = []
    for rec in records:
        raw_cpt = g(rec, cpt_c)
        raw_txn = g(rec, txn_c) or raw_cpt  # some exports mirror cpt into txn
        lines.append(ChargeLine(
            provider=loaders._norm_alnum(g(rec, prov_c)),
            patient=loaders._norm_alnum(g(rec, pat_c)),
            dos=_to_date(g(rec, dos_c)),
            cpt=codes.base_code(raw_cpt),
            txn=codes.base_code(raw_txn),
            modifiers=loaders._norm_alnum(g(rec, mod_c)),
            units=parse_money(g(rec, units_c)),
            billed=parse_money(g(rec, billed_c)),
            expected=parse_money(g(rec, exp_c)),
            status=loaders._norm(g(rec, status_c)),
            encounter_id=loaders._norm_alnum(g(rec, enc_c)),
        ))
    return lines


def _appt_status_category(status: str) -> str:
    s = loaders._norm_alnum(status)
    if "noshow" in s:
        return "no_show"
    if "cancel" in s:
        return "cancelled"
    if "reschedul" in s:
        return "rescheduled"
    if any(k in s for k in ("kept", "arrived", "completed", "complete",
                            "seen", "attended", "checkedin")):
        return "kept"
    return "other"


def build_appointments(header: Sequence[str], records: Sequence[Dict[str, str]]) -> List[Appointment]:
    prov_c = _col(header, "ProviderID", "Provider")
    pat_c = _col(header, "PatientID", "Patient")
    dos_c = loaders.resolve_dos_column("appointments", header)
    status_c = _col(header, "Appointment Status", "Status")
    out: List[Appointment] = []
    for rec in records:
        out.append(Appointment(
            provider=loaders._norm_alnum(rec.get(prov_c, "") if prov_c else ""),
            patient=loaders._norm_alnum(rec.get(pat_c, "") if pat_c else ""),
            dos=_to_date(rec.get(dos_c, "") if dos_c else ""),
            status_category=_appt_status_category(rec.get(status_c, "") if status_c else ""),
        ))
    return out


def count_signed_notes(header: Sequence[str], records: Sequence[Dict[str, str]]) -> int:
    status_c = _col(header, "Note Status", "NoteStatus", "Documentation Status",
                    "Status")
    n = 0
    for rec in records:
        s = loaders._norm_alnum(rec.get(status_c, "") if status_c else "")
        if "signed" in s and "unsigned" not in s:
            n += 1
    return n


# --------------------------------------------------------------------------- #
# Ledger computation
# --------------------------------------------------------------------------- #

def dedupe_charges(lines: Sequence[ChargeLine]) -> Tuple[List[ChargeLine], int]:
    """Remove exact-duplicate charge lines, deterministically (stable sort on the
    full row key, not input order). Returns (deduped, n_removed)."""
    ordered = sorted(lines, key=lambda ln: ln.full_key)
    deduped: List[ChargeLine] = []
    last_key = None
    for ln in ordered:
        if ln.full_key != last_key:
            deduped.append(ln)
            last_key = ln.full_key
    return deduped, len(lines) - len(deduped)


def derive_addons(
    billable: Sequence[ChargeLine], config: Config = DEFAULT_CONFIG
) -> Dict[str, List[str]]:
    """Operative add-on list DERIVED from co-occurrence, cross-checked vs the seed.

    A code is an operative add-on if, on some encounter (patient x provider x
    DOS), it co-occurs with a primary E/M or psychotherapy code and is itself
    neither primary nor a non-session code. Both directions of discrepancy vs the
    seed are reported, never silently assumed (ASSUMPTIONS §5).
    """
    by_enc: Dict[Tuple[str, str, str], set] = {}
    for ln in billable:
        by_enc.setdefault(ln.encounter_key, set()).add(ln.cpt)

    operative: set = set()
    for cpts in by_enc.values():
        if any(codes.is_primary(c) for c in cpts):
            for c in cpts:
                if not codes.is_primary(c) and not codes.is_non_session(c):
                    operative.add(c)

    seed = {codes.normalize_code(c) for c in config.add_on_seed_codes}
    return {
        "operative": sorted(operative),
        "seed_not_observed": sorted(seed - operative),
        "unexpected_not_in_seed": sorted(operative - seed),
    }


def compute_ledger(
    charges: Sequence[ChargeLine],
    appointments: Sequence[Appointment] = (),
    signed_notes: int = 0,
    config: Config = DEFAULT_CONFIG,
) -> SessionLedger:
    notes: List[str] = []

    deduped, n_dupes = dedupe_charges(charges)

    void_lines = [ln for ln in deduped if ln.is_void]
    live = [ln for ln in deduped if not ln.is_void]
    # A billable line is a live line whose code is an actual clinical session.
    billable = [ln for ln in live if not codes.is_non_session(ln.cpt)]
    non_session = [ln for ln in live if codes.is_non_session(ln.cpt)]

    # --- grains ---
    charge_lines_grain = len(billable)
    encounter_keys = {ln.encounter_key for ln in billable}
    billable_encounters = len(encounter_keys)
    kept_keys = {a.key for a in appointments if a.status_category == "kept"}
    kept_appointments = len(kept_keys)
    grains = {
        "kept_appointments": kept_appointments,
        "billable_encounters": billable_encounters,
        "charge_lines": charge_lines_grain,
        "signed_notes": int(signed_notes),
    }

    # --- group therapy: both views ---
    group_lines = [ln for ln in billable if codes.is_group_therapy(ln.cpt)]
    group = {
        "one_per_group": len({(ln.provider, ln.dos.isoformat() if ln.dos else "")
                              for ln in group_lines}),
        "per_attendee": len(group_lines),
    }

    # --- voids (excluded from counts, reported as a category) ---
    voids = {
        "n_lines": len(void_lines),
        "billed_sum": str(sum((ln.billed for ln in void_lines if ln.billed), ZERO)),
        "expected_sum": str(sum((ln.expected for ln in void_lines if ln.expected), ZERO)),
    }

    # --- dollar flags: zero-dollar kept-but-flagged vs missing-price exception ---
    missing_price = sum(1 for ln in billable if ln.expected is None)
    zero_dollar = sum(1 for ln in billable if ln.expected == ZERO)
    if missing_price:
        notes.append(f"{missing_price} billable line(s) have no expected amount "
                     "(missing price -> exception, not a silent zero)")

    # --- appointment status tally (kept for their own sake, never sessions) ---
    appt_status = {k: 0 for k in ("kept", "no_show", "cancelled", "rescheduled", "other")}
    for a in appointments:
        appt_status[a.status_category] += 1

    # --- appointments-vs-charges gap, classified ---
    appts_without_charge = len(kept_keys - encounter_keys)
    charges_without_appt = len(encounter_keys - kept_keys)
    gap = {
        "kept_appointments": kept_appointments,
        "billable_encounters": billable_encounters,
        "matched": len(kept_keys & encounter_keys),
        "appts_without_charge": appts_without_charge,   # non-billable appointment
        "charges_without_appt": charges_without_appt,   # unbilled revenue / missing appt
    }
    if appts_without_charge or charges_without_appt:
        notes.append(
            f"appointments-vs-charges gap: {appts_without_charge} kept appt(s) "
            f"with no billable charge, {charges_without_appt} billable "
            "encounter(s) with no kept appointment"
        )

    pay_periods = sorted({pay_period_from_dos(ln.dos) for ln in billable if ln.dos})

    return SessionLedger(
        grains=grains,
        headline_grain=config.session_grain,
        headline_value=grains[config.session_grain],
        voids=voids,
        dedupe={"n_duplicate_lines_removed": n_dupes},
        addons=derive_addons(billable, config),
        group=group,
        appointment_status=appt_status,
        reconciliation_gap=gap,
        dollar_flags={"missing_price_lines": missing_price,
                      "zero_dollar_lines": zero_dollar,
                      "non_session_lines": len(non_session)},
        pay_periods=pay_periods,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# Convenience loaders (from files on disk)
# --------------------------------------------------------------------------- #

def load_charges(path: str) -> List[ChargeLine]:
    header, records = loaders.load_report(path, family="charges")
    return build_charge_lines(header, records)


def load_appointments(path: str) -> List[Appointment]:
    header, records = loaders.load_report(path, family="appointments")
    return build_appointments(header, records)


def load_signed_note_count(path: str) -> int:
    header, records = loaders.load_report(path, family="documentation")
    return count_signed_notes(header, records)
