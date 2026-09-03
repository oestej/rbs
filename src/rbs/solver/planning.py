from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from rbs.models.enums import RotationKind
from rbs.models.instance import Calendar, SolverConfig, SolverProblem
from rbs.models.resident import Resident
from rbs.models.rotation import Rotation


@dataclass(frozen=True)
class Occurrence:
    """One placeable block or Elective-service candidate."""

    key: str
    resident_id: str
    pgy: int
    rotation_id: str
    duration_weeks: int
    group_id: str
    elective: bool = False
    elective_fallback: bool = False
    preference_managed: bool = False
    prerequisite_rotation_ids: tuple[str, ...] = ()
    earliest_start_week: int | None = None
    fixed_start_week: int | None = None
    rotation_group_key: str | None = None
    rotation_group_instance_id: str | None = None


def rotation_group_key(pgy: int, rotation_ids: list[str] | tuple[str, ...]) -> str:
    """Return a stable internal key for one training-level rotation group."""
    return f"pgy:{pgy}:" + ":".join(sorted(rotation_ids))


def aligned_starts(duration_weeks: int, calendar: Calendar) -> list[int]:
    align = calendar.block_start_alignment
    last = calendar.weeks - duration_weeks + 1
    return [week for week in range(1, last + 1) if (week - 1) % align == 0]


def weeks_covered(start: int, duration_weeks: int) -> list[int]:
    return list(range(start, start + duration_weeks))


def covers(start: int, duration_weeks: int, week: int) -> bool:
    return start <= week < start + duration_weeks


def spans_four_week_boundary(start: int, duration_weeks: int) -> bool:
    """Whether a block crosses between A/1 through M/13."""
    end = start + duration_weeks - 1
    return (start - 1) // 4 != (end - 1) // 4


def rotate_domain(domain: list, key: str) -> list:
    """Stable rotation so overlay search does not always start on Monday."""
    if not domain:
        return domain
    shift = sum(ord(char) for char in key) % len(domain)
    if shift == 0:
        return list(domain)
    return list(domain[shift:]) + list(domain[:shift])


def expand_occurrences(
    instance: SolverProblem,
    *,
    require_configured_electives: bool = True,
) -> list[Occurrence]:
    """Expand curriculum requirements into placeable candidate blocks.

    Schedule compilation requires every Elective slot to have a configured
    candidate. Read-only calculations which ignore Elective candidates may opt
    out so the rest of an incomplete workspace remains configurable.
    """
    occurrences: list[Occurrence] = []
    rotations = instance.rotations_by_id
    for resident in instance.residents:
        curriculum = instance.curriculum_for(resident.pgy)
        if not curriculum.blocks:
            continue
        seen: dict[tuple[str, int], int] = defaultdict(int)
        manual_blocks = [
            block for block in instance.manual_clinic_blocks if block.resident_id == resident.id
        ]
        resident_overrides = [
            override
            for override in instance.resident_rotation_overrides
            if override.resident_id == resident.id
        ]
        replacements: dict[tuple[str, int], int] = defaultdict(int)
        for manual in manual_blocks:
            replacements[manual.replaces_rotation_id, manual.duration_weeks] += 1
        for override in resident_overrides:
            replacements[
                override.replaces_rotation_id,
                override.duration_weeks,
            ] += 1
        for block in curriculum.blocks:
            replacement_count = replacements.get(
                (block.rotation_id, block.duration_weeks),
                0,
            )
            for _ in range(block.count - replacement_count):
                index = seen[(block.rotation_id, block.duration_weeks)]
                seen[(block.rotation_id, block.duration_weeks)] = index + 1
                base_key = f"{resident.id}:{block.rotation_id}:{block.duration_weeks}:{index}"
                direct_elective = rotations[block.rotation_id].kind is RotationKind.ELECTIVE
                candidates = (
                    _resident_preference_candidates(
                        instance,
                        resident,
                        block.duration_weeks,
                        require_configured_electives=require_configured_electives,
                    )
                    if direct_elective
                    else (rotations[block.rotation_id],)
                )
                for candidate in candidates:
                    rule = candidate.pgy_rule(resident.pgy)
                    elective = direct_elective
                    elective_fallback = elective and candidate.kind is RotationKind.CLINIC
                    group = (
                        instance.rotation_group_for(resident.pgy, candidate.id)
                        if not elective and candidate.id == block.rotation_id
                        else None
                    )
                    key = (
                        base_key
                        if len(candidates) == 1 and candidate.id == block.rotation_id
                        else f"{base_key}:elective-option:{candidate.id}"
                    )
                    occurrences.append(
                        Occurrence(
                            key=key,
                            resident_id=resident.id,
                            pgy=resident.pgy,
                            rotation_id=candidate.id,
                            duration_weeks=block.duration_weeks,
                            group_id=base_key if len(candidates) > 1 else key,
                            elective=elective,
                            elective_fallback=elective_fallback,
                            preference_managed=elective,
                            prerequisite_rotation_ids=tuple(rule.prerequisite_rotation_ids),
                            earliest_start_week=rule.earliest_start_week,
                            rotation_group_key=(
                                rotation_group_key(group.pgy, group.rotation_ids)
                                if group is not None
                                else None
                            ),
                        )
                    )
        for index, override in enumerate(resident_overrides):
            rule = rotations[override.rotation_id].pgy_rule(resident.pgy)
            override_group = (
                instance.rotation_group_for(resident.pgy, override.rotation_id)
                if override.group_instance_id is not None
                else None
            )
            key = (
                f"{resident.id}:resident-override:{override.rotation_id}:"
                f"{override.duration_weeks}:{index}"
            )
            occurrences.append(
                Occurrence(
                    key=key,
                    resident_id=resident.id,
                    pgy=resident.pgy,
                    rotation_id=override.rotation_id,
                    duration_weeks=override.duration_weeks,
                    group_id=key,
                    prerequisite_rotation_ids=tuple(rule.prerequisite_rotation_ids),
                    earliest_start_week=rule.earliest_start_week,
                    rotation_group_key=(
                        rotation_group_key(
                            override_group.pgy,
                            override_group.rotation_ids,
                        )
                        if override_group is not None
                        else None
                    ),
                    rotation_group_instance_id=(
                        f"{resident.id}:override-group:{override.group_instance_id}"
                        if override.group_instance_id is not None
                        else None
                    ),
                )
            )
        for index, manual in enumerate(manual_blocks):
            rule = rotations[manual.rotation_id].pgy_rule(resident.pgy)
            key = f"{resident.id}:manual-clinic:{manual.start_week}:{index}"
            occurrences.append(
                Occurrence(
                    key=key,
                    resident_id=resident.id,
                    pgy=resident.pgy,
                    rotation_id=manual.rotation_id,
                    duration_weeks=manual.duration_weeks,
                    group_id=key,
                    prerequisite_rotation_ids=tuple(rule.prerequisite_rotation_ids),
                    earliest_start_week=rule.earliest_start_week,
                    fixed_start_week=manual.start_week,
                )
            )
    return occurrences


def _resident_preference_candidates(
    instance: SolverProblem,
    resident: Resident,
    duration_weeks: int,
    *,
    require_configured_electives: bool,
) -> tuple[Rotation, ...]:
    """Candidate services for one direct resident Elective block.

    Resident requests are the complete candidate set. An otherwise unranked
    eligible service is admitted only when an Elective lock needs it, and
    Clinic is always appended as the explicit last-resort fallback.
    """
    if not require_configured_electives:
        return ()

    candidate_ids = {
        request.rotation_id
        for request in resident.elective_preferences
        if request.duration_weeks == duration_weeks
    }
    for lock in instance.locks:
        if lock.resident_id != resident.id or not lock.elective:
            continue
        if duration_weeks not in instance.block_durations_for_pgy(
            resident.pgy,
            lock.rotation_id,
            elective=True,
        ):
            continue
        if lock.exact_block and len(lock.weeks) != duration_weeks:
            continue
        candidate_ids.add(lock.rotation_id)

    candidates = [
        rotation
        for rotation in instance.rotations
        if rotation.id in candidate_ids
        and (
            (
                (option := instance.electives.option_for(rotation.id)) is not None
                and option.allows(resident.pgy, duration_weeks)
            )
            or instance.is_elective_fallback_rotation(
                rotation.id,
                resident.pgy,
                duration_weeks,
            )
        )
        and rotation.allows_duration(duration_weeks, pgy=resident.pgy)
    ]
    fallback = instance.elective_fallback_rotation(
        resident.pgy,
        duration_weeks,
    )
    if fallback is None:
        raise ValueError(
            f"{resident.id} has a {duration_weeks}-week direct Elective block, "
            "but Clinic has no compatible block configuration for fallback"
        )
    if fallback.id not in {rotation.id for rotation in candidates}:
        candidates.append(fallback)
    return tuple(candidates)


def clinic_block_week_band(instance: SolverProblem) -> tuple[int, int] | None:
    """Per-week band of dedicated Clinic blocks implied by the curriculum.

    Every Clinic occurrence is placed exactly once, so the year holds a fixed
    number of Clinic resident-weeks. Spreading them as evenly as the calendar
    allows puts each week at the floor or the ceiling of that average, which is
    the tightest band that can still be satisfied.
    """
    total = sum(
        occurrence.duration_weeks
        for occurrence in expand_occurrences(
            instance,
            require_configured_electives=False,
        )
        if instance.rotation(occurrence.rotation_id).kind is RotationKind.CLINIC
    )
    if total <= 0:
        return None
    weeks = instance.calendar.weeks
    return total // weeks, -(-total // weeks)


def resolve_clinic_block_band(
    instance: SolverProblem,
    options: SolverConfig,
) -> tuple[int | None, int | None, bool]:
    """Return the (floor, ceiling, was_automatic) clinic band to enforce.

    An explicit floor or ceiling wins outright; otherwise the curriculum-derived
    band applies when automatic balancing is on. The flag lets the engine drop
    an automatic band that turns out to be unsatisfiable rather than reporting
    the whole year infeasible.
    """
    low = options.min_clinic_blocks_per_week
    high = options.max_clinic_blocks_per_week
    if low is not None or high is not None:
        return low, high, False
    if not options.auto_balance_clinic_blocks:
        return None, None, False
    band = clinic_block_week_band(instance)
    if band is None:
        return None, None, False
    return band[0], band[1], True


def legal_starts(
    occurrence: Occurrence,
    resident: Resident,
    rotation: Rotation,
    calendar: Calendar,
    *,
    vacation_weeks: set[int] | None = None,
    allow_blocks_to_span_four_week_boundaries: bool = False,
) -> list[int]:
    vacation = set(resident.vacation_weeks if vacation_weeks is None else vacation_weeks)
    block_config = rotation.block_config(occurrence.pgy, occurrence.duration_weeks)
    starts: list[int] = []
    for start in aligned_starts(occurrence.duration_weeks, calendar):
        if not allow_blocks_to_span_four_week_boundaries and spans_four_week_boundary(
            start, occurrence.duration_weeks
        ):
            continue
        overlap = set(weeks_covered(start, occurrence.duration_weeks)) & vacation
        if overlap and not block_config.vacation.allowed:
            continue
        max_vac = block_config.vacation.max_weeks_per_block
        if max_vac is not None and len(overlap) > max_vac:
            continue
        if occurrence.earliest_start_week is not None and start < occurrence.earliest_start_week:
            continue
        starts.append(start)
    if occurrence.fixed_start_week is not None:
        return [occurrence.fixed_start_week] if occurrence.fixed_start_week in starts else []
    return starts
