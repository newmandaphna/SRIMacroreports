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
    SyncRun,
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


# ------------------------------------------------- the unsaved mapping suggestion


def clear_mapping(admin_client, source_id: int) -> None:
    """Put a source back into the state a source created before the mapping was
    persisted at creation would be in: correct headers, nothing stored."""
    with admin_client.app.state.db.session() as db:
        db.get(DataSource, source_id).column_mapping = {}


def test_an_unsaved_suggestion_is_labelled_as_unsaved(admin_client, demo):
    """The dropdowns fall back to a suggestion, so the mapping looks complete while
    Sync refuses. Without this the page contradicts itself and names no next step."""
    clear_mapping(admin_client, demo)
    page = admin_client.get(f"/admin/sources/{demo}").text

    assert "This mapping has not been saved yet" in page
    assert "suggested, not saved" in page


def test_the_sync_panel_names_the_missing_step_rather_than_the_missing_fields(admin_client, demo):
    """ "Map these first: dos" is a dead end when dos is already filled in on screen."""
    clear_mapping(admin_client, demo)
    page = admin_client.get(f"/admin/sources/{demo}").text

    assert "has not been saved" in page
    assert "Save source" in page


def test_saving_the_suggestion_clears_the_warning_and_enables_sync(admin_client, demo):
    clear_mapping(admin_client, demo)

    page = admin_client.get(f"/admin/sources/{demo}").text

    # Re-post the form the way a browser would: every select at the value it displays,
    # which for an unsaved source is the suggestion.
    data = {
        "csrf_token": token_from(page),
        "label": "Demo (synthetic)",
        "tab_name": "Q2 Snapshot (demo)",
        "header_row": "1",
        "active": "1",
    }
    for block in re.findall(r'name="(map__[a-z_]+)"[^>]*>(.*?)</select>', page, re.S):
        name, options = block
        chosen = re.search(r'<option value="([^"]*)" selected', options)
        data[name] = chosen.group(1) if chosen else ""

    assert data["map__dos"], "the form should be displaying a suggestion for dos"
    admin_client.post(f"/admin/sources/{demo}", data=data, follow_redirects=True)

    with admin_client.app.state.db.session() as db:
        source = db.get(DataSource, demo)
        assert not source.missing_required_fields
        assert source.is_ready_to_sync

    page = admin_client.get(f"/admin/sources/{demo}").text
    assert "This mapping has not been saved yet" not in page
    assert "suggested, not saved" not in page


def test_a_saved_mapping_never_shows_the_warning(admin_client, demo):
    """The demo route persists the mapping at creation, so this is the normal path."""
    page = admin_client.get(f"/admin/sources/{demo}").text
    assert "This mapping has not been saved yet" not in page
    assert "suggested, not saved" not in page


def test_the_warning_stays_off_when_the_sheet_cannot_be_read(admin_client, demo):
    """No headers means no suggestion, so the honest message is the field list."""
    with admin_client.app.state.db.session() as db:
        source = db.get(DataSource, demo)
        source.column_mapping = {}
        source.tab_name = "No Such Tab"

    page = admin_client.get(f"/admin/sources/{demo}").text
    assert "This mapping has not been saved yet" not in page
    assert "Map these first" in page


# ------------------------------------------- systemic failures and error paging


def seed_run_with_errors(admin_client, source_id: int, *, count: int, rows_read: int) -> int:
    """A run whose rejections are fabricated directly, so scale is cheap."""
    from app.models.data_source import RejectReason, SyncMode, SyncStatus

    with admin_client.app.state.db.session() as db:
        run = SyncRun(
            source_id=source_id,
            mode=SyncMode.LIVE,
            status=SyncStatus.SUCCESS,
            rows_read=rows_read,
            rows_rejected=count,
        )
        db.add(run)
        db.flush()
        for i in range(count):
            db.add(
                ImportErrorRow(
                    sync_run_id=run.id,
                    source_id=source_id,
                    reason=RejectReason.MISSING_DOS,
                    field="dos",
                    source_row_ref=str(i + 2),
                    therapist_hint="YEO",
                )
            )
        return run.id


def test_a_systemic_failure_is_named_as_a_sheet_problem(admin_client, demo):
    """6,766 identical rejections are one problem, not 6,766. The run page must say
    so instead of presenting them as individual errors to review."""
    run_id = seed_run_with_errors(admin_client, demo, count=60, rows_read=62)

    page = admin_client.get(f"/admin/sources/{demo}/runs/{run_id}").text
    assert "rejected for the same reason" in page
    assert "property of the sheet" in page
    assert "Populate the dates in the sheet" in page


def test_mixed_rejections_are_not_called_systemic(admin_client, demo):
    """The demo sheet rejects five rows for five different reasons."""
    page = admin_client.get(f"/admin/sources/{demo}").text
    admin_client.post(
        f"/admin/sources/{demo}/sync",
        data={"csrf_token": token_from(page), "mode": "live"},
        follow_redirects=True,
    )
    with admin_client.app.state.db.session() as db:
        run_id = db.execute(select(func.max(SyncRun.id))).scalar_one()

    page = admin_client.get(f"/admin/sources/{demo}/runs/{run_id}").text
    assert "rejected for the same reason" not in page


def test_the_run_page_caps_its_rejection_table(admin_client, demo):
    """A run against the real sheet can reject thousands of rows. The page shows the
    counts for all of them and the rows for a readable sample."""
    run_id = seed_run_with_errors(admin_client, demo, count=210, rows_read=211)

    page = admin_client.get(f"/admin/sources/{demo}/runs/{run_id}").text
    assert page.count("Mark reviewed") == 200
    assert "Showing the first 200 of 210" in re.sub(r"\s+", " ", page)
    assert "No date of service: 210" in page


def test_the_errors_page_paginates_instead_of_capping_silently(admin_client, demo):
    """It used to stop at 500 rows while calling itself the full account."""
    seed_run_with_errors(admin_client, demo, count=210, rows_read=211)

    first = admin_client.get(f"/admin/sources/{demo}/errors").text
    assert first.count("Mark reviewed") == 200
    assert "Showing rows 1 to 200 of 210" in re.sub(r"\s+", " ", first)
    assert "Page 1 of 2" in first

    second = admin_client.get(f"/admin/sources/{demo}/errors?page=2").text
    assert second.count("Mark reviewed") == 10
    assert "Showing rows 201 to 210 of 210" in re.sub(r"\s+", " ", second)


def test_the_errors_page_shows_reason_counts(admin_client, demo):
    seed_run_with_errors(admin_client, demo, count=60, rows_read=62)
    page = admin_client.get(f"/admin/sources/{demo}/errors").text
    assert "No date of service: 60" in page


def test_a_page_number_past_the_end_clamps_rather_than_404s(admin_client, demo):
    seed_run_with_errors(admin_client, demo, count=10, rows_read=12)
    page = admin_client.get(f"/admin/sources/{demo}/errors?page=99")
    assert page.status_code == 200
    assert "Showing rows 1 to 10 of 10" in re.sub(r"\s+", " ", page.text)


def test_superseded_errors_read_as_superseded_not_reviewed(admin_client, demo):
    """The distinction matters: reviewed records a human decision, superseded records
    that a newer read of the sheet replaced the list."""
    page = admin_client.get(f"/admin/sources/{demo}").text
    for _ in range(2):
        admin_client.post(
            f"/admin/sources/{demo}/sync",
            data={"csrf_token": csrf(admin_client, demo), "mode": "dry_run"},
            follow_redirects=True,
        )

    page = admin_client.get(f"/admin/sources/{demo}/errors?show=all").text
    assert "superseded" in page
    assert "Superseded by sync run" in page


def test_the_sync_audit_records_how_many_errors_were_superseded(admin_client, demo):
    for _ in range(2):
        admin_client.post(
            f"/admin/sources/{demo}/sync",
            data={"csrf_token": csrf(admin_client, demo), "mode": "live"},
            follow_redirects=True,
        )

    with admin_client.app.state.db.session() as db:
        entry = db.execute(
            select(AuditLog)
            .where(AuditLog.action == AuditAction.SYNC_RUN)
            .order_by(AuditLog.id.desc())
            .limit(1)
        ).scalar_one()
    assert '"superseded": 5' in (entry.detail or "")


def test_names_hidden_behind_a_date_failure_are_surfaced(admin_client, demo):
    """The date check short circuits before the roster is consulted, so a sheet wide
    date failure hides a second wave of unknown therapists. The run page names them
    now, so the roster can be built while the dates are being fixed."""
    from app.models.data_source import RejectReason, SyncMode, SyncStatus

    with admin_client.app.state.db.session() as db:
        run = SyncRun(
            source_id=demo,
            mode=SyncMode.LIVE,
            status=SyncStatus.SUCCESS,
            rows_read=6,
            rows_rejected=6,
        )
        db.add(run)
        db.flush()
        # WREN is already in the roster via the demo aliases; YEO and SOLAZZO are not.
        for i, hint in enumerate(["YEO", "YEO", "YEO", "SOLAZZO", "SOLAZZO", "WREN"]):
            db.add(
                ImportErrorRow(
                    sync_run_id=run.id,
                    source_id=demo,
                    reason=RejectReason.MISSING_DOS,
                    field="dos",
                    source_row_ref=str(i + 2),
                    therapist_hint=hint,
                )
            )
        run_id = run.id

    page = admin_client.get(f"/admin/sources/{demo}/runs/{run_id}").text
    assert "Names waiting behind these rejections" in page
    assert "prefill=YEO" in page
    assert "prefill=SOLAZZO" in page
    # A name the roster already resolves needs no creating.
    assert "prefill=WREN" not in page


def test_a_roster_wide_failure_gets_roster_advice_not_sheet_advice(admin_client, demo):
    """Sixty identical unknown therapist rejections are a roster problem. Telling the
    admin to check the sheet would send them to the wrong place."""
    from app.models.data_source import RejectReason, SyncMode, SyncStatus

    with admin_client.app.state.db.session() as db:
        run = SyncRun(
            source_id=demo,
            mode=SyncMode.LIVE,
            status=SyncStatus.SUCCESS,
            rows_read=62,
            rows_rejected=60,
        )
        db.add(run)
        db.flush()
        for i in range(60):
            db.add(
                ImportErrorRow(
                    sync_run_id=run.id,
                    source_id=demo,
                    reason=RejectReason.UNKNOWN_THERAPIST,
                    field="therapist",
                    raw_value="YEO",
                    source_row_ref=str(i + 2),
                )
            )
        run_id = run.id

    page = admin_client.get(f"/admin/sources/{demo}/runs/{run_id}").text
    assert "rejected for the same reason" in page
    assert "means the roster, not the rows" in page
    assert "property of the sheet" not in page
