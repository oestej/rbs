"""Public helpers for comparing a solve with its reference solution."""

from __future__ import annotations

from rbs.clinic_locks import (
    locked_clinic_sites,
    locked_clinic_states,
    reference_clinic_slot_map,
)
from rbs.models.enums import Session, Weekday
from rbs.models.instance import SolverProblem
from rbs.models.schedule import AssignedClinic, Schedule

RotationWeekKey = tuple[str, int]
RotationAssignmentKey = tuple[str, bool]
ClinicHalfDayKey = tuple[str, int, Weekday, Session]


def reference_rotation_grid(
    problem: SolverProblem,
    solution: Schedule | None,
) -> dict[RotationWeekKey, RotationAssignmentKey]:
    """Return comparable resident-week rotations from an existing solution."""
    if solution is None or solution.meta.academic_year != problem.academic_year:
        return {}
    resident_ids = problem.residents_by_id
    valid_weeks = set(range(1, problem.calendar.weeks + 1))
    return {
        (assignment.resident_id, week): (
            assignment.rotation_id,
            assignment.elective,
        )
        for assignment in solution.assignments
        if assignment.resident_id in resident_ids
        for week in assignment.weeks
        if week in valid_weeks
    }


def reference_clinic_half_days(
    problem: SolverProblem,
    solution: Schedule | None,
) -> set[ClinicHalfDayKey]:
    """Return non-admin clinic half-days represented by an existing solution."""
    if solution is None or solution.meta.academic_year != problem.academic_year:
        return set()
    resident_ids = problem.residents_by_id
    valid_weeks = set(range(1, problem.calendar.weeks + 1))
    result: set[ClinicHalfDayKey] = set()
    for assignment in solution.assignments:
        if assignment.resident_id not in resident_ids:
            continue
        vacation = set(assignment.vacation_weeks_during_block)
        for slot in assignment.clinic_slots:
            if slot.admin:
                continue
            weeks = [slot.week] if slot.week is not None else assignment.weeks
            result.update(
                (assignment.resident_id, week, slot.weekday, slot.session)
                for week in weeks
                if week in valid_weeks and week not in vacation
            )
    return result


def reference_clinic_sites(
    problem: SolverProblem,
    solution: Schedule | None,
) -> dict[ClinicHalfDayKey, str]:
    """Return the prior site for each comparable clinic half-day."""
    if solution is None or solution.meta.academic_year != problem.academic_year:
        return {}
    resident_ids = problem.residents_by_id
    valid_weeks = set(range(1, problem.calendar.weeks + 1))
    result: dict[ClinicHalfDayKey, str] = {}
    for assignment in solution.assignments:
        if assignment.resident_id not in resident_ids:
            continue
        vacation = set(assignment.vacation_weeks_during_block)
        for slot in assignment.clinic_slots:
            if slot.admin or slot.site is None:
                continue
            weeks = [slot.week] if slot.week is not None else assignment.weeks
            for week in weeks:
                if week in valid_weeks and week not in vacation:
                    result[
                        assignment.resident_id,
                        week,
                        slot.weekday,
                        slot.session,
                    ] = slot.site
    return result


def reference_locked_clinic_states(
    problem: SolverProblem,
    solution: Schedule | None,
) -> dict[ClinicHalfDayKey, bool]:
    """Return prior clinic/Admin occurrences protected from a re-solve."""
    return locked_clinic_states(problem, solution)


def reference_locked_clinic_sites(
    problem: SolverProblem,
    solution: Schedule | None,
) -> dict[ClinicHalfDayKey, str]:
    """Return prior clinic sites protected from post-solve reallocation."""
    return locked_clinic_sites(problem, solution)


def reference_clinic_metadata(
    problem: SolverProblem,
    solution: Schedule | None,
) -> dict[ClinicHalfDayKey, AssignedClinic]:
    """Return prior per-occurrence editor metadata for compatible output slots."""
    return reference_clinic_slot_map(problem, solution)


def changed_resident_weeks(
    problem: SolverProblem,
    reference: Schedule | None,
    solution: Schedule,
) -> tuple[int, int]:
    """Return ``(changed, compared)`` resident-week rotation placements."""
    before = reference_rotation_grid(problem, reference)
    after = reference_rotation_grid(problem, solution)
    return sum(after.get(key) != assignment for key, assignment in before.items()), len(before)
