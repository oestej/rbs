"""Mandatory rotation detail panel and creation form."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from pydantic import ValidationError

from rbs.models.enums import RotationKind
from rbs.models.instance import SchedulerInput
from rbs.models.rotation import (
    ROTATION_CODE_MAX_LENGTH,
    Rotation,
)
from rbs.ui import master_detail
from rbs.ui.buttons import (
    ICON_BUTTON_PROPS,
    PRIMARY_BUTTON_PROPS,
    TERTIARY_BUTTON_PROPS,
    button_props,
)
from rbs.ui.drafts import Draft
from rbs.ui.editor_common import (
    _DEFAULT_BLOCK_DURATION_WEEKS,
    _DURATION_OPTIONS,
    _as_int,
    _validation_message,
)
from rbs.ui.rotations.forms import (
    _direct_elective_weeks,
    _mandatory_elective_availability,
    _rotation_detail_contents,
    _rotation_editor,
)
from rbs.ui.rotations.ops import (
    add_mandatory_rotation,
    next_mandatory_rotation_id,
    remove_mandatory_rotation,
)
from rbs.ui.rotations.summary import _rotation_identity
from rbs.ui.rotations.types import (
    SaveRotation,
    SelectRotation,
)
from rbs.ui.rotations.widgets import (
    rotation_color_palette,
)


def _rotation_detail_panel(
    instance: SchedulerInput,
    *,
    rotation: Rotation | None,
    creating: bool,
    missing_id: str | None,
    on_select: SelectRotation,
    on_save: SaveRotation,
) -> None:
    editing = False
    panel = master_detail.detail_panel()

    def render_panel() -> None:
        nonlocal editing
        panel.clear()
        with panel:
            if creating:
                _new_mandatory_rotation_form(
                    instance,
                    on_cancel=partial(on_select, None),
                    on_save=on_save,
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


def _new_mandatory_rotation_form(
    instance: SchedulerInput,
    *,
    on_cancel: Callable[[], None],
    on_save: SaveRotation,
) -> None:
    from nicegui import ui

    requirements: dict[int, Draft] = {}
    first_eligible_pgy = next(
        (
            curriculum.pgy
            for curriculum in instance.requirements
            if _direct_elective_weeks(instance, curriculum.pgy)
        ),
        None,
    )
    for curriculum in instance.requirements:
        requirements[curriculum.pgy] = {
            "enabled": curriculum.pgy == first_eligible_pgy,
            "duration_weeks": _DEFAULT_BLOCK_DURATION_WEEKS,
            "count": 1,
        }
    color_draft: Draft = {"color": instance.color_scheme.neutral.color}
    elective_draft: Draft = {
        "eligible": False,
        "eligible_pgys": [],
        "eligible_block_sizes": list(instance.elective_block_sizes),
        "repeatable": False,
    }

    with master_detail.detail_card():
        with ui.row().classes("w-full items-center justify-between gap-4 p-5"):
            with ui.column().classes("gap-0"):
                ui.label("New mandatory rotation").classes("rbs-type-page-title")
                ui.label(
                    "Create its training-level requirements, then configure advanced rules."
                ).classes("rbs-text-muted")
            with ui.button(icon="close", on_click=on_cancel).props(
                button_props(
                    ICON_BUTTON_PROPS,
                    "aria-label='Cancel new rotation'",
                )
            ):
                ui.tooltip("Cancel new rotation")
        ui.separator()
        with ui.column().classes("w-full gap-5 p-5"):
            with ui.column().classes("w-full gap-3"):
                ui.label("Basic information").classes("rbs-type-section-title")
                with ui.row().classes("w-full items-start gap-4"):
                    code = (
                        ui.input("Rotation code")
                        .props(f"outlined maxlength={ROTATION_CODE_MAX_LENGTH} counter")
                        .classes("rbs-rotation-code-input w-full sm:w-56")
                    )
                    name = ui.input("Rotation name").props("outlined").classes("w-full sm:flex-1")
                rotation_color_palette(color_draft, instance.color_scheme.palette)

            _mandatory_elective_availability(
                elective_draft,
                instance,
            )

            with ui.column().classes("w-full gap-3"):
                with ui.column().classes("gap-0"):
                    ui.label("Training-level requirements").classes("rbs-type-section-title")
                    ui.label(
                        "Required blocks replace the same total number of direct Elective "
                        "weeks so every curriculum remains complete."
                    ).classes("rbs-type-caption rbs-text-muted")
                for pgy, requirement in requirements.items():
                    available_weeks = _direct_elective_weeks(instance, pgy)
                    with (
                        ui.card()
                        .props("flat bordered")
                        .classes("rbs-rotation-nested-card w-full p-4 gap-3")
                    ):
                        with ui.row().classes("w-full items-center justify-between gap-3"):
                            enabled = ui.checkbox(
                                f"Required for {instance.training_level_name(pgy)}",
                                value=bool(requirement["enabled"]),
                            )
                            ui.label(
                                f"{available_weeks} direct Elective weeks available"
                                if available_weeks
                                else "No direct Elective weeks available"
                            ).classes("rbs-type-caption rbs-text-muted")
                        with ui.row().classes("w-full items-end gap-3"):
                            duration = (
                                ui.select(
                                    _DURATION_OPTIONS,
                                    value=int(requirement["duration_weeks"]),
                                    label="Block length",
                                )
                                .props("outlined options-dense")
                                .classes("w-full sm:flex-1")
                            )
                            count = (
                                ui.number(
                                    "Blocks per resident",
                                    value=int(requirement["count"]),
                                    min=1,
                                    max=instance.calendar.weeks,
                                    precision=0,
                                    step=1,
                                )
                                .props("outlined")
                                .classes("w-full sm:flex-1")
                            )
                        controls_enabled = bool(requirement["enabled"]) and bool(available_weeks)
                        duration.set_enabled(controls_enabled)
                        count.set_enabled(controls_enabled)
                        enabled.set_enabled(bool(available_weeks))
                        duration.bind_value(
                            requirement,
                            "duration_weeks",
                            forward=_as_int,
                        )
                        count.bind_value(requirement, "count", forward=_as_int)

                        def toggle_requirement(
                            event,
                            *,
                            requirement: Draft = requirement,
                            duration=duration,
                            count=count,
                        ) -> None:
                            requirement["enabled"] = bool(event.value)
                            duration.set_enabled(bool(event.value))
                            count.set_enabled(bool(event.value))

                        enabled.on_value_change(toggle_requirement)

            ui.label(
                "The new rotation starts with no continuity clinic. Staffing, placement, "
                "vacation, and clinic rules can be changed after it is added."
            ).classes("rbs-type-caption rbs-text-muted")

            def save() -> None:
                try:
                    selected = {
                        pgy: requirement
                        for pgy, requirement in requirements.items()
                        if requirement.get("enabled")
                    }
                    if not selected:
                        raise ValueError("select at least one training-level requirement")
                    maximum_duration = max(
                        int(requirement["duration_weeks"]) for requirement in selected.values()
                    )
                    rotation_id = next_mandatory_rotation_id(
                        instance,
                        str(name.value or code.value or ""),
                    )
                    rotation = Rotation.model_validate(
                        {
                            "id": rotation_id,
                            "code": str(code.value or ""),
                            "name": str(name.value or ""),
                            "color": color_draft["color"],
                            "kind": RotationKind.STANDARD.value,
                            "pgy_rules": [
                                {
                                    "pgy": pgy,
                                    "min_concurrent": None,
                                    "max_concurrent": None,
                                    "prerequisite_rotation_ids": [],
                                    "earliest_start_week": None,
                                    "block_configs": [
                                        {
                                            "duration_weeks": int(requirement["duration_weeks"]),
                                            "vacation": {
                                                "allowed": False,
                                                "max_weeks_per_block": None,
                                            },
                                        }
                                    ],
                                }
                                for pgy, requirement in selected.items()
                            ],
                            "no_clinic_hours": True,
                            "max_consecutive_weeks": max(4, maximum_duration),
                        }
                    )
                    counts = {
                        (pgy, int(requirement["duration_weeks"])): int(requirement["count"])
                        for pgy, requirement in selected.items()
                    }
                    updated = add_mandatory_rotation(
                        instance,
                        rotation,
                        counts,
                        eligible_as_elective=bool(elective_draft["eligible"]),
                        eligible_elective_pgys=[
                            int(pgy) for pgy in elective_draft.get("eligible_pgys", [])
                        ],
                        eligible_elective_block_sizes=[
                            int(size)
                            for size in elective_draft.get(
                                "eligible_block_sizes",
                                [],
                            )
                        ],
                        elective_repeatable=bool(elective_draft.get("repeatable")),
                    )
                    ui.notify(f"Added {rotation.name}", type="positive")
                    on_save(updated, rotation.id)
                except (TypeError, ValidationError, ValueError) as exc:
                    ui.notify(_validation_message(exc), type="negative", multi_line=True)

            with ui.row().classes("items-center gap-2"):
                ui.button("Add rotation", icon="add", on_click=save).props(PRIMARY_BUTTON_PROPS)
                ui.button("Cancel", on_click=on_cancel).props(TERTIARY_BUTTON_PROPS)


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
