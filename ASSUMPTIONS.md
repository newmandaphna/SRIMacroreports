# ASSUMPTIONS

Every definitional choice made while building the SRI Practice Dashboard, recorded at the
moment it was made. Read this after every phase. Anything marked **NEEDS CONFIRMATION** is a
default I picked to keep moving, not a decision I am confident in.

Last updated: Phase 3 complete.

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

**A-039. Eleven rows have a blank DOS, and they cost 424.55 of reported revenue.**
A row with no date of service cannot be placed in any period, so it cannot be counted. Those rows
are rejected to `import_errors` for admin review rather than imported and quietly excluded from
every report.

Measured by running the finished import engine over the real Q2 Snapshot, this is the difference
between what the sheet contains and what the dashboard will show:

| | In the sheet | Imported | Gap |
| --- | --- | --- | --- |
| Rows | 9,389 | 9,378 | 11 rejected, all blank DOS |
| Sessions after the exclusion list | 8,454 | 8,444 | 10 |
| Cancellations | 912 | 911 | 1 |
| Revenue collected | 926,412.60 | 925,988.05 | 424.55 |
| Revenue outstanding | 198,520.62 | 197,870.86 | 649.76 |
| No show fee revenue | 17,674.46 | 17,674.46 | none |

The figures elsewhere in this document are counted over the raw sheet. The dashboard will report
the imported column, because those 11 rows genuinely cannot be placed in a week, a month, or a
quarter. The gap is not hidden: the rows sit in the import errors queue with the reason, and the
sync summary names the count.
**TO RAISE WITH THE PRACTICE**: 11 rows carrying 424.55 in collected revenue are missing a date of
service. Adding the dates in the Q sheet and re-syncing brings them in, and nothing else is needed.

**A-039a. The benefits session threshold defaults to 25 per week, and 25 is demonstrably wrong.**
Utilization status compares a therapist's weekly session average against
`benefits_session_threshold`: at or above is fine, within 20 percent below is watch, further below
is the alert state. It is config, editable by an admin at `/admin/config`, so correcting it is a
form field rather than a code change.

Running the finished reporting layer over the real Q2 data shows the placeholder is not merely
unconfirmed, it is wrong. Sessions per week across the 42 therapists with activity in Q2:

| | Sessions per week |
| --- | --- |
| Minimum | 0.0 |
| 25th percentile | 5.7 |
| **Median** | **16.9** |
| 75th percentile | 22.3 |
| Maximum | 40.2 |
| Mean | 15.5 |

At a threshold of 25, **30 of 42 therapists land in the alert state**. A dashboard that opens with
71 percent of the practice flagged red is not reporting a problem, it is reporting a bad threshold,
and it teaches everyone to ignore the colour.

What each candidate threshold would produce:

| Threshold | Below | Watch | At or above |
| --- | --- | --- | --- |
| 10 per week | 12 | 3 | 27 |
| 15 per week | 16 | 2 | 24 |
| 18 per week | 18 | 4 | 20 |
| 20 per week | 20 | 10 | 12 |
| 25 per week (current default) | 30 | 5 | 7 |

**TO CONFIRM WITH THE PRACTICE**: the real threshold. Somewhere around 18 to 20 would put roughly
half the practice above it, which is what a threshold usually means, but that is arithmetic and not
a policy, and the practice's actual benefits agreement is the only thing that settles it.

**A-039b. The 42 therapist figure includes everyone with any Q2 activity, at any employment type.**
The distribution above treats all 42 as salaried, which they are not. Only therapists marked
`salaried_benefits` are measured against the threshold at all; percentage based therapists have no
session minimum and carry no status, since flagging them below a target they were never given
would be a false alarm about a real person's work.

Employment type is set per therapist at `/admin/therapists` and defaults to `other`, which is also
unmeasured. So the number of therapists actually shown in the alert state depends on two things the
practice has not yet supplied: the threshold, and who is salaried.

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

## 5a. Import and storage decisions (Phase 2)

**A-060. Money is stored as integer cents and handled as Decimal.**
Not as a float. SQLite has no DECIMAL type, so SQLAlchemy's Numeric falls back to float there, and
summing nine thousand floats drifts. The sheet is full of values like 156.05 that have no exact
binary representation, and a revenue figure that is wrong in the cents is a revenue figure nobody
trusts. A `Money` column type converts at the boundary, so callers only ever see Decimal, and the
representation ports to PostgreSQL unchanged.

**A-061. The Python class for a session row is called `Visit`; the table is still `sessions`.**
The domain word is session and the table name follows the specification. The class is not called
`Session` because the codebase already has SQLAlchemy's `Session` and the auth `UserSession`, and
three different things called Session is how somebody eventually authenticates against a therapy
appointment.

**A-062. Rejected rows can contain patient identity, and that is accepted rather than avoided.**
`import_errors` stores the offending raw value plus a patient and therapist hint, because the row
that failed to parse is usually a patient's own row and an admin cannot fix what they cannot see.
This is the same data class already held in `sessions`. Mitigations: the review pages are admin
only, every view of them is audit logged as a PHI view, and only the offending cell is stored
rather than the whole row.

**A-063. The importer stops at the first row with no identity at all.**
The real sheet carries about 5,000 rows of dragged down formulas below the data, every identity
column blank and every money column 0. Importing them would add thousands of meaningless zero
rows. A blank row appearing *inside* the data block would be a rejection, not a stop signal; there
are currently none.

**A-064. Re-syncing unchanged rows changes nothing.**
The importer compares the payload and leaves identical rows alone, so `updated_at` does not churn
across nine thousand rows on every sync and the modification history stays meaningful. Verified
against the real sheet: a second live run reports 9,378 unchanged, 0 inserted, 0 updated, in
0.6 seconds.

**A-065. A dry run writes no session rows, but does record its own findings.**
It performs the same read, mapping, validation, and rejection analysis, and inserts or changes no
visit. It does persist its `SyncRun` and its `import_errors`, because a preview whose findings
vanish when you navigate away is not a preview anyone can act on.

**A-066. Unmapped sheet columns are reported, not ignored.**
Every header the mapping does not claim is listed on the run summary. A new column appearing is
how a quarter's layout drift announces itself, and silence would waste that signal. On the real Q2
Snapshot there are none: all 18 allowlisted fields map, and the two unnamed columns (M, the
composite match key, and N, empty) are correctly left alone.

**A-067. The app ships a synthetic demo source.**
A bundled workbook that mirrors the real Q2 Snapshot header row exactly, including the two unnamed
columns, Patient Code sitting in column S, Excel float codes, the suffixed CPT spellings, and both
cancellation codes. Patients are Patient AA through Patient AN with codes PATAA through PATAN.
It exists so the whole import path can be exercised with no credentials and no PHI, which also
means the engine is covered by tests that never touch the network. Five of its rows are
deliberately broken, one per rejection reason.

**A-068. Enum columns are declared as enums, not as strings.**
A plain String column stores an enum's value correctly but hands back a bare `str`. Anything then
reaching for `.value` or `.label` raises an AttributeError, Jinja swallows it into an empty string,
and a status badge silently shows the wrong state. This was a real bug, caught in Phase 2 and
fixed across every enum column including the Phase 1 ones.

**A-069. Alias resolution is exact, and creating a therapist is never automatic.**
An unrecognized therapist rejects the row and offers ranked suggestions to an admin. The alias
column is globally unique, so two therapists cannot both claim one alias and a silent merge is
impossible at the database level, not merely by convention. This is the PAVLOVA and ROSENFELD
lesson from A-040a expressed as a constraint.

---

## 5b. Reporting decisions (Phase 3)

**A-070. Aggregate queries never select a patient column, enforced against the SQL.**
Every builder in `app/reporting/queries.py` is aggregate or therapist grain. A test executes all
of them, captures the SQL actually emitted, and fails if `patient_name`, `patient_code`, or
`patient_name_normalized` appears anywhere in it. The test enumerates the module rather than a
fixed list, so a builder added later is covered without anyone remembering to add it.

**A-071. A period with no activity is a zero, not a missing point.**
A gap in a trend series lets the line close over it, which reads as though nothing happened rather
than as though nothing was done. Every series is continuous across the selected range.

**A-072. Deltas compare equal length windows, not calendar periods.**
The comparison for "quarter to date" three days in is the previous three days, not the previous
full quarter. Comparing a partial period against a whole one would show a collapse that is only
the calendar.

**A-073. Colour follows meaning, not direction.**
Outstanding balances falling is good and rising is bad, which is the opposite of collections. The
KPI cards carry an explicit `lower_is_better` flag and colour the change by what it means. Alert
red remains reserved for below threshold, and appears nowhere else.

**A-074. The default range is the last 4 weeks, and an empty result explains itself.**
Two different empty states, because they need different actions: nothing has ever been synced (go
to Data Sources) versus nothing in this range (here is the range you do have, one click to see it).

**A-075. Bucket size is chosen from the range length unless overridden.**
Up to 120 days is weekly, up to 800 days monthly, beyond that quarterly, so a chart never has two
bars or four hundred. An admin can override it in the filter bar.

**A-076. Exported CSVs carry a provenance line.**
Filename, date range, and export timestamp above the header row. Without it an exported table is a
set of numbers with no context, which is how a quarter to date figure gets forwarded and read as a
full quarter.

**A-077. Cancellation rate is shown practice wide only.**
Per therapist rates are deliberately absent from the overview. See A-032b: the recorded rates span
0.5 to 39.1 percent, which is far likelier to be inconsistent recording than real patient behaviour,
and the number names a real person.

**A-078. Therapist administration exists because the importer will not create therapists.**
The sync rejects an unrecognized therapist rather than inventing one, which means that without an
admin page to create therapists and their aliases, a first sync against a real sheet would reject
every single row with nowhere to fix it. That page was missing after Phase 2 and is added here. It
enforces the same alias uniqueness the database does, so a conflict is reported at the point of
configuration rather than discovered later.

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
