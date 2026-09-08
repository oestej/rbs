"""Workspace-case exceptions that sit outside reusable rotation definitions."""

from __future__ import annotations

from collections import Counter

from rbs.models.enums import RotationKind
from rbs.models.instance import (
    ManualClinicBlock,
    ResidentRotationOverride,
    ResidentRotationWaiver,
    SchedulerInput,
)
from rbs.models.rotation import rotation_display_sort_key
from rbs.models.schedule import Schedule
from rbs.ui.drafts import Draft
from rbs.ui.locks import (
    ScheduleBlock,
    clear_schedule_block,
    replace_schedule_block,
)


def add_manual_clinic_block(
    instance: SchedulerInput,
    block: ManualClinicBlock | Draft,
) -> SchedulerInput:
    """Add a fixed resident Clinic block and validate its funding."""
    added = (
        block
        if isinstance(block, ManualClinicBlock)
        else ManualClinicBlock.model_validate(block)
    )
    return instance.revised(
        manual_clinic_blocks=[*instance.manual_clinic_blocks, added]
    )


def remove_manual_clinic_block(
    instance: SchedulerInput,
    index: int,
) -> SchedulerInput:
    """Remove a fixed resident Clinic block by its displayed position."""
    if not 0 <= index < len(instance.manual_clinic_blocks):
        raise ValueError("manual Clinic block not found")
    blocks = list(instance.manual_clinic_blocks)
    blocks.pop(index)
    return instance.revised(manual_clinic_blocks=blocks)


def place_manual_clinic_block(
    instance: SchedulerInput,
    schedule: Schedule | None,
    block: ManualClinicBlock | Draft,
) -> tuple[SchedulerInput, Schedule | None]:
    """Add a fixed Clinic block and place it on the working schedule."""
    updated = add_manual_clinic_block(instance, block)
    added = updated.manual_clinic_blocks[-1]
    draft = replace_schedule_block(
        updated,
        schedule,
        ScheduleBlock(
            resident_id=added.resident_id,
            rotation_id=added.rotation_id,
            start_week=added.start_week,
            duration_weeks=added.duration_weeks,
            elective=False,
        ),
    )
    if schedule is not None and draft is schedule:
        return updated, None
    return updated, draft


def withdraw_manual_clinic_block(
    instance: SchedulerInput,
    schedule: Schedule | None,
    index: int,
) -> tuple[SchedulerInput, Schedule | None]:
    """Remove a fixed Clinic block and clear its draft placement when present."""
    if not 0 <= index < len(instance.manual_clinic_blocks):
        raise ValueError("manual Clinic block not found")
    removed = instance.manual_clinic_blocks[index]
    updated = remove_manual_clinic_block(instance, index)
    if schedule is None:
        return updated, None
    try:
        draft = clear_schedule_block(
            schedule,
            ScheduleBlock(
                resident_id=removed.resident_id,
                rotation_id=removed.rotation_id,
                start_week=removed.start_week,
                duration_weeks=removed.duration_weeks,
                elective=False,
            ),
        )
    except ValueError:
        return updated, None
    return updated, draft


def add_resident_rotation_waiver(
    instance: SchedulerInput,
    waiver: ResidentRotationWaiver | Draft,
) -> SchedulerInput:
    """Excuse one resident from one direct curriculum block."""
    added = (
        waiver
        if isinstance(waiver, ResidentRotationWaiver)
        else ResidentRotationWaiver.model_validate(waiver)
    )
    return instance.revised(
        resident_rotation_waivers=[*instance.resident_rotation_waivers, added]
    )


def remove_resident_rotation_waiver(
    instance: SchedulerInput,
    index: int,
) -> SchedulerInput:
    """Remove a resident waiver by its position in the case."""
    if not 0 <= index < len(instance.resident_rotation_waivers):
        raise ValueError("resident rotation waiver not found")
    waivers = list(instance.resident_rotation_waivers)
    waivers.pop(index)
    return instance.revised(resident_rotation_waivers=waivers)


def remaining_direct_elective_blocks(
    instance: SchedulerInput,
    resident_id: str,
) -> dict[tuple[str, int], int]:
    """Unconsumed direct Elective blocks by rotation and duration."""
    resident = instance.residents_by_id.get(resident_id)
    if resident is None:
        return {}
    remaining: Counter[tuple[str, int]] = Counter()
    for block in instance.curriculum_for(resident.pgy).blocks:
        if instance.rotation(block.rotation_id).kind is RotationKind.ELECTIVE:
            remaining[block.rotation_id, block.duration_weeks] += block.count
    for waiver in instance.resident_rotation_waivers:
        if waiver.resident_id == resident_id:
            remaining[waiver.rotation_id, waiver.duration_weeks] -= 1
    for manual in instance.manual_clinic_blocks:
        if manual.resident_id == resident_id and manual.replaces_rotation_id is not None:
            remaining[manual.replaces_rotation_id, manual.duration_weeks] -= 1
    for override in instance.resident_rotation_overrides:
        if (
            override.resident_id == resident_id
            and override.replaces_rotation_id is not None
        ):
            remaining[override.replaces_rotation_id, override.duration_weeks] -= 1
    return {key: count for key, count in sorted(remaining.items()) if count > 0}


def named_elective_take_service_options(
    instance: SchedulerInput,
    resident_id: str,
    duration_weeks: int,
) -> dict[str, str]:
    """Eligible services for one named elective take, keyed by rotation ID."""
    resident = instance.residents_by_id.get(resident_id)
    if resident is None:
        return {}
    choices = [
        (
            rotation_display_sort_key(rotation),
            rotation.id,
            f"{rotation.code} · {rotation.name}",
        )
        for rotation in instance.elective_options_for(resident.pgy, duration_weeks)
    ]
    return {value: label for _sort, value, label in sorted(choices)}


def named_elective_take_duration_options(
    instance: SchedulerInput,
    resident_id: str,
) -> list[int]:
    """Block lengths with at least one eligible take service."""
    resident = instance.residents_by_id.get(resident_id)
    if resident is None:
        return []
    return sorted(
        duration
        for duration in range(1, 6)
        if instance.elective_options_for(resident.pgy, duration)
    )


def add_named_elective_take(
    instance: SchedulerInput,
    *,
    resident_id: str,
    rotation_id: str,
    duration_weeks: int,
    replaces_rotation_id: str | None,
) -> SchedulerInput:
    """Add one solver-placed elective take of a named service."""
    added = ResidentRotationOverride(
        resident_id=resident_id,
        rotation_id=rotation_id,
        duration_weeks=duration_weeks,
        elective=True,
        replaces_rotation_id=replaces_rotation_id,
    )
    return instance.revised(
        resident_rotation_overrides=[*instance.resident_rotation_overrides, added]
    )


def named_elective_takes(
    instance: SchedulerInput,
    resident_id: str,
) -> list[tuple[int, ResidentRotationOverride]]:
    """Elective takes for one resident with their case positions."""
    return [
        (index, override)
        for index, override in enumerate(instance.resident_rotation_overrides)
        if override.elective and override.resident_id == resident_id
    ]


def remove_named_elective_take(
    instance: SchedulerInput,
    index: int,
) -> SchedulerInput:
    """Remove one elective take by its position in the case."""
    if not 0 <= index < len(instance.resident_rotation_overrides):
        raise ValueError("resident elective take not found")
    if not instance.resident_rotation_overrides[index].elective:
        raise ValueError("resident elective take not found")
    kept = list(instance.resident_rotation_overrides)
    kept.pop(index)
    return instance.revised(resident_rotation_overrides=kept)


def elective_waiver_duration_options(
    instance: SchedulerInput,
    resident_id: str,
) -> dict[int, str]:
    """Direct elective block lengths one resident can still waive."""
    durations = sorted(
        {
            duration
            for _, duration in remaining_direct_elective_blocks(
                instance,
                resident_id,
            )
        }
    )
    return {
        duration: f"{duration}-week elective block" for duration in durations
    }


def resolve_elective_waiver_rotation(
    instance: SchedulerInput,
    resident_id: str,
    duration_weeks: int,
) -> str:
    """Return a waivable direct elective rotation for one block length."""
    remaining = remaining_direct_elective_blocks(instance, resident_id)
    rotation_id = next(
        (rid for (rid, duration) in remaining if duration == duration_weeks),
        None,
    )
    if rotation_id is None:
        raise ValueError("no waivable elective block of that length remains")
    return rotation_id
