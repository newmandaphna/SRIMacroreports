"""Gate 1 defect fixes (defects #1-#5, #7). One area per class."""
import re
from pathlib import Path

import pytest

from src import loaders
from src.loaders import (
    MissingColumnsError,
    is_code_column,
    resolve_dos_column,
    resolve_posting_column,
)
from src.money import MoneyParseError, parse_money, value_shape
from src.profile_raw import _classify_column, _profile_column, profile_file, run
from src.config import Config

FIXTURES = Path(__file__).parent / "fixtures"


# --- Defect #1: DOS column must never fall back to a posting/appointment date ---

def test_dos_never_selects_posting_date_for_appointments():
    # A loose "any column containing 'date'" fallback would wrongly pick Posting Date.
    header = ["Provider", "Posting Date", "Appointment Date", "Status"]
    assert resolve_dos_column("appointments", header) == "Appointment Date"


def test_statements_dos_and_posting_are_distinct():
    header = ["GroupingLevel1", "ChargeDateOfService", "StatementDate",
              "InsurancePayments", "PatientPayments"]
    dos = resolve_dos_column("statements", header)
    posting = resolve_posting_column("statements", header)
    assert dos == "ChargeDateOfService"
    assert posting == "StatementDate"
    assert dos != posting


def test_missing_dos_raises_not_guesses():
    header = ["Provider", "Posting Date", "Status"]  # no DOS at all
    with pytest.raises(MissingColumnsError):
        resolve_dos_column("charges", header)


# --- Defect #2: an unrecognized file must not crash the run ---

def test_unrecognized_file_profiles_into_bucket(tmp_path):
    mystery = tmp_path / "SomethingValantNeverMade.csv"
    mystery.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    fp = profile_file(str(mystery))  # must not raise
    assert fp.family is None
    assert "exceptions bucket" in " ".join(fp.notes)


def test_run_survives_a_mix_of_good_and_unrecognized(tmp_path):
    import shutil
    data = tmp_path / "raw"
    data.mkdir()
    shutil.copy(FIXTURES / "ChargesHistoryDetailProviderPatientCode_SYNTHETIC.csv",
                data / "ChargesHistoryDetailProviderPatientCode_SYNTHETIC.csv")
    (data / "Mystery.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    summary = run(data_dir=data, dict_path=tmp_path / "d.md", config=Config(), write=True)
    assert summary["n_unrecognized"] == 1
    assert summary["n_recognized"] == 1


# --- Defect #3: one dirty cell must not downgrade a whole column ---

def test_one_dirty_cell_does_not_kill_a_date_column():
    cells = ["2026-07-01"] * 19 + ["N/A"]
    dtype, nonconf = _classify_column(cells)
    assert dtype == "date"
    assert nonconf == 1


def test_genuinely_mixed_column_is_string():
    cells = ["2026-07-01", "hello", "42", "world", "n/a"]
    dtype, _ = _classify_column(cells)
    assert dtype == "string"


def test_money_column_with_a_few_whole_dollars_is_decimal():
    cells = ["$10.50", "20.25", "30", "40.00", "5.75"]
    dtype, nonconf = _classify_column(cells)
    assert dtype == "decimal"
    assert nonconf == 0


# --- Defect #4: PHI must never escape through exception messages ---

def test_money_error_redacts_the_value():
    phi = "Jane Doe, DOB 1990-01-01"
    with pytest.raises(MoneyParseError) as ei:
        parse_money(phi)
    msg = str(ei.value)
    assert phi not in msg
    assert "Jane" not in msg and "1990" not in msg
    assert "len=" in msg  # only a shape leaks


def test_value_shape_hides_the_value():
    assert "Secret" not in value_shape("Secret123")
    assert value_shape("Secret123") == "len=9 digit+alpha"
    assert value_shape("") == "len=0 empty"


# --- Defect #5: CPT/HCPCS codes are opaque strings, never int ---

def test_cpt_column_is_code_not_int():
    assert is_code_column("charges", "CPT/HCPCS Code") is True
    col = _profile_column("CPT/HCPCS Code", ["90837", "90834", "0362T"], "charges")
    assert col.dtype == "code"  # not "int" -- leading zeros / T-codes survive


def test_non_code_numeric_column_still_int():
    col = _profile_column("Units", ["1", "2", "1"], "charges")
    assert col.dtype == "int"


# --- Defect #7: test fragility + xlsx coverage ---

def test_xlsx_round_trip():
    openpyxl = pytest.importorskip("openpyxl")
    import tempfile
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Provider", "Appointment Date", "Facility", "Appointment Status"])
    ws.append(["Dr Alice Rivera", "2026-07-02", "Main Office", "Kept"])
    ws.append(["Dr Bob Nguyen", "2026-07-15", "Main Office", "No-Show"])
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "AppointmentsPatientInfoByProviderThenDayThenFacility_x.xlsx"
        wb.save(p)
        header, records = loaders.load_report(str(p))
        assert header[0] == "Provider"
        assert "Appointment Status" in header
        assert len(records) == 2


# --- Gate 2: --json output mode is valid, deterministic JSON ---

def test_json_output_mode(capsys, tmp_path):
    import json
    from src.profile_raw import main
    rc = main(["--data-dir", str(tmp_path), "--no-write", "--json",
               "--period", "2026-07"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)  # must be valid JSON, nothing else on stdout
    assert payload["status"] == "EMPTY"
    assert payload["target_period"] == "2026-07"
    assert payload["rules_md_reconciled"] is True

    # Deterministic: same input -> byte-identical output.
    main(["--data-dir", str(tmp_path), "--no-write", "--json", "--period", "2026-07"])
    out2 = capsys.readouterr().out
    assert out == out2

