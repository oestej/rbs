from __future__ import annotations

from collections import defaultdict
from typing import Any

from rbs.models.enums import RotationKind
from rbs.solver.core.context import (
    ElectiveMatchingState,
    ModelBuildError,
    PlanningContext,
)
from rbs.solver.core.groups import add_rotation_group_constraints
from rbs.solver.planning import Occurrence, covers, resolve_clinic_block_band


def add_hard_constraints(context: PlanningContext) -> ElectiveMatchingState:
    _place_groups(context)
    matching = _elective_preferences(context)
    _elective_repeatability(context)
    add_rotation_group_constraints(context)
    _cover_each_week(context)
    _capacity(context)
    _locks(context)
    _symmetry(context)
    _sequence(context)
    _consecutive(context)
    _total_weeks(context)
    _clinic_block_balance(context)
    return matching


def _elective_repeatability(context: PlanningContext) -> None:
    """Limit non-repeatable Elective services to one block per resident."""
    nonrepeatable = {
        option.rotation_id
        for option in context.instance.electives.rotation_options
        if not option.repeatable
    }
    if not nonrepeatable:
        return
    for occurrences in context.by_resident.values():
        for rotation_id in nonrepeatable:
            selected = [
                context.placements[occurrence.key, start]
                for occurrence in occurrences
                if occurrence.elective
                and not occurrence.elective_fallback
                and occurrence.rotation_id == rotation_id
                for start in context.starts[occurrence.key]
            ]
            if selected:
                context.model.Add(sum(selected) <= 1)


def _clinic_block_balance(context: PlanningContext) -> None:
    """Bound how many residents may sit on a dedicated Clinic block each week.

    The soft evenness weight only nudges, and the solver rarely converges far
    enough for a nudge to land. This states the flat calendar as a rule instead,
    which measurement showed costs almost nothing in attending sessions.
    """
    lowest, highest, automatic = resolve_clinic_block_band(
        context.instance,
        context.options,
    )
    if lowest is None and highest is None:
        return
    clinic_ids = {
        rotation.id
        for rotation in context.instance.rotations
        if rotation.kind is RotationKind.CLINIC
    }
    if not clinic_ids:
        return
    for week in context.weeks:
        literals = [
            context.placements[occurrence.key, start]
            for occurrence in context.occurrences
            if occurrence.rotation_id in clinic_ids
            and (not automatic or not occurrence.elective_fallback)
            for start in context.starts[occurrence.key]
            if covers(start, occurrence.duration_weeks, week)
        ]
        if not literals:
            continue
        if lowest is not None:
            context.model.Add(sum(literals) >= lowest)
        if highest is not None:
            context.model.Add(sum(literals) <= highest)


def _elective_preferences(context: PlanningContext) -> ElectiveMatchingState:
    """Enforce resident request quotas and expose global matching tiers."""
    max_rank = max(
        (len(resident.elective_preferences) for resident in context.residents.values()),
        default=0,
    )
    rank_literals: list[list[Any]] = [[] for _ in range(max_rank)]
    fallback_literals: list[Any] = []

    for occurrence in context.occurrences:
        if occurrence.elective_fallback:
            fallback_literals.extend(
                context.placements[occurrence.key, start]
                for start in context.starts[occurrence.key]
            )

    for resident in context.residents.values():
        managed = [
            occurrence
            for occurrence in context.by_resident.get(resident.id, [])
            if occurrence.preference_managed and not occurrence.elective_fallback
        ]
        candidate_shapes = {
            (occurrence.rotation_id, occurrence.duration_weeks) for occurrence in managed
        }
        for rotation_id, duration_weeks in sorted(candidate_shapes):
            matching_occurrences = [
                occurrence
                for occurrence in managed
                if occurrence.rotation_id == rotation_id
                and occurrence.duration_weeks == duration_weeks
            ]
            selected = [
                context.placements[occurrence.key, start]
                for occurrence in matching_occurrences
                for start in context.starts[occurrence.key]
            ]
            requests: list[Any] = []
            for rank, request in enumerate(resident.elective_preferences):
                if request.rotation_id != rotation_id or request.duration_weeks != duration_weeks:
                    continue
                matched = context.model.NewBoolVar(
                    f"elective-request:{resident.id}:rank-{rank + 1}"
                )
                requests.append(matched)
                rank_literals[rank].append(matched)
            for earlier, later in zip(requests, requests[1:], strict=False):
                context.model.Add(earlier >= later)
            if requests:
                context.model.Add(sum(requests) <= sum(selected))

            locked_weeks = {
                week
                for lock in context.instance.locks
                if lock.resident_id == resident.id
                and lock.elective
                and lock.rotation_id == rotation_id
                for week in lock.weeks
            }
            lock_covering = [
                context.placements[occurrence.key, start]
                for occurrence in matching_occurrences
                for start in context.starts[occurrence.key]
                if any(covers(start, occurrence.duration_weeks, week) for week in locked_weeks)
            ]
            context.model.Add(sum(selected) <= sum(requests) + sum(lock_covering))

    return ElectiveMatchingState(
        fallback_literals=tuple(fallback_literals),
        rank_literals=tuple(tuple(items) for items in rank_literals),
    )


def _place_groups(context: PlanningContext) -> None:
    grouped: dict[str, list[Occurrence]] = defaultdict(list)
    for occurrence in context.occurrences:
        grouped[occurrence.group_id].append(occurrence)
    for group_id, group in grouped.items():
        literals = [
            context.placements[occurrence.key, start]
            for occurrence in group
            for start in context.starts[occurrence.key]
        ]
        if not literals:
            raise ModelBuildError(f"no start variables for group {group_id}")
        context.model.AddExactlyOne(literals)


def _cover_each_week(context: PlanningContext) -> None:
    for resident_id, occurrences in context.by_resident.items():
        for week in context.weeks:
            literals = [
                context.placements[occurrence.key, start]
                for occurrence in occurrences
                for start in context.starts[occurrence.key]
                if covers(start, occurrence.duration_weeks, week)
            ]
            if not literals:
                raise ModelBuildError(f"{resident_id} week {week} has no covering block")
            context.model.AddExactlyOne(literals)


def _capacity(context: PlanningContext) -> None:
    for rotation in context.instance.rotations:
        occurrences = context.by_rotation.get(rotation.id, [])
        if not occurrences:
            continue
        for week in context.weeks:
            literals = [
                context.placements[occurrence.key, start]
                for occurrence in occurrences
                for start in context.starts[occurrence.key]
                if covers(start, occurrence.duration_weeks, week)
            ]
            _add_capacity_bounds(
                context,
                literals,
                minimum=rotation.capacity.min_concurrent,
                maximum=rotation.capacity.max_concurrent,
                label=f"{rotation.id} total",
                week=week,
            )
            for rule in rotation.pgy_rules:
                pgy_literals = [
                    context.placements[occurrence.key, start]
                    for occurrence in occurrences
                    if occurrence.pgy == rule.pgy
                    for start in context.starts[occurrence.key]
                    if covers(start, occurrence.duration_weeks, week)
                ]
                _add_capacity_bounds(
                    context,
                    pgy_literals,
                    minimum=rule.min_concurrent,
                    maximum=rule.max_concurrent,
                    label=(
                        f"{rotation.id} "
                        f"{context.instance.training_level_label(rule.pgy, compact=True)}"
                    ),
                    week=week,
                )


def _add_capacity_bounds(
    context: PlanningContext,
    literals: list[Any],
    *,
    minimum: int | None,
    maximum: int | None,
    label: str,
    week: int,
) -> None:
    if not literals:
        if minimum:
            raise ModelBuildError(
                f"{label} week {week} has no candidates but min_concurrent={minimum}"
            )
        return
    if maximum is not None:
        context.model.Add(sum(literals) <= maximum)
    if minimum is not None:
        context.model.Add(sum(literals) >= minimum)


def _locks(context: PlanningContext) -> None:
    for lock in context.instance.locks:
        occurrences = [
            occurrence
            for occurrence in context.by_resident.get(lock.resident_id, [])
            if occurrence.rotation_id == lock.rotation_id and occurrence.elective == lock.elective
        ]
        if lock.exact_block:
            start = lock.weeks[0]
            duration = len(lock.weeks)
            literals = [
                context.placements[occurrence.key, start]
                for occurrence in occurrences
                if occurrence.duration_weeks == duration and start in context.starts[occurrence.key]
            ]
            if not literals:
                raise ModelBuildError(
                    f"exact block lock cannot be satisfied: {lock.resident_id} "
                    f"weeks {lock.weeks[0]}-{lock.weeks[-1]} as {lock.rotation_id}"
                )
            context.model.Add(sum(literals) == 1)
            continue
        for week in lock.weeks:
            literals = [
                context.placements[occurrence.key, start]
                for occurrence in occurrences
                for start in context.starts[occurrence.key]
                if covers(start, occurrence.duration_weeks, week)
            ]
            if not literals:
                raise ModelBuildError(
                    f"lock cannot be satisfied: {lock.resident_id} week {week} "
                    f"as {lock.rotation_id}"
                )
            context.model.Add(sum(literals) == 1)


def _symmetry(context: PlanningContext) -> None:
    groups: dict[tuple[str, str, int], list[Occurrence]] = defaultdict(list)
    for occurrence in context.occurrences:
        if occurrence.group_id == occurrence.key and occurrence.fixed_start_week is None:
            groups[
                occurrence.resident_id,
                occurrence.rotation_id,
                occurrence.duration_weeks,
            ].append(occurrence)
    for group in groups.values():
        if len(group) < 2:
            continue
        starts_int = []
        for occurrence in group:
            start_var = context.model.NewIntVar(
                min(context.starts[occurrence.key]),
                max(context.starts[occurrence.key]),
                f"s:{occurrence.key}",
            )
            context.model.Add(
                start_var
                == sum(
                    start * context.placements[occurrence.key, start]
                    for start in context.starts[occurrence.key]
                )
            )
            starts_int.append((occurrence, start_var))
        for (left, left_start), (_right, right_start) in zip(
            starts_int, starts_int[1:], strict=False
        ):
            context.model.Add(left_start + left.duration_weeks <= right_start)


def _sequence(context: PlanningContext) -> None:
    def presence_var(occurrence: Occurrence):
        variable = context.model.NewBoolVar(f"present:{occurrence.key}")
        context.model.Add(
            variable
            == sum(
                context.placements[occurrence.key, start]
                for start in context.starts[occurrence.key]
            )
        )
        return variable

    def start_var(occurrence: Occurrence):
        upper = max(context.starts[occurrence.key])
        variable = context.model.NewIntVar(0, upper, f"seq:{occurrence.key}")
        context.model.Add(
            variable
            == sum(
                start * context.placements[occurrence.key, start]
                for start in context.starts[occurrence.key]
            )
        )
        return variable

    def cached_start(occurrence: Occurrence, cache: dict[str, Any]):
        if occurrence.key not in cache:
            cache[occurrence.key] = start_var(occurrence)
        return cache[occurrence.key]

    def cached_presence(occurrence: Occurrence, cache: dict[str, Any]):
        if occurrence.key not in cache:
            cache[occurrence.key] = presence_var(occurrence)
        return cache[occurrence.key]

    for occurrences in context.by_resident.values():
        by_rotation: dict[str, list[Occurrence]] = defaultdict(list)
        for occurrence in occurrences:
            by_rotation[occurrence.rotation_id].append(occurrence)
        start_cache: dict[str, Any] = {}
        presence_cache: dict[str, Any] = {}

        for occurrence in occurrences:
            if not occurrence.prerequisite_rotation_ids:
                continue
            this_start = cached_start(occurrence, start_cache)
            this_presence = cached_presence(occurrence, presence_cache)
            for predecessor_id in occurrence.prerequisite_rotation_ids:
                predecessors = by_rotation.get(predecessor_id, [])
                if not predecessors:
                    raise ModelBuildError(
                        f"{occurrence.resident_id} {occurrence.rotation_id} requires "
                        f"{predecessor_id!r}, but that rotation is not in this curriculum"
                    )
                valid_predecessors = []
                for predecessor in predecessors:
                    predecessor_presence = cached_presence(predecessor, presence_cache)
                    predecessor_start = cached_start(predecessor, start_cache)
                    valid = context.model.NewBoolVar(
                        f"prerequisite:{occurrence.key}:{predecessor.key}"
                    )
                    context.model.Add(
                        this_start >= predecessor_start + predecessor.duration_weeks
                    ).OnlyEnforceIf(valid)
                    context.model.Add(valid <= predecessor_presence)
                    valid_predecessors.append(valid)
                context.model.AddBoolOr([*valid_predecessors, this_presence.Not()])


def _consecutive(context: PlanningContext) -> None:
    for rotation in context.instance.rotations:
        limit = rotation.max_consecutive_weeks
        if limit is None:
            continue
        for resident_id, occurrences in context.by_resident.items():
            matching = [
                occurrence for occurrence in occurrences if occurrence.rotation_id == rotation.id
            ]
            if not matching:
                continue
            presence = []
            for week in context.weeks:
                literals = [
                    context.placements[occurrence.key, start]
                    for occurrence in matching
                    for start in context.starts[occurrence.key]
                    if covers(start, occurrence.duration_weeks, week)
                ]
                present = context.model.NewBoolVar(f"run:{resident_id}:{rotation.id}:w{week}")
                context.model.Add(present == (sum(literals) if literals else 0))
                presence.append(present)
            window = limit + 1
            for index in range(0, len(presence) - limit):
                context.model.Add(sum(presence[index : index + window]) <= limit)


def _total_weeks(context: PlanningContext) -> None:
    """Cap each resident's combined placements on a configured rotation."""
    for rotation in context.instance.rotations:
        for resident_id, occurrences in context.by_resident.items():
            try:
                limit = rotation.max_total_weeks_for_pgy(context.residents[resident_id].pgy)
            except KeyError:
                continue
            if limit is None:
                continue
            matching = [
                occurrence for occurrence in occurrences if occurrence.rotation_id == rotation.id
            ]
            placements = [
                occurrence.duration_weeks * context.placements[occurrence.key, start]
                for occurrence in matching
                for start in context.starts[occurrence.key]
            ]
            if placements:
                context.model.Add(sum(placements) <= limit)
