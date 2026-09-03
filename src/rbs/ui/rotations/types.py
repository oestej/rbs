"""Shared callback aliases and sentinel ids for the rotations editor."""

from __future__ import annotations

from collections.abc import Callable

from rbs.models.instance import SchedulerInput

SelectRotation = Callable[[str | None], None]

SaveRotation = Callable[[SchedulerInput, str | None], None]

NEW_MANDATORY_ROTATION_ID = "__new_mandatory_rotation__"
