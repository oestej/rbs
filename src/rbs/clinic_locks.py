"""Shared lock semantics for solved clinic-schedule occurrences."""

from __future__ import annotations

from datetime import date

from rbs.models.clinic import clinic_slot_date
from rbs.models.enums import Session, Weekday
from rbs.models.instance import SchedulerInput, SchedulingCase, SolverProblem
from rbs.models.schedule import AssignedClinic, Schedule

ClinicOccurrenceKey = tuple[str, int, Weekday, Session]
ClinicLockContext = SchedulerInput | SchedulingCase | SolverProblem


def clinic_slot_is_automatically_locked(
    instance: ClinicLockContext,
    slot: AssignedClinic,
    week: int,
    *,
    today: date | None = None,
) -> bool:
    """Whether the through-today setting protects this clinic occurrence."""
    return (
        not slot.automatic_lock_exempt
        and clinic_slot_is_in_automatic_lock_window(
            instance,
            slot,
            week,
            today=today,
        )
    )


def clinic_slot_is_in_automatic_lock_window(
    instance: ClinicLockContext,
    slot: AssignedClinic,
    week: int,
    *,
    today: date | None = None,
) -> bool:
    """Whether the setting would lock this date absent a manual exception."""
    calendar_day = clinic_slot_date(
        instance.calendar.first_week_start,
        week,
        slot.weekday,
    )
    cutoff = getattr(instance, "clinic_lock_cutoff_date", None)
    if cutoff is not None:
        return calendar_day <= cutoff
    if not getattr(instance, "lock_through_today", False):
        return False
    return calendar_day <= (today or date.today())


def clinic_slot_is_locked(
    instance: ClinicLockContext,
    slot: AssignedClinic,
    week: int,
    *,
    today: date | None = None,
) -> bool:
    """Return the effective manual-or-automatic lock for one occurrence."""
    return slot.locked or clinic_slot_is_automatically_locked(
        instance,
        slot,
        week,
        today=today,
    )


def reference_clinic_slot_map(
    instance: ClinicLockContext,
    schedule: Schedule | None,
) -> dict[ClinicOccurrenceKey, AssignedClinic]:
    """Expand a compatible schedule into week-specific clinic occurrences."""
    if schedule is None or schedule.meta.academic_year != instance.academic_year:
        return {}
    resident_ids = instance.residents_by_id
    valid_weeks = set(range(1, instance.calendar.weeks + 1))
    result: dict[ClinicOccurrenceKey, AssignedClinic] = {}
    for assignment in schedule.assignments:
        if assignment.resident_id not in resident_ids:
            continue
        vacation = set(assignment.vacation_weeks_during_block)
        for slot in assignment.clinic_slots:
            weeks = [slot.week] if slot.week is not None else assignment.weeks
            for week in weeks:
                if week not in valid_weeks or week in vacation:
                    continue
                result.setdefault(
                    (
                        assignment.resident_id,
                        week,
                        slot.weekday,
                        slot.session,
                    ),
                    slot,
                )
    return result


def locked_clinic_states(
    instance: ClinicLockContext,
    schedule: Schedule | None,
    *,
    today: date | None = None,
) -> dict[ClinicOccurrenceKey, bool]:
    """Return locked prior occurrences and whether each is clinic (vs. Admin)."""
    return {
        key: not slot.admin
        for key, slot in reference_clinic_slot_map(instance, schedule).items()
        if clinic_slot_is_locked(instance, slot, key[1], today=today)
    }


def locked_clinic_sites(
    instance: ClinicLockContext,
    schedule: Schedule | None,
    *,
    today: date | None = None,
) -> dict[ClinicOccurrenceKey, str]:
    """Return site assignments that a clinic lock makes immutable."""
    return {
        key: slot.site
        for key, slot in reference_clinic_slot_map(instance, schedule).items()
        if slot.site is not None
        and not slot.admin
        and clinic_slot_is_locked(instance, slot, key[1], today=today)
    }


def automatic_clinic_lock_count(
    instance: ClinicLockContext,
    schedule: Schedule | None,
    *,
    today: date | None = None,
) -> int:
    """Count clinic occurrences protected by the through-today setting."""
    return sum(
        clinic_slot_is_automatically_locked(instance, slot, key[1], today=today)
        for key, slot in reference_clinic_slot_map(instance, schedule).items()
    )
