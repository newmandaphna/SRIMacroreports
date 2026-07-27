"""Administrator user management, password policy, and admin seeding."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.enums import AuditAction, Module, Role
from app.models.user import User
from app.security.passwords import (
    PasswordPolicyError,
    hash_password,
    validate_password,
    verify_password,
)
from tests.conftest import (
    KNOWN_PASSWORD,
    SEED_ADMIN_EMAIL,
    SEED_ADMIN_PASSWORD,
    make_user,
    sign_in,
)


def token_from(response_text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response_text)
    assert match, "no CSRF token on page"
    return match.group(1)


@pytest.fixture
def admin_client(client):
    with client.app.state.db.session() as session:
        user = make_user(session, email="boss.aa@example.invalid", role=Role.ADMIN)
        email = user.email
    sign_in(client, email)
    return client


# --------------------------------------------------------------------------- users


def test_admin_can_create_a_user_and_sees_the_password_once(admin_client):
    form = admin_client.get("/admin/users/new")
    token = token_from(form.text)

    response = admin_client.post(
        "/admin/users/new",
        data={
            "csrf_token": token,
            "email": "New.Person@Example.Invalid",
            "display_name": "New Person",
            "role": Role.VIEWER.value,
            "modules": [Module.FINANCIAL.value],
        },
    )
    assert response.status_code == 201
    assert "Temporary password" in response.text

    with admin_client.app.state.db.session() as session:
        user = session.execute(
            select(User).where(User.email == "new.person@example.invalid")
        ).scalar_one()
        assert user.must_change_password is True
        assert user.granted_modules == {Module.FINANCIAL}
        # Stored as a hash, never in readable form.
        assert not user.password_hash.startswith("$2")
        assert user.password_hash.startswith("$argon2")


def test_created_user_can_sign_in_with_the_temporary_password(admin_client):
    token = token_from(admin_client.get("/admin/users/new").text)
    response = admin_client.post(
        "/admin/users/new",
        data={
            "csrf_token": token,
            "email": "temp.aa@example.invalid",
            "display_name": "Temp Person",
            "role": Role.VIEWER.value,
        },
    )
    temp = re.search(r'class="temp-password">([^<]+)<', response.text).group(1).strip()

    from fastapi.testclient import TestClient

    other = TestClient(admin_client.app)
    login = other.post(
        "/login",
        data={"email": "temp.aa@example.invalid", "password": temp},
        follow_redirects=False,
    )
    assert login.status_code == 303
    # Straight to the forced change, not to the app.
    assert login.headers["location"] == "/change-password"


def test_duplicate_email_is_refused(admin_client):
    token = token_from(admin_client.get("/admin/users/new").text)
    payload = {
        "csrf_token": token,
        "email": "dupe.aa@example.invalid",
        "display_name": "Dupe",
        "role": Role.VIEWER.value,
    }
    assert admin_client.post("/admin/users/new", data=payload).status_code == 201
    second = admin_client.post("/admin/users/new", data=payload)
    assert second.status_code == 400
    assert "already exists" in second.text


def test_grant_changes_are_audited_individually(admin_client):
    with admin_client.app.state.db.session() as session:
        target = make_user(session, email="grantee.aa@example.invalid", modules=(Module.FINANCIAL,))
        target_id = target.id

    token = token_from(admin_client.get(f"/admin/users/{target_id}").text)
    admin_client.post(
        f"/admin/users/{target_id}",
        data={
            "csrf_token": token,
            "display_name": "Grantee",
            "role": Role.MANAGER.value,
            "modules": [Module.THERAPIST_UTILIZATION.value],
        },
    )

    with admin_client.app.state.db.session() as session:
        actions = session.execute(
            select(AuditLog.action, AuditLog.detail).where(AuditLog.target_id == str(target_id))
        ).all()

    kinds = {a for a, _ in actions}
    assert AuditAction.ROLE_CHANGED in kinds
    assert AuditAction.GRANT_ADDED in kinds
    assert AuditAction.GRANT_REMOVED in kinds

    removed = next(d for a, d in actions if a == AuditAction.GRANT_REMOVED)
    assert Module.FINANCIAL.value in removed


def test_the_last_admin_cannot_be_demoted(admin_client):
    with admin_client.app.state.db.session() as session:
        admin = session.execute(select(User).where(User.role == Role.ADMIN)).scalar_one()
        admin_id = admin.id

    token = token_from(admin_client.get(f"/admin/users/{admin_id}").text)
    response = admin_client.post(
        f"/admin/users/{admin_id}",
        data={
            "csrf_token": token,
            "display_name": "Boss",
            "role": Role.VIEWER.value,
        },
    )
    assert response.status_code == 400
    assert "only active administrator" in response.text

    with admin_client.app.state.db.session() as session:
        assert session.get(User, admin_id).role_enum is Role.ADMIN


def test_admin_cannot_deactivate_themselves(admin_client):
    with admin_client.app.state.db.session() as session:
        admin_id = session.execute(select(User).where(User.role == Role.ADMIN)).scalar_one().id

    token = token_from(admin_client.get("/admin/users").text)
    response = admin_client.post(f"/admin/users/{admin_id}/deactivate", data={"csrf_token": token})
    assert response.status_code == 400
    with admin_client.app.state.db.session() as session:
        assert session.get(User, admin_id).is_active is True


def test_users_are_deactivated_never_deleted(admin_client):
    with admin_client.app.state.db.session() as session:
        target_id = make_user(session, email="bye.aa@example.invalid").id

    token = token_from(admin_client.get("/admin/users").text)
    admin_client.post(f"/admin/users/{target_id}/deactivate", data={"csrf_token": token})

    with admin_client.app.state.db.session() as session:
        user = session.get(User, target_id)
        assert user is not None, "the row must survive so audit entries still resolve"
        assert user.is_active is False


def test_password_reset_forces_a_change_and_kills_sessions(admin_client):
    from fastapi.testclient import TestClient

    with admin_client.app.state.db.session() as session:
        target = make_user(session, email="reset.aa@example.invalid")
        target_id, target_email = target.id, target.email

    victim = TestClient(admin_client.app)
    sign_in(victim, target_email)
    assert victim.get("/").status_code == 200

    token = token_from(admin_client.get("/admin/users").text)
    response = admin_client.post(
        f"/admin/users/{target_id}/reset-password", data={"csrf_token": token}
    )
    assert response.status_code == 200
    assert "Temporary password" in response.text

    assert victim.get("/", follow_redirects=False).status_code == 303
    with admin_client.app.state.db.session() as session:
        assert session.get(User, target_id).must_change_password is True


def test_reset_clears_a_lockout(admin_client):
    from fastapi.testclient import TestClient

    from app.routers.auth import MAX_FAILED_LOGINS

    with admin_client.app.state.db.session() as session:
        target = make_user(session, email="locked.aa@example.invalid")
        target_id, target_email = target.id, target.email

    stranger = TestClient(admin_client.app)
    for _ in range(MAX_FAILED_LOGINS):
        stranger.post("/login", data={"email": target_email, "password": "nope-nope-nope"})

    with admin_client.app.state.db.session() as session:
        assert session.get(User, target_id).is_locked

    token = token_from(admin_client.get("/admin/users").text)
    admin_client.post(f"/admin/users/{target_id}/reset-password", data={"csrf_token": token})

    with admin_client.app.state.db.session() as session:
        user = session.get(User, target_id)
        assert not user.is_locked
        assert user.failed_login_count == 0


# ------------------------------------------------------------------ password policy


@pytest.mark.parametrize(
    "bad",
    [
        "short",
        "elevenchars",
        "password1234",
        "Password123!",
        "aaaaaaaaaaaaaaa",
        "abcabcabcabcabcabc",
        "  leading-space-pad  ",
    ],
)
def test_weak_passwords_are_refused(bad):
    with pytest.raises(PasswordPolicyError):
        validate_password(bad)


@pytest.mark.parametrize(
    "good",
    [
        "quiet-lantern-thicket-9412",
        "sixteen chars plus more",
        "Zt7#vqLm2!spQr8w",
    ],
)
def test_reasonable_passwords_are_accepted(good):
    validate_password(good)


def test_password_may_not_contain_the_account_identifiers():
    with pytest.raises(PasswordPolicyError, match="email address or your name"):
        validate_password(
            "kmalone-something-long", email="kmalone@example.invalid", display_name="K Malone"
        )
    with pytest.raises(PasswordPolicyError, match="email address or your name"):
        validate_password(
            "reallylongmalonepassword", email="x@example.invalid", display_name="Kim Malone"
        )


def test_practice_specific_terms_are_refused():
    """Staff reach for their own vocabulary, and none of it is unguessable."""
    for term in ("sripsychological", "jenkintown2026", "telehealth123"):
        with pytest.raises(PasswordPolicyError):
            validate_password(term)


def test_hashes_are_argon2_and_verify():
    h = hash_password(KNOWN_PASSWORD)
    assert h.startswith("$argon2id$")
    assert verify_password(KNOWN_PASSWORD, h)
    assert not verify_password("something else entirely", h)


def test_verify_is_safe_against_a_malformed_hash():
    assert verify_password("anything", "not-a-hash") is False


# ------------------------------------------------------------------------- seeding


def test_admin_is_seeded_from_the_environment(seeded_client):
    with seeded_client.app.state.db.session() as session:
        admin = session.execute(select(User).where(User.role == Role.ADMIN)).scalar_one()
    assert admin.email == SEED_ADMIN_EMAIL
    assert admin.must_change_password is True
    # Seeded admin gets explicit grants, so routine work is not logged as emergency.
    assert admin.granted_modules == set(Module)


def test_seeded_admin_must_change_password_before_anything_else(seeded_client):
    response = seeded_client.post(
        "/login",
        data={"email": SEED_ADMIN_EMAIL, "password": SEED_ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/change-password"
    assert seeded_client.get("/admin/users", follow_redirects=False).status_code == 303


def test_seeding_is_idempotent(seeded_env):
    """A restart must not create a second admin or reset the first one's password."""
    from fastapi.testclient import TestClient

    from app.config import load_settings
    from app.main import create_app

    settings = load_settings()
    with TestClient(create_app(settings)) as first:
        with first.app.state.db.session() as session:
            original = session.execute(select(User).where(User.role == Role.ADMIN)).scalar_one()
            original_hash = original.password_hash

    with TestClient(create_app(settings)) as second:
        with second.app.state.db.session() as session:
            admins = session.execute(select(User).where(User.role == Role.ADMIN)).scalars().all()
            assert len(admins) == 1
            assert admins[0].password_hash == original_hash


def test_weak_seed_password_fails_startup(env, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import ConfigError, load_settings
    from app.main import create_app

    monkeypatch.setenv("ADMIN_EMAIL", "admin.zz@example.invalid")
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", "password123")

    with pytest.raises(ConfigError, match="password policy"):
        with TestClient(create_app(load_settings())):
            pass
