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

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.data_source import (
    IMPORT_ALLOWLIST,
    MONEY_FIELDS,
    REQUIRED_FIELDS,
    DataSource,
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
    date_min: date | None = None
    date_max: date | None = None
    error_message: str | None = None
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
        _execute(db, source, client, run, result, dry_run=dry_run)
        db.flush()
        savepoint.commit()
    except SheetsError as exc:
        savepoint.rollback()
        result.error_message = str(exc)
    except Exception:
        savepoint.rollback()
        logger.exception("Sync failed for source %s", source.id)
        result.error_message = (
            "The import failed unexpectedly and nothing was written. The error has "
            "been logged with a correlation id."
        )

    run.status = SyncStatus.FAILED if result.error_message else SyncStatus.SUCCESS
    run.error_message = result.error_message
    run.finished_at = utcnow()
    # A failed run wrote nothing, whatever the in flight counters had reached before
    # the savepoint was rolled back. Reporting rows inserted on a FAILED run would
    # send an admin looking for data that is not there.
    if result.error_message:
        result.rows_inserted = 0
        result.rows_updated = 0
        result.rows_unchanged = 0
    run.rows_read = result.rows_read
    run.rows_inserted = result.rows_inserted
    run.rows_updated = result.rows_updated
    run.rows_unchanged = result.rows_unchanged
    run.rows_rejected = result.rows_rejected
    run.unmapped_columns = result.unmapped_columns
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
        )

    if not dry_run and result.ok:
        source.last_synced_at = utcnow()
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
) -> int:
    """Resolve unresolved errors that earlier runs of this source left behind.

    Resolved, not deleted: the rows stay visible under the All filter, each carrying a
    note naming the run whose account replaced them, so nothing is silently dropped.
    A failed run never supersedes anything, because it did not produce a new account.

    Four deliberate boundaries:
    - Only errors at or before `last_examined_row`. The current run's account covers
      exactly the sheet rows it read, no further.
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
    stmt = select(ImportErrorRow.id, ImportErrorRow.source_row_ref).where(
        ImportErrorRow.source_id == source_id,
        ImportErrorRow.sync_run_id < current_run_id,
        ImportErrorRow.resolved_at.is_(None),
    )
    if dry_run:
        stmt = stmt.join(SyncRun, SyncRun.id == ImportErrorRow.sync_run_id).where(
            SyncRun.mode == SyncMode.DRY_RUN
        )
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
    from sqlalchemy import func

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
        raise SheetsError("This source has no tab selected yet.")
    assert_tab_allowed(source.tab_name)

    missing = source.missing_required_fields
    if missing:
        raise SheetsError(
            "The column mapping is incomplete. Still needed: " + ", ".join(sorted(missing)) + "."
        )

    data: SheetData = client.read_tab(
        source.spreadsheet_id or "", source.tab_name, source.header_row
    )

    positions, unmapped = build_column_index(data.headers, source.column_mapping)
    result.unmapped_columns = unmapped

    still_missing = REQUIRED_FIELDS - set(positions)
    if still_missing:
        raise SheetsError(
            "The sheet does not contain the mapped header for: "
            + ", ".join(sorted(still_missing))
            + ". The tab layout may have changed; check the column mapping."
        )

    # A mapped column whose header text changed in the sheet used to vanish silently:
    # build_column_index cannot find it, so the field simply never enters the position
    # map, every cell reads as blank, and blank money means zero by design. Renaming
    # "Total paid" to "Paid total" produced a SUCCESS run with no rejections and rows
    # reading total_due 175.00 against total_paid 0.00. Revenue read zero while billed
    # read the full amount. Only the four required fields were guarded; the other
    # fourteen, which is every money column, were not.
    #
    # Refusing the run is the right default for money. An unmapped column is a
    # deliberate choice and stays silent; a mapped column that has gone missing is
    # drift, and drift gets a FAILED run with an actionable message.
    vanished = {field for field, header in source.column_mapping.items() if header} - set(positions)
    vanished &= set(IMPORT_ALLOWLIST)
    if vanished:
        raise SheetsError(
            "These columns are mapped but no longer present in the sheet: "
            + ", ".join(
                f"{field} (expected header {source.column_mapping[field]!r})"
                for field in sorted(vanished)
            )
            + ". The tab layout may have changed; check the column mapping. Nothing was "
            "imported, because a missing money column would otherwise read as zero on "
            "every row."
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
    stmt = select(Visit)
    if earliest is not None and latest is not None:
        stmt = stmt.where(Visit.dos >= earliest, Visit.dos <= latest)
    visits = db.execute(stmt).scalars().all()
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

    if current.differs_from(payload):
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
        current.apply(payload)
        current.last_sync_run_id = run.id
        result.rows_updated += 1
    else:
        # Unchanged rows are left alone so a re-sync does not churn updated_at on
        # every row and make the history useless.
        result.rows_unchanged += 1
