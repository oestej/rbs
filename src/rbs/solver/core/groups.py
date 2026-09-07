from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from rbs.models.enums import RotationKind
from rbs.solver.core.context import ModelBuildError, PlanningContext
from rbs.solver.planning import Occurrence, rotation_group_key


@dataclass
class _SelectedMember:
    rotation_id: str
    start: Any
    duration: Any
    end: Any


@dataclass
class _FlexibleCluster:
    anchor: Occurrence
    selected: dict[str, _SelectedMember]
    matches: dict[str, Any]
    exempt: Any


@dataclass
class _FixedCluster:
    occurrences: list[Occurrence]
    selected: dict[str, _SelectedMember]
    exempt: Any


def add_rotation_group_constraints(context: PlanningContext) -> None:
    """Make each configured Mandatory group one gap-free unordered sequence."""
    configured = {
        rotation_group_key(group.pgy, group.rotation_ids): group
        for group in context.instance.rotation_groups
    }
    grouped: dict[tuple[str, str], list[Occurrence]] = defaultdict(list)
    for occurrence in context.occurrences:
        if occurrence.rotation_group_key is not None:
            grouped[occurrence.resident_id, occurrence.rotation_group_key].append(
                occurrence
            )

    for (resident_id, key), occurrences in grouped.items():
        group = configured.get(key)
        if group is None:
            raise ModelBuildError(f"unknown rotation group key {key!r}")
        fixed: dict[str, list[Occurrence]] = defaultdict(list)
        flexible: list[Occurrence] = []
        for occurrence in occurrences:
            if occurrence.rotation_group_instance_id is None:
                flexible.append(occurrence)
            else:
                fixed[occurrence.rotation_group_instance_id].append(occurrence)

        fixed_clusters: list[_FixedCluster] = []
        for instance_id, bundle in fixed.items():
            by_rotation = {occurrence.rotation_id: occurrence for occurrence in bundle}
            if set(by_rotation) != set(group.rotation_ids):
                raise ModelBuildError(
                    f"{resident_id} override group {instance_id!r} does not contain "
                    "every configured rotation"
                )
            selected = {
                rotation_id: _fixed_member(context, by_rotation[rotation_id])
                for rotation_id in group.rotation_ids
            }
            fixed_clusters.append(
                _FixedCluster(
                    occurrences=bundle,
                    selected=selected,
                    exempt=context.model.NewBoolVar(f"group-exempt:{instance_id}"),
                )
            )

        if not flexible:
            _bind_manual_exemptions(
                context,
                resident_id,
                key,
                occurrences,
                [],
                fixed_clusters,
            )
            for index, cluster in enumerate(fixed_clusters):
                _enforce_cluster(
                    context,
                    cluster.selected,
                    group.rotation_ids,
                    pgy=group.pgy,
                    name=f"{key}:{resident_id}:fixed:{index}",
                    exempt=cluster.exempt,
                )
            continue
        by_rotation: dict[str, list[Occurrence]] = defaultdict(list)
        for occurrence in flexible:
            by_rotation[occurrence.rotation_id].append(occurrence)
        counts = {rotation_id: len(by_rotation[rotation_id]) for rotation_id in group.rotation_ids}
        if len(set(counts.values())) != 1 or not next(iter(counts.values()), 0):
            detail = ", ".join(f"{rotation_id}={count}" for rotation_id, count in counts.items())
            raise ModelBuildError(
                f"{resident_id} rotation group has unmatched occurrences ({detail})"
            )
        for values in by_rotation.values():
            values.sort(key=lambda occurrence: occurrence.key)

        anchors = by_rotation[group.rotation_ids[0]]
        start_vars = {
            occurrence.key: _occurrence_start(context, occurrence)
            for occurrence in flexible
        }
        clusters: list[_FlexibleCluster] = []
        for anchor_index, anchor in enumerate(anchors):
            selected = {
                anchor.rotation_id: _member_from_start(
                    context,
                    anchor.rotation_id,
                    start_vars[anchor.key],
                    anchor.duration_weeks,
                    f"group:{key}:{resident_id}:{anchor_index}:{anchor.rotation_id}",
                )
            }
            matches: dict[str, Any] = {anchor.key: None}
            for rotation_id in group.rotation_ids[1:]:
                candidates = by_rotation[rotation_id]
                candidate_matches = []
                selected_start = context.model.NewIntVar(
                    1,
                    context.instance.calendar.weeks,
                    f"group-start:{key}:{resident_id}:{anchor_index}:{rotation_id}",
                )
                selected_duration = context.model.NewIntVar(
                    1,
                    5,
                    f"group-duration:{key}:{resident_id}:{anchor_index}:{rotation_id}",
                )
                for candidate in candidates:
                    match = context.model.NewBoolVar(
                        f"group-match:{anchor.key}:{candidate.key}"
                    )
                    candidate_matches.append(match)
                    matches[candidate.key] = match
                    context.model.Add(
                        selected_start == start_vars[candidate.key]
                    ).OnlyEnforceIf(match)
                    context.model.Add(
                        selected_duration == candidate.duration_weeks
                    ).OnlyEnforceIf(match)
                context.model.AddExactlyOne(candidate_matches)
                selected[rotation_id] = _member_from_start(
                    context,
                    rotation_id,
                    selected_start,
                    selected_duration,
                    f"group:{key}:{resident_id}:{anchor_index}:{rotation_id}",
                )
            exempt = context.model.NewBoolVar(
                f"group-exempt:{key}:{resident_id}:{anchor_index}"
            )
            clusters.append(
                _FlexibleCluster(
                    anchor=anchor,
                    selected=selected,
                    matches=matches,
                    exempt=exempt,
                )
            )

        for rotation_id in group.rotation_ids[1:]:
            for candidate in by_rotation[rotation_id]:
                context.model.AddExactlyOne(
                    cluster.matches[candidate.key] for cluster in clusters
                )

        _bind_manual_exemptions(
            context,
            resident_id,
            key,
            occurrences,
            clusters,
            fixed_clusters,
        )
        for index, cluster in enumerate(clusters):
            _enforce_cluster(
                context,
                cluster.selected,
                group.rotation_ids,
                pgy=group.pgy,
                name=f"{key}:{resident_id}:{index}",
                exempt=cluster.exempt,
            )
        for index, cluster in enumerate(fixed_clusters):
            _enforce_cluster(
                context,
                cluster.selected,
                group.rotation_ids,
                pgy=group.pgy,
                name=f"{key}:{resident_id}:fixed:{index}",
                exempt=cluster.exempt,
            )


def add_anchored_rotation_group_constraints(context: PlanningContext) -> None:
    """Group every selected anchor with distinct Clinic/FMED companions.

    Unlike an ordinary rotation group, companions may have extra occurrences.
    Elective anchors are optional candidates, so the matching and contiguous
    cluster are activated only when that candidate actually fills its slot.
    """
    anchors = [
        occurrence
        for occurrence in context.occurrences
        if occurrence.grouped_with_rotation_ids
    ]
    if not anchors:
        return

    by_resident_rotation: dict[tuple[str, str], list[Occurrence]] = defaultdict(list)
    for occurrence in context.occurrences:
        by_resident_rotation[occurrence.resident_id, occurrence.rotation_id].append(
            occurrence
        )

    selected_cache: dict[str, Any] = {}
    start_cache: dict[str, Any] = {}
    companion_uses: dict[str, list[Any]] = defaultdict(list)

    for anchor in anchors:
        anchor_selected = _optional_occurrence_selected(
            context,
            anchor,
            selected_cache,
        )
        exemption_literals = _anchored_group_exemption_literals(context, anchor)
        requires_group = context.model.NewBoolVar(
            f"anchored-group-required:{anchor.key}"
        )
        context.model.Add(
            requires_group == anchor_selected - sum(exemption_literals)
        )

        anchor_start = _optional_occurrence_start(
            context,
            anchor,
            start_cache,
        )
        selected = {
            anchor.rotation_id: _member_from_start(
                context,
                anchor.rotation_id,
                anchor_start,
                anchor.duration_weeks,
                f"anchored-group:{anchor.key}:{anchor.rotation_id}",
            )
        }
        complete = True
        for rotation_id in anchor.grouped_with_rotation_ids:
            companion_kind = context.rotations[rotation_id].kind
            candidates = [
                occurrence
                for occurrence in by_resident_rotation.get(
                    (anchor.resident_id, rotation_id),
                    [],
                )
                if companion_kind in {RotationKind.CLINIC, RotationKind.FMED}
                or not occurrence.elective
            ]
            if not candidates:
                context.model.Add(requires_group == 0)
                complete = False
                continue

            matches = []
            selected_start = context.model.NewIntVar(
                1,
                context.instance.calendar.weeks,
                f"anchored-group-start:{anchor.key}:{rotation_id}",
            )
            selected_duration = context.model.NewIntVar(
                1,
                5,
                f"anchored-group-duration:{anchor.key}:{rotation_id}",
            )
            for companion in candidates:
                match = context.model.NewBoolVar(
                    f"anchored-group-match:{anchor.key}:{companion.key}"
                )
                matches.append(match)
                companion_uses[companion.key].append(match)
                companion_selected = _optional_occurrence_selected(
                    context,
                    companion,
                    selected_cache,
                )
                context.model.Add(match <= companion_selected)
                context.model.Add(
                    selected_start
                    == _optional_occurrence_start(
                        context,
                        companion,
                        start_cache,
                    )
                ).OnlyEnforceIf(match)
                context.model.Add(
                    selected_duration == companion.duration_weeks
                ).OnlyEnforceIf(match)
            context.model.Add(sum(matches) == requires_group)
            selected[rotation_id] = _member_from_start(
                context,
                rotation_id,
                selected_start,
                selected_duration,
                f"anchored-group:{anchor.key}:{rotation_id}",
            )

        if complete:
            rotation_ids = [anchor.rotation_id, *anchor.grouped_with_rotation_ids]
            _enforce_cluster(
                context,
                selected,
                rotation_ids,
                pgy=anchor.pgy,
                name=f"anchored:{anchor.key}",
                exempt=requires_group.Not(),
            )

    for matches in companion_uses.values():
        context.model.Add(sum(matches) <= 1)


def _optional_occurrence_selected(
    context: PlanningContext,
    occurrence: Occurrence,
    cache: dict[str, Any],
):
    selected = cache.get(occurrence.key)
    if selected is not None:
        return selected
    selected = context.model.NewBoolVar(f"occurrence-selected:{occurrence.key}")
    context.model.Add(
        selected
        == sum(
            context.placements[occurrence.key, start]
            for start in context.starts[occurrence.key]
        )
    )
    cache[occurrence.key] = selected
    return selected


def _optional_occurrence_start(
    context: PlanningContext,
    occurrence: Occurrence,
    cache: dict[str, Any],
):
    variable = cache.get(occurrence.key)
    if variable is not None:
        return variable
    starts = context.starts[occurrence.key]
    variable = context.model.NewIntVar(
        min(starts),
        max(starts),
        f"optional-occurrence-start:{occurrence.key}",
    )
    for start in starts:
        context.model.Add(variable == start).OnlyEnforceIf(
            context.placements[occurrence.key, start]
        )
    cache[occurrence.key] = variable
    return variable


def _anchored_group_exemption_literals(
    context: PlanningContext,
    occurrence: Occurrence,
) -> list[Any]:
    placements = []
    seen_starts: set[int] = set()
    for lock in context.instance.locks:
        if (
            lock.resident_id != occurrence.resident_id
            or lock.rotation_id != occurrence.rotation_id
            or lock.elective != occurrence.elective
            or not lock.grouping_exempt
            or not lock.exact_block
            or len(lock.weeks) != occurrence.duration_weeks
        ):
            continue
        start = lock.weeks[0]
        if start in context.starts[occurrence.key] and start not in seen_starts:
            placements.append(context.placements[occurrence.key, start])
            seen_starts.add(start)
    return placements


def _fixed_member(context: PlanningContext, occurrence: Occurrence) -> _SelectedMember:
    return _member_from_start(
        context,
        occurrence.rotation_id,
        _occurrence_start(context, occurrence),
        occurrence.duration_weeks,
        f"fixed-group:{occurrence.key}",
    )


def _occurrence_start(context: PlanningContext, occurrence: Occurrence):
    starts = context.starts[occurrence.key]
    variable = context.model.NewIntVar(
        min(starts),
        max(starts),
        f"group-occurrence-start:{occurrence.key}",
    )
    context.model.Add(
        variable
        == sum(
            start * context.placements[occurrence.key, start]
            for start in starts
        )
    )
    return variable


def _member_from_start(
    context: PlanningContext,
    rotation_id: str,
    start,
    duration,
    name: str,
) -> _SelectedMember:
    end = context.model.NewIntVar(
        2,
        context.instance.calendar.weeks + 1,
        f"{name}:end",
    )
    context.model.Add(end == start + duration)
    return _SelectedMember(
        rotation_id=rotation_id,
        start=start,
        duration=duration,
        end=end,
    )


def _enforce_cluster(
    context: PlanningContext,
    selected: dict[str, _SelectedMember],
    rotation_ids: list[str],
    *,
    pgy: int,
    name: str,
    exempt,
) -> None:
    members = [selected[rotation_id] for rotation_id in rotation_ids]
    minimum = context.model.NewIntVar(
        1,
        context.instance.calendar.weeks,
        f"group-min:{name}",
    )
    maximum = context.model.NewIntVar(
        2,
        context.instance.calendar.weeks + 1,
        f"group-max:{name}",
    )
    context.model.AddMinEquality(minimum, [member.start for member in members])
    context.model.AddMaxEquality(maximum, [member.end for member in members])
    contiguous = context.model.Add(
        maximum - minimum == sum(member.duration for member in members)
    )
    if exempt is not None:
        contiguous.OnlyEnforceIf(exempt.Not())

    member_ids = set(rotation_ids)
    for rotation_id in rotation_ids:
        rule = context.rotations[rotation_id].pgy_rule(pgy)
        for predecessor_id in rule.prerequisite_rotation_ids:
            if predecessor_id not in member_ids:
                continue
            ordering = context.model.Add(
                selected[rotation_id].start >= selected[predecessor_id].end
            )
            if exempt is not None:
                ordering.OnlyEnforceIf(exempt.Not())


def _bind_manual_exemptions(
    context: PlanningContext,
    resident_id: str,
    key: str,
    occurrences: list[Occurrence],
    clusters: list[_FlexibleCluster],
    fixed_clusters: list[_FixedCluster],
) -> None:
    locks = [
        lock
        for lock in context.instance.locks
        if lock.resident_id == resident_id and lock.grouping_exempt
    ]
    relevant = []
    for lock in locks:
        candidates = [
            occurrence
            for occurrence in occurrences
            if occurrence.rotation_id == lock.rotation_id
            and occurrence.duration_weeks == len(lock.weeks)
            and lock.weeks[0] in context.starts[occurrence.key]
        ]
        if candidates:
            relevant.append((lock, candidates))
    all_exemptions = [
        *(cluster.exempt for cluster in clusters),
        *(cluster.exempt for cluster in fixed_clusters),
    ]
    if not relevant:
        for exemption in all_exemptions:
            context.model.Add(exemption == 0)
        return

    for lock, candidates in relevant:
        lock_links = []
        start = lock.weeks[0]
        for candidate in candidates:
            placed = context.placements[candidate.key, start]
            for cluster in clusters:
                if candidate.key == cluster.anchor.key:
                    link = placed
                else:
                    match = cluster.matches.get(candidate.key)
                    if match is None:
                        continue
                    link = context.model.NewBoolVar(
                        f"group-exempt-link:{key}:{candidate.key}:{cluster.anchor.key}"
                    )
                    context.model.Add(link <= placed)
                    context.model.Add(link <= match)
                    context.model.Add(link >= placed + match - 1)
                context.model.Add(cluster.exempt >= link)
                lock_links.append(link)
            for fixed_cluster in fixed_clusters:
                if candidate not in fixed_cluster.occurrences:
                    continue
                context.model.Add(fixed_cluster.exempt >= placed)
                lock_links.append(placed)
        context.model.AddExactlyOne(lock_links)
    context.model.Add(sum(all_exemptions) == len(relevant))
