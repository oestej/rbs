"""Attending schedule diagnostics independent of UI and solver implementations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rbs.models.attending import (
    ATTENDING_WORK_TYPE_LABELS,
    Attending,
    AttendingWeeklyTargetMode,
    AttendingWorkType,
    EffectiveAttendingWeek,
)
from rbs.models.clinic import clinic_slot_date
from rbs.models.enums import Session
from rbs.models.instance import SchedulerInput, SolverProblem
from rbs.models.schedule import AssignedAttendingWork, Schedule


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
            issue for issue in self.issues if issue.severity is AttendingScheduleIssueSeverity.ERROR
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
            tuple(issue for issue in self.issues if issue.attending_id == attending_id)
        )


ScheduledWorkByWeek = dict[tuple[str, int], tuple[AssignedAttendingWork, ...]]
"""A schedule's attending work grouped by attending and academic week."""


def effective_attending_schedule_week(
    instance: SchedulerInput,
    attending: Attending,
    week: int,
    schedule: Schedule | None = None,
    *,
    work_by_week: ScheduledWorkByWeek | None = None,
) -> EffectiveAttendingWeek:
    """Combine accepted case work and compatible generated work for a calendar week.

    ``work_by_week`` is ``instance.scheduled_attending_work(schedule)``. A
    caller projecting many weeks groups the schedule once and passes it.
    """
    grouped = (
        work_by_week if work_by_week is not None else instance.scheduled_attending_work(schedule)
    )
    return instance.effective_attending_week(
        attending,
        week,
        scheduled_work=grouped.get((attending.id, week), ()),
    )


def preserved_attending_work_issues(
    instance: SolverProblem,
    schedule: Schedule | None,
    *,
    attending_id: str | None = None,
) -> tuple[AttendingScheduleIssue, ...]:
    """Explain temporarily hidden protected work without parsing solver notes."""
    people = {person.id: person for person in instance.attendings}
    issues = []
    for (person_id, week), work in instance.scheduled_attending_work(schedule).items():
        if attending_id is not None and person_id != attending_id:
            continue
        person = people[person_id]
        effective = instance.effective_attending_week(person, week)
        for item in work:
            if not item.is_protected_input:
                continue
            if (
                item.clinic_id is not None
                and item.clinic_id not in instance.attending_managed_clinic_ids
            ):
                continue
            day = clinic_slot_date(instance.calendar.first_week_start, week, item.weekday)
            if person.is_on_vacation(day):
                reason = "vacation"
            elif item.weekday not in effective.available_weekdays:
                reason = "schedule dates"
            elif effective.assignment_on(item.weekday, item.session) is not None:
                reason = "other hand-entered work or the academic half-day's Admin Time"
            else:
                continue
            label = ATTENDING_WORK_TYPE_LABELS[item.work_type]
            if item.clinic_id is not None:
                label += f" at {instance.clinic_policy.site_name(item.clinic_id)}"
            period = "AM" if item.session is Session.MORNING else "PM"
            lock = "Locked" if item.locked else "Hand-entered"
            issues.append(
                AttendingScheduleIssue(
                    code="attending_work_preserved",
                    severity=AttendingScheduleIssueSeverity.WARNING,
                    attending_id=person_id,
                    week=week,
                    work_type=item.work_type,
                    message=f"{person.name} · {day:%b %d, %Y} {period}: {lock} {label} "
                    f"is hidden because of {reason}. The work and its lock state are preserved "
                    "and will return when the conflict is removed.",
                )
            )
    return tuple(issues)


def attending_schedule_report(
    instance: SchedulerInput,
    *,
    attending_id: str | None = None,
    schedule: Schedule | None = None,
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

    issues: list[AttendingScheduleIssue] = list(
        preserved_attending_work_issues(instance, schedule, attending_id=attending_id)
    )
    work_by_week = instance.scheduled_attending_work(schedule)
    for attending in attendings:
        for label, pattern in (
            ("Preferred weekly schedule", attending.preferred_weekly_schedule_half_days),
            ("Schedule template", attending.schedule_template_half_days),
        ):
            ineligible = sorted(
                {
                    item.clinic_id
                    for item in pattern
                    if item.work_type is AttendingWorkType.PRECEPTING_CLINIC
                    and item.clinic_id not in attending.eligible_clinic_ids
                }
            )
            for clinic_id in ineligible:
                capacity_managed = clinic_id not in instance.attending_managed_clinic_ids
                guidance = (
                    "This clinic is capacity-managed. Remove precepting from the pattern "
                    "or switch the clinic to attending-managed."
                    if capacity_managed
                    else "Update eligible clinics or change the pattern. "
                    "Preferences cannot override eligibility; "
                    "applied templates enter work by hand."
                )
                issues.append(
                    AttendingScheduleIssue(
                        code="attending_pattern_eligibility",
                        severity=AttendingScheduleIssueSeverity.WARNING,
                        attending_id=attending.id,
                        week=1,
                        message=f"{attending.name}: {label} includes "
                        f"{instance.clinic_policy.site_name(clinic_id)}, which is not eligible. "
                        + guidance,
                    )
                )
        for week in range(1, instance.calendar.weeks + 1):
            effective = effective_attending_schedule_week(
                instance,
                attending,
                week,
                work_by_week=work_by_week,
            )
            if not effective.is_active_week:
                continue
            for assignment in effective.assignments:
                if (
                    assignment.work_type is AttendingWorkType.PRECEPTING_CLINIC
                    and assignment.clinic_id not in attending.eligible_clinic_ids
                ):
                    capacity_managed = (
                        assignment.clinic_id not in instance.attending_managed_clinic_ids
                    )
                    guidance = (
                        "is at a capacity-managed clinic. Remove the precepting work "
                        "or switch the clinic to attending-managed."
                        if capacity_managed
                        else "overrides clinic eligibility. Select this clinic as eligible "
                        "or change the work."
                    )
                    issues.append(
                        AttendingScheduleIssue(
                            code="attending_clinic_staffing"
                            if capacity_managed
                            else "attending_clinic_eligibility",
                            severity=AttendingScheduleIssueSeverity.WARNING,
                            attending_id=attending.id,
                            week=week,
                            work_type=assignment.work_type,
                            message=f"{attending.name} · week {week}: Precepting Clinic at "
                            f"{instance.clinic_policy.site_name(assignment.clinic_id)} " + guidance,
                        )
                    )
            minimums_apply = effective.is_full_schedule_week and not effective.has_vacation
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
        label = ATTENDING_WORK_TYPE_LABELS[target.work_type]
        expected = (
            str(target.minimum_shifts_per_week)
            if target.minimum_shifts_per_week == target.maximum_shifts_per_week
            else (f"{target.minimum_shifts_per_week}–{target.maximum_shifts_per_week}")
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
