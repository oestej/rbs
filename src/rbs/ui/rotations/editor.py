"""Structured editor for standard rotation definitions."""

from __future__ import annotations

from functools import partial

from rbs.models.curriculum import (
    default_training_level_code,
)
from rbs.models.enums import RotationKind
from rbs.models.instance import SchedulerInput
from rbs.models.rotation import (
    Rotation,
)
from rbs.models.schedule import Schedule
from rbs.ui import master_detail, page_shells
from rbs.ui.rotations.academic import (
    _academic_configuration,
    _academic_week_label,
    _academic_week_options,
)
from rbs.ui.rotations.elective import (
    _confirm_remove_elective_rotation,
    _elective_configuration,
    _elective_detail_panel,
    _elective_directory,
    _elective_list_item,
    _elective_rotation_editor,
    _elective_rotation_view,
    _new_elective_rotation_draft,
    _open_elective_rotation_dialog,
)
from rbs.ui.rotations.fmed import (
    _dedicated_rotation_cards,
    _fmed_clinic_concurrency_editor,
    _open_fmed_pgy_rules_dialog,
    _set_fmed_pgy_clinic_limit,
)
from rbs.ui.rotations.forms import (
    _apply_away_selection,
    _apply_clinic_sites,
    _block_config_editor,
    _change_required_block_duration,
    _clinic_rule_editor,
    _core_settings,
    _direct_elective_weeks,
    _draft_has_clinic_configuration,
    _draft_requirement_label,
    _mandatory_elective_availability,
    _open_bulk_clinic_sites_dialog,
    _pgy_rule_editor,
    _remove_block_config,
    _remove_required_block_config,
    _rotation_detail_contents,
    _rotation_editor,
    _rotation_group_editor,
    _rotation_group_member_options,
    _set_required_block_count,
    _set_unique_clinic_slots,
    _staffing_and_blocks,
)
from rbs.ui.rotations.mandatory import (
    _confirm_remove_mandatory_rotation,
    _new_mandatory_rotation_form,
    _rotation_detail_panel,
    _rotation_view,
)
from rbs.ui.rotations.ops import (
    standard_rotations,
)
from rbs.ui.rotations.overrides import (
    _editor_manages_resident_override,
    _open_resident_rotation_override_dialog,
    _resident_override_duration_options,
    _resident_override_elective_options,
    _resident_override_group_bundle,
    _resident_override_resident_options,
    _resident_rotation_overrides_editor,
    _resident_rotation_overrides_view,
)
from rbs.ui.rotations.special import (
    _SPECIAL_EVENT_TIME_OPTIONS,
    _open_special_rotation_dialog,
    _special_configuration,
    _special_rotation_period_label,
    _special_rotation_row,
    _special_type_card,
)
from rbs.ui.rotations.summary import (
    _clinic_slot_label,
    _configured_duration_label,
    _configured_training_level_label,
    _elective_block_size_options,
    _elective_policy_summary_chips,
    _eligible_elective_block_size_label,
    _missing_mandatory_html,
    _resident_time_off_label,
    _rotation_clinic_overview,
    _rotation_identity,
    _rotation_kind_label,
    _rotation_operational_overview,
    _rotation_overview_row,
    _rotation_pgy_overview,
    _rotation_requirement_label,
    _rotation_summary,
    _rotation_summary_cell_class,
    _rotation_summary_chip,
    _rotation_summary_html,
    _rotation_total_label,
    _week_total_label,
)
from rbs.ui.rotations.types import (
    NEW_MANDATORY_ROTATION_ID,
    SaveRotation,
    SelectRotation,
)
from rbs.ui.rotations.widgets import rotation_code_style

__all__ = [
    "SelectRotation",
    "SaveRotation",
    "NEW_MANDATORY_ROTATION_ID",
    "_rotation_summary",
    "_rotation_summary_html",
    "_rotation_summary_cell_class",
    "_missing_mandatory_html",
    "_week_total_label",
    "_resident_time_off_label",
    "_rotation_total_label",
    "_rotation_kind_label",
    "_configured_training_level_label",
    "_configured_duration_label",
    "_eligible_elective_block_size_label",
    "_elective_policy_summary_chips",
    "_elective_block_size_options",
    "_rotation_identity",
    "_rotation_requirement_label",
    "_rotation_summary_chip",
    "_rotation_overview_row",
    "_rotation_pgy_overview",
    "_clinic_slot_label",
    "_rotation_clinic_overview",
    "_rotation_operational_overview",
    "_resident_rotation_overrides_view",
    "_resident_override_elective_options",
    "_editor_manages_resident_override",
    "_resident_override_group_bundle",
    "_resident_override_duration_options",
    "_resident_override_resident_options",
    "_open_resident_rotation_override_dialog",
    "_resident_rotation_overrides_editor",
    "_rotation_editor",
    "_core_settings",
    "_staffing_and_blocks",
    "_pgy_rule_editor",
    "_rotation_group_editor",
    "_rotation_group_member_options",
    "_draft_requirement_label",
    "_block_config_editor",
    "_clinic_rule_editor",
    "_set_unique_clinic_slots",
    "_apply_clinic_sites",
    "_open_bulk_clinic_sites_dialog",
    "_draft_has_clinic_configuration",
    "_apply_away_selection",
    "_remove_block_config",
    "_set_required_block_count",
    "_change_required_block_duration",
    "_remove_required_block_config",
    "_mandatory_elective_availability",
    "_direct_elective_weeks",
    "_rotation_detail_contents",
    "_rotation_detail_panel",
    "_new_mandatory_rotation_form",
    "_rotation_view",
    "_confirm_remove_mandatory_rotation",
    "_dedicated_rotation_cards",
    "_open_fmed_pgy_rules_dialog",
    "_fmed_clinic_concurrency_editor",
    "_set_fmed_pgy_clinic_limit",
    "_elective_configuration",
    "_elective_directory",
    "_elective_list_item",
    "_elective_detail_panel",
    "_elective_rotation_view",
    "_new_elective_rotation_draft",
    "_elective_rotation_editor",
    "_open_elective_rotation_dialog",
    "_confirm_remove_elective_rotation",
    "_SPECIAL_EVENT_TIME_OPTIONS",
    "_special_configuration",
    "_special_type_card",
    "_special_rotation_row",
    "_special_rotation_period_label",
    "_open_special_rotation_dialog",
    "_academic_configuration",
    "_academic_week_options",
    "_academic_week_label",
    "render_rotations_tab",
    "_rotation_directory",
    "_rotation_list_item",
]


def render_rotations_tab(
    instance: SchedulerInput,
    *,
    schedule: Schedule | None = None,
    selected_rotation_id: str | None,
    on_select: SelectRotation,
    on_save: SaveRotation,
    on_color_save: SaveRotation | None = None,
    active_section: str = "rotation_summary",
    on_section_change=None,
    resident_edit_url: str | None = None,
) -> None:
    """Render rotation-summary and rotation-configuration workspaces."""
    from nicegui import ui

    rotations = standard_rotations(instance)
    creating = selected_rotation_id == NEW_MANDATORY_ROTATION_ID
    selected = next(
        (rotation for rotation in rotations if rotation.id == selected_rotation_id),
        None,
    )

    with page_shells.configuration(
        "Rotations",
        subtitle="Review and configure rotations, curricula, and scheduling rules.",
    ):
        with (
            ui.tabs(on_change=on_section_change)
            .props("dense no-caps align=left")
            .classes("rbs-configuration-tabs w-full") as tabs
        ):
            summary_tab = ui.tab("rotation_summary", label="Summary")
            rotations_tab = ui.tab("standard_rotations", label="Mandatory")
            fmed_tab = ui.tab("fmed_configuration", label="FMED/Inpatient")
            electives_tab = ui.tab("elective_configuration", label="Electives")
            special_tab = ui.tab("special_configuration", label="Special")
            academic_tab = ui.tab("academic_configuration", label="Academic")

        section_tabs = {
            "rotation_summary": summary_tab,
            "standard_rotations": rotations_tab,
            "fmed_configuration": fmed_tab,
            "elective_configuration": electives_tab,
            "special_configuration": special_tab,
            "academic_configuration": academic_tab,
        }

        with (
            ui.tab_panels(
                tabs,
                value=section_tabs.get(active_section, summary_tab),
            )
            .props("animated")
            .classes("rbs-configuration-panels w-full")
        ):
            with ui.tab_panel(summary_tab).classes("p-0 pt-4"):
                _rotation_summary(
                    instance,
                    schedule=schedule,
                    resident_edit_url=resident_edit_url,
                )
            with ui.tab_panel(rotations_tab).classes("p-0 pt-4"):
                with master_detail.split(detail_selected=selected_rotation_id is not None):
                    _rotation_directory(
                        instance,
                        rotations,
                        selected_rotation_id=selected_rotation_id,
                        on_select=on_select,
                    )
                    _rotation_detail_panel(
                        instance,
                        rotation=selected,
                        creating=creating,
                        missing_id=(
                            selected_rotation_id
                            if selected_rotation_id and selected is None and not creating
                            else None
                        ),
                        on_select=on_select,
                        on_save=on_save,
                    )
            with ui.tab_panel(fmed_tab).classes("p-0 pt-4"):
                _dedicated_rotation_cards(
                    instance,
                    RotationKind.FMED,
                    selected_rotation_id=selected_rotation_id,
                    on_save=on_save,
                    on_color_save=on_color_save,
                )
            with ui.tab_panel(electives_tab).classes("p-0 pt-4"):
                _elective_configuration(
                    instance,
                    selected_rotation_id=selected_rotation_id,
                    on_select=on_select,
                    on_save=on_save,
                    on_color_save=on_color_save or on_save,
                )
            with ui.tab_panel(special_tab).classes("p-0 pt-4"):
                _special_configuration(
                    instance,
                    selected_rotation_id=selected_rotation_id,
                    on_save=on_save,
                )
            with ui.tab_panel(academic_tab).classes("p-0 pt-4"):
                _academic_configuration(
                    instance,
                    selected_rotation_id=selected_rotation_id,
                    on_save=on_save,
                )


def _rotation_directory(
    instance: SchedulerInput,
    rotations: list[Rotation],
    *,
    selected_rotation_id: str | None,
    on_select: SelectRotation,
) -> None:
    from nicegui import ui

    elements = master_detail.directory(
        "Rotation directory",
        search_label="Search rotations",
        search_placeholder="Code, name, kind, or training level",
        action_label="New rotation",
        action_icon="add",
        on_action=partial(on_select, NEW_MANDATORY_ROTATION_ID),
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
            or query in rotation.kind.value.casefold()
            or any(
                query in instance.training_level_label(rule.pgy).casefold()
                or query
                in instance.training_level_label(
                    rule.pgy,
                    compact=True,
                ).casefold()
                or query in f"pgy {rule.pgy}"
                or query in f"year {rule.pgy}"
                for rule in rotation.pgy_rules
            )
        ]
        with directory:
            if not filtered:
                master_detail.empty_directory(
                    icon="search_off",
                    title="No matching rotations",
                    description="Try a different code, name, kind, or training level.",
                )
                return
            master_detail.directory_heading("Rotations by code", len(filtered))
            with ui.list().props("separator").classes("w-full"):
                for rotation in filtered:
                    _rotation_list_item(
                        rotation,
                        selected_rotation_id,
                        on_select,
                        instance=instance,
                    )

    search.on_value_change(lambda: render_directory())
    render_directory()


def _rotation_list_item(
    rotation: Rotation,
    selected_rotation_id: str | None,
    on_select: SelectRotation,
    *,
    instance: SchedulerInput | None = None,
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
            details = [
                instance.training_level_code(rule.pgy)
                if instance is not None
                else default_training_level_code(rule.pgy)
                for rule in rotation.pgy_rules
            ]
            if rotation.away:
                details.append("Away")
            ui.item_label(" · ".join(details)).props("caption")
        with ui.item_section().props("side"):
            ui.icon("chevron_right").props("size=20px").classes("rbs-text-subtle")
