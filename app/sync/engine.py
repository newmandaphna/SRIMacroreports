"""The import engine.

One pass over a source tab: map, normalize, validate, resolve the therapist, upsert.
Idempotent through the identity key, so running it twice changes nothing the second
time.

Dry run does everything except write session rows: same read, same mapping, same
validation, same rejection reasons, same counts, no visit inserted or changed. That is
what makes it useful before a quarter rotation rather than a checkbox.

A dry run does still record its own SyncRun and its rejections, because a preview
whose findings vanish when you navigate away is not a preview you can act on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.data_source import (
    IMPORT_ALLOWLIST,
    MONEY_FIELDS,
    REQUIRED_FIELDS,
    DataSource,
    ErrorKind,
    RejectReason,
    SyncMode,
    SyncRun,
    SyncStatus,
)
from app.models.data_source import (
    ImportError as ImportErrorRow,
)
from app.models.therapist import Therapist, TherapistAlias, normalize_therapist_name
from app.models.types import utcnow
from app.models.user import User
from app.models.visit import Visit
from app.sync import normalize
from app.sync.sheets import SheetData, SheetsClient, SheetsError, assert_tab_allowed

logger = logging.getLogger(__name__)

# Reconciliation only warns when the span already held this much collected money and
# the swing is at least this share of it. Generous on purpose: a first import, a
# catch up entry, or a remittance batch must not cry wolf, or the warning becomes
# wallpaper and takes the credible warnings down with it.
RECONCILE_MIN_BASE = Decimal("1000.00")
RECONCILE_WARN_RATIO = Decimal("0.20")

# Enough movers to see what happened, few enough to read.
MOVERS_SHOWN = 8


@dataclass
class Rejection:
    row_ref: str
    reason: RejectReason
    field: str | None = None
    raw_value: str | None = None
    detail: str | None = None
    patient_hint: str | None = None
    therapist_hint: str | None = None


@dataclass
class SyncResult:
    run_id: int | None
    mode: SyncMode
    rows_read: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_unchanged: int = 0
    rejections: list[Rejection] = field(default_factory=list)
    unmapped_columns: list[str] = field(default_factory=list)
    # Things a successful run still has to say, loudly: a mapped descriptive column
    # missing from the sheet, imported around rather than refused over.
    warnings: list[str] = field(default_factory=list)
    date_min: date | None = None
    date_max: date | None = None
    error_message: str | None = None
    # The machine readable half of the failure, mirrored onto the SyncRun.
    error_kind: ErrorKind | None = None
    error_detail: dict | None = None
    # Money movements observed while updating existing rows, for the run page's
    # biggest movers table. Row references and codes only, never patient identity.
    movers: list[dict] = field(default_factory=list)
    # What this live run changed against what its span already held. See A-100.
    reconciliation: dict | None = None
    # Unresolved errors from earlier runs of this source that this run replaced.
    superseded_errors: int = 0

    @property
    def rows_rejected(self) -> int:
        return len(self.rejections)

    @property
    def rows_upserted(self) -> int:
        return self.rows_inserted + self.rows_updated

    @property
    def ok(self) -> bool:
        return self.error_message is None


class AliasResolver:
    """Resolves a raw therapist string to a therapist, or reports that it cannot.

    Exact match on the whole normalized name only. Never substring, never fuzzy, and
    never automatic creation: a wrong merge is invisible once it has happened and
    corrupts the utilization figures of two people at once. See ASSUMPTIONS.md A-041
    and A-040a.
    """

    def __init__(self, db: Session) -> None:
        rows = db.execute(select(TherapistAlias.alias, TherapistAlias.therapist_id)).all()
        self._by_alias: dict[str, int] = {alias: tid for alias, tid in rows}
        # Display names resolve too, so a therapist created by hand works immediately
        # without an admin also having to add an alias identical to the name.
        for name, tid in db.execute(select(Therapist.display_name, Therapist.id)).all():
            self._by_alias.setdefault(normalize_therapist_name(name), tid)

    def resolve(self, raw: object) -> int | None:
        key = normalize_therapist_name(normalize.clean_text(raw))
        return self._by_alias.get(key) if key else None

    def suggest(self, raw: object, limit: int = 3) -> list[str]:
        """Ranked near matches, for the admin review queue. Suggestions only."""
        import difflib

        key = normalize_therapist_name(normalize.clean_text(raw))
        if not key:
            return []
        return difflib.get_close_matches(key, list(self._by_alias), n=limit, cutoff=0.6)


def build_column_index(
    headers: list[str], mapping: dict[str, str]
) -> tuple[dict[str, int], list[str]]:
    """Map canonical field -> column position, and list unclaimed headers.

    Enforces the allowlist here, at import, and not only in the mapping UI: a mapping
    naming a column outside the allowlist is ignored rather than honoured, so a hand
    crafted mapping cannot smuggle in a date of birth (SECURITY.md 6.1).
    """
    positions: dict[str, int] = {}
    claimed: set[int] = set()

    normalized_headers = [h.strip().lower() for h in headers]

    for canonical_field, header_text in mapping.items():
        if canonical_field not in IMPORT_ALLOWLIST:
            logger.warning(
                "Ignoring mapping for %r, which is not on the import allowlist",
                canonical_field,
            )
            continue
        if not header_text:
            continue
        try:
            index = normalized_headers.index(header_text.strip().lower())
        except ValueError:
            continue
        positions[canonical_field] = index
        claimed.add(index)

    unmapped = [headers[i] for i in range(len(headers)) if i not in claimed and headers[i].strip()]
    return positions, unmapped


def suggest_mapping(headers: list[str]) -> dict[str, str]:
    """Best guess mapping for a new source, matched on header text.

    Prefills the form so an admin adding next quarter confirms rather than retypes.
    """
    by_lower = {h.strip().lower(): h for h in headers if h and h.strip()}
    suggestion: dict[str, str] = {}
    for canonical_field, expected in IMPORT_ALLOWLIST.items():
        match = by_lower.get(expected.strip().lower())
        if match:
            suggestion[canonical_field] = match
    return suggestion


def _row_is_empty(row: list[object], positions: dict[str, int]) -> bool:
    """True when a row carries no identity at all.

    The real sheet has about five thousand rows of dragged down formulas below the
    data, all evaluating to 0 in the money columns with every identity column blank.
    Importing those would add thousands of meaningless zero rows.
    """
    for canonical_field in ("therapist", "patient_name", "dos", "cpt"):
        index = positions.get(canonical_field)
        if index is not None and normalize.clean_text(row[index]):
            return False
    return True


def extract_row(
    row: list[object], positions: dict[str, int], row_ref: str
) -> tuple[dict[str, Any] | None, Rejection | None]:
    """Normalize and validate one row. Returns (values, None) or (None, rejection)."""

    def cell(canonical_field: str) -> object:
        index = positions.get(canonical_field)
        return row[index] if index is not None else None

    therapist_raw = normalize.clean_text(cell("therapist"))
    patient_raw = normalize.clean_text(cell("patient_name"))

    if not therapist_raw:
        return None, Rejection(
            row_ref,
            RejectReason.MISSING_THERAPIST,
            field="therapist",
            patient_hint=patient_raw or None,
        )

    if not patient_raw:
        return None, Rejection(
            row_ref,
            RejectReason.MISSING_PATIENT_NAME,
            field="patient_name",
            therapist_hint=therapist_raw,
        )

    try:
        dos = normalize.parse_date(cell("dos"))
    except normalize.ParseError as exc:
        return None, Rejection(
            row_ref,
            RejectReason.BAD_DATE,
            field="dos",
            raw_value=str(exc.raw),
            detail=str(exc),
            patient_hint=patient_raw,
            therapist_hint=therapist_raw,
        )

    if dos is None:
        return None, Rejection(
            row_ref,
            RejectReason.MISSING_DOS,
            field="dos",
            detail="A row with no date of service cannot be placed in any period.",
            patient_hint=patient_raw,
            therapist_hint=therapist_raw,
        )

    cpt = normalize.normalize_cpt(cell("cpt"))
    if not cpt:
        return None, Rejection(
            row_ref,
            RejectReason.MISSING_CPT,
            field="cpt",
            patient_hint=patient_raw,
            therapist_hint=therapist_raw,
        )

    money: dict[str, Decimal] = {}
    for money_field in MONEY_FIELDS:
        try:
            money[money_field] = normalize.parse_money(cell(money_field))
        except normalize.ParseError as exc:
            # Blank reads as 0; unparseable does not. A typo must never quietly
            # become a missing payment.
            return None, Rejection(
                row_ref,
                RejectReason.BAD_MONEY,
                field=money_field,
                raw_value=str(exc.raw),
                detail=str(exc),
                patient_hint=patient_raw,
                therapist_hint=therapist_raw,
            )

    values: dict[str, Any] = {
        "therapist_raw": therapist_raw,
        "patient_name": patient_raw,
        "patient_name_normalized": normalize.normalize_patient_name(patient_raw),
        "patient_code": normalize.normalize_short_code(cell("patient_code")),
        "dos": dos,
        "cpt": cpt,
        "cpt_base": normalize.cpt_base(cpt),
        "insurance_short": normalize.normalize_short_code(cell("insurance_short")),
        "location_short": normalize.normalize_short_code(cell("location_short")),
        "note_code": normalize.normalize_short_code(cell("note_code")),
        "recorded_flag": normalize.normalize_short_code(cell("recorded_flag")),
        "source_row_ref": row_ref,
        **money,
    }

    too_long = _first_overlong_value(values)
    if too_long is not None:
        field_name, limit, actual = too_long
        return None, Rejection(
            row_ref,
            RejectReason.VALUE_TOO_LONG,
            field=field_name,
            raw_value=str(values[field_name])[:120],
            detail=(
                f"{actual} characters, but the {field_name} column holds {limit}. "
                "Shorten the cell in the source and sync again."
            ),
            patient_hint=patient_raw,
            therapist_hint=therapist_raw,
        )

    return values, None


def _first_overlong_value(values: dict[str, Any]) -> tuple[str, int, int] | None:
    """The first value the database would refuse for being too long, if any.

    Reads the limits off the model's own columns rather than repeating them, so a
    column that grows or shrinks cannot leave this check describing the old schema.
    Without it, one over-long cell (a 21 character note, a long payer name) failed
    at flush time and took the whole import with it, valid rows included.
    """
    for field_name, value in values.items():
        if not isinstance(value, str):
            continue
        column = Visit.__table__.columns.get(field_name)
        limit = getattr(getattr(column, "type", None), "length", None)
        if limit and len(value) > limit:
            return field_name, limit, len(value)
    return None


# Namespace for the advisory locks this application takes, so they cannot collide with
# a lock some other tool holds on the same database. "SRI1" as an int32.
_LOCK_NAMESPACE = 0x53524931


def _hold_source_lock(db: Session, source_id: int) -> bool:
    """Claim this source for the current transaction, or report that somebody else has it.

    A transaction scoped lock, so it is released by the commit or rollback that ends the
    sync and there is no unlock to forget or to run on the wrong connection. Re-entrant
    within one transaction, so a caller that syncs the same source twice is unaffected.

    The reason it exists: two syncs of one source reading the same sheet at once both
    look up the existing rows, both decide the same row is new, and the unique
    constraint then kills whichever writes second, so a scheduled pass overlapping a
    manual Sync produced a failed import out of two valid ones.
    """
    if db.get_bind().dialect.name != "postgresql":  # pragma: no cover - production is PG
        return True
    return bool(
        db.execute(select(func.pg_try_advisory_xact_lock(_LOCK_NAMESPACE, source_id))).scalar_one()
    )


def _hunt_header_row(data: SheetData, mapping: dict[str, str]) -> int | None:
    """The sheet row that looks like it holds the mapped headers, if any does.

    Used when the required headers are missing from the configured header row, which
    is what a title row inserted above the headers produces. Scans the first few rows
    below the configured one for the mapped header texts, matched the way the
    importer matches them (stripped, case insensitive), and names the best row when
    it matches at least two of them and at least half. Never acts on the answer:
    naming the fix is the system's job, confirming it is the admin's.
    """
    wanted = {h.strip().lower() for h in mapping.values() if h and h.strip()}
    if len(wanted) < 2:
        return None

    best_row: int | None = None
    best_hits = 0
    for i, row in enumerate(data.rows[:10]):
        cells = {str(c).strip().lower() for c in row if c is not None and str(c).strip()}
        hits = len(wanted & cells)
        if hits > best_hits:
            best_hits = hits
            best_row = data.row_numbers[i]

    if best_hits >= max(2, len(wanted) // 2):
        return best_row
    return None


def _check_duplicate_headers(data: SheetData, source: DataSource, result: SyncResult) -> None:
    """Refuse or announce a mapped header that appears more than once in the sheet.

    build_column_index takes the first occurrence, which was silent: a pasted copy of
    a money column meant the leftmost quietly won even when the stale copy was the
    leftmost. Ambiguity about money or identity fails the run; a duplicated
    descriptive header keeps the leftmost and says so in a warning.
    """
    normalized = [h.strip().lower() for h in data.headers if h and h.strip()]
    duplicated_critical: dict[str, int] = {}
    for field_name, header in source.column_mapping.items():
        if not header or field_name not in IMPORT_ALLOWLIST:
            continue
        occurrences = normalized.count(header.strip().lower())
        if occurrences <= 1:
            continue
        if field_name in MONEY_FIELDS or field_name in REQUIRED_FIELDS:
            duplicated_critical[field_name] = occurrences
        else:
            result.warnings.append(
                f"The header {header!r} appears {occurrences} times in the sheet, and "
                f"{field_name} was read from the leftmost copy. If the wrong copy is "
                "leftmost, remove or rename the duplicate in the sheet and sync again."
            )

    if duplicated_critical:
        raise SheetsError(
            "These mapped headers appear more than once in the sheet: "
            + ", ".join(
                f"{source.column_mapping[field]!r} ({field}, {count} times)"
                for field, count in sorted(duplicated_critical.items())
            )
            + ". Nothing was imported, because with two columns wearing the same name "
            "there is no safe way to know which one holds the real figures. Remove or "
            "rename the duplicate column in the sheet, then sync again.",
            kind=ErrorKind.DUPLICATE_HEADER,
            detail={
                "fields": sorted(duplicated_critical),
                "headers": {field: source.column_mapping[field] for field in duplicated_critical},
            },
        )


def run_sync(
    db: Session,
    source: DataSource,
    client: SheetsClient,
    *,
    dry_run: bool,
    actor: User | None = None,
) -> SyncResult:
    """Import one source. Writes a SyncRun either way, including for a dry run."""
    mode = SyncMode.DRY_RUN if dry_run else SyncMode.LIVE

    run = SyncRun(
        source_id=source.id,
        mode=mode,
        status=SyncStatus.RUNNING,
        run_by_id=actor.id if actor else None,
        tab_name=source.tab_name,
        header_row=source.header_row,
    )
    db.add(run)
    db.flush()

    result = SyncResult(run_id=run.id, mode=mode)

    # The row writes happen inside a savepoint, and the flush that sends them to the
    # database happens INSIDE this handler on purpose.
    #
    # Without both, a database level rejection (a cell too long for its column, a
    # value the type refuses) surfaced on a later flush, after this handler had
    # closed. The whole transaction then rolled back and took the sync run, every
    # recorded rejection and the audit entry with it, so an import that destroyed a
    # quarter's upload left no trace that it had ever run. The savepoint keeps the
    # run row, which was flushed before it, while discarding the failed writes.
    savepoint = db.begin_nested()
    try:
        if not _hold_source_lock(db, source.id):
            raise SheetsError(
                "Another import of this source is running right now, so this one did "
                "nothing rather than compete with it. Wait for that run to finish, "
                "check the history below, and start again only if it did not do what "
                "you wanted.",
                kind=ErrorKind.CONCURRENT_RUN,
            )
        _execute(db, source, client, run, result, dry_run=dry_run)
        db.flush()
        savepoint.commit()
    except SheetsError as exc:
        savepoint.rollback()
        result.error_message = str(exc)
        result.error_kind = exc.kind
        result.error_detail = exc.detail
    except Exception:
        savepoint.rollback()
        logger.exception("Sync failed for source %s", source.id)
        result.error_message = (
            "The import failed unexpectedly and nothing was written. The error has "
            "been logged with a correlation id."
        )
        result.error_kind = ErrorKind.UNEXPECTED

    run.status = SyncStatus.FAILED if result.error_message else SyncStatus.SUCCESS
    run.error_message = result.error_message
    run.error_kind = result.error_kind
    run.error_detail = result.error_detail
    run.finished_at = utcnow()
    # A failed run wrote nothing, whatever the in flight counters had reached before
    # the savepoint was rolled back. Reporting rows inserted on a FAILED run would
    # send an admin looking for data that is not there. Warnings go the same way,
    # for the same reason: they are collected before the writes and phrased as
    # "the import went ahead", which on a failed run is a lie beside an error that
    # says it did not. The drift they describe is not lost, because the failure
    # message names every stale column and the mapping page flags them all.
    if result.error_message:
        result.rows_inserted = 0
        result.rows_updated = 0
        result.rows_unchanged = 0
        result.warnings = []
        # The reconciliation account describes writes, and the writes rolled back.
        result.reconciliation = None
    run.rows_read = result.rows_read
    run.rows_inserted = result.rows_inserted
    run.rows_updated = result.rows_updated
    run.rows_unchanged = result.rows_unchanged
    run.rows_rejected = result.rows_rejected
    run.unmapped_columns = result.unmapped_columns
    run.warnings = result.warnings
    run.reconciliation = result.reconciliation
    run.date_min = result.date_min
    run.date_max = result.date_max

    # Rejections are recorded for both modes: the point of a dry run is to see them.
    for rejection in result.rejections:
        db.add(
            ImportErrorRow(
                sync_run_id=run.id,
                source_id=source.id,
                source_row_ref=rejection.row_ref,
                reason=rejection.reason,
                field=rejection.field,
                raw_value=rejection.raw_value,
                detail=rejection.detail,
                patient_hint=rejection.patient_hint,
                therapist_hint=rejection.therapist_hint,
            )
        )

    # A completed read of the sheet, dry or live, produces the current account of what
    # did not make it in, so the accounts left by earlier runs are stale copies of it.
    # Without this every sync appended its rejections to the pile: a sheet with 6,766
    # undated rows became 6,766 open errors on the first look and 13,532 on the second,
    # and fixing the sheet cleared none of them.
    #
    # Scoped to the rows this run actually examined. The read stops at the first
    # identity-blank row, and an admin can repoint the tab or header row between runs,
    # so a successful run is not proof the whole sheet was seen. An open error past the
    # last row read keeps its place in the review queue rather than being resolved on
    # the strength of a read that never reached it.
    if result.ok and result.rows_read > 0:
        result.superseded_errors = _supersede_stale_errors(
            db,
            source.id,
            run.id,
            last_examined_row=source.header_row + result.rows_read,
            dry_run=dry_run,
            tab_name=source.tab_name,
            header_row=source.header_row,
        )

    if not dry_run and result.ok:
        source.last_synced_at = utcnow()
        # Only when the run actually read something. A run over an empty tab, or over a
        # tab an admin has just repointed, has no dates of its own, and writing those
        # Nones erased the source's recorded coverage while its rows stayed in the
        # database. The source then claimed to cover nothing, the status page said so,
        # and the vintage comparison that decides which sheet may revise money lost the
        # one signal it has.
        if result.rows_read > 0:
            source.coverage_start = result.date_min
            source.coverage_end = result.date_max
        db.flush()
        source.row_count = _count_visits(db, source.id)

    return result


def _supersede_stale_errors(
    db: Session,
    source_id: int,
    current_run_id: int,
    *,
    last_examined_row: int,
    dry_run: bool,
    tab_name: str | None,
    header_row: int,
) -> int:
    """Resolve unresolved errors that earlier runs of this source left behind.

    Resolved, not deleted: the rows stay visible under the All filter, each carrying a
    note naming the run whose account replaced them, so nothing is silently dropped.
    A failed run never supersedes anything, because it did not produce a new account.

    Five deliberate boundaries:
    - Only errors at or before `last_examined_row`. The current run's account covers
      exactly the sheet rows it read, no further.
    - Only errors from runs that read the same tab at the same header row. A row
      reference is a row NUMBER, and row 5 of one tab is not row 5 of another, so after
      an admin repointed the tab this resolved another sheet's open rejections on the
      strength of a read that never looked at them.
    - Only errors from runs with a lower id. Two overlapping syncs must not let the
      older read mark the newer read's account as stale.
    - Only errors whose row reference is a plain row number. One that is not cannot be
      placed inside or outside the read, so it keeps its place in the review queue.
    - A dry run replaces only earlier dry runs' findings. The open queue tracks what
      is missing from the database, and a dry run changes nothing in the database: a
      clean preview of a fixed sheet must not clear a live run's errors while the
      fixed rows are still unimported, or every page would call the source fully
      accounted for when its data exists nowhere.
    """
    stmt = (
        select(ImportErrorRow.id, ImportErrorRow.source_row_ref)
        .join(SyncRun, SyncRun.id == ImportErrorRow.sync_run_id)
        .where(
            ImportErrorRow.source_id == source_id,
            ImportErrorRow.sync_run_id < current_run_id,
            ImportErrorRow.resolved_at.is_(None),
            SyncRun.tab_name.is_(None) if tab_name is None else SyncRun.tab_name == tab_name,
            SyncRun.header_row == header_row,
        )
    )
    if dry_run:
        stmt = stmt.where(SyncRun.mode == SyncMode.DRY_RUN)
    candidates = db.execute(stmt).all()
    stale_ids = [
        row_id
        for row_id, ref in candidates
        if ref is not None and ref.isdigit() and int(ref) <= last_examined_row
    ]
    if not stale_ids:
        return 0

    superseded = 0
    note = (
        f"Superseded by sync run {current_run_id}, which re-read this part of the "
        "sheet. That run's error list is the current account of it."
    )
    # Chunked so the IN clause stays a sane size against a six-figure backlog.
    for start in range(0, len(stale_ids), 5000):
        chunk = stale_ids[start : start + 5000]
        superseded += db.execute(
            update(ImportErrorRow)
            .where(ImportErrorRow.id.in_(chunk))
            .values(resolved_at=utcnow(), resolution_note=note)
            .execution_options(synchronize_session=False)
        ).rowcount
    return superseded


def _count_visits(db: Session, source_id: int) -> int:
    return db.execute(select(func.count(Visit.id)).where(Visit.source_id == source_id)).scalar_one()


def _execute(
    db: Session,
    source: DataSource,
    client: SheetsClient,
    run: SyncRun,
    result: SyncResult,
    *,
    dry_run: bool,
) -> None:
    if not source.tab_name:
        raise SheetsError("This source has no tab selected yet.", kind=ErrorKind.BAD_SOURCE_CONFIG)
    assert_tab_allowed(source.tab_name)

    missing = source.missing_required_fields
    if missing:
        raise SheetsError(
            "The column mapping is incomplete. Still needed: " + ", ".join(sorted(missing)) + ".",
            kind=ErrorKind.MAPPING_INCOMPLETE,
            detail={"fields": sorted(missing)},
        )

    data: SheetData = client.read_tab(
        source.spreadsheet_id or "", source.tab_name, source.header_row
    )

    positions, unmapped = build_column_index(data.headers, source.column_mapping)
    result.unmapped_columns = unmapped

    # This is the check that actually catches a vanished IDENTITY column: a mapped
    # required field whose header changed never enters the position map, so it
    # surfaces here, before the graded check below ever sees it. The message carries
    # the same fix guidance as the money branch for that reason.
    still_missing = REQUIRED_FIELDS - set(positions)
    if still_missing:
        # Someone inserting a title row above the headers shifts every header down,
        # and the refusal used to leave the admin to work out why. Hunt the first few
        # rows for the mapped header texts and, if one looks like the real header
        # row, say so. A suggestion in the message only: the Header row setting is
        # never changed automatically, per the no guessing rule.
        suggested_row = _hunt_header_row(data, source.column_mapping)
        hint = (
            f" Row {suggested_row} of the tab looks like it holds these headers; if a "
            f"row was inserted above them, set Header row to {suggested_row} and save."
            if suggested_row is not None
            else ""
        )
        raise SheetsError(
            "The sheet does not contain the mapped header for: "
            + ", ".join(
                f"{field} (expected header {source.column_mapping.get(field)!r})"
                if source.column_mapping.get(field)
                else field
                for field in sorted(still_missing)
            )
            + ". The tab layout may have changed; check the column mapping. To fix "
            "it: open this source's column mapping, and for each field above either "
            "pick the column's new header from the dropdown or correct the sheet, "
            "then save and sync again." + hint,
            kind=ErrorKind.HEADER_DRIFT_IDENTITY,
            detail={
                "fields": sorted(still_missing),
                "expected": {
                    field: source.column_mapping.get(field) for field in sorted(still_missing)
                },
                "suggested_header_row": suggested_row,
            },
        )

    # A mapped header appearing more than once is ambiguity about which column is
    # real, and the position map silently took the leftmost. For money and identity
    # that ambiguity is dangerous, so it refuses; for a descriptive field the
    # leftmost is used and said out loud, graded exactly like the vanished check.
    _check_duplicate_headers(data, source, result)

    # A mapped column whose header text changed in the sheet used to vanish silently:
    # build_column_index cannot find it, so the field simply never enters the position
    # map, every cell reads as blank, and blank money means zero by design. Renaming
    # "Total paid" to "Paid total" produced a SUCCESS run with no rejections and rows
    # reading total_due 175.00 against total_paid 0.00. Revenue read zero while billed
    # read the full amount. Only the four required fields were guarded; the other
    # fourteen, which is every money column, were not.
    #
    # The response is graded by what the field means, because the failure modes are
    # not the same size. A vanished money or identity column silently corrupts every
    # figure, so it fails the run. A vanished descriptive column (a payer code, a
    # location, a note flag) makes rows less complete, not wrong: refusing the run
    # over it once blocked an entire quarter's import because "Recorded" had been
    # retitled in the sheet, which is a worse outcome than the gap itself. Those now
    # import with the field empty and a warning that survives on the run page, so the
    # drift is visible and gets fixed rather than quietly becoming permanent.
    #
    # An unmapped column is a deliberate choice and stays silent either way. And
    # nothing here ever guesses that a new header means an old column: deciding that
    # "Unrecorded" is the artist formerly known as "Recorded" is a remap only an
    # admin may confirm, because a wrong guess silently reassigns the wrong data.
    vanished = {field for field, header in source.column_mapping.items() if header} - set(positions)
    vanished &= set(IMPORT_ALLOWLIST)
    # REQUIRED_FIELDS is belt and braces here: the still_missing check above already
    # intercepts a vanished identity column, so in practice this set only ever holds
    # money fields. It stays in the intersection so the run still fails if that check
    # is ever moved or narrowed.
    vanished_critical = vanished & (MONEY_FIELDS | REQUIRED_FIELDS)
    vanished_descriptive = frozenset(vanished - MONEY_FIELDS - REQUIRED_FIELDS)
    if vanished_critical:
        # The descriptive stragglers are named in the same error, so a mixed rename
        # is fixed in one visit to the mapping rather than discovered one run at a
        # time. Their warnings are not recorded on this run: it failed, nothing was
        # imported around anything, and a failed run's warnings would have to lie.
        also_stale = (
            (
                " While fixing it, these descriptive columns are also missing and will "
                "import with gaps until remapped: "
                + ", ".join(
                    f"{field} (expected header {source.column_mapping[field]!r})"
                    for field in sorted(vanished_descriptive)
                )
                + "."
            )
            if vanished_descriptive
            else ""
        )
        raise SheetsError(
            "These columns are mapped but no longer present in the sheet: "
            + ", ".join(
                f"{field} (expected header {source.column_mapping[field]!r})"
                for field in sorted(vanished_critical)
            )
            + ". The tab layout may have changed; check the column mapping. Nothing was "
            "imported, because a missing money column would otherwise read as zero on "
            "every row. To fix it: open this source's column mapping, and for each "
            "field above either pick the column's new header from the dropdown or "
            "clear the mapping if the column is gone for good, then save and sync "
            "again." + also_stale,
            kind=ErrorKind.HEADER_DRIFT_MONEY,
            detail={
                "fields": sorted(vanished_critical),
                "expected": {
                    field: source.column_mapping[field] for field in sorted(vanished_critical)
                },
                "also_stale": sorted(vanished_descriptive),
            },
        )

    for field_name in sorted(vanished_descriptive):
        # Tense follows the mode, because this text sits on a page whose banner says
        # "Nothing was written" for a dry run, and a warning that contradicts the
        # page it is on teaches the reader to trust neither.
        effect = (
            "A live import will go ahead anyway: new rows will land without this "
            "field, and rows already stored will keep the value they have."
            if dry_run
            else "The import went ahead: new rows landed without this field, and "
            "rows already stored kept the value they had."
        )
        result.warnings.append(
            f"The mapped column for {field_name} (expected header "
            f"{source.column_mapping[field_name]!r}) is missing from the sheet. "
            f"{effect} Until the mapping is fixed, anything grouped by this field "
            "will undercount. To fix it: open this source's column mapping, pick the "
            "column's new header for this field or clear it if the column is gone "
            "for good, then save and sync again."
        )

    resolver = AliasResolver(db)

    # Pre-pass for the date span of the incoming sheet, so the existing rows can be
    # loaded for exactly that window. Cheap: the rows are already in memory.
    incoming_dates = _incoming_date_span(data, positions)
    existing = _load_existing(db, *incoming_dates) if not dry_run else {}
    # How far forward this sheet reaches, against how far every other source reaches.
    # Used to tell a later export revising money from an older one reverting it.
    incoming_end = incoming_dates[1]
    vintages = _source_vintages(db)
    # The span's totals before anything is touched, for the reconciliation account.
    # Captured now because the loop below mutates these same objects in place.
    span_before = {
        "rows": len(existing),
        "collected": sum((v.total_paid for v in existing.values()), Decimal("0.00")),
        "billed": sum((v.total_due for v in existing.values()), Decimal("0.00")),
    }
    seen_keys: set[tuple] = set()

    for row, row_number in zip(data.rows, data.row_numbers, strict=True):
        if _row_is_empty(row, positions):
            # End of the data block. Everything past here is formula residue.
            break

        row_ref = str(row_number)
        result.rows_read += 1

        values, rejection = extract_row(row, positions, row_ref)
        if rejection is not None:
            result.rejections.append(rejection)
            continue

        assert values is not None
        therapist_id = resolver.resolve(values["therapist_raw"])
        if therapist_id is None:
            suggestions = resolver.suggest(values["therapist_raw"])
            result.rejections.append(
                Rejection(
                    row_ref,
                    RejectReason.UNKNOWN_THERAPIST,
                    field="therapist",
                    raw_value=values["therapist_raw"],
                    detail=(
                        "Did you mean: " + ", ".join(suggestions) + "? "
                        "Add an alias on the therapist, then sync again."
                        if suggestions
                        else "No therapist matches. Create one, or add an alias."
                    ),
                    patient_hint=values["patient_name"],
                    therapist_hint=values["therapist_raw"],
                )
            )
            continue

        key = (
            therapist_id,
            values["patient_name_normalized"],
            values["dos"],
            values["cpt"],
        )

        if key in seen_keys:
            # A true duplicate within one sheet. Both are kept and the second is
            # flagged, rather than one silently overwriting the other.
            result.rejections.append(
                Rejection(
                    row_ref,
                    RejectReason.DUPLICATE_KEY,
                    detail=(
                        "Another row in this sheet has the same therapist, patient, "
                        "date, and CPT. Both were left in place for review."
                    ),
                    patient_hint=values["patient_name"],
                    therapist_hint=values["therapist_raw"],
                )
            )
            continue
        seen_keys.add(key)

        _track_dates(result, values["dos"])

        if dry_run:
            # Counted as an insert for reporting purposes: a dry run deliberately
            # does not read existing rows, so it cannot tell insert from update.
            result.rows_inserted += 1
            continue

        _upsert(
            db,
            source,
            run,
            existing,
            key,
            values,
            therapist_id,
            result,
            row_ref=row_ref,
            incoming_end=incoming_end,
            vintages=vintages,
            unseen_fields=vanished_descriptive,
        )

    if not dry_run and incoming_dates[0] is not None:
        _reconcile(source, existing, seen_keys, span_before, incoming_dates, result)


def _reconcile(
    source: DataSource,
    existing: dict[tuple, Visit],
    seen_keys: set[tuple],
    span_before: dict,
    incoming_dates: tuple[date | None, date | None],
    result: SyncResult,
) -> None:
    """The account of what this live run changed inside its own date span.

    Advisory by design: nothing here blocks or reverts anything. The whole silent
    wrong category, a fat fingered amount, a mass paste error, a filtered sort that
    destroyed rows, shares one property: each import is individually valid and only
    the before and after comparison shows something moved. So the comparison is
    computed here, where both sides are in hand, persisted on the run, and shown as
    neutral information, escalating to a warning only at extremes. The human decides;
    the next sync converges on whatever the corrected sheet says. See A-100.

    The one legitimate cause of large swings, a first import into an empty span, is
    excluded by the threshold on the size of what was already there.
    """
    rows_after = len(existing)
    collected_after = sum((v.total_paid for v in existing.values()), Decimal("0.00"))
    billed_after = sum((v.total_due for v in existing.values()), Decimal("0.00"))

    # Rows this source itself imported, inside this sheet's own span, that this read
    # did not contain. Scoped to this source so a quarter boundary overlap with
    # another sheet never counts as a deletion, and to pre existing rows so the ones
    # this run just inserted are not compared against themselves.
    vanished_rows = [
        visit
        for key, visit in existing.items()
        if key not in seen_keys and visit.id is not None and visit.source_id == source.id
    ]

    movers = sorted(result.movers, key=lambda m: m["magnitude"], reverse=True)[:MOVERS_SHOWN]

    result.reconciliation = {
        "span_start": incoming_dates[0].isoformat() if incoming_dates[0] else None,
        "span_end": incoming_dates[1].isoformat() if incoming_dates[1] else None,
        "rows_before": span_before["rows"],
        "rows_after": rows_after,
        "collected_before": str(span_before["collected"]),
        "collected_after": str(collected_after),
        "billed_before": str(span_before["billed"]),
        "billed_after": str(billed_after),
        "movers": movers,
        "vanished_count": len(vanished_rows),
        "vanished_span": (
            [
                min(v.dos for v in vanished_rows).isoformat(),
                max(v.dos for v in vanished_rows).isoformat(),
            ]
            if vanished_rows
            else None
        ),
    }

    before_collected = span_before["collected"]
    if before_collected >= RECONCILE_MIN_BASE:
        shift = abs(collected_after - before_collected)
        ratio = shift / before_collected
        if ratio >= RECONCILE_WARN_RATIO:
            result.warnings.append(
                f"Collected money for {incoming_dates[0]} to {incoming_dates[1]} moved "
                f"from {before_collected} to {collected_after} in this one sync, a "
                f"change of {(ratio * 100).quantize(Decimal('1'))} percent. A batch of "
                "insurance payments landing does this legitimately; a paste error does "
                "it too. The largest movers are listed on this run's page: check them "
                "before relying on the new figures."
            )

    if vanished_rows:
        first = min(v.dos for v in vanished_rows)
        last = max(v.dos for v in vanished_rows)
        result.warnings.append(
            f"{len(vanished_rows)} row(s) already imported from this source, dated "
            f"{first} to {last}, were not in this read (or failed to import from it). "
            "Nothing was deleted here: the stored rows stand and the figures still "
            "include them. If they were removed from the sheet by accident, restore "
            "them with the sheet's version history (File, then Version history) and "
            "sync again. If they were voided on purpose, tell an administrator: this "
            "application keeps imported rows, so the figures overcount until that is "
            "resolved."
        )


def _incoming_date_span(
    data: SheetData, positions: dict[str, int]
) -> tuple[date | None, date | None]:
    """The earliest and latest readable date in the sheet, for scoping the preload."""
    index = positions.get("dos")
    if index is None:
        return (None, None)

    earliest: date | None = None
    latest: date | None = None
    for row in data.rows:
        if _row_is_empty(row, positions):
            break
        try:
            parsed = normalize.parse_date(row[index])
        except normalize.ParseError:
            continue
        if parsed is None:
            continue
        earliest = parsed if earliest is None else min(earliest, parsed)
        latest = parsed if latest is None else max(latest, parsed)
    return (earliest, latest)


def _track_dates(result: SyncResult, dos: date) -> None:
    result.date_min = dos if result.date_min is None else min(result.date_min, dos)
    result.date_max = dos if result.date_max is None else max(result.date_max, dos)


def _load_existing(db: Session, earliest: date | None, latest: date | None) -> dict[tuple, Visit]:
    """Every visit already stored in the incoming date range, keyed by identity.

    Deliberately NOT scoped to one source. A visit is the same visit whichever sheet
    it arrives on, so a row already imported from last quarter's sheet has to be found
    and updated rather than inserted a second time. Scoping this to the source was a
    real defect: the practice's export window rolls back into the previous quarter, so
    a visit near a quarter boundary appeared in both sheets, stored twice, and was
    counted twice in every figure. See ASSUMPTIONS.md A-022.

    Scoped to the incoming date range rather than loading the whole table, so memory
    stays proportional to the quarter being imported rather than to the entire history
    the application accumulates.

    Loaded once rather than queried per row: nine thousand round trips against an
    encrypted SQLite file is the difference between a sync that takes a second and one
    that takes a minute.
    """
    if earliest is None or latest is None:
        # No readable date anywhere in the incoming sheet. The fallback used to be to
        # load the whole sessions table, every row of every quarter ever imported, as
        # ORM objects each joining its therapist: the one case where the preload was
        # unbounded was the case where it was useless. A row with no readable date
        # rejects before it reaches the upsert, so there is nothing here to match
        # against and an empty map is both cheaper and correct.
        return {}

    visits = (
        db.execute(select(Visit).where(Visit.dos >= earliest, Visit.dos <= latest)).scalars().all()
    )
    return {(v.therapist_id, v.patient_name_normalized, v.dos, v.cpt): v for v in visits}


@dataclass(frozen=True)
class _SourceVintage:
    """What we know about how recent another source's account of a visit is.

    `coverage_end` is the latest date of service that source actually contained,
    observed by its own sync rather than typed in, so it is the one honest ordering
    signal available. A rolling quarterly export reaches further forward than a
    historical backfill of an earlier quarter.
    """

    label: str
    coverage_end: date | None


def _source_vintages(db: Session) -> dict[int, _SourceVintage]:
    rows = db.execute(select(DataSource.id, DataSource.label, DataSource.coverage_end)).all()
    return {sid: _SourceVintage(label, end) for sid, label, end in rows}


def _money_disagreements(current: Visit, payload: dict[str, Any]) -> list[str]:
    return sorted(f for f in MONEY_FIELDS if getattr(current, f) != payload.get(f))


def _older_owner(
    current: Visit,
    source: DataSource,
    incoming_end: date | None,
    vintages: dict[int, _SourceVintage],
) -> _SourceVintage | None:
    """The source holding this row, when its account is more recent than the incoming one.

    None whenever we have no grounds to call the incoming sheet older: the row came from
    this same source, either span is unknown, or the incoming sheet reaches at least as
    far forward.
    """
    if current.source_id == source.id or incoming_end is None:
        return None
    owner = vintages.get(current.source_id)
    if owner is None or owner.coverage_end is None:
        return None
    return owner if incoming_end < owner.coverage_end else None


def _upsert(
    db: Session,
    source: DataSource,
    run: SyncRun,
    existing: dict[tuple, Visit],
    key: tuple,
    values: dict[str, Any],
    therapist_id: int,
    result: SyncResult,
    *,
    row_ref: str,
    incoming_end: date | None,
    vintages: dict[int, _SourceVintage],
    unseen_fields: frozenset[str] = frozenset(),
) -> None:
    payload = {k: v for k, v in values.items() if k != "therapist_raw"}

    current = existing.get(key)
    if current is None:
        visit = Visit(
            source_id=source.id,
            therapist_id=therapist_id,
            last_sync_run_id=run.id,
            **payload,
        )
        db.add(visit)
        existing[key] = visit
        result.rows_inserted += 1
        return

    if current.differs_from(payload, ignore=unseen_fields):
        # A visit is global, so the same one arrives on more than one sheet: the
        # practice's export window rolls back into the previous quarter, and a
        # historical upload covers ground the live sheet also covers. When two sheets
        # disagreed about money, whichever synced last won outright, so importing an
        # old backfill silently reverted collected revenue on every shared row and no
        # figure, page or log said anything had moved.
        #
        # Later sheets legitimately revise money, because payments land after the date
        # of service. Earlier ones cannot. So an incoming sheet that ends before the
        # sheet already holding the row is treated as the older account: the stored
        # figures stand and the disagreement goes to the review queue for a human,
        # rather than being applied or thrown away.
        owner = _older_owner(current, source, incoming_end, vintages)
        disagreements = _money_disagreements(current, payload) if owner else []
        if owner is not None and disagreements:
            first = disagreements[0]
            result.rejections.append(
                Rejection(
                    row_ref,
                    RejectReason.CONFLICTING_SNAPSHOT,
                    field=first,
                    raw_value=str(payload.get(first)),
                    detail=(
                        f"This row is already recorded from {owner.label!r}, whose data runs "
                        f"to {owner.coverage_end}, later than this sheet. They disagree on "
                        + ", ".join(disagreements)
                        + f" (stored {getattr(current, first)}, this sheet {payload.get(first)}). "
                        "The stored figures were kept, because an earlier export cannot have "
                        "seen payments that landed after it. Nothing was changed; check which "
                        "sheet is right."
                    ),
                    patient_hint=values.get("patient_name"),
                    therapist_hint=values.get("therapist_raw"),
                )
            )
            return
        paid_before, due_before = current.total_paid, current.total_due
        current.apply(payload, ignore=unseen_fields)
        current.last_sync_run_id = run.id
        result.rows_updated += 1
        # Feed the reconciliation account's biggest movers table. Row reference,
        # date and code only: money movement is not patient identity.
        magnitude = abs(current.total_paid - paid_before) + abs(current.total_due - due_before)
        if magnitude > 0:
            result.movers.append(
                {
                    "row_ref": row_ref,
                    "dos": str(values["dos"]),
                    "cpt": values["cpt"],
                    "paid_before": str(paid_before),
                    "paid_after": str(current.total_paid),
                    "due_before": str(due_before),
                    "due_after": str(current.total_due),
                    "magnitude": float(magnitude),
                }
            )
    else:
        # Unchanged rows are left alone so a re-sync does not churn updated_at on
        # every row and make the history useless.
        result.rows_unchanged += 1
