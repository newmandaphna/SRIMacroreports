"""The Data Sources admin area: access control, rotation, sync, and lookups."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import func, select

from app.models.audit import AuditLog
from app.models.data_source import (
    DataSource,
    Lookup,
    LookupKind,
    SourceProvider,
)
from app.models.data_source import (
    ImportError as ImportErrorRow,
)
from app.models.enums import AuditAction, Module, Role
from app.models.therapist import Therapist, TherapistAlias
from app.models.visit import Visit
from tests.conftest import make_user, sign_in


def token_from(text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', text)
    assert match, "no CSRF token on page"
    return match.group(1)


@pytest.fixture
def admin_client(client):
    with client.app.state.db.session() as db:
        email = make_user(db, email="src.admin@example.invalid", role=Role.ADMIN).email
    sign_in(client, email)
    return client


@pytest.fixture
def demo(admin_client):
    """Create the demo source through the admin route, as an admin would."""
    page = admin_client.get("/admin/sources")
    response = admin_client.post(
        "/admin/sources/demo",
        data={"csrf_token": token_from(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[-1])


def csrf(admin_client, source_id: int) -> str:
    return token_from(admin_client.get(f"/admin/sources/{source_id}").text)


# --------------------------------------------------------------------- access


def test_non_admin_cannot_reach_data_sources(client):
    with client.app.state.db.session() as db:
        email = make_user(
            db,
            email="mgr.src@example.invalid",
            role=Role.MANAGER,
            modules=(Module.FINANCIAL,),
        ).email
    sign_in(client, email)
    assert client.get("/admin/sources").status_code == 403


def test_anonymous_is_sent_to_login(client):
    response = client.get("/admin/sources", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/admin/sources"


# ------------------------------------------------------------------ the registry


def test_empty_state_explains_what_to_do(admin_client):
    page = admin_client.get("/admin/sources")
    assert page.status_code == 200
    assert "No data sources yet" in page.text


def test_adding_a_quarter_extracts_the_spreadsheet_id(admin_client):
    page = admin_client.get("/admin/sources")
    response = admin_client.post(
        "/admin/sources/new",
        data={
            "csrf_token": token_from(page.text),
            "label": "Q3 2026",
            "provider": SourceProvider.GOOGLE_SHEETS.value,
            "spreadsheet_url": "https://docs.google.com/spreadsheets/d/SHEET123/edit?usp=sharing",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with admin_client.app.state.db.session() as db:
        source = db.execute(select(DataSource).where(DataSource.label == "Q3 2026")).scalar_one()
    assert source.spreadsheet_id == "SHEET123"


def test_a_bad_url_is_refused_with_guidance(admin_client):
    page = admin_client.get("/admin/sources")
    response = admin_client.post(
        "/admin/sources/new",
        data={
            "csrf_token": token_from(page.text),
            "label": "Q4 2026",
            "provider": SourceProvider.GOOGLE_SHEETS.value,
            "spreadsheet_url": "https://example.invalid/not/a/sheet",
        },
    )
    assert response.status_code == 400
    assert "/spreadsheets/d/" in response.text


def test_duplicate_label_is_refused(admin_client, demo):
    page = admin_client.get("/admin/sources")
    response = admin_client.post(
        "/admin/sources/new",
        data={
            "csrf_token": token_from(page.text),
            "label": "Demo (synthetic)",
            "provider": SourceProvider.GOOGLE_SHEETS.value,
            "spreadsheet_url": "https://docs.google.com/spreadsheets/d/OTHER/edit",
        },
    )
    assert response.status_code == 400
    assert "already exists" in response.text


def test_next_quarter_prefills_the_previous_mapping(admin_client, demo):
    """Adding a quarter should be a confirmation, not eighteen retyped headers."""
    page = admin_client.get("/admin/sources")
    admin_client.post(
        "/admin/sources/new",
        data={
            "csrf_token": token_from(page.text),
            "label": "Q3 2026",
            "provider": SourceProvider.GOOGLE_SHEETS.value,
            "spreadsheet_url": "https://docs.google.com/spreadsheets/d/NEXTQ/edit",
        },
    )
    with admin_client.app.state.db.session() as db:
        previous = db.get(DataSource, demo)
        new = db.execute(select(DataSource).where(DataSource.label == "Q3 2026")).scalar_one()
    assert new.column_mapping == previous.column_mapping
    assert new.column_mapping != {}


def test_deactivating_a_source_leaves_its_rows_in_place(admin_client, demo):
    """Rotation must never touch history: the database is the system of record."""
    admin_client.post(
        f"/admin/sources/{demo}/sync",
        data={"csrf_token": csrf(admin_client, demo), "mode": "live"},
    )
    with admin_client.app.state.db.session() as db:
        before = db.execute(select(func.count(Visit.id))).scalar_one()
    assert before > 0

    admin_client.post(
        f"/admin/sources/{demo}",
        data={
            "csrf_token": csrf(admin_client, demo),
            "label": "Demo (synthetic)",
            "tab_name": "Q2 Snapshot (demo)",
            "header_row": "1",
            # `active` omitted, which is how an unchecked checkbox arrives.
        },
    )

    with admin_client.app.state.db.session() as db:
        source = db.get(DataSource, demo)
        assert source.active is False
        assert db.execute(select(func.count(Visit.id))).scalar_one() == before


# ----------------------------------------------------------------------- syncing


def test_dry_run_then_live(admin_client, demo):
    response = admin_client.post(
        f"/admin/sources/{demo}/sync",
        data={"csrf_token": csrf(admin_client, demo), "mode": "dry_run"},
        follow_redirects=False,
    )
    run_page = admin_client.get(response.headers["location"])
    assert "Nothing was written" in run_page.text

    with admin_client.app.state.db.session() as db:
        assert db.execute(select(func.count(Visit.id))).scalar_one() == 0

    admin_client.post(
        f"/admin/sources/{demo}/sync",
        data={"csrf_token": csrf(admin_client, demo), "mode": "live"},
    )
    with admin_client.app.state.db.session() as db:
        assert db.execute(select(func.count(Visit.id))).scalar_one() > 0


def test_every_sync_is_audited_with_its_counts(admin_client, demo):
    admin_client.post(
        f"/admin/sources/{demo}/sync",
        data={"csrf_token": csrf(admin_client, demo), "mode": "live"},
    )
    with admin_client.app.state.db.session() as db:
        entry = db.execute(
            select(AuditLog)
            .where(AuditLog.action == AuditAction.SYNC_RUN)
            .order_by(AuditLog.id.desc())
            .limit(1)
        ).scalar_one()
    assert '"mode": "live"' in (entry.detail or "")
    assert '"rows_read"' in (entry.detail or "")
    assert '"rejected"' in (entry.detail or "")


def test_source_changes_are_audited(admin_client, demo):
    with admin_client.app.state.db.session() as db:
        entries = (
            db.execute(select(AuditLog).where(AuditLog.action == AuditAction.DATA_SOURCE_CHANGED))
            .scalars()
            .all()
        )
    assert entries


def test_run_page_lists_the_rejections(admin_client, demo):
    response = admin_client.post(
        f"/admin/sources/{demo}/sync",
        data={"csrf_token": csrf(admin_client, demo), "mode": "live"},
        follow_redirects=False,
    )
    page = admin_client.get(response.headers["location"]).text
    assert "Rejected rows" in page
    assert "Therapist not recognized" in page
    assert "Amount could not be read" in page


def test_viewing_rejections_is_logged_as_a_phi_view(admin_client, demo):
    """Rejected rows can carry patient identity, so reading them is a PHI read."""
    admin_client.post(
        f"/admin/sources/{demo}/sync",
        data={"csrf_token": csrf(admin_client, demo), "mode": "live"},
    )
    admin_client.get(f"/admin/sources/{demo}/errors")

    with admin_client.app.state.db.session() as db:
        views = (
            db.execute(select(AuditLog).where(AuditLog.action == AuditAction.PHI_VIEW))
            .scalars()
            .all()
        )
    assert views


def test_marking_a_rejection_reviewed(admin_client, demo):
    admin_client.post(
        f"/admin/sources/{demo}/sync",
        data={"csrf_token": csrf(admin_client, demo), "mode": "live"},
    )
    with admin_client.app.state.db.session() as db:
        entry = db.execute(select(ImportErrorRow).limit(1)).scalar_one()
        entry_id = entry.id

    with admin_client.app.state.db.session() as db:
        open_before = db.execute(
            select(func.count(ImportErrorRow.id)).where(ImportErrorRow.resolved_at.is_(None))
        ).scalar_one()

    page = admin_client.get(f"/admin/sources/{demo}/errors")
    admin_client.post(
        f"/admin/sources/{demo}/errors/{entry_id}/resolve",
        data={"csrf_token": token_from(page.text), "note": "typo in the sheet"},
    )

    with admin_client.app.state.db.session() as db:
        entry = db.get(ImportErrorRow, entry_id)
        assert entry.is_resolved
        assert entry.resolved_by_id is not None
        assert entry.resolution_note == "typo in the sheet"

        open_after = db.execute(
            select(func.count(ImportErrorRow.id)).where(ImportErrorRow.resolved_at.is_(None))
        ).scalar_one()
    assert open_after == open_before - 1


# ----------------------------------------------------------------------- lookups


def test_abbreviations_import(admin_client, demo):
    admin_client.post(
        f"/admin/sources/{demo}/lookups",
        data={
            "csrf_token": csrf(admin_client, demo),
            "tab_name": "Abbreviations",
            "kind": "abbreviations",
        },
    )
    with admin_client.app.state.db.session() as db:
        rows = db.execute(select(Lookup)).scalars().all()

    assert rows
    kinds = {r.kind for r in rows}
    assert LookupKind.INSURANCE in kinds
    assert LookupKind.LOCATION in kinds

    # Many to one: several long insurance names share a short code.
    blsh = [r for r in rows if r.short_code == "BLSH"]
    assert len(blsh) > 1


def test_abbreviations_import_replaces_rather_than_accumulates(admin_client, demo):
    for _ in range(2):
        admin_client.post(
            f"/admin/sources/{demo}/lookups",
            data={
                "csrf_token": csrf(admin_client, demo),
                "tab_name": "Abbreviations",
                "kind": "abbreviations",
            },
        )
    with admin_client.app.state.db.session() as db:
        first_pass = db.execute(select(func.count(Lookup.id))).scalar_one()

    admin_client.post(
        f"/admin/sources/{demo}/lookups",
        data={
            "csrf_token": csrf(admin_client, demo),
            "tab_name": "Abbreviations",
            "kind": "abbreviations",
        },
    )
    with admin_client.app.state.db.session() as db:
        assert db.execute(select(func.count(Lookup.id))).scalar_one() == first_pass


def test_provider_alias_import_from_the_config_tab(admin_client, demo):
    admin_client.post(
        f"/admin/sources/{demo}/lookups",
        data={
            "csrf_token": csrf(admin_client, demo),
            "tab_name": "Config",
            "kind": "config",
        },
    )
    with admin_client.app.state.db.session() as db:
        wren = db.execute(select(Therapist).where(Therapist.display_name == "Wren")).scalar_one()
        aliases = {
            a.alias
            for a in db.execute(
                select(TherapistAlias).where(TherapistAlias.therapist_id == wren.id)
            ).scalars()
        }
    assert "ROSALIND WREN" in aliases


def test_alias_import_never_reassigns_an_existing_alias(admin_client, demo):
    """The guard against folding two different therapists into one record."""
    with admin_client.app.state.db.session() as db:
        other = Therapist(display_name="Someone Else")
        db.add(other)
        db.flush()
        db.add(TherapistAlias(therapist_id=other.id, alias="ROSALIND WREN"))
        other_id = other.id

    admin_client.post(
        f"/admin/sources/{demo}/lookups",
        data={
            "csrf_token": csrf(admin_client, demo),
            "tab_name": "Config",
            "kind": "config",
        },
    )

    with admin_client.app.state.db.session() as db:
        owner = db.execute(
            select(TherapistAlias.therapist_id).where(TherapistAlias.alias == "ROSALIND WREN")
        ).scalar_one()
        entry = db.execute(
            select(AuditLog)
            .where(AuditLog.action == AuditAction.DATA_SOURCE_CHANGED)
            .order_by(AuditLog.id.desc())
            .limit(1)
        ).scalar_one()

    assert owner == other_id, "an existing alias must not be silently reassigned"
    assert "conflicts" in (entry.detail or "")


def test_raw_tab_is_refused_for_lookup_import(admin_client, demo):
    response = admin_client.post(
        f"/admin/sources/{demo}/lookups",
        data={
            "csrf_token": csrf(admin_client, demo),
            "tab_name": "RAW_Appointments",
            "kind": "abbreviations",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "problem=" in response.headers["location"]

    with admin_client.app.state.db.session() as db:
        assert db.execute(select(func.count(Lookup.id))).scalar_one() == 0


def test_raw_tab_is_not_offered_in_the_picker(admin_client, demo):
    page = admin_client.get(f"/admin/sources/{demo}")
    assert "RAW_Appointments" not in page.text
