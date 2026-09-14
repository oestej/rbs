"""Attending mutations and date helpers, free of NiceGUI."""

from __future__ import annotations

from datetime import date, timedelta

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
)
from rbs.models.enums import Session, Weekday
from rbs.models.instance import SchedulerInput

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
