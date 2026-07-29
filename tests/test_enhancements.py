"""The enhancement batch: per-person expectations, psychiatry split, payer groups,
cancellation percentage, and the drop attribution insight."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app import config_store
from app.config_store import PracticeConfig
from app.models.data_source import DataSource, SourceProvider
from app.models.enums import Module, Role
from app.models.therapist import Discipline, EmploymentType, Therapist
from app.models.visit import Visit
from app.reporting import queries
from app.reporting.periods import today_in, week_start
from tests.conftest import make_user, sign_in

CONFIG = PracticeConfig(
    benefits_session_threshold=30,
    cpt_exclusions=("99998", "99999"),
    week_starts_monday=True,
    session_timeout_minutes=15,
    timezone="America/New_York",
    insurance_groups={"KS": "IBC", "PC": "IBC", "IA": "IBC"},
)


# ------------------------------------------------------------------- expectations


def test_each_employment_type_carries_its_own_default():
    assert CONFIG.expectation_for(EmploymentType.SALARIED_BENEFITS, None).threshold == 30
    assert CONFIG.expectation_for(EmploymentType.FULL_TIME_NO_BENEFITS, None).threshold == 25
    assert CONFIG.expectation_for(EmploymentType.PART_TIME, None).label == "5 to 20"
    assert CONFIG.expectation_for(EmploymentType.PERCENTAGE_LEGACY, None).kind == "none"


def test_a_personal_override_beats_the_type_default():
    assert CONFIG.expectation_for(EmploymentType.SALARIED_BENEFITS, 25).threshold == 25
    assert CONFIG.expectation_for(EmploymentType.FULL_TIME_NO_BENEFITS, 30).threshold == 30


def test_status_follows_the_personal_expectation():
    # Lexi at 25, O'Meally at 30: identical work, different judgements.
    assert CONFIG.status_for(EmploymentType.SALARIED_BENEFITS, 25, 26) == "ok"
    assert CONFIG.status_for(EmploymentType.SALARIED_BENEFITS, 30, 26) == "watch"
    assert CONFIG.status_for(EmploymentType.SALARIED_BENEFITS, 30, 20) == "below"


def test_part_time_is_a_band_not_a_floor():
    assert CONFIG.status_for(EmploymentType.PART_TIME, None, 3) == "below"
    assert CONFIG.status_for(EmploymentType.PART_TIME, None, 12) == "ok"
    assert CONFIG.status_for(EmploymentType.PART_TIME, None, 21) == "over"


def test_the_unmeasured_carry_no_status():
    assert CONFIG.status_for(EmploymentType.OTHER, None, 0) == ""


# -------------------------------------------------------------- payer group folding


def test_member_codes_fold_into_their_group():
    rows = [
        queries.Breakdown(key="KS", label="KS", sessions=10, collected=Decimal(100)),
        queries.Breakdown(key="PC", label="PC", sessions=5, collected=Decimal(50)),
        queries.Breakdown(key="AET", label="AET", sessions=3, collected=Decimal(30)),
    ]
    folded = queries._fold_groups(rows, {"KS": "IBC", "PC": "IBC"})
    ibc = next(r for r in folded if r.label == "IBC")
    assert ibc.sessions == 15
    assert ibc.collected == Decimal(150)
    assert any(r.label == "AET" for r in folded)


def test_group_parsing_refuses_a_code_claimed_twice():
    from app.routers.admin_config import _parse_groups

    mapping, error = _parse_groups("IBC = KS, PC\nOTHER = KS")
    assert error is not None and "KS" in error

    mapping, error = _parse_groups("IBC = KS, PC, IA")
    assert error is None
    assert mapping == {"KS": "IBC", "PC": "IBC", "IA": "IBC"}


# ------------------------------------------------------------------ data fixtures


@pytest.fixture
def clinic(client):
    """A therapist and a psychiatrist with mixed CPT codes in April 2026."""
    with client.app.state.db.session() as db:
        source = DataSource(
            label="E", provider=SourceProvider.DEMO, tab_name="t", column_mapping={}
        )
        therapist = Therapist(
            display_name="Lexi Example",
            employment_type=EmploymentType.SALARIED_BENEFITS,
            weekly_expected_sessions=25,
        )
        psychiatrist = Therapist(
            display_name="Yoe Example",
            employment_type=EmploymentType.OTHER,
            discipline=Discipline.PSYCHIATRIST,
        )
        db.add_all([source, therapist, psychiatrist])
        db.flush()

        def visit(who, day, cpt, insurance="KS"):
            return Visit(
                source_id=source.id,
                therapist_id=who.id,
                patient_name=f"Patient AA {day} {cpt}",
                patient_name_normalized=f"PATIENT AA {day} {cpt}",
                dos=day,
                cpt=cpt,
                cpt_base=cpt,
                insurance_short=insurance,
                total_paid=Decimal("150.00"),
                total_due=Decimal("150.00"),
                total_balance=Decimal("0.00"),
            )

        db.add_all(
            [
                visit(therapist, date(2026, 4, 6), "90837"),
                visit(therapist, date(2026, 4, 7), "90837", insurance="PC"),
                visit(therapist, date(2026, 4, 8), "99998"),
                visit(psychiatrist, date(2026, 4, 6), "99213"),
                visit(psychiatrist, date(2026, 4, 7), "99214", insurance="AET"),
            ]
        )
        return {"therapist": therapist.id, "psychiatrist": psychiatrist.id}


APRIL = queries.Filters(
    start=date(2026, 4, 1), end=date(2026, 4, 30), cpt_exclusions=("99998", "99999")
)


def test_psychiatry_splits_by_code(client, clinic):
    with client.app.state.db.session() as db:
        totals = queries.totals(db, APRIL)
    assert totals.sessions == 4
    assert totals.psychiatry_sessions == 2  # the 99213 and 99214, never the 99998
    assert totals.therapy_sessions == 2


def test_by_insurance_folds_the_practice_groups(client, clinic):
    with client.app.state.db.session() as db:
        rows = queries.by_insurance(db, APRIL, groups={"KS": "IBC", "PC": "IBC"})
    labels = [r.label for r in rows]
    assert "IBC" in labels and "KS" not in labels and "PC" not in labels
    ibc = next(r for r in rows if r.label == "IBC")
    # Two therapy sessions plus the KS-billed psychiatry session; the cancellation
    # is never a session.
    assert ibc.sessions == 3


def test_the_board_grades_and_splits_by_discipline(client, clinic):
    with client.app.state.db.session() as db:
        email = make_user(
            db,
            email="board@example.invalid",
            role=Role.MANAGER,
            modules=(Module.THERAPIST_UTILIZATION,),
        ).email
    sign_in(client, email)
    page = client.get(
        "/reports/therapist-utilization?preset=custom&start=2026-04-06&end=2026-04-12"
    ).text
    assert "Psychiatry" in page  # the psychiatrist renders in their own board
    assert "Yoe Example" in page
    # Lexi's personal expectation (25) shows in the Expected column.
    assert ">25</td>" in page.replace("\n", "").replace(" ", "")
    # Cancellation percentage: 1 cancellation over 3 scheduled is 33.3 percent.
    assert "33.3%" in page


def test_weekly_counts_show_the_split(client, clinic):
    with client.app.state.db.session() as db:
        email = make_user(
            db,
            email="weekly@example.invalid",
            role=Role.MANAGER,
            modules=(Module.THERAPIST_UTILIZATION,),
        ).email
    sign_in(client, email)
    page = client.get("/reports/therapist-utilization/weekly?weeks=52").text
    assert "Psychiatry" in page and "Therapy" in page


# --------------------------------------------------------------- drop attribution


def test_a_big_drop_names_its_contributors(client):
    from app.reporting.insights import build_insights

    with client.app.state.db.session() as db:
        source = DataSource(
            label="D", provider=SourceProvider.DEMO, tab_name="t", column_mapping={}
        )
        busy = Therapist(
            display_name="Suddenly Out", employment_type=EmploymentType.SALARIED_BENEFITS
        )
        steady = Therapist(
            display_name="Steady Colleague", employment_type=EmploymentType.SALARIED_BENEFITS
        )
        db.add_all([source, busy, steady])
        db.flush()

        this_week = week_start(today_in("America/New_York"))
        prior_monday = this_week - timedelta(weeks=2)
        last_monday = this_week - timedelta(weeks=1)

        def seed(who, monday, count):
            for i in range(count):
                day = monday + timedelta(days=i % 5)
                db.add(
                    Visit(
                        source_id=source.id,
                        therapist_id=who.id,
                        patient_name=f"Patient A{monday}x{who.id}x{i}",
                        patient_name_normalized=f"PATIENT A{monday}X{who.id}X{i}",
                        dos=day,
                        cpt="90837",
                        cpt_base="90837",
                        total_paid=Decimal("150.00"),
                        total_due=Decimal("150.00"),
                        total_balance=Decimal("0.00"),
                    )
                )

        # Twelve weeks of history so trend insights run, then the drop: the busy
        # therapist vanishes entirely in the last completed week.
        for n in range(3, 13):
            seed(busy, this_week - timedelta(weeks=n), 13)
            seed(steady, this_week - timedelta(weeks=n), 12)
        seed(busy, prior_monday, 25)
        seed(steady, prior_monday, 10)
        seed(steady, last_monday, 10)

    with client.app.state.db.session() as db:
        config = config_store.load(db, client.app.state.settings)
        report = build_insights(db, config=config, cpt_exclusions=config.cpt_exclusions)

    drop = next(i for i in report.insights if i.key == "sessions_drop")
    assert drop.tone == "watch"
    assert "Suddenly Out" in drop.detail
    assert "likely out" in drop.detail
