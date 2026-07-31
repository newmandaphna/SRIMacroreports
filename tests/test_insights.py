"""The insights engine: deterministic findings from the practice's own history.

All test data is synthetic (Patient A0, Patient A1, ...), per the repository rule.
Dates are computed relative to today the same way the engine computes them, so the
tests hold on any day of the week.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app import config_store
from app.models.data_source import DataSource, SourceProvider
from app.models.enums import Module, Role
from app.models.therapist import EmploymentType, Therapist
from app.models.visit import Visit
from app.reporting.insights import build_insights
from app.reporting.periods import today_in, week_start
from tests.conftest import make_user, sign_in


def completed_week_start(n: int, timezone: str = "America/New_York"):
    """The Monday of the nth most recent COMPLETED week (n=1 is last week)."""
    this_week = week_start(today_in(timezone))
    return this_week - timedelta(weeks=n)


@pytest.fixture
def practice(client):
    """A source and one salaried therapist; visits are added per test."""
    with client.app.state.db.session() as db:
        source = DataSource(
            label="H", provider=SourceProvider.DEMO, tab_name="t", column_mapping={}
        )
        therapist = Therapist(
            display_name="Alpha", employment_type=EmploymentType.SALARIED_BENEFITS
        )
        db.add_all([source, therapist])
        db.flush()
        return {"source": source.id, "therapist": therapist.id}


def seed_weeks(client, ids, sessions_per_week: dict[int, int], therapist_id=None):
    """Add N sessions in the nth most recent completed week, spread over weekdays."""
    with client.app.state.db.session() as db:
        for weeks_ago, count in sessions_per_week.items():
            monday = completed_week_start(weeks_ago)
            for i in range(count):
                day = monday + timedelta(days=i % 5)
                db.add(
                    Visit(
                        source_id=ids["source"],
                        therapist_id=therapist_id or ids["therapist"],
                        patient_name=f"Patient A{weeks_ago}x{i}",
                        patient_name_normalized=f"PATIENT A{weeks_ago}X{i}",
                        dos=day,
                        cpt="90837",
                        cpt_base="90837",
                        total_paid=Decimal("150.00"),
                        total_due=Decimal("150.00"),
                        total_balance=Decimal("0.00"),
                    )
                )


def insights_for(client):
    with client.app.state.db.session() as db:
        config = config_store.load(db, client.app.state.settings)
        return build_insights(db, config=config, cpt_exclusions=config.cpt_exclusions)


def keys(report) -> set[str]:
    return {i.key for i in report.insights}


def by_key(report, key: str):
    return next(i for i in report.insights if i.key == key)


# ------------------------------------------------------------------------- engine


def test_no_data_yields_exactly_one_honest_insight(client):
    report = insights_for(client)
    assert keys(report) == {"no_data"}
    assert report.history_weeks == 0


def test_thin_history_abstains_from_trends_and_says_why(client, practice):
    seed_weeks(client, practice, {1: 10, 2: 10})
    report = insights_for(client)
    assert "thin_history" in keys(report)
    assert "sessions_momentum" not in keys(report)
    assert "revenue_momentum" not in keys(report)


def test_rising_sessions_are_called_rising_with_the_arithmetic(client, practice):
    # Six earlier weeks at 10 a week, six recent weeks at 20: +100 percent.
    seed_weeks(client, practice, {n: 10 for n in range(7, 13)} | {n: 20 for n in range(1, 7)})
    report = insights_for(client)
    momentum = by_key(report, "sessions_momentum")
    assert momentum.tone == "good"
    assert "rising" in momentum.headline
    assert momentum.spark  # the reader can see the shape, not just the verdict


def test_falling_sessions_are_called_falling(client, practice):
    seed_weeks(client, practice, {n: 20 for n in range(7, 13)} | {n: 10 for n in range(1, 7)})
    momentum = by_key(insights_for(client), "sessions_momentum")
    assert momentum.tone == "bad"
    assert "falling" in momentum.headline


def test_trend_findings_unlock_at_exactly_the_stated_number_of_weeks(client, practice):
    """The gate has to open where the page says it opens.

    History was measured as the span between the first Monday and the last completed
    Sunday divided by seven. That span is 7n - 1 days for n whole weeks, so the count
    came back one short and every gate needed one week more than it advertised: the
    trend findings waited for a ninth week while the page promised eight.
    """
    from app.reporting.insights import MIN_TREND_WEEKS

    seed_weeks(client, practice, {n: 10 for n in range(1, MIN_TREND_WEEKS + 1)})
    report = insights_for(client)
    assert report.history_weeks == MIN_TREND_WEEKS
    assert "thin_history" not in keys(report)
    assert "sessions_momentum" in keys(report)


def test_one_week_short_of_the_gate_still_abstains(client, practice):
    """The other side of the boundary, so the fix is not simply an inflated count."""
    from app.reporting.insights import MIN_TREND_WEEKS

    seed_weeks(client, practice, {n: 10 for n in range(1, MIN_TREND_WEEKS)})
    report = insights_for(client)
    assert report.history_weeks == MIN_TREND_WEEKS - 1
    assert "thin_history" in keys(report)


def test_a_caseload_falling_to_zero_is_the_mover_it_looks_like(client, practice):
    """The largest possible fall used to be the one fall this could not see.

    Recent rows were filtered to therapists with at least one session, so a caseload
    that went to nothing dropped out of the comparison entirely and no mover fired.
    """
    from app.models.therapist import EmploymentType, Therapist

    with client.app.state.db.session() as db:
        vanished = Therapist(
            display_name="Vanished", employment_type=EmploymentType.SALARIED_BENEFITS
        )
        db.add(vanished)
        db.flush()
        vanished_id = vanished.id

    # The steady therapist keeps the record busy enough for the trend gates to open.
    seed_weeks(client, practice, {n: 15 for n in range(1, 13)})
    # The other one works the earlier four weeks and then stops dead.
    seed_weeks(client, practice, {n: 10 for n in range(5, 9)}, therapist_id=vanished_id)

    report = insights_for(client)
    assert "mover_down" in keys(report), "a caseload going to zero must be visible"
    down = by_key(report, "mover_down")
    assert "Vanished" in down.headline
    assert "-100" in down.headline


def test_a_departed_therapist_is_not_reported_as_a_mover(client, practice):
    """A resignation is not a finding about somebody's work."""
    from app.models.therapist import EmploymentType, Therapist

    with client.app.state.db.session() as db:
        left = Therapist(
            display_name="Departed", employment_type=EmploymentType.SALARIED_BENEFITS, active=False
        )
        db.add(left)
        db.flush()
        left_id = left.id

    seed_weeks(client, practice, {n: 15 for n in range(1, 13)})
    seed_weeks(client, practice, {n: 10 for n in range(5, 9)}, therapist_id=left_id)

    report = insights_for(client)
    assert "Departed" not in "".join(i.headline for i in report.insights)


def test_a_silent_week_in_a_busy_record_is_flagged_as_a_probable_gap(client, practice):
    spec = {n: 15 for n in range(1, 13)}
    del spec[6]  # one week with zero sessions in an otherwise busy record
    seed_weeks(client, practice, spec)
    report = insights_for(client)
    silent = by_key(report, "silent_weeks")
    assert silent.tone == "watch"
    assert "upload" in silent.detail  # points at the fix, not just the problem


def test_unreviewed_rejections_are_an_insight(client, practice):
    from app.models.data_source import ImportError as ImportErrorRow
    from app.models.data_source import RejectReason, SyncMode, SyncRun, SyncStatus

    seed_weeks(client, practice, {n: 12 for n in range(1, 10)})
    with client.app.state.db.session() as db:
        run = SyncRun(source_id=practice["source"], mode=SyncMode.LIVE, status=SyncStatus.SUCCESS)
        db.add(run)
        db.flush()
        db.add(
            ImportErrorRow(
                sync_run_id=run.id,
                source_id=practice["source"],
                reason=RejectReason.UNKNOWN_THERAPIST,
                raw_value="SOMEBODY NEW",
            )
        )
    report = insights_for(client)
    assert by_key(report, "open_rejections").tone == "watch"


def test_a_steady_full_record_says_all_quiet(client, practice):
    seed_weeks(client, practice, {n: 15 for n in range(1, 13)})
    report = insights_for(client)
    tones = {i.key: i.tone for i in report.insights}
    assert "silent_weeks" not in tones
    assert "open_rejections" not in tones
    # Steady momentum is info, so the record is allowed to say so.
    assert tones.get("sessions_momentum") == "info"


def test_top_insights_rank_problems_first(client, practice):
    spec = {n: 15 for n in range(1, 13)}
    del spec[6]
    seed_weeks(client, practice, spec)
    report = insights_for(client)
    tones = [i.tone for i in report.top]
    assert tones == sorted(tones, key=lambda t: {"bad": 0, "watch": 1, "good": 2, "info": 3}[t])


# -------------------------------------------------------------------------- routes


@pytest.fixture
def financial_user(client):
    with client.app.state.db.session() as db:
        email = make_user(
            db, email="insights@example.invalid", role=Role.VIEWER, modules=(Module.FINANCIAL,)
        ).email
    sign_in(client, email)
    return client


def test_payer_concentration_folds_the_member_codes_into_their_group(client, practice):
    """The insight that fires exactly when it matters least, before this fix.

    KS, PC and IA are one payer under three member codes. Unfolded, they competed with
    each other for the top spot, so the practice's dominant payer read as three medium
    ones and no concentration insight ever fired for it. The case worth knowing about
    was the case the insight could not see.
    """
    with client.app.state.db.session() as db:
        for weeks_ago in range(1, 13):
            monday = completed_week_start(weeks_ago)
            # 9 sessions a week under the three IBC codes, 3 under an independent.
            for i, payer in enumerate(["KS", "PC", "IA"] * 3 + ["AET"] * 3):
                db.add(
                    Visit(
                        source_id=practice["source"],
                        therapist_id=practice["therapist"],
                        patient_name=f"Patient B{weeks_ago}x{i}",
                        patient_name_normalized=f"PATIENT B{weeks_ago}X{i}",
                        dos=monday + timedelta(days=i % 5),
                        cpt="90837",
                        cpt_base="90837",
                        insurance_short=payer,
                        total_paid=Decimal("150.00"),
                        total_due=Decimal("150.00"),
                        total_balance=Decimal("0.00"),
                    )
                )

    report = insights_for(client)
    assert "payer_concentration" in keys(report), (
        "three quarters of revenue under one payer must be visible"
    )
    insight = by_key(report, "payer_concentration")
    assert "IBC" in insight.headline
    assert "75%" in insight.headline


def test_insights_page_renders_for_the_financial_grant(financial_user, practice):
    seed_weeks(financial_user, practice, {n: 15 for n in range(1, 13)})
    page = financial_user.get("/reports/insights")
    assert page.status_code == 200
    assert "completed weeks of" in page.text
    assert "never a black box" in page.text


def test_insights_page_refuses_without_the_grant(client):
    with client.app.state.db.session() as db:
        email = make_user(db, email="nogrant@example.invalid", role=Role.VIEWER).email
    sign_in(client, email)
    assert client.get("/reports/insights").status_code == 403


def test_overview_carries_the_top_findings(financial_user, practice):
    seed_weeks(financial_user, practice, {n: 15 for n in range(1, 13)})
    page = financial_user.get("/reports?preset=last_12_weeks").text
    assert "What the record says" in page
    assert "All insights" in page


# ------------------------------------------------------------------ year over year


def seed_on(client, ids, day, count):
    with client.app.state.db.session() as db:
        for i in range(count):
            db.add(
                Visit(
                    source_id=ids["source"],
                    therapist_id=ids["therapist"],
                    patient_name=f"Patient B{day.isoformat()}x{i}",
                    patient_name_normalized=f"PATIENT B{day.isoformat()}X{i}",
                    dos=day,
                    cpt="90837",
                    cpt_base="90837",
                    total_paid=Decimal("150.00"),
                    total_due=Decimal("150.00"),
                    total_balance=Decimal("0.00"),
                )
            )


def yoy_for(client):
    from app.reporting.compare import year_over_year

    with client.app.state.db.session() as db:
        config = config_store.load(db, client.app.state.settings)
        return year_over_year(db, config=config, cpt_exclusions=config.cpt_exclusions)


def test_year_over_year_compares_a_period_to_its_own_shadow(client, practice):
    anchor = today_in("America/New_York") - timedelta(days=30)
    seed_on(client, practice, anchor, 10)
    seed_on(client, practice, anchor.replace(year=anchor.year - 1), 5)

    ytd = next(r for r in yoy_for(client) if "year to date" in r.label)
    assert ytd.has_comparison
    assert ytd.current.sessions == 10
    assert ytd.previous.sessions == 5
    assert ytd.sessions_change == Decimal("100.0")


def test_year_over_year_admits_when_last_year_is_missing(client, practice):
    seed_on(client, practice, today_in("America/New_York") - timedelta(days=30), 10)
    rows = yoy_for(client)
    assert all(not r.has_comparison for r in rows)
    ytd = next(r for r in yoy_for(client) if "year to date" in r.label)
    assert ytd.sessions_change is None


def test_insights_page_renders_the_yoy_section(financial_user, practice):
    seed_weeks(financial_user, practice, {n: 15 for n in range(1, 13)})
    page = financial_user.get("/reports/insights").text
    assert "Same time last year" in page
    assert "Nothing to compare yet" in page  # no last-year data seeded


# ---------------------------------------------------------------------- projection


def test_projection_unlocks_at_a_year_and_states_its_method(client, practice):
    seed_weeks(client, practice, {n: 10 for n in range(1, 80)})
    report = insights_for(client)
    projection = by_key(report, "projection")
    assert "next 4 weeks" in projection.headline
    assert "last year" in projection.detail
    assert "projection_locked" not in keys(report)


def test_projection_abstains_below_a_year_and_names_the_unlock(client, practice):
    seed_weeks(client, practice, {n: 10 for n in range(1, 20)})
    report = insights_for(client)
    assert "projection" not in keys(report)
    locked = by_key(report, "projection_locked")
    assert "full year" in locked.headline


# -------------------------------------------------------------------- backup nag


def test_a_record_with_no_offline_backup_is_nagged(client, practice):
    seed_weeks(client, practice, {n: 15 for n in range(1, 13)})
    report = insights_for(client)
    nag = by_key(report, "backup_overdue")
    assert nag.tone == "watch"
    assert "ever been taken" in nag.headline


def test_a_recent_backup_silences_the_nag(client, practice):
    from app.models.enums import AuditAction
    from app.security import audit

    seed_weeks(client, practice, {n: 15 for n in range(1, 13)})
    with client.app.state.db.session() as db:
        audit.record(
            db,
            action=AuditAction.EXPORT,
            target_type="database",
            target_id="backup",
            actor_label="test",
            detail={"bytes": 1},
        )
    report = insights_for(client)
    assert "backup_overdue" not in keys(report)


# ---------------------------------------------------------------- sync health


def _failing_source(client, ids, *, kind, failures=2, last_success=None):
    from app.models.data_source import (
        DataSource,
        SourceProvider,
        SyncMode,
        SyncRun,
        SyncStatus,
    )

    with client.app.state.db.session() as db:
        source = DataSource(
            label="Live Q",
            provider=SourceProvider.GOOGLE_SHEETS,
            spreadsheet_id="x",
            tab_name="Q Snapshot",
            header_row=1,
            column_mapping={},
            active=True,
            last_synced_at=last_success,
        )
        db.add(source)
        db.flush()
        for _ in range(failures):
            db.add(
                SyncRun(
                    source_id=source.id,
                    mode=SyncMode.LIVE,
                    status=SyncStatus.FAILED,
                    error_message="failed",
                    error_kind=kind,
                )
            )
        return source.id


def test_a_structurally_failing_sync_reaches_the_leadership_page(client, practice):
    """Failed runs live on an admin page a viewer never opens, so stale data read as
    current. Two consecutive structural failures now say so where the figures are,
    in viewer safe words: what it means and who can fix it, never the raw error."""
    from app.models.data_source import ErrorKind

    seed_weeks(client, practice, {1: 10, 2: 10})
    _failing_source(client, practice, kind=ErrorKind.HEADER_DRIFT_MONEY)

    report = insights_for(client)
    assert "sync_failing" in keys(report)
    failing = by_key(report, "sync_failing")
    assert failing.tone == "bad"
    assert "Live Q" in failing.headline
    assert "administrator" in failing.detail
    assert "retrying will not fix" in failing.detail.lower()
    assert "failed" != failing.detail, "raw error text stays on the admin pages"


def test_a_transient_failure_asks_for_patience_not_alarm(client, practice):
    from app.models.data_source import ErrorKind

    seed_weeks(client, practice, {1: 10, 2: 10})
    _failing_source(client, practice, kind=ErrorKind.RATE_LIMITED)

    report = insights_for(client)
    failing = by_key(report, "sync_failing")
    assert failing.tone == "watch"
    assert "temporary" in failing.detail


def test_a_single_failure_never_fires_the_insight(client, practice):
    """One recovered rate limit must not paint the dashboard: the gate is two
    consecutive failures, so transient noise stays noise."""
    from app.models.data_source import ErrorKind

    seed_weeks(client, practice, {1: 10, 2: 10})
    _failing_source(client, practice, kind=ErrorKind.RATE_LIMITED, failures=1)

    report = insights_for(client)
    assert "sync_failing" not in keys(report)


def test_a_successful_dry_run_does_not_silence_a_broken_live_sync(client, practice):
    """A dry run is a preview. Previewing the fixed sheet is exactly what an admin
    does while the live imports are still failing, and the insight must keep firing
    until a LIVE run succeeds."""
    from app.models.data_source import (
        ErrorKind,
        SyncMode,
        SyncRun,
        SyncStatus,
    )

    seed_weeks(client, practice, {1: 10, 2: 10})
    source_id = _failing_source(client, practice, kind=ErrorKind.HEADER_DRIFT_MONEY, failures=2)
    with client.app.state.db.session() as db:
        db.add(SyncRun(source_id=source_id, mode=SyncMode.DRY_RUN, status=SyncStatus.SUCCESS))

    report = insights_for(client)
    assert "sync_failing" in keys(report), "a clean preview is not a working import"


def test_a_recovered_source_never_fires_the_insight(client, practice):
    """A success after a failure means the failure is history, not a finding."""
    from app.models.data_source import (
        ErrorKind,
        SyncMode,
        SyncRun,
        SyncStatus,
    )

    seed_weeks(client, practice, {1: 10, 2: 10})
    source_id = _failing_source(client, practice, kind=ErrorKind.HEADER_DRIFT_MONEY, failures=2)
    with client.app.state.db.session() as db:
        db.add(SyncRun(source_id=source_id, mode=SyncMode.LIVE, status=SyncStatus.SUCCESS))

    report = insights_for(client)
    assert "sync_failing" not in keys(report)
