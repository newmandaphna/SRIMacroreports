"""The app boots, connects to PostgreSQL, and security headers are set."""

from __future__ import annotations

import pytest

from app.config import ConfigError, load_settings
from app.db import DatabaseHandle


def test_health_endpoint(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readiness_hits_the_database(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_index_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "SRI Practice Dashboard" in response.text


def test_security_headers_present(client):
    headers = client.get("/healthz").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    # Patient data must not sit in any cache.
    assert "no-store" in headers["Cache-Control"]


def test_no_api_docs_exposed(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_database_connects_to_postgresql(settings):
    """Verify the app connects to PostgreSQL and can execute a query."""
    handle = DatabaseHandle(settings)
    with handle.session() as session:
        from sqlalchemy import text

        result = session.execute(text("SELECT version()")).scalar()
        assert result is not None
        assert "PostgreSQL" in result
    handle.dispose()


def test_bad_database_url_fails_loudly(env, monkeypatch):
    """A wrong or unreachable DATABASE_URL raises ConfigError at startup."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:invalid@localhost:5432/nonexistent")
    s = load_settings()
    with pytest.raises(ConfigError, match="Could not connect"):
        DatabaseHandle(s)
