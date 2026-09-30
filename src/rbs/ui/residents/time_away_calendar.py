"""Shared calendar picker for vacation Mondays and individual days off."""

from __future__ import annotations

import calendar
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from functools import partial
from typing import TYPE_CHECKING

from rbs.models.instance import SchedulerInput
from rbs.ui.residents.ops import (
    day_off_is_selectable,
    vacation_monday,
    vacation_monday_is_selectable,
    vacation_month_dates,
)

if TYPE_CHECKING:
    from nicegui.elements.input import Input


@dataclass(frozen=True)
class TimeAwayCalendar:
    selected_date: Input
    reset: Callable[[], None]


def time_away_calendar(instance: SchedulerInput, *, vacation: bool) -> TimeAwayCalendar:
    """Render shared navigation while retaining each time-away selection policy."""
    from nicegui import ui

    first_day = instance.calendar.first_week_start
    last_day = vacation_monday(instance, instance.calendar.weeks) + timedelta(days=6)
    first_month = first_day.replace(day=1)
    last_month = last_day.replace(day=1)
    visible_month = first_month
    highlighted: date | None = None
    is_selectable = vacation_monday_is_selectable if vacation else day_off_is_selectable

    with (
        ui.input(
            "Add vacation Monday" if vacation else "Add day off",
            placeholder="Choose a Monday" if vacation else "Choose a date",
        )
        .props("outlined readonly")
        .classes("min-w-64 flex-1") as selected_date
    ):
        with ui.menu() as calendar_menu:
            container = ui.column().classes("rbs-vacation-calendar gap-0")

            def select_day(selected: date) -> None:
                nonlocal highlighted
                highlighted = selected
                selected_date.set_value(selected.isoformat())
                render_calendar()

            def move_month(offset: int) -> None:
                nonlocal visible_month
                month_index = visible_month.year * 12 + visible_month.month - 1 + offset
                visible_month = date(month_index // 12, month_index % 12 + 1, 1)
                render_calendar()

            def render_calendar() -> None:
                container.clear()
                with container:
                    with ui.row().classes(
                        "rbs-vacation-calendar-nav w-full items-center justify-between"
                    ):
                        previous = ui.button(
                            icon="chevron_left", on_click=partial(move_month, -1),
                        ).props("flat round dense aria-label='Previous month'")
                        if visible_month <= first_month:
                            previous.props("disable")
                        ui.label(
                            f"{calendar.month_name[visible_month.month]} {visible_month.year}"
                        ).classes("rbs-font-semibold")
                        following = ui.button(
                            icon="chevron_right", on_click=partial(move_month, 1),
                        ).props("flat round dense aria-label='Next month'")
                        if visible_month >= last_month:
                            following.props("disable")

                    with ui.element("div").classes("rbs-vacation-calendar-grid"):
                        for weekday in ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"):
                            ui.label(weekday).classes("rbs-vacation-calendar-weekday")
                        for day in vacation_month_dates(visible_month.year, visible_month.month):
                            classes = ["rbs-vacation-calendar-day"]
                            if day.month != visible_month.month:
                                classes.append("is-outside-month")
                            if (
                                vacation
                                and highlighted is not None
                                and highlighted <= day <= highlighted + timedelta(days=6)
                            ):
                                classes.append("is-selected-week")
                            selectable = is_selectable(instance, day)
                            if selectable:
                                classes.append("is-selectable")
                            if day == highlighted:
                                classes.append(
                                    "is-selected-monday" if vacation else "is-selected-day"
                                )
                            with ui.element("div").classes(" ".join(classes)):
                                if selectable:
                                    choice = "Monday" if vacation else "day off"
                                    ui.button(
                                        str(day.day), on_click=partial(select_day, day),
                                    ).props(
                                        "flat dense round "
                                        f"aria-label='Choose {choice} {day:%b %d, %Y}'"
                                    ).classes(
                                        "rbs-vacation-calendar-monday"
                                        if vacation else "rbs-day-off-calendar-date"
                                    )
                                else:
                                    ui.label(str(day.day))

            render_calendar()
            with ui.row().classes("w-full justify-end p-2"):
                ui.button("Close", on_click=calendar_menu.close).props("flat dense")
        with selected_date.add_slot("append"):
            ui.icon("event").classes("cursor-pointer").on("click", calendar_menu.open)
    selected_date.on("click", calendar_menu.open)

    def reset() -> None:
        nonlocal highlighted
        selected_date.set_value(None)
        highlighted = None
        calendar_menu.close()
        render_calendar()

    return TimeAwayCalendar(selected_date=selected_date, reset=reset)
