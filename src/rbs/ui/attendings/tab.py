"""Attending directory and availability editor for the workspace UI."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, timedelta
from functools import partial

from pydantic import ValidationError

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
    attending_display_sort_key,
)
from rbs.models.enums import Session, Weekday
from rbs.models.instance import SchedulerInput
from rbs.ui import master_detail, page_shells
from rbs.ui.attendings.ops import (
    AD_HOC_ALL_DAY,
    WEEKLY_SHIFT_TARGET_NONE,
    academic_year_date_range,
    add_ad_hoc_work_half_days,
    add_attending,
    add_vacation_range,
    apply_schedule_template,
    move_work_half_day,
    next_attending_id,
    parse_attending_date,
    remove_attending,
    replace_attending,
    replace_weekly_shift_target,
    replace_weekly_work_schedule,
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
from rbs.ui.half_day_schedule import (
    create_draggable_half_day_event,
    create_half_day_cell,
    render_half_day_grid,
)

SelectAttending = Callable[[str | None], None]
SaveAttending = Callable[[SchedulerInput, str | None], None]
NEW_ATTENDING_ID = "__new_attending__"

_WORK_TYPE_OPTIONS = {
    AttendingWorkType.INPATIENT_SERVICE.value: "Inpatient Service",
    AttendingWorkType.ATTENDING_CLINIC.value: "Attending Clinic",
    AttendingWorkType.PRECEPTING_CLINIC.value: "Precepting Clinic",
    AttendingWorkType.ADMIN_TIME.value: "Admin Time",
    AttendingWorkType.SPECIAL_OTHER.value: "Special/Other",
}

_WEEKLY_TARGET_MODE_OPTIONS = {
    WEEKLY_SHIFT_TARGET_NONE: "No target",
    AttendingWeeklyTargetMode.FIXED.value: "Fixed",
    AttendingWeeklyTargetMode.FLEXIBLE.value: "Flexible",
}


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
            "Manage week-by-week attending schedules, category targets, schedule "
            "dates, ad hoc work, and vacation."
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
                        ui.label("Weekly category targets").classes(
                            "rbs-type-section-title"
                        )
                        ui.label(
                            "Targets describe the intended weekly mix; they do not place "
                            "work blocks."
                        ).classes("rbs-type-caption rbs-text-muted")
                    ui.badge(
                        _weekly_shift_target_count_label(
                            len(attending.weekly_shift_targets)
                        )
                    ).props("outline").classes("rbs-muted-badge")
                _attending_weekly_shift_target_list(attending.weekly_shift_targets)

        with master_detail.detail_card():
            with ui.column().classes("w-full gap-3 p-5"):
                with ui.row().classes("w-full items-center justify-between gap-3"):
                    with ui.column().classes("gap-0"):
                        ui.label("Week-by-week schedule").classes("rbs-type-section-title")
                        ui.label(
                            "Each academic week is independent. Only Precepting Clinic "
                            "work supplies resident clinic capacity."
                        ).classes("rbs-type-caption rbs-text-muted")
                    ui.badge(
                        _configured_week_count_label(
                            len(attending.weekly_work_schedules),
                            instance.calendar.weeks,
                        )
                    ).props("outline").classes("rbs-muted-badge")
                _attending_weekly_work_list(
                    instance,
                    attending.weekly_work_schedules,
                )

        with master_detail.detail_card():
            with ui.column().classes("w-full gap-3 p-5"):
                with ui.row().classes("w-full items-center justify-between gap-3"):
                    with ui.column().classes("gap-0"):
                        ui.label("Schedule template").classes("rbs-type-section-title")
                        ui.label(
                            "This reusable pattern changes the schedule only when it is "
                            "applied to selected weeks."
                        ).classes("rbs-type-caption rbs-text-muted")
                    ui.badge(
                        _template_assignment_count_label(
                            len(attending.schedule_template_half_days),
                            attending.half_days_per_week,
                        )
                    ).props("outline").classes("rbs-muted-badge")
                _attending_work_pattern_list(
                    instance,
                    attending.schedule_template_half_days,
                    empty_title="No template pattern assigned.",
                    empty_description=(
                        "Build a pattern in Edit availability, then apply it to the weeks "
                        "that should use it."
                    ),
                )

        with master_detail.detail_card():
            with ui.column().classes("w-full gap-3 p-5"):
                with ui.row().classes("w-full items-center justify-between gap-3"):
                    with ui.column().classes("gap-0"):
                        ui.label("Ad hoc work").classes("rbs-type-section-title")
                        ui.label(
                            "A dated assignment replaces scheduled work for that half-day."
                        ).classes("rbs-type-caption rbs-text-muted")
                    ui.badge(
                        _ad_hoc_work_count_label(len(attending.ad_hoc_work_half_days))
                    ).props("outline").classes("rbs-muted-badge")
                _attending_ad_hoc_work_list(instance, attending.ad_hoc_work_half_days)

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


def _attending_work_pattern_list(
    instance: SchedulerInput,
    half_days: list[AttendingWorkHalfDay],
    *,
    empty_title: str,
    empty_description: str,
) -> None:
    from nicegui import ui

    if not half_days:
        with ui.row().classes(
            "rbs-attending-work-empty w-full items-center gap-3 rounded px-4 py-4"
        ):
            ui.icon("date_range").classes("rbs-text-subtle")
            with ui.column().classes("gap-0"):
                ui.label(empty_title).classes("rbs-font-semibold")
                ui.label(empty_description).classes("rbs-type-caption rbs-text-muted")
        return
    for half_day in half_days:
        with ui.row().classes(
            "rbs-attending-work-row w-full items-center gap-3 rounded px-4 py-3"
        ):
            ui.icon("calendar_view_week").classes("rbs-text-primary")
            with ui.column().classes("min-w-0 flex-1 gap-0"):
                ui.label(
                    f"{half_day.weekday.value.title()} · "
                    f"{_work_session_label(half_day.session)}"
                ).classes("rbs-font-semibold")
                ui.label(_work_assignment_label(instance, half_day)).classes(
                    "rbs-type-caption rbs-text-muted"
                )


def _attending_weekly_shift_target_list(
    targets: list[AttendingWeeklyShiftTarget],
) -> None:
    from nicegui import ui

    if not targets:
        with ui.row().classes(
            "rbs-attending-work-empty w-full items-center gap-3 rounded px-4 py-4"
        ):
            ui.icon("track_changes").classes("rbs-text-subtle")
            with ui.column().classes("gap-0"):
                ui.label("No weekly category targets.").classes("rbs-font-semibold")
                ui.label(
                    "Each category can remain untargeted or use a Fixed or Flexible target."
                ).classes("rbs-type-caption rbs-text-muted")
        return
    for target in targets:
        with ui.row().classes(
            "rbs-attending-work-row w-full items-center gap-3 rounded px-4 py-3"
        ):
            ui.icon("track_changes").classes("rbs-text-primary")
            with ui.column().classes("min-w-0 flex-1 gap-0"):
                ui.label(_WORK_TYPE_OPTIONS[target.work_type.value]).classes(
                    "rbs-font-semibold"
                )
                ui.label(_shift_target_label(target.shifts_per_week)).classes(
                    "rbs-type-caption rbs-text-muted"
                )
            ui.badge(_weekly_target_mode_label(target.mode)).props("outline")


def _attending_weekly_work_list(
    instance: SchedulerInput,
    schedules: list[AttendingWeeklyWorkSchedule],
) -> None:
    from nicegui import ui

    if not schedules:
        with ui.row().classes(
            "rbs-attending-work-empty w-full items-center gap-3 rounded px-4 py-4"
        ):
            ui.icon("calendar_view_week").classes("rbs-text-subtle")
            with ui.column().classes("gap-0"):
                ui.label("No academic weeks configured.").classes("rbs-font-semibold")
                ui.label(
                    "There is no scheduled work until weeks are edited or a template "
                    "is applied."
                ).classes("rbs-type-caption rbs-text-muted")
        return
    first_day = instance.calendar.first_week_start
    for schedule in schedules:
        week_start = first_day + timedelta(weeks=schedule.week - 1)
        week_end = week_start + timedelta(days=6)
        with ui.row().classes(
            "rbs-attending-work-row w-full items-start gap-3 rounded px-4 py-3"
        ):
            ui.icon("calendar_view_week").classes("rbs-text-primary")
            with ui.column().classes("min-w-0 flex-1 gap-0"):
                ui.label(
                    f"Week {schedule.week} · {_short_week_range_label(week_start, week_end)}"
                ).classes("rbs-font-semibold")
                ui.label(
                    _weekly_assignment_summary(instance, schedule.half_days)
                ).classes("rbs-type-caption rbs-text-muted")
            ui.badge(_assigned_half_day_count_label(len(schedule.half_days))).props(
                "outline"
            ).classes("rbs-muted-badge")


def _attending_ad_hoc_work_list(
    instance: SchedulerInput,
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
                    "Only the saved week-by-week schedule applies to this attending."
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
                ui.label(_work_assignment_label(instance, half_day)).classes(
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
    initial_vacations = list(attending.vacation_ranges) if attending is not None else []
    initial_weekly_shift_targets = (
        list(attending.weekly_shift_targets) if attending is not None else []
    )
    initial_schedule_template = (
        list(attending.schedule_template_half_days) if attending is not None else []
    )
    initial_weekly_schedules = (
        list(attending.weekly_work_schedules) if attending is not None else []
    )
    initial_ad_hoc_work = (
        list(attending.ad_hoc_work_half_days) if attending is not None else []
    )
    weekly_shift_target_controls: list[tuple[AttendingWorkType, object, object]] = []
    controls: dict[str, object] = {}

    def configured_weekly_shift_targets() -> list[AttendingWeeklyShiftTarget]:
        if not weekly_shift_target_controls:
            return initial_weekly_shift_targets
        targets: list[AttendingWeeklyShiftTarget] = []
        for work_type, mode, shifts in weekly_shift_target_controls:
            targets = replace_weekly_shift_target(
                targets,
                work_type_value=work_type.value,
                mode_value=getattr(mode, "value", None),
                shifts_value=getattr(shifts, "value", None),
            )
        return targets

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
                weekly_shift_targets=configured_weekly_shift_targets(),
                schedule_template_half_days=initial_schedule_template,
                weekly_work_schedules=initial_weekly_schedules,
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
        "Add their basic schedule details. Targets, work, and vacation come next."
        if creating
        else (
            f"Update {attending.name}'s targets, weekly schedule, dates, ad hoc work, "
            "and vacation."
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
        if creating:
            with ui.column().classes("w-full gap-5 p-5"):
                _attending_details_editor(
                    instance,
                    attending=None,
                    controls=controls,
                    autofocus=True,
                )
                ui.label(
                    "After adding this attending, use their editor to configure category "
                    "targets, weekly work, a reusable template, ad hoc work, and vacation."
                ).classes("rbs-type-caption rbs-text-muted")
        else:
            with (
                ui.tabs()
                .props("dense no-caps inline-label align=left")
                .classes(
                    "rbs-resident-schedule-tabs rbs-attending-editor-tabs w-full min-w-0"
                ) as editor_tabs
            ):
                details_tab = ui.tab("details", label="Details", icon="person")
                weekly_tab = ui.tab(
                    "weekly_schedule",
                    label="Schedule",
                    icon="calendar_view_week",
                )
                targets_tab = ui.tab(
                    "weekly_targets",
                    label="Targets",
                    icon="track_changes",
                )
                template_tab = ui.tab(
                    "schedule_template",
                    label="Template",
                    icon="content_copy",
                )
                ad_hoc_tab = ui.tab("ad_hoc_work", label="Ad hoc", icon="event")
                vacation_tab = ui.tab("vacation", label="Vacation", icon="beach_access")
            with ui.tab_panels(editor_tabs, value=details_tab).classes(
                "rbs-resident-schedule-panels w-full min-w-0"
            ):
                with ui.tab_panel(details_tab).classes("p-0"):
                    with ui.column().classes("w-full gap-5 p-5"):
                        half_days_per_week = _attending_details_editor(
                            instance,
                            attending=attending,
                            controls=controls,
                        )

                with ui.tab_panel(weekly_tab).classes("p-0"):
                    with ui.column().classes("w-full gap-4 p-5"):
                        refresh_weekly_editor = _weekly_work_editor(
                            instance,
                            initial_weekly_schedules,
                            initial_schedule_template,
                            weekly_target=half_days_per_week,
                        )

                with ui.tab_panel(targets_tab).classes("p-0"):
                    with ui.column().classes("w-full gap-4 p-5"):
                        _weekly_shift_target_editor(
                            initial_weekly_shift_targets,
                            weekly_shift_target_controls,
                        )

                with ui.tab_panel(template_tab).classes("p-0"):
                    with ui.column().classes("w-full gap-4 p-5"):
                        _schedule_template_editor(
                            instance,
                            initial_schedule_template,
                            initial_weekly_schedules,
                            weekly_target=half_days_per_week,
                            on_applied=refresh_weekly_editor,
                        )

                with ui.tab_panel(ad_hoc_tab).classes("p-0"):
                    with ui.column().classes("w-full gap-4 p-5"):
                        _ad_hoc_work_editor(instance, initial_ad_hoc_work)

                with ui.tab_panel(vacation_tab).classes("p-0"):
                    with ui.column().classes("w-full gap-4 p-5"):
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
            "weekly_shift_targets": [
                {
                    "work_type": work_type.value,
                    "mode": getattr(mode, "value", None),
                    "shifts_per_week": (
                        getattr(shifts, "value", None)
                        if getattr(mode, "value", None) != WEEKLY_SHIFT_TARGET_NONE
                        else None
                    ),
                }
                for work_type, mode, shifts in weekly_shift_target_controls
            ],
            "schedule_template": [
                half_day.model_dump(mode="json")
                for half_day in initial_schedule_template
            ],
            "weekly_work_schedules": [
                schedule.model_dump(mode="json")
                for schedule in initial_weekly_schedules
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


def _attending_details_editor(
    instance: SchedulerInput,
    *,
    attending: Attending | None,
    controls: dict[str, object],
    autofocus: bool = False,
) -> object:
    from nicegui import ui

    first_day, last_day = academic_year_date_range(instance)
    initial_start = attending.schedule_start_date if attending is not None else None
    initial_end = attending.schedule_end_date if attending is not None else None

    with ui.column().classes("w-full gap-3"):
        ui.label("Basic information").classes("rbs-type-section-title")
        with ui.row().classes("w-full items-start gap-4 flex-wrap"):
            name = (
                ui.input(
                    "Full name",
                    value=(attending.name if attending is not None else ""),
                )
                .props("outlined autofocus" if autofocus else "outlined")
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
        if autofocus:
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
        end_default.on_value_change(lambda event: end.set_enabled(not bool(event.value)))
        controls.update(
            {
                "default_start": start_default,
                "start": start,
                "default_end": end_default,
                "end": end,
            }
        )
    return half_days_per_week


def _weekly_shift_target_editor(
    targets: list[AttendingWeeklyShiftTarget],
    controls: list[tuple[AttendingWorkType, object, object]],
) -> None:
    from nicegui import ui

    by_work_type = {target.work_type: target for target in targets}
    with ui.column().classes("w-full gap-0"):
        ui.label("Weekly category targets").classes("rbs-type-section-title")
        ui.label(
            "A Fixed target is an exact requirement. A Flexible target is a preference "
            "that may vary. Targets describe the weekly mix but do not place work blocks."
        ).classes("rbs-type-caption rbs-text-muted")

    for work_type in AttendingWorkType:
        category_label = _WORK_TYPE_OPTIONS[work_type.value]
        configured = by_work_type.get(work_type)
        with ui.column().classes(
            "rbs-attending-form-section w-full gap-3 rounded p-4"
        ):
            with ui.row().classes("w-full items-center gap-3"):
                ui.icon("track_changes").classes("rbs-text-primary")
                ui.label(category_label).classes("rbs-type-section-title")
            with ui.row().classes("w-full items-start gap-3 flex-wrap"):
                mode = (
                    ui.select(
                        _WEEKLY_TARGET_MODE_OPTIONS,
                        value=(
                            configured.mode.value
                            if configured is not None
                            else WEEKLY_SHIFT_TARGET_NONE
                        ),
                        label="Target type",
                    )
                    .props(
                        "outlined options-dense "
                        f"aria-label='{category_label} target type'"
                    )
                    .classes("w-full md:flex-1")
                )
                shifts = (
                    ui.number(
                        "Shifts per week",
                        value=(configured.shifts_per_week if configured is not None else 0),
                        min=0,
                        max=MAX_ATTENDING_HALF_DAYS_PER_WEEK,
                        step=1,
                        precision=0,
                    )
                    .props(f"outlined aria-label='{category_label} shifts per week'")
                    .classes("w-full md:w-48")
                )
                shifts.set_enabled(configured is not None)
                mode.on_value_change(
                    lambda event, target_count=shifts: target_count.set_enabled(
                        event.value != WEEKLY_SHIFT_TARGET_NONE
                    )
                )
                controls.append((work_type, mode, shifts))

def _weekly_work_editor(
    instance: SchedulerInput,
    schedules: list[AttendingWeeklyWorkSchedule],
    template: list[AttendingWorkHalfDay],
    *,
    weekly_target: object,
) -> Callable[[], None]:
    from nicegui import ui

    def target_value() -> int:
        value = getattr(weekly_target, "value", DEFAULT_ATTENDING_HALF_DAYS_PER_WEEK)
        return int(value if value is not None else DEFAULT_ATTENDING_HALF_DAYS_PER_WEEK)

    with ui.row().classes("w-full items-center justify-between gap-3"):
        with ui.column().classes("gap-0"):
            ui.label("Week-by-week schedule").classes("rbs-type-section-title")
            ui.label(
                "Edit each academic week independently. An unscheduled slot means the "
                "attending does not work that half-day."
            ).classes("rbs-type-caption rbs-text-muted")
        count_badge = (
            ui.badge(
                _configured_week_count_label(len(schedules), instance.calendar.weeks)
            )
            .props("outline")
            .classes("rbs-muted-badge")
        )

    week_select = (
        ui.select(
            _academic_week_options(instance),
            value=1,
            label="Academic week",
        )
        .props("outlined options-dense")
        .classes("w-full md:w-80")
    )
    week_container = ui.column().classes("w-full gap-3")

    def selected_week() -> int:
        return int(getattr(week_select, "value", 1) or 1)

    def schedule_for(week: int) -> AttendingWeeklyWorkSchedule | None:
        return next((schedule for schedule in schedules if schedule.week == week), None)

    def refresh_count() -> None:
        count_badge.set_text(
            _configured_week_count_label(len(schedules), instance.calendar.weeks)
        )

    def render_week() -> None:
        week = selected_week()
        week_container.clear()
        schedule = schedule_for(week)
        half_days = list(schedule.half_days) if schedule is not None else []
        week_start = instance.calendar.first_week_start + timedelta(weeks=week - 1)
        week_end = week_start + timedelta(days=6)
        with week_container:
            with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
                with ui.column().classes("gap-0"):
                    ui.label(
                        f"Week {week} · {_short_week_range_label(week_start, week_end)}"
                    ).classes("rbs-font-semibold")
                    status_label = ui.label(
                        "This week has its own saved schedule."
                        if schedule is not None
                        else "Not configured — no work is scheduled in this week."
                    ).classes("rbs-type-caption rbs-text-muted")
                assigned_badge = (
                    ui.badge(
                        _assigned_half_day_target_label(len(half_days), target_value())
                    )
                    .props("outline")
                    .classes("rbs-muted-badge")
                )
            with ui.row().classes("w-full items-center gap-2 flex-wrap"):

                def apply_to_week() -> None:
                    def replace() -> None:
                        try:
                            schedules[:] = replace_weekly_work_schedule(
                                instance,
                                schedules,
                                week=week,
                                half_days=template,
                            )
                            refresh_count()
                            render_week()
                            ui.notify(f"Applied the template to week {week}", type="positive")
                        except (ValidationError, ValueError) as exc:
                            ui.notify(
                                _validation_message(exc),
                                type="negative",
                                multi_line=True,
                            )

                    current = schedule_for(week)
                    if current is not None and current.half_days != template:
                        _confirm_draft_schedule_replacement(
                            title=f"Replace week {week}?",
                            description=(
                                "Applying the template replaces every saved AM and PM "
                                "assignment in this week."
                            ),
                            confirm_label="Replace week",
                            on_confirm=replace,
                        )
                    else:
                        replace()

                ui.button(
                    "Apply template to this week",
                    icon="content_copy",
                    on_click=apply_to_week,
                ).props(SECONDARY_BUTTON_PROPS)

                def clear_week() -> None:
                    current = schedule_for(week)
                    if current is None or not current.half_days:
                        return

                    def clear() -> None:
                        schedules[:] = replace_weekly_work_schedule(
                            instance,
                            schedules,
                            week=week,
                            half_days=[],
                        )
                        refresh_count()
                        render_week()
                        ui.notify(f"Cleared week {week}", type="positive")

                    _confirm_draft_schedule_replacement(
                        title=f"Clear week {week}?",
                        description=(
                            "Every saved AM and PM assignment in this week will be "
                            "removed from the draft."
                        ),
                        confirm_label="Clear week",
                        on_confirm=clear,
                    )

                clear_button = ui.button(
                    "Clear this week",
                    icon="event_busy",
                    on_click=clear_week,
                ).props(DESTRUCTIVE_BUTTON_PROPS)
                clear_button.set_enabled(
                    schedule is not None and bool(schedule.half_days)
                )

            def save_half_days(updated: list[AttendingWorkHalfDay]) -> None:
                schedules[:] = replace_weekly_work_schedule(
                    instance,
                    schedules,
                    week=week,
                    half_days=updated,
                )
                status_label.set_text("This week has its own saved schedule.")
                assigned_badge.set_text(
                    _assigned_half_day_target_label(len(updated), target_value())
                )
                clear_button.set_enabled(bool(updated))
                refresh_count()

            ui.label(
                "Click a half-day to assign or edit it. Drag work blocks to move "
                "or swap them within this week."
            ).classes("rbs-type-caption rbs-text-muted")
            _work_schedule_grid(
                instance,
                half_days,
                on_change=save_half_days,
                scope=f"attending-week-{week}",
                week=week,
                day_details={
                    weekday: (
                        f"{week_start + timedelta(days=index):%b} "
                        f"{(week_start + timedelta(days=index)).day}"
                    )
                    for index, weekday in enumerate(Weekday)
                },
                context_label=f"week {week}",
            )

    week_select.on_value_change(lambda _event: render_week())
    weekly_target.on_value_change(lambda _event: render_week())
    render_week()
    return render_week


def _schedule_template_editor(
    instance: SchedulerInput,
    template: list[AttendingWorkHalfDay],
    schedules: list[AttendingWeeklyWorkSchedule],
    *,
    weekly_target: object,
    on_applied: Callable[[], None],
) -> None:
    from nicegui import ui

    def target_value() -> int:
        value = getattr(weekly_target, "value", DEFAULT_ATTENDING_HALF_DAYS_PER_WEEK)
        return int(value if value is not None else DEFAULT_ATTENDING_HALF_DAYS_PER_WEEK)

    with ui.row().classes("w-full items-center justify-between gap-3"):
        with ui.column().classes("gap-0"):
            ui.label("Schedule template").classes("rbs-type-section-title")
            ui.label(
                "Build a reusable pattern, then copy it into a week range. Applied "
                "weeks remain independent when this template changes later."
            ).classes("rbs-type-caption rbs-text-muted")
        template_badge = (
            ui.badge(_template_assignment_count_label(len(template), target_value()))
            .props("outline")
            .classes("rbs-muted-badge")
        )

    def template_changed(updated: list[AttendingWorkHalfDay]) -> None:
        template[:] = updated
        template_badge.set_text(
            _template_assignment_count_label(len(template), target_value())
        )

    weekly_target.on_value_change(
        lambda _event: template_badge.set_text(
            _template_assignment_count_label(len(template), target_value())
        )
    )
    ui.label(
        "Click a half-day to assign or edit it. Drag work blocks to move or swap "
        "them within the template."
    ).classes("rbs-type-caption rbs-text-muted")
    _work_schedule_grid(
        instance,
        template,
        on_change=template_changed,
        scope="attending-template",
        week=0,
        context_label="schedule template",
    )

    ui.separator()
    with ui.column().classes("w-full gap-3"):
        ui.label("Apply template").classes("rbs-font-semibold")
        ui.label(
            "Applying replaces the saved schedule in every selected week. It makes "
            "copies, not a continuing link to the template."
        ).classes("rbs-type-caption rbs-text-muted")
        week_options = _academic_week_options(instance)
        with ui.row().classes("w-full items-end gap-3 flex-wrap"):
            first_week = (
                ui.select(week_options, value=1, label="First week")
                .props("outlined options-dense")
                .classes("w-full md:flex-1")
            )
            last_week = (
                ui.select(
                    week_options,
                    value=instance.calendar.weeks,
                    label="Last week",
                )
                .props("outlined options-dense")
                .classes("w-full md:flex-1")
            )

            def apply_range() -> None:
                def apply() -> None:
                    try:
                        schedules[:] = apply_schedule_template(
                            instance,
                            schedules,
                            template,
                            first_week=first_week.value,
                            last_week=last_week.value,
                        )
                        on_applied()
                        first = int(first_week.value)
                        last = int(last_week.value)
                        ui.notify(
                            _template_applied_label(first, last),
                            type="positive",
                        )
                    except (TypeError, ValidationError, ValueError) as exc:
                        ui.notify(
                            _validation_message(exc),
                            type="negative",
                            multi_line=True,
                        )

                try:
                    first = int(first_week.value)
                    last = int(last_week.value)
                except (TypeError, ValueError):
                    apply()
                    return
                replaced = sum(first <= schedule.week <= last for schedule in schedules)
                if replaced:
                    _confirm_draft_schedule_replacement(
                        title="Replace configured weeks?",
                        description=(
                            f"This replaces {replaced} already-configured "
                            f"{'week' if replaced == 1 else 'weeks'} in the selected range."
                        ),
                        confirm_label="Apply and replace",
                        on_confirm=apply,
                    )
                else:
                    apply()

            ui.button(
                "Apply template to range",
                icon="content_copy",
                on_click=apply_range,
            ).props(PRIMARY_BUTTON_PROPS)


def _work_schedule_grid(
    instance: SchedulerInput,
    half_days: list[AttendingWorkHalfDay],
    *,
    on_change: Callable[[list[AttendingWorkHalfDay]], None],
    scope: str,
    week: int,
    day_details: Mapping[Weekday, str] | None = None,
    context_label: str,
) -> None:
    from nicegui import ui

    grid_container = ui.column().classes("w-full min-w-0 gap-0")

    def update_half_days(updated: list[AttendingWorkHalfDay]) -> None:
        normalized = AttendingWeeklyWorkSchedule(week=1, half_days=updated)
        half_days[:] = normalized.half_days
        on_change(list(half_days))
        render_grid()

    def replace_slot(assignment: AttendingWorkHalfDay) -> None:
        updated = [
            half_day
            for half_day in half_days
            if (half_day.weekday, half_day.session)
            != (assignment.weekday, assignment.session)
        ]
        update_half_days([*updated, assignment])

    def remove_slot(weekday: Weekday, session: Session) -> None:
        update_half_days(
            [
                half_day
                for half_day in half_days
                if (half_day.weekday, half_day.session) != (weekday, session)
            ]
        )

    def drop_on(
        event,
        *,
        target_weekday: Weekday,
        target_session: Session,
    ) -> None:
        payload = event.args if isinstance(event.args, dict) else {}
        try:
            if str(payload.get("scope", "")) != scope:
                return
            if int(payload.get("week")) != week:
                return
            updated = move_work_half_day(
                half_days,
                source_weekday=Weekday(str(payload.get("weekday"))),
                source_session=Session(str(payload.get("session"))),
                target_weekday=target_weekday,
                target_session=target_session,
            )
            if updated == half_days:
                return
            update_half_days(updated)
            ui.notify("Work half-day moved", type="positive")
        except (TypeError, ValidationError, ValueError) as exc:
            ui.notify(
                f"Work half-day not moved: {_validation_message(exc)}",
                type="warning",
                multi_line=True,
            )

    def open_slot(
        weekday: Weekday,
        session: Session,
        _event: object | None = None,
    ) -> None:
        assignment = next(
            (
                half_day
                for half_day in half_days
                if (half_day.weekday, half_day.session) == (weekday, session)
            ),
            None,
        )
        _work_half_day_dialog(
            instance,
            weekday=weekday,
            session=session,
            assignment=assignment,
            context_label=context_label,
            on_save=replace_slot,
            on_remove=partial(remove_slot, weekday, session),
        )

    def render_cell(weekday: Weekday, session: Session) -> None:
        assignment = next(
            (
                half_day
                for half_day in half_days
                if (half_day.weekday, half_day.session) == (weekday, session)
            ),
            None,
        )
        action = "Assign" if assignment is None else "Edit"
        accessible_name = (
            f"{action} {weekday.value.title()} {_work_session_label(session)} "
            f"in {context_label}"
        )
        cell = create_half_day_cell(
            classes="is-occupied" if assignment is not None else "",
            on_drop=partial(
                drop_on,
                target_weekday=weekday,
                target_session=session,
            ),
            on_click=(partial(open_slot, weekday, session) if assignment is None else None),
            accessible_name=(accessible_name if assignment is None else None),
        )
        with cell:
            if assignment is None:
                ui.icon("add").classes("rbs-resident-clinic-session-empty")
                return
            event = create_draggable_half_day_event(
                classes=_work_event_class(assignment),
                draggable=True,
                week=week,
                weekday=weekday,
                session=session,
                scope=scope,
                on_click=partial(open_slot, weekday, session),
                accessible_name=accessible_name,
            )
            if assignment.clinic_id is not None:
                site = instance.clinic_policy.site(assignment.clinic_id)
                event.style(
                    f"--rbs-clinic-site-color: {site.color}; "
                    f"--rbs-clinic-site-tint: {site.light_color}"
                )
            with event:
                ui.label(_work_assignment_label(instance, assignment)).classes(
                    "rbs-resident-clinic-event-location"
                )
                with ui.icon("drag_indicator").classes(
                    "rbs-resident-clinic-event-lock rbs-text-muted"
                ):
                    ui.tooltip("Drag to move or swap this work half-day")

    def render_grid() -> None:
        grid_container.clear()
        with grid_container, ui.element("div").classes(
            "rbs-resident-clinic-calendar-list is-editing w-full min-w-0"
        ):
            with ui.element("section").classes("rbs-resident-clinic-week w-full"):
                render_half_day_grid(
                    tuple(Weekday),
                    render_cell=render_cell,
                    day_details=day_details,
                )

    render_grid()


def _work_half_day_dialog(
    instance: SchedulerInput,
    *,
    weekday: Weekday,
    session: Session,
    assignment: AttendingWorkHalfDay | None,
    context_label: str,
    on_save: Callable[[AttendingWorkHalfDay], None],
    on_remove: Callable[[], None],
) -> None:
    from nicegui import ui

    clinic_options = {site.id: site.name for site in instance.clinic_policy.sites}
    initial_type = assignment.work_type.value if assignment is not None else None
    initial_clinic = (
        assignment.clinic_id
        if assignment is not None and assignment.clinic_id is not None
        else instance.clinic_policy.primary_site_id
    )
    title = "Edit work half-day" if assignment is not None else "Assign work half-day"
    with ui.dialog() as dialog, ui.card().classes("w-[min(92vw,520px)] p-0 gap-0"):
        with ui.row().classes("w-full items-center justify-between gap-3 px-5 py-4"):
            with ui.column().classes("gap-0"):
                ui.label(title).classes("rbs-type-dialog-title")
                ui.label(
                    f"{weekday.value.title()} · {_work_session_label(session)} · "
                    f"{context_label}"
                ).classes("rbs-type-caption rbs-text-muted")
            with ui.button(icon="close", on_click=dialog.close).props(
                button_props(
                    ICON_BUTTON_PROPS,
                    "aria-label='Close work half-day dialog'",
                )
            ):
                ui.tooltip("Close work half-day dialog")
        ui.separator()
        with ui.column().classes("w-full gap-4 p-5"):
            work_type = (
                ui.select(
                    _WORK_TYPE_OPTIONS,
                    value=initial_type,
                    label="Work type",
                )
                .props("outlined options-dense")
                .classes("w-full")
            )
            clinic = (
                ui.select(
                    clinic_options,
                    value=initial_clinic,
                    label="Clinic",
                )
                .props("outlined options-dense")
                .classes("w-full")
            )
            clinic.set_visibility(
                initial_type == AttendingWorkType.PRECEPTING_CLINIC.value
            )

            def change_work_type(event) -> None:
                is_precepting = (
                    event.value == AttendingWorkType.PRECEPTING_CLINIC.value
                )
                clinic.set_visibility(is_precepting)
                if is_precepting and clinic.value is None:
                    clinic.value = instance.clinic_policy.primary_site_id

            work_type.on_value_change(change_work_type)

            with ui.row().classes("w-full items-center justify-between gap-3 pt-1"):
                if assignment is not None:

                    def remove() -> None:
                        dialog.close()
                        on_remove()

                    ui.button(
                        "Remove assignment",
                        icon="delete_outline",
                        on_click=remove,
                    ).props(DESTRUCTIVE_BUTTON_PROPS)
                else:
                    ui.element("span")

                with ui.row().classes("items-center gap-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat no-caps")

                    def save() -> None:
                        try:
                            if work_type.value in (None, ""):
                                raise ValueError("select a work type")
                            selected_type = AttendingWorkType(work_type.value)
                            saved = AttendingWorkHalfDay(
                                weekday=weekday,
                                session=session,
                                work_type=selected_type,
                                clinic_id=(
                                    str(clinic.value)
                                    if selected_type
                                    is AttendingWorkType.PRECEPTING_CLINIC
                                    and clinic.value not in (None, "")
                                    else None
                                ),
                            )
                            dialog.close()
                            on_save(saved)
                        except (ValidationError, ValueError) as exc:
                            ui.notify(
                                _validation_message(exc),
                                type="negative",
                                multi_line=True,
                            )

                    ui.button(
                        "Save assignment" if assignment is not None else "Assign half-day",
                        icon="save" if assignment is not None else "add",
                        on_click=save,
                    ).props(PRIMARY_BUTTON_PROPS)
    dialog.open()


def _work_event_class(assignment: AttendingWorkHalfDay) -> str:
    if assignment.work_type is AttendingWorkType.ADMIN_TIME:
        return "admin"
    if assignment.work_type is AttendingWorkType.SPECIAL_OTHER:
        return "special-event"
    if assignment.work_type is AttendingWorkType.INPATIENT_SERVICE:
        return "academic"
    return "attending-work"


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
                "Add dated morning or afternoon work. It replaces any week-by-week "
                "assignment in that exact half-day."
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
                        ui.label(_work_assignment_label(instance, half_day)).classes(
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
        work_type = (
            ui.select(
                _WORK_TYPE_OPTIONS,
                value=AttendingWorkType.SPECIAL_OTHER.value,
                label="Work type",
            )
            .props("outlined options-dense")
            .classes("w-full md:w-56")
        )
        clinic = (
            ui.select(
                {site.id: site.name for site in instance.clinic_policy.sites},
                value=None,
                label="Clinic",
            )
            .props("outlined options-dense")
            .classes("w-full md:w-56")
        )
        clinic.set_visibility(False)

        def change_work_type(event) -> None:
            is_precepting = event.value == AttendingWorkType.PRECEPTING_CLINIC.value
            clinic.set_visibility(is_precepting)
            if is_precepting and clinic.value is None:
                clinic.value = instance.clinic_policy.primary_site_id
            elif not is_precepting:
                clinic.value = None

        work_type.on_value_change(change_work_type)

        def add_work() -> None:
            try:
                validated = add_ad_hoc_work_half_days(
                    instance,
                    half_days,
                    date_value=work_date.value,
                    session_value=work_time.value,
                    work_type_value=work_type.value,
                    clinic_id_value=clinic.value,
                )
                half_days[:] = validated
                render_selected()
            except (ValidationError, ValueError) as exc:
                ui.notify(_validation_message(exc), type="negative", multi_line=True)

        ui.button("Add work half-day", icon="add", on_click=add_work).props(
            SECONDARY_BUTTON_PROPS
        )


def _confirm_draft_schedule_replacement(
    *,
    title: str,
    description: str,
    confirm_label: str,
    on_confirm: Callable[[], None],
) -> None:
    from nicegui import ui

    with ui.dialog() as dialog, ui.card().classes("w-[min(92vw,480px)] p-5"):
        ui.label(title).classes("rbs-type-dialog-title")
        ui.label(description).classes("rbs-type-body rbs-text-muted")
        ui.label(
            "You can still cancel the attending editor to discard this draft change."
        ).classes("rbs-type-caption rbs-text-muted")
        with ui.row().classes("w-full justify-end gap-3 pt-2"):
            ui.button("Keep current schedule", on_click=dialog.close).props("flat no-caps")

            def confirm() -> None:
                dialog.close()
                on_confirm()

            ui.button(confirm_label, on_click=confirm).props(
                DESTRUCTIVE_BUTTON_PROPS
            )
    dialog.open()


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
            "Their weekly schedules, template, schedule dates, ad hoc work, and vacation "
            "ranges will be removed from this workspace. This cannot be undone."
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
            _configured_week_count_label(
                len(attending.weekly_work_schedules),
                instance.calendar.weeks,
            ),
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


def _configured_week_count_label(count: int, total: int) -> str:
    return f"{count} of {total} weeks configured"


def _assigned_half_day_count_label(count: int) -> str:
    return "1 half-day assigned" if count == 1 else f"{count} half-days assigned"


def _assigned_half_day_target_label(count: int, target: int) -> str:
    return f"{count} of {target} half-days assigned"


def _template_assignment_count_label(count: int, target: int) -> str:
    return f"{count} of {target} template half-days"


def _weekly_shift_target_count_label(count: int) -> str:
    if count == 0:
        return "No category targets"
    return f"{count} of {len(AttendingWorkType)} categories targeted"


def _shift_target_label(count: int) -> str:
    return "1 shift per week" if count == 1 else f"{count} shifts per week"


def _weekly_target_mode_label(mode: AttendingWeeklyTargetMode) -> str:
    return "Fixed" if mode is AttendingWeeklyTargetMode.FIXED else "Flexible"


def _academic_week_options(instance: SchedulerInput) -> dict[int, str]:
    first_day = instance.calendar.first_week_start
    return {
        week: f"Week {week} · {_academic_week_date_range_label(first_day, week)}"
        for week in range(1, instance.calendar.weeks + 1)
    }


def _academic_week_date_range_label(first_day: date, week: int) -> str:
    week_start = first_day + timedelta(weeks=week - 1)
    return _short_week_range_label(week_start, week_start + timedelta(days=6))


def _short_week_range_label(first_day: date, last_day: date) -> str:
    if first_day.year == last_day.year:
        return (
            f"{first_day:%b} {first_day.day}–{last_day:%b} {last_day.day}, "
            f"{last_day:%Y}"
        )
    return (
        f"{first_day:%b} {first_day.day}, {first_day:%Y}–"
        f"{last_day:%b} {last_day.day}, {last_day:%Y}"
    )


def _weekly_assignment_summary(
    instance: SchedulerInput,
    half_days: list[AttendingWorkHalfDay],
) -> str:
    if not half_days:
        return "No scheduled work"
    return "; ".join(
        f"{half_day.weekday.value[:3].title()} "
        f"{'AM' if half_day.session is Session.MORNING else 'PM'} · "
        f"{_work_assignment_label(instance, half_day)}"
        for half_day in half_days
    )


def _template_applied_label(first_week: int, last_week: int) -> str:
    if first_week == last_week:
        return f"Applied the template to week {first_week}"
    return f"Applied the template to weeks {first_week}–{last_week}"


def _work_session_label(session: Session) -> str:
    return "Morning (AM)" if session is Session.MORNING else "Afternoon (PM)"


def _work_assignment_label(
    instance: SchedulerInput,
    assignment: AttendingWorkHalfDay | AttendingAdHocWorkHalfDay,
) -> str:
    label = _WORK_TYPE_OPTIONS[assignment.work_type.value]
    if assignment.clinic_id is not None:
        return f"{label} · {instance.clinic_policy.site_name(assignment.clinic_id)}"
    return label


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
