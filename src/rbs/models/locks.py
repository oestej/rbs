"""Pinned block placements declared on the instance.

``LockedPlacement`` is the data; lock *semantics* live in ``rbs.clinic_locks``
(solved schedule occurrences) and ``rbs.ui.locks`` (workspace UI generation).
"""

from typing import Literal

from pydantic import Field, field_validator, model_validator

from rbs.models.common import StrictModel

LockSource = Literal["manual", "through_today"]


class LockedPlacement(StrictModel):
    """Force a resident onto a rotation for one or more weeks.

    Weeks do not have to be contiguous. A 4-week ICU lock is typically
    ``weeks: [17, 18, 19, 20]``; a single-week pin is ``weeks: [12]``.

    ``exact_block`` distinguishes a hardcoded block placement from a set of
    week-level pins. Exact blocks require one curriculum block to begin on the
    first listed week and span the complete contiguous range. The default is
    deliberately false so historical locks retain their original semantics.
    """

    resident_id: str
    rotation_id: str
    elective: bool = False
    weeks: list[int] = Field(min_length=1)
    source: LockSource = "manual"
    exact_block: bool = False
    grouping_exempt: bool = Field(
        default=False,
        description=(
            "Allows this manually placed exact block to opt one otherwise-required "
            "rotation-group instance out of adjacency."
        ),
    )

    @field_validator("resident_id", "rotation_id")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("weeks")
    @classmethod
    def valid_weeks(cls, weeks: list[int]) -> list[int]:
        if len(weeks) != len(set(weeks)):
            raise ValueError("lock weeks must be unique")
        for week in weeks:
            if week < 1 or week > 52:
                raise ValueError(f"lock week {week} is outside 1..52")
        return sorted(weeks)

    @model_validator(mode="after")
    def exact_blocks_are_contiguous(self) -> "LockedPlacement":
        if self.grouping_exempt and (
            self.source != "manual" or not self.exact_block
        ):
            raise ValueError(
                "a grouping exemption requires a manual exact-block lock"
            )
        if self.exact_block and self.weeks != list(
            range(self.weeks[0], self.weeks[-1] + 1)
        ):
            raise ValueError("exact block lock weeks must be contiguous")
        return self
