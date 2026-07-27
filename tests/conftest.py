"""Shared test fixtures.

Every credential here is obviously fake.

ISOLATION BOUNDARY AND SAFETY DESIGN
=====================================
Tests that create a TestClient call _reset_schema(), which drops every table
and re-runs Alembic migrations from scratch.  This is intentionally destructive.
The fixture uses TEST_DATABASE_URL as the designated test database, and TEST_DATABASE_URL
is the ONLY path to _reset_schema().  DATABASE_URL is never used as a fallback for
destructive operations.

How TEST_DATABASE_URL is resolved
----------------------------------
1. If TEST_DATABASE_URL is explicitly set in the environment, it is used.
2. If this process is running in the Replit development workspace
   (REPL_ID is set AND REPLIT_DEPLOYMENT is NOT set), TEST_DATABASE_URL is
   auto-populated from DATABASE_URL at module import.  This is the only case
   where DATABASE_URL is used as a fallback, and it requires the explicit absence
   of the production deployment signal.
3. On any other machine (a developer laptop, a CI box without REPL_ID set),
   TEST_DATABASE_URL must be provided explicitly.  Tests that need schema reset
   will skip if it is absent.

Production safeguard
---------------------
REPLIT_DEPLOYMENT=1 is set by Replit in every Autoscale deployment.  The
_maybe_auto_configure_test_db() function refuses to populate TEST_DATABASE_URL
when that signal is present, so a deployed app can never reach _reset_schema()
through the normal fixture path.  _assert_not_production() provides an explicit
secondary check that can be called from tests when they want to confirm the guard
is in place.
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

# Obviously fake, and long enough to pass policy.
SEED_ADMIN_EMAIL = "admin.aa@example.invalid"
SEED_ADMIN_PASSWORD = "correct-horse-battery-staple-aa"  # noqa: S105
KNOWN_PASSWORD = "quiet-lantern-thicket-9412"  # noqa: S105


class ProductionDatabaseError(RuntimeError):
    """Raised when a fixture would otherwise destroy a production database."""


# ---------------------------------------------------------------------------
# Module-level auto-configuration
# ---------------------------------------------------------------------------


def _maybe_auto_configure_test_db() -> None:
    """Populate TEST_DATABASE_URL from DATABASE_URL in the Replit dev workspace.

    This is the ONLY place DATABASE_URL feeds into the destructive test path.
    It fires only when:

      - TEST_DATABASE_URL is not already explicitly configured
      - REPL_ID is set       → we are inside a Replit workspace
      - REPLIT_DEPLOYMENT is absent → this is NOT a production deployment

    Any other environment (developer laptop, CI runner without REPL_ID) must
    set TEST_DATABASE_URL explicitly; tests that need schema reset will skip
    if it is absent.
    """
    if os.environ.get("TEST_DATABASE_URL", "").strip():
        return  # Already explicitly configured — nothing to do.
    if not os.environ.get("REPL_ID", "").strip():
        return  # Not a Replit workspace — require explicit TEST_DATABASE_URL.
    if os.environ.get("REPLIT_DEPLOYMENT", "").strip():
        return  # Production deployment — never auto-configure.

    db_url = os.environ.get("DATABASE_URL", "").strip()
    if db_url:
        os.environ["TEST_DATABASE_URL"] = db_url


# Run once at import so every fixture picks up the resolved value.
_maybe_auto_configure_test_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_not_production() -> None:
    """Raise ProductionDatabaseError if any production signal is present.

    Can be called from tests to verify the guard is active. Not called inside
    _reset_schema (the auto-configure function is the gatekeeper there).

    Signals checked:
    - REPLIT_DEPLOYMENT  Replit sets this to "1" in every Autoscale deployment.
    - ENVIRONMENT=production  Explicit opt-in to production mode.
    """
    if os.environ.get("REPLIT_DEPLOYMENT", "").strip():
        raise ProductionDatabaseError(
            "REPLIT_DEPLOYMENT is set — this process is running inside a "
            "Replit production deployment. Refusing to act on a production database. "
            "Set TEST_DATABASE_URL to a dedicated test database if you need to run tests."
        )
    if os.environ.get("ENVIRONMENT", "").strip().lower() == "production":
        raise ProductionDatabaseError(
            "ENVIRONMENT=production — refusing to act on a production database. "
            "Set TEST_DATABASE_URL to a dedicated test database if you need to run tests."
        )


def _resolve_test_database_url() -> str:
    """Return TEST_DATABASE_URL, or empty string if it is not configured.

    DATABASE_URL is never used as a fallback here. Callers that need a URL for
    destructive operations must have TEST_DATABASE_URL set (either explicitly or
    via the Replit-dev auto-configure above).
    """
    return os.environ.get("TEST_DATABASE_URL", "").strip()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch) -> Iterator[dict[str, str]]:
    """A minimal valid environment, applied to os.environ for the test.

    Uses TEST_DATABASE_URL as DATABASE_URL inside the fixture scope. Skips if
    TEST_DATABASE_URL is not configured (see module docstring for how to set it).
    """
    database_url = _resolve_test_database_url()
    if not database_url:
        pytest.skip(
            "TEST_DATABASE_URL is not set. "
            "In the Replit workspace it is auto-populated from DATABASE_URL. "
            "On other machines, set TEST_DATABASE_URL to a dedicated test database."
        )

    values = {
        "ENVIRONMENT": "test",
        "DEBUG": "false",
        "SESSION_SECRET_KEY": FAKE_SESSION_SECRET,
        "DATABASE_URL": database_url,
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


def _reset_schema(settings: Settings) -> None:
    """Drop all tables (including alembic_version) for a clean test slate.

    Two hard checks run before any DROP is issued:

    1. TEST_DATABASE_URL must be set — ensures the destructive path is only
       reachable via the explicitly designated test database, never through a
       raw DATABASE_URL that could point anywhere.

    2. _assert_not_production() — raises if REPLIT_DEPLOYMENT is set or
       ENVIRONMENT=production, even when TEST_DATABASE_URL is present. This
       prevents destruction if someone accidentally sets TEST_DATABASE_URL in
       a production environment.

    Base.metadata.drop_all() removes application tables but leaves alembic_version
    intact because Alembic manages that table outside SQLAlchemy metadata. Dropping
    alembic_version forces a full re-run of all migrations on the next boot.
    """
    test_url = _resolve_test_database_url()
    if not test_url:
        raise ProductionDatabaseError(
            "_reset_schema was called but TEST_DATABASE_URL is not set. "
            "Refusing to drop tables against an unverified database. "
            "In the Replit workspace TEST_DATABASE_URL is auto-populated from DATABASE_URL. "
            "On other machines, set TEST_DATABASE_URL to a dedicated test database."
        )

    # Belt-and-suspenders: refuse even if TEST_DATABASE_URL is set, because
    # someone could have misconfigured it to point at a production database.
    _assert_not_production()

    from sqlalchemy import text

    from app.db import Base, create_db_engine

    engine = create_db_engine(settings)
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    engine.dispose()


@pytest.fixture
def client(settings) -> Iterator[TestClient]:
    _reset_schema(settings)
    from app.main import create_app

    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def seeded_client(seeded_env) -> Iterator[TestClient]:
    """A client whose app seeded the initial administrator at startup."""
    from app.config import load_settings
    from app.main import create_app

    s = load_settings()
    _reset_schema(s)
    with TestClient(create_app(s)) as test_client:
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
