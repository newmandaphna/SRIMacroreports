"""Aggregate patient flow: how many people, how many new, how many drifting away.

This module's rule is stricter than it looks and different from queries.py: the
patient identity column MAY appear inside COUNT(DISTINCT ...) and in a subquery's
GROUP BY, because a count of people is not a person, but it may NEVER be selected
as output. No function here returns anything except integers and dates, and a test
holds every emitted statement to that.

This keeps the app's core privacy promise intact even inside the patient flow
module: no report page renders a patient name, ever. A version that lists actual
patients (a working referral or discharge list) is a deliberate future decision
for the practice owner, documented in SECURITY.md, not something this module
quietly grows into.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models.visit import Visit
from app.reporting.periods import Granularity, format_period, next_period, period_series
from app.reporting.queries import (
    Filters,
    _base_conditions,
    _is_session,
    _population_conditions,
)

# A patient with no session in this many days is counted as lapsed. A judgement
# call, not a clinical fact: therapy cadences vary, so the page says the number.
LAPSE_DAYS = 90


@dataclass
class FlowPoint:
    start: date
    label: str
    active: int = 0
    new: int = 0


@dataclass
class FlowSummary:
    unique_patients: int
    new_patients: int
    average_sessions: float
    current_census: int
    census_since: date


def _attended(filters: Filters) -> list:
    """Session visits only. A cancellation is not a person seen."""
    return [*_base_conditions(filters), _is_session(filters.cpt_exclusions)]


def _distinct_patients(db: Session, filters: Filters) -> int:
    return db.execute(
        select(func.count(func.distinct(Visit.patient_name_normalized))).where(
            and_(*_attended(filters))
        )
    ).scalar_one()


def _new_patients(db: Session, filters: Filters) -> int:
    """Patients whose first ever attended session falls inside the window.

    First-ever is computed over the whole record, not the window, so a returning
    patient can never be miscounted as new because the picker starts late.

    The therapist and location filters still apply, though, and that is the part that
    was missing: the subquery looked at every session in the practice, so filtering the
    page to one therapist narrowed the patient count beside it and left this one
    counting the whole practice's new patients. On a one therapist view new could
    exceed active, which is arithmetically impossible for the same population. First
    ever now means first ever within the filtered population, which is also what a
    reader of a filtered page means by it.
    """
    first_visit = (
        select(func.min(Visit.dos).label("first_dos"))
        .where(_is_session(filters.cpt_exclusions), *_population_conditions(filters))
        .group_by(Visit.patient_name_normalized)
        .subquery()
    )
    return db.execute(
        select(func.count()).where(
            first_visit.c.first_dos >= filters.start,
            first_visit.c.first_dos <= filters.end,
        )
    ).scalar_one()


def flow_series(
    db: Session,
    filters: Filters,
    granularity: Granularity,
    *,
    week_starts_monday: bool = True,
) -> list[FlowPoint]:
    """Distinct and first-time patients per period. Two small counts per bucket,
    bounded by the period series cap, which is fine at this practice's scale."""
    points: list[FlowPoint] = []
    for start in period_series(
        filters.start, filters.end, granularity, week_starts_monday=week_starts_monday
    ):
        end = min(next_period(start, granularity) - timedelta(days=1), filters.end)
        bucket = filters.replaced(start=max(start, filters.start), end=end)
        points.append(
            FlowPoint(
                start=start,
                label=format_period(start, granularity),
                active=_distinct_patients(db, bucket),
                new=_new_patients(db, bucket),
            )
        )
    return points


def summary(db: Session, filters: Filters, *, today: date) -> FlowSummary:
    unique = _distinct_patients(db, filters)
    sessions = db.execute(
        select(func.count(Visit.id)).where(and_(*_attended(filters)))
    ).scalar_one()
    census_since = today - timedelta(days=LAPSE_DAYS)
    return FlowSummary(
        unique_patients=unique,
        new_patients=_new_patients(db, filters),
        average_sessions=round(sessions / unique, 1) if unique else 0.0,
        current_census=_distinct_patients(db, filters.replaced(start=census_since, end=today)),
        census_since=census_since,
    )
