"""Pure projections for the resident-by-rotation configuration summary."""

from __future__ import annotations

from collections import Counter

from rbs.models.enums import RotationKind
from rbs.models.instance import SchedulerInput
from rbs.models.rotation import rotation_display_sort_key
from rbs.models.schedule import Schedule
from rbs.ui.locks import ScheduleBlock, schedule_blocks


def _rotation_summary_category(kind: RotationKind) -> str:
    if kind is RotationKind.ELECTIVE:
        return "elective"
    if kind is RotationKind.CLINIC:
        return "clinic"
    return "mandatory"


def _resident_planned_blocks(
    instance: SchedulerInput,
    schedule: Schedule | None,
    resident_id: str,
) -> list[ScheduleBlock]:
    """Overlay pending exact manual blocks on the latest schedule."""
    blocks = schedule_blocks(schedule, resident_id=resident_id)
    for lock in instance.locks:
        if (
            lock.source != "manual"
            or not lock.exact_block
            or lock.resident_id != resident_id
        ):
            continue
        hardcoded = ScheduleBlock(
            resident_id=resident_id,
            rotation_id=lock.rotation_id,
            start_week=lock.weeks[0],
            duration_weeks=len(lock.weeks),
            elective=lock.elective,
        )
        hardcoded_weeks = set(hardcoded.weeks)
        blocks = [
            block for block in blocks if not (set(block.weeks) & hardcoded_weeks)
        ]
        blocks.append(hardcoded)
    return sorted(blocks, key=lambda block: (block.start_week, block.rotation_id))


def resident_missing_mandatory_rotations(
    instance: SchedulerInput,
    schedule: Schedule | None,
    resident_id: str,
) -> tuple[int, list[str]]:
    """Return missing mandatory block count and concise block labels."""
    resident = next(item for item in instance.residents if item.id == resident_id)
    curriculum = instance.curriculum_for(resident.pgy)
    actual: Counter[tuple[str, int]] = Counter(
        (block.rotation_id, block.duration_weeks)
        for block in _resident_planned_blocks(instance, schedule, resident_id)
        if not block.elective
    )
    required: Counter[tuple[str, int]] = Counter()
    for block in curriculum.blocks:
        if (
            _rotation_summary_category(instance.rotation(block.rotation_id).kind)
            == "mandatory"
        ):
            required[block.rotation_id, block.duration_weeks] += block.count
    for override in instance.resident_rotation_overrides:
        if override.resident_id == resident_id:
            required[override.rotation_id, override.duration_weeks] += 1
    for waiver in instance.resident_rotation_waivers:
        if waiver.resident_id == resident_id:
            key = (waiver.rotation_id, waiver.duration_weeks)
            required[key] = max(0, required.get(key, 0) - 1)

    missing_count = 0
    labels: list[str] = []
    for (rotation_id, duration), required_count in sorted(
        required.items(),
        key=lambda item: rotation_display_sort_key(instance.rotation(item[0][0])),
    ):
        present = min(actual[rotation_id, duration], required_count)
        actual[rotation_id, duration] -= present
        missing = required_count - present
        if missing <= 0:
            continue
        missing_count += missing
        rotation = instance.rotation(rotation_id)
        block_label = f"{rotation.code} ({duration} wk)"
        labels.append(
            f"{missing}× {block_label}" if missing > 1 else block_label
        )

    return missing_count, labels


def resident_rotation_week_totals(
    instance: SchedulerInput,
    resident_id: str,
) -> dict[str, int]:
    """Return configured weeks in each Rotation Summary category."""
    resident = next(item for item in instance.residents if item.id == resident_id)
    curriculum = instance.curriculum_for(resident.pgy)
    totals = {"mandatory": 0, "elective": 0, "clinic": 0}
    for block in curriculum.blocks:
        category = _rotation_summary_category(
            instance.rotation(block.rotation_id).kind
        )
        totals[category] += block.duration_weeks * block.count
    for block in instance.manual_clinic_blocks:
        if block.resident_id != resident_id:
            continue
        totals["clinic"] += block.duration_weeks
        if block.replaces_rotation_id is not None:
            totals["elective"] -= block.duration_weeks
    for override in instance.resident_rotation_overrides:
        if override.resident_id != resident_id:
            continue
        category = "elective" if override.elective else "mandatory"
        totals[category] += override.duration_weeks
        if override.replaces_rotation_id is not None:
            totals["elective"] -= override.duration_weeks
    for waiver in instance.resident_rotation_waivers:
        if waiver.resident_id != resident_id:
            continue
        category = _rotation_summary_category(
            instance.rotation(waiver.rotation_id).kind
        )
        totals[category] -= waiver.duration_weeks
    return totals
