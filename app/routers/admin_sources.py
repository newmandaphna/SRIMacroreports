"""Admin: the Data Sources registry, sync, and import error review.

Quarterly rotation is meant to be three steps: paste next quarter's URL, pick the tab,
activate. Deactivating the old source leaves its rows in place, because the database
is the system of record and the sheets are only ingestion.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Form, Query, Request
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
from app.sync.engine import run_sync, suggest_mapping
from app.sync.sheets import (
    SheetsError,
    client_for,
    extract_spreadsheet_id,
    selectable_tabs,
)
from app.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/sources", tags=["admin"])


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
    return {
        "sources": sources,
        "visit_counts": visit_counts,
        "open_errors": open_errors,
        "total_visits": sum(visit_counts.values()),
    }


@router.get("", response_class=HTMLResponse)
async def list_sources(request: Request, db: DbSession, auth: AdminUser) -> Response:
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
async def create_source(
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

    if not label:
        error = "Give the source a label, for example Q3 2026."
    elif db.execute(select(DataSource).where(DataSource.label == label)).scalar_one_or_none():
        error = f"A source labelled {label!r} already exists."
    elif provider == SourceProvider.GOOGLE_SHEETS:
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

    source = DataSource(
        label=label,
        provider=SourceProvider(provider),
        spreadsheet_id=spreadsheet_id,
        spreadsheet_url=spreadsheet_url.strip() or None,
        tab_name=DEMO_TAB_NAME if provider == SourceProvider.DEMO else None,
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
async def create_demo_source(request: Request, db: DbSession, auth: AdminUser) -> Response:
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
async def source_detail(
    request: Request, db: DbSession, auth: AdminUser, source_id: int
) -> Response:
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

    try:
        client = _client(request, source)
        if source.spreadsheet_id or source.provider is SourceProvider.DEMO:
            tabs = selectable_tabs(client.list_tabs(source.spreadsheet_id or ""))
        if source.tab_name and source.tab_name in tabs:
            headers = client.read_tab(
                source.spreadsheet_id or "", source.tab_name, source.header_row
            ).headers
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
            "suggested": suggest_mapping(headers) if headers else {},
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
    return RedirectResponse(f"/admin/sources/{source.id}", status_code=303)


@router.post("/{source_id}/sync")
async def sync_source(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    source_id: int,
    mode: Annotated[str, Form()] = "dry_run",
) -> Response:
    source = db.get(DataSource, source_id)
    if source is None:
        return RedirectResponse("/admin/sources", status_code=303)

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
            "unmapped_columns": result.unmapped_columns,
            "error": result.error_message,
        },
    )

    return RedirectResponse(f"/admin/sources/{source.id}/runs/{result.run_id}", status_code=303)


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
            params["notice"] = "No new aliases to import — all were already up to date."
        else:
            parts = []
            if result.created_aliases:
                n = result.created_aliases
                parts.append(f"{n} alias{'es' if n != 1 else ''} created.")
            if not result.created_aliases and not result.unmatched:
                parts.append("All aliases already up to date.")
            if result.conflicts:
                n = len(result.conflicts)
                parts.append(f"{n} skipped — already point at a different therapist.")
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
async def run_detail(
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

    rejections = (
        db.execute(
            select(ImportErrorRow)
            .where(ImportErrorRow.sync_run_id == run.id)
            .order_by(ImportErrorRow.id)
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
            detail={"view": "import_errors", "rows": len(rejections)},
        )

    # Unique unrecognized therapist names, ordered by frequency so the most
    # impactful ones are at the top of the callout.
    unknown_therapists: list[tuple[str, int]] = []
    _unknown_counts: dict[str, int] = {}
    for r in rejections:
        if r.reason.value == RejectReason.UNKNOWN_THERAPIST.value and r.raw_value:
            _unknown_counts[r.raw_value] = _unknown_counts.get(r.raw_value, 0) + 1
    unknown_therapists = sorted(_unknown_counts.items(), key=lambda t: -t[1])

    return render(
        request,
        "admin/sync_run.html",
        {
            "page_title": f"Sync run {run.id}",
            "auth": auth,
            "source": source,
            "run": run,
            "rejections": rejections,
            "reasons": {r.value: r.label for r in RejectReason},
            "unknown_therapists": unknown_therapists,
        },
    )


@router.post("/{source_id}/errors/{error_id}/resolve")
async def resolve_error(
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
async def import_lookups(
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
async def source_errors(
    request: Request,
    db: DbSession,
    auth: AdminUser,
    source_id: int,
    show: str = Query(default="open"),
) -> Response:
    source = db.get(DataSource, source_id)
    if source is None:
        return RedirectResponse("/admin/sources", status_code=303)

    stmt = select(ImportErrorRow).where(ImportErrorRow.source_id == source.id)
    if show == "open":
        stmt = stmt.where(ImportErrorRow.resolved_at.is_(None))
    rejections = db.execute(stmt.order_by(ImportErrorRow.id.desc()).limit(500)).scalars().all()

    if rejections:
        audit.record(
            db,
            action=AuditAction.PHI_VIEW,
            actor=auth.user,
            target_type="data_source",
            target_id=source.id,
            request=request,
            detail={"view": "import_errors", "show": show, "rows": len(rejections)},
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
        },
    )
