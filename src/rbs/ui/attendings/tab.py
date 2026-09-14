"""Attending directory and scheduling editor for the workspace UI."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, timedelta
from functools import partial

from pydantic import ValidationError

from rbs.attending_schedule import (
    AttendingScheduleIssueSeverity,
    AttendingScheduleReport,
    attending_schedule_report,
)
from rbs.models.attending import (
    ATTENDING_PREFERRED_WORK_TYPES,
    ATTENDING_WEEKLY_TARGET_WORK_TYPES,
    ATTENDING_WORK_DESCRIPTION_MAX_LENGTH,
    DEFAULT_ATTENDING_HALF_DAYS_PER_WEEK,
    MAX_ATTENDING_CLINIC_DAYS_PER_WEEK,
    MAX_ATTENDING_HALF_DAYS_PER_WEEK,
    Attending,
    AttendingSchedule,
    AttendingVacation,
    AttendingWeeklyShiftTarget,
    AttendingWeeklyTargetMode,
    AttendingWeeklyWorkSchedule,
    AttendingWorkHalfDay,
    AttendingWorkType,
    EffectiveAttendingWeek,
    attending_display_sort_key,
    effective_attending_week,
)
from rbs.models.enums import Session, Weekday
from rbs.models.instance import SchedulerInput
from rbs.ui import master_detail, page_shells
from rbs.ui.attendings.ops import (
    WEEKLY_SHIFT_TARGET_NONE,
    academic_year_date_range,
    add_attending,
    add_vacation_range,
    apply_schedule_template,
    clear_weekly_work_schedule,
    move_work_half_day,
    next_attending_id,
    override_weekly_half_day_total,
    parse_attending_date,
    remove_attending,
    replace_attending_with_schedule,
    replace_weekly_shift_target,
    replace_weekly_work_schedule,
    use_default_weekly_half_day_total,
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
            "Manage week-by-week attending schedules, preferred weekly patterns, "
            "category targets, schedule dates, weekly overrides, and vacation."
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
            or query in _attending_card_summary_label(attending).casefold()
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
                        description="Add an attending to configure their schedule.",
                    )
                return
            master_detail.directory_heading("Attendings", len(filtered))
            with ui.list().props("separator").classes("w-full"):
                for attending in sorted(filtered, key=attending_display_sort_key):
                    _attending_list_item(
                        attending,
                        selected_attending_id,
                        on_select,
                    )

    search.on_value_change(lambda: render_directory())
    render_directory()


def _attending_list_item(
    attending: Attending,
    selected_attending_id: str | None,
    on_select: SelectAttending,
) -> None:
    master_detail.person_directory_item(
        attending.name,
        _attending_card_summary_label(attending),
        selected=attending.id == selected_attending_id,
        on_click=partial(on_select, attending.id),
        render_avatar=partial(_attending_avatar, attending.name),
    )


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
    on_save: SaveAttending,
) -> None:
    from nicegui import ui

    first_day, last_day = academic_year_date_range(instance)
    attending_schedule = instance.attending_schedule_for(attending.id)
    weekly_schedules = attending_schedule.weeks if attending_schedule is not None else []
    schedule_report = attending_schedule_report(
        instance,
        attending_id=attending.id,
    )
    with ui.column().classes("w-full gap-4"):
        with master_detail.detail_card():
            with ui.row().classes("rbs-attending-summary w-full items-center gap-4 p-4"):
                _attending_avatar(attending.name)
                with ui.column().classes("min-w-0 gap-0"):
                    ui.label(attending.name).classes("rbs-type-dialog-title")
                    ui.label(_attending_card_summary_label(attending)).classes(
                        "rbs-type-body rbs-text-muted"
                    )
                ui.space()
                ui.button("Edit attending", icon="edit", on_click=on_edit).props(
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
                            "Fixed ranges are required and Flexible ranges are preferred. "
                            "Targets do not place work blocks. Vacation weeks are excluded "
                            "from the Attending Clinic day minimum."
                        ).classes("rbs-type-caption rbs-text-muted")
                    ui.badge(
                        _weekly_shift_target_count_label(
                            len(attending.weekly_shift_targets)
                        )
                    ).props("outline").classes("rbs-muted-badge")
                with ui.row().classes(
                    "rbs-attending-work-row w-full items-center gap-3 rounded px-4 py-3"
                ):
                    ui.icon("calendar_view_week").classes("rbs-text-primary")
                    with ui.column().classes("min-w-0 flex-1 gap-0"):
                        ui.label("Attending Clinic days").classes("rbs-font-semibold")
                        ui.label(
                            _minimum_attending_clinic_days_label(
                                attending.minimum_attending_clinic_days_per_week
                            )
                        ).classes("rbs-type-caption rbs-text-muted")
                _attending_weekly_shift_target_list(attending.weekly_shift_targets)

        with master_detail.detail_card():
            with ui.column().classes("w-full gap-3 p-5"):
                with ui.row().classes("w-full items-center justify-between gap-3"):
                    with ui.column().classes("gap-0"):
                        ui.label("Preferred weekly schedule").classes(
                            "rbs-type-section-title"
                        )
                        ui.label(
                            "A soft placement preference that does not create scheduled "
                            "work or clinic capacity."
                        ).classes("rbs-type-caption rbs-text-muted")
                    ui.badge(
                        _preferred_schedule_count_label(
                            len(attending.preferred_weekly_schedule_half_days),
                            attending.half_days_per_week,
                        )
                    ).props("outline").classes("rbs-muted-badge")
                _attending_work_pattern_list(
                    instance,
                    attending.preferred_weekly_schedule_half_days,
                    empty_title="No preferred weekly schedule.",
                    empty_description=(
                        "Open Edit attending to choose preferred weekday and AM/PM "
                        "placements."
                    ),
                )

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
                            len(weekly_schedules),
                            instance.calendar.weeks,
                        )
                    ).props("outline").classes("rbs-muted-badge")
                if instance.clinic_policy.academic_half_day_is_attending_admin_time:
                    with ui.row().classes(
                        "rbs-attending-work-row w-full items-center gap-3 rounded px-4 py-3"
                    ):
                        ui.icon("school").classes("rbs-text-primary")
                        ui.label(
                            "The effective academic half-day is automatically reserved "
                            "as Admin Time in active, non-vacation weeks."
                        ).classes("rbs-type-caption rbs-text-muted")
                _attending_weekly_work_list(
                    instance,
                    attending,
                )

        with master_detail.detail_card():
            with ui.column().classes("w-full gap-3 p-5"):
                with ui.row().classes(
                    "w-full items-center justify-between gap-3"
                ):
                    with ui.column().classes("gap-0"):
                        ui.label("Schedule checks").classes(
                            "rbs-type-section-title"
                        )
                        ui.label(
                            "Fixed rules are errors; Flexible targets and preferred "
                            "placements are warnings."
                        ).classes("rbs-type-caption rbs-text-muted")
                    ui.badge(
                        _attending_schedule_issue_count_label(
                            len(schedule_report.errors),
                            len(schedule_report.warnings),
                        )
                    ).props("outline").classes("rbs-muted-badge")
                _attending_schedule_issue_list(schedule_report)

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
                        "Open Edit attending to build a pattern, then apply it to the "
                        "weeks that should use it."
                    ),
                )

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
                    "Recurring categories can remain untargeted or use a Fixed or "
                    "Flexible range. Special/Other work is set on the schedule."
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
                ui.label(
                    _shift_target_range_label(
                        target.minimum_shifts_per_week,
                        target.maximum_shifts_per_week,
                    )
                ).classes(
                    "rbs-type-caption rbs-text-muted"
                )
            ui.badge(_weekly_target_mode_label(target.mode)).props("outline")


def _attending_weekly_work_list(
    instance: SchedulerInput,
    attending: Attending,
) -> None:
    from nicegui import ui

    attending_schedule = instance.attending_schedule_for(attending.id)
    schedules = attending_schedule.weeks if attending_schedule is not None else []
    if not schedules:
        empty_description = (
            "Automatic academic Admin Time still applies. Add other work by "
            "editing a week or applying the template."
            if instance.clinic_policy.academic_half_day_is_attending_admin_time
            else "Add work by editing a week or applying the template."
        )
        with ui.row().classes(
            "rbs-attending-work-empty w-full items-center gap-3 rounded px-4 py-4"
        ):
            ui.icon("calendar_view_week").classes("rbs-text-subtle")
            with ui.column().classes("gap-0"):
                ui.label("No week-specific work configured.").classes(
                    "rbs-font-semibold"
                )
                ui.label(empty_description).classes(
                    "rbs-type-caption rbs-text-muted"
                )
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
            with ui.column().classes("items-end gap-1"):
                ui.badge(
                    _assigned_half_day_target_label(
                        _configured_assignment_count_with_academic_admin(
                            instance,
                            attending,
                            schedule,
                        ),
                        schedule.effective_half_days(
                            attending.half_days_per_week
                        ),
                    )
                ).props("outline").classes("rbs-muted-badge")
                if schedule.half_days_override is not None:
                    ui.badge("Week override").props("outline").classes(
                        "rbs-muted-badge"
                    )


def _configured_assignment_count_with_academic_admin(
    instance: SchedulerInput,
    attending: Attending,
    schedule: AttendingWeeklyWorkSchedule,
) -> int:
    return instance.effective_attending_week(
        attending,
        schedule.week,
    ).assigned_half_days


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
                ui.label(_weekday_count_label(vacation.weekdays)).classes(
                    "rbs-type-caption rbs-text-muted"
                )


def _attending_schedule_issue_list(report: AttendingScheduleReport) -> None:
    from nicegui import ui

    if not report.issues:
        with ui.row().classes(
            "rbs-attending-work-row w-full items-center gap-3 rounded px-4 py-3"
        ):
            ui.icon("check_circle").classes("rbs-text-primary")
            ui.label("The attending schedule matches its configured rules.").classes(
                "rbs-type-caption rbs-text-muted"
            )
        return

    shown = report.issues[:5]
    for issue in shown:
        is_error = issue.severity is AttendingScheduleIssueSeverity.ERROR
        with ui.row().classes(
            "rbs-attending-work-row w-full items-start gap-3 rounded px-4 py-3"
        ):
            ui.icon("error_outline" if is_error else "warning_amber").classes(
                "rbs-text-danger" if is_error else "rbs-text-warning"
            )
            ui.label(issue.message).classes("rbs-type-caption rbs-text-muted")
    remaining = len(report.issues) - len(shown)
    if remaining:
        ui.label(
            f"{remaining} additional schedule "
            f"{'check needs' if remaining == 1 else 'checks need'} attention."
        ).classes("rbs-type-caption rbs-text-muted")


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
    initial_minimum_clinic_days = (
        attending.minimum_attending_clinic_days_per_week
        if attending is not None
        else 0
    )
    initial_preferred_schedule = (
        list(attending.preferred_weekly_schedule_half_days)
        if attending is not None
        else []
    )
    initial_schedule_template = (
        list(attending.schedule_template_half_days) if attending is not None else []
    )
    saved_schedule = (
        instance.attending_schedule_for(attending.id)
        if attending is not None
        else None
    )
    initial_weekly_schedules = list(saved_schedule.weeks) if saved_schedule else []
    weekly_shift_target_controls: list[
        tuple[AttendingWorkType, object, object, object]
    ] = []
    controls: dict[str, object] = {}
    first_academic_day, last_academic_day = academic_year_date_range(instance)

    def draft_boundary(
        *,
        default_control: str,
        date_control: str,
        academic_default: date,
        saved_value: date | None,
    ) -> date:
        if bool(getattr(controls.get(default_control), "value", True)):
            return academic_default
        try:
            return date.fromisoformat(
                str(getattr(controls.get(date_control), "value", "") or "")
            )
        except ValueError:
            return saved_value or academic_default

    def draft_effective_week(week: int) -> EffectiveAttendingWeek:
        assert attending is not None
        start_date = draft_boundary(
            default_control="default_start",
            date_control="start",
            academic_default=first_academic_day,
            saved_value=(attending.schedule_start_date if attending is not None else None),
        )
        end_date = draft_boundary(
            default_control="default_end",
            date_control="end",
            academic_default=last_academic_day,
            saved_value=(attending.schedule_end_date if attending is not None else None),
        )
        try:
            draft_attending = Attending(
                id=attending.id,
                name=attending.name,
                half_days_per_week=getattr(
                    controls.get("half_days_per_week"),
                    "value",
                    attending.half_days_per_week,
                ),
                schedule_start_date=start_date,
                schedule_end_date=end_date,
                vacation_ranges=list(initial_vacations),
            )
        except ValidationError:
            # Keep the board usable while a date or number control contains an
            # incomplete draft. Save still reports the precise validation error.
            draft_attending = attending
        draft_schedule = (
            AttendingSchedule(
                attending_id=attending.id,
                weeks=list(initial_weekly_schedules),
            )
            if initial_weekly_schedules
            else None
        )
        return effective_attending_week(
            draft_attending,
            draft_schedule,
            week=week,
            first_day=first_academic_day,
            last_day=last_academic_day,
            academic_half_day=instance.academic_half_day_for_week(week),
            automatic_academic_admin=(
                instance.clinic_policy.academic_half_day_is_attending_admin_time
            ),
        )

    def configured_weekly_shift_targets() -> list[AttendingWeeklyShiftTarget]:
        if not weekly_shift_target_controls:
            return initial_weekly_shift_targets
        targets: list[AttendingWeeklyShiftTarget] = []
        for work_type, mode, minimum, maximum in weekly_shift_target_controls:
            targets = replace_weekly_shift_target(
                targets,
                work_type_value=work_type.value,
                mode_value=getattr(mode, "value", None),
                minimum_value=getattr(minimum, "value", None),
                maximum_value=getattr(maximum, "value", None),
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
                minimum_attending_clinic_days_per_week=getattr(
                    controls.get("minimum_attending_clinic_days_per_week"),
                    "value",
                    initial_minimum_clinic_days,
                ),
                preferred_weekly_schedule_half_days=initial_preferred_schedule,
                schedule_template_half_days=initial_schedule_template,
            )
            if attending is None:
                updated = add_attending(instance, saved_attending)
                message = f"Added {saved_attending.name}"
            else:
                updated = replace_attending_with_schedule(
                    instance,
                    attending.id,
                    saved_attending,
                    initial_weekly_schedules,
                )
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
            f"Update {attending.name}'s targets, preferred schedule, weekly work, "
            "dates, and vacation."
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
                    "targets, a preferred weekly schedule, weekly work, a reusable "
                    "template, and vacation."
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
                preferences_tab = ui.tab(
                    "preferences",
                    label="Preferences",
                    icon="favorite_border",
                )
                template_tab = ui.tab(
                    "schedule_template",
                    label="Template",
                    icon="content_copy",
                )
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
                            effective_week_for=draft_effective_week,
                        )

                with ui.tab_panel(targets_tab).classes("p-0"):
                    with ui.column().classes("w-full gap-4 p-5"):
                        controls["minimum_attending_clinic_days_per_week"] = (
                            _weekly_shift_target_editor(
                                initial_weekly_shift_targets,
                                weekly_shift_target_controls,
                                minimum_attending_clinic_days=(
                                    initial_minimum_clinic_days
                                ),
                            )
                        )

                with ui.tab_panel(preferences_tab).classes("p-0"):
                    with ui.column().classes("w-full gap-4 p-5"):
                        _preferred_weekly_schedule_editor(
                            instance,
                            initial_preferred_schedule,
                            weekly_target=half_days_per_week,
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

                with ui.tab_panel(vacation_tab).classes("p-0"):
                    with ui.column().classes("w-full gap-4 p-5"):
                        _vacation_range_editor(
                            instance,
                            initial_vacations,
                            on_change=refresh_weekly_editor,
                        )

            for control_name in (
                "default_start",
                "start",
                "default_end",
                "end",
            ):
                controls[control_name].on_value_change(
                    lambda _event: refresh_weekly_editor()
                )

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
                    "minimum_shifts_per_week": (
                        getattr(minimum, "value", None)
                        if getattr(mode, "value", None) != WEEKLY_SHIFT_TARGET_NONE
                        else None
                    ),
                    "maximum_shifts_per_week": (
                        getattr(maximum, "value", None)
                        if getattr(mode, "value", None) != WEEKLY_SHIFT_TARGET_NONE
                        else None
                    ),
                }
                for work_type, mode, minimum, maximum in weekly_shift_target_controls
            ],
            "minimum_attending_clinic_days_per_week": getattr(
                controls.get("minimum_attending_clinic_days_per_week"),
                "value",
                initial_minimum_clinic_days,
            ),
            "preferred_weekly_schedule": [
                half_day.model_dump(mode="json")
                for half_day in initial_preferred_schedule
            ],
            "schedule_template": [
                half_day.model_dump(mode="json")
                for half_day in initial_schedule_template
            ],
            "weekly_work_schedules": [
                schedule.model_dump(mode="json")
                for schedule in initial_weekly_schedules
            ],
        }

    initial_editor_state = current_editor_state()
    if guard is not None:
        guard.is_dirty = lambda: current_editor_state() != initial_editor_state
        guard.save = save
        guard.subject = (
            "the new attending" if creating else f"{attending.name}'s attending setup"
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
    controls: list[tuple[AttendingWorkType, object, object, object]],
    *,
    minimum_attending_clinic_days: int,
) -> object:
    from nicegui import ui

    by_work_type = {target.work_type: target for target in targets}
    with ui.column().classes("w-full gap-0"):
        ui.label("Weekly category targets").classes("rbs-type-section-title")
        ui.label(
            "Set a minimum and maximum for each recurring category. A Fixed range is "
            "required; a Flexible range is preferred. Special/Other work is added "
            "directly to the schedule."
        ).classes("rbs-type-caption rbs-text-muted")

    with ui.column().classes(
        "rbs-attending-form-section w-full gap-3 rounded p-4"
    ):
        with ui.row().classes("w-full items-center gap-3"):
            ui.icon("calendar_view_week").classes("rbs-text-primary")
            with ui.column().classes("min-w-0 gap-0"):
                ui.label("Attending Clinic days").classes(
                    "rbs-type-section-title"
                )
                ui.label(
                    "An Attending Clinic day is a distinct weekday containing one or "
                    "two Attending Clinic half-days. Weeks containing weekday vacation "
                    "are excluded."
                ).classes("rbs-type-caption rbs-text-muted")
        minimum_clinic_days = (
            ui.number(
                "Minimum Attending Clinic days per week",
                value=minimum_attending_clinic_days,
                min=0,
                max=MAX_ATTENDING_CLINIC_DAYS_PER_WEEK,
                step=1,
                precision=0,
            )
            .props("outlined")
            .classes("w-full md:w-72")
        )

    for work_type in ATTENDING_WEEKLY_TARGET_WORK_TYPES:
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
                    .classes("w-full md:w-40")
                )
                minimum = (
                    ui.number(
                        "Minimum shifts per week",
                        value=(
                            configured.minimum_shifts_per_week
                            if configured is not None
                            else 0
                        ),
                        min=0,
                        max=MAX_ATTENDING_HALF_DAYS_PER_WEEK,
                        step=1,
                        precision=0,
                    )
                    .props(
                        f"outlined aria-label='{category_label} minimum shifts per week'"
                    )
                    .classes("w-full min-w-0 md:flex-1")
                )
                maximum = (
                    ui.number(
                        "Maximum shifts per week",
                        value=(
                            configured.maximum_shifts_per_week
                            if configured is not None
                            else 0
                        ),
                        min=0,
                        max=MAX_ATTENDING_HALF_DAYS_PER_WEEK,
                        step=1,
                        precision=0,
                    )
                    .props(
                        f"outlined aria-label='{category_label} maximum shifts per week'"
                    )
                    .classes("w-full min-w-0 md:flex-1")
                )
                minimum.set_enabled(configured is not None)
                maximum.set_enabled(configured is not None)

                def toggle_range(event, lower=minimum, upper=maximum) -> None:
                    enabled = event.value != WEEKLY_SHIFT_TARGET_NONE
                    lower.set_enabled(enabled)
                    upper.set_enabled(enabled)

                mode.on_value_change(toggle_range)
                controls.append((work_type, mode, minimum, maximum))
    return minimum_clinic_days


def _weekly_work_editor(
    instance: SchedulerInput,
    schedules: list[AttendingWeeklyWorkSchedule],
    template: list[AttendingWorkHalfDay],
    *,
    weekly_target: object,
    effective_week_for: Callable[[int], EffectiveAttendingWeek],
) -> Callable[[], None]:
    from nicegui import ui

    def target_value() -> int:
        value = getattr(weekly_target, "value", DEFAULT_ATTENDING_HALF_DAYS_PER_WEEK)
        return int(value if value is not None else DEFAULT_ATTENDING_HALF_DAYS_PER_WEEK)

    with ui.row().classes("w-full items-center justify-between gap-3"):
        with ui.column().classes("gap-0"):
            ui.label("Week-by-week schedule").classes("rbs-type-section-title")
            ui.label(
                "Edit each academic week independently. Override can make the current "
                "assignment count this week's half-day total."
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
        reserved_admin = effective_week_for(week).automatic_admin_half_day
        week_start = instance.calendar.first_week_start + timedelta(weeks=week - 1)
        week_end = week_start + timedelta(days=6)

        def effective_target() -> int:
            return effective_week_for(week).target_half_days

        def assigned_count() -> int:
            return effective_week_for(week).assigned_half_days

        with week_container:
            with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
                with ui.column().classes("gap-0"):
                    ui.label(
                        f"Week {week} · {_short_week_range_label(week_start, week_end)}"
                    ).classes("rbs-font-semibold")
                    status_label = ui.label(
                        "Not configured — no work is scheduled in this week."
                    ).classes("rbs-type-caption rbs-text-muted")
                with ui.row().classes("items-center gap-2"):
                    assigned_badge = (
                        ui.badge(
                            _assigned_half_day_target_label(
                                assigned_count(),
                                effective_target(),
                            )
                        )
                        .props("outline")
                        .classes("rbs-muted-badge")
                    )
                    override_badge = ui.badge("Week override").props("outline").classes(
                        "rbs-muted-badge"
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
                                half_days_override=(
                                    current.half_days_override
                                    if current is not None
                                    else None
                                ),
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
                                "Applying the template replaces every AM and PM assignment "
                                "in this week. Its half-day override is preserved."
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

                def override_week() -> None:
                    try:
                        schedules[:] = override_weekly_half_day_total(
                            instance,
                            schedules,
                            week=week,
                            assigned_half_days=assigned_count(),
                        )
                        current = schedule_for(week)
                        assert current is not None
                        assert current.half_days_override is not None
                        override_label = _half_days_per_week_label(
                            current.half_days_override
                        )
                        ui.notify(
                            f"Week {week} now uses {override_label}",
                            type="positive",
                        )
                        render_week()
                    except (ValidationError, ValueError) as exc:
                        ui.notify(
                            _validation_message(exc),
                            type="negative",
                            multi_line=True,
                        )

                with ui.button(
                    "Override",
                    icon="tune",
                    on_click=override_week,
                ).props(SECONDARY_BUTTON_PROPS) as override_button:
                    ui.tooltip(
                        "Use the number currently assigned as this week's half-day total"
                    )

                def use_default() -> None:
                    try:
                        schedules[:] = use_default_weekly_half_day_total(
                            instance,
                            schedules,
                            week=week,
                            default_half_days=target_value(),
                            assigned_half_days=assigned_count(),
                        )
                        ui.notify(
                            f"Week {week} now uses the attending default",
                            type="positive",
                        )
                        render_week()
                    except (ValidationError, ValueError) as exc:
                        ui.notify(
                            _validation_message(exc),
                            type="negative",
                            multi_line=True,
                        )

                with ui.button(
                    "Use default",
                    icon="undo",
                    on_click=use_default,
                ).props(SECONDARY_BUTTON_PROPS) as default_button:
                    ui.tooltip("Remove this week's half-day override")

                def clear_week() -> None:
                    current = schedule_for(week)
                    if current is None:
                        return

                    def clear() -> None:
                        schedules[:] = clear_weekly_work_schedule(
                            instance,
                            schedules,
                            week=week,
                        )
                        refresh_count()
                        render_week()
                        ui.notify(f"Cleared week {week}", type="positive")

                    _confirm_draft_schedule_replacement(
                        title=f"Clear week {week}?",
                        description=(
                            "Every AM and PM assignment and this week's half-day override "
                            "will be removed from the draft."
                        ),
                        confirm_label="Clear week",
                        on_confirm=clear,
                    )

                clear_button = ui.button(
                    "Clear this week",
                    icon="event_busy",
                    on_click=clear_week,
                ).props(DESTRUCTIVE_BUTTON_PROPS)

            def refresh_week_controls() -> None:
                current = schedule_for(week)
                assigned = assigned_count()
                target = effective_target()
                status_label.set_text(
                    "Academic half-day is automatically reserved as Admin Time."
                    if current is None and reserved_admin is not None
                    else "Not configured — no work is scheduled in this week."
                    if current is None
                    else (
                        "This week uses its own half-day total."
                        if current.half_days_override is not None
                        else "This week uses the attending's default half-day total."
                    )
                )
                assigned_badge.set_text(
                    _assigned_half_day_target_label(assigned, target)
                )
                has_override = (
                    current is not None and current.half_days_override is not None
                )
                override_badge.set_visibility(has_override)
                override_button.set_visibility(assigned != target)
                default_button.set_visibility(has_override)
                default_button.set_enabled(assigned <= target_value())
                clear_button.set_enabled(current is not None)
                refresh_count()

            def save_half_days(updated: list[AttendingWorkHalfDay]) -> None:
                current = schedule_for(week)
                schedules[:] = replace_weekly_work_schedule(
                    instance,
                    schedules,
                    week=week,
                    half_days=updated,
                    half_days_override=(
                        current.half_days_override if current is not None else None
                    ),
                )
                refresh_week_controls()

            ui.label(
                "Click a half-day to assign or edit it. Drag work blocks to move "
                "or swap them within this week. If the assigned count differs from "
                "the total, Override adopts the current count for this week."
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
                reserved_half_days=(
                    [reserved_admin] if reserved_admin is not None else []
                ),
            )
            refresh_week_controls()

    week_select.on_value_change(lambda _event: render_week())
    weekly_target.on_value_change(lambda _event: render_week())
    render_week()
    return render_week


def _preferred_weekly_schedule_editor(
    instance: SchedulerInput,
    preferred_schedule: list[AttendingWorkHalfDay],
    *,
    weekly_target: object,
) -> None:
    from nicegui import ui

    def target_value() -> int:
        value = getattr(weekly_target, "value", DEFAULT_ATTENDING_HALF_DAYS_PER_WEEK)
        return int(value if value is not None else DEFAULT_ATTENDING_HALF_DAYS_PER_WEEK)

    with ui.row().classes("w-full items-center justify-between gap-3"):
        with ui.column().classes("gap-0"):
            ui.label("Preferred weekly schedule").classes("rbs-type-section-title")
            ui.label(
                "Choose the ideal weekday and AM/PM placement for recurring work. "
                "This is a soft preference and does not place work in any week."
            ).classes("rbs-type-caption rbs-text-muted")
        preferred_badge = (
            ui.badge(
                _preferred_schedule_count_label(
                    len(preferred_schedule),
                    target_value(),
                )
            )
            .props("outline")
            .classes("rbs-muted-badge")
        )

    def preferred_changed(updated: list[AttendingWorkHalfDay]) -> None:
        preferred_schedule[:] = updated
        preferred_badge.set_text(
            _preferred_schedule_count_label(
                len(preferred_schedule),
                target_value(),
            )
        )

    weekly_target.on_value_change(
        lambda _event: preferred_badge.set_text(
            _preferred_schedule_count_label(
                len(preferred_schedule),
                target_value(),
            )
        )
    )
    ui.label(
        "Click a half-day to assign or edit it. Drag work blocks to move or swap "
        "them within the preferred week."
    ).classes("rbs-type-caption rbs-text-muted")
    _work_schedule_grid(
        instance,
        preferred_schedule,
        on_change=preferred_changed,
        scope="attending-preference",
        week=0,
        context_label="preferred weekly schedule",
        allowed_work_types=ATTENDING_PREFERRED_WORK_TYPES,
    )


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
    allowed_work_types: tuple[AttendingWorkType, ...] = tuple(AttendingWorkType),
    reserved_half_days: list[AttendingWorkHalfDay] | None = None,
) -> None:
    from nicegui import ui

    grid_container = ui.column().classes("w-full min-w-0 gap-0")
    reserved_by_slot = {
        (half_day.weekday, half_day.session): half_day
        for half_day in reserved_half_days or []
    }

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
            allowed_work_types=allowed_work_types,
        )

    def render_cell(weekday: Weekday, session: Session) -> None:
        stored_assignment = next(
            (
                half_day
                for half_day in half_days
                if (half_day.weekday, half_day.session) == (weekday, session)
            ),
            None,
        )
        reserved_assignment = reserved_by_slot.get((weekday, session))
        assignment = reserved_assignment or stored_assignment
        action = "Assign" if assignment is None else "Edit"
        accessible_name = (
            f"{action} {weekday.value.title()} {_work_session_label(session)} "
            f"in {context_label}"
        )
        cell = create_half_day_cell(
            classes="is-occupied" if assignment is not None else "",
            on_drop=(
                None
                if reserved_assignment is not None
                else partial(
                    drop_on,
                    target_weekday=weekday,
                    target_session=session,
                )
            ),
            on_click=(
                partial(open_slot, weekday, session)
                if assignment is None and reserved_assignment is None
                else None
            ),
            accessible_name=(accessible_name if assignment is None else None),
        )
        with cell:
            if assignment is None:
                ui.icon("add").classes("rbs-resident-clinic-session-empty")
                return
            event = create_draggable_half_day_event(
                classes=_work_event_class(assignment),
                draggable=reserved_assignment is None,
                week=week,
                weekday=weekday,
                session=session,
                scope=scope,
                on_click=(
                    None
                    if reserved_assignment is not None
                    else partial(open_slot, weekday, session)
                ),
                accessible_name=accessible_name,
            )
            if assignment.clinic_id is not None:
                site = instance.clinic_policy.site(assignment.clinic_id)
                event.style(
                    f"--rbs-clinic-site-color: {site.color}; "
                    f"--rbs-clinic-site-tint: {site.light_color}"
                )
            with event:
                label = (
                    "Admin Time · Academic half-day"
                    if reserved_assignment is not None
                    else _work_assignment_label(instance, assignment)
                )
                ui.label(label).classes(
                    "rbs-resident-clinic-event-location"
                )
                with ui.icon(
                    "lock" if reserved_assignment is not None else "drag_indicator"
                ).classes(
                    "rbs-resident-clinic-event-lock rbs-text-muted"
                ):
                    ui.tooltip(
                        "Reserved by the program academic half-day"
                        if reserved_assignment is not None
                        else "Drag to move or swap this work half-day"
                    )

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
    allowed_work_types: tuple[AttendingWorkType, ...] = tuple(AttendingWorkType),
) -> None:
    from nicegui import ui

    clinic_options = {site.id: site.name for site in instance.clinic_policy.sites}
    work_type_options = {
        work_type.value: _WORK_TYPE_OPTIONS[work_type.value]
        for work_type in allowed_work_types
    }
    initial_type = assignment.work_type.value if assignment is not None else None
    initial_clinic = (
        assignment.clinic_id
        if assignment is not None and assignment.clinic_id is not None
        else instance.clinic_policy.primary_site_id
    )
    initial_description = assignment.description if assignment is not None else None
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
                    work_type_options,
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
            description = (
                ui.input(
                    "Description",
                    value=initial_description,
                    placeholder="What is this time for?",
                )
                .props(
                    f"outlined maxlength={ATTENDING_WORK_DESCRIPTION_MAX_LENGTH}"
                )
                .classes("w-full")
            )
            description.set_visibility(
                initial_type == AttendingWorkType.SPECIAL_OTHER.value
            )

            def change_work_type(event) -> None:
                is_precepting = (
                    event.value == AttendingWorkType.PRECEPTING_CLINIC.value
                )
                clinic.set_visibility(is_precepting)
                description.set_visibility(
                    event.value == AttendingWorkType.SPECIAL_OTHER.value
                )
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
                            if selected_type not in allowed_work_types:
                                raise ValueError(
                                    "select an available work type for this schedule"
                                )
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
                                description=(
                                    str(description.value or "")
                                    if selected_type is AttendingWorkType.SPECIAL_OTHER
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
    *,
    on_change: Callable[[], None] | None = None,
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
                        ui.label(_weekday_count_label(vacation.weekdays)).classes(
                            "rbs-type-caption rbs-text-muted"
                        )

                    def remove(event=None, *, selected_index: int = index) -> None:
                        vacations.pop(selected_index)
                        render_selected()
                        if on_change is not None:
                            on_change()

                    with ui.button(icon="delete_outline", on_click=remove).props(
                        button_props(
                            DESTRUCTIVE_ICON_BUTTON_PROPS,
                            "aria-label='Remove vacation range "
                            f"{_vacation_period_label(vacation)}'",
                        )
                    ):
                        ui.tooltip("Remove vacation range")
            total_weekdays = sum(vacation.weekdays for vacation in vacations)
            ui.label(
                f"{_vacation_count_label(len(vacations))} · "
                f"{_weekday_count_label(total_weekdays)} total"
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
                if on_change is not None:
                    on_change()
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
            "Their category targets, weekly schedules, overrides, template, schedule "
            "dates, and vacation ranges will be removed from this workspace. This "
            "cannot be undone."
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
                "Choose someone from the directory to view their schedule, "
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


def _attending_card_summary_label(attending: Attending) -> str:
    return (
        f"{_weekly_work_label(attending)} · "
        f"{_weekday_off_label(attending.vacation_ranges)}"
    )


def _weekly_work_label(attending: Attending) -> str:
    return _half_days_per_week_label(attending.half_days_per_week)


def _vacation_count_label(count: int) -> str:
    if count == 0:
        return "No vacation"
    return f"{count} vacation range" if count == 1 else f"{count} vacation ranges"


def _half_days_per_week_label(count: int) -> str:
    if count == 1:
        return "1 half-day per week"
    return f"{count} half-days per week"


def _configured_week_count_label(count: int, total: int) -> str:
    return f"{count} of {total} weeks configured"


def _assigned_half_day_target_label(count: int, target: int) -> str:
    return f"{count} of {target} half-days assigned"


def _template_assignment_count_label(count: int, target: int) -> str:
    return f"{count} of {target} template half-days"


def _preferred_schedule_count_label(count: int, target: int) -> str:
    return f"{count} of {target} preferred half-days"


def _minimum_attending_clinic_days_label(count: int) -> str:
    if count == 0:
        return "No Attending Clinic day minimum"
    unit = "day" if count == 1 else "days"
    return f"At least {count} Attending Clinic {unit} per full non-vacation week"


def _weekly_shift_target_count_label(count: int) -> str:
    if count == 0:
        return "No category targets"
    return f"{count} of {len(ATTENDING_WEEKLY_TARGET_WORK_TYPES)} categories targeted"


def _attending_schedule_issue_count_label(errors: int, warnings: int) -> str:
    if not errors and not warnings:
        return "No schedule issues"
    parts: list[str] = []
    if errors:
        parts.append(f"{errors} {'error' if errors == 1 else 'errors'}")
    if warnings:
        parts.append(f"{warnings} {'warning' if warnings == 1 else 'warnings'}")
    return " · ".join(parts)


def _shift_target_range_label(minimum: int, maximum: int) -> str:
    if minimum == maximum:
        return "1 shift per week" if minimum == 1 else f"{minimum} shifts per week"
    return f"{minimum}–{maximum} shifts per week"


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
    assignment: AttendingWorkHalfDay,
) -> str:
    label = _WORK_TYPE_OPTIONS[assignment.work_type.value]
    if assignment.clinic_id is not None:
        return f"{label} · {instance.clinic_policy.site_name(assignment.clinic_id)}"
    if assignment.description is not None:
        return f"{label} · {assignment.description}"
    return label


def _weekday_count_label(count: int) -> str:
    return "1 weekday off" if count == 1 else f"{count} weekdays off"


def _weekday_off_label(vacations: list[AttendingVacation]) -> str:
    count = sum(vacation.weekdays for vacation in vacations)
    return _weekday_count_label(count)


def _date_label(value: date) -> str:
    return f"{value:%b} {value.day}, {value:%Y}"


def _vacation_period_label(vacation: AttendingVacation) -> str:
    if vacation.start_date == vacation.end_date:
        return f"{vacation.start_date:%a, %b} {vacation.start_date.day}, {vacation.start_date:%Y}"
    return f"{_date_label(vacation.start_date)}–{_date_label(vacation.end_date)}"
