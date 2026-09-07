"""Resident rotation-override views and dialogs."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from uuid import uuid4

from rbs.models.enums import RotationKind
from rbs.models.instance import SchedulerInput
from rbs.models.resident import resident_display_sort_key
from rbs.models.rotation import (
    Rotation,
    rotation_display_sort_key,
)
from rbs.ui.drafts import Draft
from rbs.ui.editor_common import (
    _default_block_duration,
    _weeks_label,
)
from rbs.ui.rotations.summary import _rotation_overview_row


def _resident_rotation_overrides_view(
    instance: SchedulerInput,
    rotation: Rotation,
) -> None:
    from nicegui import ui

    overrides = [
        override
        for override in instance.resident_rotation_overrides
        if override.rotation_id == rotation.id
    ]
    if not overrides:
        return
    residents = instance.residents_by_id
    rotations = instance.rotations_by_id
    waivers = [
        waiver for waiver in instance.resident_rotation_waivers if waiver.rotation_id == rotation.id
    ]
    if not overrides and not waivers:
        return
    with ui.card().props("flat bordered").classes("rbs-rotation-overview-card w-full gap-3 p-4"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.icon("person_add").classes("rbs-text-primary")
            ui.label("Individual exceptions").classes("rbs-type-control-label")
        for override in overrides:
            resident = residents[override.resident_id]
            funded_by = (
                "unallocated time"
                if override.replaces_rotation_id is None
                else f"replaces {rotations[override.replaces_rotation_id].code}"
            )
            _rotation_overview_row(
                f"{resident.name} · {instance.training_level_name(resident.pgy)}",
                f"{_weeks_label(override.duration_weeks)} extra · {funded_by}",
                icon="person",
            )
        for waiver in waivers:
            resident = residents[waiver.resident_id]
            _rotation_overview_row(
                f"{resident.name} · {instance.training_level_name(resident.pgy)}",
                f"{_weeks_label(waiver.duration_weeks)} excused · skips {rotation.code}",
                icon="person_off",
            )


def _resident_override_elective_options(
    instance: SchedulerInput,
    rotation: Rotation,
    override_drafts: list[Draft],
    resident_id: str,
    duration_weeks: int,
) -> dict[str, str]:
    resident = next(item for item in instance.residents if item.id == resident_id)
    used: dict[str, int] = {}
    for manual in instance.manual_clinic_blocks:
        if manual.resident_id == resident_id and manual.duration_weeks == duration_weeks:
            used[manual.replaces_rotation_id] = used.get(manual.replaces_rotation_id, 0) + 1
    for override in instance.resident_rotation_overrides:
        if (
            not _editor_manages_resident_override(instance, override, rotation.id)
            and override.resident_id == resident_id
            and override.duration_weeks == duration_weeks
        ):
            used[override.replaces_rotation_id] = used.get(override.replaces_rotation_id, 0) + 1
    for override in override_drafts:
        if (
            str(override["resident_id"]) == resident_id
            and int(override["duration_weeks"]) == duration_weeks
        ):
            replacement_id = str(override["replaces_rotation_id"])
            used[replacement_id] = used.get(replacement_id, 0) + 1

    options: list[tuple[tuple[str, str, str], str, str]] = []
    for block in instance.curriculum_for(resident.pgy).blocks:
        elective = instance.rotation(block.rotation_id)
        if (
            block.duration_weeks != duration_weeks
            or elective.kind is not RotationKind.ELECTIVE
            or block.count <= used.get(block.rotation_id, 0)
        ):
            continue
        options.append(
            (
                rotation_display_sort_key(elective),
                f"{elective.code} — {elective.name}",
                elective.id,
            )
        )
    return {elective_id: label for _sort, label, elective_id in sorted(options)}


_UNALLOCATED_FUNDING_KEY = "unallocated"
"""Widget-only sentinel for unallocated-funded overrides; drafts store None."""


def _resident_override_unallocated_remaining(
    instance: SchedulerInput,
    override_drafts: list[Draft],
    resident_id: str,
) -> int:
    """Unallocated weeks left in the resident's training level.

    Accounts for saved unallocated-funded extras and unsaved drafts sharing
    the training-level pool.
    """
    resident = next(item for item in instance.residents if item.id == resident_id)
    residents = instance.residents_by_id
    used = 0
    for override in instance.resident_rotation_overrides:
        if override.replaces_rotation_id is not None:
            continue
        saved = residents.get(override.resident_id)
        if saved is not None and saved.pgy == resident.pgy:
            used += override.duration_weeks
    for draft in override_drafts:
        if draft.get("replaces_rotation_id") is not None:
            continue
        drafted = residents.get(str(draft["resident_id"]))
        if drafted is not None and drafted.pgy == resident.pgy:
            used += int(draft["duration_weeks"])
    return instance.unallocated_weeks(resident.pgy) - used


def _resident_override_funding_options(
    instance: SchedulerInput,
    rotation: Rotation,
    override_drafts: list[Draft],
    resident_id: str,
    duration_weeks: int,
) -> list[tuple[str | None, str]]:
    """Compatible funding, elective blocks first, then unallocated time.

    Each entry is a ``replaces_rotation_id`` (None funds from unallocated
    weeks) paired with its display label.
    """
    options = [
        (elective_id, label)
        for elective_id, label in _resident_override_elective_options(
            instance,
            rotation,
            override_drafts,
            resident_id,
            duration_weeks,
        ).items()
    ]
    remaining = _resident_override_unallocated_remaining(
        instance,
        override_drafts,
        resident_id,
    )
    if remaining >= duration_weeks:
        options.append((None, f"Unallocated time ({remaining} weeks available)"))
    return options


def _editor_manages_resident_override(
    instance: SchedulerInput,
    override,
    rotation_id: str,
) -> bool:
    if override.rotation_id == rotation_id:
        return True
    if override.group_instance_id is None:
        return False
    resident = instance.residents_by_id.get(override.resident_id)
    if resident is None:
        return False
    group = instance.rotation_group_for(resident.pgy, rotation_id)
    return group is not None and override.rotation_id in group.rotation_ids


def _resident_override_group_bundle(
    instance: SchedulerInput,
    rotation: Rotation,
    override_drafts: list[Draft],
    resident_id: str,
    selected_duration: int,
) -> list[Draft] | None:
    resident = instance.residents_by_id[resident_id]
    group = instance.rotation_group_for(resident.pgy, rotation.id)
    if group is None:
        return None
    bundle: list[Draft] = []
    instance_id = uuid4().hex
    for rotation_id in group.rotation_ids:
        member = instance.rotation(rotation_id)
        durations = (
            [selected_duration]
            if rotation_id == rotation.id
            else sorted(member.configured_durations(resident.pgy))
        )
        chosen = None
        for duration in durations:
            funding = _resident_override_funding_options(
                instance,
                rotation,
                [*override_drafts, *bundle],
                resident_id,
                duration,
            )
            if not funding:
                continue
            replaces_id, _label = funding[0]
            chosen = {
                "resident_id": resident_id,
                "rotation_id": rotation_id,
                "duration_weeks": duration,
                "replaces_rotation_id": replaces_id,
                "group_instance_id": instance_id,
            }
            break
        if chosen is None:
            return None
        bundle.append(chosen)
    return bundle


def _resident_override_duration_options(
    instance: SchedulerInput,
    rotation: Rotation,
    override_drafts: list[Draft],
    resident_id: str,
) -> dict[int, str]:
    resident = next(item for item in instance.residents if item.id == resident_id)
    try:
        rule = rotation.pgy_rule(resident.pgy)
    except KeyError:
        return {}
    return {
        config.duration_weeks: _weeks_label(config.duration_weeks)
        for config in rule.block_configs
        if _resident_override_funding_options(
            instance,
            rotation,
            override_drafts,
            resident_id,
            config.duration_weeks,
        )
    }


def _resident_override_resident_options(
    instance: SchedulerInput,
    rotation: Rotation,
    override_drafts: list[Draft],
) -> dict[str, str]:
    return {
        resident.id: (f"{resident.name} · {instance.training_level_name(resident.pgy)}")
        for resident in sorted(instance.residents, key=resident_display_sort_key)
        if _resident_override_duration_options(
            instance,
            rotation,
            override_drafts,
            resident.id,
        )
    }


def _open_resident_rotation_override_dialog(
    instance: SchedulerInput,
    rotation: Rotation,
    override_drafts: list[Draft],
    refresh: Callable[[], None],
) -> None:
    from nicegui import ui

    residents = _resident_override_resident_options(
        instance,
        rotation,
        override_drafts,
    )
    if not residents:
        ui.notify("No resident has Elective or unallocated time available", type="warning")
        return
    resident_id = next(iter(residents))
    durations = _resident_override_duration_options(
        instance,
        rotation,
        override_drafts,
        resident_id,
    )

    def funding_option_key(replaces_id: str | None) -> str:
        return _UNALLOCATED_FUNDING_KEY if replaces_id is None else replaces_id

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl p-0 gap-0"):
        with ui.row().classes("w-full items-center justify-between gap-3 px-5 py-4"):
            ui.label(f"Add resident override · {rotation.name}").classes("rbs-type-dialog-title")
            ui.button(icon="close", on_click=dialog.close).props(
                "flat round dense aria-label='Close resident override dialog'"
            )
        ui.separator()
        with ui.column().classes("w-full gap-4 p-5"):
            ui.label(
                "Adds one Mandatory block for the selected resident. A same-length "
                "Elective block is replaced automatically, or the block is funded "
                "from unallocated time."
            ).classes("rbs-type-body rbs-text-muted")
            resident_select = (
                ui.select(
                    residents,
                    value=resident_id,
                    label="Resident",
                )
                .props("outlined options-dense use-input")
                .classes("w-full")
            )
            duration_select = (
                ui.select(
                    durations,
                    value=_default_block_duration(durations),
                    label="Block length",
                )
                .props("outlined options-dense")
                .classes("w-full")
            )
            funding_select = (
                ui.select(
                    {},
                    value=None,
                    label="Funded by",
                )
                .props("outlined options-dense")
                .classes("w-full")
            )
            group_choice = ui.radio(
                {
                    "group": "Add the complete rotation group",
                    "unmatched": "Add an unmatched extra",
                },
                value="unmatched",
            ).props("inline")
            group_choice_help = ui.label().classes("rbs-type-caption rbs-text-muted")
            group_choice.on_value_change(
                lambda _event: funding_select.set_visibility(group_choice.value != "group")
            )

        def refresh_funding() -> None:
            if resident_select.value is None or duration_select.value is None:
                funding_select.set_options({}, value=None)
                return
            funding = _resident_override_funding_options(
                instance,
                rotation,
                override_drafts,
                str(resident_select.value),
                int(duration_select.value),
            )
            options = {funding_option_key(replaces_id): label for replaces_id, label in funding}
            value = (
                str(funding_select.value)
                if funding_select.value in options
                else next(iter(options), None)
            )
            funding_select.set_options(options, value=value)

        def refresh_group_choice() -> None:
            refresh_funding()
            if resident_select.value is None or duration_select.value is None:
                group_choice.set_visibility(False)
                group_choice_help.set_visibility(False)
                funding_select.set_visibility(False)
                return
            selected_resident = str(resident_select.value)
            resident = instance.residents_by_id[selected_resident]
            group = instance.rotation_group_for(resident.pgy, rotation.id)
            if group is None:
                group_choice.value = "unmatched"
                group_choice.set_visibility(False)
                group_choice_help.set_visibility(False)
                funding_select.set_visibility(True)
                return
            bundle = _resident_override_group_bundle(
                instance,
                rotation,
                override_drafts,
                selected_resident,
                int(duration_select.value),
            )
            group_choice.set_visibility(True)
            group_choice_help.set_visibility(True)
            if bundle is None:
                group_choice.value = "unmatched"
                group_choice.set_options(
                    {"unmatched": "Add an unmatched extra"},
                    value="unmatched",
                )
                group_choice_help.set_text(
                    "A complete group cannot be added because this resident does not "
                    "have enough compatible Elective or unallocated time to fund every member."
                )
                funding_select.set_visibility(True)
                return
            funding_select.set_visibility(group_choice.value != "group")
            group_choice.set_options(
                {
                    "group": "Add the complete rotation group",
                    "unmatched": "Add an unmatched extra",
                },
                value="group",
            )
            codes = " + ".join(instance.rotation(str(item["rotation_id"])).code for item in bundle)
            group_choice_help.set_text(
                f"Complete group: {codes}. Grouped extras are scheduled contiguously; "
                "an unmatched extra is independent."
            )

        def change_resident(_event) -> None:
            available = _resident_override_duration_options(
                instance,
                rotation,
                override_drafts,
                str(resident_select.value),
            )
            duration_select.set_options(
                available,
                value=_default_block_duration(available),
            )
            refresh_group_choice()

        resident_select.on_value_change(change_resident)
        duration_select.on_value_change(lambda _event: refresh_group_choice())
        refresh_group_choice()

        ui.separator()
        with ui.row().classes("w-full justify-end gap-3 p-4"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")

            def add_override() -> None:
                if resident_select.value is None or duration_select.value is None:
                    ui.notify("Select a resident and block length", type="negative")
                    return
                selected_resident = str(resident_select.value)
                selected_duration = int(duration_select.value)
                if group_choice.value == "group":
                    bundle = _resident_override_group_bundle(
                        instance,
                        rotation,
                        override_drafts,
                        selected_resident,
                        selected_duration,
                    )
                    if bundle is None:
                        ui.notify(
                            "The complete group no longer has enough compatible Elective "
                            "or unallocated time",
                            type="negative",
                        )
                        return
                    override_drafts.extend(bundle)
                else:
                    funding_value = (
                        None if funding_select.value is None else str(funding_select.value)
                    )
                    if funding_value == _UNALLOCATED_FUNDING_KEY:
                        replaces_id: str | None = None
                        if (
                            _resident_override_unallocated_remaining(
                                instance,
                                override_drafts,
                                selected_resident,
                            )
                            < selected_duration
                        ):
                            ui.notify(
                                "Not enough unallocated time remains",
                                type="negative",
                            )
                            return
                    else:
                        replaces_id = funding_value
                        electives = _resident_override_elective_options(
                            instance,
                            rotation,
                            override_drafts,
                            selected_resident,
                            selected_duration,
                        )
                        if replaces_id not in electives:
                            ui.notify(
                                "No same-length Elective block remains available",
                                type="negative",
                            )
                            return
                    override_drafts.append(
                        {
                            "resident_id": selected_resident,
                            "rotation_id": rotation.id,
                            "duration_weeks": selected_duration,
                            "replaces_rotation_id": replaces_id,
                            "group_instance_id": None,
                        }
                    )
                dialog.close()
                refresh()

            ui.button("Add override", icon="add", on_click=add_override).props("unelevated no-caps")
    dialog.open()


def _resident_waiver_duration_options(
    instance: SchedulerInput,
    rotation: Rotation,
    waiver_drafts: list[Draft],
    resident_id: str,
) -> dict[int, str]:
    resident = next(item for item in instance.residents if item.id == resident_id)
    waived: dict[int, int] = {}
    for draft in waiver_drafts:
        if str(draft["resident_id"]) == resident_id:
            duration = int(draft["duration_weeks"])
            waived[duration] = waived.get(duration, 0) + 1
    return {
        duration: _weeks_label(duration)
        for duration in sorted(
            {
                block.duration_weeks
                for block in instance.curriculum_for(resident.pgy).blocks
                if block.rotation_id == rotation.id
            }
        )
        if (
            sum(
                block.count
                for block in instance.curriculum_for(resident.pgy).blocks
                if block.rotation_id == rotation.id and block.duration_weeks == duration
            )
            - waived.get(duration, 0)
            > 0
        )
    }


def _resident_waiver_resident_options(
    instance: SchedulerInput,
    rotation: Rotation,
    waiver_drafts: list[Draft],
) -> dict[str, str]:
    return {
        resident.id: (f"{resident.name} · {instance.training_level_name(resident.pgy)}")
        for resident in sorted(instance.residents, key=resident_display_sort_key)
        if _resident_waiver_duration_options(
            instance,
            rotation,
            waiver_drafts,
            resident.id,
        )
    }


def _open_resident_rotation_waiver_dialog(
    instance: SchedulerInput,
    rotation: Rotation,
    waiver_drafts: list[Draft],
    refresh: Callable[[], None],
) -> None:
    from nicegui import ui

    residents = _resident_waiver_resident_options(
        instance,
        rotation,
        waiver_drafts,
    )
    if not residents:
        ui.notify("No resident has this requirement available to waive", type="warning")
        return
    resident_id = next(iter(residents))
    durations = _resident_waiver_duration_options(
        instance,
        rotation,
        waiver_drafts,
        resident_id,
    )

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl p-0 gap-0"):
        with ui.row().classes("w-full items-center justify-between gap-3 px-5 py-4"):
            ui.label(f"Waive requirement · {rotation.name}").classes("rbs-type-dialog-title")
            ui.button(icon="close", on_click=dialog.close).props(
                "flat round dense aria-label='Close resident waiver dialog'"
            )
        ui.separator()
        with ui.column().classes("w-full gap-4 p-5"):
            ui.label(
                "Excuses one resident from one required block without changing the "
                "training-level curriculum. The freed weeks become unscheduled time."
            ).classes("rbs-type-body rbs-text-muted")
            resident_select = (
                ui.select(
                    residents,
                    value=resident_id,
                    label="Resident",
                )
                .props("outlined options-dense use-input")
                .classes("w-full")
            )
            duration_select = (
                ui.select(
                    durations,
                    value=_default_block_duration(durations),
                    label="Block length",
                )
                .props("outlined options-dense")
                .classes("w-full")
            )

        def change_resident(_event) -> None:
            available = _resident_waiver_duration_options(
                instance,
                rotation,
                waiver_drafts,
                str(resident_select.value),
            )
            duration_select.set_options(
                available,
                value=_default_block_duration(available),
            )

        resident_select.on_value_change(change_resident)

        ui.separator()
        with ui.row().classes("w-full justify-end gap-3 p-4"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")

            def add_waiver() -> None:
                if resident_select.value is None or duration_select.value is None:
                    ui.notify("Select a resident and block length", type="negative")
                    return
                available = _resident_waiver_duration_options(
                    instance,
                    rotation,
                    waiver_drafts,
                    str(resident_select.value),
                )
                if int(duration_select.value) not in available:
                    ui.notify(
                        "That requirement is no longer available to waive",
                        type="negative",
                    )
                    return
                waiver_drafts.append(
                    {
                        "resident_id": str(resident_select.value),
                        "rotation_id": rotation.id,
                        "duration_weeks": int(duration_select.value),
                    }
                )
                dialog.close()
                refresh()

            ui.button("Add waiver", icon="add", on_click=add_waiver).props("unelevated no-caps")
    dialog.open()


def _resident_rotation_overrides_editor(
    instance: SchedulerInput,
    rotation: Rotation,
    override_drafts: list[Draft],
    waiver_drafts: list[Draft],
) -> None:
    from nicegui import ui

    container = ui.column().classes("w-full gap-3")

    def render() -> None:
        container.clear()
        residents = instance.residents_by_id
        rotations = instance.rotations_by_id
        with container:
            ui.label(
                "Add a Mandatory block for one resident, funded by an Elective "
                "block or unallocated time, or excuse one resident from a "
                "required block — all without changing the training-level curriculum."
            ).classes("rbs-type-body rbs-text-muted")
            if not override_drafts and not waiver_drafts:
                ui.label("No resident-specific exceptions.").classes("rbs-type-body rbs-text-muted")

            def remove_override(index: int) -> None:
                target = override_drafts[index]
                instance_id = target.get("group_instance_id")
                if instance_id is None:
                    override_drafts.pop(index)
                else:
                    resident_id = str(target["resident_id"])
                    override_drafts[:] = [
                        item
                        for item in override_drafts
                        if not (
                            str(item["resident_id"]) == resident_id
                            and item.get("group_instance_id") == instance_id
                        )
                    ]
                render()

            for index, override in enumerate(override_drafts):
                resident = residents[str(override["resident_id"])]
                replaces_id = override.get("replaces_rotation_id")
                funded_by = (
                    "unallocated time"
                    if replaces_id is None
                    else f"replaces {rotations[str(replaces_id)].code}"
                )
                target_rotation = rotations[str(override["rotation_id"])]
                with ui.row().classes(
                    "rbs-resident-rotation-override w-full items-center gap-3 rounded p-3"
                ):
                    with ui.column().classes("min-w-0 flex-1 gap-0"):
                        ui.label(
                            f"{resident.name} · {instance.training_level_name(resident.pgy)}"
                        ).classes("rbs-font-semibold")
                        ui.label(
                            f"{target_rotation.code} · "
                            f"{_weeks_label(int(override['duration_weeks']))} extra "
                            f"· {funded_by}"
                            + (
                                " · complete group"
                                if override.get("group_instance_id") is not None
                                else " · unmatched"
                            )
                        ).classes("rbs-type-caption rbs-text-muted")
                    ui.button(
                        icon="delete_outline",
                        on_click=partial(remove_override, index),
                    ).props(
                        "flat round dense color=negative "
                        "aria-label='Remove resident rotation override'"
                    )

            def remove_waiver(index: int) -> None:
                waiver_drafts.pop(index)
                render()

            for index, waiver in enumerate(waiver_drafts):
                resident = residents[str(waiver["resident_id"])]
                with ui.row().classes(
                    "rbs-resident-rotation-waiver w-full items-center gap-3 rounded p-3"
                ):
                    with ui.column().classes("min-w-0 flex-1 gap-0"):
                        ui.label(
                            f"{resident.name} · {instance.training_level_name(resident.pgy)}"
                        ).classes("rbs-font-semibold")
                        ui.label(
                            f"{rotation.code} · "
                            f"{_weeks_label(int(waiver['duration_weeks']))} excused"
                        ).classes("rbs-type-caption rbs-text-muted")
                    ui.button(
                        icon="delete_outline",
                        on_click=partial(remove_waiver, index),
                    ).props(
                        "flat round dense color=negative "
                        "aria-label='Remove resident rotation waiver'"
                    )
            with ui.row().classes("w-full items-center gap-3 flex-wrap"):
                add_button = ui.button(
                    "Add resident override",
                    icon="person_add",
                    on_click=partial(
                        _open_resident_rotation_override_dialog,
                        instance,
                        rotation,
                        override_drafts,
                        render,
                    ),
                ).props("outline no-caps")
                add_button.set_enabled(
                    bool(
                        _resident_override_resident_options(
                            instance,
                            rotation,
                            override_drafts,
                        )
                    )
                )
                waive_button = ui.button(
                    "Waive requirement",
                    icon="person_off",
                    on_click=partial(
                        _open_resident_rotation_waiver_dialog,
                        instance,
                        rotation,
                        waiver_drafts,
                        render,
                    ),
                ).props("outline no-caps")
                waive_button.set_enabled(
                    bool(
                        _resident_waiver_resident_options(
                            instance,
                            rotation,
                            waiver_drafts,
                        )
                    )
                )

    render()
