"""How each figure was calculated, generated from the calculation itself.

Every headline number in this application can explain itself: the definition in
plain words, the arithmetic that produced the exact figure on screen, which rows
were counted and which were set aside, and the caveats that apply.

The design rule that makes it trustworthy is that a Derivation holds two
independent expressions of the same number:

  `value`      read straight off the attribute the page prints
  `recomputed` evaluated from the terms and components stated in the explanation

A test asserts they agree, on real rows, across several date ranges. So a change
to how a figure is calculated that does not also update its stated arithmetic
turns that test red. A wrong explanation is worse than no explanation, because it
teaches a reader to trust a number they should have questioned.

Nothing here selects a patient column. Therapist names appear only when the reader
already holds the utilization grant, decided at build time by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config_store import PracticeConfig
from app.models.therapist import Therapist
from app.reporting import queries
from app.reporting.periods import Granularity

ZERO = Decimal("0.00")

# How close the stated arithmetic must land to the printed figure. Not zero,
# because a rate is quantized for display and its components are not, so exact
# equality would fail for honest reasons. Wide enough to tolerate rounding, far
# too tight to hide a real disagreement.
TOLERANCE = Decimal("0.05")


@dataclass(frozen=True)
class Term:
    """One named input in the arithmetic, with the value that went in."""

    label: str
    value: Decimal | int
    kind: str = "count"  # count, money, or rate


@dataclass(frozen=True)
class Component:
    """One period's contribution, for the evidence table."""

    label: str
    value: Decimal | int


@dataclass(frozen=True)
class Census:
    """What the row count was made of, so a reader can reconcile it."""

    imported: int
    counted: int
    excluded: int
    excluded_note: str

    @property
    def reconciles(self) -> bool:
        return self.counted + self.excluded == self.imported


@dataclass
class Derivation:
    """One figure, with everything needed to check it."""

    key: str
    title: str
    value: Decimal | int | None
    kind: str
    sentence: str
    operation: str
    window: str
    terms: list[Term] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    # False for rates and averages, whose period contributions genuinely do not
    # add up to the whole. Saying so beats presenting a column that does not sum.
    components_sum_to_value: bool = True
    components_note: str = ""
    census: Census | None = None
    caveats: list[Term] | list[str] = field(default_factory=list)
    unavailable_because: str = ""

    @property
    def recomputed(self) -> Decimal | None:
        """The figure evaluated from the stated arithmetic alone.

        Deliberately ignores `value`: this is the second, independent expression
        that the drift test compares against the first.
        """
        if self.value is None:
            return None
        if self.operation == "sum" and self.components:
            return sum((Decimal(str(c.value)) for c in self.components), Decimal(0))
        if self.operation == "divide" and len(self.terms) >= 2:
            denominator = Decimal(str(self.terms[1].value))
            if denominator == 0:
                return None
            scale = Decimal(100) if self.kind == "rate" else Decimal(1)
            return Decimal(str(self.terms[0].value)) / denominator * scale
        if self.operation == "subtract" and len(self.terms) >= 2:
            return Decimal(str(self.terms[0].value)) - Decimal(str(self.terms[1].value))
        return None

    @property
    def discrepancy(self) -> Decimal | None:
        """How far the stated arithmetic lands from the printed figure.

        None when there is nothing to compare. Surfaced in the page rather than
        swallowed: a mismatch is exactly the thing a reader needs to know.
        """
        recomputed = self.recomputed
        if recomputed is None or self.value is None:
            return None
        return (recomputed - Decimal(str(self.value))).copy_abs()

    @property
    def agrees(self) -> bool:
        gap = self.discrepancy
        return gap is None or gap <= TOLERANCE


# --------------------------------------------------------------------------- text


def _window_words(filters: queries.Filters, *, therapist_names: list[str] | None = None) -> str:
    """The window as the query actually scoped it, filters included.

    The date range was all this said, while the query it describes also applies the
    therapist and location filters. On a filtered page the explanation therefore
    described a different population from the figure above it, which is the one thing
    this module exists to prevent. Named therapists appear only when the reader is
    allowed to see therapist names at all; otherwise they are counted.
    """
    parts = [
        f"{filters.start.strftime('%-d %b %Y')} to {filters.end.strftime('%-d %b %Y')}, "
        "by date of service"
    ]
    if filters.therapist_ids:
        if therapist_names:
            parts.append("limited to " + ", ".join(therapist_names))
        else:
            count = len(filters.therapist_ids)
            parts.append(f"limited to {count} selected therapist{'s' if count != 1 else ''}")
    if filters.locations:
        named = [loc or "unknown" for loc in filters.locations]
        parts.append("limited to " + ", ".join(named))
    return "; ".join(parts)


def _exclusion_sentence(filters: queries.Filters) -> str:
    """Describes the list this query actually applied.

    Deliberately the filters and not the stored configuration: the explanation must
    describe what produced the number in front of the reader. A test caught these
    two disagreeing, which is exactly the drift this module exists to prevent.
    """
    if not filters.cpt_exclusions:
        return "No CPT codes are currently excluded, so every imported row counts as a session."
    codes = ", ".join(filters.cpt_exclusions)
    return (
        f"These CPT codes do not count as sessions: {codes}. Their money still counts "
        "toward revenue, because a cancellation fee is real money."
    )


def _money_caveat() -> str:
    return (
        "Money is whatever the source sheet held at the last sync. The sheet carries no "
        "payment or claim dates, so nothing here can say when money actually arrived."
    )


# ------------------------------------------------------------------- the registry

# Ordered, because this drives both the links on the pages and a test that every
# figure the pages offer is actually explainable.
EXPLAINABLE: tuple[str, ...] = (
    "collected",
    "sessions",
    "outstanding",
    "billed",
    "below_threshold",
    "collection_rate",
    "cancellation_rate",
    "revenue_per_session",
    "psychiatry_split",
    "no_show_fee_revenue",
)

TITLES: dict[str, str] = {
    "collected": "Revenue collected",
    "sessions": "Sessions",
    "outstanding": "Outstanding",
    "billed": "Billed",
    "below_threshold": "Providers below their expectation",
    "collection_rate": "Collection rate",
    "cancellation_rate": "Cancellation rate",
    "revenue_per_session": "Revenue per session",
    "psychiatry_split": "Therapy and psychiatry split",
    "no_show_fee_revenue": "No show fee income",
}


def build_derivation(
    db: Session,
    key: str,
    *,
    filters: queries.Filters,
    config: PracticeConfig,
    granularity: Granularity,
    may_see_therapists: bool = False,
) -> Derivation | None:
    """The derivation for one figure, computed against the caller's own filters.

    Returns None for an unknown key so a mistyped URL is a 404 rather than a
    stack trace.
    """
    if key not in EXPLAINABLE:
        return None

    totals = queries.totals(db, filters)
    therapist_names = None
    if may_see_therapists and filters.therapist_ids:
        therapist_names = [
            name
            for name in db.execute(
                select(Therapist.display_name)
                .where(Therapist.id.in_(filters.therapist_ids))
                .order_by(func.lower(Therapist.display_name))
            ).scalars()
        ]
    window = _window_words(filters, therapist_names=therapist_names)
    census = Census(
        imported=totals.visits,
        counted=totals.sessions,
        excluded=totals.excluded_rows,
        excluded_note=(
            f"{totals.cancellations} of the excluded rows are cancellations "
            f"({totals.cancellations_with_fee} with a fee charged). The rest carry the "
            "other excluded codes."
        ),
    )

    def periods() -> list[queries.PeriodPoint]:
        return queries.by_period(
            db, filters, granularity, week_starts_monday=config.week_starts_monday
        )

    if key == "collected":
        return Derivation(
            key=key,
            title=TITLES[key],
            value=totals.collected,
            kind="money",
            sentence=(
                "Every imported row's Total paid column, added up, for rows whose date "
                "of service falls in the window."
            ),
            operation="sum",
            window=window,
            terms=[Term("Rows added up", totals.visits), Term("Total", totals.collected, "money")],
            components=[Component(p.label, p.collected) for p in periods()],
            components_note="Each period's collected money. These add up to the total.",
            census=census,
            caveats=[
                "Includes cancellation fee money, because a fee that was charged and paid "
                "is real revenue. It is broken out separately as no show fee income.",
                _money_caveat(),
            ],
        )

    if key == "billed":
        return Derivation(
            key=key,
            title=TITLES[key],
            value=totals.billed,
            kind="money",
            sentence="Every imported row's Total due column, added up, across the window.",
            operation="sum",
            window=window,
            terms=[Term("Rows added up", totals.visits), Term("Total", totals.billed, "money")],
            components=[Component(p.label, p.billed) for p in periods()],
            components_note="Each period's billed amount. These add up to the total.",
            census=census,
            caveats=[
                "This is the sheet's Total due, not a gross charge. The source has no charge "
                "column, so nothing in this application is a contractual write off rate.",
                _money_caveat(),
            ],
        )

    if key == "outstanding":
        return Derivation(
            key=key,
            title=TITLES[key],
            value=totals.outstanding,
            kind="money",
            sentence=(
                "Every imported row's Total balance column, added up. Credits, meaning "
                "negative balances, are included here and pull the total down."
            ),
            operation="sum",
            window=window,
            terms=[
                Term("Patient side", totals.outstanding_patient, "money"),
                Term("Insurance side", totals.outstanding_insurance, "money"),
                Term("Total balance", totals.outstanding, "money"),
            ],
            components=[Component(p.label, p.outstanding) for p in periods()],
            components_note="Each period's open balance. These add up to the total.",
            census=census,
            caveats=[
                "The patient and insurance figures are shown as a split, not as two halves "
                "that must add to the total: on this source data they do not reconcile exactly.",
                "Balances are aged by date of service on the financial page, because the "
                "source carries no claim or payment dates.",
                _money_caveat(),
            ],
        )

    if key == "sessions":
        return Derivation(
            key=key,
            title=TITLES[key],
            value=totals.sessions,
            kind="count",
            sentence=(
                "Imported rows in the window whose CPT code is not on the excluded list. "
                "One row is one session."
            ),
            operation="sum",
            window=window,
            terms=[
                Term("Rows imported", totals.visits),
                Term("Rows excluded", totals.excluded_rows),
                Term("Sessions counted", totals.sessions),
            ],
            components=[Component(p.label, p.sessions) for p in periods()],
            components_note="Sessions per period. These add up to the total.",
            census=census,
            caveats=[
                _exclusion_sentence(filters),
                "A row is one billed line. Two lines billed for one appointment count twice.",
            ],
        )

    if key == "psychiatry_split":
        return Derivation(
            key=key,
            title=TITLES[key],
            value=totals.psychiatry_sessions,
            kind="count",
            sentence=(
                "Sessions whose CPT code begins 99 are counted as psychiatry, and the rest "
                "as therapy. The cancellation codes also begin 99 but are never sessions, so "
                "the session rule removes them before this split is applied."
            ),
            operation="subtract",
            window=window,
            terms=[
                Term("All sessions", totals.sessions),
                Term("Therapy sessions", totals.therapy_sessions),
                Term("Psychiatry sessions", totals.psychiatry_sessions),
            ],
            components=[Component(p.label, p.psychiatry_sessions) for p in periods()],
            components_note="Psychiatry sessions per period.",
            census=census,
            caveats=[
                "The split is by billing code, not by who delivered the session. A provider's "
                "own discipline is set on their record and drives the separate boards.",
            ],
        )

    if key == "no_show_fee_revenue":
        return Derivation(
            key=key,
            title=TITLES[key],
            value=totals.no_show_fee_revenue,
            kind="money",
            sentence=(
                "Total paid on rows coded 99999, a cancellation with a no show fee charged, "
                "added up across the window."
            ),
            operation="sum",
            window=window,
            terms=[
                Term("Cancellations with a fee", totals.cancellations_with_fee),
                Term("Fee money collected", totals.no_show_fee_revenue, "money"),
            ],
            components=[],
            components_sum_to_value=False,
            components_note="",
            census=census,
            caveats=[
                "Real revenue but not clinical revenue, so it is broken out rather than "
                "blended into therapy income. It is already inside the collected figure.",
                "How faithfully cancellations are recorded varies between colleagues, so read "
                "this as a consistency check rather than a behaviour finding.",
            ],
        )

    if key == "collection_rate":
        return Derivation(
            key=key,
            title=TITLES[key],
            value=totals.collection_rate,
            kind="rate",
            sentence="Collected money divided by billed money, as a percentage.",
            operation="divide",
            window=window,
            terms=[
                Term("Collected", totals.collected, "money"),
                Term("Billed", totals.billed, "money"),
            ],
            components=[],
            components_sum_to_value=False,
            components_note=(
                "A rate has no period contributions that add up, so none are shown. The "
                "collected and billed figures each have their own explanation."
            ),
            census=census,
            caveats=[
                "Recent claims have not had time to pay, so a window ending close to today "
                "reads low for reasons that are only the calendar. The insights page judges "
                "this rate on windows ending five weeks back for exactly that reason.",
                "Collected over billed, which is not a contractual write off rate: the source "
                "has no gross charge column.",
            ],
            unavailable_because=(
                "" if totals.collection_rate is not None else "Nothing was billed in this window."
            ),
        )

    if key == "cancellation_rate":
        scheduled = totals.sessions + totals.cancellations
        return Derivation(
            key=key,
            title=TITLES[key],
            value=totals.cancellation_rate,
            kind="rate",
            sentence=(
                "Cancellations divided by everything scheduled, meaning sessions plus "
                "cancellations, as a percentage."
            ),
            operation="divide",
            window=window,
            terms=[
                Term("Cancellations", totals.cancellations),
                Term("Scheduled (sessions plus cancellations)", scheduled),
            ],
            components=[],
            components_sum_to_value=False,
            components_note="A rate has no period contributions that add up, so none are shown.",
            census=census,
            caveats=[
                "The denominator is what the sheet recorded, not what a scheduling system "
                "booked. This application has no appointment data, so a cancellation that was "
                "never written down is invisible here.",
                "Recorded cancellation rates vary far more between colleagues than patient "
                "behaviour plausibly does, so treat the direction as real and the level as "
                "approximate.",
            ],
            unavailable_because=(
                ""
                if totals.cancellation_rate is not None
                else "Nothing was scheduled in this window."
            ),
        )

    if key == "revenue_per_session":
        return Derivation(
            key=key,
            title=TITLES[key],
            value=totals.revenue_per_session,
            kind="money",
            sentence="Collected money divided by the number of sessions.",
            operation="divide",
            window=window,
            terms=[
                Term("Collected", totals.collected, "money"),
                Term("Sessions", totals.sessions),
            ],
            components=[],
            components_sum_to_value=False,
            components_note="An average has no period contributions that add up.",
            census=census,
            caveats=[
                "Cancellation fee money is in the numerator but a cancellation is not a "
                "session, so fees lift this figure.",
                "Payer mix and session type mix both move it, which the financial page "
                "breakdowns show directly.",
            ],
            unavailable_because=(
                "" if totals.revenue_per_session is not None else "No sessions in this window."
            ),
        )

    if key == "below_threshold":
        rows = queries.by_therapist(db, filters, weeks_in_range=_weeks(filters))
        graded = [
            (
                r,
                config.status_for(
                    r.employment_type, r.weekly_expected_sessions, r.sessions_per_week
                ),
            )
            for r in rows
        ]
        below = [(r, s) for r, s in graded if s == "below"]
        measured = [(r, s) for r, s in graded if s]
        return Derivation(
            key=key,
            title=TITLES[key],
            value=len(below),
            kind="count",
            sentence=(
                "Providers whose sessions per week over this window fall below their own "
                "expectation. Each person is measured against a personal weekly figure if one "
                "is set, otherwise the default for their employment type."
            ),
            operation="count",
            window=window,
            terms=[
                Term("Providers on the board", len(rows)),
                Term("Providers with an expectation", len(measured)),
                Term("Below their expectation", len(below)),
            ],
            components=(
                [
                    Component(
                        f"{r.display_name}, expected "
                        + config.expectation_for(
                            r.employment_type, r.weekly_expected_sessions
                        ).label,
                        r.sessions_per_week,
                    )
                    for r, _ in below
                ]
                if may_see_therapists
                else []
            ),
            components_sum_to_value=False,
            components_note=(
                "Each provider's sessions per week, against their own expectation."
                if may_see_therapists
                else "Provider names need the therapist utilization permission, which this "
                "account does not hold. The counts above are unaffected."
            ),
            census=census,
            caveats=[
                "Sessions per week is sessions divided by the exact number of weeks in the "
                "window, so the answer does not change with the day it is opened.",
                "Providers on a percentage arrangement, and anyone not yet classified, carry "
                "no expectation and so can never be counted here.",
                "A low count is a question and not a conclusion: leave, referral flow, and a "
                "part time agreement all produce one.",
            ],
        )

    return None


def _weeks(filters: queries.Filters) -> Decimal:
    """Exact weeks in the window, matching how the report pages compute it."""
    return Decimal((filters.end - filters.start).days + 1) / Decimal(7)
