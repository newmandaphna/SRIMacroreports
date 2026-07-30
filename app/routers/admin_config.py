"""Admin: the practice settings that drive the reports.

The benefits threshold in particular is not a cosmetic setting. It decides which
therapists appear in the alert state on the overview, and that number names a real
person, so it needs to be editable by the practice rather than fixed by whoever
deployed the app.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from app import config_store
from app.models.data_source import DataSource, SourceProvider, SyncMode, SyncRun
from app.models.enums import AuditAction, AuditResult
from app.security import audit
from app.security.deps import AdminUser, DbSession
from app.templating import render

router = APIRouter(prefix="/admin/config", tags=["admin"])

MAX_THRESHOLD = 200
MAX_TIMEOUT_MINUTES = 480
MAX_AUTO_SYNC_DAYS = 30


def _auto_sync_status(db: DbSession) -> tuple:
    """Return (last_auto_run, has_ready_sources) for the config page status panel."""
    last_auto_run = db.execute(
        select(SyncRun)
        .where(SyncRun.run_by_id.is_(None), SyncRun.mode == SyncMode.LIVE)
        .order_by(SyncRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    active_sources = (
        db.execute(
            select(DataSource).where(
                DataSource.active.is_(True),
                DataSource.provider != SourceProvider.UPLOAD,
            )
        )
        .scalars()
        .all()
    )
    has_ready_sources = any(s.is_ready_to_sync for s in active_sources)

    return last_auto_run, has_ready_sources


@router.get("", response_class=HTMLResponse)
async def config_form(request: Request, db: DbSession, auth: AdminUser) -> Response:
    settings = request.app.state.settings
    last_auto_run, has_ready_sources = _auto_sync_status(db)
    return render(
        request,
        "admin/config.html",
        {
            "page_title": "Practice settings",
            "auth": auth,
            "values": config_store.current_values(db, settings),
            "defaults": {
                "benefits_session_threshold": settings.benefits_session_threshold,
                "cpt_exclusion_list": list(settings.cpt_exclusions),
            },
            "last_auto_run": last_auto_run,
            "has_ready_sources": has_ready_sources,
        },
    )


@router.post("")
async def save_config(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    benefits_session_threshold: Annotated[int, Form()],
    cpt_exclusion_list: Annotated[str, Form()],
    week_start_day: Annotated[str, Form()] = "monday",
    session_timeout_minutes: Annotated[int, Form()] = 15,
    auto_sync_days: Annotated[int, Form()] = 0,
    insurance_groups: Annotated[str, Form()] = "",
) -> Response:
    settings = request.app.state.settings
    before = config_store.current_values(db, settings)

    error: str | None = None
    if not 0 < benefits_session_threshold <= MAX_THRESHOLD:
        error = f"The session threshold must be between 1 and {MAX_THRESHOLD}."
    elif not 1 <= session_timeout_minutes <= MAX_TIMEOUT_MINUTES:
        error = f"The session timeout must be between 1 and {MAX_TIMEOUT_MINUTES} minutes."
    elif week_start_day not in {"monday", "sunday"}:
        error = "The week must start on Monday or Sunday."
    elif not 0 <= auto_sync_days <= MAX_AUTO_SYNC_DAYS:
        error = f"Auto sync must be between 0 (off) and {MAX_AUTO_SYNC_DAYS} days."

    groups_mapping: dict[str, str] = {}
    if error is None:
        groups_mapping, groups_error = _parse_groups(insurance_groups)
        if groups_error:
            error = groups_error

    if error:
        audit.record(
            db,
            action=AuditAction.CONFIG_CHANGED,
            result=AuditResult.FAILURE,
            actor=auth.user,
            request=request,
            detail={"rejected": error},
        )
        last_auto_run, has_ready_sources = _auto_sync_status(db)
        return render(
            request,
            "admin/config.html",
            {
                "page_title": "Practice settings",
                "auth": auth,
                "values": before,
                "defaults": {
                    "benefits_session_threshold": settings.benefits_session_threshold,
                    "cpt_exclusion_list": list(settings.cpt_exclusions),
                },
                "error": error,
                "last_auto_run": last_auto_run,
                "has_ready_sources": has_ready_sources,
            },
            status_code=400,
        )

    exclusions = [part.strip().upper() for part in cpt_exclusion_list.split(",") if part.strip()]

    config_store.set_value(
        db,
        config_store.BENEFITS_THRESHOLD,
        benefits_session_threshold,
        actor_id=auth.user.id,
    )
    config_store.set_value(db, config_store.CPT_EXCLUSIONS, exclusions, actor_id=auth.user.id)
    config_store.set_value(db, config_store.WEEK_START_DAY, week_start_day, actor_id=auth.user.id)
    config_store.set_value(
        db, config_store.SESSION_TIMEOUT, session_timeout_minutes, actor_id=auth.user.id
    )
    config_store.set_value(db, config_store.AUTO_SYNC_DAYS, auto_sync_days, actor_id=auth.user.id)
    config_store.set_value(db, config_store.INSURANCE_GROUPS, groups_mapping, actor_id=auth.user.id)

    after = config_store.current_values(db, settings)
    changes = {k: {"from": before[k], "to": after[k]} for k in after if before[k] != after[k]}

    audit.record(
        db,
        action=AuditAction.CONFIG_CHANGED,
        actor=auth.user,
        request=request,
        detail={"changes": changes} if changes else {"changes": "none"},
    )
    return RedirectResponse("/admin/config?saved=1", status_code=303)


def _parse_groups(raw: str) -> tuple[dict[str, str], str | None]:
    """One group per line, "IBC = KS, PC, IA". Returns {member: group}.

    A member claimed by two groups is an error rather than a silent last-wins,
    because a payer counted twice is exactly the confusion this setting removes.
    """
    mapping: dict[str, str] = {}
    for line_number, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        if "=" not in line:
            return {}, f"Line {line_number} of the payer groups needs the form GROUP = CODE, CODE."
        group, _, members = line.partition("=")
        group = group.strip().upper()
        if not group:
            return {}, f"Line {line_number} of the payer groups has no group name."
        for member in members.split(","):
            member = member.strip().upper()
            if not member:
                continue
            if member in mapping and mapping[member] != group:
                return {}, (
                    f"The code {member} appears under both {mapping[member]} and {group}. "
                    "Each code can belong to one group only."
                )
            mapping[member] = group
    return mapping, None
