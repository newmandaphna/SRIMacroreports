"""Temporary reproduction of the unbounded unknown-therapist callout. Delete after use."""

from __future__ import annotations

import re
import time

from tests.test_admin_sources import admin_client, csrf, demo  # noqa: F401
from app.models.data_source import (
    ImportError as ImportErrorRow,
)
from app.models.data_source import (
    RejectReason,
    SyncMode,
    SyncRun,
    SyncStatus,
)

N = 1000


def test_unknown_therapist_callout_is_unbounded(admin_client, demo):
    # Seed a run mirroring the documented real-sheet failure: every row rejects
    # as UNKNOWN_THERAPIST with a distinct raw value (per-row data in the column).
    with admin_client.app.state.db.session() as db:
        run = SyncRun(
            source_id=demo,
            mode=SyncMode.DRY_RUN,
            status=SyncStatus.SUCCESS,
            rows_read=N,
            rows_rejected=N,
        )
        db.add(run)
        db.flush()
        run_id = run.id
        for i in range(N):
            db.add(
                ImportErrorRow(
                    sync_run_id=run_id,
                    source_id=demo,
                    source_row_ref=str(i + 2),
                    reason=RejectReason.UNKNOWN_THERAPIST,
                    field="therapist",
                    raw_value=f"Patient Name {i:04d}",
                    detail="No therapist matches. Create one, or add an alias.",
                    patient_hint=f"Patient Name {i:04d}",
                    therapist_hint=f"Patient Name {i:04d}",
                )
            )
        db.commit()

    t0 = time.monotonic()
    resp = admin_client.get(f"/admin/sources/{demo}/runs/{run_id}")
    elapsed = time.monotonic() - t0
    assert resp.status_code == 200
    page = resp.text

    # The callout block is everything between its opening alert and the systemic banner.
    m = re.search(r"unrecognized therapist name", page)
    assert m, "callout did not render at all"

    create_links = page.count("Create therapist →")
    li_items = page.count("<li")
    table_rows = page.count("Mark reviewed")
    systemic = "rejected for the same reason" in page

    header = re.search(r"<strong>([\d,]+) unrecognized therapist name", page)
    print(f"\npage size: {len(page):,} bytes, rendered in {elapsed:.2f}s")
    print(f"callout header count: {header.group(1) if header else '??'}")
    print(f"'Create therapist' links: {create_links}")
    print(f"<li> items on page: {li_items}")
    print(f"rejection table rows (Mark reviewed buttons): {table_rows}")
    print(f"systemic banner shown: {systemic}")

    # The finding's claims:
    assert table_rows == 200, "table was expected to cap at REJECTION_PAGE_SIZE=200"
    assert create_links == N, f"expected {N} uncapped callout links, got {create_links}"
    assert systemic, "systemic banner expected to fire at the same time"
