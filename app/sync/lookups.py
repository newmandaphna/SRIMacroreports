"""Importing the workbook's Abbreviations and Config tabs.

Both are reference data the practice already maintains by hand, so the app reads them
rather than inventing its own. Imported on demand from an admin button, not on every
sync, because they change once a quarter at most.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.data_source import DataSource, Lookup, LookupKind
from app.models.therapist import (
    AliasSource,
    Therapist,
    TherapistAlias,
    normalize_therapist_name,
)
from app.sync import normalize
from app.sync.sheets import SheetsClient

logger = logging.getLogger(__name__)

ABBREVIATIONS_COLUMNS: tuple[tuple[str, str, LookupKind], ...] = (
    ("Insurance Company Name", "Ins - Short", LookupKind.INSURANCE),
    ("Locations", "Loc - Short", LookupKind.LOCATION),
    ("Note Codes", "Notes - Short", LookupKind.NOTE),
)


@dataclass
class LookupImportResult:
    imported: int = 0
    skipped: int = 0
    by_kind: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.by_kind is None:
            self.by_kind = {}


@dataclass
class AliasImportResult:
    created_aliases: int = 0
    matched_therapists: int = 0
    # Overrides naming a therapist the app does not have. Reported rather than
    # auto created, because creating a therapist from a typo is worse than a warning.
    unmatched: list[str] = None  # type: ignore[assignment]
    # Overrides refused because the alias already resolves to a different therapist.
    # This is the guard that stops two people being folded into one record.
    conflicts: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.unmatched is None:
            self.unmatched = []
        if self.conflicts is None:
            self.conflicts = []


def import_abbreviations(
    db: Session, source: DataSource, client: SheetsClient, tab_name: str
) -> LookupImportResult:
    """Replace this source's lookups from its Abbreviations tab.

    Replace rather than merge: the tab is the practice's current answer, and a stale
    row that has been deleted upstream should not survive here.
    """
    data = client.read_tab(source.spreadsheet_id or "", tab_name, 1)
    headers = [h.strip().lower() for h in data.headers]

    result = LookupImportResult()

    db.execute(delete(Lookup).where(Lookup.source_id == source.id))

    seen: set[tuple[str, str]] = set()

    for long_header, short_header, kind in ABBREVIATIONS_COLUMNS:
        try:
            long_index = headers.index(long_header.lower())
            short_index = headers.index(short_header.lower())
        except ValueError:
            logger.info("Abbreviations tab has no %r column, skipping", long_header)
            continue

        for row in data.rows:
            long_name = normalize.clean_text(row[long_index])
            short_code = normalize.clean_text(row[short_index]).upper()
            if not long_name or not short_code:
                result.skipped += 1
                continue

            dedupe_key = (kind.value, long_name.lower())
            if dedupe_key in seen:
                result.skipped += 1
                continue
            seen.add(dedupe_key)

            db.add(
                Lookup(
                    kind=kind,
                    long_name=long_name,
                    short_code=short_code,
                    source_id=source.id,
                )
            )
            result.imported += 1
            result.by_kind[kind.value] = result.by_kind.get(kind.value, 0) + 1

    return result


def import_provider_aliases(
    db: Session, source: DataSource, client: SheetsClient, tab_name: str, *, actor_id: int | None
) -> AliasImportResult:
    """Read PROVIDER rows from the workbook's Config tab into therapist aliases.

    The tab's own semantics are "Raw Text Contains", but this app matches on the whole
    normalized name instead. That difference is deliberate and load bearing: a rule
    written as just `Rosenfeld` would match both Inna Pavlova-Rosenfeld and the
    unrelated therapist ROSENFELD, who are confirmed to be different people, and would
    silently merge them. See ASSUMPTIONS.md A-040a.

    An alias that already points at a different therapist is refused and reported, not
    reassigned.
    """
    data = client.read_tab(source.spreadsheet_id or "", tab_name, 1)
    headers = [h.strip().lower() for h in data.headers]

    try:
        type_index = headers.index("type")
        raw_index = headers.index("raw text contains")
        output_index = headers.index("output")
    except ValueError as exc:
        from app.sync.sheets import SheetsError

        raise SheetsError(
            "That tab does not look like the Config tab. It needs Type, "
            "Raw Text Contains, and Output columns."
        ) from exc

    result = AliasImportResult()

    by_display = {
        normalize_therapist_name(name): tid
        for name, tid in db.execute(select(Therapist.display_name, Therapist.id)).all()
    }
    existing_aliases = {
        alias: tid
        for alias, tid in db.execute(
            select(TherapistAlias.alias, TherapistAlias.therapist_id)
        ).all()
    }

    for row in data.rows:
        if normalize.clean_text(row[type_index]).upper() != "PROVIDER":
            continue

        raw_text = normalize.clean_text(row[raw_index])
        output = normalize.clean_text(row[output_index])
        if not raw_text or not output:
            continue

        alias = normalize_therapist_name(raw_text)
        target_key = normalize_therapist_name(output)
        therapist_id = by_display.get(target_key) or existing_aliases.get(target_key)

        if therapist_id is None:
            result.unmatched.append(f"{raw_text} -> {output}")
            continue

        result.matched_therapists += 1

        owner = existing_aliases.get(alias)
        if owner == therapist_id:
            continue
        if owner is not None:
            result.conflicts.append(
                f"{raw_text} already resolves to a different therapist, left unchanged"
            )
            continue

        db.add(
            TherapistAlias(
                therapist_id=therapist_id,
                alias=alias,
                source=AliasSource.SHEET_CONFIG_TAB,
                created_by_id=actor_id,
            )
        )
        existing_aliases[alias] = therapist_id
        result.created_aliases += 1

    return result
