# SRI Practice Dashboard

Internal practice management reporting for SRI Psychological Services. Tracks financial
performance and therapist utilization from session-level data synced from the practice's
quarterly Google Sheet.

**This application holds PHI.** See `SECURITY.md` before touching data, logging, or
access control. See `AGENTS.md` for the working rules; the ones agents break most are:
never commit real patient names anywhere (test data uses Patient AA / PATAA), never
delete or rewrite security code you did not fully trace, and keep commits to one
concern.

## Before you commit

Run the same three checks CI runs, from the repository root:

    ruff check .
    ruff format --check .
    pytest

All three must pass. Better, install the hook once and let it run itself:

    pip install pre-commit
    pre-commit install

Every red build in this repository so far was a commit that skipped this step, not a
real defect in the code.

## Stack

- Python 3.11, FastAPI, Jinja2, server-rendered pages. No SPA framework, no htmx: the
  few dynamic behaviours are small vanilla scripts in `app/static/js/`.
- PostgreSQL (Replit managed), via SQLAlchemy + Alembic. No application level
  encryption at rest: see SECURITY.md section 5.2.
- Chart.js 4 from CDN, reading design tokens from `app/static/css/tokens.css`.

## How to run

### 1. Secrets, in Replit Secrets

| Secret | Notes |
|---|---|
| `DATABASE_URL` | Injected automatically by the Replit PostgreSQL database. Nothing to set by hand. |
| `ADMIN_EMAIL` | Email address for the first admin account. |
| `ADMIN_INITIAL_PASSWORD` | Password for the seeded admin. A change is forced on first login, and after that the app never resets it. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full JSON key file content for a Google Service Account (see below). Required before a Google Sheets sync can run. |
| `SESSION_SECRET_KEY` | Optional, reserved for signing. Nothing signs with it yet. If set, at least 32 characters. |

#### Setting up Google Sheets access

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project (or use an existing one).
2. Enable the **Google Sheets API** and **Google Drive API** for that project.
3. Create a **Service Account** (IAM & Admin, Service Accounts, Create).
4. Under the service account, go to **Keys, Add Key, Create new key, JSON**. Download the `.json` file.
5. **Share the Google Sheet** (or the Drive folder containing it) with the service account's email address (shown in the JSON as `client_email`) as **Viewer**.
6. Copy the entire contents of the downloaded `.json` file and set it as the `GOOGLE_SERVICE_ACCOUNT_JSON` secret in Replit Secrets.

Once the secret is set, go to **Admin, Data Sources**, create a Google Sheets source,
paste the sheet URL, pick the tab (RAW_ tabs are blocked on purpose), map the columns,
save, dry run, then sync.

### 2. Optional environment variables (defaults shown)

```
ENVIRONMENT=development
DEBUG=false
SESSION_TIMEOUT_MINUTES=15
SESSION_WARNING_MINUTES=13
BENEFITS_SESSION_THRESHOLD=25
CPT_EXCLUSIONS=99998,99999,QBCHK,FORM,PRO BONO
WEEK_START_DAY=monday
APP_TIMEZONE=America/New_York
FEATURE_ROOM_UTILIZATION=false
```

A deployed instance is treated as production automatically (Replit sets
`REPLIT_DEPLOYMENT`), so secure cookies and HSTS do not depend on anyone remembering to
set `ENVIRONMENT=production`.

### 3. Run command

```bash
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 5000
```

On boot the app runs `alembic upgrade head` and seeds the admin account if it does not
exist, so a redeploy picks up new migrations by itself.

### Useful URLs

- `/` signed-in landing page
- `/healthz` liveness, `/readyz` readiness
- `/reports` overview, `/reports/financial`, `/reports/therapist-utilization`
- `/admin/sources` data sources and sync, `/admin/therapists` roster and aliases
- `/admin/config` thresholds and exclusions, `/admin/users`, `/admin/audit`

## Current state

Auth, roles, module grants, audit log, Google Sheets sync with a strict column
allowlist, import error review with supersession, financial and utilization reports,
and the practice roster seeding are all built and tested. The importer never creates a
therapist; unknown names reject to the errors queue and are resolved in the admin.

## User preferences
