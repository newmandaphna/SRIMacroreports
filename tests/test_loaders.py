"""Loader must absorb Valant quirks and fail loudly on malformed files."""
from pathlib import Path

import pytest

from src import loaders
from src.loaders import (
    HeaderNotFoundError,
    MissingColumnsError,
    assert_columns,
    classify_filename,
    detect_header_row,
    load_report,
)

FIXTURES = Path(__file__).parent / "fixtures"
CHARGES = FIXTURES / "ChargesHistoryDetailProviderPatientCode_SYNTHETIC.csv"
APPTS = FIXTURES / "AppointmentsPatientInfoByProviderThenDayThenFacility_SYNTHETIC.csv"


def test_classify_by_filename_prefix():
    assert classify_filename(str(CHARGES)) == "charges"
    assert classify_filename(str(APPTS)) == "appointments"
    assert classify_filename("SomethingRandom.csv") is None


def test_header_detected_past_three_line_preamble():
    # The header is on row index 3 (0-based) after the Textbox/date/blank preamble.
    header, records = load_report(str(CHARGES))
    assert header[0] == "Provider"
    assert "Date of Service" in header
    assert "Encounter ID" in header
    # 8 data rows in the fixture, preamble and header excluded.
    assert len(records) == 8


def test_no_hardcoded_skiprows_detection_is_content_based():
    rows = [
        ["Textbox12", "junk"],
        ["Date Range: x", ""],
        ["", ""],
        ["Provider", "Date of Service", "Code"],
        ["Dr X", "2026-07-01", "90837"],
    ]
    assert detect_header_row(rows, ("provider", "date of service", "code")) == 3


def test_header_not_found_raises():
    rows = [["a", "b"], ["c", "d"]]
    with pytest.raises(HeaderNotFoundError):
        detect_header_row(rows, ("provider", "date of service", "code"))


def test_appointments_header_no_preamble():
    header, records = load_report(str(APPTS))
    assert header[0] == "Provider"
    assert "Appointment Status" in header
    assert len(records) == 6


def test_assert_columns_missing_fails_loudly():
    header, _ = load_report(str(CHARGES))
    assert assert_columns(header, ["Provider", "Expected Amount"]) is True
    with pytest.raises(MissingColumnsError):
        assert_columns(header, ["Provider", "Diagnosis"])


def test_blank_header_column_rejected(tmp_path):
    bad = tmp_path / "ChargesHistoryDetailProviderPatientCode_bad.csv"
    bad.write_text(
        "pre,amble,x\n"
        "Provider,Date of Service,,Code\n"  # blank column between DOS and Code
        "Dr X,2026-07-01,,90837\n",
        encoding="utf-8",
    )
    with pytest.raises(MissingColumnsError):
        load_report(str(bad))


def test_duplicate_header_column_rejected(tmp_path):
    bad = tmp_path / "ChargesHistoryDetailProviderPatientCode_dupe.csv"
    bad.write_text(
        "Provider,Date of Service,Code,Code\n"
        "Dr X,2026-07-01,90837,90837\n",
        encoding="utf-8",
    )
    with pytest.raises(MissingColumnsError):
        load_report(str(bad))


def test_unrecognized_file_without_tokens_raises():
    with pytest.raises(loaders.LoaderError):
        load_report("Mystery.csv")
