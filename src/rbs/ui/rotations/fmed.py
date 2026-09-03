"""FMED/inpatient cards and PGY clinic-concurrency rules."""

from __future__ import annotations

from functools import partial

from pydantic import ValidationError

from rbs.models.enums import RotationKind
from rbs.models.instance import SchedulerInput
from rbs.ui import master_detail
from rbs.ui.drafts import Draft
from rbs.ui.editor_common import (
    _as_int,
    _optional_float,
    _validation_message,
)
from rbs.ui.rotations.forms import (
    _mandatory_elective_availability,
    _rotation_detail_contents,
    _staffing_and_blocks,
)
from rbs.ui.rotations.ops import (
    replace_fmed_pgy_rules,
    replace_rotation_color,
    rotation_editor_state,
    rotation_from_editor_state,
)
from rbs.ui.rotations.summary import _rotation_kind_label
from rbs.ui.rotations.types import (
    SaveRotation,
)
from rbs.ui.rotations.widgets import (
    rotation_code_style,
    rotation_color_palette,
)


def _dedicated_rotation_cards(
    instance: SchedulerInput,
    kind: RotationKind,
    *,
    selected_rotation_id: str | None,
    on_save: SaveRotation,
    on_color_save: SaveRotation | None = None,
) -> None:
    from nicegui import ui

    rotations = sorted(
        (rotation for rotation in instance.rotations if rotation.kind is kind),
        key=lambda rotation: rotation.code.casefold(),
    )
    if not rotations:
        master_detail.empty_detail(
            icon="inventory_2",
            title="No configuration found",
            description="Import a constraint catalog containing this rotation type.",
        )
        return
    with ui.column().classes("w-full gap-4"):
        for rotation in rotations:
            with master_detail.detail_card():
                with ui.row().classes("w-full items-center gap-4 p-5"):
                    with ui.row().classes("items-center gap-4"):
                        with (
                            ui.avatar(color=None)
                            .props("square")
                            .classes("rbs-rotation-code-avatar rbs-rotation-code-avatar-large")
                            .style(rotation_code_style(rotation.color))
                        ):
                            ui.label(rotation.code).classes("rbs-rotation-code-text")
                        with ui.column().classes("gap-0"):
                            ui.label(rotation.name).classes("rbs-type-page-title")
                            kind_label = _rotation_kind_label(rotation)
                            if kind_label is not None:
                                ui.label(kind_label).classes("rbs-text-muted")
                with ui.column().classes("w-full gap-3 px-5 pb-5"):
                    color_draft: Draft = {"color": rotation.color}

                    def save_color(color: str, rotation_id: str = rotation.id) -> None:
                        try:
                            updated = replace_rotation_color(instance, rotation_id, color)
                            ui.notify("Block schedule color updated", type="positive")
                            (on_color_save or on_save)(updated, selected_rotation_id)
                        except (ValidationError, ValueError) as exc:
                            ui.notify(
                                _validation_message(exc),
                                type="negative",
                                multi_line=True,
                            )

                    rotation_color_palette(
                        color_draft,
                        instance.color_scheme.palette,
                        on_change=save_color,
                    )
                ui.separator()
                _rotation_detail_contents(
                    instance,
                    rotation,
                    on_edit_pgy_rules=(
                        partial(
                            _open_fmed_pgy_rules_dialog,
                            instance,
                            rotation.id,
                            selected_rotation_id=selected_rotation_id,
                            on_save=on_save,
                        )
                        if kind is RotationKind.FMED
                        else None
                    ),
                )


def _open_fmed_pgy_rules_dialog(
    instance: SchedulerInput,
    rotation_id: str,
    *,
    selected_rotation_id: str | None,
    on_save: SaveRotation,
) -> None:
    from nicegui import ui

    rotation = instance.rotation(rotation_id)
    draft = rotation_editor_state(rotation)
    elective_option = instance.electives.option_for(rotation.id)
    elective_draft: Draft = {
        "eligible": elective_option is not None,
        "eligible_pgys": list(elective_option.eligible_pgys if elective_option is not None else []),
        "eligible_block_sizes": list(
            instance.eligible_elective_block_sizes(rotation.id)
            or instance.available_elective_block_sizes(rotation.id)
        ),
        "repeatable": bool(elective_option and elective_option.repeatable),
    }
    counts = {
        (rule.pgy, config.duration_weeks): sum(
            block.count
            for block in instance.curriculum_for(rule.pgy).blocks
            if block.rotation_id == rotation_id and block.duration_weeks == config.duration_weeks
        )
        for rule in rotation.pgy_rules
        if rule.pgy in instance.training_level_ids
        for config in rule.block_configs
    }

    with (
        ui.dialog() as dialog,
        ui.card()
        .classes("rbs-fmed-rules-dialog p-0 gap-0")
        .style(
            "width:calc(100vw - 64px);max-width:960px;height:calc(100vh - 64px);max-height:900px"
        ),
    ):
        with ui.row().classes("w-full items-center justify-between gap-3 px-5 py-4"):
            ui.label(f"Edit FMED rules · {rotation.name}").classes("rbs-type-dialog-title")
            ui.button(icon="close", on_click=dialog.close).props(
                "flat round dense aria-label='Close FMED rules'"
            )
        ui.separator()
        with ui.scroll_area().classes("w-full flex-1 min-h-0"):
            with ui.column().classes("w-full gap-4 p-5"):
                _mandatory_elective_availability(
                    elective_draft,
                    instance,
                )
                _fmed_clinic_concurrency_editor(instance, draft)
                _staffing_and_blocks(
                    instance,
                    draft,
                    rotation_id,
                    requirement_counts=counts,
                )
        ui.separator()
        with ui.row().classes("w-full justify-end gap-3 p-4"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")

            def save_rules() -> None:
                try:
                    replacement = rotation_from_editor_state(draft)
                    updated = replace_fmed_pgy_rules(
                        instance,
                        rotation_id,
                        replacement,
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
                    dialog.close()
                    ui.notify("FMED rules updated", type="positive")
                    on_save(updated, selected_rotation_id)
                except (ValidationError, ValueError) as exc:
                    ui.notify(
                        _validation_message(exc),
                        type="negative",
                        multi_line=True,
                    )

            ui.button("Save rules", icon="save", on_click=save_rules).props("unelevated no-caps")
    dialog.open()


def _fmed_clinic_concurrency_editor(
    instance: SchedulerInput,
    draft: Draft,
) -> None:
    """Edit the half-day headcount caps owned by an FMED clinic rule."""
    from nicegui import ui

    clinic = draft.get("clinic")
    with ui.column().classes("rbs-rotation-editor-subsection w-full gap-3 rounded p-4"):
        with ui.column().classes("gap-0"):
            ui.label("Inpatient clinic concurrency").classes("rbs-type-control-label")
            ui.label(
                "Limit residents from this inpatient service who may attend the same "
                "clinic half-day. Training-level limits apply together with the "
                "overall limit."
            ).classes("rbs-type-caption rbs-text-muted")
        if not isinstance(clinic, dict):
            ui.label("This inpatient rotation has no clinic rule.").classes(
                "rbs-type-body rbs-text-muted"
            )
            return

        overall = (
            ui.number(
                "Maximum residents in clinic at one time",
                value=_optional_float(clinic.get("max_concurrent")),
                min=1,
                precision=0,
                step=1,
                placeholder="No maximum",
            )
            .props("outlined clearable")
            .classes("w-full")
        )
        overall.bind_value(clinic, "max_concurrent", forward=_as_int)

        limits = clinic.setdefault("max_concurrent_by_pgy", {})
        with ui.row().classes("w-full gap-3 flex-wrap"):
            for pgy in instance.training_level_ids:
                level_name = instance.training_level_name(pgy)
                pgy_limit = (
                    ui.number(
                        f"Maximum {level_name} residents in clinic at one time",
                        value=_optional_float(limits.get(str(pgy), limits.get(pgy))),
                        min=1,
                        precision=0,
                        step=1,
                        placeholder="No maximum",
                    )
                    .props("outlined clearable")
                    .classes("w-full md:flex-1")
                )
                pgy_limit.on_value_change(partial(_set_fmed_pgy_clinic_limit, limits, pgy))


def _set_fmed_pgy_clinic_limit(limits: Draft, pgy: int, event) -> None:
    limits.pop(pgy, None)
    limits.pop(str(pgy), None)
    maximum = _as_int(event.value)
    if maximum is not None:
        limits[str(pgy)] = maximum
