"""Configuration must fail loudly. These tests are the proof."""

from __future__ import annotations

import pytest

from app.config import ConfigError, load_settings


def test_missing_session_secret_raises(env, monkeypatch):
    monkeypatch.delenv("SESSION_SECRET_KEY", raising=False)
    with pytest.raises(ConfigError) as exc:
        load_settings()
    assert "SESSION_SECRET_KEY" in str(exc.value)


def test_missing_database_url_raises(env, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ConfigError) as exc:
        load_settings()
    assert "DATABASE_URL" in str(exc.value)


def test_blank_secret_counts_as_missing(env, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "   ")
    with pytest.raises(ConfigError):
        load_settings()


def test_short_session_secret_rejected(env, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "tooshort")
    with pytest.raises(ConfigError, match="at least 32"):
        load_settings()


def test_debug_forbidden_in_production(env, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DEBUG", "true")
    with pytest.raises(ConfigError, match="DEBUG"):
        load_settings()


def test_warning_must_precede_timeout(env, monkeypatch):
    monkeypatch.setenv("SESSION_TIMEOUT_MINUTES", "15")
    monkeypatch.setenv("SESSION_WARNING_MINUTES", "15")
    with pytest.raises(ConfigError, match="SESSION_WARNING_MINUTES"):
        load_settings()


def test_defaults(settings):
    assert settings.session_timeout_minutes == 15
    assert settings.session_warning_minutes == 13
    assert settings.week_start_day == "monday"
    assert settings.timezone == "America/New_York"
    # Both gated modules default to off, per the build prompt.
    assert settings.room_utilization_enabled is False
    assert settings.patient_funnel_enabled is False


def test_default_cpt_exclusions(settings):
    """Confirmed with the practice on 2026-07-27. See ASSUMPTIONS.md A-030."""
    assert settings.cpt_exclusions == ("99998", "99999", "QBCHK", "FORM", "PRO BONO")


def test_cpt_exclusions_override_is_normalized(env, monkeypatch):
    monkeypatch.setenv("CPT_EXCLUSIONS", " 99998 ,qbchk,, pro bono ")
    assert load_settings().cpt_exclusions == ("99998", "QBCHK", "PRO BONO")


def test_repr_never_leaks_secrets(settings):
    rendered = repr(settings)
    assert settings.session_secret_key not in rendered
    assert settings.database_url not in rendered
    assert "REDACTED" in rendered
