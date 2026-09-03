from datetime import date, timedelta

from pydantic import Field, field_validator

from rbs.models.clinic import normalize_clinic_site_ids
from rbs.models.common import StrictModel
from rbs.models.enums import Session, Weekday

_WEEKDAY_OFFSETS = {
    Weekday.MONDAY: 0,
    Weekday.TUESDAY: 1,
    Weekday.WEDNESDAY: 2,
    Weekday.THURSDAY: 3,
    Weekday.FRIDAY: 4,
    Weekday.SATURDAY: 5,
    Weekday.SUNDAY: 6,
}


class ResidentClinicHalfDay(StrictModel):
    """A resident's recurring continuity-clinic half-day."""

    weekday: Weekday
    session: Session
    sites: list[str] = Field(
        default_factory=list,
        description="Allowed clinic sites; empty uses the workspace clinic directory.",
    )

    @field_validator("sites")
    @classmethod
    def unique_sites(cls, sites: list[str]) -> list[str]:
        return normalize_clinic_site_ids(sites)


class ElectivePreferenceRequest(StrictModel):
    """One ranked request for a service and Elective block shape.

    Repeating the same request asks for that service more than once. The list
    order on :class:`Resident` is therefore significant and duplicates are
    intentionally preserved.
    """

    rotation_id: str
    duration_weeks: int = Field(ge=1, le=5)

    @field_validator("rotation_id")
    @classmethod
    def normalize_rotation_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("elective preference rotation ID cannot be empty")
        return normalized


class Resident(StrictModel):
    id: str
    name: str
    pgy: int = Field(
        ge=1,
        description="Stable key of the resident's configured training level.",
    )
    vacation_weeks: list[int] = Field(default_factory=list)
    days_off: list[date] = Field(
        default_factory=list,
        description="Individual full days off in addition to whole vacation weeks.",
    )
    clinic_half_days: list[ResidentClinicHalfDay] = Field(
        default_factory=list,
        description=(
            "Recurring continuity-clinic sessions added whenever the resident is not "
            "on an Away rotation."
        ),
    )
    elective_preferences: list[ElectivePreferenceRequest] = Field(
        default_factory=list,
        description=(
            "Stack-ranked requests for direct Elective curriculum blocks. "
            "Order and duplicate requests are meaningful."
        ),
    )

    @field_validator("id", "name")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("vacation_weeks")
    @classmethod
    def valid_weeks(cls, weeks: list[int]) -> list[int]:
        if len(weeks) != len(set(weeks)):
            raise ValueError("vacation_weeks must be unique")
        for week in weeks:
            if week < 1 or week > 52:
                raise ValueError(f"vacation week {week} is outside 1..52")
        return sorted(weeks)

    @field_validator("days_off")
    @classmethod
    def valid_days_off(cls, days: list[date]) -> list[date]:
        if len(days) != len(set(days)):
            raise ValueError("days_off must be unique")
        return sorted(days)

    @field_validator("clinic_half_days")
    @classmethod
    def unique_clinic_half_days(
        cls,
        half_days: list[ResidentClinicHalfDay],
    ) -> list[ResidentClinicHalfDay]:
        keys = [(item.weekday, item.session) for item in half_days]
        if len(keys) != len(set(keys)):
            raise ValueError("resident clinic half-days must be unique")
        return sorted(
            half_days,
            key=lambda item: (
                _WEEKDAY_OFFSETS[item.weekday],
                list(Session).index(item.session),
            ),
        )

    def is_day_off(
        self,
        first_week_start: date,
        week: int,
        weekday: Weekday,
    ) -> bool:
        """Return whether a weekly schedule slot falls on an individual day off."""
        scheduled_day = first_week_start + timedelta(
            weeks=week - 1,
            days=_WEEKDAY_OFFSETS[weekday],
        )
        return scheduled_day in self.days_off
