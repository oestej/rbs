"""Pure mutations for dated Special rotations."""

from __future__ import annotations

from rbs.models.instance import SchedulerInput
from rbs.models.special import SpecialRotation
from rbs.ui.drafts import Draft

__all__ = [
    "add_special_rotation",
    "next_special_rotation_id",
    "remove_special_rotation",
    "replace_special_rotation",
]


def next_special_rotation_id(instance: SchedulerInput) -> str:
    """Return the next neutral workspace-scoped Special rotation ID."""
    used = {special.id for special in instance.special_rotations}
    sequence = 1
    while True:
        candidate = f"special-{sequence:03d}"
        if candidate not in used:
            return candidate
        sequence += 1


def add_special_rotation(
    instance: SchedulerInput,
    special: SpecialRotation | Draft,
) -> SchedulerInput:
    """Add one validated dated Special rotation."""
    added = (
        special if isinstance(special, SpecialRotation) else SpecialRotation.model_validate(special)
    )
    if added.id in instance.special_rotations_by_id:
        raise ValueError(f"special rotation ID {added.id!r} already exists")
    return instance.revised(special_rotations=[*instance.special_rotations, added])


def replace_special_rotation(
    instance: SchedulerInput,
    original_id: str,
    replacement: SpecialRotation | Draft,
) -> SchedulerInput:
    """Replace a Special rotation while retaining its stable ID."""
    if original_id not in instance.special_rotations_by_id:
        raise ValueError(f"unknown special rotation {original_id!r}")
    updated = (
        replacement
        if isinstance(replacement, SpecialRotation)
        else SpecialRotation.model_validate(replacement)
    )
    if updated.id != original_id:
        raise ValueError("special rotation ID is a system key and cannot be changed")
    return instance.revised(
        special_rotations=[
            updated if special.id == original_id else special
            for special in instance.special_rotations
        ]
    )


def remove_special_rotation(
    instance: SchedulerInput,
    special_id: str,
) -> SchedulerInput:
    """Remove one Special rotation by stable ID."""
    if special_id not in instance.special_rotations_by_id:
        raise ValueError(f"unknown special rotation {special_id!r}")
    return instance.revised(
        special_rotations=[
            special for special in instance.special_rotations if special.id != special_id
        ]
    )
