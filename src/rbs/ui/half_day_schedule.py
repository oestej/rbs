"""Shared interactive AM/PM week-grid primitives."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from rbs.models.enums import Session, Weekday

_HALF_DAY_DRAG_START_JS = """
(event) => {
  if (event.currentTarget.getAttribute('draggable') !== 'true') {
    event.preventDefault();
    return;
  }
  const payload = JSON.stringify({
    scope: event.currentTarget.dataset.scope || '',
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
_HALF_DAY_DRAG_END_JS = """
(event) => {
  event.currentTarget.classList.remove('is-dragging');
  document.querySelectorAll('.rbs-resident-clinic-session-cell.is-drag-over')
    .forEach((cell) => cell.classList.remove('is-drag-over'));
}
"""
_HALF_DAY_DRAG_OVER_JS = """
(event) => {
  const types = Array.from(event.dataTransfer.types || []);
  if (!types.includes('application/x-rbs-clinic-slot') && !types.includes('text/plain')) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = 'move';
  event.currentTarget.classList.add('is-drag-over');
}
"""
_HALF_DAY_DRAG_LEAVE_JS = """
(event) => {
  if (!event.currentTarget.contains(event.relatedTarget)) {
    event.currentTarget.classList.remove('is-drag-over');
  }
}
"""
_HALF_DAY_DROP_JS = """
(event) => {
  event.preventDefault();
  event.currentTarget.classList.remove('is-drag-over');
  const payload = event.dataTransfer.getData('application/x-rbs-clinic-slot')
    || event.dataTransfer.getData('text/plain');
  if (!payload) return;
  try {
    emit(JSON.parse(payload));
  } catch (_error) {
    // Ignore drops which did not originate from an RBS half-day block.
  }
}
"""


def render_half_day_grid(
    weekdays: tuple[Weekday, ...],
    *,
    render_cell: Callable[[Weekday, Session], None],
    day_details: Mapping[Weekday, str] | None = None,
) -> None:
    """Render the shared resident-style week grid around caller-owned cells."""
    from nicegui import ui

    details = day_details or {}
    with (
        ui.element("div")
        .classes("rbs-resident-clinic-week-grid w-full")
        .style(f"--rbs-resident-clinic-days: {len(weekdays)}")
    ):
        ui.element("div").classes("rbs-resident-clinic-grid-corner")
        for weekday in weekdays:
            with ui.element("div").classes("rbs-resident-clinic-day-header"):
                ui.label(weekday.value[:3]).classes("rbs-resident-clinic-day-name")
                detail = details.get(weekday)
                if detail:
                    ui.label(detail).classes("rbs-resident-clinic-day-date")
        for session in Session:
            ui.label("AM" if session is Session.MORNING else "PM").classes(
                "rbs-resident-clinic-session-label"
            )
            for weekday in weekdays:
                render_cell(weekday, session)


def create_half_day_cell(
    *,
    classes: str = "",
    on_drop: Callable[[Any], None] | None = None,
    on_click: Callable[[], None] | None = None,
    accessible_name: str | None = None,
):
    """Create one shared schedule cell with optional drop and click behavior."""
    from nicegui import ui

    cell = ui.element("div").classes(
        f"rbs-resident-clinic-session-cell {classes}".strip()
    )
    if on_drop is not None:
        bind_half_day_drop(cell, on_drop)
    if on_click is not None:
        if accessible_name:
            cell.props(f"role=button tabindex=0 aria-label='{accessible_name}'")
        else:
            cell.props("role=button tabindex=0")
        cell.on("click", on_click)
        cell.on("keydown.enter", on_click)
    return cell


def bind_half_day_drop(cell: Any, on_drop: Callable[[Any], None]) -> None:
    """Give a shared schedule cell the resident scheduler's drop behavior."""
    cell.on("dragover", js_handler=_HALF_DAY_DRAG_OVER_JS)
    cell.on("dragleave", js_handler=_HALF_DAY_DRAG_LEAVE_JS)
    cell.on("drop", on_drop, js_handler=_HALF_DAY_DROP_JS)


def create_draggable_half_day_event(
    *,
    classes: str,
    draggable: bool,
    week: int,
    weekday: Weekday,
    session: Session,
    scope: str = "",
    on_click: Callable[[], None] | None = None,
    accessible_name: str | None = None,
):
    """Create the same draggable event block used by resident clinic schedules."""
    from nicegui import ui

    event = (
        ui.element("div")
        .classes(f"rbs-resident-clinic-event {classes}".strip())
        .props(
            f"draggable={'true' if draggable else 'false'} data-scope={scope} "
            f"data-week={week} data-weekday={weekday.value} "
            f"data-session={session.value}"
        )
    )
    if draggable:
        event.on("dragstart", js_handler=_HALF_DAY_DRAG_START_JS)
        event.on("dragend", js_handler=_HALF_DAY_DRAG_END_JS)
    if on_click is not None:
        if accessible_name:
            event.props(f"role=button tabindex=0 aria-label='{accessible_name}'")
        else:
            event.props("role=button tabindex=0")
        event.on("click", on_click)
        event.on("keydown.enter", on_click)
    return event
