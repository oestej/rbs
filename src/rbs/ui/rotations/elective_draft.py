"""Shared working state for Elective-option controls across rotation editors."""

from __future__ import annotations

from typing import TypedDict

from rbs.models.instance import SchedulerInput
from rbs.models.rotation import Rotation
from rbs.ui.rotations.ops import elective_shapes_for_rotation


class ElectiveOptionDraft(TypedDict):
    """Mutable UI state common to Mandatory, FMED, and Elective editors."""

    eligible_block_sizes: list[int]
    repeatable: bool
    blackout_weeks: set[int]
    shapes: set[tuple[int, int]]


def elective_option_draft(
    instance: SchedulerInput,
    rotation: Rotation | None,
) -> ElectiveOptionDraft:
    """Build one complete Elective-option draft from current workspace state."""
    option = instance.electives.option_for(rotation.id) if rotation is not None else None
    return {
        "eligible_block_sizes": (
            list(instance.eligible_elective_block_sizes(rotation.id))
            if rotation is not None
            else list(instance.elective_block_sizes)
        ),
        "repeatable": bool(option and option.repeatable),
        "blackout_weeks": set(option.blackout_weeks if option is not None else ()),
        "shapes": (
            elective_shapes_for_rotation(instance, rotation.id)
            if rotation is not None
            else set()
        ),
    }
