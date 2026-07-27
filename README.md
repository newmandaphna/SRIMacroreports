# SRI Practice Dashboard

Internal practice management reporting for SRI Psychological Services: financial
performance, therapist utilization, room utilization, and (later, gated) a patient level
funnel, all derived from one imported dataset of session level rows synced from the
quarterly Q sheet.

**This application holds PHI.** Read [SECURITY.md](SECURITY.md) before changing anything
that touches data, logging, or access control. Read [ASSUMPTIONS.md](ASSUMPTIONS.md) for
every definitional choice, including the places where observed data forced a deviation
from the build specification.

Current state: **Phase 0, scaffold.** The app boots, proves its configuration, opens an
encrypted database, and serves a health endpoint plus a placeholder shell built on the
real design tokens. There is no authentication and no data model yet.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in SESSION_SECRET_KEY and DATABASE_ENCRYPTION_KEY. Both are required.
python -c "import secrets; print(secrets.token_urlsafe(48))"   # for each

uvicorn app.main:create_app --factory --reload
```

Then:

- `http://127.0.0.1:8000/` the shell
- `http://127.0.0.1:8000/healthz` liveness
- `http://127.0.0.1:8000/readyz` readiness, which proves the encrypted database opens

Tests and lint:

```bash
pytest
ruff check . && ruff format --check .
```

Migrations (there are none yet; the data model lands in Phase 2):

```bash
alembic revision --autogenerate -m "description"
alembic upgrade head
```

Note that `alembic.ini` deliberately carries no `sqlalchemy.url`. The database is
encrypted and needs its key applied as a `PRAGMA` before any other statement, so
`migrations/env.py` builds the engine from application settings instead. A URL in the
ini file could not carry the key without committing the key.

---

## Configuration

All configuration comes from the environment. In Replit these belong in **Secrets**, not
in a file. `.env` is gitignored and `.env.example` never holds a real value.

| Variable | Required | Notes |
| --- | --- | --- |
| `SESSION_SECRET_KEY` | yes | Minimum 32 characters. Signs session cookies. |
| `DATABASE_ENCRYPTION_KEY` | yes | SQLCipher key. **Losing it means losing the database.** |
| `DATABASE_PATH` | no | Defaults to `data/sri_dashboard.db`. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | for sync | Full service account JSON, one line. |
| `ADMIN_EMAIL`, `ADMIN_INITIAL_PASSWORD` | Phase 1 | Seeds one admin. Password change forced on first login. |
| `ENVIRONMENT` | no | `development`, `test`, or `production`. |
| `DEBUG` | no | Refused in production. Debug output can carry PHI. |
| `SESSION_TIMEOUT_MINUTES` | no | Default 15, idle, enforced server side. |
| `SESSION_WARNING_MINUTES` | no | Default 13. Must be less than the timeout. |
| `BENEFITS_SESSION_THRESHOLD` | no | Default 25 sessions per week. Editable by an admin later. |
| `WEEK_START_DAY` | no | `monday` (default) or `sunday`. |
| `APP_TIMEZONE` | no | Default `America/New_York`. |
| `FEATURE_ROOM_UTILIZATION` | no | Default off, per the build specification. |
| `FEATURE_PATIENT_FUNNEL` | no | Default off. Phase 6, gated on your confirmation. |

A missing required secret is a startup failure with a named error. There is no fallback,
and in particular there is no fallback to an unencrypted database.

### Rotating the database encryption key

SQLCipher supports rekeying in place. Do it with the service stopped and a verified
backup in hand, and update the secret before restarting.

```bash
# service stopped, backup taken
python - <<'PY'
import sqlcipher3
conn = sqlcipher3.connect("data/sri_dashboard.db")
conn.execute("PRAGMA key = 'OLD_KEY'")
conn.execute("PRAGMA rekey = 'NEW_KEY'")
conn.close()
PY
# then set DATABASE_ENCRYPTION_KEY to the new value and restart
```

Backups of the encrypted file are worthless without the key, so the key must be escrowed
separately from the backups, by the practice, outside this system.

---

## Google service account setup

The app reads the quarterly Q sheet through a Google service account with read only
access to a single Drive folder. Sharing the **folder** rather than each sheet is what
makes quarterly rotation zero touch: next quarter's sheet inherits access automatically.

1. **Create a Google Cloud project.** In the Google Cloud Console, create a project, for
   example `sri-dashboard`.

2. **Enable two APIs** in that project: the **Google Sheets API** and the **Google Drive
   API**. Drive is needed to list the sheets in the folder; Sheets is needed to read them.

3. **Create a service account**, for example `sri-dashboard-reader`. Google gives it an
   address that looks like
   `sri-dashboard-reader@sri-dashboard.iam.gserviceaccount.com`.

4. **Create a JSON key** for that service account and download it. This file is a
   credential. Do not put it in the repository, do not email it, do not paste it into a
   chat.

5. **Put the JSON into Replit Secrets** as `GOOGLE_SERVICE_ACCOUNT_JSON`, the whole file
   contents as the value.

6. **Create a Drive folder**, for example `SRI Q Sheets`, and share **the folder** with
   the service account address as **Viewer**. Viewer, not Editor: the app must never be
   able to write to a Q sheet.

7. **Keep every quarter's sheet in that folder.** Q3 2026, Q4 2026, and so on. Each new
   sheet inherits the share, so adding a quarter in the app is just pasting its URL on
   the Data Sources page and picking a tab. No re-share, no downtime.

8. The app extracts the spreadsheet ID from the URL you paste and reads by ID.

### What the app is allowed to read

Only 18 columns, enforced server side (see SECURITY.md section 6). The `RAW_*` tabs in
the source workbook carry patient dates of birth, home and work emails, phone numbers,
and ZIP codes. The app blocks those tabs outright and never imports any of it.

---

## Architecture

```
app/
  config.py          environment configuration, fails loud on missing secrets
  db.py              SQLCipher engine, key application, encryption verification
  logging_setup.py   PHI scrubbing log filter and formatter
  middleware.py      security headers, CSP, safe error responses
  main.py            application factory, health endpoints, routes
  models/            SQLAlchemy models (Phase 1 and 2)
  templates/         Jinja2 server rendered pages
  static/css/        tokens.css defines every colour, space, and size; app.css uses them
  static/js/         charts.js, the one Chart.js wrapper every chart goes through
migrations/          Alembic, engine built from settings so the key stays out of the repo
tests/
```

- Python, FastAPI, Jinja2, htmx. No SPA framework.
- SQLAlchemy with Alembic from day one. SQLite with SQLCipher now, structured so
  PostgreSQL later is a configuration change.
- Chart.js 4 from CDN, wrapped in `static/js/charts.js`, which reads the design tokens
  off the document so charts and page chrome cannot drift apart.
- The Content Security Policy allows exactly one off origin source, the Chart.js CDN.

---

## Build phases

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Scaffold, dependencies, README, SECURITY.md, ASSUMPTIONS.md | **done** |
| 1 | Auth, roles, module grants, user administration, audit log, session timeout, seeding | next |
| 2 | Data model, Data Sources registry, sync engine with dry run and import errors | |
| 3 | Financial module and the Reports overview dashboard | |
| 4 | Therapist utilization: threshold config, status board, drill in, notes | |
| 5 | Room utilization behind its flag, manual upload path | |
| 6 | Patient funnel: AR aging, new patient volume, no show patterns. Gated. | |

Each phase stops for review before the next one starts.

---

## Conventions

- No em dashes anywhere: code, comments, UI copy, or documents. Commas, colons,
  parentheses, and hyphens instead.
- Small commits, one concern each.
- Every definitional choice goes into ASSUMPTIONS.md the moment it is made.
- Every Security Rule control goes into SECURITY.md with its implementation status.
- Never fabricate realistic looking patient data. Synthetic test patients are obviously
  fake: `Patient AA`, `Patient AB`, codes `PATAA`, `PATAB`.
- Where the build specification and the requirements document disagree, the build
  specification wins, and the conflict gets logged in ASSUMPTIONS.md section 6.
