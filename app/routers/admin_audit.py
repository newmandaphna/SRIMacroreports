"""The audit log viewer and its CSV export.

Read and export only. There is no edit or delete route here, and there is no code
path anywhere that could add one without tripping the guards in app/models/audit.py.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.enums import AuditAction, AuditResult
from app.security import audit
from app.security.deps import AdminUser, DbSession
from app.templating import render

router = APIRouter(prefix="/admin/audit", tags=["admin"])

PAGE_SIZE = 100
MAX_EXPORT_ROWS = 50_000


def _filtered_query(action: str | None, result: str | None, days: int):
    stmt = select(AuditLog).order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
    if action and action in {a.value for a in AuditAction}:
        stmt = stmt.where(AuditLog.action == action)
    if result and result in {r.value for r in AuditResult}:
        stmt = stmt.where(AuditLog.result == result)
    if days > 0:
        stmt = stmt.where(AuditLog.occurred_at >= datetime.now(UTC) - timedelta(days=days))
    return stmt


@router.get("", response_class=HTMLResponse)
async def view_audit_log(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    action: str | None = None,
    result: str | None = None,
    days: int = Query(default=30, ge=0, le=2200),
    page: int = Query(default=1, ge=1),
) -> Response:
    stmt = _filtered_query(action, result, days).limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)
    entries = db.execute(stmt).scalars().all()

    # Viewing the audit log is itself an auditable event.
    audit.record(
        db,
        action=AuditAction.AUDIT_VIEWED,
        actor=auth.user,
        request=request,
        detail={"action": action, "result": result, "days": days, "page": page},
    )

    return render(
        request,
        "admin/audit.html",
        {
            "page_title": "Audit log",
            "auth": auth,
            "entries": entries,
            "actions": sorted(a.value for a in AuditAction),
            "results": [r.value for r in AuditResult],
            "filters": {"action": action, "result": result, "days": days},
            "page": page,
            "has_next": len(entries) == PAGE_SIZE,
        },
    )


@router.get("/export.csv")
async def export_audit_log(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    action: str | None = None,
    result: str | None = None,
    days: int = Query(default=30, ge=0, le=2200),
) -> Response:
    stmt = _filtered_query(action, result, days).limit(MAX_EXPORT_ROWS)
    entries = db.execute(stmt).scalars().all()

    # Every export is audit logged, with the filters and the row count.
    audit.record(
        db,
        action=AuditAction.EXPORT,
        actor=auth.user,
        target_type="audit_log",
        request=request,
        detail={
            "action": action,
            "result": result,
            "days": days,
            "rows": len(entries),
            "truncated": len(entries) == MAX_EXPORT_ROWS,
        },
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "occurred_at_utc",
            "actor",
            "action",
            "result",
            "target_type",
            "target_id",
            "source_ip",
            "detail",
        ]
    )
    for e in entries:
        writer.writerow(
            [
                e.occurred_at.isoformat(),
                e.actor_label,
                e.action,
                e.result,
                e.target_type or "",
                e.target_id or "",
                e.source_ip or "",
                e.detail or "",
            ]
        )
    buffer.seek(0)

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="sri-audit-log-{stamp}.csv"'},
    )
