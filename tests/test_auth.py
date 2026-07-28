"""Authentication, the idle timeout, and CSRF."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.enums import AuditAction, AuditResult, Module, Role
from app.models.session import UserSession
from app.models.user import User
from app.routers.auth import MAX_FAILED_LOGINS
from tests.conftest import KNOWN_PASSWORD, csrf_for, make_user, sign_in


@pytest.fixture
def viewer(client):
    with client.app.state.db.session() as session:
        user = make_user(session, email="viewer.aa@example.invalid", modules=(Module.FINANCIAL,))
        return user.id, user.email


def test_root_redirects_to_login_when_anonymous(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_page_renders(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "Sign in" in response.text


def test_successful_login_sets_cookie_and_reaches_home(client, viewer):
    _, email = viewer
    response = sign_in(client, email)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "sri_session" in response.cookies or client.cookies.get("sri_session")

    home = client.get("/")
    assert home.status_code == 200
    assert "Overview" in home.text


def test_session_cookie_flags(client, viewer):
    _, email = viewer
    sign_in(client, email)
    raw = client.cookies.jar
    cookie = next(c for c in raw if c.name == "sri_session")
    assert cookie.has_nonstandard_attr("HttpOnly")
    # SameSite=Strict is set; in test the environment is not production so Secure is
    # off, which is what lets the test client speak plain HTTP.
    assert "strict" in str(cookie._rest).lower()


def test_wrong_password_is_rejected_with_generic_message(client, viewer):
    _, email = viewer
    response = client.post("/login", data={"email": email, "password": "wrong-password-here"})
    assert response.status_code == 401
    assert "Email or password is incorrect" in response.text


def test_unknown_user_gets_the_same_message(client):
    response = client.post(
        "/login",
        data={"email": "nobody.zz@example.invalid", "password": "wrong-password-here"},
    )
    assert response.status_code == 401
    # Identical wording, so the form cannot enumerate who has an account.
    assert "Email or password is incorrect" in response.text


def test_account_locks_after_repeated_failures(client, viewer):
    user_id, email = viewer
    for _ in range(MAX_FAILED_LOGINS):
        client.post("/login", data={"email": email, "password": "wrong-password-here"})

    with client.app.state.db.session() as session:
        user = session.get(User, user_id)
        assert user.is_locked

    # Even the correct password is refused while locked.
    response = client.post("/login", data={"email": email, "password": KNOWN_PASSWORD})
    assert response.status_code == 401
    assert "locked" in response.text.lower()


def test_successful_login_clears_the_failure_counter(client, viewer):
    user_id, email = viewer
    client.post("/login", data={"email": email, "password": "wrong-password-here"})
    sign_in(client, email)
    with client.app.state.db.session() as session:
        assert session.get(User, user_id).failed_login_count == 0


def test_inactive_user_cannot_sign_in(client):
    with client.app.state.db.session() as session:
        user = make_user(session, email="gone.aa@example.invalid", is_active=False)
        email = user.email
    response = sign_in(client, email)
    assert response.status_code == 401


def test_logout_revokes_the_session(client, viewer):
    _, email = viewer
    sign_in(client, email)
    token = csrf_for(client)

    response = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303

    with client.app.state.db.session() as session:
        live = (
            session.execute(select(UserSession).where(UserSession.revoked_at.is_(None)))
            .scalars()
            .all()
        )
        assert live == []

    assert client.get("/", follow_redirects=False).status_code == 303


def test_idle_session_expires_server_side(client, viewer):
    """The server enforces the timeout; the client timer is only a courtesy."""
    _, email = viewer
    sign_in(client, email)
    assert client.get("/").status_code == 200

    timeout = client.app.state.settings.session_timeout_minutes
    with client.app.state.db.session() as session:
        user_session = session.execute(
            select(UserSession).where(UserSession.revoked_at.is_(None))
        ).scalar_one()
        user_session.last_seen_at = datetime.now(UTC) - timedelta(minutes=timeout + 1)

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_activity_resets_the_idle_clock(client, viewer):
    _, email = viewer
    sign_in(client, email)

    with client.app.state.db.session() as session:
        before = session.execute(
            select(UserSession).where(UserSession.revoked_at.is_(None))
        ).scalar_one()
        before.last_seen_at = datetime.now(UTC) - timedelta(minutes=5)

    client.get("/")

    with client.app.state.db.session() as session:
        after = session.execute(
            select(UserSession).where(UserSession.revoked_at.is_(None))
        ).scalar_one()
        assert (datetime.now(UTC) - after.last_seen_at).total_seconds() < 5


def test_session_status_does_not_extend_the_session(client, viewer):
    """Polling for the countdown must not be what keeps a session alive."""
    _, email = viewer
    sign_in(client, email)

    stale = datetime.now(UTC) - timedelta(minutes=10)
    with client.app.state.db.session() as session:
        session.execute(
            select(UserSession).where(UserSession.revoked_at.is_(None))
        ).scalar_one().last_seen_at = stale

    response = client.get("/session/status")
    assert response.status_code == 200
    assert response.json()["authenticated"] is True

    with client.app.state.db.session() as session:
        current = session.execute(
            select(UserSession).where(UserSession.revoked_at.is_(None))
        ).scalar_one()
        assert abs((current.last_seen_at - stale).total_seconds()) < 2


def test_session_extend_does_extend(client, viewer):
    _, email = viewer
    sign_in(client, email)
    token = csrf_for(client)

    with client.app.state.db.session() as session:
        session.execute(
            select(UserSession).where(UserSession.revoked_at.is_(None))
        ).scalar_one().last_seen_at = datetime.now(UTC) - timedelta(minutes=10)

    response = client.post("/session/extend", headers={"X-CSRF-Token": token})
    assert response.status_code == 200
    assert response.json()["extended"] is True

    with client.app.state.db.session() as session:
        current = session.execute(
            select(UserSession).where(UserSession.revoked_at.is_(None))
        ).scalar_one()
        assert (datetime.now(UTC) - current.last_seen_at).total_seconds() < 5


def test_post_without_csrf_token_is_rejected(client, viewer):
    _, email = viewer
    sign_in(client, email)

    response = client.post("/logout", data={})
    assert response.status_code == 403
    assert "could not be verified" in response.text


def test_post_with_wrong_csrf_token_is_rejected(client, viewer):
    _, email = viewer
    sign_in(client, email)

    response = client.post("/logout", data={"csrf_token": "not-the-right-token"})
    assert response.status_code == 403


def test_open_redirect_is_refused(client, viewer):
    _, email = viewer
    response = client.post(
        "/login",
        data={"email": email, "password": KNOWN_PASSWORD, "next": "https://evil.invalid"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/"


def test_next_parameter_is_honoured_when_relative(client):
    with client.app.state.db.session() as session:
        admin = make_user(session, email="admin.bb@example.invalid", role=Role.ADMIN)
        email = admin.email

    response = client.post(
        "/login",
        data={"email": email, "password": KNOWN_PASSWORD, "next": "/admin/users"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/admin/users"


def test_authentication_events_are_audited(client, viewer):
    _, email = viewer
    client.post("/login", data={"email": email, "password": "wrong-password-here"})
    sign_in(client, email)

    from app.models.audit import AuditLog

    with client.app.state.db.session() as session:
        actions = session.execute(
            select(AuditLog.action, AuditLog.result).order_by(AuditLog.id)
        ).all()

    assert (AuditAction.LOGIN_FAILURE, AuditResult.FAILURE) in actions
    assert (AuditAction.LOGIN_SUCCESS, AuditResult.SUCCESS) in actions


# ------------------------------------------------- the startup password sync guard


def test_a_chosen_password_survives_a_restart(seeded_env, monkeypatch):
    """Every redeploy used to revert the admin's chosen password to the bootstrap
    secret, which broke their login and kept the bootstrap credential valid forever.
    The sync may only touch an account still on its bootstrap password."""
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.config import load_settings
    from app.main import create_app
    from app.models.user import User
    from app.security.passwords import verify_password
    from tests.conftest import SEED_ADMIN_EMAIL, SEED_ADMIN_PASSWORD, _reset_schema

    chosen = "a-password-the-admin-chose-9944"  # noqa: S105

    s = load_settings()
    _reset_schema(s)
    with TestClient(create_app(s)) as client:
        client.post(
            "/login",
            data={"email": SEED_ADMIN_EMAIL, "password": SEED_ADMIN_PASSWORD},
            follow_redirects=False,
        )
        page = client.get("/change-password")
        import re

        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        client.post(
            "/change-password",
            data={
                "csrf_token": token,
                "current_password": SEED_ADMIN_PASSWORD,
                "new_password": chosen,
                "confirm_password": chosen,
            },
        )

    # The restart: same environment, ADMIN_INITIAL_PASSWORD still set to the old value.
    with TestClient(create_app(load_settings())) as client:
        with client.app.state.db.session() as db:
            admin = db.execute(select(User).where(User.email == SEED_ADMIN_EMAIL)).scalar_one()
            assert verify_password(chosen, admin.password_hash), (
                "the admin's chosen password was overwritten at startup"
            )
            assert admin.must_change_password is False
