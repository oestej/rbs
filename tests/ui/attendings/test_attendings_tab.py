from datetime import timedelta

from rbs.catalog import blank_instance
from rbs.models.attending import (
    Attending,
    AttendingAdHocWorkHalfDay,
    AttendingVacation,
)
from rbs.models.enums import Session
from rbs.ui.attendings.tab import (
    NEW_ATTENDING_ID,
    _attending_form,
    _attending_view,
    _confirm_remove_attending,
    render_attendings_tab,
)
from rbs.ui.editor_guard import EditorGuard


def _created_elements(before: set[int]) -> list:
    from nicegui import ui

    return [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before and not element._deleted
    ]


def _click(button) -> None:
    next(iter(button._event_listeners.values())).handler(None)


def _element_texts(element) -> list[str]:
    texts = [element._text] if getattr(element, "_text", None) else []
    for slot in element.slots.values():
        for child in slot.children:
            texts.extend(_element_texts(child))
    return [str(text) for text in texts]


def test_attendings_tab_renders_an_empty_directory_and_new_action() -> None:
    from nicegui import ui

    instance = blank_instance()
    before = set(ui.context.client.elements)

    render_attendings_tab(
        instance,
        selected_attending_id=None,
        on_select=lambda _attending_id: None,
        on_save=lambda _instance, _attending_id: None,
    )

    created = _created_elements(before)
    labels = {getattr(element, "_text", None) for element in created}
    buttons = {
        element._props.get("label")
        for element in created
        if element.__class__.__name__ == "Button"
    }

    assert "Attendings" in labels
    assert (
        "Manage attending weekly schedules, schedule dates, ad hoc work, and vacation."
        in labels
    )
    assert "Attending directory" in labels
    assert "No attendings yet" in labels
    assert "Select an attending" in labels
    assert "New attending" in buttons
    assert any(
        "rbs-master-no-selection" in getattr(element, "_classes", [])
        for element in created
    )


def test_new_attending_form_exposes_full_year_defaults_and_uncapped_vacation() -> None:
    from nicegui import ui

    instance = blank_instance()
    before = set(ui.context.client.elements)

    render_attendings_tab(
        instance,
        selected_attending_id=NEW_ATTENDING_ID,
        on_select=lambda _attending_id: None,
        on_save=lambda _instance, _attending_id: None,
    )

    created = _created_elements(before)
    labels = {getattr(element, "_text", None) for element in created}
    inputs = {
        element._props.get("label"): element
        for element in created
        if element.__class__.__name__ == "Input"
    }
    checkboxes = {
        getattr(element, "_text", None): element
        for element in created
        if element.__class__.__name__ == "Checkbox"
    }
    numbers = {
        element._props.get("label"): element
        for element in created
        if element.__class__.__name__ == "Number"
    }

    assert "Add as many ranges as needed. Each range can start and end on any day." in labels
    assert "0 weekdays off" in labels
    assert "No ad hoc work" in labels
    assert {"Full name", "Schedule start date", "Schedule end date", "Work date"} <= set(
        inputs
    )
    assert numbers["Half-days per week"].value == 10
    assert numbers["Half-days per week"]._props.get("min") == 0
    assert numbers["Half-days per week"]._props.get("max") == 14
    assert inputs["Schedule start date"]._props.get("disable") is True
    assert inputs["Schedule end date"]._props.get("disable") is True
    assert checkboxes["Use academic year start"].value is True
    assert checkboxes["Use academic year end"].value is True


def test_attending_form_saves_zero_baseline_with_all_day_ad_hoc_work() -> None:
    from nicegui import ui

    instance = blank_instance()
    work_date = instance.calendar.first_week_start + timedelta(days=8)
    saved: list[tuple] = []
    before = set(ui.context.client.elements)
    _attending_form(
        instance,
        attending=None,
        on_cancel=lambda: None,
        on_save=lambda *args: saved.append(args),
    )

    created = _created_elements(before)
    inputs = {
        element._props.get("label"): element
        for element in created
        if element.__class__.__name__ == "Input"
    }
    buttons = {
        element._props.get("label"): element
        for element in created
        if element.__class__.__name__ == "Button"
        and element._props.get("label") is not None
    }
    half_days_per_week = next(
        element
        for element in created
        if element.__class__.__name__ == "Number"
        and element._props.get("label") == "Half-days per week"
    )
    work_time = next(
        element
        for element in created
        if element.__class__.__name__ == "Select"
        and element._props.get("label") == "Work time"
    )
    work_badge = next(
        element
        for element in created
        if element.__class__.__name__ == "Badge"
        and getattr(element, "_text", None) == "No ad hoc work"
    )
    inputs["Full name"].value = "Ada Lovelace"
    half_days_per_week.value = 0
    inputs["Work date"].value = work_date.isoformat()
    work_time.value = "all_day"

    _click(buttons["Add work half-day"])
    assert work_badge._text == "2 ad hoc half-days"
    _click(buttons["Add attending"])

    assert len(saved) == 1
    attending = saved[0][0].attendings[0]
    assert attending.half_days_per_week == 0
    assert [(half_day.date, half_day.session) for half_day in attending.ad_hoc_work_half_days] == [
        (work_date, Session.MORNING),
        (work_date, Session.AFTERNOON),
    ]


def test_attending_form_saves_custom_dates_and_arbitrary_vacation_days() -> None:
    from nicegui import ui

    instance = blank_instance()
    first_day = instance.calendar.first_week_start
    saved: list[tuple] = []
    before = set(ui.context.client.elements)
    _attending_form(
        instance,
        attending=None,
        on_cancel=lambda: None,
        on_save=lambda *args: saved.append(args),
    )

    created = _created_elements(before)
    inputs = {
        element._props.get("label"): element
        for element in created
        if element.__class__.__name__ == "Input"
    }
    checkboxes = {
        getattr(element, "_text", None): element
        for element in created
        if element.__class__.__name__ == "Checkbox"
    }
    buttons = {
        element._props.get("label"): element
        for element in created
        if element.__class__.__name__ == "Button"
        and element._props.get("label") is not None
    }
    half_days_per_week = next(
        element
        for element in created
        if element.__class__.__name__ == "Number"
        and element._props.get("label") == "Half-days per week"
    )
    weekday_badge = next(
        element
        for element in created
        if element.__class__.__name__ == "Badge"
        and getattr(element, "_text", None) == "0 weekdays off"
    )
    inputs["Full name"].value = "Ada Lovelace"
    half_days_per_week.value = 6
    checkboxes["Use academic year start"].value = False
    checkboxes["Use academic year end"].value = False
    inputs["Schedule start date"].value = (first_day + timedelta(days=3)).isoformat()
    inputs["Schedule end date"].value = (first_day + timedelta(days=300)).isoformat()
    inputs["Vacation start date"].value = (first_day + timedelta(days=9)).isoformat()
    inputs["Vacation end date"].value = (first_day + timedelta(days=13)).isoformat()

    _click(buttons["Add vacation"])
    assert weekday_badge._text == "3 weekdays off"
    _click(buttons["Add attending"])

    assert len(saved) == 1
    updated, attending_id = saved[0]
    attending = updated.attendings[0]
    assert attending_id == "attending-001"
    assert attending.name == "Ada Lovelace"
    assert attending.half_days_per_week == 6
    assert attending.schedule_start_date == first_day + timedelta(days=3)
    assert attending.schedule_end_date == first_day + timedelta(days=300)
    assert attending.vacation_ranges == [
        AttendingVacation(
            start_date=first_day + timedelta(days=9),
            end_date=first_day + timedelta(days=13),
        )
    ]


def test_missing_schedule_dates_have_distinct_boundary_cards() -> None:
    from nicegui import ui

    instance = blank_instance()
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
    )
    instance = instance.revised(attendings=[attending])
    before = set(ui.context.client.elements)

    _attending_view(
        instance,
        attending,
        on_edit=lambda: None,
        on_select=lambda _attending_id: None,
        on_save=lambda _instance, _attending_id: None,
    )

    created = _created_elements(before)
    labels = [getattr(element, "_text", None) for element in created]
    defaults = [
        element
        for element in created
        if "uses-academic-boundary" in getattr(element, "_classes", [])
    ]

    assert labels.count("No custom date — this follows the workspace calendar.") == 2
    assert "Academic year start" in labels
    assert "Academic year end" in labels
    assert "10 half-days per week" in labels
    assert len(defaults) == 2


def test_zero_baseline_attending_with_dated_work_is_labeled_ad_hoc_only() -> None:
    from nicegui import ui

    instance = blank_instance()
    work_date = instance.calendar.first_week_start + timedelta(days=1)
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=0,
        ad_hoc_work_half_days=[
            AttendingAdHocWorkHalfDay(
                date=work_date,
                session=Session.AFTERNOON,
            )
        ],
    )
    instance = instance.revised(attendings=[attending])
    before = set(ui.context.client.elements)

    _attending_view(
        instance,
        attending,
        on_edit=lambda: None,
        on_select=lambda _attending_id: None,
        on_save=lambda _instance, _attending_id: None,
    )

    created = _created_elements(before)
    labels = [getattr(element, "_text", None) for element in created]

    assert "Ad hoc only" in labels
    assert "1 ad hoc half-day" in labels
    assert "Afternoon (PM)" in labels
    assert any(
        "rbs-attending-work-row" in getattr(element, "_classes", [])
        for element in created
    )


def test_remove_attending_requires_confirmation_and_saves_the_removal() -> None:
    from nicegui import ui

    attending = Attending(id="attending-001", name="Ada Lovelace")
    instance = blank_instance().revised(attendings=[attending])
    saved: list[tuple] = []
    before = set(ui.context.client.elements)

    _confirm_remove_attending(
        instance,
        attending,
        on_save=lambda *args: saved.append(args),
    )

    created = _created_elements(before)
    confirm = next(
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Remove attending"
    )
    assert confirm._props.get("color") == "negative"

    _click(confirm)

    assert len(saved) == 1
    updated, attending_id = saved[0]
    assert updated.attendings == []
    assert attending_id is None


def test_attending_editor_close_protects_a_dirty_draft() -> None:
    from nicegui import ui

    attending = Attending(id="attending-001", name="Ada Lovelace")
    instance = blank_instance().revised(attendings=[attending])
    guard = EditorGuard()
    cancels: list[bool] = []
    before = set(ui.context.client.elements)

    _attending_form(
        instance,
        attending=attending,
        on_cancel=lambda: cancels.append(True),
        on_save=lambda _instance, _attending_id: None,
        guard=guard,
    )
    created = _created_elements(before)
    name = next(
        element
        for element in created
        if element.__class__.__name__ == "Input"
        and element._props.get("label") == "Full name"
    )
    close = next(
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and element._props.get("aria-label") == "Cancel attending editing"
    )
    name.value = "Ada Byron"
    clicked = set(ui.context.client.elements)

    _click(close)

    assert cancels == []
    confirmation = _created_elements(clicked)
    assert "Discard unsaved changes?" in {
        getattr(element, "_text", None) for element in confirmation
    }
    discard = next(
        element
        for element in confirmation
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Discard changes"
    )
    _click(discard)

    assert cancels == [True]
    assert guard.is_dirty is None


def test_attending_directory_selection_protects_a_dirty_draft() -> None:
    from nicegui import ui

    instance = blank_instance().revised(
        attendings=[
            Attending(id="attending-001", name="Ada Lovelace"),
            Attending(id="attending-002", name="Grace Hopper"),
        ]
    )
    selections: list[str | None] = []
    before = set(ui.context.client.elements)
    render_attendings_tab(
        instance,
        selected_attending_id="attending-001",
        on_select=selections.append,
        on_save=lambda _instance, _attending_id: None,
    )
    created = _created_elements(before)
    edit = next(
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Edit availability"
    )
    _click(edit)
    editing = _created_elements(before)
    name = next(
        element
        for element in editing
        if element.__class__.__name__ == "Input"
        and element._props.get("label") == "Full name"
    )
    grace = next(
        element
        for element in editing
        if element.__class__.__name__ == "Item"
        and "Grace Hopper" in _element_texts(element)
    )
    name.value = "Ada Byron"
    clicked = set(ui.context.client.elements)

    _click(grace)

    assert selections == []
    confirmation = _created_elements(clicked)
    discard = next(
        element
        for element in confirmation
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Discard changes"
    )
    _click(discard)

    assert selections == ["attending-002"]
