"""The sync engine, against the synthetic workbook that mirrors the real layout."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.data_source import (
    DataSource,
    RejectReason,
    SourceProvider,
    SyncMode,
    SyncStatus,
)
from app.models.data_source import (
    ImportError as ImportErrorRow,
)
from app.models.therapist import (
    AliasSource,
    EmploymentType,
    Therapist,
    TherapistAlias,
)
from app.models.visit import Visit
from app.sync.demo_data import DEMO_TAB_NAME, DEMO_THERAPISTS, Q2_HEADERS
from app.sync.engine import build_column_index, run_sync, suggest_mapping
from app.sync.sheets import DemoSheetsClient, SheetsError, extract_spreadsheet_id

EXCLUDED = {"99998", "99999", "QBCHK", "FORM", "PRO BONO"}


@pytest.fixture
def demo_source(client):
    """A demo source with its therapists, as the admin button creates them."""
    with client.app.state.db.session() as db:
        for display_name, aliases in DEMO_THERAPISTS:
            therapist = Therapist(
                display_name=display_name,
                employment_type=EmploymentType.SALARIED_BENEFITS,
            )
            db.add(therapist)
            db.flush()
            for alias in aliases:
                db.add(
                    TherapistAlias(
                        therapist_id=therapist.id, alias=alias, source=AliasSource.MANUAL
                    )
                )

        headers = [str(h) if h is not None else "" for h in Q2_HEADERS]
        source = DataSource(
            label="Demo (synthetic)",
            provider=SourceProvider.DEMO,
            tab_name=DEMO_TAB_NAME,
            header_row=1,
            column_mapping=suggest_mapping(headers),
            active=True,
        )
        db.add(source)
        db.flush()
        return source.id


def sync(client, source_id: int, *, dry_run: bool):
    with client.app.state.db.session() as db:
        source = db.get(DataSource, source_id)
        return run_sync(db, source, DemoSheetsClient(), dry_run=dry_run)


# ------------------------------------------------------------------------- mapping


def test_suggested_mapping_covers_the_whole_allowlist():
    headers = [str(h) if h is not None else "" for h in Q2_HEADERS]
    mapping = suggest_mapping(headers)
    from app.models.data_source import IMPORT_ALLOWLIST

    assert set(mapping) == set(IMPORT_ALLOWLIST)


def test_unnamed_columns_are_not_mapped():
    """Column M holds a composite key with a patient name in it. Column N is empty."""
    headers = [str(h) if h is not None else "" for h in Q2_HEADERS]
    positions, unmapped = build_column_index(headers, suggest_mapping(headers))
    assert 12 not in positions.values()
    assert 13 not in positions.values()
    assert unmapped == []


def test_mapping_outside_the_allowlist_is_ignored_at_import():
    """Enforced at import, not only in the UI, so a hand crafted mapping cannot leak."""
    headers = ["Therapist", "BirthDate1", "HomeEmail"]
    positions, _ = build_column_index(
        headers,
        {"therapist": "Therapist", "birth_date": "BirthDate1", "email": "HomeEmail"},
    )
    assert set(positions) == {"therapist"}


def test_patient_code_position_is_found_by_header_not_index():
    """In the real sheet Patient Code is column S, not where the spec lists it."""
    headers = [str(h) if h is not None else "" for h in Q2_HEADERS]
    positions, _ = build_column_index(headers, suggest_mapping(headers))
    assert positions["patient_code"] == 18
    assert positions["recorded_flag"] == 19


# ---------------------------------------------------------------------------- runs


def test_dry_run_writes_no_visits(client, demo_source):
    result = sync(client, demo_source, dry_run=True)

    assert result.mode is SyncMode.DRY_RUN
    assert result.rows_read > 0
    assert result.rows_inserted > 0

    with client.app.state.db.session() as db:
        assert db.execute(select(func.count(Visit.id))).scalar_one() == 0


def test_dry_run_still_records_its_findings(client, demo_source):
    """A preview whose findings vanish is not a preview you can act on."""
    result = sync(client, demo_source, dry_run=True)

    with client.app.state.db.session() as db:
        rejections = (
            db.execute(select(ImportErrorRow).where(ImportErrorRow.sync_run_id == result.run_id))
            .scalars()
            .all()
        )
    assert len(rejections) == result.rows_rejected > 0


def test_live_sync_imports_the_clean_rows(client, demo_source):
    result = sync(client, demo_source, dry_run=False)

    assert result.ok
    assert result.rows_inserted > 0
    assert result.rows_rejected > 0

    with client.app.state.db.session() as db:
        assert db.execute(select(func.count(Visit.id))).scalar_one() == result.rows_inserted


def test_sync_is_idempotent(client, demo_source):
    first = sync(client, demo_source, dry_run=False)
    second = sync(client, demo_source, dry_run=False)

    assert second.rows_inserted == 0
    assert second.rows_updated == 0
    assert second.rows_unchanged == first.rows_inserted

    with client.app.state.db.session() as db:
        assert db.execute(select(func.count(Visit.id))).scalar_one() == first.rows_inserted


def test_changed_values_update_rather_than_duplicate(client, demo_source):
    sync(client, demo_source, dry_run=False)

    with client.app.state.db.session() as db:
        before = db.execute(select(func.count(Visit.id))).scalar_one()
        visit = db.execute(select(Visit).limit(1)).scalar_one()
        visit.total_paid = Decimal("1.23")

    result = sync(client, demo_source, dry_run=False)
    assert result.rows_updated == 1
    assert result.rows_inserted == 0

    with client.app.state.db.session() as db:
        assert db.execute(select(func.count(Visit.id))).scalar_one() == before


def test_formula_residue_below_the_data_is_not_imported(client, demo_source):
    """The real sheet has about 5,000 rows of dragged down formulas after the data."""
    result = sync(client, demo_source, dry_run=False)
    from app.sync.demo_data import DEMO_BROKEN_ROW_COUNT, DEMO_CLEAN_ROW_COUNT

    assert result.rows_read == DEMO_CLEAN_ROW_COUNT + DEMO_BROKEN_ROW_COUNT

    with client.app.state.db.session() as db:
        zero_rows = db.execute(
            select(func.count(Visit.id)).where(Visit.patient_name == "")
        ).scalar_one()
    assert zero_rows == 0


def test_run_records_the_date_range_and_status(client, demo_source):
    result = sync(client, demo_source, dry_run=False)

    assert result.date_min == date(2026, 4, 1)
    assert result.date_max is not None and result.date_max >= date(2026, 5, 7)

    with client.app.state.db.session() as db:
        source = db.get(DataSource, demo_source)
        assert source.coverage_start == result.date_min
        assert source.coverage_end == result.date_max
        assert source.last_synced_at is not None

        from app.models.data_source import SyncRun

        run = db.get(SyncRun, result.run_id)
        assert run.status is SyncStatus.SUCCESS
        assert run.mode is SyncMode.LIVE


# --------------------------------------------------------------------- rejections


def test_every_failure_mode_is_rejected_with_its_reason(client, demo_source):
    result = sync(client, demo_source, dry_run=False)
    reasons = {r.reason for r in result.rejections}

    assert RejectReason.MISSING_DOS in reasons
    assert RejectReason.BAD_DATE in reasons
    assert RejectReason.BAD_MONEY in reasons
    assert RejectReason.UNKNOWN_THERAPIST in reasons
    assert RejectReason.MISSING_PATIENT_NAME in reasons


def test_unparseable_money_keeps_the_raw_string(client, demo_source):
    result = sync(client, demo_source, dry_run=False)
    bad_money = next(r for r in result.rejections if r.reason is RejectReason.BAD_MONEY)
    assert "see note" in (bad_money.raw_value or "")


def test_unknown_therapist_is_never_auto_created(client, demo_source):
    """A wrong merge is invisible once it happens. Suggest, never create."""
    result = sync(client, demo_source, dry_run=False)

    with client.app.state.db.session() as db:
        names = set(db.execute(select(Therapist.display_name)).scalars().all())
    assert "UNKNOWNPERSON" not in names
    assert len(names) == len(DEMO_THERAPISTS)

    unknown = next(r for r in result.rejections if r.reason is RejectReason.UNKNOWN_THERAPIST)
    assert "UNKNOWNPERSON" in (unknown.raw_value or "")


def test_rejected_rows_carry_a_pointer_back_to_the_sheet(client, demo_source):
    result = sync(client, demo_source, dry_run=False)
    assert all(r.row_ref for r in result.rejections)


def test_rejections_are_never_silently_dropped(client, demo_source):
    result = sync(client, demo_source, dry_run=False)

    with client.app.state.db.session() as db:
        stored = db.execute(
            select(func.count(ImportErrorRow.id)).where(ImportErrorRow.sync_run_id == result.run_id)
        ).scalar_one()
    assert stored == result.rows_rejected


# ------------------------------------------------------------------ alias matching


def test_valant_style_name_resolves_through_an_alias(client, demo_source):
    """ "Rosalind Wren, LMFT (R.Wren)" and "WREN" are one therapist."""
    sync(client, demo_source, dry_run=False)

    with client.app.state.db.session() as db:
        wren = db.execute(select(Therapist).where(Therapist.display_name == "Wren")).scalar_one()
        count = db.execute(
            select(func.count(Visit.id)).where(Visit.therapist_id == wren.id)
        ).scalar_one()
    # Five plain WREN rows plus the one written the Valant way.
    assert count >= 6


def test_alias_is_globally_unique_so_two_therapists_cannot_share_one(client, demo_source):
    """The constraint that makes a silent PAVLOVA / ROSENFELD style merge impossible."""
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        with client.app.state.db.session() as db:
            other = Therapist(display_name="Someone Else")
            db.add(other)
            db.flush()
            db.add(TherapistAlias(therapist_id=other.id, alias="WREN"))


# ------------------------------------------------------------------- derived data


def test_cancellations_are_imported_but_are_not_sessions(client, demo_source):
    sync(client, demo_source, dry_run=False)

    with client.app.state.db.session() as db:
        rows = db.execute(select(Visit)).scalars().all()

    cancellations = [v for v in rows if v.cpt_base in {"99998", "99999"}]
    assert cancellations, "the cancellation codes must still be imported"

    sessions = [v for v in rows if v.cpt_base not in EXCLUDED]
    assert all(v.cpt_base not in {"99998", "99999"} for v in sessions)


def test_no_show_fees_stay_in_revenue(client, demo_source):
    """Excluding a code from counting does not make its money unreal (A-031)."""
    sync(client, demo_source, dry_run=False)

    with client.app.state.db.session() as db:
        rows = db.execute(select(Visit)).scalars().all()

    fee_revenue = sum(v.total_paid for v in rows if v.cpt_base == "99999")
    assert fee_revenue > 0

    collected_all = sum(v.total_paid for v in rows)
    collected_sessions_only = sum(v.total_paid for v in rows if v.cpt_base not in EXCLUDED)
    assert collected_all > collected_sessions_only


def test_money_round_trips_exactly(client, demo_source):
    sync(client, demo_source, dry_run=False)

    with client.app.state.db.session() as db:
        visit = db.execute(
            select(Visit).where(Visit.paid_by_ins == Decimal("156.05")).limit(1)
        ).scalar_one()
    assert visit.paid_by_ins == Decimal("156.05")
    assert isinstance(visit.paid_by_ins, Decimal)


def test_blank_patient_code_still_imports(client, demo_source):
    """Patient Code is blank on 41 percent of the real sheet."""
    sync(client, demo_source, dry_run=False)

    with client.app.state.db.session() as db:
        blank = db.execute(
            select(func.count(Visit.id)).where(Visit.patient_code.is_(None))
        ).scalar_one()
    assert blank >= 2


def test_blank_insurance_is_null_not_self_pay(client, demo_source):
    sync(client, demo_source, dry_run=False)

    with client.app.state.db.session() as db:
        nulls = db.execute(
            select(func.count(Visit.id)).where(Visit.insurance_short.is_(None))
        ).scalar_one()
        self_pay = db.execute(
            select(func.count(Visit.id)).where(Visit.insurance_short == "SP")
        ).scalar_one()
    assert nulls >= 1
    assert self_pay >= 1


def test_suffixed_cpts_group_with_their_base_code(client, demo_source):
    sync(client, demo_source, dry_run=False)

    with client.app.state.db.session() as db:
        adhd_rows = db.execute(select(Visit).where(Visit.cpt.like("%ADHD%"))).scalars().all()
    assert adhd_rows
    assert {v.cpt_base for v in adhd_rows} <= {"90791", "90837"}


# ------------------------------------------------------------------------ safety


def test_raw_tabs_are_refused(client, demo_source):
    with client.app.state.db.session() as db:
        source = db.get(DataSource, demo_source)
        source.tab_name = "RAW_Appointments"
        result = run_sync(db, source, DemoSheetsClient(), dry_run=True)

    assert not result.ok
    assert "dates of birth" in (result.error_message or "")


def test_raw_tabs_are_not_offered_for_selection():
    from app.sync.sheets import selectable_tabs

    tabs = DemoSheetsClient().list_tabs("")
    assert "RAW_Appointments" in tabs
    assert "RAW_Appointments" not in selectable_tabs(tabs)


def test_incomplete_mapping_fails_before_reading(client, demo_source):
    with client.app.state.db.session() as db:
        source = db.get(DataSource, demo_source)
        source.column_mapping = {"therapist": "Therapist"}
        result = run_sync(db, source, DemoSheetsClient(), dry_run=True)

    assert not result.ok
    assert "mapping is incomplete" in (result.error_message or "")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://docs.google.com/spreadsheets/d/1Pft_PfZtdnU2d_Lhtc89U-323M8k3Z6m-f44s78rBHY/edit?usp=sharing",
            "1Pft_PfZtdnU2d_Lhtc89U-323M8k3Z6m-f44s78rBHY",
        ),
        (
            "https://docs.google.com/spreadsheets/d/abc123/edit#gid=0",
            "abc123",
        ),
        ("abc123", "abc123"),
    ],
)
def test_spreadsheet_id_extraction(raw, expected):
    assert extract_spreadsheet_id(raw) == expected


def test_spreadsheet_id_extraction_rejects_nonsense():
    with pytest.raises(SheetsError):
        extract_spreadsheet_id("")
    with pytest.raises(SheetsError):
        extract_spreadsheet_id("https://example.invalid/some/other/thing")


# ----------------------------------------------------------- error supersession


def open_errors(client, source_id: int) -> list[ImportErrorRow]:
    with client.app.state.db.session() as db:
        return (
            db.execute(
                select(ImportErrorRow).where(
                    ImportErrorRow.source_id == source_id,
                    ImportErrorRow.resolved_at.is_(None),
                )
            )
            .scalars()
            .all()
        )


def test_a_new_run_supersedes_the_previous_runs_errors(client, demo_source):
    """Each run's list is the current account of the source, so the previous run's
    unresolved errors are stale copies of it. Without this, every sync appended its
    rejections and a 6,766 row problem doubled on the second look."""
    first = sync(client, demo_source, dry_run=False)
    second = sync(client, demo_source, dry_run=False)

    assert second.superseded_errors == first.rows_rejected

    remaining = open_errors(client, demo_source)
    assert len(remaining) == second.rows_rejected
    assert {r.sync_run_id for r in remaining} == {second.run_id}


def test_superseded_errors_are_resolved_not_deleted(client, demo_source):
    """Never silently dropped still holds: the stale rows stay, marked with the run
    whose account replaced them."""
    first = sync(client, demo_source, dry_run=False)
    second = sync(client, demo_source, dry_run=False)

    with client.app.state.db.session() as db:
        stale = (
            db.execute(select(ImportErrorRow).where(ImportErrorRow.sync_run_id == first.run_id))
            .scalars()
            .all()
        )
    assert len(stale) == first.rows_rejected
    for entry in stale:
        assert entry.resolved_at is not None
        assert f"run {second.run_id}" in (entry.resolution_note or "")


def test_a_dry_run_also_supersedes(client, demo_source):
    """Admins re-run dry runs while debugging a sheet, which is exactly when the pile
    grows fastest. A completed read is a completed read."""
    first = sync(client, demo_source, dry_run=True)
    sync(client, demo_source, dry_run=True)

    remaining = open_errors(client, demo_source)
    assert first.run_id not in {r.sync_run_id for r in remaining}
    assert len(remaining) == first.rows_rejected  # same sheet, same account


def test_a_failed_run_supersedes_nothing(client, demo_source):
    """A run that read nothing produced no account, so it replaces nothing."""
    live = sync(client, demo_source, dry_run=False)
    assert live.rows_rejected > 0

    with client.app.state.db.session() as db:
        source = db.get(DataSource, demo_source)
        source.column_mapping = {"therapist": "Therapist"}
        failed = run_sync(db, source, DemoSheetsClient(), dry_run=True)

    assert not failed.ok
    assert failed.superseded_errors == 0
    assert len(open_errors(client, demo_source)) == live.rows_rejected


def test_supersession_is_scoped_to_its_own_source(client, demo_source):
    """Another source's open errors are its own account and must not be touched."""
    from app.models.data_source import SyncRun

    with client.app.state.db.session() as db:
        other = DataSource(
            label="Other", provider=SourceProvider.DEMO, tab_name="t", column_mapping={}
        )
        db.add(other)
        db.flush()
        other_run = SyncRun(source_id=other.id, mode=SyncMode.LIVE)
        db.add(other_run)
        db.flush()
        db.add(
            ImportErrorRow(
                sync_run_id=other_run.id,
                source_id=other.id,
                reason=RejectReason.MISSING_DOS,
                source_row_ref="2",
            )
        )
        other_id = other.id

    sync(client, demo_source, dry_run=False)
    sync(client, demo_source, dry_run=False)

    assert len(open_errors(client, other_id)) == 1


def test_a_manually_reviewed_error_is_not_relabelled(client, demo_source):
    """A human's resolution note is a record of a decision. Supersession only touches
    rows that nobody resolved."""
    from app.models.types import utcnow

    first = sync(client, demo_source, dry_run=False)

    with client.app.state.db.session() as db:
        entry = db.execute(
            select(ImportErrorRow).where(ImportErrorRow.sync_run_id == first.run_id).limit(1)
        ).scalar_one()
        entry.resolved_at = utcnow()
        entry.resolution_note = "Expected, per the practice manager."
        reviewed_id = entry.id

    sync(client, demo_source, dry_run=False)

    with client.app.state.db.session() as db:
        entry = db.get(ImportErrorRow, reviewed_id)
        assert entry.resolution_note == "Expected, per the practice manager."


def test_supersession_stops_at_the_last_row_the_run_examined(client, demo_source):
    """The read stops at the first identity-blank row, so a successful run is not
    proof the whole sheet was seen. An error past the last row read keeps its place
    in the review queue rather than being resolved by a read that never reached it."""
    from app.models.data_source import SyncRun
    from app.sync.engine import _supersede_stale_errors

    with client.app.state.db.session() as db:
        early_run = SyncRun(source_id=demo_source, mode=SyncMode.LIVE)
        db.add(early_run)
        db.flush()
        for ref in ("3", "5000", None):
            db.add(
                ImportErrorRow(
                    sync_run_id=early_run.id,
                    source_id=demo_source,
                    reason=RejectReason.MISSING_DOS,
                    source_row_ref=ref,
                )
            )
        current = SyncRun(source_id=demo_source, mode=SyncMode.LIVE)
        db.add(current)
        db.flush()

        # The current run examined sheet rows up to 100 only.
        n = _supersede_stale_errors(db, demo_source, current.id, last_examined_row=100)
        assert n == 1

        remaining = (
            db.execute(select(ImportErrorRow).where(ImportErrorRow.resolved_at.is_(None)))
            .scalars()
            .all()
        )
    # Row 5000 was never re-read, and the row without a number cannot be placed,
    # so both stay open. Only row 3 is covered by the new account.
    assert sorted(r.source_row_ref or "none" for r in remaining) == ["5000", "none"]


def test_an_older_read_never_supersedes_a_newer_account(client, demo_source):
    """With two overlapping syncs, the run with the lower id may finish second. Its
    read is still the older one, so it must not mark the newer list as stale."""
    from app.models.data_source import SyncRun
    from app.sync.engine import _supersede_stale_errors

    with client.app.state.db.session() as db:
        older = SyncRun(source_id=demo_source, mode=SyncMode.LIVE)
        newer = SyncRun(source_id=demo_source, mode=SyncMode.LIVE)
        db.add_all([older, newer])
        db.flush()
        db.add(
            ImportErrorRow(
                sync_run_id=newer.id,
                source_id=demo_source,
                reason=RejectReason.MISSING_DOS,
                source_row_ref="3",
            )
        )

        # The older run finishes second and tries to supersede.
        assert _supersede_stale_errors(db, demo_source, older.id, last_examined_row=9999) == 0
