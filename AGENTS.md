# Working in this repository

Read this before you commit.

## The gate

Run all three from the repository root:

    ruff check .
    ruff format --check .
    pytest

CI runs exactly these and nothing else, so a clean local run means a green
build. The one people forget is `ruff format --check`: `ruff check` passing
does not mean the file is formatted.

Install the hook once per clone and it runs itself on every commit:

    pip install pre-commit
    pre-commit install

## Keep the Ruff versions in step

Ruff is pinned to the same version in `requirements.txt` and in
`.pre-commit-config.yaml`. Different Ruff versions disagree about formatting,
so a mismatch lets the hook pass locally and the build fail anyway. If you
bump one, bump the other in the same commit.

## Do not let tests leak environment variables

`tests/conftest.py` writes `TEST_DATABASE_URL` into `os.environ` at import
time. `monkeypatch.delenv(name, raising=False)` records nothing when the name
is absent, so a test that deletes a variable which was never set there does
not get it restored, and the change leaks into every test that runs after it.
That is how one bad assumption became seventy errors in a single run.

If a test changes an environment variable, set it before you delete it, or
restore it explicitly. A test that passes on its own and fails in the full
suite is almost always this.

## Conventions

- No em dashes anywhere, in code, comments, UI copy, or documents. Use
  commas, colons, parentheses, or hyphens.
- Small commits, one concern each.
- Never invent realistic looking patient data. Synthetic fixtures are
  obviously fake on sight.
- This application holds PHI. Read `SECURITY.md` before changing anything
  that touches data, logging, or access control.

## Prefer a branch

`main` is what gets deployed. Push to a branch and open a pull request so CI
reports on the change before it reaches `main`, not after.
