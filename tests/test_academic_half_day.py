"""An optional academic half-day: program-wide, and per week.

A program may run no protected teaching half-day at all, and any single week may
cancel its own — a conference week or a holiday that displaces teaching rather
than moving it.
"""

import pytest
from pydantic import ValidationError

from rbs.catalog import sample_instance
from rbs.models.case_blocks import AcademicHalfDayOverride
from rbs.models.enums import Session, Weekday
from rbs.models.rotation import ClinicSlot
from rbs.ui.clinic.board import is_academic as board_is_academic
from rbs.ui.clinic.ops import (
    cancel_academic_half_day_for_week,
    disable_academic_half_day,
    remove_academic_half_day_override,
    replace_academic_half_day,
    set_academic_half_day_override,
)

RECURRING = (Weekday.WEDNESDAY, Session.AFTERNOON)


def _no_overrides():
    return sample_instance().model_copy(update={"academic_half_day_overrides": []})


# ---- program-wide ------------------------------------------------------


def test_the_sample_program_runs_a_recurring_academic_half_day() -> None:
    instance = _no_overrides()

    assert instance.clinic_policy.academic_enabled
    assert instance.clinic_policy.recurring_academic_half_day == RECURRING
    assert instance.academic_half_day_for_week(5) == RECURRING


def test_the_academic_half_day_can_be_turned_off_program_wide() -> None:
    instance = disable_academic_half_day(_no_overrides())

    assert not instance.clinic_policy.academic_enabled
    assert instance.clinic_policy.recurring_academic_half_day is None
    assert instance.academic_half_day_for_week(5) is None
    assert not instance.is_academic_half_day(5, *RECURRING)


def test_turning_it_off_frees_the_slot_it_used_to_reserve() -> None:
    instance = disable_academic_half_day(_no_overrides())

    assert not board_is_academic(instance.clinic_policy, *RECURRING)
    for week in (1, instance.calendar.weeks):
        assert not instance.is_academic_half_day(week, *RECURRING)


def test_a_disabled_academic_half_day_never_matches_a_wildcard_clinic_slot() -> None:
    """An empty slot must not compare equal to 'any weekday, any session'."""
    policy = disable_academic_half_day(_no_overrides()).clinic_policy

    assert not policy.is_academic(ClinicSlot())
    assert not policy.is_academic(ClinicSlot(weekday=Weekday.WEDNESDAY))
    assert not policy.is_academic(ClinicSlot(session=Session.AFTERNOON))


def test_half_a_selection_is_rejected_program_wide() -> None:
    with pytest.raises(ValueError, match="both a day and a session"):
        replace_academic_half_day(_no_overrides(), Weekday.THURSDAY, None)


def test_a_resident_clinic_half_day_may_use_the_freed_slot() -> None:
    instance = disable_academic_half_day(_no_overrides())
    raw = instance.model_dump(mode="json")
    raw["residents"][0]["clinic_half_days"] = [
        {"weekday": Weekday.WEDNESDAY.value, "session": Session.AFTERNOON.value, "sites": []}
    ]

    restored = type(instance).model_validate(raw)

    assert restored.residents[0].clinic_half_days[0].weekday is Weekday.WEDNESDAY


def test_a_resident_clinic_half_day_still_cannot_overlap_a_live_academic_slot() -> None:
    instance = _no_overrides()
    raw = instance.model_dump(mode="json")
    raw["residents"][0]["clinic_half_days"] = [
        {"weekday": Weekday.WEDNESDAY.value, "session": Session.AFTERNOON.value, "sites": []}
    ]

    with pytest.raises(ValidationError, match="cannot overlap the recurring"):
        type(instance).model_validate(raw)


# ---- per week ----------------------------------------------------------


def test_one_week_can_cancel_its_academic_half_day() -> None:
    instance = cancel_academic_half_day_for_week(_no_overrides(), 7)

    assert instance.academic_half_day_for_week(7) is None
    assert not instance.is_academic_half_day(7, *RECURRING)
    assert instance.academic_half_day_for_week(8) == RECURRING
    assert instance.is_academic_half_day(8, *RECURRING)


def test_a_cancelled_week_is_distinct_from_having_no_override() -> None:
    instance = cancel_academic_half_day_for_week(_no_overrides(), 7)

    assert instance.has_academic_half_day_override(7)
    assert not instance.has_academic_half_day_override(8)

    restored = remove_academic_half_day_override(instance, 7)
    assert restored.academic_half_day_for_week(7) == RECURRING


def test_a_cancelling_override_is_exempt_from_the_different_day_rule() -> None:
    """Moving an override onto the recurring day is meaningless; cancelling is not."""
    instance = _no_overrides()

    with pytest.raises(ValidationError, match="must use a different day"):
        set_academic_half_day_override(instance, 7, *RECURRING)

    cancelled = cancel_academic_half_day_for_week(instance, 7)
    assert cancelled.academic_half_day_for_week(7) is None


def test_one_week_can_schedule_teaching_while_the_program_runs_none() -> None:
    instance = disable_academic_half_day(_no_overrides())

    scheduled = set_academic_half_day_override(
        instance,
        9,
        Weekday.TUESDAY,
        Session.MORNING,
    )

    assert scheduled.academic_half_day_for_week(9) == (Weekday.TUESDAY, Session.MORNING)
    assert scheduled.academic_half_day_for_week(10) is None


def test_half_a_selection_is_rejected_for_a_week() -> None:
    with pytest.raises(ValueError, match="both a day and a session"):
        set_academic_half_day_override(_no_overrides(), 7, None, Session.MORNING)

    with pytest.raises(ValidationError, match="both a day and a session"):
        AcademicHalfDayOverride.model_validate({"week": 7, "weekday": "monday"})


def test_a_cancelling_override_round_trips_through_json() -> None:
    instance = cancel_academic_half_day_for_week(_no_overrides(), 7)

    restored = type(instance).model_validate(instance.model_dump(mode="json"))

    override = restored.academic_half_day_overrides[0]
    assert override.week == 7
    assert override.cancels_week
    assert restored.academic_half_day_for_week(7) is None


# ---- the solver sees it ------------------------------------------------


def test_a_disabled_academic_half_day_opens_the_slot_in_the_clinic_model() -> None:
    from ortools.sat.python import cp_model

    from rbs.solver.core.compile import compile_problem

    instance = disable_academic_half_day(_no_overrides())

    problem = compile_problem(instance, instance.solver, cp_model)
    entries = [
        entry
        for (_resident_id, week), week_entries in problem.clinic.in_clinic.items()
        if week == 11
        for entry in week_entries
    ]

    assert entries
    assert any(
        weekday is Weekday.WEDNESDAY and session is Session.AFTERNOON
        for _keys, weekday, session, _lit in entries
    )


def test_a_cancelled_week_opens_the_slot_only_for_that_week() -> None:
    from ortools.sat.python import cp_model

    from rbs.solver.core.compile import compile_problem

    instance = cancel_academic_half_day_for_week(_no_overrides(), 11)

    problem = compile_problem(instance, instance.solver, cp_model)

    def slots(week: int):
        return [
            (weekday, session)
            for (_resident_id, entry_week), entries in problem.clinic.in_clinic.items()
            if entry_week == week
            for _keys, weekday, session, _lit in entries
        ]

    assert RECURRING in slots(11)
    assert RECURRING not in slots(12)
