"""Pre-solve checks for incomplete or contradictory workspace configuration.

Model validation deliberately permits a workspace while it is being assembled.
Readiness is the boundary that turns those editable intermediate states into
specific, actionable reasons a solve cannot start.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from rbs.models.enums import RotationKind
from rbs.models.instance import SolverProblem

__all__ = [
    "ReadinessIssue",
    "ReadinessResult",
    "unallocated_weeks_by_level",
    "check_solve_readiness",
]


@dataclass(frozen=True)
class ReadinessIssue:
    """One configuration conflict, including enough context to open its editor."""

    code: str
    message: str
    suggestions: tuple[str, ...] = ()
    rotation_id: str | None = None
    pgy: int | None = None


@dataclass(frozen=True)
class ReadinessResult:
    """Why a workspace cannot be solved yet, if it cannot."""

    errors: tuple[str, ...] = ()
    issues: tuple[ReadinessIssue, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.errors


def unallocated_weeks_by_level(instance: SolverProblem) -> dict[int, int]:
    """Return unscheduled weeks per training level, omitting fully allocated ones."""
    return {
        curriculum.pgy: remaining
        for curriculum in instance.requirements
        if (remaining := instance.unallocated_weeks(curriculum.pgy)) > 0
    }


def check_solve_readiness(instance: SolverProblem) -> ReadinessResult:
    """Report configuration gaps and contradictions before starting the engine.

    Only levels with residents block a solve. Every check here is a necessary
    condition, so it may miss a difficult shared contradiction but must never
    reject a model that could be feasible.
    """
    staffed = {resident.pgy for resident in instance.residents}
    issues: list[ReadinessIssue] = []

    for pgy in sorted(staffed):
        weeks = instance.unallocated_weeks(pgy)
        residents = [resident for resident in instance.residents if resident.pgy == pgy]
        remaining = []
        for resident in residents:
            resident_weeks = instance.resident_unallocated_weeks(resident.id)
            if resident_weeks > 0:
                remaining.append((resident, resident_weeks))
        if not remaining:
            continue
        level = instance.training_level_label(pgy, compact=True)
        if len(remaining) == len(residents) and all(
            resident_weeks == weeks for _resident, resident_weeks in remaining
        ):
            message = (
                f"{level} has {weeks} unscheduled {'week' if weeks == 1 else 'weeks'} to allocate"
            )
        else:
            by_weeks: dict[int, list[str]] = defaultdict(list)
            for resident, resident_weeks in remaining:
                by_weeks[resident_weeks].append(resident.name)
            detail = "; ".join(
                f"{resident_weeks} "
                f"{'week' if resident_weeks == 1 else 'weeks'}: " + ", ".join(names)
                for resident_weeks, names in sorted(by_weeks.items())
            )
            message = f"{level} has unscheduled resident time remaining ({detail})"
        issues.append(
            ReadinessIssue(
                code="unallocated_weeks",
                message=message,
                suggestions=(
                    "Add named-resident rotations that use the remaining unallocated time.",
                    "Or allocate the remaining weeks for the whole training level under Rotations.",
                ),
                pgy=pgy,
            )
        )

    issues.extend(_missing_elective_fallbacks(instance, staffed))
    issues.extend(_rotation_rule_conflicts(instance, staffed))
    return ReadinessResult(
        errors=tuple(issue.message for issue in issues),
        issues=tuple(issues),
    )


def _missing_elective_fallbacks(
    instance: SolverProblem,
    staffed: set[int],
) -> list[ReadinessIssue]:
    issues: list[ReadinessIssue] = []
    for pgy in instance.training_level_ids:
        if pgy not in staffed:
            continue
        level = instance.training_level_label(pgy, compact=True)
        curriculum = instance.curriculum_for(pgy)
        required_durations = sorted(
            {
                block.duration_weeks
                for block in curriculum.blocks
                if instance.rotation(block.rotation_id).kind is RotationKind.ELECTIVE
                and _some_resident_still_requires(
                    instance,
                    pgy,
                    block.rotation_id,
                    block.duration_weeks,
                    block.count,
                )
            }
        )
        for duration in required_durations:
            if instance.elective_fallback_rotation(pgy, duration) is not None:
                continue
            issues.append(
                ReadinessIssue(
                    code="missing_elective_fallback",
                    message=(
                        f"Clinic · {level}: a {duration}-week Elective requires a compatible "
                        "Clinic fallback, but none is configured."
                    ),
                    suggestions=(
                        f"Add a {duration}-week Clinic block configuration for {level}.",
                        f"Or remove or resize the {duration}-week direct Elective requirement.",
                    ),
                    pgy=pgy,
                )
            )
    return issues


def _rotation_rule_conflicts(
    instance: SolverProblem,
    staffed: set[int],
) -> list[ReadinessIssue]:
    issues: list[ReadinessIssue] = []
    potential_weeks = _potential_resident_weeks(instance)
    calendar_weeks = instance.calendar.weeks

    for rotation in instance.rotations:
        capacity_details: list[str] = []
        staffed_rules = [rule for rule in rotation.pgy_rules if rule.pgy in staffed]
        option = instance.electives.option_for(rotation.id)
        minimum_weeks = (
            calendar_weeks - len(option.blackout_weeks)
            if option is not None and rotation.kind is RotationKind.ELECTIVE
            else calendar_weeks
        )
        minimum_total = sum(rule.min_concurrent or 0 for rule in staffed_rules)
        overall_maximum = rotation.capacity.max_concurrent
        if overall_maximum is not None and minimum_total > overall_maximum:
            capacity_details.append(
                f"training-level minimums total {minimum_total} residents every week, "
                f"above the overall maximum of {overall_maximum}"
            )

        overall_minimum = rotation.capacity.min_concurrent or 0
        overall_available = sum(
            weeks
            for (rotation_id, _pgy), weeks in potential_weeks.items()
            if rotation_id == rotation.id
        )
        overall_required = overall_minimum * minimum_weeks
        if overall_minimum and overall_available < overall_required:
            capacity_details.append(
                f"the overall minimum of {overall_minimum} needs {overall_required} "
                f"resident-weeks, but at most {overall_available} are available"
            )

        for rule in staffed_rules:
            minimum = rule.min_concurrent or 0
            if not minimum:
                continue
            available = potential_weeks[rotation.id, rule.pgy]
            required = minimum * minimum_weeks
            if available >= required:
                continue
            level = instance.training_level_label(rule.pgy, compact=True)
            capacity_details.append(
                f"the {level} minimum of {minimum} needs {required} resident-weeks, "
                f"but at most {available} are available"
            )

        if capacity_details:
            issues.append(
                ReadinessIssue(
                    code="rotation_capacity_conflict",
                    message=f"{rotation.name}: " + "; ".join(capacity_details) + ".",
                    suggestions=(
                        "Clear a minimum unless this rotation must be staffed every week.",
                        "Keep training-level minimums within the overall maximum.",
                    ),
                    rotation_id=rotation.id,
                )
            )

        for curriculum in instance.requirements:
            if curriculum.pgy not in staffed or rotation.kind is RotationKind.ELECTIVE:
                continue
            offending = sorted(
                {
                    block.duration_weeks
                    for block in curriculum.blocks
                    if block.rotation_id == rotation.id
                    and block.duration_weeks > rotation.max_consecutive_weeks
                    and _some_resident_still_requires(
                        instance,
                        curriculum.pgy,
                        block.rotation_id,
                        block.duration_weeks,
                        block.count,
                    )
                }
            )
            if not offending:
                continue
            duration = max(offending)
            level = instance.training_level_label(curriculum.pgy, compact=True)
            issues.append(
                ReadinessIssue(
                    code="block_exceeds_consecutive_limit",
                    message=(
                        f"{rotation.name} · {level}: a required {duration}-week block "
                        f"cannot fit the {rotation.max_consecutive_weeks}-week maximum "
                        "consecutive limit."
                    ),
                    suggestions=(
                        f"Raise maximum consecutive weeks to at least {duration}.",
                        f"Or shorten the required {level} block.",
                    ),
                    rotation_id=rotation.id,
                    pgy=curriculum.pgy,
                )
            )
    return issues


def _some_resident_still_requires(
    instance: SolverProblem,
    pgy: int,
    rotation_id: str,
    duration_weeks: int,
    count: int,
) -> bool:
    """Whether overrides, manual replacements, and waivers leave an occurrence."""
    for resident in instance.residents:
        if resident.pgy != pgy:
            continue
        replaced = sum(
            block.resident_id == resident.id
            and block.replaces_rotation_id == rotation_id
            and block.duration_weeks == duration_weeks
            for block in instance.manual_clinic_blocks
        )
        replaced += sum(
            override.resident_id == resident.id
            and override.replaces_rotation_id == rotation_id
            and override.duration_weeks == duration_weeks
            for override in instance.resident_rotation_overrides
        )
        replaced += sum(
            waiver.resident_id == resident.id
            and waiver.rotation_id == rotation_id
            and waiver.duration_weeks == duration_weeks
            for waiver in instance.resident_rotation_waivers
        )
        if replaced < count:
            return True
    return False


def _potential_resident_weeks(instance: SolverProblem) -> defaultdict[tuple[str, int], int]:
    """Return a safe upper bound on resident-weeks available to each rotation.

    Elective possibilities are deliberately over-counted. That keeps the check
    conclusive: if even this generous upper bound cannot sustain a weekly
    minimum, the complete model cannot sustain it either.
    """
    result: defaultdict[tuple[str, int], int] = defaultdict(int)
    for resident in instance.residents:
        curriculum = instance.curriculum_for(resident.pgy)
        for block in curriculum.blocks:
            source = instance.rotation(block.rotation_id)
            weeks = block.duration_weeks * block.count
            if source.kind is not RotationKind.ELECTIVE:
                result[source.id, resident.pgy] += weeks
                continue
            for candidate in instance.rotations:
                option = instance.electives.option_for(candidate.id)
                eligible = (
                    option is not None
                    and option.allows(resident.pgy, block.duration_weeks)
                    and candidate.allows_duration(block.duration_weeks, pgy=resident.pgy)
                ) or instance.is_elective_fallback_rotation(
                    candidate.id,
                    resident.pgy,
                    block.duration_weeks,
                )
                if eligible:
                    result[candidate.id, resident.pgy] += weeks

        for override in instance.resident_rotation_overrides:
            if override.resident_id == resident.id:
                result[override.rotation_id, resident.pgy] += override.duration_weeks
        for block in instance.manual_clinic_blocks:
            if block.resident_id == resident.id:
                result[block.rotation_id, resident.pgy] += block.duration_weeks
    return result
