"""The audit log is append only, and that is enforced rather than promised.

SECURITY.md section 4 tells an auditor there is no update path and no delete path in
the code. These tests are the evidence.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import delete, select, update

from app.models.audit import AuditLog, AuditLogImmutableError
from app.models.enums import AuditAction, AuditResult, Role
from app.security import audit
from tests.conftest import make_user, sign_in


@pytest.fixture
def admin_client(client):
    with client.app.state.db.session() as session:
        user = make_user(session, email="a.audit@example.invalid", role=Role.ADMIN)
        email = user.email
    sign_in(client, email)
    return client


def test_records_can_be_written(client):
    with client.app.state.db.session() as session:
        audit.record(session, action=AuditAction.SYNC_RUN, actor_label="test")
    with client.app.state.db.session() as session:
        assert session.execute(select(AuditLog)).scalars().all()


def test_updating_a_record_raises(client):
    with client.app.state.db.session() as session:
        audit.record(session, action=AuditAction.SYNC_RUN, actor_label="test")

    with pytest.raises(AuditLogImmutableError):
        with client.app.state.db.session() as session:
            entry = session.execute(select(AuditLog)).scalars().first()
            entry.detail = "tampered"


def test_deleting_a_record_raises(client):
    with client.app.state.db.session() as session:
        audit.record(session, action=AuditAction.SYNC_RUN, actor_label="test")

    with pytest.raises(AuditLogImmutableError):
        with client.app.state.db.session() as session:
            entry = session.execute(select(AuditLog)).scalars().first()
            session.delete(entry)


def test_a_bulk_statement_really_does_bypass_the_orm_guard(client):
    """Demonstrate the gap rather than describe it.

    The ORM event guard fires per instance, so a Core UPDATE or DELETE goes straight to
    the database. That is a real limit on the immutability claim, and the compensating
    control is `test_no_audit_mutation_in_application_code` below, which fails the build
    if such a statement ever appears in the source.

    This test used to end with `assert update is not None and delete is not None`, which
    asserts only that two imported names exist. It could not fail for any reason
    connected to the audit log, so the gap it claimed to document was undocumented and a
    future ORM level fix would not have shown up here at all.
    """
    with client.app.state.db.session() as session:
        audit.record(session, action=AuditAction.SYNC_RUN, actor_label="test")

    with client.app.state.db.session() as session:
        assert len(session.execute(select(AuditLog)).scalars().all()) == 1

    # Inside a savepoint, rolled back, so the demonstration leaves no mark.
    session = client.app.state.db.session_factory()
    try:
        savepoint = session.begin_nested()
        bulk_update = session.execute(
            update(AuditLog)
            .values(actor_label="rewritten")
            .execution_options(synchronize_session=False)
        )
        bulk_delete = session.execute(delete(AuditLog).execution_options(synchronize_session=False))
        savepoint.rollback()
    finally:
        session.rollback()
        session.close()

    assert bulk_update.rowcount == 1, (
        "a Core UPDATE was refused, which would mean the guard now covers bulk "
        "statements. Good news, and this test and its docstring need rewriting."
    )
    assert bulk_delete.rowcount == 1, (
        "a Core DELETE was refused, which would mean the guard now covers bulk "
        "statements. Good news, and this test and its docstring need rewriting."
    )

    # The row survives, because the demonstration was rolled back.
    with client.app.state.db.session() as session:
        surviving = session.execute(select(AuditLog)).scalars().all()
    assert len(surviving) == 1
    assert surviving[0].actor_label == "test"


def test_no_audit_mutation_in_application_code():
    """Grep the source for any write to the audit log other than an insert.

    This is the control that makes "there is no delete path in code" checkable rather
    than asserted, and it fails the build if someone adds one later.
    """
    from pathlib import Path

    app_dir = Path(__file__).resolve().parent.parent / "app"
    offenders: list[str] = []

    patterns = [
        re.compile(r"update\s*\(\s*AuditLog\s*\)"),
        re.compile(r"delete\s*\(\s*AuditLog\s*\)"),
        re.compile(r"\.delete\s*\(\s*[a-z_]*audit[a-z_]*\s*\)", re.IGNORECASE),
        re.compile(r"DELETE\s+FROM\s+audit_log", re.IGNORECASE),
        re.compile(r"UPDATE\s+audit_log", re.IGNORECASE),
    ]

    for path in app_dir.rglob("*.py"):
        text = path.read_text()
        for pattern in patterns:
            if pattern.search(text):
                offenders.append(f"{path.name}: {pattern.pattern}")

    assert offenders == [], f"Audit log mutation found in application code: {offenders}"


def test_detail_is_scrubbed_before_storage(client):
    """A filter parameter carrying a patient name must not land in the log."""
    with client.app.state.db.session() as session:
        audit.record(
            session,
            action=AuditAction.PHI_VIEW,
            actor_label="test",
            detail={"filters": {"patient_name": "Patientaa,Testcase"}},
        )

    with client.app.state.db.session() as session:
        entry = session.execute(select(AuditLog)).scalars().one()
    assert "Patientaa" not in (entry.detail or "")
    assert "REDACTED" in (entry.detail or "")


def test_admin_can_view_the_log(admin_client):
    response = admin_client.get("/admin/audit")
    assert response.status_code == 200
    assert "Audit log" in response.text


def test_viewing_the_log_is_itself_audited(admin_client):
    admin_client.get("/admin/audit")
    with admin_client.app.state.db.session() as session:
        actions = session.execute(select(AuditLog.action)).scalars().all()
    assert AuditAction.AUDIT_VIEWED in actions


def test_export_is_csv_and_is_audited(admin_client):
    response = admin_client.get("/admin/audit/export.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert "occurred_at_utc,actor,action" in response.text

    with admin_client.app.state.db.session() as session:
        exports = (
            session.execute(select(AuditLog).where(AuditLog.action == AuditAction.EXPORT))
            .scalars()
            .all()
        )
    assert len(exports) == 1
    assert '"rows"' in (exports[0].detail or "")


def test_non_admin_cannot_export(client):
    with client.app.state.db.session() as session:
        user = make_user(session, email="v.audit@example.invalid", role=Role.VIEWER)
        email = user.email
    sign_in(client, email)
    assert client.get("/admin/audit/export.csv").status_code == 403


def test_failed_login_records_the_attempted_identifier(client):
    client.post(
        "/login",
        data={"email": "typo.aa@example.invalid", "password": "wrong-password-here"},
    )
    with client.app.state.db.session() as session:
        entry = (
            session.execute(select(AuditLog).where(AuditLog.action == AuditAction.LOGIN_FAILURE))
            .scalars()
            .one()
        )
    assert entry.actor_id is None
    assert entry.actor_label == "typo.aa@example.invalid"
    assert entry.result == AuditResult.FAILURE
