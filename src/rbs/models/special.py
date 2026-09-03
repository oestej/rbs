"""Dated resident activities configured from the Rotations: Special workspace."""

from __future__ import annotations

from datetime import date, timedelta
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from rbs.models.common import StrictModel
from rbs.models.enums import Session


class SpecialRotationKind(StrEnum):
    """How a dated Special rotation appears in the scheduling workspace."""

    CONFERENCE = "conference"
    EVENT = "event"


class SpecialRotation(StrictModel):
    """One dated conference or clinic-calendar event with assigned residents.

    Conferences may span one or more whole days. Events occupy either one
    half-day or the full day, represented by ``session=None``.
    """

    id: str
    name: str = Field(max_length=120)
    kind: SpecialRotationKind
    start_date: date
    end_date: date
    session: Session | None = None
    resident_ids: list[str] = Field(min_length=1)

    @field_validator("id", "name")
    @classmethod
    def not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("resident_ids")
    @classmethod
    def unique_residents(cls, resident_ids: list[str]) -> list[str]:
        normalized = [resident_id.strip() for resident_id in resident_ids]
        if any(not resident_id for resident_id in normalized):
            raise ValueError("resident IDs cannot be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("resident IDs must be unique")
        return normalized

    @model_validator(mode="after")
    def valid_period(self) -> SpecialRotation:
        if self.end_date < self.start_date:
            raise ValueError("end date cannot be before start date")
        if self.kind is SpecialRotationKind.CONFERENCE:
            if self.session is not None:
                raise ValueError("Conference/Multi-Day rotations must block full days")
        elif self.end_date != self.start_date:
            raise ValueError("Events must occur on a single date")
        return self

    def dates(self) -> tuple[date, ...]:
        """Return every inclusive calendar date occupied by this activity."""
        return tuple(
            self.start_date + timedelta(days=offset)
            for offset in range((self.end_date - self.start_date).days + 1)
        )

    def blocks(self, calendar_day: date, session: Session | None = None) -> bool:
        """Whether this activity blocks ``calendar_day`` and optional half-day."""
        if not self.start_date <= calendar_day <= self.end_date:
            return False
        if self.kind is SpecialRotationKind.CONFERENCE or self.session is None:
            return True
        return session is None or session is self.session


__all__ = ["SpecialRotation", "SpecialRotationKind"]
