"""Week-1 anchor choices and rebasing."""

from datetime import date, timedelta

import pytest

from rbs.academic_year import (
    first_week_start_for_academic_year,
    rebase_week_start,
    start_new_academic_year,
    week_start_choices,
)
from rbs.catalog import sample_instance
from rbs.models.case_blocks import ManualClinicBlock
from rbs.models.clinic_site import ClinicCapacityOverride
from rbs.models.enums import Session


def test_week_start_choices_center_on_the_july_anchor() -> None:
    selected = date(2026, 6, 29)

    choices = week_start_choices(selected, academic_year="2026-2027")

    assert len(choices) == 9
    assert all(day.weekday() == 0 for day in choices)
    assert first_week_start_for_academic_year("2026-2027") in choices
    assert selected in choices


def test_week_start_choices_retain_an_out_of_window_value() -> None:
    far = date(2026, 1, 5)

    choices = week_start_choices(far, academic_year="2026-2027")

    assert far in choices


def test_rebase_week_start_moves_only_the_anchor() -> None:
    instance = sample_instance()
    day_off = instance.calendar.first_week_start + timedelta(days=30)
    resident = instance.residents[0].model_copy(update={"days_off": [day_off]})
    instance = instance.revised(residents=[resident, *instance.residents[1:]])
    new_start = instance.calendar.first_week_start + timedelta(weeks=1)

    moved = rebase_week_start(instance, new_start)

    assert moved.calendar.first_week_start == new_start
    assert moved.residents[0].days_off == [day_off]


def test_rebase_week_start_is_a_no_op_for_the_current_anchor() -> None:
    instance = sample_instance()

    assert rebase_week_start(instance, instance.calendar.first_week_start) is instance


def test_rebase_week_start_rejects_a_non_monday() -> None:
    instance = sample_instance()
    tuesday = instance.calendar.first_week_start + timedelta(days=1)

    with pytest.raises(ValueError, match="Monday"):
        rebase_week_start(instance, tuesday)


def test_start_new_academic_year_clears_year_specific_work() -> None:
    instance = sample_instance()
    resident = instance.residents[2]
    maple = instance.clinic_policy.site("maple").revised(
        capacity_overrides=[
            ClinicCapacityOverride(
                date=instance.calendar.first_week_start,
                session=Session.MORNING,
                attendings=2,
            )
        ]
    )
    policy = instance.clinic_policy.revised(
        sites=[
            maple if site.id == maple.id else site
            for site in instance.clinic_policy.sites
        ]
    )
    configured = instance.revised(
        clinic_policy=policy,
        manual_clinic_blocks=[
            ManualClinicBlock(
                resident_id=resident.id,
                rotation_id="clinic",
                start_week=1,
                duration_weeks=2,
                replaces_rotation_id="elective",
            )
        ],
    )

    moved = start_new_academic_year(configured, "2027-2028")

    assert moved.academic_year == "2027-2028"
    assert moved.calendar.first_week_start == date(2027, 6, 28)
    assert all(not item.vacation_weeks for item in moved.residents)
    assert all(not item.days_off for item in moved.residents)
    assert not moved.academic_half_day_overrides
    assert not moved.locks
    assert not moved.manual_clinic_blocks
    assert not moved.special_rotations
    assert not moved.clinic_policy.closure_days
    assert all(not site.closure_days for site in moved.clinic_policy.sites)
    assert all(not site.capacity_overrides for site in moved.clinic_policy.sites)

    assert [item.id for item in moved.residents] == [
        item.id for item in configured.residents
    ]
    assert moved.rotations == configured.rotations
    assert moved.requirements == configured.requirements
    assert moved.clinic_policy.academic == configured.clinic_policy.academic
    assert moved.clinic_policy.allocation_rules == configured.clinic_policy.allocation_rules


def test_start_new_academic_year_is_a_no_op_for_the_current_year() -> None:
    instance = sample_instance()

    assert start_new_academic_year(instance, instance.academic_year) is instance
