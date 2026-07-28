"""The one-click practice roster seed.

The importer still never creates a therapist. This button exists so that 41 names the
practice's own exports already spell out do not have to be typed in one at a time, and
everything it creates is an ordinary record an admin can edit, reclassify, or delete.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import func, select

from app.models.audit import AuditLog
from app.models.enums import AuditAction, Role
from app.models.therapist import EmploymentType, Therapist, TherapistAlias
from app.practice_roster import PRACTICE_ROSTER
from tests.conftest import make_user, sign_in


def token_from(text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', text)
    assert match, "no CSRF token on page"
    return match.group(1)


@pytest.fixture
def admin(client):
    with client.app.state.db.session() as db:
        email = make_user(db, email="roster.admin@example.invalid", role=Role.ADMIN).email
    sign_in(client, email)
    return client


def seed(client):
    page = client.get("/admin/therapists")
    return client.post(
        "/admin/therapists/seed-roster",
        data={"csrf_token": token_from(page.text)},
        follow_redirects=False,
    )


def test_the_roster_is_plausible():
    """41 distinct people, 41 distinct sheet surnames, nobody blank."""
    assert len(PRACTICE_ROSTER) == 41
    assert len({a for a, _, _ in PRACTICE_ROSTER}) == 41
    assert len({d for _, d, _ in PRACTICE_ROSTER}) == 41
    assert all(a and d for a, d, _ in PRACTICE_ROSTER)


def test_one_click_creates_the_whole_roster(admin):
    seed(admin)

    with admin.app.state.db.session() as db:
        count = db.execute(select(func.count(Therapist.id))).scalar_one()
        aliases = set(db.execute(select(TherapistAlias.alias)).scalars().all())
    assert count == 41
    assert {a for a, _, _ in PRACTICE_ROSTER} <= aliases


def test_pavlova_and_rosenfeld_are_two_records(admin):
    """Confirmed by the practice and by the exports: two different people."""
    seed(admin)
    with admin.app.state.db.session() as db:
        names = set(db.execute(select(Therapist.display_name)).scalars().all())
    assert "Inna Pavlova-Rosenfeld" in names
    assert "Anita Rosenfeld" in names


def test_seeded_therapists_are_unclassified_on_purpose(admin):
    """The exports do not know who is salaried, so nobody is put on the utilization
    board by this button. The In development page keeps flagging them instead."""
    seed(admin)
    with admin.app.state.db.session() as db:
        types = set(db.execute(select(Therapist.employment_type)).scalars().all())
    assert types == {EmploymentType.OTHER}

    assert "not measured against any threshold" in admin.get("/status").text


def test_seeding_twice_changes_nothing(admin):
    seed(admin)
    response = seed(admin)
    assert response.status_code == 303

    with admin.app.state.db.session() as db:
        assert db.execute(select(func.count(Therapist.id))).scalar_one() == 41
        assert db.execute(select(func.count(TherapistAlias.id))).scalar_one() == 41


def test_an_edited_record_wins_over_the_list(admin):
    """The list is weaker than the records it creates: a rename an admin made is
    never reverted, and the alias stays attached to the renamed person."""
    seed(admin)
    with admin.app.state.db.session() as db:
        yeo = db.execute(
            select(Therapist).where(Therapist.display_name == "Hyung Yeo")
        ).scalar_one()
        yeo.display_name = "Hyung Yeo, MD"
        yeo_id = yeo.id

    seed(admin)
    with admin.app.state.db.session() as db:
        assert db.execute(select(func.count(Therapist.id))).scalar_one() == 41
        alias = db.execute(select(TherapistAlias).where(TherapistAlias.alias == "YEO")).scalar_one()
        assert alias.therapist_id == yeo_id


def test_a_hand_created_therapist_gets_the_alias_attached(admin):
    """The person exists under their full name; only the sheet surname was missing,
    which is exactly what stops the importer resolving them."""
    with admin.app.state.db.session() as db:
        db.add(Therapist(display_name="Violet Benner", employment_type=EmploymentType.OTHER))

    seed(admin)
    with admin.app.state.db.session() as db:
        assert db.execute(select(func.count(Therapist.id))).scalar_one() == 41
        alias = db.execute(
            select(TherapistAlias).where(TherapistAlias.alias == "BENNER")
        ).scalar_one()
        owner = db.get(Therapist, alias.therapist_id)
    assert owner.display_name == "Violet Benner"


def test_the_button_appears_only_while_names_are_missing(admin):
    assert "Create the practice roster" in admin.get("/admin/therapists").text
    seed(admin)
    assert "Create the practice roster" not in admin.get("/admin/therapists").text


def test_seeding_is_audited_with_its_counts(admin):
    seed(admin)
    with admin.app.state.db.session() as db:
        entry = db.execute(
            select(AuditLog)
            .where(AuditLog.action == AuditAction.MANUAL_EDIT)
            .order_by(AuditLog.id.desc())
            .limit(1)
        ).scalar_one()
    assert '"created": 41' in (entry.detail or "")


def test_only_an_admin_can_seed(client):
    with client.app.state.db.session() as db:
        email = make_user(db, email="viewer.roster@example.invalid").email
    sign_in(client, email)
    response = client.post(
        "/admin/therapists/seed-roster", data={"csrf_token": "x"}, follow_redirects=False
    )
    assert response.status_code in (303, 403)
    with client.app.state.db.session() as db:
        assert db.execute(select(func.count(Therapist.id))).scalar_one() == 0


def test_the_importer_resolves_every_sheet_surname_after_seeding(admin):
    """The point of the whole exercise: each alias resolves through the same path the
    sync engine uses, so the real sheet's names stop rejecting."""
    from app.sync.engine import AliasResolver

    seed(admin)
    with admin.app.state.db.session() as db:
        resolver = AliasResolver(db)
        unresolved = [a for a, _, _ in PRACTICE_ROSTER if resolver.resolve(a) is None]
    assert unresolved == []
