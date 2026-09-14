from datetime import timedelta

from rbs.catalog import blank_instance
from rbs.models.attending import (
    ATTENDING_WEEKLY_TARGET_WORK_TYPES,
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
    next(
        listener
        for listener in button._event_listeners.values()
        if listener.type == "click"
    ).handler(None)


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
        "Manage week-by-week attending schedules, preferred weekly patterns, category "
        "targets, schedule dates, weekly overrides, and vacation."
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


def test_attending_directory_uses_the_compact_person_card_setup() -> None:
    from nicegui import ui

    instance = blank_instance()
    first_day = instance.calendar.first_week_start
    instance = instance.revised(
        attendings=[
            Attending(
                id="attending-001",
                name="Ada Lovelace",
                schedule_start_date=first_day + timedelta(days=1),
                vacation_ranges=[
                    AttendingVacation(
                        start_date=first_day + timedelta(days=1),
                        end_date=first_day + timedelta(days=3),
                    )
                ],
            )
        ]
    )
    before = set(ui.context.client.elements)

    render_attendings_tab(
        instance,
        selected_attending_id=None,
        on_select=lambda _attending_id: None,
        on_save=lambda _instance, _attending_id: None,
    )

    created = _created_elements(before)
    item = next(element for element in created if element.__class__.__name__ == "Item")
    item_texts = _element_texts(item)
    summary = next(
        element
        for element in created
        if "rbs-person-directory-summary" in getattr(element, "_classes", [])
    )

    assert "rbs-person-directory-item" in item._classes
    assert item_texts == [
        "AL",
        "Ada Lovelace",
        "10 half-days per week · 3 weekdays off",
    ]
    assert summary._props.get("caption") is True


def test_new_attending_form_is_limited_to_basic_schedule_details() -> None:
    from nicegui import ui

    instance = blank_instance()
    saved: list[tuple] = []
    before = set(ui.context.client.elements)

    render_attendings_tab(
        instance,
        selected_attending_id=NEW_ATTENDING_ID,
        on_select=lambda _attending_id: None,
        on_save=lambda *args: saved.append(args),
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
    assert (
        "After adding this attending, use their editor to configure category targets, "
        "a preferred weekly schedule, weekly work, a reusable template, and vacation."
        in labels
    )
    assert {"Full name", "Schedule start date", "Schedule end date"} <= set(inputs)
    assert "Work date" not in inputs
    assert "Vacation start date" not in inputs
    assert "Vacation end date" not in inputs
    assert numbers["Half-days per week"].value == 10
    assert numbers["Half-days per week"]._props.get("min") == 0
    assert numbers["Half-days per week"]._props.get("max") == 14
    assert inputs["Schedule start date"]._props.get("disable") is True
    assert inputs["Schedule end date"]._props.get("disable") is True
    assert checkboxes["Use academic year start"].value is True
    assert checkboxes["Use academic year end"].value is True
    assert not any(element.__class__.__name__ == "Tab" for element in created)
    assert not any(element.__class__.__name__ == "TabPanels" for element in created)
    assert not any(element.__class__.__name__ == "Select" for element in created)

    inputs["Full name"].value = "Ada Lovelace"
    add = next(
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Add attending"
    )
    _click(add)

    added = saved[0][0].attendings[0]
    assert added.half_days_per_week == 10
    assert added.schedule_start_date is None
    assert added.schedule_end_date is None
    assert added.weekly_shift_targets == []
    assert added.minimum_attending_clinic_days_per_week == 0
    assert added.preferred_weekly_schedule_half_days == []
    assert saved[0][0].attending_schedules == []
    assert added.schedule_template_half_days == []
    assert added.vacation_ranges == []


def test_attending_form_overrides_a_zero_baseline_with_the_assigned_count() -> None:
    from nicegui import ui

    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=0,
    )
    instance = blank_instance().revised(attendings=[attending])
    saved: list[tuple] = []
    before = set(ui.context.client.elements)
    _attending_form(
        instance,
        attending=attending,
        on_cancel=lambda: None,
        on_save=lambda *args: saved.append(args),
    )

    created = _created_elements(before)
    buttons = {
        element._props.get("label"): element
        for element in created
        if element.__class__.__name__ == "Button"
        and element._props.get("label") is not None
    }
    weekly_cell = next(
        element
        for element in created
        if element._props.get("aria-label")
        == "Assign Monday Morning (AM) in week 1"
    )
    assigned_badge = next(
        element
        for element in created
        if element.__class__.__name__ == "Badge"
        and getattr(element, "_text", None) == "1 of 0 half-days assigned"
    )

    before_dialog = set(ui.context.client.elements)
    _click(weekly_cell)
    dialog_elements = _created_elements(before_dialog)
    work_type = next(
        element
        for element in dialog_elements
        if element.__class__.__name__ == "Select"
        and element._props.get("label") == "Work type"
    )
    description = next(
        element
        for element in dialog_elements
        if element.__class__.__name__ == "Input"
        and element._props.get("label") == "Description"
    )
    assign = next(
        element
        for element in dialog_elements
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Assign half-day"
    )
    work_type.value = AttendingWorkType.SPECIAL_OTHER.value
    description.value = "Credentialing committee"
    _click(assign)

    assert assigned_badge._text == "2 of 0 half-days assigned"
    _click(buttons["Override"])
    assert any(
        element.__class__.__name__ == "Badge"
        and getattr(element, "_text", None) == "2 of 2 half-days assigned"
        for element in _created_elements(before)
    )
    assert any(
        element.__class__.__name__ == "Badge"
        and getattr(element, "_text", None) == "Week override"
        for element in _created_elements(before)
    )
    _click(buttons["Save changes"])

    assert len(saved) == 1
    saved_instance = saved[0][0]
    attending = saved_instance.attendings[0]
    assert attending.half_days_per_week == 0
    accepted_schedule = saved_instance.attending_schedule_for(attending.id)
    assert accepted_schedule is not None
    week = accepted_schedule.weeks[0]
    assert week.half_days_override == 2
    assert [(half_day.weekday, half_day.session) for half_day in week.half_days] == [
        (Weekday.MONDAY, Session.MORNING)
    ]
    assert week.half_days[0].description == "Credentialing committee"


def test_attending_form_saves_custom_dates_and_arbitrary_vacation_days() -> None:
    from nicegui import ui

    attending = Attending(id="attending-001", name="Ada Lovelace")
    instance = blank_instance().revised(attendings=[attending])
    first_day = instance.calendar.first_week_start
    saved: list[tuple] = []
    before = set(ui.context.client.elements)
    _attending_form(
        instance,
        attending=attending,
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
    half_days_per_week.value = 6
    checkboxes["Use academic year start"].value = False
    checkboxes["Use academic year end"].value = False
    inputs["Schedule start date"].value = (first_day + timedelta(days=3)).isoformat()
    inputs["Schedule end date"].value = (first_day + timedelta(days=300)).isoformat()
    inputs["Vacation start date"].value = (first_day + timedelta(days=9)).isoformat()
    inputs["Vacation end date"].value = (first_day + timedelta(days=13)).isoformat()

    _click(buttons["Add vacation"])
    assert weekday_badge._text == "3 weekdays off"
    updated_labels = {
        getattr(element, "_text", None) for element in _created_elements(before)
    }
    assert "1 vacation range · 3 weekdays off total" in updated_labels
    assert not any(
        isinstance(label, str) and "calendar day" in label
        for label in updated_labels
    )
    _click(buttons["Save changes"])

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


def test_vacation_removes_and_restores_automatic_academic_admin_in_editor() -> None:
    from nicegui import ui

    instance = blank_instance()
    academic_day = instance.calendar.first_week_start + timedelta(days=2)
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        vacation_ranges=[
            AttendingVacation(start_date=academic_day, end_date=academic_day)
        ],
    )
    instance = instance.revised(attendings=[attending])
    before = set(ui.context.client.elements)
    _attending_form(
        instance,
        attending=attending,
        on_cancel=lambda: None,
        on_save=lambda _instance, _attending_id: None,
    )

    created = _created_elements(before)
    assert "Admin Time · Academic half-day" not in {
        getattr(element, "_text", None) for element in created
    }
    assert any(
        element.__class__.__name__ == "Badge"
        and getattr(element, "_text", None) == "0 of 10 half-days assigned"
        for element in created
    )
    remove_vacation = next(
        element
        for element in created
        if str(element._props.get("aria-label", "")).startswith(
            "Remove vacation range"
        )
    )
    _click(remove_vacation)

    updated = _created_elements(before)
    assert "Admin Time · Academic half-day" in {
        getattr(element, "_text", None) for element in updated
    }
    assert any(
        element.__class__.__name__ == "Badge"
        and getattr(element, "_text", None) == "1 of 10 half-days assigned"
        for element in updated
    )


def test_attending_form_saves_fixed_and_flexible_target_ranges() -> None:
    from nicegui import ui

    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=8,
    )
    instance = blank_instance().revised(attendings=[attending])
    saved: list[tuple] = []
    before = set(ui.context.client.elements)
    _attending_form(
        instance,
        attending=attending,
        on_cancel=lambda: None,
        on_save=lambda *args: saved.append(args),
    )

    created = _created_elements(before)
    target_types = {
        element._props.get("aria-label"): element
        for element in created
        if element.__class__.__name__ == "Select"
        and str(element._props.get("aria-label", "")).endswith("target type")
    }
    target_minimums = {
        element._props.get("aria-label"): element
        for element in created
        if element.__class__.__name__ == "Number"
        and "minimum shifts per week" in str(element._props.get("aria-label", ""))
    }
    target_maximums = {
        element._props.get("aria-label"): element
        for element in created
        if element.__class__.__name__ == "Number"
        and "maximum shifts per week" in str(element._props.get("aria-label", ""))
    }
    assert len(target_types) == len(ATTENDING_WEEKLY_TARGET_WORK_TYPES)
    assert all(target.value == "none" for target in target_types.values())
    assert all(count._props.get("disable") is True for count in target_minimums.values())
    assert all(count._props.get("disable") is True for count in target_maximums.values())
    assert "Special/Other target type" not in target_types
    clinic_day_minimum = next(
        element
        for element in created
        if element.__class__.__name__ == "Number"
        and element._props.get("label") == "Minimum Attending Clinic days per week"
    )
    assert clinic_day_minimum.value == 0
    assert clinic_day_minimum._props.get("max") == 7

    clinic_day_minimum.value = 2
    target_types["Precepting Clinic target type"].value = "fixed"
    target_minimums["Precepting Clinic minimum shifts per week"].value = 2
    target_maximums["Precepting Clinic maximum shifts per week"].value = 3
    target_types["Admin Time target type"].value = "flexible"
    target_minimums["Admin Time minimum shifts per week"].value = 1
    target_maximums["Admin Time maximum shifts per week"].value = 2
    save = next(
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Save changes"
    )
    _click(save)

    assert len(saved) == 1
    saved_attending = saved[0][0].attendings[0]
    assert saved_attending.minimum_attending_clinic_days_per_week == 2
    targets = saved_attending.weekly_shift_targets
    assert [target.work_type for target in targets] == [
        AttendingWorkType.PRECEPTING_CLINIC,
        AttendingWorkType.ADMIN_TIME,
    ]
    assert [
        (
            target.minimum_shifts_per_week,
            target.maximum_shifts_per_week,
            target.mode,
        )
        for target in targets
    ] == [
        (2, 3, AttendingWeeklyTargetMode.FIXED),
        (1, 2, AttendingWeeklyTargetMode.FLEXIBLE),
    ]


def test_attending_form_assigns_typed_work_to_one_academic_week() -> None:
    from nicegui import ui

    attending = Attending(id="attending-001", name="Ada Lovelace")
    instance = blank_instance().revised(attendings=[attending])
    saved: list[tuple] = []
    before = set(ui.context.client.elements)
    _attending_form(
        instance,
        attending=attending,
        on_cancel=lambda: None,
        on_save=lambda *args: saved.append(args),
    )

    created = _created_elements(before)
    name = next(
        element
        for element in created
        if element.__class__.__name__ == "Input"
        and element._props.get("label") == "Full name"
    )
    weekly_cell = next(
        element
        for element in created
        if element._props.get("aria-label")
        == "Assign Monday Morning (AM) in week 1"
    )
    add = next(
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Save changes"
    )
    name.value = "Ada Lovelace"

    before_dialog = set(ui.context.client.elements)
    _click(weekly_cell)
    dialog_elements = _created_elements(before_dialog)
    weekly_work_type = next(
        element
        for element in dialog_elements
        if element.__class__.__name__ == "Select"
        and element._props.get("label") == "Work type"
    )
    assign = next(
        element
        for element in dialog_elements
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Assign half-day"
    )
    weekly_work_type.value = AttendingWorkType.PRECEPTING_CLINIC.value
    _click(assign)

    _click(add)

    assert len(saved) == 1
    saved_instance = saved[0][0]
    attending = saved_instance.attendings[0]
    accepted_schedule = saved_instance.attending_schedule_for(attending.id)
    assert accepted_schedule is not None
    assert accepted_schedule.weeks[0].week == 1
    assignment = accepted_schedule.weeks[0].half_days[0]
    assert assignment.weekday is Weekday.MONDAY
    assert assignment.session is Session.MORNING
    assert assignment.work_type is AttendingWorkType.PRECEPTING_CLINIC
    assert assignment.clinic_id == instance.clinic_policy.primary_site_id


def test_attending_form_saves_a_soft_preferred_weekly_schedule() -> None:
    from nicegui import ui

    attending = Attending(id="attending-001", name="Ada Lovelace")
    instance = blank_instance().revised(attendings=[attending])
    saved: list[tuple] = []
    before = set(ui.context.client.elements)
    _attending_form(
        instance,
        attending=attending,
        on_cancel=lambda: None,
        on_save=lambda *args: saved.append(args),
    )

    created = _created_elements(before)
    preferred_cell = next(
        element
        for element in created
        if element._props.get("aria-label")
        == "Assign Tuesday Morning (AM) in preferred weekly schedule"
    )
    save_attending = next(
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Save changes"
    )

    before_dialog = set(ui.context.client.elements)
    _click(preferred_cell)
    dialog_elements = _created_elements(before_dialog)
    work_type = next(
        element
        for element in dialog_elements
        if element.__class__.__name__ == "Select"
        and element._props.get("label") == "Work type"
    )
    assert AttendingWorkType.SPECIAL_OTHER.value not in work_type.options
    work_type.value = AttendingWorkType.ATTENDING_CLINIC.value
    assign = next(
        element
        for element in dialog_elements
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Assign half-day"
    )
    _click(assign)
    _click(save_attending)

    saved_attending = saved[0][0].attendings[0]
    assert saved[0][0].attending_schedules == []
    assert saved_attending.preferred_weekly_schedule_half_days == [
        AttendingWorkHalfDay(
            weekday=Weekday.TUESDAY,
            session=Session.MORNING,
            work_type=AttendingWorkType.ATTENDING_CLINIC,
        )
    ]


def test_schedule_template_applies_independent_copies_to_a_week_range() -> None:
    from nicegui import ui

    attending = Attending(id="attending-001", name="Ada Lovelace")
    instance = blank_instance().revised(attendings=[attending])
    saved: list[tuple] = []
    before = set(ui.context.client.elements)
    _attending_form(
        instance,
        attending=attending,
        on_cancel=lambda: None,
        on_save=lambda *args: saved.append(args),
    )

    created = _created_elements(before)
    name = next(
        element
        for element in created
        if element.__class__.__name__ == "Input"
        and element._props.get("label") == "Full name"
    )
    template_cell = next(
        element
        for element in created
        if element._props.get("aria-label")
        == "Assign Monday Morning (AM) in schedule template"
    )
    week_selects = {
        element._props.get("label"): element
        for element in created
        if element.__class__.__name__ == "Select"
        and element._props.get("label") in {"First week", "Last week"}
    }
    buttons = {
        element._props.get("label"): element
        for element in created
        if element.__class__.__name__ == "Button"
        and element._props.get("label") is not None
    }
    name.value = "Ada Lovelace"

    before_dialog = set(ui.context.client.elements)
    _click(template_cell)
    dialog_elements = _created_elements(before_dialog)
    template_work_type = next(
        element
        for element in dialog_elements
        if element.__class__.__name__ == "Select"
        and element._props.get("label") == "Work type"
    )
    assign = next(
        element
        for element in dialog_elements
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Assign half-day"
    )
    template_work_type.value = AttendingWorkType.ADMIN_TIME.value
    _click(assign)
    week_selects["First week"].value = 2
    week_selects["Last week"].value = 3

    _click(buttons["Apply template to range"])
    _click(buttons["Save changes"])

    saved_instance = saved[0][0]
    attending = saved_instance.attendings[0]
    assert len(attending.schedule_template_half_days) == 1
    accepted_schedule = saved_instance.attending_schedule_for(attending.id)
    assert accepted_schedule is not None
    assert [week.week for week in accepted_schedule.weeks] == [2, 3]
    assert all(
        week.half_days == attending.schedule_template_half_days
        for week in accepted_schedule.weeks
    )


def test_attending_work_boards_reuse_clickable_draggable_resident_grid() -> None:
    from nicegui import ui

    weekly = AttendingWorkHalfDay(
        weekday=Weekday.MONDAY,
        session=Session.MORNING,
        work_type=AttendingWorkType.ADMIN_TIME,
    )
    template = AttendingWorkHalfDay(
        weekday=Weekday.TUESDAY,
        session=Session.AFTERNOON,
        work_type=AttendingWorkType.SPECIAL_OTHER,
        description="Faculty meeting",
    )
    preferred = AttendingWorkHalfDay(
        weekday=Weekday.THURSDAY,
        session=Session.MORNING,
        work_type=AttendingWorkType.ATTENDING_CLINIC,
    )
    instance = blank_instance()
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        preferred_weekly_schedule_half_days=[preferred],
        schedule_template_half_days=[template],
    )
    instance = instance.revised(
        attendings=[attending],
        attending_schedules=[
            AttendingSchedule(
                attending_id=attending.id,
                weeks=[
            AttendingWeeklyWorkSchedule(week=1, half_days=[weekly])
                ],
            )
        ],
    )
    before = set(ui.context.client.elements)

    _attending_form(
        instance,
        attending=attending,
        on_cancel=lambda: None,
        on_save=lambda _instance, _attending_id: None,
    )

    created = _created_elements(before)
    cells = [
        element
        for element in created
        if "rbs-resident-clinic-session-cell" in getattr(element, "_classes", [])
    ]
    events = [
        element
        for element in created
        if "rbs-resident-clinic-event" in getattr(element, "_classes", [])
    ]
    tabs = {
        element._props.get("label")
        for element in created
        if element.__class__.__name__ == "Tab"
    }

    assert len(cells) == 42
    assert tabs == {
        "Details",
        "Schedule",
        "Targets",
        "Preferences",
        "Template",
        "Vacation",
    }
    assert any(element.__class__.__name__ == "TabPanels" for element in created)
    assert {event._props.get("data-scope") for event in events} == {
        "attending-week-1",
        "attending-preference",
        "attending-template",
    }
    assert [event._props.get("draggable") for event in events].count("false") == 1
    assert [event._props.get("draggable") for event in events].count("true") == 3
    assert "Special/Other · Faculty meeting" in [
        getattr(element, "_text", None) for element in created
    ]
    assert "Admin Time · Academic half-day" in [
        getattr(element, "_text", None) for element in created
    ]
    assert {
        element._props.get("aria-label")
        for element in cells
        if element._props.get("aria-label")
    } >= {
        "Assign Tuesday Morning (AM) in week 1",
        "Assign Monday Morning (AM) in preferred weekly schedule",
        "Assign Monday Morning (AM) in schedule template",
    }


def test_missing_schedule_dates_have_distinct_boundary_cards() -> None:
    from nicegui import ui

    instance = blank_instance()
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        vacation_ranges=[
            AttendingVacation(
                start_date=instance.calendar.first_week_start + timedelta(days=9),
                end_date=instance.calendar.first_week_start + timedelta(days=13),
            )
        ],
    )
    instance = instance.revised(attendings=[attending])
    before = set(ui.context.client.elements)

    _attending_view(
        instance,
        attending,
        on_edit=lambda: None,
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
    assert "3 weekdays off" in labels
    assert not any(
        isinstance(label, str) and "calendar day" in label for label in labels
    )
    assert len(defaults) == 2
    assert not any(
        element._props.get("aria-label") == "Back to attending directory"
        for element in created
    )


def test_attending_view_shows_configured_category_targets_and_modes() -> None:
    from nicegui import ui

    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        minimum_attending_clinic_days_per_week=2,
        preferred_weekly_schedule_half_days=[
            AttendingWorkHalfDay(
                weekday=Weekday.THURSDAY,
                session=Session.MORNING,
                work_type=AttendingWorkType.ATTENDING_CLINIC,
            )
        ],
        weekly_shift_targets=[
            AttendingWeeklyShiftTarget(
                work_type=AttendingWorkType.PRECEPTING_CLINIC,
                minimum_shifts_per_week=2,
                maximum_shifts_per_week=3,
                mode=AttendingWeeklyTargetMode.FIXED,
            ),
            AttendingWeeklyShiftTarget(
                work_type=AttendingWorkType.ADMIN_TIME,
                minimum_shifts_per_week=0,
                maximum_shifts_per_week=1,
                mode=AttendingWeeklyTargetMode.FLEXIBLE,
            ),
        ],
    )
    instance = blank_instance().revised(attendings=[attending])
    before = set(ui.context.client.elements)

    _attending_view(
        instance,
        attending,
        on_edit=lambda: None,
        on_save=lambda _instance, _attending_id: None,
    )

    labels = [
        getattr(element, "_text", None) for element in _created_elements(before)
    ]
    assert "Weekly category targets" in labels
    assert "2 of 4 categories targeted" in labels
    assert "Precepting Clinic" in labels
    assert "2–3 shifts per week" in labels
    assert "Fixed" in labels
    assert "Admin Time" in labels
    assert "0–1 shifts per week" in labels
    assert "Flexible" in labels
    assert "At least 2 Attending Clinic days per full non-vacation week" in labels
    assert "Preferred weekly schedule" in labels
    assert "1 of 10 preferred half-days" in labels
    assert "Thursday · Morning (AM)" in labels
    assert "Attending Clinic" in labels


def test_zero_baseline_attending_shows_its_weekly_override() -> None:
    from nicegui import ui

    instance = blank_instance()
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=0,
    )
    instance = instance.revised(
        attendings=[attending],
        attending_schedules=[
            AttendingSchedule(
                attending_id=attending.id,
                weeks=[
                    AttendingWeeklyWorkSchedule(
                        week=1,
                        half_days_override=2,
                        half_days=[
                            AttendingWorkHalfDay(
                                weekday=Weekday.TUESDAY,
                                session=Session.AFTERNOON,
                            )
                        ],
                    )
                ],
            )
        ],
    )
    before = set(ui.context.client.elements)

    _attending_view(
        instance,
        attending,
        on_edit=lambda: None,
        on_save=lambda _instance, _attending_id: None,
    )

    created = _created_elements(before)
    labels = [getattr(element, "_text", None) for element in created]

    assert "0 half-days per week" in labels
    assert "2 of 2 half-days assigned" in labels
    assert "Week override" in labels
    assert any(
        "rbs-attending-work-row" in getattr(element, "_classes", [])
        for element in created
    )


def test_attending_view_distinguishes_schedule_errors_from_warnings() -> None:
    from nicegui import ui

    instance = blank_instance()
    first_day = instance.calendar.first_week_start
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=2,
        schedule_start_date=first_day,
        schedule_end_date=first_day + timedelta(days=4),
        preferred_weekly_schedule_half_days=[
            AttendingWorkHalfDay(
                weekday=Weekday.MONDAY,
                session=Session.MORNING,
                work_type=AttendingWorkType.ADMIN_TIME,
            )
        ],
    )
    instance = instance.revised(attendings=[attending])
    before = set(ui.context.client.elements)

    _attending_view(
        instance,
        attending,
        on_edit=lambda: None,
        on_save=lambda _instance, _attending_id: None,
    )

    labels = [getattr(element, "_text", None) for element in _created_elements(before)]
    assert "Schedule checks" in labels
    assert "1 error · 1 warning" in labels
    assert "Ada Lovelace · week 1: 1 of 2 half-days assigned." in labels
    assert "Ada Lovelace · week 1: 1 preferred half-day is not matched." in labels


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
    assert any(
        isinstance(text, str) and text.startswith("Their category targets")
        for text in (getattr(element, "_text", None) for element in created)
    )

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
        and element._props.get("label") == "Edit attending"
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
