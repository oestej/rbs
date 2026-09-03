"""CP-SAT hints derived from an existing reference solution."""

from __future__ import annotations

from collections import defaultdict

from rbs.models.enums import RotationKind, Session, Weekday
from rbs.models.schedule import Schedule

BlockSignature = tuple[str, str, int, bool]


def add_reference_hints(context, decisions, reference_schedule: Schedule | None) -> int:
    """Warm-start CP-SAT from compatible block starts and clinic choices.

    Only compatible decisions are hinted. The hints remain repairable suggestions,
    so a revised hard constraint can move any part of the prior draft.
    """
    if (
        reference_schedule is None
        or reference_schedule.meta.academic_year != context.instance.academic_year
    ):
        return 0

    starts_by_signature: dict[BlockSignature, set[int]] = defaultdict(set)
    assignments_by_block: dict[tuple[str, str, int, int, bool], list] = defaultdict(list)
    for assignment in reference_schedule.assignments:
        block_start = assignment.block_start_week or assignment.start_week
        block_duration = assignment.block_duration_weeks or (
            assignment.end_week - assignment.start_week + 1
        )
        signature = (
            assignment.resident_id,
            assignment.rotation_id,
            block_duration,
            assignment.elective,
        )
        starts_by_signature[signature].add(block_start)
        assignments_by_block[
            assignment.resident_id,
            assignment.rotation_id,
            block_start,
            block_duration,
            assignment.elective,
        ].append(assignment)

    occurrences_by_signature = defaultdict(list)
    for occurrence in context.occurrences:
        occurrences_by_signature[
            occurrence.resident_id,
            occurrence.rotation_id,
            occurrence.duration_weeks,
            occurrence.elective,
        ].append(occurrence)

    matched_starts: dict[str, int] = {}
    for signature, reference_starts in starts_by_signature.items():
        available = sorted(
            occurrences_by_signature.get(signature, []),
            key=lambda occurrence: (
                occurrence.fixed_start_week is None,
                occurrence.key,
            ),
        )
        for start in sorted(reference_starts):
            occurrence = next(
                (candidate for candidate in available if start in context.starts[candidate.key]),
                None,
            )
            if occurrence is None:
                continue
            matched_starts[occurrence.key] = start
            available.remove(occurrence)

    hints = 0
    occurrences_by_key = {occurrence.key: occurrence for occurrence in context.occurrences}
    complete_blocks = _block_hint_is_complete(context, matched_starts)
    for (occurrence_key, start), placement in context.placements.items():
        selected = matched_starts.get(occurrence_key) == start
        if complete_blocks or selected:
            context.model.AddHint(placement, int(selected))
            hints += 1

    for occurrence_key, start in matched_starts.items():
        decision = decisions.get(occurrence_key)
        occurrence = occurrences_by_key[occurrence_key]
        if decision is None:
            continue
        assignments = assignments_by_block.get(
            (
                occurrence.resident_id,
                occurrence.rotation_id,
                start,
                occurrence.duration_weeks,
                occurrence.elective,
            ),
            [],
        )
        desired: set[tuple[Weekday, Session]] = set()
        clinic_block = (
            context.rotations[occurrence.rotation_id].kind is RotationKind.CLINIC
        )
        for assignment in assignments:
            for slot in assignment.clinic_slots:
                if clinic_block:
                    if slot.admin:
                        desired.add((slot.weekday, slot.session))
                elif not slot.admin:
                    desired.add((slot.weekday, slot.session))
        selected_indices = {
            index
            for index, slot in enumerate(decision.domain)
            if slot.weekday is not None
            and slot.session is not None
            and (slot.weekday, slot.session) in desired
        }
        if len(selected_indices) != decision.pick:
            continue
        for index, selected in enumerate(decision.selected):
            context.model.AddHint(selected, int(index in selected_indices))
            hints += 1
    return hints


def _block_hint_is_complete(context, matched_starts: dict[str, int]) -> bool:
    """Whether the matched starts form a complete block solution."""
    if not matched_starts:
        return False
    occurrences_by_key = {occurrence.key: occurrence for occurrence in context.occurrences}
    for resident_id, occurrences in context.by_resident.items():
        if not occurrences:
            continue
        for week in context.weeks:
            covering = sum(
                start <= week < start + occurrences_by_key[key].duration_weeks
                for key, start in matched_starts.items()
                if occurrences_by_key[key].resident_id == resident_id
            )
            if covering != 1:
                return False

    groups: dict[str, list] = defaultdict(list)
    for occurrence in context.occurrences:
        groups[occurrence.group_id].append(occurrence)
    return all(
        sum(occurrence.key in matched_starts for occurrence in group) == 1
        for group in groups.values()
    )
