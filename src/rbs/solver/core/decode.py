from __future__ import annotations

from collections import Counter

from rbs.models.enums import RotationKind, SolverEngineName, SolverStatus
from rbs.models.schedule import (
    AssignedClinic,
    Assignment,
    Schedule,
    ScheduleMeta,
    ScheduleMetrics,
)
from rbs.solver.core import kinds as rotation_kinds
from rbs.solver.core.clinic_allocation import (
    assign_clinic_sites,
    clinic_attending_total,
    clinic_block_weekly_headcount,
    clinic_weekly_attendings,
    clinic_weekly_sessions,
)
from rbs.solver.core.context import CompiledProblem
from rbs.solver.planning import Occurrence, weeks_covered
from rbs.solver.reference import changed_resident_weeks
from rbs.solver.validation import validate_schedule


def decode_solution(
    problem: CompiledProblem,
    solver,
    solver_status: SolverStatus,
    *,
    solver_objective: float | None,
    solver_best_bound: float | None,
) -> Schedule:
    context = problem.context
    instance = context.instance
    chosen: list[tuple[Occurrence, int]] = []
    for occurrence in context.occurrences:
        selected = [
            start
            for start in context.starts[occurrence.key]
            if solver.Value(context.placements[occurrence.key, start])
        ]
        if len(selected) > 1:
            raise RuntimeError(f"multiple starts for {occurrence.key}: {selected}")
        if selected:
            chosen.append((occurrence, selected[0]))

    assignments: list[Assignment] = []
    for occurrence, start in chosen:
        rotation = context.rotations[occurrence.rotation_id]
        resident = context.residents[occurrence.resident_id]
        block_weeks = weeks_covered(start, occurrence.duration_weeks)
        vacation = [week for week in block_weeks if week in resident.vacation_weeks]
        locked = [
            week
            for week in block_weeks
            if instance.locked_assignment(occurrence.resident_id, week)
            == (occurrence.rotation_id, occurrence.elective)
        ]
        solved_clinic = _solved_clinic(
            occurrence,
            rotation.kind,
            problem.clinic.decisions,
            solver,
        )
        clinic_slots = _decode_clinic_slots(
            occurrence,
            rotation,
            block_weeks,
            solved_clinic,
            problem,
            solver,
            set(vacation),
        )
        clinic_slots = _add_resident_clinic_half_days(
            instance,
            resident,
            rotation,
            block_weeks,
            set(vacation),
            clinic_slots,
        )
        assignments.append(
            Assignment(
                resident_id=occurrence.resident_id,
                rotation_id=occurrence.rotation_id,
                kind=rotation.kind,
                elective=occurrence.elective,
                elective_fallback=occurrence.elective_fallback,
                start_week=start,
                end_week=start + occurrence.duration_weeks - 1,
                weeks=block_weeks,
                block_start_week=start,
                block_duration_weeks=occurrence.duration_weeks,
                clinic_slots=clinic_slots,
                vacation_weeks_during_block=vacation,
                locked_weeks=locked,
            )
        )

    assignments.sort(key=lambda item: (item.resident_id, item.start_week, item.rotation_id))
    allocation_result = assign_clinic_sites(
        instance,
        assignments,
        reference_schedule=problem.reference_schedule,
    )
    final_status = final_status_for(
        solver_status,
        postprocessed=allocation_result.postprocessed,
        valid=True,
    )

    scheduled = {occurrence.resident_id for occurrence, _start in chosen}
    unassigned = [resident.id for resident in instance.residents if resident.id not in scheduled]
    primary_site_id = instance.clinic_policy.primary_site_id
    attending_total = clinic_attending_total(
        instance,
        assignments,
        primary_site_id,
    )
    weekly = clinic_weekly_attendings(
        instance,
        assignments,
        primary_site_id,
    )
    loaded = [count for count in weekly.values() if count > 0]
    weekly_sessions = list(clinic_weekly_sessions(instance, assignments).values())
    weekly_blocks = list(clinic_block_weekly_headcount(instance, assignments).values())
    metrics = ScheduleMetrics(
        elective_fallback_blocks=sum(
            1 for assignment in assignments if assignment.elective_fallback
        ),
        elective_preference_rank_counts=_elective_preference_rank_counts(
            context,
            chosen,
        ),
        clinic_block_weekly_min=min(weekly_blocks) if weekly_blocks else None,
        clinic_block_weekly_max=max(weekly_blocks) if weekly_blocks else None,
        clinic_block_weekly_spread=(
            max(weekly_blocks) - min(weekly_blocks) if weekly_blocks else None
        ),
        clinic_weekly_session_min=min(weekly_sessions) if weekly_sessions else None,
        clinic_weekly_session_max=max(weekly_sessions) if weekly_sessions else None,
        clinic_weekly_session_spread=(
            max(weekly_sessions) - min(weekly_sessions) if weekly_sessions else None
        ),
        primary_site_attending_sessions=attending_total,
        primary_site_weekly_min=min(loaded) if loaded else None,
        primary_site_weekly_max=max(loaded) if loaded else None,
        primary_site_weekly_spread=(max(loaded) - min(loaded)) if loaded else None,
        allocation_target_sessions=allocation_result.target_sessions,
        allocation_assigned_sessions=allocation_result.assigned_sessions,
        allocation_target_shortfall=allocation_result.shortfall,
    )
    notes = _notes(
        instance,
        attending_total,
        loaded,
        allocation_result,
        stabilizing=problem.clinic.stability_comparisons > 0,
        elective_fallback_blocks=metrics.elective_fallback_blocks,
    )
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.CP_SAT,
            status=final_status,
            solver_status=solver_status,
            solver_objective=solver_objective,
            solver_best_bound=solver_best_bound,
            postprocessed=allocation_result.postprocessed,
            metrics=metrics,
            notes=notes,
        ),
        assignments=assignments,
        unassigned=unassigned,
    )
    changed_weeks, compared_weeks = changed_resident_weeks(
        instance,
        problem.reference_schedule,
        schedule,
    )
    if compared_weeks:
        schedule.meta.notes.append(
            "re-solve stability: preserved "
            f"{compared_weeks - changed_weeks}/{compared_weeks} existing "
            f"resident-week placements; {changed_weeks} changed"
        )
    validation = validate_schedule(instance, schedule)
    final_status = final_status_for(
        solver_status,
        postprocessed=allocation_result.postprocessed,
        valid=not validation.errors,
    )
    allocation_shortfalls = [
        f"{instance.clinic_policy.site_name(clinic_id)} target shortfall: "
        f"{target - allocation_result.assigned_by_clinic.get(clinic_id, 0)} "
        "clinic session(s)"
        for clinic_id, target in allocation_result.target_by_clinic.items()
        if allocation_result.assigned_by_clinic.get(clinic_id, 0) < target
    ]
    schedule.meta = schedule.meta.model_copy(
        update={
            "status": final_status,
            "validation_errors": list(validation.errors),
            "validation_warnings": [
                *validation.warnings,
                *allocation_shortfalls,
            ],
        }
    )
    return schedule


def _elective_preference_rank_counts(
    context,
    chosen: list[tuple[Occurrence, int]],
) -> list[int]:
    """Count selected services against each resident's earliest matching requests."""
    selected: Counter[tuple[str, str, int]] = Counter(
        (
            occurrence.resident_id,
            occurrence.rotation_id,
            occurrence.duration_weeks,
        )
        for occurrence, _start in chosen
        if occurrence.preference_managed and not occurrence.elective_fallback
    )
    maximum_rank = max(
        (len(resident.elective_preferences) for resident in context.residents.values()),
        default=0,
    )
    counts = [0] * maximum_rank
    for resident in context.residents.values():
        for rank, request in enumerate(resident.elective_preferences):
            key = resident.id, request.rotation_id, request.duration_weeks
            if selected[key] <= 0:
                continue
            selected[key] -= 1
            counts[rank] += 1
    return counts


def final_status_for(
    solver_status: SolverStatus, *, postprocessed: bool, valid: bool
) -> SolverStatus:
    if not valid:
        return SolverStatus.UNKNOWN
    if postprocessed and solver_status is SolverStatus.OPTIMAL:
        return SolverStatus.FEASIBLE
    return solver_status


def _solved_clinic(
    occurrence,
    kind,
    decisions,
    solver,
) -> list[AssignedClinic] | None:
    decision = decisions.get(occurrence.key)
    if decision is None:
        return None
    return [
        AssignedClinic(
            weekday=slot.weekday,
            session=slot.session,
            allowed_sites=slot.sites,
            admin=kind is RotationKind.CLINIC,
        )
        for slot in decision.selected_slots(solver)
        if slot.weekday is not None and slot.session is not None
    ]


def _decode_clinic_slots(
    occurrence: Occurrence,
    rotation,
    weeks: list[int],
    solved_clinic: list[AssignedClinic] | None,
    problem: CompiledProblem,
    solver,
    vacation: set[int],
) -> list[AssignedClinic]:
    if rotation.clinic_hours_disabled:
        return []
    in_clinic = problem.clinic.in_clinic
    slots: list[AssignedClinic] = []
    admin_slots = []
    resident = problem.context.residents[occurrence.resident_id]
    instance = problem.context.instance
    if rotation.kind is RotationKind.CLINIC and solved_clinic:
        admin_slots = [(slot.weekday, slot.session) for slot in solved_clinic if slot.admin]

    weekly = False
    for week in weeks:
        if week in vacation:
            continue
        for weekday, session in admin_slots:
            if instance.resident_clinic_is_blocked(
                resident.id,
                week,
                weekday,
                session,
            ):
                continue
            if instance.is_academic_half_day(week, weekday, session):
                continue
            weekly = True
            slots.append(
                AssignedClinic(
                    weekday=weekday,
                    session=session,
                    admin=True,
                    week=week,
                )
            )
        # Only the occurrence the solver actually placed over this week can have a
        # true literal, so the resident-keyed bucket needs no occurrence filter.
        for _keys, weekday, session, literal in in_clinic.get(
            (occurrence.resident_id, week), ()
        ):
            if not solver.Value(literal):
                continue
            weekly = True
            slots.append(
                AssignedClinic(
                    weekday=weekday,
                    session=session,
                    week=week,
                )
            )
    if weekly or solved_clinic is not None:
        slots.sort(key=lambda item: (item.week or 0, item.weekday, item.session, item.admin))
        return slots
    return rotation_kinds.clinic_slots_for(
        rotation,
        solved_clinic,
        problem.context.instance.clinic_policy,
    )


def _add_resident_clinic_half_days(
    instance,
    resident,
    rotation,
    weeks: list[int],
    vacation: set[int],
    slots: list[AssignedClinic],
) -> list[AssignedClinic]:
    """Add recurring resident continuity sessions unless the block is Away."""
    if rotation.away or not resident.clinic_half_days:
        return slots
    combined = list(slots)
    for week in weeks:
        if week in vacation:
            continue
        for half_day in resident.clinic_half_days:
            if instance.resident_clinic_is_blocked(
                resident.id,
                week,
                half_day.weekday,
                half_day.session,
            ):
                continue
            if instance.is_academic_half_day(
                week,
                half_day.weekday,
                half_day.session,
            ):
                continue
            if any(
                slot.week in {None, week}
                and slot.weekday is half_day.weekday
                and slot.session is half_day.session
                for slot in combined
            ):
                continue
            combined.append(
                AssignedClinic(
                    weekday=half_day.weekday,
                    session=half_day.session,
                    allowed_sites=half_day.sites,
                    week=week,
                )
            )
    combined.sort(
        key=lambda item: (
            item.week or 0,
            item.weekday,
            item.session,
            item.admin,
        )
    )
    return combined


def _notes(
    instance,
    attending_total,
    loaded,
    allocation_result,
    *,
    stabilizing: bool,
    elective_fallback_blocks: int,
) -> list[str]:
    policy = instance.clinic_policy
    primary_name = policy.site_name(policy.primary_site_id)
    clinic_names = "/".join(site.name for site in policy.sites)
    notes = [
        "CP-SAT block and clinic-session model",
        "hard: curriculum, vacations, capacity, grouping, locks, sequencing, consecutive caps",
        (
            "solver objective: minimize Elective fallbacks, maximize each global "
            "preference rank, minimize changes from the existing draft, then "
            f"weighted {primary_name} attending load and clinic balance before "
            f"flexible {clinic_names} site post-processing"
            if stabilizing
            else "solver objective: minimize Elective fallbacks, maximize each global "
            f"preference rank, then weighted {primary_name} attending load and clinic "
            f"balance before flexible {clinic_names} site post-processing"
        ),
        "clinic allocation is a validated heuristic post-process; final status "
        "is not reported as optimal when that post-process changes the schedule",
        *rotation_kinds.notes(),
    ]
    if instance.locks:
        notes.append(f"{len(instance.locks)} lock(s) applied")
    if elective_fallback_blocks:
        notes.append(
            f"{elective_fallback_blocks} Elective block(s) assigned to "
            "Clinic (Elective fallback)"
        )
    individual_days_off = sum(len(resident.days_off) for resident in instance.residents)
    if individual_days_off:
        notes.append(f"{individual_days_off} individual resident day(s) off omitted from clinic")
    if instance.special_rotations:
        notes.append(
            f"{len(instance.special_rotations)} special rotation(s) applied to clinic"
        )
    notes.append(f"{primary_name} attending-sessions: {attending_total}")
    if loaded:
        notes.append(
            f"{primary_name} attending-sessions per week: {min(loaded)}–{max(loaded)}"
        )
    for rule in policy.allocation_rules_for():
        clinic_name = policy.site_name(rule.clinic_id)
        assigned = allocation_result.assigned_by_clinic.get(rule.clinic_id, 0)
        target = allocation_result.target_by_clinic.get(rule.clinic_id, 0)
        notes.append(f"{clinic_name} sessions: {assigned}/{target} targeted")
    return notes
