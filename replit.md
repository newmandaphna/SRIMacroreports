# SRI Practice Dashboard

Internal practice management reporting for SRI Psychological Services. Tracks financial performance, therapist utilization, and room utilization from session-level data synced from a Google Sheet.

**This application holds PHI.** See `SECURITY.md` before touching data, logging, or access control.

## Before you commit

Run the same three checks CI runs, from the repository root:

    ruff check .
    ruff format --check .
    pytest

All three must pass. Better, install the hook once and let it run itself:

    pip install pre-commit
    pre-commit install

See `AGENTS.md` for the rest. Every red build in this repository so far was a
commit that skipped this step, not a real defect in the code.

## Stack

- Python 3.11, FastAPI, Jinja2, htmx
- PostgreSQL (Replit managed), via SQLAlchemy + Alembic. No application level encryption at
  rest: see SECURITY.md section 5.2
- Chart.js 4 from CDN
- No SPA framework

## How to run

### 1. Set required secrets in Replit Secrets

| Secret | Notes |
|---|---|
| `SESSION_SECRET_KEY` | At least 32 chars. Generate: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `DATABASE_URL` | Injected automatically by the Replit PostgreSQL database. Nothing to set by hand. |
| `ADMIN_INITIAL_PASSWORD` | Password for the seeded admin account. You will be forced to change it on first login. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full JSON key file content for a Google Service Account (see below). Required to sync from Google Sheets. |

#### Setting up Google Sheets access

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project (or use an existing one).
2. Enable the **Google Sheets API** and **Google Drive API** for that project.
3. Create a **Service Account** (IAM & Admin → Service Accounts → Create).
4. Under the service account, go to **Keys → Add Key → Create new key → JSON**. Download the `.json` file.
5. **Share the Google Sheet** (or the Drive folder containing it) with the service account's email address (shown in the JSON as `client_email`) as **Viewer**.
6. Copy the entire contents of the downloaded `.json` file and set it as the `GOOGLE_SERVICE_ACCOUNT_JSON` secret in Replit Secrets.

Once the secret is set, go to **Admin → Data Sources**, create a new Google Sheets source, paste the sheet URL, pick the tab, map the columns, and run a sync.

### 2. Set required environment variables

| Variable | Notes |
|---|---|
| `ADMIN_EMAIL` | Email address for the first admin account |

Optional variables (defaults shown):

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
FEATURE_PATIENT_FUNNEL=false
```

### 3. Start the workflow

The run command is:

```bash
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 5000 --reload
```

On first boot the app runs `alembic upgrade head` and seeds the admin account automatically.

### Useful URLs

- `/` - redirects to sign in
- `/healthz` - liveness check
- `/readyz` - readiness (confirms encrypted database opens)
- `/admin/users` - user management (admin only)
- `/admin/audit` - audit log (admin only)

## Current phase

Phase 1 complete: auth, roles, module grants, user administration, audit log, session timeout, admin seeding.

Phase 2 (data model + Google Sheets sync) is next.

## Run command

```
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 5000
```

## User preferences
