from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from rbs.catalog import blank_instance, sample_instance
from rbs.models.attending import (
    DEFAULT_ATTENDING_HALF_DAYS_PER_WEEK,
    MAX_ATTENDING_HALF_DAYS_PER_WEEK,
    Attending,
    AttendingAdHocWorkHalfDay,
    AttendingVacation,
    AttendingWeeklyShiftTarget,
    AttendingWeeklyTargetMode,
    AttendingWeeklyWorkSchedule,
    AttendingWorkHalfDay,
    AttendingWorkType,
)
from rbs.models.enums import Session, Weekday
from rbs.models.instance import SchedulerInput, SolverProblem
from rbs.ui.attendings.ops import (
    AD_HOC_ALL_DAY,
    WEEKLY_SHIFT_TARGET_NONE,
    add_ad_hoc_work_half_days,
    add_attending,
    add_vacation_range,
    apply_schedule_template,
    move_work_half_day,
    next_attending_id,
    remove_attending,
    replace_attending,
    replace_weekly_shift_target,
    replace_weekly_work_schedule,
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


def test_weekly_shift_targets_are_optional_typed_and_category_unique() -> None:
    fixed_zero = AttendingWeeklyShiftTarget(
        work_type=AttendingWorkType.SPECIAL_OTHER,
        shifts_per_week=0,
        mode=AttendingWeeklyTargetMode.FIXED,
    )
    flexible_admin = AttendingWeeklyShiftTarget(
        work_type=AttendingWorkType.ADMIN_TIME,
        shifts_per_week=2,
    )
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=6,
        weekly_shift_targets=[fixed_zero, flexible_admin],
    )

    assert attending.weekly_shift_targets == [flexible_admin, fixed_zero]
    assert flexible_admin.mode is AttendingWeeklyTargetMode.FLEXIBLE
    assert attending.weekly_shift_target_for(AttendingWorkType.SPECIAL_OTHER) == fixed_zero
    assert attending.weekly_shift_target_for(AttendingWorkType.INPATIENT_SERVICE) is None
    assert attending.model_dump(mode="json")["weekly_shift_targets"][1] == {
        "work_type": "special_other",
        "shifts_per_week": 0,
        "mode": "fixed",
    }

    with pytest.raises(ValidationError, match="must use unique categories"):
        attending.revised(weekly_shift_targets=[flexible_admin, flexible_admin])


def test_weekly_shift_targets_must_fit_the_attending_weekly_total() -> None:
    fixed_inpatient = AttendingWeeklyShiftTarget(
        work_type=AttendingWorkType.INPATIENT_SERVICE,
        shifts_per_week=4,
        mode=AttendingWeeklyTargetMode.FIXED,
    )
    fixed_precepting = AttendingWeeklyShiftTarget(
        work_type=AttendingWorkType.PRECEPTING_CLINIC,
        shifts_per_week=3,
        mode=AttendingWeeklyTargetMode.FIXED,
    )
    flexible_admin = AttendingWeeklyShiftTarget(
        work_type=AttendingWorkType.ADMIN_TIME,
        shifts_per_week=6,
    )

    with pytest.raises(ValidationError, match="fixed weekly shift targets cannot exceed"):
        Attending(
            id="attending-001",
            name="Ada Lovelace",
            half_days_per_week=6,
            weekly_shift_targets=[fixed_inpatient, fixed_precepting],
        )
    with pytest.raises(ValidationError, match="weekly shift target cannot exceed"):
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
                shifts_per_week=6,
            ),
        ],
    )
    assert len(attending.weekly_shift_targets) == 3


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


def test_weekly_work_schedules_are_independent_unique_and_bounded() -> None:
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
        weekly_work_schedules=[
            AttendingWeeklyWorkSchedule(week=3, half_days=[tuesday, monday]),
            AttendingWeeklyWorkSchedule(week=1, half_days=[]),
        ],
    )

    assert [schedule.week for schedule in attending.weekly_work_schedules] == [1, 3]
    assert attending.weekly_work_schedules[1].half_days == [monday, tuesday]
    with pytest.raises(ValidationError, match="must use unique weeks"):
        attending.revised(
            weekly_work_schedules=[
                AttendingWeeklyWorkSchedule(week=1),
                AttendingWeeklyWorkSchedule(week=1),
            ]
        )
    with pytest.raises(ValidationError, match="week 3 work half-days cannot exceed"):
        attending.revised(half_days_per_week=1)


def test_only_precepting_clinic_work_accepts_a_clinic_reference() -> None:
    with pytest.raises(ValidationError, match="must select a clinic"):
        AttendingWorkHalfDay(
            weekday=Weekday.MONDAY,
            session=Session.MORNING,
            work_type=AttendingWorkType.PRECEPTING_CLINIC,
        )
    with pytest.raises(ValidationError, match="only Precepting Clinic"):
        AttendingAdHocWorkHalfDay(
            date=date(2026, 7, 1),
            session=Session.MORNING,
            work_type=AttendingWorkType.ATTENDING_CLINIC,
            clinic_id="maple",
        )


def test_zero_baseline_attending_supports_ordered_ad_hoc_am_and_pm_work() -> None:
    first_day = date(2026, 7, 1)
    morning = AttendingAdHocWorkHalfDay(
        date=first_day,
        session=Session.MORNING,
    )
    afternoon = AttendingAdHocWorkHalfDay(
        date=first_day,
        session=Session.AFTERNOON,
    )
    later = AttendingAdHocWorkHalfDay(
        date=first_day + timedelta(days=3),
        session=Session.MORNING,
    )

    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=0,
        ad_hoc_work_half_days=[later, afternoon, morning],
    )

    assert attending.ad_hoc_work_half_days == [morning, afternoon, later]
    assert attending.has_ad_hoc_work(first_day, Session.MORNING)
    assert attending.has_ad_hoc_work(first_day, Session.AFTERNOON)
    assert not attending.has_ad_hoc_work(first_day + timedelta(days=1), Session.MORNING)


def test_ad_hoc_work_rejects_duplicates_and_conflicting_attending_dates() -> None:
    work = AttendingAdHocWorkHalfDay(
        date=date(2026, 8, 5),
        session=Session.MORNING,
    )

    with pytest.raises(ValidationError, match="ad hoc work half-days must be unique"):
        Attending(
            id="attending-001",
            name="Ada Lovelace",
            ad_hoc_work_half_days=[work, work],
        )

    with pytest.raises(ValidationError, match="cannot be before the schedule start date"):
        Attending(
            id="attending-001",
            name="Ada Lovelace",
            schedule_start_date=work.date + timedelta(days=1),
            ad_hoc_work_half_days=[work],
        )

    with pytest.raises(ValidationError, match="conflicts with vacation"):
        Attending(
            id="attending-001",
            name="Ada Lovelace",
            vacation_ranges=[
                AttendingVacation(
                    start_date=work.date,
                    end_date=work.date + timedelta(days=2),
                )
            ],
            ad_hoc_work_half_days=[work],
        )


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
        half_days_per_week=1,
        weekly_work_schedules=[
            AttendingWeeklyWorkSchedule(week=1, half_days=[assignment])
        ],
        vacation_ranges=[
            AttendingVacation(start_date=first_monday, end_date=first_monday)
        ],
    )

    assert (
        attending.work_half_day_on(
            first_monday,
            Session.MORNING,
            academic_year_start=first_monday,
            academic_year_end=first_monday + timedelta(days=6),
        )
        is None
    )


def test_weekly_work_is_effective_template_is_not_and_dated_work_takes_precedence() -> None:
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
        weekly_work_schedules=[
            AttendingWeeklyWorkSchedule(week=1, half_days=[precepting]),
            AttendingWeeklyWorkSchedule(week=3, half_days=[precepting]),
        ],
        ad_hoc_work_half_days=[
            AttendingAdHocWorkHalfDay(
                date=first_monday + timedelta(days=7),
                session=Session.MORNING,
                work_type=AttendingWorkType.ADMIN_TIME,
            )
        ],
        vacation_ranges=[
            AttendingVacation(
                start_date=first_monday + timedelta(days=14),
                end_date=first_monday + timedelta(days=14),
            )
        ],
    )

    scheduled = attending.work_half_day_on(
        first_monday,
        Session.MORNING,
        academic_year_start=first_monday,
        academic_year_end=first_monday + timedelta(days=30),
    )
    dated = attending.work_half_day_on(
        first_monday + timedelta(days=7),
        Session.MORNING,
        academic_year_start=first_monday,
        academic_year_end=first_monday + timedelta(days=30),
    )
    vacation = attending.work_half_day_on(
        first_monday + timedelta(days=14),
        Session.MORNING,
        academic_year_start=first_monday,
        academic_year_end=first_monday + timedelta(days=30),
    )
    template_only = attending.work_half_day_on(
        first_monday + timedelta(days=21),
        Session.MORNING,
        academic_year_start=first_monday,
        academic_year_end=first_monday + timedelta(days=30),
    )

    assert scheduled is not None
    assert scheduled.work_type is AttendingWorkType.PRECEPTING_CLINIC
    assert dated is not None
    assert dated.work_type is AttendingWorkType.ADMIN_TIME
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

    with pytest.raises(ValidationError, match="ad hoc work half-day.*outside academic year"):
        instance.revised(
            attendings=[
                Attending(
                    id="attending-001",
                    name="Ada Lovelace",
                    ad_hoc_work_half_days=[
                        AttendingAdHocWorkHalfDay(
                            date=last_day + timedelta(days=1),
                            session=Session.AFTERNOON,
                        )
                    ],
                )
            ]
        )

    with pytest.raises(ValidationError, match="weekly work schedule week.*outside"):
        instance.revised(
            attendings=[
                Attending(
                    id="attending-001",
                    name="Ada Lovelace",
                    weekly_work_schedules=[
                        AttendingWeeklyWorkSchedule(
                            week=instance.calendar.weeks + 1,
                        )
                    ],
                )
            ]
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
            weekly_work_schedules=[
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
        Attending(
            id="attending-002",
            name="Grace Hopper",
            half_days_per_week=0,
            ad_hoc_work_half_days=[
                AttendingAdHocWorkHalfDay(
                    date=first_day,
                    session=Session.MORNING,
                    work_type=AttendingWorkType.PRECEPTING_CLINIC,
                    clinic_id=clinic_id,
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


def test_attending_clinic_reference_must_exist_in_the_combined_workspace() -> None:
    instance = blank_instance()
    with pytest.raises(ValidationError, match="references unknown clinic"):
        instance.revised(
            attendings=[
                Attending(
                    id="attending-001",
                    name="Ada Lovelace",
                    half_days_per_week=1,
                    weekly_work_schedules=[
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
            ]
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


def test_ad_hoc_work_editor_helper_expands_all_day_to_am_and_pm() -> None:
    instance = blank_instance()
    work_date = instance.calendar.first_week_start + timedelta(days=2)

    half_days = add_ad_hoc_work_half_days(
        instance,
        [],
        date_value=work_date.isoformat(),
        session_value=AD_HOC_ALL_DAY,
    )

    assert [(half_day.date, half_day.session) for half_day in half_days] == [
        (work_date, Session.MORNING),
        (work_date, Session.AFTERNOON),
    ]
    with pytest.raises(ValidationError, match="must be unique"):
        add_ad_hoc_work_half_days(
            instance,
            half_days,
            date_value=work_date.isoformat(),
            session_value=Session.MORNING.value,
        )


def test_weekly_shift_target_helper_sets_replaces_and_removes_categories() -> None:
    targets = replace_weekly_shift_target(
        [],
        work_type_value=AttendingWorkType.ADMIN_TIME.value,
        mode_value=AttendingWeeklyTargetMode.FLEXIBLE.value,
        shifts_value=2,
    )
    targets = replace_weekly_shift_target(
        targets,
        work_type_value=AttendingWorkType.INPATIENT_SERVICE.value,
        mode_value=AttendingWeeklyTargetMode.FIXED.value,
        shifts_value=4,
    )
    targets = replace_weekly_shift_target(
        targets,
        work_type_value=AttendingWorkType.ADMIN_TIME.value,
        mode_value=AttendingWeeklyTargetMode.FIXED.value,
        shifts_value=1,
    )

    assert [target.work_type for target in targets] == [
        AttendingWorkType.INPATIENT_SERVICE,
        AttendingWorkType.ADMIN_TIME,
    ]
    assert targets[1].shifts_per_week == 1
    assert targets[1].mode is AttendingWeeklyTargetMode.FIXED

    targets = replace_weekly_shift_target(
        targets,
        work_type_value=AttendingWorkType.INPATIENT_SERVICE.value,
        mode_value=WEEKLY_SHIFT_TARGET_NONE,
        shifts_value=None,
    )
    assert [target.work_type for target in targets] == [AttendingWorkType.ADMIN_TIME]


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
        [],
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
        weekly_work_schedules=[
            AttendingWeeklyWorkSchedule(week=1, half_days=[precepting])
        ],
    )
    capacity_managed_edit = instance.revised(attendings=[attending])

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
    attending_managed_edit = attending_managed.revised(attendings=[attending])

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

    restored = SchedulerInput.model_validate(payload)

    assert restored.attendings == []


def test_ad_hoc_work_defaults_when_loading_an_earlier_attending_shape() -> None:
    payload = sample_instance().model_dump(mode="json")
    for attending in payload["attendings"]:
        attending.pop("ad_hoc_work_half_days")

    restored = SchedulerInput.model_validate(payload)

    assert all(not attending.ad_hoc_work_half_days for attending in restored.attendings)


def test_weekly_shift_targets_default_when_loading_an_earlier_attending_shape() -> None:
    payload = sample_instance().model_dump(mode="json")
    for attending in payload["attendings"]:
        attending.pop("weekly_shift_targets")

    restored = SchedulerInput.model_validate(payload)

    assert all(not attending.weekly_shift_targets for attending in restored.attendings)
