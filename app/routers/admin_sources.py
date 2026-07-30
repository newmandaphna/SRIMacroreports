"""Admin: the Data Sources registry, sync, and import error review.

Quarterly rotation is meant to be three steps: paste next quarter's URL, pick the tab,
activate. Deactivating the old source leaves its rows in place, because the database
is the system of record and the sheets are only ingestion.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select

from app.models.data_source import (
    IMPORT_ALLOWLIST,
    DataSource,
    RejectReason,
    SourceProvider,
    SyncRun,
)
from app.models.data_source import (
    ImportError as ImportErrorRow,
)
from app.models.enums import AuditAction, AuditResult
from app.models.visit import Visit
from app.security import audit
from app.security.deps import AdminUser, DbSession
from app.sync import lookups as lookups_import
from app.sync.demo_data import (
    DEMO_ABBREVIATIONS_TAB,
    DEMO_CONFIG_TAB,
    DEMO_TAB_NAME,
    DEMO_THERAPISTS,
)
from app.sync.engine import AliasResolver, run_sync, suggest_mapping
from app.sync.sheets import (
    SheetsError,
    client_for,
    extract_spreadsheet_id,
    selectable_tabs,
)
from app.sync.upload import CSV_TAB_NAME, MAX_UPLOAD_BYTES, UploadedWorkbookClient
from app.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/sources", tags=["admin"])

# Rejection tables render at most this many rows per page. A real sheet can reject
# thousands of rows at once, and a page that tries to show all of them is unreadable,
# heavy with patient hints, and slow to build. The counts always describe the whole
# set; only the row detail is paged.
REJECTION_PAGE_SIZE = 200


def _client(request: Request, source: DataSource):
    settings = request.app.state.settings
    return client_for(source.provider, settings.google_service_account_json)


def _sources_context(db: DbSession) -> dict:
    sources = db.execute(select(DataSource).order_by(DataSource.label.desc())).scalars().all()
    visit_counts = dict(
        db.execute(select(Visit.source_id, func.count(Visit.id)).group_by(Visit.source_id)).all()
    )
    open_errors = dict(
        db.execute(
            select(ImportErrorRow.source_id, func.count(ImportErrorRow.id))
            .where(ImportErrorRow.resolved_at.is_(None))
            .group_by(ImportErrorRow.source_id)
        ).all()
    )

    # Which sources had their most recent live run triggered by auto-sync?
    # Used to show a small "auto" badge on the sources list.
    from sqlalchemy import and_

    latest_run_subq = (
        select(SyncRun.source_id, func.max(SyncRun.id).label("max_id"))
        .group_by(SyncRun.source_id)
        .subquery()
    )
    auto_source_ids: set[int] = set(
        db.execute(
            select(SyncRun.source_id)
            .join(
                latest_run_subq,
                and_(
                    SyncRun.source_id == latest_run_subq.c.source_id,
                    SyncRun.id == latest_run_subq.c.max_id,
                ),
            )
            .where(SyncRun.run_by_id.is_(None))
        )
        .scalars()
        .all()
    )

    return {
        "sources": sources,
        "visit_counts": visit_counts,
        "open_errors": open_errors,
        "total_visits": sum(visit_counts.values()),
        "auto_source_ids": auto_source_ids,
    }


@router.get("", response_class=HTMLResponse)
def list_sources(request: Request, db: DbSession, auth: AdminUser) -> Response:
    recent_runs = (
        db.execute(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(10)).scalars().all()
    )
    return render(
        request,
        "admin/sources.html",
        {
            "page_title": "Data sources",
            "auth": auth,
            "recent_runs": recent_runs,
            "providers": list(SourceProvider),
            **_sources_context(db),
        },
    )


@router.post("/new")
def create_source(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    label: Annotated[str, Form()],
    provider: Annotated[str, Form()] = SourceProvider.GOOGLE_SHEETS.value,
    spreadsheet_url: Annotated[str, Form()] = "",
) -> Response:
    label = label.strip()
    error: str | None = None
    spreadsheet_id: str | None = None

    try:
        chosen_provider = SourceProvider(provider)
    except ValueError:
        chosen_provider = SourceProvider.GOOGLE_SHEETS
        error = f"Unknown provider {provider!r}."

    if error:
        pass
    elif not label:
        error = "Give the source a label, for example Q3 2026."
    elif db.execute(select(DataSource).where(DataSource.label == label)).scalar_one_or_none():
        error = f"A source labelled {label!r} already exists."
    elif chosen_provider is SourceProvider.GOOGLE_SHEETS:
        try:
            spreadsheet_id = extract_spreadsheet_id(spreadsheet_url)
        except SheetsError as exc:
            error = str(exc)

    if error:
        return render(
            request,
            "admin/sources.html",
            {
                "page_title": "Data sources",
                "auth": auth,
                "error": error,
                "recent_runs": [],
                "providers": list(SourceProvider),
                **_sources_context(db),
            },
            status_code=400,
        )

    # Prefill the mapping from the most recent source, so adding a quarter is a
    # confirmation rather than eighteen retyped header names.
    previous = db.execute(
        select(DataSource).order_by(DataSource.created_at.desc()).limit(1)
    ).scalar_one_or_none()

    # An upload source defaults its tab to the CSV pseudo-tab, which is correct for
    # any CSV upload; an Excel upload whose tabs disagree gets a message naming them.
    default_tab = None
    if chosen_provider is SourceProvider.DEMO:
        default_tab = DEMO_TAB_NAME
    elif chosen_provider is SourceProvider.UPLOAD:
        default_tab = CSV_TAB_NAME

    source = DataSource(
        label=label,
        provider=chosen_provider,
        spreadsheet_id=spreadsheet_id,
        spreadsheet_url=spreadsheet_url.strip() or None,
        tab_name=default_tab,
        column_mapping=dict(previous.column_mapping) if previous else {},
        active=True,
        created_by_id=auth.user.id,
    )
    db.add(source)
    db.flush()

    audit.record(
        db,
        action=AuditAction.DATA_SOURCE_CHANGED,
        actor=auth.user,
        target_type="data_source",
        target_id=source.id,
        request=request,
        detail={"created": label, "provider": provider},
    )
    return RedirectResponse(f"/admin/sources/{source.id}", status_code=303)


# Registered before the "/{source_id}" routes: FastAPI matches in registration
# order, so a literal path declared after a parameterized one is shadowed by it.
@router.post("/demo")
def create_demo_source(request: Request, db: DbSession, auth: AdminUser) -> Response:
    """One click: a synthetic source with its therapists, ready to sync.

    Exists so the whole import path can be demonstrated before any Google credential
    is in place. Every patient in it is obviously fake.
    """
    from app.models.therapist import AliasSource, EmploymentType, Therapist, TherapistAlias

    existing = db.execute(
        select(DataSource).where(DataSource.provider == SourceProvider.DEMO)
    ).scalar_one_or_none()
    if existing is not None:
        return RedirectResponse(f"/admin/sources/{existing.id}", status_code=303)

    for display_name, aliases in DEMO_THERAPISTS:
        therapist = db.execute(
            select(Therapist).where(Therapist.display_name == display_name)
        ).scalar_one_or_none()
        if therapist is None:
            therapist = Therapist(
                display_name=display_name,
                employment_type=EmploymentType.SALARIED_BENEFITS,
                notes="Created with the demo data source. Synthetic, not a real person.",
            )
            db.add(therapist)
            db.flush()
        for alias in aliases:
            if not db.execute(
                select(TherapistAlias).where(TherapistAlias.alias == alias)
            ).scalar_one_or_none():
                db.add(
                    TherapistAlias(
                        therapist_id=therapist.id,
                        alias=alias,
                        source=AliasSource.MANUAL,
                        created_by_id=auth.user.id,
                    )
                )

    from app.sync.demo_data import Q2_HEADERS

    headers = [str(h) if h is not None else "" for h in Q2_HEADERS]
    source = DataSource(
        label="Demo (synthetic)",
        provider=SourceProvider.DEMO,
        tab_name=DEMO_TAB_NAME,
        header_row=1,
        column_mapping=suggest_mapping(headers),
        active=True,
        created_by_id=auth.user.id,
    )
    db.add(source)
    db.flush()

    audit.record(
        db,
        action=AuditAction.DATA_SOURCE_CHANGED,
        actor=auth.user,
        target_type="data_source",
        target_id=source.id,
        request=request,
        detail={"created": "demo source with synthetic therapists"},
    )
    return RedirectResponse(f"/admin/sources/{source.id}", status_code=303)


@router.get("/{source_id}", response_class=HTMLResponse)
def source_detail(request: Request, db: DbSession, auth: AdminUser, source_id: int) -> Response:
    source = db.get(DataSource, source_id)
    if source is None:
        return render(
            request,
            "errors/not_found.html",
            {"page_title": "Not found", "auth": auth},
            status_code=404,
        )

    tabs: list[str] = []
    tab_error: str | None = None
    headers: list[str] = []

    # An upload source has no live workbook to read tabs or headers from; its tab
    # and mapping are typed in, usually prefilled from the previous source.
    if source.provider is not SourceProvider.UPLOAD:
        try:
            client = _client(request, source)
            if source.spreadsheet_id or source.provider is SourceProvider.DEMO:
                tabs = selectable_tabs(client.list_tabs(source.spreadsheet_id or ""))
            if source.tab_name and source.tab_name in tabs:
                # Headers only. This page renders the mapping dropdowns and needs
                # nothing but the column names, and it used to read the whole tab to
                # get them: a quarter of patient data over the network, and held in
                # memory, on every view of an admin page.
                headers = client.read_headers(
                    source.spreadsheet_id or "", source.tab_name, source.header_row
                )
        except SheetsError as exc:
            tab_error = str(exc)

    runs = (
        db.execute(
            select(SyncRun)
            .where(SyncRun.source_id == source.id)
            .order_by(SyncRun.started_at.desc())
            .limit(10)
        )
        .scalars()
        .all()
    )

    open_error_count = db.execute(
        select(func.count(ImportErrorRow.id)).where(
            ImportErrorRow.source_id == source.id, ImportErrorRow.resolved_at.is_(None)
        )
    ).scalar_one()

    suggested = suggest_mapping(headers) if headers else {}

    # The mapping dropdowns fall back to `suggested` when nothing is saved, so a source
    # with an empty mapping renders as though it were fully configured while the sync
    # panel, which reads only what is stored, correctly refuses to run. The page
    # contradicted itself and gave no hint that the missing step was pressing Save.
    # This flag lets both halves say the same thing.
    unsaved_suggestion = bool(
        source.missing_required_fields and not (source.missing_required_fields - set(suggested))
    )

    return render(
        request,
        "admin/source_detail.html",
        {
            "page_title": source.label,
            "auth": auth,
            "source": source,
            "tabs": tabs,
            "tab_error": tab_error,
            "headers": [h for h in headers if h.strip()],
            "allowlist": IMPORT_ALLOWLIST,
            "suggested": suggested,
            "unsaved_suggestion": unsaved_suggestion,
            "runs": runs,
            "open_error_count": open_error_count,
            "visit_count": db.execute(
                select(func.count(Visit.id)).where(Visit.source_id == source.id)
            ).scalar_one(),
            "demo_abbrev_tab": DEMO_ABBREVIATIONS_TAB,
            "demo_config_tab": DEMO_CONFIG_TAB,
        },
    )


@router.post("/{source_id}")
async def update_source(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    source_id: int,
    label: Annotated[str, Form()],
    tab_name: Annotated[str, Form()] = "",
    header_row: Annotated[int, Form()] = 1,
    active: Annotated[str, Form()] = "",
) -> Response:
    source = db.get(DataSource, source_id)
    if source is None:
        return RedirectResponse("/admin/sources", status_code=303)

    form = await request.form()

    mapping: dict[str, str] = {}
    for canonical_field in IMPORT_ALLOWLIST:
        chosen = str(form.get(f"map__{canonical_field}", "")).strip()
        if chosen:
            mapping[canonical_field] = chosen

    source.label = label.strip() or source.label
    source.tab_name = tab_name.strip() or None
    source.header_row = max(1, header_row)
    source.column_mapping = mapping
    source.active = bool(active)

    audit.record(
        db,
        action=AuditAction.DATA_SOURCE_CHANGED,
        actor=auth.user,
        target_type="data_source",
        target_id=source.id,
        request=request,
        detail={
            "tab_name": source.tab_name,
            "header_row": source.header_row,
            "mapped_fields": sorted(mapping),
            "active": source.active,
        },
    )
    from urllib.parse import quote

    saved_note = quote(
        f"Saved. Reading tab {source.tab_name!r} from header row {source.header_row}."
        if source.tab_name
        else "Saved. No tab chosen yet."
    )
    return RedirectResponse(f"/admin/sources/{source.id}?notice={saved_note}", status_code=303)


@router.post("/{source_id}/delete")
def delete_source(request: Request, db: DbSession, auth: AdminUser, source_id: int) -> Response:
    """Remove a source that was created by mistake or is no longer wanted.

    Real session rows are the system of record and are never deleted from here: a
    source that holds any refuses to go, and the answer for a finished quarter is to
    deactivate it. The demo source is the one exception, because every row it holds
    is synthetic by construction.
    """
    source = db.get(DataSource, source_id)
    if source is None:
        return RedirectResponse("/admin/sources", status_code=303)

    visit_count = db.execute(
        select(func.count(Visit.id)).where(Visit.source_id == source.id)
    ).scalar_one()

    if visit_count and source.provider is not SourceProvider.DEMO:
        return _detail_redirect_with_flash(
            source.id,
            f"Not deleted: this source holds {visit_count} imported session rows. "
            "Deactivate it instead; imported rows are never deleted from here.",
        )

    # A source with no visits can still be carrying the whole account of what went
    # wrong. The rejections cascade with it, so deleting a sheet whose every row was
    # refused, which is exactly the sheet an admin is most tempted to delete, silently
    # destroyed the queue explaining why. Nothing said so, and nothing brought it back.
    open_rejections = db.execute(
        select(func.count(ImportErrorRow.id)).where(
            ImportErrorRow.source_id == source.id,
            ImportErrorRow.resolved_at.is_(None),
        )
    ).scalar_one()

    if open_rejections and source.provider is not SourceProvider.DEMO:
        return _detail_redirect_with_flash(
            source.id,
            f"Not deleted: {open_rejections} rejected row(s) from this source are still "
            "unreviewed, and they would be deleted with it. Work through the review "
            "queue first, or deactivate the source and leave the queue intact.",
        )

    run_count = db.execute(
        select(func.count(SyncRun.id)).where(SyncRun.source_id == source.id)
    ).scalar_one()

    label = source.label
    audit.record(
        db,
        action=AuditAction.DATA_SOURCE_CHANGED,
        actor=auth.user,
        target_type="data_source",
        target_id=source.id,
        request=request,
        detail={
            "deleted": label,
            "provider": source.provider.value,
            "visits_deleted": visit_count,
            # What went with it. Both cascade, so the log is the only place they survive.
            "sync_runs_deleted": run_count,
            "resolved_rejections_deleted": db.execute(
                select(func.count(ImportErrorRow.id)).where(ImportErrorRow.source_id == source.id)
            ).scalar_one(),
        },
    )
    db.delete(source)

    from urllib.parse import quote

    return RedirectResponse(
        f"/admin/sources?notice={quote(f'Deleted source {label!r}.')}", status_code=303
    )


@router.post("/{source_id}/sync")
def sync_source(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    source_id: int,
    mode: Annotated[str, Form()] = "dry_run",
) -> Response:
    source = db.get(DataSource, source_id)
    if source is None:
        return RedirectResponse("/admin/sources", status_code=303)

    if not source.active:
        return _detail_redirect_with_flash(source.id, _INACTIVE_MESSAGE)

    dry_run = mode != "live"

    try:
        client = _client(request, source)
    except SheetsError as exc:
        audit.record(
            db,
            action=AuditAction.SYNC_RUN,
            result=AuditResult.FAILURE,
            actor=auth.user,
            target_type="data_source",
            target_id=source.id,
            request=request,
            detail={"error": str(exc)},
        )
        return _detail_redirect_with_flash(source.id, str(exc))

    result = run_sync(db, source, client, dry_run=dry_run, actor=auth.user)

    audit.record(
        db,
        action=AuditAction.SYNC_RUN,
        result=AuditResult.SUCCESS if result.ok else AuditResult.FAILURE,
        actor=auth.user,
        target_type="data_source",
        target_id=source.id,
        request=request,
        detail={
            "mode": result.mode.value,
            "rows_read": result.rows_read,
            "inserted": result.rows_inserted,
            "updated": result.rows_updated,
            "unchanged": result.rows_unchanged,
            "rejected": result.rows_rejected,
            "superseded": result.superseded_errors,
            "unmapped_columns": result.unmapped_columns,
            "error": result.error_message,
        },
    )

    return RedirectResponse(f"/admin/sources/{source.id}/runs/{result.run_id}", status_code=303)


@router.post("/{source_id}/upload")
async def upload_workbook(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    source_id: int,
    file: Annotated[UploadFile, File()],
    mode: Annotated[str, Form()] = "dry_run",
) -> Response:
    """Import an uploaded .xlsx or .csv through the ordinary sync pipeline.

    The file lives only in memory for the duration of this request. Everything
    downstream, allowlist, validation, alias resolution, rejection queue, audit,
    is exactly the sync path; only where the rows came from differs.
    """
    source = db.get(DataSource, source_id)
    if source is None:
        return RedirectResponse("/admin/sources", status_code=303)

    if not source.active:
        return _detail_redirect_with_flash(source.id, _INACTIVE_MESSAGE)

    if not source.is_ready_to_sync:
        return _detail_redirect_with_flash(
            source.id,
            "Map and save the required columns before uploading, so the file can be "
            "validated the same way a live sheet is.",
        )

    # Read one byte past the cap so an oversized file is detected without ever
    # holding an unbounded body in memory.
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    dry_run = mode != "live"

    try:
        client = UploadedWorkbookClient(file.filename or "", content)
    except SheetsError as exc:
        audit.record(
            db,
            action=AuditAction.SYNC_RUN,
            result=AuditResult.FAILURE,
            actor=auth.user,
            target_type="data_source",
            target_id=source.id,
            request=request,
            detail={"upload": file.filename, "error": str(exc)},
        )
        return _detail_redirect_with_flash(source.id, str(exc))

    result = run_sync(db, source, client, dry_run=dry_run, actor=auth.user)

    audit.record(
        db,
        action=AuditAction.SYNC_RUN,
        result=AuditResult.SUCCESS if result.ok else AuditResult.FAILURE,
        actor=auth.user,
        target_type="data_source",
        target_id=source.id,
        request=request,
        detail={
            "upload": file.filename,
            "mode": result.mode.value,
            "rows_read": result.rows_read,
            "inserted": result.rows_inserted,
            "updated": result.rows_updated,
            "unchanged": result.rows_unchanged,
            "rejected": result.rows_rejected,
            "superseded": result.superseded_errors,
            "unmapped_columns": result.unmapped_columns,
            "error": result.error_message,
        },
    )

    return RedirectResponse(f"/admin/sources/{source.id}/runs/{result.run_id}", status_code=303)


# Auto sync has always skipped inactive sources. The manual buttons did not, so
# switching a source off stopped the schedule and nothing else: a retired quarter's
# sheet could still be imported by hand, and an older sheet re-importing over a newer
# one is exactly how a figure moves without an explanation.
_INACTIVE_MESSAGE = (
    "This source is switched off, so it cannot be imported from. Auto sync already "
    "skips it. Tick Active and save if you want to import from it again."
)


def _detail_redirect_with_flash(source_id: int, message: str) -> Response:
    from urllib.parse import quote

    return RedirectResponse(f"/admin/sources/{source_id}?problem={quote(message)}", status_code=303)


def _lookup_redirect(
    source_id: int,
    kind: str,
    result: lookups_import.AliasImportResult | lookups_import.LookupImportResult,
) -> Response:
    from urllib.parse import urlencode

    from app.sync.lookups import AliasImportResult, LookupImportResult

    params: dict[str, str] = {}

    if isinstance(result, AliasImportResult):
        if result.created_aliases == 0 and not result.unmatched and not result.conflicts:
            params["notice"] = "No new aliases to import: all were already up to date."
        else:
            parts = []
            if result.created_aliases:
                n = result.created_aliases
                parts.append(f"{n} alias{'es' if n != 1 else ''} created.")
            if not result.created_aliases and not result.unmatched:
                parts.append("All aliases already up to date.")
            if result.conflicts:
                n = len(result.conflicts)
                parts.append(f"{n} skipped: already point at a different therapist.")
            params["notice"] = " ".join(parts) if parts else "Import complete."
        if result.unmatched:
            # Pipe-separated so commas inside names survive round-trip.
            params["unmatched"] = "|".join(result.unmatched)
    elif isinstance(result, LookupImportResult):
        if result.imported == 0:
            params["notice"] = "No abbreviations found to import."
        else:
            params["notice"] = (
                f"{result.imported} abbreviation{'s' if result.imported != 1 else ''} imported"
                + (
                    " (" + ", ".join(f"{v} {k}" for k, v in sorted(result.by_kind.items())) + ")."
                    if result.by_kind
                    else "."
                )
            )

    qs = urlencode(params) if params else ""
    url = f"/admin/sources/{source_id}?{qs}" if qs else f"/admin/sources/{source_id}"
    return RedirectResponse(url, status_code=303)


@router.get("/{source_id}/runs/{run_id}", response_class=HTMLResponse)
def run_detail(
    request: Request, db: DbSession, auth: AdminUser, source_id: int, run_id: int
) -> Response:
    run = db.get(SyncRun, run_id)
    source = db.get(DataSource, source_id)
    if run is None or source is None or run.source_id != source.id:
        return render(
            request,
            "errors/not_found.html",
            {"page_title": "Not found", "auth": auth},
            status_code=404,
        )

    # Aggregates first, in SQL: a run against the real sheet can reject thousands of
    # rows, and 6,797 table rows in one page is unreadable and unrenderable. The
    # breakdown carries the diagnosis; the table below it is a capped sample.
    reason_rows = db.execute(
        select(
            ImportErrorRow.reason,
            func.max(ImportErrorRow.field),
            func.count(ImportErrorRow.id),
        )
        .where(ImportErrorRow.sync_run_id == run.id)
        .group_by(ImportErrorRow.reason)
        .order_by(func.count(ImportErrorRow.id).desc())
    ).all()
    reason_counts = [
        (RejectReason(reason).label, field_name, n) for reason, field_name, n in reason_rows
    ]
    total_rejections = sum(n for _, _, n in reason_counts)

    # When one reason accounts for nearly every row, the problem is the sheet or the
    # mapping, not the rows, and saying so beats presenting 6,766 identical errors as
    # though each deserved individual review.
    systemic = None
    if reason_rows and run.rows_read:
        top_reason, top_field, top_count = reason_rows[0]
        if top_count >= 50 and top_count >= 0.9 * run.rows_read:
            systemic = {
                "label": RejectReason(top_reason).label,
                "field": top_field,
                "count": top_count,
                "total_read": run.rows_read,
                "is_missing_dos": RejectReason(top_reason) is RejectReason.MISSING_DOS,
                "is_unknown_therapist": RejectReason(top_reason) is RejectReason.UNKNOWN_THERAPIST,
            }

    rejections = (
        db.execute(
            select(ImportErrorRow)
            .where(ImportErrorRow.sync_run_id == run.id)
            .order_by(ImportErrorRow.id)
            .limit(REJECTION_PAGE_SIZE)
        )
        .scalars()
        .all()
    )

    # Rejected rows can carry patient identity, because the thing that failed to parse
    # is usually the patient's own row. Every view of them is logged as a PHI view.
    if rejections:
        audit.record(
            db,
            action=AuditAction.PHI_VIEW,
            actor=auth.user,
            target_type="sync_run",
            target_id=run.id,
            request=request,
            detail={"view": "import_errors", "rows": len(rejections), "total": total_rejections},
        )

    # Unique unrecognized therapist names, ordered by frequency so the most
    # impactful ones are at the top of the callout. Computed over the whole run, not
    # the displayed sample.
    unknown_therapists = [
        (name, n)
        for name, n in db.execute(
            select(ImportErrorRow.raw_value, func.count(ImportErrorRow.id))
            .where(
                ImportErrorRow.sync_run_id == run.id,
                ImportErrorRow.reason == RejectReason.UNKNOWN_THERAPIST,
                ImportErrorRow.raw_value.is_not(None),
            )
            .group_by(ImportErrorRow.raw_value)
            .order_by(func.count(ImportErrorRow.id).desc())
        ).all()
    ]

    # Names carried by rows that were rejected before the therapist was ever checked.
    # The date check short circuits first, so a sheet wide date failure hides a second
    # wave: none of those rows has been tested against the roster yet, and fixing the
    # dates alone converts them into unknown therapist rejections on the next sync.
    # Naming the gap now lets the roster be built in parallel, so the next sync
    # imports in one pass instead of failing a second way.
    unchecked_therapists: list[tuple[str, int]] = []
    pending_names = db.execute(
        select(ImportErrorRow.therapist_hint, func.count(ImportErrorRow.id))
        .where(
            ImportErrorRow.sync_run_id == run.id,
            ImportErrorRow.reason != RejectReason.UNKNOWN_THERAPIST,
            ImportErrorRow.therapist_hint.is_not(None),
        )
        .group_by(ImportErrorRow.therapist_hint)
        .order_by(func.count(ImportErrorRow.id).desc())
    ).all()
    if pending_names:
        resolver = AliasResolver(db)
        unchecked_therapists = [
            (name, n) for name, n in pending_names if resolver.resolve(name) is None
        ]

    return render(
        request,
        "admin/sync_run.html",
        {
            "page_title": f"Sync run {run.id}",
            "auth": auth,
            "source": source,
            "run": run,
            "rejections": rejections,
            "total_rejections": total_rejections,
            "reason_counts": reason_counts,
            "systemic": systemic,
            "reasons": {r.value: r.label for r in RejectReason},
            "unknown_therapists": unknown_therapists,
            "unchecked_therapists": unchecked_therapists,
        },
    )


@router.post("/{source_id}/errors/{error_id}/resolve")
def resolve_error(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    source_id: int,
    error_id: int,
    note: Annotated[str, Form()] = "",
) -> Response:
    entry = db.get(ImportErrorRow, error_id)
    if entry is not None and entry.resolved_at is None:
        from app.models.types import utcnow

        entry.resolved_at = utcnow()
        entry.resolved_by_id = auth.user.id
        entry.resolution_note = note.strip() or None
        audit.record(
            db,
            action=AuditAction.MANUAL_EDIT,
            actor=auth.user,
            target_type="import_error",
            target_id=entry.id,
            request=request,
            detail={"resolved": True},
        )
    run_id = entry.sync_run_id if entry else None
    return RedirectResponse(
        f"/admin/sources/{source_id}/runs/{run_id}" if run_id else "/admin/sources",
        status_code=303,
    )


@router.post("/{source_id}/lookups")
def import_lookups(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    source_id: int,
    tab_name: Annotated[str, Form()],
    kind: Annotated[str, Form()] = "abbreviations",
) -> Response:
    source = db.get(DataSource, source_id)
    if source is None:
        return RedirectResponse("/admin/sources", status_code=303)

    try:
        client = _client(request, source)
        if kind == "config":
            result = lookups_import.import_provider_aliases(
                db, source, client, tab_name, actor_id=auth.user.id
            )
            detail = {
                "tab": tab_name,
                "aliases_created": result.created_aliases,
                "unmatched": result.unmatched,
                "conflicts": result.conflicts,
            }
        else:
            result = lookups_import.import_abbreviations(db, source, client, tab_name)
            detail = {
                "tab": tab_name,
                "imported": result.imported,
                "skipped": result.skipped,
                "by_kind": result.by_kind,
            }
    except SheetsError as exc:
        audit.record(
            db,
            action=AuditAction.DATA_SOURCE_CHANGED,
            result=AuditResult.FAILURE,
            actor=auth.user,
            target_type="data_source",
            target_id=source.id,
            request=request,
            detail={"lookup_import_failed": str(exc)},
        )
        return _detail_redirect_with_flash(source.id, str(exc))

    audit.record(
        db,
        action=AuditAction.DATA_SOURCE_CHANGED,
        actor=auth.user,
        target_type="data_source",
        target_id=source.id,
        request=request,
        detail={"lookup_import": kind, **detail},
    )
    return _lookup_redirect(source.id, kind, result)


@router.get("/{source_id}/errors", response_class=HTMLResponse)
def source_errors(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    source_id: int,
    show: str = Query(default="open"),
    page: int = Query(default=1, ge=1),
) -> Response:
    source = db.get(DataSource, source_id)
    if source is None:
        return RedirectResponse("/admin/sources", status_code=303)

    conditions = [ImportErrorRow.source_id == source.id]
    if show == "open":
        conditions.append(ImportErrorRow.resolved_at.is_(None))

    # Counts describe the whole set; the table below is one page of it. The page used
    # to stop silently at 500 rows while calling itself the full account, which at
    # 6,766 rejections meant most of the account was invisible with no sign it existed.
    total = db.execute(select(func.count(ImportErrorRow.id)).where(*conditions)).scalar_one()
    reason_counts = [
        (RejectReason(reason).label, n)
        for reason, n in db.execute(
            select(ImportErrorRow.reason, func.count(ImportErrorRow.id))
            .where(*conditions)
            .group_by(ImportErrorRow.reason)
            .order_by(func.count(ImportErrorRow.id).desc())
        ).all()
    ]

    last_page = max(1, -(-total // REJECTION_PAGE_SIZE))  # ceiling division
    page = min(page, last_page)
    rejections = (
        db.execute(
            select(ImportErrorRow)
            .where(*conditions)
            .order_by(ImportErrorRow.id.desc())
            .offset((page - 1) * REJECTION_PAGE_SIZE)
            .limit(REJECTION_PAGE_SIZE)
        )
        .scalars()
        .all()
    )

    if rejections:
        audit.record(
            db,
            action=AuditAction.PHI_VIEW,
            actor=auth.user,
            target_type="data_source",
            target_id=source.id,
            request=request,
            detail={
                "view": "import_errors",
                "show": show,
                "page": page,
                "rows": len(rejections),
                "total": total,
            },
        )

    return render(
        request,
        "admin/import_errors.html",
        {
            "page_title": f"Import errors, {source.label}",
            "auth": auth,
            "source": source,
            "rejections": rejections,
            "show": show,
            "page": page,
            "last_page": last_page,
            "total": total,
            "page_size": REJECTION_PAGE_SIZE,
            "reason_counts": reason_counts,
        },
    )
