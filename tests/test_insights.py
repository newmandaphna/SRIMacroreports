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
