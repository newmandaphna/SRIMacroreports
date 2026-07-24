# Valant Export Request — SRI Practice Health Analytics

**Why you're reading this:** `./data/raw/` is empty, so there is nothing to profile and
nothing to analyze. Per the project's hard rule, the pipeline does **not** fabricate
data. Pull the reports below, drop them in `./data/raw/` (which is gitignored — they
never get committed), and I'll run Phase A for real.

**Target reporting period:** `PERIOD=2026-07` (July 2026).
**Report generation date assumed:** 2026-07-24 (today). Pull "as of" the latest
available date so payment postings are as complete as possible.

---

## The one rule that governs every export: **date basis = DATE OF SERVICE**

Every window in this project is bucketed by **date of service (DOS)** — not posting
date, not payment date. Where a report lets you choose the date basis, choose
**Date of Service**. The *one* exception is the payment/posting date on
`PatientStatement`, which we need **in addition to** DOS to build the claim-lag
maturity model. So for statements: DOS for bucketing **and** posting date for lag.

---

## Date range to pull (all reports): **2024-01-01 → 2026-07-31** (by DATE OF SERVICE)

Rationale — the analysis needs three things, and each drives the range:

| Need | Windows required | Earliest DOS needed |
|---|---|---|
| Current vs same period prior year | 2026-07 vs 2025-07 | 2025-07-01 |
| Rolling 12 vs prior rolling 12 | 2025-08→2026-07 vs 2024-08→2025-07 | 2024-08-01 |
| Trailing 3 vs same 3 prior year | 2026-05→07 vs 2025-05→07 | 2025-05-01 |
| Claim-lag maturity curves (built from a *fully matured* prior year) | needs a prior year whose collections are essentially complete, observed month-by-month | 2024-01-01 |

The binding constraint is the maturity model: to know how "mature" July 2026's
collections are, I fit a lag-to-collection curve on **older** service months whose
collections have finished arriving. That requires a clean, fully-matured stretch of
2024 service dates with their payment postings. **Start at 2024-01-01.** If you can
easily go back to 2023-01-01, even better (more curve stability) — but 2024-01 is the
floor.

**Payment/posting completeness:** for `PatientStatement`, pull postings through the
**latest available date** (do not cap postings at 2026-07-31 — we want every payment
that has landed for those service dates, however late it posted).

---

## The four reports (exact report names, grouping, date basis)

### 1. Charges — `ChargesHistoryDetailProviderPatientCode` (the "NewFeeSchedule" variant)
- **Grouping:** Provider → Patient → Code.
- **Date basis:** Date of Service.
- **Date range:** 2024-01-01 → 2026-07-31 (DOS).
- **Must include columns:** Provider, Date of Service, CPT/HCPCS code, Modifier(s),
  Units, **Billed/charge amount**, **Contracted/expected (fee-schedule) amount**,
  charge-line status (so voids/reversals are visible), and an encounter or charge ID.
- **Heads-up:** this variant has the three-line preamble (Textbox row, date row, blank)
  before the header. Leave it as-is; the loader detects the header programmatically.
  Do **not** hand-edit the file to remove the preamble.

### 2. Appointments — `AppointmentsPatientInfoByProviderThenDayThenFacility`
- **Grouping:** Provider → Day → Facility.
- **Date basis:** Appointment date.
- **Date range:** 2024-01-01 → 2026-07-31.
- **Must include:** Provider, Appointment date, Facility, **Appointment status**
  (kept / arrived / no-show / cancelled / rescheduled), Appointment type, and a
  new-patient / intake indicator if available.
- **Why:** this is the utilization signal (no-shows, cancellations) and one half of the
  appointments-vs-charges reconciliation gate.

### 3. Payments/cash — `PatientStatement` (GroupingLevel3)
- **Grouping:** GroupingLevel3 (as previously used).
- **Date basis:** Date of Service for bucketing **plus** posting/payment date retained.
- **Date range:** service dates 2024-01-01 → 2026-07-31; **postings through latest
  available**.
- **Must include:** **InsurancePayments**, **PatientPayments**, Date of Service,
  posting/payment date, Payer, and (if available) CPT and Provider.
- **Why:** this is the only source of *actual cash collected* and the only source that
  lets us measure claim lag.

### 4. Note status — `AppointmentDocumentation`
- **Date basis:** Date of Service.
- **Date range:** 2024-01-01 → 2026-07-31 (2026-05→07 is the part that matters most,
  but pull the full range for consistency).
- **Must include:** Provider, Date of Service, **note status** (signed / unsigned /
  co-sign pending), signed date if available.
- **Why:** LEAKAGE — "sessions held for unsigned notes."

---

## File format & handling notes

- **Format:** CSV or XLSX, whichever Valant produces natively. Don't convert or clean
  them — the loader is built to handle Valant's raw quirks. Hand-cleaning hides the very
  problems the reconciliation harness is designed to catch.
- **Filenames:** keep Valant's default report name in the filename so the loader can
  route each file to the right parser (e.g. anything starting
  `ChargesHistoryDetailProviderPatientCode…`).
- **Where to put them:** `./data/raw/`. This directory is gitignored and verified so —
  the PHI never enters version control, even though the repo is public.
- **Do not** rename columns, delete the preamble, filter rows, or "fix" anything in
  Excel first.

---

## What happens after you drop the files in

1. Phase A profiler runs and rewrites `docs/data-dictionary.md` with the **real**
   inventory (row counts, columns, dtypes, date ranges, grain) — values never printed.
2. If prior-year coverage is confirmed, I proceed to Phases B–F.
3. If any report is missing a required column or the prior-year window is short, I stop
   again and tell you exactly what's short — no silent fallbacks.

---

## Quick checklist to hand to whoever pulls the exports

- [ ] `ChargesHistoryDetailProviderPatientCode` (NewFeeSchedule), Provider→Patient→Code,
      **DOS basis**, 2024-01-01→2026-07-31, includes contracted/expected amount.
- [ ] `AppointmentsPatientInfoByProviderThenDayThenFacility`, Provider→Day→Facility,
      2024-01-01→2026-07-31, includes appointment status.
- [ ] `PatientStatement` GroupingLevel3, **DOS + posting date**, service dates
      2024-01-01→2026-07-31, **postings through latest available**, includes
      InsurancePayments + PatientPayments.
- [ ] `AppointmentDocumentation`, **DOS basis**, 2024-01-01→2026-07-31, includes note
      status.
- [ ] All four dropped in `./data/raw/`, unmodified, original filenames.
