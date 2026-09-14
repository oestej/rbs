from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from rbs.catalog import blank_instance, sample_instance
from rbs.models.attending import (
    ATTENDING_WORK_DESCRIPTION_MAX_LENGTH,
    DEFAULT_ATTENDING_HALF_DAYS_PER_WEEK,
    MAX_ATTENDING_HALF_DAYS_PER_WEEK,
    Attending,
    AttendingSchedule,
    AttendingVacation,
    AttendingWeeklyShiftTarget,
    AttendingWeeklyTargetMode,
    AttendingWeeklyWorkSchedule,
    AttendingWorkHalfDay,
    AttendingWorkType,
    effective_attending_week,
)
from rbs.models.enums import Session, Weekday
from rbs.models.instance import SchedulerInput, SchedulingCase, SolverProblem
from rbs.ui.attendings.ops import (
    WEEKLY_SHIFT_TARGET_NONE,
    add_attending,
    add_vacation_range,
    apply_schedule_template,
    clear_weekly_work_schedule,
    move_work_half_day,
    next_attending_id,
    override_weekly_half_day_total,
    remove_attending,
    replace_attending,
    replace_weekly_shift_target,
    replace_weekly_work_schedule,
    use_default_weekly_half_day_total,
)
from rbs.ui.clinic.ops import (
    cancel_academic_half_day_for_week,
    replace_academic_half_day,
    set_academic_half_day_override,
)
from rbs.ui.edit_policy import instance_edit_impact
from rbs.workspaces import InstanceEditImpact


def test_attending_supports_uncapped_day_level_vacation_ranges() -> None:
    first_day = date(2026, 7, 1)
    vacations = [
        AttendingVacation(
            start_date=first_day + timedelta(days=index * 10),
            end_date=first_day + timedelta(days=index * 10 + index % 4),
        )
        for index in reversed(range(12))
    ]

    attending = Attending(
        id=" attending-100 ",
        name=" Ada Lovelace ",
        vacation_ranges=vacations,
    )

    assert attending.id == "attending-100"
    assert attending.name == "Ada Lovelace"
    assert len(attending.vacation_ranges) == 12
    assert attending.vacation_ranges == sorted(
        vacations,
        key=lambda vacation: (vacation.start_date, vacation.end_date),
    )
    assert attending.vacation_ranges[0].days == 1
    assert attending.vacation_ranges[3].days == 4


def test_attending_half_days_per_week_default_to_ten_and_are_editable() -> None:
    attending = Attending(id="attending-001", name="Ada Lovelace")

    assert attending.half_days_per_week == DEFAULT_ATTENDING_HALF_DAYS_PER_WEEK
    assert attending.revised(half_days_per_week=6).half_days_per_week == 6
    assert attending.revised(half_days_per_week=0).half_days_per_week == 0

    for invalid in (-1, MAX_ATTENDING_HALF_DAYS_PER_WEEK + 1):
        with pytest.raises(ValidationError, match="half_days_per_week"):
            attending.revised(half_days_per_week=invalid)


def test_attending_clinic_day_minimum_and_preferred_schedule_are_typed() -> None:
    preferred_clinic = AttendingWorkHalfDay(
        weekday=Weekday.TUESDAY,
        session=Session.MORNING,
        work_type=AttendingWorkType.ATTENDING_CLINIC,
    )
    preferred_admin = AttendingWorkHalfDay(
        weekday=Weekday.MONDAY,
        session=Session.AFTERNOON,
        work_type=AttendingWorkType.ADMIN_TIME,
    )
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=3,
        minimum_attending_clinic_days_per_week=2,
        preferred_weekly_schedule_half_days=[preferred_clinic, preferred_admin],
    )

    assert attending.minimum_attending_clinic_days_per_week == 2
    assert attending.preferred_weekly_schedule_half_days == [
        preferred_admin,
        preferred_clinic,
    ]
    assert Attending(id="attending-002", name="Grace Hopper").model_dump(
        mode="json"
    )[
        "preferred_weekly_schedule_half_days"
    ] == []

    with pytest.raises(ValidationError, match="minimum Attending Clinic days"):
        attending.revised(
            half_days_per_week=1,
            preferred_weekly_schedule_half_days=[],
        )
    with pytest.raises(ValidationError, match="cannot include Special/Other"):
        attending.revised(
            preferred_weekly_schedule_half_days=[
                AttendingWorkHalfDay(
                    weekday=Weekday.FRIDAY,
                    session=Session.AFTERNOON,
                    work_type=AttendingWorkType.SPECIAL_OTHER,
                    description="Coverage exception",
                )
            ]
        )
    with pytest.raises(ValidationError, match="preferred schedule half-days must be unique"):
        attending.revised(
            preferred_weekly_schedule_half_days=[
                preferred_clinic,
                preferred_clinic,
            ]
        )


def test_weekly_shift_targets_are_optional_typed_and_category_unique() -> None:
    fixed_zero = AttendingWeeklyShiftTarget(
        work_type=AttendingWorkType.PRECEPTING_CLINIC,
        minimum_shifts_per_week=0,
        maximum_shifts_per_week=0,
        mode=AttendingWeeklyTargetMode.FIXED,
    )
    flexible_admin = AttendingWeeklyShiftTarget(
        work_type=AttendingWorkType.ADMIN_TIME,
        minimum_shifts_per_week=1,
        maximum_shifts_per_week=2,
    )
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=6,
        weekly_shift_targets=[fixed_zero, flexible_admin],
    )

    assert attending.weekly_shift_targets == [fixed_zero, flexible_admin]
    assert flexible_admin.mode is AttendingWeeklyTargetMode.FLEXIBLE
    assert attending.weekly_shift_target_for(AttendingWorkType.PRECEPTING_CLINIC) == fixed_zero
    assert attending.weekly_shift_target_for(AttendingWorkType.INPATIENT_SERVICE) is None
    assert attending.model_dump(mode="json")["weekly_shift_targets"][0] == {
        "work_type": "precepting_clinic",
        "minimum_shifts_per_week": 0,
        "maximum_shifts_per_week": 0,
        "mode": "fixed",
    }

    with pytest.raises(ValidationError, match="must use unique categories"):
        attending.revised(weekly_shift_targets=[flexible_admin, flexible_admin])

    with pytest.raises(ValidationError, match="scheduled manually"):
        AttendingWeeklyShiftTarget(
            work_type=AttendingWorkType.SPECIAL_OTHER,
            minimum_shifts_per_week=0,
            maximum_shifts_per_week=1,
        )
    with pytest.raises(ValidationError, match="minimum shifts per week cannot exceed"):
        AttendingWeeklyShiftTarget(
            work_type=AttendingWorkType.ADMIN_TIME,
            minimum_shifts_per_week=3,
            maximum_shifts_per_week=2,
        )
    migrated = AttendingWeeklyShiftTarget.model_validate(
        {
            "work_type": "admin_time",
            "shifts_per_week": 2,
            "mode": "flexible",
        }
    )
    assert migrated.minimum_shifts_per_week == 2
    assert migrated.maximum_shifts_per_week == 2


def test_weekly_shift_targets_must_fit_the_attending_weekly_total() -> None:
    fixed_inpatient = AttendingWeeklyShiftTarget(
        work_type=AttendingWorkType.INPATIENT_SERVICE,
        minimum_shifts_per_week=4,
        maximum_shifts_per_week=4,
        mode=AttendingWeeklyTargetMode.FIXED,
    )
    fixed_precepting = AttendingWeeklyShiftTarget(
        work_type=AttendingWorkType.PRECEPTING_CLINIC,
        minimum_shifts_per_week=3,
        maximum_shifts_per_week=3,
        mode=AttendingWeeklyTargetMode.FIXED,
    )
    flexible_admin = AttendingWeeklyShiftTarget(
        work_type=AttendingWorkType.ADMIN_TIME,
        minimum_shifts_per_week=2,
        maximum_shifts_per_week=6,
    )

    with pytest.raises(ValidationError, match="fixed weekly shift target minimums"):
        Attending(
            id="attending-001",
            name="Ada Lovelace",
            half_days_per_week=6,
            weekly_shift_targets=[fixed_inpatient, fixed_precepting],
        )
    with pytest.raises(ValidationError, match="maximum weekly shift target cannot exceed"):
        Attending(
            id="attending-001",
            name="Ada Lovelace",
            half_days_per_week=5,
            weekly_shift_targets=[flexible_admin],
        )

    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=6,
        weekly_shift_targets=[
            fixed_inpatient,
            flexible_admin,
            AttendingWeeklyShiftTarget(
                work_type=AttendingWorkType.ATTENDING_CLINIC,
                minimum_shifts_per_week=0,
                maximum_shifts_per_week=6,
            ),
        ],
    )
    assert len(attending.weekly_shift_targets) == 3


def test_special_other_work_allows_only_a_trimmed_optional_description() -> None:
    special = AttendingWorkHalfDay(
        weekday=Weekday.MONDAY,
        session=Session.MORNING,
        work_type=AttendingWorkType.SPECIAL_OTHER,
        description="  Credentialing committee  ",
    )

    assert special.description == "Credentialing committee"
    assert special.revised(description="   ").description is None
    maximum_description = "x" * ATTENDING_WORK_DESCRIPTION_MAX_LENGTH
    assert special.revised(description=f"  {maximum_description}  ").description == (
        maximum_description
    )
    with pytest.raises(ValidationError, match="at most 120 characters"):
        special.revised(description=f"{maximum_description}x")
    with pytest.raises(ValidationError, match="only Special/Other work"):
        AttendingWorkHalfDay(
            weekday=Weekday.MONDAY,
            session=Session.AFTERNOON,
            work_type=AttendingWorkType.ADMIN_TIME,
            description="Committee meeting",
        )


def test_schedule_template_is_typed_unique_and_bounded_by_weekly_total() -> None:
    precepting = AttendingWorkHalfDay(
        weekday=Weekday.MONDAY,
        session=Session.MORNING,
        work_type=AttendingWorkType.PRECEPTING_CLINIC,
        clinic_id="maple",
    )
    admin = AttendingWorkHalfDay(
        weekday=Weekday.MONDAY,
        session=Session.AFTERNOON,
        work_type=AttendingWorkType.ADMIN_TIME,
    )

    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=2,
        schedule_template_half_days=[admin, precepting],
    )

    assert attending.schedule_template_half_days == [precepting, admin]
    with pytest.raises(ValidationError, match="schedule template half-days must be unique"):
        attending.revised(schedule_template_half_days=[precepting, precepting])
    with pytest.raises(ValidationError, match="cannot exceed the configured"):
        attending.revised(half_days_per_week=1)


def test_weekly_work_schedules_are_independent_unique_and_structural() -> None:
    monday = AttendingWorkHalfDay(
        weekday=Weekday.MONDAY,
        session=Session.MORNING,
        work_type=AttendingWorkType.ADMIN_TIME,
    )
    tuesday = AttendingWorkHalfDay(
        weekday=Weekday.TUESDAY,
        session=Session.AFTERNOON,
        work_type=AttendingWorkType.SPECIAL_OTHER,
    )
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=2,
    )
    schedule = AttendingSchedule(
        attending_id=attending.id,
        weeks=[
            AttendingWeeklyWorkSchedule(week=3, half_days=[tuesday, monday]),
            AttendingWeeklyWorkSchedule(week=1, half_days=[]),
        ],
    )

    assert [week.week for week in schedule.weeks] == [1, 3]
    assert schedule.weeks[1].half_days == [monday, tuesday]
    assert schedule.weeks[1].effective_half_days(2) == 2
    with pytest.raises(ValidationError, match="must use unique academic weeks"):
        AttendingSchedule(
            attending_id=attending.id,
            weeks=[
                AttendingWeeklyWorkSchedule(week=1),
                AttendingWeeklyWorkSchedule(week=1),
            ],
        )
    incomplete = blank_instance().revised(
        attendings=[attending.revised(half_days_per_week=1)],
        attending_schedules=[schedule],
    )
    assert incomplete.attending_schedule_for(attending.id) == schedule


def test_only_precepting_clinic_work_accepts_a_clinic_reference() -> None:
    with pytest.raises(ValidationError, match="must select a clinic"):
        AttendingWorkHalfDay(
            weekday=Weekday.MONDAY,
            session=Session.MORNING,
            work_type=AttendingWorkType.PRECEPTING_CLINIC,
        )
    with pytest.raises(ValidationError, match="only Precepting Clinic"):
        AttendingWorkHalfDay(
            weekday=Weekday.MONDAY,
            session=Session.MORNING,
            work_type=AttendingWorkType.ATTENDING_CLINIC,
            clinic_id="maple",
        )


def test_zero_baseline_attending_supports_a_weekly_half_day_override() -> None:
    first_monday = date(2026, 7, 6)
    morning = AttendingWorkHalfDay(
        weekday=Weekday.MONDAY,
        session=Session.MORNING,
        work_type=AttendingWorkType.ADMIN_TIME,
    )
    afternoon = AttendingWorkHalfDay(
        weekday=Weekday.THURSDAY,
        session=Session.AFTERNOON,
        work_type=AttendingWorkType.ATTENDING_CLINIC,
    )

    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=0,
    )
    accepted_schedule = AttendingSchedule(
        attending_id=attending.id,
        weeks=[
            AttendingWeeklyWorkSchedule(
                week=1,
                half_days_override=2,
                half_days=[afternoon, morning],
            )
        ],
    )

    week = accepted_schedule.weeks[0]
    assert week.half_days == [morning, afternoon]
    assert week.effective_half_days(attending.half_days_per_week) == 2
    assert (
        effective_attending_week(
            attending,
            accepted_schedule,
            week=1,
            first_day=first_monday,
            last_day=first_monday + timedelta(days=6),
            academic_half_day=None,
            automatic_academic_admin=False,
        ).assignment_on(Weekday.MONDAY, Session.MORNING)
        == morning
    )


def test_weekly_override_is_bounded_but_target_mismatches_can_be_saved() -> None:
    morning = AttendingWorkHalfDay(
        weekday=Weekday.MONDAY,
        session=Session.MORNING,
    )
    afternoon = AttendingWorkHalfDay(
        weekday=Weekday.MONDAY,
        session=Session.AFTERNOON,
    )

    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=0,
    )
    without_override = blank_instance().revised(
        attendings=[attending],
        attending_schedules=[
            AttendingSchedule(
                attending_id=attending.id,
                weeks=[
                    AttendingWeeklyWorkSchedule(week=1, half_days=[morning])
                ],
            )
        ],
    )
    assert without_override.attending_schedule_for(attending.id) is not None
    with_override = blank_instance().revised(
        attendings=[attending],
        attending_schedules=[
            AttendingSchedule(
                attending_id=attending.id,
                weeks=[
                    AttendingWeeklyWorkSchedule(
                        week=1,
                        half_days_override=1,
                        half_days=[morning, afternoon],
                    )
                ],
            )
        ],
    )
    accepted_schedule = with_override.attending_schedule_for(attending.id)
    assert accepted_schedule is not None
    assert accepted_schedule.weeks[0].half_days_override == 1
    for invalid in (-1, MAX_ATTENDING_HALF_DAYS_PER_WEEK + 1):
        with pytest.raises(ValidationError, match="half_days_override"):
            AttendingWeeklyWorkSchedule(week=1, half_days_override=invalid)


@pytest.mark.parametrize("work_type", list(AttendingWorkType))
def test_vacation_suppresses_every_attending_work_type(
    work_type: AttendingWorkType,
) -> None:
    first_monday = date(2026, 7, 6)
    assignment = AttendingWorkHalfDay(
        weekday=Weekday.MONDAY,
        session=Session.MORNING,
        work_type=work_type,
        clinic_id=(
            "maple"
            if work_type is AttendingWorkType.PRECEPTING_CLINIC
            else None
        ),
    )
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=0,
        vacation_ranges=[
            AttendingVacation(start_date=first_monday, end_date=first_monday)
        ],
    )
    schedule = AttendingSchedule(
        attending_id=attending.id,
        weeks=[
            AttendingWeeklyWorkSchedule(
                week=1,
                half_days_override=1,
                half_days=[assignment],
            )
        ],
    )
    assert (
        effective_attending_week(
            attending,
            schedule,
            week=1,
            first_day=first_monday,
            last_day=first_monday + timedelta(days=6),
            academic_half_day=None,
            automatic_academic_admin=False,
        ).assignment_on(Weekday.MONDAY, Session.MORNING)
        is None
    )


def test_weekly_work_is_effective_but_the_template_is_not() -> None:
    first_monday = date(2026, 7, 6)
    precepting = AttendingWorkHalfDay(
        weekday=Weekday.MONDAY,
        session=Session.MORNING,
        work_type=AttendingWorkType.PRECEPTING_CLINIC,
        clinic_id="maple",
    )
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=1,
        schedule_template_half_days=[precepting],
        vacation_ranges=[
            AttendingVacation(
                start_date=first_monday + timedelta(days=14),
                end_date=first_monday + timedelta(days=14),
            )
        ],
    )
    schedule = AttendingSchedule(
        attending_id=attending.id,
        weeks=[
            AttendingWeeklyWorkSchedule(week=1, half_days=[precepting]),
            AttendingWeeklyWorkSchedule(
                week=2,
                half_days_override=1,
                half_days=[
                    AttendingWorkHalfDay(
                        weekday=Weekday.MONDAY,
                        session=Session.MORNING,
                        work_type=AttendingWorkType.ADMIN_TIME,
                    )
                ],
            ),
            AttendingWeeklyWorkSchedule(week=3, half_days=[precepting]),
        ],
    )

    def work_in_week(week: int) -> AttendingWorkHalfDay | None:
        return effective_attending_week(
            attending,
            schedule,
            week=week,
            first_day=first_monday,
            last_day=first_monday + timedelta(days=34),
            academic_half_day=None,
            automatic_academic_admin=False,
        ).assignment_on(Weekday.MONDAY, Session.MORNING)

    scheduled = work_in_week(1)
    overridden = work_in_week(2)
    vacation = work_in_week(3)
    template_only = work_in_week(4)

    assert scheduled is not None
    assert scheduled.work_type is AttendingWorkType.PRECEPTING_CLINIC
    assert overridden is not None
    assert overridden.work_type is AttendingWorkType.ADMIN_TIME
    assert vacation is None
    assert template_only is None


def test_attending_rejects_reversed_schedule_and_vacation_ranges() -> None:
    with pytest.raises(ValidationError, match="schedule end date cannot be before"):
        Attending(
            id="attending-001",
            name="Ada Lovelace",
            schedule_start_date=date(2026, 8, 1),
            schedule_end_date=date(2026, 7, 31),
        )

    with pytest.raises(ValidationError, match="vacation end date cannot be before"):
        AttendingVacation(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 7, 31),
        )


def test_attending_rejects_overlapping_vacation_ranges_but_allows_adjacency() -> None:
    first = AttendingVacation(
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 5),
    )
    adjacent = AttendingVacation(
        start_date=date(2026, 8, 6),
        end_date=date(2026, 8, 8),
    )

    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        vacation_ranges=[adjacent, first],
    )
    assert attending.vacation_ranges == [first, adjacent]

    with pytest.raises(ValidationError, match="vacation ranges cannot overlap"):
        attending.revised(
            vacation_ranges=[
                first,
                AttendingVacation(
                    start_date=date(2026, 8, 5),
                    end_date=date(2026, 8, 9),
                ),
            ]
        )


def test_attending_dates_must_belong_to_the_workspace_academic_year() -> None:
    instance = blank_instance()
    first_day = instance.calendar.first_week_start
    last_day = first_day + timedelta(days=instance.calendar.weeks * 7 - 1)

    with pytest.raises(ValidationError, match="schedule start date.*outside academic year"):
        instance.revised(
            attendings=[
                Attending(
                    id="attending-001",
                    name="Ada Lovelace",
                    schedule_start_date=first_day - timedelta(days=1),
                )
            ]
        )

    with pytest.raises(ValidationError, match="vacation.*outside academic year"):
        instance.revised(
            attendings=[
                Attending(
                    id="attending-001",
                    name="Ada Lovelace",
                    vacation_ranges=[
                        AttendingVacation(
                            start_date=last_day,
                            end_date=last_day + timedelta(days=1),
                        )
                    ],
                )
            ]
        )

    with pytest.raises(ValidationError, match="weekly work schedule week.*outside"):
        attending = Attending(
            id="attending-001",
            name="Ada Lovelace",
        )
        instance.revised(
            attendings=[attending],
            attending_schedules=[
                AttendingSchedule(
                    attending_id=attending.id,
                    weeks=[
                        AttendingWeeklyWorkSchedule(
                            week=instance.calendar.weeks + 1,
                        )
                    ],
                )
            ],
        )


def test_attending_ids_are_unique_and_sorted_for_stable_persistence() -> None:
    instance = blank_instance()
    ada = Attending(id="attending-001", name="Ada Lovelace")
    grace = Attending(id="attending-001", name="Grace Hopper")

    with pytest.raises(ValidationError, match="attending IDs must be unique"):
        instance.revised(attendings=[ada, grace])

    sorted_instance = instance.revised(
        attendings=[
            Attending(id="attending-002", name="Grace Hopper"),
            Attending(id="attending-001", name="Ada Lovelace"),
        ]
    )
    assert [attending.name for attending in sorted_instance.attendings] == [
        "Grace Hopper",
        "Ada Lovelace",
    ]


def test_missing_schedule_dates_use_the_academic_year_boundaries() -> None:
    instance = blank_instance()
    first_day = instance.calendar.first_week_start
    last_day = first_day + timedelta(days=instance.calendar.weeks * 7 - 1)
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        vacation_ranges=[
            AttendingVacation(
                start_date=first_day + timedelta(days=10),
                end_date=first_day + timedelta(days=12),
            )
        ],
    )

    assert attending.effective_schedule_start(first_day) == first_day
    assert attending.effective_schedule_end(last_day) == last_day
    assert attending.is_scheduled_on(
        first_day,
        academic_year_start=first_day,
        academic_year_end=last_day,
    )
    assert attending.is_scheduled_on(
        last_day,
        academic_year_start=first_day,
        academic_year_end=last_day,
    )
    assert attending.is_on_vacation(first_day + timedelta(days=11))


def test_attending_clinic_days_count_distinct_effective_weekdays() -> None:
    instance = blank_instance()
    first_day = instance.calendar.first_week_start
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=5,
        minimum_attending_clinic_days_per_week=3,
    )
    schedule = AttendingSchedule(
        attending_id=attending.id,
        weeks=[
            AttendingWeeklyWorkSchedule(
                week=1,
                half_days=[
                    AttendingWorkHalfDay(
                        weekday=Weekday.MONDAY,
                        session=Session.MORNING,
                        work_type=AttendingWorkType.ATTENDING_CLINIC,
                    ),
                    AttendingWorkHalfDay(
                        weekday=Weekday.MONDAY,
                        session=Session.AFTERNOON,
                        work_type=AttendingWorkType.ATTENDING_CLINIC,
                    ),
                    AttendingWorkHalfDay(
                        weekday=Weekday.TUESDAY,
                        session=Session.MORNING,
                        work_type=AttendingWorkType.ATTENDING_CLINIC,
                    ),
                    AttendingWorkHalfDay(
                        weekday=Weekday.WEDNESDAY,
                        session=Session.AFTERNOON,
                        work_type=AttendingWorkType.ATTENDING_CLINIC,
                    ),
                ],
            )
        ],
    )
    configured = instance.revised(
        attendings=[attending],
        attending_schedules=[schedule],
    )

    # Monday counts once despite two shifts, and the academic slot is effective
    # Admin Time rather than the stored Attending Clinic assignment.
    assert configured.attending_clinic_days_for_week(attending, 1) == {
        Weekday.MONDAY,
        Weekday.TUESDAY,
    }
    assert configured.attending_clinic_day_minimum_for_week(attending, 1) == 3

    vacation_attending = attending.revised(
        vacation_ranges=[
            AttendingVacation(
                start_date=first_day + timedelta(days=1),
                end_date=first_day + timedelta(days=1),
            )
        ]
    )
    configured = instance.revised(
        attendings=[vacation_attending],
        attending_schedules=[schedule],
    )
    assert configured.attending_clinic_days_for_week(vacation_attending, 1) == {
        Weekday.MONDAY
    }
    assert configured.attending_clinic_day_minimum_for_week(vacation_attending, 1) == 0

    weekend_vacation = attending.revised(
        vacation_ranges=[
            AttendingVacation(
                start_date=first_day + timedelta(days=5),
                end_date=first_day + timedelta(days=6),
            )
        ]
    )
    configured = instance.revised(
        attendings=[weekend_vacation],
        attending_schedules=[schedule],
    )
    assert configured.attending_clinic_day_minimum_for_week(weekend_vacation, 1) == 3


def test_academic_half_day_is_derived_admin_time_for_active_attendings() -> None:
    instance = blank_instance()
    first_day = instance.calendar.first_week_start
    clinic_id = instance.clinic_policy.primary_site_id
    stored_precepting = AttendingWorkHalfDay(
        weekday=Weekday.WEDNESDAY,
        session=Session.AFTERNOON,
        work_type=AttendingWorkType.PRECEPTING_CLINIC,
        clinic_id=clinic_id,
    )
    moved_slot = AttendingWorkHalfDay(
        weekday=Weekday.FRIDAY,
        session=Session.MORNING,
        work_type=AttendingWorkType.ATTENDING_CLINIC,
    )
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=2,
    )
    schedule = AttendingSchedule(
        attending_id=attending.id,
        weeks=[
            AttendingWeeklyWorkSchedule(week=1, half_days=[stored_precepting]),
            AttendingWeeklyWorkSchedule(week=2, half_days=[moved_slot]),
            AttendingWeeklyWorkSchedule(week=3, half_days=[stored_precepting]),
        ],
    )
    configured = instance.revised(
        attendings=[attending],
        attending_schedules=[schedule],
    )

    academic_day = first_day + timedelta(days=2)
    effective = configured.attending_work_half_day_on(
        attending,
        academic_day,
        Session.AFTERNOON,
    )
    assert effective is not None
    assert effective.work_type is AttendingWorkType.ADMIN_TIME
    assert schedule.weeks[0].half_days == [stored_precepting]

    moved = set_academic_half_day_override(
        configured,
        2,
        Weekday.FRIDAY,
        Session.MORNING,
    )
    effective = moved.attending_work_half_day_on(
        moved.attendings[0],
        first_day + timedelta(days=11),
        Session.MORNING,
    )
    assert effective is not None
    assert effective.work_type is AttendingWorkType.ADMIN_TIME

    cancelled = cancel_academic_half_day_for_week(moved, 3)
    effective = cancelled.attending_work_half_day_on(
        cancelled.attendings[0],
        first_day + timedelta(days=16),
        Session.AFTERNOON,
    )
    assert effective == stored_precepting

    disabled = replace_academic_half_day(
        configured,
        Weekday.WEDNESDAY,
        Session.AFTERNOON,
        academic_half_day_is_attending_admin_time=False,
    )
    assert (
        disabled.attending_work_half_day_on(
            disabled.attendings[0],
            academic_day,
            Session.AFTERNOON,
        )
        == stored_precepting
    )


def test_academic_admin_time_respects_schedule_dates_and_vacation() -> None:
    instance = blank_instance()
    first_day = instance.calendar.first_week_start
    academic_day = first_day + timedelta(days=2)
    outside_span = Attending(
        id="attending-001",
        name="Ada Lovelace",
        schedule_start_date=first_day + timedelta(days=3),
    )
    on_vacation = Attending(
        id="attending-002",
        name="Grace Hopper",
        vacation_ranges=[
            AttendingVacation(start_date=academic_day, end_date=academic_day)
        ],
    )
    configured = instance.revised(attendings=[outside_span, on_vacation])
    by_id = {attending.id: attending for attending in configured.attendings}

    assert (
        configured.attending_work_half_day_on(
            by_id["attending-001"],
            academic_day,
            Session.AFTERNOON,
        )
        is None
    )
    assert (
        configured.attending_work_half_day_on(
            by_id["attending-002"],
            academic_day,
            Session.AFTERNOON,
        )
        is None
    )


def test_attendings_round_trip_through_workspace_case_but_not_solver_problem() -> None:
    instance = sample_instance()

    case = instance.scheduling_case()
    restored = instance.constraint_catalog().apply(case)
    solver_problem = SolverProblem.from_instance(instance)

    assert case.attendings == instance.attendings
    assert restored.attendings == instance.attendings
    assert "attendings" not in solver_problem.model_dump(mode="json")
    assert "attendings" not in SolverProblem.model_json_schema()["properties"]
    assert "attending_coverage" not in SchedulerInput.model_json_schema()["properties"]


def test_attending_managed_capacity_uses_effective_precepting_assignments() -> None:
    instance = blank_instance()
    first_day = instance.calendar.first_week_start
    clinic_id = instance.clinic_policy.primary_site_id
    raw = instance.model_dump(mode="json")
    clinic = next(
        site for site in raw["clinic_policy"]["sites"] if site["id"] == clinic_id
    )
    clinic["staffing_mode"] = "attending_managed"
    clinic["half_days"] = [
        {
            "weekday": "monday",
            "session": "morning",
            "attendings": 3,
            "min_residents": 0,
        }
    ]
    clinic["capacity_overrides"] = [
        {
            "date": first_day.isoformat(),
            "session": "morning",
            "attendings": 5,
            "min_residents": 0,
        }
    ]
    closed_day = first_day + timedelta(days=21)
    raw["clinic_policy"]["closure_days"] = [
        {
            "date": closed_day.isoformat(),
            "sites": [clinic_id],
            "name": "Closed",
        }
    ]
    raw["attendings"] = [
        Attending(
            id="attending-001",
            name="Ada Lovelace",
            half_days_per_week=1,
        ).model_dump(mode="json"),
        Attending(
            id="attending-002",
            name="Grace Hopper",
            half_days_per_week=0,
        ).model_dump(mode="json"),
        Attending(
            id="attending-003",
            name="Katherine Johnson",
            half_days_per_week=0,
            vacation_ranges=[
                AttendingVacation(start_date=first_day, end_date=first_day)
            ],
        ).model_dump(mode="json"),
    ]
    raw["attending_schedules"] = [
        AttendingSchedule(
            attending_id="attending-001",
            weeks=[
                AttendingWeeklyWorkSchedule(
                    week=week,
                    half_days=[
                        AttendingWorkHalfDay(
                            weekday=Weekday.MONDAY,
                            session=Session.MORNING,
                            work_type=AttendingWorkType.PRECEPTING_CLINIC,
                            clinic_id=clinic_id,
                        )
                    ],
                )
                for week in (1, 2, 4)
            ],
        ).model_dump(mode="json"),
        AttendingSchedule(
            attending_id="attending-002",
            weeks=[
                AttendingWeeklyWorkSchedule(
                    week=1,
                    half_days_override=1,
                    half_days=[
                        AttendingWorkHalfDay(
                            weekday=Weekday.MONDAY,
                            session=Session.MORNING,
                            work_type=AttendingWorkType.PRECEPTING_CLINIC,
                            clinic_id=clinic_id,
                        )
                    ],
                )
            ],
        ).model_dump(mode="json"),
        AttendingSchedule(
            attending_id="attending-003",
            weeks=[
                AttendingWeeklyWorkSchedule(
                    week=1,
                    half_days_override=1,
                    half_days=[
                        AttendingWorkHalfDay(
                            weekday=Weekday.MONDAY,
                            session=Session.MORNING,
                            work_type=AttendingWorkType.PRECEPTING_CLINIC,
                            clinic_id=clinic_id,
                        )
                    ],
                )
            ],
        ).model_dump(mode="json"),
    ]
    configured = SchedulerInput.model_validate(raw)

    assert configured.clinic_attending_count_on(
        clinic_id,
        first_day,
        Session.MORNING,
    ) == 2
    assert configured.clinic_max_capacity_on(
        clinic_id,
        first_day,
        Session.MORNING,
    ) == 8
    assert configured.clinic_max_capacity_on(
        clinic_id,
        first_day + timedelta(days=7),
        Session.MORNING,
    ) == 4
    assert configured.clinic_max_capacity_on(
        clinic_id,
        first_day + timedelta(days=14),
        Session.MORNING,
    ) == 0
    assert configured.clinic_max_capacity_on(
        clinic_id,
        closed_day,
        Session.MORNING,
    ) == 0

    projected = SolverProblem.from_instance(configured)
    assert configured.clinic_policy.site(clinic_id).half_days
    assert projected.clinic_policy.site(clinic_id).half_days == []
    assert projected.clinic_policy.site(clinic_id).capacity_overrides == []
    assert all(coverage.date != closed_day for coverage in projected.attending_coverage)
    assert projected.clinic_max_capacity_on(
        clinic_id,
        first_day,
        Session.MORNING,
    ) == 8
    assert "attending_coverage" not in configured.model_dump(mode="json")
    assert "attending_coverage" in projected.model_dump(mode="json")


def test_academic_admin_time_removes_precepting_coverage_until_disabled() -> None:
    instance = blank_instance()
    first_day = instance.calendar.first_week_start
    academic_day = first_day + timedelta(days=2)
    clinic_id = instance.clinic_policy.primary_site_id
    raw = instance.model_dump(mode="json")
    clinic = next(
        site for site in raw["clinic_policy"]["sites"] if site["id"] == clinic_id
    )
    clinic["staffing_mode"] = "attending_managed"
    raw["attendings"] = [
        Attending(
            id="attending-001",
            name="Ada Lovelace",
            half_days_per_week=1,
        ).model_dump(mode="json")
    ]
    raw["attending_schedules"] = [
        AttendingSchedule(
            attending_id="attending-001",
            weeks=[
                AttendingWeeklyWorkSchedule(
                    week=1,
                    half_days=[
                        AttendingWorkHalfDay(
                            weekday=Weekday.WEDNESDAY,
                            session=Session.AFTERNOON,
                            work_type=AttendingWorkType.PRECEPTING_CLINIC,
                            clinic_id=clinic_id,
                        )
                    ],
                )
            ],
        ).model_dump(mode="json")
    ]

    automatic_admin = SchedulerInput.model_validate(raw)
    assert (
        automatic_admin.clinic_attending_count_on(
            clinic_id,
            academic_day,
            Session.AFTERNOON,
        )
        == 0
    )

    raw["clinic_policy"]["academic_half_day_is_attending_admin_time"] = False
    disabled = SchedulerInput.model_validate(raw)
    assert (
        disabled.clinic_attending_count_on(
            clinic_id,
            academic_day,
            Session.AFTERNOON,
        )
        == 1
    )
    assert (
        instance_edit_impact(automatic_admin, disabled)
        is InstanceEditImpact.SOLVER_INPUT
    )
    projected = SolverProblem.from_instance(disabled)
    assert "academic_half_day_is_attending_admin_time" not in projected.model_dump(
        mode="json"
    )["clinic_policy"]
    assert (
        "academic_half_day_is_attending_admin_time"
        not in SolverProblem.model_json_schema()["$defs"]["SolverClinicPolicy"][
            "properties"
        ]
    )


def test_attending_clinic_reference_must_exist_in_the_combined_workspace() -> None:
    instance = blank_instance()
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=1,
    )
    with pytest.raises(ValidationError, match="references unknown clinic"):
        instance.revised(
            attendings=[attending],
            attending_schedules=[
                AttendingSchedule(
                    attending_id=attending.id,
                    weeks=[
                        AttendingWeeklyWorkSchedule(
                            week=1,
                            half_days=[
                                AttendingWorkHalfDay(
                                    weekday=Weekday.MONDAY,
                                    session=Session.MORNING,
                                    work_type=AttendingWorkType.PRECEPTING_CLINIC,
                                    clinic_id="unknown_clinic",
                                )
                            ],
                        )
                    ],
                )
            ],
        )


def test_attending_crud_revalidates_the_complete_instance() -> None:
    instance = blank_instance()
    first_day = instance.calendar.first_week_start
    attending = Attending(id="attending-001", name="Ada Lovelace")

    added = add_attending(instance, attending)
    replacement = attending.revised(
        name="Ada Byron",
        schedule_start_date=first_day + timedelta(days=7),
    )
    replaced = replace_attending(added, attending.id, replacement)
    removed = remove_attending(replaced, replacement.id)

    assert next_attending_id(instance) == "attending-001"
    assert next_attending_id(added) == "attending-002"
    assert replaced.attendings == [replacement]
    assert removed.attendings == []
    with pytest.raises(ValueError, match="unknown attending"):
        remove_attending(instance, "missing")


def test_vacation_editor_helper_accepts_arbitrary_days_and_rejects_overlap() -> None:
    instance = blank_instance()
    first_day = instance.calendar.first_week_start
    first = add_vacation_range(
        instance,
        [],
        start_value=(first_day + timedelta(days=2)).isoformat(),
        end_value=(first_day + timedelta(days=5)).isoformat(),
    )

    assert first[0].start_date.weekday() == 2
    assert first[0].end_date.weekday() == 5
    assert first[0].weekdays == 3
    with pytest.raises(ValidationError, match="cannot overlap"):
        add_vacation_range(
            instance,
            first,
            start_value=(first_day + timedelta(days=4)).isoformat(),
            end_value=(first_day + timedelta(days=8)).isoformat(),
        )


def test_weekly_override_helpers_adopt_the_assigned_count_and_restore_default() -> None:
    instance = blank_instance()
    half_days = [
        AttendingWorkHalfDay(
            weekday=Weekday.MONDAY,
            session=Session.MORNING,
            work_type=AttendingWorkType.ADMIN_TIME,
        ),
        AttendingWorkHalfDay(
            weekday=Weekday.THURSDAY,
            session=Session.AFTERNOON,
            work_type=AttendingWorkType.ATTENDING_CLINIC,
        ),
    ]
    schedules = replace_weekly_work_schedule(
        instance,
        [],
        week=3,
        half_days=half_days,
    )

    assert schedules[0].half_days_override is None
    overridden = override_weekly_half_day_total(
        instance,
        schedules,
        week=3,
    )
    assert overridden[0].half_days_override == 2
    attending = Attending(
        id="attending-001", name="Ada Lovelace", half_days_per_week=0
    )
    assert blank_instance().revised(
        attendings=[attending],
        attending_schedules=[
            AttendingSchedule(attending_id=attending.id, weeks=overridden)
        ],
    )

    with pytest.raises(ValueError, match="remove assignments or raise the default"):
        use_default_weekly_half_day_total(
            instance,
            overridden,
            week=3,
            default_half_days=0,
        )
    restored = use_default_weekly_half_day_total(
        instance,
        overridden,
        week=3,
        default_half_days=2,
    )
    assert restored[0].half_days_override is None
    assert clear_weekly_work_schedule(instance, restored, week=3) == []

    # Derived assignments, such as automatic academic Admin Time, are not
    # stored in the week's half-day list but still count when Override is used.
    derived_only = override_weekly_half_day_total(
        instance,
        [],
        week=4,
        assigned_half_days=1,
    )
    assert derived_only == [
        AttendingWeeklyWorkSchedule(
            week=4,
            half_days_override=1,
            half_days=[],
        )
    ]
    with pytest.raises(ValueError, match="remove assignments or raise the default"):
        use_default_weekly_half_day_total(
            instance,
            derived_only,
            week=4,
            default_half_days=0,
            assigned_half_days=1,
        )


def test_weekly_shift_target_helper_sets_replaces_and_removes_categories() -> None:
    targets = replace_weekly_shift_target(
        [],
        work_type_value=AttendingWorkType.ADMIN_TIME.value,
        mode_value=AttendingWeeklyTargetMode.FLEXIBLE.value,
        minimum_value=1,
        maximum_value=2,
    )
    targets = replace_weekly_shift_target(
        targets,
        work_type_value=AttendingWorkType.INPATIENT_SERVICE.value,
        mode_value=AttendingWeeklyTargetMode.FIXED.value,
        minimum_value=3,
        maximum_value=4,
    )
    targets = replace_weekly_shift_target(
        targets,
        work_type_value=AttendingWorkType.ADMIN_TIME.value,
        mode_value=AttendingWeeklyTargetMode.FIXED.value,
        minimum_value=1,
        maximum_value=1,
    )

    assert [target.work_type for target in targets] == [
        AttendingWorkType.INPATIENT_SERVICE,
        AttendingWorkType.ADMIN_TIME,
    ]
    assert targets[1].minimum_shifts_per_week == 1
    assert targets[1].maximum_shifts_per_week == 1
    assert targets[1].mode is AttendingWeeklyTargetMode.FIXED

    targets = replace_weekly_shift_target(
        targets,
        work_type_value=AttendingWorkType.INPATIENT_SERVICE.value,
        mode_value=WEEKLY_SHIFT_TARGET_NONE,
        minimum_value=None,
        maximum_value=None,
    )
    assert [target.work_type for target in targets] == [AttendingWorkType.ADMIN_TIME]

    with pytest.raises(ValueError, match="scheduled manually"):
        replace_weekly_shift_target(
            targets,
            work_type_value=AttendingWorkType.SPECIAL_OTHER.value,
            mode_value=AttendingWeeklyTargetMode.FLEXIBLE.value,
            minimum_value=0,
            maximum_value=1,
        )


def test_schedule_template_application_creates_independent_weekly_copies() -> None:
    instance = blank_instance()
    template = [
        AttendingWorkHalfDay(
            weekday=Weekday.MONDAY,
            session=Session.MORNING,
            work_type=AttendingWorkType.ADMIN_TIME,
        )
    ]

    schedules = apply_schedule_template(
        instance,
        [AttendingWeeklyWorkSchedule(week=2, half_days_override=3)],
        template,
        first_week=2,
        last_week=3,
    )
    template[:] = [
        AttendingWorkHalfDay(
            weekday=Weekday.TUESDAY,
            session=Session.AFTERNOON,
            work_type=AttendingWorkType.SPECIAL_OTHER,
        )
    ]

    assert [schedule.week for schedule in schedules] == [2, 3]
    assert schedules[0].half_days_override == 3
    assert schedules[1].half_days_override is None
    assert all(
        schedule.half_days[0].weekday is Weekday.MONDAY for schedule in schedules
    )
    cleared = replace_weekly_work_schedule(
        instance,
        schedules,
        week=2,
        half_days=[],
    )
    assert cleared[0] == AttendingWeeklyWorkSchedule(week=2)
    assert cleared[1].half_days

    with pytest.raises(ValueError, match="last week cannot be before"):
        apply_schedule_template(
            instance,
            schedules,
            template,
            first_week=3,
            last_week=2,
        )


def test_attending_work_half_days_move_to_open_slots_and_swap_occupied_slots() -> None:
    admin = AttendingWorkHalfDay(
        weekday=Weekday.MONDAY,
        session=Session.MORNING,
        work_type=AttendingWorkType.ADMIN_TIME,
    )
    special = AttendingWorkHalfDay(
        weekday=Weekday.TUESDAY,
        session=Session.AFTERNOON,
        work_type=AttendingWorkType.SPECIAL_OTHER,
        description="Faculty development",
    )

    moved = move_work_half_day(
        [admin, special],
        source_weekday=Weekday.MONDAY,
        source_session=Session.MORNING,
        target_weekday=Weekday.WEDNESDAY,
        target_session=Session.AFTERNOON,
    )
    assert {
        (half_day.weekday, half_day.session): half_day.work_type
        for half_day in moved
    } == {
        (Weekday.TUESDAY, Session.AFTERNOON): AttendingWorkType.SPECIAL_OTHER,
        (Weekday.WEDNESDAY, Session.AFTERNOON): AttendingWorkType.ADMIN_TIME,
    }

    swapped = move_work_half_day(
        moved,
        source_weekday=Weekday.WEDNESDAY,
        source_session=Session.AFTERNOON,
        target_weekday=Weekday.TUESDAY,
        target_session=Session.AFTERNOON,
    )
    assert {
        (half_day.weekday, half_day.session): half_day.work_type
        for half_day in swapped
    } == {
        (Weekday.TUESDAY, Session.AFTERNOON): AttendingWorkType.ADMIN_TIME,
        (Weekday.WEDNESDAY, Session.AFTERNOON): AttendingWorkType.SPECIAL_OTHER,
    }
    assert move_work_half_day(
        swapped,
        source_weekday=Weekday.TUESDAY,
        source_session=Session.AFTERNOON,
        target_weekday=Weekday.TUESDAY,
        target_session=Session.AFTERNOON,
    ) == swapped
    assert next(
        half_day
        for half_day in swapped
        if half_day.work_type is AttendingWorkType.SPECIAL_OTHER
    ).description == "Faculty development"
    with pytest.raises(ValueError, match="no longer exists"):
        move_work_half_day(
            swapped,
            source_weekday=Weekday.SUNDAY,
            source_session=Session.MORNING,
            target_weekday=Weekday.MONDAY,
            target_session=Session.MORNING,
        )


def test_nonprecepting_attending_setup_preserves_current_resident_schedule() -> None:
    instance = blank_instance()
    edited = instance.revised(
        attendings=[Attending(id="attending-001", name="Ada Lovelace")]
    )

    assert (
        instance_edit_impact(instance, edited)
        is InstanceEditImpact.COMPATIBLE_CONFIGURATION
    )

    academic_admin_disabled = replace_academic_half_day(
        instance,
        Weekday.WEDNESDAY,
        Session.AFTERNOON,
        academic_half_day_is_attending_admin_time=False,
    )
    assert (
        instance_edit_impact(instance, academic_admin_disabled)
        is InstanceEditImpact.COMPATIBLE_CONFIGURATION
    )


def test_precepting_change_stales_schedule_only_for_attending_managed_clinic() -> None:
    instance = blank_instance()
    clinic_id = instance.clinic_policy.primary_site_id
    precepting = AttendingWorkHalfDay(
        weekday=Weekday.MONDAY,
        session=Session.MORNING,
        work_type=AttendingWorkType.PRECEPTING_CLINIC,
        clinic_id=clinic_id,
    )
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=1,
    )
    schedule = AttendingSchedule(
        attending_id=attending.id,
        weeks=[
            AttendingWeeklyWorkSchedule(week=1, half_days=[precepting])
        ],
    )
    capacity_managed_edit = instance.revised(
        attendings=[attending],
        attending_schedules=[schedule],
    )

    raw = instance.model_dump(mode="json")
    site = next(
        item for item in raw["clinic_policy"]["sites"] if item["id"] == clinic_id
    )
    site["staffing_mode"] = "attending_managed"
    attending_managed = SchedulerInput.model_validate(raw)
    template_only_edit = attending_managed.revised(
        attendings=[
            Attending(
                id="attending-001",
                name="Ada Lovelace",
                half_days_per_week=1,
                schedule_template_half_days=[precepting],
            )
        ]
    )
    attending_managed_edit = attending_managed.revised(
        attendings=[attending],
        attending_schedules=[schedule],
    )

    assert (
        instance_edit_impact(instance, capacity_managed_edit)
        is InstanceEditImpact.COMPATIBLE_CONFIGURATION
    )
    assert (
        instance_edit_impact(attending_managed, template_only_edit)
        is InstanceEditImpact.COMPATIBLE_CONFIGURATION
    )
    assert (
        instance_edit_impact(attending_managed, attending_managed_edit)
        is InstanceEditImpact.SOLVER_INPUT
    )


def test_attendings_default_when_loading_an_older_unversioned_case_shape() -> None:
    payload = sample_instance().model_dump(mode="json")
    payload.pop("attendings")
    payload.pop("attending_schedules")

    restored = SchedulerInput.model_validate(payload)

    assert restored.attendings == []


def test_nested_attending_schedules_migrate_without_data_loss() -> None:
    payload = sample_instance().model_dump(mode="json")
    accepted_schedules = payload.pop("attending_schedules")
    weeks_by_attending = {
        schedule["attending_id"]: schedule["weeks"]
        for schedule in accepted_schedules
    }
    for attending in payload["attendings"]:
        attending["weekly_work_schedules"] = weeks_by_attending.get(
            attending["id"], []
        )

    restored = SchedulerInput.model_validate(payload)

    assert restored.model_dump(mode="json")["attending_schedules"] == accepted_schedules
    assert all(
        "weekly_work_schedules" not in attending
        for attending in restored.model_dump(mode="json")["attendings"]
    )


def test_mixed_attending_schedule_shapes_are_rejected() -> None:
    payload = sample_instance().model_dump(mode="json")
    accepted_schedule = payload["attending_schedules"][0]
    attending = next(
        item
        for item in payload["attendings"]
        if item["id"] == accepted_schedule["attending_id"]
    )
    attending["weekly_work_schedules"] = accepted_schedule["weeks"]

    with pytest.raises(ValidationError, match="both nested and case-level"):
        SchedulerInput.model_validate(payload)


def test_persisted_case_migrates_nested_attending_schedules() -> None:
    payload = sample_instance().scheduling_case().model_dump(mode="json")
    accepted_schedules = payload.pop("attending_schedules")
    for accepted_schedule in accepted_schedules:
        attending = next(
            item
            for item in payload["attendings"]
            if item["id"] == accepted_schedule["attending_id"]
        )
        attending["weekly_work_schedules"] = accepted_schedule["weeks"]

    restored = SchedulingCase.model_validate(payload)

    assert restored.model_dump(mode="json")["attending_schedules"] == accepted_schedules


def test_weekly_half_day_overrides_default_when_loading_an_earlier_shape() -> None:
    payload = sample_instance().model_dump(mode="json")
    for accepted_schedule in payload["attending_schedules"]:
        for week in accepted_schedule["weeks"]:
            week.pop("half_days_override")
        if accepted_schedule["weeks"]:
            attending = next(
                item
                for item in payload["attendings"]
                if item["id"] == accepted_schedule["attending_id"]
            )
            attending["half_days_per_week"] = MAX_ATTENDING_HALF_DAYS_PER_WEEK

    restored = SchedulerInput.model_validate(payload)

    assert all(
        week.half_days_override is None
        for schedule in restored.attending_schedules
        for week in schedule.weeks
    )


def test_weekly_shift_targets_default_when_loading_an_earlier_attending_shape() -> None:
    payload = sample_instance().model_dump(mode="json")
    for attending in payload["attendings"]:
        attending.pop("weekly_shift_targets")
        attending.pop("minimum_attending_clinic_days_per_week")
        attending.pop("preferred_weekly_schedule_half_days")

    restored = SchedulerInput.model_validate(payload)

    assert all(not attending.weekly_shift_targets for attending in restored.attendings)
    assert all(
        attending.minimum_attending_clinic_days_per_week == 0
        for attending in restored.attendings
    )
    assert all(
        not attending.preferred_weekly_schedule_half_days
        for attending in restored.attendings
    )
