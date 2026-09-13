"""Attending availability configured for one academic-year workspace."""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from rbs.models.clinic_site import ALL_CLINIC_SITES, normalize_clinic_site_ids
from rbs.models.common import StrictModel
from rbs.models.enums import Session, Weekday

DEFAULT_ATTENDING_HALF_DAYS_PER_WEEK = 10
MAX_ATTENDING_HALF_DAYS_PER_WEEK = 14

_SESSION_ORDER = {
    Session.MORNING: 0,
    Session.AFTERNOON: 1,
}

_WEEKDAY_ORDER = {weekday: index for index, weekday in enumerate(Weekday)}


class AttendingWorkType(StrEnum):
    """The kind of work occupying one attending half-day."""

    INPATIENT_SERVICE = "inpatient_service"
    ATTENDING_CLINIC = "attending_clinic"
    PRECEPTING_CLINIC = "precepting_clinic"
    ADMIN_TIME = "admin_time"
    SPECIAL_OTHER = "special_other"


_WORK_TYPE_ORDER = {
    work_type: index for index, work_type in enumerate(AttendingWorkType)
}


class AttendingWeeklyTargetMode(StrEnum):
    """Whether a category target is required or may vary."""

    FIXED = "fixed"
    FLEXIBLE = "flexible"


class AttendingWeeklyShiftTarget(StrictModel):
    """One attending's desired weekly count for a work category."""

    work_type: AttendingWorkType
    shifts_per_week: int = Field(
        ge=0,
        le=MAX_ATTENDING_HALF_DAYS_PER_WEEK,
    )
    mode: AttendingWeeklyTargetMode = AttendingWeeklyTargetMode.FLEXIBLE


class AttendingWorkAssignment(StrictModel):
    """Shared typed assignment fields for weekday-based and dated work."""

    work_type: AttendingWorkType = AttendingWorkType.SPECIAL_OTHER
    clinic_id: str | None = None

    @field_validator("clinic_id")
    @classmethod
    def normalize_clinic_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_clinic_site_ids([value])[0]
        if normalized == ALL_CLINIC_SITES:
            raise ValueError("attending work must select one configured clinic")
        return normalized

    @model_validator(mode="after")
    def clinic_matches_work_type(self) -> AttendingWorkAssignment:
        if self.work_type is AttendingWorkType.PRECEPTING_CLINIC:
            if self.clinic_id is None:
                raise ValueError("Precepting Clinic work must select a clinic")
        elif self.clinic_id is not None:
            raise ValueError("only Precepting Clinic work can select a clinic")
        return self


class AttendingWorkHalfDay(AttendingWorkAssignment):
    """One weekday/session assignment reusable in templates and scheduled weeks."""

    weekday: Weekday
    session: Session


def _normalized_weekday_half_days(
    half_days: list[AttendingWorkHalfDay],
    *,
    label: str,
) -> list[AttendingWorkHalfDay]:
    slots = [(half_day.weekday, half_day.session) for half_day in half_days]
    if len(slots) != len(set(slots)):
        raise ValueError(f"attending {label} half-days must be unique")
    return sorted(
        half_days,
        key=lambda half_day: (
            _WEEKDAY_ORDER[half_day.weekday],
            _SESSION_ORDER[half_day.session],
        ),
    )


class AttendingWeeklyWorkSchedule(StrictModel):
    """The complete work schedule for one one-based academic week."""

    week: int = Field(ge=1)
    half_days: list[AttendingWorkHalfDay] = Field(default_factory=list)

    @field_validator("half_days")
    @classmethod
    def work_is_distinct_and_ordered(
        cls,
        half_days: list[AttendingWorkHalfDay],
    ) -> list[AttendingWorkHalfDay]:
        return _normalized_weekday_half_days(half_days, label="weekly work")


class AttendingAdHocWorkHalfDay(AttendingWorkAssignment):
    """One dated half-day replacing the attending's scheduled-week assignment."""

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
    to be well formed and non-overlapping. Category targets are optional and do
    not create work. The reusable template is only a pattern applier: actual
    work lives in independent academic-week schedules. Dated work half-days
    explicitly replace the matching scheduled-week slot.
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
    weekly_shift_targets: list[AttendingWeeklyShiftTarget] = Field(
        default_factory=list,
        description=(
            "Optional fixed or flexible weekly shift counts for individual work categories."
        ),
    )
    schedule_template_half_days: list[AttendingWorkHalfDay] = Field(
        default_factory=list,
        description=(
            "Reusable typed pattern copied into selected weeks; it is not itself work."
        ),
    )
    weekly_work_schedules: list[AttendingWeeklyWorkSchedule] = Field(
        default_factory=list,
        description="Independent complete schedules for one-based academic weeks.",
    )
    ad_hoc_work_half_days: list[AttendingAdHocWorkHalfDay] = Field(
        default_factory=list,
        description="Explicit dated AM or PM assignments replacing the scheduled-week slot.",
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

    @field_validator("weekly_shift_targets")
    @classmethod
    def weekly_shift_targets_are_distinct_and_ordered(
        cls,
        targets: list[AttendingWeeklyShiftTarget],
    ) -> list[AttendingWeeklyShiftTarget]:
        work_types = [target.work_type for target in targets]
        if len(work_types) != len(set(work_types)):
            raise ValueError("attending weekly shift targets must use unique categories")
        return sorted(targets, key=lambda target: _WORK_TYPE_ORDER[target.work_type])

    @field_validator("schedule_template_half_days")
    @classmethod
    def schedule_template_is_distinct_and_ordered(
        cls,
        half_days: list[AttendingWorkHalfDay],
    ) -> list[AttendingWorkHalfDay]:
        return _normalized_weekday_half_days(half_days, label="schedule template")

    @field_validator("weekly_work_schedules")
    @classmethod
    def weekly_work_is_distinct_and_ordered(
        cls,
        schedules: list[AttendingWeeklyWorkSchedule],
    ) -> list[AttendingWeeklyWorkSchedule]:
        weeks = [schedule.week for schedule in schedules]
        if len(weeks) != len(set(weeks)):
            raise ValueError("attending weekly work schedules must use unique weeks")
        return sorted(schedules, key=lambda schedule: schedule.week)

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
        if len(self.schedule_template_half_days) > self.half_days_per_week:
            raise ValueError(
                "schedule template half-days cannot exceed the configured half-days per week"
            )
        for target in self.weekly_shift_targets:
            if target.shifts_per_week > self.half_days_per_week:
                raise ValueError(
                    f"{target.work_type.value} weekly shift target cannot exceed the "
                    "configured half-days per week"
                )
        fixed_target_total = sum(
            target.shifts_per_week
            for target in self.weekly_shift_targets
            if target.mode is AttendingWeeklyTargetMode.FIXED
        )
        if fixed_target_total > self.half_days_per_week:
            raise ValueError(
                "fixed weekly shift targets cannot exceed the configured half-days per week"
            )
        for schedule in self.weekly_work_schedules:
            if len(schedule.half_days) > self.half_days_per_week:
                raise ValueError(
                    f"week {schedule.week} work half-days cannot exceed the configured "
                    "half-days per week"
                )
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

    def weekly_shift_target_for(
        self,
        work_type: AttendingWorkType,
    ) -> AttendingWeeklyShiftTarget | None:
        """Return the configured target for one category, if it has one."""
        return next(
            (
                target
                for target in self.weekly_shift_targets
                if target.work_type is work_type
            ),
            None,
        )

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

    def work_schedule_for_week(self, week: int) -> AttendingWeeklyWorkSchedule | None:
        """Return the independently saved schedule for one academic week."""
        return next(
            (schedule for schedule in self.weekly_work_schedules if schedule.week == week),
            None,
        )

    def work_half_day_on(
        self,
        calendar_day: date,
        session: Session,
        *,
        academic_year_start: date,
        academic_year_end: date,
    ) -> AttendingWorkHalfDay | AttendingAdHocWorkHalfDay | None:
        """Resolve the effective assignment for an exact dated half-day.

        A dated assignment replaces the scheduled-week slot. The reusable
        template is never consulted here. Schedule boundaries and vacation
        suppress both assignment shapes.
        """
        if not self.is_scheduled_on(
            calendar_day,
            academic_year_start=academic_year_start,
            academic_year_end=academic_year_end,
        ) or self.is_on_vacation(calendar_day):
            return None
        dated = next(
            (
                half_day
                for half_day in self.ad_hoc_work_half_days
                if half_day.date == calendar_day and half_day.session is session
            ),
            None,
        )
        if dated is not None:
            return dated
        week = (calendar_day - academic_year_start).days // 7 + 1
        schedule = self.work_schedule_for_week(week)
        if schedule is None:
            return None
        weekday = tuple(Weekday)[calendar_day.weekday()]
        return next(
            (
                half_day
                for half_day in schedule.half_days
                if half_day.weekday is weekday and half_day.session is session
            ),
            None,
        )


class AttendingClinicCoverage(StrictModel):
    """Dated attending count for one attending-managed clinic half-day."""

    clinic_id: str
    date: date
    session: Session
    attendings: int = Field(ge=1)

    @field_validator("clinic_id")
    @classmethod
    def normalize_clinic_reference(cls, value: str) -> str:
        normalized = normalize_clinic_site_ids([value])[0]
        if normalized == ALL_CLINIC_SITES:
            raise ValueError("attending coverage must select one clinic")
        return normalized


def attending_clinic_coverage(
    attendings: list[Attending],
    *,
    managed_clinic_ids: set[str],
    closed_dates_by_clinic: dict[str, set[date]] | None = None,
    first_day: date,
    last_day: date,
) -> list[AttendingClinicCoverage]:
    """Expand effective Precepting Clinic work into solver-facing counts."""
    counts: Counter[tuple[str, date, Session]] = Counter()
    closed_dates = closed_dates_by_clinic or {}
    no_closed_dates: set[date] = set()
    calendar_day = first_day
    while calendar_day <= last_day:
        for attending in attendings:
            for session in Session:
                assignment = attending.work_half_day_on(
                    calendar_day,
                    session,
                    academic_year_start=first_day,
                    academic_year_end=last_day,
                )
                if (
                    assignment is None
                    or assignment.work_type is not AttendingWorkType.PRECEPTING_CLINIC
                ):
                    continue
                clinic_id = assignment.clinic_id
                assert clinic_id is not None
                if clinic_id not in managed_clinic_ids or calendar_day in closed_dates.get(
                    clinic_id,
                    no_closed_dates,
                ):
                    continue
                counts[clinic_id, calendar_day, session] += 1
        calendar_day += timedelta(days=1)
    return [
        AttendingClinicCoverage(
            clinic_id=clinic_id,
            date=calendar_day,
            session=session,
            attendings=count,
        )
        for (clinic_id, calendar_day, session), count in sorted(
            counts.items(),
            key=lambda item: (
                item[0][1],
                _SESSION_ORDER[item[0][2]],
                item[0][0],
            ),
        )
    ]


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
        weeks = (last_day - first_day).days // 7 + 1
        for schedule in attending.weekly_work_schedules:
            if schedule.week > weeks:
                raise ValueError(
                    f"{attending.id}: weekly work schedule week {schedule.week} is outside "
                    f"academic year weeks 1..{weeks}"
                )
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
