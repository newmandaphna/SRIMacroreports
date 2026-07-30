"""What the importer does when a row is bad in a way the database rejects.

The rule these tests defend: an import that fails must leave a record of having
failed. A failure that rolls back its own sync run, its own rejected rows, and its
own audit entry is indistinguishable from an import that never happened, which is
the worst possible outcome for a quarterly figure somebody is about to rely on.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import func, select

from app.models.data_source import DataSource, SourceProvider, SyncRun, SyncStatus
from app.models.data_source import ImportError as ImportErrorRow
from app.models.enums import Role
from app.models.therapist import AliasSource, EmploymentType, Therapist, TherapistAlias
from app.models.visit import Visit
from app.sync.upload import CSV_TAB_NAME
from tests.conftest import make_user, sign_in

HEADER = "Therapist,Patient name,DOS,CPT,Total paid,NOTE\n"
GOOD_ROW = "ZEBRA,Patient AA,2026-04-06,90837,150.00,OK\n"
# note_code is String(20). Twenty-one characters is the smallest cell that the
# database will refuse, and nothing upstream checks it.
TOO_LONG_ROW = f"ZEBRA,Patient AB,2026-04-07,90834,125.00,{'X' * 21}\n"

MAPPING = {
    "therapist": "Therapist",
    "patient_name": "Patient name",
    "dos": "DOS",
    "cpt": "CPT",
    "total_paid": "Total paid",
    "note_code": "NOTE",
}


def token_from(text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', text)
    assert match, "no CSRF token on page"
    return match.group(1)


@pytest.fixture
def admin_client(client):
    with client.app.state.db.session() as db:
        email = make_user(db, email="fail.admin@example.invalid", role=Role.ADMIN).email
    sign_in(client, email)
    return client


@pytest.fixture
def source_id(admin_client):
    with admin_client.app.state.db.session() as db:
        therapist = Therapist(
            display_name="Zebra Failure", employment_type=EmploymentType.SALARIED_BENEFITS
        )
        db.add(therapist)
        db.flush()
        db.add(TherapistAlias(therapist_id=therapist.id, alias="ZEBRA", source=AliasSource.MANUAL))
        source = DataSource(
            label="Failure source",
            provider=SourceProvider.UPLOAD,
            tab_name=CSV_TAB_NAME,
            header_row=1,
            column_mapping=dict(MAPPING),
            active=True,
        )
        db.add(source)
        db.flush()
        return source.id


def upload(admin_client, source_id, body: str, *, mode: str = "live"):
    token = token_from(admin_client.get(f"/admin/sources/{source_id}").text)
    return admin_client.post(
        f"/admin/sources/{source_id}/upload",
        data={"csrf_token": token, "mode": mode},
        files={"file": ("rows.csv", body.encode(), "text/csv")},
        follow_redirects=False,
    )


def counts(admin_client, source_id):
    with admin_client.app.state.db.session() as db:
        return {
            "visits": db.execute(
                select(func.count(Visit.id)).where(Visit.source_id == source_id)
            ).scalar_one(),
            "runs": db.execute(
                select(func.count(SyncRun.id)).where(SyncRun.source_id == source_id)
            ).scalar_one(),
            "errors": db.execute(
                select(func.count(ImportErrorRow.id)).where(ImportErrorRow.source_id == source_id)
            ).scalar_one(),
        }


def test_a_write_that_fails_at_the_database_leaves_a_recorded_failure(
    admin_client, source_id, monkeypatch
):
    """The error boundary, tested where it actually matters.

    The guards added alongside this catch the bad cells we know about. This test is
    about the ones we do not: any exception raised while the rows are being written
    must leave a FAILED run carrying a message. Before the fix the failure surfaced
    on a flush that happened after the handler had closed, so the transaction rolled
    back and took the sync run, every recorded rejection and the audit entry with it.
    The admin got an opaque 500 and the import history showed nothing at all, which
    is indistinguishable from an import nobody ever ran.
    """
    from app.sync import engine as engine_module

    real_upsert = engine_module._upsert

    def explode(*args, **kwargs):
        raise RuntimeError("simulated database rejection during the write")

    monkeypatch.setattr(engine_module, "_upsert", explode)
    try:
        response = upload(admin_client, source_id, HEADER + GOOD_ROW)
    finally:
        monkeypatch.setattr(engine_module, "_upsert", real_upsert)

    assert response.status_code == 303, "a failed write must not surface as a 500"

    after = counts(admin_client, source_id)
    assert after["runs"] == 1, "the failed run must persist so the admin can see it"
    assert after["visits"] == 0, "nothing may be half written"

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
    assert run.status is SyncStatus.FAILED
    assert run.error_message, "a failed run must carry a message"
    assert run.rows_inserted == 0, "a failed run must not claim rows it did not write"


def test_one_oversized_cell_rejects_its_own_row_and_the_rest_imports(admin_client, source_id):
    """The right blast radius for a bad cell is one row, named in the review queue."""
    from app.models.data_source import RejectReason

    response = upload(admin_client, source_id, HEADER + GOOD_ROW + TOO_LONG_ROW)
    assert response.status_code == 303

    after = counts(admin_client, source_id)
    assert after["visits"] == 1, "the valid row must survive its neighbour"
    assert after["errors"] == 1, "the bad row must be recorded, not silently dropped"

    with admin_client.app.state.db.session() as db:
        rejection = (
            db.execute(select(ImportErrorRow).where(ImportErrorRow.source_id == source_id))
            .scalars()
            .one()
        )
    assert rejection.reason is RejectReason.VALUE_TOO_LONG
    assert rejection.field == "note_code"
    assert "20" in (rejection.detail or ""), "the message must name the limit"


def test_a_clean_file_still_imports(admin_client, source_id):
    """The guard must not break the ordinary path."""
    response = upload(admin_client, source_id, HEADER + GOOD_ROW)
    assert response.status_code == 303
    after = counts(admin_client, source_id)
    assert after["visits"] == 1
    assert after["runs"] == 1

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
    assert run.status is SyncStatus.SUCCESS
    assert not run.error_message


def test_a_dry_run_of_a_bad_file_agrees_with_the_live_run(admin_client, source_id):
    """A dry run that calls a file clean, followed by a live run that dies on it, is
    a preview nobody can trust. Both must reach the same verdict about the row."""
    dry = upload(admin_client, source_id, HEADER + GOOD_ROW + TOO_LONG_ROW, mode="dry_run")
    assert dry.status_code == 303

    with admin_client.app.state.db.session() as db:
        dry_run_row = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
        dry_rejections = db.execute(
            select(func.count(ImportErrorRow.id)).where(
                ImportErrorRow.sync_run_id == dry_run_row.id
            )
        ).scalar_one()

    assert dry_rejections >= 1 or dry_run_row.error_message, (
        "the dry run reported the file as clean, so the live run's failure is a surprise"
    )
