"""Admin: therapists and the aliases that resolve to them.

Without this the application cannot be used against a real sheet at all: the importer
never creates a therapist (a wrong auto merge is invisible once it happens), so every
row would reject as an unknown therapist with nowhere to go and fix it.

Employment type matters as much as the name. Only salaried therapists are measured
against the benefits threshold, so getting this wrong shows a percentage based
therapist as failing a target they were never given.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select

from app.models.enums import AuditAction, AuditResult
from app.models.therapist import (
    AliasSource,
    EmploymentType,
    Therapist,
    TherapistAlias,
    normalize_therapist_name,
)
from app.models.visit import Visit
from app.security import audit
from app.security.deps import AdminUser, DbSession
from app.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/therapists", tags=["admin"])


def _list_context(db: DbSession) -> dict:
    therapists = (
        db.execute(
            select(Therapist).order_by(Therapist.active.desc(), func.lower(Therapist.display_name))
        )
        .scalars()
        .all()
    )
    visit_counts = dict(
        db.execute(
            select(Visit.therapist_id, func.count(Visit.id)).group_by(Visit.therapist_id)
        ).all()
    )
    return {
        "therapists": therapists,
        "visit_counts": visit_counts,
        "employment_types": list(EmploymentType),
    }


@router.get("", response_class=HTMLResponse)
async def list_therapists(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    prefill: str = Query(default=""),
    deleted: str = Query(default=""),
) -> Response:
    """List therapists. `?prefill=RAW+NAME` pre-fills the alias field on the add form.

    Used by the sync run page to send admins directly here after an unrecognized
    therapist rejection, with the raw name already in the alias box.

    `?deleted=<id>` shows a one-time success banner after a therapist is deleted.
    """
    return render(
        request,
        "admin/therapists.html",
        {
            "page_title": "Therapists",
            "auth": auth,
            "prefill_alias": prefill.strip(),
            "just_deleted": bool(deleted),
            **_list_context(db),
        },
    )


@router.post("/new")
async def create_therapist(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    display_name: Annotated[str, Form()],
    employment_type: Annotated[str, Form()] = EmploymentType.OTHER.value,
    aliases: Annotated[str, Form()] = "",
) -> Response:
    display_name = display_name.strip()

    error = None
    if not display_name:
        error = "Enter a name."
    elif employment_type not in {e.value for e in EmploymentType}:
        error = "Choose a valid employment type."
    elif db.execute(
        select(Therapist).where(func.lower(Therapist.display_name) == display_name.lower())
    ).scalar_one_or_none():
        error = f"A therapist called {display_name!r} already exists."

    if error is None:
        conflict = _first_alias_conflict(db, _split_aliases(aliases, display_name))
        if conflict:
            error = (
                f"The alias {conflict!r} already resolves to another therapist. "
                "Aliases are unique so that two people can never be merged by accident."
            )

    if error:
        return render(
            request,
            "admin/therapists.html",
            {
                "page_title": "Therapists",
                "auth": auth,
                "error": error,
                "form": {
                    "display_name": display_name,
                    "employment_type": employment_type,
                    "aliases": aliases,
                },
                **_list_context(db),
            },
            status_code=400,
        )

    therapist = Therapist(
        display_name=display_name, employment_type=EmploymentType(employment_type)
    )
    db.add(therapist)
    db.flush()

    added = _add_aliases(db, therapist, _split_aliases(aliases, display_name), auth.user.id)

    audit.record(
        db,
        action=AuditAction.MANUAL_EDIT,
        actor=auth.user,
        target_type="therapist",
        target_id=therapist.id,
        request=request,
        detail={
            "created": display_name,
            "employment_type": employment_type,
            "aliases": sorted(added),
        },
    )
    return RedirectResponse(f"/admin/therapists/{therapist.id}", status_code=303)


def _split_aliases(raw: str, display_name: str) -> list[str]:
    """Normalized alias list, always including the display name itself."""
    values = [normalize_therapist_name(part) for part in raw.split(",")]
    values.append(normalize_therapist_name(display_name))
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


def _first_alias_conflict(
    db: DbSession, aliases: list[str], *, owner_id: int | None = None
) -> str | None:
    for alias in aliases:
        existing = db.execute(
            select(TherapistAlias).where(TherapistAlias.alias == alias)
        ).scalar_one_or_none()
        if existing is not None and existing.therapist_id != owner_id:
            return alias
    return None


def _add_aliases(
    db: DbSession, therapist: Therapist, aliases: list[str], actor_id: int
) -> set[str]:
    added: set[str] = set()
    for alias in aliases:
        if db.execute(
            select(TherapistAlias).where(TherapistAlias.alias == alias)
        ).scalar_one_or_none():
            continue
        db.add(
            TherapistAlias(
                therapist_id=therapist.id,
                alias=alias,
                source=AliasSource.MANUAL,
                created_by_id=actor_id,
            )
        )
        added.add(alias)
    return added


@router.get("/{therapist_id}", response_class=HTMLResponse)
async def therapist_detail(
    request: Request, db: DbSession, auth: AdminUser, therapist_id: int
) -> Response:
    therapist = db.get(Therapist, therapist_id)
    if therapist is None:
        return render(
            request,
            "errors/not_found.html",
            {"page_title": "Not found", "auth": auth},
            status_code=404,
        )

    visit_count = db.execute(
        select(func.count(Visit.id)).where(Visit.therapist_id == therapist.id)
    ).scalar_one()

    return render(
        request,
        "admin/therapist_detail.html",
        {
            "page_title": therapist.display_name,
            "auth": auth,
            "therapist": therapist,
            "visit_count": visit_count,
            "employment_types": list(EmploymentType),
        },
    )


@router.post("/{therapist_id}")
async def update_therapist(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    therapist_id: int,
    display_name: Annotated[str, Form()],
    employment_type: Annotated[str, Form()],
    active: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
) -> Response:
    therapist = db.get(Therapist, therapist_id)
    if therapist is None:
        return RedirectResponse("/admin/therapists", status_code=303)

    if employment_type not in {e.value for e in EmploymentType}:
        return RedirectResponse(f"/admin/therapists/{therapist_id}", status_code=303)

    before = {
        "display_name": therapist.display_name,
        "employment_type": therapist.employment_type.value,
        "active": therapist.active,
    }

    therapist.display_name = display_name.strip() or therapist.display_name
    therapist.employment_type = EmploymentType(employment_type)
    therapist.active = bool(active)
    therapist.notes = notes.strip() or None

    after = {
        "display_name": therapist.display_name,
        "employment_type": therapist.employment_type.value,
        "active": therapist.active,
    }

    audit.record(
        db,
        action=AuditAction.MANUAL_EDIT,
        actor=auth.user,
        target_type="therapist",
        target_id=therapist.id,
        request=request,
        detail={"changes": {k: [before[k], after[k]] for k in after if before[k] != after[k]}},
    )
    return RedirectResponse(f"/admin/therapists/{therapist.id}", status_code=303)


@router.post("/{therapist_id}/aliases")
async def add_alias(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    therapist_id: int,
    alias: Annotated[str, Form()],
) -> Response:
    therapist = db.get(Therapist, therapist_id)
    if therapist is None:
        return RedirectResponse("/admin/therapists", status_code=303)

    normalized = normalize_therapist_name(alias)
    if not normalized:
        return RedirectResponse(f"/admin/therapists/{therapist_id}", status_code=303)

    conflict = _first_alias_conflict(db, [normalized], owner_id=therapist.id)
    if conflict:
        audit.record(
            db,
            action=AuditAction.MANUAL_EDIT,
            result=AuditResult.FAILURE,
            actor=auth.user,
            target_type="therapist",
            target_id=therapist.id,
            request=request,
            detail={"alias_conflict": conflict},
        )
        return render(
            request,
            "admin/therapist_detail.html",
            {
                "page_title": therapist.display_name,
                "auth": auth,
                "therapist": therapist,
                "visit_count": db.execute(
                    select(func.count(Visit.id)).where(Visit.therapist_id == therapist.id)
                ).scalar_one(),
                "employment_types": list(EmploymentType),
                "error": (
                    f"{normalized!r} already resolves to a different therapist. Aliases are "
                    "unique so that two people can never be folded into one record."
                ),
            },
            status_code=400,
        )

    _add_aliases(db, therapist, [normalized], auth.user.id)
    audit.record(
        db,
        action=AuditAction.MANUAL_EDIT,
        actor=auth.user,
        target_type="therapist",
        target_id=therapist.id,
        request=request,
        detail={"alias_added": normalized},
    )
    return RedirectResponse(f"/admin/therapists/{therapist.id}", status_code=303)


@router.post("/{therapist_id}/delete")
async def delete_therapist(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    therapist_id: int,
) -> Response:
    """Delete a therapist record and all its aliases.

    Blocked when the therapist has any linked visits (the FK is RESTRICT).  The
    UI already hides the button in that case, but we enforce the guard here too
    so a direct POST cannot bypass it.
    """
    therapist = db.get(Therapist, therapist_id)
    if therapist is None:
        return RedirectResponse("/admin/therapists", status_code=303)

    visit_count = db.execute(
        select(func.count(Visit.id)).where(Visit.therapist_id == therapist_id)
    ).scalar_one()

    if visit_count > 0:
        return render(
            request,
            "admin/therapist_detail.html",
            {
                "page_title": therapist.display_name,
                "auth": auth,
                "therapist": therapist,
                "visit_count": visit_count,
                "employment_types": list(EmploymentType),
                "error": (
                    f"Cannot delete {therapist.display_name!r}: "
                    f"{visit_count} imported row(s) are linked to this record. "
                    "Remove or reassign those visits first."
                ),
            },
            status_code=409,
        )

    name = therapist.display_name
    audit.record(
        db,
        action=AuditAction.MANUAL_EDIT,
        actor=auth.user,
        target_type="therapist",
        target_id=therapist_id,
        request=request,
        detail={"deleted": name},
    )
    db.delete(therapist)
    db.flush()
    logger.info("Deleted therapist %d (%s)", therapist_id, name)
    return RedirectResponse("/admin/therapists?deleted=" + str(therapist_id), status_code=303)


@router.post("/{therapist_id}/aliases/{alias_id}/remove")
async def remove_alias(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    therapist_id: int,
    alias_id: int,
) -> Response:
    entry = db.get(TherapistAlias, alias_id)
    if entry is not None and entry.therapist_id == therapist_id:
        removed = entry.alias
        db.delete(entry)
        audit.record(
            db,
            action=AuditAction.MANUAL_EDIT,
            actor=auth.user,
            target_type="therapist",
            target_id=therapist_id,
            request=request,
            detail={"alias_removed": removed},
        )
    return RedirectResponse(f"/admin/therapists/{therapist_id}", status_code=303)
