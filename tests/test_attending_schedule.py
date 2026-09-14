from datetime import timedelta

import pytest
from pydantic import ValidationError

from rbs.attending_schedule import (
    AttendingScheduleIssueSeverity,
    attending_schedule_report,
)
from rbs.catalog import blank_instance
from rbs.models.attending import (
    Attending,
    AttendingSchedule,
    AttendingVacation,
    AttendingWeeklyShiftTarget,
    AttendingWeeklyTargetMode,
    AttendingWeeklyWorkSchedule,
    AttendingWorkHalfDay,
    AttendingWorkType,
)
from rbs.models.enums import Session, Weekday
from rbs.models.instance import SchedulerInput, SolverProblem


def _work(
    weekday: Weekday,
    session: Session,
    work_type: AttendingWorkType,
    *,
    clinic_id: str | None = None,
) -> AttendingWorkHalfDay:
    return AttendingWorkHalfDay(
        weekday=weekday,
        session=session,
        work_type=work_type,
        clinic_id=clinic_id,
    )


def test_schedule_report_distinguishes_required_and_preferred_rules() -> None:
    instance = blank_instance()
    first_day = instance.calendar.first_week_start
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=4,
        schedule_start_date=first_day,
        schedule_end_date=first_day + timedelta(days=4),
        minimum_attending_clinic_days_per_week=2,
        weekly_shift_targets=[
            AttendingWeeklyShiftTarget(
                work_type=AttendingWorkType.INPATIENT_SERVICE,
                minimum_shifts_per_week=2,
                maximum_shifts_per_week=2,
                mode=AttendingWeeklyTargetMode.FIXED,
            ),
            AttendingWeeklyShiftTarget(
                work_type=AttendingWorkType.ADMIN_TIME,
                minimum_shifts_per_week=3,
                maximum_shifts_per_week=3,
                mode=AttendingWeeklyTargetMode.FLEXIBLE,
            ),
        ],
        preferred_weekly_schedule_half_days=[
            _work(
                Weekday.MONDAY,
                Session.AFTERNOON,
                AttendingWorkType.ATTENDING_CLINIC,
            )
        ],
    )
    schedule = AttendingSchedule(
        attending_id=attending.id,
        weeks=[
            AttendingWeeklyWorkSchedule(
                week=1,
                half_days=[
                    _work(
                        Weekday.MONDAY,
                        Session.MORNING,
                        AttendingWorkType.INPATIENT_SERVICE,
                    ),
                    _work(
                        Weekday.TUESDAY,
                        Session.MORNING,
                        AttendingWorkType.ATTENDING_CLINIC,
                    ),
                    _work(
                        Weekday.THURSDAY,
                        Session.MORNING,
                        AttendingWorkType.ADMIN_TIME,
                    ),
                ],
            )
        ],
    )
    configured = instance.revised(
        attendings=[attending],
        attending_schedules=[schedule],
    )

    report = attending_schedule_report(configured)

    assert {issue.code for issue in report.issues} == {
        "attending_fixed_target",
        "attending_flexible_target",
        "attending_clinic_day_minimum",
        "attending_preferred_schedule",
    }
    assert {issue.code for issue in report.errors} == {
        "attending_fixed_target",
        "attending_clinic_day_minimum",
    }
    assert {issue.code for issue in report.warnings} == {
        "attending_flexible_target",
        "attending_preferred_schedule",
    }
    assert all(issue.week == 1 for issue in report.issues)


def test_vacation_removes_all_work_and_exempts_weekly_minimums() -> None:
    instance = blank_instance()
    first_day = instance.calendar.first_week_start
    tuesday = first_day + timedelta(days=1)
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=3,
        schedule_start_date=first_day,
        schedule_end_date=first_day + timedelta(days=4),
        vacation_ranges=[AttendingVacation(start_date=tuesday, end_date=tuesday)],
        minimum_attending_clinic_days_per_week=3,
        weekly_shift_targets=[
            AttendingWeeklyShiftTarget(
                work_type=AttendingWorkType.INPATIENT_SERVICE,
                minimum_shifts_per_week=2,
                maximum_shifts_per_week=3,
                mode=AttendingWeeklyTargetMode.FIXED,
            )
        ],
        preferred_weekly_schedule_half_days=[
            _work(
                Weekday.TUESDAY,
                Session.MORNING,
                AttendingWorkType.ATTENDING_CLINIC,
            )
        ],
    )
    schedule = AttendingSchedule(
        attending_id=attending.id,
        weeks=[
            AttendingWeeklyWorkSchedule(
                week=1,
                half_days=[
                    _work(
                        Weekday.MONDAY,
                        Session.MORNING,
                        AttendingWorkType.ADMIN_TIME,
                    ),
                    _work(
                        Weekday.TUESDAY,
                        Session.MORNING,
                        AttendingWorkType.ATTENDING_CLINIC,
                    ),
                    _work(
                        Weekday.WEDNESDAY,
                        Session.AFTERNOON,
                        AttendingWorkType.INPATIENT_SERVICE,
                    ),
                    _work(
                        Weekday.THURSDAY,
                        Session.MORNING,
                        AttendingWorkType.ADMIN_TIME,
                    ),
                ],
            )
        ],
    )
    configured = instance.revised(
        attendings=[attending],
        attending_schedules=[schedule],
    )

    effective = configured.effective_attending_week(attending, 1)

    assert effective.assignment_on(Weekday.TUESDAY, Session.MORNING) is None
    assert (
        effective.assignment_on(Weekday.WEDNESDAY, Session.AFTERNOON).work_type
        is AttendingWorkType.ADMIN_TIME
    )
    assert effective.attending_clinic_day_minimum == 0
    assert effective.assigned_half_days == 3
    assert attending_schedule_report(configured).issues == ()


def test_partial_boundary_week_can_fall_below_the_weekly_total() -> None:
    instance = blank_instance()
    first_day = instance.calendar.first_week_start
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=2,
        schedule_start_date=first_day + timedelta(days=2),
        schedule_end_date=first_day + timedelta(days=4),
        minimum_attending_clinic_days_per_week=2,
    )
    configured = instance.revised(attendings=[attending])

    effective = configured.effective_attending_week(attending, 1)

    assert effective.is_active_week
    assert not effective.is_full_schedule_week
    assert effective.assigned_half_days == 1
    assert effective.attending_clinic_day_minimum == 0
    assert attending_schedule_report(configured).issues == ()


def test_effective_coverage_is_the_only_attending_data_sent_to_solver() -> None:
    instance = blank_instance()
    first_day = instance.calendar.first_week_start
    clinic_id = instance.clinic_policy.primary_site_id
    raw = instance.model_dump(mode="json")
    site = next(item for item in raw["clinic_policy"]["sites"] if item["id"] == clinic_id)
    site["staffing_mode"] = "attending_managed"
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=3,
        schedule_start_date=first_day,
        schedule_end_date=first_day + timedelta(days=4),
        vacation_ranges=[
            AttendingVacation(start_date=first_day, end_date=first_day)
        ],
    )
    schedule = AttendingSchedule(
        attending_id=attending.id,
        weeks=[
            AttendingWeeklyWorkSchedule(
                week=1,
                half_days=[
                    _work(
                        Weekday.MONDAY,
                        Session.MORNING,
                        AttendingWorkType.PRECEPTING_CLINIC,
                        clinic_id=clinic_id,
                    ),
                    _work(
                        Weekday.TUESDAY,
                        Session.MORNING,
                        AttendingWorkType.PRECEPTING_CLINIC,
                        clinic_id=clinic_id,
                    ),
                    _work(
                        Weekday.WEDNESDAY,
                        Session.AFTERNOON,
                        AttendingWorkType.PRECEPTING_CLINIC,
                        clinic_id=clinic_id,
                    ),
                ],
            )
        ],
    )
    raw["attendings"] = [attending.model_dump(mode="json")]
    raw["attending_schedules"] = [schedule.model_dump(mode="json")]
    configured = SchedulerInput.model_validate(raw)

    assert len(configured.attending_coverage) == 1
    coverage = configured.attending_coverage[0]
    assert coverage.date == first_day + timedelta(days=1)
    assert coverage.session is Session.MORNING
    assert coverage.attendings == 1

    projected = SolverProblem.from_instance(configured)
    payload = projected.model_dump(mode="json")
    assert "attendings" not in payload
    assert "attending_schedules" not in payload
    assert payload["attending_coverage"] == [coverage.model_dump(mode="json")]


def test_attending_schedule_references_are_strict_but_completion_is_not() -> None:
    instance = blank_instance()
    attending = Attending(id="attending-001", name="Ada Lovelace")
    empty_schedule = AttendingSchedule(attending_id=attending.id)

    configured = instance.revised(
        attendings=[attending],
        attending_schedules=[empty_schedule],
    )
    assert attending_schedule_report(configured).errors
    assert configured.attending_schedule_for(attending.id) == empty_schedule
    assert "weekly_work_schedules" not in attending.model_dump(mode="json")

    with pytest.raises(ValidationError, match="unknown attending"):
        instance.revised(attending_schedules=[empty_schedule])
    with pytest.raises(ValidationError, match="at most once"):
        instance.revised(
            attendings=[attending],
            attending_schedules=[empty_schedule, empty_schedule],
        )


def test_report_can_be_filtered_to_one_attending() -> None:
    instance = blank_instance().revised(
        attendings=[
            Attending(id="attending-001", name="Ada Lovelace"),
            Attending(id="attending-002", name="Grace Hopper"),
        ]
    )

    report = attending_schedule_report(instance, attending_id="attending-001")

    assert report.errors
    assert all(issue.attending_id == "attending-001" for issue in report.issues)
    assert all(
        issue.severity is AttendingScheduleIssueSeverity.ERROR
        for issue in report.issues
    )
    with pytest.raises(ValueError, match="unknown attending"):
        attending_schedule_report(instance, attending_id="missing")
