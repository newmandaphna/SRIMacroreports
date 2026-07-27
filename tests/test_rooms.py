"""Room utilization: the feature flag, the manual upload, and the heat grid."""

from __future__ import annotations

import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import load_settings
from app.models.audit import AuditLog
from app.models.enums import AuditAction, Module, Role
from app.models.room import Room, RoomUsage
from tests.conftest import make_user, sign_in

APRIL = "preset=custom&start=2026-04-01&end=2026-04-30&granularity=week"


def token_from(text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', text)
    assert match, "no CSRF token on page"
    return match.group(1)


def app_with_flag(env, monkeypatch, *, enabled: bool):
    from app.main import create_app

    monkeypatch.setenv("FEATURE_ROOM_UTILIZATION", "true" if enabled else "false")
    return create_app(load_settings())


@pytest.fixture
def rooms_off(env, monkeypatch):
    with TestClient(app_with_flag(env, monkeypatch, enabled=False)) as client:
        yield client


@pytest.fixture
def rooms_on(env, monkeypatch):
    with TestClient(app_with_flag(env, monkeypatch, enabled=True)) as client:
        yield client


def as_admin(client) -> None:
    with client.app.state.db.session() as db:
        email = make_user(
            db, email="rooms.admin@example.invalid", role=Role.ADMIN, modules=tuple(Module)
        ).email
    sign_in(client, email)


def add_room(client, name: str, location: str = "1", slots: int = 8):
    page = client.get("/admin/rooms")
    return client.post(
        "/admin/rooms/new",
        data={
            "csrf_token": token_from(page.text),
            "name": name,
            "location_short": location,
            "default_slots_per_day": str(slots),
        },
    )


def upload(client, text: str):
    page = client.get("/admin/rooms")
    return client.post(
        "/admin/rooms/upload",
        data={"csrf_token": token_from(page.text)},
        files={"file": ("usage.csv", text, "text/csv")},
    )


# ---------------------------------------------------------------------- the flag


def test_with_the_flag_off_every_route_is_a_404(rooms_off):
    as_admin(rooms_off)
    assert rooms_off.get("/reports/room-utilization").status_code == 404
    assert rooms_off.get("/reports/room-utilization/export.csv").status_code == 404
    assert rooms_off.get("/admin/rooms").status_code == 404


def test_the_flag_is_checked_before_authentication(rooms_off):
    """An anonymous prober gets a 404, not a login redirect that confirms the path."""
    response = rooms_off.get("/reports/room-utilization", follow_redirects=False)
    assert response.status_code == 404


def test_with_the_flag_off_the_module_is_absent_from_navigation(rooms_off):
    as_admin(rooms_off)
    assert ">Rooms</a>" not in rooms_off.get("/reports").text


def test_with_the_flag_on_the_module_appears(rooms_on):
    as_admin(rooms_on)
    assert rooms_on.get("/reports/room-utilization").status_code == 200
    assert rooms_on.get("/admin/rooms").status_code == 200
    assert ">Rooms</a>" in rooms_on.get("/reports").text


def test_the_module_still_needs_its_grant(rooms_on):
    with rooms_on.app.state.db.session() as db:
        email = make_user(db, email="fin.rooms@example.invalid", modules=(Module.FINANCIAL,)).email
    sign_in(rooms_on, email)
    assert rooms_on.get("/reports/room-utilization").status_code == 403


def test_only_an_admin_manages_rooms(rooms_on):
    with rooms_on.app.state.db.session() as db:
        email = make_user(
            db,
            email="mgr.rooms@example.invalid",
            role=Role.MANAGER,
            modules=(Module.ROOM_UTILIZATION,),
        ).email
    sign_in(rooms_on, email)
    assert rooms_on.get("/reports/room-utilization").status_code == 200
    assert rooms_on.get("/admin/rooms").status_code == 403


# ------------------------------------------------------------------- empty states


def test_empty_state_says_where_the_data_comes_from(rooms_on):
    as_admin(rooms_on)
    page = rooms_on.get("/reports/room-utilization").text
    assert "No rooms yet" in page
    assert "not in it" in page


def test_rooms_without_usage_get_their_own_state(rooms_on):
    as_admin(rooms_on)
    add_room(rooms_on, "Jenkintown 1")
    page = rooms_on.get("/reports/room-utilization").text
    assert "No usage recorded yet" in page


def test_the_report_says_the_numbers_are_uploaded_not_measured(rooms_on):
    as_admin(rooms_on)
    assert "uploaded, not measured" in rooms_on.get("/reports/room-utilization").text


# ------------------------------------------------------------------------- rooms


def test_a_room_can_be_added(rooms_on):
    as_admin(rooms_on)
    add_room(rooms_on, "Jenkintown 1", location="1", slots=8)

    with rooms_on.app.state.db.session() as db:
        room = db.execute(select(Room)).scalar_one()
    assert room.name == "Jenkintown 1"
    assert room.location_short == "1"
    assert room.default_slots_per_day == 8


def test_a_duplicate_room_is_refused(rooms_on):
    as_admin(rooms_on)
    add_room(rooms_on, "Jenkintown 1")
    response = add_room(rooms_on, "jenkintown 1")
    assert response.status_code == 400
    assert "already exists" in response.text


# ------------------------------------------------------------------------ upload


GOOD_CSV = """room,date,slots used,slots available,note
Jenkintown 1,2026-04-06,7,8,
Jenkintown 1,2026-04-07,5,8,two cancellations
Jenkintown 2,2026-04-06,8,8,
"""

MIXED_CSV = (
    GOOD_CSV
    + """Nowhere Room,2026-04-06,3,4,
Jenkintown 1,not-a-date,3,8,
Jenkintown 2,2026-04-08,99,8,
"""
)


@pytest.fixture
def stocked(rooms_on):
    as_admin(rooms_on)
    add_room(rooms_on, "Jenkintown 1", location="1", slots=8)
    add_room(rooms_on, "Jenkintown 2", location="1", slots=8)
    return rooms_on


def test_a_clean_upload_imports_every_row(stocked):
    upload(stocked, GOOD_CSV)
    with stocked.app.state.db.session() as db:
        rows = db.execute(select(RoomUsage)).scalars().all()
    assert len(rows) == 3


def test_each_failure_mode_is_rejected_with_its_reason(stocked):
    response = upload(stocked, MIXED_CSV)
    assert "No room called" in response.text
    assert "Could not read the date" in response.text
    assert "more than available" in response.text

    with stocked.app.state.db.session() as db:
        assert len(db.execute(select(RoomUsage)).scalars().all()) == 3


def test_re_uploading_replaces_rather_than_duplicates(stocked):
    upload(stocked, GOOD_CSV)
    response = upload(stocked, GOOD_CSV)

    flat = re.sub(r"\s+", " ", response.text)
    assert "0 added, 3 updated" in flat

    with stocked.app.state.db.session() as db:
        assert len(db.execute(select(RoomUsage)).scalars().all()) == 3


def test_slots_available_falls_back_to_the_room_default(stocked):
    upload(stocked, "room,date,slots used\nJenkintown 1,2026-04-06,6\n")
    with stocked.app.state.db.session() as db:
        entry = db.execute(select(RoomUsage)).scalar_one()
    assert entry.slots_available == 8
    assert entry.rate == 75


def test_loosely_named_columns_are_accepted(stocked):
    """The file is written by hand, so the header spelling should not matter."""
    upload(stocked, "Room Name,Day,Used,Capacity\nJenkintown 1,2026-04-06,4,8\n")
    with stocked.app.state.db.session() as db:
        entry = db.execute(select(RoomUsage)).scalar_one()
    assert entry.slots_used == 4


def test_a_file_missing_required_columns_is_refused(stocked):
    response = upload(stocked, "something,else\n1,2\n")
    assert "needs at least room, date, and slots used" in response.text


def test_a_non_utf8_file_is_refused_without_erroring(stocked):
    page = stocked.get("/admin/rooms")
    response = stocked.post(
        "/admin/rooms/upload",
        data={"csrf_token": token_from(page.text)},
        files={"file": ("usage.csv", b"\xff\xfe\x00binary", "text/csv")},
    )
    assert response.status_code == 400
    assert "not UTF-8" in response.text


def test_uploads_are_audited(stocked):
    upload(stocked, GOOD_CSV)
    with stocked.app.state.db.session() as db:
        entry = db.execute(
            select(AuditLog)
            .where(AuditLog.target_type == "room_usage")
            .order_by(AuditLog.id.desc())
            .limit(1)
        ).scalar_one()
    assert '"inserted": 3' in (entry.detail or "")


# --------------------------------------------------------------------- heat grid


def test_the_grid_aggregates_by_period(stocked):
    """Two days in the same week combine: 7 of 8 plus 5 of 8 is 12 of 16."""
    upload(stocked, GOOD_CSV)
    page = stocked.get(f"/reports/room-utilization?{APRIL}").text

    assert "75.0%" in page  # Jenkintown 1, week of 6 April
    assert "100.0%" in page  # Jenkintown 2, week of 6 April


def test_a_period_with_nothing_recorded_is_blank_not_zero(stocked):
    """A room nobody recorded is not the same as a room that sat empty."""
    upload(stocked, GOOD_CSV)
    page = stocked.get(f"/reports/room-utilization?{APRIL}").text
    assert "heat-blank" in page


def test_the_grid_uses_a_single_hue_ramp_not_the_status_colours(stocked):
    """A busy room is not good news and an empty one is not an alert."""
    upload(stocked, GOOD_CSV)
    page = stocked.get(f"/reports/room-utilization?{APRIL}").text
    assert "heat-cell--l" in page
    assert "pill--below" not in page


def test_daily_rows_survive_a_daily_reading(stocked):
    upload(stocked, GOOD_CSV)
    with stocked.app.state.db.session() as db:
        entry = db.execute(
            select(RoomUsage).where(RoomUsage.usage_date == date(2026, 4, 6)).limit(1)
        ).scalar_one()
    assert entry.rate is not None


def test_a_room_with_no_available_slots_reports_no_rate(stocked):
    """Closed is not zero percent utilized."""
    upload(stocked, "room,date,slots used,slots available\nJenkintown 1,2026-04-06,0,0\n")
    with stocked.app.state.db.session() as db:
        entry = db.execute(select(RoomUsage)).scalar_one()
    assert entry.rate is None


# ------------------------------------------------------------------------ export


def test_export_carries_provenance_and_is_audited(stocked):
    upload(stocked, GOOD_CSV)
    response = stocked.get(f"/reports/room-utilization/export.csv?{APRIL}")

    assert response.status_code == 200
    assert "SRI Practice Dashboard, room utilization" in response.text
    assert "2026-04-01 to 2026-04-30" in response.text

    with stocked.app.state.db.session() as db:
        entry = db.execute(
            select(AuditLog)
            .where(AuditLog.action == AuditAction.EXPORT)
            .order_by(AuditLog.id.desc())
            .limit(1)
        ).scalar_one()
    assert entry.target_id == "room_utilization"
