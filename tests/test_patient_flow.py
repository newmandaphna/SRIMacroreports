"""Patient flow: aggregate counts, module gating, and the no-identity rule."""

from __future__ import annotations

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
    for statement in statements:
        if "patient_name_normalized" in statement:
            assert "count(distinct" in statement or "group by" in statement, statement
            assert not statement.strip().startswith("select visits.patient_name"), statement


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
