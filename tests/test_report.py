"""Phase F reporting: labels always attached, no fabrication, no PHI."""
import json
import re
import shutil
from pathlib import Path

from src.config import Config
from src.reconcile import RosterSnapshot
from src.report import (
    build_report,
    collect_sources,
    render_report_md,
    report_summary,
    main,
)

FIXTURES = Path(__file__).parent / "fixtures"
PATIENT_TOKENS = [f"P00{i}" for i in range(1, 8)]


def _data_dir(tmp_path):
    d = tmp_path / "raw"
    d.mkdir()
    for f in FIXTURES.glob("*_SYNTHETIC.csv"):
        shutil.copy(f, d / f.name)
    return d


def test_empty_data_dir_reports_no_data_and_does_not_fabricate(tmp_path):
    empty = tmp_path / "raw"
    empty.mkdir()
    rep = build_report(empty, Config())
    assert rep.has_data is False
    md = render_report_md(rep)
    assert "NO DATA" in md
    assert "export-request.md" in md
    assert "does not fabricate" in " ".join(rep.notes)


def test_all_four_sources_discovered(tmp_path):
    d = _data_dir(tmp_path)
    sources = collect_sources(d)
    assert set(sources) == {"charges", "appointments", "statements", "documentation"}


def test_report_attaches_maturity_and_reconciliation_to_headlines(tmp_path):
    d = _data_dir(tmp_path)
    rep = build_report(d, Config())
    md = render_report_md(rep)
    # The target period is immature, and the report must say so prominently.
    assert rep.headline_label() == "INCOMPLETE"
    assert "INCOMPLETE" in md
    assert "do NOT compare it against a matured period" in md
    # Headline table carries both columns.
    assert "| headline | value | maturity | reconciliation | sources |" in md
    for headline in ("session_count", "expected_revenue", "collected_revenue",
                     "unique_patients", "active_providers"):
        assert headline in md


def test_report_shows_all_three_dollar_measures_separately(tmp_path):
    rep = build_report(_data_dir(tmp_path), Config())
    md = render_report_md(rep)
    assert "billed (context)" in md and "expected (HEADLINE)" in md
    assert "710.00" in md and "479.50" in md and "240.00" in md


def test_report_shows_decomposition_summing_to_change(tmp_path):
    rep = build_report(_data_dir(tmp_path), Config())
    for w in rep.windows:
        d = w.decomposition
        if d.defined:
            assert d.volume_effect + d.rate_effect + d.mix_effect == d.total_change
    md = render_report_md(rep)
    assert "volume" in md and "rate" in md and "mix" in md


def test_report_never_leaks_patient_identifiers(tmp_path):
    rep = build_report(_data_dir(tmp_path), Config())
    md = render_report_md(rep)
    for token in PATIENT_TOKENS:
        assert token not in md, f"PHI token {token} leaked into the report"


def test_report_reconciliation_is_not_claimed_clean_when_open(tmp_path):
    rep = build_report(_data_dir(tmp_path), Config())
    assert rep.reconciliation.clean is False
    md = render_report_md(rep)
    assert "OPEN ITEMS" in md
    assert "never averaged" in md


def test_roster_snapshot_findings_reach_the_report(tmp_path):
    d = _data_dir(tmp_path)
    roster = RosterSnapshot(
        active=frozenset({"dralicerivera", "drbobnguyen", "drcarolsmith", "drghostly"}),
    )
    rep = build_report(d, Config(), roster=roster)
    prov = rep.reconciliation.by_headline("active_providers")
    assert "zero_sessions" in " ".join(prov.notes)
    assert "drghostly" in " ".join(prov.notes)


def test_json_summary_is_deterministic_and_aggregate_only(tmp_path, capsys):
    d = _data_dir(tmp_path)
    rc = main(["--data-dir", str(d), "--period", "2026-07", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["has_data"] is True
    assert payload["maturity_label"] == "INCOMPLETE"
    assert payload["incomplete_periods"] == ["2026-07"]
    assert payload["reconciliation"]["clean"] is False
    assert payload["grains"]["billable_encounters"] == 5
    for token in PATIENT_TOKENS:
        assert token not in out

    main(["--data-dir", str(d), "--period", "2026-07", "--json"])
    assert capsys.readouterr().out == out   # byte-identical repeat


def test_cli_writes_report_and_reconciliation_files(tmp_path):
    d = _data_dir(tmp_path)
    rpt = tmp_path / "report.md"
    rec = tmp_path / "reconciliation.md"
    rc = main(["--data-dir", str(d), "--period", "2026-07",
               "--out", str(rpt), "--reconciliation-out", str(rec), "--json"])
    assert rc == 0
    report_text = rpt.read_text(encoding="utf-8")
    recon_text = rec.read_text(encoding="utf-8")
    assert "Practice Health Report" in report_text
    assert "# Reconciliation" in recon_text
    # Both artifacts are PHI-free.
    for text in (report_text, recon_text):
        for token in PATIENT_TOKENS:
            assert token not in text


def test_missing_optional_source_is_noted_not_silently_ignored(tmp_path):
    d = tmp_path / "raw"
    d.mkdir()
    shutil.copy(
        FIXTURES / "ChargesHistoryDetailProviderPatientCode_SYNTHETIC.csv",
        d / "ChargesHistoryDetailProviderPatientCode_SYNTHETIC.csv",
    )
    rep = build_report(d, Config())
    assert rep.has_data is True
    joined = " ".join(rep.notes)
    for missing in ("appointments", "statements", "documentation"):
        assert missing in joined
