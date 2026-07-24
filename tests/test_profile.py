"""Phase A profiler: aggregates only, no fabrication, PHI never emitted."""
import shutil
from pathlib import Path

from src.config import Config
from src.profile_raw import (
    check_prior_year_coverage,
    profile_file,
    render_part1,
    run,
)

FIXTURES = Path(__file__).parent / "fixtures"
CHARGES = FIXTURES / "ChargesHistoryDetailProviderPatientCode_SYNTHETIC.csv"

# Synthetic patient tokens present in the fixtures. None of these may ever appear
# in a profiler-produced artifact -- that is the PHI guarantee under test.
PATIENT_TOKENS = ["P001", "P002", "P003", "P004", "P005", "P006", "P007"]


def test_empty_raw_dir_stops_and_does_not_fabricate(tmp_path):
    data_dir = tmp_path / "raw"
    data_dir.mkdir()
    (data_dir / ".gitkeep").write_text("", encoding="utf-8")
    dict_path = tmp_path / "data-dictionary.md"

    summary = run(data_dir=data_dir, dict_path=dict_path, config=Config(), write=True)

    assert summary["status"] == "EMPTY"
    assert summary["n_files"] == 0
    text = dict_path.read_text(encoding="utf-8")
    assert "EMPTY" in text
    assert "_none present_" in text


def test_profile_charges_aggregates(tmp_path):
    fp = profile_file(str(CHARGES))
    assert fp.family == "charges"
    assert fp.n_rows == 8
    # Composite charge-line grain is 1:1 in the fixture (add-ons are distinct CPTs).
    assert fp.grain_confirmed is True
    assert fp.n_distinct_keys == 8
    # DOS extent spans the fixture's min/max service dates.
    assert fp.dos_min.startswith("2024-02-11")
    assert fp.dos_max.startswith("2026-07-22")
    # Money columns are recognized as decimal, not string.
    dtypes = {c.name: c.dtype for c in fp.columns}
    assert dtypes["Billed Amount"] == "decimal"
    assert dtypes["Expected Amount"] == "decimal"
    assert dtypes["Date of Service"] == "date"


def test_prior_year_coverage_pass_and_fail():
    fp = profile_file(str(CHARGES))
    ok, _ = check_prior_year_coverage([fp], "2026-07")
    assert ok is True  # data reaches back to 2024-02, well before 2025-07 floor
    ok2, _ = check_prior_year_coverage([fp], "2024-06")
    assert ok2 is False  # would need DOS history before 2023-06


def test_rendered_dictionary_contains_no_patient_tokens(tmp_path):
    data_dir = tmp_path / "raw"
    data_dir.mkdir()
    shutil.copy(CHARGES, data_dir / CHARGES.name)
    dict_path = tmp_path / "data-dictionary.md"

    run(data_dir=data_dir, dict_path=dict_path, config=Config(), write=True)
    text = dict_path.read_text(encoding="utf-8")

    for token in PATIENT_TOKENS:
        assert token not in text, f"PHI token {token!r} leaked into the data dictionary"
    # Provider names are permitted in outputs, but patient identifiers are not;
    # confirm the profiler emitted structure (column names) without row values.
    assert "Expected Amount" in text
    assert "$" not in text  # no money *values* rendered, only dtype labels


def test_profile_run_summary_flags_unreconciled_rules():
    summary = run(
        data_dir=FIXTURES,  # contains recognized fixtures + coverage
        dict_path=Path("/dev/null"),
        config=Config(),
        write=False,
    )
    assert summary["rules_md_reconciled"] is False
    assert summary["n_recognized"] >= 1
