# SECURITY

How the SRI Practice Dashboard implements the HIPAA Security Rule safeguards that apply to it.
This document is written to be handed to an auditor. It is kept current as each phase lands, and
every control below is marked with its implementation status so nobody mistakes an intention for a
control.

Last updated: Phase 5.
Status key: **IMPLEMENTED** / **PARTIAL** / **PLANNED (phase N)**.

---

## 1. Scope and PHI posture

SRI Psychological Services has an executed Business Associate Agreement with Replit, the hosting
provider. This application therefore may store and process Protected Health Information, and it
does. Imported session rows carry patient name, patient code, insurance, and account balances.

The application is a HIPAA covered workload. The Security Rule safeguards in sections 3 through 7
below are direct requirements of this system, not aspirations.

**What this application deliberately does not hold.** The upstream Valant exports contain patient
dates of birth, home and work email addresses, phone numbers, and ZIP codes. No module needs any of
it. None of it is imported, logged, cached, or held in memory past the API read. See section 6.

---

## 2. Data classification

| Class | Examples | Where it may appear |
| --- | --- | --- |
| PHI, identified | patient_name, patient_code | `sessions` table, Phase 6 module only |
| PHI, financial | due_from_pt, paid_by_pt, ins_balance, total_balance at row grain | `sessions` table, aggregates only outside Phase 6 |
| Not PHI | therapist, CPT, location, aggregate counts and sums | any module |
| Secrets | service account JSON, DB key, session secret | environment only, never repo, never logs |

The controlling rule: **aggregation strips identity**. Every query outside the Phase 6 patient
funnel selects aggregates or therapist grain rows. None of them select patient columns.

---

## 3. Access control, 45 CFR 164.312(a)

### 3.1 Unique user identification, 164.312(a)(2)(i)

**IMPLEMENTED (Phase 1).**
Every person gets their own account keyed on their own email address. There are no shared logins,
no generic `admin` or `frontdesk` account, and no service accounts that a human signs into. Accounts
are deactivated, never deleted, so that audit log entries always resolve to a real identity.

### 3.2 Authentication

**IMPLEMENTED (Phase 1).**
- Passwords hashed with Argon2id via passlib. No reversible storage, no MD5 or SHA family.
- Minimum 12 characters, checked against a common password list. No forced rotation, per current
  NIST guidance, since rotation drives users toward predictable increments.
- Every password field carries a show and hide toggle, masked by default. NIST 800-63B recommends
  allowing this: a password the user can check is a password they will make longer, and length is
  what this application asks for. Revealing is a display change in the browser only, never sent,
  stored, or logged, and the field re-masks itself when the form is submitted or the page is left
  so a validation error or a back navigation cannot redisplay the secret. The residual risk is
  shoulder surfing in a shared clinical space, which is why it is off until the user asks for it
  and obvious while it is on.
- One admin is seeded from `ADMIN_EMAIL` and `ADMIN_INITIAL_PASSWORD`. That password must be changed
  on first login before any other route is reachable. Same for every admin created user, who gets a
  temporary password and a forced change.
- Server side sessions. The cookie holds an opaque 256 bit random token, never user data, never a
  role claim, never a JWT the client could tamper with. Only the token's SHA-256 hash is stored, so
  reading the database does not yield usable session cookies.
- Note that no signing key is involved anywhere. A random token looked up server side cannot be
  forged without guessing 256 bits, so a signature would add nothing. `SESSION_SECRET_KEY` is
  therefore optional: the name is reserved for the first feature that genuinely signs
  something, and it is not read at runtime.

### 3.3 Authorization

**IMPLEMENTED (Phase 1).**
Two orthogonal axes:
- **Role** controls what you may do: `ADMIN` (everything, including user administration, config,
  Data Sources, sync, audit log), `MANAGER` (read granted modules, plus enter utilization data and
  notes), `VIEWER` (read granted modules).
- **Module grants** control what you may see: `financial`, `therapist_utilization`,
  `room_utilization`, `patient_funnel`.

Financial access does not confer patient level access. The `patient_funnel` grant is separate and
must be granted explicitly. Its first shipped page, patient flow, is deliberately aggregate only:
distinct and first-time patient counts per period, computed with the identity column confined to
COUNT(DISTINCT) and never selected as output (see `app/reporting/patients.py` and its test). A
future version that lists actual patients would change this posture and requires the practice
owner's explicit sign-off first.

**Enforcement is server side.** Hiding a nav link is a usability choice, never a security control.
A user who types a URL for a module they lack gets a 403 from the route dependency, and the refusal
is written to the audit log. Navigation is rendered from the same grants the dependency checks, so
the two cannot disagree.

A second, independent check at the query layer, refusing to build a patient grain query without the
`patient_funnel` grant, is **PLANNED (Phase 6)** and lands with the first query that could select a
patient column. Until then there are no such queries to guard.

### 3.4 Automatic logoff, 164.312(a)(2)(iii)

**IMPLEMENTED (Phase 1).**
Server side idle expiry, default 15 minutes. The client shows a warning at 13 minutes with an
extend option. Expiry is evaluated on the server against the stored session record on every
authenticated request, so a client that never fires its timer still gets logged out, and a browser
with JavaScript disabled is no less protected.

The countdown endpoint (`/session/status`) deliberately reports the remaining time without
extending the session. If polling for the countdown refreshed the clock, the idle timeout would
never fire at all.

Both values are configurable by environment variable today. Moving them into the admin config page
is **PLANNED (Phase 2)**, with the config table.

### 3.5 Emergency access, 164.312(a)(2)(ii)

**IMPLEMENTED (Phase 1).**
An ADMIN may reach any module regardless of their own grants. Every such access is written to the
audit log with a distinct action type (`emergency_access`) so that break glass use is separable from
routine use when the log is reviewed.

---

## 4. Audit controls, 45 CFR 164.312(b)

**IMPLEMENTED (Phase 1).**

The `audit_log` table is append only. There is no update path and no delete path anywhere in the
codebase: no ORM update or delete on the model, no raw SQL, no admin UI affordance. Admins may read
and export the log. Nobody may edit it.

Every record carries: actor user id (or null with the attempted identifier, for failed logins),
action, target type, target id, timestamp in UTC, source IP, and result (success or failure).

Logged events:

| Category | Events | Status |
| --- | --- | --- |
| Authentication | login success, login failure, logout, session expiry, session extension, password change, account lockout | live |
| Authorization | access denied (role, grant, or CSRF), emergency access by an admin | live |
| User administration | user created, updated, role changed, grant added or removed, deactivated or reactivated, password reset | live |
| Audit | every view of the audit log itself | live |
| **Export** | every CSV export, with the table, row count, date range, and filters | live |
| Configuration | benefits threshold, CPT exclusions, week start, session timeout, with before and after values | live |
| Therapists | created, edited, alias added or removed, alias conflict refused | live |
| **PHI read** | every load of a patient level view, with the filter parameters used | live, exercised from Phase 6 |
| Data sources | source created, edited, activated, deactivated, mapping changed | live |
| Sync | every run, dry run and live, with rows read, upserted, rejected, and by whom | live |
| Manual data entry | utilization notes, with the previous text on every edit | live |
| Manual data entry | room utilization uploads, with counts and rejections | live |

The action types for the later rows exist in the enum now, so adding the feature does not also
require remembering to add its audit call.

Retention is 6 years, met by never deleting.

**Audit writes survive the failures they describe.** A refusal raises an exception that propagates
out of the route, and a blanket rollback on exception would discard the ACCESS_DENIED record
written moments earlier, leaving a log that faithfully records successes and stays silent about
refusals. The request scoped session therefore commits on the two control flow exceptions
(a redirect and an access denial) and rolls back on everything else.

**The audit log itself contains no PHI.** A patient level view read is logged as the view name plus
its filter parameters, not as the rows returned. Where a target is a specific patient record, the
target id is the internal row id, not the patient name.

---

## 5. Integrity and transmission security, 45 CFR 164.312(c) and (e)

### 5.1 In transit

**IMPLEMENTED (Phase 1).**
- HTTPS only. HTTP requests are redirected, and the app is intended to run only behind TLS
  termination.
- HSTS with a long max age.
- Session cookies are `Secure`, `HttpOnly`, and `SameSite=Strict`.
- CSRF tokens on every state changing route (POST, PUT, PATCH, DELETE), enforced centrally in
  middleware rather than per route, so a new handler is protected by default instead of by
  remembering. The token is bound to the server side session rather than double submitted in a
  cookie, so an attacker who can write cookies for a sibling subdomain still cannot forge a
  request. A missing or mismatched token is a 403 and an audit entry.
- Security headers: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, and a Content Security Policy. The CSP allows the Chart.js CDN
  and nothing else off origin.

### 5.2 At rest

**NOT IMPLEMENTED at the application layer. This is a known open gap, recorded here rather than
papered over.**

Until 2026-07-27 the database was SQLite encrypted with SQLCipher, keyed from
`DATABASE_ENCRYPTION_KEY` and applied as a `PRAGMA key` on every connection, with tests asserting
that a canary value did not appear in the file's bytes and that the stock `sqlite3` driver could not
open the file. The deployment then moved to Replit's managed PostgreSQL. SQLCipher, the encryption
key, and the tests that proved the encryption all went with it. Nothing in the application encrypts
data at rest now.

**What this means in practice.** PHI in this system is currently protected at rest only by whatever
Replit's managed PostgreSQL provides at the storage layer, plus the access controls in section 3 and
the transport protections in section 5.1. That may well be adequate, but this document cannot claim
it, because:

- The specific control has not been confirmed. Managed PostgreSQL providers generally encrypt
  volumes, but the algorithm, scope, and key custody have not been read from Replit's
  documentation and recorded here.
- The key would be held by the provider, not by the practice. The previous design deliberately kept
  the key in the practice's own Replit Secrets so that a copy of the database, however obtained, was
  useless without a key the practice controlled. That property is gone.
- Section 1 records an executed BAA with Replit as the hosting provider. The managed database is a
  distinct service with its own subprocessor, so whether that agreement reaches it should be
  confirmed. Encryption at rest would not substitute for the agreement either way.

**Three ways to close it,** in rough order of effort:

1. Confirm and document what Replit's PostgreSQL actually provides, obtain the BAA, and rewrite this
   section to describe the real control. Cheapest, and honest, but leaves key custody with the
   provider.
2. Encrypt the PHI columns in the application (`patient_name`, `patient_name_normalized`,
   `patient_code`), keyed from a secret the practice holds. Restores the property the old design
   had. Costs a `TypeDecorator` and rules out SQL `LIKE` on those columns. Nothing outside the
   Phase 6 patient funnel reads them (section 6), so the blast radius is small.
3. Return to SQLCipher SQLite. Restores the old guarantee exactly and gives up managed backups.

Until one of these lands, the application is not making a claim about encryption at rest, and this
gap is listed on the In development page inside the application so it is visible to whoever is
reading the numbers rather than only to whoever reads this file.

**Portability.** All database access goes through SQLAlchemy with Alembic migrations, so the move
from SQLite to PostgreSQL was a configuration change in `app/db.py` plus a new baseline migration,
not a rewrite. Nothing in the model layer depends on either engine.

### 5.3 Integrity of imported data

**IMPLEMENTED (Phase 2).**
Imports are idempotent through the upsert key (see ASSUMPTIONS.md A-020, which documents a
deviation from the originally specified key and the measurement that forced it). Identity is global
rather than per source sheet (A-022), so a visit exported on two overlapping quarterly sheets is
one row and is counted once. Rows that fail validation are never silently dropped; they land in
`import_errors` with the reason and the offending raw value, for admin review. Every sync run
writes a summary record, dry runs included, and every run is separately audit logged with its
counts and the user who ran it.

Money is stored as integer cents and handled as Decimal, so totals over thousands of rows are
exact rather than approximately right (ASSUMPTIONS.md A-060). An amount that cannot be parsed
rejects its row rather than defaulting to zero, so a typo can never quietly become a missing
payment.

---

## 6. Minimum necessary, 45 CFR 164.502(b)

**PARTIAL (Phase 0 states the rule and adds the log scrubber, Phase 2 enforces the allowlist).**

### 6.1 Column allowlist

**IMPLEMENTED (Phase 2), verified by test.**

Exactly 18 columns may be imported:

```
Therapist, Patient name, Patient Code, DOS, CPT, Ins, Loc, NOTE,
Due from pt, Paid by pt, Pt. Amount Due, Due from ins, Paid by ins,
Ins balance, Total due, Total paid, Total balance, Recorded
```

The allowlist is enforced server side at import time, not in the mapping UI alone. The importer
builds its column index from the allowlist, so a mapping naming any other column is ignored rather
than honoured: a hand crafted mapping claiming `BirthDate1` imports nothing. Values for unmapped
columns are never read out of the row, so they do not reach the ORM, the database, or a log.

A test asserts this directly by feeding the mapper a mapping for `BirthDate1` and `HomeEmail` and
checking that neither survives.

### 6.2 RAW tabs are blocked

**IMPLEMENTED (Phase 2), verified by test.**

The workbook's `RAW_Appointments`, `RAW_Documentation`, `RAW_PatientStatement`, and `RAW_Unrecorded`
tabs carry dates of birth, home and work emails, phone numbers, and ZIP codes. The Data Sources
mapping UI will not offer any tab whose name begins with `RAW_`, and the importer refuses one even
if a source is configured against it by hand. This is belt and braces with the column allowlist:
either control alone would stop a DOB from being imported.

### 6.2a Import errors hold patient identity, deliberately

A rejected row records its offending raw value plus a patient and therapist hint, because the row
that failed to parse is usually a patient's own row and an administrator cannot fix what they
cannot see. This is the same data class already held in `sessions`, not a new exposure.

Controls: the review pages are admin only; every view of them is audit logged as a PHI view with
the row count and never the rows; and only the offending cell is stored, not the whole row.

### 6.3 Aggregate views carry no identity

**IMPLEMENTED (Phase 3), verified by test.**

Financial and therapist utilization queries do not select `patient_name` or `patient_code`. This
is a property of the query builders in `app/reporting/queries.py`, not of the templates: a
template that forgot to omit a column would leak, whereas a column that is never selected cannot.

The test that enforces it does not read the source and hope. It executes every public builder in
the module, captures the SQL actually sent to the database, and fails if any patient column
appears in it. The list of builders is enumerated from the module rather than hardcoded, so a
query added in a later phase is covered without anyone remembering to add it, and a companion test
proves the assertion is not vacuous by showing what a leaking statement looks like.

Two further consequences, also tested: no report page renders a patient name, and no CSV export
contains one.

Because these views hold no identity, they are deliberately **not** logged as PHI reads. Logging
them would bury the genuine PHI reads in noise, which is the same failure as not logging at all.
Patient identity is rendered nowhere in the reports, including the patient flow page, whose
counts keep the identity column inside aggregates. It appears only in the import error review
pages (6.2a), which are admin only and are logged as PHI views.

### 6.4 PHI never reaches logs

**IMPLEMENTED (Phase 0).**
A logging filter is installed on the root logger before any application logger is created. It
redacts patient name and patient code patterns, and it redacts secret bearing keys, from log
records and from formatted exception text.

In addition:
- Application exception handlers return a generic message and a correlation id to the client. Stack
  traces are never rendered to a browser.
- Debug mode is off in any environment holding real data, and the config module refuses to enable
  it when the database is not the local development one.
- ORM statement echoing is off. SQLAlchemy `echo` would print bound parameters, which is a direct
  PHI to log path.
- The scrubber is a backstop, not a licence. The primary control is not putting PHI into a log call
  in the first place.

---

## 7. Secrets management

| Secret | Variable | Notes |
| --- | --- | --- |
| Google service account key | `GOOGLE_SERVICE_ACCOUNT_JSON` | Full JSON, from Replit Secrets. Read only Sheets and Drive scopes. |
| Database connection URL | `DATABASE_URL` | Injected by Replit for its managed PostgreSQL. Carries the database password, so it is treated as a secret: redacted from `Settings.__repr__` and never logged. |
| Session signing secret | `SESSION_SECRET_KEY` | Optional. Reserved: nothing signs with it yet, because session and CSRF tokens are random values validated server side rather than signed. If set, at least 32 characters. See README. |
| Seed admin | `ADMIN_EMAIL`, `ADMIN_INITIAL_PASSWORD` | Used once. Password change forced on first login. |

Rules that hold for all of them:
- Missing required configuration at boot is a hard failure with a named error, not a
  warning and a default.
- Never written to the repository. `.gitignore` excludes `.env` and credential file patterns.
- Never logged. The scrubber redacts keys matching secret, token, password, credential, and
  `private_key`, but the primary control is not logging them.
- The Google service account is granted **Viewer** on a single Drive folder, nothing wider. It
  cannot write to the Q sheets, and it cannot read anything the practice has not placed in that
  folder.

---

## 8. Known gaps and accepted risks

Recorded honestly, because an auditor will ask and a document that claims completeness at Phase 0
is not credible.

1. **The reporting modules do not exist yet.** Sections 3 and 4 are implemented and tested, but
   they currently guard an application with no data in it. The controls will not have been
   exercised against real PHI until Phase 2 syncs the first rows.
2. **No BAA coverage claim is made for Google.** The Google Sheets side of this is the practice's
   existing workflow, and whether Google Workspace is covered by a BAA for that data is the
   practice's determination, not this application's. The app reads from it either way.
3. **Encryption at rest is not implemented at the application layer.** See 5.2, which describes the
   gap in full and the three ways to close it. This is the most significant open item in this
   document. Backup storage and retention are operational matters outside this codebase, and with
   no application level encryption a backup is only as protected as wherever it is kept.
4. **The existing Replit BAA has not been checked against the managed database specifically.**
   Section 1 records an executed BAA with Replit as the hosting provider, which was true of the
   arrangement when PHI lived in an application managed encrypted file on Replit's disk. PHI now
   lives in Replit's managed PostgreSQL service, which is a different service with its own
   subprocessor. Whether the existing agreement covers it is a question for the practice, and it is
   worth asking now rather than at audit.
5. **Login throttling is per account, not per source.** After 5 consecutive failed passwords an
   account locks for 15 minutes, and the lockout is audited. That stops password guessing against
   one account. It does not stop password spraying, one attempt each against many accounts from one
   source, because there is no per IP limit. Adding one at the application layer is of limited
   value behind a proxy that can be told to send any `X-Forwarded-For`; the right place is the
   hosting edge. Flagged rather than half solved.

   Note the tradeoff the lockout itself creates: an attacker who knows a colleague's email address
   can lock them out for 15 minutes at will. For an internal application of this size, with an
   administrator who can reset on request, that is the better side of the trade.
6. **No intrusion detection and no WAF.** Both depend on the hosting posture rather than on this
   codebase.
7. **A bulk SQL statement could still modify the audit log.** The append only guarantee is enforced
   by ORM events, which fire per instance and are bypassed by a hand written
   `UPDATE audit_log` or `DELETE FROM audit_log`. No such statement exists in the codebase, and a
   test greps the source on every build to keep it that way, but a database administrator with
   direct database access is outside what application code can control.
8. **Room utilization data is self reported.** Every figure in that module comes from a file
   somebody filled in by hand, because no system records actual room usage. It is labelled as
   such on every view of it. It contains no PHI: rooms, dates, and slot counts only.
9. **Single tenant assumption.** The application serves one practice. There is no tenant isolation
   layer, because there are no other tenants.
