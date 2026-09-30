"""Week-by-week attending availability shared by readiness, solving, and validation.

A solve fills each attending's open half-days around work that is already
fixed: hand-entered week-by-week work, the automatic academic Admin Time, and
scheduled work that is locked by hand or inside the automatic lock window.
This module states those facts once, without OR-Tools, so the readiness
probe, the block solve's precepting envelope, the weekly attending pass, and
schedule validation all agree on who could work when.

The solve places attending work Monday through Friday. A weekend half-day is
open only when it appears in the attending's preferred weekly schedule, so
weekend work otherwise stays hand-entered. Special/Other work is never
placed automatically, and a category is placed only when the attending has a
weekly target for it or a preferred half-day of it. Precepting Clinic at an
attending-managed clinic is the exception: resident coverage can call for it
unless a Fixed range rules it out.
"""

from __future__ import annotations

from collections import Counter, OrderedDict, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta

from rbs.clinic_locks import attending_work_is_locked
from rbs.models.attending import (
    Attending,
    AttendingClinicCoverage,
    AttendingWeeklyTargetMode,
    AttendingWorkHalfDay,
    AttendingWorkType,
)
from rbs.models.enums import WEEKDAYS_MF, Session, Weekday
from rbs.models.instance import SolverProblem
from rbs.models.schedule import AssignedAttendingWork, Schedule

HalfDay = tuple[Weekday, Session]
TargetRange = tuple[int, int]

SCHEDULED_WORK_TYPES = (
    AttendingWorkType.INPATIENT_SERVICE,
    AttendingWorkType.ATTENDING_CLINIC,
    AttendingWorkType.PRECEPTING_CLINIC,
    AttendingWorkType.ADMIN_TIME,
)

_WEEKDAY_INDEX = {weekday: index for index, weekday in enumerate(Weekday)}
_SESSION_INDEX = {session: index for index, session in enumerate(Session)}


def half_day_sort_key(half_day: HalfDay) -> tuple[int, int]:
    return _WEEKDAY_INDEX[half_day[0]], _SESSION_INDEX[half_day[1]]


def precepting_allowed(
    problem: SolverProblem,
    clinic_id: str,
    calendar_day: date,
    session: Session,
) -> bool:
    """Whether any attending may precept at a clinic on one date and session.

    Every clinic's weekly grid and dated overrides set a maximum number of
    attendings per half-day; a clinic with no maximum then, or closed that
    day, has no precepting at all.
    """
    if clinic_id not in problem.clinic_policy.site_ids:
        return False
    return problem.clinic_max_attendings_on(clinic_id, calendar_day, session) > 0


@dataclass(frozen=True, slots=True)
class AttendingWeekFacts:
    """Everything a solve needs to place one attending's work in one week."""

    attending_id: str
    name: str
    week: int
    week_start: date
    target: int
    full_week: bool
    fixed: tuple[AttendingWorkHalfDay, ...]
    locked_work: tuple[AssignedAttendingWork, ...]
    open_half_days: tuple[HalfDay, ...]
    fixed_ranges: tuple[tuple[AttendingWorkType, TargetRange], ...]
    flexible_ranges: tuple[tuple[AttendingWorkType, TargetRange], ...]
    clinic_day_minimum: int
    work_types: frozenset[AttendingWorkType]
    precepting_clinics: tuple[str, ...]
    fill_goal: int
    preferred: dict[HalfDay, AttendingWorkHalfDay] = field(default_factory=dict)
    reference: dict[HalfDay, AssignedAttendingWork] = field(default_factory=dict)
    released_locks: tuple[AssignedAttendingWork, ...] = ()

    @property
    def remaining(self) -> int:
        """Most half-days the solve may add without exceeding the weekly total."""
        return max(0, self.target - len(self.fixed))

    @property
    def can_precept(self) -> bool:
        return AttendingWorkType.PRECEPTING_CLINIC in self.work_types and bool(
            self.precepting_clinics
        )

    def fixed_count(self, work_type: AttendingWorkType) -> int:
        return sum(assignment.work_type is work_type for assignment in self.fixed)

    @property
    def fixed_clinic_days(self) -> frozenset[Weekday]:
        return frozenset(
            assignment.weekday
            for assignment in self.fixed
            if assignment.work_type is AttendingWorkType.ATTENDING_CLINIC
        )

    def date_of(self, weekday: Weekday) -> date:
        return self.week_start + timedelta(days=_WEEKDAY_INDEX[weekday])

    def feasibility_signature(self) -> tuple:
        """Identity of the hard rules, independent of who the attending is.

        Two attending-weeks with equal signatures can precept exactly the same
        sets of half-days, so the solve computes their limits once.
        """
        return (
            self.full_week,
            self.remaining,
            self.open_half_days,
            tuple(sorted(Counter(item.work_type for item in self.fixed).items())),
            tuple(sorted(self.fixed_clinic_days, key=_WEEKDAY_INDEX.__getitem__)),
            self.fixed_ranges,
            self.clinic_day_minimum,
            tuple(sorted(self.work_types)),
            self.can_precept,
        )


def attending_week_facts(
    problem: SolverProblem,
    reference: Schedule | None = None,
    *,
    today: date | None = None,
) -> dict[int, list[AttendingWeekFacts]]:
    """Return per-week facts for every attending, in deterministic order.

    ``reference`` is the previous schedule. Its locked attending work (and
    work inside the automatic lock window) is kept exactly; its other work
    only guides stability. Locked work that no longer fits the attending's
    dates, vacation, hand-entered work, or Admin Time is released.
    """
    reference_items = _reference_work(problem, reference)
    managed = sorted(problem.attending_managed_clinic_ids)
    by_week: dict[int, list[AttendingWeekFacts]] = defaultdict(list)
    # Identity order, not display order: renaming someone must not change
    # how the solve breaks ties.
    for attending in sorted(problem.attendings, key=lambda attending: attending.id):
        for week in range(1, problem.calendar.weeks + 1):
            facts = _week_facts(
                problem,
                attending,
                week,
                reference_items.get((attending.id, week), ()),
                managed_clinic_ids=managed,
                today=today,
            )
            if facts is not None:
                by_week[week].append(facts)
    return dict(by_week)


def _week_facts(
    problem: SolverProblem,
    attending: Attending,
    week: int,
    prior: Iterable[AssignedAttendingWork],
    *,
    managed_clinic_ids: list[str],
    today: date | None,
) -> AttendingWeekFacts | None:
    hand_entered = problem.effective_attending_week(attending, week)
    prior = tuple(prior)
    if not hand_entered.is_active_week:
        if not prior:
            return None
        # Outside the attending's schedule dates nothing can be kept. Report
        # the locked work being released instead of dropping it silently.
        return AttendingWeekFacts(
            attending_id=attending.id,
            name=attending.name,
            week=week,
            week_start=hand_entered.week_start,
            target=hand_entered.target_half_days,
            full_week=False,
            fixed=(),
            locked_work=(),
            open_half_days=(),
            fixed_ranges=(),
            flexible_ranges=(),
            clinic_day_minimum=0,
            work_types=frozenset(),
            precepting_clinics=(),
            fill_goal=0,
            released_locks=tuple(
                item
                for item in prior
                if item.work_type is AttendingWorkType.SPECIAL_OTHER
                or attending_work_is_locked(problem, item, today=today)
            ),
        )
    occupied = {
        (assignment.weekday, assignment.session)
        for assignment in hand_entered.assignments
    }
    available_days = hand_entered.available_weekdays
    preferred_by_slot = {
        (item.weekday, item.session): item
        for item in attending.preferred_weekly_schedule_half_days
    }
    automatic_slots = {
        (weekday, session)
        for weekday in WEEKDAYS_MF
        for session in Session
    } | set(preferred_by_slot)
    schedulable = {
        slot
        for slot in automatic_slots
        if slot[0] in available_days and slot not in occupied
    }

    locked: list[AssignedAttendingWork] = []
    released: list[AssignedAttendingWork] = []
    reference: dict[HalfDay, AssignedAttendingWork] = {}
    for item in prior:
        slot = (item.weekday, item.session)
        viable = item.weekday in available_days and slot not in occupied
        # A solve never places Special/Other work, so it cannot recreate an
        # unlocked copy either. Keep it rather than silently dropping it.
        if item.work_type is AttendingWorkType.SPECIAL_OTHER or attending_work_is_locked(
            problem, item, today=today
        ):
            if viable:
                locked.append(item)
            else:
                released.append(item)
        elif slot in schedulable:
            reference[slot] = item
    locked_slots = {(item.weekday, item.session) for item in locked}
    fixed = (
        *hand_entered.assignments,
        *(
            AttendingWorkHalfDay(
                weekday=item.weekday,
                session=item.session,
                work_type=item.work_type,
                clinic_id=item.clinic_id,
                description=item.description,
            )
            for item in locked
        ),
    )
    open_half_days = tuple(
        sorted(schedulable - locked_slots, key=half_day_sort_key)
    )
    fixed_ranges = tuple(
        (target.work_type, (target.minimum_shifts_per_week, target.maximum_shifts_per_week))
        for target in attending.weekly_shift_targets
        if target.mode is AttendingWeeklyTargetMode.FIXED
    )
    flexible_ranges = tuple(
        (target.work_type, (target.minimum_shifts_per_week, target.maximum_shifts_per_week))
        for target in attending.weekly_shift_targets
        if target.mode is AttendingWeeklyTargetMode.FLEXIBLE
    )
    configured = {
        target.work_type
        for target in attending.weekly_shift_targets
        if target.maximum_shifts_per_week > 0
    } | {item.work_type for item in attending.preferred_weekly_schedule_half_days}
    fixed_precepting = dict(fixed_ranges).get(AttendingWorkType.PRECEPTING_CLINIC)
    precepting_room = fixed_precepting is None or fixed_precepting[1] > sum(
        assignment.work_type is AttendingWorkType.PRECEPTING_CLINIC for assignment in fixed
    )
    work_types = {work_type for work_type in configured if work_type in SCHEDULED_WORK_TYPES}
    if managed_clinic_ids and precepting_room:
        work_types.add(AttendingWorkType.PRECEPTING_CLINIC)
    if not precepting_room:
        work_types.discard(AttendingWorkType.PRECEPTING_CLINIC)
    candidates = [
        *managed_clinic_ids,
        *sorted(
            item.clinic_id
            for item in attending.preferred_weekly_schedule_half_days
            if item.work_type is AttendingWorkType.PRECEPTING_CLINIC
            and item.clinic_id is not None
        ),
    ]
    if AttendingWorkType.PRECEPTING_CLINIC in configured:
        # Precepting the attending is asked for can always fall back to the
        # primary clinic when the clinics they prefer are full or closed.
        candidates.append(problem.clinic_policy.primary_site_id)
    precepting_clinics = tuple(dict.fromkeys(candidates))
    full_week = hand_entered.is_full_schedule_week and not hand_entered.has_vacation
    target = hand_entered.target_half_days
    remaining = max(0, target - len(fixed))
    if full_week:
        fill_goal = remaining
    else:
        weekdays_available = sum(weekday in available_days for weekday in WEEKDAYS_MF)
        prorated = (target * weekdays_available * 2 + 5) // 10
        fill_goal = max(0, min(prorated - len(fixed), remaining))
    fill_goal = min(fill_goal, len(open_half_days))
    return AttendingWeekFacts(
        attending_id=attending.id,
        name=attending.name,
        week=week,
        week_start=hand_entered.week_start,
        target=target,
        full_week=full_week,
        fixed=tuple(fixed),
        locked_work=tuple(locked),
        open_half_days=open_half_days,
        fixed_ranges=fixed_ranges,
        flexible_ranges=flexible_ranges,
        clinic_day_minimum=hand_entered.attending_clinic_day_minimum,
        work_types=frozenset(work_types),
        precepting_clinics=precepting_clinics,
        fill_goal=fill_goal,
        preferred={
            slot: preferred
            for slot, preferred in preferred_by_slot.items()
            if slot in open_half_days
        },
        reference=reference,
        released_locks=tuple(released),
    )


def _reference_work(
    problem: SolverProblem,
    reference: Schedule | None,
) -> dict[tuple[str, int], tuple[AssignedAttendingWork, ...]]:
    if reference is None or reference.meta.academic_year != problem.academic_year:
        return {}
    known = {attending.id for attending in problem.attendings}
    weeks = problem.calendar.weeks
    grouped: dict[tuple[str, int], list[AssignedAttendingWork]] = defaultdict(list)
    for item in reference.attending_work:
        if item.attending_id not in known or not 1 <= item.week <= weeks:
            continue
        if (
            item.work_type is AttendingWorkType.PRECEPTING_CLINIC
            and item.clinic_id not in problem.clinic_policy.site_ids
        ):
            continue
        grouped[item.attending_id, item.week].append(item)
    return {key: tuple(items) for key, items in grouped.items()}


def potential_attending_coverage(
    problem: SolverProblem,
    facts_by_week: dict[int, list[AttendingWeekFacts]] | None = None,
) -> list[AttendingClinicCoverage]:
    """Most preceptors each attending-managed clinic half-day could receive.

    Each attending who could precept on a half-day counts toward every
    attending-managed clinic open then, on top of fixed Precepting Clinic
    work there. This is an upper bound for filtering and readiness; the
    solve itself decides who precepts where.
    """
    managed = problem.attending_managed_clinic_ids
    if not managed:
        return list(problem.attending_coverage)
    facts_by_week = (
        facts_by_week if facts_by_week is not None else attending_week_facts(problem)
    )
    # Hand-entered precepting is already the problem's own coverage; locked
    # scheduled work adds to it. Every attending who could still precept adds
    # more, up to the clinic's maximum for that half-day.
    fixed: Counter[tuple[str, date, Session]] = Counter(
        {
            (item.clinic_id, item.date, item.session): item.attendings
            for item in problem.attending_coverage
        }
    )
    available: Counter[tuple[str, date, Session]] = Counter()
    for weekly in facts_by_week.values():
        for facts in weekly:
            for item in facts.locked_work:
                if (
                    item.work_type is AttendingWorkType.PRECEPTING_CLINIC
                    and item.clinic_id in managed
                ):
                    fixed[item.clinic_id, facts.date_of(item.weekday), item.session] += 1
            if not facts.can_precept or facts.remaining < 1:
                continue
            for weekday, session in facts.open_half_days:
                calendar_day = facts.date_of(weekday)
                for clinic_id in managed:
                    available[clinic_id, calendar_day, session] += 1
    coverage = []
    for key in sorted(set(fixed) | set(available)):
        clinic_id, calendar_day, session = key
        maximum = problem.clinic_max_attendings_on(clinic_id, calendar_day, session)
        count = max(fixed[key], min(maximum, fixed[key] + available[key]))
        if count > 0 and not problem.clinic_policy.site(clinic_id).is_closed(calendar_day):
            coverage.append(
                AttendingClinicCoverage(
                    clinic_id=clinic_id,
                    date=calendar_day,
                    session=session,
                    attendings=count,
                )
            )
    return coverage


def potential_capacity_view(
    problem: SolverProblem,
    facts_by_week: dict[int, list[AttendingWeekFacts]] | None = None,
) -> SolverProblem:
    """Return a problem whose attending-managed capacity is its upper bound."""
    if not problem.attending_managed_clinic_ids:
        return problem
    return problem.with_attending_coverage(
        potential_attending_coverage(problem, facts_by_week)
    )


def schedule_attending_coverage(
    problem: SolverProblem,
    schedule: Schedule | None,
) -> list[AttendingClinicCoverage]:
    """Preceptors a schedule provides: hand-entered plus scheduled work.

    Scheduled work counts exactly where the effective attending week would
    show it: on the attending's scheduled, non-vacation days, at an open
    clinic, and not under hand-entered work or the automatic academic Admin
    Time.
    """
    managed = problem.attending_managed_clinic_ids
    if not managed or schedule is None or not schedule.attending_work:
        return list(problem.attending_coverage)
    counts: Counter[tuple[str, date, Session]] = Counter(
        {
            (item.clinic_id, item.date, item.session): item.attendings
            for item in problem.attending_coverage
        }
    )
    attendings = {attending.id: attending for attending in problem.attendings}
    first_day = problem.calendar.first_week_start
    last_day = first_day + timedelta(days=problem.calendar.weeks * 7 - 1)
    automatic_admin = problem.clinic_policy.academic_half_day_is_attending_admin_time
    hand_entered: dict[tuple[str, int], set[HalfDay]] = {}
    for item in schedule.attending_work:
        if (
            item.work_type is not AttendingWorkType.PRECEPTING_CLINIC
            or item.clinic_id not in managed
        ):
            continue
        attending = attendings.get(item.attending_id)
        if attending is None or item.week > problem.calendar.weeks:
            continue
        calendar_day = first_day + timedelta(
            weeks=item.week - 1,
            days=_WEEKDAY_INDEX[item.weekday],
        )
        if (
            not attending.is_scheduled_on(
                calendar_day,
                academic_year_start=first_day,
                academic_year_end=last_day,
            )
            or attending.is_on_vacation(calendar_day)
            or problem.clinic_policy.site(item.clinic_id).is_closed(calendar_day)
        ):
            continue
        slot = (item.weekday, item.session)
        if automatic_admin and problem.academic_half_day_for_week(item.week) == slot:
            continue
        key = (attending.id, item.week)
        if key not in hand_entered:
            schedule_record = problem.attending_schedule_for(attending.id)
            saved_week = (
                schedule_record.schedule_for_week(item.week)
                if schedule_record is not None
                else None
            )
            hand_entered[key] = (
                {(half_day.weekday, half_day.session) for half_day in saved_week.half_days}
                if saved_week is not None
                else set()
            )
        if slot in hand_entered[key]:
            continue
        counts[item.clinic_id, calendar_day, item.session] += 1
    return [
        AttendingClinicCoverage(
            clinic_id=clinic_id,
            date=calendar_day,
            session=session,
            attendings=count,
        )
        for (clinic_id, calendar_day, session), count in sorted(
            counts.items(),
            key=lambda item: (item[0][1], _SESSION_INDEX[item[0][2]], item[0][0]),
        )
        if count > 0
    ]


_VIEW_CACHE: OrderedDict[
    tuple[int, int],
    tuple[SolverProblem, Schedule, SolverProblem],
] = OrderedDict()
_VIEW_CACHE_SIZE = 8


def schedule_capacity_view(
    problem: SolverProblem,
    schedule: Schedule | None,
) -> SolverProblem:
    """Return a problem whose attending-managed capacity is what ``schedule`` staffs.

    Editors ask this for every drop target, so the latest views are kept by
    identity. The cache holds its keys alive, so an ``id`` is never reused
    while it is cached, and schedules are replaced rather than mutated when
    their attending work changes.
    """
    if (
        not problem.attending_managed_clinic_ids
        or schedule is None
        or not schedule.attending_work
    ):
        return problem
    key = (id(problem), id(schedule))
    cached = _VIEW_CACHE.get(key)
    if cached is not None and cached[0] is problem and cached[1] is schedule:
        _VIEW_CACHE.move_to_end(key)
        return cached[2]
    view = problem.with_attending_coverage(schedule_attending_coverage(problem, schedule))
    _VIEW_CACHE[key] = (problem, schedule, view)
    while len(_VIEW_CACHE) > _VIEW_CACHE_SIZE:
        _VIEW_CACHE.popitem(last=False)
    return view


__all__ = [
    "SCHEDULED_WORK_TYPES",
    "AttendingWeekFacts",
    "HalfDay",
    "attending_week_facts",
    "half_day_sort_key",
    "precepting_allowed",
    "potential_attending_coverage",
    "potential_capacity_view",
    "schedule_attending_coverage",
    "schedule_capacity_view",
]
