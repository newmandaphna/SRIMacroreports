"""Phase F -- reporting.

Assembles the practice-health report from the phases beneath it. Its one editorial
rule: **no headline is ever shown naked.** Every number carries, inline, the two
things that decide whether it can be trusted --

  - its maturity label (COMPLETE / INCOMPLETE / UNKNOWN, from Phase C), and
  - its reconciliation status (from Phase E).

A number without both is a number that can be misread, so the renderer attaches
them rather than leaving it to the reader.

With `data/raw/` empty the report does not fabricate anything: it stops and points
at `docs/export-request.md`, exactly like the Phase A profiler.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import loaders
from src.config import DEFAULT_CONFIG, Config
from src.decomposition import compare_all_windows
from src.dollars import DollarLadder, compute_ladder, load_payments
from src.profile_raw import discover_files
from src.reconcile import (
    ReconciliationReport,
    RosterSnapshot,
    reconcile_all,
    render_reconciliation_md,
)
from src.sessions import (
    compute_ledger,
    load_appointments,
    load_charges,
    load_notes,
)

ZERO = Decimal("0")


@dataclass
class PracticeHealthReport:
    period: str
    sources: Dict[str, str]
    ledger: object = None
    ladder: Optional[DollarLadder] = None
    windows: List = field(default_factory=list)
    reconciliation: Optional[ReconciliationReport] = None
    encounter_exceptions: Dict[str, int] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    has_data: bool = False

    def headline_label(self) -> str:
        """The label the target period carries, if the ladder computed one."""
        if self.ladder is None:
            return "UNKNOWN"
        row = self.ladder.by_period(self.period)
        return row.label if row else "UNKNOWN"


def collect_sources(data_dir: Path) -> Dict[str, str]:
    """Map report family -> path for whatever is present (deterministic order)."""
    found: Dict[str, str] = {}
    for p in discover_files(Path(data_dir)):
        fam = loaders.classify_filename(str(p))
        if fam and fam not in found:
            found[fam] = str(p)
    return found


def build_report(
    data_dir: Path,
    config: Config = DEFAULT_CONFIG,
    roster: Optional[RosterSnapshot] = None,
    payer_map: Optional[Dict[str, str]] = None,
) -> PracticeHealthReport:
    sources = collect_sources(Path(data_dir))
    rep = PracticeHealthReport(period=config.target_period, sources=sources)

    if "charges" not in sources:
        rep.notes.append(
            "no charges export present in data/raw/ -- nothing to report. The "
            "pipeline does not fabricate data; see docs/export-request.md for "
            "exactly what to pull."
        )
        return rep

    charges = load_charges(sources["charges"])
    appts = load_appointments(sources["appointments"]) if "appointments" in sources else []
    notes = load_notes(sources["documentation"]) if "documentation" in sources else []
    payments = load_payments(sources["statements"]) if "statements" in sources else []

    signed = sum(1 for n in notes if n.is_signed)
    rep.ledger = compute_ledger(charges, appts, signed, config)
    rep.ladder = compute_ladder(charges, payments, config)
    incomplete = rep.ladder.incomplete_periods()
    rep.windows, rep.encounter_exceptions = compare_all_windows(
        charges, appts, config, payer_map, "cpt", incomplete, config.target_period
    )
    rep.reconciliation = reconcile_all(
        rep.ledger, charges, payments, appts, notes, roster, config=config
    )
    rep.has_data = True

    for missing in ("appointments", "statements", "documentation"):
        if missing not in sources:
            rep.notes.append(
                f"no {missing} export present -- headlines that depend on it lose "
                "a corroborating source (see the reconciliation section)"
            )
    return rep


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def _money(d: Optional[Decimal]) -> str:
    return "--" if d is None else f"{d:,.2f}"


def render_report_md(rep: PracticeHealthReport) -> str:
    out: List[str] = [
        "# Practice Health Report -- SRI",
        "",
        f"**Target period:** `{rep.period}`",
        "",
    ]

    if not rep.has_data:
        out += [
            "## Status: NO DATA",
            "",
            *[f"> {n}" for n in rep.notes],
            "",
            "See [`export-request.md`](export-request.md) for the exact pull list.",
            "",
        ]
        return "\n".join(out)

    recon = rep.reconciliation
    label = rep.headline_label()
    out += [
        "## Status",
        "",
        f"- **Maturity of `{rep.period}`:** **{label}**"
        + ("  <- collections for this period are still arriving; do NOT compare it "
           "against a matured period without this label."
           if label != "COMPLETE" else ""),
        f"- **Reconciliation:** {'CLEAN' if recon and recon.clean else 'OPEN ITEMS'}"
        + (f" ({len(recon.unresolved)} unresolved, {len(recon.uncorroborated)} "
           f"uncorroborated)" if recon else ""),
        f"- **Sources used:** {', '.join(f'`{k}`' for k in sorted(rep.sources))}",
        "",
    ]

    # --- headlines, each with BOTH labels attached ---
    out += [
        "## Headlines",
        "",
        "_Every headline carries its maturity label and its reconciliation status. "
        "A number without both can be misread._",
        "",
        "| headline | value | maturity | reconciliation | sources |",
        "|---|---:|---|---|---:|",
    ]
    if recon:
        for v in recon.variances:
            money_headline = "revenue" in v.headline
            val = _money(Decimal(str(v.reported_value))) if money_headline \
                else str(v.reported_value)
            mat = label if v.headline in (
                "expected_revenue", "collected_revenue", "session_count") else "n/a"
            out.append(
                f"| {v.headline} | {val} | {mat} | {v.status} | {len(v.values)} |"
            )
    out.append("")

    # --- session ledger ---
    led = rep.ledger
    out += [
        "## Session ledger (Phase B)",
        "",
        "| grain | sessions |",
        "|---|---:|",
    ]
    for g in ("kept_appointments", "billable_encounters", "charge_lines", "signed_notes"):
        star = "  **(headline)**" if g == led.headline_grain else ""
        out.append(f"| {g}{star} | {led.grains[g]} |")
    out += [
        "",
        f"- **Group therapy:** one-per-group = {led.group['one_per_group']}, "
        f"per-attendee = {led.group['per_attendee']} (both reported, §6)",
        f"- **Voids excluded but counted:** {led.voids['n_lines']} line(s), "
        f"expected {led.voids['expected_sum']}",
        f"- **Duplicate lines removed:** {led.dedupe['n_duplicate_lines_removed']}",
        f"- **Add-ons (operative, derived):** {', '.join(led.addons['operative']) or 'none'}"
        + (f"; in seed but not observed: {', '.join(led.addons['seed_not_observed'])}"
           if led.addons["seed_not_observed"] else ""),
        f"- **Appointment status:** " + ", ".join(
            f"{k}={v}" for k, v in sorted(led.appointment_status.items()) if v),
        f"- **Appointments-vs-charges gap:** "
        f"{led.reconciliation_gap['appts_without_charge']} kept appt(s) with no "
        f"charge (non-billable), "
        f"{led.reconciliation_gap['charges_without_appt']} encounter(s) with no "
        f"kept appointment (unbilled revenue)",
        "",
    ]

    # --- dollar ladder ---
    out += [
        "## Dollar ladder (Phase C)",
        "",
        "_Three measures, never substituted for one another. `expected` is the "
        "headline; `billed` is context only._",
        "",
        "| period | billed (context) | expected (HEADLINE) | collected | maturity | label |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for r in rep.ladder.periods:
        mat = f"{r.maturity:.4f}" if r.maturity is not None else "--"
        out.append(
            f"| {r.period} | {_money(r.billed_charges)} | "
            f"{_money(r.expected_collection)} | {_money(r.collected)} | {mat} | "
            f"{r.label} |"
        )
    out.append("")
    if rep.ladder.reference_periods:
        out.append(
            f"- Maturity curve fit from fully-matured months: "
            f"{', '.join(rep.ladder.reference_periods)} (as of {rep.ladder.as_of})"
        )
    for n in rep.ladder.notes:
        out.append(f"- {n}")
    out.append("")

    # --- windows ---
    out += [
        "## Year-over-year windows (Phase D)",
        "",
        "_Volume / rate / mix always sum exactly to the total change._",
        "",
    ]
    for w in rep.windows:
        d = w.decomposition
        out.append(f"### {w.name}  ({w.label})")
        out.append("")
        out.append(
            f"- current `{w.current.months[0]}..{w.current.months[-1]}`: "
            f"{w.current.sessions} sessions, expected {_money(w.current.expected)}, "
            f"{w.current.clinic_days} clinic days, "
            f"{w.current.sessions_per_clinic_day} sessions/clinic-day"
        )
        out.append(
            f"- prior `{w.prior.months[0]}..{w.prior.months[-1]}`: "
            f"{w.prior.sessions} sessions, expected {_money(w.prior.expected)}, "
            f"{w.prior.clinic_days} clinic days, "
            f"{w.prior.sessions_per_clinic_day} sessions/clinic-day"
        )
        if d.defined:
            out.append(
                f"- **change {_money(d.total_change)}** = volume "
                f"{_money(d.volume_effect)} + rate {_money(d.rate_effect)} + mix "
                f"{_money(d.mix_effect)}"
            )
        else:
            out.append("- decomposition UNDEFINED (no prior baseline) -- not fabricated")
        if w.incomplete_periods:
            out.append(
                f"- **INCOMPLETE**: {', '.join(w.incomplete_periods)} still maturing"
            )
        out.append("")

    # --- reconciliation ---
    out += ["## Reconciliation (Phase E)", ""]
    if recon:
        out.append(
            f"- tolerance {recon.tolerance}; disagreements are **named, never averaged**"
        )
        for v in recon.variances:
            out.append(f"- `{v.headline}`: {v.status} (spread {v.spread})")
        out.append("")
        out.append("Full detail: [`reconciliation.md`](reconciliation.md)")
    out.append("")

    exc = {k: v for k, v in rep.encounter_exceptions.items() if v}
    if exc or rep.notes:
        out += ["## Exceptions and caveats", ""]
        for k, v in sorted(exc.items()):
            out.append(f"- `{k}`: {v}")
        for n in rep.notes:
            out.append(f"- {n}")
        out.append("")
    return "\n".join(out)


def report_summary(rep: PracticeHealthReport) -> Dict[str, object]:
    """Aggregate-only JSON summary (deterministic; safe to print)."""
    recon = rep.reconciliation
    summary: Dict[str, object] = {
        "period": rep.period,
        "has_data": rep.has_data,
        "sources": sorted(rep.sources),
        "maturity_label": rep.headline_label(),
    }
    if rep.has_data:
        summary["grains"] = dict(sorted(rep.ledger.grains.items()))
        summary["headline_grain"] = rep.ledger.headline_grain
        summary["totals"] = {k: str(v) for k, v in sorted(rep.ladder.totals.items())}
        summary["incomplete_periods"] = rep.ladder.incomplete_periods()
        summary["reconciliation"] = {
            "clean": recon.clean if recon else None,
            "statuses": {v.headline: v.status for v in (recon.variances if recon else [])},
            "unresolved": [v.headline for v in (recon.unresolved if recon else [])],
            "uncorroborated": [v.headline for v in (recon.uncorroborated if recon else [])],
        }
        summary["windows"] = {
            w.name: {
                "label": w.label,
                "total_change": str(w.decomposition.total_change),
                "volume": str(w.decomposition.volume_effect),
                "rate": str(w.decomposition.rate_effect),
                "mix": str(w.decomposition.mix_effect),
            }
            for w in rep.windows if w.decomposition.defined
        }
    return summary


REPO_ROOT = Path(__file__).resolve().parent.parent


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="SRI practice-health report (Phase F).")
    ap.add_argument("--data-dir", default=str(REPO_ROOT / "data" / "raw"))
    ap.add_argument("--period", default=DEFAULT_CONFIG.target_period)
    ap.add_argument("--out", default=None, help="write the report markdown here")
    ap.add_argument("--reconciliation-out", default=None,
                    help="write docs/reconciliation.md here")
    ap.add_argument("--json", action="store_true", help="emit the summary as JSON")
    args = ap.parse_args(argv)

    cfg = Config(target_period=args.period)
    rep = build_report(Path(args.data_dir), cfg)

    if args.out:
        Path(args.out).write_text(render_report_md(rep), encoding="utf-8")
    if args.reconciliation_out and rep.reconciliation is not None:
        Path(args.reconciliation_out).write_text(
            render_reconciliation_md(rep.reconciliation), encoding="utf-8"
        )

    if args.json:
        print(json.dumps(report_summary(rep), sort_keys=True, indent=2, default=str))
        return 0

    print(render_report_md(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
