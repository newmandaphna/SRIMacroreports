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


def test_middleware_runs_in_the_intended_order(client):
    """Nothing pinned this, and the order was wrong.

    Starlette applies the last added first, so the list below is outermost first. CSRF
    was added after everything else and therefore ran ahead of the host and scheme
    guards: a request with a forged Host, or one arriving over plain HTTP that was about
    to be redirected, had its form parsed and could drive an audit write first. Host and
    scheme are the cheapest rejections there are and belong outside everything. Security
    headers stay outside CSRF so a refused request still carries them on the way out.
    """
    from app.middleware import SecurityHeadersMiddleware
    from app.security.csrf import CSRFMiddleware

    installed = [m.cls for m in client.app.user_middleware]
    assert installed.index(SecurityHeadersMiddleware) < installed.index(CSRFMiddleware), (
        "CSRF must run inside the security headers, so a refusal is still given them"
    )


def test_production_installs_the_host_and_scheme_guards_outermost(env, monkeypatch):
    """The production-only middleware had no test at all, so nothing proved it was
    installed, let alone that it sat outside the rest."""
    from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    from app.config import load_settings
    from app.main import create_app
    from app.middleware import SecurityHeadersMiddleware
    from app.security.csrf import CSRFMiddleware

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DEBUG", "false")
    settings = load_settings()
    assert settings.is_production is True

    installed = [m.cls for m in create_app(settings).user_middleware]
    order = [
        TrustedHostMiddleware,
        HTTPSRedirectMiddleware,
        SecurityHeadersMiddleware,
        CSRFMiddleware,
    ]
    for middleware in order:
        assert middleware in installed, f"{middleware.__name__} is not installed in production"
    positions = [installed.index(m) for m in order]
    assert positions == sorted(positions), (
        f"middleware order is wrong: {[m.__name__ for m in installed]}"
    )


def test_production_sets_strict_transport_security(env, monkeypatch):
    """HSTS is production only, so the ordinary client can never show it is set."""
    from fastapi.testclient import TestClient

    from app.config import load_settings
    from app.main import create_app

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DEBUG", "false")
    app = create_app(load_settings())

    # base_url is https so the redirect middleware lets the request through, and the
    # Host is one the trusted-host list accepts.
    with TestClient(app, base_url="https://sri.replit.app") as production:
        headers = production.get("/healthz").headers

    assert "max-age=" in headers.get("Strict-Transport-Security", "")
    assert headers["X-Frame-Options"] == "DENY"


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
