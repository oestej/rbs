"""Resident list and focused editor for the workspace UI."""

from __future__ import annotations

import calendar as calendar_module
from collections.abc import Callable
from datetime import date, timedelta
from functools import partial

from pydantic import ValidationError

from rbs.models.instance import SchedulerInput
from rbs.models.resident import Resident
from rbs.models.schedule import Schedule
from rbs.ui import master_detail, page_shells
from rbs.ui.buttons import (
    ICON_BUTTON_PROPS,
    PRIMARY_BUTTON_PROPS,
    SECONDARY_BUTTON_PROPS,
    TERTIARY_BUTTON_PROPS,
    button_props,
)
from rbs.ui.residents.ops import (
    add_resident,
    day_off_date,
    day_off_is_selectable,
    next_resident_id,
    replace_resident,
    vacation_monday,
    vacation_monday_is_selectable,
    vacation_month_dates,
    vacation_week_for_monday,
)
from rbs.ui.residents.schedule import (
    _resident_clinic_half_day_editor,
    _resident_schedule_workspace,
)

SelectResident = Callable[[str | None], None]
SaveResident = Callable[[SchedulerInput, str], None]
SaveResidentSchedule = Callable[[SchedulerInput, str, bool], None]
SaveResidentScheduleResult = Callable[[Schedule, str, bool], None]
NEW_RESIDENT_ID = "__new_resident__"


def render_residents_tab(
    instance: SchedulerInput,
    schedule: Schedule | None = None,
    *,
    selected_resident_id: str | None,
    on_select: SelectResident,
    on_save: SaveResident,
    on_schedule_save: SaveResidentSchedule | None = None,
    on_schedule_change: SaveResidentScheduleResult | None = None,
    schedule_is_current: bool = True,
    block_schedule_editing: bool = False,
    on_block_schedule_editing_change: Callable[[bool], None] | None = None,
    schedule_editing: bool = False,
    on_schedule_editing_change: Callable[[bool], None] | None = None,
    active_schedule_section: str = "resident_block_schedule",
    on_schedule_section_change=None,
) -> None:
    creating = selected_resident_id == NEW_RESIDENT_ID
    selected = next(
        (resident for resident in instance.residents if resident.id == selected_resident_id),
        None,
    )

    with page_shells.master_detail(
        "Residents",
        subtitle="Manage resident details, time off, preferences, and schedules.",
    ):
        with master_detail.split(detail_selected=selected_resident_id is not None):
            _resident_directory(
                instance,
                selected_resident_id=selected_resident_id,
                on_select=on_select,
            )
            _resident_detail_panel(
                instance,
                schedule=schedule,
                resident=selected,
                creating=creating,
                missing_id=(
                    selected_resident_id
                    if selected_resident_id and not creating and selected is None
                    else None
                ),
                on_select=on_select,
                on_save=on_save,
                on_schedule_save=on_schedule_save,
                on_schedule_change=on_schedule_change,
                schedule_is_current=schedule_is_current,
                block_schedule_editing=block_schedule_editing,
                on_block_schedule_editing_change=on_block_schedule_editing_change,
                schedule_editing=schedule_editing,
                on_schedule_editing_change=on_schedule_editing_change,
                active_schedule_section=active_schedule_section,
                on_schedule_section_change=on_schedule_section_change,
            )


def _resident_directory(
    instance: SchedulerInput,
    *,
    selected_resident_id: str | None,
    on_select: SelectResident,
) -> None:
    from nicegui import ui

    elements = master_detail.directory(
        "Resident directory",
        search_label="Search residents",
        search_placeholder="Name or training year",
        action_label="New resident",
        action_icon="person_add",
        on_action=partial(on_select, NEW_RESIDENT_ID),
    )
    search = elements.search
    directory = elements.body

    def render_directory() -> None:
        directory.clear()
        query = str(search.value or "").strip().casefold()
        filtered = [
            resident
            for resident in instance.residents
            if not query
            or query in resident.name.casefold()
            or query in instance.training_level_label(resident.pgy).casefold()
            or query
            in instance.training_level_label(
                resident.pgy,
                compact=True,
            ).casefold()
            or query in f"pgy {resident.pgy}"
            or query in f"year {resident.pgy}"
        ]
        with directory:
            if not filtered:
                master_detail.empty_directory(
                    icon="person_search",
                    title="No matching residents",
                    description="Try a different name or year.",
                )
                return
            grouped: dict[int, list[Resident]] = {}
            for resident in filtered:
                grouped.setdefault(resident.pgy, []).append(resident)
            for pgy in sorted(grouped, key=instance.training_level_sort_key):
                master_detail.directory_heading(
                    instance.training_level_label(pgy, compact=True),
                    len(grouped[pgy]),
                )
                with ui.list().props("separator").classes("w-full"):
                    for resident in grouped[pgy]:
                        _resident_list_item(
                            resident,
                            selected_resident_id,
                            on_select,
                        )

    search.on_value_change(lambda: render_directory())
    render_directory()


def _resident_list_item(
    resident: Resident,
    selected_resident_id: str | None,
    on_select: SelectResident,
) -> None:
    from nicegui import ui

    item_classes = master_detail.selected_class(resident.id == selected_resident_id)
    with (
        ui.item(on_click=partial(on_select, resident.id))
        .props("clickable v-ripple")
        .classes(item_classes)
    ):
        with ui.item_section().props("avatar"):
            _resident_avatar(resident.name)
        with ui.item_section():
            ui.item_label(resident.name).classes("rbs-type-section-title")
            vacation_label = _vacation_week_count_label(len(resident.vacation_weeks))
            day_label = _individual_day_count_label(len(resident.days_off))
            ui.item_label(f"{vacation_label} · {day_label}").props("caption")
        with ui.item_section().props("side"):
            ui.icon("chevron_right").props("size=20px").classes("rbs-text-subtle")


def _resident_detail_panel(
    instance: SchedulerInput,
    *,
    schedule: Schedule | None,
    resident: Resident | None,
    creating: bool,
    missing_id: str | None,
    on_select: SelectResident,
    on_save: SaveResident,
    on_schedule_save: SaveResidentSchedule | None = None,
    on_schedule_change: SaveResidentScheduleResult | None = None,
    schedule_is_current: bool = True,
    block_schedule_editing: bool = False,
    on_block_schedule_editing_change: Callable[[bool], None] | None = None,
    schedule_editing: bool = False,
    on_schedule_editing_change: Callable[[bool], None] | None = None,
    active_schedule_section: str = "resident_block_schedule",
    on_schedule_section_change=None,
) -> None:
    editing = creating
    panel = master_detail.detail_panel()

    def render_panel() -> None:
        nonlocal editing
        panel.clear()
        with panel:
            if creating:
                _resident_form(
                    instance,
                    resident=None,
                    on_cancel=partial(on_select, None),
                    on_save=on_save,
                )
            elif resident is not None and editing:

                def stop_editing() -> None:
                    nonlocal editing
                    editing = False
                    render_panel()

                _resident_form(
                    instance,
                    resident=resident,
                    on_cancel=stop_editing,
                    on_save=on_save,
                )
            elif resident is not None:

                def start_editing() -> None:
                    nonlocal editing
                    editing = True
                    render_panel()

                _resident_view(
                    instance,
                    schedule,
                    resident,
                    start_editing,
                    on_select,
                    on_schedule_save=on_schedule_save,
                    on_schedule_change=on_schedule_change,
                    schedule_is_current=schedule_is_current,
                    block_schedule_editing=block_schedule_editing,
                    on_block_schedule_editing_change=on_block_schedule_editing_change,
                    schedule_editing=schedule_editing,
                    on_schedule_editing_change=on_schedule_editing_change,
                    active_schedule_section=active_schedule_section,
                    on_schedule_section_change=on_schedule_section_change,
                )
            else:
                _empty_resident_detail(missing_id)

    render_panel()


def _resident_view(
    instance: SchedulerInput,
    schedule: Schedule | None,
    resident: Resident,
    on_edit: Callable[[], None],
    on_select: SelectResident,
    *,
    on_schedule_save: SaveResidentSchedule | None = None,
    on_schedule_change: SaveResidentScheduleResult | None = None,
    schedule_is_current: bool = True,
    block_schedule_editing: bool = False,
    on_block_schedule_editing_change: Callable[[bool], None] | None = None,
    schedule_editing: bool = False,
    on_schedule_editing_change: Callable[[bool], None] | None = None,
    active_schedule_section: str = "resident_block_schedule",
    on_schedule_section_change=None,
) -> None:
    from nicegui import ui

    with ui.column().classes("w-full gap-4"):
        with master_detail.detail_card():
            with ui.row().classes("rbs-resident-summary w-full items-center gap-4 p-4"):
                _resident_avatar(resident.name)
                with ui.column().classes("gap-0"):
                    ui.label(resident.name).classes("rbs-type-dialog-title")
                    ui.label(
                        f"{instance.training_level_label(resident.pgy)} · "
                        f"{_vacation_week_count_label(len(resident.vacation_weeks))} · "
                        f"{_individual_day_count_label(len(resident.days_off))}"
                    ).classes("rbs-type-body rbs-text-muted")
                ui.space()
                ui.button("Edit info/time off", icon="edit", on_click=on_edit).props(
                    SECONDARY_BUTTON_PROPS
                )
                with ui.button(
                    icon="arrow_back",
                    on_click=partial(on_select, None),
                ).props(
                    button_props(
                        ICON_BUTTON_PROPS,
                        "aria-label='Back to resident directory'",
                    )
                ):
                    ui.tooltip("Back to resident directory")
        _resident_schedule_workspace(
            instance,
            schedule,
            resident,
            on_schedule_save=on_schedule_save,
            on_schedule_change=on_schedule_change,
            schedule_is_current=schedule_is_current,
            block_schedule_editing=block_schedule_editing,
            on_block_schedule_editing_change=on_block_schedule_editing_change,
            schedule_editing=schedule_editing,
            on_schedule_editing_change=on_schedule_editing_change,
            active_section=active_schedule_section,
            on_section_change=on_schedule_section_change,
        )


def _resident_form(
    instance: SchedulerInput,
    *,
    resident: Resident | None,
    on_cancel: Callable[[], None],
    on_save: SaveResident,
) -> None:
    from nicegui import ui

    creating = resident is None
    title = "New resident" if creating else "Edit resident"
    subtitle = (
        "Add a resident to this workspace."
        if creating
        else f"Update {resident.name}'s information."
    )
    initial_name = resident.name if resident is not None else ""
    pgy_options = instance.training_level_name_options
    default_pgy = resident.pgy if resident is not None else next(iter(pgy_options), 1)
    initial_vacations = resident.vacation_weeks if resident is not None else []
    initial_days_off = resident.days_off if resident is not None else []
    initial_clinic_half_days = resident.clinic_half_days if resident is not None else []

    with master_detail.detail_card():
        with ui.row().classes("w-full items-center justify-between gap-4 p-5"):
            with ui.column().classes("gap-0"):
                ui.label(title).classes("rbs-type-page-title")
                ui.label(subtitle).classes("rbs-text-muted")
            with ui.button(icon="close", on_click=on_cancel).props(
                button_props(
                    ICON_BUTTON_PROPS,
                    "aria-label='Cancel resident editing'",
                )
            ):
                ui.tooltip("Cancel resident editing")
        ui.separator()
        with ui.column().classes("w-full gap-5 p-5"):
            with ui.column().classes("w-full gap-3"):
                ui.label("Basic information").classes("rbs-type-section-title")
                with ui.row().classes("w-full items-start gap-4"):
                    name = (
                        ui.input("Full name", value=initial_name)
                        .props("outlined autofocus" if creating else "outlined")
                        .classes("w-full md:flex-1")
                    )
                    pgy = (
                        ui.select(
                            pgy_options,
                            value=default_pgy,
                            label="Training level",
                        )
                        .props("outlined options-dense")
                        .classes("w-full md:w-56")
                    )

            vacation_weeks = initial_vacations
            days_off = initial_days_off
            if not creating:
                with ui.column().classes("rbs-resident-form-section w-full gap-3 rounded p-4"):
                    with ui.column().classes("gap-0"):
                        ui.label("Vacation and Other Days Off (single days)").classes(
                            "rbs-type-section-title"
                        )
                        ui.label("Add whole vacation weeks or individual full days away.").classes(
                            "rbs-type-caption rbs-text-muted"
                        )
                    vacation_weeks = _vacation_week_editor(instance, initial_vacations)
                    ui.separator()
                    days_off = _days_off_editor(instance, initial_days_off)

            with ui.column().classes("rbs-resident-form-section w-full gap-3 rounded p-4"):
                with ui.column().classes("gap-0"):
                    ui.label("Continuity clinic half-days").classes("rbs-type-section-title")
                    ui.label(
                        "These recurring sessions are added to the Clinic Schedule in "
                        "every eligible week. They are omitted while the resident is on "
                        "an Away rotation."
                    ).classes("rbs-type-caption rbs-text-muted")
                clinic_half_days = _resident_clinic_half_day_editor(
                    instance,
                    initial_clinic_half_days,
                )

            def save() -> None:
                try:
                    saved_resident = Resident(
                        id=resident.id if resident is not None else next_resident_id(instance),
                        name=name.value or "",
                        pgy=int(pgy.value or default_pgy),
                        vacation_weeks=vacation_weeks,
                        days_off=days_off,
                        clinic_half_days=clinic_half_days,
                        elective_preferences=(
                            resident.elective_preferences if resident is not None else []
                        ),
                    )
                    if resident is None:
                        updated = add_resident(instance, saved_resident)
                        message = f"Added {saved_resident.name}"
                    else:
                        updated = replace_resident(instance, resident.id, saved_resident)
                        message = f"Saved {saved_resident.name}"
                    ui.notify(message, type="positive")
                    on_save(updated, saved_resident.id)
                except (ValidationError, ValueError) as exc:
                    ui.notify(str(exc), type="negative", multi_line=True)

            with ui.row().classes("items-center gap-2"):
                ui.button(
                    "Add resident" if creating else "Save changes",
                    icon="person_add" if creating else "save",
                    on_click=save,
                ).props(PRIMARY_BUTTON_PROPS)
                ui.button("Cancel", on_click=on_cancel).props(TERTIARY_BUTTON_PROPS)


def _empty_resident_detail(missing_id: str | None) -> None:
    title = "Resident not found" if missing_id else "Select a resident"
    description = (
        "That resident is no longer in this workspace. Select another resident from the directory."
        if missing_id
        else "Choose someone from the directory to view their details, or add a new resident."
    )
    master_detail.empty_detail(
        icon="person_search",
        title=title,
        description=description,
    )


def _resident_initials(name: str) -> str:
    parts = [part for part in name.split() if part]
    if not parts:
        return "?"
    return "".join(part[0].upper() for part in parts[:2])


def _resident_avatar(name: str, *, large: bool = False) -> None:
    from nicegui import ui

    classes = "rbs-resident-avatar rbs-resident-avatar-large" if large else "rbs-resident-avatar"
    with ui.avatar(color=None).classes(classes):
        ui.label(_resident_initials(name)).classes("rbs-resident-initials")


def _week_count_label(count: int) -> str:
    return f"{count} week" if count == 1 else f"{count} weeks"


def _vacation_week_count_label(count: int) -> str:
    if count == 0:
        return "No vacation weeks"
    return f"{count} vacation week" if count == 1 else f"{count} vacation weeks"


def _day_count_label(count: int) -> str:
    return f"{count} day" if count == 1 else f"{count} days"


def _individual_day_count_label(count: int) -> str:
    if count == 0:
        return "No individual days off"
    return f"{count} individual day off" if count == 1 else f"{count} individual days off"


def _vacation_week_editor(
    instance: SchedulerInput,
    initial_weeks: list[int],
) -> list[int]:
    from nicegui import ui

    weeks = sorted(initial_weeks)
    ui.label("Vacation weeks").classes("rbs-type-control-label")
    selected_container = ui.column().classes("w-full gap-2")

    def render_selected() -> None:
        selected_container.clear()
        with selected_container:
            if not weeks:
                ui.label("No vacation weeks selected.").classes("rbs-type-body rbs-text-muted")
            else:
                with ui.row().classes("w-full gap-2"):
                    for week in weeks:
                        monday = vacation_monday(instance, week)

                        def remove(event, selected_week: int = week) -> None:
                            if event.value:
                                return
                            weeks.remove(selected_week)
                            render_selected()

                        ui.chip(
                            f"{monday:%b} {monday.day}, {monday:%Y} · Week {week}",
                            icon="event",
                            color=None,
                            removable=True,
                            on_value_change=remove,
                        ).classes("rbs-vacation-chip")
            if len(weeks) > 4:
                with ui.row().classes("rbs-vacation-warning w-full items-center gap-2 rounded p-2"):
                    ui.icon("warning")
                    ui.label(
                        f"{len(weeks)} vacation weeks selected — this exceeds four weeks."
                    ).classes("rbs-font-semibold")
            else:
                ui.label(f"{len(weeks)} of 4 vacation weeks selected").classes(
                    "rbs-type-caption rbs-text-muted"
                )

    render_selected()

    with ui.row().classes("w-full items-end gap-2"):
        with (
            ui.input(
                "Add vacation Monday",
                placeholder="Choose a Monday",
            )
            .props("outlined readonly")
            .classes("min-w-64 flex-1") as selected_date
        ):
            with ui.menu() as calendar_menu:
                first_monday = instance.calendar.first_week_start
                last_monday = vacation_monday(instance, instance.calendar.weeks)
                first_month = first_monday.replace(day=1)
                last_month = (last_monday + timedelta(days=6)).replace(day=1)
                visible_month = first_month
                highlighted_monday: date | None = None
                calendar_container = ui.column().classes("rbs-vacation-calendar gap-0")

                def select_calendar_monday(selected: date) -> None:
                    nonlocal highlighted_monday
                    highlighted_monday = selected
                    selected_date.set_value(selected.isoformat())
                    render_calendar()

                def move_calendar_month(offset: int) -> None:
                    nonlocal visible_month
                    month_index = visible_month.year * 12 + visible_month.month - 1 + offset
                    visible_month = date(month_index // 12, month_index % 12 + 1, 1)
                    render_calendar()

                def render_calendar() -> None:
                    calendar_container.clear()
                    with calendar_container:
                        with ui.row().classes(
                            "rbs-vacation-calendar-nav w-full items-center justify-between"
                        ):
                            previous = ui.button(
                                icon="chevron_left",
                                on_click=partial(move_calendar_month, -1),
                            ).props("flat round dense aria-label='Previous month'")
                            if visible_month <= first_month:
                                previous.props("disable")
                            ui.label(
                                f"{calendar_module.month_name[visible_month.month]} "
                                f"{visible_month.year}"
                            ).classes("rbs-font-semibold")
                            following = ui.button(
                                icon="chevron_right",
                                on_click=partial(move_calendar_month, 1),
                            ).props("flat round dense aria-label='Next month'")
                            if visible_month >= last_month:
                                following.props("disable")

                        with ui.element("div").classes("rbs-vacation-calendar-grid"):
                            for weekday in ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"):
                                ui.label(weekday).classes("rbs-vacation-calendar-weekday")
                            highlighted_sunday = (
                                highlighted_monday + timedelta(days=6)
                                if highlighted_monday is not None
                                else None
                            )
                            for calendar_day in vacation_month_dates(
                                visible_month.year, visible_month.month
                            ):
                                classes = ["rbs-vacation-calendar-day"]
                                if calendar_day.month != visible_month.month:
                                    classes.append("is-outside-month")
                                if (
                                    highlighted_monday is not None
                                    and highlighted_sunday is not None
                                    and highlighted_monday <= calendar_day <= highlighted_sunday
                                ):
                                    classes.append("is-selected-week")
                                selectable = vacation_monday_is_selectable(instance, calendar_day)
                                if selectable:
                                    classes.append("is-selectable")
                                if calendar_day == highlighted_monday:
                                    classes.append("is-selected-monday")
                                with ui.element("div").classes(" ".join(classes)):
                                    if selectable:
                                        ui.button(
                                            str(calendar_day.day),
                                            on_click=partial(select_calendar_monday, calendar_day),
                                        ).props(
                                            "flat dense round "
                                            f"aria-label='Choose Monday {calendar_day:%b %d, %Y}'"
                                        ).classes("rbs-vacation-calendar-monday")
                                    else:
                                        ui.label(str(calendar_day.day))

                render_calendar()
                with ui.row().classes("w-full justify-end p-2"):
                    ui.button("Close", on_click=calendar_menu.close).props("flat dense")
            with selected_date.add_slot("append"):
                ui.icon("event").classes("cursor-pointer").on("click", calendar_menu.open)
        selected_date.on("click", calendar_menu.open)

        def add_week() -> None:
            nonlocal highlighted_monday
            try:
                week = vacation_week_for_monday(instance, selected_date.value or "")
                if week in weeks:
                    raise ValueError(f"week {week} is already selected")
                weeks.append(week)
                weeks.sort()
                selected_date.set_value(None)
                highlighted_monday = None
                calendar_menu.close()
                render_selected()
                render_calendar()
            except ValueError as exc:
                ui.notify(str(exc), type="negative")

        ui.button("Add week", icon="add", on_click=add_week).props(SECONDARY_BUTTON_PROPS)

    return weeks


def _days_off_editor(
    instance: SchedulerInput,
    initial_days: list[date],
) -> list[date]:
    from nicegui import ui

    days = sorted(initial_days)
    ui.label("Other days off").classes("rbs-type-control-label")
    ui.label("Choose individual full days away from the program.").classes(
        "rbs-type-caption rbs-text-muted"
    )
    selected_container = ui.column().classes("w-full gap-2")

    def render_selected() -> None:
        selected_container.clear()
        with selected_container:
            if not days:
                ui.label("No individual days off selected.").classes("rbs-type-body rbs-text-muted")
                return
            with ui.row().classes("w-full gap-2"):
                for selected_day in days:

                    def remove(event, day_to_remove: date = selected_day) -> None:
                        if event.value:
                            return
                        days.remove(day_to_remove)
                        render_selected()

                    ui.chip(
                        f"{selected_day:%a, %b} {selected_day.day}, {selected_day:%Y}",
                        icon="event_busy",
                        color=None,
                        removable=True,
                        on_value_change=remove,
                    ).classes("rbs-day-off-chip")
            ui.label(_day_count_label(len(days)) + " off").classes(
                "rbs-type-caption rbs-text-muted"
            )

    render_selected()

    with ui.row().classes("w-full items-end gap-2"):
        with (
            ui.input(
                "Add day off",
                placeholder="Choose a date",
            )
            .props("outlined readonly")
            .classes("min-w-64 flex-1") as selected_date
        ):
            with ui.menu() as calendar_menu:
                first_day = instance.calendar.first_week_start
                last_day = first_day + timedelta(days=instance.calendar.weeks * 7 - 1)
                first_month = first_day.replace(day=1)
                last_month = last_day.replace(day=1)
                visible_month = first_month
                highlighted_day: date | None = None
                calendar_container = ui.column().classes("rbs-vacation-calendar gap-0")

                def select_calendar_day(selected: date) -> None:
                    nonlocal highlighted_day
                    highlighted_day = selected
                    selected_date.set_value(selected.isoformat())
                    render_calendar()

                def move_calendar_month(offset: int) -> None:
                    nonlocal visible_month
                    month_index = visible_month.year * 12 + visible_month.month - 1 + offset
                    visible_month = date(month_index // 12, month_index % 12 + 1, 1)
                    render_calendar()

                def render_calendar() -> None:
                    calendar_container.clear()
                    with calendar_container:
                        with ui.row().classes(
                            "rbs-vacation-calendar-nav w-full items-center justify-between"
                        ):
                            previous = ui.button(
                                icon="chevron_left",
                                on_click=partial(move_calendar_month, -1),
                            ).props("flat round dense aria-label='Previous month'")
                            if visible_month <= first_month:
                                previous.props("disable")
                            ui.label(
                                f"{calendar_module.month_name[visible_month.month]} "
                                f"{visible_month.year}"
                            ).classes("rbs-font-semibold")
                            following = ui.button(
                                icon="chevron_right",
                                on_click=partial(move_calendar_month, 1),
                            ).props("flat round dense aria-label='Next month'")
                            if visible_month >= last_month:
                                following.props("disable")

                        with ui.element("div").classes("rbs-vacation-calendar-grid"):
                            for weekday in ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"):
                                ui.label(weekday).classes("rbs-vacation-calendar-weekday")
                            for calendar_day in vacation_month_dates(
                                visible_month.year, visible_month.month
                            ):
                                classes = ["rbs-vacation-calendar-day"]
                                if calendar_day.month != visible_month.month:
                                    classes.append("is-outside-month")
                                selectable = day_off_is_selectable(instance, calendar_day)
                                if selectable:
                                    classes.append("is-selectable")
                                if calendar_day == highlighted_day:
                                    classes.append("is-selected-day")
                                with ui.element("div").classes(" ".join(classes)):
                                    if selectable:
                                        ui.button(
                                            str(calendar_day.day),
                                            on_click=partial(select_calendar_day, calendar_day),
                                        ).props(
                                            "flat dense round "
                                            f"aria-label='Choose day off {calendar_day:%b %d, %Y}'"
                                        ).classes("rbs-day-off-calendar-date")
                                    else:
                                        ui.label(str(calendar_day.day))

                render_calendar()
                with ui.row().classes("w-full justify-end p-2"):
                    ui.button("Close", on_click=calendar_menu.close).props("flat dense")
            with selected_date.add_slot("append"):
                ui.icon("event").classes("cursor-pointer").on("click", calendar_menu.open)
        selected_date.on("click", calendar_menu.open)

        def add_day() -> None:
            nonlocal highlighted_day
            try:
                selected_day = day_off_date(instance, selected_date.value or "")
                if selected_day in days:
                    raise ValueError(f"{selected_day:%b %d, %Y} is already selected")
                days.append(selected_day)
                days.sort()
                selected_date.set_value(None)
                highlighted_day = None
                calendar_menu.close()
                render_selected()
                render_calendar()
            except ValueError as exc:
                ui.notify(str(exc), type="negative")

        ui.button("Add day", icon="add", on_click=add_day).props(SECONDARY_BUTTON_PROPS)

    return days
