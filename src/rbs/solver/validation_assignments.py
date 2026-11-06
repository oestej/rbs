"""Schedule validation: assignments and clinic-slot placement."""

from __future__ import annotations

from rbs.models.clinic import clinic_slot_date
from rbs.models.elective import PLACEHOLDER_ELECTIVE_ID
from rbs.models.instance import SolverProblem
from rbs.models.rotation import RotationBlockConfig
from rbs.models.schedule import Schedule
from rbs.solver.clinic_sites import declared_clinic_sites
from rbs.solver.validation_inherited import InheritedWork


def _validate_assignments(
    instance: SolverProblem,
    schedule: Schedule,
    expected_weeks: set[int],
    block_vacations: dict[tuple[str, str, int, int], set[int]],
    block_rules: dict[tuple[str, str, int, int], RotationBlockConfig],
    errors: list[str],
    *,
    inherited: InheritedWork | None = None,
) -> None:
    """Check every block and session; ``inherited`` work only has to reference this problem."""
    for assignment in schedule.assignments:
        resident = instance.residents_by_id.get(assignment.resident_id)
        rotation = instance.rotations_by_id.get(assignment.rotation_id)
        if resident is None:
            errors.append(f"assignment references unknown resident {assignment.resident_id!r}")
            continue
        if rotation is None:
            errors.append(f"assignment references unknown rotation {assignment.rotation_id!r}")
            continue
        block_must_fit = inherited is None or not inherited.holds_block(assignment)
        _validate_assignment_header(
            instance,
            assignment,
            rotation,
            expected_weeks,
            errors,
            must_fit=block_must_fit,
        )
        _record_assignment_vacations(
            instance,
            assignment,
            resident,
            rotation,
            block_vacations,
            block_rules,
            errors,
            must_fit=block_must_fit,
        )
        new_slots = [
            slot
            for slot in assignment.clinic_slots
            if inherited is None or not inherited.holds_session(assignment, slot)
        ]
        if rotation.away and any(not slot.manual_override for slot in new_slots):
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
                must_fit=any(slot is new for new in new_slots),
            )


def _validate_assignment_header(
    instance: SolverProblem,
    assignment,
    rotation,
    expected_weeks: set[int],
    errors: list[str],
    *,
    must_fit: bool = True,
) -> None:
    label = f"{assignment.resident_id} {assignment.rotation_id}"
    if assignment.kind is not rotation.kind:
        errors.append(
            f"{label}: assignment kind {assignment.kind} "
            f"does not match catalog kind {rotation.kind}"
        )
    outside = set(assignment.weeks) - expected_weeks
    if outside:
        errors.append(f"{label}: weeks outside calendar {sorted(outside)}")
    if not must_fit:
        return
    fallback = instance.is_elective_fallback_rotation(
        assignment.rotation_id,
        instance.residents_by_id[assignment.resident_id].pgy,
        assignment.block_duration_weeks,
    )
    if assignment.elective_fallback != (assignment.elective and fallback):
        errors.append(f"{label}: Elective Clinic assignments must carry the fallback marker")
    resident = instance.residents_by_id[assignment.resident_id]
    option = instance.electives.option_for(assignment.rotation_id)
    # The generic placeholder is always eligible for any elective slot while
    # the option is on; it intentionally has no elective option record.
    placeholder = (
        assignment.rotation_id == PLACEHOLDER_ELECTIVE_ID
        and bool(getattr(instance, "use_placeholder_electives", False))
    )
    if assignment.elective and not (
        (option is not None and option.allows(resident.pgy, assignment.block_duration_weeks))
        or fallback
        or placeholder
    ):
        errors.append(f"{label}: assignment is marked Elective but the service is not eligible")
    if rotation.kind.value == "elective" and not assignment.elective:
        errors.append(f"{label}: standalone Elective assignment is missing its Elective marker")


def _record_assignment_vacations(
    instance: SolverProblem,
    assignment,
    resident,
    rotation,
    block_vacations: dict[tuple[str, str, int, int], set[int]],
    block_rules: dict[tuple[str, str, int, int], RotationBlockConfig],
    errors: list[str],
    *,
    must_fit: bool = True,
) -> None:
    overlap = set(assignment.weeks) & instance.resident_scheduling_vacation_weeks(resident.id)
    if assignment.block_start_week is None or assignment.block_duration_weeks is None:
        if must_fit:
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
    if not must_fit:
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
    *,
    must_fit: bool = True,
) -> None:
    display_week = slot.week or assignment.start_week
    configured_sites = set(instance.clinic_policy.site_ids)
    if slot.site is not None and slot.site not in configured_sites:
        errors.append(
            f"{assignment.resident_id} week {display_week}: "
            f"clinic site {slot.site!r} is not configured"
        )
    rotation = instance.rotations_by_id[assignment.rotation_id]
    source_sites = instance.clinic_policy.resolve_site_ids(
        declared_clinic_sites(resident, rotation, slot.weekday, slot.session)
    )
    allowed_sites = instance.clinic_policy.resolve_site_ids(
        slot.allowed_sites or declared_clinic_sites(resident, rotation, slot.weekday, slot.session)
    )
    unknown_allowed_sites = sorted(set(allowed_sites) - configured_sites)
    if unknown_allowed_sites:
        errors.append(
            f"{assignment.resident_id} week {display_week}: allowed clinic sites "
            f"are not configured {unknown_allowed_sites}"
        )
    original_site = slot.manual_override_original_site
    if original_site is not None and original_site not in configured_sites:
        errors.append(
            f"{assignment.resident_id} week {display_week}: original clinic site "
            f"{original_site!r} is not configured"
        )
    if not must_fit:
        return
    if not slot.admin and not slot.manual_override and (
        allowed_sites and slot.site not in allowed_sites
        or source_sites and slot.site not in source_sites
    ):
        errors.append(
            f"{assignment.resident_id} week {display_week}: clinic site "
            f"{slot.site or 'none'} is not allowed for "
            f"{slot.weekday.value} {slot.session.value}"
        )
    for week in [slot.week] if slot.week is not None else assignment.weeks:
        if not 1 <= week <= instance.calendar.weeks:
            continue
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
    if (
        scheduled_site is not None
        and scheduled_site in instance.clinic_policy.site_ids
        and instance.clinic_policy.is_site_closed(
            scheduled_site,
            calendar_day,
        )
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
