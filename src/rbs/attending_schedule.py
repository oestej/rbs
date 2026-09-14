"""Attending schedule diagnostics independent of UI and solver implementations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rbs.models.attending import (
    Attending,
    AttendingWeeklyTargetMode,
    AttendingWorkType,
    EffectiveAttendingWeek,
)
from rbs.models.instance import SchedulerInput


class AttendingScheduleIssueSeverity(StrEnum):
    """Whether a schedule rule is required or preferred."""

    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class AttendingScheduleIssue:
    """One actionable mismatch in an effective attending week."""

    code: str
    severity: AttendingScheduleIssueSeverity
    attending_id: str
    week: int
    message: str
    work_type: AttendingWorkType | None = None


@dataclass(frozen=True, slots=True)
class AttendingScheduleReport:
    """Non-blocking checks for an accepted attending schedule."""

    issues: tuple[AttendingScheduleIssue, ...] = ()

    @property
    def errors(self) -> tuple[AttendingScheduleIssue, ...]:
        return tuple(
            issue
            for issue in self.issues
            if issue.severity is AttendingScheduleIssueSeverity.ERROR
        )

    @property
    def warnings(self) -> tuple[AttendingScheduleIssue, ...]:
        return tuple(
            issue
            for issue in self.issues
            if issue.severity is AttendingScheduleIssueSeverity.WARNING
        )

    def for_attending(self, attending_id: str) -> AttendingScheduleReport:
        return AttendingScheduleReport(
            tuple(
                issue for issue in self.issues if issue.attending_id == attending_id
            )
        )


_WORK_TYPE_LABELS = {
    AttendingWorkType.INPATIENT_SERVICE: "Inpatient Service",
    AttendingWorkType.ATTENDING_CLINIC: "Attending Clinic",
    AttendingWorkType.PRECEPTING_CLINIC: "Precepting Clinic",
    AttendingWorkType.ADMIN_TIME: "Admin Time",
    AttendingWorkType.SPECIAL_OTHER: "Special/Other",
}


def attending_schedule_report(
    instance: SchedulerInput,
    *,
    attending_id: str | None = None,
) -> AttendingScheduleReport:
    """Check actual weeks against totals, targets, clinic days, and preferences.

    These diagnostics intentionally do not participate in Pydantic validation:
    a workspace can be configured incrementally. Required checks are errors and
    soft preferences are warnings. Vacation and partial boundary weeks may fall
    below ordinary totals and category minimums because those dated rules remove
    any kind of work.
    """
    attendings = [
        attending
        for attending in instance.attendings
        if attending_id is None or attending.id == attending_id
    ]
    if attending_id is not None and not attendings:
        raise ValueError(f"unknown attending {attending_id!r}")

    issues: list[AttendingScheduleIssue] = []
    for attending in attendings:
        for week in range(1, instance.calendar.weeks + 1):
            effective = instance.effective_attending_week(attending, week)
            if not effective.is_active_week:
                continue
            minimums_apply = (
                effective.is_full_schedule_week and not effective.has_vacation
            )
            assigned = effective.assigned_half_days
            target = effective.target_half_days
            if assigned > target or (minimums_apply and assigned < target):
                issues.append(
                    AttendingScheduleIssue(
                        code="attending_half_day_total",
                        severity=AttendingScheduleIssueSeverity.ERROR,
                        attending_id=attending.id,
                        week=week,
                        message=(
                            f"{attending.name} · week {week}: {assigned} of {target} "
                            "half-days assigned."
                        ),
                    )
                )

            issues.extend(
                _category_target_issues(
                    attending,
                    week=week,
                    minimums_apply=minimums_apply,
                    effective=effective,
                )
            )

            clinic_days = len(effective.attending_clinic_days)
            clinic_minimum = effective.attending_clinic_day_minimum
            if clinic_days < clinic_minimum:
                issues.append(
                    AttendingScheduleIssue(
                        code="attending_clinic_day_minimum",
                        severity=AttendingScheduleIssueSeverity.ERROR,
                        attending_id=attending.id,
                        week=week,
                        work_type=AttendingWorkType.ATTENDING_CLINIC,
                        message=(
                            f"{attending.name} · week {week}: {clinic_days} of at least "
                            f"{clinic_minimum} Attending Clinic days assigned."
                        ),
                    )
                )

            preferred_misses = _preferred_misses(attending, effective)
            if preferred_misses:
                issues.append(
                    AttendingScheduleIssue(
                        code="attending_preferred_schedule",
                        severity=AttendingScheduleIssueSeverity.WARNING,
                        attending_id=attending.id,
                        week=week,
                        message=(
                            f"{attending.name} · week {week}: {preferred_misses} preferred "
                            f"{'half-day is' if preferred_misses == 1 else 'half-days are'} "
                            "not matched."
                        ),
                    )
                )

    return AttendingScheduleReport(tuple(issues))


def _category_target_issues(
    attending: Attending,
    *,
    week: int,
    minimums_apply: bool,
    effective: EffectiveAttendingWeek,
) -> list[AttendingScheduleIssue]:
    issues: list[AttendingScheduleIssue] = []
    for target in attending.weekly_shift_targets:
        count = effective.category_count(target.work_type)
        below = minimums_apply and count < target.minimum_shifts_per_week
        above = count > target.maximum_shifts_per_week
        if not below and not above:
            continue
        severity = (
            AttendingScheduleIssueSeverity.ERROR
            if target.mode is AttendingWeeklyTargetMode.FIXED
            else AttendingScheduleIssueSeverity.WARNING
        )
        label = _WORK_TYPE_LABELS[target.work_type]
        expected = (
            str(target.minimum_shifts_per_week)
            if target.minimum_shifts_per_week == target.maximum_shifts_per_week
            else (
                f"{target.minimum_shifts_per_week}–"
                f"{target.maximum_shifts_per_week}"
            )
        )
        issues.append(
            AttendingScheduleIssue(
                code=(
                    "attending_fixed_target"
                    if severity is AttendingScheduleIssueSeverity.ERROR
                    else "attending_flexible_target"
                ),
                severity=severity,
                attending_id=attending.id,
                week=week,
                work_type=target.work_type,
                message=(
                    f"{attending.name} · week {week}: {label} has {count} shifts; "
                    f"the {target.mode.value.title()} target is {expected}."
                ),
            )
        )
    return issues


def _preferred_misses(
    attending: Attending,
    effective: EffectiveAttendingWeek,
) -> int:
    misses = 0
    for preferred in attending.preferred_weekly_schedule_half_days:
        if preferred.weekday not in effective.available_weekdays:
            continue
        if effective.assignment_on(preferred.weekday, preferred.session) != preferred:
            misses += 1
    return misses
