"""Admin: a real offline database backup, on demand.

Replit's managed PostgreSQL keeps continuous point-in-time recovery for a rolling
window (7 days on the current plan) and offers no snapshots and no full export
button. That protects against last week's mistake, not against corruption
discovered late, a plan lapse, or account level loss. This route streams a
pg_dump custom-format archive to the admin's browser, so an offline copy can
exist somewhere Replit is not.

The dump contains every table, PHI included. It is a deliberate, audit logged
PHI export, admin only, and whoever clicks the button becomes the custodian of
the file: it belongs on an encrypted drive, not in a download folder.

Restore, against a fresh database:

    pg_restore --clean --if-exists --no-owner -d <connection-url> <file>
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from app.models.enums import AuditAction, AuditResult
from app.models.types import utcnow
from app.security import audit
from app.security.deps import AdminUser, DbSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/backup", tags=["admin"])

DUMP_TIMEOUT_SECONDS = 180


def plain_postgres_url(database_url: str) -> str:
    """pg_dump does not understand SQLAlchemy driver suffixes: postgresql+psycopg2
    becomes postgresql. Everything after the scheme is untouched."""
    scheme, _, rest = database_url.partition("://")
    return f"{scheme.split('+', 1)[0]}://{rest}"


@router.post("")
async def download_backup(request: Request, db: DbSession, auth: AdminUser) -> Response:
    settings = request.app.state.settings
    if shutil.which("pg_dump") is None:
        return _config_redirect(
            "pg_dump is not installed in this environment, so no backup can be "
            "produced here. See the README's backup section for the alternative."
        )

    command = [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        f"--dbname={plain_postgres_url(settings.database_url)}",
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, URL from settings
            command,
            capture_output=True,
            timeout=DUMP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _config_redirect(
            f"The backup did not finish within {DUMP_TIMEOUT_SECONDS} seconds and was "
            "abandoned. Try again; if it keeps happening the database has outgrown "
            "this button."
        )

    if completed.returncode != 0 or not completed.stdout:
        # stderr can echo the host name but never credentials; log it, do not show it.
        logger.error("pg_dump failed: %s", completed.stderr.decode(errors="replace")[:500])
        audit.record(
            db,
            action=AuditAction.EXPORT,
            result=AuditResult.FAILURE,
            actor=auth.user,
            target_type="database",
            target_id="backup",
            request=request,
            detail={"error": "pg_dump failed; see application log"},
        )
        return _config_redirect(
            "The backup failed. The reason is in the application log; the usual "
            "cause is a pg_dump client older than the database server."
        )

    stamp = utcnow().strftime("%Y%m%d-%H%M")
    filename = f"sri-backup-{stamp}Z.dump"

    # The whole practice's history leaves the building in this response. That is
    # the point, and it is why the record of it must be unmissable.
    audit.record(
        db,
        action=AuditAction.EXPORT,
        actor=auth.user,
        target_type="database",
        target_id="backup",
        request=request,
        detail={"bytes": len(completed.stdout), "filename": filename},
    )

    return Response(
        content=completed.stdout,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _config_redirect(message: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/config?problem={quote(message)}", status_code=303)
