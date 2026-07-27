# SRI Practice Dashboard

Internal practice management reporting for SRI Psychological Services. Tracks financial performance, therapist utilization, and room utilization from session-level data synced from a Google Sheet.

**This application holds PHI.** See `SECURITY.md` before touching data, logging, or access control.

## Stack

- Python 3.11, FastAPI, Jinja2, htmx
- SQLite with SQLCipher (encrypted at rest), managed via SQLAlchemy + Alembic
- Chart.js 4 from CDN
- No SPA framework

## How to run

### 1. Set required secrets in Replit Secrets

| Secret | Notes |
|---|---|
| `SESSION_SECRET_KEY` | At least 32 chars. Generate: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `DATABASE_ENCRYPTION_KEY` | Encrypts the SQLite database. Back this up separately -- losing it means losing the database. |
| `ADMIN_INITIAL_PASSWORD` | Password for the seeded admin account. You will be forced to change it on first login. |

### 2. Set required environment variables

| Variable | Notes |
|---|---|
| `ADMIN_EMAIL` | Email address for the first admin account |

Optional variables (defaults shown):

```
ENVIRONMENT=development
DEBUG=false
DATABASE_PATH=data/sri_dashboard.db
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
