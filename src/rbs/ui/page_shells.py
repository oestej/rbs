"""Application-level page shells and toolbar rhythm."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager


def page_header(title: str, *, subtitle: str | None = None) -> None:
    """Render the shared title block used by every full application page."""
    from nicegui import ui

    with ui.column().classes("rbs-page-shell-heading min-w-0 gap-0"):
        ui.label(title).classes("rbs-type-page-title")
        if subtitle:
            ui.label(subtitle).classes("rbs-type-body rbs-text-muted")


@contextmanager
def schedule_canvas(title: str, *, subtitle: str | None = None) -> Iterator[None]:
    """Wide shell for read-mostly schedules and their compact toolbars."""
    from nicegui import ui

    with ui.column().classes("rbs-page-shell rbs-schedule-canvas w-full min-w-0 gap-4"):
        page_header(title, subtitle=subtitle)
        yield


@contextmanager
def master_detail(title: str, *, subtitle: str | None = None) -> Iterator[None]:
    """Centered shell for a searchable directory and focused detail view."""
    from nicegui import ui

    with ui.column().classes("rbs-page-shell rbs-master-page w-full max-w-7xl mx-auto gap-5"):
        page_header(title, subtitle=subtitle)
        yield


@contextmanager
def configuration(
    title: str,
    *,
    subtitle: str | None = None,
    max_width: str = "max-w-7xl",
) -> Iterator[None]:
    """Centered shell for settings and other tabbed configuration pages."""
    from nicegui import ui

    with ui.column().classes(
        f"rbs-page-shell rbs-configuration-page w-full {max_width} mx-auto gap-4"
    ):
        page_header(title, subtitle=subtitle)
        yield


@contextmanager
def toolbar(*, extra_classes: str = "") -> Iterator[None]:
    """Place context on the left and filters before actions on the right."""
    from nicegui import ui

    with ui.row().classes(
        f"rbs-page-toolbar w-full min-w-0 items-center justify-between gap-3 {extra_classes}"
    ):
        yield


@contextmanager
def toolbar_actions(*, extra_classes: str = "") -> Iterator[None]:
    """Right-aligned, wrapping toolbar controls in filter-then-action order."""
    from nicegui import ui

    with ui.row().classes(f"rbs-page-toolbar-actions items-center gap-2 {extra_classes}"):
        yield
