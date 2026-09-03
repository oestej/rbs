"""Schedule validation: coverage, locks, vacations, and capacities."""

from __future__ import annotations

from collections import defaultdict

from rbs.models.enums import SolverStatus
from rbs.models.instance import SolverProblem
from rbs.models.rotation import RotationBlockConfig
from rbs.models.schedule import Schedule


def _successful_schedule(schedule: Schedule) -> bool:
    accepted = {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    return schedule.meta.solver_status in accepted or schedule.meta.status in accepted


def _validate_resident_coverage(
    instance: SolverProblem,
    schedule: Schedule,
    grid: dict[str, dict[str, str]],
    expected_weeks: set[int],
    successful: bool,
    errors: list[str],
    warnings: list[str],
) -> None:
    unassigned = set(schedule.unassigned)
    unknown = unassigned - set(instance.residents_by_id)
    if unknown:
        errors.append(f"unassigned contains unknown residents {sorted(unknown)}")
    for resident_id in instance.residents_by_id:
        assigned = {int(week) for week in grid.get(resident_id, {})}
        if successful and assigned != expected_weeks:
            missing = sorted(expected_weeks - assigned)
            errors.append(
                f"{resident_id} does not cover the academic year; missing weeks {missing[:8]}"
            )
        if resident_id in unassigned and assigned:
            errors.append(f"{resident_id} is both assigned and listed as unassigned")
        if resident_id not in unassigned and not assigned and not successful:
            warnings.append(f"{resident_id} has no assignments but is not listed as unassigned")


def _validate_locks(
    instance: SolverProblem,
    schedule: Schedule,
    successful: bool,
    errors: list[str],
) -> None:
    if not successful:
        return
    for lock in instance.locks:
        for week in lock.weeks:
            actual = schedule.assignment_for(lock.resident_id, week)
            actual_key = (actual.rotation_id, actual.elective) if actual is not None else None
            expected_key = (lock.rotation_id, lock.elective)
            if actual_key == expected_key:
                continue
            actual_label = (
                None
                if actual is None
                else actual.rotation_id + (" (Elec)" if actual.elective else "")
            )
            errors.append(
                f"lock mismatch: {lock.resident_id} week {week} expected "
                f"{lock.rotation_id}{' (Elec)' if lock.elective else ''}, "
                f"got {actual_label}"
            )


def _week_rotation_index(
    instance: SolverProblem,
    grid: dict[str, dict[str, str]],
) -> dict[tuple[int, str], set[str]]:
    result: dict[tuple[int, str], set[str]] = defaultdict(set)
    for resident_id, weeks in grid.items():
        if resident_id not in instance.residents_by_id:
            continue
        for week_text, rotation_id in weeks.items():
            result[int(week_text), rotation_id].add(resident_id)
    return result


def _validate_block_vacation_limits(
    block_vacations: dict[tuple[str, str, int, int], set[int]],
    block_rules: dict[tuple[str, str, int, int], RotationBlockConfig],
    errors: list[str],
) -> None:
    for key, vacation_weeks in block_vacations.items():
        maximum = block_rules[key].vacation.max_weeks_per_block
        if maximum is None or len(vacation_weeks) <= maximum:
            continue
        resident_id, rotation_id, start_week, _duration = key
        errors.append(
            f"{resident_id} {rotation_id} block starting week {start_week}: "
            f"{len(vacation_weeks)} vacation weeks exceeds maximum {maximum}"
        )


def _validate_rotation_capacities(
    instance: SolverProblem,
    by_week_rotation: dict[tuple[int, str], set[str]],
    expected_weeks: set[int],
    successful: bool,
    errors: list[str],
) -> None:
    for rotation in instance.rotations:
        for week in expected_weeks:
            present = by_week_rotation.get((week, rotation.id), set())
            _validate_rotation_capacity(rotation, week, present, successful, errors)
            for rule in rotation.pgy_rules:
                pgy_count = sum(
                    instance.residents_by_id[resident_id].pgy == rule.pgy for resident_id in present
                )
                _validate_pgy_capacity(
                    instance,
                    rotation.id,
                    week,
                    rule,
                    pgy_count,
                    successful,
                    errors,
                )


def _validate_rotation_capacity(
    rotation,
    week: int,
    present: set[str],
    successful: bool,
    errors: list[str],
) -> None:
    count = len(present)
    minimum = rotation.capacity.min_concurrent
    maximum = rotation.capacity.max_concurrent
    if successful and minimum is not None and count < minimum:
        errors.append(f"{rotation.id} week {week}: {count} below minimum {minimum}")
    if maximum is not None and count > maximum:
        errors.append(f"{rotation.id} week {week}: {count} exceeds maximum {maximum}")


def _validate_pgy_capacity(
    instance: SolverProblem,
    rotation_id: str,
    week: int,
    rule,
    count: int,
    successful: bool,
    errors: list[str],
) -> None:
    level_code = instance.training_level_label(rule.pgy, compact=True)
    if successful and rule.min_concurrent is not None and count < rule.min_concurrent:
        errors.append(
            f"{rotation_id} week {week}: {level_code} count {count} "
            f"is below minimum {rule.min_concurrent}"
        )
    if rule.max_concurrent is not None and count > rule.max_concurrent:
        errors.append(
            f"{rotation_id} week {week}: {level_code} count {count} "
            f"exceeds maximum {rule.max_concurrent}"
        )
