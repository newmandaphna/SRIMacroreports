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
    interval. A source that has never synced is due immediately once it is ready."""
    if interval_days <= 0:
        return []
    cutoff = now - timedelta(days=interval_days)
    sources = (
        db.execute(
            select(DataSource).where(
                DataSource.active.is_(True),
                DataSource.provider != SourceProvider.UPLOAD,
            )
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

    A failure on one source is audited and does not stop the others.
    """
    config = config_store.load(db, settings)
    interval = config.auto_sync_days

    if interval <= 0:
        logger.info("Auto-sync check: disabled (auto_sync_days=0)")
        return 0

    due = sources_due(db, now=utcnow(), interval_days=interval)
    logger.info(
        "Auto-sync check: interval=%d day(s), %d source(s) due to sync",
        interval,
        len(due),
    )

    ran = 0
    for source in due:
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
            continue

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
        ran += 1
    return ran


async def auto_sync_loop(app) -> None:
    """Hourly forever. Sleeps first so boot is never slowed by a sync."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        try:
            with app.state.db.session() as db:
                ran = run_due_syncs(db, app.state.settings)
            logger.info("Auto-sync wake complete: %d source(s) synced this hour", ran)
        except Exception:
            # The loop must survive anything: a dead scheduler is silent, and
            # silence here means the practice quietly stops getting fresh data.
            logger.exception("Auto-sync pass failed; will retry next hour")
