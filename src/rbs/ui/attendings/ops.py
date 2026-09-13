"""Attending mutations and date helpers, free of NiceGUI."""

from __future__ import annotations

from datetime import date, timedelta

from rbs.models.attending import (
    MAX_ATTENDING_HALF_DAYS_PER_WEEK,
    Attending,
    AttendingAdHocWorkHalfDay,
    AttendingVacation,
    AttendingWeeklyShiftTarget,
    AttendingWeeklyTargetMode,
    AttendingWeeklyWorkSchedule,
    AttendingWorkHalfDay,
    AttendingWorkType,
)
from rbs.models.enums import Session, Weekday
from rbs.models.instance import SchedulerInput

AD_HOC_ALL_DAY = "all_day"
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


def remove_attending(instance: SchedulerInput, attending_id: str) -> SchedulerInput:
    if not any(attending.id == attending_id for attending in instance.attendings):
        raise ValueError(f"unknown attending {attending_id!r}")
    return instance.revised(
        attendings=[
            attending for attending in instance.attendings if attending.id != attending_id
        ]
    )


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
    shifts_value: object,
) -> list[AttendingWeeklyShiftTarget]:
    """Set or remove one category target and return deterministic ordering."""
    try:
        work_type = AttendingWorkType(str(work_type_value))
    except ValueError as exc:
        raise ValueError("select a work category") from exc
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
        shifts_per_week=shifts_value,
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
) -> list[AttendingWeeklyWorkSchedule]:
    """Replace one complete academic-week schedule and keep stable ordering."""
    selected_week = parse_academic_week(instance, week, label="Academic week")
    replacement = AttendingWeeklyWorkSchedule(
        week=selected_week,
        half_days=half_days,
    )
    validated = Attending(
        id="attending-weekly-work-draft",
        name="Weekly work draft",
        half_days_per_week=MAX_ATTENDING_HALF_DAYS_PER_WEEK,
        weekly_work_schedules=[
            *(
                schedule
                for schedule in schedules
                if schedule.week != selected_week
            ),
            replacement,
        ],
    )
    return validated.weekly_work_schedules


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
        by_week[week] = AttendingWeeklyWorkSchedule(
            week=week,
            half_days=[half_day.model_copy(deep=True) for half_day in template],
        )
    validated = Attending(
        id="attending-template-application-draft",
        name="Template application draft",
        half_days_per_week=MAX_ATTENDING_HALF_DAYS_PER_WEEK,
        weekly_work_schedules=list(by_week.values()),
    )
    return validated.weekly_work_schedules


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


def add_ad_hoc_work_half_days(
    instance: SchedulerInput,
    half_days: list[AttendingAdHocWorkHalfDay],
    *,
    date_value: object,
    session_value: object,
    work_type_value: object = AttendingWorkType.SPECIAL_OTHER,
    clinic_id_value: object = None,
) -> list[AttendingAdHocWorkHalfDay]:
    """Validate and add one dated session, or both sessions for an all-day choice."""
    calendar_day = parse_attending_date(
        instance,
        date_value,
        label="Work date",
    )
    if session_value == AD_HOC_ALL_DAY:
        sessions = tuple(Session)
    else:
        try:
            sessions = (Session(session_value),)
        except (TypeError, ValueError) as exc:
            raise ValueError("select Morning, Afternoon, or All day") from exc
    try:
        work_type = AttendingWorkType(work_type_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("select a work type") from exc
    clinic_id = (
        str(clinic_id_value).strip()
        if clinic_id_value is not None and str(clinic_id_value).strip()
        else None
    )
    additions = [
        AttendingAdHocWorkHalfDay(
            date=calendar_day,
            session=session,
            work_type=work_type,
            clinic_id=clinic_id,
        )
        for session in sessions
    ]
    # Reuse the external model's duplicate and deterministic-order guarantees.
    validated = Attending(
        id="attending-ad-hoc-work-draft",
        name="Ad hoc work draft",
        ad_hoc_work_half_days=[*half_days, *additions],
    )
    return validated.ad_hoc_work_half_days
