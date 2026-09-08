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
    _validate_anchored_rotation_groups,
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
    _validate_anchored_rotation_groups(instance, schedule, successful, errors)
    _validate_consecutive(instance, grid, errors)
    _validate_total_weeks(instance, grid, errors)
    _validate_clinics(
        instance,
        schedule,
        errors,
        successful=successful,
    )
    return ScheduleValidationResult(tuple(errors), tuple(warnings))


def _validate_working_draft_integrity(
    instance: SolverProblem,
    schedule: Schedule,
) -> ScheduleValidationResult:
    """Validate invariants a later solve cannot safely repair.

    Working drafts may be incomplete and may temporarily violate cohort-wide
    capacity, adjacency, and annual-total constraints. They still have to
    reference this problem's residents, rotations, clinic sites, calendar,
    and viable block definitions.
    """
    errors: list[str] = []
    warnings: list[str] = []
    expected_weeks = set(range(1, instance.calendar.weeks + 1))
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
    _validate_block_vacation_limits(block_vacations, block_rules, errors)
    _validate_elective_policies(instance, schedule, errors)
    _validate_resident_coverage(
        instance,
        schedule,
        schedule.week_grid,
        expected_weeks,
        False,
        errors,
        warnings,
    )
    _validate_placement_rules(
        instance,
        schedule.week_grid,
        instance.residents_by_id,
        errors,
        successful=False,
    )
    return ScheduleValidationResult(tuple(errors), tuple(warnings))


def validate_schedule_or_raise(instance: SolverProblem, schedule: Schedule) -> None:
    result = validate_schedule(instance, schedule)
    if result.errors:
        preview = "; ".join(result.errors[:5])
        raise ValueError(f"schedule does not match instance: {preview}")


def validate_persistable_schedule_or_raise(
    instance: SolverProblem,
    schedule: Schedule,
) -> Schedule:
    """Return revalidated output, permitting an explicitly unsolved working draft.

    A manual block edit can temporarily exceed capacity, consecutive-week, or
    annual-total constraints until the next solve rearranges the cohort. The
    draft must remain structurally valid and refer only to data in the current
    academic-year problem.
    """
    # ``model_copy(update=...)`` does not run Pydantic validators. A UI edit
    # may therefore hand persistence a Schedule object whose individual
    # fields look valid but whose assignments overlap or whose nested ranges
    # are contradictory. Reparse the wire shape before applying the more
    # permissive working-draft policy so malformed values can never reach a
    # transaction.
    schedule = Schedule.model_validate(schedule.model_dump(mode="json"))

    if schedule.is_working_draft:
        result = _validate_working_draft_integrity(instance, schedule)
        if result.errors:
            preview = "; ".join(result.errors[:5])
            raise ValueError(f"working draft does not match instance: {preview}")
        return schedule
    validate_schedule_or_raise(instance, schedule)
    return schedule
