"""The load-bearing PHI guarantee, under test (Gate 1 defect #6).

README.md and ASSUMPTIONS.md claim data/raw/ is "gitignored and proven so" and
that only aggregates are committed. This test is that proof, and it must never be
skipped: (a) data/raw payloads are gitignored, (b) nothing but .gitkeep is tracked
under data/, (c) no synthetic patient token appears in any *shipped* tracked file.
"""
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The synthetic patient tokens used in tests/fixtures/. They are allowed to exist
# in tests/ (they are synthetic, not real PHI), but must never leak into a shipped
# artifact (docs, source, generated output).
SYNTHETIC_PATIENT_TOKENS = [f"P00{i}" for i in range(1, 8)]


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True,
    )


def test_data_raw_payload_is_gitignored():
    # A representative real-export path must be ignored (patterns, not existence).
    for rel in (
        "data/raw/ChargesHistoryDetailProviderPatientCode_REAL.csv",
        "data/raw/PatientStatement_REAL.xlsx",
        "data/whatever.csv",
    ):
        r = _git("check-ignore", "-v", rel)
        assert r.returncode == 0, f"{rel} is NOT gitignored -- PHI could be committed"


def test_nothing_tracked_under_data_except_gitkeep():
    r = _git("ls-files", "data")
    tracked = sorted(x for x in r.stdout.splitlines() if x.strip())
    assert tracked == ["data/raw/.gitkeep"], (
        f"unexpected tracked files under data/: {tracked}"
    )


def test_no_patient_token_in_shipped_tracked_files():
    r = _git("ls-files")
    files = [f for f in r.stdout.splitlines() if f.strip()]
    assert files, "git ls-files returned nothing -- is this a git checkout?"
    # tests/ legitimately contains synthetic tokens (fixtures + token lists);
    # everything shipped as an artifact must be clean.
    shipped = [f for f in files if not f.startswith("tests/")]
    leaks = []
    for f in shipped:
        try:
            text = (REPO / f).read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeError):
            continue
        for tok in SYNTHETIC_PATIENT_TOKENS:
            if tok in text:
                leaks.append((f, tok))
    assert not leaks, f"patient token leaked into shipped files: {leaks}"
