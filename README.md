# SRI Practice Dashboard

Internal practice management reporting for SRI Psychological Services: financial
performance, therapist utilization, room utilization, and (later, gated) a patient level
funnel, all derived from one imported dataset of session level rows synced from the
quarterly Q sheet.

**This application holds PHI.** Read [SECURITY.md](SECURITY.md) before changing anything
that touches data, logging, or access control. Read [ASSUMPTIONS.md](ASSUMPTIONS.md) for
every definitional choice, including the places where observed data forced a deviation
from the build specification.

Current state: **Phase 5 complete.** Authentication and administration, the import
pipeline, the Financial module with the Reports overview dashboard, therapist
utilization with per period notes, and room utilization behind a feature flag. A
synthetic demo source lets you exercise the whole path without credentials.

Phase 6, the patient level funnel, is **not built**. It is gated on the practice owner
confirming its scope, and it is the only module that would surface patient identity
outside the import error queue.

Whatever is still undecided is listed in the application itself at **`/status`**, worked
out from the current state of the data rather than from a hardcoded list, so an item
disappears once it is genuinely resolved.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in SESSION_SECRET_KEY. Required.
python -c "import secrets; print(secrets.token_urlsafe(48))"
# Set DATABASE_URL to a PostgreSQL database. In Replit it is injected for you.
# Also set ADMIN_EMAIL and ADMIN_INITIAL_PASSWORD, or nobody can sign in.

alembic upgrade head
uvicorn app.main:create_app --factory --reload
```

Then:

- `http://127.0.0.1:8000/` the application, which redirects to sign in
- `http://127.0.0.1:8000/healthz` liveness
- `http://127.0.0.1:8000/readyz` readiness, which proves the encrypted database opens

Sign in as `ADMIN_EMAIL`. You will be required to choose a new password before anything
else is reachable. Then:

| Where | What |
| --- | --- |
| `/status` | **In development**: open questions, caveats on the numbers, what is not built |
| `/reports` | The overview dashboard |
| `/reports/financial` | Sessions, revenue, outstanding, and the breakdowns |
| `/reports/therapist-utilization` | Status board, weekly history, and per period notes |
| `/reports/room-utilization` | Heat grid, only when `FEATURE_ROOM_UTILIZATION` is on |
| `/admin/sources` | The quarterly Q sheets and the sync |
| `/admin/therapists` | Therapists, their aliases, and their employment type |
| `/admin/rooms` | Rooms and the manual usage upload, only when the flag is on |
| `/admin/config` | Benefits threshold, CPT exclusions, week start, session timeout |
| `/admin/users` | People and their access |
| `/admin/audit` | The append only log |

**Set up therapists before the first sync.** The importer never creates one, because a
wrong merge between two people is invisible once it happens. An unrecognized name
rejects to the import errors queue with a suggestion, and you resolve it by adding the
therapist and its aliases, then syncing again.

### Trying the import without credentials

On `/admin/sources`, click **Create demo source**. That builds a synthetic workbook
mirroring the real Q2 Snapshot layout exactly, with obviously fake patients (Patient AA,
Patient AB) and three invented therapists. Run a dry run, then a live sync, then look at
the rejected rows: five are deliberately broken, one per failure mode.

Tests and lint:

```bash
pytest
ruff check . && ruff format --check .
```

Migrations:

```bash
alembic upgrade head                                  # apply
alembic revision --autogenerate -m "description"      # create after a model change
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
| `SESSION_SECRET_KEY` | yes | Minimum 32 characters. Reserved for signing. See the secrets reference below. |
| `DATABASE_URL` | yes | PostgreSQL connection URL. Injected automatically by Replit for its managed database. Carries the database password, so treat it as a secret. |
| `TEST_DATABASE_URL` | for tests | A **separate** database the test suite is allowed to drop and rebuild. Never point this at the real one. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | for sync | Full service account JSON, one line. |
| `ADMIN_EMAIL`, `ADMIN_INITIAL_PASSWORD` | to sign in | Seeds one admin on first start. Password change forced on first login. Must meet the password policy or startup fails. |
| `ENVIRONMENT` | no | `development`, `test`, or `production`. |
| `DEBUG` | no | Refused in production. Debug output can carry PHI. |
| `SESSION_TIMEOUT_MINUTES` | no | Default 15, idle, enforced server side. |
| `SESSION_WARNING_MINUTES` | no | Default 13. Must be less than the timeout. |
| `BENEFITS_SESSION_THRESHOLD` | no | Default 25 sessions per week. Editable by an admin later. |
| `CPT_EXCLUSIONS` | no | Comma separated. Defaults to `99998,99999,QBCHK,FORM,PRO BONO`. Governs session counts only, not revenue. |
| `WEEK_START_DAY` | no | `monday` (default) or `sunday`. |
| `APP_TIMEZONE` | no | Default `America/New_York`. |
| `FEATURE_ROOM_UTILIZATION` | no | Default off. With it off the module is a 404 everywhere and absent from navigation. |
| `FEATURE_PATIENT_FUNNEL` | no | Default off. Phase 6, gated on your confirmation. |

A missing required secret is a startup failure with a named error. There is no fallback,
and in particular there is no fallback to an unencrypted database.

### Secrets reference

Two secrets are required at boot. They do very different things, and only one of them
is dangerous to change.

#### `SESSION_SECRET_KEY`

**What it does today: nothing at runtime.** It is validated at startup (present, at
least 32 characters) and then not used, because the session design does not need a
signing key. The session cookie holds an opaque 256 bit random token, and only its
SHA-256 hash is stored, so the server validates a session by looking the token up
rather than by verifying a signature. See `app/security/sessions.py`. The CSRF token
works the same way: random per session, stored server side, compared directly.

It is kept and required so that it is already provisioned for the first feature that
genuinely needs signing (a password reset link, or a signed export URL). Until then it
is reserved, not load bearing.

**Safe to generate a fresh random value?** Yes, always, on any deployment. Changing it
signs nobody out and breaks nothing, because nothing depends on it.

Generate with:
```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

**Is `SESSION_SECRET` the same thing?** No. This application reads only
`SESSION_SECRET_KEY` (see `REQUIRED_SECRETS` in `app/config.py`). A variable named
`SESSION_SECRET` belongs to something else in the environment and is ignored here.
Setting one does not satisfy the other. Leave any pre-existing `SESSION_SECRET` alone
and add `SESSION_SECRET_KEY` separately.

#### `DATABASE_URL`

**What it does:** points the application at its PostgreSQL database. In Replit this is
injected automatically by the built in database; there is nothing to generate. It embeds
the database password, so it is treated as a secret: it is redacted from
`Settings.__repr__` and never logged.

**Safe to change?** Pointing it at a different database points the application at
different data. The schema is created by `alembic upgrade head`, which runs on boot, so an
empty database is populated automatically. An existing database keeps its rows.

#### `TEST_DATABASE_URL`

The test suite drops every table and re runs the migrations before each test, so it must
never touch the real database. It uses `TEST_DATABASE_URL` and nothing else: `DATABASE_URL`
is deliberately not a fallback for the destructive path. Tests **skip** rather than fail
when it is unset, so a suite that appears to pass with no database configured has in fact
asserted nothing. CI sets it explicitly and checks the database is reachable first, for
exactly that reason.

### Encryption at rest

**There is currently none at the application layer.** Earlier versions of this application
stored data in a SQLCipher encrypted SQLite file keyed from `DATABASE_ENCRYPTION_KEY`; the
move to Replit's managed PostgreSQL removed that layer along with the tests that proved it.
`DATABASE_ENCRYPTION_KEY` is no longer read anywhere and can be removed from Secrets.

This is a real open gap on a PHI system, not a tidy up item. **SECURITY.md section 5.2**
states what is and is not true now and sets out three ways to close it. Read that before
treating the deployment as compliant.

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

## Adding next quarter's sheet

The Q sheet is a new Google Sheet every quarter. Rotation is meant to be three steps and
no downtime.

1. Put the new sheet in the shared Drive folder. It inherits the service account's
   access, so there is nothing to re-share.
2. On **Data sources**, paste its URL and give it a label such as `Q3 2026`. The column
   mapping is prefilled from the previous quarter, so you confirm rather than retype.
3. Pick the tab, save, run a **dry run**, read the summary, then **Sync now**.

Deactivate the old quarter whenever you like. Its rows stay exactly where they are: the
database is the system of record and the sheets are only ingestion, so the app is the
only place full cross quarter history exists.

What the dry run tells you before you commit to anything: how many rows were read, the
date range found, any column in the sheet that nothing maps to (which is how layout
drift announces itself), and every row that would be rejected with the reason and the
offending value.

---

## Architecture

```
app/
  config.py          environment configuration, fails loud on missing secrets
  db.py              PostgreSQL engine, session factory, connectivity verification
  logging_setup.py   PHI scrubbing log filter and formatter
  middleware.py      security headers, CSP, safe error responses
  main.py            application factory, health endpoints, routes
  models/            SQLAlchemy models: users, grants, therapists, visits,
                     data sources, sync runs, import errors, audit log
  routers/           auth, users, audit, data sources, therapists, settings, reports
  security/          passwords, sessions, CSRF, audit writer, route dependencies
  sync/              normalization, Sheets clients, the import engine, demo data
  reporting/         period maths, aggregate queries, KPI construction
  config_store.py    admin editable settings, seeded from the environment
  seed.py            first administrator, from the environment, once
  templating.py      one render() that supplies CSRF, user, and navigation everywhere
  templates/         Jinja2 server rendered pages
  static/css/        tokens.css defines every colour, space, and size; app.css uses them
  static/js/         charts.js, the one Chart.js wrapper every chart goes through,
                     plus the idle session warning
migrations/          Alembic, engine built from settings so the key stays out of the repo
tests/
```

- Python, FastAPI, Jinja2, htmx. No SPA framework.
- SQLAlchemy with Alembic from day one. Originally SQLite with SQLCipher, now PostgreSQL;
  the move was a change to `app/db.py` and a new baseline migration, nothing in the models.
- Chart.js 4 from CDN, wrapped in `static/js/charts.js`, which reads the design tokens
  off the document so charts and page chrome cannot drift apart.
- The Content Security Policy allows exactly one off origin source, the Chart.js CDN.

---

## Build phases

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Scaffold, dependencies, README, SECURITY.md, ASSUMPTIONS.md | **done** |
| 1 | Auth, roles, module grants, user administration, audit log, session timeout, seeding | **done** |
| 2 | Data model, Data Sources registry, sync engine with dry run and import errors | **done** |
| 3 | Financial module and the Reports overview dashboard | **done** |
| 4 | Therapist utilization: threshold config, status board, drill in, notes | **done** |
| 5 | Room utilization behind its flag, manual upload path | **done** |
| 6 | Patient funnel: AR aging, new patient volume, no show patterns | **gated, not started** |

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
