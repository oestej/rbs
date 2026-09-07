"""Elective week-availability controls shared by rotation editors."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from functools import partial

from rbs.models.instance import SchedulerInput
from rbs.ui.editor_common import _academic_block_name


def _elective_availability_editor(
    instance: SchedulerInput,
    blackout_weeks: set[int],
) -> None:
    """Edit a mutable set of weeks when an elective cannot be placed."""
    from nicegui import ui

    calendar_weeks = instance.calendar.weeks
    valid_weeks = set(range(1, calendar_weeks + 1))
    blackout_weeks.intersection_update(valid_weeks)
    week_controls = {}

    def availability_label() -> str:
        unavailable = len(blackout_weeks)
        available = calendar_weeks - unavailable
        if unavailable == 0:
            return f"Available all {calendar_weeks} weeks"
        if available == 0:
            return "Unavailable all year"
        return f"{available} weeks available · {unavailable} blackout weeks"

    status = ui.label(availability_label()).classes("rbs-type-body rbs-font-semibold")

    def refresh_status() -> None:
        status.set_text(availability_label())

    def set_week(week: int, event) -> None:
        if event.value:
            blackout_weeks.discard(week)
        else:
            blackout_weeks.add(week)
        refresh_status()

    def set_weeks(weeks: Iterable[int], available: bool) -> None:
        selected = set(weeks)
        if available:
            blackout_weeks.difference_update(selected)
        else:
            blackout_weeks.update(selected)
        for week in selected:
            control = week_controls.get(week)
            if control is not None and bool(control.value) != available:
                control.set_value(available)
        refresh_status()

    with ui.row().classes("w-full items-center gap-2 flex-wrap"):
        ui.button(
            "Available all year",
            icon="event_available",
            on_click=partial(set_weeks, valid_weeks, True),
        ).props("outline dense no-caps")
        ui.button(
            "Black out all weeks",
            icon="event_busy",
            on_click=partial(set_weeks, valid_weeks, False),
        ).props("flat dense no-caps")
    ui.label(
        "Checked weeks are available. To offer only one block, black out all weeks, "
        "then make that block available."
    ).classes("rbs-type-caption rbs-text-muted")

    with ui.element("div").classes(
        "rbs-elective-availability-grid grid w-full grid-cols-1 gap-3 xl:grid-cols-2"
    ):
        for start_week in range(1, calendar_weeks + 1, 4):
            block_weeks = tuple(range(start_week, min(start_week + 4, calendar_weeks + 1)))
            block_name = _academic_block_name(start_week)
            start_date = instance.calendar.first_week_start + timedelta(weeks=start_week - 1)
            end_date = instance.calendar.first_week_start + timedelta(
                weeks=block_weeks[-1] - 1,
                days=6,
            )
            with ui.card().props("flat bordered").classes("w-full gap-3 p-4"):
                with ui.row().classes("w-full items-start justify-between gap-3"):
                    with ui.column().classes("min-w-0 gap-0"):
                        ui.label(block_name).classes("rbs-type-control-label")
                        ui.label(
                            f"{start_date:%b} {start_date.day}–"
                            f"{end_date:%b} {end_date.day}, {end_date.year}"
                        ).classes("rbs-type-caption rbs-text-muted")
                    with ui.row().classes("items-center gap-1"):
                        ui.button(
                            icon="done_all",
                            on_click=partial(set_weeks, block_weeks, True),
                        ).props(
                            "flat round dense "
                            f"aria-label='Make {block_name} available'"
                        ).tooltip(f"Make {block_name} available")
                        ui.button(
                            icon="block",
                            on_click=partial(set_weeks, block_weeks, False),
                        ).props(
                            "flat round dense "
                            f"aria-label='Black out {block_name}'"
                        ).tooltip(f"Black out {block_name}")
                with ui.element("div").classes(
                    "grid w-full grid-cols-1 gap-x-3 gap-y-1 sm:grid-cols-2"
                ):
                    for week in block_weeks:
                        monday = instance.calendar.first_week_start + timedelta(weeks=week - 1)
                        sunday = monday + timedelta(days=6)
                        checkbox = ui.checkbox(
                            f"Week {week} · {monday:%b} {monday.day}–"
                            f"{sunday:%b} {sunday.day}",
                            value=week not in blackout_weeks,
                        ).classes("w-full")
                        checkbox.on_value_change(partial(set_week, week))
                        week_controls[week] = checkbox


__all__ = ["_elective_availability_editor"]
