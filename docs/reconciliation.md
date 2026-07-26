# Reconciliation -- SRI Practice Health Analytics

**Status: no run has been performed against real data.** `data/raw/` is empty, so
there is nothing to reconcile. This file is **regenerated** by the pipeline on
every real run:

```bash
python -m src.report --reconciliation-out docs/reconciliation.md
```

Until then, what follows is the contract this document will be held to -- not
results.

---

## The rule

> **A variance with no name is a bug, not a rounding artifact.**

Every headline is derived from **at least two independent source files**. When
they disagree by more than the tolerance (default 1%), the harness does exactly
one thing: it **names the variance**. It never averages the sources, never picks
a "best" number silently, and never lets a difference dissolve into rounding.

## The four outcomes

| status | meaning |
|---|---|
| `RECONCILED` | sources agree within tolerance |
| `EXPLAINED` | they disagree, but named causes account for the entire gap |
| `UNRESOLVED` | a residual nobody can explain -- listed in full, by name |
| `INSUFFICIENT_SOURCES` | fewer than two independent sources exist for this headline |

That last state is deliberate. Where the Valant exports genuinely cannot
corroborate a headline twice, the honest outcome is to say so -- **not** to invent
a second derivation from the same file and call it independent.

## The cause vocabulary (ASSUMPTIONS §15)

A variance may only be attributed to these named causes:

`add_ons` · `group_attendees` · `voids` · `roster_exclusions` · `date_boundary` · `dedupe`

## Headlines and their independent sources

| headline | sources |
|---|---|
| `session_count` | charges (billable encounters), appointments (kept), documentation (signed notes) |
| `expected_revenue` | charges row sum vs Valant's own printed grand total |
| `collected_revenue` | statements row sum vs the statement's printed total |
| `unique_patients` | charges, appointments, documentation (**counts only** -- no patient key is ever emitted) |
| `active_providers` | charges (providers billing), appointments, roster snapshot |

## Provider roster findings

`active_providers` uses the compensation engine's taxonomy verbatim
(ASSUMPTIONS §1):

- `provider_not_in_config` (**block**) -- billed but not on the roster
- `therapist_inactive` (**block**) -- billed but marked inactive
- `zero_sessions` (**warning**) -- on the active roster but billed nothing

This is where a 41-vs-43 style gap (providers billing vs providers in the
compensation workbook) is carried **by name**, never resolved silently.

## PHI

This document is committed to a public repository. It contains only aggregate
counts, dollar totals, and provider-level findings. No patient identifier ever
appears in it -- enforced by `tests/test_phi_guard.py`.
