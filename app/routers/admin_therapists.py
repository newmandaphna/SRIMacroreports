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
    Discipline,
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
        "disciplines": list(Discipline),
    }


@router.get("", response_class=HTMLResponse)
async def list_therapists(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    prefill: str = Query(default=""),
    deleted: str = Query(default=""),
    notice: str = Query(default=""),
    return_to: str = Query(default=""),
) -> Response:
    """List therapists. `?prefill=RAW+NAME` pre-fills the alias field on the add form.

    Used by the sync run page to send admins directly here after an unrecognized
    therapist rejection, with the raw name already in the alias box.

    `?deleted=<id>` shows a one-time success banner after a therapist is deleted.
    """
    from app.practice_roster import PRACTICE_ROSTER

    existing_aliases = set(db.execute(select(TherapistAlias.alias)).scalars().all())
    roster_missing = sum(1 for alias, _, _ in PRACTICE_ROSTER if alias not in existing_aliases)

    return render(
        request,
        "admin/therapists.html",
        {
            "page_title": "Therapists",
            "auth": auth,
            "prefill_alias": prefill.strip(),
            "just_deleted": bool(deleted),
            "notice": notice.strip(),
            # Internal paths only: this value round-trips through a form and becomes
            # a redirect target, so anything not our own admin pages is dropped.
            "return_to": return_to if return_to.startswith("/admin/") else "",
            "roster_missing": roster_missing,
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
    return_to: Annotated[str, Form()] = "",
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
    # Back to the sync run the admin came from, so fixing five rejected names is a
    # loop rather than five dead ends. Internal admin paths only.
    if return_to.startswith("/admin/"):
        return RedirectResponse(return_to, status_code=303)
    return RedirectResponse(f"/admin/therapists/{therapist.id}", status_code=303)


def _parse_expected(raw: str) -> int | None:
    """A personal weekly expectation. Blank means use the type default; anything
    unparseable or absurd is treated as blank rather than saved as garbage."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 1 <= value <= 80 else None


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


@router.get("/bulk", response_class=HTMLResponse)
async def bulk_edit_form(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    saved: str = Query(default=""),
) -> Response:
    """Editable table for updating all therapists' display names and employment types at once.

    Useful after an initial sync that creates placeholder records (all-caps abbreviations
    like ALEXANDER, HARRIS) and sets employment_type=OTHER for every one of them.
    """
    therapists = (
        db.execute(
            select(Therapist).order_by(Therapist.active.desc(), func.lower(Therapist.display_name))
        )
        .scalars()
        .all()
    )
    return render(
        request,
        "admin/therapists_bulk.html",
        {
            "page_title": "Bulk edit therapists",
            "auth": auth,
            "therapists": therapists,
            "employment_types": list(EmploymentType),
            "disciplines": list(Discipline),
            "just_saved": bool(saved),
        },
    )


@router.post("/bulk")
async def bulk_update_therapists(
    request: Request,
    db: DbSession,
    auth: AdminUser,
) -> Response:
    """Apply name and employment-type changes for every therapist in a single submission.

    Strictly two-phase: validate every row first (zero DB writes), then apply all
    changes only when the entire submission is clean. One bad row aborts the whole
    operation so the dataset is never left partially updated.
    """
    form = await request.form()
    therapists = db.execute(select(Therapist).order_by(Therapist.id)).scalars().all()

    # ------------------------------------------------------------------ phase 1
    # Validate every row. Collect proposed (therapist, new_name, new_type) tuples.
    # No DB mutations happen here.
    # ------------------------------------------------------------------ phase 1
    errors: list[str] = []
    # List of (therapist_obj, new_name, new_type, new_discipline, new_expected)
    proposed: list[tuple[Therapist, str, EmploymentType, Discipline, int | None]] = []
    # Lower-cased names already claimed by earlier rows in this same submission.
    # Checked in addition to the DB so two rows renaming to the same value are
    # caught before any write, not at commit time.
    proposed_names_lower: set[str] = set()

    valid_types = {e.value for e in EmploymentType}
    valid_disciplines = {d.value for d in Discipline}

    for therapist in therapists:
        raw_name = str(form.get(f"name_{therapist.id}", "")).strip()
        raw_type = str(form.get(f"employment_{therapist.id}", "")).strip()
        raw_discipline = str(form.get(f"discipline_{therapist.id}", "")).strip()
        raw_expected = str(form.get(f"expected_{therapist.id}", "")).strip()

        if not raw_name:
            errors.append(f"Name for therapist #{therapist.id} is empty.")
            continue

        if raw_type not in valid_types:
            errors.append(f"Invalid employment type for {therapist.display_name!r}.")
            continue

        new_name = raw_name
        new_type = EmploymentType(raw_type)
        new_discipline = (
            Discipline(raw_discipline)
            if raw_discipline in valid_disciplines
            else therapist.discipline
        )
        new_expected = _parse_expected(raw_expected)
        new_name_lower = new_name.lower()

        # Uniqueness check: only needed when the name actually changes.
        if new_name_lower != therapist.display_name.lower():
            # 1. Collision with a name already proposed earlier in this batch.
            if new_name_lower in proposed_names_lower:
                errors.append(f"{new_name!r} appears more than once in this submission.")
                continue

            # 2. Collision with an existing DB record not being renamed right now.
            clash = db.execute(
                select(Therapist).where(
                    func.lower(Therapist.display_name) == new_name_lower,
                    Therapist.id != therapist.id,
                )
            ).scalar_one_or_none()
            if clash:
                errors.append(f"{new_name!r} already belongs to another therapist.")
                continue

        proposed_names_lower.add(new_name_lower)

        if (
            new_name != therapist.display_name
            or new_type != therapist.employment_type
            or new_discipline != therapist.discipline
            or new_expected != therapist.weekly_expected_sessions
        ):
            proposed.append((therapist, new_name, new_type, new_discipline, new_expected))

    # ------------------------------------------------------------------ phase 2
    # Only reached when validation found zero errors. Apply all writes atomically.
    # ------------------------------------------------------------------ phase 2
    if errors:
        therapists_fresh = (
            db.execute(
                select(Therapist).order_by(
                    Therapist.active.desc(), func.lower(Therapist.display_name)
                )
            )
            .scalars()
            .all()
        )
        return render(
            request,
            "admin/therapists_bulk.html",
            {
                "page_title": "Bulk edit therapists",
                "auth": auth,
                "therapists": therapists_fresh,
                "employment_types": list(EmploymentType),
                "disciplines": list(Discipline),
                "just_saved": False,
                "errors": errors,
            },
            status_code=400,
        )

    changed: list[dict] = []
    for therapist, new_name, new_type, new_discipline, new_expected in proposed:
        before = {
            "display_name": therapist.display_name,
            "employment_type": therapist.employment_type.value,
            "discipline": therapist.discipline.value,
            "weekly_expected_sessions": therapist.weekly_expected_sessions,
        }
        therapist.display_name = new_name
        therapist.employment_type = new_type
        therapist.discipline = new_discipline
        therapist.weekly_expected_sessions = new_expected
        changed.append(
            {
                "id": therapist.id,
                "before": before,
                "after": {
                    "display_name": new_name,
                    "employment_type": new_type.value,
                    "discipline": new_discipline.value,
                    "weekly_expected_sessions": new_expected,
                },
            }
        )

    if changed:
        audit.record(
            db,
            action=AuditAction.MANUAL_EDIT,
            actor=auth.user,
            target_type="therapist",
            target_id=None,
            request=request,
            detail={"bulk_update": {"updated": len(changed), "changes": changed}},
        )

    return RedirectResponse("/admin/therapists/bulk?saved=1", status_code=303)


# Registered before the "/{therapist_id}" routes so the literal path is not shadowed.
@router.post("/seed-roster")
async def seed_practice_roster(request: Request, db: DbSession, auth: AdminUser) -> Response:
    """Create the practice's therapists from its own Valant exports, one click.

    Idempotent, and deliberately weaker than the records it creates: an entry is
    skipped whenever its alias or its display name already exists, so anything an
    admin has edited, renamed, or deactivated in the app wins over this list. The
    importer itself still never creates a therapist; this route only exists so that
    41 known names do not have to be typed in one at a time.
    """
    from urllib.parse import quote

    from app.practice_roster import PRACTICE_ROSTER, SEED_NOTE

    created = aliased = skipped = 0
    for alias, display_name, credential in PRACTICE_ROSTER:
        alias_owner = db.execute(
            select(TherapistAlias).where(TherapistAlias.alias == alias)
        ).scalar_one_or_none()
        if alias_owner is not None:
            skipped += 1
            continue

        therapist = db.execute(
            select(Therapist).where(func.lower(Therapist.display_name) == display_name.lower())
        ).scalar_one_or_none()
        if therapist is None:
            therapist = Therapist(
                display_name=display_name,
                employment_type=EmploymentType.OTHER,
                notes=f"{credential}. {SEED_NOTE}" if credential else SEED_NOTE,
            )
            db.add(therapist)
            db.flush()
            created += 1
        else:
            # The person exists under their full name; only the sheet surname was
            # missing, which is exactly what stops the importer resolving them.
            aliased += 1
        db.add(
            TherapistAlias(
                therapist_id=therapist.id,
                alias=alias,
                source=AliasSource.MANUAL,
                created_by_id=auth.user.id,
            )
        )

    audit.record(
        db,
        action=AuditAction.MANUAL_EDIT,
        actor=auth.user,
        target_type="therapist",
        target_id=None,
        request=request,
        detail={"seed_roster": {"created": created, "alias_added": aliased, "skipped": skipped}},
    )

    parts = []
    if created:
        parts.append(f"{created} therapist{'s' if created != 1 else ''} created")
    if aliased:
        parts.append(f"{aliased} alias{'es' if aliased != 1 else ''} added to existing records")
    if skipped:
        parts.append(f"{skipped} already present and left untouched")
    message = (", ".join(parts) or "Nothing to do, the roster is already present") + (
        ". Every record is editable here, and each still needs its employment type set."
        if created
        else "."
    )
    return RedirectResponse(f"/admin/therapists?notice={quote(message)}", status_code=303)


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
            "disciplines": list(Discipline),
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
    discipline: Annotated[str, Form()] = Discipline.THERAPIST.value,
    weekly_expected_sessions: Annotated[str, Form()] = "",
    active: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
) -> Response:
    therapist = db.get(Therapist, therapist_id)
    if therapist is None:
        return RedirectResponse("/admin/therapists", status_code=303)

    if employment_type not in {e.value for e in EmploymentType}:
        return RedirectResponse(f"/admin/therapists/{therapist_id}", status_code=303)
    if discipline not in {d.value for d in Discipline}:
        discipline = Discipline.THERAPIST.value

    before = {
        "display_name": therapist.display_name,
        "employment_type": therapist.employment_type.value,
        "discipline": therapist.discipline.value,
        "weekly_expected_sessions": therapist.weekly_expected_sessions,
        "active": therapist.active,
    }

    therapist.display_name = display_name.strip() or therapist.display_name
    therapist.employment_type = EmploymentType(employment_type)
    therapist.discipline = Discipline(discipline)
    therapist.weekly_expected_sessions = _parse_expected(weekly_expected_sessions)
    therapist.active = bool(active)
    therapist.notes = notes.strip() or None

    after = {
        "display_name": therapist.display_name,
        "employment_type": therapist.employment_type.value,
        "discipline": therapist.discipline.value,
        "weekly_expected_sessions": therapist.weekly_expected_sessions,
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
                "disciplines": list(Discipline),
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
                "disciplines": list(Discipline),
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
