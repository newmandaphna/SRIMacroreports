"""Therapist utilization: the status board, the drill in, and the notes.

The number on this page is about a person's work, so two restraints run through it:

  a therapist with no session minimum carries no status at all, rather than a
  passing one, because a green tick implies a target they were never given

  a low count without its context is a conclusion waiting to be drawn wrongly, so
  the note travels with the number onto the board itself, not onto a page somebody
  has to think to open
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import select

from app.config_store import PracticeConfig
from app.models.enums import AuditAction, AuditResult, Module
from app.models.therapist import Therapist
from app.models.types import utcnow
from app.models.utilization import UtilizationNote
from app.reporting import queries
from app.reporting.periods import period_start
from app.routers.reports import Ctx, ReportContext, _ranked_for_board
from app.security import audit
from app.security.deps import AuthContext, DbSession, require_module, require_utilization_writer
from app.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports/therapist-utilization", tags=["reports"])

UtilizationUser = Annotated[AuthContext, Depends(require_module(Module.THERAPIST_UTILIZATION))]
UtilizationWriter = Annotated[AuthContext, Depends(require_utilization_writer)]

MAX_NOTE_CHARS = 2000


def _board(db: DbSession, ctx: ReportContext) -> list:
    rows = queries.by_therapist(db, ctx.filters, weeks_in_range=ctx.weeks_in_range)
    notes = queries.latest_notes(db, [r.therapist_id for r in rows])
    for row in rows:
        row.notes = notes.get(row.therapist_id)
    return _ranked_for_board(rows, ctx.config)


@router.get("", response_class=HTMLResponse)
async def status_board(
    request: Request, db: DbSession, ctx: Ctx, auth: UtilizationUser
) -> Response:
    board = _board(db, ctx)

    return render(
        request,
        "reports/utilization.html",
        {
            "page_title": "Therapist utilization",
            "auth": auth,
            "active_page": "utilization",
            "board": board,
            "can_write": auth.role.can_write_utilization,
            "summary": _summarize(board, ctx.config),
            **ctx.as_template_context(),
        },
    )


def _summarize(board: list, config: PracticeConfig) -> dict:
    measured = [(row, status) for row, status in board if status]
    return {
        "measured": len(measured),
        "unmeasured": len(board) - len(measured),
        "below": sum(1 for _, s in measured if s == "below"),
        "watch": sum(1 for _, s in measured if s == "watch"),
        "ok": sum(1 for _, s in measured if s == "ok"),
        "threshold": config.benefits_session_threshold,
    }


@router.get("/export.csv")
async def export_board(
    request: Request, db: DbSession, ctx: Ctx, auth: UtilizationUser
) -> Response:
    board = _board(db, ctx)

    audit.record(
        db,
        action=AuditAction.EXPORT,
        actor=auth.user,
        target_type="report",
        target_id="therapist_utilization",
        request=request,
        detail={
            "rows": len(board),
            "start": ctx.range.start.isoformat(),
            "end": ctx.range.end.isoformat(),
            "threshold": ctx.config.benefits_session_threshold,
        },
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "SRI Practice Dashboard, therapist utilization",
            f"{ctx.range.start.isoformat()} to {ctx.range.end.isoformat()}",
            f"threshold {ctx.config.benefits_session_threshold} sessions per week",
            f"exported {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}",
        ]
    )
    writer.writerow([])
    writer.writerow(
        [
            "Therapist",
            "Employment type",
            "Sessions",
            "Sessions per week",
            "Status",
            "Collected",
            "Cancellations",
            "Latest note",
        ]
    )
    for row, status in board:
        writer.writerow(
            [
                row.display_name,
                row.employment_type.label,
                row.sessions,
                row.sessions_per_week,
                status or "not measured",
                row.collected,
                row.cancellations,
                row.notes or "",
            ]
        )
    buffer.seek(0)

    filename = f"sri-utilization-{ctx.range.start}-to-{ctx.range.end}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{therapist_id}", response_class=HTMLResponse)
async def therapist_drill_in(
    request: Request, db: DbSession, ctx: Ctx, auth: UtilizationUser, therapist_id: int
) -> Response:
    therapist = db.get(Therapist, therapist_id)
    if therapist is None:
        return render(
            request,
            "errors/not_found.html",
            {"page_title": "Not found", "auth": auth},
            status_code=404,
        )

    history = queries.therapist_history(
        db,
        therapist_id,
        ctx.filters,
        ctx.granularity,
        week_starts_monday=ctx.config.week_starts_monday,
    )

    scoped = ctx.filters.replaced(start=ctx.range.start, end=ctx.range.end)
    totals = queries.totals(
        db,
        queries.Filters(
            start=scoped.start,
            end=scoped.end,
            cpt_exclusions=scoped.cpt_exclusions,
            therapist_ids=(therapist_id,),
            locations=scoped.locations,
        ),
    )

    measured = therapist.employment_type.counts_against_threshold
    periods_with_status = [
        (
            point,
            ctx.config.utilization_status(point.sessions) if measured else "",
        )
        for point in history
    ]

    return render(
        request,
        "reports/therapist_history.html",
        {
            "page_title": therapist.display_name,
            "auth": auth,
            "active_page": "utilization",
            "therapist": therapist,
            "measured": measured,
            "history": periods_with_status,
            "totals": totals,
            "can_write": auth.role.can_write_utilization,
            **ctx.as_template_context(),
        },
    )


@router.post("/{therapist_id}/notes")
async def save_note(
    request: Request,
    db: DbSession,
    ctx: Ctx,
    auth: UtilizationWriter,
    therapist_id: int,
    period: Annotated[str, Form()],
    body: Annotated[str, Form()] = "",
) -> Response:
    """Create, replace, or clear the note for one therapist and one period.

    Restricted to managers and admins by require_utilization_writer: a viewer is read
    only on this module and gets a 403 from the dependency, not a hidden form.
    """
    therapist = db.get(Therapist, therapist_id)
    if therapist is None:
        return RedirectResponse("/reports/therapist-utilization", status_code=303)

    try:
        raw_period = date.fromisoformat(period)
    except ValueError:
        audit.record(
            db,
            action=AuditAction.MANUAL_EDIT,
            result=AuditResult.FAILURE,
            actor=auth.user,
            target_type="utilization_note",
            request=request,
            detail={"reason": "unparseable period", "therapist_id": therapist_id},
        )
        return RedirectResponse(
            f"/reports/therapist-utilization/{therapist_id}?{ctx.query_string()}",
            status_code=303,
        )

    # Snap to the period boundary so a note always attaches to a period the reports
    # actually render, whatever date the form happened to submit.
    normalized = period_start(
        raw_period, ctx.granularity, week_starts_monday=ctx.config.week_starts_monday
    )
    text = body.strip()[:MAX_NOTE_CHARS]

    existing = db.execute(
        select(UtilizationNote).where(
            UtilizationNote.therapist_id == therapist_id,
            UtilizationNote.period_start == normalized,
            UtilizationNote.granularity == ctx.granularity,
        )
    ).scalar_one_or_none()

    before = existing.body if existing else None

    if not text:
        if existing is not None:
            db.delete(existing)
        action_detail = {"cleared": True}
    elif existing is None:
        db.add(
            UtilizationNote(
                therapist_id=therapist_id,
                period_start=normalized,
                granularity=ctx.granularity,
                body=text,
                created_by_id=auth.user.id,
                updated_by_id=auth.user.id,
            )
        )
        action_detail = {"created": True}
    else:
        existing.body = text
        existing.updated_by_id = auth.user.id
        existing.updated_at = utcnow()
        action_detail = {"updated": True}

    # The note text itself is recorded, before and after. It is staff written context
    # about a colleague's workload, not patient information, and the point of an
    # audit trail on it is that an edit to someone's explanation is recoverable.
    audit.record(
        db,
        action=AuditAction.MANUAL_EDIT,
        actor=auth.user,
        target_type="utilization_note",
        target_id=f"{therapist_id}:{normalized.isoformat()}",
        request=request,
        detail={
            **action_detail,
            "therapist": therapist.display_name,
            "period": normalized.isoformat(),
            "granularity": ctx.granularity.value,
            "from": before,
            "to": text or None,
        },
    )

    return RedirectResponse(
        f"/reports/therapist-utilization/{therapist_id}?{ctx.query_string()}",
        status_code=303,
    )
