"""Therapist administration, which is what makes a real sync usable at all."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import func, select

from app.models.audit import AuditLog
from app.models.enums import AuditAction, Role
from app.models.therapist import EmploymentType, Therapist, TherapistAlias
from tests.conftest import make_user, sign_in


def token_from(text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', text)
    assert match, "no CSRF token on page"
    return match.group(1)


@pytest.fixture
def admin_client(client):
    with client.app.state.db.session() as db:
        email = make_user(db, email="th.admin@example.invalid", role=Role.ADMIN).email
    sign_in(client, email)
    return client


def create(admin_client, name: str, aliases: str = "", employment: str = "salaried_benefits"):
    page = admin_client.get("/admin/therapists")
    return admin_client.post(
        "/admin/therapists/new",
        data={
            "csrf_token": token_from(page.text),
            "display_name": name,
            "aliases": aliases,
            "employment_type": employment,
        },
        follow_redirects=False,
    )


def test_non_admin_is_refused(client):
    with client.app.state.db.session() as db:
        email = make_user(db, email="v.th@example.invalid", role=Role.VIEWER).email
    sign_in(client, email)
    assert client.get("/admin/therapists").status_code == 403


def test_empty_state_explains_the_consequence(admin_client):
    page = admin_client.get("/admin/therapists")
    assert "No therapists yet" in page.text
    assert "reject at import" in page.text


def test_creating_a_therapist_adds_the_name_as_an_alias(admin_client):
    """Otherwise a therapist could exist and still not resolve anything."""
    response = create(admin_client, "Harris")
    assert response.status_code == 303

    with admin_client.app.state.db.session() as db:
        therapist = db.execute(
            select(Therapist).where(Therapist.display_name == "Harris")
        ).scalar_one()
        assert "HARRIS" in therapist.alias_values


def test_aliases_are_normalized_on_the_way_in(admin_client):
    create(admin_client, "Harris", aliases="  andrea harris, LMFT (a.harris)  ")

    with admin_client.app.state.db.session() as db:
        therapist = db.execute(
            select(Therapist).where(Therapist.display_name == "Harris")
        ).scalar_one()
    assert "ANDREA HARRIS" in therapist.alias_values
    assert "LMFT (A.HARRIS)" in therapist.alias_values


def test_an_alias_belonging_to_someone_else_is_refused(admin_client):
    """The PAVLOVA and ROSENFELD guard, at the point of configuration."""
    create(admin_client, "Pavlova", aliases="INNA PAVLOVA-ROSENFELD")
    response = create(admin_client, "Rosenfeld", aliases="INNA PAVLOVA-ROSENFELD")

    assert response.status_code == 400
    assert "already resolves to another therapist" in response.text

    with admin_client.app.state.db.session() as db:
        assert (
            db.execute(
                select(func.count(Therapist.id)).where(Therapist.display_name == "Rosenfeld")
            ).scalar_one()
            == 0
        )


def test_two_different_therapists_with_distinct_aliases_both_exist(admin_client):
    """PAVLOVA and ROSENFELD are different people and must both be creatable."""
    create(admin_client, "Pavlova", aliases="INNA PAVLOVA-ROSENFELD, PAVLOVA")
    create(admin_client, "Rosenfeld", aliases="ROSENFELD")

    with admin_client.app.state.db.session() as db:
        names = set(db.execute(select(Therapist.display_name)).scalars().all())
    assert names == {"Pavlova", "Rosenfeld"}


def test_duplicate_name_is_refused(admin_client):
    create(admin_client, "Harris")
    response = create(admin_client, "harris")
    assert response.status_code == 400
    assert "already exists" in response.text


def test_adding_a_conflicting_alias_later_is_also_refused(admin_client):
    create(admin_client, "Pavlova", aliases="PAVLOVA")
    create(admin_client, "Rosenfeld", aliases="ROSENFELD")

    with admin_client.app.state.db.session() as db:
        rosenfeld_id = db.execute(
            select(Therapist.id).where(Therapist.display_name == "Rosenfeld")
        ).scalar_one()

    page = admin_client.get(f"/admin/therapists/{rosenfeld_id}")
    response = admin_client.post(
        f"/admin/therapists/{rosenfeld_id}/aliases",
        data={"csrf_token": token_from(page.text), "alias": "PAVLOVA"},
    )
    assert response.status_code == 400
    assert "already resolves to a different therapist" in response.text


def test_employment_type_can_be_changed(admin_client):
    create(admin_client, "Harris", employment="salaried_benefits")
    with admin_client.app.state.db.session() as db:
        therapist_id = db.execute(select(Therapist.id)).scalar_one()

    page = admin_client.get(f"/admin/therapists/{therapist_id}")
    admin_client.post(
        f"/admin/therapists/{therapist_id}",
        data={
            "csrf_token": token_from(page.text),
            "display_name": "Harris",
            "employment_type": "percentage_legacy",
            "notes": "Part time, two days a week.",
            "active": "1",
        },
    )

    with admin_client.app.state.db.session() as db:
        therapist = db.get(Therapist, therapist_id)
    assert therapist.employment_type is EmploymentType.PERCENTAGE_LEGACY
    assert therapist.notes == "Part time, two days a week."


def test_an_alias_can_be_removed(admin_client):
    create(admin_client, "Harris", aliases="A.HARRIS")
    with admin_client.app.state.db.session() as db:
        alias = db.execute(
            select(TherapistAlias).where(TherapistAlias.alias == "A.HARRIS")
        ).scalar_one()
        therapist_id, alias_id = alias.therapist_id, alias.id

    page = admin_client.get(f"/admin/therapists/{therapist_id}")
    admin_client.post(
        f"/admin/therapists/{therapist_id}/aliases/{alias_id}/remove",
        data={"csrf_token": token_from(page.text)},
    )

    with admin_client.app.state.db.session() as db:
        assert db.get(TherapistAlias, alias_id) is None


def test_changes_are_audited(admin_client):
    create(admin_client, "Harris", aliases="A.HARRIS")

    with admin_client.app.state.db.session() as db:
        entries = (
            db.execute(
                select(AuditLog).where(
                    AuditLog.action == AuditAction.MANUAL_EDIT,
                    AuditLog.target_type == "therapist",
                )
            )
            .scalars()
            .all()
        )
    assert entries
    assert "Harris" in (entries[0].detail or "")


def test_a_created_therapist_resolves_a_previously_rejected_row(admin_client):
    """The whole point: reject, add the therapist, sync again, row lands."""
    from app.models.data_source import DataSource, RejectReason, SourceProvider
    from app.sync.demo_data import DEMO_TAB_NAME, Q2_HEADERS
    from app.sync.engine import run_sync, suggest_mapping
    from app.sync.sheets import DemoSheetsClient

    headers = [str(h) if h is not None else "" for h in Q2_HEADERS]
    with admin_client.app.state.db.session() as db:
        source = DataSource(
            label="Demo",
            provider=SourceProvider.DEMO,
            tab_name=DEMO_TAB_NAME,
            header_row=1,
            column_mapping=suggest_mapping(headers),
        )
        db.add(source)
        db.flush()
        source_id = source.id

    # Nobody is known yet, so every row rejects as an unknown therapist.
    with admin_client.app.state.db.session() as db:
        first = run_sync(db, db.get(DataSource, source_id), DemoSheetsClient(), dry_run=True)
    unknown = [r for r in first.rejections if r.reason is RejectReason.UNKNOWN_THERAPIST]
    assert len(unknown) > 5

    create(admin_client, "Quincey", aliases="QUINCEY")

    with admin_client.app.state.db.session() as db:
        second = run_sync(db, db.get(DataSource, source_id), DemoSheetsClient(), dry_run=True)
    still_unknown = [r for r in second.rejections if r.reason is RejectReason.UNKNOWN_THERAPIST]
    assert len(still_unknown) < len(unknown)
