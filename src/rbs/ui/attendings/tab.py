"""Attending directory and availability editor for the workspace UI."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from functools import partial

from pydantic import ValidationError

from rbs.models.attending import (
    DEFAULT_ATTENDING_HALF_DAYS_PER_WEEK,
    MAX_ATTENDING_HALF_DAYS_PER_WEEK,
    Attending,
    AttendingAdHocWorkHalfDay,
    AttendingVacation,
    attending_display_sort_key,
)
from rbs.models.enums import Session
from rbs.models.instance import SchedulerInput
from rbs.ui import master_detail, page_shells
from rbs.ui.attendings.ops import (
    AD_HOC_ALL_DAY,
    academic_year_date_range,
    add_ad_hoc_work_half_days,
    add_attending,
    add_vacation_range,
    next_attending_id,
    parse_attending_date,
    remove_attending,
    replace_attending,
)
from rbs.ui.buttons import (
    DESTRUCTIVE_BUTTON_PROPS,
    DESTRUCTIVE_ICON_BUTTON_PROPS,
    ICON_BUTTON_PROPS,
    PRIMARY_BUTTON_PROPS,
    SECONDARY_BUTTON_PROPS,
    button_props,
)
from rbs.ui.editor_common import _validation_message
from rbs.ui.editor_guard import EditorGuard, confirm_guarded_navigation

SelectAttending = Callable[[str | None], None]
SaveAttending = Callable[[SchedulerInput, str | None], None]
NEW_ATTENDING_ID = "__new_attending__"


def render_attendings_tab(
    instance: SchedulerInput,
    *,
    selected_attending_id: str | None,
    on_select: SelectAttending,
    on_save: SaveAttending,
) -> None:
    guard = EditorGuard()

    def guarded_select(attending_id: str | None) -> None:
        confirm_guarded_navigation(guard, partial(on_select, attending_id))

    creating = selected_attending_id == NEW_ATTENDING_ID
    selected = next(
        (
            attending
            for attending in instance.attendings
            if attending.id == selected_attending_id
        ),
        None,
    )

    with page_shells.master_detail(
        "Attendings",
        subtitle=(
            "Manage attending weekly schedules, schedule dates, ad hoc work, and vacation."
        ),
    ):
        with master_detail.split(detail_selected=selected_attending_id is not None):
            _attending_directory(
                instance,
                selected_attending_id=selected_attending_id,
                on_select=guarded_select,
            )
            _attending_detail_panel(
                instance,
                attending=selected,
                creating=creating,
                missing_id=(
                    selected_attending_id
                    if selected_attending_id and not creating and selected is None
                    else None
                ),
                on_select=on_select,
                on_save=on_save,
                guard=guard,
            )


def _attending_directory(
    instance: SchedulerInput,
    *,
    selected_attending_id: str | None,
    on_select: SelectAttending,
) -> None:
    from nicegui import ui

    elements = master_detail.directory(
        "Attending directory",
        search_label="Search attendings",
        search_placeholder="Name or schedule dates",
        action_label="New attending",
        action_icon="person_add",
        on_action=partial(on_select, NEW_ATTENDING_ID),
    )
    search = elements.search
    directory = elements.body

    def render_directory() -> None:
        directory.clear()
        query = str(search.value or "").strip().casefold()
        filtered = [
            attending
            for attending in instance.attendings
            if not query
            or query in attending.name.casefold()
            or query in _schedule_span_label(instance, attending).casefold()
            or query in _attending_summary_label(instance, attending).casefold()
        ]
        with directory:
            if not filtered:
                if instance.attendings:
                    master_detail.empty_directory(
                        icon="person_search",
                        title="No matching attendings",
                        description="Try a different name or schedule date.",
                    )
                else:
                    master_detail.empty_directory(
                        icon="groups",
                        title="No attendings yet",
                        description="Add an attending to configure availability.",
                    )
                return
            master_detail.directory_heading("Attendings", len(filtered))
            with ui.list().props("separator").classes("w-full"):
                for attending in sorted(filtered, key=attending_display_sort_key):
                    _attending_list_item(
                        instance,
                        attending,
                        selected_attending_id,
                        on_select,
                    )

    search.on_value_change(lambda: render_directory())
    render_directory()


def _attending_list_item(
    instance: SchedulerInput,
    attending: Attending,
    selected_attending_id: str | None,
    on_select: SelectAttending,
) -> None:
    from nicegui import ui

    item_classes = master_detail.selected_class(attending.id == selected_attending_id)
    with (
        ui.item(on_click=partial(on_select, attending.id))
        .props("clickable v-ripple")
        .classes(item_classes)
    ):
        with ui.item_section().props("avatar"):
            _attending_avatar(attending.name)
        with ui.item_section():
            ui.item_label(attending.name).classes("rbs-type-section-title")
            ui.item_label(_attending_summary_label(instance, attending)).props("caption")
        with ui.item_section().props("side"):
            ui.icon("chevron_right").props("size=20px").classes("rbs-text-subtle")


def _attending_detail_panel(
    instance: SchedulerInput,
    *,
    attending: Attending | None,
    creating: bool,
    missing_id: str | None,
    on_select: SelectAttending,
    on_save: SaveAttending,
    guard: EditorGuard,
) -> None:
    editing = creating
    panel = master_detail.detail_panel()

    def render_panel() -> None:
        nonlocal editing
        panel.clear()
        with panel:
            if creating:
                _attending_form(
                    instance,
                    attending=None,
                    on_cancel=partial(on_select, None),
                    on_save=on_save,
                    guard=guard,
                )
            elif attending is not None and editing:

                def stop_editing() -> None:
                    nonlocal editing
                    editing = False
                    render_panel()

                _attending_form(
                    instance,
                    attending=attending,
                    on_cancel=stop_editing,
                    on_save=on_save,
                    guard=guard,
                )
            elif attending is not None:
                guard.clear()

                def start_editing() -> None:
                    nonlocal editing
                    editing = True
                    render_panel()

                _attending_view(
                    instance,
                    attending,
                    on_edit=start_editing,
                    on_select=on_select,
                    on_save=on_save,
                )
            else:
                guard.clear()
                _empty_attending_detail(missing_id)

    render_panel()


def _attending_view(
    instance: SchedulerInput,
    attending: Attending,
    *,
    on_edit: Callable[[], None],
    on_select: SelectAttending,
    on_save: SaveAttending,
) -> None:
    from nicegui import ui

    first_day, last_day = academic_year_date_range(instance)
    with ui.column().classes("w-full gap-4"):
        with master_detail.detail_card():
            with ui.row().classes("rbs-attending-summary w-full items-center gap-4 p-4"):
                _attending_avatar(attending.name)
                with ui.column().classes("min-w-0 gap-0"):
                    ui.label(attending.name).classes("rbs-type-dialog-title")
                    ui.label(_attending_summary_label(instance, attending)).classes(
                        "rbs-type-body rbs-text-muted"
                    )
                ui.space()
                ui.button("Edit availability", icon="edit", on_click=on_edit).props(
                    SECONDARY_BUTTON_PROPS
                )
                with ui.button(
                    icon="delete_outline",
                    on_click=partial(
                        _confirm_remove_attending,
                        instance,
                        attending,
                        on_save=on_save,
                    ),
                ).props(
                    button_props(
                        DESTRUCTIVE_ICON_BUTTON_PROPS,
                        f"aria-label='Remove attending {attending.name}'",
                    )
                ):
                    ui.tooltip("Remove attending")
                with ui.button(
                    icon="arrow_back",
                    on_click=partial(on_select, None),
                ).props(
                    button_props(
                        ICON_BUTTON_PROPS,
                        "aria-label='Back to attending directory'",
                    )
                ):
                    ui.tooltip("Back to attending directory")

        with master_detail.detail_card():
            with ui.column().classes("w-full gap-4 p-5"):
                with ui.row().classes("w-full items-center justify-between gap-3"):
                    with ui.column().classes("gap-0"):
                        ui.label("Schedule dates").classes("rbs-type-section-title")
                        ui.label(
                            "Academic year boundaries apply wherever no custom date is set."
                        ).classes("rbs-type-caption rbs-text-muted")
                    ui.badge(
                        _weekly_work_label(attending)
                    ).props("outline").classes("rbs-muted-badge")
                with ui.row().classes("w-full items-stretch gap-4 flex-wrap"):
                    _schedule_boundary_card(
                        heading="Schedule starts",
                        explicit_date=attending.schedule_start_date,
                        effective_date=attending.effective_schedule_start(first_day),
                        default_label="Academic year start",
                    )
                    _schedule_boundary_card(
                        heading="Schedule ends",
                        explicit_date=attending.schedule_end_date,
                        effective_date=attending.effective_schedule_end(last_day),
                        default_label="Academic year end",
                    )

        with master_detail.detail_card():
            with ui.column().classes("w-full gap-3 p-5"):
                with ui.row().classes("w-full items-center justify-between gap-3"):
                    with ui.column().classes("gap-0"):
                        ui.label("Ad hoc work").classes("rbs-type-section-title")
                        ui.label(
                            "These required half-days are added to the recurring weekly schedule."
                        ).classes("rbs-type-caption rbs-text-muted")
                    ui.badge(
                        _ad_hoc_work_count_label(len(attending.ad_hoc_work_half_days))
                    ).props("outline").classes("rbs-muted-badge")
                _attending_ad_hoc_work_list(attending.ad_hoc_work_half_days)

        with master_detail.detail_card():
            with ui.column().classes("w-full gap-3 p-5"):
                with ui.row().classes("w-full items-center justify-between gap-3"):
                    with ui.column().classes("gap-0"):
                        ui.label("Vacation").classes("rbs-type-section-title")
                        ui.label("Vacation ranges may begin and end on any day.").classes(
                            "rbs-type-caption rbs-text-muted"
                        )
                    ui.badge(_weekday_off_label(attending.vacation_ranges)).props(
                        "outline"
                    ).classes("rbs-muted-badge")
                _attending_vacation_list(attending.vacation_ranges)


def _schedule_boundary_card(
    *,
    heading: str,
    explicit_date: date | None,
    effective_date: date,
    default_label: str,
) -> None:
    from nicegui import ui

    state_class = " uses-academic-boundary" if explicit_date is None else ""
    with ui.column().classes(
        "rbs-attending-boundary-card"
        f"{state_class} w-full md:flex-1 min-w-0 gap-1 rounded p-4"
    ):
        with ui.row().classes("w-full items-center justify-between gap-2"):
            ui.label(heading).classes("rbs-type-control-label")
            ui.badge(default_label if explicit_date is None else "Custom date").props("outline")
        ui.label(_date_label(effective_date)).classes("rbs-type-dialog-title")
        if explicit_date is None:
            ui.label("No custom date — this follows the workspace calendar.").classes(
                "rbs-type-caption rbs-text-muted"
            )


def _attending_ad_hoc_work_list(
    half_days: list[AttendingAdHocWorkHalfDay],
) -> None:
    from nicegui import ui

    if not half_days:
        with ui.row().classes(
            "rbs-attending-work-empty w-full items-center gap-3 rounded px-4 py-4"
        ):
            ui.icon("event_available").classes("rbs-text-subtle")
            with ui.column().classes("gap-0"):
                ui.label("No ad hoc work configured.").classes("rbs-font-semibold")
                ui.label(
                    "Only the recurring weekly schedule applies to this attending."
                ).classes("rbs-type-caption rbs-text-muted")
        return
    for half_day in half_days:
        with ui.row().classes(
            "rbs-attending-work-row w-full items-center gap-3 rounded px-4 py-3"
        ):
            ui.icon("work_outline").classes("rbs-text-primary")
            with ui.column().classes("min-w-0 flex-1 gap-0"):
                ui.label(_date_label(half_day.date)).classes("rbs-font-semibold")
                ui.label(_work_session_label(half_day.session)).classes(
                    "rbs-type-caption rbs-text-muted"
                )


def _attending_vacation_list(vacations: list[AttendingVacation]) -> None:
    from nicegui import ui

    if not vacations:
        with ui.row().classes(
            "rbs-attending-vacation-empty w-full items-center gap-3 rounded px-4 py-4"
        ):
            ui.icon("event_available").classes("rbs-text-subtle")
            with ui.column().classes("gap-0"):
                ui.label("No vacation configured.").classes("rbs-font-semibold")
                ui.label("This attending has no vacation ranges in this academic year.").classes(
                    "rbs-type-caption rbs-text-muted"
                )
        return
    for vacation in vacations:
        with ui.row().classes(
            "rbs-attending-vacation-row w-full items-center gap-3 rounded px-4 py-3"
        ):
            ui.icon("beach_access").classes("rbs-text-secondary")
            with ui.column().classes("min-w-0 flex-1 gap-0"):
                ui.label(_vacation_period_label(vacation)).classes("rbs-font-semibold")
                ui.label(_day_count_label(vacation.days)).classes(
                    "rbs-type-caption rbs-text-muted"
                )


def _attending_form(
    instance: SchedulerInput,
    *,
    attending: Attending | None,
    on_cancel: Callable[[], None],
    on_save: SaveAttending,
    guard: EditorGuard | None = None,
) -> None:
    from nicegui import ui

    creating = attending is None
    first_day, last_day = academic_year_date_range(instance)
    initial_start = attending.schedule_start_date if attending is not None else None
    initial_end = attending.schedule_end_date if attending is not None else None
    initial_vacations = list(attending.vacation_ranges) if attending is not None else []
    initial_ad_hoc_work = (
        list(attending.ad_hoc_work_half_days) if attending is not None else []
    )
    controls: dict[str, object] = {}

    def save() -> bool:
        try:
            saved_attending = Attending(
                id=attending.id if attending is not None else next_attending_id(instance),
                name=str(getattr(controls["name"], "value", "") or ""),
                half_days_per_week=getattr(
                    controls["half_days_per_week"],
                    "value",
                    None,
                ),
                schedule_start_date=(
                    None
                    if bool(getattr(controls["default_start"], "value", False))
                    else parse_attending_date(
                        instance,
                        getattr(controls["start"], "value", None),
                        label="Schedule start date",
                    )
                ),
                schedule_end_date=(
                    None
                    if bool(getattr(controls["default_end"], "value", False))
                    else parse_attending_date(
                        instance,
                        getattr(controls["end"], "value", None),
                        label="Schedule end date",
                    )
                ),
                vacation_ranges=initial_vacations,
                ad_hoc_work_half_days=initial_ad_hoc_work,
            )
            if attending is None:
                updated = add_attending(instance, saved_attending)
                message = f"Added {saved_attending.name}"
            else:
                updated = replace_attending(instance, attending.id, saved_attending)
                message = f"Saved {saved_attending.name}"
            if guard is not None:
                guard.clear()
            ui.notify(message, type="positive")
            on_save(updated, saved_attending.id)
            return True
        except (TypeError, ValidationError, ValueError) as exc:
            ui.notify(_validation_message(exc), type="negative", multi_line=True)
            return False

    title = "New attending" if creating else "Edit attending"
    subtitle = (
        "Add an attending and their weekly or ad hoc schedule."
        if creating
        else (
            f"Update {attending.name}'s weekly schedule, dates, ad hoc work, and vacation."
        )
    )
    with master_detail.detail_card():
        with ui.row().classes("w-full items-center justify-between gap-4 p-5"):
            with ui.column().classes("gap-0"):
                ui.label(title).classes("rbs-type-page-title")
                ui.label(subtitle).classes("rbs-text-muted")
            with ui.row().classes("items-center gap-2"):
                ui.button(
                    "Add attending" if creating else "Save changes",
                    icon="person_add" if creating else "save",
                    on_click=save,
                ).props(PRIMARY_BUTTON_PROPS)
                with ui.button(
                    icon="close",
                    on_click=partial(confirm_guarded_navigation, guard, on_cancel),
                ).props(
                    button_props(
                        ICON_BUTTON_PROPS,
                        "aria-label='Cancel attending editing'",
                    )
                ):
                    ui.tooltip("Cancel attending editing")
        ui.separator()
        with ui.column().classes("w-full gap-5 p-5"):
            with ui.column().classes("w-full gap-3"):
                ui.label("Basic information").classes("rbs-type-section-title")
                with ui.row().classes("w-full items-start gap-4 flex-wrap"):
                    name = (
                        ui.input(
                            "Full name",
                            value=attending.name if attending is not None else "",
                        )
                        .props("outlined autofocus" if creating else "outlined")
                        .classes("w-full md:flex-1")
                    )
                    half_days_per_week = (
                        ui.number(
                            "Half-days per week",
                            value=(
                                attending.half_days_per_week
                                if attending is not None
                                else DEFAULT_ATTENDING_HALF_DAYS_PER_WEEK
                            ),
                            min=0,
                            max=MAX_ATTENDING_HALF_DAYS_PER_WEEK,
                            step=1,
                            precision=0,
                        )
                        .props("outlined")
                        .classes("w-full md:w-48")
                    )
                if creating:
                    name.run_method("focus")
                controls["name"] = name
                controls["half_days_per_week"] = half_days_per_week

            with ui.column().classes(
                "rbs-attending-form-section w-full gap-4 rounded p-4"
            ):
                with ui.column().classes("gap-0"):
                    ui.label("Schedule dates").classes("rbs-type-section-title")
                    ui.label(
                        "Keep a boundary tied to the academic year, or set a custom date."
                    ).classes("rbs-type-caption rbs-text-muted")
                with ui.row().classes("w-full items-start gap-4 flex-wrap"):
                    start_default = ui.checkbox(
                        "Use academic year start",
                        value=initial_start is None,
                    ).props("dense")
                    start = (
                        ui.input(
                            "Schedule start date",
                            value=(initial_start or first_day).isoformat(),
                        )
                        .props(
                            f"outlined type=date min={first_day.isoformat()} "
                            f"max={last_day.isoformat()}"
                        )
                        .classes("w-full md:flex-1")
                    )
                    start.set_enabled(initial_start is not None)
                with ui.row().classes("w-full items-start gap-4 flex-wrap"):
                    end_default = ui.checkbox(
                        "Use academic year end",
                        value=initial_end is None,
                    ).props("dense")
                    end = (
                        ui.input(
                            "Schedule end date",
                            value=(initial_end or last_day).isoformat(),
                        )
                        .props(
                            f"outlined type=date min={first_day.isoformat()} "
                            f"max={last_day.isoformat()}"
                        )
                        .classes("w-full md:flex-1")
                    )
                    end.set_enabled(initial_end is not None)

                start_default.on_value_change(
                    lambda event: start.set_enabled(not bool(event.value))
                )
                end_default.on_value_change(
                    lambda event: end.set_enabled(not bool(event.value))
                )
                controls.update(
                    {
                        "default_start": start_default,
                        "start": start,
                        "default_end": end_default,
                        "end": end,
                    }
                )

            with ui.column().classes(
                "rbs-attending-form-section w-full gap-4 rounded p-4"
            ):
                _ad_hoc_work_editor(instance, initial_ad_hoc_work)

            with ui.column().classes(
                "rbs-attending-form-section w-full gap-4 rounded p-4"
            ):
                _vacation_range_editor(instance, initial_vacations)

    def current_editor_state() -> dict[str, object]:
        return {
            "name": str(getattr(controls["name"], "value", "") or ""),
            "half_days_per_week": getattr(
                controls["half_days_per_week"],
                "value",
                None,
            ),
            "default_start": bool(
                getattr(controls["default_start"], "value", False)
            ),
            "start": str(getattr(controls["start"], "value", "") or ""),
            "default_end": bool(getattr(controls["default_end"], "value", False)),
            "end": str(getattr(controls["end"], "value", "") or ""),
            "vacations": [
                vacation.model_dump(mode="json") for vacation in initial_vacations
            ],
            "ad_hoc_work": [
                half_day.model_dump(mode="json") for half_day in initial_ad_hoc_work
            ],
        }

    initial_editor_state = current_editor_state()
    if guard is not None:
        guard.is_dirty = lambda: current_editor_state() != initial_editor_state
        guard.save = save
        guard.subject = (
            "the new attending" if creating else f"{attending.name}'s availability"
        )
        guard.save_label = "Add attending" if creating else "Save changes"
        guard.save_icon = "person_add" if creating else "save"


def _ad_hoc_work_editor(
    instance: SchedulerInput,
    half_days: list[AttendingAdHocWorkHalfDay],
) -> None:
    from nicegui import ui

    first_day, last_day = academic_year_date_range(instance)
    default_day = min(max(date.today(), first_day), last_day)
    with ui.row().classes("w-full items-center justify-between gap-3"):
        with ui.column().classes("gap-0"):
            ui.label("Ad hoc work").classes("rbs-type-section-title")
            ui.label(
                "Add required morning or afternoon work outside the recurring weekly total."
            ).classes("rbs-type-caption rbs-text-muted")
        work_badge = (
            ui.badge(_ad_hoc_work_count_label(len(half_days)))
            .props("outline")
            .classes("rbs-muted-badge")
        )

    selected_container = ui.column().classes("w-full gap-2")

    def render_selected() -> None:
        work_badge.set_text(_ad_hoc_work_count_label(len(half_days)))
        selected_container.clear()
        with selected_container:
            if not half_days:
                ui.label("No ad hoc work configured.").classes(
                    "rbs-type-body rbs-text-muted"
                )
                return
            for index, half_day in enumerate(half_days):
                with ui.row().classes(
                    "rbs-attending-work-row w-full items-center gap-3 rounded px-4 py-3"
                ):
                    ui.icon("work_outline").classes("rbs-text-primary")
                    with ui.column().classes("min-w-0 flex-1 gap-0"):
                        ui.label(_date_label(half_day.date)).classes("rbs-font-semibold")
                        ui.label(_work_session_label(half_day.session)).classes(
                            "rbs-type-caption rbs-text-muted"
                        )

                    def remove(event=None, *, selected_index: int = index) -> None:
                        half_days.pop(selected_index)
                        render_selected()

                    with ui.button(icon="delete_outline", on_click=remove).props(
                        button_props(
                            DESTRUCTIVE_ICON_BUTTON_PROPS,
                            "aria-label='Remove ad hoc work "
                            f"{half_day.date.isoformat()} {half_day.session.value}'",
                        )
                    ):
                        ui.tooltip("Remove ad hoc work half-day")

    render_selected()

    work_time_options = {
        Session.MORNING.value: "Morning (AM)",
        Session.AFTERNOON.value: "Afternoon (PM)",
        AD_HOC_ALL_DAY: "All day (AM and PM)",
    }
    with ui.row().classes("w-full items-end gap-3 flex-wrap"):
        work_date = (
            ui.input("Work date", value=default_day.isoformat())
            .props(
                f"outlined type=date min={first_day.isoformat()} max={last_day.isoformat()}"
            )
            .classes("w-full md:flex-1")
        )
        work_time = (
            ui.select(
                work_time_options,
                value=Session.MORNING.value,
                label="Work time",
            )
            .props("outlined options-dense")
            .classes("w-full md:w-56")
        )

        def add_work() -> None:
            try:
                validated = add_ad_hoc_work_half_days(
                    instance,
                    half_days,
                    date_value=work_date.value,
                    session_value=work_time.value,
                )
                half_days[:] = validated
                render_selected()
            except (ValidationError, ValueError) as exc:
                ui.notify(_validation_message(exc), type="negative", multi_line=True)

        ui.button("Add work half-day", icon="add", on_click=add_work).props(
            SECONDARY_BUTTON_PROPS
        )


def _vacation_range_editor(
    instance: SchedulerInput,
    vacations: list[AttendingVacation],
) -> None:
    from nicegui import ui

    first_day, last_day = academic_year_date_range(instance)
    default_day = min(max(date.today(), first_day), last_day)
    with ui.row().classes("w-full items-center justify-between gap-3"):
        with ui.column().classes("gap-0"):
            ui.label("Vacation").classes("rbs-type-section-title")
            ui.label(
                "Add as many ranges as needed. Each range can start and end on any day."
            ).classes("rbs-type-caption rbs-text-muted")
        weekday_badge = (
            ui.badge(_weekday_off_label(vacations))
            .props("outline")
            .classes("rbs-muted-badge")
        )

    selected_container = ui.column().classes("w-full gap-2")

    def render_selected() -> None:
        weekday_badge.set_text(_weekday_off_label(vacations))
        selected_container.clear()
        with selected_container:
            if not vacations:
                ui.label("No vacation ranges configured.").classes(
                    "rbs-type-body rbs-text-muted"
                )
                return
            for index, vacation in enumerate(vacations):
                with ui.row().classes(
                    "rbs-attending-vacation-row w-full items-center gap-3 rounded px-4 py-3"
                ):
                    ui.icon("beach_access").classes("rbs-text-secondary")
                    with ui.column().classes("min-w-0 flex-1 gap-0"):
                        ui.label(_vacation_period_label(vacation)).classes("rbs-font-semibold")
                        ui.label(_day_count_label(vacation.days)).classes(
                            "rbs-type-caption rbs-text-muted"
                        )

                    def remove(event=None, *, selected_index: int = index) -> None:
                        vacations.pop(selected_index)
                        render_selected()

                    with ui.button(icon="delete_outline", on_click=remove).props(
                        button_props(
                            DESTRUCTIVE_ICON_BUTTON_PROPS,
                            "aria-label='Remove vacation range "
                            f"{_vacation_period_label(vacation)}'",
                        )
                    ):
                        ui.tooltip("Remove vacation range")
            total_days = sum(vacation.days for vacation in vacations)
            ui.label(
                f"{_vacation_count_label(len(vacations))} · "
                f"{_day_count_label(total_days)} total"
            ).classes("rbs-type-caption rbs-text-muted")

    render_selected()

    with ui.row().classes("w-full items-end gap-3 flex-wrap"):
        start = (
            ui.input("Vacation start date", value=default_day.isoformat())
            .props(
                f"outlined type=date min={first_day.isoformat()} max={last_day.isoformat()}"
            )
            .classes("w-full md:flex-1")
        )
        end = (
            ui.input("Vacation end date", value=default_day.isoformat())
            .props(
                f"outlined type=date min={first_day.isoformat()} max={last_day.isoformat()}"
            )
            .classes("w-full md:flex-1")
        )

        def add_range() -> None:
            try:
                validated = add_vacation_range(
                    instance,
                    vacations,
                    start_value=start.value,
                    end_value=end.value,
                )
                vacations[:] = validated
                render_selected()
            except (ValidationError, ValueError) as exc:
                ui.notify(_validation_message(exc), type="negative", multi_line=True)

        ui.button("Add vacation", icon="add", on_click=add_range).props(
            SECONDARY_BUTTON_PROPS
        )


def _confirm_remove_attending(
    instance: SchedulerInput,
    attending: Attending,
    *,
    on_save: SaveAttending,
) -> None:
    from nicegui import ui

    with ui.dialog() as dialog, ui.card().classes("w-[min(92vw,480px)] p-5"):
        ui.label(f"Remove {attending.name}?").classes("rbs-type-dialog-title")
        ui.label(
            "Their schedule dates, ad hoc work, and vacation ranges will be removed from "
            "this workspace. This cannot be undone."
        ).classes("rbs-type-body rbs-text-muted")
        with ui.row().classes("w-full justify-end gap-3 pt-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")

            def remove() -> None:
                try:
                    updated = remove_attending(instance, attending.id)
                    dialog.close()
                    ui.notify(f"Removed {attending.name}", type="positive")
                    on_save(updated, None)
                except (ValidationError, ValueError) as exc:
                    ui.notify(_validation_message(exc), type="negative", multi_line=True)

            ui.button(
                "Remove attending",
                icon="delete_outline",
                on_click=remove,
            ).props(DESTRUCTIVE_BUTTON_PROPS)
    dialog.open()


def _empty_attending_detail(missing_id: str | None) -> None:
    master_detail.empty_detail(
        icon="person_search",
        title="Attending not found" if missing_id else "Select an attending",
        description=(
            "That attending is no longer in this workspace. Select another attending "
            "from the directory."
            if missing_id
            else (
                "Choose someone from the directory to view their availability, "
                "or add an attending."
            )
        ),
    )


def _attending_avatar(name: str) -> None:
    from nicegui import ui

    parts = [part for part in name.split() if part]
    initials = "?" if not parts else "".join(part[0].upper() for part in parts[:2])
    with ui.avatar(color=None).classes("rbs-attending-avatar"):
        ui.label(initials).classes("rbs-attending-initials")


def _schedule_span_label(instance: SchedulerInput, attending: Attending) -> str:
    if attending.schedule_start_date is None and attending.schedule_end_date is None:
        return "Full academic year"
    first_day, last_day = academic_year_date_range(instance)
    start = attending.effective_schedule_start(first_day)
    end = attending.effective_schedule_end(last_day)
    return f"{start:%b} {start.day}, {start:%Y}–{end:%b} {end.day}, {end:%Y}"


def _attending_summary_label(instance: SchedulerInput, attending: Attending) -> str:
    return " · ".join(
        (
            _schedule_span_label(instance, attending),
            _weekly_work_label(attending),
            _ad_hoc_work_count_label(len(attending.ad_hoc_work_half_days)),
            _vacation_count_label(len(attending.vacation_ranges)),
        )
    )


def _weekly_work_label(attending: Attending) -> str:
    if attending.half_days_per_week == 0:
        if attending.ad_hoc_work_half_days:
            return "Ad hoc only"
        return "No recurring half-days"
    return _half_days_per_week_label(attending.half_days_per_week)


def _vacation_count_label(count: int) -> str:
    if count == 0:
        return "No vacation"
    return f"{count} vacation range" if count == 1 else f"{count} vacation ranges"


def _half_days_per_week_label(count: int) -> str:
    if count == 1:
        return "1 half-day per week"
    return f"{count} half-days per week"


def _ad_hoc_work_count_label(count: int) -> str:
    if count == 0:
        return "No ad hoc work"
    return "1 ad hoc half-day" if count == 1 else f"{count} ad hoc half-days"


def _work_session_label(session: Session) -> str:
    return "Morning (AM)" if session is Session.MORNING else "Afternoon (PM)"


def _day_count_label(count: int) -> str:
    return f"{count} calendar day" if count == 1 else f"{count} calendar days"


def _weekday_off_label(vacations: list[AttendingVacation]) -> str:
    count = sum(vacation.weekdays for vacation in vacations)
    return "1 weekday off" if count == 1 else f"{count} weekdays off"


def _date_label(value: date) -> str:
    return f"{value:%b} {value.day}, {value:%Y}"


def _vacation_period_label(vacation: AttendingVacation) -> str:
    if vacation.start_date == vacation.end_date:
        return f"{vacation.start_date:%a, %b} {vacation.start_date.day}, {vacation.start_date:%Y}"
    return f"{_date_label(vacation.start_date)}–{_date_label(vacation.end_date)}"
