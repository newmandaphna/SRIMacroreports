"""The reporting query layer, including the property SECURITY.md 6.3 depends on."""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import sqlite

from app.models.therapist import EmploymentType, Therapist, TherapistAlias
from app.models.visit import Visit
from app.reporting import queries
from app.reporting.periods import Granularity

EXCLUSIONS = ("99998", "99999", "QBCHK", "FORM", "PRO BONO")


@pytest.fixture
def seeded(client):
    """A small, hand checked dataset so every expected figure is verifiable by eye.

    Two therapists, one salaried and one percentage based, across three weeks.
    """
    from app.models.data_source import DataSource, SourceProvider

    with client.app.state.db.session() as db:
        source = DataSource(
            label="Test source", provider=SourceProvider.DEMO, tab_name="t", column_mapping={}
        )
        db.add(source)

        salaried = Therapist(display_name="Alpha", employment_type=EmploymentType.SALARIED_BENEFITS)
        percentage = Therapist(
            display_name="Beta", employment_type=EmploymentType.PERCENTAGE_LEGACY
        )
        db.add_all([salaried, percentage])
        db.flush()
        db.add_all(
            [
                TherapistAlias(therapist_id=salaried.id, alias="ALPHA"),
                TherapistAlias(therapist_id=percentage.id, alias="BETA"),
            ]
        )

        def visit(therapist, name, dos, cpt, paid, due, balance, ins="KS", loc="TH"):
            return Visit(
                source_id=source.id,
                therapist_id=therapist.id,
                patient_name=name,
                patient_name_normalized=name.upper(),
                patient_code=None,
                dos=dos,
                cpt=cpt,
                cpt_base=cpt,
                insurance_short=ins,
                location_short=loc,
                total_paid=Decimal(paid),
                total_due=Decimal(due),
                total_balance=Decimal(balance),
                pt_amount_due=Decimal(balance) / 2,
                ins_balance=Decimal(balance) / 2,
            )

        db.add_all(
            [
                # Week of 2026-04-06: 3 sessions for Alpha
                visit(
                    salaried, "Patient AA", date(2026, 4, 6), "90837", "150.00", "150.00", "0.00"
                ),
                visit(
                    salaried, "Patient AB", date(2026, 4, 7), "90837", "150.00", "200.00", "50.00"
                ),
                visit(
                    salaried, "Patient AC", date(2026, 4, 8), "90834", "100.00", "100.00", "0.00"
                ),
                # A cancellation with a fee: money, but not a session.
                visit(salaried, "Patient AA", date(2026, 4, 9), "99999", "75.00", "75.00", "0.00"),
                # A cancellation with no fee: neither.
                visit(salaried, "Patient AB", date(2026, 4, 10), "99998", "0.00", "0.00", "0.00"),
                # Week of 2026-04-13: 1 session for Alpha, 2 for Beta
                visit(
                    salaried, "Patient AC", date(2026, 4, 13), "90837", "150.00", "150.00", "0.00"
                ),
                visit(
                    percentage,
                    "Patient AD",
                    date(2026, 4, 14),
                    "90837",
                    "150.00",
                    "150.00",
                    "0.00",
                    loc="1",
                ),
                visit(
                    percentage,
                    "Patient AE",
                    date(2026, 4, 15),
                    "90791",
                    "200.00",
                    "200.00",
                    "0.00",
                    ins="MC",
                    loc="1",
                ),
            ]
        )
        db.flush()
        return {"salaried": salaried.id, "percentage": percentage.id}


def full_range() -> queries.Filters:
    return queries.Filters(start=date(2026, 4, 1), end=date(2026, 4, 30), cpt_exclusions=EXCLUSIONS)


# ------------------------------------------------------------- the security property


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=sqlite.dialect())).lower()


def test_no_reporting_query_selects_patient_identity(client, seeded):
    """SECURITY.md 6.3, asserted against the SQL rather than trusted.

    Every public builder in app.reporting.queries is called and its emitted SQL is
    checked. This holds for whatever is added later, not only for what exists today,
    because the test enumerates the module rather than a hardcoded list.
    """
    import app.reporting.queries as module

    builders = [
        name
        for name, obj in vars(module).items()
        if inspect.isfunction(obj)
        and not name.startswith("_")
        and obj.__module__ == module.__name__
    ]
    # Guard against the enumeration silently finding nothing.
    assert len(builders) >= 6, f"expected the query builders, found {builders}"

    # The enumeration must actually DRIVE the calls, not merely be counted. It used
    # to be counted and then ignored in favour of a hardcoded list below, which meant
    # a builder added later was never exercised and the guard silently stopped
    # covering it. Anything new in the module now fails here until it is called.
    called = {
        "totals",
        "by_period",
        "by_therapist",
        "by_insurance",
        "by_location",
        "by_cpt",
        "by_weekday",
        "aging_by_insurance",
        "coverage",
        "available_locations",
        "active_therapists",
        "therapist_history",
        "notes_for_period",
        "latest_notes",
    }
    uncovered = set(builders) - called
    assert not uncovered, (
        "these query builders are not exercised by this guard, so nothing proves they "
        f"avoid patient columns: {sorted(uncovered)}"
    )

    forbidden = ("patient_name", "patient_code", "patient_name_normalized")
    statements: list[str] = []

    with client.app.state.db.session() as db:
        # Capture every statement the builders actually emit.
        from sqlalchemy import event

        engine = client.app.state.db.engine

        def record(conn, cursor, statement, params, context, executemany):
            statements.append(statement.lower())

        event.listen(engine, "before_cursor_execute", record)
        try:
            filters = full_range()
            queries.totals(db, filters)
            queries.by_period(db, filters, Granularity.WEEK)
            queries.by_therapist(db, filters, weeks_in_range=4)
            queries.by_insurance(db, filters)
            queries.by_location(db, filters)
            queries.by_cpt(db, filters)
            queries.by_weekday(db, filters)
            queries.aging_by_insurance(db, today=date(2026, 4, 30))
            queries.coverage(db)
            queries.available_locations(db)
            queries.active_therapists(db)
            queries.therapist_history(db, 1, filters, Granularity.WEEK)
            queries.notes_for_period(db, date(2026, 4, 6), Granularity.WEEK)
            queries.latest_notes(db, [1])
        finally:
            event.remove(engine, "before_cursor_execute", record)

    assert statements, "no SQL was captured"
    for statement in statements:
        for column in forbidden:
            assert column not in statement, f"a reporting query referenced {column}:\n{statement}"


def test_the_guard_would_actually_catch_a_leak(client, seeded):
    """Prove the assertion above is not vacuous by showing what a leak looks like."""
    leaky = select(Visit.patient_name).where(Visit.dos >= date(2026, 4, 1))
    assert "patient_name" in _compiled(leaky)


# ------------------------------------------------------------------------- arithmetic


def test_totals(client, seeded):
    with client.app.state.db.session() as db:
        result = queries.totals(db, full_range())

    assert result.visits == 8
    # Eight rows, minus one 99999 and one 99998.
    assert result.sessions == 6
    assert result.collected == Decimal("975.00")
    assert result.billed == Decimal("1025.00")
    assert result.outstanding == Decimal("50.00")
    assert result.cancellations == 2
    assert result.cancellations_with_fee == 1


def test_no_show_fee_revenue_is_inside_collected_and_reported_separately(client, seeded):
    """ASSUMPTIONS.md A-031: excluding a code from counting does not unmake its money."""
    with client.app.state.db.session() as db:
        result = queries.totals(db, full_range())

    assert result.no_show_fee_revenue == Decimal("75.00")
    assert result.collected == Decimal("975.00")
    # The fee is part of collected, not additional to it.
    assert result.no_show_fee_revenue < result.collected


def test_collection_rate_and_cancellation_rate(client, seeded):
    with client.app.state.db.session() as db:
        result = queries.totals(db, full_range())

    assert result.collection_rate == Decimal("95.1")  # 975 / 1025
    assert result.cancellation_rate == Decimal("25.0")  # 2 of 8


def test_rates_are_none_rather_than_zero_when_undefined(client):
    empty = queries.Totals()
    assert empty.collection_rate is None
    assert empty.cancellation_rate is None
    assert empty.revenue_per_session is None


def test_money_is_exact(client, seeded):
    with client.app.state.db.session() as db:
        result = queries.totals(db, full_range())
    assert isinstance(result.collected, Decimal)
    assert result.collected == Decimal("975.00")


# ----------------------------------------------------------------------- time series


def test_by_period_returns_a_continuous_series(client, seeded):
    """A week with no sessions must be a zero, not a missing point.

    A gap would let the trend line close over it and read as though nothing happened.
    """
    with client.app.state.db.session() as db:
        points = queries.by_period(db, full_range(), Granularity.WEEK)

    starts = [p.start for p in points]
    assert starts == [
        date(2026, 3, 30),
        date(2026, 4, 6),
        date(2026, 4, 13),
        date(2026, 4, 20),
        date(2026, 4, 27),
    ]
    by_start = {p.start: p for p in points}
    assert by_start[date(2026, 3, 30)].sessions == 0
    assert by_start[date(2026, 4, 6)].sessions == 3
    assert by_start[date(2026, 4, 13)].sessions == 3
    assert by_start[date(2026, 4, 20)].sessions == 0


def test_weeks_are_labelled_by_their_monday(client, seeded):
    with client.app.state.db.session() as db:
        points = queries.by_period(db, full_range(), Granularity.WEEK)
    assert points[1].label == "6 Apr"


def test_monthly_and_quarterly_rollups(client, seeded):
    with client.app.state.db.session() as db:
        monthly = queries.by_period(db, full_range(), Granularity.MONTH)
        quarterly = queries.by_period(db, full_range(), Granularity.QUARTER)

    assert [p.label for p in monthly] == ["Apr 2026"]
    assert monthly[0].sessions == 6
    assert [p.label for p in quarterly] == ["Q2 2026"]
    assert quarterly[0].sessions == 6


# ------------------------------------------------------------------ therapist grain


def test_by_therapist(client, seeded):
    with client.app.state.db.session() as db:
        rows = queries.by_therapist(db, full_range(), weeks_in_range=4)

    by_name = {r.display_name: r for r in rows}
    assert by_name["Alpha"].sessions == 4
    assert by_name["Alpha"].cancellations == 2
    assert by_name["Beta"].sessions == 2
    assert by_name["Alpha"].sessions_per_week == Decimal("1.0")


def test_percentage_therapists_are_not_measured_against_the_threshold(client, seeded):
    """They have no session minimum, so flagging them below it is a false alarm."""
    with client.app.state.db.session() as db:
        rows = queries.by_therapist(db, full_range(), weeks_in_range=4)

    by_name = {r.display_name: r for r in rows}
    assert by_name["Alpha"].measured_against_threshold is True
    assert by_name["Beta"].measured_against_threshold is False


# ---------------------------------------------------------------------- breakdowns


def test_breakdowns(client, seeded):
    with client.app.state.db.session() as db:
        filters = full_range()
        insurance = {r.label: r for r in queries.by_insurance(db, filters)}
        locations = {r.label: r for r in queries.by_location(db, filters)}
        cpts = {r.label: r for r in queries.by_cpt(db, filters)}

    # Five KS sessions: four of Alpha's, plus Beta's 90837, which also defaults to KS.
    # Alpha's two cancellations are KS rows too but are not sessions.
    assert insurance["KS"].sessions == 5
    assert insurance["MC"].sessions == 1
    assert locations["TH"].sessions == 4
    assert locations["1"].sessions == 2
    # Cancellation codes appear in the CPT breakdown but count zero sessions.
    assert cpts["99999"].sessions == 0
    assert cpts["99999"].collected == Decimal("75.00")


# ------------------------------------------------------------------------- filtering


def test_therapist_filter(client, seeded):
    with client.app.state.db.session() as db:
        filters = queries.Filters(
            start=date(2026, 4, 1),
            end=date(2026, 4, 30),
            cpt_exclusions=EXCLUSIONS,
            therapist_ids=(seeded["percentage"],),
        )
        result = queries.totals(db, filters)
    assert result.sessions == 2
    assert result.visits == 2


def test_location_filter(client, seeded):
    with client.app.state.db.session() as db:
        filters = queries.Filters(
            start=date(2026, 4, 1),
            end=date(2026, 4, 30),
            cpt_exclusions=EXCLUSIONS,
            locations=("1",),
        )
        result = queries.totals(db, filters)
    assert result.sessions == 2


def test_date_range_filter(client, seeded):
    with client.app.state.db.session() as db:
        filters = queries.Filters(
            start=date(2026, 4, 13), end=date(2026, 4, 30), cpt_exclusions=EXCLUSIONS
        )
        result = queries.totals(db, filters)
    assert result.sessions == 3


def test_empty_exclusion_list_counts_everything(client, seeded):
    with client.app.state.db.session() as db:
        filters = queries.Filters(start=date(2026, 4, 1), end=date(2026, 4, 30), cpt_exclusions=())
        result = queries.totals(db, filters)
    assert result.sessions == 8


# -------------------------------------------------------------------------- coverage


def test_coverage_reports_what_exists(client, seeded):
    with client.app.state.db.session() as db:
        cov = queries.coverage(db)
    assert cov.has_data
    assert cov.min_date == date(2026, 4, 6)
    assert cov.max_date == date(2026, 4, 15)
    assert cov.visits == 8


def test_coverage_on_an_empty_database(client):
    with client.app.state.db.session() as db:
        cov = queries.coverage(db)
    assert not cov.has_data
    assert cov.min_date is None
