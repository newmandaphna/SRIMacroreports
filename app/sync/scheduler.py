"""Automatic sync, so nobody has to remember a button.

An admin sets an interval in days on the settings page (0 keeps it off). A
background loop wakes hourly and live-syncs any active, fully mapped source whose
last sync is older than the interval. Every run lands in the same sync history and
audit log as a manual one, labelled auto-sync, so the record never shows an import
nobody can account for.

Upload sources are never auto-synced: they have no live workbook to poll.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config_store
from app.config import Settings
from app.models.data_source import DataSource, SourceProvider
from app.models.enums import AuditAction, AuditResult
from app.models.types import utcnow
from app.security import audit
from app.sync.engine import run_sync
from app.sync.sheets import SheetsError, client_for

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 3600


def sources_due(db: Session, *, now: datetime, interval_days: int) -> list[DataSource]:
    """Active, fully mapped, non-upload sources whose last sync is older than the
    interval. A source that has never synced is due immediately once it is ready.

    Ordered oldest sync first, so a pass that two sources both overlap resolves the
    same way every time. Without an ORDER BY the database returns rows in whatever
    order it likes, and two sheets sharing a row at a quarter boundary then took turns
    winning between passes: the same figure moved back and forth with no import
    changing and nothing to point at.
    """
    if interval_days <= 0:
        return []
    cutoff = now - timedelta(days=interval_days)
    sources = (
        db.execute(
            select(DataSource)
            .where(
                DataSource.active.is_(True),
                DataSource.provider != SourceProvider.UPLOAD,
            )
            .order_by(DataSource.last_synced_at.asc().nullsfirst(), DataSource.id.asc())
        )
        .scalars()
        .all()
    )
    return [
        s
        for s in sources
        if s.is_ready_to_sync and (s.last_synced_at is None or s.last_synced_at <= cutoff)
    ]


def run_due_syncs(db: Session, settings: Settings) -> int:
    """One pass: sync everything due. Returns how many sources were synced.

    Each source is its own transaction, committed before the next one starts. The pass
    used to share one, so the claim that a failure on one source does not stop the
    others was only half true: an error escaping the engine's own handling rolled the
    whole transaction back and took every earlier source's imported rows and audit
    entries with it. A pass interrupted part way through now keeps what it finished.
    """
    config = config_store.load(db, settings)
    due = sources_due(db, now=utcnow(), interval_days=config.auto_sync_days)
    source_ids = [s.id for s in due]

    ran = 0
    for source_id in source_ids:
        try:
            ran += _sync_one(db, settings, source_id)
            db.commit()
        except Exception:
            # Contained to this source. The next one starts from a clean transaction
            # rather than inheriting a broken one.
            db.rollback()
            logger.exception("Auto sync of source %s failed; continuing with the rest", source_id)
    return ran


def _sync_one(db: Session, settings: Settings, source_id: int) -> int:
    """One source, inside the caller's transaction. Returns 1 if it ran, 0 if it did not."""
    source = db.get(DataSource, source_id)
    if source is None or not source.active:
        # Deleted or switched off since the due list was built.
        return 0

    try:
        client = client_for(source.provider, settings.google_service_account_json)
    except SheetsError as exc:
        audit.record(
            db,
            action=AuditAction.SYNC_RUN,
            result=AuditResult.FAILURE,
            actor_label="auto-sync",
            target_type="data_source",
            target_id=source.id,
            detail={"auto": True, "error": str(exc)},
        )
        return 0

    result = run_sync(db, source, client, dry_run=False, actor=None)
    audit.record(
        db,
        action=AuditAction.SYNC_RUN,
        result=AuditResult.SUCCESS if result.ok else AuditResult.FAILURE,
        actor_label="auto-sync",
        target_type="data_source",
        target_id=source.id,
        detail={
            "auto": True,
            "mode": result.mode.value,
            "rows_read": result.rows_read,
            "inserted": result.rows_inserted,
            "updated": result.rows_updated,
            "unchanged": result.rows_unchanged,
            "rejected": result.rows_rejected,
            "error": result.error_message,
        },
    )
    return 1


async def auto_sync_loop(app) -> None:
    """Hourly forever. Sleeps first so boot is never slowed by a sync."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        try:
            with app.state.db.session() as db:
                ran = run_due_syncs(db, app.state.settings)
            if ran:
                logger.info("Auto sync pass complete: %d source(s) synced", ran)
        except Exception:
            # The loop must survive anything: a dead scheduler is silent, and
            # silence here means the practice quietly stops getting fresh data.
            logger.exception("Auto sync pass failed; will retry next hour")
