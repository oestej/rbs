"""Academic calendar value object."""

from __future__ import annotations

from datetime import date

from pydantic import Field, field_validator

from rbs.models.common import StrictModel


class Calendar(StrictModel):
    weeks: int = Field(default=52, ge=1)
    first_week_start: date = Field(
        description="Monday that starts week 1 (week of July 1; weeks always start Monday).",
    )
    block_start_alignment: int = Field(
        default=1,
        ge=1,
        description="Block start weeks must satisfy (week - 1) % alignment == 0. "
        "1 means any week; smallest unit is one week.",
    )

    @field_validator("weeks")
    @classmethod
    def typical_year(cls, weeks: int) -> int:
        if weeks != 52:
            raise ValueError("this scheduler currently models a 52-week academic year")
        return weeks

    @field_validator("first_week_start")
    @classmethod
    def monday_start(cls, value: date) -> date:
        if value.weekday() != 0:
            raise ValueError("weeks always start on Monday")
        return value
