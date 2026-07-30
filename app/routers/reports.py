"""The Reports area: overview dashboard, financial module, and CSV export.

Access is gated per module by the same dependency the rest of the app uses, so a
viewer without the financial grant gets a 403 from the route, not a hidden nav link.

Every query these routes run is aggregate or therapist grain. Nothing here can select
a patient name or code (see app/reporting/queries.py).

The handlers are plain `def`, not `async def`, throughout this application. They all
talk to the database through a synchronous driver, and an `async def` handler doing
blocking work runs ON the event loop: one slow report, or one nine thousand row import,
stalled every other request in the process, health checks included. A synchronous
handler is dispatched to a threadpool instead, which is what the blocking work wants.
Only handlers that genuinely await something, reading an uploaded body or a form, stay
async.
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
from app.reporting.compare import last_year, pct_change, year_over_year
from app.reporting.insights import build_insights
from app.reporting.metrics import Kpi
from app.reporting.periods import (
    PICKER_PRESETS,
    DateRange,
    Granularity,
    resolve_range,
    today_in,
)
from app.reporting.provenance import EXPLAINABLE, TITLES, build_derivation
from app.reporting.weekly import WEEK_WINDOW_CHOICES, parse_week_count, weekly_counts
from app.security import audit
from app.security.deps import AuthContext, DbSession, require_module
from app.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports", tags=["reports"])

FinancialUser = Annotated[AuthContext, Depends(require_module(Module.FINANCIAL))]
PatientFlowUser = Annotated[AuthContext, Depends(require_module(Module.PATIENT_FUNNEL))]

# The practice has about forty clinicians and a handful of locations, so this is far
# above any real selection and far below a query that hurts.
MAX_FILTER_VALUES = 200


def _capped(values: tuple) -> tuple:
    """The first MAX_FILTER_VALUES filter values, in the order they arrived."""
    return tuple(values[:MAX_FILTER_VALUES])


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

        self.therapists = queries.active_therapists(db)
        self.locations = queries.available_locations(db)

        # Filter values arrive on the query string, which anybody can hand edit, and
        # they went into every query on the page unbounded. Nothing can be injected,
        # because they are bound parameters, but a URL carrying a thousand of them built
        # a thousand element IN clause on each of the dozen queries a page runs.
        #
        # Deliberately NOT filtered to values that exist. A filter naming a therapist or
        # a location the record does not have narrows to nothing, and the empty state
        # says so, which is the truth. Dropping the unknown value instead would show the
        # whole practice under a filter bar claiming to show one person.
        self.filters = queries.Filters(
            start=self.range.start,
            end=self.range.end,
            cpt_exclusions=self.config.cpt_exclusions,
            therapist_ids=_capped(therapist_ids),
            locations=_capped(locations),
        )

        try:
            self.granularity = (
                Granularity(granularity) if granularity else self.range.suggested_granularity()
            )
        except ValueError:
            self.granularity = self.range.suggested_granularity()

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

_NO_COMPARISON = (
    "No comparison: the record holds no imported rows for the preceding period of the "
    "same length, so there is nothing to compare against rather than a period of zero."
)


def build_kpis(db: DbSession, ctx: ReportContext, current: queries.Totals) -> list[Kpi]:
    previous_range = ctx.range.previous()
    previous = queries.totals(
        db, ctx.filters.replaced(start=previous_range.start, end=previous_range.end)
    )

    # A comparison window holding no imported rows at all is not a period in which the
    # practice collected nothing. It is a period the record does not cover, which is
    # the normal state of the first quarter imported and of any range that reaches back
    # before the earliest source. Treated as a real zero, every card read as growth
    # from nothing: the first month showed the whole month's revenue as an increase.
    # None means no comparison, and the cards then show the figure without a delta.
    comparable = previous if previous.visits > 0 else None

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
            previous=comparable.collected if comparable else None,
            no_comparison_reason=_NO_COMPARISON if comparable is None else None,
            kind="currency",
            sparkline=collected_spark,
            href=f"/reports/financial?{qs}",
        ),
        Kpi(
            key="sessions",
            question="How much clinical work did we do?",
            label="Sessions",
            value=current.sessions,
            previous=comparable.sessions if comparable else None,
            no_comparison_reason=_NO_COMPARISON if comparable is None else None,
            kind="count",
            sparkline=session_spark,
            href=f"/reports/financial?{qs}",
            note=(
                f"Therapy {current.therapy_sessions:,}, psychiatry "
                f"{current.psychiatry_sessions:,}. Cancellations are not counted."
            ),
        ),
        Kpi(
            key="outstanding",
            question="What are we still owed?",
            label="Outstanding",
            value=current.outstanding,
            previous=comparable.outstanding if comparable else None,
            no_comparison_reason=_NO_COMPARISON if comparable is None else None,
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
                "Each measured against their own expectation: a personal override, "
                "or their employment type's default."
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
        if ctx.config.status_for(r.employment_type, r.weekly_expected_sessions, r.sessions_per_week)
        == "below"
    )


# ---------------------------------------------------------------------------- routes


@router.get("", response_class=HTMLResponse)
def overview(
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

    # Staleness is an admin's problem to fix, so only admins get nagged. Only live
    # sheet sources count: an upload source has nothing to go stale against.
    stale_sync_days = None
    if auth.role.is_admin:
        from sqlalchemy import func, select

        from app.models.data_source import DataSource, SourceProvider
        from app.models.types import utcnow

        newest = db.execute(
            select(func.max(DataSource.last_synced_at)).where(
                DataSource.active.is_(True),
                DataSource.provider == SourceProvider.GOOGLE_SHEETS,
            )
        ).scalar()
        if newest is not None:
            stale_sync_days = (utcnow() - newest).days

    return render(
        request,
        "reports/overview.html",
        {
            "page_title": "Overview",
            "auth": auth,
            "active_page": "overview",
            "totals": current,
            "kpis": build_kpis(db, ctx, current),
            "explainable": EXPLAINABLE,
            "trend": trend,
            "weekly": weekly,
            "week_choices": WEEK_WINDOW_CHOICES,
            "top_insights": insight_report.top,
            "stale_sync_days": stale_sync_days,
            "auto_sync_days": ctx.config.auto_sync_days,
            "therapist_rows": _ranked_for_board(therapist_rows, ctx.config),
            "can_see_utilization": auth.user.can_view(Module.THERAPIST_UTILIZATION)[0],
            **ctx.as_template_context(),
        },
    )


@router.get("/month", response_class=HTMLResponse)
def month_review(
    request: Request,
    db: DbSession,
    ctx: Ctx,
    auth: FinancialUser,
    month: str = Query(default=""),
) -> Response:
    """One printable page: a month against the month before it and the same month
    last year. Defaults to the last completed month, because a month in progress
    compared against full months reads as a collapse that is only the calendar."""
    from datetime import timedelta

    from app.reporting.periods import month_start

    today = today_in(ctx.config.timezone)
    this_month = month_start(today)
    default_month = month_start(this_month - timedelta(days=1))

    try:
        chosen = month_start(date.fromisoformat(f"{month}-01")) if month else default_month
    except ValueError:
        chosen = default_month
    if chosen > this_month:
        chosen = default_month

    in_progress = chosen == this_month
    month_end = (
        today if in_progress else month_start(chosen + timedelta(days=32)) - timedelta(days=1)
    )

    def month_totals(start: date, end: date) -> queries.Totals:
        return queries.totals(
            db, queries.Filters(start=start, end=end, cpt_exclusions=ctx.config.cpt_exclusions)
        )

    current = month_totals(chosen, month_end)

    prev_start = month_start(chosen - timedelta(days=1))
    prev_end = chosen - timedelta(days=1)
    previous = month_totals(prev_start, prev_end)

    ly_start, ly_end = last_year(chosen), last_year(month_end)
    same_month_ly = month_totals(ly_start, ly_end)

    weeks = queries.by_period(
        db,
        queries.Filters(start=chosen, end=month_end, cpt_exclusions=ctx.config.cpt_exclusions),
        Granularity.WEEK,
        week_starts_monday=ctx.config.week_starts_monday,
    )

    payers = queries.by_insurance(
        db,
        queries.Filters(start=chosen, end=month_end, cpt_exclusions=ctx.config.cpt_exclusions),
        limit=5,
        groups=ctx.config.insurance_groups,
    )

    def deltas(totals: queries.Totals) -> dict:
        if totals.visits == 0:
            return {}
        return {
            "sessions": pct_change(current.sessions, totals.sessions),
            "collected": pct_change(current.collected, totals.collected),
        }

    return render(
        request,
        "reports/month.html",
        {
            "page_title": chosen.strftime("%B %Y"),
            "auth": auth,
            "active_page": "overview",
            "month": chosen,
            "month_end": month_end,
            "in_progress": in_progress,
            "totals": current,
            "previous": previous,
            "previous_label": prev_start.strftime("%b %Y"),
            "vs_previous": deltas(previous),
            "same_month_ly": same_month_ly,
            "ly_label": ly_start.strftime("%b %Y"),
            "vs_ly": deltas(same_month_ly),
            "weeks": weeks,
            "payers": payers,
            "prev_month_param": prev_start.strftime("%Y-%m"),
            "next_month_param": (
                month_start(chosen + timedelta(days=32)).strftime("%Y-%m")
                if chosen < this_month
                else None
            ),
            **ctx.as_template_context(),
        },
    )


@router.get("/explain/{key}", response_class=HTMLResponse)
def explain(request: Request, db: DbSession, ctx: Ctx, auth: FinancialUser, key: str) -> Response:
    """How one figure was calculated, for the caller's own filters.

    The same filters the reader had on the page they came from, so the arithmetic
    shown produced the exact number they clicked. See app/reporting/provenance.py
    for why the sentence and the sum cannot drift apart.
    """
    derivation = build_derivation(
        db,
        key,
        filters=ctx.filters,
        config=ctx.config,
        granularity=ctx.granularity,
        may_see_therapists=auth.user.can_view(Module.THERAPIST_UTILIZATION)[0],
    )
    if derivation is None:
        return render(
            request,
            "errors/not_found.html",
            {"page_title": "Not found", "auth": auth},
            status_code=404,
        )

    return render(
        request,
        "reports/explain.html",
        {
            "page_title": f"How {derivation.title} is calculated",
            "auth": auth,
            "derivation": derivation,
            "explainable": EXPLAINABLE,
            "titles": TITLES,
            **ctx.as_template_context(),
        },
    )


@router.get("/patient-flow", response_class=HTMLResponse)
def patient_flow(request: Request, db: DbSession, ctx: Ctx, auth: PatientFlowUser) -> Response:
    """Aggregate patient flow. Counts only: no patient is ever named here.

    Gated on the patient_funnel module grant, so access is a deliberate decision
    per user even though the page shows no identity.
    """
    from app.reporting import patients

    series = patients.flow_series(
        db, ctx.filters, ctx.granularity, week_starts_monday=ctx.config.week_starts_monday
    )
    flow_summary = patients.summary(db, ctx.filters, today=today_in(ctx.config.timezone))

    return render(
        request,
        "reports/patient_flow.html",
        {
            "page_title": "Patient flow",
            "auth": auth,
            "active_page": "patient_flow",
            "series": series,
            "summary": flow_summary,
            "lapse_days": patients.LAPSE_DAYS,
            **ctx.as_template_context(),
        },
    )


@router.get("/insights", response_class=HTMLResponse)
def insights_page(request: Request, db: DbSession, ctx: Ctx, auth: FinancialUser) -> Response:
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

    Each person is graded against their own expectation: a personal override if
    one is set, otherwise their employment type's default. The unmeasured carry
    no status at all rather than a passing one, because they have no threshold to
    meet and a green tick would imply they did.
    """
    order = {"below": 0, "over": 1, "watch": 2, "ok": 3, "": 4}
    decorated = [
        (
            row,
            config.status_for(
                row.employment_type, row.weekly_expected_sessions, row.sessions_per_week
            ),
        )
        for row in rows
    ]
    decorated.sort(key=lambda pair: (order.get(pair[1], 9), -pair[0].sessions))
    return decorated


@router.get("/financial", response_class=HTMLResponse)
def financial(request: Request, db: DbSession, ctx: Ctx, auth: FinancialUser) -> Response:
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
            "explainable": EXPLAINABLE,
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
            "insurance_rows": queries.by_insurance(
                db, ctx.filters, groups=ctx.config.insurance_groups
            ),
            "location_rows": queries.by_location(db, ctx.filters),
            "cpt_rows": queries.by_cpt(db, ctx.filters),
            "aging": queries.aging_by_insurance(
                db,
                today=today_in(ctx.config.timezone),
                groups=ctx.config.insurance_groups,
                filters=ctx.filters,
            ),
            "aging_labels": queries.AGING_BUCKET_LABELS,
            "can_see_utilization": auth.user.can_view(Module.THERAPIST_UTILIZATION)[0],
            **ctx.as_template_context(),
        },
    )


@router.get("/financial/trend", response_class=HTMLResponse)
def financial_trend_partial(
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
def export_financial(
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
                "Discipline",
                "Employment type",
                "Expected per week",
                "Sessions",
                "Sessions per week",
                "Cancellation rate",
                "Collected",
                "Cancellations",
            ],
            [
                [
                    r.display_name,
                    r.discipline.label,
                    r.employment_type.label,
                    ctx.config.expectation_for(r.employment_type, r.weekly_expected_sessions).label,
                    r.sessions,
                    r.sessions_per_week,
                    f"{r.cancellation_rate}%" if r.cancellation_rate is not None else "",
                    r.collected,
                    r.cancellations,
                ]
                for r in rows
            ],
        )

    if table == "aging":
        # Same arguments as the page renders with. An export that folds payers
        # differently from the table it came from is two answers to one question.
        rows, total_row = queries.aging_by_insurance(
            db,
            today=today_in(ctx.config.timezone),
            groups=ctx.config.insurance_groups,
            filters=ctx.filters,
        )
        header = ["Payer", *queries.AGING_BUCKET_LABELS, "Total"]
        body: list[Iterable[object]] = [[r.label, *r.buckets, r.total] for r in rows]
        body.append([total_row.label, *total_row.buckets, total_row.total])
        return header, body

    if table in {"insurance", "location", "cpt"}:
        if table == "insurance":
            rows = queries.by_insurance(db, ctx.filters, groups=ctx.config.insurance_groups)
        else:
            builder = {"location": queries.by_location, "cpt": queries.by_cpt}[table]
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
