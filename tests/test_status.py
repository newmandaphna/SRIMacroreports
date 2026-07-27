"""The In development page.

Its whole value is that it reflects reality rather than a hardcoded list, so most of
these tests are about items appearing and disappearing as the underlying state changes.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest

from app.models.data_source import DataSource, RejectReason, SourceProvider, SyncMode, SyncRun
from app.models.data_source import ImportError as ImportErrorRow
from app.models.enums import Module, Role
from app.models.therapist import EmploymentType, Therapist
from app.models.visit import Visit
from tests.conftest import make_user, sign_in


def token_from(text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', text)
    assert match, "no CSRF token on page"
    return match.group(1)


@pytest.fixture
def admin(client):
    with client.app.state.db.session() as db:
        email = make_user(
            db, email="status.admin@example.invalid", role=Role.ADMIN, modules=tuple(Module)
        ).email
    sign_in(client, email)
    return client


@pytest.fixture
def viewer(client):
    with client.app.state.db.session() as db:
        email = make_user(
            db, email="status.viewer@example.invalid", modules=(Module.FINANCIAL,)
        ).email
    sign_in(client, email)
    return client


def add_therapist(client, name: str, employment: EmploymentType) -> int:
    with client.app.state.db.session() as db:
        therapist = Therapist(display_name=name, employment_type=employment)
        db.add(therapist)
        db.flush()
        return therapist.id


def add_rejection(client, reason: RejectReason) -> None:
    from sqlalchemy import select

    with client.app.state.db.session() as db:
        source = db.execute(select(DataSource)).scalars().first()
        if source is None:
            source = DataSource(
                label="S", provider=SourceProvider.DEMO, tab_name="t", column_mapping={}
            )
            db.add(source)
            db.flush()
        run = SyncRun(source_id=source.id, mode=SyncMode.LIVE)
        db.add(run)
        db.flush()
        db.add(
            ImportErrorRow(
                sync_run_id=run.id, source_id=source.id, reason=reason, source_row_ref="7"
            )
        )


def set_threshold(client, value: int) -> None:
    page = client.get("/admin/config")
    client.post(
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


def test_any_signed_in_user_can_read_it(viewer):
    assert viewer.get("/status").status_code == 200


def test_anonymous_is_sent_to_login(client):
    response = client.get("/status", follow_redirects=False)
    assert response.status_code == 303


def test_it_is_linked_from_every_page(admin):
    assert "In development" in admin.get("/reports").text


def test_it_holds_no_patient_identity(admin):
    """It is a page about the state of the build, not about anyone's care."""
    from app.models.data_source import DataSource, SourceProvider

    with admin.app.state.db.session() as db:
        source = DataSource(
            label="S", provider=SourceProvider.DEMO, tab_name="t", column_mapping={}
        )
        therapist = Therapist(
            display_name="Alpha", employment_type=EmploymentType.SALARIED_BENEFITS
        )
        db.add_all([source, therapist])
        db.flush()
        db.add(
            Visit(
                source_id=source.id,
                therapist_id=therapist.id,
                patient_name="Patient AA",
                patient_name_normalized="PATIENT AA",
                dos=date(2026, 4, 6),
                cpt="90837",
                cpt_base="90837",
                total_paid=Decimal("150.00"),
                total_due=Decimal("150.00"),
                total_balance=Decimal("0.00"),
            )
        )

    page = admin.get("/status").text
    assert "Patient AA" not in page


# ------------------------------------------------------------ the threshold item


def test_the_unconfirmed_threshold_is_flagged(admin):
    page = admin.get("/status").text
    assert "threshold has never been confirmed" in page
    assert "Set the threshold" in page


def test_confirming_the_threshold_removes_the_item(admin):
    set_threshold(admin, 18)
    page = admin.get("/status").text
    assert "threshold has never been confirmed" not in page


def test_deliberately_choosing_the_default_looks_like_never_choosing(admin):
    """A known limitation, asserted so it is documented rather than discovered.

    The page infers "unconfirmed" from the stored value equalling the environment
    default, so an admin who considers 25 and decides it is right still sees the
    prompt. Telling the two apart would need a separate "confirmed" flag per setting,
    which is more machinery than the problem deserves: the cost is one prompt that
    can be dismissed by setting any other value.
    """
    default = admin.app.state.settings.benefits_session_threshold
    set_threshold(admin, default)
    assert "threshold has never been confirmed" in admin.get("/status").text


# --------------------------------------------------------- the employment type item


def test_unmeasured_therapists_are_flagged(admin):
    add_therapist(admin, "Unset One", EmploymentType.OTHER)
    page = admin.get("/status").text
    assert "not measured against any threshold" in page
    assert "Set employment types" in page


def test_the_item_disappears_when_everyone_is_classified(admin):
    add_therapist(admin, "Salaried One", EmploymentType.SALARIED_BENEFITS)
    page = admin.get("/status").text
    assert "not measured against any threshold" not in page


def test_percentage_therapists_count_as_unmeasured(admin):
    """They genuinely have no threshold, so the page should say so rather than hide it."""
    add_therapist(admin, "Percentage One", EmploymentType.PERCENTAGE_LEGACY)
    page = admin.get("/status").text
    assert "not measured against any threshold" in page


# ------------------------------------------------------------------------ caveats


def test_undated_rows_are_surfaced_as_a_caveat(admin):
    add_rejection(admin, RejectReason.MISSING_DOS)
    page = admin.get("/status").text
    assert "no usable date of service" in page
    assert "understated" in page


def test_resolving_the_rejection_removes_the_caveat(admin):
    add_rejection(admin, RejectReason.MISSING_DOS)
    assert "no usable date of service" in admin.get("/status").text

    from sqlalchemy import select

    from app.models.types import utcnow

    with admin.app.state.db.session() as db:
        entry = db.execute(select(ImportErrorRow)).scalar_one()
        entry.resolved_at = utcnow()

    assert "no usable date of service" not in admin.get("/status").text


def test_other_rejections_are_reported_separately(admin):
    add_rejection(admin, RejectReason.UNKNOWN_THERAPIST)
    page = admin.get("/status").text
    assert "rejected and not yet reviewed" in page


def test_data_caveats_appear_only_once_there_is_data(admin):
    """No point warning about cancellation recording before anything is imported."""
    assert "Cancellation rates are not comparable" not in admin.get("/status").text

    from app.models.data_source import DataSource, SourceProvider

    with admin.app.state.db.session() as db:
        source = DataSource(
            label="S2", provider=SourceProvider.DEMO, tab_name="t", column_mapping={}
        )
        therapist = Therapist(display_name="Beta", employment_type=EmploymentType.SALARIED_BENEFITS)
        db.add_all([source, therapist])
        db.flush()
        db.add(
            Visit(
                source_id=source.id,
                therapist_id=therapist.id,
                patient_name="Patient AB",
                patient_name_normalized="PATIENT AB",
                dos=date(2026, 4, 6),
                cpt="90837",
                cpt_base="90837",
                total_paid=Decimal("150.00"),
                total_due=Decimal("150.00"),
                total_balance=Decimal("0.00"),
            )
        )

    page = admin.get("/status").text
    assert "Cancellation rates are not comparable" in page
    assert "counted as revenue, not as sessions" in page


# --------------------------------------------------------------------- not built


def test_the_gated_module_is_listed_as_not_started(admin):
    page = admin.get("/status").text
    assert "Patient level funnel" in page
    assert "practice owner confirms" in page


def test_a_disabled_feature_is_listed(admin):
    """Room utilization is built but off, which is different from not existing."""
    page = admin.get("/status").text
    assert "Room utilization" in page
    assert "switched off" in page


# ----------------------------------------------------------------------- actions


def test_a_viewer_is_not_offered_admin_actions(viewer):
    """The page tells everyone what is open; only an admin gets the button."""
    page = viewer.get("/status").text
    assert "threshold has never been confirmed" in page
    assert "Set the threshold" not in page
    assert "/admin/config" not in page
