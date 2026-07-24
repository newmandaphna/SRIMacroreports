"""Phase B session ledger (ASSUMPTIONS §4-§9c), including property tests."""
from datetime import date
from decimal import Decimal
from pathlib import Path

from src import codes
from src.sessions import (
    ChargeLine,
    compute_ledger,
    dedupe_charges,
    derive_addons,
    load_appointments,
    load_charges,
    load_signed_note_count,
)

FIXTURES = Path(__file__).parent / "fixtures"
CHARGES = FIXTURES / "ChargesHistoryDetailProviderPatientCode_SYNTHETIC.csv"
APPTS = FIXTURES / "AppointmentsPatientInfoByProviderThenDayThenFacility_SYNTHETIC.csv"
NOTES = FIXTURES / "AppointmentDocumentation_SYNTHETIC.csv"


def _ledger():
    charges = load_charges(str(CHARGES))
    appts = load_appointments(str(APPTS))
    signed = load_signed_note_count(str(NOTES))
    return charges, appts, signed, compute_ledger(charges, appts, signed)


# --- code vocabulary ---

def test_code_vocabulary():
    assert codes.is_non_session("99999") and codes.is_non_session("TELE99999")
    assert codes.base_code("TELE90837") == "90837"
    assert codes.is_group_therapy("90853")
    assert codes.is_primary("90837") and codes.is_primary("99213")
    assert not codes.is_primary("90785")  # interactive-complexity add-on


# --- the four grains, headline, and that disagreement is visible ---

def test_four_grains_and_headline():
    _, _, _, led = _ledger()
    assert led.grains == {
        "kept_appointments": 4,
        "billable_encounters": 5,
        "charge_lines": 7,   # add-on lines counted here but not as encounters
        "signed_notes": 3,
    }
    assert led.headline_grain == "billable_encounters"
    assert led.headline_value == 5
    # The whole point: charge_lines (7) != billable_encounters (5) is visible.
    assert led.grains["charge_lines"] != led.grains["billable_encounters"]


# --- add-on list derived from data, cross-checked vs seed (both directions) ---

def test_addons_derived_and_cross_checked():
    _, _, _, led = _ledger()
    assert led.addons["operative"] == ["90785", "90833"]
    # 90836 and 90838 are in the seed but never co-occurred with a primary here.
    assert "90836" in led.addons["seed_not_observed"]
    assert "90838" in led.addons["seed_not_observed"]
    assert led.addons["unexpected_not_in_seed"] == []


# --- group therapy: both views reported ---

def test_group_therapy_dual_view():
    _, _, _, led = _ledger()
    assert led.group == {"one_per_group": 1, "per_attendee": 1}


# --- voids excluded from counts but reported as their own category ---

def test_voids_excluded_but_reported():
    _, _, _, led = _ledger()
    assert led.voids["n_lines"] == 1
    assert led.voids["billed_sum"] == "250.00"
    assert led.voids["expected_sum"] == "180.00"
    # The voided line (Bob 2026-07-20) is NOT among the 5 billable encounters.
    assert led.grains["billable_encounters"] == 5


# --- no-shows / cancellations kept as their own status, never sessions ---

def test_appointment_status_kept_separately():
    _, _, _, led = _ledger()
    assert led.appointment_status["kept"] == 4
    assert led.appointment_status["no_show"] == 1
    assert led.appointment_status["cancelled"] == 1


# --- appointments-vs-charges gap named and classified ---

def test_reconciliation_gap_classified():
    _, _, _, led = _ledger()
    g = led.reconciliation_gap
    assert g["matched"] == 4
    assert g["appts_without_charge"] == 0       # non-billable appointment
    assert g["charges_without_appt"] == 1       # unbilled revenue (P005, no appt)


def test_pay_periods_are_semimonthly():
    _, _, _, led = _ledger()
    assert led.pay_periods == [
        "2024-02 Period 1", "2025-07 Period 1",
        "2026-07 Period 1", "2026-07 Period 2",
    ]


# ---------------------------------------------------------------- #
# Property tests (ASSUMPTIONS §5, §9): the invariants, not examples
# ---------------------------------------------------------------- #

def test_property_addon_line_does_not_change_session_count():
    charges = load_charges(str(CHARGES))
    before = compute_ledger(charges).grains["billable_encounters"]
    # Append an add-on line onto an EXISTING encounter (same patient/provider/DOS).
    existing = next(ln for ln in charges if ln.cpt == "90837")
    addon = ChargeLine(
        provider=existing.provider, patient=existing.patient, dos=existing.dos,
        cpt="90785", txn="90785", modifiers="ic",  # distinct row, not a dupe
        units=Decimal("1"), billed=Decimal("40"), expected=Decimal("22"),
        status="posted", encounter_id=existing.encounter_id,
    )
    after = compute_ledger(list(charges) + [addon])
    assert after.grains["billable_encounters"] == before          # unchanged
    assert after.grains["charge_lines"] == len([c for c in charges
                                                if not c.is_void]) + 1


def test_property_duplicate_line_does_not_double_count():
    charges = load_charges(str(CHARGES))
    base = compute_ledger(charges)
    dup = charges[0]  # an exact copy of an existing line
    led = compute_ledger(list(charges) + [dup])
    assert led.dedupe["n_duplicate_lines_removed"] == 1
    assert led.grains == base.grains  # every grain unchanged after dedupe


def test_property_each_line_maps_to_one_encounter_and_one_period():
    from src.periods import pay_period_from_dos
    charges = load_charges(str(CHARGES))
    billable = [ln for ln in charges
                if not ln.is_void and not codes.is_non_session(ln.cpt)]
    # Each line has exactly one encounter key and one pay period; lines that share
    # an encounter share the period (same DOS).
    enc_to_period = {}
    for ln in billable:
        assert isinstance(ln.encounter_key, tuple) and len(ln.encounter_key) == 3
        period = pay_period_from_dos(ln.dos)
        prev = enc_to_period.setdefault(ln.encounter_key, period)
        assert prev == period  # one encounter -> one period, no straddling


def test_dedupe_is_order_independent():
    charges = load_charges(str(CHARGES))
    a, na = dedupe_charges(charges)
    b, nb = dedupe_charges(list(reversed(charges)))
    assert na == nb == 0
    assert [ln.full_key for ln in a] == [ln.full_key for ln in b]  # deterministic
