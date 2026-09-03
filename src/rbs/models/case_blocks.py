"""Resident-specific case blocks (overrides and manual clinic placements)."""

from __future__ import annotations

from pydantic import Field, field_validator

from rbs.models.common import StrictModel
from rbs.models.enums import Session, Weekday


class AcademicHalfDayOverride(StrictModel):
    """A one-week replacement for the program's recurring academic half-day."""

    week: int = Field(ge=1)
    weekday: Weekday
    session: Session


class ManualClinicBlock(StrictModel):
    """A resident-specific Clinic block placed at an exact week.

    The block replaces a same-length direct curriculum requirement so the
    resident's academic year remains exactly 52 weeks.
    """

    resident_id: str
    rotation_id: str
    start_week: int = Field(ge=1)
    duration_weeks: int = Field(ge=1, le=5)
    replaces_rotation_id: str


class ResidentRotationOverride(StrictModel):
    """One additional resident-specific Mandatory block.

    The solver places the block normally and removes a same-length direct
    Elective requirement for that resident.
    """

    resident_id: str
    rotation_id: str
    duration_weeks: int = Field(ge=1, le=5)
    replaces_rotation_id: str
    group_instance_id: str | None = Field(
        default=None,
        description=(
            "Shared identifier for resident overrides intentionally added as one "
            "contiguous rotation-group instance. None means an unmatched extra."
        ),
    )

    @field_validator("group_instance_id")
    @classmethod
    def normalize_group_instance_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("group_instance_id cannot be blank")
        return normalized
