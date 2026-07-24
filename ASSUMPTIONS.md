# ASSUMPTIONS — SRI Practice Health Analytics

Every definitional choice this pipeline will make, the **default** picked, and why.
Read this before I write pipeline code. Anything you disagree with, tell me and I change
the default here first — the code reads its behavior from these choices.

Legend:
- **[DEFAULT]** — the choice I'll implement unless you say otherwise.
- **[CONFIG]** — exposed as a config switch; all variants computed for comparison.
- **[RULES.md]** — a definition the compensation engine has already settled. There is no
  literal `RULES.md` file; the settled rules live in the engine's code
  (`newmandaphna/SRIcompensation`). As of Gate 0 these are **reconciled against that
  engine** with the source lines cited inline. `src/config.py` tracks which sections are
  reconciled (`rules_reconciled`); a section still open is called out explicitly.

---

## 0. Scope, repo, and the RULES.md gap  ← READ FIRST

- **Where this lives — RESOLVED.** The brief said "its own repo or its own top-level
  directory." `SRIMacroreports` was reached and is now the home: this project **is** the
  `SRIMacroreports` repo (root-level `src/`, `data/`, `docs/`, `tests/`, `.gitignore`).
  It was first scaffolded as a self-contained `sri-analytics/` directory inside
  `ISSaction` because that earlier session could not switch repos; it relocated here
  unchanged. It imports nothing from, and modifies nothing in, any other codebase.
- **The repo is PUBLIC.** This raises the stakes on the PHI rule. `data/raw/` is
  gitignored and proven so (`git check-ignore`). Every committed artifact is aggregate.
- **RULES.md RECONCILED against the engine (Gate 0).** There is no literal `RULES.md`;
  the settled rules live in the engine repo `newmandaphna/SRIcompensation`. Gate 0 read
  it and reconciled the three flagged areas — **provider roster (§1), period naming (§2),
  code handling (§3)** — with source lines cited inline. `config.rules_reconciled` now
  records `{roster, periods, codes}` as **definitionally** reconciled. Two honest caveats:
  (a) *definition reconciled ≠ implemented* — the canonical-name loaders, the
  dual-granularity period model, and non-session-code exclusion are built in Gates 1–3;
  (b) headline numbers still need real data (`data/raw/` is empty) before they mean
  anything.
- **No coupling, ever.** This analytics project does not import from, modify, or entangle
  with the engine. Gate 0 was **read-only** reuse of its definitions. Where the analytics
  needs live engine state (the provider roster), it consumes a **read-only snapshot as an
  input** (see §1), never a code dependency.

---

## 1. Provider roster  **[RULES.md — reconciled]**

- **Authoritative roster = the engine's roster**, i.e. `config.therapists` +
  `provider_map`, seeded from the comp-matrix "Data" tab (`seed.py:68`) and thereafter
  edited in the engine's Admin Center (the store is source of truth). Sizes observed:
  **seed = 43** (`tests/test_seed_store.py:118`), **live = 48** (44 active / 4 inactive),
  `provider_map` = 41 entries. `ProviderID → therapist` resolves through
  `build_provider_index` (explicit map wins) — `engine.py:208-216`.
- **[DECISION] Roster is a runtime *snapshot input*.** Because the roster drifts (43→48),
  the analytics does not hardcode it and does not read the engine repo live. It consumes a
  read-only roster snapshot (provider_map + therapist name/status/active) exported for the
  period under analysis, alongside the Valant exports. No code coupling.
- **Off-roster taxonomy (adopt the engine's, verbatim):**
  - billed but not mapped to any therapist → `provider_not_in_config` (**block**) —
    `engine.py:298`
  - mapped but marked inactive, yet billed → `therapist_inactive` (**block**) —
    `engine.py:313`
  - on roster but zero billed sessions → `zero_sessions` (**warning**) — `engine.py:478`
- **Carry the 41-vs-43 discrepancy by name.** 41 providers billed in the sample period vs
  43 in the comp workbook; the 2 non-billers are expected salaried/new/inactive
  (`Build Brief:215, 223`). Both sides surface in the exceptions report — never resolved
  silently.
- **Provider names in outputs: allowed.** Patient anything: never.

## 2. Period naming & boundaries  **[RULES.md — reconciled; DUAL GRANULARITY]**

The engine does **not** use calendar months. Its atomic period is a **semi-monthly pay
period** split at day 15, identified by a free-text label `"{YYYY-MM} Period {1|2}"`
(e.g. `2026-06 Period 1`) that the user names and that is the primary key for
adjustments/history; the pay year is regex-parsed from the label
(`views/run_period.py:773-779`, `history.py:14-24`). Period membership is the set of
charge rows in the exported file, optionally narrowed by an **inclusive** date-of-service
slice `[date_from, date_to]` (`importer.py:515-534`).

- **[DECISION] Dual granularity — the engine wins on the atomic unit; analytics rolls up.**
  - **Atomic period = the engine's semi-monthly labeled DOS window.** `src/periods.py`
    adopts: the label format `"{YYYY-MM} Period {1|2}"`, the day-15 split (Period 1 =
    days 1–15 inclusive), inclusive DOS bounds, and year-from-label. (Built in Gate 1/3.)
  - **Analytics reporting windows aggregate whole pay-periods up to calendar months.**
    The practice-health headlines (current vs same period prior year, rolling 12, trailing
    3 — §14) are composed of *whole* pay-periods, so a monthly figure always equals the sum
    of its two pay-periods and reconciles exactly to payroll. No half-period ever splits a
    reporting window.
- **Boundary discipline:** internally the rollup uses non-overlapping windows so no charge
  lands in two reporting windows; the engine's atomic slice is inclusive on both ends, and
  `periods.py` matches that when reproducing an engine period. Property-tested.
- **Note:** this supersedes the earlier calendar-month-only model and the monthly framing
  in `docs/export-request.md`, which is updated to request whole pay-periods across the
  same date range.

## 3. Code handling (CPT/HCPCS)  **[RULES.md — reconciled]**

- **Codes are opaque strings** (never numeric-coerced — a `code` dtype, §Gate 1), with
  one settled normalization: **TELE-prefix stripping.** A code beginning `TELE`
  (case-insensitive) has the 4-char prefix removed → base CPT (`TELE90837` → `90837`);
  telehealth-ness is preserved as a separate flag derived from the `TELE` prefix **or** a
  `GT`/`95` modifier (`valant_parse.py:74`, `importer.py:52,151`).
- **Non-session codes (settled set):**
  `NON_SESSION_CODES = {99998, 99999, "BAL FWD", "QBCHK", "ADHD2", "FORM FEE", "ADHD", ""}`
  (`valant_parse.py:27`). These are **never billable sessions** — they must not increment
  any session grain (§4–§9) and are reported as their own categories. Matched on the
  TELE-stripped code. The engine's *pay* treatments for these
  (`99999/99998/BAL FWD`→exclude, `QBCHK`→$15/unit, `ADHD2`→pending; `seed.py:127-140`)
  are **payroll concerns, out of scope here** — recorded only so the two projects agree on
  what the codes *are*.
- **Payer → `payer_category`** (feeds the §13 mix decomposition): reuse the engine's
  **explicit `payer_map` table**, closed set of **7 categories** —
  `Commercial, Medicare, Medicaid/CHIP, Self-Pay, QMB, LifeStance, EAP/Other`
  (`models.py:136-139`) — with exact lookup + a fee-suffix-strip fallback
  (`"Private Pay $80"` → base) and **unmapped → `None` + warning, never keyword-guessed**
  (`engine.py:185`). Do not build a second payer classifier.
- **Canonical column names:** align `src/loaders.py` to the engine's names so the two
  projects call a column the same thing — charges via `CHARGES_MAPPING` + `COLUMN_ALIASES`
  (`ProviderID, CPTCode, TransactionCode, Units, Amount, ExpectedCollectionAmount,
  Insurance123, Modifiers`, DOS = `DateOfService`; `importer.py:27-93`); statements via
  raw names (`GroupingLevel1, InsurancePayments, PatientPayments`, DOS =
  `ChargeDateOfService`; `statements.py:21`). (Wired in Gate 1.)
- **Add-on list is still derived from the data, not hardcoded blind** (§5); the engine has
  no session-grain add-on-collapsing rule to reuse (it aggregates dateless
  provider×code×payer), so §5 remains ours.

---

## 4. Session definition (Phase B)  **[CONFIG]**

- **[DEFAULT] session = one distinct billable encounter = one patient × one provider ×
  one date of service.** Add-on codes and multi-line charges collapse into one session.
- **[CONFIG]** All four variants computed every run for comparison:
  `kept_appointments | billable_encounters | charge_lines | signed_notes`.
  The headline uses `billable_encounters` (the default above); the others are reported
  alongside so disagreements are visible, not hidden.

## 5. Add-on codes that must NOT create a separate session

- **[DEFAULT]** Add-ons are **collapsed** into their parent encounter (they do not
  increment session count). Seed list from the brief: psychotherapy add-ons with E/M
  **90833, 90836, 90838** and interactive complexity **90785**.
- **The operative list is *derived from what actually appears in the data*** (codes seen
  co-occurring on the same patient×provider×DOS as a primary E/M or psychotherapy code),
  cross-checked against the seed list. Anything in the seed list not seen in data, and
  anything add-on-looking in data not in the seed list, is **reported**, not silently
  assumed. Tested: adding an add-on line to an existing encounter does not change the
  session count.

## 6. Group therapy (90853)

- **[DEFAULT]** One group appointment = **one session** at the practice/provider grain
  (a group is one unit of provider time). **Both views reported:** "group = 1 session"
  **and** "group = 1 per attendee," clearly labeled, so utilization vs. billing
  perspectives are both visible. The default headline uses **one per group**.
- **Flag:** if you want attendee-weighted volume as the headline, flip the switch.

## 7. Unit-based rows (units > 1)

- **[DEFAULT]** Units affect **dollars and billed-charge math**, not **session count**
  (units > 1 on one encounter is still one session). Units are carried through to the
  dollar ladder faithfully. Rows with missing/zero/negative units where a positive unit
  is expected → exception.

## 8. Voided / reversed / zero-dollar charge lines

- **[DEFAULT]** Voided and reversed lines are **excluded from session counts and from
  expected/collected dollars**, but **counted and reported** as their own category (they
  are a reconciliation cause, not noise to drop). Zero-dollar lines are kept but flagged;
  a legitimately zero-dollar encounter is distinguished from a *missing-price* row — the
  latter is an exception (no silent zero).

## 9. Duplicate encounter IDs

- **[DEFAULT]** Dedupe to one row per encounter ID (within the session grain), and
  **count how many were deduped** as a first-class number. Tested: a duplicated encounter
  ID dedupes rather than double-counting. Dedupe is deterministic (stable sort on
  explicit keys, not dict/order-dependent).

## 9b. No-shows and cancellations

- **[DEFAULT]** **Not** sessions, but **kept as their own status** and reported (they are
  the utilization signal). Never dropped, never counted as sessions.

## 9c. Appointments-vs-charges reconciliation gap

- **[DEFAULT]** The gap between appointment-side count and charge-side count is emitted
  as a **first-class number** and **classified** (non-billable appointment vs. unbilled
  revenue), never absorbed. (We previously saw ~88 more appointment rows than charge
  lines in one period — that gap gets a name and a cause, per §Reconciliation.)

---

## 10. Dollar ladder (Phase C) — three separate measures, never blended

- **[DEFAULT]** Three distinct columns, never substituted for one another:
  1. `billed_charges` — gross list price. **Context only, not a health metric.**
  2. `expected_collection` — contracted/expected at date of service. **THE HEADLINE.**
  3. `collected` — actual cash from `PatientStatement` (insurance + patient).
- **All money is `Decimal`, never float.** Rounding only at presentation. A test asserts
  money columns never silently become float.
- **Everything bucketed by DATE OF SERVICE**, not posting/payment date.
- **The distinction that must never blur (reconciled with the engine).** Valant's `Amount`
  (billed/gross) and `ExpectedCollectionAmount` (the **Contracted Rate** — Insurance Aging
  Detail *New Fee Schedule*, **Column W, not Column V**) are what **Valant bills and
  expects to collect from payers**. They are **NOT** SRI's clinician pay schedule. The
  clinician pay rates are a separate, CPT-keyed `RateSchedule` (`models.py:98-110`),
  admin-entered in the engine, and are what determine therapist compensation
  (`engine.py:798-805`). **This analytics project uses the Valant fee columns only**
  (`expected_collection` is the headline); the engine's pay schedule is out of scope and
  is never read, mixed in, or confused with the Valant fee. A future session must not
  substitute one for the other.

## 11. Claim-lag maturity model (Phase C)

- **[DEFAULT]** Fit lag-to-collection curves from the **prior year's** fully-matured
  service months (cumulative collected ÷ expected, by months-since-DOS). Any period whose
  collection is **< N% mature is labeled `INCOMPLETE`** in *every* output.
- **[CONFIG] N (maturity threshold) default = 95%.** A recent immature period is **never**
  compared against a fully-matured prior-year period without the `INCOMPLETE` label
  attached.
- **Flag:** N is a judgment call; 95% is the default. Tell me if you want 90% or 98%.

## 12. Calendar normalization (Phase D)

- **[DEFAULT]** Count **business days** and **clinic days** in each window; report volume
  **both raw and per-clinic-day**. "Clinic days" = days the practice actually had
  appointments (derived from the appointments export), distinct from generic business
  days. A window with an extra Monday is normalized so it isn't read as a real signal.

## 13. Volume / rate / mix decomposition (Phase D)

- **[DEFAULT]** YoY change in expected revenue split as:
  - `volume effect = (Δ sessions) × (prior-year expected yield per session)`
  - `rate effect   = (Δ yield per session) × (current-year sessions)`
  - `mix effect    = residual`, attributed to payer-mix and CPT-mix shift.
- The three components **must sum to the total change**; a test asserts this for random
  inputs. This split is the point of the exercise.

## 14. Comparison windows (Phase D)

- **[DEFAULT]** All three produced every run:
  current vs same period prior year; rolling 12 vs prior rolling 12; trailing 3 vs same 3
  prior year.

---

## 15. Reconciliation harness (Phase E)

- **[DEFAULT]** Every headline (session count, expected revenue, collected revenue,
  unique patients, active providers) is derived from **≥2 independent source files**. If
  they differ by **> 1%**, do **not** average — emit a reconciliation exception with the
  row-level cause categorized (add-ons, group attendees, voids, roster exclusions, date
  boundary, dedupe). `docs/reconciliation.md` names every unresolved variance.

## 16. Determinism & no-silent-fallback (global)

- **[DEFAULT]** Same inputs → byte-identical outputs. No wall-clock in logic, no
  dict-ordering dependence, no random sampling in the analysis path. Any random data is
  synthetic, test-only, seeded, and lives in `./tests/fixtures/` — never in `./data/`.
- **[DEFAULT]** Missing column / unparseable date / off-roster provider / unrecognized
  code → **fail loudly or route to a counted exceptions bucket**. A bad row never
  silently becomes a zero.

## 17. Unique patients (aggregate only)

- **[DEFAULT]** "Unique patients seen" is computed from a patient key **in memory** and
  **only the count** is emitted. No patient key, name, ID, DOB, or diagnosis is ever
  printed, logged, or written to any tracked file.

---

## Open questions for you (defaults chosen, but your call)

1. ~~**Repo:** stay in `sri-analytics/` under `ISSaction`, or move to `SRIMacroreports`?~~
   **RESOLVED (§0):** home is `SRIMacroreports`.
2. ~~**RULES.md:** paste it / grant repo access so roster, period naming, and code
   handling are reconciled?~~ **RESOLVED (Gate 0):** reconciled against the
   `SRIcompensation` engine; see §1–§3, §10.
3. ~~**Period model:** calendar month vs the engine's semi-monthly?~~ **RESOLVED (Gate 0):**
   dual granularity — engine's semi-monthly is the atomic unit, analytics rolls up to
   months (§2).
4. ~~**Roster source:**~~ **RESOLVED (Gate 0):** runtime snapshot input (§1).
5. **Maturity threshold N:** default 95% — good, or 90/98? (§11)
6. **Group therapy headline:** group = 1 session (default) or per-attendee? (§6)
7. **Session headline variant:** `billable_encounters` (default) — confirm? (§4)
