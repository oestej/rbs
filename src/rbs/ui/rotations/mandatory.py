"""Mandatory rotation detail panel; creation shares the full-screen editor."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from pydantic import ValidationError

from rbs.models.instance import SchedulerInput
from rbs.models.rotation import Rotation
from rbs.ui import master_detail
from rbs.ui.buttons import (
    ICON_BUTTON_PROPS,
    PRIMARY_BUTTON_PROPS,
    button_props,
)
from rbs.ui.editor_common import _validation_message
from rbs.ui.rotations.forms import (
    RotationEditorGuard,
    _rotation_detail_contents,
    _rotation_editor,
)
from rbs.ui.rotations.ops import remove_mandatory_rotation
from rbs.ui.rotations.summary import _rotation_identity
from rbs.ui.rotations.types import (
    SaveRotation,
    SelectRotation,
)


def _rotation_detail_panel(
    instance: SchedulerInput,
    *,
    rotation: Rotation | None,
    creating: bool,
    missing_id: str | None,
    on_select: SelectRotation,
    on_save: SaveRotation,
    guard: RotationEditorGuard | None = None,
) -> None:
    editing = False
    panel = master_detail.detail_panel()

    def render_panel() -> None:
        nonlocal editing
        if guard is not None:
            guard.clear()
        panel.clear()
        with panel:
            if creating:
                _rotation_editor(
                    instance,
                    None,
                    on_cancel=partial(on_select, None),
                    on_save=on_save,
                    guard=guard,
                )
            elif rotation is not None and editing:

                def stop_editing() -> None:
                    nonlocal editing
                    editing = False
                    render_panel()

                _rotation_editor(
                    instance,
                    rotation,
                    on_cancel=stop_editing,
                    on_save=on_save,
                    guard=guard,
                )
            elif rotation is not None:

                def start_editing() -> None:
                    nonlocal editing
                    editing = True
                    render_panel()

                _rotation_view(
                    instance,
                    rotation,
                    on_edit=start_editing,
                    on_select=on_select,
                    on_save=on_save,
                )
            else:
                master_detail.empty_detail(
                    icon="tune",
                    title="Rotation not found" if missing_id else "Select a rotation",
                    description=(
                        "Choose a rotation from the searchable directory to view its rules."
                    ),
                )

    render_panel()


def _rotation_view(
    instance: SchedulerInput,
    rotation: Rotation,
    *,
    on_edit: Callable[[], None],
    on_select: SelectRotation,
    on_save: SaveRotation,
) -> None:
    from nicegui import ui

    with master_detail.detail_card():
        with ui.row().classes(
            "rbs-rotation-detail-header w-full items-center justify-between gap-4 p-5"
        ):
            _rotation_identity(rotation, instance=instance)
            with ui.row().classes("items-center gap-1"):
                ui.button("Edit", icon="edit", on_click=on_edit).props(PRIMARY_BUTTON_PROPS)
                with ui.button(icon="more_vert").props(
                    button_props(
                        ICON_BUTTON_PROPS,
                        f"aria-label='More actions for {rotation.name}'",
                    )
                ):
                    ui.tooltip(f"More actions for {rotation.name}")
                    with ui.menu():
                        ui.menu_item(
                            "Remove rotation",
                            on_click=partial(
                                _confirm_remove_mandatory_rotation,
                                instance,
                                rotation,
                                on_save=on_save,
                            ),
                        ).classes("rbs-text-danger")
                with ui.button(
                    icon="arrow_back",
                    on_click=partial(on_select, None),
                ).props(
                    button_props(
                        ICON_BUTTON_PROPS,
                        "aria-label='Back to rotation directory'",
                    )
                ):
                    ui.tooltip("Back to rotation directory")
        ui.separator()
        _rotation_detail_contents(instance, rotation)


def _confirm_remove_mandatory_rotation(
    instance: SchedulerInput,
    rotation: Rotation,
    *,
    on_save: SaveRotation,
) -> None:
    from nicegui import ui

    required_weeks = sum(
        block.duration_weeks * block.count
        for curriculum in instance.requirements
        for block in curriculum.blocks
        if block.rotation_id == rotation.id
    )
    with ui.dialog() as dialog, ui.card().classes("w-[min(92vw,520px)] p-5"):
        ui.label(f"Remove {rotation.name}?").classes("rbs-type-dialog-title")
        description = (
            f"Its {required_weeks} required curriculum weeks will return to Elective time. "
            if required_weeks
            else "Its rotation definition will be removed. "
        )
        ui.label(
            description
            + "Related locks, resident overrides, prerequisites, and choice options will "
            "also be removed."
        ).classes("rbs-type-body rbs-text-muted")
        with ui.row().classes("w-full justify-end gap-3 pt-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")

            def remove() -> None:
                try:
                    updated = remove_mandatory_rotation(instance, rotation.id)
                    dialog.close()
                    ui.notify(f"Removed {rotation.name}", type="positive")
                    on_save(updated, None)
                except (ValidationError, ValueError) as exc:
                    ui.notify(_validation_message(exc), type="negative", multi_line=True)

            ui.button(
                "Remove rotation",
                icon="delete_outline",
                on_click=remove,
            ).props("unelevated no-caps color=negative")
    dialog.open()
