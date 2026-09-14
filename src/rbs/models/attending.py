"""Attending scheduling configured for one academic-year workspace."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from rbs.models.clinic_site import ALL_CLINIC_SITES, normalize_clinic_site_ids
from rbs.models.common import StrictModel
from rbs.models.enums import Session, Weekday

DEFAULT_ATTENDING_HALF_DAYS_PER_WEEK = 10
MAX_ATTENDING_HALF_DAYS_PER_WEEK = 14
MAX_ATTENDING_CLINIC_DAYS_PER_WEEK = 7
ATTENDING_WORK_DESCRIPTION_MAX_LENGTH = 120

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


ATTENDING_WEEKLY_TARGET_WORK_TYPES = (
    AttendingWorkType.INPATIENT_SERVICE,
    AttendingWorkType.ATTENDING_CLINIC,
    AttendingWorkType.PRECEPTING_CLINIC,
    AttendingWorkType.ADMIN_TIME,
)
ATTENDING_PREFERRED_WORK_TYPES = ATTENDING_WEEKLY_TARGET_WORK_TYPES

_WORK_TYPE_ORDER = {
    work_type: index for index, work_type in enumerate(AttendingWorkType)
}


class AttendingWeeklyTargetMode(StrEnum):
    """Whether a category range is required or preferred."""

    FIXED = "fixed"
    FLEXIBLE = "flexible"


class AttendingWeeklyShiftTarget(StrictModel):
    """One attending's required or preferred weekly range for a work category."""

    work_type: AttendingWorkType
    minimum_shifts_per_week: int = Field(
        ge=0,
        le=MAX_ATTENDING_HALF_DAYS_PER_WEEK,
    )
    maximum_shifts_per_week: int = Field(
        ge=0,
        le=MAX_ATTENDING_HALF_DAYS_PER_WEEK,
    )
    mode: AttendingWeeklyTargetMode = AttendingWeeklyTargetMode.FLEXIBLE

    @model_validator(mode="before")
    @classmethod
    def migrate_single_count(cls, value: object) -> object:
        """Treat the former single count as an exact minimum/maximum range."""
        if not isinstance(value, dict) or "shifts_per_week" not in value:
            return value
        if (
            "minimum_shifts_per_week" in value
            or "maximum_shifts_per_week" in value
        ):
            return value
        migrated = dict(value)
        shifts = migrated.pop("shifts_per_week")
        migrated["minimum_shifts_per_week"] = shifts
        migrated["maximum_shifts_per_week"] = shifts
        return migrated

    @model_validator(mode="after")
    def valid_target_range(self) -> AttendingWeeklyShiftTarget:
        if self.work_type is AttendingWorkType.SPECIAL_OTHER:
            raise ValueError(
                "Special/Other work is scheduled manually and cannot have a weekly target"
            )
        if self.maximum_shifts_per_week < self.minimum_shifts_per_week:
            raise ValueError(
                "minimum shifts per week cannot exceed maximum shifts per week"
            )
        return self


class AttendingWorkAssignment(StrictModel):
    """Shared typed assignment fields for attending work."""

    work_type: AttendingWorkType = AttendingWorkType.SPECIAL_OTHER
    clinic_id: str | None = None
    description: str | None = Field(
        default=None,
        max_length=ATTENDING_WORK_DESCRIPTION_MAX_LENGTH,
    )

    @field_validator("clinic_id")
    @classmethod
    def normalize_clinic_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_clinic_site_ids([value])[0]
        if normalized == ALL_CLINIC_SITES:
            raise ValueError("attending work must select one configured clinic")
        return normalized

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def clinic_matches_work_type(self) -> AttendingWorkAssignment:
        if self.work_type is AttendingWorkType.PRECEPTING_CLINIC:
            if self.clinic_id is None:
                raise ValueError("Precepting Clinic work must select a clinic")
        elif self.clinic_id is not None:
            raise ValueError("only Precepting Clinic work can select a clinic")
        if (
            self.work_type is not AttendingWorkType.SPECIAL_OTHER
            and self.description is not None
        ):
            raise ValueError("only Special/Other work can have a description")
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
    half_days_override: int | None = Field(
        default=None,
        ge=0,
        le=MAX_ATTENDING_HALF_DAYS_PER_WEEK,
        description="Optional half-day total used only for this academic week.",
    )
    half_days: list[AttendingWorkHalfDay] = Field(default_factory=list)

    @field_validator("half_days")
    @classmethod
    def work_is_distinct_and_ordered(
        cls,
        half_days: list[AttendingWorkHalfDay],
    ) -> list[AttendingWorkHalfDay]:
        return _normalized_weekday_half_days(half_days, label="weekly work")

    def effective_half_days(self, default: int) -> int:
        """Return this week's configured total, falling back to the attending default."""
        return self.half_days_override if self.half_days_override is not None else default


class AttendingSchedule(StrictModel):
    """Accepted week-by-week work for one attending.

    The schedule is deliberately separate from :class:`Attending`, which holds
    scheduling inputs such as dates, targets, preferences, and templates. That
    separation lets a future planner replace a schedule only after producing a
    valid result, without overwriting the configuration it planned from.
    """

    attending_id: str
    weeks: list[AttendingWeeklyWorkSchedule] = Field(default_factory=list)

    @field_validator("attending_id")
    @classmethod
    def attending_reference_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("attending schedule must reference an attending")
        return normalized

    @field_validator("weeks")
    @classmethod
    def weeks_are_distinct_and_ordered(
        cls,
        schedules: list[AttendingWeeklyWorkSchedule],
    ) -> list[AttendingWeeklyWorkSchedule]:
        weeks = [schedule.week for schedule in schedules]
        if len(weeks) != len(set(weeks)):
            raise ValueError("attending schedule must use unique academic weeks")
        return sorted(schedules, key=lambda schedule: schedule.week)

    def schedule_for_week(self, week: int) -> AttendingWeeklyWorkSchedule | None:
        """Return the independently accepted schedule for one academic week."""
        return next((schedule for schedule in self.weeks if schedule.week == week), None)


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
    work lives in independent academic-week schedules. An individual week can
    override the attending's usual half-day total.
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
            "Optional fixed or flexible weekly shift ranges for recurring work categories."
        ),
    )
    minimum_attending_clinic_days_per_week: int = Field(
        default=0,
        ge=0,
        le=MAX_ATTENDING_CLINIC_DAYS_PER_WEEK,
        description=(
            "Minimum distinct weekdays with Attending Clinic work in full active "
            "weeks without weekday vacation."
        ),
    )
    preferred_weekly_schedule_half_days: list[AttendingWorkHalfDay] = Field(
        default_factory=list,
        description=(
            "Soft preferred weekly work pattern; it never creates scheduled work."
        ),
    )
    schedule_template_half_days: list[AttendingWorkHalfDay] = Field(
        default_factory=list,
        description=(
            "Reusable typed pattern copied into selected weeks; it is not itself work."
        ),
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

    @field_validator("preferred_weekly_schedule_half_days")
    @classmethod
    def preferred_schedule_is_distinct_and_ordered(
        cls,
        half_days: list[AttendingWorkHalfDay],
    ) -> list[AttendingWorkHalfDay]:
        if any(
            half_day.work_type not in ATTENDING_PREFERRED_WORK_TYPES
            for half_day in half_days
        ):
            raise ValueError(
                "preferred weekly schedule cannot include Special/Other work"
            )
        return _normalized_weekday_half_days(half_days, label="preferred schedule")

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
        if len(self.preferred_weekly_schedule_half_days) > self.half_days_per_week:
            raise ValueError(
                "preferred schedule half-days cannot exceed the configured "
                "half-days per week"
            )
        if self.minimum_attending_clinic_days_per_week > self.half_days_per_week:
            raise ValueError(
                "minimum Attending Clinic days cannot exceed the configured "
                "half-days per week"
            )
        for target in self.weekly_shift_targets:
            if target.maximum_shifts_per_week > self.half_days_per_week:
                raise ValueError(
                    f"{target.work_type.value} maximum weekly shift target cannot exceed "
                    "the configured half-days per week"
                )
        fixed_target_minimum = sum(
            target.minimum_shifts_per_week
            for target in self.weekly_shift_targets
            if target.mode is AttendingWeeklyTargetMode.FIXED
        )
        if fixed_target_minimum > self.half_days_per_week:
            raise ValueError(
                "fixed weekly shift target minimums cannot exceed the configured "
                "half-days per week"
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


@dataclass(frozen=True, slots=True)
class EffectiveAttendingWeek:
    """One attending week's assignments after every dated rule is applied."""

    attending_id: str
    week: int
    week_start: date
    target_half_days: int
    assignments: tuple[AttendingWorkHalfDay, ...]
    available_weekdays: frozenset[Weekday]
    automatic_admin_half_day: AttendingWorkHalfDay | None
    is_active_week: bool
    is_full_schedule_week: bool
    has_vacation: bool
    attending_clinic_day_minimum: int

    @property
    def assigned_half_days(self) -> int:
        return len(self.assignments)

    def assignment_on(
        self,
        weekday: Weekday,
        session: Session,
    ) -> AttendingWorkHalfDay | None:
        return next(
            (
                assignment
                for assignment in self.assignments
                if assignment.weekday is weekday and assignment.session is session
            ),
            None,
        )

    def category_count(self, work_type: AttendingWorkType) -> int:
        return sum(
            assignment.work_type is work_type for assignment in self.assignments
        )

    @property
    def attending_clinic_days(self) -> frozenset[Weekday]:
        return frozenset(
            assignment.weekday
            for assignment in self.assignments
            if assignment.work_type is AttendingWorkType.ATTENDING_CLINIC
        )


def effective_attending_week(
    attending: Attending,
    schedule: AttendingSchedule | None,
    *,
    week: int,
    first_day: date,
    last_day: date,
    academic_half_day: tuple[Weekday, Session] | None,
    automatic_academic_admin: bool,
) -> EffectiveAttendingWeek:
    """Project stored work through dates, vacation, and the academic rule."""
    weeks = (last_day - first_day).days // 7 + 1
    if not 1 <= week <= weeks:
        raise ValueError(f"academic week must be between 1 and {weeks}")
    week_start = first_day + timedelta(weeks=week - 1)
    saved_week = schedule.schedule_for_week(week) if schedule is not None else None
    target = (
        saved_week.effective_half_days(attending.half_days_per_week)
        if saved_week is not None
        else attending.half_days_per_week
    )
    stored_by_slot = (
        {
            (assignment.weekday, assignment.session): assignment
            for assignment in saved_week.half_days
        }
        if saved_week is not None
        else {}
    )
    assignments: list[AttendingWorkHalfDay] = []
    automatic_admin: AttendingWorkHalfDay | None = None
    scheduled_days: list[bool] = []
    vacation_days: list[bool] = []
    for offset, weekday in enumerate(Weekday):
        calendar_day = week_start + timedelta(days=offset)
        scheduled = attending.is_scheduled_on(
            calendar_day,
            academic_year_start=first_day,
            academic_year_end=last_day,
        )
        vacation = attending.is_on_vacation(calendar_day)
        scheduled_days.append(scheduled)
        vacation_days.append(vacation)
        if not scheduled or vacation:
            continue
        for session in Session:
            if (
                automatic_academic_admin
                and academic_half_day == (weekday, session)
            ):
                automatic_admin = AttendingWorkHalfDay(
                    weekday=weekday,
                    session=session,
                    work_type=AttendingWorkType.ADMIN_TIME,
                )
                assignments.append(automatic_admin)
                continue
            assignment = stored_by_slot.get((weekday, session))
            if assignment is not None:
                assignments.append(assignment)

    weekday_vacation = any(vacation_days[:5])
    active_weekdays = any(scheduled_days[:5])
    full_weekdays = all(scheduled_days[:5])
    return EffectiveAttendingWeek(
        attending_id=attending.id,
        week=week,
        week_start=week_start,
        target_half_days=target,
        assignments=tuple(
            sorted(
                assignments,
                key=lambda assignment: (
                    _WEEKDAY_ORDER[assignment.weekday],
                    _SESSION_ORDER[assignment.session],
                ),
            )
        ),
        available_weekdays=frozenset(
            weekday
            for weekday, scheduled, vacation in zip(
                Weekday,
                scheduled_days,
                vacation_days,
                strict=True,
            )
            if scheduled and not vacation
        ),
        automatic_admin_half_day=automatic_admin,
        is_active_week=any(scheduled_days),
        is_full_schedule_week=full_weekdays,
        has_vacation=any(vacation_days),
        attending_clinic_day_minimum=(
            0
            if not active_weekdays or not full_weekdays or weekday_vacation
            else attending.minimum_attending_clinic_days_per_week
        ),
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
    effective_weeks: Iterable[EffectiveAttendingWeek],
    *,
    managed_clinic_ids: set[str],
    closed_dates_by_clinic: dict[str, set[date]] | None = None,
) -> list[AttendingClinicCoverage]:
    """Expand effective Precepting Clinic work into solver-facing counts."""
    counts: Counter[tuple[str, date, Session]] = Counter()
    closed_dates = closed_dates_by_clinic or {}
    no_closed_dates: set[date] = set()
    for effective in effective_weeks:
        for assignment in effective.assignments:
            if assignment.work_type is not AttendingWorkType.PRECEPTING_CLINIC:
                continue
            clinic_id = assignment.clinic_id
            assert clinic_id is not None
            calendar_day = effective.week_start + timedelta(
                days=_WEEKDAY_ORDER[assignment.weekday]
            )
            if clinic_id not in managed_clinic_ids or calendar_day in closed_dates.get(
                clinic_id,
                no_closed_dates,
            ):
                continue
            counts[clinic_id, calendar_day, assignment.session] += 1
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


def normalize_attending_schedules(
    schedules: list[AttendingSchedule],
) -> list[AttendingSchedule]:
    """Validate unique attending references and return deterministic ordering."""
    attending_ids = [schedule.attending_id for schedule in schedules]
    if len(attending_ids) != len(set(attending_ids)):
        raise ValueError("attending schedules must reference each attending at most once")
    return sorted(schedules, key=lambda schedule: schedule.attending_id)


def validate_attending_academic_year(
    attendings: list[Attending],
    *,
    attending_schedules: list[AttendingSchedule] | None = None,
    first_day: date,
    last_day: date,
) -> None:
    """Require every explicit attending date to belong to this workspace year."""
    by_id = {attending.id: attending for attending in attendings}
    weeks = (last_day - first_day).days // 7 + 1
    for schedule in attending_schedules or []:
        attending = by_id.get(schedule.attending_id)
        if attending is None:
            raise ValueError(
                f"attending schedule references unknown attending {schedule.attending_id!r}"
            )
        for weekly in schedule.weeks:
            if weekly.week > weeks:
                raise ValueError(
                    f"{attending.id}: weekly work schedule week {weekly.week} is outside "
                    f"academic year weeks 1..{weeks}"
                )
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
