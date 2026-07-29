"""The Reports area: overview dashboard, financial module, and CSV export.

Access is gated per module by the same dependency the rest of the app uses, so a
viewer without the financial grant gets a 403 from the route, not a hidden nav link.

Every query these routes run is aggregate or therapist grain. Nothing here can select
a patient name or code (see app/reporting/queries.py).
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from app import config_store
from app.config_store import PracticeConfig
from app.models.enums import AuditAction, Module
from app.reporting import queries
from app.reporting.compare import year_over_year
from app.reporting.insights import build_insights
from app.reporting.metrics import Kpi
from app.reporting.periods import (
    PICKER_PRESETS,
    DateRange,
    Granularity,
    resolve_range,
    today_in,
)
from app.reporting.weekly import WEEK_WINDOW_CHOICES, parse_week_count, weekly_counts
from app.security import audit
from app.security.deps import AuthContext, DbSession, require_module
from app.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports", tags=["reports"])

FinancialUser = Annotated[AuthContext, Depends(require_module(Module.FINANCIAL))]


# --------------------------------------------------------------------------- context


class ReportContext:
    """Everything a report page needs: filters, config, and the resolved range."""

    def __init__(
        self,
        db: DbSession,
        request: Request,
        *,
        preset: str | None,
        start: str | None,
        end: str | None,
        therapist_ids: tuple[int, ...],
        locations: tuple[str, ...],
        granularity: str | None,
    ) -> None:
        settings = request.app.state.settings
        self.config: PracticeConfig = config_store.load(db, settings)
        self.coverage = queries.coverage(db)

        self.range: DateRange = resolve_range(
            preset,
            start,
            end,
            timezone=self.config.timezone,
            week_starts_monday=self.config.week_starts_monday,
            data_min=self.coverage.min_date,
            data_max=self.coverage.max_date,
        )

        # Nobody chose a period, and the default window misses the imported data
        # entirely (a quarter synced in July whose sessions end in June, say). An
        # empty dashboard with a "show everything" button is a chore; showing
        # everything IS the right default. An explicitly picked range that misses
        # still gets the honest empty state.
        nothing_chosen = preset is None and start is None and end is None
        if (
            nothing_chosen
            and self.coverage.has_data
            and self.coverage.max_date is not None
            and self.coverage.min_date is not None
            and (
                self.coverage.max_date < self.range.start or self.coverage.min_date > self.range.end
            )
        ):
            self.range = resolve_range(
                "all_time",
                timezone=self.config.timezone,
                week_starts_monday=self.config.week_starts_monday,
                data_min=self.coverage.min_date,
                data_max=self.coverage.max_date,
            )

        self.filters = queries.Filters(
            start=self.range.start,
            end=self.range.end,
            cpt_exclusions=self.config.cpt_exclusions,
            therapist_ids=therapist_ids,
            locations=locations,
        )

        try:
            self.granularity = (
                Granularity(granularity) if granularity else self.range.suggested_granularity()
            )
        except ValueError:
            self.granularity = self.range.suggested_granularity()

        self.therapists = queries.active_therapists(db)
        self.locations = queries.available_locations(db)

    @property
    def weeks_in_range(self) -> Decimal:
        """Exact weeks, not rounded to whole ones.

        round(days / 7) made a therapist's below-threshold status change with the
        day of the week the report was opened: mid week, the current week counted
        as either a whole week or not at all. days / 7 as an exact Decimal gives
        the same answer on Tuesday as on Sunday.
        """
        return Decimal(self.range.days) / Decimal(7)

    def as_template_context(self) -> dict:
        return {
            # Deliberately not "range": that name shadows Jinja's range() builtin,
            # and a template loop then fails with "DateRange object is not callable".
            "date_range": self.range,
            "granularity": self.granularity,
            "granularities": list(Granularity),
            "presets": PICKER_PRESETS,
            "config": self.config,
            "coverage": self.coverage,
            "all_therapists": self.therapists,
            "all_locations": self.locations,
            "selected_therapists": list(self.filters.therapist_ids),
            "selected_locations": list(self.filters.locations),
            "query_string": self.query_string(),
        }

    def query_string(self, **overrides: object) -> str:
        from urllib.parse import urlencode

        params: list[tuple[str, str]] = [
            ("preset", str(overrides.get("preset", self.range.preset.value)))
        ]
        if self.range.preset.value == "custom" or "start" in overrides:
            params.append(("start", str(overrides.get("start", self.range.start))))
            params.append(("end", str(overrides.get("end", self.range.end))))
        params.append(("granularity", str(overrides.get("granularity", self.granularity.value))))
        for tid in self.filters.therapist_ids:
            params.append(("therapist", str(tid)))
        for loc in self.filters.locations:
            params.append(("location", loc))
        return urlencode(params)


def context_dependency(
    request: Request,
    db: DbSession,
    preset: str | None = None,
    start: str | None = None,
    end: str | None = None,
    granularity: str | None = None,
    therapist: Annotated[list[int] | None, Query()] = None,
    location: Annotated[list[str] | None, Query()] = None,
) -> ReportContext:
    return ReportContext(
        db,
        request,
        preset=preset,
        start=start,
        end=end,
        therapist_ids=tuple(therapist or ()),
        locations=tuple(location or ()),
        granularity=granularity,
    )


Ctx = Annotated[ReportContext, Depends(context_dependency)]


# ------------------------------------------------------------------------------ KPIs


def build_kpis(db: DbSession, ctx: ReportContext, current: queries.Totals) -> list[Kpi]:
    previous_range = ctx.range.previous()
    previous = queries.totals(
        db, ctx.filters.replaced(start=previous_range.start, end=previous_range.end)
    )

    trend = queries.by_period(
        db, ctx.filters, ctx.granularity, week_starts_monday=ctx.config.week_starts_monday
    )
    session_spark = [float(p.sessions) for p in trend]
    collected_spark = [float(p.collected) for p in trend]

    below = below_threshold_count(db, ctx)

    qs = ctx.query_string()

    return [
        Kpi(
            key="collected",
            question="What came in the door?",
            label="Revenue collected",
            value=current.collected,
            previous=previous.collected,
            kind="currency",
            sparkline=collected_spark,
            href=f"/reports/financial?{qs}",
        ),
        Kpi(
            key="sessions",
            question="How much clinical work did we do?",
            label="Sessions",
            value=current.sessions,
            previous=previous.sessions,
            kind="count",
            sparkline=session_spark,
            href=f"/reports/financial?{qs}",
            note="Cancellations are not counted as sessions.",
        ),
        Kpi(
            key="outstanding",
            question="What are we still owed?",
            label="Outstanding",
            value=current.outstanding,
            previous=previous.outstanding,
            kind="currency",
            lower_is_better=True,
            href=f"/reports/financial?{qs}",
            note=(
                f"Patient {_money(current.outstanding_patient)}, "
                f"insurance {_money(current.outstanding_insurance)}"
            ),
        ),
        Kpi(
            key="below_threshold",
            question="Who is short of their session threshold?",
            label="Therapists below threshold",
            value=below,
            kind="count",
            lower_is_better=True,
            href=f"/reports/financial?{qs}#utilization",
            note=(
                f"Against {ctx.config.benefits_session_threshold} sessions per week, "
                "salaried therapists only."
            ),
        ),
    ]


def _money(value: Decimal) -> str:
    return f"${value:,.0f}"


def below_threshold_count(db: DbSession, ctx: ReportContext) -> int:
    rows = queries.by_therapist(db, ctx.filters, weeks_in_range=ctx.weeks_in_range)
    return sum(
        1
        for r in rows
        if r.measured_against_threshold
        and ctx.config.utilization_status(r.sessions_per_week) == "below"
    )


# ---------------------------------------------------------------------------- routes


@router.get("", response_class=HTMLResponse)
async def overview(
    request: Request,
    db: DbSession,
    ctx: Ctx,
    auth: FinancialUser,
    weeks: str = Query(default=""),
) -> Response:
    """The overview leadership will judge the application by."""
    current = queries.totals(db, ctx.filters)
    trend = queries.by_period(
        db, ctx.filters, ctx.granularity, week_starts_monday=ctx.config.week_starts_monday
    )
    therapist_rows = queries.by_therapist(db, ctx.filters, weeks_in_range=ctx.weeks_in_range)

    # The weekly section is anchored to today, not to the report's date range: "the
    # last N weeks" should mean the same thing whatever period is picked above it.
    weekly = weekly_counts(
        db,
        ctx.filters,
        week_count=parse_week_count(weeks),
        timezone=ctx.config.timezone,
        week_starts_monday=ctx.config.week_starts_monday,
    )

    insight_report = build_insights(db, config=ctx.config, cpt_exclusions=ctx.config.cpt_exclusions)

    return render(
        request,
        "reports/overview.html",
        {
            "page_title": "Overview",
            "auth": auth,
            "active_page": "overview",
            "totals": current,
            "kpis": build_kpis(db, ctx, current),
            "trend": trend,
            "weekly": weekly,
            "week_choices": WEEK_WINDOW_CHOICES,
            "top_insights": insight_report.top,
            "therapist_rows": _ranked_for_board(therapist_rows, ctx.config),
            "can_see_utilization": auth.user.can_view(Module.THERAPIST_UTILIZATION)[0],
            **ctx.as_template_context(),
        },
    )


@router.get("/insights", response_class=HTMLResponse)
async def insights_page(request: Request, db: DbSession, ctx: Ctx, auth: FinancialUser) -> Response:
    """Plain language findings from the whole history. See app/reporting/insights.py."""
    report = build_insights(db, config=ctx.config, cpt_exclusions=ctx.config.cpt_exclusions)
    yoy_rows = year_over_year(db, config=ctx.config, cpt_exclusions=ctx.config.cpt_exclusions)
    return render(
        request,
        "reports/insights.html",
        {
            "page_title": "Insights",
            "auth": auth,
            "active_page": "insights",
            "report": report,
            "yoy_rows": yoy_rows,
            "yoy_has_any": any(r.has_comparison for r in yoy_rows),
            **ctx.as_template_context(),
        },
    )


def _ranked_for_board(
    rows: list[queries.TherapistRow], config: PracticeConfig
) -> list[tuple[queries.TherapistRow, str]]:
    """Attach a status to each therapist, worst first.

    Percentage based therapists carry no status at all rather than a passing one,
    because they have no threshold to meet and a green tick would imply they did.
    """
    order = {"below": 0, "watch": 1, "ok": 2, "": 3}
    decorated = [
        (
            row,
            config.utilization_status(row.sessions_per_week)
            if row.measured_against_threshold
            else "",
        )
        for row in rows
    ]
    decorated.sort(key=lambda pair: (order.get(pair[1], 9), -pair[0].sessions))
    return decorated


@router.get("/financial", response_class=HTMLResponse)
async def financial(request: Request, db: DbSession, ctx: Ctx, auth: FinancialUser) -> Response:
    current = queries.totals(db, ctx.filters)
    previous_range = ctx.range.previous()
    previous = queries.totals(
        db, ctx.filters.replaced(start=previous_range.start, end=previous_range.end)
    )

    return render(
        request,
        "reports/financial.html",
        {
            "page_title": "Financial",
            "auth": auth,
            "active_page": "financial",
            "totals": current,
            "previous_totals": previous,
            "trend": queries.by_period(
                db,
                ctx.filters,
                ctx.granularity,
                week_starts_monday=ctx.config.week_starts_monday,
            ),
            "therapist_rows": _ranked_for_board(
                queries.by_therapist(db, ctx.filters, weeks_in_range=ctx.weeks_in_range),
                ctx.config,
            ),
            "insurance_rows": queries.by_insurance(db, ctx.filters),
            "location_rows": queries.by_location(db, ctx.filters),
            "cpt_rows": queries.by_cpt(db, ctx.filters),
            "aging": queries.aging_by_insurance(db, today=today_in(ctx.config.timezone)),
            "aging_labels": queries.AGING_BUCKET_LABELS,
            "can_see_utilization": auth.user.can_view(Module.THERAPIST_UTILIZATION)[0],
            **ctx.as_template_context(),
        },
    )


@router.get("/financial/trend", response_class=HTMLResponse)
async def financial_trend_partial(
    request: Request, db: DbSession, ctx: Ctx, auth: FinancialUser
) -> Response:
    """htmx partial: the trend section only, for filter changes without a reload."""
    return render(
        request,
        "reports/_trend.html",
        {
            "auth": auth,
            "trend": queries.by_period(
                db,
                ctx.filters,
                ctx.granularity,
                week_starts_monday=ctx.config.week_starts_monday,
            ),
            **ctx.as_template_context(),
        },
    )


# ---------------------------------------------------------------------------- export

EXPORTS: dict[str, str] = {
    "trend": "Revenue and sessions by period",
    "therapists": "Sessions and revenue by therapist",
    "insurance": "Sessions and revenue by insurance",
    "location": "Sessions and revenue by location",
    "cpt": "Sessions and revenue by CPT",
    "aging": "Open balances by payer and age of session",
}


@router.get("/financial/export.csv")
async def export_financial(
    request: Request, db: DbSession, ctx: Ctx, auth: FinancialUser, table: str = "trend"
) -> Response:
    """CSV for any table on the financial page. Every export is audit logged."""
    if table not in EXPORTS:
        table = "trend"

    header, rows = _export_rows(db, ctx, table)

    audit.record(
        db,
        action=AuditAction.EXPORT,
        actor=auth.user,
        target_type="report",
        target_id=f"financial.{table}",
        request=request,
        detail={
            "table": table,
            "rows": len(rows),
            "start": ctx.range.start.isoformat(),
            "end": ctx.range.end.isoformat(),
            "granularity": ctx.granularity.value,
            "therapist_filter": list(ctx.filters.therapist_ids),
            "location_filter": list(ctx.filters.locations),
        },
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    # A provenance line, so a spreadsheet mailed to someone still says what it is and
    # what window it covers. Without it, an exported table is a set of numbers with no
    # context, which is how a quarter to date figure gets read as a full quarter.
    writer.writerow(
        [
            f"SRI Practice Dashboard, {EXPORTS[table]}",
            f"{ctx.range.start.isoformat()} to {ctx.range.end.isoformat()}",
            f"exported {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}",
        ]
    )
    writer.writerow([])
    writer.writerow(header)
    writer.writerows(rows)
    buffer.seek(0)

    filename = f"sri-{table}-{ctx.range.start}-to-{ctx.range.end}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _export_rows(
    db: DbSession, ctx: ReportContext, table: str
) -> tuple[list[str], list[Iterable[object]]]:
    if table == "therapists":
        rows = queries.by_therapist(db, ctx.filters, weeks_in_range=ctx.weeks_in_range)
        return (
            [
                "Therapist",
                "Employment type",
                "Sessions",
                "Sessions per week",
                "Collected",
                "Cancellations",
            ],
            [
                [
                    r.display_name,
                    r.employment_type.label,
                    r.sessions,
                    r.sessions_per_week,
                    r.collected,
                    r.cancellations,
                ]
                for r in rows
            ],
        )

    if table == "aging":
        rows, total_row = queries.aging_by_insurance(db, today=today_in(ctx.config.timezone))
        header = ["Payer", *queries.AGING_BUCKET_LABELS, "Total"]
        body: list[Iterable[object]] = [[r.label, *r.buckets, r.total] for r in rows]
        body.append([total_row.label, *total_row.buckets, total_row.total])
        return header, body

    if table in {"insurance", "location", "cpt"}:
        builder = {
            "insurance": queries.by_insurance,
            "location": queries.by_location,
            "cpt": queries.by_cpt,
        }[table]
        rows = builder(db, ctx.filters)
        return (
            [table.title(), "Sessions", "Collected", "Outstanding"],
            [[r.label, r.sessions, r.collected, r.outstanding] for r in rows],
        )

    trend = queries.by_period(
        db, ctx.filters, ctx.granularity, week_starts_monday=ctx.config.week_starts_monday
    )
    period_header = {
        Granularity.WEEK: "Week beginning",
        Granularity.MONTH: "Month",
        Granularity.QUARTER: "Quarter",
    }[ctx.granularity]
    return (
        [period_header, "Sessions", "Billed", "Collected", "Outstanding"],
        [[p.start.isoformat(), p.sessions, p.billed, p.collected, p.outstanding] for p in trend],
    )


def _iso(value: date | None) -> str:
    return value.isoformat() if value else ""
