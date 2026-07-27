"""Shared test fixtures.

Every credential here is obviously fake. Tests run against a throwaway encrypted
database in a temp directory, never against a real one.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, load_settings
from app.models.enums import Module, Role
from app.models.user import ModuleGrant, User
from app.security.passwords import hash_password

FAKE_SESSION_SECRET = "test-session-secret-value-not-real-000000000000"  # noqa: S105
FAKE_DB_KEY = "test-database-key-not-real"  # noqa: S105

# Obviously fake, and long enough to pass policy.
SEED_ADMIN_EMAIL = "admin.aa@example.invalid"
SEED_ADMIN_PASSWORD = "correct-horse-battery-staple-aa"  # noqa: S105
KNOWN_PASSWORD = "quiet-lantern-thicket-9412"  # noqa: S105


@pytest.fixture
def env(tmp_path, monkeypatch) -> Iterator[dict[str, str]]:
    """A minimal valid environment, applied to os.environ for the test."""
    values = {
        "ENVIRONMENT": "test",
        "DEBUG": "false",
        "SESSION_SECRET_KEY": FAKE_SESSION_SECRET,
        "DATABASE_ENCRYPTION_KEY": FAKE_DB_KEY,
        "DATABASE_PATH": str(tmp_path / "test.db"),
    }
    for key in list(os.environ):
        if key.startswith(
            ("ADMIN_", "APP_", "BENEFITS_", "CPT_", "FEATURE_", "GOOGLE_", "SESSION_", "WEEK_")
        ):
            monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    yield values


@pytest.fixture
def seeded_env(env, monkeypatch) -> dict[str, str]:
    """Environment with the seed admin credentials present."""
    monkeypatch.setenv("ADMIN_EMAIL", SEED_ADMIN_EMAIL)
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", SEED_ADMIN_PASSWORD)
    return env


@pytest.fixture
def settings(env) -> Settings:
    return load_settings()


@pytest.fixture
def client(settings) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def seeded_client(seeded_env) -> Iterator[TestClient]:
    """A client whose app seeded the initial administrator at startup."""
    from app.main import create_app

    with TestClient(create_app(load_settings())) as test_client:
        yield test_client


@pytest.fixture
def db(client):
    """A session against the running app's database."""
    with client.app.state.db.session() as session:
        yield session


def make_user(
    session,
    *,
    email: str,
    role: Role = Role.VIEWER,
    modules: tuple[Module, ...] = (),
    password: str = KNOWN_PASSWORD,
    must_change_password: bool = False,
    is_active: bool = True,
) -> User:
    """Create a user directly, bypassing the admin UI. Test helper only."""
    user = User(
        email=email,
        display_name=email.split("@")[0].replace(".", " ").title(),
        role=role,
        password_hash=hash_password(password),
        must_change_password=must_change_password,
        is_active=is_active,
    )
    session.add(user)
    session.flush()
    for module in modules:
        session.add(ModuleGrant(user_id=user.id, module=module))
    session.flush()
    return user


def sign_in(test_client: TestClient, email: str, password: str = KNOWN_PASSWORD):
    """Sign in and leave the session cookie on the client."""
    return test_client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )


def csrf_for(test_client: TestClient) -> str:
    """Read the signed in user's CSRF token out of a rendered page."""
    import re

    page = test_client.get("/")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match, "no CSRF token found on the page"
    return match.group(1)
