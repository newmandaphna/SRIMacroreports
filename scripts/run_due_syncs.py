"""One auto-sync pass, for a Replit Scheduled Deployment.

The in-process loop in app/sync/scheduler.py cannot be relied on under Autoscale:
the container is spun down between requests, and auto_sync_loop sleeps for an hour
BEFORE its first check, so the countdown restarts from zero on every cold start and
in practice never elapses. This runs the identical pass as a one-shot process.

Deliberately calls run_due_syncs rather than run_sync, so the auto_sync_days
setting, the readiness and upload-source filtering, the oldest-first ordering, the
per-source transaction, and the "auto-sync" audit label all behave exactly as they
do in the loop.

Run command for the scheduled deployment:  python3 -m scripts.run_due_syncs
"""

from __future__ import annotations

import logging
import os
import sys

from app.config import load_settings
from app.db import DatabaseHandle
from app.logging_setup import configure_logging
from app.sync.scheduler import run_due_syncs

logger = logging.getLogger(__name__)

# Replit injects DATABASE_URL for whichever app this job is published under.
# When the job lives in a helper app, point it at the real dashboard database
# by setting SYNC_DATABASE_URL; it always wins over the injected value.
_override = os.environ.get("SYNC_DATABASE_URL")
if _override:
    os.environ["DATABASE_URL"] = _override


def main() -> int:
    settings = load_settings()
    configure_logging("DEBUG" if settings.debug else "INFO")
    handle = DatabaseHandle(settings)
    try:
        with handle.session() as db:
            ran = run_due_syncs(db, settings)
        logger.info("Scheduled auto-sync pass complete: %d source(s) synced", ran)
        return 0
    finally:
        handle.dispose()


if __name__ == "__main__":
    sys.exit(main())
