"""Schedule validation: structural checks on attending work.

Attending rules such as weekly totals, category ranges, and preferred
placements are reported by :mod:`rbs.attending_schedule` as non-blocking
schedule checks, because a workspace can be configured incrementally. The
checks here reject invalid new placements, including work for unknown attendings
or on unavailable half-days. Protected records may remain as nonparticipating
input under temporary date and placement conflicts; effective projections hide
them from active work and clinic coverage.
"""

from __future__ import annotations

from collections import Counter

from rbs.models.attending import AttendingWorkType, EffectiveAttendingWeek
from rbs.models.clinic import clinic_slot_date
from rbs.models.enums import Session, Weekday
from rbs.models.instance import SolverProblem
from rbs.models.schedule import Schedule
from rbs.solver.validation_inherited import InheritedWork

_WEEKDAY_ORDER = {weekday: index for index, weekday in enumerate(Weekday)}


def _validate_attending_work(
    instance: SolverProblem,
    schedule: Schedule,
    errors: list[str],
    *,
    maximums: bool = True,
    inherited: InheritedWork | None = None,
) -> None:
    """Check attending work, and with ``maximums`` each clinic's preceptor limit.

    Only a solved schedule is held to the limit, and only for work the solve
    placed. Hand edits may override it in a working draft until the next
    solve, as block edits may exceed rotation capacity, and they stay
    overrides when a later lock change keeps the schedule solved.

    Work in ``inherited`` that an input edit has covered stays in a working
    draft, where the effective attending week hides it.
    """
    if not schedule.attending_work:
        return
    attendings = {attending.id: attending for attending in instance.attendings}
    site_ids = set(instance.clinic_policy.site_ids)
    weeks: dict[tuple[str, int], EffectiveAttendingWeek] = {}
    placed_precepting: Counter[tuple[str, int, Weekday, Session]] = Counter()
    override_precepting: Counter[tuple[str, int, Weekday, Session]] = Counter()
    for item in schedule.attending_work:
        attending = attendings.get(item.attending_id)
        if attending is None:
            errors.append(f"attending work references unknown attending {item.attending_id!r}")
            continue
        if item.week > instance.calendar.weeks:
            errors.append(
                f"{attending.name}: attending work in week {item.week} is outside "
                f"academic weeks 1..{instance.calendar.weeks}"
            )
            continue
        if item.work_type is AttendingWorkType.PRECEPTING_CLINIC and item.clinic_id not in site_ids:
            errors.append(
                f"{attending.name}: Precepting Clinic work references unknown clinic "
                f"{item.clinic_id!r}"
            )
            continue
        key = (attending.id, item.week)
        if key not in weeks:
            weeks[key] = instance.effective_attending_week(attending, item.week)
        effective = weeks[key]
        calendar_day = clinic_slot_date(
            instance.calendar.first_week_start,
            item.week,
            item.weekday,
        )
        half_day = "AM" if item.session is Session.MORNING else "PM"
        where = f"{attending.name} · {calendar_day:%b} {calendar_day.day} {half_day}"
        blocked = (
            item.weekday not in effective.available_weekdays
            or effective.assignment_on(item.weekday, item.session) is not None
        )
        if blocked and item.is_protected_input and (
            item.clinic_id is None or item.clinic_id in instance.attending_managed_clinic_ids
        ):
            # These records preserve accepted inputs, not active staffing. The
            # same effective projection hides them in capacity and calendars.
            continue
        if (
            inherited is not None
            and inherited.holds_attending_work(item)
            and (
                item.weekday not in effective.available_weekdays
                or effective.assignment_on(item.weekday, item.session) is not None
            )
        ):
            continue
        if item.weekday not in effective.available_weekdays:
            errors.append(
                f"{where}: scheduled work falls outside the attending's schedule dates "
                "or on vacation"
            )
        elif effective.assignment_on(item.weekday, item.session) is not None:
            errors.append(
                f"{where}: scheduled work overlaps hand-entered work or the academic "
                "half-day's Admin Time"
            )
        elif item.work_type is AttendingWorkType.PRECEPTING_CLINIC:
            assert item.clinic_id is not None
            if maximums and item.clinic_id not in instance.attending_managed_clinic_ids:
                errors.append(
                    f"{where}: Precepting Clinic work requires an attending-managed clinic"
                )
            elif (
                maximums and item.clinic_id not in attending.eligible_clinic_ids
                and not item.manual_override
            ):
                errors.append(
                    f"{where}: attending is not eligible to precept at "
                    f"{instance.clinic_policy.site_name(item.clinic_id)}"
                )
            # Whether work may override the maximum depends on who placed it,
            # never on a lock: through-today locking is not part of a saved
            # document, and any lock can later be removed. A solve records the
            # work it kept over a maximum as an override for the same reason.
            counts = override_precepting if item.manual_override else placed_precepting
            counts[item.clinic_id, item.week, item.weekday, item.session] += 1
    if maximums:
        _validate_precepting_maximums(instance, placed_precepting, override_precepting, errors)


def _validate_precepting_maximums(
    instance: SolverProblem,
    placed: Counter[tuple[str, int, Weekday, Session]],
    overrides: Counter[tuple[str, int, Weekday, Session]],
    errors: list[str],
) -> None:
    """Reject placed precepting beyond a clinic's maximum for a half-day.

    Hand-entered, hand-placed, and kept-over-the-maximum precepting count toward
    the maximum, but they may exceed it by themselves as an override. Only work
    the solve placed can break the maximum here.
    """
    if not placed:
        return
    hand_entered: Counter[tuple[str, int, Weekday, Session]] = Counter()
    for week in sorted({key[1] for key in placed}):
        for attending in instance.attendings:
            for assignment in instance.effective_attending_week(attending, week).assignments:
                if assignment.work_type is AttendingWorkType.PRECEPTING_CLINIC:
                    assert assignment.clinic_id is not None
                    hand_entered[
                        assignment.clinic_id, week, assignment.weekday, assignment.session
                    ] += 1
    for key, count in sorted(
        placed.items(),
        key=lambda item: (item[0][1], _WEEKDAY_ORDER[item[0][2]], item[0][3].value, item[0][0]),
    ):
        clinic_id, week, weekday, session = key
        calendar_day = clinic_slot_date(instance.calendar.first_week_start, week, weekday)
        maximum = instance.clinic_max_attendings_on(clinic_id, calendar_day, session)
        total = count + hand_entered[key] + overrides[key]
        if total > maximum:
            half_day = "AM" if session is Session.MORNING else "PM"
            errors.append(
                f"{instance.clinic_policy.site_name(clinic_id)} · {calendar_day:%b} "
                f"{calendar_day.day} {half_day}: {total} attendings precept, but at most "
                f"{maximum} may"
            )


__all__ = ["_validate_attending_work"]
