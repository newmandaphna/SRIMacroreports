"""Tests for the production-database safety guards in conftest.py.

Covers three independent components:

  _assert_not_production()          - raises on REPLIT_DEPLOYMENT or ENVIRONMENT=production
  _resolve_test_database_url()      - returns TEST_DATABASE_URL only; no DATABASE_URL fallback
  _maybe_auto_configure_test_db()   - auto-populates TEST_DATABASE_URL in the Replit dev workspace
  _reset_schema()                   - requires TEST_DATABASE_URL; raises if absent
"""

from __future__ import annotations

import os

import pytest

from tests.conftest import (
    ProductionDatabaseError,
    _assert_not_production,
    _maybe_auto_configure_test_db,
    _reset_schema,
    _resolve_test_database_url,
)

# ---------------------------------------------------------------------------
# _assert_not_production
# ---------------------------------------------------------------------------


def test_replit_deployment_flag_raises(monkeypatch):
    """REPLIT_DEPLOYMENT=1 (set by every Autoscale deployment) must raise."""
    monkeypatch.setenv("REPLIT_DEPLOYMENT", "1")
    with pytest.raises(ProductionDatabaseError, match="REPLIT_DEPLOYMENT"):
        _assert_not_production()


def test_environment_production_raises(monkeypatch):
    """ENVIRONMENT=production must raise."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(ProductionDatabaseError, match="ENVIRONMENT=production"):
        _assert_not_production()


def test_environment_production_case_insensitive(monkeypatch):
    """Case variants of 'production' are all caught."""
    for value in ("PRODUCTION", "Production", "  production  "):
        monkeypatch.setenv("ENVIRONMENT", value)
        with pytest.raises(ProductionDatabaseError):
            _assert_not_production()


def test_development_environment_is_allowed(monkeypatch):
    """Dev and test environments pass without raising."""
    monkeypatch.delenv("REPLIT_DEPLOYMENT", raising=False)
    for value in ("development", "test", ""):
        monkeypatch.setenv("ENVIRONMENT", value)
        _assert_not_production()  # must not raise


def test_no_guard_vars_is_allowed(monkeypatch):
    """Absence of both guard vars is treated as safe (dev default)."""
    monkeypatch.delenv("REPLIT_DEPLOYMENT", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    _assert_not_production()  # must not raise


# ---------------------------------------------------------------------------
# _resolve_test_database_url — TEST_DATABASE_URL only, no DATABASE_URL fallback
# ---------------------------------------------------------------------------


def test_returns_test_database_url_when_set(monkeypatch):
    """TEST_DATABASE_URL is returned when present."""
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://test-host/testdb")
    monkeypatch.setenv("DATABASE_URL", "postgresql://live-host/livedb")
    assert _resolve_test_database_url() == "postgresql://test-host/testdb"


def test_returns_empty_when_test_url_absent(monkeypatch):
    """Empty string is returned when TEST_DATABASE_URL is absent, regardless of DATABASE_URL."""
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://live-host/livedb")
    assert _resolve_test_database_url() == ""


def test_database_url_is_never_a_fallback(monkeypatch):
    """DATABASE_URL alone must never be returned by _resolve_test_database_url."""
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://any-host/anydb")
    assert _resolve_test_database_url() == ""


# ---------------------------------------------------------------------------
# _maybe_auto_configure_test_db
# ---------------------------------------------------------------------------


def test_auto_configure_in_replit_dev_workspace(monkeypatch):
    """REPL_ID set + REPLIT_DEPLOYMENT absent → TEST_DATABASE_URL auto-populated."""
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("REPL_ID", "some-repl-id")
    monkeypatch.delenv("REPLIT_DEPLOYMENT", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://dev-host/devdb")

    _maybe_auto_configure_test_db()

    assert os.environ.get("TEST_DATABASE_URL") == "postgresql://dev-host/devdb"


def test_no_auto_configure_in_production_deployment(monkeypatch):
    """REPLIT_DEPLOYMENT set must prevent auto-configuration."""
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("REPL_ID", "some-repl-id")
    monkeypatch.setenv("REPLIT_DEPLOYMENT", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod-host/proddb")

    _maybe_auto_configure_test_db()

    assert not os.environ.get("TEST_DATABASE_URL"), (
        "TEST_DATABASE_URL must not be set when REPLIT_DEPLOYMENT is present"
    )


def test_no_auto_configure_outside_replit(monkeypatch):
    """REPL_ID absent (local machine / CI) must not auto-configure."""
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("REPL_ID", raising=False)
    monkeypatch.delenv("REPLIT_DEPLOYMENT", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://local-host/localdb")

    _maybe_auto_configure_test_db()

    assert not os.environ.get("TEST_DATABASE_URL"), (
        "TEST_DATABASE_URL must not be auto-set outside a Replit workspace"
    )


def test_explicit_test_url_not_overwritten(monkeypatch):
    """An already-set TEST_DATABASE_URL must never be overwritten."""
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://ci-host/cidb")
    monkeypatch.setenv("REPL_ID", "some-repl-id")
    monkeypatch.delenv("REPLIT_DEPLOYMENT", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://dev-host/devdb")

    _maybe_auto_configure_test_db()

    assert os.environ.get("TEST_DATABASE_URL") == "postgresql://ci-host/cidb"


def test_no_auto_configure_when_database_url_absent(monkeypatch):
    """If DATABASE_URL is not set, auto-configure must not set TEST_DATABASE_URL."""
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("REPL_ID", "some-repl-id")
    monkeypatch.delenv("REPLIT_DEPLOYMENT", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    _maybe_auto_configure_test_db()

    assert not os.environ.get("TEST_DATABASE_URL")


# ---------------------------------------------------------------------------
# _reset_schema — requires TEST_DATABASE_URL; no DATABASE_URL fallback
# ---------------------------------------------------------------------------


def test_reset_schema_raises_when_test_url_absent(monkeypatch):
    """_reset_schema must raise ProductionDatabaseError when TEST_DATABASE_URL is unset."""
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)

    # settings object is not needed to hit the guard — the check runs before
    # any database connection is attempted.
    class _FakeSettings:
        database_url = "postgresql://dev-host/devdb"

    with pytest.raises(ProductionDatabaseError, match="TEST_DATABASE_URL"):
        _reset_schema(_FakeSettings())  # type: ignore[arg-type]


def test_reset_schema_raises_in_production_even_with_test_url(monkeypatch):
    """_reset_schema must refuse when REPLIT_DEPLOYMENT is set, even with TEST_DATABASE_URL.

    Prevents the case where someone accidentally sets TEST_DATABASE_URL in a production
    environment: the REPLIT_DEPLOYMENT guard fires before any DROP is issued.
    """
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://test-host/testdb")
    monkeypatch.setenv("REPLIT_DEPLOYMENT", "1")

    class _FakeSettings:
        database_url = "postgresql://test-host/testdb"

    with pytest.raises(ProductionDatabaseError, match="REPLIT_DEPLOYMENT"):
        _reset_schema(_FakeSettings())  # type: ignore[arg-type]


def test_reset_schema_raises_in_production_env_even_with_test_url(monkeypatch):
    """_reset_schema must refuse when ENVIRONMENT=production, even with TEST_DATABASE_URL."""
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://test-host/testdb")
    monkeypatch.delenv("REPLIT_DEPLOYMENT", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")

    class _FakeSettings:
        database_url = "postgresql://test-host/testdb"

    with pytest.raises(ProductionDatabaseError, match="ENVIRONMENT=production"):
        _reset_schema(_FakeSettings())  # type: ignore[arg-type]


def test_reset_schema_allowed_with_test_url(monkeypatch, settings):
    """_reset_schema must proceed when TEST_DATABASE_URL is configured and env is safe."""
    # settings fixture already ensures TEST_DATABASE_URL is set (via env fixture).
    # This call must not raise; the actual DROP is the desired side effect.
    _reset_schema(settings)


# ---------------------------------------------------------------------------
# Integration: env fixture uses TEST_DATABASE_URL
# ---------------------------------------------------------------------------


def test_env_fixture_passes_test_url_as_database_url(env):
    """The env fixture must expose TEST_DATABASE_URL as DATABASE_URL inside the test."""
    test_url = os.environ.get("TEST_DATABASE_URL", "")
    # env monkeypatches DATABASE_URL to the test URL value.
    assert os.environ.get("DATABASE_URL") == test_url
    assert os.environ.get("DATABASE_URL"), "DATABASE_URL must be non-empty inside env fixture"
