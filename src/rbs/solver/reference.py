"""Public helpers for comparing a solve with its reference solution."""

from __future__ import annotations

from typing import Any

from rbs.clinic_locks import (
    locked_clinic_sites,
    locked_clinic_states,
    reference_clinic_slot_map,
)
from rbs.models.clinic import clinic_slot_date
from rbs.models.enums import Session, Weekday
from rbs.models.instance import SolverProblem
from rbs.models.schedule import AssignedClinic, Schedule, SolverDiagnostic
from rbs.solver.planning import covers

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


HONORED_REFERENCE_LOCK = "honored_reference_lock"
REFERENCE_LOCK_CONFLICT = "reference_lock_conflict"


def reference_clinic_lock_barriers(
    problem: SolverProblem,
    solution: Schedule | None,
    occurrences: dict[str, Any],
    starts: dict[str, tuple[int, ...]],
) -> dict[ClinicHalfDayKey, str]:
    """Explain every reference clinic lock an extra session cannot rescue.

    Locks are the operator's special-case mechanism: they are never skipped,
    so a lock reported here stays enforced and fails the solve unless the
    operator changes the conflicting rule or unlocks the session. Each reason
    names the rule that would have to give.
    """
    locked = reference_locked_clinic_states(problem, solution)
    if not locked:
        return {}
    viable = viable_reference_clinic_locks(problem, solution, occurrences, starts)
    return {
        key: _lock_barrier(problem, occurrences, starts, key)
        for key, state in locked.items()
        if state and key not in viable
    }


def _lock_barrier(
    problem: SolverProblem,
    occurrences: dict[str, Any],
    starts: dict[str, tuple[int, ...]],
    key: ClinicHalfDayKey,
) -> str:
    """Name the rule that keeps one reference clinic lock out of reach."""
    resident_id, week, weekday, session = key
    resident = problem.residents_by_id.get(resident_id)
    if resident is not None and week in resident.vacation_weeks:
        return "a vacation week"
    if problem.is_academic_half_day(week, weekday, session):
        return "the academic half-day"
    if resident is not None:
        calendar_day = clinic_slot_date(
            problem.calendar.first_week_start, week, weekday
        )
        if calendar_day in resident.days_off:
            return "a day off"
        blocking = next(
            (
                special
                for special in problem.special_rotations_for_resident(resident_id)
                if special.blocks(calendar_day, session)
            ),
            None,
        )
        if blocking is not None:
            return f"the {blocking.name} special rotation"
    covering_locks = [
        lock
        for lock in problem.locks
        if lock.resident_id == resident_id and week in lock.weeks
    ]
    if covering_locks:
        names = sorted(
            {
                problem.rotations_by_id[lock.rotation_id].name
                for lock in covering_locks
                if lock.rotation_id in problem.rotations_by_id
            }
        )
        if names:
            return f"locked {' and '.join(names)} blocks, which offer no clinic then"
    return "no block under the current rules that can host clinic then"


def viable_reference_clinic_locks(
    problem: SolverProblem,
    solution: Schedule | None,
    occurrences: dict[str, Any],
    starts: dict[str, tuple[int, ...]],
) -> dict[ClinicHalfDayKey, bool]:
    """Return reference clinic locks an extra session could still honor.

    These are the locked sessions whose only barrier is the rotation's
    configured clinic days: the half-day is not the academic half-day, is not
    blocked by time off or a special rotation, is not a vacation week, and a
    lock-compatible block that can host clinic may still cover the week.
    Compilation schedules a one-off session for these locks (and warns)
    instead of dropping them.
    """
    locked = reference_locked_clinic_states(problem, solution)
    if not locked:
        return {}
    residents = problem.residents_by_id
    rotations = problem.rotations_by_id
    viable: dict[ClinicHalfDayKey, bool] = {}
    for key, state in locked.items():
        if not state:
            continue
        resident_id, week, weekday, session = key
        resident = residents.get(resident_id)
        if resident is None or week in resident.vacation_weeks:
            continue
        if problem.is_academic_half_day(week, weekday, session):
            continue
        if problem.resident_clinic_is_blocked(resident_id, week, weekday, session):
            continue
        if not any(
            occurrence_can_cover_week(
                occurrence, starts, problem.locks, resident_id, week
            )
            and _rotation_can_host_extra_session(
                rotations.get(occurrence.rotation_id), resident
            )
            for occurrence in occurrences.values()
        ):
            continue
        viable[key] = state
    return viable


def _rotation_can_host_extra_session(rotation: Any, resident: Any) -> bool:
    """Whether a one-off clinic session may be scheduled on a rotation.

    Only an Away block rules the session out: the resident is off site, and
    post-processing clears Away clinic slots anyway. A clinic-free service
    (ICU, Night Float) simply has its clinic-free status ignored for the
    locked session, with a warning.
    """
    del resident  # Reserved for future per-resident hosting rules.
    return rotation is not None and not rotation.away


def occurrence_can_cover_week(
    occurrence: Any,
    starts: dict[str, tuple[int, ...]],
    locks: list,
    resident_id: str,
    week: int,
) -> bool:
    """Whether one candidate occurrence may still cover a resident-week."""
    if occurrence is None or occurrence.resident_id != resident_id:
        return False
    covering = [
        lock for lock in locks if lock.resident_id == resident_id and week in lock.weeks
    ]
    if not covering:
        return any(
            covers(start, occurrence.duration_weeks, week)
            for start in starts.get(occurrence.key, ())
        )
    for lock in covering:
        if (occurrence.rotation_id, occurrence.elective) != (
            lock.rotation_id,
            lock.elective,
        ):
            return False
        if lock.exact_block and (
            occurrence.duration_weeks != len(lock.weeks)
            or lock.weeks[0] not in starts.get(occurrence.key, ())
        ):
            return False
    return True


def describe_reference_clinic_lock(
    problem: SolverProblem,
    solution: Schedule | None,
    key: ClinicHalfDayKey,
) -> str:
    """Describe one reference clinic lock in resident/rotation/week language."""
    resident_id, week, weekday, session = key
    resident = problem.residents_by_id.get(resident_id)
    name = resident.name if resident is not None else resident_id
    rotation_id, _elective = reference_rotation_grid(problem, solution).get(
        (resident_id, week), (None, False)
    )
    rotation = (
        problem.rotations_by_id.get(rotation_id) if rotation_id is not None else None
    )
    placement = f" on {rotation.name}" if rotation is not None else ""
    return f"{name} in week {week} ({weekday.value} {session.value}{placement})"


def honored_reference_lock_diagnostic(
    problem: SolverProblem,
    solution: Schedule | None,
    key: ClinicHalfDayKey,
) -> SolverDiagnostic:
    """Explain one reference lock kept as an extra session outside the template."""
    resident_id, week, _weekday, _session = key
    return SolverDiagnostic(
        code=HONORED_REFERENCE_LOCK,
        message=(
            f"{describe_reference_clinic_lock(problem, solution, key)}: the locked "
            "clinic session falls on a clinic day the rotation doesn't allow, "
            "so an extra session was scheduled to keep the lock."
        ),
        resident_ids=[resident_id],
        weeks=[week],
        suggestions=[
            "Unlock the previous draft's clinic session if the extra session "
            "is not wanted.",
            "Add this weekday to the rotation's clinic days to make the "
            "session part of the template.",
        ],
    )


def reference_lock_conflict_diagnostic(
    problem: SolverProblem,
    solution: Schedule | None,
    barriers: dict[ClinicHalfDayKey, str],
) -> SolverDiagnostic:
    """Name reference locks as the remaining infeasibility suspect.

    Used only when the focused probes explain nothing: the locks below stay
    enforced as hard equalities — locks are never skipped — so a stale one
    the probes cannot see is the most likely contradiction left. Each entry
    names the rule that would have to give for the lock to hold.
    """
    ordered = sorted(
        barriers, key=lambda key: (key[0], key[1], key[2].value, key[3].value)
    )
    shown = ordered[:5]
    details = "; ".join(
        f"{describe_reference_clinic_lock(problem, solution, key)} "
        f"(blocked by {barriers[key]})"
        for key in shown
    )
    if len(ordered) > len(shown):
        details += f"; and {len(ordered) - len(shown)} more"
    resident_ids = sorted({key[0] for key in ordered})
    weeks = sorted({key[1] for key in ordered})
    return SolverDiagnostic(
        code=REFERENCE_LOCK_CONFLICT,
        message=(
            "No feasible schedule keeps every locked clinic session carried "
            f"over from the previous draft: {details}."
        ),
        resident_ids=resident_ids,
        weeks=weeks,
        suggestions=[
            "Unlock the listed clinic sessions and solve again.",
            "Change the conflicting rule instead to keep a lock: move the "
            "vacation week, day off, or special rotation, or restore the "
            "rotation's former clinic days.",
        ],
    )


def changed_resident_weeks(
    problem: SolverProblem,
    reference: Schedule | None,
    solution: Schedule,
) -> tuple[int, int]:
    """Return ``(changed, compared)`` resident-week rotation placements."""
    before = reference_rotation_grid(problem, reference)
    after = reference_rotation_grid(problem, solution)
    return sum(after.get(key) != assignment for key, assignment in before.items()), len(before)
