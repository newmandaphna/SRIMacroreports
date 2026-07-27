"""The app boots, its database is genuinely encrypted, and headers are set."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.config import ConfigError
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


def test_database_file_is_encrypted(settings):
    handle = DatabaseHandle(settings)
    with handle.session() as session:
        from sqlalchemy import text

        session.execute(text("CREATE TABLE probe (marker TEXT)"))
        session.execute(text("INSERT INTO probe VALUES ('CANARYVALUE')"))
    handle.dispose()

    raw = Path(settings.database_path).read_bytes()
    # A plain SQLite file starts with this magic and would show the value verbatim.
    assert not raw.startswith(b"SQLite format 3")
    assert b"CANARYVALUE" not in raw

    # And the stdlib driver, which has no key, cannot read it.
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(settings.database_path).execute("SELECT * FROM probe").fetchall()


def test_wrong_key_fails_loudly(settings):
    DatabaseHandle(settings).dispose()
    wrong = settings.__class__(
        **{**settings.__dict__, "database_encryption_key": "a-different-wrong-key"}
    )
    with pytest.raises(ConfigError, match="DATABASE_ENCRYPTION_KEY"):
        DatabaseHandle(wrong)
