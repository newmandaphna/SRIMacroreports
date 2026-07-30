"""Patient flow: aggregate counts, module gating, and the no-identity rule."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest

from app.models.data_source import DataSource, SourceProvider
from app.models.enums import Module, Role
from app.models.therapist import EmploymentType, Therapist
from app.models.visit import Visit
from app.reporting import patients
from app.reporting.periods import Granularity
from app.reporting.queries import Filters
from tests.conftest import make_user, sign_in

APRIL = Filters(start=date(2026, 4, 1), end=date(2026, 4, 30), cpt_exclusions=("99998", "99999"))
SPRING = Filters(start=date(2026, 3, 1), end=date(2026, 4, 30), cpt_exclusions=("99998", "99999"))


@pytest.fixture
def caseload(client):
    """Two synthetic patients: AA starts in March and continues; AB starts in April.
    AA also has an April cancellation, which must never count as being seen."""
    with client.app.state.db.session() as db:
        source = DataSource(
            label="F", provider=SourceProvider.DEMO, tab_name="t", column_mapping={}
        )
        therapist = Therapist(
            display_name="Flow Therapist", employment_type=EmploymentType.SALARIED_BENEFITS
        )
        db.add_all([source, therapist])
        db.flush()

        def visit(patient, day, cpt="90837"):
            return Visit(
                source_id=source.id,
                therapist_id=therapist.id,
                patient_name=patient,
                patient_name_normalized=patient.upper(),
                dos=day,
                cpt=cpt,
                cpt_base=cpt,
                total_paid=Decimal("150.00"),
                total_due=Decimal("150.00"),
                total_balance=Decimal("0.00"),
            )

        db.add_all(
            [
                visit("Patient AA", date(2026, 3, 10)),
                visit("Patient AA", date(2026, 4, 7)),
                visit("Patient AA", date(2026, 4, 9), cpt="99998"),
                visit("Patient AB", date(2026, 4, 14)),
            ]
        )


def test_counts_distinct_and_first_timers(client, caseload):
    with client.app.state.db.session() as db:
        series = patients.flow_series(db, SPRING, Granularity.MONTH)

    march = next(p for p in series if p.start == date(2026, 3, 1))
    april = next(p for p in series if p.start == date(2026, 4, 1))
    assert (march.active, march.new) == (1, 1)  # AA appears, and is new
    assert (april.active, april.new) == (2, 1)  # AA returns, AB is the only new one


def test_a_cancellation_is_not_a_person_seen(client, caseload):
    with client.app.state.db.session() as db:
        cancelled_only = patients.summary(
            db,
            Filters(
                start=date(2026, 4, 8), end=date(2026, 4, 9), cpt_exclusions=("99998", "99999")
            ),
            today=date(2026, 4, 30),
        )
    assert cancelled_only.unique_patients == 0


def test_summary_census_and_averages(client, caseload):
    with client.app.state.db.session() as db:
        summary = patients.summary(db, SPRING, today=date(2026, 4, 30))
    assert summary.unique_patients == 2
    assert summary.new_patients == 2
    assert summary.average_sessions == 1.5  # 3 sessions over 2 people
    assert summary.current_census == 2  # both seen inside the 90 day window


def test_identity_stays_inside_aggregates(client, caseload):
    """The identity column may appear only inside COUNT(DISTINCT) or the first-visit
    subquery, never as selected output. Tripwire on the emitted SQL."""
    from sqlalchemy import event

    statements: list[str] = []
    engine = client.app.state.db.engine

    def record(conn, cursor, statement, params, context, executemany):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", record)
    try:
        with client.app.state.db.session() as db:
            patients.flow_series(db, SPRING, Granularity.MONTH)
            patients.summary(db, SPRING, today=date(2026, 4, 30))
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert statements

    # The loop below used to be the whole guard, and it could not fail. Nothing asserted
    # that any statement mentioned the column at all, so a change that stopped this
    # module touching patient identity, or a rename, would have left the loop skipping
    # every statement and the test passing while proving nothing. And the forbidden
    # prefix it looked for was "select visits.patient_name" when the table is `sessions`,
    # so even a statement that did select the column outright could not have tripped it.
    touching = [s for s in statements if "patient_name_normalized" in s]
    assert touching, (
        "no statement mentioned the patient identity column, so this guard checked "
        "nothing. Either the module stopped using it, in which case say so here, or the "
        "column was renamed and this test needs the new name."
    )

    for statement in touching:
        assert "count(distinct" in statement or "group by" in statement, statement

        # The real rule: the column may appear in a WHERE clause, in a GROUP BY, or
        # inside COUNT(DISTINCT ...), but never as a selected output column. Checked
        # against every select list in the statement, subqueries included, rather than
        # against a guessed prefix of the whole string.
        select_lists = re.findall(r"\bselect\b(.*?)\bfrom\b", statement, re.S)
        assert select_lists, f"could not find a select list to check in: {statement}"
        for select_list in select_lists:
            bare = re.sub(r"count\s*\(\s*distinct.*?\)", "", select_list, flags=re.S)
            assert "patient_name" not in bare, f"patient identity is a selected column: {statement}"


@pytest.fixture
def two_caseloads(client):
    """Two therapists. One has a returning patient, the other only new ones.

    The point of the shape: filtered to the returning patient's therapist, April holds
    one active patient and no new one, while the practice as a whole gains two.
    """
    with client.app.state.db.session() as db:
        source = DataSource(
            label="F2", provider=SourceProvider.DEMO, tab_name="t", column_mapping={}
        )
        keeper = Therapist(display_name="Keeper", employment_type=EmploymentType.SALARIED_BENEFITS)
        grower = Therapist(display_name="Grower", employment_type=EmploymentType.SALARIED_BENEFITS)
        db.add_all([source, keeper, grower])
        db.flush()

        def visit(therapist_id, patient, day):
            return Visit(
                source_id=source.id,
                therapist_id=therapist_id,
                patient_name=patient,
                patient_name_normalized=patient.upper(),
                dos=day,
                cpt="90837",
                cpt_base="90837",
                total_paid=Decimal("150.00"),
                total_due=Decimal("150.00"),
                total_balance=Decimal("0.00"),
            )

        db.add_all(
            [
                visit(keeper.id, "Patient AA", date(2026, 3, 10)),
                visit(keeper.id, "Patient AA", date(2026, 4, 7)),
                visit(grower.id, "Patient AB", date(2026, 4, 14)),
                visit(grower.id, "Patient AC", date(2026, 4, 15)),
            ]
        )
        return {"keeper": keeper.id, "grower": grower.id}


def test_new_patients_belongs_to_the_filtered_population(client, two_caseloads):
    """New counted the whole practice while active counted the filter.

    First ever is deliberately computed outside the window, so that a returning patient
    is never called new because the picker starts late. That subquery ignored the
    therapist filter too, though, so a page filtered to one therapist showed their
    patient count beside the practice's new patients. It could report more new patients
    than active ones, which for one population is impossible.
    """
    april = Filters(
        start=APRIL.start,
        end=APRIL.end,
        cpt_exclusions=APRIL.cpt_exclusions,
        therapist_ids=(two_caseloads["keeper"],),
    )
    with client.app.state.db.session() as db:
        keeper = patients.summary(db, april, today=date(2026, 4, 30))
        practice = patients.summary(db, APRIL, today=date(2026, 4, 30))

    assert keeper.unique_patients == 1
    assert keeper.new_patients == 0, (
        "the only patient this therapist saw in April had been seen in March"
    )
    assert keeper.new_patients <= keeper.unique_patients

    # The unfiltered page is unchanged: two people did start in April practice wide.
    assert (practice.unique_patients, practice.new_patients) == (3, 2)


def test_a_patient_new_to_one_therapist_is_new_on_that_therapists_page(client, two_caseloads):
    """The other half of the same rule. Filtered to the growing caseload, both of its
    April patients are new to it, and the returning patient elsewhere is invisible."""
    april = Filters(
        start=APRIL.start,
        end=APRIL.end,
        cpt_exclusions=APRIL.cpt_exclusions,
        therapist_ids=(two_caseloads["grower"],),
    )
    with client.app.state.db.session() as db:
        grower = patients.summary(db, april, today=date(2026, 4, 30))

    assert (grower.unique_patients, grower.new_patients) == (2, 2)


@pytest.fixture
def flow_user(client, caseload):
    with client.app.state.db.session() as db:
        email = make_user(
            db, email="flow@example.invalid", role=Role.VIEWER, modules=(Module.PATIENT_FUNNEL,)
        ).email
    sign_in(client, email)
    return client


def test_page_renders_counts_and_never_a_name(flow_user):
    page = flow_user.get("/reports/patient-flow?preset=custom&start=2026-03-01&end=2026-04-30").text
    assert "Patient flow" in page
    assert "Deliberately aggregate" in page
    assert "Patient A" not in page  # no synthetic name leaks either


def test_page_refuses_without_the_grant(client, caseload):
    with client.app.state.db.session() as db:
        email = make_user(db, email="noflow@example.invalid", role=Role.VIEWER).email
    sign_in(client, email)
    assert client.get("/reports/patient-flow").status_code == 403
