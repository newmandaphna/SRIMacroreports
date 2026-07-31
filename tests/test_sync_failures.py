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

    from app.models.data_source import ErrorKind

    assert run.error_kind is ErrorKind.UNEXPECTED, (
        "even the catch all path must tell the system which family it was"
    )


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


# ------------------------------------------------- the other silent-wrong paths


def test_a_renamed_money_column_refuses_the_import(admin_client, source_id):
    """A mapped column whose header changed used to vanish silently: every cell read
    as blank, and blank money means zero by design. Renaming "Total paid" produced a
    SUCCESS run with no rejections and revenue reading zero against a full billed
    figure. Only the four required fields were guarded; every money column was not."""
    renamed = HEADER.replace("Total paid", "Paid total")
    response = upload(admin_client, source_id, renamed + GOOD_ROW.replace("", ""))
    assert response.status_code == 303

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
    assert run.status is SyncStatus.FAILED, "a vanished money column must not import as zero"
    assert "total_paid" in (run.error_message or "")
    # The original message is pinned sentence by sentence, because the spec for the
    # graded split was "the same message"; asserting only the appended guidance would
    # let a rewrite drop the explanation and still pass.
    assert "no longer present in the sheet" in (run.error_message or "")
    assert "Nothing was imported" in (run.error_message or "")
    assert "read as zero" in (run.error_message or "")
    assert "To fix it" in (run.error_message or ""), (
        "the refusal must say what to do, not only what went wrong"
    )
    assert counts(admin_client, source_id)["visits"] == 0


def test_a_renamed_identity_column_also_fails_with_guidance(admin_client, source_id):
    """A vanished identity column is caught by the required header guard, one check
    before the graded branch, so the fix guidance has to live there too or identity
    drift gets the one refusal in the pipeline that does not say what to do."""
    renamed = HEADER.replace("CPT", "PROCEDURE")
    response = upload(admin_client, source_id, renamed + GOOD_ROW)
    assert response.status_code == 303

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
    assert run.status is SyncStatus.FAILED
    assert "cpt" in (run.error_message or "")
    assert "'CPT'" in (run.error_message or ""), "the message must name the expected header"
    assert "To fix it" in (run.error_message or "")
    assert counts(admin_client, source_id)["visits"] == 0


def test_a_mixed_rename_names_everything_stale_in_one_failure(admin_client, source_id):
    """A money rename beside a descriptive one must not be fixed one run at a time.

    The failed run names the money column that stopped it AND the descriptive column
    that would import with gaps, so the admin corrects the mapping in one visit. And a
    FAILED run carries no warnings: they are phrased as "the import went ahead", which
    beside an error saying it did not is the exact lie the zeroed row counters exist
    to prevent.
    """
    renamed = HEADER.replace("Total paid", "Paid total").replace("NOTE", "MEMO")
    response = upload(admin_client, source_id, renamed + GOOD_ROW)
    assert response.status_code == 303

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
    assert run.status is SyncStatus.FAILED
    assert "total_paid" in (run.error_message or "")
    assert "note_code" in (run.error_message or ""), (
        "the descriptive straggler must be named now, not discovered on the next run"
    )
    assert run.warnings == [], "a failed run must not carry warnings claiming an import"
    assert counts(admin_client, source_id)["visits"] == 0


def test_a_failed_write_discards_the_warnings_with_the_counters(
    admin_client, source_id, monkeypatch
):
    """Same failure as the error boundary test, plus a vanished descriptive column:
    the run fails during the writes, after the warnings were collected, and keeping
    them would render "the import went ahead" beside "the run failed"."""
    from app.sync import engine as engine_module

    def explode(*args, **kwargs):
        raise RuntimeError("simulated database rejection during the write")

    monkeypatch.setattr(engine_module, "_upsert", explode)
    response = upload(admin_client, source_id, HEADER.replace("NOTE", "MEMO") + GOOD_ROW)
    assert response.status_code == 303

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
    assert run.status is SyncStatus.FAILED
    assert run.warnings == []
    assert run.reconciliation is None, "the account describes writes, and the writes rolled back"

    page = admin_client.get(f"/admin/sources/{source_id}/runs/{run.id}").text
    assert "Finished with warnings" not in page


def test_a_renamed_descriptive_column_warns_and_imports(admin_client, source_id):
    """The other half of the graded response, from the incident that forced it.

    "Recorded" retitled in the sheet blocked an entire quarter's import even though
    nothing reads the field. A vanished descriptive column makes rows less complete,
    not wrong, so the run now proceeds: the field lands empty, and a warning naming
    the field and its expected header is persisted on the run so the drift is seen
    and fixed rather than becoming permanent. See ASSUMPTIONS.md A-099.
    """
    renamed = HEADER.replace("NOTE", "MEMO")
    response = upload(admin_client, source_id, renamed + GOOD_ROW)
    assert response.status_code == 303

    after = counts(admin_client, source_id)
    assert after["visits"] == 1, "the quarter must import despite the cosmetic rename"
    assert after["errors"] == 0

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
        visit = db.execute(select(Visit).where(Visit.source_id == source_id)).scalars().one()
        note_code = visit.note_code

    assert run.status is SyncStatus.SUCCESS
    assert not run.error_message
    assert note_code is None, "a column the run cannot see imports as empty, never as a guess"

    warnings_text = " ".join(run.warnings or [])
    assert "note_code" in warnings_text, "the warning must name the field"
    assert "'NOTE'" in warnings_text, "the warning must name the expected header"
    assert "To fix it" in warnings_text
    assert "went ahead" in warnings_text, "a live run speaks in the past tense"

    # Persisted, so it survives navigation: the run page still shows it later.
    page = admin_client.get(f"/admin/sources/{source_id}/runs/{run.id}").text
    assert "Finished with warnings" in page
    assert "note_code" in page

    # And on the audit trail, at parity with unmapped_columns.
    from app.models.audit import AuditLog
    from app.models.enums import AuditAction

    with admin_client.app.state.db.session() as db:
        entry = (
            db.execute(
                select(AuditLog)
                .where(AuditLog.action == AuditAction.SYNC_RUN)
                .order_by(AuditLog.id.desc())
            )
            .scalars()
            .first()
        )
    assert "note_code" in (entry.detail or ""), "the audit record must carry the warning"


def test_a_dry_run_previews_the_descriptive_warning(admin_client, source_id):
    """The dry run exists to show what a live run would do, warnings included."""
    renamed = HEADER.replace("NOTE", "MEMO")
    response = upload(admin_client, source_id, renamed + GOOD_ROW, mode="dry_run")
    assert response.status_code == 303

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
    assert run.status is SyncStatus.SUCCESS
    assert any("note_code" in w for w in run.warnings or [])
    assert counts(admin_client, source_id)["visits"] == 0, "a dry run still writes nothing"

    # The preview must not claim writes happened: its page says "Nothing was written"
    # two lines up, and a warning contradicting the page it sits on teaches the
    # reader to trust neither.
    warnings_text = " ".join(run.warnings or [])
    assert "will land" in warnings_text
    assert "went ahead" not in warnings_text


def test_the_incident_field_recorded_flag_no_longer_blocks_a_quarter(admin_client, source_id):
    """The exact incident from A-099: "Recorded" retitled in the sheet.

    The source maps recorded_flag to a header the file does not have. Before the
    graded split that failed the entire quarter's import; now the quarter lands,
    the field is empty, and the warning names it.
    """
    with admin_client.app.state.db.session() as db:
        source = db.get(DataSource, source_id)
        mapping = dict(source.column_mapping)
        mapping["recorded_flag"] = "Recorded"
        source.column_mapping = mapping

    response = upload(admin_client, source_id, HEADER + GOOD_ROW)
    assert response.status_code == 303

    after = counts(admin_client, source_id)
    assert after["visits"] == 1, "the quarter must import"

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
        visit = db.execute(select(Visit).where(Visit.source_id == source_id)).scalars().one()
        recorded = visit.recorded_flag

    assert run.status is SyncStatus.SUCCESS
    assert recorded is None
    warnings_text = " ".join(run.warnings or [])
    assert "recorded_flag" in warnings_text
    assert "'Recorded'" in warnings_text


def test_the_graded_split_covers_exactly_the_five_descriptive_fields():
    """Pins the membership boundary of the split.

    The descriptive set is computed as the allowlist minus money minus identity, so a
    field added to the allowlist later lands on the warn side only if someone thought
    about it here, and a money field can never drift onto the warn side without this
    failing.
    """
    from app.models.data_source import IMPORT_ALLOWLIST, MONEY_FIELDS, REQUIRED_FIELDS

    descriptive = set(IMPORT_ALLOWLIST) - MONEY_FIELDS - REQUIRED_FIELDS
    assert descriptive == {
        "patient_code",
        "insurance_short",
        "location_short",
        "note_code",
        "recorded_flag",
    }


def test_a_vanished_descriptive_column_does_not_erase_stored_values(admin_client, source_id):
    """A run with no view of a column has no opinion about it.

    The rows were imported with note codes. Re-syncing after the header rename used to
    have two bad options: fail the quarter, or overwrite every stored value with the
    blank the run could not see. Now the stored value stands, only rows never imported
    before land without it, and the run reports the untouched rows as unchanged rather
    than churning updated_at across the table.
    """
    first = upload(admin_client, source_id, HEADER + GOOD_ROW)
    assert first.status_code == 303
    with admin_client.app.state.db.session() as db:
        stored = db.execute(select(Visit).where(Visit.source_id == source_id)).scalars().one()
        assert stored.note_code == "OK", (
            "the fixture must import a value for this test to mean anything"
        )

    second = upload(admin_client, source_id, HEADER.replace("NOTE", "MEMO") + GOOD_ROW)
    assert second.status_code == 303

    with admin_client.app.state.db.session() as db:
        stored = db.execute(select(Visit).where(Visit.source_id == source_id)).scalars().one()
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
    assert stored.note_code == "OK", "a value the run could not see must not be erased by it"
    assert run.rows_unchanged == 1, "an identical row minus an unseen column is unchanged"
    assert run.rows_updated == 0


def test_a_deliberately_unmapped_column_stays_silent(admin_client, source_id):
    """The guard must distinguish drift from choice: a column nobody mapped is a
    decision, and it must not start failing imports."""
    with admin_client.app.state.db.session() as db:
        source = db.get(DataSource, source_id)
        mapping = dict(source.column_mapping)
        mapping.pop("note_code")
        source.column_mapping = mapping

    response = upload(admin_client, source_id, HEADER + GOOD_ROW)
    assert response.status_code == 303
    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
    assert run.status is SyncStatus.SUCCESS
    assert counts(admin_client, source_id)["visits"] == 1


def test_a_money_cell_reading_nan_rejects_its_row(admin_client, source_id):
    """Decimal accepts "nan" and quantizing it raises nothing, so one such cell
    poisoned every aggregate it touched or crashed the import at the database."""
    from app.models.data_source import RejectReason

    nan_row = "ZEBRA,Patient AC,2026-04-08,90837,nan,OK\n"
    response = upload(admin_client, source_id, HEADER + GOOD_ROW + nan_row)
    assert response.status_code == 303

    after = counts(admin_client, source_id)
    assert after["visits"] == 1, "the good row must still import"
    with admin_client.app.state.db.session() as db:
        rejection = (
            db.execute(select(ImportErrorRow).where(ImportErrorRow.source_id == source_id))
            .scalars()
            .one()
        )
    assert rejection.reason is RejectReason.BAD_MONEY


# ------------------------------------------------- errors the system can read


def test_the_error_kind_and_detail_travel_to_the_run(admin_client, source_id):
    """The machine readable half of a failure, mirrored from the raise to the run.

    Prose is for the admin; the kind and detail are what let the code route a
    failure, so the tests pin the facts rather than the sentences and the wording
    stays free to improve.
    """
    from app.models.data_source import ErrorKind

    renamed = HEADER.replace("Total paid", "Paid total").replace("NOTE", "MEMO")
    upload(admin_client, source_id, renamed + GOOD_ROW)

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
    assert run.error_kind is ErrorKind.HEADER_DRIFT_MONEY
    assert run.error_detail["fields"] == ["total_paid"]
    assert run.error_detail["expected"] == {"total_paid": "Total paid"}
    assert run.error_detail["also_stale"] == ["note_code"], (
        "the descriptive stragglers ride the detail so the UI can flag them too"
    )


def test_a_vanished_identity_column_carries_its_kind(admin_client, source_id):
    from app.models.data_source import ErrorKind

    upload(admin_client, source_id, HEADER.replace("CPT", "PROCEDURE") + GOOD_ROW)

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
    assert run.error_kind is ErrorKind.HEADER_DRIFT_IDENTITY
    assert run.error_detail["fields"] == ["cpt"]


def test_the_failed_run_page_offers_the_fixing_control(admin_client, source_id):
    """The kind turns the error from a description of the path into the path."""
    upload(admin_client, source_id, HEADER.replace("Total paid", "Paid total") + GOOD_ROW)

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
    page = admin_client.get(f"/admin/sources/{source_id}/runs/{run.id}").text
    assert "Open the column mapping" in page


def test_the_kind_families_are_pinned():
    """Transient and structural drive different behaviour, so the membership is a
    contract: a kind added later lands on a side because somebody chose one."""
    from app.models.data_source import ErrorKind

    transient = {kind for kind in ErrorKind if kind.is_transient}
    assert transient == {
        ErrorKind.RATE_LIMITED,
        ErrorKind.SHEET_UNREACHABLE,
        ErrorKind.CONCURRENT_RUN,
    }
    assert ErrorKind.HEADER_DRIFT_MONEY.fix_area == "mapping"
    assert ErrorKind.TAB_MISSING.fix_area == "tab"
    assert ErrorKind.RATE_LIMITED.fix_area is None


# ------------------------------------------------------------ duplicate headers


DUP_MONEY_HEADER = "Therapist,Patient name,DOS,CPT,Total paid,Total paid,NOTE\n"
DUP_MONEY_ROW = "ZEBRA,Patient AA,2026-04-06,90837,150.00,175.00,OK\n"
DUP_NOTE_HEADER = "Therapist,Patient name,DOS,CPT,Total paid,NOTE,NOTE\n"
DUP_NOTE_ROW = "ZEBRA,Patient AA,2026-04-06,90837,150.00,LEFT,RIGHT\n"


def test_a_duplicated_money_header_refuses_the_import(admin_client, source_id):
    """Two columns wearing the same money name is ambiguity, and the position map
    silently took the leftmost even when the stale copy was leftmost."""
    from app.models.data_source import ErrorKind

    response = upload(admin_client, source_id, DUP_MONEY_HEADER + DUP_MONEY_ROW)
    assert response.status_code == 303

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
    assert run.status is SyncStatus.FAILED
    assert run.error_kind is ErrorKind.DUPLICATE_HEADER
    assert "'Total paid'" in (run.error_message or "")
    assert counts(admin_client, source_id)["visits"] == 0


def test_a_duplicated_descriptive_header_warns_and_uses_the_leftmost(admin_client, source_id):
    """Graded like the vanished check: descriptive ambiguity is not worth a blocked
    quarter, but it is said out loud, naming which copy won."""
    response = upload(admin_client, source_id, DUP_NOTE_HEADER + DUP_NOTE_ROW)
    assert response.status_code == 303

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
        visit = db.execute(select(Visit).where(Visit.source_id == source_id)).scalars().one()
        note_code = visit.note_code

    assert run.status is SyncStatus.SUCCESS
    assert note_code == "LEFT"
    warnings_text = " ".join(run.warnings or [])
    assert "appears 2 times" in warnings_text
    assert "leftmost" in warnings_text


# ----------------------------------------------------------- header row hunting


def test_a_title_row_inserted_above_the_headers_gets_a_row_suggestion(admin_client, source_id):
    """The classic Sheets accident: somebody adds a heading above row 1. The refusal
    now names the row that looks like the real header row, and only names it: the
    setting is never changed automatically."""
    body = "QUARTERLY REPORT,,,,,\n" + HEADER + GOOD_ROW
    response = upload(admin_client, source_id, body)
    assert response.status_code == 303

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
    assert run.status is SyncStatus.FAILED
    assert "Row 2 of the tab looks like it holds these headers" in (run.error_message or "")
    assert run.error_detail["suggested_header_row"] == 2
    # The source is untouched: the suggestion is the admin's to take.
    with admin_client.app.state.db.session() as db:
        assert db.get(DataSource, source_id).header_row == 1


# ------------------------------------------------------- reconciliation account


def test_a_live_run_records_what_it_changed_in_its_span(admin_client, source_id):
    """The before and after account that catches plausible but wrong values.

    A fat fingered amount imports cleanly, because it is a valid number. Only the
    comparison against what the span already held shows it, so every live run
    records that comparison and the run page shows the largest movers.
    """
    upload(admin_client, source_id, HEADER + GOOD_ROW)
    upload(admin_client, source_id, HEADER + GOOD_ROW.replace("150.00", "250.00"))

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
        run_id = run.id
        recon = run.reconciliation

    assert recon["rows_before"] == 1
    assert recon["rows_after"] == 1
    assert recon["collected_before"] == "150.00"
    assert recon["collected_after"] == "250.00"
    assert len(recon["movers"]) == 1
    mover = recon["movers"][0]
    assert (mover["paid_before"], mover["paid_after"]) == ("150.00", "250.00")
    assert "Patient" not in str(recon), "the account must carry no patient identity"

    page = admin_client.get(f"/admin/sources/{source_id}/runs/{run_id}").text
    assert "What this sync changed in its span" in page
    assert "Largest money movements" in page
    # A $100 move on a $150 base is huge in percent terms but the span held less
    # than the warning floor, so it stays information rather than alarm.
    assert "moved" not in " ".join(run.warnings or [])


def test_a_large_swing_on_a_substantial_span_warns(admin_client, source_id):
    """20 percent on at least $1,000: generous enough that a remittance batch on a
    small span or a first import never cries wolf."""
    rows = "".join(
        f"ZEBRA,Patient A{chr(66 + i)},2026-04-0{(i % 5) + 1},90837,150.00,OK\n" for i in range(8)
    )
    upload(admin_client, source_id, HEADER + rows)

    shrunk = rows.replace("150.00", "100.00")
    upload(admin_client, source_id, HEADER + shrunk)

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
    assert run.status is SyncStatus.SUCCESS, "advisory means the import still lands"
    warnings_text = " ".join(run.warnings or [])
    assert "Collected money for" in warnings_text
    assert "33 percent" in warnings_text


def test_a_first_import_into_an_empty_span_never_warns(admin_client, source_id):
    rows = "".join(
        f"ZEBRA,Patient A{chr(66 + i)},2026-04-0{(i % 5) + 1},90837,150.00,OK\n" for i in range(8)
    )
    upload(admin_client, source_id, HEADER + rows)

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
    assert run.reconciliation["rows_before"] == 0
    assert "Collected money for" not in " ".join(run.warnings or [])


def test_rows_missing_from_a_re_read_are_flagged_and_kept(admin_client, source_id):
    """The sort-a-filtered-range disaster, finally visible.

    Rows deleted from the sheet used to produce no signal at all: the store kept
    them, the figures kept counting them, and nobody knew the sheet disagreed. The
    re-read now names how many stored rows it did not contain, and the rows stand.
    """
    two = HEADER + GOOD_ROW + "ZEBRA,Patient AB,2026-04-06,90834,125.00,OK\n"
    upload(admin_client, source_id, two)
    upload(admin_client, source_id, HEADER + GOOD_ROW)

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
    assert run.status is SyncStatus.SUCCESS
    assert run.reconciliation["vanished_count"] == 1
    warnings_text = " ".join(run.warnings or [])
    assert "were not in this read" in warnings_text
    assert "version history" in warnings_text, "the warning must name the sheet's undo button"
    assert counts(admin_client, source_id)["visits"] == 2, "nothing is deleted on this side"


def test_another_sources_rows_never_count_as_vanished(admin_client, source_id):
    """The rolling export window means two sheets legitimately share a span. A row
    imported from the other sheet is not a deletion from this one."""
    with admin_client.app.state.db.session() as db:
        other = DataSource(
            label="Other quarter",
            provider=SourceProvider.UPLOAD,
            tab_name=CSV_TAB_NAME,
            header_row=1,
            column_mapping=dict(MAPPING),
            active=True,
        )
        db.add(other)
        db.flush()
        therapist_id = db.execute(select(Therapist.id)).scalars().first()
        from datetime import date as _date
        from decimal import Decimal as _Decimal

        db.add(
            Visit(
                source_id=other.id,
                therapist_id=therapist_id,
                patient_name="Patient AZ",
                patient_name_normalized="PATIENT AZ",
                dos=_date(2026, 4, 6),
                cpt="90837",
                cpt_base="90837",
                total_paid=_Decimal("150.00"),
                total_due=_Decimal("150.00"),
                total_balance=_Decimal("0.00"),
            )
        )

    upload(admin_client, source_id, HEADER + GOOD_ROW)

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
    assert run.reconciliation["vanished_count"] == 0
    assert "were not in this read" not in " ".join(run.warnings or [])


def test_a_dry_run_records_no_reconciliation(admin_client, source_id):
    """A dry run deliberately reads no existing rows, so it has no before to compare
    against, and a comparison of nothing against nothing would only mislead."""
    upload(admin_client, source_id, HEADER + GOOD_ROW, mode="dry_run")

    with admin_client.app.state.db.session() as db:
        run = (
            db.execute(
                select(SyncRun).where(SyncRun.source_id == source_id).order_by(SyncRun.id.desc())
            )
            .scalars()
            .first()
        )
    assert run.reconciliation is None
