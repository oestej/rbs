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

    By default the block uses the resident's otherwise-unallocated time.
    ``replaces_rotation_id`` may instead name a same-length direct Elective
    block to remove when the resident has no suitable unallocated time.
    """

    resident_id: str
    rotation_id: str
    start_week: int = Field(ge=1)
    duration_weeks: int = Field(ge=1, le=5)
    replaces_rotation_id: str | None = Field(
        default=None,
        description=(
            "Same-length Elective block replaced to fund this placement. None uses "
            "the resident's unallocated time, which is preferred when available."
        ),
    )


class ResidentRotationOverride(StrictModel):
    """One additional resident-specific required-service or elective block.

    The solver places the block normally. By default it uses the resident's
    otherwise-unallocated time and leaves every curriculum requirement in
    place. ``replaces_rotation_id`` may instead name a same-length direct
    Elective block to remove when the resident has no suitable unallocated time.

    Set ``elective`` for a named elective take of one service instead: the
    solver places it like other elective blocks, but the service is fixed
    rather than matched from the resident's preferences. This field is an
    additive option (older documents without it load as required-service
    blocks), so it does not change the enclosing document version.
    """

    resident_id: str
    rotation_id: str
    duration_weeks: int = Field(ge=1, le=5)
    elective: bool = Field(
        default=False,
        description=(
            "Whether this addition is an elective take of the named service. "
            "Elective takes bypass preference matching and count toward the "
            "service's elective repeat limits."
        ),
    )
    replaces_rotation_id: str | None = Field(
        default=None,
        description=(
            "Same-length Elective block replaced to fund this addition. None uses "
            "the resident's unallocated time, which is preferred when available."
        ),
    )
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
    freed weeks become unscheduled time. Waivers share the resident's direct
    requirement inventory with replacement-funded additions, so one block
    cannot be both waived and replaced away.
    """

    resident_id: str
    rotation_id: str
    duration_weeks: int = Field(ge=1, le=5)
