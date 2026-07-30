"""The Data Sources registry, sync runs, and import errors.

The database is the system of record. Sheets are ingestion sources only. Once synced,
data lives in the app permanently, so quarterly rotation never touches historical data
and the app becomes the only place full cross quarter history exists.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.types import UTCDateTime, enum_column, utcnow
from app.models.user import User

# The only columns that may be imported, keyed by the canonical field name the app
# uses internally. The values are the header text as it appears in the Q sheet.
#
# This is the minimum necessary allowlist (SECURITY.md section 6.1). It is enforced at
# import, not merely in the mapping UI, so a hand crafted mapping cannot smuggle in a
# date of birth.
IMPORT_ALLOWLIST: dict[str, str] = {
    "therapist": "Therapist",
    "patient_name": "Patient name",
    "patient_code": "Patient Code",
    "dos": "DOS",
    "cpt": "CPT",
    "insurance_short": "Ins",
    "location_short": "Loc",
    "note_code": "NOTE",
    "due_from_pt": "Due from pt",
    "paid_by_pt": "Paid by pt",
    "pt_amount_due": "Pt. Amount Due",
    "due_from_ins": "Due from ins",
    "paid_by_ins": "Paid by ins",
    "ins_balance": "Ins balance",
    "total_due": "Total due",
    "total_paid": "Total paid",
    "total_balance": "Total balance",
    "recorded_flag": "Recorded",
}

# Without these a row cannot be identified or placed in a period, so a mapping that
# omits any of them is rejected before it can be saved.
REQUIRED_FIELDS: frozenset[str] = frozenset({"therapist", "patient_name", "dos", "cpt"})

MONEY_FIELDS: frozenset[str] = frozenset(
    {
        "due_from_pt",
        "paid_by_pt",
        "pt_amount_due",
        "due_from_ins",
        "paid_by_ins",
        "ins_balance",
        "total_due",
        "total_paid",
        "total_balance",
    }
)

# Tabs holding raw Valant exports. They carry dates of birth, home and work emails,
# phone numbers, and ZIP codes, none of which any module needs. Blocked outright, as
# belt and braces with the column allowlist (SECURITY.md section 6.2).
BLOCKED_TAB_PREFIX = "RAW_"


class SourceProvider(StrEnum):
    GOOGLE_SHEETS = "google_sheets"
    # A bundled synthetic workbook with obviously fake patients, so the sync engine
    # can be demonstrated and tested end to end without credentials and without PHI.
    DEMO = "demo"
    # Historical data arrives as an uploaded .xlsx or .csv instead of a live sheet.
    # Same pipeline, same allowlist; the file is parsed in memory and never stored.
    UPLOAD = "upload"

    @property
    def label(self) -> str:
        return {
            SourceProvider.GOOGLE_SHEETS: "Google Sheets",
            SourceProvider.DEMO: "Demo (synthetic data)",
            SourceProvider.UPLOAD: "File upload (historical data)",
        }[self]


class DataSource(Base):
    __tablename__ = "data_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Human label for the quarter, for example "Q2 2026".
    label: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    provider: Mapped[SourceProvider] = mapped_column(
        enum_column(SourceProvider, length=30),
        nullable=False,
        default=SourceProvider.GOOGLE_SHEETS,
    )

    spreadsheet_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    spreadsheet_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    tab_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    header_row: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # {canonical_field: sheet header text}. Per source, because tab layouts drift
    # between quarters. A new source prefills from the previous source's mapping.
    column_mapping: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Observed coverage, filled in by the sync from the rows actually read, rather
    # than typed by an admin and left to go stale.
    coverage_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    coverage_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"DataSource(id={self.id}, label={self.label!r}, active={self.active})"

    @property
    def mapped_fields(self) -> set[str]:
        return {k for k, v in self.column_mapping.items() if v}

    @property
    def missing_required_fields(self) -> set[str]:
        return REQUIRED_FIELDS - self.mapped_fields

    @property
    def is_ready_to_sync(self) -> bool:
        if self.provider is SourceProvider.GOOGLE_SHEETS and not (
            self.spreadsheet_id and self.tab_name
        ):
            return False
        # The engine refuses to run without a tab name, whatever the provider.
        if not self.tab_name:
            return False
        return not self.missing_required_fields


class SyncMode(StrEnum):
    DRY_RUN = "dry_run"
    LIVE = "live"


class SyncStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"

    @property
    def pill_class(self) -> str:
        return {
            SyncStatus.RUNNING: "pill--neutral",
            SyncStatus.SUCCESS: "pill--ok",
            SyncStatus.FAILED: "pill--below",
        }[self]


class SyncRun(Base):
    """One execution of the importer, dry run or live.

    Never deleted, so the import history is as reviewable as the audit log.
    """

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )

    mode: Mapped[SyncMode] = mapped_column(enum_column(SyncMode, length=20), nullable=False)
    status: Mapped[SyncStatus] = mapped_column(
        enum_column(SyncStatus, length=20), nullable=False, default=SyncStatus.RUNNING
    )

    # What was actually read, captured at run time. The source's tab can be repointed
    # afterwards, and a run page that cannot say which tab it read is how an entire
    # evening was once lost to syncing the wrong one.
    tab_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    header_row: Mapped[int | None] = mapped_column(Integer, nullable=True)

    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    rows_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    date_min: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_max: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Headers present in the sheet that no mapping claims. Surfaced rather than
    # ignored, because a new column appearing is how a quarter's layout drift
    # announces itself.
    unmapped_columns: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    run_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )

    source: Mapped[DataSource] = relationship(lazy="selectin")
    run_by: Mapped[User | None] = relationship(lazy="selectin")

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def rows_upserted(self) -> int:
        return self.rows_inserted + self.rows_updated


class RejectReason(StrEnum):
    MISSING_THERAPIST = "missing_therapist"
    UNKNOWN_THERAPIST = "unknown_therapist"
    MISSING_PATIENT_NAME = "missing_patient_name"
    MISSING_DOS = "missing_dos"
    BAD_DATE = "bad_date"
    BAD_MONEY = "bad_money"
    MISSING_CPT = "missing_cpt"
    DUPLICATE_KEY = "duplicate_key"
    VALUE_TOO_LONG = "value_too_long"
    CONFLICTING_SNAPSHOT = "conflicting_snapshot"

    @property
    def label(self) -> str:
        return {
            RejectReason.MISSING_THERAPIST: "No therapist",
            RejectReason.UNKNOWN_THERAPIST: "Therapist not recognized",
            RejectReason.MISSING_PATIENT_NAME: "No patient name",
            RejectReason.MISSING_DOS: "No date of service",
            RejectReason.BAD_DATE: "Date could not be read",
            RejectReason.BAD_MONEY: "Amount could not be read",
            RejectReason.MISSING_CPT: "No CPT code",
            RejectReason.DUPLICATE_KEY: "Duplicate of another row",
            RejectReason.VALUE_TOO_LONG: "Value too long for its column",
            RejectReason.CONFLICTING_SNAPSHOT: "Older sheet disagrees about money",
        }[self]


class ImportError(Base):
    """A row the importer would not accept, kept for admin review.

    Rejected rows are never silently dropped. Each one records what was wrong and the
    offending raw value, so an admin can see whether it is a typo in the sheet or a
    genuine gap.

    Note that `raw_value` and `patient_hint` can carry patient identity, because the
    thing that failed to parse is often the patient's own row. That is the same data
    class already held in `sessions`. The review page is admin only and every view of
    it is audit logged as a PHI view. See ASSUMPTIONS.md A-062.
    """

    __tablename__ = "import_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sync_run_id: Mapped[int] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )

    source_row_ref: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reason: Mapped[RejectReason] = mapped_column(
        enum_column(RejectReason), nullable=False, index=True
    )
    field: Mapped[str | None] = mapped_column(String(60), nullable=True)
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Enough to find the row in the sheet without dumping every column into the table.
    patient_hint: Mapped[str | None] = mapped_column(String(120), nullable=True)
    therapist_hint: Mapped[str | None] = mapped_column(String(120), nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    resolved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[SyncRun] = relationship(lazy="selectin")

    @property
    def is_resolved(self) -> bool:
        return self.resolved_at is not None


class LookupKind(StrEnum):
    INSURANCE = "insurance"
    LOCATION = "location"
    NOTE = "note"


class Lookup(Base):
    """Long name to short code, from the workbook's Abbreviations tab.

    The mapping is many to one: several long insurance names collapse to the same
    short code, so it cannot be reversed unambiguously. Reports therefore show the
    short code, with the long names available rather than one of them presented as
    the name. See ASSUMPTIONS.md A-044.
    """

    __tablename__ = "lookups"
    __table_args__ = ()

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[LookupKind] = mapped_column(
        enum_column(LookupKind, length=20), nullable=False, index=True
    )
    long_name: Mapped[str] = mapped_column(String(200), nullable=False)
    short_code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("data_sources.id", ondelete="SET NULL"), nullable=True
    )
    imported_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)


class AppConfig(Base):
    """Admin editable settings, seeded from the environment defaults on first run.

    Key value rather than a one row table, so adding a setting is an insert instead
    of a migration.
    """

    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    updated_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
