from datetime import timedelta

import pytest

from rbs.catalog import sample_instance
from rbs.ui.residents.time_away_calendar import time_away_calendar


def _click(button):
    listener = next(
        listener for listener in button._event_listeners.values() if listener.type == "click"
    )
    listener.handler(None)


@pytest.mark.parametrize("vacation", [True, False])
def test_calendar_selection_navigation_and_reset(vacation):
    from nicegui import ui

    before = set(ui.context.client.elements)
    picker = time_away_calendar(sample_instance(), vacation=vacation)

    def created():
        return [
            element for key, element in ui.context.client.elements.items()
            if key not in before and not element.is_deleted
        ]

    def button(label):
        return next(element for element in created() if element._props.get("aria-label") == label)

    assert button("Previous month")._props.get("disable") is True
    _click(button("Next month"))
    assert any(getattr(element, "text", None) == "July 2026" for element in created())
    choice = "Monday" if vacation else "day off"
    selected = "Jul 06, 2026" if vacation else "Jul 07, 2026"
    _click(button(f"Choose {choice} {selected}"))
    assert picker.selected_date.value == ("2026-07-06" if vacation else "2026-07-07")
    selected_class = "is-selected-monday" if vacation else "is-selected-day"
    assert sum(selected_class in element._classes for element in created()) == 1
    selected_week_days = sum("is-selected-week" in element._classes for element in created())
    assert selected_week_days == (7 if vacation else 0)
    if vacation:
        assert not any(
            element._props.get("aria-label") == "Choose Monday Jul 07, 2026"
            for element in created()
        )

    picker.reset()
    assert picker.selected_date.value is None
    assert not any(selected_class in element._classes for element in created())
    assert not any("is-selected-week" in element._classes for element in created())
    assert any(getattr(element, "text", None) == "July 2026" for element in created())

    # Navigate across the year boundary to the final month of the academic year.
    for _ in range(11):
        _click(button("Next month"))
    assert any(getattr(element, "text", None) == "June 2027" for element in created())
    assert button("Next month")._props.get("disable") is True
    last_monday = sample_instance().calendar.first_week_start + timedelta(weeks=51)
    last_day = last_monday if vacation else last_monday + timedelta(days=6)
    _click(button(f"Choose {choice} {last_day:%b %d, %Y}"))
    assert picker.selected_date.value == last_day.isoformat()
    assert not any(
        element._props.get("aria-label") == f"Choose {choice} Jun 28, 2027"
        for element in created()
    )


@pytest.mark.parametrize("vacation", [True, False])
def test_time_away_editors_add_reject_duplicates_and_remove(vacation, monkeypatch):
    from nicegui import ui

    from rbs.ui.residents.tab import _days_off_editor, _vacation_week_editor

    before = set(ui.context.client.elements)
    notices = []
    monkeypatch.setattr(ui, "notify", lambda message, **_kwargs: notices.append(message))
    instance = sample_instance()
    editor = _vacation_week_editor if vacation else _days_off_editor
    draft = editor(instance, [])

    def created():
        return [
            element for key, element in ui.context.client.elements.items()
            if key not in before and not element.is_deleted
        ]

    selected_date = next(element for element in created() if element.__class__.__name__ == "Input")
    add = next(
        element for element in created()
        if getattr(element, "text", None) == ("Add week" if vacation else "Add day")
    )
    selected = instance.calendar.first_week_start + timedelta(days=0 if vacation else 1)
    expected = 1 if vacation else selected
    selected_date.set_value(selected.isoformat())
    _click(add)
    assert draft == [expected]
    assert selected_date.value is None

    selected_date.set_value(selected.isoformat())
    _click(add)
    assert draft == [expected]
    assert "already selected" in notices[-1]
    assert selected_date.value == selected.isoformat()

    chip = next(element for element in created() if element.__class__.__name__ == "Chip")
    chip.set_value(False)
    assert draft == []
