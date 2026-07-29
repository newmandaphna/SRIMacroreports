"""Plain language findings computed from the practice's own history.

Nothing here is a model or a black box. Every insight is a deterministic statistic
with its inputs stated in the sentence, so a reader can check the arithmetic against
the tables on the other report pages. Each insight also gates on a minimum amount of
history and abstains below it, which is what makes the system visibly smarter as
quarters and historical uploads accumulate: more history, more findings, tighter
comparisons.

Everything is aggregate or therapist grain. Nothing here selects a patient column
(the queries module enforces that), and therapist names are not PHI (SECURITY.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config_store import PracticeConfig
from app.reporting import queries
from app.reporting.periods import Granularity, today_in, week_start

ZERO = Decimal(0)

# Below this many completed weeks of history, trend insights abstain: a trend over
# three weeks is noise wearing a suit.
MIN_TREND_WEEKS = 8

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass
class Insight:
    """One finding. tone is one of good, watch, bad, info."""

    key: str
    tone: str
    headline: str
    detail: str
    spark: list[float] = field(default_factory=list)

    @property
    def pill_class(self) -> str:
        return {
            "good": "pill--ok",
            "watch": "pill--watch",
            "bad": "pill--below",
            "info": "pill--neutral",
        }.get(self.tone, "pill--neutral")


@dataclass
class InsightReport:
    insights: list[Insight]
    history_weeks: int
    window_start: date
    window_end: date

    @property
    def top(self) -> list[Insight]:
        """The three most actionable findings, for the overview card."""
        order = {"bad": 0, "watch": 1, "good": 2, "info": 3}
        return sorted(self.insights, key=lambda i: order.get(i.tone, 9))[:3]


def _pct_change(current: Decimal, previous: Decimal) -> Decimal | None:
    if previous <= ZERO:
        return None
    return ((current - previous) / previous * 100).quantize(Decimal("0.1"))


def _avg(values: list) -> Decimal:
    if not values:
        return ZERO
    return (sum((Decimal(str(v)) for v in values), ZERO) / len(values)).quantize(Decimal("0.1"))


def build_insights(
    db: Session,
    *,
    config: PracticeConfig,
    cpt_exclusions: tuple[str, ...],
) -> InsightReport:
    """Everything is computed over completed calendar weeks, anchored to today.

    The current, half finished week is never part of any comparison: a Tuesday's
    worth of sessions would read as a collapse in every trend it touched.
    """
    today = today_in(config.timezone)
    wsm = config.week_starts_monday
    this_week = week_start(today, wsm)
    last_completed_end = this_week - timedelta(days=1)

    coverage = queries.coverage(db)
    insights: list[Insight] = []

    if not coverage.has_data or coverage.min_date is None:
        return InsightReport(
            insights=[
                Insight(
                    key="no_data",
                    tone="info",
                    headline="No history yet.",
                    detail=(
                        "Sync a quarter or upload a historical file and this page starts "
                        "finding patterns: momentum, drift, concentration, and gaps."
                    ),
                )
            ],
            history_weeks=0,
            window_start=today,
            window_end=today,
        )

    first_week = week_start(coverage.min_date, wsm)
    history_weeks = max(0, (last_completed_end - first_week).days // 7)

    def window(weeks: int, *, offset_weeks: int = 0) -> queries.Filters:
        end = last_completed_end - timedelta(weeks=offset_weeks)
        start = week_start(end, wsm) - timedelta(weeks=weeks - 1)
        return queries.Filters(start=start, end=end, cpt_exclusions=cpt_exclusions)

    # ---------------------------------------------------------------- history status
    if history_weeks < MIN_TREND_WEEKS:
        insights.append(
            Insight(
                key="thin_history",
                tone="info",
                headline=f"Only {history_weeks} completed weeks of history so far.",
                detail=(
                    "Trend findings need at least "
                    f"{MIN_TREND_WEEKS}. Every synced quarter and every historical upload "
                    "extends the record and unlocks more of this page."
                ),
            )
        )

    # ------------------------------------------------------------------- momentum
    trend_weeks = min(12, history_weeks)
    weekly_points = (
        queries.by_period(db, window(trend_weeks), Granularity.WEEK, week_starts_monday=wsm)
        if history_weeks >= MIN_TREND_WEEKS
        else []
    )
    if weekly_points:
        sessions_series = [p.sessions for p in weekly_points]
        collected_series = [float(p.collected) for p in weekly_points]
        half = len(weekly_points) // 2
        for key, series, label, fmt in (
            ("sessions_momentum", sessions_series, "Sessions", "{:.1f} a week"),
            ("revenue_momentum", collected_series, "Collected revenue", "${:,.0f} a week"),
        ):
            recent = _avg(series[half:])
            earlier = _avg(series[:half])
            change = _pct_change(recent, earlier)
            if change is None:
                continue
            if change >= 5:
                tone, verb = "good", "rising"
            elif change <= -5:
                tone, verb = "bad", "falling"
            else:
                tone, verb = "info", "steady"
            insights.append(
                Insight(
                    key=key,
                    tone=tone,
                    headline=f"{label} {verb}: {change:+}% against the prior stretch.",
                    detail=(
                        f"Averaging {fmt.format(recent)} over the last {len(series) - half} "
                        f"completed weeks, against {fmt.format(earlier)} across the "
                        f"{half} before them."
                    ),
                    spark=[float(v) for v in series],
                )
            )

    # ------------------------------------------------------------ sudden drops
    # A drop of 20+ sessions week over week gets investigated anyway; save the
    # investigator the first hour by naming who accounts for it. A zero week from
    # someone who was busy the week before usually means leave, not decline.
    if len(weekly_points) >= 2:
        last_week, prior_week = weekly_points[-1], weekly_points[-2]
        drop = prior_week.sessions - last_week.sessions
        if drop >= 20:
            last_rows = {
                r.therapist_id: r
                for r in queries.by_therapist(
                    db,
                    queries.Filters(
                        start=last_week.start,
                        end=last_week.start + timedelta(days=6),
                        cpt_exclusions=cpt_exclusions,
                    ),
                )
            }
            prior_rows = queries.by_therapist(
                db,
                queries.Filters(
                    start=prior_week.start,
                    end=prior_week.start + timedelta(days=6),
                    cpt_exclusions=cpt_exclusions,
                ),
            )
            contributors = []
            for before in prior_rows:
                after = last_rows.get(before.therapist_id)
                after_n = after.sessions if after else 0
                fell = before.sessions - after_n
                if fell >= 5:
                    note = " (zero sessions, likely out)" if after_n == 0 else ""
                    line = f"{before.display_name} {before.sessions} to {after_n}{note}"
                    contributors.append((fell, line))
            contributors.sort(reverse=True)
            named = "; ".join(text for _, text in contributors[:3])
            explained = sum(n for n, _ in contributors[:3])
            insights.append(
                Insight(
                    key="sessions_drop",
                    tone="watch",
                    headline=(
                        f"Sessions fell by {drop} last week "
                        f"({prior_week.sessions} to {last_week.sessions})."
                    ),
                    detail=(
                        f"Largest contributors: {named}. These account for {explained} "
                        "of the drop. A zero week next to a busy one is usually leave "
                        "or vacation, not decline."
                        if contributors
                        else "No single therapist accounts for it: the drop is spread "
                        "thinly across the roster, which usually means a holiday week "
                        "or a data gap."
                    ),
                )
            )

    # ------------------------------------------------- collection and cancellations
    if history_weeks >= MIN_TREND_WEEKS:
        cmp_weeks = min(12, history_weeks // 2)
        current_totals = queries.totals(db, window(cmp_weeks))
        previous_totals = queries.totals(db, window(cmp_weeks, offset_weeks=cmp_weeks))

        # Collection rate is judged on windows that end five weeks back: young
        # claims have not had time to pay, and judging them as uncollected reads
        # as a slump that is only the calendar.
        MATURITY_WEEKS = 5
        mature_current = queries.totals(db, window(cmp_weeks, offset_weeks=MATURITY_WEEKS))
        mature_previous = queries.totals(
            db, window(cmp_weeks, offset_weeks=MATURITY_WEEKS + cmp_weeks)
        )

        cur_rate = mature_current.collection_rate
        prev_rate = mature_previous.collection_rate
        if cur_rate is not None and prev_rate is not None:
            delta = cur_rate - prev_rate
            if delta <= -3:
                tone = "bad"
            elif delta >= 3:
                tone = "good"
            else:
                tone = "info"
            insights.append(
                Insight(
                    key="collection_drift",
                    tone=tone,
                    headline=(
                        f"Collection rate {cur_rate}% over {cmp_weeks} weeks ending "
                        f"{MATURITY_WEEKS} weeks ago ({delta:+}pts vs the {cmp_weeks} before)."
                    ),
                    detail=(
                        f"${mature_current.collected:,.0f} collected of "
                        f"${mature_current.billed:,.0f} billed. The newest "
                        f"{MATURITY_WEEKS} weeks are deliberately left out: young claims "
                        "have not had time to pay, and judging them would read as a slump "
                        "that is only the calendar. A real fall this size usually means a "
                        "payer slowed down or a billing step is being missed."
                    ),
                )
            )

        cur_cancel = current_totals.cancellation_rate
        prev_cancel = previous_totals.cancellation_rate
        if cur_cancel is not None and prev_cancel is not None:
            delta = cur_cancel - prev_cancel
            if delta >= 2:
                tone = "watch"
            elif delta <= -2:
                tone = "good"
            else:
                tone = "info"
            insights.append(
                Insight(
                    key="cancellation_drift",
                    tone=tone,
                    headline=(
                        f"Cancellation rate {cur_cancel}% "
                        f"({delta:+}pts vs the prior {cmp_weeks} weeks)."
                    ),
                    detail=(
                        f"{current_totals.cancellations} cancellations, "
                        f"{current_totals.cancellations_with_fee} with a fee charged. "
                        "Recording practice varies between therapists, so treat the "
                        "direction as real and the level as approximate."
                    ),
                )
            )

        cur_rps = current_totals.revenue_per_session
        prev_rps = previous_totals.revenue_per_session
        if cur_rps is not None and prev_rps is not None:
            change = _pct_change(cur_rps, prev_rps)
            if change is not None and abs(change) >= 5:
                insights.append(
                    Insight(
                        key="revenue_per_session",
                        tone="good" if change > 0 else "watch",
                        headline=f"Revenue per session ${cur_rps} ({change:+}%).",
                        detail=(
                            "A shift this size usually reflects payer mix or session type "
                            "mix, both visible on the Financial page breakdowns."
                        ),
                    )
                )

    # ------------------------------------------------------------- payer concentration
    if history_weeks >= MIN_TREND_WEEKS:
        conc_window = window(min(26, history_weeks))
        payers = queries.by_insurance(db, conc_window, limit=50)
        total_collected = sum((p.collected for p in payers), ZERO)
        if payers and total_collected > ZERO:
            top = payers[0]
            share = (top.collected / total_collected * 100).quantize(Decimal("1"))
            if share >= 40:
                insights.append(
                    Insight(
                        key="payer_concentration",
                        tone="watch",
                        headline=f"{top.label} is {share}% of collected revenue.",
                        detail=(
                            "Concentration this high means one payer's rate change or "
                            "processing slowdown moves the whole practice. Worth knowing, "
                            "not necessarily worth changing."
                        ),
                    )
                )

    # ------------------------------------------------------------------ busiest days
    if history_weeks >= MIN_TREND_WEEKS:
        weekday_sessions = queries.by_weekday(db, window(min(12, history_weeks)))
        total = sum(weekday_sessions)
        if total >= 50:
            busiest = max(range(7), key=lambda i: weekday_sessions[i])
            quietest_open = min(
                (i for i in range(7) if weekday_sessions[i] > 0),
                key=lambda i: weekday_sessions[i],
                default=busiest,
            )
            share = weekday_sessions[busiest] * 100 // total
            insights.append(
                Insight(
                    key="busiest_day",
                    tone="info",
                    headline=(
                        f"{WEEKDAY_NAMES[busiest]} is the busiest day, {share}% of all sessions."
                    ),
                    detail=(
                        f"Quietest working day: {WEEKDAY_NAMES[quietest_open]} with "
                        f"{weekday_sessions[quietest_open]} sessions over the same weeks. "
                        "Useful when placing new intakes or planning coverage."
                    ),
                    spark=[float(v) for v in weekday_sessions],
                )
            )

    # -------------------------------------------------------------- therapist movers
    if history_weeks >= MIN_TREND_WEEKS:
        recent_rows = {
            r.therapist_id: r
            for r in queries.by_therapist(db, window(4), weeks_in_range=4)
            if r.sessions > 0
        }
        prior_rows = {
            r.therapist_id: r
            for r in queries.by_therapist(db, window(4, offset_weeks=4), weeks_in_range=4)
        }
        movers: list[tuple[Decimal, str, int, int]] = []
        for tid, row in recent_rows.items():
            before = prior_rows.get(tid)
            if before is None or (row.sessions + before.sessions) < 12:
                continue  # too small to call a move rather than noise
            change = _pct_change(Decimal(row.sessions), Decimal(before.sessions))
            if change is not None:
                movers.append((change, row.display_name, row.sessions, before.sessions))
        movers.sort(reverse=True)
        if movers and movers[0][0] >= 25:
            change, name, now_n, before_n = movers[0]
            insights.append(
                Insight(
                    key="mover_up",
                    tone="good",
                    headline=f"{name} is up {change:+}% in sessions.",
                    detail=(
                        f"{now_n} sessions in the last 4 completed weeks against "
                        f"{before_n} in the 4 before."
                    ),
                )
            )
        if movers and movers[-1][0] <= -25:
            change, name, now_n, before_n = movers[-1]
            insights.append(
                Insight(
                    key="mover_down",
                    tone="watch",
                    headline=f"{name} is down {change:+}% in sessions.",
                    detail=(
                        f"{now_n} sessions in the last 4 completed weeks against "
                        f"{before_n} in the 4 before. Context belongs on the utilization "
                        "board, not in this number: leave, referral flow, and schedule "
                        "changes all look like this."
                    ),
                )
            )

    # ---------------------------------------------------------------- looking ahead
    # A projection is only honest once a full year of seasonal shape exists, so it
    # unlocks at 52 completed weeks and says so before then. Method: what the same
    # next four weeks did last year, scaled by how this year has been running
    # against last year recently. Arithmetic, stated, checkable.
    if history_weeks >= 52:
        from app.reporting.compare import last_year, pct_change

        next_start = last_completed_end + timedelta(days=1)
        next_end = next_start + timedelta(days=27)
        ahead_ly = queries.totals(
            db,
            queries.Filters(
                start=last_year(next_start),
                end=last_year(next_end),
                cpt_exclusions=cpt_exclusions,
            ),
        )
        recent = queries.totals(db, window(12))
        recent_window = window(12)
        recent_ly = queries.totals(
            db,
            queries.Filters(
                start=last_year(recent_window.start),
                end=last_year(recent_window.end),
                cpt_exclusions=cpt_exclusions,
            ),
        )
        if ahead_ly.sessions > 0 and recent_ly.sessions > 0:
            ratio = Decimal(recent.sessions) / Decimal(recent_ly.sessions)
            projected = int((Decimal(ahead_ly.sessions) * ratio).quantize(Decimal("1")))
            trend_pct = pct_change(Decimal(recent.sessions), Decimal(recent_ly.sessions))
            insights.append(
                Insight(
                    key="projection",
                    tone="info",
                    headline=(
                        f"Looking ahead: roughly {projected} sessions over the next 4 weeks."
                    ),
                    detail=(
                        f"The same four weeks last year held {ahead_ly.sessions} sessions, "
                        f"scaled by this year running {trend_pct:+}% against last year over "
                        "the last 12 weeks. A rough bearing, not a promise: one holiday or "
                        "one hire moves it."
                    ),
                )
            )
    elif history_weeks >= MIN_TREND_WEEKS:
        insights.append(
            Insight(
                key="projection_locked",
                tone="info",
                headline="Projections unlock at a full year of history.",
                detail=(
                    f"{history_weeks} completed weeks so far. A forecast needs the same "
                    "season last year to compare against; uploading last year's quarters "
                    "on the Data sources page unlocks it immediately."
                ),
            )
        )

    # ------------------------------------------------------------------ silent weeks
    all_weeks = queries.by_period(
        db,
        queries.Filters(start=first_week, end=last_completed_end, cpt_exclusions=cpt_exclusions),
        Granularity.WEEK,
        week_starts_monday=wsm,
    )
    if len(all_weeks) >= MIN_TREND_WEEKS:
        busy_weeks = [p.sessions for p in all_weeks if p.sessions > 0]
        typical = _avg(busy_weeks) if busy_weeks else ZERO
        silent = [p for p in all_weeks if p.sessions == 0]
        if silent and typical >= 10:
            latest = silent[-1].start
            insights.append(
                Insight(
                    key="silent_weeks",
                    tone="watch",
                    headline=(
                        f"{len(silent)} week{'s' if len(silent) != 1 else ''} in the record "
                        "hold zero sessions."
                    ),
                    detail=(
                        f"Most recent: week of {latest.strftime('%-d %b %Y')}. In a practice "
                        f"averaging {typical} sessions a week, a silent week usually means "
                        "a quarter was never imported, not a closed office. The historical "
                        "upload on the Data sources page can fill it."
                    ),
                )
            )

    # ------------------------------------------------------------- open rejections
    from app.models.data_source import ImportError as ImportErrorRow

    open_rejections = db.execute(
        select(func.count(ImportErrorRow.id)).where(ImportErrorRow.resolved_at.is_(None))
    ).scalar_one()
    if open_rejections:
        insights.append(
            Insight(
                key="open_rejections",
                tone="watch",
                headline=f"{open_rejections:,} rejected rows have not been reviewed.",
                detail=(
                    "Every rejected row is a session missing from every number on this "
                    "page. Reviewing them is how the totals become trustworthy."
                ),
            )
        )

    # ------------------------------------------------------------- offline backups
    # Replit's point-in-time recovery covers a rolling window only. Once real data
    # exists, an offline copy that is older than a month (or has never been taken)
    # is a quiet risk worth a loud sentence.
    from app.models.audit import AuditLog
    from app.models.enums import AuditAction, AuditResult
    from app.models.types import utcnow

    last_backup = db.execute(
        select(func.max(AuditLog.occurred_at)).where(
            AuditLog.action == AuditAction.EXPORT,
            AuditLog.target_type == "database",
            AuditLog.result == AuditResult.SUCCESS,
        )
    ).scalar()
    backup_age_days = None if last_backup is None else (utcnow() - last_backup).days
    if backup_age_days is None or backup_age_days > 30:
        insights.append(
            Insight(
                key="backup_overdue",
                tone="watch",
                headline=(
                    "No offline backup has ever been taken."
                    if backup_age_days is None
                    else f"The last offline backup is {backup_age_days} days old."
                ),
                detail=(
                    "The hosted database keeps a short rolling recovery window and "
                    "nothing else, and it is the only place the full history exists. "
                    "The Download backup button on the Settings page produces a copy "
                    "to keep somewhere the hosting is not."
                ),
            )
        )

    if len(insights) <= 1 and history_weeks >= MIN_TREND_WEEKS:
        insights.append(
            Insight(
                key="all_quiet",
                tone="good",
                headline="Nothing unusual in the record.",
                detail=(
                    "Trends are steady, no data gaps, no unreviewed rejections. "
                    "This page will speak up when something moves."
                ),
            )
        )

    return InsightReport(
        insights=insights,
        history_weeks=history_weeks,
        window_start=first_week,
        window_end=last_completed_end,
    )
