"""Phase E reconciliation harness (ASSUMPTIONS §15)."""
from decimal import Decimal
from pathlib import Path

from src.config import Config
from src.dollars import load_payments
from src.reconcile import (
    CAUSE_VOCABULARY,
    EXPLAINED,
    INSUFFICIENT_SOURCES,
    RECONCILED,
    UNRESOLVED,
    RosterSnapshot,
    SourceValue,
    _classify,
    reconcile_active_providers,
    reconcile_all,
    reconcile_expected_revenue,
    reconcile_session_count,
    reconcile_unique_patients,
    render_reconciliation_md,
)
from src.sessions import compute_ledger, load_appointments, load_charges, load_notes

FIXTURES = Path(__file__).parent / "fixtures"
CHARGES = FIXTURES / "ChargesHistoryDetailProviderPatientCode_SYNTHETIC.csv"
APPTS = FIXTURES / "AppointmentsPatientInfoByProviderThenDayThenFacility_SYNTHETIC.csv"
NOTES = FIXTURES / "AppointmentDocumentation_SYNTHETIC.csv"
STATEMENTS = FIXTURES / "PatientStatement_SYNTHETIC.csv"


def _all():
    charges = load_charges(str(CHARGES))
    appts = load_appointments(str(APPTS))
    notes = load_notes(str(NOTES))
    payments = load_payments(str(STATEMENTS))
    ledger = compute_ledger(charges, appts, sum(1 for n in notes if n.is_signed))
    return charges, appts, notes, payments, ledger


# --- the cardinal rule: never average ---

def test_disagreeing_sources_are_never_averaged():
    v = _classify(
        "demo", "A",
        [SourceValue("A", 10), SourceValue("B", 20)],
        tolerance=Decimal("0.01"),
    )
    assert v.status in (EXPLAINED, UNRESOLVED)
    # The carried value is one of the SOURCE values -- never their mean (15).
    assert v.reported_value == 10
    assert v.reported_value in {sv.value for sv in v.values}
    assert v.reported_value != 15


def test_every_headline_carries_a_source_value_not_a_blend():
    charges, appts, notes, payments, ledger = _all()
    rep = reconcile_all(ledger, charges, payments, appts, notes)
    for v in rep.variances:
        if v.values:
            assert v.reported_value in {sv.value for sv in v.values}


# --- within tolerance reconciles ---

def test_within_tolerance_is_reconciled():
    v = _classify("demo", "A",
                  [SourceValue("A", Decimal("1000.00")),
                   SourceValue("B", Decimal("1000.50"))],
                  tolerance=Decimal("0.01"))
    assert v.status == RECONCILED
    assert v.is_clean


def test_beyond_tolerance_is_not_reconciled():
    v = _classify("demo", "A",
                  [SourceValue("A", Decimal("1000.00")),
                   SourceValue("B", Decimal("1200.00"))],
                  tolerance=Decimal("0.01"))
    assert v.status != RECONCILED


# --- fewer than two sources is named, not faked ---

def test_single_source_is_reported_uncorroborated():
    v = _classify("demo", "A", [SourceValue("A", 42)], tolerance=Decimal("0.01"))
    assert v.status == INSUFFICIENT_SOURCES
    assert any("cannot be corroborated" in n for n in v.notes)


def test_expected_revenue_without_grand_total_is_uncorroborated():
    charges, *_ = _all()
    v = reconcile_expected_revenue(charges)          # fixture has no grand total
    assert v.status == INSUFFICIENT_SOURCES
    assert v.reported_value == Decimal("729.50")


def test_expected_revenue_with_grand_total_reconciles():
    charges, *_ = _all()
    v = reconcile_expected_revenue(charges, grand_total_expected=Decimal("729.50"))
    assert v.status == RECONCILED
    assert len(v.values) == 2


def test_expected_revenue_grand_total_mismatch_is_named():
    charges, *_ = _all()
    v = reconcile_expected_revenue(charges, grand_total_expected=Decimal("900.00"))
    assert v.status in (EXPLAINED, UNRESOLVED)
    assert v.spread == Decimal("170.50")
    # Causes come from the fixed §15 vocabulary.
    assert set(v.causes).issubset(set(CAUSE_VOCABULARY))


# --- session count across three files, with causes named ---

def test_session_count_uses_three_independent_files():
    _, _, _, _, ledger = _all()
    v = reconcile_session_count(ledger)
    assert [s.source for s in v.values] == ["charges", "appointments", "documentation"]
    assert v.reported_value == 5      # charges is primary
    # 5 vs 4 vs 3 is beyond a 1% tolerance, so it must be named, not smoothed.
    assert v.status in (EXPLAINED, UNRESOLVED)
    assert set(v.causes).issubset(set(CAUSE_VOCABULARY))

    # add_ons/voids describe how OUR number was derived inside the charges file.
    # They are NOT allowed to explain a gap against another file -- keeping them
    # separate is what prevents nonsense like "3 causes explain a 2 gap".
    assert v.derivation["add_ons"] == 2   # 7 charge lines -> 5 encounters
    assert v.derivation["voids"] == 1
    assert "add_ons" not in v.causes

    # The widest gap is against documentation (5 vs 3), and NO §15 cause covers
    # an unsigned note, so it stays honestly unexplained.
    assert v.compared_with == "documentation"
    assert v.status == UNRESOLVED
    assert v.unexplained == 2
    assert any("LEAKAGE" in n for n in v.notes)


def test_pair_scoped_causes_explain_only_their_own_pair():
    # A gap against appointments IS covered by date_boundary, and the arithmetic
    # must use only that pair's causes.
    v = _classify(
        "demo", "A",
        [SourceValue("A", 10), SourceValue("B", 8)],
        tolerance=Decimal("0.01"),
        causes_by_source={"B": {"date_boundary": 2}, "C": {"voids": 99}},
    )
    assert v.compared_with == "B"
    assert v.status == EXPLAINED
    assert v.unexplained == 0
    assert v.causes == {"date_boundary": 2}   # C's causes never leak in


def test_causes_only_come_from_the_fixed_vocabulary():
    charges, appts, notes, payments, ledger = _all()
    rep = reconcile_all(ledger, charges, payments, appts, notes)
    for v in rep.variances:
        assert set(v.causes).issubset(set(CAUSE_VOCABULARY)), v.causes


# --- unique patients: counts only, never keys (PHI) ---

def test_unique_patients_counts_only():
    charges, appts, notes, _, _ = _all()
    v = reconcile_unique_patients(charges, appts, notes)
    assert all(isinstance(s.value, int) for s in v.values)
    rendered = render_reconciliation_md(
        reconcile_all(compute_ledger(charges, appts, 3), charges, [], appts, notes)
    )
    for token in ("P001", "P002", "P003", "P004", "P005", "P006", "P007"):
        assert token not in rendered


# --- active providers: the 41-vs-43 style gap, carried by name ---

def test_active_providers_carries_roster_gap_by_name():
    charges, appts, _, _, _ = _all()
    # Roster has 4 active providers; only 3 billed, and one biller is unknown.
    roster = RosterSnapshot(
        active=frozenset({"dralicerivera", "drbobnguyen", "drcarolsmith", "drdanalee"}),
        inactive=frozenset({"drevekim"}),
    )
    v = reconcile_active_providers(charges, appts, roster)
    joined = " ".join(v.notes)
    assert "zero_sessions" in joined and "drdanalee" in joined
    assert v.causes["roster_exclusions"] >= 1
    # The roster count and the billing count are both reported, neither averaged.
    assert {s.value for s in v.values} >= {3, 4}


def test_unknown_biller_is_blocked_by_name():
    charges, appts, _, _, _ = _all()
    roster = RosterSnapshot(active=frozenset({"dralicerivera"}))
    v = reconcile_active_providers(charges, appts, roster)
    joined = " ".join(v.notes)
    assert "provider_not_in_config" in joined
    assert "drbobnguyen" in joined and "drcarolsmith" in joined


def test_inactive_biller_is_blocked_by_name():
    charges, appts, _, _, _ = _all()
    roster = RosterSnapshot(
        active=frozenset({"dralicerivera", "drcarolsmith"}),
        inactive=frozenset({"drbobnguyen"}),
    )
    v = reconcile_active_providers(charges, appts, roster)
    assert "therapist_inactive" in " ".join(v.notes)


# --- the document: every unresolved variance is named ---

def test_reconciliation_md_names_every_unresolved_variance():
    charges, appts, notes, payments, ledger = _all()
    rep = reconcile_all(ledger, charges, payments, appts, notes)
    md = render_reconciliation_md(rep)
    assert "# Reconciliation" in md
    for v in rep.unresolved:
        assert v.headline in md
        assert "unexplained residual" in md
    for v in rep.uncorroborated:
        assert v.headline in md
        assert "not corroborated" in md


def test_report_flags_itself_as_not_clean_when_anything_is_open():
    charges, appts, notes, payments, ledger = _all()
    rep = reconcile_all(ledger, charges, payments, appts, notes)
    # The fixtures genuinely have open items, so the harness must not claim clean.
    assert rep.clean is False
    assert rep.unresolved or rep.uncorroborated


def test_tolerance_is_configurable():
    charges, *_ = _all()
    loose = Config(reconciliation_tolerance=Decimal("0.50"))
    v = reconcile_expected_revenue(charges, Decimal("900.00"), loose)
    assert v.status == RECONCILED   # 170.50/900 = 0.19 < 0.50
