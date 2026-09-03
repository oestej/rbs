"""Workspace training-level configuration."""

from __future__ import annotations

from collections.abc import Callable

from pydantic import ValidationError

from rbs.models.instance import SchedulerInput
from rbs.models.workspace import Workspace
from rbs.training_levels import (
    add_training_level,
    remove_training_level,
    reorder_training_levels,
    update_training_level,
)
from rbs.ui.buttons import (
    DESTRUCTIVE_BUTTON_PROPS,
    DESTRUCTIVE_ICON_BUTTON_PROPS,
    PRIMARY_BUTTON_PROPS,
    SECONDARY_BUTTON_PROPS,
    TERTIARY_BUTTON_PROPS,
    button_props,
)

PersistInstance = Callable[..., None]

_TRAINING_LEVEL_DRAG_START_JS = """
(event) => {
  const value = event.currentTarget.dataset.trainingLevel;
  if (!value) {
    event.preventDefault();
    return;
  }
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('application/x-rbs-training-level', value);
  event.dataTransfer.setData('text/plain', value);
  event.currentTarget.closest('.rbs-training-level-card')?.classList.add('is-dragging');
}
"""
_TRAINING_LEVEL_DRAG_END_JS = """
() => {
  document.querySelectorAll(
    '.rbs-training-level-card.is-dragging, '
    + '.rbs-training-level-card.is-drag-over-before, '
    + '.rbs-training-level-card.is-drag-over-after'
  ).forEach((card) => card.classList.remove(
    'is-dragging', 'is-drag-over-before', 'is-drag-over-after'
  ));
}
"""
_TRAINING_LEVEL_DRAG_OVER_JS = """
(event) => {
  const types = Array.from(event.dataTransfer.types || []);
  if (!types.includes('application/x-rbs-training-level')) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = 'move';
  const rect = event.currentTarget.getBoundingClientRect();
  const before = event.clientY < rect.top + rect.height / 2;
  event.currentTarget.classList.toggle('is-drag-over-before', before);
  event.currentTarget.classList.toggle('is-drag-over-after', !before);
}
"""
_TRAINING_LEVEL_DRAG_LEAVE_JS = """
(event) => {
  if (!event.currentTarget.contains(event.relatedTarget)) {
    event.currentTarget.classList.remove('is-drag-over-before', 'is-drag-over-after');
  }
}
"""
_TRAINING_LEVEL_DROP_JS = """
(event) => {
  event.preventDefault();
  const card = event.currentTarget;
  const value = event.dataTransfer.getData('application/x-rbs-training-level');
  const source = Number(value);
  const target = Number(card.dataset.trainingLevel);
  const rect = card.getBoundingClientRect();
  const position = event.clientY < rect.top + rect.height / 2 ? 'before' : 'after';
  card.classList.remove('is-drag-over-before', 'is-drag-over-after');
  if (Number.isInteger(source) && Number.isInteger(target)) {
    emit({source, target, position});
  }
}
"""


def training_level_settings(
    workspace: Workspace,
    persist_instance: PersistInstance,
    *,
    schedule_is_current: bool,
) -> None:
    """Render stable short codes and descriptive names for program tracks."""
    from nicegui import ui

    instance = workspace.instance

    with ui.column().classes("rbs-training-level-settings w-full gap-4"):
        with ui.column().classes("gap-1"):
            ui.label("Training levels and tracks").classes("rbs-type-section-title")
            ui.label(
                "Configure every year or track scheduled by this workspace. Short codes "
                "appear in compact schedule cells; full names appear in descriptive views."
            ).classes("rbs-type-body rbs-text-muted")
        with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
            ui.label(
                "Drag levels into the order used throughout the app. New levels start "
                "empty so you can configure only the curriculum and rules that apply."
            ).classes("rbs-type-caption rbs-text-muted flex-1")
            ui.button(
                "Add training level",
                icon="add",
                on_click=lambda: _open_add_dialog(
                    instance,
                    persist_instance,
                    schedule_is_current=schedule_is_current,
                ),
            ).props(PRIMARY_BUTTON_PROPS)

        for curriculum in instance.requirements:
            resident_count = sum(resident.pgy == curriculum.pgy for resident in instance.residents)
            card = (
                ui.card()
                .props(f"flat bordered data-training-level={curriculum.pgy}")
                .classes("rbs-training-level-card w-full p-0")
            )

            def reorder_level(event) -> None:
                try:
                    payload = event.args if isinstance(event.args, dict) else {}
                    source = int(payload.get("source"))
                    target = int(payload.get("target"))
                    position = str(payload.get("position"))
                    ordered_ids = list(instance.training_level_ids)
                    if source == target:
                        return
                    if source not in ordered_ids or target not in ordered_ids:
                        raise ValueError("dragged training level is no longer available")
                    if position not in {"before", "after"}:
                        raise ValueError("training-level drop position is invalid")
                    ordered_ids.remove(source)
                    target_index = ordered_ids.index(target)
                    if position == "after":
                        target_index += 1
                    ordered_ids.insert(target_index, source)
                    updated = reorder_training_levels(instance, ordered_ids)
                    if updated == instance:
                        return
                    ui.notify("Training level order saved", type="positive")
                    persist_instance(
                        updated,
                        preserve_schedule=schedule_is_current,
                    )
                except (TypeError, ValidationError, ValueError) as exc:
                    ui.notify(str(exc), type="negative", multi_line=True)

            card.on("dragover", js_handler=_TRAINING_LEVEL_DRAG_OVER_JS)
            card.on("dragleave", js_handler=_TRAINING_LEVEL_DRAG_LEAVE_JS)
            card.on("drop", reorder_level, js_handler=_TRAINING_LEVEL_DROP_JS)
            with card:
                with ui.column().classes("w-full gap-4 p-5"):
                    with ui.row().classes(
                        "rbs-training-level-header w-full items-center gap-3 no-wrap"
                    ):
                        drag_handle = (
                            ui.button(icon="drag_indicator")
                            .props(
                                "flat round dense draggable=true "
                                f"data-training-level={curriculum.pgy} "
                                f"aria-label='Drag {curriculum.short_code} to reorder'"
                            )
                            .classes("rbs-training-level-drag-handle")
                        )
                        drag_handle.on(
                            "dragstart",
                            js_handler=_TRAINING_LEVEL_DRAG_START_JS,
                        )
                        drag_handle.on(
                            "dragend",
                            js_handler=_TRAINING_LEVEL_DRAG_END_JS,
                        )
                        ui.badge(
                            curriculum.short_code,
                            color="primary",
                        ).props("outline").classes("rbs-training-level-code-badge")
                        with ui.column().classes("gap-0 min-w-0 flex-1"):
                            ui.label(curriculum.display_label).classes(
                                "rbs-training-level-name rbs-type-section-title"
                            )
                            ui.label(
                                f"{resident_count} resident"
                                f"{'s' if resident_count != 1 else ''} · "
                                f"{curriculum.required_weeks()} curriculum weeks"
                            ).classes("rbs-type-caption rbs-text-muted")

                    with ui.element("div").classes("rbs-training-level-editor-grid w-full"):
                        code_input = (
                            ui.input("Short code", value=curriculum.short_code)
                            .props("outlined maxlength=5")
                            .classes("rbs-training-level-code-input w-full")
                        )
                        name_input = (
                            ui.input("Full name", value=curriculum.display_label)
                            .props("outlined maxlength=80")
                            .classes("rbs-training-level-name-input w-full")
                        )

                        def save_level(
                            *,
                            pgy: int = curriculum.pgy,
                            code=code_input,
                            name=name_input,
                        ) -> None:
                            try:
                                updated = update_training_level(
                                    instance,
                                    pgy,
                                    str(code.value or ""),
                                    str(name.value or ""),
                                )
                                ui.notify("Training level saved", type="positive")
                                persist_instance(
                                    updated,
                                    preserve_schedule=schedule_is_current,
                                )
                            except (ValidationError, ValueError) as exc:
                                ui.notify(str(exc), type="negative", multi_line=True)

                        with ui.row().classes(
                            "rbs-training-level-actions items-center gap-2 no-wrap"
                        ):
                            ui.button(
                                "Save",
                                icon="save",
                                on_click=save_level,
                            ).props(SECONDARY_BUTTON_PROPS).classes("rbs-training-level-save")
                            delete_label = f"Delete {curriculum.display_label}"
                            delete = ui.button(
                                icon="delete_outline",
                                on_click=lambda pgy=curriculum.pgy: _open_remove_dialog(
                                    instance,
                                    pgy,
                                    persist_instance,
                                    schedule_is_current=schedule_is_current,
                                ),
                            ).props(
                                button_props(
                                    DESTRUCTIVE_ICON_BUTTON_PROPS,
                                    f"aria-label='{delete_label}'",
                                )
                            )
                        delete.set_enabled(len(instance.requirements) > 1 and resident_count == 0)
                        if resident_count:
                            delete.tooltip("Move or remove assigned residents first")
                        else:
                            delete.tooltip(delete_label)


def _open_add_dialog(
    instance: SchedulerInput,
    persist_instance: PersistInstance,
    *,
    schedule_is_current: bool,
) -> None:
    from nicegui import ui

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-xl gap-4"):
        ui.label("Add training level").classes("rbs-type-dialog-title")
        ui.label(
            "Use a short code in schedules and a descriptive name in settings and "
            "details. The new level starts with no curriculum or inherited rules."
        ).classes("rbs-type-body rbs-text-muted")
        with ui.element("div").classes("rbs-training-level-dialog-grid w-full"):
            code = (
                ui.input("Short code", placeholder="SMF")
                .props("outlined maxlength=5 autofocus")
                .classes("w-full")
            )
            name = (
                ui.input("Full name", placeholder="Sports Medicine Fellow")
                .props("outlined maxlength=80")
                .classes("w-full")
            )

        def add() -> None:
            try:
                updated = add_training_level(
                    instance,
                    code=str(code.value or ""),
                    label=str(name.value or ""),
                )
                dialog.close()
                ui.notify("Training level added", type="positive")
                persist_instance(updated, preserve_schedule=schedule_is_current)
            except (ValidationError, ValueError) as exc:
                ui.notify(str(exc), type="negative", multi_line=True)

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props(TERTIARY_BUTTON_PROPS)
            ui.button("Add training level", icon="add", on_click=add).props(PRIMARY_BUTTON_PROPS)
    dialog.open()


def _open_remove_dialog(
    instance: SchedulerInput,
    pgy: int,
    persist_instance: PersistInstance,
    *,
    schedule_is_current: bool,
) -> None:
    from nicegui import ui

    curriculum = instance.curriculum_for(pgy)
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-lg gap-4"):
        ui.label(f"Delete {curriculum.display_label}?").classes("rbs-type-dialog-title")
        ui.label(
            "Its curriculum and all training-level-specific rotation, clinic, "
            "and grouping rules will be removed."
        ).classes("rbs-type-body rbs-text-muted")

        def remove() -> None:
            try:
                updated = remove_training_level(instance, pgy)
                dialog.close()
                ui.notify("Training level deleted", type="positive")
                persist_instance(updated, preserve_schedule=schedule_is_current)
            except (ValidationError, ValueError) as exc:
                ui.notify(str(exc), type="negative", multi_line=True)

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props(TERTIARY_BUTTON_PROPS)
            ui.button("Delete", icon="delete", on_click=remove).props(DESTRUCTIVE_BUTTON_PROPS)
    dialog.open()
