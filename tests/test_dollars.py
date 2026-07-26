"""Phase C dollar ladder + claim-lag maturity (ASSUMPTIONS §10-§11)."""
from datetime import date
from decimal import Decimal
from pathlib import Path

from src import codes
from src.config import Config
from src.dollars import (
    build_maturity_curve,
    compute_ladder,
    lag_months,
    load_payments,
    maturity_for_age,
    months_between,
    service_month,
)
from src.sessions import load_charges

FIXTURES = Path(__file__).parent / "fixtures"
CHARGES = FIXTURES / "ChargesHistoryDetailProviderPatientCode_SYNTHETIC.csv"
STATEMENTS = FIXTURES / "PatientStatement_SYNTHETIC.csv"


def _inputs():
    return load_charges(str(CHARGES)), load_payments(str(STATEMENTS))


def _ladder(**kw):
    charges, payments = _inputs()
    return compute_ladder(charges, payments, **kw)


# --- payments parse, including the negative-credit sign convention ---

def test_payments_parse_with_negative_credit_convention():
    _, payments = _inputs()
    assert len(payments) == 6
    p = payments[0]
    assert p.dos == date(2024, 2, 11)
    assert p.posting_date == date(2024, 3, 5)     # separate from DOS
    assert p.insurance == Decimal("-50.00")
    assert p.collected == Decimal("60.00")        # -(ins + pat)
    assert all(isinstance(x.collected, Decimal) for x in payments)


def test_dos_and_posting_never_conflated():
    _, payments = _inputs()
    for p in payments:
        assert p.dos != p.posting_date
        assert p.posting_date >= p.dos


# --- the three measures, bucketed by DATE OF SERVICE ---

def test_three_measures_never_substituted():
    led = _ladder()
    jul = led.by_period("2026-07")
    # expected: 180+22 (P001) + 96.50+61 (P002) + 120 (P005) = 479.50
    assert jul.expected_collection == Decimal("479.50")
    # billed is a DIFFERENT number (gross list price), context only
    assert jul.billed_charges == Decimal("710.00")
    # collected is cash actually received for July DOS: 150 + 90
    assert jul.collected == Decimal("240.00")
    assert jul.billed_charges != jul.expected_collection != jul.collected


def test_collected_buckets_by_dos_not_posting_date():
    led = _ladder()
    # The 2026-07-15 payment POSTED in August must land in the July DOS bucket.
    assert led.by_period("2026-07").collected == Decimal("240.00")
    assert led.by_period("2026-08") is None  # no August service dates exist


def test_voids_and_non_session_codes_excluded_from_dollars():
    charges, payments = _inputs()
    led = compute_ladder(charges, payments)
    # The voided 2026-07-20 line (billed 250 / expected 180) is excluded.
    assert led.by_period("2026-07").expected_collection == Decimal("479.50")
    voided = [c for c in charges if c.is_void]
    assert voided and voided[0].expected == Decimal("180.00")


# --- claim-lag maturity ---

def test_lag_and_month_helpers():
    assert months_between(date(2026, 7, 1), date(2026, 8, 1)) == 1
    assert lag_months(date(2024, 2, 11), date(2024, 5, 20)) == 3
    assert service_month(date(2026, 7, 22)) == "2026-07"


def test_curve_fits_from_matured_reference_months_only():
    charges, payments = _inputs()
    curve, refs = build_maturity_curve(charges, payments, as_of=date(2026, 8, 1))
    # 2024-02 (30 months old) and 2025-07 (13 months) qualify; 2026-07 does not.
    assert refs == ["2024-02", "2025-07"]
    assert curve[0] == Decimal("0")          # nothing collected in month 0
    assert Decimal("0.9") < curve[1] < Decimal("0.95")
    assert curve[3] == Decimal("1")          # fully collected by lag 3


def test_recent_period_labeled_incomplete_and_matured_complete():
    led = _ladder()
    assert led.as_of == date(2026, 8, 1)     # derived from data, not a wall clock
    assert led.by_period("2026-07").label == "INCOMPLETE"
    assert led.by_period("2024-02").label == "COMPLETE"
    assert led.by_period("2025-07").label == "COMPLETE"
    assert led.incomplete_periods() == ["2026-07"]
    assert any("not fully mature" in n for n in led.notes)


def test_threshold_is_configurable_and_changes_the_label():
    charges, payments = _inputs()
    # July's modeled maturity is ~0.927: incomplete at 95%, complete at 90%.
    strict = compute_ladder(charges, payments, config=Config())
    lax = compute_ladder(charges, payments,
                         config=Config(maturity_threshold=Decimal("0.90")))
    assert strict.by_period("2026-07").label == "INCOMPLETE"
    assert lax.by_period("2026-07").label == "COMPLETE"


def test_no_curve_means_unknown_not_assumed_complete():
    charges, payments = _inputs()
    # as_of before any month matures -> no reference months -> no curve.
    led = compute_ladder(charges, payments, as_of=date(2024, 3, 1))
    assert led.maturity_curve == {}
    assert all(p.label == "UNKNOWN" for p in led.periods)
    assert any("maturity cannot be modeled" in n for n in led.notes)


def test_maturity_for_age_clamps_to_curve():
    curve = {0: Decimal("0"), 1: Decimal("0.5"), 2: Decimal("1")}
    assert maturity_for_age(curve, 0) == Decimal("0")
    assert maturity_for_age(curve, 99) == Decimal("1")   # beyond curve -> last
    assert maturity_for_age({}, 3) is None


# --- INVARIANT: money is conserved ---

def test_invariant_money_is_conserved_end_to_end():
    charges, payments = _inputs()
    led = compute_ladder(charges, payments)

    billable = [c for c in charges
                if not c.is_void and not codes.is_non_session(c.cpt)]
    raw_billed = sum((c.billed for c in billable if c.billed), Decimal("0"))
    raw_expected = sum((c.expected for c in billable if c.expected), Decimal("0"))
    raw_collected = sum((p.collected for p in payments), Decimal("0"))

    # Nothing created or destroyed between raw lines and the ladder totals.
    assert led.totals["billed_charges"] == raw_billed
    assert led.totals["expected_collection"] == raw_expected
    assert led.totals["collected"] == raw_collected

    # And the per-period rows sum to the totals (no row dropped or double counted).
    assert sum((r.billed_charges for r in led.periods), Decimal("0")) == raw_billed
    assert sum((r.expected_collection for r in led.periods), Decimal("0")) == raw_expected
    assert sum((r.collected for r in led.periods), Decimal("0")) == raw_collected


def test_all_ladder_money_is_decimal_never_float():
    led = _ladder()
    for r in led.periods:
        for v in (r.billed_charges, r.expected_collection, r.collected):
            assert isinstance(v, Decimal)
            assert not isinstance(v, float)
    for v in led.totals.values():
        assert isinstance(v, Decimal)


def test_ladder_is_deterministic():
    a = _ladder()
    b = _ladder()
    assert [(r.period, r.expected_collection, r.collected, r.label) for r in a.periods] \
        == [(r.period, r.expected_collection, r.collected, r.label) for r in b.periods]
    assert a.reference_periods == b.reference_periods
