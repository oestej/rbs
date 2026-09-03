"""Week-1 anchor choices and rebasing."""

from datetime import date, timedelta

import pytest

from rbs.academic_year import (
    first_week_start_for_academic_year,
    rebase_week_start,
    week_start_choices,
)
from rbs.catalog import sample_instance


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
