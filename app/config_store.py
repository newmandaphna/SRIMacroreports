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
# Days between automatic syncs of the live sheet. 0 keeps auto-sync off.
AUTO_SYNC_DAYS = "auto_sync_days"
INSURANCE_GROUPS = "insurance_groups"

# Codes the practice bills separately that are one payer in reality. Seeded here,
# editable in the admin. Maps member code to group name.
DEFAULT_INSURANCE_GROUPS: dict[str, str] = {"KS": "IBC", "PC": "IBC", "IA": "IBC"}

# Threshold bands for the utilization status flag, as a fraction of the threshold.
# At or above the threshold is fine; within this much below it is a watch; further
# below is the alert state, which is the only place alert red is used.
WATCH_BAND = 0.8


@dataclass(frozen=True)
class Expectation:
    """What one therapist is measured against, resolved from their own agreement."""

    kind: str  # "threshold", "band", or "none"
    threshold: int | None = None
    label: str = "-"


@dataclass(frozen=True)
class PracticeConfig:
    """The resolved settings the reporting layer needs."""

    benefits_session_threshold: int
    cpt_exclusions: tuple[str, ...]
    week_starts_monday: bool
    session_timeout_minutes: int
    timezone: str
    # 0 means off. Anything above it is how many days may pass before the
    # background loop syncs the active sources again.
    auto_sync_days: int = 0
    # Member insurance code to group name, e.g. KS/PC/IA all belong to IBC. The
    # breakdowns and the aging table fold members into their group.
    insurance_groups: dict[str, str] = None  # type: ignore[assignment]

    def payer_group(self, code: str | None) -> str | None:
        if not code:
            return None
        return (self.insurance_groups or {}).get(str(code).strip().upper())

    def utilization_status(
        self, sessions_per_period: Decimal | float | int, threshold: int | None = None
    ) -> str:
        """One of ok, watch, below. Alert red is reserved for `below`.

        Compares the weekly average as given, without truncating it to a whole
        number first. Truncating would push a therapist at 19.9 sessions into the
        same bucket as one at 19.0, which is a rounding artifact rather than a
        judgement anyone intended to make.
        """
        limit = self.benefits_session_threshold if threshold is None else threshold
        if limit <= 0:
            return "ok"
        value = Decimal(str(sessions_per_period))
        threshold_value = Decimal(limit)
        if value >= threshold_value:
            return "ok"
        if value >= threshold_value * Decimal(str(WATCH_BAND)):
            return "watch"
        return "below"

    def expectation_for(self, employment_type, override: int | None) -> Expectation:
        """The expectation one therapist is actually measured against.

        A personal override wins over the employment type's default, because
        expectations are individual agreements: one full timer may be on 25 while
        a colleague on insurance panels needs 30.
        """
        from app.models.therapist import (
            FULL_TIME_NO_BENEFITS_EXPECTED,
            PART_TIME_MAX,
            PART_TIME_MIN,
            EmploymentType,
        )

        if not employment_type.counts_against_threshold:
            return Expectation(kind="none")
        if override:
            return Expectation(kind="threshold", threshold=override, label=str(override))
        if employment_type is EmploymentType.SALARIED_BENEFITS:
            t = self.benefits_session_threshold
            return Expectation(kind="threshold", threshold=t, label=str(t))
        if employment_type is EmploymentType.FULL_TIME_NO_BENEFITS:
            t = FULL_TIME_NO_BENEFITS_EXPECTED
            return Expectation(kind="threshold", threshold=t, label=str(t))
        return Expectation(
            kind="band",
            threshold=PART_TIME_MIN,
            label=f"{PART_TIME_MIN} to {PART_TIME_MAX}",
        )

    def status_for(
        self,
        employment_type,
        override: int | None,
        sessions_per_week: Decimal | float | int,
    ) -> str:
        """ok, watch, below, over, or empty for the unmeasured.

        "over" belongs to the part time band only: at or past the cap it is the
        arrangement being exceeded, which is a different conversation from
        underperformance and must not wear the same color.
        """
        from app.models.therapist import PART_TIME_MAX

        expectation = self.expectation_for(employment_type, override)
        if expectation.kind == "none":
            return ""
        value = Decimal(str(sessions_per_week))
        if expectation.kind == "band" and value >= PART_TIME_MAX:
            return "over"
        return self.utilization_status(value, threshold=expectation.threshold)


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
        auto_sync_days=_as_int(stored.get(AUTO_SYNC_DAYS), 0),
        insurance_groups=_as_groups(stored.get(INSURANCE_GROUPS)),
    )


def session_timeout_minutes(db: Session, settings: Settings) -> int:
    """The idle timeout actually in force, in minutes.

    A narrow lookup rather than a full `load`, because this runs on every
    authenticated request. It has to come from the database: the admin settings page
    offers this field and audits changes to it, while every enforcement point read the
    environment value, so setting it did nothing at all. A security control the
    interface claims to configure and does not is worse than no control offered.
    """
    row = db.get(AppConfig, SESSION_TIMEOUT)
    return _as_int(row.value if row is not None else None, settings.session_timeout_minutes)


def _as_groups(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return dict(DEFAULT_INSURANCE_GROUPS)
    return {
        str(code).strip().upper(): str(group).strip().upper()
        for code, group in value.items()
        if str(code).strip() and str(group).strip()
    }


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
        AUTO_SYNC_DAYS: config.auto_sync_days,
        INSURANCE_GROUPS: dict(config.insurance_groups),
    }
