"""Admin editable settings, held in the database.

Environment variables provide the initial value for each setting. Once an admin edits
one, the database wins, so a change made in the UI is not silently undone by the next
deploy.

Every change is audit logged (SECURITY.md section 4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.data_source import AppConfig

logger = logging.getLogger(__name__)

BENEFITS_THRESHOLD = "benefits_session_threshold"
CPT_EXCLUSIONS = "cpt_exclusion_list"
WEEK_START_DAY = "week_start_day"
SESSION_TIMEOUT = "session_timeout_minutes"

# Threshold bands for the utilization status flag, as a fraction of the threshold.
# At or above the threshold is fine; within this much below it is a watch; further
# below is the alert state, which is the only place alert red is used.
WATCH_BAND = 0.8


@dataclass(frozen=True)
class PracticeConfig:
    """The resolved settings the reporting layer needs."""

    benefits_session_threshold: int
    cpt_exclusions: tuple[str, ...]
    week_starts_monday: bool
    session_timeout_minutes: int
    timezone: str

    def utilization_status(self, sessions_per_period: Decimal | float | int) -> str:
        """One of ok, watch, below. Alert red is reserved for `below`.

        Compares the weekly average as given, without truncating it to a whole
        number first. Truncating would push a therapist at 19.9 sessions into the
        same bucket as one at 19.0, which is a rounding artifact rather than a
        judgement anyone intended to make.
        """
        if self.benefits_session_threshold <= 0:
            return "ok"
        value = Decimal(str(sessions_per_period))
        threshold = Decimal(self.benefits_session_threshold)
        if value >= threshold:
            return "ok"
        if value >= threshold * Decimal(str(WATCH_BAND)):
            return "watch"
        return "below"


def load(db: Session, settings: Settings) -> PracticeConfig:
    """Read the config table, falling back to the environment for anything unset."""
    stored = {row.key: row.value for row in db.execute(select(AppConfig)).scalars()}

    exclusions = stored.get(CPT_EXCLUSIONS)
    if not isinstance(exclusions, list):
        exclusions = list(settings.cpt_exclusions)

    week_start = stored.get(WEEK_START_DAY) or settings.week_start_day

    return PracticeConfig(
        benefits_session_threshold=_as_int(
            stored.get(BENEFITS_THRESHOLD), settings.benefits_session_threshold
        ),
        cpt_exclusions=tuple(str(c).strip().upper() for c in exclusions if str(c).strip()),
        week_starts_monday=str(week_start).lower() != "sunday",
        session_timeout_minutes=_as_int(
            stored.get(SESSION_TIMEOUT), settings.session_timeout_minutes
        ),
        timezone=settings.timezone,
    )


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def set_value(db: Session, key: str, value: Any, *, actor_id: int | None) -> None:
    """Upsert one setting. The caller writes the audit entry, with the old value."""
    entry = db.get(AppConfig, key)
    if entry is None:
        db.add(AppConfig(key=key, value=value, updated_by_id=actor_id))
    else:
        entry.value = value
        entry.updated_by_id = actor_id


def current_values(db: Session, settings: Settings) -> dict[str, Any]:
    """What to show in the admin form, resolved the same way `load` resolves it."""
    config = load(db, settings)
    return {
        BENEFITS_THRESHOLD: config.benefits_session_threshold,
        CPT_EXCLUSIONS: list(config.cpt_exclusions),
        WEEK_START_DAY: "monday" if config.week_starts_monday else "sunday",
        SESSION_TIMEOUT: config.session_timeout_minutes,
    }
