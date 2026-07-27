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

FAKE_SESSION_SECRET = "test-session-secret-value-not-real-000000000000"  # noqa: S105
FAKE_DB_KEY = "test-database-key-not-real"  # noqa: S105


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
def settings(env) -> Settings:
    return load_settings()


@pytest.fixture
def client(settings) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app(settings)) as test_client:
        yield test_client
