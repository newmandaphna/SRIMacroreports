"""Auto-sync: the due decision, the pass itself, and the admin setting."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models.audit import AuditLog
from app.models.data_source import DataSource, SourceProvider
from app.models.enums import AuditAction, Role
from app.models.types import utcnow
from app.models.visit import Visit
from app.sync.scheduler import run_due_syncs, sources_due
from tests.conftest import make_user, sign_in


def token_from(text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', text)
    assert match, "no CSRF token on page"
    return match.group(1)


@pytest.fixture
def demo_ready(client):
    """A demo source, ready to sync, with the demo therapists it needs."""
    from app.models.therapist import AliasSource, EmploymentType, Therapist, TherapistAlias
    from app.sync.demo_data import DEMO_TAB_NAME, DEMO_THERAPISTS, Q2_HEADERS
    from app.sync.engine import suggest_mapping

    with client.app.state.db.session() as db:
        for display_name, aliases in DEMO_THERAPISTS:
            therapist = Therapist(
                display_name=display_name, employment_type=EmploymentType.SALARIED_BENEFITS
            )
            db.add(therapist)
            db.flush()
            for alias in aliases:
                db.add(
                    TherapistAlias(
                        therapist_id=therapist.id, alias=alias, source=AliasSource.MANUAL
                    )
                )
        headers = [str(h) if h is not None else "" for h in Q2_HEADERS]
        source = DataSource(
            label="Demo",
            provider=SourceProvider.DEMO,
            tab_name=DEMO_TAB_NAME,
            header_row=1,
            column_mapping=suggest_mapping(headers),
            active=True,
        )
        db.add(source)
        db.flush()
        return source.id


def test_nothing_is_due_when_auto_sync_is_off(client, demo_ready):
    with client.app.state.db.session() as db:
        assert sources_due(db, now=utcnow(), interval_days=0) == []


def test_a_never_synced_ready_source_is_due_immediately(client, demo_ready):
    with client.app.state.db.session() as db:
        due = sources_due(db, now=utcnow(), interval_days=7)
        assert [s.id for s in due] == [demo_ready]


def test_a_recently_synced_source_is_not_due(client, demo_ready):
    with client.app.state.db.session() as db:
        db.get(DataSource, demo_ready).last_synced_at = utcnow() - timedelta(days=2)
    with client.app.state.db.session() as db:
        assert sources_due(db, now=utcnow(), interval_days=7) == []
        assert len(sources_due(db, now=utcnow(), interval_days=1)) == 1


def test_inactive_and_upload_sources_are_never_due(client, demo_ready):
    with client.app.state.db.session() as db:
        db.get(DataSource, demo_ready).active = False
        db.add(
            DataSource(
                label="Uploads",
                provider=SourceProvider.UPLOAD,
                tab_name="Uploaded CSV",
                column_mapping={"therapist": "T", "patient_name": "P", "dos": "D", "cpt": "C"},
                active=True,
            )
        )
    with client.app.state.db.session() as db:
        assert sources_due(db, now=utcnow(), interval_days=1) == []


def test_due_sources_come_back_oldest_first_and_not_at_the_database_s_whim(client, demo_ready):
    """Order matters because two sheets can hold the same visit.

    Without an ORDER BY the database returns rows however it likes, so when two sources
    overlap the last one in the pass wins and the shared figure can move between passes
    with nothing having changed. Oldest sync first, so a pass is repeatable.
    """
    from app.sync.demo_data import DEMO_TAB_NAME, Q2_HEADERS
    from app.sync.engine import suggest_mapping

    headers = [str(h) if h is not None else "" for h in Q2_HEADERS]
    with client.app.state.db.session() as db:
        db.get(DataSource, demo_ready).last_synced_at = utcnow() - timedelta(days=30)
        for label, days in (("Newer", 10), ("Oldest", 90)):
            db.add(
                DataSource(
                    label=label,
                    provider=SourceProvider.DEMO,
                    tab_name=DEMO_TAB_NAME,
                    header_row=1,
                    column_mapping=suggest_mapping(headers),
                    active=True,
                    last_synced_at=utcnow() - timedelta(days=days),
                )
            )

    with client.app.state.db.session() as db:
        due = sources_due(db, now=utcnow(), interval_days=7)
    assert [s.label for s in due] == ["Oldest", "Demo", "Newer"]


def test_a_due_pass_imports_and_audits_as_auto_sync(client, demo_ready):
    with client.app.state.db.session() as db:
        from app import config_store

        config_store.set_value(db, config_store.AUTO_SYNC_DAYS, 7, actor_id=None)

    with client.app.state.db.session() as db:
        ran = run_due_syncs(db, client.app.state.settings)
        assert ran == 1

    with client.app.state.db.session() as db:
        visits = db.execute(
            select(func.count(Visit.id)).where(Visit.source_id == demo_ready)
        ).scalar_one()
        assert visits > 0

        entry = (
            db.execute(
                select(AuditLog)
                .where(AuditLog.action == AuditAction.SYNC_RUN)
                .order_by(AuditLog.id.desc())
            )
            .scalars()
            .first()
        )
        assert entry is not None
        import json

        assert json.loads(entry.detail).get("auto") is True
        assert entry.actor_label == "auto-sync"

    # The source is stamped, so the next pass within the interval does nothing.
    with client.app.state.db.session() as db:
        assert run_due_syncs(db, client.app.state.settings) == 0


def test_the_setting_round_trips_through_the_admin_form(client):
    with client.app.state.db.session() as db:
        email = make_user(db, email="autosync.admin@example.invalid", role=Role.ADMIN).email
    sign_in(client, email)

    page = client.get("/admin/config")
    response = client.post(
        "/admin/config",
        data={
            "csrf_token": token_from(page.text),
            "benefits_session_threshold": "25",
            "cpt_exclusion_list": "99998, 99999",
            "week_start_day": "monday",
            "session_timeout_minutes": "15",
            "auto_sync_days": "7",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert 'value="7"' in client.get("/admin/config").text

    # Out of range is refused, not clamped silently.
    page = client.get("/admin/config")
    refused = client.post(
        "/admin/config",
        data={
            "csrf_token": token_from(page.text),
            "benefits_session_threshold": "25",
            "cpt_exclusion_list": "99998, 99999",
            "week_start_day": "monday",
            "session_timeout_minutes": "15",
            "auto_sync_days": "99",
        },
    )
    assert refused.status_code == 400
