# ASSUMPTIONS — SRI Practice Health Analytics

Every definitional choice this pipeline will make, the **default** picked, and why.
Read this before I write pipeline code. Anything you disagree with, tell me and I change
the default here first — the code reads its behavior from these choices.

Legend:
- **[DEFAULT]** — the choice I'll implement unless you say otherwise.
- **[CONFIG]** — exposed as a config switch; all variants computed for comparison.
- **[RULES.md]** — a definition that the compensation engine's `RULES.md` has already
  settled. **I could not read `RULES.md` from this session** (see §0). Where marked, I
  state the default I'd use *and* flag that it must be reconciled against `RULES.md`
  before any number is trusted.

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
- **RULES.md is UNRECONCILED (decision: proceed with documented defaults).**
  `SRIMacroreports` was **empty** when this landed — no compensation engine, no
  `RULES.md`. The brief says to **reuse** the engine's settled definitions for
  **provider roster, period naming, and code handling** rather than invent parallel
  ones. Since that file is not yet reachable, every such definition below is marked
  **[RULES.md]** and uses the documented default, and `config.rules_md_reconciled`
  is `False`. **Before any headline number is trusted, these must be reconciled against
  the real `RULES.md`** — paste it, or point me at the repo/path that holds it.
- **No coupling, ever.** This analytics project will not import from, modify, or
  entangle with the compensation engine. Read-only reuse of definitions only.

---

## 1. Provider roster  **[RULES.md]**

- **[DEFAULT]** Use the roster/period/code definitions from the engine's `RULES.md`
  verbatim once available. Until then, the roster is derived from providers **observed in
  the charges export**, and every provider not on the settled roster is routed to an
  **explicit exceptions bucket** (counted, reported), never silently included or dropped.
- **Provider names in outputs: allowed.** Patient anything: never.
- **Flag:** roster membership decides "active provider count" and roster-exclusion
  reconciliation. Must match `RULES.md`.

## 2. Period naming & boundaries  **[RULES.md]**

- **[DEFAULT]** `PERIOD=YYYY-MM` names a calendar month by date of service. `2026-07` =
  service dates 2026-07-01 00:00:00 through 2026-07-31 23:59:59.999, inclusive.
- **Same period prior year** = the identically-named calendar month one year back
  (2026-07 → 2025-07). No date-shifting to align weekdays; calendar-effect normalization
  is handled separately (§10), not by moving boundaries.
- **Boundary rule:** a session at 23:59 on the last day lands in that period and only
  that period. Half-open interval `[start, next_month_start)` internally to avoid
  double-counting. Tested (property test).
- **Flag:** if `RULES.md` names periods differently (e.g. pay-period weeks, 4-4-5
  calendar), that wins. Reconcile before trusting.

## 3. Code handling (CPT/HCPCS)  **[RULES.md]**

- **[DEFAULT]** Codes are treated as opaque strings, left-padded/normalized only as
  `RULES.md` specifies. Unrecognized codes → **exceptions bucket**, counted, never
  coerced to zero or dropped.
- **Add-on list is derived from the data, not hardcoded blind** (§5).
- **Flag:** any code-normalization or code-category mapping in `RULES.md` overrides.

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

1. **Repo:** stay in `sri-analytics/` under `ISSaction`, or move to `SRIMacroreports`? (§0)
2. **RULES.md:** paste it / grant repo access so roster, period naming, and code handling
   are reconciled, not assumed? (§0–3)
3. **Maturity threshold N:** default 95% — good, or 90/98? (§11)
4. **Group therapy headline:** group = 1 session (default) or per-attendee? (§6)
5. **Session headline variant:** `billable_encounters` (default) — confirm? (§4)
