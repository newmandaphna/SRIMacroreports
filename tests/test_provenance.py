"""Provenance: the explanation of a figure must agree with the figure.

The point of these tests is drift. An explanation that has fallen out of step with
the calculation is worse than no explanation, because it teaches a reader to trust
a number they should have questioned. So the sentence and the sum are compared
against real rows, across several date ranges, including the awkward ones.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app import config_store
from app.models.data_source import DataSource, SourceProvider
from app.models.enums import Module, Role
from app.models.therapist import EmploymentType, Therapist
from app.models.visit import Visit
from app.reporting import queries
from app.reporting.periods import Granularity
from app.reporting.provenance import EXPLAINABLE, TITLES, build_derivation
from tests.conftest import make_user, sign_in

EXCLUSIONS = ("99998", "99999", "QBCHK")


@pytest.fixture
def ledger(client):
    """A month of mixed rows: therapy, psychiatry, a cancellation with a fee, a
    bookkeeping code, and a credit balance."""
    with client.app.state.db.session() as db:
        source = DataSource(
            label="P", provider=SourceProvider.DEMO, tab_name="t", column_mapping={}
        )
        therapist = Therapist(
            display_name="Provenance Example",
            employment_type=EmploymentType.SALARIED_BENEFITS,
            weekly_expected_sessions=25,
        )
        db.add_all([source, therapist])
        db.flush()

        def visit(day, cpt, paid, due, balance):
            return Visit(
                source_id=source.id,
                therapist_id=therapist.id,
                patient_name=f"Patient AA {day} {cpt}",
                patient_name_normalized=f"PATIENT AA {day} {cpt}",
                dos=day,
                cpt=cpt,
                cpt_base=cpt,
                insurance_short="KS",
                total_paid=Decimal(paid),
                total_due=Decimal(due),
                total_balance=Decimal(balance),
                pt_amount_due=Decimal("0.00"),
                ins_balance=Decimal(balance),
            )

        db.add_all(
            [
                visit(date(2026, 4, 6), "90837", "150.00", "200.00", "50.00"),
                visit(date(2026, 4, 7), "90834", "125.00", "125.00", "0.00"),
                visit(date(2026, 4, 8), "99213", "90.00", "120.00", "30.00"),
                visit(date(2026, 4, 9), "99999", "75.00", "75.00", "0.00"),
                visit(date(2026, 4, 10), "QBCHK", "0.00", "0.00", "0.00"),
                visit(date(2026, 5, 4), "90837", "150.00", "150.00", "-20.00"),
            ]
        )
        return {"therapist": therapist.id}


def derive(client, key, start, end, *, exclusions=EXCLUSIONS, granularity=Granularity.MONTH):
    with client.app.state.db.session() as db:
        config = config_store.load(db, client.app.state.settings)
        filters = queries.Filters(start=start, end=end, cpt_exclusions=exclusions)
        return build_derivation(
            db,
            key,
            filters=filters,
            config=config,
            granularity=granularity,
            may_see_therapists=True,
        )


# --------------------------------------------------------------- the drift guard

# The awkward windows, not just the comfortable one: an empty range, a single day,
# a range holding a credit, and one where nothing is excluded at all.
WINDOWS = [
    ("full", date(2026, 4, 1), date(2026, 5, 31), EXCLUSIONS),
    ("april", date(2026, 4, 1), date(2026, 4, 30), EXCLUSIONS),
    ("one day", date(2026, 4, 6), date(2026, 4, 6), EXCLUSIONS),
    ("empty", date(2026, 1, 1), date(2026, 1, 31), EXCLUSIONS),
    ("credit month", date(2026, 5, 1), date(2026, 5, 31), EXCLUSIONS),
    ("no exclusions", date(2026, 4, 1), date(2026, 5, 31), ()),
]


@pytest.mark.parametrize("key", EXPLAINABLE)
def test_the_stated_arithmetic_reproduces_the_figure(client, ledger, key):
    """Every explanation, every window: the sum must land on the printed number."""
    for name, start, end, exclusions in WINDOWS:
        d = derive(client, key, start, end, exclusions=exclusions)
        assert d is not None, f"{key} produced no derivation"
        assert d.agrees, (
            f"{key} in the {name} window: the stated arithmetic gives {d.recomputed} "
            f"but the figure is {d.value}, a gap of {d.discrepancy}"
        )


@pytest.mark.parametrize("key", EXPLAINABLE)
def test_every_explanation_says_something(client, ledger, key):
    d = derive(client, key, date(2026, 4, 1), date(2026, 5, 31))
    assert d.title == TITLES[key]
    assert len(d.sentence) > 20, "the definition must be a sentence, not a label"
    assert d.window
    assert d.caveats, "a figure with no caveat is a figure nobody has thought about"


@pytest.mark.parametrize("key", EXPLAINABLE)
def test_the_row_census_reconciles(client, ledger, key):
    d = derive(client, key, date(2026, 4, 1), date(2026, 5, 31))
    assert d.census is not None
    assert d.census.reconciles, "counted plus excluded must equal imported"


def test_the_guard_catches_a_planted_disagreement(client, ledger):
    """Prove the drift assertion is not vacuous."""
    d = derive(client, "collected", date(2026, 4, 1), date(2026, 5, 31))
    assert d.agrees
    d.value = Decimal(str(d.value)) + Decimal("100.00")
    assert not d.agrees
    assert d.discrepancy == Decimal("100.00")


# ------------------------------------------------------------ specific arithmetic


def test_sessions_counts_what_it_claims(client, ledger):
    d = derive(client, "sessions", date(2026, 4, 1), date(2026, 4, 30))
    # Six rows in April minus the 99999 cancellation and the QBCHK bookkeeping row.
    assert d.value == 3
    assert d.census.imported == 5
    assert d.census.excluded == 2


def test_turning_off_the_exclusions_changes_the_sentence(client, ledger):
    on = derive(client, "sessions", date(2026, 4, 1), date(2026, 4, 30))
    off = derive(client, "sessions", date(2026, 4, 1), date(2026, 4, 30), exclusions=())
    assert "99998" in " ".join(on.caveats)
    assert "No CPT codes are currently excluded" in " ".join(off.caveats)
    assert off.value == 5, "with nothing excluded every imported row is a session"


def test_a_rate_says_its_components_do_not_add_up(client, ledger):
    d = derive(client, "collection_rate", date(2026, 4, 1), date(2026, 5, 31))
    assert d.components_sum_to_value is False
    assert d.components == []
    assert "five weeks" in " ".join(d.caveats), "the maturity caveat must travel with the rate"


def test_an_unavailable_figure_says_why(client, ledger):
    d = derive(client, "revenue_per_session", date(2026, 1, 1), date(2026, 1, 31))
    assert d.value is None
    assert d.unavailable_because


# ------------------------------------------------------------------------- route


@pytest.fixture
def reader(client, ledger):
    with client.app.state.db.session() as db:
        email = make_user(
            db, email="prov@example.invalid", role=Role.VIEWER, modules=(Module.FINANCIAL,)
        ).email
    sign_in(client, email)
    return client


RANGE = "preset=custom&start=2026-04-01&end=2026-05-31&granularity=month"


def test_the_explain_page_shows_the_arithmetic(reader):
    page = reader.get(f"/reports/explain/collected?{RANGE}")
    assert page.status_code == 200
    assert "How revenue collected is calculated" in page.text
    assert "Which rows were counted" in page.text
    assert "must equal the figure above" in page.text


def test_the_explain_page_never_names_a_patient(reader):
    for key in EXPLAINABLE:
        page = reader.get(f"/reports/explain/{key}?{RANGE}")
        assert page.status_code == 200
        assert "Patient A" not in page.text, f"{key} leaked a patient name"


def test_an_unknown_figure_is_a_404_not_a_crash(reader):
    assert reader.get(f"/reports/explain/nonsense?{RANGE}").status_code == 404


def test_the_explain_page_needs_the_financial_grant(client, ledger):
    with client.app.state.db.session() as db:
        email = make_user(db, email="noprov@example.invalid", role=Role.VIEWER).email
    sign_in(client, email)
    assert client.get("/reports/explain/collected").status_code == 403


def test_provider_names_need_the_utilization_grant(client, ledger):
    """The count is the same either way; only the naming is gated."""
    with client.app.state.db.session() as db:
        email = make_user(
            db, email="finonly@example.invalid", role=Role.VIEWER, modules=(Module.FINANCIAL,)
        ).email
    sign_in(client, email)
    page = client.get(f"/reports/explain/below_threshold?{RANGE}").text
    assert "Provenance Example" not in page
    assert "does not hold" in page


def test_the_tiles_offer_their_explanations(reader):
    page = reader.get(f"/reports?{RANGE}").text
    for key in ("collected", "sessions", "outstanding", "below_threshold"):
        assert f"/reports/explain/{key}" in page, f"the {key} tile offers no explanation"


def test_the_financial_tiles_offer_their_explanations(reader):
    page = reader.get(f"/reports/financial?{RANGE}").text
    for key in ("collected", "sessions", "billed", "outstanding", "revenue_per_session"):
        assert f"/reports/explain/{key}" in page, f"the {key} tile offers no explanation"
