"""Special rotation configuration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from functools import partial

from pydantic import ValidationError

from rbs.models.enums import Session
from rbs.models.instance import SchedulerInput
from rbs.models.special import SpecialRotation, SpecialRotationKind
from rbs.ui import master_detail
from rbs.ui.editor_common import (
    _validation_message,
)
from rbs.ui.rotations.special_ops import (
    add_special_rotation,
    next_special_rotation_id,
    remove_special_rotation,
    replace_special_rotation,
)
from rbs.ui.rotations.types import (
    SaveRotation,
)

_SPECIAL_EVENT_TIME_OPTIONS = {
    "full_day": "Full day",
    Session.MORNING.value: "Morning",
    Session.AFTERNOON.value: "Afternoon",
}


def _special_configuration(
    instance: SchedulerInput,
    *,
    selected_rotation_id: str | None,
    on_save: SaveRotation,
) -> None:
    """Render dated conference and clinic-event scheduling."""
    from nicegui import ui

    with ui.column().classes("w-full gap-4"):
        with ui.row().classes("w-full items-stretch gap-4 flex-wrap"):
            _special_type_card(
                title="Conference/Multi-Day",
                description=("Overrides block schedule and suppresses continuity clinic."),
                icon="date_range",
                action_label="Add conference",
                on_add=partial(
                    _open_special_rotation_dialog,
                    instance,
                    SpecialRotationKind.CONFERENCE,
                    selected_rotation_id=selected_rotation_id,
                    on_save=on_save,
                ),
            )
            _special_type_card(
                title="Events (Half/Single Day)",
                description=(
                    "A half-day or full-day shift that replaces clinic/block "
                    "sections for its assigned residents."
                ),
                icon="event",
                action_label="Add event",
                on_add=partial(
                    _open_special_rotation_dialog,
                    instance,
                    SpecialRotationKind.EVENT,
                    selected_rotation_id=selected_rotation_id,
                    on_save=on_save,
                ),
            )

        with master_detail.detail_card():
            with ui.column().classes("w-full gap-3 p-5"):
                with ui.row().classes("w-full items-center justify-between gap-3"):
                    ui.label("Scheduled").classes("rbs-type-section-title")
                    count = len(instance.special_rotations)
                    ui.badge(
                        f"{count} item" if count == 1 else f"{count} items",
                        color="secondary",
                    ).props("outline")
                if not instance.special_rotations:
                    with ui.row().classes(
                        "rbs-special-empty w-full items-center gap-3 rounded px-4 py-4"
                    ):
                        ui.icon("event_available").classes("rbs-text-subtle")
                        with ui.column().classes("gap-0"):
                            ui.label("No special rotations scheduled.").classes("rbs-font-semibold")
                            ui.label("Add a conference or event to assign residents.").classes(
                                "rbs-type-caption rbs-text-muted"
                            )
                else:
                    for special in instance.special_rotations:
                        _special_rotation_row(
                            instance,
                            special,
                            selected_rotation_id=selected_rotation_id,
                            on_save=on_save,
                        )


def _special_type_card(
    *,
    title: str,
    description: str,
    icon: str,
    action_label: str,
    on_add: Callable[[], None],
) -> None:
    from nicegui import ui

    with (
        ui.card().props("flat bordered").classes("rbs-special-type-card min-w-72 flex-1 p-5 gap-4")
    ):
        with ui.row().classes("w-full items-start gap-3"):
            with (
                ui.avatar(icon=icon, color="primary", text_color="white")
                .props("size=42px")
                .classes("rbs-primary-avatar")
            ):
                pass
            with ui.column().classes("min-w-0 flex-1 gap-1"):
                ui.label(title).classes("rbs-type-section-title")
                ui.label(description).classes("rbs-type-body rbs-text-muted")
        ui.button(action_label, icon="add", on_click=on_add).props("outline no-caps").classes(
            "self-start"
        )


def _special_rotation_row(
    instance: SchedulerInput,
    special: SpecialRotation,
    *,
    selected_rotation_id: str | None,
    on_save: SaveRotation,
) -> None:
    from nicegui import ui

    residents = [instance.residents_by_id[resident_id] for resident_id in special.resident_ids]
    names = ", ".join(resident.name for resident in residents)
    if special.kind is SpecialRotationKind.CONFERENCE:
        icon = "date_range"
        badge = "Conference/Multi-Day"
    else:
        icon = "event"
        badge = "Event"
    with ui.row().classes("rbs-special-row w-full items-center gap-3 rounded px-3 py-3"):
        ui.icon(icon).classes("rbs-special-row-icon rbs-text-primary")
        with ui.column().classes("min-w-0 flex-1 gap-0"):
            with ui.row().classes("items-center gap-2"):
                ui.label(special.name).classes("rbs-font-semibold")
                ui.badge(badge, color="secondary").props("outline")
            ui.label(_special_rotation_period_label(special)).classes(
                "rbs-type-caption rbs-text-muted"
            )
            ui.label(names).classes("rbs-type-body")

        ui.button(
            icon="edit",
            on_click=partial(
                _open_special_rotation_dialog,
                instance,
                special.kind,
                selected_rotation_id=selected_rotation_id,
                on_save=on_save,
                initial=special,
            ),
        ).props(f"flat round dense aria-label='Edit special rotation {special.name}'")

        def delete_special() -> None:
            try:
                updated = remove_special_rotation(instance, special.id)
                ui.notify(f"Removed {special.name}", type="positive")
                on_save(updated, selected_rotation_id)
            except (ValidationError, ValueError) as exc:
                ui.notify(_validation_message(exc), type="negative", multi_line=True)

        ui.button(icon="delete_outline", on_click=delete_special).props(
            f"flat round dense color=negative aria-label='Delete special rotation {special.name}'"
        )


def _special_rotation_period_label(special: SpecialRotation) -> str:
    if special.start_date == special.end_date:
        dates = f"{special.start_date:%a, %b} {special.start_date.day}, {special.start_date:%Y}"
    else:
        dates = (
            f"{special.start_date:%b} {special.start_date.day}–"
            f"{special.end_date:%b} {special.end_date.day}, {special.end_date:%Y}"
        )
    if special.kind is SpecialRotationKind.EVENT:
        time = (
            _SPECIAL_EVENT_TIME_OPTIONS[special.session.value]
            if special.session is not None
            else _SPECIAL_EVENT_TIME_OPTIONS["full_day"]
        )
        return f"{dates} · {time}"
    return dates


def _open_special_rotation_dialog(
    instance: SchedulerInput,
    kind: SpecialRotationKind,
    *,
    selected_rotation_id: str | None,
    on_save: SaveRotation,
    initial: SpecialRotation | None = None,
) -> None:
    """Open the shared date-first, residents-second Special scheduling flow."""
    from nicegui import ui

    first_day = instance.calendar.first_week_start
    last_day = first_day + timedelta(days=instance.calendar.weeks * 7 - 1)
    default_day = min(max(date.today(), first_day), last_day)
    title = (
        "Conference/Multi-Day"
        if kind is SpecialRotationKind.CONFERENCE
        else "Event (Half/Single Day)"
    )
    residents = {
        resident.id: (f"{resident.name} · {instance.training_level_name(resident.pgy)}")
        for resident in sorted(instance.residents, key=lambda item: item.name.casefold())
    }

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-3xl p-0 gap-0"):
        with ui.row().classes("w-full items-center justify-between gap-3 px-5 py-4"):
            ui.label(f"{'Edit' if initial is not None else 'Add'} {title}").classes(
                "rbs-type-dialog-title"
            )
            ui.button(icon="close", on_click=dialog.close).props(
                "flat round dense aria-label='Close special rotation dialog'"
            )
        ui.separator()
        with ui.column().classes("w-full gap-5 p-5"):
            name = (
                ui.input("Name", value=initial.name if initial is not None else "")
                .props("outlined maxlength=120")
                .classes("w-full")
            )

            with ui.column().classes("rbs-special-dialog-step w-full gap-3 rounded p-4"):
                with ui.row().classes("items-center gap-2"):
                    ui.badge("1", color="primary").props("rounded")
                    ui.label("Choose date or dates").classes("rbs-font-semibold")
                if kind is SpecialRotationKind.CONFERENCE:
                    with ui.row().classes("w-full items-end gap-4 flex-wrap"):
                        start_date = (
                            ui.input(
                                "Start date",
                                value=(
                                    initial.start_date.isoformat()
                                    if initial is not None
                                    else default_day.isoformat()
                                ),
                            )
                            .props(
                                f"outlined type=date min={first_day.isoformat()} "
                                f"max={last_day.isoformat()}"
                            )
                            .classes("min-w-52 flex-1")
                        )
                        end_date = (
                            ui.input(
                                "End date",
                                value=(
                                    initial.end_date.isoformat()
                                    if initial is not None
                                    else default_day.isoformat()
                                ),
                            )
                            .props(
                                f"outlined type=date min={first_day.isoformat()} "
                                f"max={last_day.isoformat()}"
                            )
                            .classes("min-w-52 flex-1")
                        )
                    event_time = None
                else:
                    with ui.row().classes("w-full items-end gap-4 flex-wrap"):
                        start_date = (
                            ui.input(
                                "Date",
                                value=(
                                    initial.start_date.isoformat()
                                    if initial is not None
                                    else default_day.isoformat()
                                ),
                            )
                            .props(
                                f"outlined type=date min={first_day.isoformat()} "
                                f"max={last_day.isoformat()}"
                            )
                            .classes("min-w-52 flex-1")
                        )
                        event_time = (
                            ui.select(
                                _SPECIAL_EVENT_TIME_OPTIONS,
                                value=(
                                    initial.session.value
                                    if initial is not None and initial.session is not None
                                    else "full_day"
                                ),
                                label="Time",
                            )
                            .props("outlined options-dense")
                            .classes("min-w-52 flex-1")
                        )
                    end_date = start_date

            with ui.column().classes("rbs-special-dialog-step w-full gap-3 rounded p-4"):
                with ui.row().classes("items-center gap-2"):
                    ui.badge("2", color="primary").props("rounded")
                    ui.label("Attach residents").classes("rbs-font-semibold")
                resident_select = (
                    ui.select(
                        residents,
                        value=(list(initial.resident_ids) if initial is not None else []),
                        label="Residents",
                        multiple=True,
                        with_input=True,
                    )
                    .props("outlined options-dense use-chips")
                    .classes("w-full")
                )

        ui.separator()
        with ui.row().classes("w-full justify-end gap-3 p-4"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")

            def save_special() -> None:
                try:
                    selected_day = date.fromisoformat(str(start_date.value or ""))
                    selected_end = date.fromisoformat(str(end_date.value or ""))
                    session = (
                        None
                        if event_time is None or event_time.value == "full_day"
                        else Session(str(event_time.value))
                    )
                    special = SpecialRotation(
                        id=(
                            initial.id
                            if initial is not None
                            else next_special_rotation_id(instance)
                        ),
                        name=str(name.value or ""),
                        kind=kind,
                        start_date=selected_day,
                        end_date=selected_end,
                        session=session,
                        resident_ids=list(resident_select.value or []),
                    )
                    updated = (
                        replace_special_rotation(instance, initial.id, special)
                        if initial is not None
                        else add_special_rotation(instance, special)
                    )
                    dialog.close()
                    ui.notify(f"Saved {special.name}", type="positive")
                    on_save(updated, selected_rotation_id)
                except (TypeError, ValidationError, ValueError) as exc:
                    ui.notify(_validation_message(exc), type="negative", multi_line=True)

            ui.button("Save", icon="save", on_click=save_special).props("unelevated no-caps")
    dialog.open()
