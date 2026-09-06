"""Resident-specific case blocks (overrides and manual clinic placements)."""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from rbs.models.common import StrictModel
from rbs.models.enums import Session, Weekday


class AcademicHalfDayOverride(StrictModel):
    """A one-week change to the program's recurring academic half-day.

    Setting a day and session moves that week's academic half-day. Omitting both
    cancels it for the week, which is how a program records a conference week or
    a holiday that displaces teaching without moving it.
    """

    week: int = Field(ge=1)
    weekday: Weekday | None = Field(
        default=None,
        description="None, with no session, cancels the academic half-day for this week.",
    )
    session: Session | None = Field(default=None)

    @model_validator(mode="after")
    def day_and_session_move_together(self) -> AcademicHalfDayOverride:
        if (self.weekday is None) != (self.session is None):
            raise ValueError(
                "academic half-day override must set both a day and a session, "
                "or neither to cancel the week"
            )
        return self

    @property
    def cancels_week(self) -> bool:
        """Whether this override removes the week's academic half-day entirely."""
        return self.weekday is None


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
    Elective requirement for that resident. When ``replaces_rotation_id`` is
    None the block is instead funded by the training level's unallocated
    weeks, leaving every curriculum requirement in place.
    """

    resident_id: str
    rotation_id: str
    duration_weeks: int = Field(ge=1, le=5)
    replaces_rotation_id: str | None = Field(default=None)
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


class ResidentRotationWaiver(StrictModel):
    """One resident-specific excusal from a direct curriculum block.

    The solver simply does not place the waived block for that resident; the
    freed weeks become unscheduled time. Waivers consume the same direct
    inventory as Mandatory rotation overrides, so a block cannot be both
    waived and replaced away.
    """

    resident_id: str
    rotation_id: str
    duration_weeks: int = Field(ge=1, le=5)
