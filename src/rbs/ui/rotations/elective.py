"""Standalone elective rotation configuration."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from pydantic import ValidationError

from rbs.models.enums import RotationKind
from rbs.models.instance import SchedulerInput
from rbs.models.rotation import (
    Rotation,
)
from rbs.ui import master_detail
from rbs.ui.buttons import (
    DESTRUCTIVE_ICON_BUTTON_PROPS,
    ICON_BUTTON_PROPS,
    PRIMARY_BUTTON_PROPS,
    button_props,
)
from rbs.ui.clinic.ops import (
    _default_clinic_rule,
)
from rbs.ui.drafts import Draft
from rbs.ui.editor_common import (
    _DEFAULT_BLOCK_DURATION_WEEKS,
    _validation_message,
    _weeks_label,
)
from rbs.ui.rotations.fmed import _open_fmed_pgy_rules_dialog
from rbs.ui.rotations.forms import (
    _clinic_rule_editor,
    _core_settings,
    _draft_has_clinic_configuration,
    _rotation_detail_contents,
    _rotation_editor,
    _staffing_and_blocks,
)
from rbs.ui.rotations.ops import (
    add_elective_rotation,
    elective_rotations,
    next_mandatory_rotation_id,
    remove_elective_rotation,
    replace_elective_color,
    replace_elective_rotation,
    rotation_editor_state,
    rotation_from_editor_state,
)
from rbs.ui.rotations.summary import (
    _elective_block_size_options,
    _rotation_identity,
)
from rbs.ui.rotations.types import (
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
) -> None:
    """Render shared Elective policy and a unified option workspace."""
    from nicegui import ui

    available = [
        instance.rotation(option.rotation_id) for option in instance.electives.rotation_options
    ]
    available.sort(key=lambda rotation: rotation.code.casefold())
    selected = next(
        (rotation for rotation in available if rotation.id == selected_rotation_id),
        None,
    )

    with ui.column().classes("w-full gap-4"):
        with master_detail.detail_card():
            with ui.column().classes("w-full gap-3 p-5"):
                with ui.row().classes("w-full items-start justify-between gap-3"):
                    with ui.column().classes("gap-0"):
                        ui.label("Shared elective properties").classes("rbs-type-section-title")
                color_draft: Draft = {"color": instance.electives.color}

                def save_color(color: str) -> None:
                    try:
                        updated = replace_elective_color(instance, color)
                        ui.notify("Elective schedule color updated", type="positive")
                        on_color_save(updated, selected_rotation_id)
                    except (ValidationError, ValueError) as exc:
                        ui.notify(_validation_message(exc), type="negative", multi_line=True)

                rotation_color_palette(
                    color_draft,
                    instance.color_scheme.palette,
                    on_change=save_color,
                )

        with master_detail.split(detail_selected=selected is not None):
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
                missing_id=(
                    selected_rotation_id
                    if selected_rotation_id is not None
                    and selected_rotation_id not in instance.rotations_by_id
                    else None
                ),
                on_select=on_select,
                on_save=on_save,
            )


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
        on_action=partial(
            _open_elective_rotation_dialog,
            instance,
            selected_rotation_id=selected_rotation_id,
            on_save=on_save,
        ),
    )
    search = elements.search
    directory = elements.body

    def render_directory() -> None:
        directory.clear()
        query = str(search.value or "").strip().casefold()
        filtered = [
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
        ]
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
            ui.item_label(f"{option_type} · {levels} · {sizes} · {repeat_label}").props("caption")
        with ui.item_section().props("side"):
            ui.icon("chevron_right").props("size=20px").classes("rbs-text-subtle")


def _elective_detail_panel(
    instance: SchedulerInput,
    *,
    rotation: Rotation | None,
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
                    )
                elif rotation.kind is RotationKind.ELECTIVE:
                    _elective_rotation_editor(
                        instance,
                        rotation,
                        on_cancel=stop_editing,
                        on_save=on_save,
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


def _elective_rotation_editor(
    instance: SchedulerInput,
    rotation: Rotation,
    *,
    on_cancel: Callable[[], None],
    on_save: SaveRotation,
) -> None:
    """Edit an existing standalone Elective in the master-detail workspace."""
    from nicegui import ui

    draft = rotation_editor_state(rotation)
    draft["color"] = instance.electives.color
    draft["kind"] = RotationKind.ELECTIVE.value
    size_draft: Draft = {
        "eligible_block_sizes": list(instance.eligible_elective_block_sizes(rotation.id)),
    }
    academic_half_day = (
        instance.clinic_policy.academic.weekday,
        instance.clinic_policy.academic.session,
    )
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

    def save() -> None:
        try:
            draft["color"] = instance.electives.color
            draft["kind"] = RotationKind.ELECTIVE.value
            replacement = rotation_from_editor_state(draft)
            eligible_block_sizes = [
                int(size) for size in size_draft.get("eligible_block_sizes", [])
            ]
            if not eligible_block_sizes:
                raise ValueError("select at least one eligible Elective block size")
            updated = replace_elective_rotation(
                instance,
                rotation.id,
                replacement,
                eligible_block_sizes=eligible_block_sizes,
            )
            if save_error is not None:
                save_error.set_text("")
            ui.notify(f"Saved {replacement.code} — {replacement.name}", type="positive")
            on_save(updated, replacement.id)
        except (ValidationError, ValueError) as exc:
            message = _validation_message(exc)
            if save_error is not None:
                save_error.set_text(message)
            ui.notify(message, type="negative", multi_line=True)

    with master_detail.detail_card():
        with ui.row().classes(
            "rbs-rotation-detail-header w-full items-center justify-between gap-3 p-5"
        ):
            _rotation_identity(rotation, instance=instance, editing=True)
            with ui.button(icon="close", on_click=on_cancel).props(
                button_props(
                    ICON_BUTTON_PROPS,
                    "aria-label='Cancel elective editing'",
                )
            ):
                ui.tooltip("Cancel elective editing")
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
                            value=list(size_draft["eligible_block_sizes"]),
                            label="Eligible elective block sizes",
                            multiple=True,
                        )
                        .props("outlined options-dense use-chips")
                        .classes("w-full")
                    )
                    elective_sizes.bind_value(size_draft, "eligible_block_sizes")

            with ui.tab_panel(pgy_tab).classes("p-0"):
                with ui.column().classes("w-full gap-4 p-5"):
                    ui.label("Elective Rules").classes("rbs-type-section-title")
                    _staffing_and_blocks(instance, draft, rotation.id)

            with ui.tab_panel(clinic_tab).classes("p-0"):
                with ui.column().classes("w-full gap-4 p-5"):
                    with ui.column().classes("gap-0"):
                        ui.label("Continuity clinic").classes("rbs-type-section-title")
                    clinic_editor = ui.column().classes("w-full min-w-0 max-w-full")
                    render_clinic_editor()

        with ui.row().classes(
            "rbs-rotation-editor-actions w-full items-center justify-end gap-2 px-5 py-3"
        ):
            save_error = ui.label().classes(
                "rbs-rotation-save-error min-w-0 flex-1 rbs-type-caption rbs-text-danger"
            )
            ui.button("Save elective", icon="save", on_click=save).props("unelevated no-caps")


def _open_elective_rotation_dialog(
    instance: SchedulerInput,
    *,
    selected_rotation_id: str | None,
    on_save: SaveRotation,
    initial: Rotation | None = None,
) -> None:
    """Edit a standalone Elective with the shared rotation-rule fields."""
    from nicegui import ui

    draft = (
        rotation_editor_state(initial)
        if initial is not None
        else _new_elective_rotation_draft(instance)
    )
    draft["color"] = instance.electives.color
    draft["kind"] = RotationKind.ELECTIVE.value
    available_elective_sizes = instance.elective_block_sizes
    configured_elective_sizes = (
        instance.eligible_elective_block_sizes(initial.id)
        if initial is not None and instance.is_elective_option(initial.id)
        else available_elective_sizes
    )
    elective_size_draft: Draft = {
        "eligible_block_sizes": list(configured_elective_sizes),
    }
    academic_half_day = (
        instance.clinic_policy.academic.weekday,
        instance.clinic_policy.academic.session,
    )
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

    title = "Edit elective rotation" if initial is not None else "New elective rotation"
    with (
        ui.dialog() as dialog,
        (
            ui.card()
            .classes("rbs-elective-editor-dialog p-0 gap-0")
            .style("width:calc(100vw - 32px);max-width:1200px;max-height:calc(100vh - 32px)")
        ),
    ):
        with ui.row().classes("rbs-clinic-editor-header w-full items-center gap-5 px-5 py-4"):
            ui.label(title).classes(
                "rbs-clinic-editor-title rbs-type-dialog-title whitespace-nowrap"
            )
            with (
                ui.tabs()
                .props("dense no-caps inline-label align=left mobile-arrows outside-arrows")
                .classes("rbs-clinic-editor-tabs min-w-0") as tabs
            ):
                general_tab = ui.tab("elective_general", label="General", icon="tune")
                pgy_tab = ui.tab(
                    "elective_pgy",
                    label="Training-level rules",
                    icon="groups",
                )
                clinic_tab = ui.tab(
                    "elective_clinic",
                    label="Clinic",
                    icon="event_available",
                )
            ui.space()
            ui.button(icon="close", on_click=dialog.close).props(
                "flat round dense aria-label='Close elective rotation dialog'"
            )
        with ui.scroll_area().classes("rbs-elective-editor-scroll w-full h-[min(68vh,800px)]"):
            with ui.tab_panels(tabs, value=general_tab).classes(
                "rbs-elective-editor-panels w-full min-w-0 max-w-full"
            ):
                with ui.tab_panel(general_tab).classes("p-5"):
                    _core_settings(
                        draft,
                        palette=instance.color_scheme.palette,
                        on_clinic_availability_change=render_clinic_editor,
                        show_color=False,
                        show_max_total_weeks=True,
                    )
                    elective_sizes = (
                        ui.select(
                            _elective_block_size_options(available_elective_sizes),
                            value=list(elective_size_draft["eligible_block_sizes"]),
                            label="Eligible elective block sizes",
                            multiple=True,
                        )
                        .props("outlined options-dense use-chips")
                        .classes("w-full")
                    )
                    elective_sizes.bind_value(
                        elective_size_draft,
                        "eligible_block_sizes",
                    )
                    elective_sizes.set_enabled(
                        bool(available_elective_sizes)
                        and (initial is None or instance.is_elective_option(initial.id))
                    )
                with ui.tab_panel(pgy_tab).classes("p-5"):
                    ui.label("Elective Rules").classes("rbs-type-section-title")
                    _staffing_and_blocks(instance, draft, str(draft["id"]))
                with ui.tab_panel(clinic_tab).classes("min-w-0 max-w-full p-5"):
                    clinic_editor = ui.column().classes("w-full min-w-0 max-w-full")
                    render_clinic_editor()
        ui.separator()
        with ui.row().classes("w-full items-center justify-end gap-3 p-4"):

            def save() -> None:
                try:
                    if initial is None:
                        draft["id"] = next_mandatory_rotation_id(
                            instance,
                            str(draft.get("name") or ""),
                        )
                    draft["color"] = instance.electives.color
                    draft["kind"] = RotationKind.ELECTIVE.value
                    replacement = rotation_from_editor_state(draft)
                    eligible_block_sizes = [
                        int(size) for size in elective_size_draft.get("eligible_block_sizes", [])
                    ]
                    if not eligible_block_sizes:
                        raise ValueError("select at least one eligible Elective block size")
                    updated = (
                        add_elective_rotation(
                            instance,
                            replacement,
                            eligible_block_sizes=eligible_block_sizes,
                        )
                        if initial is None
                        else replace_elective_rotation(
                            instance,
                            initial.id,
                            replacement,
                            eligible_block_sizes=eligible_block_sizes,
                        )
                    )
                    dialog.close()
                    ui.notify(f"Saved {replacement.code} — {replacement.name}", type="positive")
                    on_save(updated, replacement.id)
                except (ValidationError, ValueError) as exc:
                    ui.notify(_validation_message(exc), type="negative", multi_line=True)

            ui.button("Save elective rotation", icon="save", on_click=save).props(
                "unelevated no-caps"
            )
    dialog.open()


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
