# ASSUMPTIONS

Every definitional choice made while building the SRI Practice Dashboard, recorded at the
moment it was made. Read this after every phase. Anything marked **NEEDS CONFIRMATION** is a
default I picked to keep moving, not a decision I am confident in.

Last updated: Phase 0.

---

## 0. Scope and source of truth

**A-001. The requirements doc is missing.**
The build prompt says to read "SRI Practice Dashboard Requirements.md" in the repo root before
writing any code. That file is not in the repository and was not supplied. I am proceeding with
the build prompt alone, which the prompt itself designates as the source of truth for
architecture, security, data design, and sequencing. Scope items that live only in the
requirements doc are therefore unknown to me.
**NEEDS CONFIRMATION**: send the requirements doc, or confirm the build prompt is the whole scope.

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
Consequence: the tab list the app must show in the Data Sources mapping UI is the tab list of the
**live Q workbook**, which I have never seen. I will not hardcode any tab name.
**NEEDS CONFIRMATION**: the URL of the actual Q2 Google Sheet, so the mapping UI can list its
real tabs.

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
| Sessions after excluding CPT 99998 and 99999 | 8,477 |
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
`(source_id, therapist_id, patient_name_normalized, dos, cpt_normalized)`.
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

**A-030. A session is one imported row whose normalized CPT is not on the exclusion list.**
Default exclusion list: `99998`, `99999`. On the Q2 data that is 648 rows of 99998 and 264 rows of
99999, giving 8,477 sessions out of 9,389 rows.

**A-031. Excluded CPTs are excluded from session counts but NOT from revenue.**
This is a distinction the build prompt does not make, and it matters. The 264 rows coded 99999
carry **17,674.46 in Total paid**. If 99999 rows were dropped from revenue as well as from session
counts, collected revenue would be understated by that amount. So: the exclusion list governs
`session_count` only. `revenue_collected` and `revenue_outstanding` sum over every imported row.
**NEEDS CONFIRMATION**, since it changes headline revenue by about 2 percent.

**A-032. What 99998 and 99999 mean is still unknown.**
The `RAW_Unrecorded` tab shows CPT 99998 alongside a "do not bill" billing comment, which supports
reading 99998 as an internal no show or non billable placeholder. 99999 carrying real payments
does not fit that reading, so the two codes probably do not mean the same thing. Both are retained
in the database for later no show analysis in Phase 6.
**NEEDS CONFIRMATION**: what each code means. This is the single most useful answer you can give me.

**A-033. CPT is a normalized string, not a number.**
Excel has stored the numeric codes as floats (`90837.0`). The sheet also contains 53 rows whose
CPT is not a code at all: `QBCHK` (19), `90791-ADHD` (18), `ADHD` (6), `ADHD2` (4), `pro bono` (3),
`90837 - ADHD` (2), `FORM` (1).
Normalization rule: trim, uppercase, and drop a trailing `.0` from float valued codes, so `90837.0`
becomes `90837`. Everything else is stored verbatim as an uppercase string. Non numeric CPTs are
imported and counted as sessions by default, because `90791-ADHD` and `90837 - ADHD` are clearly
real clinical visits.
**NEEDS CONFIRMATION**: whether `QBCHK`, `FORM`, and `pro bono` should count as sessions. My
default counts all three. Nineteen `QBCHK` rows is not nothing.

**A-034. Revenue collected is `sum(total_paid)`. Revenue outstanding is `sum(total_balance)`.**
The patient side and insurance side split uses `pt_amount_due` and `ins_balance`.
Note that these do not reconcile exactly: on the Q2 data, `sum(pt_amount_due) + sum(ins_balance)`
is 198,328.86 against a `sum(total_balance)` of 198,520.62, a gap of 191.76 (one visit's worth).
The dashboard shows `total_balance` as the outstanding headline and shows the split beneath it,
labelled as a split rather than as components that must add up. The gap is surfaced in the import
summary rather than hidden.

**A-035. Weeks start Monday. Week labels are the Monday date.**
Configurable via `week_start_day`, default Monday. Quarters are calendar quarters. This matches the
Q sheet, whose Q2 runs 2026-04-01 to 2026-06-30.

**A-036. Timezone is America/New_York, and DOS is a date with no time.**
The practice is in Pennsylvania (Jenkintown, Revere Commons). All dates of service in the sheet
carry a midnight time component, which is an Excel artifact, so DOS is stored as a `DATE`.
Timestamps that are genuinely moments in time (audit log, sync runs, imported_at) are stored as UTC
and rendered in America/New_York.

**A-037. Blank money is 0. Unparseable money is a rejected row.**
Per the build prompt: a blank money cell reads as 0. A money cell that will not parse does not read
as 0, it sends the row to `import_errors` with the raw string preserved, so nobody silently loses a
payment to a typo.

**A-038. Eleven rows have a blank DOS.**
A row with no date of service cannot be placed in any period, so it cannot be counted. Those rows
are rejected to `import_errors` for admin review rather than imported and quietly excluded from
every report.

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
**NEEDS CONFIRMATION**: are `PAVLOVA` and `ROSENFELD` the same therapist?

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
Server side expiry. The client warning is a courtesy; the server is what enforces it. Configurable
per the build prompt.

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
   applies to revenue. It cannot apply to revenue without losing 17,674.46. See A-031.
4. **Patient Code column position.** The prompt's allowlist lists Patient Code in header order after
   the money columns. In the actual sheet it is column S, after Total balance, with `Recorded` in T.
   The mapping is by header text, not position, so this is handled, but it confirms that positional
   mapping would be wrong.
