"""The Reports pages: access control, rendering, empty states, and export."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.enums import AuditAction, Module, Role
from app.models.therapist import EmploymentType, Therapist
from app.models.visit import Visit
from tests.conftest import make_user, sign_in


def token_from(text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', text)
    assert match, "no CSRF token on page"
    return match.group(1)


@pytest.fixture
def financial_user(client):
    with client.app.state.db.session() as db:
        email = make_user(
            db, email="fin@example.invalid", role=Role.VIEWER, modules=(Module.FINANCIAL,)
        ).email
    sign_in(client, email)
    return client


@pytest.fixture
def with_data(client):
    from app.models.data_source import DataSource, SourceProvider

    with client.app.state.db.session() as db:
        source = DataSource(
            label="S", provider=SourceProvider.DEMO, tab_name="t", column_mapping={}
        )
        therapist = Therapist(
            display_name="Alpha", employment_type=EmploymentType.SALARIED_BENEFITS
        )
        db.add_all([source, therapist])
        db.flush()
        for i, day in enumerate([date(2026, 4, 6), date(2026, 4, 7), date(2026, 4, 14)]):
            db.add(
                Visit(
                    source_id=source.id,
                    therapist_id=therapist.id,
                    patient_name=f"Patient A{chr(65 + i)}",
                    patient_name_normalized=f"PATIENT A{chr(65 + i)}",
                    dos=day,
                    cpt="90837",
                    cpt_base="90837",
                    insurance_short="KS",
                    location_short="TH",
                    total_paid=Decimal("150.00"),
                    total_due=Decimal("150.00"),
                    total_balance=Decimal("0.00"),
                )
            )
    return client


ALL = "preset=custom&start=2026-01-01&end=2026-12-31"


# ------------------------------------------------------------------------- access


def test_anonymous_is_sent_to_login(client):
    response = client.get("/reports", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/reports"


def test_viewer_without_the_grant_is_refused(client):
    with client.app.state.db.session() as db:
        email = make_user(db, email="nogrant@example.invalid", role=Role.VIEWER).email
    sign_in(client, email)
    assert client.get("/reports").status_code == 403
    assert client.get("/reports/financial").status_code == 403
    assert client.get("/reports/financial/export.csv").status_code == 403


def test_viewer_with_the_grant_gets_in(financial_user):
    assert financial_user.get("/reports").status_code == 200
    assert financial_user.get("/reports/financial").status_code == 200


def test_admin_without_the_grant_is_logged_as_emergency_access(client):
    with client.app.state.db.session() as db:
        email = make_user(db, email="adm@example.invalid", role=Role.ADMIN).email
    sign_in(client, email)
    assert client.get("/reports").status_code == 200

    with client.app.state.db.session() as db:
        entries = (
            db.execute(select(AuditLog).where(AuditLog.action == AuditAction.EMERGENCY_ACCESS))
            .scalars()
            .all()
        )
    assert entries


def test_reports_are_not_logged_as_a_phi_view(financial_user, with_data):
    """Aggregate views hold no identity, so they are not PHI reads."""
    financial_user.get(f"/reports?{ALL}")
    financial_user.get(f"/reports/financial?{ALL}")

    with financial_user.app.state.db.session() as db:
        views = (
            db.execute(select(AuditLog).where(AuditLog.action == AuditAction.PHI_VIEW))
            .scalars()
            .all()
        )
    assert views == []


# ------------------------------------------------------------------------ rendering


def test_overview_asks_its_questions_in_plain_language(financial_user, with_data):
    page = financial_user.get(f"/reports?{ALL}").text
    for question in (
        "How much clinical work did we do?",
        "What came in the door?",
        "What are we still owed?",
        "Who is short of their session threshold?",
        "Are we collecting what we bill?",
    ):
        assert question in page, question


def test_overview_never_renders_a_patient_name(financial_user, with_data):
    page = financial_user.get(f"/reports?{ALL}").text
    assert "Patient A" not in page
    financial_page = financial_user.get(f"/reports/financial?{ALL}").text
    assert "Patient A" not in financial_page


def test_figures_render(financial_user, with_data):
    page = financial_user.get(f"/reports/financial?{ALL}").text
    assert "$450.00" in page  # three sessions at 150
    assert ">3<" in page.replace(" ", "").replace("\n", "") or "3" in page


def test_therapist_row_is_plain_text_without_the_utilization_grant(financial_user, with_data):
    """A link that would 403 is worse than no link."""
    page = financial_user.get(f"/reports?{ALL}").text
    assert "Alpha" in page
    assert "/reports/therapist-utilization/" not in page


def test_therapist_row_drills_into_the_weekly_history_when_granted(client, with_data):
    with client.app.state.db.session() as db:
        email = make_user(
            db,
            email="both@example.invalid",
            modules=(Module.FINANCIAL, Module.THERAPIST_UTILIZATION),
        ).email
    sign_in(client, email)

    page = client.get(f"/reports?{ALL}").text
    assert "/reports/therapist-utilization/" in page


def test_kpi_cards_link_into_the_module(financial_user, with_data):
    page = financial_user.get(f"/reports?{ALL}").text
    assert 'class="card card--link" href="/reports/financial' in page


# --------------------------------------------------------------------- empty states


def test_empty_state_when_nothing_has_ever_synced(financial_user):
    page = financial_user.get("/reports").text
    assert "No synced data yet" in page


def test_empty_state_when_the_range_misses_the_data(financial_user, with_data):
    """Tells you what you do have and offers one click to see it."""
    page = financial_user.get("/reports?preset=custom&start=2020-01-01&end=2020-12-31").text
    assert "No data in this range" in page
    assert "2026-04-06" in page
    assert "Show everything imported" in page


def test_a_nonsense_range_still_renders(financial_user, with_data):
    assert financial_user.get("/reports?preset=banana").status_code == 200
    assert financial_user.get("/reports?preset=custom&start=x&end=y").status_code == 200


# -------------------------------------------------------------------------- filters


def test_therapist_filter_narrows_the_figures(financial_user, with_data):
    with financial_user.app.state.db.session() as db:
        therapist_id = db.execute(select(Therapist.id)).scalar_one()

    unfiltered = financial_user.get(f"/reports/financial?{ALL}").text
    filtered = financial_user.get(f"/reports/financial?{ALL}&therapist={therapist_id}").text
    assert "$450.00" in unfiltered
    assert "$450.00" in filtered

    missing = financial_user.get(f"/reports/financial?{ALL}&therapist=9999").text
    assert "No data in this range" in missing


def test_granularity_can_be_chosen(financial_user, with_data):
    monthly = financial_user.get(f"/reports/financial?{ALL}&granularity=month").text
    assert "Apr 2026" in monthly


def test_htmx_partial_returns_only_the_trend(financial_user, with_data):
    response = financial_user.get(f"/reports/financial/trend?{ALL}")
    assert response.status_code == 200
    assert "Are we collecting what we bill?" in response.text
    # A fragment, not a whole page.
    assert "<html" not in response.text


# --------------------------------------------------------------------------- export


@pytest.mark.parametrize("table", ["trend", "therapists", "insurance", "location", "cpt"])
def test_every_table_exports(financial_user, with_data, table):
    response = financial_user.get(f"/reports/financial/export.csv?table={table}&{ALL}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]


def test_export_carries_its_provenance(financial_user, with_data):
    """A spreadsheet mailed onward should still say what window it covers."""
    text = financial_user.get(f"/reports/financial/export.csv?table=trend&{ALL}").text
    assert "SRI Practice Dashboard" in text
    assert "2026-01-01 to 2026-12-31" in text


def test_export_contains_no_patient_identity(financial_user, with_data):
    for table in ("trend", "therapists", "insurance", "location", "cpt"):
        text = financial_user.get(f"/reports/financial/export.csv?table={table}&{ALL}").text
        assert "Patient A" not in text


def test_every_export_is_audit_logged_with_its_filters(financial_user, with_data):
    financial_user.get(f"/reports/financial/export.csv?table=therapists&{ALL}")

    with financial_user.app.state.db.session() as db:
        entry = db.execute(
            select(AuditLog)
            .where(AuditLog.action == AuditAction.EXPORT)
            .order_by(AuditLog.id.desc())
            .limit(1)
        ).scalar_one()

    assert entry.target_id == "financial.therapists"
    assert '"rows"' in (entry.detail or "")
    assert "2026-01-01" in (entry.detail or "")


def test_an_unknown_table_falls_back_rather_than_erroring(financial_user, with_data):
    response = financial_user.get(f"/reports/financial/export.csv?table=nonsense&{ALL}")
    assert response.status_code == 200


# ------------------------------------------------------------------ admin settings


def test_only_an_admin_can_change_the_threshold(financial_user):
    assert financial_user.get("/admin/config").status_code == 403


def test_changing_the_threshold_moves_the_status_board(client, with_data):
    with client.app.state.db.session() as db:
        email = make_user(
            db, email="cfg@example.invalid", role=Role.ADMIN, modules=(Module.FINANCIAL,)
        ).email
    sign_in(client, email)

    page = client.get("/admin/config")
    assert page.status_code == 200

    client.post(
        "/admin/config",
        data={
            "csrf_token": token_from(page.text),
            "benefits_session_threshold": "50",
            "cpt_exclusion_list": "99998, 99999",
            "week_start_day": "monday",
            "session_timeout_minutes": "15",
        },
    )

    overview = client.get(f"/reports?{ALL}").text
    assert "Against 50 sessions per week" in overview
    assert "below threshold" in overview


def test_config_changes_are_audited_with_before_and_after(client):
    with client.app.state.db.session() as db:
        email = make_user(db, email="cfg2@example.invalid", role=Role.ADMIN).email
    sign_in(client, email)

    page = client.get("/admin/config")
    client.post(
        "/admin/config",
        data={
            "csrf_token": token_from(page.text),
            "benefits_session_threshold": "31",
            "cpt_exclusion_list": "99998",
            "week_start_day": "monday",
            "session_timeout_minutes": "15",
        },
    )

    with client.app.state.db.session() as db:
        entry = db.execute(
            select(AuditLog)
            .where(AuditLog.action == AuditAction.CONFIG_CHANGED)
            .order_by(AuditLog.id.desc())
            .limit(1)
        ).scalar_one()
    assert '"to": 31' in (entry.detail or "")


def test_an_out_of_range_threshold_is_refused(client):
    with client.app.state.db.session() as db:
        email = make_user(db, email="cfg3@example.invalid", role=Role.ADMIN).email
    sign_in(client, email)

    page = client.get("/admin/config")
    response = client.post(
        "/admin/config",
        data={
            "csrf_token": token_from(page.text),
            "benefits_session_threshold": "0",
            "cpt_exclusion_list": "",
            "week_start_day": "monday",
            "session_timeout_minutes": "15",
        },
    )
    assert response.status_code == 400
    assert "between 1 and" in response.text
