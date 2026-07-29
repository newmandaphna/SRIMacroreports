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

## Known risk: commits that bypass CI

Replit's "Published your App" flow, and agents working inside the Replit
workspace, commit directly to `main` without CI running first. Both recent
main breakages arrived exactly this way:

- `tests/test_auth.py` kept importing `MAX_FAILED_LOGINS` after the lockout
  logic was deleted from `app/routers/auth.py`, which broke test collection
  outright with an ImportError.
- A vendored skill bundle under `.agents/` landed with 119 ruff violations,
  which failed the Lint step before pytest ever ran, so every push anywhere
  went red for a reason that had nothing to do with the app.

The guard is a branch protection rule on `main` requiring the CI check to
pass before anything lands, direct pushes included. That is a repository
setting only the owner can turn on: GitHub, Settings, Branches, add a rule
for `main`, require status checks, select the CI workflow. Until it is on,
treat any red CI immediately after a Replit publish as probably caused by
the publish, and check the newest commits on `main` first.
