"""Historical data upload: the file-backed client and the upload route.

Everything here uses synthetic patients (Patient AA, PATAA), per the repository rule
that no real patient name ever appears in test data.
"""

from __future__ import annotations

import io
import re

import pytest
from sqlalchemy import func, select

from app.models.data_source import DataSource, SourceProvider
from app.models.enums import Role
from app.models.therapist import AliasSource, EmploymentType, Therapist, TherapistAlias
from app.models.visit import Visit
from app.sync.sheets import SheetsError
from app.sync.upload import CSV_TAB_NAME, UploadedWorkbookClient
from tests.conftest import make_user, sign_in

CSV_CONTENT = (
    b"Therapist,Patient name,DOS,CPT,Total paid\n"
    b"ZEBRA,Patient AA,2025-01-06,90837,150.00\n"
    b"ZEBRA,Patient AB,2025-01-07,90834,125.00\n"
)

MAPPING = {
    "therapist": "Therapist",
    "patient_name": "Patient name",
    "dos": "DOS",
    "cpt": "CPT",
    "total_paid": "Total paid",
}


def token_from(text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', text)
    assert match, "no CSRF token on page"
    return match.group(1)


def xlsx_bytes(tabs: dict[str, list[list[object]]]) -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, rows in tabs.items():
        sheet = workbook.create_sheet(title=title)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ------------------------------------------------------------------ client parsing


def test_a_csv_answers_any_allowed_tab_name():
    client = UploadedWorkbookClient("history.csv", CSV_CONTENT)
    data = client.read_tab("", "Whatever The Source Says", 1)
    assert data.headers[:4] == ["Therapist", "Patient name", "DOS", "CPT"]
    assert len(data.rows) == 2


def test_a_csv_still_refuses_a_raw_tab_name():
    client = UploadedWorkbookClient("history.csv", CSV_CONTENT)
    with pytest.raises(SheetsError, match="never imports"):
        client.read_tab("", "RAW_PatientStatement", 1)


def test_an_xlsx_reads_its_named_tab():
    content = xlsx_bytes(
        {
            "Q1 Snapshot": [
                ["Therapist", "Patient name", "DOS", "CPT"],
                ["ZEBRA", "Patient AA", "2025-01-06", "90837"],
            ]
        }
    )
    data = UploadedWorkbookClient("q1.xlsx", content).read_tab("", "Q1 Snapshot", 1)
    assert data.rows[0][0] == "ZEBRA"


def test_an_xlsx_with_the_wrong_tab_names_the_real_ones():
    content = xlsx_bytes({"Q1 Snapshot": [["Therapist"]], "Notes": [["x"]]})
    with pytest.raises(SheetsError, match="Q1 Snapshot"):
        UploadedWorkbookClient("q1.xlsx", content).read_tab("", "Uploaded CSV", 1)


def test_an_xlsx_raw_tab_is_blocked_and_never_suggested():
    content = xlsx_bytes({"RAW_Patients": [["DOB"]], "Q1 Snapshot": [["Therapist"]]})
    client = UploadedWorkbookClient("q1.xlsx", content)
    with pytest.raises(SheetsError, match="never imports"):
        client.read_tab("", "RAW_Patients", 1)
    # The wrong-tab message must not advertise the blocked tab either.
    with pytest.raises(SheetsError) as exc:
        client.read_tab("", "Nope", 1)
    assert "RAW_Patients" not in str(exc.value)


def test_other_file_types_are_refused():
    with pytest.raises(SheetsError, match="Only .xlsx and .csv"):
        UploadedWorkbookClient("data.xls", b"not really")
    with pytest.raises(SheetsError, match="empty"):
        UploadedWorkbookClient("data.csv", b"")


def test_a_broken_xlsx_fails_with_advice_not_a_traceback():
    with pytest.raises(SheetsError, match="could not be read"):
        UploadedWorkbookClient("data.xlsx", b"this is not a zip archive")


# ------------------------------------------------------------------ the upload route


@pytest.fixture
def admin_client(client):
    with client.app.state.db.session() as db:
        email = make_user(db, email="upload.admin@example.invalid", role=Role.ADMIN).email
    sign_in(client, email)
    return client


@pytest.fixture
def upload_source(admin_client):
    """An upload source with its therapist, mapped and ready."""
    with admin_client.app.state.db.session() as db:
        therapist = Therapist(
            display_name="Zebra Historical", employment_type=EmploymentType.SALARIED_BENEFITS
        )
        db.add(therapist)
        db.flush()
        db.add(TherapistAlias(therapist_id=therapist.id, alias="ZEBRA", source=AliasSource.MANUAL))
        source = DataSource(
            label="Q1 2025 (historical)",
            provider=SourceProvider.UPLOAD,
            tab_name=CSV_TAB_NAME,
            header_row=1,
            column_mapping=dict(MAPPING),
            active=True,
        )
        db.add(source)
        db.flush()
        return source.id


def upload(admin_client, source_id: int, *, mode: str, content: bytes = CSV_CONTENT):
    token = token_from(admin_client.get(f"/admin/sources/{source_id}").text)
    return admin_client.post(
        f"/admin/sources/{source_id}/upload",
        data={"csrf_token": token, "mode": mode},
        files={"file": ("history.csv", content, "text/csv")},
        follow_redirects=False,
    )


def visit_count(admin_client, source_id: int) -> int:
    with admin_client.app.state.db.session() as db:
        return db.execute(
            select(func.count(Visit.id)).where(Visit.source_id == source_id)
        ).scalar_one()


def test_a_dry_run_upload_validates_and_writes_nothing(admin_client, upload_source):
    response = upload(admin_client, upload_source, mode="dry_run")
    assert response.status_code == 303
    assert "/runs/" in response.headers["location"]
    assert visit_count(admin_client, upload_source) == 0


def test_a_live_upload_imports_through_the_normal_pipeline(admin_client, upload_source):
    response = upload(admin_client, upload_source, mode="live")
    assert response.status_code == 303
    assert visit_count(admin_client, upload_source) == 2

    # Re-uploading the same file changes nothing: same global visit identity.
    upload(admin_client, upload_source, mode="live")
    assert visit_count(admin_client, upload_source) == 2


def test_an_unmapped_source_refuses_the_upload(admin_client, upload_source):
    with admin_client.app.state.db.session() as db:
        db.get(DataSource, upload_source).column_mapping = {}
    response = upload(admin_client, upload_source, mode="live")
    assert response.status_code == 303
    assert "Map+and+save" in response.headers["location"].replace("%20", "+")
    assert visit_count(admin_client, upload_source) == 0


def test_a_bad_file_flashes_the_error(admin_client, upload_source):
    response = upload(admin_client, upload_source, mode="live", content=b"")
    assert response.status_code == 303
    assert "empty" in response.headers["location"]
    assert visit_count(admin_client, upload_source) == 0


def test_a_viewer_cannot_upload(client, upload_source):
    with client.app.state.db.session() as db:
        email = make_user(db, email="viewer.upload@example.invalid", role=Role.VIEWER).email
    sign_in(client, email)
    response = client.post(
        f"/admin/sources/{upload_source}/upload",
        data={"mode": "live"},
        files={"file": ("history.csv", CSV_CONTENT, "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code in (303, 403)
    assert visit_count(client, upload_source) == 0
