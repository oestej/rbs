"""Standalone elective rotation configuration."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from functools import partial

from pydantic import ValidationError

from rbs.models.enums import RotationKind
from rbs.models.instance import (
    ResidentRotationOverride,
    ResidentRotationWaiver,
    SchedulerInput,
)
from rbs.models.resident import Resident, resident_display_sort_key
from rbs.models.rotation import (
    Rotation,
    rotation_display_sort_key,
)
from rbs.ui import master_detail
from rbs.ui.buttons import (
    DESTRUCTIVE_ICON_BUTTON_PROPS,
    ICON_BUTTON_PROPS,
    PRIMARY_BUTTON_PROPS,
    TERTIARY_BUTTON_PROPS,
    button_props,
)
from rbs.ui.clinic.ops import (
    _default_clinic_rule,
)
from rbs.ui.drafts import Draft
from rbs.ui.editor_common import (
    _DEFAULT_BLOCK_DURATION_WEEKS,
    _DURATION_OPTIONS,
    _validation_message,
    _weeks_label,
)
from rbs.ui.rotations.availability import _elective_availability_editor
from rbs.ui.rotations.fmed import _open_fmed_pgy_rules_dialog
from rbs.ui.rotations.forms import (
    RotationEditorGuard,
    _clinic_rule_editor,
    _confirm_close_editor,
    _core_settings,
    _draft_has_clinic_configuration,
    _rotation_detail_contents,
    _rotation_editor,
    _staffing_and_blocks,
)
from rbs.ui.rotations.ops import (
    add_elective_rotation,
    add_named_elective_take,
    add_resident_rotation_waiver,
    direct_elective_counts,
    elective_rotations,
    elective_waiver_duration_options,
    named_elective_take_duration_options,
    named_elective_take_service_options,
    named_elective_takes,
    next_mandatory_rotation_id,
    remaining_direct_elective_blocks,
    remove_elective_rotation,
    replace_elective_color,
    replace_elective_rotation,
    resolve_elective_waiver_rotation,
    rotation_editor_state,
    rotation_from_editor_state,
    rotation_group_members_by_pgy,
    set_elective_allocation,
)
from rbs.ui.rotations.summary import (
    _elective_block_size_options,
    _rotation_identity,
)
from rbs.ui.rotations.types import (
    NEW_ELECTIVE_ROTATION_ID,
    SaveRotation,
    SelectRotation,
)
from rbs.ui.rotations.widgets import (
    rotation_code_style,
    rotation_color_palette,
)


def _elective_configuration(
    instance: SchedulerInput,
    *,
    selected_rotation_id: str | None,
    on_select: SelectRotation,
    on_save: SaveRotation,
    on_color_save: SaveRotation,
    guard: RotationEditorGuard | None = None,
) -> None:
    """Render shared Elective policy and a unified option workspace."""
    from nicegui import ui

    available = [
        instance.rotation(option.rotation_id) for option in instance.electives.rotation_options
    ]
    available.sort(key=rotation_display_sort_key)
    selected = next(
        (rotation for rotation in available if rotation.id == selected_rotation_id),
        None,
    )

    creating = selected_rotation_id == NEW_ELECTIVE_ROTATION_ID

    with ui.column().classes("w-full gap-4"):
        with master_detail.detail_card():
            with ui.column().classes("w-full gap-3 p-5"):
                with ui.row().classes("w-full items-start justify-between gap-3"):
                    with ui.column().classes("gap-0"):
                        ui.label("Shared elective properties").classes("rbs-type-section-title")
                    ui.button(
                        "Edit rules",
                        icon="edit",
                        on_click=partial(
                            _open_elective_properties_dialog,
                            instance,
                            selected_rotation_id=selected_rotation_id,
                            on_save=on_save,
                            on_color_save=on_color_save,
                        ),
                    ).props("outline dense no-caps")
                _elective_shared_summary(instance)

        with master_detail.split(detail_selected=selected is not None or creating):
            _elective_directory(
                instance,
                available,
                selected_rotation_id=selected_rotation_id,
                on_select=on_select,
                on_save=on_save,
            )
            _elective_detail_panel(
                instance,
                rotation=selected,
                creating=creating,
                missing_id=(
                    selected_rotation_id
                    if selected_rotation_id is not None
                    and selected_rotation_id not in instance.rotations_by_id
                    and not creating
                    else None
                ),
                on_select=on_select,
                on_save=on_save,
                guard=guard,
            )


def _elective_shared_summary(instance: SchedulerInput) -> None:
    """Report the shared Elective color and each level's committed Elective time.

    Elective time is the one requirement a program allocates without naming a
    service, so it has no rotation of its own to be edited from. Without this
    surface a level's Elective blocks could only be created as a side effect of
    adding a Mandatory requirement or enabling an option.
    """
    from nicegui import ui

    with ui.row().classes(
        "rbs-elective-shared-summary w-full items-start gap-5 flex-wrap"
    ):
        with ui.column().classes("rbs-elective-shared-color gap-1"):
            ui.label("Block schedule color").classes(
                "rbs-type-caption rbs-font-semibold uppercase rbs-text-muted"
            )
            with ui.row().classes("items-center gap-2"):
                ui.element("span").classes("rbs-rotation-color-swatch").style(
                    f"--rbs-rotation-choice-color:{instance.electives.color}"
                )
                ui.label(instance.electives.color).classes("rbs-type-body")
        with ui.column().classes("rbs-elective-shared-time min-w-0 flex-1 gap-2"):
            ui.label("Elective time").classes(
                "rbs-type-caption rbs-font-semibold uppercase rbs-text-muted"
            )
            if not instance.requirements:
                ui.label("No training levels are configured yet.").classes(
                    "rbs-type-body rbs-text-muted"
                )
                return
            with ui.element("div").classes("rbs-elective-shared-grid w-full"):
                for curriculum in instance.requirements:
                    _elective_time_level_summary(instance, curriculum.pgy)


def _elective_time_level_summary(instance: SchedulerInput, pgy: int) -> None:
    """One training level's committed Elective blocks and its unscheduled time."""
    from nicegui import ui

    committed = direct_elective_counts(instance, pgy)
    total = sum(duration * count for duration, count in committed.items())
    with ui.column().classes("rbs-elective-time-summary min-w-0 gap-0"):
        with ui.row().classes("w-full items-baseline justify-between gap-2 flex-nowrap"):
            ui.label(instance.training_level_name(pgy)).classes("rbs-type-control-label")
            ui.label(_weeks_label(total)).classes("rbs-type-caption rbs-text-muted")
        ui.label(
            "; ".join(
                f"{count} × {_weeks_label(duration)}" for duration, count in committed.items()
            )
            or "None committed"
        ).classes("rbs-type-body rbs-font-semibold")
        ui.label(f"{_weeks_label(instance.unallocated_weeks(pgy))} unscheduled").classes(
            "rbs-type-caption rbs-text-muted"
        )


def _sorted_residents(instance: SchedulerInput) -> list[Resident]:
    return sorted(instance.residents, key=resident_display_sort_key)


def _take_funding_options(
    instance: SchedulerInput,
    resident_id: str,
    duration_weeks: int,
) -> dict[str, str]:
    """Unallocated and replacement funding choices for one elective take."""
    if instance.residents_by_id.get(resident_id) is None:
        return {}
    options: dict[str, str] = {}
    unallocated = instance.resident_unallocated_weeks(resident_id)
    if unallocated >= duration_weeks:
        options["unallocated"] = f"Unallocated time ({unallocated} weeks available)"
    for (rotation_id, duration), count in remaining_direct_elective_blocks(
        instance, resident_id
    ).items():
        if duration != duration_weeks:
            continue
        rotation = instance.rotation(rotation_id)
        options[f"{rotation_id}|{duration}"] = (
            f"Replace {rotation.code} · {duration}-week elective"
            + (f" ({count} available)" if count > 1 else "")
        )
    return options


def _elective_waivers(
    instance: SchedulerInput,
    resident_id: str,
) -> list[tuple[int, ResidentRotationWaiver]]:
    """Elective-kind waivers for one resident with their case positions."""
    return [
        (index, waiver)
        for index, waiver in enumerate(instance.resident_rotation_waivers)
        if waiver.resident_id == resident_id
        and instance.rotation(waiver.rotation_id).kind is RotationKind.ELECTIVE
    ]


def _staged_take_row(
    instance: SchedulerInput,
    resident: Resident,
    index: int,
    take: ResidentRotationOverride,
    removed: set[int],
    refresh: Callable[[], None],
) -> None:
    from nicegui import ui

    rotation = instance.rotation(take.rotation_id)
    funded_by = (
        "uses unallocated time"
        if take.replaces_rotation_id is None
        else f"replaces {instance.rotation(take.replaces_rotation_id).code}"
    )

    def _stage_removal() -> None:
        removed.add(index)
        refresh()

    with ui.row().classes(
        "rbs-resident-rotation-override w-full items-center gap-3 rounded p-3"
    ):
        with ui.column().classes("min-w-0 flex-1 gap-0"):
            ui.label(
                f"{resident.name} · {instance.training_level_name(resident.pgy)}"
            ).classes("rbs-font-semibold")
            ui.label(
                f"{rotation.code} · {_weeks_label(take.duration_weeks)} Elective Slot · "
                f"{funded_by}"
            ).classes("rbs-type-caption rbs-text-muted")
        ui.button(
            icon="delete_outline",
            on_click=lambda _event: _stage_removal(),
        ).props(
            "flat round dense color=negative aria-label='Remove Elective Slot'"
        )


def _take_draft_row(
    instance: SchedulerInput,
    draft_index: int,
    draft: Draft,
    take_drafts: list[Draft],
    refresh: Callable[[], None],
) -> None:
    from nicegui import ui

    resident = instance.residents_by_id[str(draft["resident_id"])]
    rotation = instance.rotation(str(draft["rotation_id"]))
    funding = str(draft["funding"])
    funded_by = (
        "uses unallocated time"
        if funding == "unallocated"
        else f"replaces {instance.rotation(funding.rsplit('|', 1)[0]).code}"
    )

    def _discard() -> None:
        take_drafts.pop(draft_index)
        refresh()

    with ui.row().classes(
        "rbs-resident-rotation-override w-full items-center gap-3 rounded p-3"
    ):
        with ui.column().classes("min-w-0 flex-1 gap-0"):
            ui.label(
                f"{resident.name} · {instance.training_level_name(resident.pgy)}"
            ).classes("rbs-font-semibold")
            ui.label(
                f"{rotation.code} · {_weeks_label(int(draft['duration_weeks']))} "
                f"Elective Slot · {funded_by} · added on save"
            ).classes("rbs-type-caption rbs-text-muted")
        ui.button(
            icon="delete_outline",
            on_click=lambda _event: _discard(),
        ).props(
            "flat round dense color=negative aria-label='Discard Elective Slot'"
        )


def _elective_slot_duration_options(
    instance: SchedulerInput,
    resident_id: str,
) -> dict[int, str]:
    return {
        duration: _weeks_label(duration)
        for duration in named_elective_take_duration_options(instance, resident_id)
        if _take_funding_options(instance, resident_id, duration)
    }


def _elective_slot_resident_options(instance: SchedulerInput) -> dict[str, str]:
    return {
        resident.id: f"{resident.name} · {instance.training_level_name(resident.pgy)}"
        for resident in _sorted_residents(instance)
        if _elective_slot_duration_options(instance, resident.id)
    }


def _open_elective_slot_dialog(
    instance: SchedulerInput,
    take_drafts: list[Draft],
    refresh: Callable[[], None],
) -> None:
    from nicegui import ui

    resident_options = _elective_slot_resident_options(instance)
    if not resident_options:
        ui.notify(
            "No resident has compatible unallocated or Elective time available",
            type="warning",
        )
        return
    resident_id = next(iter(resident_options))
    durations = _elective_slot_duration_options(instance, resident_id)
    initial_duration = next(iter(durations))
    service_options = named_elective_take_service_options(
        instance,
        resident_id,
        initial_duration,
    )
    funding_options = _take_funding_options(instance, resident_id, initial_duration)

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl p-0 gap-0"):
        with ui.row().classes("w-full items-center justify-between gap-3 px-5 py-4"):
            ui.label("Add Elective Slot").classes("rbs-type-dialog-title")
            ui.button(icon="close", on_click=dialog.close).props(
                "flat round dense aria-label='Close Elective Slot dialog'"
            )
        ui.separator()
        with ui.column().classes("w-full gap-4 p-5"):
            ui.label(
                "Add one named elective service for a resident without changing the "
                "training-level rules. Unallocated time is used when available; "
                "otherwise, replace a same-length elective block."
            ).classes("rbs-type-body rbs-text-muted")
            resident_select = (
                ui.select(resident_options, value=resident_id, label="Resident")
                .props("outlined options-dense use-input")
                .classes("w-full")
            )
            duration_select = (
                ui.select(
                    durations,
                    value=initial_duration,
                    label="Block length",
                )
                .props("outlined options-dense")
                .classes("w-full")
            )
            service_select = (
                ui.select(
                    service_options,
                    value=next(iter(service_options)),
                    label="Service",
                )
                .props("outlined options-dense use-input")
                .classes("w-full")
            )
            funding_select = (
                ui.select(
                    funding_options,
                    value=next(iter(funding_options)),
                    label="Funded by",
                )
                .props("outlined options-dense")
                .classes("w-full")
            )

        def refresh_services_and_funding() -> None:
            if resident_select.value is None or duration_select.value is None:
                service_select.set_options({}, value=None)
                funding_select.set_options({}, value=None)
                return
            duration = int(duration_select.value)
            services = named_elective_take_service_options(
                instance, str(resident_select.value), duration
            )
            service_select.set_options(
                services,
                value=(
                    service_select.value
                    if service_select.value in services
                    else next(iter(services), None)
                ),
            )
            funding = _take_funding_options(instance, str(resident_select.value), duration)
            funding_select.set_options(funding, value=next(iter(funding), None))

        def refresh_all() -> None:
            resident_choice = (
                str(resident_select.value) if resident_select.value is not None else None
            )
            choices = (
                _elective_slot_duration_options(instance, resident_choice)
                if resident_choice is not None
                else {}
            )
            duration_select.set_options(
                choices,
                value=(
                    duration_select.value
                    if isinstance(duration_select.value, int)
                    and duration_select.value in choices
                    else next(iter(choices), None)
                ),
            )
            refresh_services_and_funding()

        resident_select.on_value_change(lambda _event: refresh_all())
        duration_select.on_value_change(lambda _event: refresh_services_and_funding())

        def stage_slot() -> None:
            try:
                if (
                    resident_select.value is None
                    or duration_select.value is None
                    or service_select.value is None
                    or funding_select.value is None
                ):
                    raise ValueError("complete all Elective Slot fields")
                resident_choice = str(resident_select.value)
                duration = int(duration_select.value)
                rotation_id = str(service_select.value)
                if rotation_id not in named_elective_take_service_options(
                    instance, resident_choice, duration
                ):
                    raise ValueError("that service cannot fill this Elective Slot")
                funding = _take_funding_options(instance, resident_choice, duration)
                funding_key = str(funding_select.value)
                if funding_key not in funding:
                    raise ValueError("no compatible unallocated or elective time remains")
                take_drafts.append(
                    {
                        "resident_id": resident_choice,
                        "rotation_id": rotation_id,
                        "duration_weeks": duration,
                        "funding": funding_key,
                    }
                )
                dialog.close()
                refresh()
            except (ValidationError, ValueError) as exc:
                ui.notify(str(exc), type="negative", multi_line=True)

        ui.separator()
        with ui.row().classes("w-full justify-end gap-3 p-4"):
            ui.button("Cancel", on_click=dialog.close).props(TERTIARY_BUTTON_PROPS)
            ui.button("Add slot", icon="add", on_click=stage_slot).props(
                PRIMARY_BUTTON_PROPS
            )
    dialog.open()


def _staged_waiver_row(
    instance: SchedulerInput,
    resident: Resident,
    index: int,
    waiver: ResidentRotationWaiver,
    removed: set[int],
    refresh: Callable[[], None],
) -> None:
    from nicegui import ui

    rotation = instance.rotation(waiver.rotation_id)

    def _stage_removal() -> None:
        removed.add(index)
        refresh()

    with ui.row().classes(
        "rbs-resident-rotation-waiver w-full items-center gap-3 rounded p-3"
    ):
        with ui.column().classes("min-w-0 flex-1 gap-0"):
            ui.label(
                f"{resident.name} · {instance.training_level_name(resident.pgy)}"
            ).classes("rbs-font-semibold")
            ui.label(
                f"{rotation.code} · {_weeks_label(waiver.duration_weeks)} waived"
            ).classes("rbs-type-caption rbs-text-muted")
        ui.button(
            icon="delete_outline",
            on_click=lambda _event: _stage_removal(),
        ).props(
            "flat round dense color=negative aria-label='Remove elective waiver'"
        )


def _waiver_draft_row(
    instance: SchedulerInput,
    draft_index: int,
    draft: Draft,
    waiver_drafts: list[Draft],
    refresh: Callable[[], None],
) -> None:
    from nicegui import ui

    resident = instance.residents_by_id[str(draft["resident_id"])]
    rotation = instance.rotation(str(draft["rotation_id"]))

    def _discard() -> None:
        waiver_drafts.pop(draft_index)
        refresh()

    with ui.row().classes(
        "rbs-resident-rotation-waiver w-full items-center gap-3 rounded p-3"
    ):
        with ui.column().classes("min-w-0 flex-1 gap-0"):
            ui.label(
                f"{resident.name} · {instance.training_level_name(resident.pgy)}"
            ).classes("rbs-font-semibold")
            ui.label(
                f"{rotation.code} · {_weeks_label(int(draft['duration_weeks']))} "
                "waived · added on save"
            ).classes("rbs-type-caption rbs-text-muted")
        ui.button(
            icon="delete_outline",
            on_click=lambda _event: _discard(),
        ).props(
            "flat round dense color=negative aria-label='Discard elective waiver'"
        )


def _elective_waiver_resident_options(instance: SchedulerInput) -> dict[str, str]:
    return {
        resident.id: f"{resident.name} · {instance.training_level_name(resident.pgy)}"
        for resident in _sorted_residents(instance)
        if elective_waiver_duration_options(instance, resident.id)
    }


def _open_elective_waiver_dialog(
    instance: SchedulerInput,
    waiver_drafts: list[Draft],
    refresh: Callable[[], None],
) -> None:
    from nicegui import ui

    resident_options = _elective_waiver_resident_options(instance)
    if not resident_options:
        ui.notify("No resident has an elective block available to waive", type="warning")
        return
    resident_id = next(iter(resident_options))
    durations = elective_waiver_duration_options(instance, resident_id)

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl p-0 gap-0"):
        with ui.row().classes("w-full items-center justify-between gap-3 px-5 py-4"):
            ui.label("Waive elective block").classes("rbs-type-dialog-title")
            ui.button(icon="close", on_click=dialog.close).props(
                "flat round dense aria-label='Close elective waiver dialog'"
            )
        ui.separator()
        with ui.column().classes("w-full gap-4 p-5"):
            ui.label(
                "Excuse one resident from a direct elective block without changing "
                "the training-level rules for everyone else."
            ).classes("rbs-type-body rbs-text-muted")
            resident_select = (
                ui.select(resident_options, value=resident_id, label="Resident")
                .props("outlined options-dense use-input")
                .classes("w-full")
            )
            duration_select = (
                ui.select(
                    durations,
                    value=next(iter(durations)),
                    label="Block length",
                )
                .props("outlined options-dense")
                .classes("w-full")
            )

        def refresh_durations() -> None:
            choices = (
                elective_waiver_duration_options(instance, str(resident_select.value))
                if resident_select.value is not None
                else {}
            )
            duration_select.set_options(
                choices,
                value=next(iter(choices), None),
            )

        resident_select.on_value_change(lambda _event: refresh_durations())

        def stage_waiver() -> None:
            try:
                if (
                    resident_select.value is None
                    or duration_select.value is None
                ):
                    raise ValueError("choose a resident and a block length")
                resident_choice = str(resident_select.value)
                duration = int(duration_select.value)
                rotation_id = resolve_elective_waiver_rotation(
                    instance, resident_choice, duration
                )
                waiver_drafts.append(
                    {
                        "resident_id": resident_choice,
                        "rotation_id": rotation_id,
                        "duration_weeks": duration,
                    }
                )
                dialog.close()
                refresh()
            except (ValidationError, ValueError) as exc:
                ui.notify(str(exc), type="negative", multi_line=True)

        ui.separator()
        with ui.row().classes("w-full justify-end gap-3 p-4"):
            ui.button("Cancel", on_click=dialog.close).props(TERTIARY_BUTTON_PROPS)
            ui.button("Add waiver", icon="person_off", on_click=stage_waiver).props(
                PRIMARY_BUTTON_PROPS
            )
    dialog.open()


def _elective_overrides_editor(
    instance: SchedulerInput,
    take_drafts: list[Draft],
    waiver_drafts: list[Draft],
    removed_take_indices: set[int],
    removed_waiver_indices: set[int],
) -> None:
    """Render Elective Slots and waivers like the other resident exceptions."""
    from nicegui import ui

    container = ui.column().classes("w-full gap-3")

    def render() -> None:
        container.clear()
        has_saved_slot = any(
            index not in removed_take_indices and override.elective
            for index, override in enumerate(instance.resident_rotation_overrides)
        )
        has_saved_waiver = any(
            index not in removed_waiver_indices
            and instance.rotation(waiver.rotation_id).kind is RotationKind.ELECTIVE
            for index, waiver in enumerate(instance.resident_rotation_waivers)
        )
        with container:
            ui.label(
                "Elective Slots add a named elective service for one resident. "
                "Waivers excuse one resident from a direct elective block. Both "
                "leave the training-level rules unchanged."
            ).classes("rbs-type-body rbs-text-muted")
            if not (has_saved_slot or has_saved_waiver or take_drafts or waiver_drafts):
                ui.label("No resident-specific exceptions.").classes(
                    "rbs-type-body rbs-text-muted"
                )

            for resident in _sorted_residents(instance):
                for index, take in named_elective_takes(instance, resident.id):
                    if index not in removed_take_indices:
                        _staged_take_row(
                            instance,
                            resident,
                            index,
                            take,
                            removed_take_indices,
                            render,
                        )
            for draft_index, draft in enumerate(take_drafts):
                _take_draft_row(
                    instance,
                    draft_index,
                    draft,
                    take_drafts,
                    render,
                )
            for resident in _sorted_residents(instance):
                for index, waiver in _elective_waivers(instance, resident.id):
                    if index not in removed_waiver_indices:
                        _staged_waiver_row(
                            instance,
                            resident,
                            index,
                            waiver,
                            removed_waiver_indices,
                            render,
                        )
            for draft_index, draft in enumerate(waiver_drafts):
                _waiver_draft_row(
                    instance,
                    draft_index,
                    draft,
                    waiver_drafts,
                    render,
                )

            with ui.row().classes("w-full items-center gap-3 flex-wrap"):
                slot_button = ui.button(
                    "Add Elective Slot",
                    icon="person_add",
                    on_click=partial(
                        _open_elective_slot_dialog,
                        instance,
                        take_drafts,
                        render,
                    ),
                ).props("outline no-caps")
                slot_button.set_enabled(bool(_elective_slot_resident_options(instance)))
                waiver_button = ui.button(
                    "Waive elective block",
                    icon="person_off",
                    on_click=partial(
                        _open_elective_waiver_dialog,
                        instance,
                        waiver_drafts,
                        render,
                    ),
                ).props("outline no-caps")
                waiver_button.set_enabled(
                    bool(_elective_waiver_resident_options(instance))
                )

    render()


def _open_elective_properties_dialog(
    instance: SchedulerInput,
    *,
    selected_rotation_id: str | None,
    on_save: SaveRotation,
    on_color_save: SaveRotation | None = None,
) -> None:
    """Edit the shared Elective color and every level's Elective blocks."""
    from nicegui import ui

    color_draft: Draft = {"color": instance.electives.color}
    drafts: dict[int, list[Draft]] = {
        curriculum.pgy: [
            {"duration_weeks": duration, "count": count}
            for duration, count in direct_elective_counts(instance, curriculum.pgy).items()
        ]
        for curriculum in instance.requirements
    }
    # Elective time can always be re-spent on itself, so a level's budget is its
    # unscheduled weeks plus whatever Elective time it already holds.
    budgets = {
        pgy: instance.unallocated_weeks(pgy)
        + sum(int(row["duration_weeks"]) * int(row["count"]) for row in rows)
        for pgy, rows in drafts.items()
    }
    overspent: dict[int, bool] = {}
    actions: dict[str, object] = {"save": None}
    # Resident overrides stage alongside the shared rows and commit on save.
    # Additions validate against the committed case when staged; the save
    # below arbitrates the staged composition as a whole.
    take_drafts: list[Draft] = []
    waiver_drafts: list[Draft] = []
    removed_take_indices: set[int] = set()
    removed_waiver_indices: set[int] = set()

    def refresh_save() -> None:
        save = actions["save"]
        if save is not None:
            save.set_enabled(not any(overspent.values()))

    def save_properties() -> None:
        try:
            recolored = replace_elective_color(instance, str(color_draft["color"]))
            updated = recolored
            for pgy, rows in drafts.items():
                desired = {
                    int(row["duration_weeks"]): int(row["count"])
                    for row in rows
                    if int(row["count"])
                }
                # Reallocating an untouched level would reshape its curriculum
                # blocks for nothing, and would hide a color-only edit.
                if desired == direct_elective_counts(instance, pgy):
                    continue
                updated = set_elective_allocation(updated, pgy, desired)
            # Removals apply first so additions validate against freed time.
            if removed_take_indices or removed_waiver_indices:
                kept_overrides = [
                    override
                    for index, override in enumerate(updated.resident_rotation_overrides)
                    if index not in removed_take_indices or not override.elective
                ]
                kept_waivers = [
                    waiver
                    for index, waiver in enumerate(updated.resident_rotation_waivers)
                    if index not in removed_waiver_indices
                    or instance.rotation(waiver.rotation_id).kind
                    is not RotationKind.ELECTIVE
                ]
                updated = updated.revised(
                    resident_rotation_overrides=kept_overrides,
                    resident_rotation_waivers=kept_waivers,
                )
            for draft in take_drafts:
                funding = str(draft["funding"])
                updated = add_named_elective_take(
                    updated,
                    resident_id=str(draft["resident_id"]),
                    rotation_id=str(draft["rotation_id"]),
                    duration_weeks=int(draft["duration_weeks"]),
                    replaces_rotation_id=(
                        None if funding == "unallocated" else funding.rsplit("|", 1)[0]
                    ),
                )
            for draft in waiver_drafts:
                updated = add_resident_rotation_waiver(
                    updated,
                    {
                        "resident_id": str(draft["resident_id"]),
                        "rotation_id": str(draft["rotation_id"]),
                        "duration_weeks": int(draft["duration_weeks"]),
                    },
                )
            dialog.close()
            ui.notify("Shared elective properties updated", type="positive")
            # A color-only edit leaves a solved schedule valid.
            overrides_dirty = bool(
                take_drafts or waiver_drafts or removed_take_indices or removed_waiver_indices
            )
            if updated == recolored and not overrides_dirty and on_color_save is not None:
                on_color_save(updated, selected_rotation_id)
            else:
                on_save(updated, selected_rotation_id)
        except (ValidationError, ValueError) as exc:
            ui.notify(_validation_message(exc), type="negative", multi_line=True)

    with (
        ui.dialog() as dialog,
        ui.card()
        .classes("rbs-elective-properties-dialog p-0 gap-0")
        .style(
            "width:calc(100vw - 64px);max-width:1200px;"
            "height:calc(100vh - 64px);max-height:900px"
        ),
    ):
        with ui.row().classes(
            "rbs-elective-properties-header w-full items-center gap-5 px-5 py-4"
        ):
            ui.label("Edit shared elective properties").classes(
                "rbs-elective-properties-title rbs-type-dialog-title whitespace-nowrap"
            )
            with (
                ui.tabs()
                .props("dense no-caps inline-label align=left mobile-arrows outside-arrows")
                .classes("rbs-elective-properties-tabs min-w-0") as tabs
            ):
                general_tab = ui.tab(
                    "elective_properties_general",
                    label="General",
                    icon="tune",
                )
                time_tab = ui.tab(
                    "elective_properties_time",
                    label="Training-level rules",
                    icon="groups",
                )
                overrides_tab = ui.tab(
                    "elective_properties_overrides",
                    label="Resident overrides",
                    icon="person_add",
                )
            ui.space()
            actions["save"] = ui.button(
                "Save",
                icon="save",
                on_click=save_properties,
            ).props(PRIMARY_BUTTON_PROPS).classes("rbs-elective-properties-save")
            ui.button(icon="close", on_click=dialog.close).props(
                button_props(ICON_BUTTON_PROPS, "aria-label='Close shared elective properties'")
            ).classes("rbs-elective-properties-close")
        with (
            ui.tab_panels(tabs, value=general_tab)
            .props("animated")
            .classes("rbs-elective-properties-panels w-full flex-1 min-h-0")
        ):
            with ui.tab_panel(general_tab).classes("h-full p-0"):
                with ui.scroll_area().classes("h-full w-full"):
                    with ui.column().classes("w-full gap-5 p-6"):
                        with ui.column().classes("gap-1"):
                            ui.label("General elective settings").classes(
                                "rbs-type-section-title"
                            )
                            ui.label(
                                "Choose the block schedule color shared by standalone electives."
                            ).classes("rbs-type-caption rbs-text-muted")
                        rotation_color_palette(
                            color_draft,
                            instance.color_scheme.palette,
                        )

            with ui.tab_panel(time_tab).classes("h-full p-0"):
                with ui.scroll_area().classes("h-full w-full"):
                    with ui.column().classes("w-full gap-4 p-6"):
                        with ui.column().classes("gap-1"):
                            ui.label("Elective time").classes("rbs-type-section-title")
                            ui.label(
                                "Adding blocks spends a training level's unscheduled weeks; "
                                "removing them gives the time back."
                            ).classes("rbs-type-caption rbs-text-muted")
                        if not drafts:
                            ui.label("No training levels are configured yet.").classes(
                                "rbs-type-body rbs-text-muted"
                            )
                        for pgy, rows in drafts.items():
                            _elective_time_level_editor(
                                instance,
                                pgy,
                                rows,
                                budget_weeks=budgets[pgy],
                                overspent=overspent,
                                on_change=refresh_save,
                                expanded=len(drafts) == 1,
                            )

            with ui.tab_panel(overrides_tab).classes("h-full p-0"):
                with ui.scroll_area().classes("h-full w-full"):
                    with ui.column().classes("w-full gap-4 p-6"):
                        with ui.column().classes("gap-1"):
                            ui.label("Resident overrides").classes(
                                "rbs-type-section-title"
                            )
                            ui.label(
                                "Add or waive a named resident's elective placement "
                                "without changing the rules for everyone else."
                            ).classes("rbs-type-caption rbs-text-muted")
                        _elective_overrides_editor(
                            instance,
                            take_drafts,
                            waiver_drafts,
                            removed_take_indices,
                            removed_waiver_indices,
                        )
    refresh_save()
    dialog.open()


def _elective_time_level_editor(
    instance: SchedulerInput,
    pgy: int,
    rows: list[Draft],
    *,
    budget_weeks: int,
    overspent: dict[int, bool],
    on_change: Callable[[], None],
    expanded: bool = False,
) -> None:
    """Edit one training level's Elective slots against the weeks it can spend."""
    from nicegui import ui

    def planned_weeks() -> int:
        return sum(int(row["duration_weeks"]) * int(row["count"]) for row in rows)

    def allocation_caption() -> str:
        planned = planned_weeks()
        remaining = max(budget_weeks - planned, 0)
        return f"{_weeks_label(planned)} Elective · {_weeks_label(remaining)} unscheduled"

    with ui.expansion(
        instance.training_level_name(pgy),
        caption=allocation_caption(),
        icon="school",
        value=expanded,
    ).classes("rbs-pgy-rule rbs-elective-time-level w-full") as level:
        body = ui.column().classes("w-full gap-3")

        def unused_durations(keeping: int | None = None) -> dict[int, str]:
            taken = {int(row["duration_weeks"]) for row in rows} - {keeping}
            return {
                duration: label
                for duration, label in _DURATION_OPTIONS.items()
                if duration not in taken
            }

        def update_budget() -> None:
            planned = planned_weeks()
            remaining = budget_weeks - planned
            level.props["caption"] = allocation_caption()
            overspend.set_text(
                f"{_weeks_label(-remaining)} more than "
                f"{instance.training_level_label(pgy, compact=True)} has left to spend"
                if remaining < 0
                else ""
            )
            overspend.set_visibility(remaining < 0)
            add_button.set_enabled(bool(unused_durations()))
            overspent[pgy] = remaining < 0
            on_change()

        def render_rows() -> None:
            body.clear()
            with body:
                if not rows:
                    ui.label("No Elective time committed for this training level.").classes(
                        "rbs-type-caption rbs-text-muted"
                    )
                for index, row in enumerate(rows):
                    _elective_time_row(
                        rows,
                        row,
                        index,
                        unused_durations(int(row["duration_weeks"])),
                        on_duration_change=render_rows,
                        on_count_change=update_budget,
                    )
            update_budget()

        def add_row() -> None:
            available = unused_durations()
            if not available:
                return
            rows.append(
                {
                    "duration_weeks": (
                        _DEFAULT_BLOCK_DURATION_WEEKS
                        if _DEFAULT_BLOCK_DURATION_WEEKS in available
                        else next(iter(available))
                    ),
                    "count": 1,
                }
            )
            render_rows()

        with ui.row().classes("w-full items-center gap-2"):
            add_button = ui.button(
                "Add block length",
                icon="add",
                on_click=add_row,
            ).props(TERTIARY_BUTTON_PROPS)
        overspend = ui.label().classes("rbs-type-caption rbs-text-danger")

        render_rows()


def _elective_time_row(
    rows: list[Draft],
    row: Draft,
    index: int,
    duration_options: dict[int, str],
    *,
    on_duration_change: Callable[[], None],
    on_count_change: Callable[[], None],
) -> None:
    """One Elective block length and the number of those blocks per resident."""
    from nicegui import ui

    with ui.row().classes("rbs-elective-time-row w-full items-center gap-3"):
        duration = (
            ui.select(
                duration_options,
                value=int(row["duration_weeks"]),
                label="Elective block length",
            )
            .props("outlined dense options-dense")
            .classes("w-full sm:w-48")
        )
        count = (
            ui.number(
                "Blocks per resident",
                value=int(row["count"]),
                min=0,
                precision=0,
                step=1,
            )
            .props("outlined dense")
            .classes("w-full sm:w-48")
        )
        ui.button(
            icon="delete_outline",
            on_click=partial(_remove_elective_time_row, rows, index, on_duration_change),
        ).props(
            button_props(
                DESTRUCTIVE_ICON_BUTTON_PROPS,
                "aria-label='Remove Elective block length'",
            )
        )
    # A changed block length re-keys the allocation, so every other row has to
    # re-render to drop the length this one now holds. A changed count does not.
    duration.on_value_change(partial(_set_elective_time_duration, row, on_duration_change))
    count.on_value_change(partial(_set_elective_time_count, row, on_count_change))


def _set_elective_time_duration(row: Draft, refresh: Callable[[], None], event) -> None:
    row["duration_weeks"] = int(event.value)
    refresh()


def _set_elective_time_count(row: Draft, refresh: Callable[[], None], event) -> None:
    row["count"] = max(0, int(event.value) if event.value is not None else 0)
    refresh()


def _remove_elective_time_row(
    rows: list[Draft],
    index: int,
    refresh: Callable[[], None],
) -> None:
    del rows[index]
    refresh()


def _elective_directory(
    instance: SchedulerInput,
    rotations: list[Rotation],
    *,
    selected_rotation_id: str | None,
    on_select: SelectRotation,
    on_save: SaveRotation,
) -> None:
    from nicegui import ui

    elements = master_detail.directory(
        "Available Electives",
        search_label="Search electives",
        search_placeholder="Code, name, type, training level, or block size",
        action_label="New elective",
        action_icon="add",
        on_action=partial(on_select, NEW_ELECTIVE_ROTATION_ID),
    )
    search = elements.search
    directory = elements.body

    def render_directory() -> None:
        directory.clear()
        query = str(search.value or "").strip().casefold()
        filtered = sorted(
            (
                rotation
                for rotation in rotations
                if not query
                or query in rotation.code.casefold()
                or query in rotation.name.casefold()
                or query
                in (
                    "mandatory service"
                    if rotation.kind is RotationKind.STANDARD
                    else "fmed service"
                    if rotation.kind is RotationKind.FMED
                    else "standalone elective"
                )
                or any(
                    query in instance.training_level_label(rule.pgy).casefold()
                    or query in instance.training_level_label(rule.pgy, compact=True).casefold()
                    or query in f"pgy {rule.pgy}"
                    or query in f"year {rule.pgy}"
                    for rule in rotation.pgy_rules
                )
                or any(
                    query in _weeks_label(size).casefold()
                    for size in instance.eligible_elective_block_sizes(rotation.id)
                )
            ),
            key=rotation_display_sort_key,
        )
        with directory:
            if not filtered:
                empty_configuration = not rotations and not query
                master_detail.empty_directory(
                    icon="add_circle_outline" if empty_configuration else "search_off",
                    title=(
                        "No electives configured"
                        if empty_configuration
                        else "No matching electives"
                    ),
                    description=(
                        "Add an elective or enable one from its Mandatory rotation."
                        if empty_configuration
                        else ("Try a different code, name, type, training level, or block size.")
                    ),
                )
                return
            master_detail.directory_heading("Electives by code", len(filtered))
            with ui.list().props("separator").classes("w-full"):
                for rotation in filtered:
                    _elective_list_item(
                        instance,
                        rotation,
                        selected_rotation_id,
                        on_select,
                    )

    search.on_value_change(lambda: render_directory())
    render_directory()


def _elective_list_item(
    instance: SchedulerInput,
    rotation: Rotation,
    selected_rotation_id: str | None,
    on_select: SelectRotation,
) -> None:
    from nicegui import ui

    classes = master_detail.selected_class(rotation.id == selected_rotation_id)
    with (
        ui.item(on_click=partial(on_select, rotation.id))
        .props("clickable v-ripple")
        .classes(classes)
    ):
        with ui.item_section().props("avatar"):
            with (
                ui.avatar(color=None)
                .props("square")
                .classes("rbs-rotation-code-avatar")
                .style(rotation_code_style(rotation.color))
            ):
                ui.label(rotation.code).classes("rbs-rotation-code-text")
        with ui.item_section():
            ui.item_label(rotation.name).classes("rbs-type-section-title")
            option_type = (
                "Mandatory service"
                if rotation.kind is RotationKind.STANDARD
                else "FMED service"
                if rotation.kind is RotationKind.FMED
                else "Standalone elective"
            )
            sizes = ", ".join(
                _weeks_label(size) for size in instance.eligible_elective_block_sizes(rotation.id)
            )
            levels = ", ".join(
                instance.training_level_label(pgy, compact=True)
                for pgy in instance.eligible_elective_pgys(rotation.id)
            )
            repeat_label = (
                "repeatable"
                if instance.elective_option_is_repeatable(rotation.id)
                else "once per resident"
            )
            grouped_with = sorted(
                {
                    instance.rotation(member).code
                    for group in instance.rotation_groups
                    if group.anchor_rotation_id == rotation.id
                    for member in group.rotation_ids
                    if member != rotation.id
                }
            )
            grouping_label = (
                f" · grouped with {' + '.join(grouped_with)}" if grouped_with else ""
            )
            ui.item_label(
                f"{option_type} · {levels} · {sizes} · {repeat_label}{grouping_label}"
            ).props("caption")
        with ui.item_section().props("side"):
            ui.icon("chevron_right").props("size=20px").classes("rbs-text-subtle")


def _elective_detail_panel(
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
                _elective_rotation_editor(
                    instance,
                    None,
                    on_cancel=partial(on_select, None),
                    on_save=on_save,
                    guard=guard,
                )
                return
            if rotation is None:
                master_detail.empty_detail(
                    icon="school",
                    title=("Elective not found" if missing_id else "Select an elective"),
                    description=("Choose an available elective from the searchable directory."),
                )
                return
            if editing:

                def stop_editing() -> None:
                    nonlocal editing
                    editing = False
                    render_panel()

                if rotation.kind is RotationKind.STANDARD:
                    _rotation_editor(
                        instance,
                        rotation,
                        on_cancel=stop_editing,
                        on_save=on_save,
                        guard=guard,
                    )
                elif rotation.kind is RotationKind.ELECTIVE:
                    _elective_rotation_editor(
                        instance,
                        rotation,
                        on_cancel=stop_editing,
                        on_save=on_save,
                        guard=guard,
                    )
                return

            def edit() -> None:
                nonlocal editing
                if rotation.kind is RotationKind.FMED:
                    _open_fmed_pgy_rules_dialog(
                        instance,
                        rotation.id,
                        selected_rotation_id=rotation.id,
                        on_save=on_save,
                    )
                    return
                editing = True
                render_panel()

            _elective_rotation_view(
                instance,
                rotation,
                on_edit=edit,
                on_select=on_select,
                on_save=on_save,
            )

    render_panel()


def _elective_rotation_view(
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
                if rotation.kind is RotationKind.ELECTIVE:
                    ui.button(
                        icon="delete_outline",
                        on_click=partial(
                            _confirm_remove_elective_rotation,
                            instance,
                            rotation,
                            selected_rotation_id=rotation.id,
                            on_save=on_save,
                        ),
                    ).props(
                        f"{DESTRUCTIVE_ICON_BUTTON_PROPS} "
                        f"aria-label='Delete elective rotation {rotation.name}'"
                    )
                with ui.button(
                    icon="arrow_back",
                    on_click=partial(on_select, None),
                ).props(
                    button_props(
                        ICON_BUTTON_PROPS,
                        "aria-label='Back to elective directory'",
                    )
                ):
                    ui.tooltip("Back to elective directory")
        ui.separator()
        _rotation_detail_contents(instance, rotation)


def _new_elective_rotation_draft(instance: SchedulerInput) -> Draft:
    configured = elective_rotations(instance)
    if configured:
        draft = rotation_editor_state(configured[0])
    else:
        draft = {
            "id": "new_elective",
            "code": "ELEC",
            "name": "New Elective",
            "color": instance.electives.color,
            "kind": RotationKind.ELECTIVE.value,
            "pgy_rules": [
                {
                    "pgy": instance.training_level_ids[0],
                    "min_concurrent": None,
                    "max_concurrent": None,
                    "prerequisite_rotation_ids": [],
                    "earliest_start_week": None,
                    "block_configs": [
                        {
                            "duration_weeks": _DEFAULT_BLOCK_DURATION_WEEKS,
                            "vacation": {"allowed": False},
                        }
                    ],
                }
            ],
            "clinic": _default_clinic_rule(),
            "capacity": {"min_concurrent": None, "max_concurrent": None},
            "away": False,
            "no_clinic_hours": False,
            "no_weekend_call": False,
            "max_consecutive_weeks": 4,
            "max_total_weeks": None,
        }
    draft.update(
        {
            "id": "new_elective",
            "code": "",
            "name": "",
            "color": instance.electives.color,
            "kind": RotationKind.ELECTIVE.value,
            "no_clinic_hours": False,
        }
    )
    if not _draft_has_clinic_configuration(draft):
        draft["clinic"] = _default_clinic_rule()
    return draft


def _elective_rule_pgys(draft: Draft) -> list[int]:
    """Training levels with rules in a standalone-elective draft.

    The editor's per-year availability checkboxes own these; the save passes
    them through as the option's eligible years so newly added years take
    effect instead of lingering on the previous option.
    """
    return sorted({int(rule["pgy"]) for rule in draft.get("pgy_rules", [])})


def _elective_rotation_editor(
    instance: SchedulerInput,
    rotation: Rotation | None,
    *,
    on_cancel: Callable[[], None],
    on_save: SaveRotation,
    guard: RotationEditorGuard | None = None,
) -> None:
    """Edit a standalone Elective in the master-detail workspace.

    A None rotation creates a new Elective with the same full-screen editor,
    so adding and editing share every option and component.
    """
    from nicegui import ui

    creating = rotation is None
    draft = (
        rotation_editor_state(rotation)
        if rotation is not None
        else _new_elective_rotation_draft(instance)
    )
    draft["color"] = instance.electives.color
    draft["kind"] = RotationKind.ELECTIVE.value
    elective_option = instance.electives.option_for(rotation.id) if rotation is not None else None
    elective_draft: Draft = {
        "eligible_block_sizes": (
            list(instance.eligible_elective_block_sizes(rotation.id))
            if rotation is not None
            else list(instance.elective_block_sizes)
        ),
        "repeatable": bool(elective_option and elective_option.repeatable),
    }
    blackout_weeks = set(
        instance.elective_blackout_weeks(rotation.id) if rotation is not None else ()
    )
    group_draft = (
        rotation_group_members_by_pgy(instance, rotation.id)
        if rotation is not None
        else {pgy: [] for pgy in instance.training_level_ids}
    )
    academic_half_day = instance.clinic_policy.recurring_academic_half_day
    site_options = {site.id: site.name for site in instance.clinic_policy.sites}
    default_site_ids = list(instance.clinic_policy.site_ids)
    clinic_editor = None

    def render_clinic_editor() -> None:
        if clinic_editor is None:
            return
        clinic_editor.clear()
        with clinic_editor:
            _clinic_rule_editor(
                draft,
                "clinic",
                enable_label="Schedule continuity clinic during this elective",
                show_enable=False,
                disabled=bool(draft.get("no_clinic_hours")),
                academic_half_day=academic_half_day,
                site_options=site_options,
                default_site_ids=default_site_ids,
            )

    save_error = None

    def save() -> bool:
        try:
            if creating:
                draft["id"] = next_mandatory_rotation_id(
                    instance,
                    str(draft.get("name") or ""),
                )
            draft["color"] = instance.electives.color
            draft["kind"] = RotationKind.ELECTIVE.value
            replacement = rotation_from_editor_state(draft)
            eligible_block_sizes = [
                int(size) for size in elective_draft.get("eligible_block_sizes", [])
            ]
            if not eligible_block_sizes:
                raise ValueError("select at least one eligible Elective block size")
            updated = (
                add_elective_rotation(
                    instance,
                    replacement,
                    eligible_block_sizes=eligible_block_sizes,
                    repeatable=bool(elective_draft.get("repeatable")),
                    blackout_weeks=sorted(blackout_weeks),
                    group_members_by_pgy=group_draft,
                )
                if creating
                else replace_elective_rotation(
                    instance,
                    rotation.id,
                    replacement,
                    eligible_pgys=_elective_rule_pgys(draft),
                    eligible_block_sizes=eligible_block_sizes,
                    repeatable=bool(elective_draft.get("repeatable")),
                    blackout_weeks=sorted(blackout_weeks),
                    group_members_by_pgy=group_draft,
                )
            )
            if save_error is not None:
                save_error.set_text("")
            ui.notify(f"Saved {replacement.code} — {replacement.name}", type="positive")
            on_save(updated, replacement.id)
            return True
        except (ValidationError, ValueError) as exc:
            message = _validation_message(exc)
            if save_error is not None:
                save_error.set_text(message)
            ui.notify(message, type="negative", multi_line=True)
            return False

    def current_editor_state() -> dict:
        return {
            "draft": draft,
            "elective": elective_draft,
            "blackout_weeks": blackout_weeks,
            "group": group_draft,
        }

    initial_editor_state: dict = {}

    if creating or rotation is None:
        subject = "the new elective"
    else:
        subject = f"{rotation.name} ({rotation.code})"

    def discard_to_cancel() -> None:
        if guard is not None:
            guard.clear()
        on_cancel()

    def request_close() -> None:
        if current_editor_state() == initial_editor_state:
            on_cancel()
            return
        _confirm_close_editor(
            subject=subject,
            save_label="Save elective",
            save_icon="save",
            on_discard=discard_to_cancel,
            on_save_click=save,
        )

    with master_detail.detail_card():
        with ui.row().classes(
            "rbs-rotation-detail-header w-full items-center justify-between gap-3 p-5"
        ):
            if creating:
                with ui.column().classes("min-w-0 gap-1"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label("New elective").classes("rbs-type-page-title")
                        ui.badge("Creating", color="secondary").props("outline")
            else:
                _rotation_identity(rotation, instance=instance, editing=True)
            with ui.row().classes("items-center gap-2"):
                ui.button(
                    "Save elective",
                    icon="save",
                    on_click=save,
                ).props("unelevated no-caps")
                with ui.button(icon="close", on_click=request_close).props(
                    button_props(
                        ICON_BUTTON_PROPS,
                        "aria-label='Close elective editor'",
                    )
                ):
                    ui.tooltip("Close elective editor")
        ui.separator()
        with (
            ui.tabs()
            .props("dense no-caps align=left inline-label mobile-arrows outside-arrows")
            .classes("rbs-rotation-editor-tabs w-full") as editor_tabs
        ):
            general_tab = ui.tab("elective_detail_general", label="General", icon="tune")
            pgy_tab = ui.tab(
                "elective_detail_pgy",
                label="Training-level rules",
                icon="groups",
            )
            clinic_tab = ui.tab(
                "elective_detail_clinic",
                label="Clinic",
                icon="event_available",
            )
            availability_tab = ui.tab(
                "elective_detail_availability",
                label="Availability",
                icon="event_busy",
            )

        with (
            ui.tab_panels(editor_tabs, value=general_tab)
            .props("animated")
            .classes("rbs-rotation-editor-panels w-full")
        ):
            with ui.tab_panel(general_tab).classes("p-0"):
                with ui.column().classes("w-full gap-5 p-5"):
                    _core_settings(
                        draft,
                        palette=instance.color_scheme.palette,
                        on_clinic_availability_change=render_clinic_editor,
                        show_color=False,
                        show_max_total_weeks=True,
                    )
                    elective_sizes = (
                        ui.select(
                            _elective_block_size_options(instance.elective_block_sizes),
                            value=list(elective_draft["eligible_block_sizes"]),
                            label="Eligible elective block sizes",
                            multiple=True,
                        )
                        .props("outlined options-dense use-chips")
                        .classes("w-full")
                    )
                    elective_sizes.bind_value(elective_draft, "eligible_block_sizes")
                    with ui.column().classes("rbs-rotation-flags w-full gap-2 rounded p-3"):
                        ui.label("Elective repeat rules").classes("rbs-type-control-label")
                        ui.label(
                            "Leave this off to limit each resident to one block. "
                            "When it is on, use Maximum total weeks above to limit "
                            "their total time on this elective."
                        ).classes("rbs-type-caption rbs-text-muted")
                        repeatable = ui.checkbox(
                            "Can be taken more than once as an elective",
                            value=bool(elective_draft.get("repeatable")),
                        )
                        repeatable.bind_value(elective_draft, "repeatable")

            with ui.tab_panel(pgy_tab).classes("p-0"):
                with ui.column().classes("w-full gap-4 p-5"):
                    ui.label("Elective Rules").classes("rbs-type-section-title")
                    _staffing_and_blocks(
                        instance,
                        draft,
                        rotation.id if rotation is not None else str(draft["id"]),
                        group_members_by_pgy=group_draft,
                    )

            with ui.tab_panel(clinic_tab).classes("p-0"):
                with ui.column().classes("w-full gap-4 p-5"):
                    with ui.column().classes("gap-0"):
                        ui.label("Continuity clinic").classes("rbs-type-section-title")
                    clinic_editor = ui.column().classes("w-full min-w-0 max-w-full")
                    render_clinic_editor()

            with ui.tab_panel(availability_tab).classes("p-0"):
                with ui.column().classes("w-full gap-4 p-5"):
                    with ui.column().classes("gap-0"):
                        ui.label("Elective availability").classes("rbs-type-section-title")
                        ui.label(
                            "Choose every week when this elective may be scheduled. "
                            "Use the block controls to make a whole four-week block "
                            "available or unavailable at once."
                        ).classes("rbs-type-caption rbs-text-muted")
                    _elective_availability_editor(instance, blackout_weeks)

        with ui.row().classes(
            "rbs-rotation-editor-actions w-full items-center justify-end gap-2 px-5 py-3"
        ):
            save_error = ui.label().classes(
                "rbs-rotation-save-error min-w-0 flex-1 rbs-type-caption rbs-text-danger"
            )

    initial_editor_state.update(deepcopy(current_editor_state()))
    if guard is not None:
        guard.is_dirty = lambda: current_editor_state() != initial_editor_state
        guard.save = save
        guard.subject = subject
        guard.save_label = "Save elective"
        guard.save_icon = "save"


def _confirm_remove_elective_rotation(
    instance: SchedulerInput,
    rotation: Rotation,
    *,
    selected_rotation_id: str | None,
    on_save: SaveRotation,
) -> None:
    from nicegui import ui

    with ui.dialog() as dialog, ui.card().classes("w-[min(92vw,520px)] p-5"):
        ui.label(f"Remove {rotation.name}?").classes("rbs-type-dialog-title")
        ui.label(
            "It will no longer be available for elective scheduling. Existing locks "
            "for this option will also be removed."
        ).classes("rbs-type-body rbs-text-muted")
        with ui.row().classes("w-full justify-end gap-3 pt-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")

            def remove() -> None:
                try:
                    updated = remove_elective_rotation(instance, rotation.id)
                    dialog.close()
                    ui.notify(f"Removed {rotation.name}", type="positive")
                    on_save(updated, None)
                except (ValidationError, ValueError) as exc:
                    ui.notify(_validation_message(exc), type="negative", multi_line=True)

            ui.button("Remove elective", icon="delete_outline", on_click=remove).props(
                "unelevated no-caps color=negative"
            )
    dialog.open()
