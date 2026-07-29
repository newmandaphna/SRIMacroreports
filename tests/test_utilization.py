"""The therapist utilization module: status board, drill in, and notes."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.enums import AuditAction, Module, Role
from app.models.therapist import EmploymentType, Therapist
from app.models.utilization import UtilizationNote
from app.models.visit import Visit
from tests.conftest import make_user, sign_in

ALL = "preset=custom&start=2026-04-01&end=2026-04-28&granularity=week"
# One clean week so the weekly average is unambiguous: Alpha has exactly 2 sessions
# in it (plus one cancellation, which is not a session), over exactly 1 week.
WEEK1 = "preset=custom&start=2026-04-06&end=2026-04-12&granularity=week"


def token_from(text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', text)
    assert match, "no CSRF token on page"
    return match.group(1)


@pytest.fixture
def practice(client):
    """A salaried therapist well below any threshold, and a percentage one."""
    from app.models.data_source import DataSource, SourceProvider

    with client.app.state.db.session() as db:
        source = DataSource(
            label="S", provider=SourceProvider.DEMO, tab_name="t", column_mapping={}
        )
        salaried = Therapist(display_name="Alpha", employment_type=EmploymentType.SALARIED_BENEFITS)
        percentage = Therapist(
            display_name="Beta", employment_type=EmploymentType.PERCENTAGE_LEGACY
        )
        db.add_all([source, salaried, percentage])
        db.flush()

        def visit(therapist, day, cpt="90837", paid="150.00"):
            return Visit(
                source_id=source.id,
                therapist_id=therapist.id,
                patient_name="Patient AA",
                patient_name_normalized="PATIENT AA",
                dos=day,
                cpt=cpt,
                cpt_base=cpt,
                insurance_short="KS",
                location_short="TH",
                total_paid=Decimal(paid),
                total_due=Decimal(paid),
                total_balance=Decimal("0.00"),
            )

        db.add_all(
            [
                visit(salaried, date(2026, 4, 6)),
                visit(salaried, date(2026, 4, 7)),
                visit(salaried, date(2026, 4, 13)),
                visit(salaried, date(2026, 4, 9), cpt="99998", paid="0.00"),
                visit(percentage, date(2026, 4, 14)),
            ]
        )
        return {"salaried": salaried.id, "percentage": percentage.id}


@pytest.fixture
def manager(client, practice):
    with client.app.state.db.session() as db:
        email = make_user(
            db,
            email="mgr.util@example.invalid",
            role=Role.MANAGER,
            modules=(Module.THERAPIST_UTILIZATION,),
        ).email
    sign_in(client, email)
    return client


@pytest.fixture
def viewer(client, practice):
    with client.app.state.db.session() as db:
        email = make_user(
            db,
            email="view.util@example.invalid",
            role=Role.VIEWER,
            modules=(Module.THERAPIST_UTILIZATION,),
        ).email
    sign_in(client, email)
    return client


def set_threshold(client, value: int) -> None:
    """Threshold changes need an admin, so use a separate admin client."""
    from fastapi.testclient import TestClient

    with client.app.state.db.session() as db:
        email = make_user(db, email=f"adm{value}@example.invalid", role=Role.ADMIN).email
    admin = TestClient(client.app)
    sign_in(admin, email)
    page = admin.get("/admin/config")
    admin.post(
        "/admin/config",
        data={
            "csrf_token": token_from(page.text),
            "benefits_session_threshold": str(value),
            "cpt_exclusion_list": "99998, 99999",
            "week_start_day": "monday",
            "session_timeout_minutes": "15",
        },
    )


# --------------------------------------------------------------------------- access


def test_the_module_needs_its_own_grant(client, practice):
    """Financial access does not carry utilization access."""
    with client.app.state.db.session() as db:
        email = make_user(db, email="fin.only@example.invalid", modules=(Module.FINANCIAL,)).email
    sign_in(client, email)
    assert client.get("/reports/therapist-utilization").status_code == 403


def test_a_granted_viewer_can_read(viewer):
    assert viewer.get(f"/reports/therapist-utilization?{ALL}").status_code == 200


def test_anonymous_is_sent_to_login(client):
    response = client.get("/reports/therapist-utilization", follow_redirects=False)
    assert response.status_code == 303


# ---------------------------------------------------------------------- status board


def test_board_renders_with_its_questions(manager):
    page = manager.get(f"/reports/therapist-utilization?{ALL}").text
    assert "Who is short of their threshold?" in page
    assert "Who is not measured at all?" in page
    assert "How do the therapists compare?" in page


def test_board_shows_no_patient_identity(manager):
    page = manager.get(f"/reports/therapist-utilization?{ALL}").text
    assert "Patient AA" not in page


def test_percentage_therapists_carry_no_status(manager, practice):
    """Not a passing status. A green tick implies a target they never had."""
    set_threshold(manager, 10)
    page = manager.get(f"/reports/therapist-utilization?{ALL}").text

    assert "not measured" in page
    # Beta has one session but must not be flagged below a threshold.
    beta_row = re.search(r"Beta.*?</tr>", page, re.S)
    assert beta_row and "below threshold" not in beta_row.group(0)


def test_a_salaried_therapist_below_the_threshold_is_flagged(manager):
    set_threshold(manager, 10)
    page = manager.get(f"/reports/therapist-utilization?{ALL}").text
    assert "below expectation" in page


def test_the_threshold_decides_the_status(manager):
    """Alpha runs 2.0 sessions per week in this range, so the status follows it."""
    set_threshold(manager, 2)
    relaxed = manager.get(f"/reports/therapist-utilization?{WEEK1}").text
    assert "meeting it" in relaxed
    assert "below threshold" not in relaxed

    set_threshold(manager, 10)
    strict = manager.get(f"/reports/therapist-utilization?{WEEK1}").text
    assert "below expectation" in strict
    assert "at threshold" not in strict


def test_cancellations_carry_their_caveat(manager):
    page = manager.get(f"/reports/therapist-utilization?{ALL}").text
    assert "Ask before concluding" in page


def test_low_counts_carry_their_caveat(manager):
    page = manager.get(f"/reports/therapist-utilization?{ALL}").text
    assert "A low count is not a conclusion" in page


# ------------------------------------------------------------------------- drill in


def test_drill_in_shows_period_history(manager, practice):
    page = manager.get(f"/reports/therapist-utilization/{practice['salaried']}?{ALL}").text
    assert "Is this a trend or a bad week?" in page
    assert "therapist-history-data" in page
    # Weeks are labelled by their Monday.
    assert "6 Apr" in page


def test_drill_in_for_an_unmeasured_therapist_says_so(manager, practice):
    page = manager.get(f"/reports/therapist-utilization/{practice['percentage']}?{ALL}").text
    assert "No session threshold applies" in page


def test_drill_in_on_a_missing_therapist_is_a_404(manager):
    assert manager.get(f"/reports/therapist-utilization/9999?{ALL}").status_code == 404


# ----------------------------------------------------------------------------- notes


def save_note(client, therapist_id: int, period: str, body: str):
    page = client.get(f"/reports/therapist-utilization/{therapist_id}?{ALL}")
    return client.post(
        f"/reports/therapist-utilization/{therapist_id}/notes?{ALL}",
        data={"csrf_token": token_from(page.text), "period": period, "body": body},
        follow_redirects=False,
    )


def test_a_manager_can_add_a_note(manager, practice):
    response = save_note(manager, practice["salaried"], "2026-04-06", "Referral shortage.")
    assert response.status_code == 303

    page = manager.get(f"/reports/therapist-utilization/{practice['salaried']}?{ALL}").text
    assert "Referral shortage." in page


def test_a_note_on_the_first_bucket_is_not_lost(manager, practice):
    """The first week usually starts before the range does, which is easy to drop."""
    page = manager.get(f"/reports/therapist-utilization/{practice['salaried']}?{ALL}")
    periods = re.findall(r'name="period" value="([^"]+)"', page.text)
    assert periods[0] < "2026-04-01", "expected the first bucket to precede the range"

    save_note(manager, practice["salaried"], periods[0], "Note on the leading week.")

    refreshed = manager.get(f"/reports/therapist-utilization/{practice['salaried']}?{ALL}").text
    assert "Note on the leading week." in refreshed


def test_the_note_travels_onto_the_status_board(manager, practice):
    """Context belongs where the number is read, not one page away."""
    save_note(manager, practice["salaried"], "2026-04-06", "On leave for two weeks.")
    board = manager.get(f"/reports/therapist-utilization?{ALL}").text
    assert "On leave for two weeks." in board


def test_a_note_can_be_edited(manager, practice):
    save_note(manager, practice["salaried"], "2026-04-06", "First version.")
    save_note(manager, practice["salaried"], "2026-04-06", "Second version.")

    page = manager.get(f"/reports/therapist-utilization/{practice['salaried']}?{ALL}").text
    assert "Second version." in page
    assert "First version." not in page

    with manager.app.state.db.session() as db:
        notes = db.execute(select(UtilizationNote)).scalars().all()
    assert len(notes) == 1


def test_clearing_a_note_removes_it(manager, practice):
    save_note(manager, practice["salaried"], "2026-04-06", "Temporary.")
    save_note(manager, practice["salaried"], "2026-04-06", "")

    with manager.app.state.db.session() as db:
        assert db.execute(select(UtilizationNote)).scalars().all() == []


def test_a_period_is_snapped_to_its_boundary(manager, practice):
    """A note must attach to a period the reports actually render."""
    save_note(manager, practice["salaried"], "2026-04-08", "Midweek submission.")

    with manager.app.state.db.session() as db:
        note = db.execute(select(UtilizationNote)).scalar_one()
    assert note.period_start == date(2026, 4, 6)


def test_an_unparseable_period_is_refused_without_erroring(manager, practice):
    response = save_note(manager, practice["salaried"], "not-a-date", "Body.")
    assert response.status_code == 303

    with manager.app.state.db.session() as db:
        assert db.execute(select(UtilizationNote)).scalars().all() == []


def test_a_viewer_cannot_write_a_note(viewer, practice):
    """Role controls what you can do, separately from what you can see."""
    page = viewer.get(f"/reports/therapist-utilization/{practice['salaried']}?{ALL}")
    assert page.status_code == 200
    # The form is not rendered, but that is a courtesy, not the control.
    assert 'name="body"' not in page.text

    response = viewer.post(
        f"/reports/therapist-utilization/{practice['salaried']}/notes?{ALL}",
        data={
            "csrf_token": token_from(page.text),
            "period": "2026-04-06",
            "body": "Should not save.",
        },
    )
    assert response.status_code == 403

    with viewer.app.state.db.session() as db:
        assert db.execute(select(UtilizationNote)).scalars().all() == []


def test_note_changes_are_audited_with_the_previous_text(manager, practice):
    save_note(manager, practice["salaried"], "2026-04-06", "First version.")
    save_note(manager, practice["salaried"], "2026-04-06", "Second version.")

    with manager.app.state.db.session() as db:
        entries = (
            db.execute(
                select(AuditLog)
                .where(AuditLog.target_type == "utilization_note")
                .order_by(AuditLog.id)
            )
            .scalars()
            .all()
        )

    assert len(entries) == 2
    assert '"created": true' in (entries[0].detail or "").lower()
    assert "First version." in (entries[1].detail or "")
    assert "Second version." in (entries[1].detail or "")


def test_a_refused_viewer_write_is_audited(viewer, practice):
    page = viewer.get(f"/reports/therapist-utilization/{practice['salaried']}?{ALL}")
    viewer.post(
        f"/reports/therapist-utilization/{practice['salaried']}/notes?{ALL}",
        data={"csrf_token": token_from(page.text), "period": "2026-04-06", "body": "x"},
    )

    with viewer.app.state.db.session() as db:
        denied = (
            db.execute(select(AuditLog).where(AuditLog.action == AuditAction.ACCESS_DENIED))
            .scalars()
            .all()
        )
    assert denied


# ---------------------------------------------------------------------------- export


def test_export_includes_status_and_notes(manager, practice):
    save_note(manager, practice["salaried"], "2026-04-06", "Referral shortage.")
    response = manager.get(f"/reports/therapist-utilization/export.csv?{ALL}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "Referral shortage." in response.text
    assert "measured against their own ex" in response.text
    assert "Patient AA" not in response.text


def test_export_is_audited(manager):
    manager.get(f"/reports/therapist-utilization/export.csv?{ALL}")

    with manager.app.state.db.session() as db:
        entry = db.execute(
            select(AuditLog)
            .where(AuditLog.action == AuditAction.EXPORT)
            .order_by(AuditLog.id.desc())
            .limit(1)
        ).scalar_one()
    assert entry.target_id == "therapist_utilization"
    assert '"threshold"' in (entry.detail or "")


def test_export_route_is_not_shadowed_by_the_drill_in(manager):
    """`/export.csv` must not be parsed as a therapist id."""
    assert manager.get(f"/reports/therapist-utilization/export.csv?{ALL}").status_code == 200


# ---------------------------------------------- threshold math fixes (audit tier 1)


def test_monthly_buckets_carry_no_weekly_status(manager, practice):
    """25 per week cannot grade a month of sessions. Statuses only at week."""
    tid = practice["salaried"]

    monthly = manager.get(
        f"/reports/therapist-utilization/{tid}"
        "?preset=custom&start=2026-04-01&end=2026-04-30&granularity=month"
    ).text
    assert "statuses appear on the weekly view" in monthly
    assert ">below<" not in monthly and ">at threshold<" not in monthly

    weekly = manager.get(
        f"/reports/therapist-utilization/{tid}"
        "?preset=custom&start=2026-04-01&end=2026-04-30&granularity=week"
    ).text
    assert (">below<" in weekly) or (">watch<" in weekly) or (">at threshold<" in weekly)


def test_per_week_average_is_exact_not_weekday_rounded(manager, practice):
    """A 10 day range is 10/7 weeks whatever day it is viewed on. round(days/7) used
    to flip a person across the alert line depending on the viewing weekday."""
    from decimal import Decimal

    from app.reporting import queries

    with manager.app.state.db.session() as db:
        rows = queries.by_therapist(
            db,
            queries.Filters(
                start=date(2026, 4, 6),
                end=date(2026, 4, 15),
                cpt_exclusions=("99998", "99999"),
            ),
            weeks_in_range=Decimal(10) / Decimal(7),
        )
    alpha = next(r for r in rows if r.display_name == "Alpha")
    # 3 sessions in 10 days is exactly 2.1 a week, not 3/1 or 3/2.
    assert alpha.sessions == 3
    assert alpha.sessions_per_week == Decimal("2.1")


def test_a_salaried_therapist_with_zero_visits_is_on_the_board(manager, practice):
    """The person most below threshold is the one with no sessions at all. An inner
    join silently removed them from the exact page built to show them."""
    with manager.app.state.db.session() as db:
        db.add(
            Therapist(
                display_name="Zero Sessions", employment_type=EmploymentType.SALARIED_BENEFITS
            )
        )

    page = manager.get(
        "/reports/therapist-utilization?preset=custom&start=2026-04-01&end=2026-04-30"
    ).text
    assert "Zero Sessions" in page


def test_an_inactive_therapist_with_no_range_visits_stays_off_the_board(manager, practice):
    with manager.app.state.db.session() as db:
        db.add(
            Therapist(
                display_name="Long Gone",
                employment_type=EmploymentType.SALARIED_BENEFITS,
                active=False,
            )
        )

    page = manager.get(
        "/reports/therapist-utilization?preset=custom&start=2026-04-01&end=2026-04-30"
    ).text
    assert "Long Gone" not in page


# ----------------------------------------------------------- weekly session counts


def test_weekly_counts_shows_one_row_per_week(manager):
    page = manager.get("/reports/therapist-utilization/weekly?weeks=4").text
    assert page.count("Week of ") == 4
    assert "Last 4 weeks" in page


def test_weekly_counts_carry_their_explicit_date_range(manager):
    """Each week names its own Monday to Sunday span, so nobody needs a calendar to
    decode which days "week of 6 Apr" covers."""
    page = manager.get("/reports/therapist-utilization/weekly?weeks=52").text
    assert "Week of 6 Apr" in page
    assert "Mon 6 Apr 2026 to Sun 12 Apr 2026" in page


def test_weekly_counts_count_sessions_not_cancellations(manager):
    """The window spanning April holds 4 sessions: Alpha's 3 plus Beta's 1. Alpha's
    99998 cancellation is present in the data and must not be counted."""
    page = manager.get("/reports/therapist-utilization/weekly?weeks=52").text
    match = re.search(r'"sessions": (\[[^\]]*\])', page)
    assert match, "no chart data island on the page"
    import json

    assert sum(json.loads(match.group(1))) == 4


def test_weekly_counts_garbage_and_extremes_fall_back_not_error(manager):
    """A mistyped URL shows a dashboard, not a stack trace, like every other picker."""
    assert "Last 8 weeks" in manager.get("/reports/therapist-utilization/weekly?weeks=banana").text
    assert "Last 104 weeks" in manager.get("/reports/therapist-utilization/weekly?weeks=9999").text
    assert "Last 1 week," in manager.get("/reports/therapist-utilization/weekly?weeks=-3").text
