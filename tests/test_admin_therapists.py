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


def test_bulk_edit_get_renders_all_therapists(admin_client):
    create(admin_client, "Alexander", employment="other")
    create(admin_client, "Harris", employment="salaried_benefits")

    page = admin_client.get("/admin/therapists/bulk")
    assert page.status_code == 200
    assert "Alexander" in page.text
    assert "Harris" in page.text


def test_bulk_edit_updates_names_and_types(admin_client):
    create(admin_client, "ALEXANDER", employment="other")
    create(admin_client, "HARRIS", employment="other")

    with admin_client.app.state.db.session() as db:
        therapists = db.execute(select(Therapist).order_by(Therapist.display_name)).scalars().all()
        ids = {t.display_name: t.id for t in therapists}

    page = admin_client.get("/admin/therapists/bulk")
    token = token_from(page.text)

    resp = admin_client.post(
        "/admin/therapists/bulk",
        data={
            "csrf_token": token,
            f"name_{ids['ALEXANDER']}": "Dr. Sarah Alexander",
            f"employment_{ids['ALEXANDER']}": "salaried_benefits",
            f"name_{ids['HARRIS']}": "Andrea Harris, LMFT",
            f"employment_{ids['HARRIS']}": "percentage_legacy",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with admin_client.app.state.db.session() as db:
        t1 = db.get(Therapist, ids["ALEXANDER"])
        t2 = db.get(Therapist, ids["HARRIS"])

    assert t1.display_name == "Dr. Sarah Alexander"
    assert t1.employment_type == EmploymentType.SALARIED_BENEFITS
    assert t2.display_name == "Andrea Harris, LMFT"
    assert t2.employment_type == EmploymentType.PERCENTAGE_LEGACY


def test_bulk_edit_preserves_existing_aliases(admin_client):
    """Changing display name must not remove pre-existing aliases."""
    create(admin_client, "HARRIS", aliases="ANDREA HARRIS, LMFT (A.HARRIS)", employment="other")

    with admin_client.app.state.db.session() as db:
        t = db.execute(select(Therapist).where(Therapist.display_name == "HARRIS")).scalar_one()
        tid = t.id
        original_aliases = set(t.alias_values)

    page = admin_client.get("/admin/therapists/bulk")
    resp = admin_client.post(
        "/admin/therapists/bulk",
        data={
            "csrf_token": token_from(page.text),
            f"name_{tid}": "Andrea Harris, LMFT",
            f"employment_{tid}": "salaried_benefits",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with admin_client.app.state.db.session() as db:
        t = db.get(Therapist, tid)
        assert t.display_name == "Andrea Harris, LMFT"
        # All original aliases must still be present.
        assert original_aliases.issubset(t.alias_values)


def test_bulk_edit_is_atomic_one_bad_row_prevents_all_changes(admin_client):
    """If any row fails validation, zero rows must be written to the database."""
    create(admin_client, "ALEXANDER", employment="other")
    create(admin_client, "HARRIS", employment="other")

    with admin_client.app.state.db.session() as db:
        therapists = db.execute(select(Therapist).order_by(Therapist.display_name)).scalars().all()
        ids = {t.display_name: t.id for t in therapists}

    page = admin_client.get("/admin/therapists/bulk")
    resp = admin_client.post(
        "/admin/therapists/bulk",
        data={
            "csrf_token": token_from(page.text),
            f"name_{ids['ALEXANDER']}": "Dr. Sarah Alexander",  # valid
            f"employment_{ids['ALEXANDER']}": "salaried_benefits",
            # HARRIS gets an invalid (empty) name — should abort the whole batch.
            f"name_{ids['HARRIS']}": "",
            f"employment_{ids['HARRIS']}": "percentage_legacy",
        },
    )
    # Should return 400 (validation error page).
    assert resp.status_code == 400

    # ALEXANDER must be unchanged — the valid row must NOT have been written.
    with admin_client.app.state.db.session() as db:
        t = db.get(Therapist, ids["ALEXANDER"])
    assert t.display_name == "ALEXANDER", (
        "Atomic rollback failed: the valid row was written even though another row was invalid"
    )


def test_bulk_edit_rejects_intra_batch_duplicate_names(admin_client):
    """Two rows in the same submission sharing a new name must fail with 400 and zero writes."""
    create(admin_client, "ALEXANDER", employment="other")
    create(admin_client, "HARRIS", employment="other")

    with admin_client.app.state.db.session() as db:
        therapists = db.execute(select(Therapist).order_by(Therapist.display_name)).scalars().all()
        ids = {t.display_name: t.id for t in therapists}

    page = admin_client.get("/admin/therapists/bulk")
    resp = admin_client.post(
        "/admin/therapists/bulk",
        data={
            "csrf_token": token_from(page.text),
            # Both rows renamed to the same value — should be caught in phase-1.
            f"name_{ids['ALEXANDER']}": "Dr. Same Name",
            f"employment_{ids['ALEXANDER']}": "salaried_benefits",
            f"name_{ids['HARRIS']}": "Dr. Same Name",
            f"employment_{ids['HARRIS']}": "percentage_legacy",
        },
    )
    assert resp.status_code == 400
    assert "more than once" in resp.text

    # Neither therapist must have been written.
    with admin_client.app.state.db.session() as db:
        t1 = db.get(Therapist, ids["ALEXANDER"])
        t2 = db.get(Therapist, ids["HARRIS"])
    assert t1.display_name == "ALEXANDER", "Intra-batch dup check: first row was mutated"
    assert t2.display_name == "HARRIS", "Intra-batch dup check: second row was mutated"


def test_bulk_edit_rejects_duplicate_display_name(admin_client):
    create(admin_client, "Alexander")
    create(admin_client, "Harris")

    with admin_client.app.state.db.session() as db:
        t = db.execute(select(Therapist).where(Therapist.display_name == "Harris")).scalar_one()
        tid = t.id

    page = admin_client.get("/admin/therapists/bulk")
    resp = admin_client.post(
        "/admin/therapists/bulk",
        data={
            "csrf_token": token_from(page.text),
            f"name_{tid}": "Alexander",  # already taken
            f"employment_{tid}": "salaried_benefits",
        },
    )
    assert resp.status_code == 400
    assert "already belongs to another therapist" in resp.text


def test_bulk_edit_is_audited(admin_client):
    create(admin_client, "HARRIS", employment="other")

    with admin_client.app.state.db.session() as db:
        t = db.execute(select(Therapist).where(Therapist.display_name == "HARRIS")).scalar_one()
        tid = t.id

    page = admin_client.get("/admin/therapists/bulk")
    admin_client.post(
        "/admin/therapists/bulk",
        data={
            "csrf_token": token_from(page.text),
            f"name_{tid}": "Andrea Harris",
            f"employment_{tid}": "salaried_benefits",
        },
        follow_redirects=False,
    )

    with admin_client.app.state.db.session() as db:
        entry = db.execute(
            select(AuditLog).where(
                AuditLog.action == AuditAction.MANUAL_EDIT,
                AuditLog.detail.contains("bulk_update"),
            )
        ).scalar_one_or_none()

    assert entry is not None
    assert "Andrea Harris" in (entry.detail or "")


# ---------------------------------------- moving an alias that already has history
#
# A visit's identity is (therapist_id, patient, date, CPT). An alias is how a sheet
# name becomes a therapist_id, so moving an alias that already attributed rows changes
# the identity of history without touching the rows, and the next sync of the very same
# sheet inserts a second copy of every one of them.

ALIAS_HEADER = "Therapist,Patient name,DOS,CPT,Total paid\n"
ALIAS_ROWS = "ZEBRA,Patient AA,2026-04-06,90837,150.00\nZEBRA,Patient AB,2026-04-07,90834,125.00\n"
ALIAS_MAPPING = {
    "therapist": "Therapist",
    "patient_name": "Patient name",
    "dos": "DOS",
    "cpt": "CPT",
    "total_paid": "Total paid",
}


def _upload_source(admin_client) -> int:
    from app.models.data_source import DataSource, SourceProvider
    from app.sync.upload import CSV_TAB_NAME

    with admin_client.app.state.db.session() as db:
        source = DataSource(
            label="Alias history",
            provider=SourceProvider.UPLOAD,
            tab_name=CSV_TAB_NAME,
            header_row=1,
            column_mapping=dict(ALIAS_MAPPING),
            active=True,
        )
        db.add(source)
        db.flush()
        return source.id


def _upload_rows(admin_client, source_id: int, body: str):
    token = token_from(admin_client.get(f"/admin/sources/{source_id}").text)
    return admin_client.post(
        f"/admin/sources/{source_id}/upload",
        data={"csrf_token": token, "mode": "live"},
        files={"file": ("rows.csv", body.encode(), "text/csv")},
        follow_redirects=False,
    )


def _visit_totals(admin_client) -> tuple[int, str]:
    from app.models.visit import Visit

    with admin_client.app.state.db.session() as db:
        count = db.execute(select(func.count(Visit.id))).scalar_one()
        paid = db.execute(select(func.coalesce(func.sum(Visit.total_paid), 0))).scalar_one()
    return count, str(paid)


def test_an_alias_carrying_imported_rows_cannot_be_removed(admin_client):
    """The two click double count, refused at the first click.

    Remove the alias from one therapist, add it to another, and every historical row
    is now attributed to somebody who has no rows matching it. Re-syncing the
    unchanged sheet then inserts a second copy of all of them: sessions and money
    double, and the duplicates are indistinguishable from real rows.
    """
    create(admin_client, "Zebra Alias", aliases="ZEBRA")
    source_id = _upload_source(admin_client)
    assert _upload_rows(admin_client, source_id, ALIAS_HEADER + ALIAS_ROWS).status_code == 303

    before = _visit_totals(admin_client)
    assert before[0] == 2, "the fixture rows must have imported for this test to mean anything"

    with admin_client.app.state.db.session() as db:
        alias = db.execute(
            select(TherapistAlias).where(TherapistAlias.alias == "ZEBRA")
        ).scalar_one()
        therapist_id, alias_id = alias.therapist_id, alias.id

    page = admin_client.get(f"/admin/therapists/{therapist_id}")
    response = admin_client.post(
        f"/admin/therapists/{therapist_id}/aliases/{alias_id}/remove",
        data={"csrf_token": token_from(page.text)},
    )

    assert response.status_code == 409
    assert "deactivate the therapist instead" in response.text

    with admin_client.app.state.db.session() as db:
        assert db.get(TherapistAlias, alias_id) is not None, "the alias must survive the refusal"

    # The invariant the guard exists for: re-syncing the same file changes nothing.
    assert _upload_rows(admin_client, source_id, ALIAS_HEADER + ALIAS_ROWS).status_code == 303
    assert _visit_totals(admin_client) == before


def test_an_alias_with_no_imported_rows_can_still_be_removed(admin_client):
    """The guard must not turn an ordinary typo fix into a permanent record."""
    create(admin_client, "Harris", aliases="A.HARRIS")
    with admin_client.app.state.db.session() as db:
        alias = db.execute(
            select(TherapistAlias).where(TherapistAlias.alias == "A.HARRIS")
        ).scalar_one()
        therapist_id, alias_id = alias.therapist_id, alias.id

    page = admin_client.get(f"/admin/therapists/{therapist_id}")
    response = admin_client.post(
        f"/admin/therapists/{therapist_id}/aliases/{alias_id}/remove",
        data={"csrf_token": token_from(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with admin_client.app.state.db.session() as db:
        assert db.get(TherapistAlias, alias_id) is None


def test_an_alias_that_shadows_another_therapists_display_name_is_refused(admin_client):
    """The same double count reached from the other direction.

    The resolver checks aliases before display names, so attaching a name that is
    already somebody's display name re-points that name's future imports without any
    alias row changing hands. Reachable in one step: a bulk rename leaves the old
    all-caps alias behind, so the new display name is not itself an alias and the
    existing alias-table check cannot see the collision.
    """
    create(admin_client, "ZEBRA", aliases="ZEBRA")
    create(admin_client, "Other Person", aliases="OTHER")

    source_id = _upload_source(admin_client)
    assert _upload_rows(admin_client, source_id, ALIAS_HEADER + ALIAS_ROWS).status_code == 303
    before = _visit_totals(admin_client)
    assert before[0] == 2

    with admin_client.app.state.db.session() as db:
        zebra_id = db.execute(
            select(Therapist.id).where(Therapist.display_name == "ZEBRA")
        ).scalar_one()
        other_id = db.execute(
            select(Therapist.id).where(Therapist.display_name == "Other Person")
        ).scalar_one()

    # Rename by the route an admin actually uses. The ZEBRA alias stays behind, so
    # "Zebra Person" is now a display name that no alias row mentions.
    page = admin_client.get("/admin/therapists/bulk")
    assert (
        admin_client.post(
            "/admin/therapists/bulk",
            data={
                "csrf_token": token_from(page.text),
                f"name_{zebra_id}": "Zebra Person",
                f"employment_{zebra_id}": "salaried_benefits",
                f"name_{other_id}": "Other Person",
                f"employment_{other_id}": "salaried_benefits",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )

    page = admin_client.get(f"/admin/therapists/{other_id}")
    response = admin_client.post(
        f"/admin/therapists/{other_id}/aliases",
        data={"csrf_token": token_from(page.text), "alias": "Zebra Person"},
    )
    assert response.status_code == 400
    assert "already resolves to a different therapist" in response.text

    with admin_client.app.state.db.session() as db:
        owner = db.execute(
            select(TherapistAlias).where(TherapistAlias.alias == "ZEBRA PERSON")
        ).scalar_one_or_none()
    assert owner is None, "the shadowing alias must not have been created"


def test_shadowing_a_display_name_with_no_rows_is_allowed(admin_client):
    """Nothing to protect means nothing to refuse: a therapist who never imported a
    row is exactly the placeholder an admin is trying to tidy up."""
    create(admin_client, "PLACEHOLDER", aliases="PLACEHOLDER")
    create(admin_client, "Real Person", aliases="REAL")

    with admin_client.app.state.db.session() as db:
        placeholder_id = db.execute(
            select(Therapist.id).where(Therapist.display_name == "PLACEHOLDER")
        ).scalar_one()
        real_id = db.execute(
            select(Therapist.id).where(Therapist.display_name == "Real Person")
        ).scalar_one()

    page = admin_client.get("/admin/therapists/bulk")
    admin_client.post(
        "/admin/therapists/bulk",
        data={
            "csrf_token": token_from(page.text),
            f"name_{placeholder_id}": "Tidy Name",
            f"employment_{placeholder_id}": "other",
            f"name_{real_id}": "Real Person",
            f"employment_{real_id}": "salaried_benefits",
        },
        follow_redirects=False,
    )

    page = admin_client.get(f"/admin/therapists/{real_id}")
    response = admin_client.post(
        f"/admin/therapists/{real_id}/aliases",
        data={"csrf_token": token_from(page.text), "alias": "Tidy Name"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with admin_client.app.state.db.session() as db:
        entry = db.execute(
            select(TherapistAlias).where(TherapistAlias.alias == "TIDY NAME")
        ).scalar_one()
    assert entry.therapist_id == real_id


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
