"""In development: what is still open, and what it changes.

This page exists because the caveats that qualify a number belong where the number is
read, not in a document nobody opens. Everything on it is computed from the current
state of the database rather than hardcoded, so an item disappears when it is genuinely
resolved and nobody has to remember to delete it.

Visible to every signed in user. It holds no patient information: counts, flags, and
questions only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select

from app import config_store
from app.models.data_source import ImportError as ImportErrorRow
from app.models.data_source import RejectReason
from app.models.therapist import EmploymentType, Therapist
from app.models.visit import Visit
from app.security.deps import CurrentUser, DbSession
from app.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(tags=["status"])


@dataclass
class OpenItem:
    """One thing that is still undecided, and what turns on it."""

    title: str
    detail: str
    # What changes in the application once this is answered.
    consequence: str
    # "waiting" on someone outside the app, or "ready" for an admin to act on here.
    kind: str = "waiting"
    action_label: str | None = None
    action_href: str | None = None


@dataclass
class StatusReport:
    open_items: list[OpenItem] = field(default_factory=list)
    caveats: list[OpenItem] = field(default_factory=list)
    not_built: list[OpenItem] = field(default_factory=list)

    @property
    def open_count(self) -> int:
        return len(self.open_items)


def build_status(db: DbSession, request: Request, is_admin: bool) -> StatusReport:
    settings = request.app.state.settings
    config = config_store.load(db, settings)
    report = StatusReport()

    # --- the benefits threshold -------------------------------------------------
    threshold_rows = db.execute(
        select(Therapist.employment_type, func.count(Therapist.id))
        .where(Therapist.active.is_(True))
        .group_by(Therapist.employment_type)
    ).all()
    by_type = {EmploymentType(t): n for t, n in threshold_rows}
    salaried = by_type.get(EmploymentType.SALARIED_BENEFITS, 0)
    unmeasured = sum(n for t, n in by_type.items() if not t.counts_against_threshold)

    if config.benefits_session_threshold == settings.benefits_session_threshold:
        report.open_items.append(
            OpenItem(
                title="The weekly session threshold has never been confirmed",
                detail=(
                    f"It is still the placeholder default of "
                    f"{config.benefits_session_threshold} sessions per week. On the Q2 data the "
                    "median therapist runs about 17 a week, so a threshold of 25 puts most of "
                    "the practice in the alert state, which teaches everyone to ignore the "
                    "colour."
                ),
                consequence=(
                    "Decides who appears as below threshold on the utilization board and on "
                    "the overview."
                ),
                kind="ready" if is_admin else "waiting",
                action_label="Set the threshold" if is_admin else None,
                action_href="/admin/config" if is_admin else None,
            )
        )

    if unmeasured:
        report.open_items.append(
            OpenItem(
                title=f"{unmeasured} therapist{'' if unmeasured == 1 else 's'} are not measured "
                "against any threshold",
                detail=(
                    f"{salaried} are marked salaried with benefits and {unmeasured} are not, so "
                    "only the salaried ones are compared against the threshold at all. A "
                    "therapist left on the default employment type carries no status, which "
                    "looks the same as one who has no session minimum."
                ),
                consequence="Decides who appears on the utilization board at all.",
                kind="ready" if is_admin else "waiting",
                action_label="Set employment types" if is_admin else None,
                action_href="/admin/therapists" if is_admin else None,
            )
        )

    # --- rows the sheet cannot place in a period --------------------------------
    undated = db.execute(
        select(func.count(ImportErrorRow.id)).where(
            ImportErrorRow.reason.in_([RejectReason.MISSING_DOS, RejectReason.BAD_DATE]),
            ImportErrorRow.resolved_at.is_(None),
        )
    ).scalar_one()
    if undated:
        report.caveats.append(
            OpenItem(
                title=f"{undated} row{'' if undated == 1 else 's'} have no usable date of service",
                detail=(
                    "A row with no date cannot be placed in a week, a month, or a quarter, so "
                    "it is excluded from every figure on every page. Adding the dates in the Q "
                    "sheet and syncing again brings them in."
                ),
                consequence=(
                    "Session counts and revenue are understated by whatever those rows hold."
                ),
                kind="ready" if is_admin else "waiting",
                action_label="Review import errors" if is_admin else None,
                action_href="/admin/sources" if is_admin else None,
            )
        )

    unresolved = db.execute(
        select(func.count(ImportErrorRow.id)).where(ImportErrorRow.resolved_at.is_(None))
    ).scalar_one()
    if unresolved > undated:
        report.caveats.append(
            OpenItem(
                title=f"{unresolved} imported rows were rejected and not yet reviewed",
                detail=(
                    "Rejected rows are never silently dropped, but they are also not counted. "
                    "Each one carries the reason it was refused."
                ),
                consequence="Anything they contain is missing from the reports.",
                kind="ready" if is_admin else "waiting",
                action_label="Review them" if is_admin else None,
                action_href="/admin/sources" if is_admin else None,
            )
        )

    # --- caveats that hold regardless of the data -------------------------------
    has_data = db.execute(select(func.count(Visit.id))).scalar_one() > 0
    if has_data:
        report.caveats.append(
            OpenItem(
                title="Cancellation rates are not comparable between therapists",
                detail=(
                    "Recorded cancellation rates across the practice range from under 1 percent "
                    "to nearly 40 percent. A difference that large between colleagues is far "
                    "likelier to be inconsistent recording than real patient behaviour, so the "
                    "practice wide rate is shown without qualification and per therapist rates "
                    "are not presented as a comparison."
                ),
                consequence=(
                    "Needs someone to confirm whether every therapist records cancellations the "
                    "same way in Valant."
                ),
            )
        )
        report.caveats.append(
            OpenItem(
                title="Cancellation fees are counted as revenue, not as sessions",
                detail=(
                    "CPT 99999 is a cancellation with a no show fee charged, and 99998 is a "
                    "cancellation with no fee. Neither counts as a session, because no clinical "
                    "service was delivered, but the fees are real money and stay in the revenue "
                    "figures. No show fee income is broken out separately on the overview."
                ),
                consequence="Explains why revenue per session is higher than a session rate.",
            )
        )

    # --- what is not built ------------------------------------------------------
    report.not_built.append(
        OpenItem(
            title="Patient level funnel",
            detail=(
                "AR aging by account, new patient volume, and no show patterns. This is the "
                "only module that would show patient names and account balances, so it is held "
                "until the practice owner confirms both that it is wanted and what it should "
                "contain."
            ),
            consequence="Not started. Nothing about it is reachable in the application.",
        )
    )

    if not settings.room_utilization_enabled:
        report.not_built.append(
            OpenItem(
                title="Room utilization",
                detail=(
                    "Built, but switched off. The practice has schedules and no system that "
                    "records which rooms were actually used, so the module accepts a manual "
                    "upload rather than deriving usage from appointments. Turning it on is a "
                    "deployment setting."
                ),
                consequence="Absent from the application entirely while the flag is off.",
            )
        )

    return report


@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request, db: DbSession, auth: CurrentUser) -> Response:
    report = build_status(db, request, auth.role.is_admin)
    return render(
        request,
        "status.html",
        {"page_title": "In development", "auth": auth, "report": report},
    )
