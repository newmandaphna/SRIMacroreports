"""Room utilization, behind a feature flag that is off by default.

The practice has schedules but has not confirmed a source of actual room usage, so
this module ships inert: with the flag off every route here is a 404, the module is
absent from navigation, and nothing appears anywhere else in the application. Turning
`FEATURE_ROOM_UTILIZATION` on is a deployment decision, made once a real source exists.

Because there is no source, the only ingestion path is a manual CSV upload. That is
deliberate rather than a stopgap: inventing room usage from appointment schedules would
produce a confident number for something nobody actually measured.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import func, select

from app.models.enums import AuditAction, AuditResult, Module
from app.models.room import Room, RoomUsage, UsageSource
from app.reporting.periods import format_period, period_series, period_start
from app.routers.reports import Ctx, ReportContext
from app.security import audit
from app.security.deps import (
    AdminUser,
    AuthContext,
    DbSession,
    FeatureDisabled,
    require_module,
)
from app.sync.normalize import ParseError, clean_text, parse_date
from app.templating import render

logger = logging.getLogger(__name__)


def require_room_feature(request: Request) -> None:
    """Refuse every route here when the flag is off.

    Attached to the router rather than to each route, so it runs before the auth
    dependencies do. Checking it after authentication would send an anonymous
    visitor to the login page, which tells them the path exists.
    """
    if not request.app.state.settings.room_utilization_enabled:
        raise FeatureDisabled


router = APIRouter(tags=["rooms"], dependencies=[Depends(require_room_feature)])

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_ROWS = 20_000

# Accepted CSV headers, lowercased. Deliberately forgiving about the spelling of
# each one, because this file is produced by hand.
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "room": ("room", "room name", "name"),
    "date": ("date", "usage date", "day"),
    "slots_used": ("slots used", "used", "slots_used", "sessions"),
    "slots_available": ("slots available", "available", "slots_available", "capacity"),
    "note": ("note", "notes", "comment"),
}


RoomUser = Annotated[AuthContext, Depends(require_module(Module.ROOM_UTILIZATION))]


# ------------------------------------------------------------------------- reporting


@dataclass
class GridCell:
    label: str
    rate: Decimal | None
    used: int
    available: int


@dataclass
class GridRow:
    room: Room
    cells: list[GridCell]
    total_used: int = 0
    total_available: int = 0

    @property
    def rate(self) -> Decimal | None:
        if self.total_available <= 0:
            return None
        return (Decimal(self.total_used) / Decimal(self.total_available) * 100).quantize(
            Decimal("0.1")
        )


def _build_grid(db: DbSession, ctx: ReportContext) -> tuple[list[GridRow], list[str]]:
    """Rooms down, periods across. Empty periods present, so gaps read as gaps."""
    starts = period_series(
        ctx.range.start,
        ctx.range.end,
        ctx.granularity,
        week_starts_monday=ctx.config.week_starts_monday,
    )
    labels = [format_period(s, ctx.granularity) for s in starts]

    rooms = (
        db.execute(select(Room).where(Room.active.is_(True)).order_by(func.lower(Room.name)))
        .scalars()
        .all()
    )

    rows = db.execute(
        select(
            RoomUsage.room_id,
            RoomUsage.usage_date,
            func.sum(RoomUsage.slots_used),
            func.sum(RoomUsage.slots_available),
        )
        .where(RoomUsage.usage_date >= ctx.range.start, RoomUsage.usage_date <= ctx.range.end)
        .group_by(RoomUsage.room_id, RoomUsage.usage_date)
    ).all()

    totals: dict[tuple[int, date], list[int]] = {}
    for room_id, usage_date, used, available in rows:
        bucket = period_start(
            usage_date, ctx.granularity, week_starts_monday=ctx.config.week_starts_monday
        )
        entry = totals.setdefault((room_id, bucket), [0, 0])
        entry[0] += int(used or 0)
        entry[1] += int(available or 0)

    grid: list[GridRow] = []
    for room in rooms:
        cells: list[GridCell] = []
        row = GridRow(room=room, cells=cells)
        for start, label in zip(starts, labels, strict=True):
            used, available = totals.get((room.id, start), [0, 0])
            rate = (
                (Decimal(used) / Decimal(available) * 100).quantize(Decimal("0.1"))
                if available > 0
                else None
            )
            cells.append(GridCell(label=label, rate=rate, used=used, available=available))
            row.total_used += used
            row.total_available += available
        grid.append(row)

    return grid, labels


@router.get("/reports/room-utilization", response_class=HTMLResponse)
async def room_report(request: Request, db: DbSession, ctx: Ctx, auth: RoomUser) -> Response:
    grid, labels = _build_grid(db, ctx)
    recorded_days = db.execute(select(func.count(RoomUsage.id))).scalar_one()

    return render(
        request,
        "reports/rooms.html",
        {
            "page_title": "Room utilization",
            "auth": auth,
            "active_page": "rooms",
            "grid": grid,
            "labels": labels,
            "recorded_days": recorded_days,
            **ctx.as_template_context(),
        },
    )


@router.get("/reports/room-utilization/export.csv")
async def export_rooms(request: Request, db: DbSession, ctx: Ctx, auth: RoomUser) -> Response:
    grid, labels = _build_grid(db, ctx)

    audit.record(
        db,
        action=AuditAction.EXPORT,
        actor=auth.user,
        target_type="report",
        target_id="room_utilization",
        request=request,
        detail={
            "rooms": len(grid),
            "start": ctx.range.start.isoformat(),
            "end": ctx.range.end.isoformat(),
        },
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "SRI Practice Dashboard, room utilization",
            f"{ctx.range.start.isoformat()} to {ctx.range.end.isoformat()}",
            f"exported {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}",
        ]
    )
    writer.writerow([])
    writer.writerow(["Room", "Location", *labels, "Overall rate"])
    for row in grid:
        writer.writerow(
            [
                row.room.name,
                row.room.location_short or "",
                *[("" if c.rate is None else f"{c.rate}") for c in row.cells],
                "" if row.rate is None else f"{row.rate}",
            ]
        )
    buffer.seek(0)

    filename = f"sri-rooms-{ctx.range.start}-to-{ctx.range.end}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------- administration


@router.get("/admin/rooms", response_class=HTMLResponse)
async def admin_rooms(request: Request, db: DbSession, auth: AdminUser) -> Response:
    return render(
        request,
        "admin/rooms.html",
        {"page_title": "Rooms", "auth": auth, **_rooms_context(db)},
    )


def _rooms_context(db: DbSession) -> dict:
    rooms = (
        db.execute(select(Room).order_by(Room.active.desc(), func.lower(Room.name))).scalars().all()
    )
    day_counts = dict(
        db.execute(
            select(RoomUsage.room_id, func.count(RoomUsage.id)).group_by(RoomUsage.room_id)
        ).all()
    )
    return {"rooms": rooms, "day_counts": day_counts}


@router.post("/admin/rooms/new")
async def create_room(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    name: Annotated[str, Form()],
    location_short: Annotated[str, Form()] = "",
    default_slots_per_day: Annotated[int, Form()] = 0,
) -> Response:
    name = name.strip()
    error = None
    if not name:
        error = "Enter a room name."
    elif db.execute(select(Room).where(func.lower(Room.name) == name.lower())).scalar_one_or_none():
        error = f"A room called {name!r} already exists."
    elif default_slots_per_day < 0:
        error = "Slots per day cannot be negative."

    if error:
        return render(
            request,
            "admin/rooms.html",
            {"page_title": "Rooms", "auth": auth, "error": error, **_rooms_context(db)},
            status_code=400,
        )

    room = Room(
        name=name,
        location_short=clean_text(location_short).upper() or None,
        default_slots_per_day=default_slots_per_day,
    )
    db.add(room)
    db.flush()

    audit.record(
        db,
        action=AuditAction.MANUAL_EDIT,
        actor=auth.user,
        target_type="room",
        target_id=room.id,
        request=request,
        detail={"created": name, "default_slots_per_day": default_slots_per_day},
    )
    return RedirectResponse("/admin/rooms", status_code=303)


@dataclass
class UploadOutcome:
    rows_read: int = 0
    inserted: int = 0
    updated: int = 0
    rejected: list[tuple[int, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rejected is None:
            self.rejected = []


@router.post("/admin/rooms/upload")
async def upload_usage(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    file: Annotated[UploadFile, File()],
) -> Response:
    """Import a hand written CSV of room usage.

    Same posture as the sheet importer: a row that cannot be read is rejected with its
    reason and its line number, never coerced into a zero.
    """
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        return _upload_error(request, db, auth, "That file is larger than 2 MB.")

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return _upload_error(
            request, db, auth, "That file is not UTF-8 text. Export it as CSV and retry."
        )

    outcome = _apply_upload(db, text, actor_id=auth.user.id)

    audit.record(
        db,
        action=AuditAction.MANUAL_EDIT,
        result=AuditResult.SUCCESS if outcome.rows_read else AuditResult.FAILURE,
        actor=auth.user,
        target_type="room_usage",
        request=request,
        detail={
            "filename": file.filename,
            "rows_read": outcome.rows_read,
            "inserted": outcome.inserted,
            "updated": outcome.updated,
            "rejected": len(outcome.rejected),
        },
    )

    return render(
        request,
        "admin/rooms.html",
        {
            "page_title": "Rooms",
            "auth": auth,
            "outcome": outcome,
            **_rooms_context(db),
        },
    )


def _upload_error(request: Request, db: DbSession, auth: AdminUser, message: str) -> Response:
    audit.record(
        db,
        action=AuditAction.MANUAL_EDIT,
        result=AuditResult.FAILURE,
        actor=auth.user,
        target_type="room_usage",
        request=request,
        detail={"upload_rejected": message},
    )
    return render(
        request,
        "admin/rooms.html",
        {"page_title": "Rooms", "auth": auth, "error": message, **_rooms_context(db)},
        status_code=400,
    )


def _resolve_headers(fieldnames: list[str] | None) -> dict[str, str]:
    """Map our canonical field names onto whatever the file happens to call them."""
    if not fieldnames:
        return {}
    lowered = {(f or "").strip().lower(): f for f in fieldnames}
    resolved: dict[str, str] = {}
    for canonical, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                resolved[canonical] = lowered[alias]
                break
    return resolved


def _apply_upload(db: DbSession, text: str, *, actor_id: int) -> UploadOutcome:
    outcome = UploadOutcome()
    reader = csv.DictReader(io.StringIO(text))
    headers = _resolve_headers(reader.fieldnames)

    missing = {"room", "date", "slots_used"} - set(headers)
    if missing:
        outcome.rejected.append((0, "The file needs at least room, date, and slots used columns."))
        return outcome

    rooms = {room.name.strip().lower(): room for room in db.execute(select(Room)).scalars().all()}
    existing = {(u.room_id, u.usage_date): u for u in db.execute(select(RoomUsage)).scalars().all()}

    for line_number, record in enumerate(reader, start=2):
        if line_number - 1 > MAX_UPLOAD_ROWS:
            outcome.rejected.append((line_number, f"Stopped after {MAX_UPLOAD_ROWS} rows."))
            break

        room_name = clean_text(record.get(headers["room"]))
        if not room_name:
            continue

        outcome.rows_read += 1

        room = rooms.get(room_name.lower())
        if room is None:
            outcome.rejected.append((line_number, f"No room called {room_name!r}. Add it first."))
            continue

        try:
            usage_date = parse_date(record.get(headers["date"]))
        except ParseError:
            outcome.rejected.append((line_number, "Could not read the date."))
            continue
        if usage_date is None:
            outcome.rejected.append((line_number, "No date."))
            continue

        used = _as_int(record.get(headers["slots_used"]))
        if used is None:
            outcome.rejected.append((line_number, "Slots used is not a whole number."))
            continue

        if "slots_available" in headers:
            available = _as_int(record.get(headers["slots_available"]))
            if available is None:
                available = room.default_slots_per_day
        else:
            available = room.default_slots_per_day

        if available > 0 and used > available:
            outcome.rejected.append(
                (line_number, f"Used ({used}) is more than available ({available}).")
            )
            continue

        note = clean_text(record.get(headers.get("note", ""), "")) or None

        current = existing.get((room.id, usage_date))
        if current is None:
            entry = RoomUsage(
                room_id=room.id,
                usage_date=usage_date,
                slots_used=used,
                slots_available=available,
                source=UsageSource.MANUAL_UPLOAD,
                note=note,
                recorded_by_id=actor_id,
            )
            db.add(entry)
            existing[(room.id, usage_date)] = entry
            outcome.inserted += 1
        else:
            current.slots_used = used
            current.slots_available = available
            current.note = note
            current.recorded_by_id = actor_id
            outcome.updated += 1

    return outcome


def _as_int(value: object) -> int | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        parsed = int(float(text))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
