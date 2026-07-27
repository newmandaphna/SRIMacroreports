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

from sqlalchemy import select
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
    return values, None


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
    )
    db.add(run)
    db.flush()

    result = SyncResult(run_id=run.id, mode=mode)

    try:
        _execute(db, source, client, run, result, dry_run=dry_run)
    except SheetsError as exc:
        result.error_message = str(exc)
    except Exception:
        logger.exception("Sync failed for source %s", source.id)
        result.error_message = "The import failed unexpectedly. The error has been logged."

    run.status = SyncStatus.FAILED if result.error_message else SyncStatus.SUCCESS
    run.error_message = result.error_message
    run.finished_at = utcnow()
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

    if not dry_run and result.ok:
        source.last_synced_at = utcnow()
        source.coverage_start = result.date_min
        source.coverage_end = result.date_max
        db.flush()
        source.row_count = _count_visits(db, source.id)

    return result


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

    resolver = AliasResolver(db)
    existing = _load_existing(db, source.id) if not dry_run else {}
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

        _upsert(db, source, run, existing, key, values, therapist_id, result)


def _track_dates(result: SyncResult, dos: date) -> None:
    result.date_min = dos if result.date_min is None else min(result.date_min, dos)
    result.date_max = dos if result.date_max is None else max(result.date_max, dos)


def _load_existing(db: Session, source_id: int) -> dict[tuple, Visit]:
    """Every visit already imported from this source, keyed by identity.

    Loaded once rather than queried per row: nine thousand round trips against an
    encrypted SQLite file is the difference between a sync that takes a second and
    one that takes a minute.
    """
    visits = db.execute(select(Visit).where(Visit.source_id == source_id)).scalars().all()
    return {(v.therapist_id, v.patient_name_normalized, v.dos, v.cpt): v for v in visits}


def _upsert(
    db: Session,
    source: DataSource,
    run: SyncRun,
    existing: dict[tuple, Visit],
    key: tuple,
    values: dict[str, Any],
    therapist_id: int,
    result: SyncResult,
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
        current.apply(payload)
        current.last_sync_run_id = run.id
        result.rows_updated += 1
    else:
        # Unchanged rows are left alone so a re-sync does not churn updated_at on
        # every row and make the history useless.
        result.rows_unchanged += 1
