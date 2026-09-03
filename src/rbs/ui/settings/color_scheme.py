"""Atomic color-scheme edits for the Settings workspace."""

from __future__ import annotations

from rbs.models.color_scheme import ColorScheme
from rbs.models.instance import SchedulerInput

__all__ = ["replace_color_scheme"]


def replace_color_scheme(
    instance: SchedulerInput,
    scheme: ColorScheme,
) -> SchedulerInput:
    """Replace the scheme and carry palette-slot changes into assigned colors."""
    replacements = {
        old.color: new.color
        for old, new in zip(
            instance.color_scheme.selectable_colors,
            scheme.selectable_colors,
            strict=False,
        )
        if old.color != new.color
    }

    raw = instance.model_dump(mode="json")
    raw["color_scheme"] = scheme.model_dump(mode="json")
    raw["electives"]["color"] = replacements.get(
        raw["electives"]["color"],
        raw["electives"]["color"],
    )
    for rotation in raw["rotations"]:
        rotation["color"] = replacements.get(rotation["color"], rotation["color"])
    for clinic in raw["clinic_policy"]["sites"]:
        clinic["color"] = replacements.get(clinic["color"], clinic["color"])
    return SchedulerInput.from_payload(raw)
