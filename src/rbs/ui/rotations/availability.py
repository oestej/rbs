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
    block_controls = {}

    def availability_label() -> str:
        unavailable = len(blackout_weeks)
        available = calendar_weeks - unavailable
        if unavailable == 0:
            return f"Available all {calendar_weeks} weeks"
        if available == 0:
            return "Unavailable all year"
        blackout_label = "week" if unavailable == 1 else "weeks"
        return f"{available} weeks available · {unavailable} blackout {blackout_label}"

    def block_label(block_name: str, weeks: tuple[int, ...]) -> str:
        available = sum(week not in blackout_weeks for week in weeks)
        if available == len(weeks):
            availability = f"All {len(weeks)} weeks available"
        elif available == 0:
            availability = "Unavailable"
        else:
            availability = f"{available} of {len(weeks)} weeks available"
        return f"{block_name} · {availability}"

    def refresh_status(changed_weeks: Iterable[int] | None = None) -> None:
        status.set_text(availability_label())
        starts = (
            range(1, calendar_weeks + 1, 4)
            if changed_weeks is None
            else {((week - 1) // 4) * 4 + 1 for week in changed_weeks}
        )
        for start_week in starts:
            expansion = block_controls.get(start_week)
            if expansion is None:
                continue
            weeks = tuple(range(start_week, min(start_week + 4, calendar_weeks + 1)))
            expansion.set_text(block_label(_academic_block_name(start_week), weeks))

    def set_week(week: int, event) -> None:
        if event.value:
            blackout_weeks.discard(week)
        else:
            blackout_weeks.add(week)
        refresh_status((week,))

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
        refresh_status(selected)

    with ui.row().classes(
        "rbs-elective-availability-toolbar w-full items-center justify-between gap-3 flex-wrap"
    ):
        status = ui.label(availability_label()).classes("rbs-type-body rbs-font-semibold")
        with ui.row().classes("items-center gap-2 flex-wrap"):
            ui.button(
                "Available all year",
                on_click=partial(set_weeks, valid_weeks, True),
            ).props("outline dense no-caps")
            ui.button(
                "Black out all weeks",
                on_click=partial(set_weeks, valid_weeks, False),
            ).props("flat dense no-caps")
    ui.label(
        "Open a block to change individual weeks. Checked weeks are available."
    ).classes("rbs-type-caption rbs-text-muted")

    with ui.element("div").classes("rbs-elective-availability-grid w-full"):
        for start_week in range(1, calendar_weeks + 1, 4):
            block_weeks = tuple(range(start_week, min(start_week + 4, calendar_weeks + 1)))
            block_name = _academic_block_name(start_week)
            start_date = instance.calendar.first_week_start + timedelta(weeks=start_week - 1)
            end_date = instance.calendar.first_week_start + timedelta(
                weeks=block_weeks[-1] - 1,
                days=6,
            )
            expansion = ui.expansion(
                block_label(block_name, block_weeks),
                caption=(
                    f"{start_date:%b} {start_date.day}–"
                    f"{end_date:%b} {end_date.day}, {end_date.year}"
                ),
                value=False,
            ).classes("rbs-elective-availability-block w-full")
            block_controls[start_week] = expansion
            with expansion:
                with ui.column().classes("w-full gap-3 px-4 pb-4"):
                    with ui.row().classes("w-full items-center justify-end gap-2 flex-wrap"):
                        ui.button(
                            "All available",
                            icon="done_all",
                            on_click=partial(set_weeks, block_weeks, True),
                        ).props(
                            "flat dense no-caps "
                            f"aria-label='Make {block_name} available'"
                        )
                        ui.button(
                            "Black out block",
                            icon="block",
                            on_click=partial(set_weeks, block_weeks, False),
                        ).props(
                            "flat dense no-caps "
                            f"aria-label='Black out {block_name}'"
                        )
                    with ui.element("div").classes("rbs-elective-availability-weeks w-full"):
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
