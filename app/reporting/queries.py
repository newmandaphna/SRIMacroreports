"""Aggregate queries for the reporting modules.

**Every query in this module is aggregate or therapist grain. None of them selects
`patient_name` or `patient_code`.** That is the enforcement of SECURITY.md section 6.3,
and it lives here rather than in the templates: a template that forgot to omit a column
would leak, whereas a column that is never selected cannot.

A test asserts the property directly against the compiled SQL of every builder, so it
holds for whatever gets added later rather than only for what exists today.

Money arrives as Decimal from the Money column type and stays Decimal all the way to
the template, so nothing rounds twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.models.therapist import Discipline, EmploymentType, Therapist
from app.models.visit import Visit
from app.reporting.periods import Granularity, format_period, period_series

ZERO = Decimal("0.00")

# The two cancellation codes, confirmed with the practice on 2026-07-27:
# 99998 is a cancellation with no fee, 99999 is a cancellation with a no show fee.
CANCELLED_NO_FEE = "99998"
CANCELLED_WITH_FEE = "99999"
CANCELLATION_CODES = (CANCELLED_NO_FEE, CANCELLED_WITH_FEE)


@dataclass(frozen=True)
class Filters:
    """What the picker and the filter bar resolve to."""

    start: date
    end: date
    cpt_exclusions: tuple[str, ...]
    therapist_ids: tuple[int, ...] = ()
    locations: tuple[str, ...] = ()

    def replaced(self, *, start: date, end: date) -> Filters:
        return Filters(
            start=start,
            end=end,
            cpt_exclusions=self.cpt_exclusions,
            therapist_ids=self.therapist_ids,
            locations=self.locations,
        )


@dataclass
class Totals:
    """Headline figures for one window."""

    sessions: int = 0
    psychiatry_sessions: int = 0
    visits: int = 0
    collected: Decimal = ZERO
    outstanding: Decimal = ZERO
    outstanding_patient: Decimal = ZERO
    outstanding_insurance: Decimal = ZERO
    billed: Decimal = ZERO
    no_show_fee_revenue: Decimal = ZERO
    cancellations: int = 0
    cancellations_with_fee: int = 0

    @property
    def therapy_sessions(self) -> int:
        return self.sessions - self.psychiatry_sessions

    @property
    def excluded_rows(self) -> int:
        """Imported rows that were not counted as sessions.

        Exact by construction rather than a second query: every imported row is
        either a session or excluded by the CPT list. Cancellations are a subset
        of these, and the rest are the bookkeeping codes. The explanation layer
        uses this to reconcile its row census, which is how a reader can tell that
        a figure counted what it claimed to count.
        """
        return self.visits - self.sessions

    @property
    def collection_rate(self) -> Decimal | None:
        """Collected as a share of billed. None when nothing was billed."""
        if self.billed <= ZERO:
            return None
        return (self.collected / self.billed * 100).quantize(Decimal("0.1"))

    @property
    def cancellation_rate(self) -> Decimal | None:
        denominator = self.sessions + self.cancellations
        if denominator == 0:
            return None
        return (Decimal(self.cancellations) / denominator * 100).quantize(Decimal("0.1"))

    @property
    def revenue_per_session(self) -> Decimal | None:
        if self.sessions == 0:
            return None
        return (self.collected / self.sessions).quantize(Decimal("0.01"))


@dataclass
class PeriodPoint:
    start: date
    label: str
    sessions: int = 0
    psychiatry_sessions: int = 0
    collected: Decimal = ZERO
    billed: Decimal = ZERO
    outstanding: Decimal = ZERO


@dataclass
class TherapistRow:
    therapist_id: int
    display_name: str
    employment_type: EmploymentType
    discipline: Discipline = Discipline.THERAPIST
    weekly_expected_sessions: int | None = None
    sessions: int = 0
    collected: Decimal = ZERO
    cancellations: int = 0
    weeks_in_range: Decimal = Decimal(1)
    notes: str | None = None
    # Whether the person is still on the roster. A departed therapist's numbers are
    # history rather than a finding, so anything that reads a fall as news has to be
    # able to tell the two apart.
    active: bool = True

    @property
    def sessions_per_week(self) -> Decimal:
        if self.weeks_in_range <= 0:
            return Decimal(self.sessions)
        return (Decimal(self.sessions) / Decimal(self.weeks_in_range)).quantize(Decimal("0.1"))

    @property
    def cancellation_rate(self) -> Decimal | None:
        """Cancellations as a share of what was scheduled (sessions plus
        cancellations). None when nothing was scheduled at all."""
        scheduled = self.sessions + self.cancellations
        if scheduled == 0:
            return None
        return (Decimal(self.cancellations) / scheduled * 100).quantize(Decimal("0.1"))

    @property
    def measured_against_threshold(self) -> bool:
        """Whether this person's arrangement carries a session expectation at all.

        Flagging the unmeasured as "below threshold" would be a false alarm about
        a real person's work, so they are shown without a status instead.
        """
        return self.employment_type.counts_against_threshold


@dataclass
class Breakdown:
    """A generic one dimensional grouping, for insurance and location tables."""

    key: str
    label: str
    sessions: int = 0
    collected: Decimal = ZERO
    outstanding: Decimal = ZERO


@dataclass
class Coverage:
    min_date: date | None = None
    max_date: date | None = None
    visits: int = 0

    @property
    def has_data(self) -> bool:
        return self.visits > 0

    # Convenience for the empty state, which offers a link to the data you do have.
    other: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- helpers


def _is_session(exclusions: tuple[str, ...]):
    """SQL predicate: this row counts as a session.

    The exclusion list governs session counts only. It never filters revenue, because
    a cancellation fee is real money (ASSUMPTIONS.md A-031).
    """
    if not exclusions:
        return Visit.id.is_not(None)
    return Visit.cpt_base.notin_(exclusions)


def _population_conditions(filters: Filters) -> list:
    """Everything the filter bar narrows EXCEPT the date range.

    Separate from the dates because some figures are deliberately computed over the
    whole record while still belonging to the filtered population: a patient's first
    ever session, for instance, has to be looked for outside the window or a returning
    patient reads as new, but it must still be their first session with the therapist
    the page is filtered to.
    """
    conditions = []
    if filters.therapist_ids:
        conditions.append(Visit.therapist_id.in_(filters.therapist_ids))
    if filters.locations:
        # A NULL location is included when "unknown" is explicitly among the choices.
        if "" in filters.locations:
            named = [loc for loc in filters.locations if loc]
            conditions.append(or_(Visit.location_short.in_(named), Visit.location_short.is_(None)))
        else:
            conditions.append(Visit.location_short.in_(filters.locations))
    return conditions


def _base_conditions(filters: Filters) -> list:
    return [
        Visit.dos >= filters.start,
        Visit.dos <= filters.end,
        *_population_conditions(filters),
    ]


def _session_count_expr(exclusions: tuple[str, ...]):
    return func.coalesce(func.sum(case((_is_session(exclusions), 1), else_=0)), 0)


def _cancellation_count_expr(code: str | None = None):
    codes = (code,) if code else CANCELLATION_CODES
    return func.coalesce(func.sum(case((Visit.cpt_base.in_(codes), 1), else_=0)), 0)


def _psychiatry_session_count_expr(exclusions: tuple[str, ...]):
    """Sessions whose base CPT starts with 99: evaluation and management codes,
    which at this practice means psychiatry. The cancellation codes also start
    with 99 but are never sessions, so the session predicate screens them out."""
    return func.coalesce(
        func.sum(case((and_(_is_session(exclusions), Visit.cpt_base.like("99%")), 1), else_=0)),
        0,
    )


def _money(column) -> Decimal:
    return func.coalesce(func.sum(column), 0)


# ---------------------------------------------------------------------------- totals


def totals(db: Session, filters: Filters) -> Totals:
    """Headline figures. One query, no patient columns."""
    stmt: Select = select(
        func.count(Visit.id),
        _session_count_expr(filters.cpt_exclusions),
        _psychiatry_session_count_expr(filters.cpt_exclusions),
        _money(Visit.total_paid),
        _money(Visit.total_balance),
        _money(Visit.pt_amount_due),
        _money(Visit.ins_balance),
        _money(Visit.total_due),
        func.coalesce(
            func.sum(case((Visit.cpt_base == CANCELLED_WITH_FEE, Visit.total_paid), else_=0)),
            0,
        ),
        _cancellation_count_expr(),
        _cancellation_count_expr(CANCELLED_WITH_FEE),
    ).where(and_(*_base_conditions(filters)))

    row = db.execute(stmt).one()
    return Totals(
        visits=row[0] or 0,
        sessions=int(row[1] or 0),
        psychiatry_sessions=int(row[2] or 0),
        collected=_as_money(row[3]),
        outstanding=_as_money(row[4]),
        outstanding_patient=_as_money(row[5]),
        outstanding_insurance=_as_money(row[6]),
        billed=_as_money(row[7]),
        no_show_fee_revenue=_as_money(row[8]),
        cancellations=int(row[9] or 0),
        cancellations_with_fee=int(row[10] or 0),
    )


def _as_money(value: object) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"))
    # The Money type returns Decimal for column values, but SUM over it comes back as
    # an integer number of cents, so convert here rather than trusting the driver.
    return (Decimal(int(value)) / 100).quantize(Decimal("0.01"))


# ------------------------------------------------------------------------ trend data


def by_period(
    db: Session,
    filters: Filters,
    granularity: Granularity,
    *,
    week_starts_monday: bool = True,
) -> list[PeriodPoint]:
    """A continuous series, with empty periods present as zeros.

    A missing week would let a trend line close the gap and read as though nothing
    happened, which is the opposite of what a gap means.
    """
    rows = db.execute(
        select(
            Visit.dos,
            _session_count_expr(filters.cpt_exclusions),
            _psychiatry_session_count_expr(filters.cpt_exclusions),
            _money(Visit.total_paid),
            _money(Visit.total_due),
            _money(Visit.total_balance),
        )
        .where(and_(*_base_conditions(filters)))
        .group_by(Visit.dos)
    ).all()

    buckets: dict[date, PeriodPoint] = {}
    for start in period_series(
        filters.start, filters.end, granularity, week_starts_monday=week_starts_monday
    ):
        buckets[start] = PeriodPoint(start=start, label=format_period(start, granularity))

    from app.reporting.periods import period_start

    for dos, sessions, psychiatry, collected, billed, outstanding in rows:
        bucket_start = period_start(dos, granularity, week_starts_monday=week_starts_monday)
        point = buckets.get(bucket_start)
        if point is None:
            continue
        point.sessions += int(sessions or 0)
        point.psychiatry_sessions += int(psychiatry or 0)
        point.collected += _as_money(collected)
        point.billed += _as_money(billed)
        point.outstanding += _as_money(outstanding)

    return list(buckets.values())


# ------------------------------------------------------------------- therapist grain


def by_therapist(
    db: Session, filters: Filters, *, weeks_in_range: Decimal | int = 1
) -> list[TherapistRow]:
    """Therapist grain, never patient grain.

    An OUTER join with the range conditions on the join, not in WHERE: an inner join
    dropped any therapist with zero visits in the range, which hid exactly the person
    the utilization board most needs to show. Active therapists always get a row;
    inactive ones appear only when the range actually contains their visits.
    """
    range_filter = list(filters.therapist_ids)
    stmt = (
        select(
            Therapist.id,
            Therapist.display_name,
            Therapist.employment_type,
            Therapist.discipline,
            Therapist.weekly_expected_sessions,
            Therapist.active,
            _session_count_expr(filters.cpt_exclusions),
            _money(Visit.total_paid),
            _cancellation_count_expr(),
        )
        .outerjoin(Visit, and_(Visit.therapist_id == Therapist.id, *_base_conditions(filters)))
        .group_by(
            Therapist.id,
            Therapist.display_name,
            Therapist.employment_type,
            Therapist.discipline,
            Therapist.weekly_expected_sessions,
            Therapist.active,
        )
        .having(or_(Therapist.active.is_(True), func.count(Visit.id) > 0))
        .order_by(func.lower(Therapist.display_name))
    )
    if range_filter:
        stmt = stmt.where(Therapist.id.in_(range_filter))
    rows = db.execute(stmt).all()

    return [
        TherapistRow(
            therapist_id=r[0],
            display_name=r[1],
            employment_type=EmploymentType(r[2]),
            discipline=Discipline(r[3]),
            weekly_expected_sessions=r[4],
            active=bool(r[5]),
            sessions=int(r[6] or 0),
            collected=_as_money(r[7]),
            cancellations=int(r[8] or 0),
            weeks_in_range=Decimal(weeks_in_range) if weeks_in_range > 0 else Decimal(1),
        )
        for r in rows
    ]


# ----------------------------------------------------------------------- breakdowns


def by_insurance(
    db: Session,
    filters: Filters,
    limit: int = 15,
    *,
    groups: dict[str, str] | None = None,
) -> list[Breakdown]:
    """Per payer, with member codes folded into their configured group (for
    example KS, PC, and IA are all IBC), so one payer is one row."""
    rows = _breakdown(db, filters, Visit.insurance_short, "Unknown", limit=200)
    return _fold_groups(rows, groups)[:limit]


def _fold_groups(rows: list[Breakdown], groups: dict[str, str] | None) -> list[Breakdown]:
    if not groups:
        return rows
    folded: dict[str, Breakdown] = {}
    for row in rows:
        name = groups.get(row.key.strip().upper(), row.label) if row.key else row.label
        target = folded.get(name)
        if target is None:
            folded[name] = Breakdown(
                key=name,
                label=name,
                sessions=row.sessions,
                collected=row.collected,
                outstanding=row.outstanding,
            )
        else:
            target.sessions += row.sessions
            target.collected += row.collected
            target.outstanding += row.outstanding
    return sorted(folded.values(), key=lambda b: b.collected, reverse=True)


def by_location(db: Session, filters: Filters, limit: int = 15) -> list[Breakdown]:
    return _breakdown(db, filters, Visit.location_short, "Unknown", limit)


def by_cpt(db: Session, filters: Filters, limit: int = 20) -> list[Breakdown]:
    return _breakdown(db, filters, Visit.cpt_base, "Unknown", limit)


def _breakdown(
    db: Session, filters: Filters, column, null_label: str, limit: int
) -> list[Breakdown]:
    rows = db.execute(
        select(
            column,
            _session_count_expr(filters.cpt_exclusions),
            _money(Visit.total_paid),
            _money(Visit.total_balance),
        )
        .where(and_(*_base_conditions(filters)))
        .group_by(column)
        .order_by(func.sum(Visit.total_paid).desc())
        .limit(limit)
    ).all()

    return [
        Breakdown(
            key=r[0] or "",
            label=r[0] or null_label,
            sessions=int(r[1] or 0),
            collected=_as_money(r[2]),
            outstanding=_as_money(r[3]),
        )
        for r in rows
    ]


AGING_BUCKET_LABELS = ("0 to 30 days", "31 to 60 days", "61 to 90 days", "Over 90 days")


@dataclass
class AgingRow:
    key: str
    label: str
    buckets: tuple[Decimal, Decimal, Decimal, Decimal]
    total: Decimal


# Enough payers to fold member codes into their groups before the list is cut down to
# the displayed length. The real sheet carries about thirty distinct payer codes.
_AGING_FETCH = 200


def aging_by_insurance(
    db: Session,
    *,
    today: date,
    limit: int = 12,
    groups: dict[str, str] | None = None,
    filters: Filters | None = None,
) -> tuple[list[AgingRow], AgingRow]:
    """Open balances bucketed by the age of the session they sit on, per payer.

    Deliberately unfiltered by the page's date RANGE: an old balance does not stop
    being owed because the picker shows April. The therapist and location filters do
    apply when given, because those narrow which work is being looked at rather than
    when: a page filtered to one therapist that showed the whole practice's debt beside
    that therapist's sessions read as though the debt were theirs.

    Aged by date of service, because the source sheet carries no payment or claim
    dates; say so wherever this renders. Credits (negative balances) are excluded
    rather than netted, so a payer's old debt cannot hide behind a recent overpayment.
    Still no patient columns.
    """
    b30, b60, b90 = (today - timedelta(days=n) for n in (30, 60, 90))
    bucket_cases = (
        case((Visit.dos >= b30, Visit.total_balance), else_=0),
        case((and_(Visit.dos < b30, Visit.dos >= b60), Visit.total_balance), else_=0),
        case((and_(Visit.dos < b60, Visit.dos >= b90), Visit.total_balance), else_=0),
        case((Visit.dos < b90, Visit.total_balance), else_=0),
    )
    sums = [func.coalesce(func.sum(expr), 0) for expr in bucket_cases]
    scope = [Visit.total_balance > 0, *(_population_conditions(filters) if filters else [])]

    # Over-fetched on purpose. Slicing to `limit` before folding meant KS, PC and IA
    # each spent one of the twelve rows and then merged into a single IBC row, so the
    # table showed fewer payers than it was asked for and the payers it dropped were
    # real ones displaced by rows that no longer existed.
    rows = db.execute(
        select(Visit.insurance_short, *sums, _money(Visit.total_balance))
        .where(and_(*scope))
        .group_by(Visit.insurance_short)
        .order_by(func.sum(Visit.total_balance).desc())
        .limit(_AGING_FETCH)
    ).all()

    # Grand totals over ALL open balances, not just the displayed payers, so the
    # bottom line never quietly shrinks because a small payer fell off the list.
    grand = db.execute(select(*sums, _money(Visit.total_balance)).where(and_(*scope))).one()

    payer_rows = [
        AgingRow(
            key=r[0] or "",
            label=r[0] or "Unknown",
            buckets=tuple(_as_money(v) for v in r[1:5]),
            total=_as_money(r[5]),
        )
        for r in rows
    ]
    if groups:
        folded: dict[str, AgingRow] = {}
        for row in payer_rows:
            name = groups.get(row.key.strip().upper(), row.label) if row.key else row.label
            target = folded.get(name)
            if target is None:
                folded[name] = AgingRow(key=name, label=name, buckets=row.buckets, total=row.total)
            else:
                folded[name] = AgingRow(
                    key=name,
                    label=name,
                    buckets=tuple(a + b for a, b in zip(target.buckets, row.buckets, strict=True)),
                    total=target.total + row.total,
                )
        payer_rows = sorted(folded.values(), key=lambda r: r.total, reverse=True)
    payer_rows = payer_rows[:limit]
    total_row = AgingRow(
        key="",
        label="All payers",
        buckets=tuple(_as_money(v) for v in grand[0:4]),
        total=_as_money(grand[4]),
    )
    return payer_rows, total_row


def by_weekday(db: Session, filters: Filters) -> list[int]:
    """Session counts by day of week, Monday first. Still no patient columns.

    Grouped by DOS in SQL and folded to weekdays in Python, so the answer does not
    depend on any database specific day-of-week function or its numbering.
    """
    rows = db.execute(
        select(Visit.dos, _session_count_expr(filters.cpt_exclusions))
        .where(and_(*_base_conditions(filters)))
        .group_by(Visit.dos)
    ).all()

    out = [0] * 7
    for dos, sessions in rows:
        out[dos.weekday()] += int(sessions or 0)
    return out


# -------------------------------------------------------------------------- coverage


def coverage(db: Session) -> Coverage:
    """What data exists at all, for the empty state to point at."""
    row = db.execute(select(func.min(Visit.dos), func.max(Visit.dos), func.count(Visit.id))).one()
    return Coverage(min_date=row[0], max_date=row[1], visits=row[2] or 0)


def available_locations(db: Session) -> list[str]:
    rows = (
        db.execute(select(Visit.location_short).distinct().order_by(Visit.location_short))
        .scalars()
        .all()
    )
    return [r for r in rows if r]


def active_therapists(db: Session) -> list[Therapist]:
    return (
        db.execute(
            select(Therapist)
            .where(Therapist.active.is_(True))
            .order_by(func.lower(Therapist.display_name))
        )
        .scalars()
        .all()
    )


# ------------------------------------------------------- therapist weekly history


@dataclass
class TherapistPeriod:
    """One period in a single therapist's history, with its note and status."""

    start: date
    label: str
    sessions: int = 0
    collected: Decimal = ZERO
    cancellations: int = 0
    note: str | None = None


def therapist_history(
    db: Session,
    therapist_id: int,
    filters: Filters,
    granularity: Granularity,
    *,
    week_starts_monday: bool = True,
) -> list[TherapistPeriod]:
    """One therapist's periods across the range, continuous and note annotated.

    Still no patient columns: this is one therapist's aggregate by period.
    """
    scoped = Filters(
        start=filters.start,
        end=filters.end,
        cpt_exclusions=filters.cpt_exclusions,
        therapist_ids=(therapist_id,),
        locations=filters.locations,
    )

    rows = db.execute(
        select(
            Visit.dos,
            _session_count_expr(scoped.cpt_exclusions),
            _money(Visit.total_paid),
            _cancellation_count_expr(),
        )
        .where(and_(*_base_conditions(scoped)))
        .group_by(Visit.dos)
    ).all()

    from app.reporting.periods import period_start

    buckets: dict[date, TherapistPeriod] = {
        start: TherapistPeriod(start=start, label=format_period(start, granularity))
        for start in period_series(
            filters.start, filters.end, granularity, week_starts_monday=week_starts_monday
        )
    }

    for dos, sessions, collected, cancellations in rows:
        bucket = buckets.get(period_start(dos, granularity, week_starts_monday=week_starts_monday))
        if bucket is None:
            continue
        bucket.sessions += int(sessions or 0)
        bucket.collected += _as_money(collected)
        bucket.cancellations += int(cancellations or 0)

    from app.models.utilization import UtilizationNote

    # Bounded by the buckets, not by the filter range. The first bucket usually starts
    # before the range does (a range beginning mid week belongs to the Monday before
    # it), and bounding on the range would silently drop that week's note.
    notes = (
        db.execute(
            select(UtilizationNote.period_start, UtilizationNote.body).where(
                UtilizationNote.therapist_id == therapist_id,
                UtilizationNote.granularity == granularity,
                UtilizationNote.period_start >= min(buckets),
                UtilizationNote.period_start <= max(buckets),
            )
        ).all()
        if buckets
        else []
    )
    for period, body in notes:
        if period in buckets:
            buckets[period].note = body

    return list(buckets.values())


def notes_for_period(
    db: Session, period_start_date: date, granularity: Granularity
) -> dict[int, str]:
    """Every therapist's note for one period, keyed by therapist id."""
    from app.models.utilization import UtilizationNote

    rows = db.execute(
        select(UtilizationNote.therapist_id, UtilizationNote.body).where(
            UtilizationNote.period_start == period_start_date,
            UtilizationNote.granularity == granularity,
        )
    ).all()
    return dict(rows)


def latest_notes(db: Session, therapist_ids: list[int]) -> dict[int, str]:
    """The most recent note per therapist, for the status board.

    Shown so that a low number carries its explanation on the board itself rather
    than only on a page somebody has to think to open.
    """
    from app.models.utilization import UtilizationNote

    if not therapist_ids:
        return {}
    rows = db.execute(
        select(
            UtilizationNote.therapist_id,
            UtilizationNote.body,
            UtilizationNote.period_start,
        )
        .where(UtilizationNote.therapist_id.in_(therapist_ids))
        .order_by(UtilizationNote.therapist_id, UtilizationNote.period_start.desc())
    ).all()

    out: dict[int, str] = {}
    for therapist_id, body, _period in rows:
        out.setdefault(therapist_id, body)
    return out
