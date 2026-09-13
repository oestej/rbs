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
)
from rbs.models.enums import Session
from rbs.models.instance import SchedulerInput, SolverProblem
from rbs.ui.attendings.ops import (
    AD_HOC_ALL_DAY,
    add_ad_hoc_work_half_days,
    add_attending,
    add_vacation_range,
    next_attending_id,
    remove_attending,
    replace_attending,
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


def test_attending_setup_preserves_current_resident_schedule_for_now() -> None:
    instance = blank_instance()
    edited = instance.revised(
        attendings=[Attending(id="attending-001", name="Ada Lovelace")]
    )

    assert (
        instance_edit_impact(instance, edited)
        is InstanceEditImpact.COMPATIBLE_CONFIGURATION
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
