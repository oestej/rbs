"""Attending mutations and date helpers, free of NiceGUI."""

from __future__ import annotations

from datetime import date, timedelta

from rbs.models.attending import (
    Attending,
    AttendingAdHocWorkHalfDay,
    AttendingVacation,
)
from rbs.models.enums import Session
from rbs.models.instance import SchedulerInput

AD_HOC_ALL_DAY = "all_day"


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
    additions = [
        AttendingAdHocWorkHalfDay(date=calendar_day, session=session)
        for session in sessions
    ]
    # Reuse the external model's duplicate and deterministic-order guarantees.
    validated = Attending(
        id="attending-ad-hoc-work-draft",
        name="Ad hoc work draft",
        ad_hoc_work_half_days=[*half_days, *additions],
    )
    return validated.ad_hoc_work_half_days
