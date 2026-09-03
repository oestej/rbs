"""Academic-year labels, calendar anchors, and workspace rebasing."""

from __future__ import annotations

import re
from datetime import date, timedelta

from rbs.models.instance import SchedulerInput

_ACADEMIC_YEAR_PATTERN = re.compile(r"^(\d{4})-(\d{4})$")


def academic_year_label(start_year: int) -> str:
    """Return the conventional label for an academic year beginning in July."""
    if start_year < 1 or start_year >= 9999:
        raise ValueError("academic year start must be between 1 and 9998")
    return f"{start_year:04d}-{start_year + 1:04d}"


def academic_year_start_year(value: str) -> int:
    """Parse a consecutive ``YYYY-YYYY`` academic-year label."""
    match = _ACADEMIC_YEAR_PATTERN.fullmatch(str(value).strip())
    if match is None:
        raise ValueError("academic year must use YYYY-YYYY format")
    start, end = (int(part) for part in match.groups())
    if end != start + 1:
        raise ValueError("academic year must contain consecutive years")
    return start


def first_week_start_for_academic_year(value: str) -> date:
    """Return the Monday beginning the week that contains July 1."""
    july_first = date(academic_year_start_year(value), 7, 1)
    return july_first - timedelta(days=july_first.weekday())


def academic_year_for_date(day: date | None = None) -> str:
    """Return the academic year containing ``day`` under the July-1-week rule."""
    day = day or date.today()
    candidate = academic_year_label(day.year)
    start_year = day.year if day >= first_week_start_for_academic_year(candidate) else day.year - 1
    return academic_year_label(start_year)


def week_start_choices(
    selected: date,
    *,
    academic_year: str | None = None,
    weeks_before: int = 4,
    weeks_after: int = 4,
) -> list[date]:
    """Candidate week-1 Mondays around the conventional July-1 anchor.

    The workspace's current value is retained even when it falls outside
    the window, so a previously saved choice never disappears.
    """
    if academic_year is not None:
        try:
            anchor = first_week_start_for_academic_year(academic_year)
        except ValueError:
            anchor = selected - timedelta(days=selected.weekday())
    else:
        anchor = selected - timedelta(days=selected.weekday())
    choices = sorted(
        anchor + timedelta(weeks=offset)
        for offset in range(-weeks_before, weeks_after + 1)
    )
    if selected not in choices:
        choices = sorted([*choices, selected])
    return choices


def rebase_week_start(instance: SchedulerInput, value: date) -> SchedulerInput:
    """Move the week-1 anchor, keeping real-world dates and weeks fixed.

    Dated inputs (days off, closures, overrides, special rotations) keep
    their calendar dates and week-based inputs keep their week numbers;
    only the mapping between the two moves. Dates that fall outside the
    redefined year are reported by ordinary instance validation.
    """
    if value.weekday() != 0:
        raise ValueError("the annual calendar must start on a Monday")
    if value == instance.calendar.first_week_start:
        return instance
    calendar = instance.calendar.model_copy(update={"first_week_start": value})
    return instance.revised(calendar=calendar)


def academic_year_choices(
    selected: str,
    *,
    today: date | None = None,
    years_before: int = 3,
    years_after: int = 5,
) -> list[str]:
    """Build a rolling year selector while retaining the workspace's current value."""
    center = academic_year_start_year(academic_year_for_date(today))
    starts = set(range(center - years_before, center + years_after + 1))
    try:
        starts.add(academic_year_start_year(selected))
    except ValueError:
        pass
    choices = [academic_year_label(start) for start in sorted(starts)]
    if selected not in choices:
        choices.insert(0, selected)
    return choices


def rebase_academic_year(instance: SchedulerInput, value: str) -> SchedulerInput:
    """Move a workspace to another academic year, including absolute-date inputs.

    Week-based inputs retain their week numbers. Calendar dates such as individual
    days off, clinic closures, and capacity overrides retain their month and day in
    the corresponding year of the newly selected academic year.
    """
    normalized = academic_year_label(academic_year_start_year(value))
    if normalized == instance.academic_year:
        # A zero-year move shifts no dates, so there is nothing to rebase.
        # Returning early also preserves a customized week-1 start and the
        # generated through-today locks a name-only save must not disturb.
        return instance

    try:
        previous_start_year = academic_year_start_year(instance.academic_year)
    except ValueError:
        previous_start_year = academic_year_start_year(
            academic_year_for_date(instance.calendar.first_week_start)
        )
    next_start_year = academic_year_start_year(normalized)
    year_delta = next_start_year - previous_start_year

    residents = [
        resident.model_copy(
            update={
                "days_off": [_shift_year(day, year_delta) for day in resident.days_off],
            }
        )
        for resident in instance.residents
    ]
    sites = [
        site.model_copy(
            update={
                "capacity_overrides": [
                    override.model_copy(
                        update={"date": _shift_year(override.date, year_delta)}
                    )
                    for override in site.capacity_overrides
                ],
                "closure_days": [
                    closure.model_copy(
                        update={"date": _shift_year(closure.date, year_delta)}
                    )
                    for closure in site.closure_days
                ],
            }
        )
        for site in instance.clinic_policy.sites
    ]
    clinic_policy = instance.clinic_policy.model_copy(
        update={
            "sites": sites,
            # ClinicPolicy serializes this compatibility view alongside the
            # clinic-owned closures, so both representations must move together.
            "closure_days": [
                closure.model_copy(
                    update={"date": _shift_year(closure.date, year_delta)}
                )
                for closure in instance.clinic_policy.closure_days
            ],
        }
    )
    calendar = instance.calendar.model_copy(
        update={"first_week_start": first_week_start_for_academic_year(normalized)}
    )
    special_rotations = [
        special.model_copy(
            update={
                "start_date": _shift_year(special.start_date, year_delta),
                "end_date": _shift_year(special.end_date, year_delta),
            }
        )
        for special in instance.special_rotations
    ]
    return instance.revised(
        academic_year=normalized,
        calendar=calendar,
        residents=residents,
        clinic_policy=clinic_policy,
        special_rotations=special_rotations,
        # These pins came from the previous year's solved schedule. Manual
        # week-based choices remain useful, but generated through-today locks do not.
        locks=[lock for lock in instance.locks if lock.source != "through_today"],
    )


def _shift_year(value: date, delta: int) -> date:
    try:
        return value.replace(year=value.year + delta)
    except ValueError:
        # A leap-day request becomes the last valid day of February.
        return value.replace(year=value.year + delta, day=28)
