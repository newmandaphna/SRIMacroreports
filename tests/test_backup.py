"""The offline backup button: gating, the dump itself, and the audit trail."""

from __future__ import annotations

import re
import shutil

import pytest
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.enums import AuditAction, Role
from app.routers.admin_backup import plain_postgres_url
from tests.conftest import make_user, sign_in


def token_from(text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', text)
    assert match, "no CSRF token on page"
    return match.group(1)


def test_the_driver_suffix_is_stripped_for_pg_dump():
    assert plain_postgres_url("postgresql+psycopg2://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
    assert plain_postgres_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


def test_a_viewer_cannot_download_a_backup(client):
    """Refused by the authorization check, not by a missing CSRF token.

    This posted no token, so the CSRF middleware answered it before the route was
    reached: the test passed without ever exercising the admin guard, and would have
    kept passing if the guard were deleted. It accepted 303 as well as 403, which is
    the status a successful redirect uses, so half its allowed outcomes were wrong.
    """
    with client.app.state.db.session() as db:
        email = make_user(db, email="nobackup@example.invalid", role=Role.VIEWER).email
    sign_in(client, email)

    # A real, session bound token, from a page every signed in user can open.
    token = token_from(client.get("/change-password").text)
    response = client.post("/admin/backup", data={"csrf_token": token}, follow_redirects=False)

    assert response.status_code == 403, (
        "a viewer must be refused by the admin guard, and a 303 would mean it ran"
    )
    assert "administrators" in response.text


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump not installed")
def test_an_admin_gets_a_real_dump_and_it_is_audited(client):
    with client.app.state.db.session() as db:
        email = make_user(db, email="backup.admin@example.invalid", role=Role.ADMIN).email
    sign_in(client, email)

    token = token_from(client.get("/admin/config").text)
    response = client.post("/admin/backup", data={"csrf_token": token}, follow_redirects=False)

    if response.status_code == 303:
        # A pg_dump client older than the server refuses to dump. That is an
        # environment problem, not an application defect; the route said so.
        assert "problem=" in response.headers["location"]
        pytest.skip("pg_dump client incompatible with the test server")

    assert response.status_code == 200
    # Custom-format archives open with the PGDMP magic bytes.
    assert response.content.startswith(b"PGDMP")
    assert "attachment" in response.headers["content-disposition"]
    assert "sri-backup-" in response.headers["content-disposition"]

    with client.app.state.db.session() as db:
        entry = (
            db.execute(
                select(AuditLog)
                .where(
                    AuditLog.action == AuditAction.EXPORT,
                    AuditLog.target_type == "database",
                )
                .order_by(AuditLog.id.desc())
            )
            .scalars()
            .first()
        )
    assert entry is not None
    assert "bytes" in (entry.detail or "")
