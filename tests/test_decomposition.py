"""Phase D: calendar normalization + volume/rate/mix decomposition (§12-§14)."""
import random
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.decomposition import (
    build_calendar_window,
    build_encounters,
    business_days_in_months,
    category_totals,
    compare_all_windows,
    comparison_windows,
    decompose,
    map_payer_category,
)
from src.periods import month_range, shift_month
from src.sessions import load_appointments, load_charges

FIXTURES = Path(__file__).parent / "fixtures"
CHARGES = FIXTURES / "ChargesHistoryDetailProviderPatientCode_SYNTHETIC.csv"
APPTS = FIXTURES / "AppointmentsPatientInfoByProviderThenDayThenFacility_SYNTHETIC.csv"


def _inputs():
    return load_charges(str(CHARGES)), load_appointments(str(APPTS))


# --- month arithmetic / window specs (§14) ---

def test_month_shift_and_range():
    assert shift_month("2026-07", -12) == "2025-07"
    assert shift_month("2026-01", -1) == "2025-12"
    assert shift_month("2025-12", 1) == "2026-01"
    assert month_range("2026-03", 3) == ["2026-01", "2026-02", "2026-03"]


def test_all_three_windows_produced_every_run():
    specs = comparison_windows("2026-07")
    assert [s.name for s in specs] == [
        "current_vs_same_period_prior_year",
        "rolling_12_vs_prior_rolling_12",
        "trailing_3_vs_same_3_prior_year",
    ]
    cur_vs_prior, rolling, trailing = specs
    assert cur_vs_prior.current_months == ["2026-07"]
    assert cur_vs_prior.prior_months == ["2025-07"]
    assert rolling.current_months[0] == "2025-08" and rolling.current_months[-1] == "2026-07"
    assert rolling.prior_months[0] == "2024-08" and rolling.prior_months[-1] == "2025-07"
    assert trailing.current_months == ["2026-05", "2026-06", "2026-07"]
    assert trailing.prior_months == ["2025-05", "2025-06", "2025-07"]
    # Windows must not overlap their own prior comparator.
    for s in specs:
        assert not (set(s.current_months) & set(s.prior_months))


# --- calendar normalization (§12) ---

def test_business_days_counted():
    assert business_days_in_months(["2026-07"]) == 23   # July 2026
    assert business_days_in_months(["2026-02"]) == 20   # Feb 2026
    assert business_days_in_months(["2026-01", "2026-02"]) == 22 + 20


def test_clinic_days_distinct_from_business_days():
    charges, appts = _inputs()
    encs, _ = build_encounters(charges)
    w = build_calendar_window(["2026-07"], encs, appts)
    # 3 kept appointments in July 2026 on 2 distinct dates (02, 15);
    # scheduled days also include the no-show (16) and cancellation (18).
    assert w.clinic_days == 2
    assert w.scheduled_days == 4
    assert w.business_days == 23
    assert w.clinic_days != w.business_days


def test_volume_reported_raw_and_per_clinic_day():
    charges, appts = _inputs()
    encs, _ = build_encounters(charges)
    w = build_calendar_window(["2026-07"], encs, appts)
    assert w.sessions == 3                      # raw
    assert w.sessions_per_clinic_day == Decimal("1.500")   # normalized
    assert w.expected == Decimal("479.50")
    assert w.expected_per_clinic_day == Decimal("239.75")


def test_extra_operating_day_does_not_look_like_growth():
    """The §12 point: more sessions purely from an extra clinic day is flat
    per-clinic-day, so it cannot be misread as a real signal."""
    from src.decomposition import CalendarWindow
    a = CalendarWindow(["2026-06"], sessions=20, expected=Decimal("2000"),
                       business_days=22, clinic_days=10, scheduled_days=10)
    b = CalendarWindow(["2026-07"], sessions=22, expected=Decimal("2200"),
                       business_days=23, clinic_days=11, scheduled_days=11)
    assert b.sessions > a.sessions                                  # raw: "growth"
    assert b.sessions_per_clinic_day == a.sessions_per_clinic_day   # normalized: flat


# --- encounters carry category + dollars, conserving money ---

def test_encounters_conserve_expected_dollars():
    charges, _ = _inputs()
    encs, exc = build_encounters(charges)
    from src import codes
    billable = [c for c in charges
                if not c.is_void and not codes.is_non_session(c.cpt)]
    assert sum((e.expected for e in encs), Decimal("0")) == \
        sum((c.expected for c in billable if c.expected), Decimal("0"))
    # Add-on lines fold into their primary encounter, not their own category.
    p001 = next(e for e in encs if e.dos == date(2026, 7, 2))
    assert p001.primary_cpt == "90837" and p001.n_lines == 2
    assert p001.expected == Decimal("202.00")   # 180 primary + 22 add-on

    # The 2026-07-22 line bills add-on 90838 with NO primary code alongside it.
    # That is a real data anomaly (Phase B flagged the same line as an add-on
    # never observed with a primary), so it must be COUNTED, not silently
    # absorbed -- while its dollars still conserve above.
    assert exc["encounters_without_primary"] == 1
    orphan = next(e for e in encs if e.dos == date(2026, 7, 22))
    assert orphan.primary_cpt == "90838"        # named by its highest-expected line


def test_payer_category_uses_engine_rules_and_flags_unmapped():
    pm = {"Aetna": "Commercial", "Private Pay": "Self-Pay"}
    assert map_payer_category("Aetna", pm) == "Commercial"
    assert map_payer_category("Private Pay $80", pm) == "Self-Pay"  # fee suffix
    assert map_payer_category("Mystery", pm) == "UNMAPPED"          # never guessed
    assert map_payer_category("", pm) == "NO_PAYER"


# --- decomposition (§13) ---

def test_decomposition_identity_on_fixtures():
    charges, _ = _inputs()
    encs, _ = build_encounters(charges)
    dec = decompose(
        category_totals(encs, ["2025-07"], "cpt"),
        category_totals(encs, ["2026-07"], "cpt"),
    )
    assert dec.prior_sessions == 1 and dec.current_sessions == 3
    assert dec.prior_expected == Decimal("180.00")
    assert dec.current_expected == Decimal("479.50")
    assert dec.total_change == Decimal("299.50")
    assert dec.components_sum() == dec.total_change   # the invariant


def test_pure_volume_change_is_all_volume_effect():
    # Same category, same yield, twice the sessions -> all volume, no rate/mix.
    prior = {"90837": (10, Decimal("1800.00"))}
    current = {"90837": (20, Decimal("3600.00"))}
    dec = decompose(prior, current)
    assert dec.volume_effect == Decimal("1800.00")
    assert dec.rate_effect == Decimal("0.00")
    assert dec.mix_effect == Decimal("0.00")
    assert dec.components_sum() == dec.total_change


def test_pure_rate_change_is_all_rate_effect():
    # Same sessions, same mix, higher price per session -> all rate.
    prior = {"90837": (10, Decimal("1800.00"))}
    current = {"90837": (10, Decimal("2000.00"))}
    dec = decompose(prior, current)
    assert dec.volume_effect == Decimal("0.00")
    assert dec.rate_effect == Decimal("200.00")
    assert dec.mix_effect == Decimal("0.00")


def test_pure_mix_shift_is_all_mix_effect():
    # Same total sessions and same per-category rates, but the mix shifts from
    # the cheap code to the expensive one -> the change is entirely mix.
    prior = {"90832": (10, Decimal("1000.00")), "90837": (10, Decimal("2000.00"))}
    current = {"90832": (5, Decimal("500.00")), "90837": (15, Decimal("3000.00"))}
    dec = decompose(prior, current)
    assert dec.volume_effect == Decimal("0.00")
    assert dec.rate_effect == Decimal("0.00")
    assert dec.mix_effect == Decimal("500.00")
    assert dec.components_sum() == dec.total_change


def test_no_prior_baseline_is_undefined_not_fabricated():
    dec = decompose({}, {"90837": (5, Decimal("900.00"))})
    assert dec.defined is False
    assert dec.volume_effect is None and dec.rate_effect is None
    assert dec.components_sum() is None
    assert any("undefined" in n for n in dec.notes)


def test_new_and_lost_categories_are_reported():
    prior = {"90832": (10, Decimal("1000.00"))}
    current = {"90837": (10, Decimal("2000.00"))}
    dec = decompose(prior, current)
    assert dec.new_categories == ["90837"]
    assert dec.lost_categories == ["90832"]
    assert dec.components_sum() == dec.total_change   # identity still exact
    assert any("new this period" in n for n in dec.notes)


# ------------------------------------------------------------------ #
# PROPERTY TEST (§13): the three components must sum to the total
# change -- on GENERATED inputs, not hand-picked cases.
# ------------------------------------------------------------------ #

def test_property_components_sum_to_total_change_generated():
    rng = random.Random(20260726)   # seeded: synthetic, test-only, deterministic
    cat_pool = ["90791", "90832", "90834", "90837", "90853", "99213", "99214"]

    for trial in range(500):
        def make(min_cats=1):
            cats = rng.sample(cat_pool, rng.randint(min_cats, len(cat_pool)))
            out = {}
            for c in cats:
                sessions = rng.randint(1, 60)
                # Money with cents, so rounding is genuinely exercised.
                per = Decimal(rng.randint(1, 30000)) / Decimal(100)
                out[c] = (sessions, (per * sessions).quantize(Decimal("0.01")))
            return out

        prior = make()          # always >= 1 category, so a baseline exists
        current = make()
        dec = decompose(prior, current)

        assert dec.defined, "prior always has sessions in this generator"
        # THE INVARIANT: exact Decimal equality, no tolerance.
        assert dec.volume_effect + dec.rate_effect + dec.mix_effect == \
            dec.total_change, (
                f"trial {trial}: components do not sum to the total change "
                f"({dec.volume_effect} + {dec.rate_effect} + {dec.mix_effect} "
                f"!= {dec.total_change})"
            )
        # And the total change is itself the honest difference.
        assert dec.total_change == dec.current_expected - dec.prior_expected


def test_property_identity_holds_when_current_is_empty():
    rng = random.Random(7)
    for _ in range(50):
        prior = {c: (rng.randint(1, 20), Decimal(rng.randint(100, 5000)))
                 for c in ("90834", "90837")}
        dec = decompose(prior, {})
        assert dec.components_sum() == dec.total_change
        assert dec.total_change == -dec.prior_expected


# --- all windows wired together, carrying INCOMPLETE labels (§11) ---

def test_compare_all_windows_and_incomplete_label_propagates():
    charges, appts = _inputs()
    windows, exc = compare_all_windows(
        charges, appts, incomplete=["2026-07"], period="2026-07",
    )
    assert len(windows) == 3
    assert all(w.decomposition.components_sum() == w.decomposition.total_change
               for w in windows if w.decomposition.defined)
    # Every window touching the immature 2026-07 must carry the label.
    for w in windows:
        assert w.label == "INCOMPLETE"
        assert "2026-07" in w.incomplete_periods


def test_windows_are_deterministic():
    charges, appts = _inputs()
    a, _ = compare_all_windows(charges, appts, period="2026-07")
    b, _ = compare_all_windows(charges, appts, period="2026-07")
    assert [(w.name, w.current.sessions, w.decomposition.total_change) for w in a] == \
           [(w.name, w.current.sessions, w.decomposition.total_change) for w in b]
