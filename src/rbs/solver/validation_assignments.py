"""Schedule validation: assignments and clinic-slot placement."""

from __future__ import annotations

from rbs.models.clinic import clinic_slot_date
from rbs.models.instance import SolverProblem
from rbs.models.rotation import RotationBlockConfig
from rbs.models.schedule import Schedule


def _validate_assignments(
    instance: SolverProblem,
    schedule: Schedule,
    expected_weeks: set[int],
    block_vacations: dict[tuple[str, str, int, int], set[int]],
    block_rules: dict[tuple[str, str, int, int], RotationBlockConfig],
    errors: list[str],
) -> None:
    for assignment in schedule.assignments:
        resident = instance.residents_by_id.get(assignment.resident_id)
        rotation = instance.rotations_by_id.get(assignment.rotation_id)
        if resident is None:
            errors.append(f"assignment references unknown resident {assignment.resident_id!r}")
            continue
        if rotation is None:
            errors.append(f"assignment references unknown rotation {assignment.rotation_id!r}")
            continue
        _validate_assignment_header(instance, assignment, rotation, expected_weeks, errors)
        _record_assignment_vacations(
            instance,
            assignment,
            resident,
            rotation,
            block_vacations,
            block_rules,
            errors,
        )
        if rotation.away and any(not slot.manual_override for slot in assignment.clinic_slots):
            errors.append(
                f"{assignment.resident_id} {assignment.rotation_id}: clinic is scheduled "
                "during an Away rotation"
            )
        for slot in assignment.clinic_slots:
            _validate_assignment_clinic_slot(
                instance,
                assignment,
                resident,
                slot,
                errors,
            )


def _validate_assignment_header(
    instance: SolverProblem,
    assignment,
    rotation,
    expected_weeks: set[int],
    errors: list[str],
) -> None:
    label = f"{assignment.resident_id} {assignment.rotation_id}"
    if assignment.kind is not rotation.kind:
        errors.append(
            f"{label}: assignment kind {assignment.kind} "
            f"does not match catalog kind {rotation.kind}"
        )
    fallback = instance.is_elective_fallback_rotation(
        assignment.rotation_id,
        instance.residents_by_id[assignment.resident_id].pgy,
        assignment.block_duration_weeks,
    )
    if assignment.elective_fallback != (assignment.elective and fallback):
        errors.append(f"{label}: Elective Clinic assignments must carry the fallback marker")
    resident = instance.residents_by_id[assignment.resident_id]
    option = instance.electives.option_for(assignment.rotation_id)
    if assignment.elective and not (
        (option is not None and option.allows(resident.pgy, assignment.block_duration_weeks))
        or fallback
    ):
        errors.append(f"{label}: assignment is marked Elective but the service is not eligible")
    if rotation.kind.value == "elective" and not assignment.elective:
        errors.append(f"{label}: standalone Elective assignment is missing its Elective marker")
    outside = set(assignment.weeks) - expected_weeks
    if outside:
        errors.append(f"{label}: weeks outside calendar {sorted(outside)}")


def _record_assignment_vacations(
    instance: SolverProblem,
    assignment,
    resident,
    rotation,
    block_vacations: dict[tuple[str, str, int, int], set[int]],
    block_rules: dict[tuple[str, str, int, int], RotationBlockConfig],
    errors: list[str],
) -> None:
    overlap = set(assignment.weeks) & instance.resident_scheduling_vacation_weeks(resident.id)
    if assignment.block_start_week is None or assignment.block_duration_weeks is None:
        _validate_unshaped_vacation(assignment, resident, rotation, overlap, errors)
        return
    try:
        config = rotation.block_config(resident.pgy, assignment.block_duration_weeks)
    except KeyError:
        errors.append(
            f"{assignment.resident_id} {assignment.rotation_id}: no "
            f"{assignment.block_duration_weeks}-week "
            f"{instance.training_level_label(resident.pgy, compact=True)} "
            "block configuration"
        )
        return
    key = (
        assignment.resident_id,
        assignment.rotation_id,
        assignment.block_start_week,
        assignment.block_duration_weeks,
    )
    block_vacations[key].update(overlap)
    block_rules[key] = config
    if overlap and not config.vacation.allowed:
        errors.append(
            f"{assignment.resident_id} vacation week(s) {sorted(overlap)} are on "
            f"non-vacationable {assignment.rotation_id} block"
        )


def _validate_unshaped_vacation(
    assignment,
    resident,
    rotation,
    overlap: set[int],
    errors: list[str],
) -> None:
    if not overlap:
        return
    try:
        configs = rotation.pgy_rule(resident.pgy).block_configs
    except KeyError:
        configs = []
    if not any(config.vacation.allowed for config in configs):
        errors.append(
            f"{assignment.resident_id} vacation week(s) {sorted(overlap)} are on "
            f"non-vacationable {assignment.rotation_id}"
        )


def _validate_assignment_clinic_slot(
    instance: SolverProblem,
    assignment,
    resident,
    slot,
    errors: list[str],
) -> None:
    display_week = slot.week or assignment.start_week
    if slot.site is not None and slot.site not in instance.clinic_policy.site_ids:
        errors.append(
            f"{assignment.resident_id} week {display_week}: "
            f"clinic site {slot.site!r} is not configured"
        )
    allowed_sites = instance.clinic_policy.resolve_site_ids(slot.allowed_sites)
    if not slot.manual_override and allowed_sites and slot.site not in allowed_sites:
        errors.append(
            f"{assignment.resident_id} week {display_week}: clinic site "
            f"{slot.site or 'none'} is not allowed for "
            f"{slot.weekday.value} {slot.session.value}"
        )
    for week in [slot.week] if slot.week is not None else assignment.weeks:
        _validate_clinic_slot_week(instance, assignment, resident, slot, week, errors)


def _validate_clinic_slot_week(
    instance: SolverProblem,
    assignment,
    resident,
    slot,
    week: int,
    errors: list[str],
) -> None:
    if instance.is_academic_half_day(week, slot.weekday, slot.session):
        errors.append(f"{assignment.resident_id} week {week}: clinic overlaps academic half day")
    calendar_day = clinic_slot_date(
        instance.calendar.first_week_start,
        week,
        slot.weekday,
    )
    scheduled_site = None if slot.admin else (slot.site or instance.clinic_policy.primary_site_id)
    if scheduled_site is not None and instance.clinic_policy.is_site_closed(
        scheduled_site,
        calendar_day,
    ):
        _append_closed_clinic_error(
            instance,
            assignment.resident_id,
            week,
            scheduled_site,
            calendar_day,
            errors,
        )
    special_blocks = instance.special_rotations_for_resident(
        resident.id,
        calendar_day=calendar_day,
        session=slot.session,
    )
    if special_blocks:
        errors.append(
            f"{assignment.resident_id} week {week}: clinic overlaps "
            + ", ".join(special.name for special in special_blocks)
        )
    elif not slot.manual_override and (
        week in resident.vacation_weeks or calendar_day in resident.days_off
    ):
        errors.append(
            f"{assignment.resident_id} week {week}: clinic is scheduled on blocked time "
            f"{slot.weekday.value} {slot.session.value}"
        )


def _append_closed_clinic_error(
    instance: SolverProblem,
    resident_id: str,
    week: int,
    site_id: str,
    calendar_day,
    errors: list[str],
) -> None:
    site_name = (
        instance.clinic_policy.site_name(site_id)
        if site_id in instance.clinic_policy.site_ids
        else site_id
    )
    closure = instance.clinic_policy.closure_on(calendar_day)
    closure_name = f" ({closure.name})" if closure is not None and closure.name else ""
    errors.append(
        f"{resident_id} week {week}: {site_name} is closed on "
        f"{calendar_day:%B} {calendar_day.day}, {calendar_day.year}{closure_name}"
    )
