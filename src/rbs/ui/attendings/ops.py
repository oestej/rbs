"""Attending mutations and date helpers, free of NiceGUI."""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from rbs.attending_schedule import ScheduledWorkByWeek, effective_attending_schedule_week
from rbs.clinic_locks import attending_work_is_locked, clinic_slot_is_in_automatic_lock_window
from rbs.models.attending import (
    ATTENDING_WEEKLY_TARGET_WORK_TYPES,
    Attending,
    AttendingSchedule,
    AttendingVacation,
    AttendingWeeklyShiftTarget,
    AttendingWeeklyTargetMode,
    AttendingWeeklyWorkSchedule,
    AttendingWorkHalfDay,
    AttendingWorkType,
    EffectiveAttendingWeek,
)
from rbs.models.clinic import clinic_slot_date
from rbs.models.enums import Session, SolverStatus, Weekday
from rbs.models.instance import SchedulerInput
from rbs.models.schedule import AssignedAttendingWork, Schedule, ScheduleMeta, ScheduleMetrics

WEEKLY_SHIFT_TARGET_NONE = "none"


def add_attending(instance: SchedulerInput, attending: Attending) -> SchedulerInput:
    return instance.revised(attendings=[*instance.attendings, attending])


def replace_attending(
    instance: SchedulerInput,
    original_id: str,
    replacement: Attending,
) -> SchedulerInput:
    if not any(attending.id == original_id for attending in instance.attendings):
        raise ValueError(f"unknown attending {original_id!r}")
    return instance.revised(
        attendings=[
            replacement if attending.id == original_id else attending
            for attending in instance.attendings
        ]
    )


def replace_attending_with_schedule(
    instance: SchedulerInput,
    original_id: str,
    replacement: Attending,
    weeks: list[AttendingWeeklyWorkSchedule],
) -> SchedulerInput:
    """Atomically replace attending configuration and its accepted schedule."""
    if not any(attending.id == original_id for attending in instance.attendings):
        raise ValueError(f"unknown attending {original_id!r}")
    schedules = [
        schedule
        for schedule in instance.attending_schedules
        if schedule.attending_id != original_id
    ]
    if weeks:
        schedules.append(AttendingSchedule(attending_id=replacement.id, weeks=weeks))
    return instance.revised(
        attendings=[
            replacement if attending.id == original_id else attending
            for attending in instance.attendings
        ],
        attending_schedules=schedules,
    )


def remove_attending(instance: SchedulerInput, attending_id: str) -> SchedulerInput:
    if not any(attending.id == attending_id for attending in instance.attendings):
        raise ValueError(f"unknown attending {attending_id!r}")
    return instance.revised(
        attendings=[
            attending for attending in instance.attendings if attending.id != attending_id
        ],
        attending_schedules=[
            schedule
            for schedule in instance.attending_schedules
            if schedule.attending_id != attending_id
        ],
    )


def replace_attending_schedule(
    instance: SchedulerInput,
    attending_id: str,
    weeks: list[AttendingWeeklyWorkSchedule],
) -> SchedulerInput:
    """Replace one attending's accepted schedule, omitting empty containers."""
    if not any(attending.id == attending_id for attending in instance.attendings):
        raise ValueError(f"unknown attending {attending_id!r}")
    remaining = [
        schedule
        for schedule in instance.attending_schedules
        if schedule.attending_id != attending_id
    ]
    schedules = (
        [*remaining, AttendingSchedule(attending_id=attending_id, weeks=weeks)]
        if weeks
        else remaining
    )
    return instance.revised(attending_schedules=schedules)


def next_attending_id(instance: SchedulerInput) -> str:
    used = {attending.id for attending in instance.attendings}
    sequence = 1
    while True:
        candidate = f"attending-{sequence:03d}"
        if candidate not in used:
            return candidate
        sequence += 1


def academic_year_date_range(instance: SchedulerInput) -> tuple[date, date]:
    first_day = instance.calendar.first_week_start
    last_day = first_day + timedelta(days=instance.calendar.weeks * 7 - 1)
    return first_day, last_day


def parse_attending_date(
    instance: SchedulerInput,
    value: object,
    *,
    label: str,
) -> date:
    try:
        selected = date.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise ValueError(f"select a {label.lower()}") from exc
    first_day, last_day = academic_year_date_range(instance)
    if not first_day <= selected <= last_day:
        raise ValueError(
            f"{label.lower()} must be between {first_day:%b %d, %Y} "
            f"and {last_day:%b %d, %Y}"
        )
    return selected


def parse_academic_week(instance: SchedulerInput, value: object, *, label: str) -> int:
    try:
        week = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"select a {label.lower()}") from exc
    if not 1 <= week <= instance.calendar.weeks:
        raise ValueError(f"{label.lower()} must be between 1 and {instance.calendar.weeks}")
    return week


def replace_weekly_shift_target(
    targets: list[AttendingWeeklyShiftTarget],
    *,
    work_type_value: object,
    mode_value: object,
    minimum_value: object,
    maximum_value: object,
) -> list[AttendingWeeklyShiftTarget]:
    """Set or remove one category target and return deterministic ordering."""
    try:
        work_type = AttendingWorkType(str(work_type_value))
    except ValueError as exc:
        raise ValueError("select a work category") from exc
    if work_type not in ATTENDING_WEEKLY_TARGET_WORK_TYPES:
        raise ValueError(
            "Special/Other work is scheduled manually and does not use a weekly target"
        )
    remaining = [target for target in targets if target.work_type is not work_type]
    if mode_value == WEEKLY_SHIFT_TARGET_NONE:
        return sorted(
            remaining,
            key=lambda target: tuple(AttendingWorkType).index(target.work_type),
        )
    try:
        mode = AttendingWeeklyTargetMode(str(mode_value))
    except ValueError as exc:
        raise ValueError("select Fixed, Flexible, or No target") from exc
    replacement = AttendingWeeklyShiftTarget(
        work_type=work_type,
        minimum_shifts_per_week=minimum_value,
        maximum_shifts_per_week=maximum_value,
        mode=mode,
    )
    return sorted(
        [*remaining, replacement],
        key=lambda target: tuple(AttendingWorkType).index(target.work_type),
    )


def replace_weekly_work_schedule(
    instance: SchedulerInput,
    schedules: list[AttendingWeeklyWorkSchedule],
    *,
    week: object,
    half_days: list[AttendingWorkHalfDay],
    half_days_override: int | None = None,
) -> list[AttendingWeeklyWorkSchedule]:
    """Replace one academic-week draft and keep stable ordering.

    A schedule may contain more assignments than its effective total so the
    editor and schedule report can show the mismatch before the user chooses
    Override. Persisted target mismatches remain valid incremental setup.
    """
    selected_week = parse_academic_week(instance, week, label="Academic week")
    replacement = AttendingWeeklyWorkSchedule(
        week=selected_week,
        half_days_override=half_days_override,
        half_days=half_days,
    )
    return sorted(
        [
            *(schedule for schedule in schedules if schedule.week != selected_week),
            replacement,
        ],
        key=lambda schedule: schedule.week,
    )


def override_weekly_half_day_total(
    instance: SchedulerInput,
    schedules: list[AttendingWeeklyWorkSchedule],
    *,
    week: object,
    assigned_half_days: int | None = None,
) -> list[AttendingWeeklyWorkSchedule]:
    """Set one week's total to its current number of assigned half-days."""
    selected_week = parse_academic_week(instance, week, label="Academic week")
    current = next(
        (schedule for schedule in schedules if schedule.week == selected_week),
        None,
    )
    if current is None and assigned_half_days is None:
        raise ValueError(f"week {selected_week} has no schedule to override")
    half_days = current.half_days if current is not None else []
    assignment_count = (
        len(half_days) if assigned_half_days is None else assigned_half_days
    )
    return replace_weekly_work_schedule(
        instance,
        schedules,
        week=selected_week,
        half_days=half_days,
        half_days_override=assignment_count,
    )


def use_default_weekly_half_day_total(
    instance: SchedulerInput,
    schedules: list[AttendingWeeklyWorkSchedule],
    *,
    week: object,
    default_half_days: int,
    assigned_half_days: int | None = None,
) -> list[AttendingWeeklyWorkSchedule]:
    """Remove one week's override when its assignments fit the attending default."""
    selected_week = parse_academic_week(instance, week, label="Academic week")
    current = next(
        (schedule for schedule in schedules if schedule.week == selected_week),
        None,
    )
    if current is None:
        raise ValueError(f"week {selected_week} has no schedule")
    assignment_count = (
        len(current.half_days)
        if assigned_half_days is None
        else assigned_half_days
    )
    if assignment_count > default_half_days:
        raise ValueError(
            f"week {selected_week} has {assignment_count} assigned half-days; "
            f"remove assignments or raise the default to at least {assignment_count}"
        )
    return replace_weekly_work_schedule(
        instance,
        schedules,
        week=selected_week,
        half_days=current.half_days,
    )


def clear_weekly_work_schedule(
    instance: SchedulerInput,
    schedules: list[AttendingWeeklyWorkSchedule],
    *,
    week: object,
) -> list[AttendingWeeklyWorkSchedule]:
    """Remove one academic week's assignments and half-day override."""
    selected_week = parse_academic_week(instance, week, label="Academic week")
    return [schedule for schedule in schedules if schedule.week != selected_week]


def apply_schedule_template(
    instance: SchedulerInput,
    schedules: list[AttendingWeeklyWorkSchedule],
    template: list[AttendingWorkHalfDay],
    *,
    first_week: object,
    last_week: object,
) -> list[AttendingWeeklyWorkSchedule]:
    """Copy a template into every week in an inclusive range."""
    first = parse_academic_week(instance, first_week, label="First week")
    last = parse_academic_week(instance, last_week, label="Last week")
    if last < first:
        raise ValueError("last week cannot be before first week")
    by_week = {schedule.week: schedule for schedule in schedules}
    for week in range(first, last + 1):
        existing = by_week.get(week)
        by_week[week] = AttendingWeeklyWorkSchedule(
            week=week,
            half_days_override=(
                existing.half_days_override if existing is not None else None
            ),
            half_days=[half_day.model_copy(deep=True) for half_day in template],
        )
    return sorted(by_week.values(), key=lambda schedule: schedule.week)


def move_work_half_day(
    half_days: list[AttendingWorkHalfDay],
    *,
    source_weekday: Weekday,
    source_session: Session,
    target_weekday: Weekday,
    target_session: Session,
) -> list[AttendingWorkHalfDay]:
    """Move a work block to an open slot or swap it with an occupied slot."""
    source_key = (source_weekday, source_session)
    target_key = (target_weekday, target_session)
    if source_key == target_key:
        return half_days
    by_slot = {
        (half_day.weekday, half_day.session): half_day for half_day in half_days
    }
    source = by_slot.get(source_key)
    if source is None:
        raise ValueError("the work block being moved no longer exists")
    target = by_slot.get(target_key)
    by_slot.pop(source_key)
    by_slot.pop(target_key, None)
    by_slot[target_key] = source.revised(
        weekday=target_weekday,
        session=target_session,
    )
    if target is not None:
        by_slot[source_key] = target.revised(
            weekday=source_weekday,
            session=source_session,
        )
    return AttendingWeeklyWorkSchedule(
        week=1,
        half_days=list(by_slot.values()),
    ).half_days


def _validate_attending_work_slot(
    instance: SchedulerInput,
    attending_id: str,
    week: int,
    weekday: Weekday,
    session: Session,
) -> None:
    attending = next((person for person in instance.attendings if person.id == attending_id), None)
    if attending is None:
        raise ValueError("This attending is no longer in the workspace.")
    effective = instance.effective_attending_week(attending, week)
    first_day, last_day = academic_year_date_range(instance)
    calendar_day = effective.week_start + timedelta(days=tuple(Weekday).index(weekday))
    if not attending.is_scheduled_on(
        calendar_day, academic_year_start=first_day, academic_year_end=last_day,
    ):
        raise ValueError("This half-day is outside the attending's schedule dates.")
    if attending.is_on_vacation(calendar_day):
        raise ValueError("This attending is on vacation on this date.")
    reserved = effective.automatic_admin_half_day
    if reserved is not None and (reserved.weekday, reserved.session) == (weekday, session):
        raise ValueError("The academic half-day is reserved as Admin Time.")


def set_attending_work_half_day(
    instance: SchedulerInput,
    attending_id: str,
    *,
    week: int,
    weekday: Weekday,
    session: Session,
    assignment: AttendingWorkHalfDay | None,
) -> SchedulerInput:
    """Accept one calendar edit while preserving other work and the week override."""
    _validate_attending_work_slot(instance, attending_id, week, weekday, session)
    if assignment is not None and (assignment.weekday, assignment.session) != (weekday, session):
        raise ValueError("The assignment must use the selected half-day.")
    schedules = list(instance.attending_schedule_weeks(attending_id))
    current = next((schedule for schedule in schedules if schedule.week == week), None)
    half_days = [
        half_day for half_day in (current.half_days if current is not None else [])
        if (half_day.weekday, half_day.session) != (weekday, session)
    ]
    if assignment is not None:
        half_days.append(assignment)
    return replace_attending_schedule(
        instance,
        attending_id,
        replace_weekly_work_schedule(
            instance, schedules, week=week, half_days=half_days,
            half_days_override=current.half_days_override if current is not None else None,
        ),
    )


def move_attending_work_half_day(
    instance: SchedulerInput,
    attending_id: str,
    *,
    week: int,
    source_weekday: Weekday,
    source_session: Session,
    target_weekday: Weekday,
    target_session: Session,
) -> SchedulerInput:
    """Accept a move or swap between editable half-days in one attending week."""
    _validate_attending_work_slot(instance, attending_id, week, source_weekday, source_session)
    _validate_attending_work_slot(instance, attending_id, week, target_weekday, target_session)
    schedules = list(instance.attending_schedule_weeks(attending_id))
    current = next((schedule for schedule in schedules if schedule.week == week), None)
    half_days = move_work_half_day(
        current.half_days if current is not None else [],
        source_weekday=source_weekday, source_session=source_session,
        target_weekday=target_weekday, target_session=target_session,
    )
    return replace_attending_schedule(
        instance,
        attending_id,
        replace_weekly_work_schedule(
            instance, schedules, week=week, half_days=half_days,
            half_days_override=current.half_days_override if current is not None else None,
        ),
    )


def attending_schedule_work_on(
    instance: SchedulerInput,
    schedule: Schedule | None,
    attending_id: str,
    *,
    week: int,
    weekday: Weekday,
    session: Session,
    effective: EffectiveAttendingWeek | None = None,
    work_by_week: ScheduledWorkByWeek | None = None,
) -> AssignedAttendingWork | None:
    """Resolve visible work together with its persisted lock state.

    Hand-entered case work predates schedule lock fields. Materialize its state
    here; locking it moves it into the schedule, marked hand-entered, without
    changing the placement. Academic Admin Time is program-owned and cannot be
    unlocked. A caller rendering a whole week passes its ``effective`` week
    and ``work_by_week`` grouping so each half-day does not recompute them.
    """
    attending = next((person for person in instance.attendings if person.id == attending_id), None)
    if attending is None:
        raise ValueError("This attending is no longer in the workspace.")
    if work_by_week is None:
        work_by_week = instance.scheduled_attending_work(schedule)
    if effective is None:
        effective = effective_attending_schedule_week(
            instance, attending, week, work_by_week=work_by_week,
        )
    assignment = effective.assignment_on(weekday, session)
    if assignment is None or assignment == effective.automatic_admin_half_day:
        return None
    if effective.is_scheduled(weekday, session):
        return next(
            item
            for item in work_by_week.get((attending_id, week), ())
            if item.weekday is weekday and item.session is session
        )
    return AssignedAttendingWork(
        attending_id=attending_id,
        week=week,
        **assignment.model_dump(),
        manual_override=True,
        hand_entered=True,
    )


def attending_working_draft(instance: SchedulerInput, schedule: Schedule | None) -> Schedule:
    """Retain prior work while marking changed placements as an unsolved draft."""
    if schedule is None:
        return Schedule(meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=instance.solver.engine,
            status=SolverStatus.UNKNOWN,
            solver_status=SolverStatus.UNKNOWN,
        ))
    if schedule.meta.academic_year != instance.academic_year:
        raise ValueError("This schedule belongs to a different academic year. Reopen the schedule.")
    attendings = {person.id: person for person in instance.attendings}
    weeks = {}
    compatible = []
    for item in schedule.attending_work:
        attending = attendings.get(item.attending_id)
        if attending is None or item.week > instance.calendar.weeks:
            continue
        key = item.attending_id, item.week
        if key not in weeks:
            weeks[key] = instance.effective_attending_week(attending, item.week)
        effective = weeks[key]
        if (item.weekday in effective.available_weekdays
                and effective.assignment_on(item.weekday, item.session) is None
                and (item.clinic_id is None or item.clinic_id in instance.clinic_policy.site_ids)):
            compatible.append(item)
    notes = list(schedule.meta.notes)
    if len(compatible) != len(schedule.attending_work):
        note = (
            "Attending work that no longer fits schedule dates, vacation, or other "
            "assigned work was left out of this working draft."
        )
        if note not in notes:
            notes.append(note)
    return schedule.revised(attending_work=compatible, meta=schedule.meta.revised(
        status=SolverStatus.UNKNOWN,
        solver_status=SolverStatus.UNKNOWN,
        solver_objective=None,
        solver_best_bound=None,
        metrics=ScheduleMetrics(),
        validation_errors=[],
        validation_warnings=[],
        diagnostics=[],
        notes=notes,
    ))


def _store_attending_work(
    instance: SchedulerInput,
    schedule: Schedule | None,
    attending_id: str,
    *,
    cleared: set[tuple[str, int, Weekday, Session]],
    placed: list[AssignedAttendingWork],
    working_draft: bool,
) -> tuple[SchedulerInput, Schedule | None]:
    """Clear half-days in both the case and the schedule, then store ``placed``.

    Each item keeps its provenance. Hand-entered work with no lock state to
    record returns to the attending's week-by-week case schedule; anything
    locked, explicitly unlocked inside the automatic lock window, or placed
    by a solve stays in the schedule. ``working_draft`` marks the schedule
    as changed placements; a lock change alone keeps a solved result.
    """
    to_case = [
        item for item in placed
        if item.hand_entered and not item.locked and not item.automatic_lock_exempt
    ]
    to_schedule = [item for item in placed if item not in to_case]
    by_week = {weekly.week: weekly for weekly in instance.attending_schedule_weeks(attending_id)}
    for week in sorted({key[1] for key in cleared} | {item.week for item in to_case}):
        weekly = by_week.get(week)
        kept = [
            half_day for half_day in (weekly.half_days if weekly is not None else [])
            if (attending_id, week, half_day.weekday, half_day.session) not in cleared
        ]
        returned = [
            AttendingWorkHalfDay(
                weekday=item.weekday,
                session=item.session,
                work_type=item.work_type,
                clinic_id=item.clinic_id,
                description=item.description,
            )
            for item in to_case
            if item.week == week
        ]
        if weekly is not None:
            by_week[week] = weekly.revised(half_days=[*kept, *returned])
        elif returned:
            by_week[week] = AttendingWeeklyWorkSchedule(week=week, half_days=returned)
    updated = replace_attending_schedule(
        instance, attending_id, sorted(by_week.values(), key=lambda weekly: weekly.week),
    )
    if schedule is None and not to_schedule:
        return updated, None
    base = (
        attending_working_draft(updated, schedule)
        if working_draft or schedule is None
        else schedule
    )
    return updated, base.revised(attending_work=[
        *(item for item in base.attending_work if item.key not in cleared),
        *to_schedule,
    ])


def _save_attending_work_locks(
    instance: SchedulerInput,
    schedule: Schedule | None,
    attending_id: str,
    items: list[AssignedAttendingWork],
    *,
    locked: bool,
    today: date | None,
) -> tuple[SchedulerInput, Schedule | None]:
    changes = {
        item.key: item.revised(
            locked=locked,
            automatic_lock_exempt=(
                not locked
                and clinic_slot_is_in_automatic_lock_window(instance, item, item.week, today=today)
            ),
        )
        for item in items
        if attending_work_is_locked(instance, item, today=today) != locked
    }
    if not changes:
        return instance, schedule
    # A lock change leaves the displayed work unchanged, so an existing solved
    # result stays solved; the caller marks an already stale reference as a
    # draft before persisting it.
    return _store_attending_work(
        instance, schedule, attending_id,
        cleared=set(changes), placed=list(changes.values()), working_draft=False,
    )


def set_attending_work_locked(
    instance: SchedulerInput,
    schedule: Schedule | None,
    attending_id: str,
    *,
    week: int,
    weekday: Weekday,
    session: Session,
    locked: bool,
    today: date | None = None,
) -> tuple[SchedulerInput, Schedule | None]:
    """Lock a placement for editing and the next solve, or explicitly unlock it."""
    _validate_attending_work_slot(instance, attending_id, week, weekday, session)
    item = attending_schedule_work_on(
        instance, schedule, attending_id, week=week, weekday=weekday, session=session,
    )
    if item is None:
        raise ValueError("This work half-day no longer exists.")
    return _save_attending_work_locks(
        instance, schedule, attending_id, [item], locked=locked, today=today,
    )


def set_attending_schedule_locked(
    instance: SchedulerInput,
    schedule: Schedule | None,
    attending_id: str,
    *,
    locked: bool,
    today: date | None = None,
) -> tuple[SchedulerInput, Schedule | None]:
    """Change all visible work locks for one attending, preserving masked work."""
    attending = next((person for person in instance.attendings if person.id == attending_id), None)
    if attending is None:
        raise ValueError("This attending is no longer in the workspace.")
    work_by_week = instance.scheduled_attending_work(schedule)
    items = []
    for week in range(1, instance.calendar.weeks + 1):
        effective = effective_attending_schedule_week(
            instance, attending, week, work_by_week=work_by_week,
        )
        for assignment in effective.assignments:
            item = attending_schedule_work_on(
                instance, schedule, attending_id, week=week,
                weekday=assignment.weekday, session=assignment.session,
                effective=effective, work_by_week=work_by_week,
            )
            if item is not None:
                items.append(item)
    return _save_attending_work_locks(
        instance, schedule, attending_id, items, locked=locked, today=today,
    )


def _require_unlocked_attending_work(
    instance: SchedulerInput,
    schedule: Schedule | None,
    attending_id: str,
    *,
    week: int,
    weekday: Weekday,
    session: Session,
    today: date | None,
) -> AssignedAttendingWork | None:
    """Return the visible work in a half-day, rejecting it if it is locked."""
    item = attending_schedule_work_on(
        instance, schedule, attending_id, week=week, weekday=weekday, session=session,
    )
    if item is not None and attending_work_is_locked(instance, item, today=today):
        raise ValueError("This work half-day is locked. Unlock it before changing or removing it.")
    return item


def _in_schedule(schedule: Schedule | None, item: AssignedAttendingWork | None) -> bool:
    """Whether visible work is stored in the schedule rather than the case.

    Identity, not the half-day key: schedule work hidden under hand-entered
    work shares the key of the case work actually shown there.
    """
    return (
        schedule is not None
        and item is not None
        and any(existing is item for existing in schedule.attending_work)
    )


def set_attending_schedule_half_day(
    instance: SchedulerInput,
    schedule: Schedule | None,
    attending_id: str,
    *,
    week: int,
    weekday: Weekday,
    session: Session,
    assignment: AttendingWorkHalfDay | None,
    today: date | None = None,
) -> tuple[SchedulerInput, Schedule | None]:
    """Edit combined case/generated work, rejecting a locked placement."""
    previous = _require_unlocked_attending_work(
        instance, schedule, attending_id, week=week, weekday=weekday, session=session, today=today,
    )
    if schedule is None:
        return set_attending_work_half_day(
            instance, attending_id, week=week, weekday=weekday, session=session,
            assignment=assignment,
        ), None
    _validate_attending_work_slot(instance, attending_id, week, weekday, session)
    if assignment is not None and (assignment.weekday, assignment.session) != (weekday, session):
        raise ValueError("The assignment must use the selected half-day.")
    # Edited work keeps its provenance: work a solve placed stays schedule
    # work, keeping an explicit unlock so a past-date edit does not acquire
    # the automatic lock again, and anything else is hand-entered.
    scheduled = _in_schedule(schedule, previous)
    replacement = [] if assignment is None else [AssignedAttendingWork(
        attending_id=attending_id, week=week, **assignment.model_dump(),
        locked=False,
        automatic_lock_exempt=bool(scheduled and previous.automatic_lock_exempt),
        manual_override=True,
        hand_entered=not scheduled or previous.hand_entered,
    )]
    # Clearing the half-day in both places also drops schedule work hidden
    # under the case work being replaced, so it cannot reappear.
    return _store_attending_work(
        instance, schedule, attending_id,
        cleared={(attending_id, week, weekday, session)}, placed=replacement,
        working_draft=True,
    )


def move_attending_schedule_half_day(
    instance: SchedulerInput,
    schedule: Schedule | None,
    attending_id: str,
    *,
    week: int,
    source_weekday: Weekday,
    source_session: Session,
    target_weekday: Weekday,
    target_session: Session,
    today: date | None = None,
) -> tuple[SchedulerInput, Schedule | None]:
    """Move or swap combined work while protecting both source and destination locks.

    Moved work keeps its provenance: hand-entered work stays a hard input for
    the next solve, and work a solve placed stays a stability preference.
    """
    visible = {}
    for weekday, session in ((source_weekday, source_session), (target_weekday, target_session)):
        _validate_attending_work_slot(instance, attending_id, week, weekday, session)
        visible[weekday, session] = _require_unlocked_attending_work(
            instance, schedule, attending_id,
            week=week, weekday=weekday, session=session, today=today,
        )
    if (source_weekday, source_session) == (target_weekday, target_session):
        return instance, schedule
    source = visible[source_weekday, source_session]
    target = visible[target_weekday, target_session]
    if source is None:
        raise ValueError("The work block being moved no longer exists.")
    if schedule is None:
        return move_attending_work_half_day(
            instance, attending_id, week=week,
            source_weekday=source_weekday, source_session=source_session,
            target_weekday=target_weekday, target_session=target_session,
        ), None
    keys = {source.key, (attending_id, week, target_weekday, target_session)}
    if not any(item.key in keys for item in schedule.attending_work):
        updated = move_attending_work_half_day(
            instance, attending_id, week=week,
            source_weekday=source_weekday, source_session=source_session,
            target_weekday=target_weekday, target_session=target_session,
        )
        return updated, attending_working_draft(updated, schedule)

    def moved(item: AssignedAttendingWork, weekday: Weekday, session: Session):
        return item.revised(
            weekday=weekday, session=session, locked=False,
            automatic_lock_exempt=clinic_slot_is_in_automatic_lock_window(
                instance, item.revised(weekday=weekday), week, today=today,
            ),
            manual_override=True,
        )

    placed = [moved(source, target_weekday, target_session)]
    if target is not None:
        placed.append(moved(target, source_weekday, source_session))
    return _store_attending_work(
        instance, schedule, attending_id, cleared=keys, placed=placed, working_draft=True,
    )


PreceptingHalfDay = tuple[str, Weekday, Session]


def precepting_over_maximum(
    instance: SchedulerInput,
    schedule: Schedule | None,
    week: int,
    *,
    work_by_week: ScheduledWorkByWeek | None = None,
) -> dict[PreceptingHalfDay, tuple[int, int]]:
    """Clinic half-days in one week where more attendings precept than allowed.

    Every attending's work counts the way the calendar shows it: hand-entered
    work, the schedule's work around it, dates, and vacation. Values are the
    number of attendings precepting and the clinic's maximum for that date.
    """
    if work_by_week is None:
        work_by_week = instance.scheduled_attending_work(schedule)
    site_ids = set(instance.clinic_policy.site_ids)
    counts: Counter[PreceptingHalfDay] = Counter()
    for attending in instance.attendings:
        effective = effective_attending_schedule_week(
            instance, attending, week, work_by_week=work_by_week,
        )
        for assignment in effective.assignments:
            if (
                assignment.work_type is AttendingWorkType.PRECEPTING_CLINIC
                and assignment.clinic_id in site_ids
            ):
                counts[assignment.clinic_id, assignment.weekday, assignment.session] += 1
    over: dict[PreceptingHalfDay, tuple[int, int]] = {}
    for (clinic_id, weekday, session), count in counts.items():
        maximum = instance.clinic_max_attendings_on(
            clinic_id,
            clinic_slot_date(instance.calendar.first_week_start, week, weekday),
            session,
        )
        if count > maximum:
            over[clinic_id, weekday, session] = (count, maximum)
    return over


def _precepting_over_maximum_text(
    instance: SchedulerInput,
    clinic_id: str,
    calendar_day: date,
    count: int,
    maximum: int,
    *,
    when: str,
) -> str:
    site = instance.clinic_policy.site(clinic_id)
    precepting = "1 attending precepts" if count == 1 else f"{count} attendings precept"
    if site.is_closed(calendar_day):
        return f"{site.name} is closed {when}, but {precepting} there."
    if maximum == 0:
        return f"{site.name} allows no precepting {when}, but {precepting} there."
    allowed = "1 attending" if maximum == 1 else f"{maximum} attendings"
    return f"{site.name} allows at most {allowed} to precept {when}, but {precepting} there."


def precepting_maximum_warning(
    instance: SchedulerInput,
    schedule: Schedule | None,
    attending_id: str,
    *,
    week: int,
    weekday: Weekday,
    session: Session,
) -> str | None:
    """Explain an accepted edit that puts a clinic over its maximum attendings.

    Edits may override a clinic's maximum; the solve never does. The message
    says what is over and how to keep or allow the override.
    """
    item = attending_schedule_work_on(
        instance, schedule, attending_id, week=week, weekday=weekday, session=session,
    )
    if (
        item is None
        or item.work_type is not AttendingWorkType.PRECEPTING_CLINIC
        or item.clinic_id is None
    ):
        return None
    over = precepting_over_maximum(instance, schedule, week).get(
        (item.clinic_id, weekday, session)
    )
    if over is None:
        return None
    site = instance.clinic_policy.site(item.clinic_id)
    calendar_day = clinic_slot_date(instance.calendar.first_week_start, week, weekday)
    when = (
        f"on {calendar_day:%a}, {calendar_day:%b} {calendar_day.day} "
        f"{'AM' if session is Session.MORNING else 'PM'}"
    )
    message = (
        _precepting_over_maximum_text(instance, item.clinic_id, calendar_day, *over, when=when)
        + " Saved as a manual override."
    )
    coverage = (
        "Max attendings"
        if item.clinic_id in instance.attending_managed_clinic_ids
        else "attending coverage"
    )
    # A closed clinic has no maximum to raise.
    allow = None if site.is_closed(calendar_day) else (
        f"raise {site.name}'s {coverage} for that date with an exception in the Clinic tab"
    )
    if (
        _in_schedule(schedule, item)
        and not item.hand_entered
        and not attending_work_is_locked(instance, item)
    ):
        # Unlocked work a solve placed only guides the next solve, which keeps
        # every clinic within its maximum. Hand-entered work is always kept.
        keep = "Lock this work half-day to keep it through the next solve"
        return f"{message} {keep}, or {allow} to allow it." if allow else f"{message} {keep}."
    return f"{message} To allow it, {allow}." if allow else message


def precepting_maximum_marker(
    instance: SchedulerInput,
    over: dict[PreceptingHalfDay, tuple[int, int]],
    clinic_id: str | None,
    calendar_day: date,
    session: Session,
) -> str | None:
    """Calendar explanation for work at a clinic half-day over its maximum."""
    weekday = tuple(Weekday)[calendar_day.weekday()]
    if clinic_id is None or (clinic_id, weekday, session) not in over:
        return None
    return _precepting_over_maximum_text(
        instance,
        clinic_id,
        calendar_day,
        *over[clinic_id, weekday, session],
        when="on this half-day",
    )


def add_vacation_range(
    instance: SchedulerInput,
    vacations: list[AttendingVacation],
    *,
    start_value: object,
    end_value: object,
) -> list[AttendingVacation]:
    """Validate and add one uncapped, day-level vacation range."""
    vacation = AttendingVacation(
        start_date=parse_attending_date(
            instance,
            start_value,
            label="Vacation start date",
        ),
        end_date=parse_attending_date(
            instance,
            end_value,
            label="Vacation end date",
        ),
    )
    # Reuse the external model's overlap and deterministic-order guarantees.
    validated = Attending(
        id="attending-vacation-draft",
        name="Vacation draft",
        vacation_ranges=[*vacations, vacation],
    )
    return validated.vacation_ranges
