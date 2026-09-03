"""Schedule validation: elective policies and rotation groups."""

from __future__ import annotations

from collections import defaultdict
from itertools import product

from rbs.models.instance import SolverProblem
from rbs.models.schedule import Schedule


def _validate_elective_policies(
    instance: SolverProblem,
    schedule: Schedule,
    errors: list[str],
) -> None:
    """Validate per-level eligibility and per-resident repeat limits."""
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for assignment in schedule.assignments:
        if not assignment.elective or assignment.elective_fallback:
            continue
        resident = instance.residents_by_id.get(assignment.resident_id)
        option = instance.electives.option_for(assignment.rotation_id)
        if resident is None or option is None:
            continue
        if not option.allows(resident.pgy, assignment.block_duration_weeks):
            errors.append(
                f"{assignment.resident_id} {assignment.rotation_id}: elective is not "
                f"available for {instance.training_level_label(resident.pgy, compact=True)} "
                f"in {assignment.block_duration_weeks}-week blocks"
            )
        counts[assignment.resident_id, assignment.rotation_id] += 1

    for (resident_id, rotation_id), count in counts.items():
        option = instance.electives.option_for(rotation_id)
        if option is not None and not option.repeatable and count > 1:
            errors.append(
                f"{resident_id} has {count} elective blocks on {rotation_id}, "
                "but that service may be taken only once as an elective"
            )


def _validate_rotation_groups(
    instance: SolverProblem,
    schedule: Schedule,
    successful: bool,
    errors: list[str],
) -> None:
    """Confirm each required group instance is a gap-free sequence.

    Group members may appear in any order unless ordinary prerequisites constrain
    them. Resident-specific unmatched extras are excluded, while one explicit
    grouping-exempt exact lock releases one complete instance.
    """
    if not successful or not instance.rotation_groups:
        return
    assignments_by_resident: dict[str, list] = defaultdict(list)
    for assignment in schedule.assignments:
        if not assignment.elective:
            assignments_by_resident[assignment.resident_id].append(assignment)

    for resident in instance.residents:
        curriculum = instance.curriculum_for(resident.pgy)
        resident_assignments = assignments_by_resident.get(resident.id, [])
        for group in (item for item in instance.rotation_groups if item.pgy == resident.pgy):
            direct_count = sum(
                block.count
                for block in curriculum.blocks
                if block.rotation_id == group.rotation_ids[0]
            )
            linked_ids = {
                override.group_instance_id
                for override in instance.resident_rotation_overrides
                if override.resident_id == resident.id
                and override.rotation_id in group.rotation_ids
                and override.group_instance_id is not None
            }
            required_clusters = direct_count + len(linked_ids)
            by_rotation = {
                rotation_id: [
                    assignment
                    for assignment in resident_assignments
                    if assignment.rotation_id == rotation_id
                ]
                for rotation_id in group.rotation_ids
            }
            exempt_assignments = _grouping_exempt_assignments(
                instance,
                resident.id,
                by_rotation,
            )
            exemptions = len(exempt_assignments)
            if exemptions > required_clusters:
                errors.append(
                    f"{resident.id} rotation group has {exemptions} grouping exemptions "
                    f"but only {required_clusters} required instances"
                )
                continue

            unmatched = {
                rotation_id: sum(
                    1
                    for override in instance.resident_rotation_overrides
                    if override.resident_id == resident.id
                    and override.rotation_id == rotation_id
                    and override.group_instance_id is None
                )
                for rotation_id in group.rotation_ids
            }
            expected_counts = {
                rotation_id: required_clusters + unmatched[rotation_id]
                for rotation_id in group.rotation_ids
            }
            actual_counts = {
                rotation_id: len(by_rotation[rotation_id]) for rotation_id in group.rotation_ids
            }
            if actual_counts != expected_counts:
                detail = ", ".join(
                    f"{rotation_id}={actual_counts[rotation_id]} "
                    f"(expected {expected_counts[rotation_id]})"
                    for rotation_id in group.rotation_ids
                )
                errors.append(f"{resident.id} rotation group occurrence mismatch: {detail}")
                continue

            candidates = _rotation_group_candidates(
                instance,
                resident.pgy,
                group.rotation_ids,
                by_rotation,
                exempt_assignments,
            )
            if not _can_select_disjoint_group_candidates(
                candidates,
                required_clusters - exemptions,
            ):
                labels = " + ".join(group.rotation_ids)
                errors.append(
                    f"{resident.id} rotation group {labels} is not arranged as "
                    "the required contiguous instance(s)"
                )


def _grouping_exempt_assignments(
    instance: SolverProblem,
    resident_id: str,
    by_rotation: dict[str, list],
) -> set[int]:
    exempt: set[int] = set()
    for lock in instance.locks:
        if lock.resident_id != resident_id or not lock.grouping_exempt:
            continue
        for assignment in by_rotation.get(lock.rotation_id, []):
            if assignment.weeks == lock.weeks:
                exempt.add(id(assignment))
                break
    return exempt


def _rotation_group_candidates(
    instance: SolverProblem,
    pgy: int,
    rotation_ids: list[str],
    by_rotation: dict[str, list],
    exempt_assignments: set[int],
) -> list[frozenset[int]]:
    candidates: list[frozenset[int]] = []
    domains = [by_rotation[rotation_id] for rotation_id in rotation_ids]
    for selected in product(*domains):
        if any(id(assignment) in exempt_assignments for assignment in selected):
            continue
        chronological = sorted(selected, key=lambda assignment: assignment.start_week)
        if any(
            left.end_week + 1 != right.start_week
            for left, right in zip(chronological, chronological[1:], strict=False)
        ):
            continue
        chosen = {assignment.rotation_id: assignment for assignment in selected}
        if any(
            predecessor_id in chosen
            and chosen[predecessor_id].end_week >= chosen[rotation_id].start_week
            for rotation_id in rotation_ids
            for predecessor_id in instance.rotation(rotation_id)
            .pgy_rule(pgy)
            .prerequisite_rotation_ids
        ):
            continue
        candidates.append(frozenset(id(assignment) for assignment in selected))
    return candidates


def _can_select_disjoint_group_candidates(
    candidates: list[frozenset[int]],
    count: int,
) -> bool:
    if count == 0:
        return True

    def search(index: int, remaining: int, used: frozenset[int]) -> bool:
        if remaining == 0:
            return True
        if len(candidates) - index < remaining:
            return False
        for candidate_index in range(index, len(candidates)):
            candidate = candidates[candidate_index]
            if candidate.isdisjoint(used) and search(
                candidate_index + 1,
                remaining - 1,
                used | candidate,
            ):
                return True
        return False

    return search(0, count, frozenset())
