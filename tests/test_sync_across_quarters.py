"""What happens when a second quarter's sheet is added to a database that already
holds the first one.

This is the case the practice will actually hit, and the case that was wrong in the
first build. The Q sheet's own Instructions tab specifies a rolling export window
("Appointment Date: 04/27/2026 through yesterday") rather than one snapped to quarter
boundaries, so consecutive quarterly exports are expected to overlap, and the same
visit will arrive twice. Two properties have to hold together:

  - Adding a quarter must not disturb the quarters already imported, otherwise there is
    no history to report on.
  - A visit on both sheets must be one row, otherwise every figure double counts it.

See ASSUMPTIONS.md A-022.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.data_source import DataSource, SourceProvider
from app.models.therapist import AliasSource, EmploymentType, Therapist, TherapistAlias
from app.models.visit import Visit
from app.reporting import queries
from app.sync.demo_data import Q2_HEADERS, _row  # noqa: PLC2701
from app.sync.engine import run_sync, suggest_mapping
from app.sync.sheets import SheetData

HEADERS = [str(h) if h is not None else "" for h in Q2_HEADERS]

# One visit per date, all for the same patient and therapist, all $171.05 collected.
JUNE_29 = "06/29/2026"
JUNE_30 = "06/30/2026"
JULY_06 = "07/06/2026"

PER_VISIT = Decimal("171.05")


def visit_row(dos: str) -> list[object]:
    return _row(
        "HALVORSEN", "Patient AA", "PATAA", dos, "90837", "AET", 1, "", 15.0, 15.0, 160.0, 156.05
    )


# Q2's export ends mid quarter. Q3's begins before Q2's ended, so 30 June is on both.
Q2_ROWS = [visit_row(JUNE_29), visit_row(JUNE_30)]
Q3_ROWS = [visit_row(JUNE_30), visit_row(JULY_06)]


class TwoQuarterClient:
    """Serves a different set of rows per tab, so two sources can overlap."""

    def __init__(self) -> None:
        self._tabs = {"Q2 Snapshot": Q2_ROWS, "Q3 Snapshot": Q3_ROWS}

    def list_tabs(self, spreadsheet_id: str) -> list[str]:
        return list(self._tabs)

    def read_tab(self, spreadsheet_id: str, tab_name: str, header_row: int) -> SheetData:
        rows = self._tabs[tab_name]
        return SheetData(
            headers=list(HEADERS),
            rows=[list(r) for r in rows],
            row_numbers=list(range(2, 2 + len(rows))),
        )


@pytest.fixture
def quarters(client):
    """One therapist and two sources, one per quarterly tab."""
    with client.app.state.db.session() as db:
        therapist = Therapist(
            display_name="Halvorsen", employment_type=EmploymentType.SALARIED_BENEFITS
        )
        db.add(therapist)
        db.flush()
        db.add(
            TherapistAlias(therapist_id=therapist.id, alias="HALVORSEN", source=AliasSource.MANUAL)
        )

        ids = {}
        for label, tab in (("Q2 2026", "Q2 Snapshot"), ("Q3 2026", "Q3 Snapshot")):
            source = DataSource(
                label=label,
                provider=SourceProvider.DEMO,
                tab_name=tab,
                header_row=1,
                column_mapping=suggest_mapping(HEADERS),
                active=True,
            )
            db.add(source)
            db.flush()
            ids[label] = source.id
        return ids


def sync(client, source_id: int):
    with client.app.state.db.session() as db:
        source = db.get(DataSource, source_id)
        return run_sync(db, source, TwoQuarterClient(), dry_run=False)


def stored(client) -> list[Visit]:
    with client.app.state.db.session() as db:
        return db.execute(select(Visit).order_by(Visit.dos)).scalars().all()


EXCLUSIONS = ("99998", "99999", "QBCHK", "FORM", "PRO BONO")


def summary(client, start: date, end: date) -> queries.Totals:
    with client.app.state.db.session() as db:
        return queries.totals(db, queries.Filters(start=start, end=end, cpt_exclusions=EXCLUSIONS))


# ----------------------------------------------------------------- history is kept


def test_adding_a_quarter_leaves_the_earlier_one_in_place(client, quarters):
    sync(client, quarters["Q2 2026"])
    before = {(v.dos, v.cpt) for v in stored(client)}

    sync(client, quarters["Q3 2026"])
    after = {(v.dos, v.cpt) for v in stored(client)}

    assert before <= after, "a Q2 visit disappeared when Q3 was added"
    assert (date(2026, 7, 6), "90837") in after


def test_reporting_spans_both_quarters_without_a_source_filter(client, quarters):
    """Nothing partitions visits by source, so a range that crosses quarters just works."""
    sync(client, quarters["Q2 2026"])
    sync(client, quarters["Q3 2026"])

    whole_year = summary(client, date(2026, 1, 1), date(2026, 12, 31))
    assert whole_year.sessions == 3
    assert whole_year.collected == PER_VISIT * 3

    q2_only = summary(client, date(2026, 4, 1), date(2026, 6, 30))
    q3_only = summary(client, date(2026, 7, 1), date(2026, 9, 30))
    assert q2_only.sessions == 2
    assert q3_only.sessions == 1


def test_deactivating_an_old_source_does_not_remove_its_data(client, quarters):
    """Stopping future syncs of a finished quarter must not erase the quarter."""
    sync(client, quarters["Q2 2026"])
    sync(client, quarters["Q3 2026"])

    with client.app.state.db.session() as db:
        db.get(DataSource, quarters["Q2 2026"]).active = False

    assert summary(client, date(2026, 4, 1), date(2026, 6, 30)).sessions == 2


# ----------------------------------------------------- the overlap is one visit only


def test_a_visit_on_both_sheets_is_stored_once(client, quarters):
    sync(client, quarters["Q2 2026"])
    second = sync(client, quarters["Q3 2026"])

    assert second.rows_inserted == 1, "only 6 July is new"
    assert second.rows_updated + second.rows_unchanged == 1, "30 June was already here"

    rows = stored(client)
    assert len(rows) == 3
    assert len({(v.therapist_id, v.patient_name_normalized, v.dos, v.cpt) for v in rows}) == 3


def test_the_overlapping_visit_is_not_counted_twice(client, quarters):
    """The defect this file exists for: 4 sessions and $684.20 where 3 and $513.15 are true."""
    sync(client, quarters["Q2 2026"])
    sync(client, quarters["Q3 2026"])

    result = summary(client, date(2026, 1, 1), date(2026, 12, 31))
    assert result.sessions == 3
    assert result.collected == PER_VISIT * 3


def test_the_shared_visit_keeps_one_row_whichever_order_the_sheets_arrive(client, quarters):
    """An admin may well import the newer quarter first."""
    sync(client, quarters["Q3 2026"])
    sync(client, quarters["Q2 2026"])

    assert len(stored(client)) == 3
    assert summary(client, date(2026, 1, 1), date(2026, 12, 31)).sessions == 3


def test_re_syncing_either_quarter_changes_nothing(client, quarters):
    sync(client, quarters["Q2 2026"])
    sync(client, quarters["Q3 2026"])

    for label in ("Q2 2026", "Q3 2026"):
        again = sync(client, quarters[label])
        assert again.rows_inserted == 0
        assert again.rows_updated == 0

    assert len(stored(client)) == 3


def test_provenance_records_where_a_visit_was_first_seen(client, quarters):
    """source_id and source_row_ref are the sheet and row a visit arrived on, and they
    stay that way. Reporting never reads either (A-022)."""
    sync(client, quarters["Q2 2026"])

    shared = next(v for v in stored(client) if v.dos == date(2026, 6, 30))
    assert shared.source_id == quarters["Q2 2026"]
    assert shared.source_row_ref == "3", "30 June is the second data row of the Q2 sheet"

    # 30 June is row 2 of the Q3 sheet. Neither half of the pointer may move to it,
    # because row 2 of the Q2 sheet is a different visit.
    sync(client, quarters["Q3 2026"])
    shared = next(v for v in stored(client) if v.dos == date(2026, 6, 30))
    assert (shared.source_id, shared.source_row_ref) == (quarters["Q2 2026"], "3")


def test_an_overlapping_sheet_does_not_report_a_phantom_update(client, quarters):
    """The same visit at a different row number is not a changed visit."""
    sync(client, quarters["Q2 2026"])
    second = sync(client, quarters["Q3 2026"])

    assert second.rows_updated == 0
    assert second.rows_unchanged == 1


# ------------------------------------------------------ the sync reads only its span


def test_a_sync_does_not_load_visits_outside_its_own_date_range(client, quarters):
    """Identity is global, but the pre-pass is still bounded, so a quarterly sheet does
    not pull the whole table into memory as history accumulates."""
    from app.sync.engine import _incoming_date_span, build_column_index

    positions, _ = build_column_index(HEADERS, suggest_mapping(HEADERS))
    data = TwoQuarterClient().read_tab("", "Q3 Snapshot", 1)

    earliest, latest = _incoming_date_span(data, positions)
    assert earliest == date(2026, 6, 30)
    assert latest == date(2026, 7, 6)


def test_an_undated_sheet_falls_back_to_loading_everything(client, quarters):
    """With no span to bound by, correctness beats the optimisation: load it all."""
    from app.sync.engine import _incoming_date_span, build_column_index

    positions, _ = build_column_index(HEADERS, suggest_mapping(HEADERS))
    data = SheetData(
        headers=list(HEADERS),
        rows=[visit_row("")],
        row_numbers=[2],
    )
    assert _incoming_date_span(data, positions) == (None, None)


# ------------------------------------------------------------------ the constraint


def test_the_database_itself_refuses_a_second_copy_of_one_visit(client, quarters):
    """Belt and braces: the engine avoids the duplicate, and the schema forbids it."""
    from sqlalchemy.exc import IntegrityError

    sync(client, quarters["Q2 2026"])

    with pytest.raises(IntegrityError):
        with client.app.state.db.session() as db:
            existing = db.execute(select(Visit).limit(1)).scalar_one()
            db.add(
                Visit(
                    # A different source, everything about the visit the same.
                    source_id=quarters["Q3 2026"],
                    therapist_id=existing.therapist_id,
                    patient_name=existing.patient_name,
                    patient_name_normalized=existing.patient_name_normalized,
                    dos=existing.dos,
                    cpt=existing.cpt,
                    cpt_base=existing.cpt_base,
                    total_due=Decimal("0.00"),
                    total_paid=Decimal("0.00"),
                    total_balance=Decimal("0.00"),
                )
            )


def test_no_source_column_appears_in_a_reporting_query(client, quarters):
    """The reason cross quarter history works at all: nothing filters on the sheet."""
    conditions = queries._base_conditions(  # noqa: SLF001
        queries.Filters(start=date(2026, 1, 1), end=date(2026, 12, 31), cpt_exclusions=EXCLUSIONS)
    )
    rendered = " ".join(str(c) for c in conditions)
    assert "source_id" not in rendered


def test_counts_are_unaffected_by_how_many_sources_exist(client, quarters):
    """Adding a third source with nothing new in it must not move a single figure."""
    sync(client, quarters["Q2 2026"])
    sync(client, quarters["Q3 2026"])
    baseline = summary(client, date(2026, 1, 1), date(2026, 12, 31))

    with client.app.state.db.session() as db:
        third = DataSource(
            label="Q3 2026 again",
            provider=SourceProvider.DEMO,
            tab_name="Q3 Snapshot",
            header_row=1,
            column_mapping=suggest_mapping(HEADERS),
            active=True,
        )
        db.add(third)
        db.flush()
        third_id = third.id

    sync(client, third_id)

    after = summary(client, date(2026, 1, 1), date(2026, 12, 31))
    assert after.sessions == baseline.sessions
    assert after.collected == baseline.collected
    assert db_count(client) == 3


def db_count(client) -> int:
    with client.app.state.db.session() as db:
        return db.execute(select(func.count(Visit.id))).scalar_one()
