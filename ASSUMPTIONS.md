# ASSUMPTIONS

Every definitional choice made while building the SRI Practice Dashboard, recorded at the
moment it was made. Read this after every phase. Anything marked **NEEDS CONFIRMATION** is a
default I picked to keep moving, not a decision I am confident in.

Last updated: Phase 1 complete.

---

## 0. Scope and source of truth

**A-001. There is no separate requirements doc. RESOLVED.**
The build prompt refers to "SRI Practice Dashboard Requirements.md" in the repo root. That file
does not exist and, confirmed on 2026-07-27, is not expected to. The build prompt is the whole
scope, and it is the source of truth for architecture, security, data design, and sequencing.

**A-002. The prior project in this repository was deleted, not migrated.**
The repository previously held a different project (a CSV based Phase A analytics package under
`src/`). It was removed in full at your instruction. No code, schema, or definition was carried
forward. The old commits are still reachable in git history if anything needs to be recovered.

**A-003. The uploaded workbook is the Live Review workbook, not the Q sheet.**
The file supplied ("Valant Live Review Q2 2026.xlsx") is the daily reconciliation workbook, not
the quarterly Q sheet itself. Its "Menu Items Description" tab states that "Pull Q2 Snapshot
Only" opens the actual Q2 workbook and writes the current Q2 visit data into the Q2 Snapshot tab
as static values. So "Q2 Snapshot" is a point in time copy of the real Q sheet, and the real Q
sheet is a separate Google Sheet.
This matches the note in the build prompt about a "Q2 Visits" tab that does not exist in the xlsx.

**A-004. The real Q2 sheet has been identified. PARTIALLY RESOLVED.**
Spreadsheet ID `1Pft_PfZtdnU2d_Lhtc89U-323M8k3Z6m-f44s78rBHY`, supplied 2026-07-27. This is
seeded as the first Data Sources entry, labelled "Q2 2026".

Its tab list is still unverified: the build environment's network policy blocks Google Docs, so
the sheet could not be read during Phase 0. This changes nothing in the design, because the
mapping UI was always specified to enumerate tabs from the Sheets API rather than from a
hardcoded list. It does mean the first thing to check in Phase 2, once the service account
credential exists, is which visit level tab actually exists in this workbook ("Q2 Visits",
"Q2 Snapshot", or something else). No tab name is hardcoded anywhere.

The 9,389 row profile in section 1 below is therefore taken from the Live Review workbook's
static copy. It should be representative, but it is a snapshot, not the live sheet.

---

## 1. What the Q2 Snapshot data actually contains

Profiled from the uploaded xlsx. These are measurements, not assumptions, and they are recorded
here because several of them contradict the build prompt.

| Fact | Value |
| --- | --- |
| Header row | Row 1 |
| Data rows | 9,389 (sheet rows 2 through 9,390) |
| Rows below the data block | ~5,000 rows of dragged down formulas evaluating to 0, no identity |
| Date of service range | 2026-04-01 through 2026-06-30 |
| Distinct therapist tokens | 42 |
| Distinct patient names | 2,252 |
| Distinct patient codes | 1,627 |
| Sessions after the CPT exclusion list (A-030) | 8,454 |
| Sum of Total paid | 926,412.60 |
| Sum of Total balance | 198,520.62 |

**A-010. Columns M and N have no header and are not imported.**
Column M holds a composite match key of the form `THERAPIST|Patient name|M/D/YYYY|CPT`, used by
the workbook's own reconciliation macros. Column N is empty throughout. Neither is on the import
allowlist. Both are ignored.

**A-011. The data block ends where the Therapist column ends.**
Rows past the last populated Therapist cell contain only formula residue (0 values in Pt. Amount
Due, Ins balance, Total due, Total paid, Total balance). The importer treats a blank Therapist as
end of data and stops, rather than importing thousands of empty zero rows. A blank Therapist row
that appears *inside* the block would be a rejected row, not a stop signal. There are currently
zero such rows.

---

## 2. The upsert key does not work as specified

**A-020. Patient Code is populated on only 5,538 of 9,389 rows (59 percent).**
The build prompt specifies the unique upsert key as
`(source_id, patient_code, dos, cpt, therapist_id)`.
Tested against the real data, that key collides on **2,467 rows across 782 groups**, and every
single collision is a row where `patient_code` is blank. The key is not viable as written.

The same key with patient name substituted for patient code collides on **zero rows**:
`(source_id, patient_name, dos, cpt, therapist_id)` is unique across all 9,389 rows.

**Decision (default, reversible):** the upsert key is
`(source_id, therapist_id, patient_name_normalized, dos, cpt)`, using the normalized `cpt` from
A-033 rather than the derived `cpt_base`, so that two genuinely different sheet entries do not
collapse into one row just because they share a base code.
`patient_name_normalized` is uppercased and whitespace collapsed. `patient_code` is imported and
stored when present, and is used as the preferred join key for the Phase 6 patient funnel, but it
does not participate in identity for the import.

Supporting checks on the same data: no patient name maps to more than one patient code. Two
patient codes map to more than one name, which is consistent with two people sharing a six
character code stem, and those two are flagged to `import_errors` for review rather than merged.

**NEEDS CONFIRMATION**: this puts patient *name* into the identity key, which means a name
correction in the Q sheet creates a new row rather than updating the old one. The alternative is
to require Patient Code to be filled in on the Q sheet before import. That is an operations fix,
not a code fix, and it is the better long term answer if the practice is willing.

**A-021. True duplicates are kept, both of them, and flagged.**
Per the build prompt, if two rows collide on the upsert key the importer keeps both and writes an
`import_errors` entry for admin review. It does not silently overwrite. With the key in A-020 this
currently never fires on the Q2 data.

---

## 3. Session, revenue, and period definitions

**A-030. A session is one imported row whose base CPT is not on the exclusion list.**
Exclusion list, confirmed 2026-07-27: `99998`, `99999`, `QBCHK`, `FORM`, `PRO BONO`.

On the Q2 data:

| Excluded code | Rows |
| --- | --- |
| 99998 | 648 |
| 99999 | 264 |
| QBCHK | 19 |
| PRO BONO | 3 |
| FORM | 1 |
| **total excluded** | **935** |

That gives **8,454 sessions out of 9,389 rows**. The exclusion list is stored in the config table
and is editable by an admin, so this is a starting value, not a constant in code.

**A-031. Excluded CPTs are excluded from session counts but NOT from revenue.**
This is a distinction the build prompt does not make, and it matters. The 264 rows coded 99999
carry **17,674.46 in Total paid**, and the newly excluded `QBCHK`, `FORM`, and `PRO BONO` rows
carry a further **3,345.00**. If excluded rows were dropped from revenue as well as from session
counts, collected revenue would be understated by over 21,000. So: the exclusion list governs
`session_count` only. `revenue_collected` and `revenue_outstanding` sum over every imported row.
Excluding a code from counting is a statement about what a session is, not a statement that the
money is not real.

**A-032. 99998 and 99999 are both cancellations. RESOLVED 2026-07-27.**

- **99998**: the patient cancelled and **no fee** was charged. 648 rows.
- **99999**: the patient cancelled and a **no show fee was charged**. 264 rows.

Both stay excluded from session counts, which is now a definitional statement rather than a
guess: no clinical service was delivered, so neither is a session. Both remain fully imported.

The data agrees with the answer on every point:

| | 99998, no fee | 99999, fee charged |
| --- | --- | --- |
| Rows | 648 | 264 |
| Total paid | 243.50 | 17,674.46 |
| Total balance | 440.00 | 1,521.77 |
| Rows with a patient charge of exactly 75.00 | 8 | 252 |
| Rows with any insurance payment | 1 | 0 |

Two things follow.

**The standard no show fee is 75.00**, on 252 of 264 rows, and insurance never pays it. The
remaining 12 rows carry other amounts, which is presumably discretion applied case by case.

**No show fee income is real revenue but it is not clinical revenue.** It stays inside
`revenue_collected` (it is money in the door) and the financial module additionally breaks it out
as a named subtotal, because 17,674.46 in a quarter is worth seeing on its own rather than
blended into therapy income. This is the concrete instance of the rule in A-031.

**A-032a. Cancellation metrics are now well defined, for Phase 6.**
- `cancellations` = rows with base CPT `99998` or `99999`.
- `cancellations_billed` = rows with `99999`. `cancellations_unbilled` = rows with `99998`.
- `cancellation_rate` = cancellations divided by (cancellations + sessions).
- `no_show_fee_revenue` = `sum(total_paid)` over `99999` rows.
- `no_show_fee_uncollected` = `sum(total_balance)` over `99999` rows.

On the Q2 data the practice wide cancellation rate is **9.7 percent** (912 of 9,366).

**A-032b. Per therapist cancellation rates are not trustworthy yet. Do not ship them without a caveat.**
Across therapists with 150 or more rows, the cancellation rate ranges from **0.5 percent to 39.1
percent**. A 78 fold spread in patient behaviour between colleagues in one practice is not
credible. The far likelier explanation is inconsistent recording: some therapists log cancellations
in Valant and some do not, so a low rate may mean a disciplined caseload or may mean nobody is
entering the cancellations.

This matters because the number names a real person. A dashboard that reports "39 percent
cancellation rate" next to a therapist's name, when the true driver is that their colleagues are
not recording theirs, is a damaging misread of someone's work. So when Phase 6 surfaces this:
the practice wide rate is shown without qualification, and the per therapist breakdown carries an
explicit note that it measures recorded cancellations and is only comparable between therapists
once recording practice is known to be consistent.
**TO RAISE WITH THE PRACTICE**: is cancellation recording in Valant consistent across therapists?

**A-033. CPT normalizes to a base code, and the base code is what gets compared.**
Two steps:

1. **Normalize.** Trim, collapse internal whitespace, uppercase, and drop a trailing `.0` left by
   Excel's float storage, so `90837.0` becomes `90837` and `pro bono` becomes `PRO BONO`. The
   normalized value is stored as `cpt`.
2. **Derive a base.** If the normalized value starts with a 4 or 5 digit numeric code, take that
   code as `cpt_base`. Otherwise `cpt_base` equals `cpt`.

`cpt_base` is what the exclusion list is compared against and what reports group by. This matters
because the sheet writes the same procedure several ways: `90791-ADHD` (18 rows) and
`90837 - ADHD` (2 rows) fold into `90791` and `90837`, where they belong, instead of splintering
into their own report lines. Both raw and normalized values are retained, so the ADHD annotation
is not lost.

**A-034. Bare `ADHD` and `ADHD2` count as sessions, provisionally.**
Ten rows: `ADHD` (6) and `ADHD2` (4). Unlike the suffixed forms in A-033 these carry no numeric
code at all, so there is nothing to fold them into. They are not on the exclusion list, so they
count. Ten rows out of 9,389 will not move a dashboard, and this is a config edit if wrong.
Noted rather than asked, since the answer changes almost nothing.

**A-035. Revenue collected is `sum(total_paid)`. Revenue outstanding is `sum(total_balance)`.**
The patient side and insurance side split uses `pt_amount_due` and `ins_balance`.
Note that these do not reconcile exactly: on the Q2 data, `sum(pt_amount_due) + sum(ins_balance)`
is 198,328.86 against a `sum(total_balance)` of 198,520.62, a gap of 191.76 (one visit's worth).
The dashboard shows `total_balance` as the outstanding headline and shows the split beneath it,
labelled as a split rather than as components that must add up. The gap is surfaced in the import
summary rather than hidden.

**A-036. Weeks start Monday. Week labels are the Monday date.**
Configurable via `week_start_day`, default Monday. Quarters are calendar quarters. This matches the
Q sheet, whose Q2 runs 2026-04-01 to 2026-06-30.

**A-037. Timezone is America/New_York, and DOS is a date with no time.**
The practice is in Pennsylvania (Jenkintown, Revere Commons). All dates of service in the sheet
carry a midnight time component, which is an Excel artifact, so DOS is stored as a `DATE`.
Timestamps that are genuinely moments in time (audit log, sync runs, imported_at) are stored as UTC
and rendered in America/New_York.

**A-038. Blank money is 0. Unparseable money is a rejected row.**
Per the build prompt: a blank money cell reads as 0. A money cell that will not parse does not read
as 0, it sends the row to `import_errors` with the raw string preserved, so nobody silently loses a
payment to a typo.

**A-039. Eleven rows have a blank DOS.**
A row with no date of service cannot be placed in any period, so it cannot be counted. Those rows
are rejected to `import_errors` for admin review rather than imported and quietly excluded from
every report.

**A-039a. The benefits session threshold defaults to 25 per week, and that number is a placeholder.**
Utilization status is derived by comparing a therapist's weekly session count against
`benefits_session_threshold`: at or above it is fine, moderately below it is watch, well below it
is the alert state. The 25 is my invention, not the practice's number, and it drives every status
flag on the utilization board. It is config, editable by an admin, so correcting it is a form
field rather than a code change.
**TO CONFIRM WITH THE PRACTICE**: the real threshold, and whether it is uniform or varies by
employment type. The data model already separates `salaried_benefits` from `percentage_legacy`,
so a per type threshold is a small change if that is how the practice actually works.

---

## 4. Therapists, locations, insurance

**A-040. The Q sheet's Config tab is the alias seed, and it is authoritative.**
The workbook already maintains a provider alias table on its `Config` tab, keyed
`Type | Raw Text Contains | Output`:

```
PROVIDER  Mary Kate McNulty        -> MCNULTY
PROVIDER  Mary Kathleen McNulty    -> MCNULTY
PROVIDER  Inna Pavlova-Rosenfeld   -> PAVLOVA
PROVIDER  Sigman                   -> SALERNO SIGMAN
PROVIDER  Fleishman-Pogach         -> FLEISHMAN
LOCATION  Telehealth               -> TH
LOCATION  SRI Psychological Serv.  -> 1
LOCATION  In Person                -> 1
```

The app imports this as the initial alias set rather than inventing its own. Note that the practice
has already decided `Pavlova-Rosenfeld` maps to `PAVLOVA`, while the Q sheet separately contains a
therapist token `ROSENFELD` with 121 sessions. Those may or may not be the same person. They are
**not** auto merged. An admin gets a fuzzy match suggestion and decides.

**RESOLVED 2026-07-27**: `PAVLOVA` and `ROSENFELD` are **different people**. They stay two
therapist records and must never be merged. `PAVLOVA` has 35 rows in the Q2 data and `ROSENFELD`
has 121.

**A-040a. Alias rules are matched on the whole normalized name, never on a substring.**
This is the direct consequence of A-040, and it is a real trap rather than a theoretical one. The
practice's own `Config` tab uses "Raw Text Contains" semantics, and one of its rules maps
`Inna Pavlova-Rosenfeld` to `PAVLOVA`. That rule is written out in full, so it is safe. A rule
written as just `Rosenfeld` would not be: it would match Inna Pavlova-Rosenfeld and the unrelated
therapist `ROSENFELD` alike, silently folding two different people into one record and corrupting
both of their utilization figures with no error anywhere.

So the app does not implement "contains". It matches an alias against the fully normalized
therapist string. In addition, when aliases are imported or edited the app checks for ambiguity,
where one alias pattern is a substring of another or two patterns both resolve the same raw name,
and refuses the change with the collision named. A wrong merge is invisible once it has happened,
so it has to be caught at the point of configuration.

**A-041. Alias matching is exact first, fuzzy only as a suggestion.**
Exact match on a known alias resolves silently. No exact match produces a rejected row plus a ranked
fuzzy suggestion in the admin review queue. Aliases are never created automatically, because a wrong
auto merge silently corrupts every utilization number for two people at once.

**A-042. Location `1` is a number in the sheet and a string in the app.**
Excel stores the Jenkintown location code as the float `1.0`. Locations are stored as strings, and
`1.0` normalizes to `1`. Per the Abbreviations tab: `1` = Jenkintown, `2` = Revere Commons,
`TH` = Telehealth (all telehealth variants, including the Medicare and Optum flavors, collapse to TH).

**A-043. Insurance is blank on 214 rows.**
Blank insurance is imported as NULL, not as self pay. `SP` is a real code in the sheet with 421 rows,
so blank means unknown, not self pay. Blank insurance rows still count as sessions and still count
toward revenue.

**A-044. The Abbreviations tab is imported on demand, not on every sync.**
Per the build prompt, an admin button imports it into a `lookups` table. It maps long insurance
names to short codes, locations to short codes, and note codes (`Draft` -> `D`, `Finalized` -> `X`)
to short codes. Note that several long insurance names map to the same short code, so the mapping is
many to one and cannot be reversed unambiguously. Reports display the short code with the long name
available on hover, rather than pretending a short code has one long name.

**A-045. NOTE is documentation status, not free text.**
Values in the Q2 data: `X` (9,273), blank (106), `D` (9), and one `X ` with a trailing space that
normalizes to `X`. Per the Abbreviations tab and the Instructions tab, `X` means the note is signed
or finalized and `D` means draft or in editing. It is stored as `note_code`.

**A-046. `Recorded` is almost entirely empty and is stored as imported.**
Only 6 of 9,389 rows carry a value, all of them the literal `UNRECORDED`. Per the Instructions tab
the semantics are: a visit appearing on the Valant unrecorded list is UNRECORDED, otherwise blank
means recorded. So blank is the normal case and means recorded. The app stores the raw value and
does not infer anything from it in Phases 3 through 5.

---

## 5. Security and privacy defaults

**A-050. The RAW tabs are never mapped, and this is enforced, not just documented.**
`RAW_Appointments` carries `BirthDate1`, `HomeEmail`, `WorkEmail`, `MainPhoneAndExtension`,
`Phone4`, and `PatientZipCode`. `RAW_Documentation` and `RAW_PatientStatement` carry patient
identity as well. None of it is needed by any module. The Data Sources mapping UI refuses to select
a tab whose name begins with `RAW_`, and the column allowlist is enforced server side at import, so
a hand crafted mapping cannot smuggle a DOB in either.

**A-051. Only the 18 allowlisted columns are read.**
Therapist, Patient name, Patient Code, DOS, CPT, Ins, Loc, NOTE, Due from pt, Paid by pt,
Pt. Amount Due, Due from ins, Paid by ins, Ins balance, Total due, Total paid, Total balance,
Recorded. Everything else in the API response is discarded at the boundary, before the row is
constructed, so unmapped values do not survive in memory beyond the read.

**A-052. Aggregate views carry no patient identity at all.**
Financial, therapist utilization, and room utilization queries never select `patient_name` or
`patient_code`. This is enforced in the query layer, not by template omission, so a template bug
cannot leak a name into a chart tooltip.

**A-053. Session timeout is 15 minutes idle, warned at 13.**
Server side expiry, evaluated on every authenticated request against `last_seen_at`. The client
warning is a courtesy; the server is what enforces it. Configurable per the build prompt.

The countdown endpoint reports remaining time without extending the session. This is deliberate:
if the poll that drives the warning also refreshed the clock, the idle timeout could never fire.

**A-053a. Failed logins lock an account for 15 minutes after 5 attempts.**
Neither number came from the practice. Five is low enough to stop password guessing and high
enough to survive an ordinary bad morning; fifteen minutes is short enough that a locked colleague
usually waits rather than calls. An admin password reset clears a lockout immediately.

Accepted tradeoff, recorded because it is a real one: anyone who knows a colleague's email address
can lock them out for 15 minutes on purpose. For an internal application of this size, with an
admin who can reset on request, that is the better side of the trade. The alternative, no lockout,
leaves the login form open to unlimited guessing.

**A-053b. Session cookies are browser session scoped, with no max age.**
Closing the browser drops the cookie. The server side idle timeout is the real control either way,
so a persistent cookie would only widen the window in which a stolen cookie is useful.

**A-053c. Sessions are revoked immediately on any change to what a person may do.**
Deactivation, a role change, a grant change, and a password reset all revoke every live session for
that user. A password change revokes every session except the one making the change, so a stolen
cookie does not survive the change intended to shut it out. The alternative, waiting for the next
login, means a revoked grant stays usable for up to a working day.

**A-053d. The last active administrator cannot be demoted or deactivated.**
Including by themselves. Nothing else in the app can restore admin access, so allowing it would
mean a support call and a hand edited database. Refusing it costs an admin one extra step on the
rare occasion they genuinely want to hand over: promote the successor first.

**A-053e. Failed logins reveal nothing about whether an account exists.**
Wrong password and unknown address return the same message, the same status, and take about the
same time (an unknown address still runs a password verification against a dummy hash). The one
deliberate exception is a locked account, which says so: that user is almost certainly the account
owner, and "incorrect password" would only send them into more failed attempts.

**A-053f. All timestamps are stored and returned as timezone aware UTC.**
SQLite has no native timestamp type, so `DateTime(timezone=True)` silently drops the offset on a
round trip and hands back a naive value. A custom `UTCDateTime` column type makes the contract
explicit. Under PostgreSQL it becomes a no op, so the models port unchanged. Display conversion to
America/New_York happens at the template layer, never in storage.

**A-054. Database encryption at rest uses SQLCipher, and the app fails loudly without a key.**
Details and the operational caveats are in SECURITY.md. If the SQLCipher driver is unavailable in
the deployment environment, the app refuses to start rather than silently falling back to an
unencrypted SQLite file.

**A-055. Audit log retention is 6 years, and there is no delete path in code.**
No ORM delete, no update, no admin UI affordance. Retention is enforced by not deleting.

**A-056. Synthetic test data uses obviously fake names.**
`Patient AA`, `Patient AB`, and so on, with codes `PATAA`, `PATAB`. No name that could be mistaken
for a real person appears in any fixture, seed, or demo.

---

## 6. Conflicts between the build prompt and observed reality

Logged per the rule that the prompt wins but conflicts get flagged.

1. **Upsert key.** Prompt specifies patient_code in the key. The data makes that unworkable on 41
   percent of rows. See A-020. This is the one place I have deviated from the prompt, because
   following it would corrupt the import.
2. **"Q2 Snapshot is the canonical tab."** The prompt says to confirm before hardcoding, and A-003
   shows why: the snapshot tab is a static copy inside a *different* workbook. Nothing is hardcoded.
3. **Excluded CPTs and revenue.** The prompt defines the exclusion list without saying whether it
   applies to revenue. It cannot apply to revenue without losing over 21,000. See A-031.
4. **Patient Code column position.** The prompt's allowlist lists Patient Code in header order after
   the money columns. In the actual sheet it is column S, after Total balance, with `Recorded` in T.
   The mapping is by header text, not position, so this is handled, but it confirms that positional
   mapping would be wrong.
5. **Requirements doc.** The prompt says to read one. There is not one. See A-001. Resolved.

---

## 7. Open questions

One place to see what is still unanswered. Nothing here blocks the current phase.

| # | Question | Status | If the answer changes things |
| --- | --- | --- | --- |
| 1 | Can Patient Code be filled in on the Q sheet going forward? | not yet asked | Would let the upsert key drop patient name, which is the cleaner design. See A-020. |
| 2 | Is cancellation recording in Valant consistent across therapists? | not yet asked | Decides whether per therapist cancellation rates can be compared at all. See A-032b. |
| 3 | Which visit level tab exists in the live Q2 workbook? | blocked until credentials | Answered by the mapping UI in Phase 2. Nothing hardcoded. See A-004. |
| 4 | Is the 25 session benefits threshold correct? | not yet asked | Drives every utilization status flag. Placeholder default. See A-039a. |

Answered:

| Question | Answer | Date | Entry |
| --- | --- | --- | --- |
| Is there a separate requirements doc? | No, the build prompt is the whole scope | 2026-07-27 | A-001 |
| Which Google Sheet is the real Q2 sheet? | ID `1Pft...rBHY` supplied | 2026-07-27 | A-004 |
| Do `QBCHK`, `FORM`, `pro bono` count as sessions? | No | 2026-07-27 | A-030 |
| What is CPT `99998`? | Patient cancelled, no fee charged | 2026-07-27 | A-032 |
| What is CPT `99999`? | Patient cancelled, no show fee charged | 2026-07-27 | A-032 |
| Are `PAVLOVA` and `ROSENFELD` the same therapist? | No, different people, never merge | 2026-07-27 | A-040 |
