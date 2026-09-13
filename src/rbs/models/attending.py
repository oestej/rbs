"""Attending availability configured for one academic-year workspace."""

from __future__ import annotations

from datetime import date

from pydantic import Field, field_validator, model_validator

from rbs.models.common import StrictModel
from rbs.models.enums import Session

DEFAULT_ATTENDING_HALF_DAYS_PER_WEEK = 10
MAX_ATTENDING_HALF_DAYS_PER_WEEK = 14

_SESSION_ORDER = {
    Session.MORNING: 0,
    Session.AFTERNOON: 1,
}


class AttendingAdHocWorkHalfDay(StrictModel):
    """One dated half-day an attending is explicitly scheduled to work."""

    date: date
    session: Session


class AttendingVacation(StrictModel):
    """One inclusive attending vacation range with day-level precision."""

    start_date: date
    end_date: date

    @model_validator(mode="after")
    def dates_are_ordered(self) -> AttendingVacation:
        if self.end_date < self.start_date:
            raise ValueError("vacation end date cannot be before its start date")
        return self

    @property
    def days(self) -> int:
        """Inclusive number of calendar days in this vacation range."""
        return (self.end_date - self.start_date).days + 1

    @property
    def weekdays(self) -> int:
        """Inclusive number of Monday-through-Friday days in this range."""
        full_weeks, remaining_days = divmod(self.days, 7)
        return full_weeks * 5 + sum(
            1
            for offset in range(remaining_days)
            if (self.start_date.weekday() + offset) % 7 < 5
        )

    def includes(self, calendar_day: date) -> bool:
        return self.start_date <= calendar_day <= self.end_date


class Attending(StrictModel):
    """One attending's academic-year schedule span, work, and vacations.

    Missing schedule boundaries mean the first and last dates of the workspace
    academic year. Vacation ranges are intentionally uncapped; they only need
    to be well formed and non-overlapping. Ad hoc work half-days are explicit,
    additive commitments outside the recurring weekly total.
    """

    id: str
    name: str
    half_days_per_week: int = Field(
        default=DEFAULT_ATTENDING_HALF_DAYS_PER_WEEK,
        ge=0,
        le=MAX_ATTENDING_HALF_DAYS_PER_WEEK,
        description="Configured number of scheduled half-days in each week.",
    )
    schedule_start_date: date | None = Field(
        default=None,
        description="Optional first scheduled date; missing uses the academic-year start.",
    )
    schedule_end_date: date | None = Field(
        default=None,
        description="Optional last scheduled date; missing uses the academic-year end.",
    )
    vacation_ranges: list[AttendingVacation] = Field(
        default_factory=list,
        description="Uncapped inclusive vacation ranges with day-level precision.",
    )
    ad_hoc_work_half_days: list[AttendingAdHocWorkHalfDay] = Field(
        default_factory=list,
        description="Explicit dated AM or PM work commitments added to the weekly total.",
    )

    @field_validator("id", "name")
    @classmethod
    def not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("vacation_ranges")
    @classmethod
    def vacations_are_distinct_and_ordered(
        cls,
        vacations: list[AttendingVacation],
    ) -> list[AttendingVacation]:
        ordered = sorted(
            vacations,
            key=lambda vacation: (vacation.start_date, vacation.end_date),
        )
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current.start_date <= previous.end_date:
                raise ValueError(
                    "attending vacation ranges cannot overlap: "
                    f"{previous.start_date.isoformat()}..{previous.end_date.isoformat()} "
                    "and "
                    f"{current.start_date.isoformat()}..{current.end_date.isoformat()}"
                )
        return ordered

    @field_validator("ad_hoc_work_half_days")
    @classmethod
    def ad_hoc_work_is_distinct_and_ordered(
        cls,
        half_days: list[AttendingAdHocWorkHalfDay],
    ) -> list[AttendingAdHocWorkHalfDay]:
        slots = [(half_day.date, half_day.session) for half_day in half_days]
        if len(slots) != len(set(slots)):
            raise ValueError("attending ad hoc work half-days must be unique")
        return sorted(
            half_days,
            key=lambda half_day: (
                half_day.date,
                _SESSION_ORDER[half_day.session],
            ),
        )

    @model_validator(mode="after")
    def schedule_dates_are_ordered(self) -> Attending:
        if (
            self.schedule_start_date is not None
            and self.schedule_end_date is not None
            and self.schedule_end_date < self.schedule_start_date
        ):
            raise ValueError("schedule end date cannot be before schedule start date")
        for half_day in self.ad_hoc_work_half_days:
            slot = f"{half_day.date.isoformat()} {half_day.session.value}"
            if (
                self.schedule_start_date is not None
                and half_day.date < self.schedule_start_date
            ):
                raise ValueError(
                    f"ad hoc work half-day {slot} cannot be before the schedule start date"
                )
            if (
                self.schedule_end_date is not None
                and half_day.date > self.schedule_end_date
            ):
                raise ValueError(
                    f"ad hoc work half-day {slot} cannot be after the schedule end date"
                )
            conflicting_vacation = next(
                (
                    vacation
                    for vacation in self.vacation_ranges
                    if vacation.includes(half_day.date)
                ),
                None,
            )
            if conflicting_vacation is not None:
                raise ValueError(
                    f"ad hoc work half-day {slot} conflicts with vacation "
                    f"{conflicting_vacation.start_date.isoformat()}.."
                    f"{conflicting_vacation.end_date.isoformat()}"
                )
        return self

    def effective_schedule_start(self, academic_year_start: date) -> date:
        return self.schedule_start_date or academic_year_start

    def effective_schedule_end(self, academic_year_end: date) -> date:
        return self.schedule_end_date or academic_year_end

    def is_scheduled_on(
        self,
        calendar_day: date,
        *,
        academic_year_start: date,
        academic_year_end: date,
    ) -> bool:
        return (
            self.effective_schedule_start(academic_year_start)
            <= calendar_day
            <= self.effective_schedule_end(academic_year_end)
        )

    def is_on_vacation(self, calendar_day: date) -> bool:
        return any(vacation.includes(calendar_day) for vacation in self.vacation_ranges)

    def has_ad_hoc_work(self, calendar_day: date, session: Session) -> bool:
        """Whether an exact dated half-day is an explicit work commitment."""
        return any(
            half_day.date == calendar_day and half_day.session is session
            for half_day in self.ad_hoc_work_half_days
        )


def normalize_attendings(attendings: list[Attending]) -> list[Attending]:
    """Validate stable identities and return deterministic display order."""
    ids = [attending.id for attending in attendings]
    if len(ids) != len(set(ids)):
        raise ValueError("attending IDs must be unique")
    return sorted(attendings, key=attending_display_sort_key)


def validate_attending_academic_year(
    attendings: list[Attending],
    *,
    first_day: date,
    last_day: date,
) -> None:
    """Require every explicit attending date to belong to this workspace year."""
    for attending in attendings:
        for label, calendar_day in (
            ("schedule start date", attending.schedule_start_date),
            ("schedule end date", attending.schedule_end_date),
        ):
            if calendar_day is not None and not first_day <= calendar_day <= last_day:
                raise ValueError(
                    f"{attending.id}: {label} {calendar_day.isoformat()} is outside "
                    f"academic year {first_day.isoformat()}..{last_day.isoformat()}"
                )
        for vacation in attending.vacation_ranges:
            if vacation.start_date < first_day or vacation.end_date > last_day:
                raise ValueError(
                    f"{attending.id}: vacation {vacation.start_date.isoformat()}.."
                    f"{vacation.end_date.isoformat()} is outside academic year "
                    f"{first_day.isoformat()}..{last_day.isoformat()}"
                )
        for half_day in attending.ad_hoc_work_half_days:
            if not first_day <= half_day.date <= last_day:
                raise ValueError(
                    f"{attending.id}: ad hoc work half-day "
                    f"{half_day.date.isoformat()} {half_day.session.value} is outside "
                    f"academic year {first_day.isoformat()}..{last_day.isoformat()}"
                )


def attending_display_sort_key(attending: Attending) -> tuple[str, str, str, str]:
    """Sort display names by their final token, matching the resident directory."""
    name_parts = attending.name.split()
    first_name = name_parts[0]
    last_name = name_parts[-1] if len(name_parts) > 1 else first_name
    return (
        last_name.casefold(),
        first_name.casefold(),
        attending.name.casefold(),
        attending.id.casefold(),
    )
