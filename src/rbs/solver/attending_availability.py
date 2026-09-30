"""Week-by-week attending availability shared by readiness, solving, and validation.

A solve fills each attending's open half-days around work that is already
fixed: hand-entered week-by-week work (including hand-entered work the
schedule holds only to record its lock state), the automatic academic Admin
Time, and scheduled work that is locked by hand or inside the automatic lock
window.
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

from collections import Counter, defaultdict
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

    ``reference`` is the previous schedule. Its locked attending work, work
    inside the automatic lock window, and hand-entered work are kept exactly;
    its other work only guides stability. Kept work that no longer fits the
    attending's dates, vacation, other hand-entered work, or Admin Time is
    released.
    """
    reference_items = problem.scheduled_attending_work(reference)
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
                or item.hand_entered
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
        # unlocked copy either. Keep it rather than silently dropping it, and
        # keep hand-entered work whether or not it is locked.
        if (
            item.work_type is AttendingWorkType.SPECIAL_OTHER
            or item.hand_entered
            or attending_work_is_locked(problem, item, today=today)
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

    Scheduled work counts exactly where the effective attending week shows
    it: on the attending's scheduled, non-vacation days, at an open clinic,
    and not under hand-entered work or the automatic academic Admin Time.
    """
    managed = problem.attending_managed_clinic_ids
    if not managed or schedule is None or not schedule.attending_work:
        return list(problem.attending_coverage)
    attendings = {attending.id: attending for attending in problem.attendings}
    precepting = {
        key: [
            item
            for item in items
            if item.work_type is AttendingWorkType.PRECEPTING_CLINIC
            and item.clinic_id in managed
        ]
        for key, items in problem.scheduled_attending_work(schedule).items()
    }
    # Each half-day holds one item, so the other scheduled work in a week
    # cannot change where its precepting shows.
    return problem.attending_coverage_for(
        (
            problem.effective_attending_week(
                attendings[attending_id],
                week,
                scheduled_work=items,
            )
            for (attending_id, week), items in precepting.items()
            if items
        ),
        scheduled_only=True,
        base=problem.attending_coverage,
    )


def schedule_capacity_view(
    problem: SolverProblem,
    schedule: Schedule | None,
) -> SolverProblem:
    """Return a problem whose attending-managed capacity is what ``schedule`` staffs.

    Build the view once per render or validation and pass it down; editors
    that check many drop targets must not rebuild it for each one.
    """
    if (
        not problem.attending_managed_clinic_ids
        or schedule is None
        or not schedule.attending_work
    ):
        return problem
    return problem.with_attending_coverage(schedule_attending_coverage(problem, schedule))


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
