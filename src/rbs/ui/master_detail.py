"""Shared master-detail layout primitives for workspace editors."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from rbs.ui.buttons import PRIMARY_BUTTON_PROPS


@dataclass(frozen=True)
class DirectoryElements:
    search: object
    body: object


def page_header(
    title: str,
    *,
    count_label: str | None = None,
    subtitle: str | None,
    action_label: str | None = None,
    action_icon: str | None = None,
    on_action: Callable[[], None] | None = None,
) -> None:
    from nicegui import ui

    with ui.row().classes("w-full items-center justify-between gap-4"):
        with ui.column().classes("gap-0"):
            with ui.row().classes("items-baseline gap-3"):
                ui.label(title).classes("rbs-type-page-title")
                if count_label:
                    ui.badge(count_label).props("outline").classes("rbs-muted-badge")
            if subtitle:
                ui.label(subtitle).classes("rbs-text-muted")
        if action_label is not None:
            ui.button(
                action_label,
                icon=action_icon,
                on_click=on_action,
            ).props(PRIMARY_BUTTON_PROPS)


@contextmanager
def page() -> Iterator[None]:
    from nicegui import ui

    with ui.column().classes("rbs-master-page w-full gap-5"):
        yield


@contextmanager
def split(*, detail_selected: bool | None = None) -> Iterator[None]:
    from nicegui import ui

    state_class = (
        ""
        if detail_selected is None
        else " rbs-master-has-selection"
        if detail_selected
        else " rbs-master-no-selection"
    )
    with ui.row().classes(f"rbs-master-split{state_class} w-full items-stretch gap-5"):
        yield


def directory(
    title: str,
    *,
    search_label: str,
    search_placeholder: str,
    action_label: str | None = None,
    action_icon: str | None = None,
    on_action: Callable[[], None] | None = None,
) -> DirectoryElements:
    from nicegui import ui

    with (
        ui.card()
        .props("flat bordered")
        .classes("rbs-master-directory w-full lg:w-96 lg:shrink-0 p-0 gap-0")
    ):
        with ui.column().classes("w-full gap-3 p-4"):
            with ui.row().classes("w-full items-center justify-between gap-2"):
                ui.label(title).classes("rbs-type-section-title")
                if action_label is not None:
                    ui.button(
                        action_label,
                        icon=action_icon,
                        on_click=on_action,
                    ).props(PRIMARY_BUTTON_PROPS)
            search = (
                ui.input(
                    search_label,
                    placeholder=search_placeholder,
                )
                .props("outlined dense clearable")
                .classes("w-full")
            )
            with search.add_slot("prepend"):
                ui.icon("search")
        ui.separator()
        body = ui.column().classes("rbs-master-directory-body w-full gap-0")
    return DirectoryElements(search=search, body=body)


def directory_heading(label: str, count: int) -> None:
    from nicegui import ui

    with ui.row().classes(
        "rbs-master-directory-heading w-full items-center justify-between px-4 py-2"
    ):
        ui.label(label).classes("rbs-type-caption uppercase rbs-font-semibold rbs-text-muted")
        ui.label(str(count)).classes("rbs-type-caption rbs-text-muted")


def empty_directory(*, icon: str, title: str, description: str) -> None:
    from nicegui import ui

    with ui.column().classes("w-full items-center gap-1 p-6"):
        ui.icon(icon).classes("rbs-icon-lg rbs-text-disabled")
        ui.label(title).classes("rbs-font-semibold")
        ui.label(description).classes("rbs-type-caption rbs-text-muted")


def detail_panel():
    from nicegui import ui

    return ui.column().classes("rbs-master-detail-panel w-full min-w-0 flex-1")


@contextmanager
def detail_card() -> Iterator[None]:
    from nicegui import ui

    with ui.card().props("flat bordered").classes("rbs-master-detail w-full p-0 gap-0"):
        yield


def empty_detail(*, icon: str, title: str, description: str) -> None:
    from nicegui import ui

    with (
        ui.card()
        .props("flat bordered")
        .classes("rbs-master-detail w-full min-h-72 items-center justify-center p-8")
    ):
        ui.icon(icon).classes("rbs-icon-xl rbs-text-disabled")
        ui.label(title).classes("rbs-type-dialog-title")
        ui.label(description).classes("rbs-text-muted text-center")


def selected_class(selected: bool) -> str:
    return "rbs-master-selected" if selected else ""
