"""Resident block and clinic schedule workspace."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from functools import partial
from typing import Any

from pydantic import ValidationError

from rbs.logging import get_logger
from rbs.models.enums import RotationKind, Session, Weekday
from rbs.models.instance import SchedulerInput
from rbs.models.locks import LockedPlacement
from rbs.models.resident import Resident, ResidentClinicHalfDay
from rbs.models.schedule import AssignedClinic, Schedule
from rbs.ui import master_detail
from rbs.ui.clinic.board import clinic_weekdays, occupancy
from rbs.ui.editor_common import _default_block_duration
from rbs.ui.locks import (
    THROUGH_TODAY_SOURCE,
    ScheduleBlock,
    block_overlapping_lock_sources,
    clear_schedule_block,
    lock_resident_schedule,
    lock_schedule_block,
    remove_manual_lock,
    replace_manual_block,
    schedule_blocks,
    schedule_gaps,
    unlock_resident_schedule,
    unlock_schedule_block,
)
from rbs.ui.residents.electives import render_elective_preferences
from rbs.ui.residents.ops import (
    ClinicOccupancy,
    _compact_week_ranges,
    _resident_clinic_session_key,
    _vacation_date_range,
    add_resident_clinic_slot,
    change_resident_clinic_slot_site,
    move_resident_clinic_slot,
    remove_resident_clinic_slot,
    resident_clinic_available_site_ids,
    resident_clinic_schedule_report_rows,
    resident_clinic_slot,
    resident_clinic_slot_locked,
    resident_clinic_target_conflicts,
    resident_clinic_week_override_delta,
    resident_schedule_report_rows,
    set_resident_clinic_slot_locked,
    vacation_monday,
)
from rbs.ui.residents.schedule_pdf import (
    build_resident_schedule_pdf,
    resident_schedule_pdf_filename,
)

SaveResidentSchedule = Callable[[SchedulerInput, str, bool], None]
SaveResidentScheduleResult = Callable[[Schedule, str, bool], None]
ChangeResidentScheduleEditing = Callable[[bool], None]

_CLINIC_DRAG_START_JS = """
(event) => {
  if (event.currentTarget.getAttribute('draggable') !== 'true') {
    event.preventDefault();
    return;
  }
  const payload = JSON.stringify({
    week: Number(event.currentTarget.dataset.week),
    weekday: event.currentTarget.dataset.weekday,
    session: event.currentTarget.dataset.session,
  });
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('application/x-rbs-clinic-slot', payload);
  event.dataTransfer.setData('text/plain', payload);
  event.currentTarget.classList.add('is-dragging');
}
"""
_CLINIC_DRAG_END_JS = """
(event) => {
  event.currentTarget.classList.remove('is-dragging');
  document.querySelectorAll('.rbs-resident-clinic-session-cell.is-drag-over')
    .forEach((cell) => cell.classList.remove('is-drag-over'));
}
"""
_CLINIC_DRAG_OVER_JS = """
(event) => {
  const types = Array.from(event.dataTransfer.types || []);
  if (!types.includes('application/x-rbs-clinic-slot') && !types.includes('text/plain')) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = 'move';
  event.currentTarget.classList.add('is-drag-over');
}
"""
_CLINIC_DRAG_LEAVE_JS = """
(event) => {
  if (!event.currentTarget.contains(event.relatedTarget)) {
    event.currentTarget.classList.remove('is-drag-over');
  }
}
"""
_CLINIC_DROP_JS = """
(event) => {
  event.preventDefault();
  event.currentTarget.classList.remove('is-drag-over');
  const payload = event.dataTransfer.getData('application/x-rbs-clinic-slot')
    || event.dataTransfer.getData('text/plain');
  if (!payload) return;
  try {
    emit(JSON.parse(payload));
  } catch (_error) {
    // Ignore drops which did not originate from a resident clinic block.
  }
}
"""


def _resident_schedule_workspace(
    instance: SchedulerInput,
    schedule: Schedule | None,
    resident: Resident,
    *,
    today: date | None = None,
    on_schedule_save: SaveResidentSchedule | None = None,
    on_schedule_change: SaveResidentScheduleResult | None = None,
    schedule_is_current: bool = True,
    block_schedule_editing: bool = False,
    on_block_schedule_editing_change: ChangeResidentScheduleEditing | None = None,
    schedule_editing: bool = False,
    on_schedule_editing_change: ChangeResidentScheduleEditing | None = None,
    active_section: str = "resident_block_schedule",
    on_section_change=None,
):
    from nicegui import ui

    block_report_state = {
        "show_completed": False,
        "editing": bool(block_schedule_editing),
    }
    clinic_report_state = {
        "show_completed": False,
        "editing": bool(schedule_editing and not block_schedule_editing),
    }
    schedule_state: dict[str, Schedule | None] = {"value": schedule}

    def export_to_pdf() -> None:
        try:
            pdf = build_resident_schedule_pdf(
                resident=resident,
                academic_year=instance.academic_year,
                training_level_label=instance.training_level_label(
                    resident.pgy,
                    compact=True,
                ),
                block_rows=resident_schedule_report_rows(
                    instance,
                    schedule_state["value"],
                    resident.id,
                    show_completed=bool(block_report_state["show_completed"]),
                    today=today,
                ),
                clinic_rows=resident_clinic_schedule_report_rows(
                    instance,
                    schedule_state["value"],
                    resident.id,
                    show_completed=bool(clinic_report_state["show_completed"]),
                    today=today,
                ),
            )
            ui.download.content(
                pdf,
                resident_schedule_pdf_filename(resident, instance.academic_year),
                "application/pdf",
            )
            get_logger("documents").info(
                "schedule.exported",
                source="resident_pdf",
            )
        except Exception as exc:
            get_logger("documents").error(
                "schedule.export_failed",
                source="resident_pdf",
                error_code=type(exc).__name__,
                exc_info=True,
            )
            ui.notify(f"Unable to export schedule PDF: {exc}", type="negative")

    with master_detail.detail_card():
        with ui.row().classes("rbs-resident-schedule-header w-full items-center gap-4 px-4"):
            with (
                ui.tabs(on_change=on_section_change)
                .props("dense no-caps inline-label align=left")
                .classes("rbs-resident-schedule-tabs min-w-0 flex-1") as tabs
            ):
                block_tab = ui.tab(
                    "resident_block_schedule",
                    label="Block Schedule",
                    icon="view_timeline",
                )
                clinic_tab = ui.tab(
                    "resident_clinic_schedule",
                    label="Clinic Schedule",
                    icon="calendar_view_week",
                )
                elective_tab = ui.tab(
                    "resident_elective_preference",
                    label="Elective preferences",
                    icon="format_list_numbered",
                )
            with ui.row().classes("rbs-resident-schedule-actions items-center gap-2"):
                ui.button(
                    "Export to PDF",
                    icon="picture_as_pdf",
                    on_click=export_to_pdf,
                ).props("outline no-caps").classes("whitespace-nowrap")
        section_tabs = {
            "resident_block_schedule": block_tab,
            "resident_clinic_schedule": clinic_tab,
            "resident_elective_preference": elective_tab,
        }
        initial_schedule_tab = (
            clinic_tab
            if clinic_report_state["editing"]
            else block_tab
            if block_report_state["editing"]
            else section_tabs.get(active_section, block_tab)
        )
        with ui.tab_panels(tabs, value=initial_schedule_tab).classes(
            "rbs-resident-schedule-panels w-full min-w-0"
        ):
            with ui.tab_panel(block_tab).classes("rbs-resident-schedule-panel p-0"):
                block_report = ui.column().classes("w-full min-w-0 gap-0")

                def render_block_report() -> None:
                    block_report.clear()
                    with block_report:
                        _resident_block_schedule_report(
                            instance,
                            schedule_state["value"],
                            resident,
                            show_completed=bool(block_report_state["show_completed"]),
                            on_show_completed_change=toggle_completed,
                            editing=bool(block_report_state["editing"]),
                            on_editing_change=toggle_block_editing,
                            on_schedule_save=on_schedule_save,
                            on_schedule_change=on_schedule_change,
                            schedule_is_current=schedule_is_current,
                            today=today,
                        )

                def toggle_completed(event) -> None:
                    block_report_state["show_completed"] = bool(event.value)
                    render_block_report()

                def toggle_block_editing() -> None:
                    editing = not bool(block_report_state["editing"])
                    block_report_state["editing"] = editing
                    clinic_was_editing = bool(clinic_report_state["editing"])
                    if editing:
                        clinic_report_state["editing"] = False
                    if on_block_schedule_editing_change is not None:
                        on_block_schedule_editing_change(editing)
                    if editing and clinic_was_editing and on_schedule_editing_change is not None:
                        on_schedule_editing_change(False)
                    render_block_report()
                    if clinic_was_editing:
                        render_clinic_report()

                render_block_report()
            with ui.tab_panel(clinic_tab).classes("rbs-resident-schedule-panel p-0"):
                clinic_report = ui.column().classes("w-full min-w-0 gap-0")

                def render_clinic_report() -> None:
                    clinic_report.clear()
                    with clinic_report:
                        _resident_clinic_schedule_report(
                            instance,
                            schedule_state,
                            resident,
                            show_completed=bool(clinic_report_state["show_completed"]),
                            on_show_completed_change=toggle_clinic_completed,
                            editing=bool(clinic_report_state["editing"]),
                            on_editing_change=toggle_clinic_editing,
                            on_schedule_change=on_schedule_change,
                            today=today,
                        )

                def toggle_clinic_completed(event) -> None:
                    clinic_report_state["show_completed"] = bool(event.value)
                    render_clinic_report()

                def toggle_clinic_editing() -> None:
                    editing = not bool(clinic_report_state["editing"])
                    clinic_report_state["editing"] = editing
                    block_was_editing = bool(block_report_state["editing"])
                    if editing:
                        block_report_state["editing"] = False
                    if on_schedule_editing_change is not None:
                        on_schedule_editing_change(editing)
                    if (
                        editing
                        and block_was_editing
                        and on_block_schedule_editing_change is not None
                    ):
                        on_block_schedule_editing_change(False)
                    render_clinic_report()
                    if block_was_editing:
                        render_block_report()

                render_clinic_report()
            with ui.tab_panel(elective_tab).classes("rbs-resident-schedule-panel p-0"):
                render_elective_preferences(
                    instance,
                    schedule_state["value"],
                    resident,
                    on_schedule_save=on_schedule_save,
                    schedule_is_current=schedule_is_current,
                )


def _resident_block_schedule_manager(
    instance: SchedulerInput,
    schedule: Schedule | None,
    resident: Resident,
    *,
    on_schedule_save: SaveResidentSchedule,
    on_schedule_change: SaveResidentScheduleResult | None,
    schedule_is_current: bool,
) -> None:
    """Render resident-scoped block editing and lock controls."""
    from nicegui import ui

    blocks = schedule_blocks(schedule, resident_id=resident.id)
    gaps = schedule_gaps(
        schedule,
        resident_id=resident.id,
        calendar_weeks=instance.calendar.weeks,
    )
    matching_manual_ids = {
        id(lock)
        for block in blocks
        for lock in instance.locks
        if lock.source == "manual"
        and lock.resident_id == resident.id
        and lock.rotation_id == block.rotation_id
        and set(lock.weeks) == set(block.weeks)
    }
    unmatched_manual = [
        lock
        for lock in instance.locks
        if lock.source == "manual"
        and lock.resident_id == resident.id
        and id(lock) not in matching_manual_ids
    ]

    def save_action(action, success: str, *, preserve_schedule: bool) -> None:
        try:
            updated = action()
            if updated == instance:
                ui.notify("No lock changes were needed", type="info")
                return
            ui.notify(success, type="positive")
            on_schedule_save(
                updated,
                resident.id,
                bool(preserve_schedule and schedule_is_current),
            )
        except (ValidationError, ValueError) as exc:
            ui.notify(str(exc), type="negative", multi_line=True)

    def clear_block_action(block: ScheduleBlock) -> None:
        if schedule is None or on_schedule_change is None:
            return
        try:
            updated = clear_schedule_block(schedule, block)
            ui.notify(
                f"Weeks {_compact_week_ranges(block.weeks)} cleared; solve required",
                type="warning",
            )
            on_schedule_change(updated, resident.id, True)
        except (ValidationError, ValueError) as exc:
            ui.notify(str(exc), type="negative", multi_line=True)

    with ui.column().classes("w-full gap-3"):
        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
            ui.button(
                "Add block",
                icon="add",
                on_click=lambda: _open_resident_block_dialog(
                    instance,
                    schedule,
                    resident,
                    on_schedule_save=on_schedule_save,
                    schedule_is_current=schedule_is_current,
                ),
            ).props("unelevated no-caps")
            lock_all = ui.button(
                "Lock current schedule",
                icon="lock",
                on_click=lambda: save_action(
                    lambda: lock_resident_schedule(
                        instance,
                        schedule,
                        resident.id,
                    ),
                    "Current schedule locked",
                    preserve_schedule=True,
                ),
            ).props("outline no-caps")
            lock_all.set_enabled(bool(blocks))
            manual_count = sum(
                lock.source == "manual" and lock.resident_id == resident.id
                for lock in instance.locks
            )
            unlock_all = ui.button(
                "Unlock all manual",
                icon="lock_open",
                on_click=lambda: save_action(
                    lambda: unlock_resident_schedule(instance, resident.id),
                    "Manual locks removed",
                    preserve_schedule=True,
                ),
            ).props("flat no-caps")
            unlock_all.set_enabled(manual_count > 0)

        if schedule is not None and (blocks or gaps):
            ui.label("Current schedule").classes("rbs-type-control-label")
            timeline = sorted(
                [(block.start_week, "block", block) for block in blocks]
                + [(weeks[0], "gap", weeks) for weeks in gaps],
                key=lambda item: item[0],
            )
            for _start_week, item_type, item in timeline:
                if item_type == "gap":
                    _resident_schedule_gap_row(item)
                else:
                    _resident_block_management_row(
                        instance,
                        schedule,
                        resident,
                        item,
                        on_schedule_save=on_schedule_save,
                        on_schedule_change=on_schedule_change,
                        schedule_is_current=schedule_is_current,
                        save_action=save_action,
                        clear_block_action=clear_block_action,
                    )
        elif schedule is None and not unmatched_manual:
            ui.label(
                "No solved blocks yet. Add blocks here, then run Solve to fill the remaining weeks."
            ).classes("rbs-type-body rbs-text-muted")

        if unmatched_manual:
            ui.label("Pending blocks and manual pins").classes("rbs-type-control-label mt-2")
            for lock in unmatched_manual:
                _resident_manual_lock_row(
                    instance,
                    schedule,
                    resident,
                    lock,
                    on_schedule_save=on_schedule_save,
                    schedule_is_current=schedule_is_current,
                    save_action=save_action,
                )


def _resident_block_management_row(
    instance: SchedulerInput,
    schedule: Schedule | None,
    resident: Resident,
    block: ScheduleBlock,
    *,
    on_schedule_save: SaveResidentSchedule,
    on_schedule_change: SaveResidentScheduleResult | None,
    schedule_is_current: bool,
    save_action,
    clear_block_action,
) -> None:
    from nicegui import ui

    sources = block_overlapping_lock_sources(instance, block)
    manual = "manual" in sources
    automatic = THROUGH_TODAY_SOURCE in sources
    if manual and automatic:
        status = "Manual + automatic"
        status_color = "primary"
    elif manual:
        status = "Manual"
        status_color = "primary"
    elif automatic:
        status = "Automatic"
        status_color = "secondary"
    else:
        status = "Unlocked"
        status_color = None
    rotation_label = instance.assignment_label(
        block.rotation_id,
        elective=block.elective,
    )

    with ui.row().classes(
        "rbs-resident-block-management-row w-full items-center gap-3 rounded p-3"
    ):
        with ui.column().classes("min-w-0 flex-1 gap-0"):
            ui.label(f"Weeks {_compact_week_ranges(block.weeks)}").classes(
                "rbs-type-caption rbs-text-muted"
            )
            ui.label(rotation_label).classes("rbs-font-semibold")
        status_badge = ui.badge(status, color=status_color).props("outline")
        if status_color is None:
            status_badge.classes("rbs-muted-badge")
        if manual and automatic:
            ui.button(
                "Unlock manual",
                icon="lock_open",
                on_click=lambda: save_action(
                    lambda: unlock_schedule_block(instance, block),
                    "Manual lock removed; block remains automatically locked",
                    preserve_schedule=True,
                ),
            ).props("flat dense no-caps")
        elif manual:
            ui.button(
                "Unlock",
                icon="lock_open",
                on_click=lambda: save_action(
                    lambda: unlock_schedule_block(instance, block),
                    "Block unlocked",
                    preserve_schedule=True,
                ),
            ).props("flat dense no-caps")
        elif automatic:
            ui.button(
                "Locked by settings",
                icon="lock",
            ).props("flat dense no-caps disable")
        else:
            ui.button(
                "Lock",
                icon="lock",
                on_click=lambda: save_action(
                    lambda: lock_schedule_block(instance, block),
                    "Block locked",
                    preserve_schedule=True,
                ),
            ).props("flat dense no-caps")
            remove_button = ui.button(
                "Delete",
                icon="delete_outline",
                on_click=lambda: clear_block_action(block),
            ).props("flat dense no-caps color=negative")
            remove_button.set_enabled(on_schedule_change is not None)
        ui.button(
            "Edit",
            icon="edit",
            on_click=lambda: _open_resident_block_dialog(
                instance,
                schedule,
                resident,
                on_schedule_save=on_schedule_save,
                schedule_is_current=schedule_is_current,
                initial=block,
                original=None,
                replace_weeks=block.weeks,
            ),
        ).props("flat dense no-caps")


def _resident_schedule_gap_row(weeks: list[int]) -> None:
    from nicegui import ui

    with ui.row().classes(
        "rbs-resident-block-management-row is-gap w-full items-center gap-3 rounded p-3"
    ):
        with ui.column().classes("min-w-0 flex-1 gap-0"):
            ui.label(f"Weeks {_compact_week_ranges(weeks)}").classes(
                "rbs-type-caption rbs-text-muted"
            )
            ui.label("Unscheduled").classes("rbs-font-semibold")
        ui.badge("Needs solve", color="warning").props("outline")


def _resident_manual_lock_row(
    instance: SchedulerInput,
    schedule: Schedule | None,
    resident: Resident,
    lock: LockedPlacement,
    *,
    on_schedule_save: SaveResidentSchedule,
    schedule_is_current: bool,
    save_action,
) -> None:
    from nicegui import ui

    rotation_label = instance.assignment_label(
        lock.rotation_id,
        elective=lock.elective,
    )
    can_edit = (
        lock.exact_block
        and lock.weeks
        and len(lock.weeks)
        in instance.block_durations_for_pgy(
            resident.pgy,
            lock.rotation_id,
            elective=lock.elective,
        )
    )
    label = "Block · pending solve" if lock.exact_block else "Manual week pin"
    with ui.row().classes(
        "rbs-resident-block-management-row is-pending w-full items-center gap-3 rounded p-3"
    ):
        with ui.column().classes("min-w-0 flex-1 gap-0"):
            ui.label(f"Weeks {_compact_week_ranges(lock.weeks)}").classes(
                "rbs-type-caption rbs-text-muted"
            )
            ui.label(rotation_label).classes("rbs-font-semibold")
        ui.badge(label, color="warning").props("outline")
        if can_edit:
            block = ScheduleBlock(
                resident_id=resident.id,
                rotation_id=lock.rotation_id,
                start_week=lock.weeks[0],
                duration_weeks=len(lock.weeks),
                elective=lock.elective,
            )
            ui.button(
                "Edit",
                icon="edit",
                on_click=lambda: _open_resident_block_dialog(
                    instance,
                    schedule,
                    resident,
                    on_schedule_save=on_schedule_save,
                    schedule_is_current=schedule_is_current,
                    initial=block,
                    original=lock,
                ),
            ).props("flat dense no-caps")
        ui.button(
            "Delete" if lock.exact_block else "Unlock",
            icon="delete_outline" if lock.exact_block else "lock_open",
            on_click=lambda: save_action(
                lambda: remove_manual_lock(instance, lock),
                "Pending block deleted" if lock.exact_block else "Manual block unlocked",
                preserve_schedule=True,
            ),
        ).props("flat dense no-caps")


def _resident_block_rotation_options(
    instance: SchedulerInput,
    resident: Resident,
) -> dict[str, str]:
    curriculum = instance.curriculum_for(resident.pgy)
    curriculum_ids = {block.rotation_id for block in curriculum.blocks}
    options: list[tuple[str, str, str]] = []
    for rotation_id in curriculum_ids:
        rotation = instance.rotation(rotation_id)
        if rotation.kind is RotationKind.ELECTIVE:
            continue
        options.append(
            (
                rotation.code.casefold(),
                rotation.id,
                f"{rotation.code} · {rotation.name}",
            )
        )
    for rotation_id in instance.electives.eligible_rotation_ids:
        rotation = instance.rotation(rotation_id)
        if not instance.block_durations_for_pgy(
            resident.pgy,
            rotation_id,
            elective=True,
        ):
            continue
        options.append(
            (
                rotation.code.casefold(),
                _elective_rotation_option(rotation_id),
                instance.assignment_label(rotation_id, elective=True),
            )
        )
    fallback_ids = {
        rotation.id
        for duration in instance.direct_elective_block_counts_for_pgy(resident.pgy)
        if (rotation := instance.elective_fallback_rotation(resident.pgy, duration)) is not None
    }
    for rotation_id in fallback_ids:
        rotation = instance.rotation(rotation_id)
        options.append(
            (
                rotation.code.casefold(),
                _elective_rotation_option(rotation_id),
                instance.assignment_label(rotation_id, elective=True),
            )
        )
    return {
        value: label
        for _sort, value, label in sorted(
            options,
            key=lambda item: (item[0], item[2].casefold(), item[1]),
        )
    }


_ELECTIVE_ROTATION_OPTION_PREFIX = "elective:"


def _elective_rotation_option(rotation_id: str) -> str:
    return f"{_ELECTIVE_ROTATION_OPTION_PREFIX}{rotation_id}"


def _parse_rotation_option(value: str) -> tuple[str, bool]:
    if value.startswith(_ELECTIVE_ROTATION_OPTION_PREFIX):
        return value[len(_ELECTIVE_ROTATION_OPTION_PREFIX) :], True
    return value, False


def _resident_block_duration_options(
    instance: SchedulerInput,
    resident: Resident,
    rotation_option: str,
) -> dict[int, str]:
    rotation_id, elective = _parse_rotation_option(rotation_option)
    return {
        duration: f"{duration} week" if duration == 1 else f"{duration} weeks"
        for duration in sorted(
            instance.block_durations_for_pgy(
                resident.pgy,
                rotation_id,
                elective=elective,
            )
        )
    }


def _resident_block_start_options(
    instance: SchedulerInput,
    resident: Resident,
    rotation_option: str,
    duration_weeks: int,
) -> dict[int, str]:
    rotation_id, _elective = _parse_rotation_option(rotation_option)
    rotation = instance.rotation(rotation_id)
    rule = rotation.pgy_rule(resident.pgy)
    config = rotation.block_config(resident.pgy, duration_weeks)
    starts: dict[int, str] = {}
    for start_week in range(1, instance.calendar.weeks - duration_weeks + 2):
        if (start_week - 1) % instance.calendar.block_start_alignment:
            continue
        if rule.earliest_start_week is not None and start_week < rule.earliest_start_week:
            continue
        weeks = set(range(start_week, start_week + duration_weeks))
        vacation_overlap = weeks & instance.resident_scheduling_vacation_weeks(resident.id)
        if vacation_overlap and not config.vacation.allowed:
            continue
        maximum = config.vacation.max_weeks_per_block
        if maximum is not None and len(vacation_overlap) > maximum:
            continue
        start = vacation_monday(instance, start_week)
        end = vacation_monday(instance, start_week + duration_weeks - 1) + timedelta(days=6)
        week_label = (
            f"Week {start_week}"
            if duration_weeks == 1
            else f"Weeks {start_week}–{start_week + duration_weeks - 1}"
        )
        starts[start_week] = f"{week_label} · {_vacation_date_range(start, end)}"
    return starts


def _open_resident_block_dialog(
    instance: SchedulerInput,
    schedule: Schedule | None,
    resident: Resident,
    *,
    on_schedule_save: SaveResidentSchedule,
    schedule_is_current: bool,
    initial: ScheduleBlock | None = None,
    original: LockedPlacement | None = None,
    replace_weeks: list[int] | None = None,
) -> None:
    from nicegui import ui

    rotation_options = _resident_block_rotation_options(instance, resident)
    initial_option = (
        _elective_rotation_option(initial.rotation_id)
        if initial is not None and initial.elective
        else initial.rotation_id
        if initial is not None
        else None
    )
    initial_rotation = (
        initial_option
        if initial_option is not None and initial_option in rotation_options
        else next(iter(rotation_options), None)
    )
    if initial_rotation is None:
        ui.notify("This resident has no configured rotations", type="warning")
        return
    duration_options = _resident_block_duration_options(
        instance,
        resident,
        initial_rotation,
    )
    initial_duration = (
        initial.duration_weeks
        if initial is not None and initial.duration_weeks in duration_options
        else _default_block_duration(duration_options)
    )
    if initial_duration is None:
        ui.notify("This rotation has no compatible block lengths", type="warning")
        return
    start_options = _resident_block_start_options(
        instance,
        resident,
        initial_rotation,
        initial_duration,
    )
    initial_start = (
        initial.start_week
        if initial is not None and initial.start_week in start_options
        else next(iter(start_options), None)
    )

    title = "Edit block" if initial is not None else "Add block"
    with (
        ui.dialog() as dialog,
        ui.card().classes("rbs-resident-block-dialog w-full max-w-2xl p-0 gap-0"),
    ):
        with ui.row().classes("w-full items-center justify-between gap-3 px-5 py-4"):
            ui.label(f"{title} · {resident.name}").classes("rbs-type-dialog-title")
            ui.button(icon="close", on_click=dialog.close).props(
                "flat round dense aria-label='Close block dialog'"
            )
        ui.separator()
        with ui.column().classes("w-full gap-4 p-5"):
            ui.label(
                "Saving fixes this exact block in place. Run Solve afterward to reconcile "
                "the rest of the cohort schedule and clinic half-days."
            ).classes("rbs-type-body rbs-text-muted")
            rotation_select = (
                ui.select(
                    rotation_options,
                    value=initial_rotation,
                    label="Rotation",
                )
                .props("outlined options-dense use-input")
                .classes("w-full")
            )
            with ui.row().classes("w-full items-end gap-4 flex-wrap"):
                duration_select = (
                    ui.select(
                        duration_options,
                        value=initial_duration,
                        label="Block length",
                    )
                    .props("outlined options-dense")
                    .classes("w-48")
                )
                start_select = (
                    ui.select(
                        start_options,
                        value=initial_start,
                        label="Weeks",
                    )
                    .props("outlined options-dense use-input")
                    .classes("min-w-72 flex-1")
                )
            grouping_exempt = ui.checkbox(
                "Allow this block to be unmatched from its mandatory group",
                value=bool(original and original.grouping_exempt),
            )
            grouping_exempt_help = ui.label(
                "This explicit exception releases one whole group instance; the "
                "remaining grouped blocks may be scheduled separately."
            ).classes("rbs-type-caption rbs-text-muted")

        def refresh_fields(_event=None) -> None:
            rotation_option = str(rotation_select.value or "")
            selected_rotation_id, elective = _parse_rotation_option(rotation_option)
            grouped = (
                not elective
                and instance.rotation_group_for(
                    resident.pgy,
                    selected_rotation_id,
                )
                is not None
            )
            grouping_exempt.set_visibility(grouped)
            grouping_exempt_help.set_visibility(grouped)
            if not grouped:
                grouping_exempt.value = False
            durations = _resident_block_duration_options(
                instance,
                resident,
                rotation_option,
            )
            duration = (
                int(duration_select.value)
                if duration_select.value is not None and int(duration_select.value) in durations
                else _default_block_duration(durations)
            )
            duration_select.set_options(durations, value=duration)
            if duration is None:
                start_select.set_options({}, value=None)
                return
            starts = _resident_block_start_options(
                instance,
                resident,
                rotation_option,
                duration,
            )
            start_select.set_options(
                starts,
                value=(
                    start_select.value if start_select.value in starts else next(iter(starts), None)
                ),
            )

        rotation_select.on_value_change(refresh_fields)
        duration_select.on_value_change(refresh_fields)
        refresh_fields()

        ui.separator()
        with ui.row().classes("w-full justify-end gap-3 p-4"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")

            def save_block() -> None:
                try:
                    if (
                        rotation_select.value is None
                        or duration_select.value is None
                        or start_select.value is None
                    ):
                        raise ValueError("select a rotation, block length, and week range")
                    rotation_option = str(rotation_select.value)
                    rotation_id, elective = _parse_rotation_option(rotation_option)
                    duration = int(duration_select.value)
                    start_week = int(start_select.value)
                    updated = replace_manual_block(
                        instance,
                        resident_id=resident.id,
                        rotation_id=rotation_id,
                        start_week=start_week,
                        duration_weeks=duration,
                        elective=elective,
                        original=original,
                        replace_weeks=replace_weeks,
                        grouping_exempt=bool(grouping_exempt.value),
                    )
                    matches_current = any(
                        block.rotation_id == rotation_id
                        and block.elective == elective
                        and block.start_week == start_week
                        and block.duration_weeks == duration
                        for block in schedule_blocks(
                            schedule,
                            resident_id=resident.id,
                        )
                    )
                    dialog.close()
                    ui.notify("Block saved", type="positive")
                    on_schedule_save(
                        updated,
                        resident.id,
                        bool(schedule_is_current and matches_current),
                    )
                except (ValidationError, ValueError) as exc:
                    ui.notify(str(exc), type="negative", multi_line=True)

            ui.button("Save block", icon="save", on_click=save_block).props("unelevated no-caps")
    dialog.open()


def _resident_block_schedule_report(
    instance: SchedulerInput,
    schedule: Schedule | None,
    resident: Resident,
    *,
    show_completed: bool,
    on_show_completed_change,
    editing: bool = False,
    on_editing_change: Callable[[], None] | None = None,
    on_schedule_save: SaveResidentSchedule | None = None,
    on_schedule_change: SaveResidentScheduleResult | None = None,
    schedule_is_current: bool = True,
    today: date | None = None,
) -> None:
    from nicegui import ui

    can_edit = bool(on_schedule_save is not None and on_editing_change is not None)
    editing = bool(editing and can_edit)
    report_rows = resident_schedule_report_rows(
        instance,
        schedule,
        resident.id,
        show_completed=show_completed,
        today=today,
    )
    with ui.column().classes("rbs-resident-schedule-content w-full min-w-0 gap-3 p-5"):
        with ui.row().classes("w-full items-center justify-between gap-3"):
            if editing:
                ui.label(
                    "Add a block or use the row actions to edit, delete, lock, or unlock "
                    "blocks. Assignment changes may require a new Solve."
                ).classes("rbs-resident-block-edit-hint rbs-type-caption rbs-text-muted")
            else:
                ui.space()
            with ui.row().classes("items-center gap-2"):
                if not editing:
                    ui.checkbox(
                        "Show completed",
                        value=show_completed,
                        on_change=on_show_completed_change,
                    ).props("dense")
                if on_schedule_save is not None:
                    edit_button = ui.button(
                        "Done editing" if editing else "Edit schedule",
                        icon="check" if editing else "edit_calendar",
                        on_click=on_editing_change,
                    ).props("outline dense no-caps")
                    edit_button.set_enabled(can_edit)
        if editing:
            assert on_schedule_save is not None
            _resident_block_schedule_manager(
                instance,
                schedule,
                resident,
                on_schedule_save=on_schedule_save,
                on_schedule_change=on_schedule_change,
                schedule_is_current=schedule_is_current,
            )
            return
        if not report_rows:
            title = (
                "No block schedule available"
                if schedule is None
                else "No current or upcoming block schedule"
            )
            description = (
                "Run Solve to generate this resident's schedule."
                if schedule is None
                else "Turn on “Show completed” to include earlier rotations."
            )
            _empty_resident_schedule(
                title,
                description,
            )
            return
        with (
            ui.element("div")
            .classes("rbs-resident-block-timeline w-full")
            .props('role="list" aria-label="Block schedule"')
        ):
            for row in report_rows:
                _resident_block_schedule_lane(row)


def _resident_block_schedule_lane(row: dict[str, str]) -> None:
    """Render one calm, uninterrupted lane in the resident block timeline."""
    from nicegui import ui

    kind = row["kind"]
    week_label = "Week" if "–" not in row["weeks"] else "Weeks"
    with ui.element("div").classes(f"rbs-resident-block-lane is-{kind}").props('role="listitem"'):
        with ui.element("div").classes("rbs-resident-block-period"):
            ui.label(f"{week_label} {row['weeks']}").classes("rbs-resident-block-week-range")
            ui.label(row["dates"]).classes("rbs-resident-block-date-range")

        with ui.element("div").classes("rbs-resident-block-track"):
            if kind == "vacation":
                with ui.element("div").classes("rbs-resident-block-band is-vacation"):
                    ui.icon("beach_access").classes("rbs-resident-block-band-icon").props(
                        'aria-hidden="true"'
                    )
                    ui.label("Vacation").classes("rbs-resident-block-band-title")
            elif kind == "special":
                with ui.element("div").classes("rbs-resident-block-band is-special"):
                    ui.icon("event").classes("rbs-resident-block-band-icon").props(
                        'aria-hidden="true"'
                    )
                    ui.label(f"Conference · {row['rotation_name']}").classes(
                        "rbs-resident-block-band-title"
                    )
            else:
                title = row["rotation_name"]
                if row["continuation"] == "true":
                    title = f"{title} (Cont.)"
                with (
                    ui.element("div")
                    .classes(f"rbs-resident-block-band is-rotation {row['color_class']}")
                    .style(
                        f"--rbs-rotation-color: {row['color']}; "
                        f"--rbs-rotation-foreground: {row['foreground']}"
                    )
                ):
                    ui.label(title).classes("rbs-resident-block-band-title")

            if row["days_off"]:
                with ui.element("div").classes("rbs-resident-block-time-off"):
                    ui.icon("event_busy").classes("rbs-resident-block-time-off-icon").props(
                        'aria-hidden="true"'
                    )
                    ui.label(row["days_off"])


def _resident_clinic_schedule_report(
    instance: SchedulerInput,
    schedule_state: dict[str, Schedule | None],
    resident: Resident,
    *,
    show_completed: bool,
    on_show_completed_change,
    editing: bool = False,
    on_editing_change: Callable[[], None] | None = None,
    on_schedule_change: SaveResidentScheduleResult | None = None,
    today: date | None = None,
) -> None:
    from nicegui import ui

    current_schedule = schedule_state["value"]
    current_clinic_occupancy = (
        occupancy(instance, current_schedule) if current_schedule is not None else {}
    )
    report_rows = resident_clinic_schedule_report_rows(
        instance,
        current_schedule,
        resident.id,
        show_completed=show_completed,
        today=today,
        clinic_occupancy=current_clinic_occupancy,
    )
    can_edit = bool(
        current_schedule is not None
        and report_rows
        and on_schedule_change is not None
        and on_editing_change is not None
    )
    editing = bool(editing and can_edit)
    with ui.column().classes("rbs-resident-schedule-content w-full min-w-0 gap-3 p-5"):
        with ui.row().classes("w-full items-center justify-between gap-3"):
            if editing:
                ui.label(
                    "Drag an unlocked clinic block to an open half-day, or onto another "
                    "unlocked block to swap them within the week. Right-click a half-day "
                    "to add, remove, or change a clinic site."
                ).classes("rbs-resident-clinic-edit-hint rbs-type-caption rbs-text-muted")
            else:
                ui.space()
            with ui.row().classes("items-center gap-2"):
                if on_schedule_change is not None:
                    edit_button = ui.button(
                        "Done editing" if editing else "Edit schedule",
                        icon="check" if editing else "edit_calendar",
                        on_click=on_editing_change,
                    ).props("outline dense no-caps")
                    edit_button.set_enabled(can_edit)
                ui.checkbox(
                    "Show completed",
                    value=show_completed,
                    on_change=on_show_completed_change,
                ).props("dense")
        if not report_rows:
            if current_schedule is None or current_schedule.is_empty():
                title = "No clinic schedule available"
                description = "Run Solve to generate this resident's clinic schedule."
            elif not show_completed and resident_clinic_schedule_report_rows(
                instance,
                current_schedule,
                resident.id,
                clinic_occupancy=current_clinic_occupancy,
            ):
                title = "No current or upcoming clinic schedule"
                description = "Turn on “Show completed” to include earlier clinic sessions."
            else:
                title = "No clinic schedule available"
                description = "This resident has no assigned clinic sessions."
            _empty_resident_schedule(title, description)
            return
        calendar_classes = "rbs-resident-clinic-calendar-list w-full min-w-0 gap-4"
        if editing:
            calendar_classes += " is-editing"

        week_containers = {}

        def render_week(week: int) -> None:
            container = week_containers[week]
            container.clear()
            schedule = schedule_state["value"]
            if schedule is None:
                return
            updated_rows = resident_clinic_schedule_report_rows(
                instance,
                schedule,
                resident.id,
                show_completed=show_completed,
                today=today,
                clinic_occupancy=current_clinic_occupancy,
            )
            updated_row = next(
                (candidate for candidate in updated_rows if int(candidate["week"]) == week),
                None,
            )
            if updated_row is None:
                return
            with container:
                _resident_clinic_week_calendar(
                    instance,
                    schedule,
                    resident,
                    updated_row,
                    editing=editing,
                    on_schedule_change=partial(save_week_change, week=week),
                    clinic_occupancy=current_clinic_occupancy,
                    schedule_state=schedule_state,
                    today=today,
                )

        def save_week_change(
            updated: Schedule,
            resident_id: str,
            _refresh: bool,
            *,
            week: int,
        ) -> None:
            nonlocal current_clinic_occupancy
            if on_schedule_change is None:
                return
            on_schedule_change(updated, resident_id, False)
            schedule_state["value"] = updated
            current_clinic_occupancy = occupancy(instance, updated)
            render_week(week)

        with ui.column().classes(calendar_classes):
            for row in report_rows:
                assert current_schedule is not None
                week = int(row["week"])
                week_containers[week] = ui.column().classes("w-full gap-0")
                with week_containers[week]:
                    _resident_clinic_week_calendar(
                        instance,
                        current_schedule,
                        resident,
                        row,
                        editing=editing,
                        on_schedule_change=partial(save_week_change, week=week),
                        clinic_occupancy=current_clinic_occupancy,
                        schedule_state=schedule_state,
                        today=today,
                    )
        ui.label(
            "Every week in the selected range is listed. Weeks without assigned clinic "
            "dates remain blank; vacation weeks and individual days off are also blank, "
            "and Special events replace clinic in their scheduled half-days."
        ).classes("rbs-type-caption rbs-text-muted")


@dataclass
class _ResidentClinicWeekContext:
    instance: SchedulerInput
    schedule: Schedule
    resident: Resident
    row: dict[str, str]
    editing: bool
    on_schedule_change: SaveResidentScheduleResult | None
    clinic_occupancy: ClinicOccupancy | None
    schedule_state: dict[str, Schedule | None] | None
    today: date | None
    week: int
    has_rotation_assignment: bool
    client: Any

    def notify(self, message: str, **options) -> None:
        from nicegui import ui

        with self.client:
            ui.notify(message, **options)

    def active_schedule(self) -> Schedule:
        current = self.schedule_state["value"] if self.schedule_state is not None else self.schedule
        if current is None:
            raise ValueError("The clinic schedule is no longer available.")
        return current

    def save_lock(self, weekday: Weekday, session: Session, locked: bool) -> None:
        if self.on_schedule_change is None:
            return
        try:
            updated = set_resident_clinic_slot_locked(
                self.instance,
                self.active_schedule(),
                resident_id=self.resident.id,
                week=self.week,
                weekday=weekday,
                session=session,
                locked=locked,
                today=self.today,
            )
            self.on_schedule_change(updated, self.resident.id, False)
            self.notify(
                "Clinic block locked" if locked else "Clinic block unlocked",
                type="positive",
            )
        except (ValidationError, ValueError) as exc:
            self.notify(str(exc), type="negative", multi_line=True)

    def add_extra_block(
        self,
        weekday: Weekday,
        session: Session,
        site_id: str,
    ) -> None:
        if self.on_schedule_change is None:
            return
        try:
            updated = add_resident_clinic_slot(
                self.instance,
                self.active_schedule(),
                resident_id=self.resident.id,
                week=self.week,
                weekday=weekday,
                session=session,
                site_id=site_id,
            )
            self.on_schedule_change(updated, self.resident.id, False)
            site_name = self.instance.clinic_policy.site_name(site_id)
            self.notify(
                f"Extra clinic block added at {site_name} as a manual override",
                type="warning",
            )
        except (ValidationError, ValueError) as exc:
            self.notify(str(exc), type="negative", multi_line=True)

    def delete_block(self, weekday: Weekday, session: Session) -> None:
        if self.on_schedule_change is None:
            return
        try:
            updated = remove_resident_clinic_slot(
                self.instance,
                self.active_schedule(),
                resident_id=self.resident.id,
                week=self.week,
                weekday=weekday,
                session=session,
                today=self.today,
            )
            self.on_schedule_change(updated, self.resident.id, False)
            self.notify("Clinic block removed as a manual override", type="warning")
        except (ValidationError, ValueError) as exc:
            self.notify(str(exc), type="negative", multi_line=True)

    def change_site(
        self,
        weekday: Weekday,
        session: Session,
        site_id: str,
    ) -> None:
        if self.on_schedule_change is None:
            return
        try:
            current_schedule = self.active_schedule()
            original_slot = resident_clinic_slot(
                self.instance,
                current_schedule,
                self.resident.id,
                self.week,
                weekday,
                session,
            )
            resetting_site = original_slot.manual_override_original_site == site_id
            updated = change_resident_clinic_slot_site(
                self.instance,
                current_schedule,
                resident_id=self.resident.id,
                week=self.week,
                weekday=weekday,
                session=session,
                site_id=site_id,
                today=self.today,
            )
            if updated == current_schedule:
                return
            self.on_schedule_change(updated, self.resident.id, False)
            self._notify_site_change(updated, weekday, session, site_id, resetting_site)
        except (ValidationError, ValueError) as exc:
            self.notify(str(exc), type="negative", multi_line=True)

    def _notify_site_change(
        self,
        updated: Schedule,
        weekday: Weekday,
        session: Session,
        site_id: str,
        resetting_site: bool,
    ) -> None:
        site_name = self.instance.clinic_policy.site_name(site_id)
        if not resetting_site:
            self.notify(
                f"Clinic site changed to {site_name} as a manual override",
                type="warning",
            )
            return
        reset_slot = resident_clinic_slot(
            self.instance,
            updated,
            self.resident.id,
            self.week,
            weekday,
            session,
        )
        suffix = (
            "; this block remains a manual override for another reason"
            if reset_slot.manual_override
            else ""
        )
        self.notify(
            f"Clinic site reset to {site_name}; site override cleared{suffix}",
            type="warning" if reset_slot.manual_override else "positive",
        )

    def mark_conflict(
        self,
        cell: Any,
        marker_state: dict[str, bool],
        reason: str,
    ) -> None:
        if marker_state["suppress"]:
            return
        cell.classes(add="is-invalid-assignment")
        if marker_state["shown"]:
            return
        with cell:
            _resident_clinic_conflict_icon(reason)
        marker_state["shown"] = True

    def move_block(
        self,
        event: Any,
        *,
        target_weekday: Weekday,
        target_session: Session,
        cell: Any,
        marker_state: dict[str, bool],
    ) -> None:
        if self.on_schedule_change is None:
            return
        try:
            self._move_block(
                event,
                target_weekday=target_weekday,
                target_session=target_session,
            )
        except (TypeError, ValidationError, ValueError) as exc:
            reason = str(exc) or "This half-day conflicts with a scheduling rule."
            self.mark_conflict(cell, marker_state, reason)
            self.notify(
                f"Clinic block not moved: {reason}",
                type="warning",
                multi_line=True,
            )

    def _move_block(
        self,
        event: Any,
        *,
        target_weekday: Weekday,
        target_session: Session,
    ) -> None:
        assert self.on_schedule_change is not None
        current_schedule = self.active_schedule()
        payload = event.args if isinstance(event.args, dict) else {}
        source_week = int(payload.get("week"))
        source_weekday = Weekday(str(payload.get("weekday")))
        source_session = Session(str(payload.get("session")))
        updated = move_resident_clinic_slot(
            self.instance,
            current_schedule,
            resident_id=self.resident.id,
            source_week=source_week,
            source_weekday=source_weekday,
            source_session=source_session,
            target_week=self.week,
            target_weekday=target_weekday,
            target_session=target_session,
            today=self.today,
        )
        if updated == current_schedule:
            return
        self.on_schedule_change(updated, self.resident.id, False)
        slots = self._moved_override_slots(
            updated,
            target_weekday,
            target_session,
            source_weekday,
            source_session,
        )
        self._notify_move(updated, slots)

    def _moved_override_slots(
        self,
        updated: Schedule,
        target_weekday: Weekday,
        target_session: Session,
        source_weekday: Weekday,
        source_session: Session,
    ) -> tuple[AssignedClinic, ...]:
        slots: list[AssignedClinic] = []
        positions = dict.fromkeys(
            ((target_weekday, target_session), (source_weekday, source_session))
        )
        for weekday, session in positions:
            try:
                slot = resident_clinic_slot(
                    self.instance,
                    updated,
                    self.resident.id,
                    self.week,
                    weekday,
                    session,
                )
            except ValueError:
                continue
            if slot.manual_override:
                slots.append(slot)
        return tuple(slots)

    def _notify_move(
        self,
        updated: Schedule,
        slots: tuple[AssignedClinic, ...],
    ) -> None:
        reasons: list[str] = []
        for slot in slots:
            reasons.extend(
                resident_clinic_target_conflicts(
                    self.instance,
                    updated,
                    resident_id=self.resident.id,
                    week=self.week,
                    weekday=slot.weekday,
                    session=slot.session,
                    source_slot=slot,
                )
            )
        if reasons:
            self.notify(
                "Clinic block moved as a manual override: " + " ".join(dict.fromkeys(reasons)),
                type="warning",
                multi_line=True,
            )
        elif slots:
            self.notify(
                "Clinic block moved and remains a manual override",
                type="warning",
            )
        else:
            self.notify("Clinic block moved", type="positive")


@dataclass(frozen=True)
class _ResidentClinicCell:
    weekday: Weekday
    session: Session
    key: str
    location: str
    kind: str
    visible_slot: AssignedClinic | None
    manual_override: bool
    conflicts: tuple[str, ...]
    academic: bool
    locked: bool


def _resident_clinic_week_calendar(
    instance: SchedulerInput,
    schedule: Schedule,
    resident: Resident,
    row: dict[str, str],
    *,
    editing: bool = False,
    on_schedule_change: SaveResidentScheduleResult | None = None,
    clinic_occupancy: ClinicOccupancy | None = None,
    schedule_state: dict[str, Schedule | None] | None = None,
    today: date | None = None,
) -> None:
    from nicegui import ui

    week = int(row["week"])
    context = _ResidentClinicWeekContext(
        instance=instance,
        schedule=schedule,
        resident=resident,
        row=row,
        editing=editing,
        on_schedule_change=on_schedule_change,
        clinic_occupancy=clinic_occupancy,
        schedule_state=schedule_state,
        today=today,
        week=week,
        has_rotation_assignment=any(
            assignment.resident_id == resident.id and week in assignment.weeks
            for assignment in schedule.assignments
        ),
        client=ui.context.client,
    )
    with ui.element("section").classes("rbs-resident-clinic-week w-full"):
        _resident_clinic_week_header(context)
        _resident_clinic_week_grid(context, clinic_weekdays(instance))


def _resident_clinic_week_header(context: _ResidentClinicWeekContext) -> None:
    from nicegui import ui

    with ui.row().classes(
        "rbs-resident-clinic-week-header w-full items-center justify-between gap-3"
    ):
        with ui.column().classes("gap-1"):
            with ui.row().classes("items-baseline gap-2"):
                ui.label(f"Week {context.row['week']}").classes("rbs-resident-clinic-week-number")
                ui.label(context.row["dates"]).classes("rbs-type-caption rbs-text-muted")
            detail = _resident_clinic_override_detail(context)
            if detail:
                with ui.row().classes("rbs-resident-clinic-week-override items-center gap-1"):
                    ui.icon("info").classes("rbs-icon-sm")
                    ui.label(f"{detail} this week · manual override")
        ui.label(context.row["rotation"]).classes("rbs-resident-clinic-week-rotation rbs-type-body")


def _resident_clinic_override_detail(
    context: _ResidentClinicWeekContext,
) -> str | None:
    if not context.has_rotation_assignment:
        return None
    delta = resident_clinic_week_override_delta(
        context.instance,
        context.schedule,
        context.resident.id,
        context.week,
    )
    count = abs(delta)
    if delta > 0:
        noun = "half-day" if count == 1 else "half-days"
        return f"{count} extra clinic {noun} than usual"
    if delta == -1:
        return "1 less clinic half-day than usual"
    if delta < 0:
        return f"{count} fewer clinic half-days than usual"
    return None


def _resident_clinic_week_grid(
    context: _ResidentClinicWeekContext,
    weekdays: tuple[Weekday, ...],
) -> None:
    from nicegui import ui

    with (
        ui.element("div")
        .classes("rbs-resident-clinic-week-grid w-full")
        .style(f"--rbs-resident-clinic-days: {len(weekdays)}")
    ):
        ui.element("div").classes("rbs-resident-clinic-grid-corner")
        for weekday in weekdays:
            _resident_clinic_day_header(context.row, weekday)
        for session in Session:
            ui.label("AM" if session is Session.MORNING else "PM").classes(
                "rbs-resident-clinic-session-label"
            )
            for weekday in weekdays:
                _resident_clinic_session_cell(context, weekday, session)


def _resident_clinic_day_header(row: dict[str, str], weekday: Weekday) -> None:
    from nicegui import ui

    with ui.element("div").classes("rbs-resident-clinic-day-header"):
        ui.label(weekday.value[:3]).classes("rbs-resident-clinic-day-name")
        ui.label(row[f"{weekday.value}_date"]).classes("rbs-resident-clinic-day-date")


def _resident_clinic_cell_state(
    context: _ResidentClinicWeekContext,
    weekday: Weekday,
    session: Session,
) -> _ResidentClinicCell:
    key = _resident_clinic_session_key(weekday, session)
    location = context.row[key]
    kind = context.row[f"{key}_kind"]
    visible_slot = None
    if location and kind != "special-event":
        visible_slot = resident_clinic_slot(
            context.instance,
            context.schedule,
            context.resident.id,
            context.week,
            weekday,
            session,
        )
    conflicts = ()
    if context.has_rotation_assignment:
        conflicts = resident_clinic_target_conflicts(
            context.instance,
            context.schedule,
            resident_id=context.resident.id,
            week=context.week,
            weekday=weekday,
            session=session,
            clinic_occupancy=context.clinic_occupancy,
        )
    academic = context.instance.is_academic_half_day(context.week, weekday, session)
    locked = bool(
        visible_slot
        and resident_clinic_slot_locked(
            context.schedule,
            context.resident.id,
            context.week,
            weekday,
            session,
            instance=context.instance,
            today=context.today,
        )
    )
    return _ResidentClinicCell(
        weekday=weekday,
        session=session,
        key=key,
        location=location,
        kind=kind,
        visible_slot=visible_slot,
        manual_override=bool(visible_slot and visible_slot.manual_override),
        conflicts=conflicts,
        academic=academic,
        locked=locked,
    )


def _resident_clinic_session_cell(
    context: _ResidentClinicWeekContext,
    weekday: Weekday,
    session: Session,
) -> None:
    from nicegui import ui

    state = _resident_clinic_cell_state(context, weekday, session)
    cell = ui.element("div").classes(_resident_clinic_cell_classes(state))
    marker_state = {
        "shown": bool(
            context.editing and state.conflicts and not state.academic and not state.manual_override
        ),
        "suppress": state.academic,
    }
    if _resident_clinic_drop_enabled(context):
        _resident_clinic_bind_drop(context, state, cell, marker_state)
    with cell:
        if marker_state["shown"]:
            _resident_clinic_conflict_icon(" ".join(state.conflicts))
        if _resident_clinic_fixed_event(state):
            return
        if _resident_clinic_drop_enabled(context):
            _resident_clinic_context_menu(context, state)
        if not state.location:
            ui.label("—").classes("rbs-resident-clinic-session-empty")
            return
        _resident_clinic_event(context, state)


def _resident_clinic_cell_classes(state: _ResidentClinicCell) -> str:
    classes = "rbs-resident-clinic-session-cell"
    if state.conflicts:
        classes += " is-invalid-target"
    if state.location:
        classes += " is-occupied"
    if state.manual_override:
        classes += " has-manual-override"
    return classes


def _resident_clinic_drop_enabled(context: _ResidentClinicWeekContext) -> bool:
    return bool(
        context.editing
        and context.on_schedule_change is not None
        and context.has_rotation_assignment
    )


def _resident_clinic_bind_drop(
    context: _ResidentClinicWeekContext,
    state: _ResidentClinicCell,
    cell: Any,
    marker_state: dict[str, bool],
) -> None:
    cell.on("dragover", js_handler=_CLINIC_DRAG_OVER_JS)
    cell.on("dragleave", js_handler=_CLINIC_DRAG_LEAVE_JS)
    cell.on(
        "drop",
        partial(
            context.move_block,
            target_weekday=state.weekday,
            target_session=state.session,
            cell=cell,
            marker_state=marker_state,
        ),
        js_handler=_CLINIC_DROP_JS,
    )


def _resident_clinic_fixed_event(state: _ResidentClinicCell) -> bool:
    from nicegui import ui

    if state.kind == "special-event":
        with ui.element("div").classes("rbs-resident-clinic-event special-event is-locked"):
            ui.label(state.location).classes("rbs-resident-clinic-event-location")
            with ui.icon("lock").classes("rbs-resident-clinic-fixed-lock"):
                ui.tooltip("This Special event replaces clinic and cannot be rescheduled here.")
        return True
    if not state.academic:
        return False
    with ui.element("div").classes("rbs-resident-clinic-event academic is-locked"):
        ui.label("Academic Half Day")
        with ui.icon("lock").classes("rbs-resident-clinic-fixed-lock"):
            ui.tooltip("Academic Half Day is fixed and cannot be rescheduled.")
    return True


def _resident_clinic_context_menu(
    context: _ResidentClinicWeekContext,
    state: _ResidentClinicCell,
) -> None:
    from nicegui import ui

    available_site_ids = resident_clinic_available_site_ids(
        context.instance,
        context.schedule,
        resident_id=context.resident.id,
        week=context.week,
        weekday=state.weekday,
        session=state.session,
        clinic_occupancy=context.clinic_occupancy,
    )
    with ui.context_menu().classes("rbs-resident-clinic-context-menu"):
        if state.visible_slot is None:
            _resident_clinic_add_menu(context, state, available_site_ids)
        else:
            _resident_clinic_existing_menu(context, state, available_site_ids)


def _resident_clinic_add_menu(
    context: _ResidentClinicWeekContext,
    state: _ResidentClinicCell,
    available_site_ids: tuple[str, ...],
) -> None:
    from nicegui import ui

    ui.label("Add extra block").classes("rbs-resident-clinic-context-heading")
    if not available_site_ids:
        ui.label("No site has preceptor availability").classes("rbs-resident-clinic-context-empty")
        return
    for site_id in available_site_ids:
        ui.menu_item(
            context.instance.clinic_policy.site_name(site_id),
            on_click=partial(
                context.add_extra_block,
                state.weekday,
                state.session,
                site_id,
            ),
        )


def _resident_clinic_existing_menu(
    context: _ResidentClinicWeekContext,
    state: _ResidentClinicCell,
    available_site_ids: tuple[str, ...],
) -> None:
    from nicegui import ui

    assert state.visible_slot is not None
    delete_item = ui.menu_item(
        "Delete clinic block",
        on_click=partial(context.delete_block, state.weekday, state.session),
    )
    if state.locked:
        delete_item.props("disable")
    if state.visible_slot.admin:
        return
    ui.separator()
    ui.label("Change clinic site").classes("rbs-resident-clinic-context-heading")
    _resident_clinic_site_menu(context, state, available_site_ids)


def _resident_clinic_site_menu(
    context: _ResidentClinicWeekContext,
    state: _ResidentClinicCell,
    available_site_ids: tuple[str, ...],
) -> None:
    from nicegui import ui

    assert state.visible_slot is not None
    current_site = state.visible_slot.site or context.instance.clinic_policy.primary_site_id
    original_site = state.visible_slot.manual_override_original_site
    if original_site is not None:
        reset_item = ui.menu_item(
            "Reset to " + context.instance.clinic_policy.site_name(original_site),
            on_click=partial(
                context.change_site,
                state.weekday,
                state.session,
                original_site,
            ),
        )
        if state.locked or original_site not in available_site_ids:
            reset_item.props("disable")
    alternatives = tuple(
        site_id
        for site_id in available_site_ids
        if site_id != current_site and site_id != original_site
    )
    if not alternatives:
        ui.label("No other site has preceptor availability").classes(
            "rbs-resident-clinic-context-empty"
        )
        return
    for site_id in alternatives:
        site_item = ui.menu_item(
            context.instance.clinic_policy.site_name(site_id),
            on_click=partial(
                context.change_site,
                state.weekday,
                state.session,
                site_id,
            ),
        )
        if state.locked:
            site_item.props("disable")


def _resident_clinic_event(
    context: _ResidentClinicWeekContext,
    state: _ResidentClinicCell,
) -> None:
    from nicegui import ui

    event_classes = f"rbs-resident-clinic-event {state.kind}"
    if state.locked:
        event_classes += " is-locked"
    if state.manual_override:
        event_classes += " manual-override"
    event = (
        ui.element("div")
        .classes(event_classes)
        .props(
            f"draggable={'true' if context.editing and not state.locked else 'false'} "
            f"data-week={context.week} data-weekday={state.weekday.value} "
            f"data-session={state.session.value}"
        )
    )
    color = context.row[f"{state.key}_color"]
    tint = context.row[f"{state.key}_tint"]
    if color and tint:
        event.style(f"--rbs-clinic-site-color: {color}; --rbs-clinic-site-tint: {tint}")
    if context.editing and not state.locked:
        event.on("dragstart", js_handler=_CLINIC_DRAG_START_JS)
        event.on("dragend", js_handler=_CLINIC_DRAG_END_JS)
    with event:
        ui.label(state.location).classes("rbs-resident-clinic-event-location")
        _resident_clinic_override_badge(context, state)
        if context.editing:
            _resident_clinic_lock_button(context, state)


def _resident_clinic_override_badge(
    context: _ResidentClinicWeekContext,
    state: _ResidentClinicCell,
) -> None:
    from nicegui import ui

    if not state.manual_override or state.visible_slot is None:
        return
    reasons = resident_clinic_target_conflicts(
        context.instance,
        context.schedule,
        resident_id=context.resident.id,
        week=context.week,
        weekday=state.weekday,
        session=state.session,
        source_slot=state.visible_slot,
        clinic_occupancy=context.clinic_occupancy,
    )
    with ui.badge("Manual override").classes("rbs-resident-clinic-override-badge"):
        ui.tooltip(" ".join(reasons) or "This clinic block was changed manually.")


def _resident_clinic_lock_button(
    context: _ResidentClinicWeekContext,
    state: _ResidentClinicCell,
) -> None:
    from nicegui import ui

    action = "Unlock" if state.locked else "Lock"
    lock_button = (
        ui.button(
            icon="lock" if state.locked else "lock_open",
            on_click=partial(
                context.save_lock,
                state.weekday,
                state.session,
                not state.locked,
            ),
        )
        .props(f"flat round dense draggable=false aria-label='{action} clinic block'")
        .classes("rbs-resident-clinic-event-lock")
    )
    lock_button.on(
        "mousedown",
        js_handler="(event) => event.stopPropagation()",
    )
    with lock_button:
        ui.tooltip(f"{action} clinic block")


def _resident_clinic_conflict_icon(reason: str) -> None:
    from nicegui import ui

    with ui.icon("warning_amber").classes("rbs-resident-clinic-conflict-icon"):
        ui.tooltip(reason)


def _empty_resident_schedule(title: str, description: str) -> None:
    from nicegui import ui

    with ui.row().classes("rbs-resident-empty-schedule w-full items-center gap-3 rounded p-4"):
        ui.icon("event_busy").classes("rbs-text-subtle")
        with ui.column().classes("gap-0"):
            ui.label(title).classes("rbs-font-semibold")
            ui.label(description).classes("rbs-type-caption rbs-text-muted")


def _resident_clinic_half_day_editor(
    instance: SchedulerInput,
    initial: list[ResidentClinicHalfDay],
) -> list[dict]:
    from nicegui import ui

    drafts = [item.model_dump(mode="json") for item in initial]
    weekdays = {weekday.value: weekday.value.title() for weekday in clinic_weekdays(instance)}
    sessions = {
        Session.MORNING.value: "Morning",
        Session.AFTERNOON.value: "Afternoon",
    }
    sites = {site.id: site.name for site in instance.clinic_policy.sites}
    rows = ui.column().classes("w-full gap-2")

    def render_rows() -> None:
        rows.clear()
        with rows:
            if not drafts:
                ui.label("No recurring resident half-days.").classes("rbs-type-body rbs-text-muted")
                return
            for draft in drafts:
                with ui.row().classes(
                    "rbs-resident-clinic-half-day w-full items-center gap-3 rounded p-3 flex-wrap"
                ):
                    day = (
                        ui.select(
                            weekdays,
                            value=draft["weekday"],
                            label="Day",
                        )
                        .props("outlined dense options-dense")
                        .classes("w-40")
                    )
                    session = (
                        ui.select(
                            sessions,
                            value=draft["session"],
                            label="Time",
                        )
                        .props("outlined dense options-dense")
                        .classes("w-40")
                    )
                    clinic_sites = (
                        ui.select(
                            sites,
                            value=list(draft.get("sites") or []),
                            label="Allowed clinics (all if empty)",
                            multiple=True,
                        )
                        .props("outlined dense options-dense use-chips")
                        .classes("min-w-64 flex-1")
                    )
                    day.on_value_change(
                        lambda event, item=draft: item.__setitem__(
                            "weekday",
                            str(event.value),
                        )
                    )
                    session.on_value_change(
                        lambda event, item=draft: item.__setitem__(
                            "session",
                            str(event.value),
                        )
                    )
                    clinic_sites.on_value_change(
                        lambda event, item=draft: item.__setitem__(
                            "sites",
                            list(event.value or []),
                        )
                    )

                    def remove(item=draft) -> None:
                        drafts.remove(item)
                        render_rows()

                    ui.button(icon="delete_outline", on_click=remove).props(
                        "flat round dense color=negative aria-label='Remove clinic half-day'"
                    )

    def add_half_day() -> None:
        existing = {(str(item["weekday"]), str(item["session"])) for item in drafts}
        available = [
            (weekday, session)
            for weekday in weekdays
            for session in sessions
            if (weekday, session) not in existing
            and not (
                Weekday(weekday) is instance.clinic_policy.academic.weekday
                and Session(session) is instance.clinic_policy.academic.session
            )
        ]
        if not available:
            ui.notify("All available half-days have already been added", type="warning")
            return
        weekday, session = available[0]
        drafts.append({"weekday": weekday, "session": session, "sites": []})
        render_rows()

    render_rows()
    ui.button("Add half-day", icon="add", on_click=add_half_day).props("outline dense no-caps")
    return drafts
