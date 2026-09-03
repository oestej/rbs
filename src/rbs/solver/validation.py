from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from rbs.models.instance import SolverProblem
from rbs.models.rotation import RotationBlockConfig
from rbs.models.schedule import Schedule
from rbs.solver.validation_assignments import _validate_assignments
from rbs.solver.validation_coverage import (
    _successful_schedule,
    _validate_block_vacation_limits,
    _validate_locks,
    _validate_resident_coverage,
    _validate_rotation_capacities,
    _week_rotation_index,
)
from rbs.solver.validation_electives import (
    _validate_elective_policies,
    _validate_rotation_groups,
)
from rbs.solver.validation_placement import (
    _validate_clinics,
    _validate_consecutive,
    _validate_placement_rules,
    _validate_total_weeks,
)


@dataclass(frozen=True)
class ScheduleValidationResult:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors


def validate_schedule(instance: SolverProblem, schedule: Schedule) -> ScheduleValidationResult:
    """Validate a schedule through independent identity, block, and clinic rules."""
    errors: list[str] = []
    warnings: list[str] = []
    expected_weeks = set(range(1, instance.calendar.weeks + 1))
    grid = schedule.week_grid
    block_vacations: dict[tuple[str, str, int, int], set[int]] = defaultdict(set)
    block_rules: dict[tuple[str, str, int, int], RotationBlockConfig] = {}

    if schedule.meta.academic_year != instance.academic_year:
        errors.append(
            f"schedule academic year {schedule.meta.academic_year!r} does not match "
            f"instance {instance.academic_year!r}"
        )

    _validate_assignments(
        instance,
        schedule,
        expected_weeks,
        block_vacations,
        block_rules,
        errors,
    )
    _validate_elective_policies(instance, schedule, errors)
    successful = _successful_schedule(schedule)
    _validate_resident_coverage(
        instance,
        schedule,
        grid,
        expected_weeks,
        successful,
        errors,
        warnings,
    )
    _validate_locks(instance, schedule, successful, errors)
    by_week_rotation = _week_rotation_index(instance, grid)
    _validate_block_vacation_limits(block_vacations, block_rules, errors)
    _validate_rotation_capacities(
        instance,
        by_week_rotation,
        expected_weeks,
        successful,
        errors,
    )
    _validate_placement_rules(
        instance,
        grid,
        instance.residents_by_id,
        errors,
        successful=successful,
    )
    _validate_rotation_groups(instance, schedule, successful, errors)
    _validate_consecutive(instance, grid, errors)
    _validate_total_weeks(instance, grid, errors)
    _validate_clinics(
        instance,
        schedule,
        errors,
        successful=successful,
    )
    return ScheduleValidationResult(tuple(errors), tuple(warnings))


def validate_schedule_or_raise(instance: SolverProblem, schedule: Schedule) -> None:
    result = validate_schedule(instance, schedule)
    if result.errors:
        preview = "; ".join(result.errors[:5])
        raise ValueError(f"schedule does not match instance: {preview}")
