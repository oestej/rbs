"""Academic half-day configuration."""

from __future__ import annotations

from datetime import timedelta
from functools import partial

from pydantic import ValidationError

from rbs.models.enums import Session, Weekday
from rbs.models.instance import SchedulerInput
from rbs.ui import master_detail
from rbs.ui.buttons import (
    PRIMARY_BUTTON_PROPS,
)
from rbs.ui.clinic.ops import (
    remove_academic_half_day_override,
    replace_academic_half_day,
    set_academic_half_day_override,
)
from rbs.ui.editor_common import (
    _CLINIC_WEEK,
    _SESSION_OPTIONS,
    _WEEKDAY_OPTIONS,
    _validation_message,
)
from rbs.ui.rotations.types import (
    SaveRotation,
)


def _academic_configuration(
    instance: SchedulerInput,
    *,
    selected_rotation_id: str | None,
    on_save: SaveRotation,
) -> None:
    from nicegui import ui

    academic = instance.clinic_policy.academic
    with master_detail.detail_card():
        with ui.column().classes("w-full gap-4 p-5"):
            ui.label("Default academic half-day").classes("rbs-type-section-title")
            with ui.row().classes("w-full items-end gap-3 flex-wrap"):
                weekday = (
                    ui.select(
                        _WEEKDAY_OPTIONS,
                        value=academic.weekday.value,
                        label="Day",
                    )
                    .props("outlined options-dense")
                    .classes("w-full sm:w-56")
                )
                session = (
                    ui.select(
                        _SESSION_OPTIONS,
                        value=academic.session.value,
                        label="Time",
                    )
                    .props("outlined options-dense")
                    .classes("w-full sm:w-56")
                )

                def save() -> None:
                    try:
                        updated = replace_academic_half_day(
                            instance,
                            Weekday(str(weekday.value)),
                            Session(str(session.value)),
                        )
                        ui.notify("Academic half-day saved", type="positive")
                        on_save(updated, selected_rotation_id)
                    except (ValidationError, ValueError) as exc:
                        ui.notify(
                            _validation_message(exc),
                            type="negative",
                            multi_line=True,
                        )

                ui.button(
                    "Save default",
                    icon="save",
                    on_click=save,
                ).props(PRIMARY_BUTTON_PROPS)

            ui.separator()

            ui.label("Academic half-day — specific-date overrides").classes(
                "rbs-type-section-title"
            )

            recurring_weekday = academic.weekday
            default_override_weekday = next(
                day for day in _CLINIC_WEEK if day is not recurring_weekday
            )
            overrides_by_week = {
                override.week: override for override in instance.academic_half_day_overrides
            }
            with ui.row().classes("w-full items-end gap-3 flex-wrap"):
                override_week = (
                    ui.select(
                        _academic_week_options(instance),
                        value=None,
                        label="Week",
                    )
                    .props("outlined options-dense clearable")
                    .classes("w-full sm:w-80")
                )
                override_weekday = (
                    ui.select(
                        _WEEKDAY_OPTIONS,
                        value=default_override_weekday.value,
                        label="Day",
                    )
                    .props("outlined options-dense")
                    .classes("w-full sm:w-52")
                )
                override_session = (
                    ui.select(
                        _SESSION_OPTIONS,
                        value=academic.session.value,
                        label="Time",
                    )
                    .props("outlined options-dense")
                    .classes("w-full sm:w-52")
                )

                def save_override() -> None:
                    try:
                        if override_week.value is None:
                            raise ValueError("select a week")
                        updated = set_academic_half_day_override(
                            instance,
                            int(override_week.value),
                            Weekday(str(override_weekday.value)),
                            Session(str(override_session.value)),
                        )
                        ui.notify("Academic override saved", type="positive")
                        on_save(updated, selected_rotation_id)
                    except (ValidationError, ValueError) as exc:
                        ui.notify(
                            _validation_message(exc),
                            type="negative",
                            multi_line=True,
                        )

                ui.button(
                    "Save override",
                    icon="event_repeat",
                    on_click=save_override,
                ).props(PRIMARY_BUTTON_PROPS)

            def load_override_values(week: int) -> None:
                existing = overrides_by_week.get(week)
                if existing is None:
                    return
                override_weekday.value = existing.weekday.value
                override_session.value = existing.session.value

            def load_selected_override(event) -> None:
                if event.value is not None:
                    load_override_values(int(event.value))

            def edit_override(week: int) -> None:
                override_week.value = week
                load_override_values(week)

            override_week.on_value_change(load_selected_override)

            with ui.column().classes("w-full gap-2"):
                if not instance.academic_half_day_overrides:
                    with ui.row().classes(
                        "rbs-academic-override-empty w-full items-center gap-3 rounded px-4 py-3"
                    ):
                        ui.icon("event_available").classes("rbs-text-subtle")
                        ui.label("No overrides.").classes("rbs-text-muted")
                else:

                    def delete_override(week: int) -> None:
                        try:
                            updated = remove_academic_half_day_override(instance, week)
                            ui.notify("Academic override removed", type="positive")
                            on_save(updated, selected_rotation_id)
                        except (ValidationError, ValueError) as exc:
                            ui.notify(
                                _validation_message(exc),
                                type="negative",
                                multi_line=True,
                            )

                    for override in instance.academic_half_day_overrides:
                        with ui.row().classes(
                            "rbs-academic-override-row w-full items-center gap-3 rounded px-3 py-2"
                        ):
                            ui.icon("event_repeat").classes("rbs-text-primary")
                            with ui.column().classes("min-w-0 flex-1 gap-0"):
                                ui.label(_academic_week_label(instance, override.week)).classes(
                                    "rbs-font-semibold"
                                )
                                ui.label(
                                    f"{override.weekday.value.title()} · "
                                    f"{_SESSION_OPTIONS[override.session.value]}"
                                ).classes("rbs-type-caption rbs-text-muted")
                            ui.button(
                                icon="edit",
                                on_click=partial(edit_override, override.week),
                            ).props(
                                f"flat round dense aria-label='Edit week {override.week} override'"
                            )
                            ui.button(
                                icon="delete_outline",
                                on_click=partial(delete_override, override.week),
                            ).props(
                                "flat round dense color=negative "
                                f"aria-label='Delete week {override.week} override'"
                            )


def _academic_week_options(instance: SchedulerInput) -> dict[int, str]:
    return {
        week: _academic_week_label(instance, week) for week in range(1, instance.calendar.weeks + 1)
    }


def _academic_week_label(instance: SchedulerInput, week: int) -> str:
    monday = instance.calendar.first_week_start + timedelta(weeks=week - 1)
    sunday = monday + timedelta(days=6)
    return f"Week {week} · {monday:%b} {monday.day}–{sunday:%b} {sunday.day}, {sunday.year}"
