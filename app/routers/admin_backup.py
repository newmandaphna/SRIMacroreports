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
import os
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


def dump_invocation(database_url: str) -> tuple[list[str], dict[str, str]]:
    """The pg_dump argv and the environment to run it with.

    The password goes in the environment, never in the argv. A connection URL passed
    as `--dbname=postgresql://user:secret@host/db` is visible in the process list to
    anything that can read /proc on the host, and on a shared runtime that is not a
    theoretical audience. PGPASSWORD is the interface libpq offers for exactly this.
    """
    from urllib.parse import unquote, urlsplit

    plain = plain_postgres_url(database_url)
    parts = urlsplit(plain)

    argv = ["pg_dump", "--format=custom", "--no-owner", "--no-privileges"]
    if parts.hostname:
        argv.append(f"--host={parts.hostname}")
    if parts.port:
        argv.append(f"--port={parts.port}")
    if parts.username:
        argv.append(f"--username={unquote(parts.username)}")
    argv.append(f"--dbname={parts.path.lstrip('/') or 'postgres'}")

    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if parts.password:
        env["PGPASSWORD"] = unquote(parts.password)
    # Everything this application writes and reads is UTC (app/models/types.py), so the
    # dump is taken with the same understanding rather than the host's local one.
    env["PGTZ"] = "UTC"
    return argv, env


@router.post("")
def download_backup(request: Request, db: DbSession, auth: AdminUser) -> Response:
    settings = request.app.state.settings
    if shutil.which("pg_dump") is None:
        return _config_redirect(
            "pg_dump is not installed in this environment, so no backup can be "
            "produced here. See the README's backup section for the alternative."
        )

    command, env = dump_invocation(settings.database_url)
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, values from settings
            command,
            capture_output=True,
            timeout=DUMP_TIMEOUT_SECONDS,
            env=env,
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
