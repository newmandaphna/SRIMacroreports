# SRI Practice Health Analytics

A self-contained, PHI-safe analytics pipeline over Valant practice exports. It
measures **practice health** -- session volume, expected vs. collected revenue,
claim-lag maturity, and year-over-year volume/rate/mix -- from raw Valant reports,
without ever letting patient-grain data enter version control.

> **This repository is PUBLIC.** The one inviolable rule: **nothing at patient
> grain is ever committed.** Raw exports live in `data/raw/`, which is gitignored
> and verified so. Every committed artifact is aggregate.

## Status

| Phase | What it does | State |
|---|---|---|
| **A** | Discover & profile raw exports; emit data dictionary or export request | **implemented** (this repo) |
| **B** | Session ledger (4 grains, derived add-ons, group dual-view, voids, dedupe, appts-vs-charges gap) | **implemented** (`src/sessions.py`) |
| **C** | Dollar ladder (billed / expected / collected) + claim-lag maturity | **implemented** (`src/dollars.py`) |
| **D** | Calendar normalization + volume/rate/mix decomposition + YoY windows | **implemented** (`src/decomposition.py`) |
| E | Reconciliation harness (every headline from >=2 sources) | designed, not yet built |
| F | Reporting | designed, not yet built |

Phase A is runnable today. Phases B-F have their definitions frozen in
`ASSUMPTIONS.md` and build on the Phase A loaders.

## Two things must be settled before any headline number is trusted

1. **Real data.** `data/raw/` is empty. The pipeline does **not** fabricate data.
   `docs/export-request.md` is the exact pull list (four Valant reports, date
   basis = **date of service**, range 2024-01-01 -> 2026-07-31). Drop the files
   in `data/raw/` and re-run Phase A.
2. **RULES.md reconciliation.** Provider roster, period naming, and CPT/HCPCS
   handling are meant to *reuse* the compensation engine's settled `RULES.md`
   rather than reinvent them. That file was not reachable when this was built, so
   those definitions currently use **documented defaults** flagged `[RULES.md]`
   in `ASSUMPTIONS.md`, and `config.rules_md_reconciled` is `False`. Numbers stay
   provisional until it is reconciled.

## Layout

```
src/
  config.py       # machine-readable form of ASSUMPTIONS.md defaults
  money.py        # Decimal money parsing; missing != zero; fails loudly
  periods.py      # half-open [start, next) period boundaries; prior-year mapping
  loaders.py      # Valant loaders: programmatic header detection, no pandas
  profile_raw.py  # Phase A profiler (aggregate-only; PHI never emitted)
docs/
  export-request.md      # what to pull when data/raw is empty
  data-dictionary.md     # expected schema + profiler-regenerated observed inventory
data/raw/         # raw Valant exports land here -- GITIGNORED, never committed
tests/            # pytest suite + synthetic (non-PHI) fixtures
ASSUMPTIONS.md    # every definitional default, why, and what must reconcile to RULES.md
```

## Run it

```bash
# Profile whatever is in data/raw/ and (re)write the data dictionary's Part 1.
python -m src.profile_raw            # or: python src/profile_raw.py
python -m src.profile_raw --no-write # profile without touching docs
python -m src.profile_raw --json     # deterministic, aggregate-only JSON (for CI/tools)

# With data/raw/ empty, it stops cleanly and points you at docs/export-request.md.
```

## CI & the PHI pre-commit hook

- **CI** (`.github/workflows/ci.yml`) runs the suite on push and PR across a matrix
  of Python 3.11/3.12 × {stdlib-only, openpyxl}, so **both loader branches** (the
  CSV/stdlib path and the optional `.xlsx` path) are exercised.
- **Pre-commit PHI hook.** Enable it once per clone so a patient-grain leak can
  never be committed (this repo is public — a bad commit is a PHI incident):

  ```bash
  git config core.hooksPath .githooks       # plain git, no dependencies
  # — or, with the pre-commit framework:
  pip install pre-commit && pre-commit install
  ```

  The hook runs `tests/test_phi_guard.py` and **blocks the commit on failure**
  (it fails closed).

## License

Proprietary — see [`LICENSE`](LICENSE). All rights reserved; no reuse license is
granted. (Placed as the conservative default for an internal, health-adjacent
tool; swap for MIT/Apache-2.0 if open licensing is intended.)

## Test it

```bash
pip install pytest        # openpyxl too, only if you need the .xlsx path
pytest                    # stdlib-only core; fixtures are synthetic, never real PHI
```

Tests cover: Decimal money (missing != zero, loud failure on garbage), half-open
period boundaries (no datetime in two periods), Valant preamble/header detection
(content-based, not `skiprows=3`), loud failure on blank/duplicate/missing
columns, the empty-`data/raw` stop path, and a guarantee that **no synthetic
patient token ever appears** in a profiler-produced artifact.

## Design commitments (from `ASSUMPTIONS.md`)

- **PHI never committed.** Patient keys live in memory only; only counts are
  emitted. `data/raw/` is gitignored and proven so.
- **Money is `Decimal`, never float.** Rounding only at presentation.
- **No silent fallbacks.** A missing column, unparseable date, off-roster
  provider, or unrecognized code fails loudly or lands in a counted exceptions
  bucket -- a bad row never becomes a silent zero.
- **Deterministic.** Same inputs -> byte-identical outputs. No wall-clock or
  randomness in the analysis path; any random data is synthetic, seeded, and
  test-only.
