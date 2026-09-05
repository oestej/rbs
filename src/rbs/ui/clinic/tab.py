"""Clinic workspace: block rules, manual placements, sites, and capacity."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from functools import partial

from pydantic import ValidationError

from rbs.models.clinic import ClinicSiteConfig
from rbs.models.enums import WEEKDAYS_MF, RotationKind, Session, Weekday
from rbs.models.instance import ManualClinicBlock, SchedulerInput
from rbs.models.rotation import Rotation
from rbs.ui import page_shells
from rbs.ui.buttons import SECONDARY_BUTTON_PROPS
from rbs.ui.clinic.ops import (
    _default_clinic_rule,
    _new_clinic_draft,
    add_clinic,
    remove_clinic,
    replace_clinic,
    replace_primary_clinic,
)
from rbs.ui.drafts import Draft
from rbs.ui.editor_common import (
    _CONSECUTIVE_OPTIONS,
    _DURATION_OPTIONS,
    _SESSION_OPTIONS,
    _academic_block_start_for_week,
    _academic_block_start_options,
    _as_int,
    _as_percent,
    _as_string_list,
    _as_text,
    _clinic_capacity_range_label,
    _clinic_pgy_capacity_label,
    _default_block_duration,
    _from_percent,
    _optional_float,
    _remove_index,
    _validation_message,
    _weeks_label,
)
from rbs.ui.rotations.ops import (
    add_manual_clinic_block,
    remove_manual_clinic_block,
    replace_clinic_block_rules,
    rotation_editor_state,
)
from rbs.ui.rotations.widgets import (
    add_block_config,
    clinic_week_editor,
    prerequisite_options,
    rotation_color_palette,
    set_block_vacation_allowed,
    toggle_pgy_rule,
)

SaveRotation = Callable[[SchedulerInput, str | None], None]


def render_clinic_tab(
    instance: SchedulerInput,
    *,
    on_save: SaveRotation,
    active_section: str = "clinic_sites",
    on_section_change=None,
) -> None:
    """Render Clinic block rules, manual placements, and clinic sites."""
    from nicegui import ui

    with page_shells.configuration(
        "Clinic",
        subtitle="Configure clinics, block rules, and manual placements.",
    ):
        with (
            ui.tabs(on_change=on_section_change)
            .props("dense no-caps align=left")
            .classes("rbs-configuration-tabs w-full") as tabs
        ):
            sites_tab = ui.tab("clinic_sites", label="Clinics")
            rules_tab = ui.tab("clinic_block_rules", label="Block rules")
            manual_tab = ui.tab(
                "clinic_manual_blocks",
                label=f"Manual blocks ({len(instance.manual_clinic_blocks)})",
            )

        sections = {
            "clinic_sites": sites_tab,
            "clinic_block_rules": rules_tab,
            "clinic_manual_blocks": manual_tab,
        }
        with (
            ui.tab_panels(tabs, value=sections.get(active_section, sites_tab))
            .props("animated")
            .classes("rbs-configuration-panels w-full")
        ):
            with ui.tab_panel(sites_tab).classes("p-0 pt-4"):
                _clinic_directory_configuration(
                    instance,
                    selected_rotation_id=None,
                    on_save=on_save,
                )
            with ui.tab_panel(rules_tab).classes("p-0 pt-4"):
                _clinic_block_rules_configuration(instance, on_save=on_save)
            with ui.tab_panel(manual_tab).classes("p-0 pt-4"):
                _manual_clinic_blocks_configuration(instance, on_save=on_save)


def _clinic_block_rules_configuration(
    instance: SchedulerInput,
    *,
    on_save: SaveRotation,
) -> None:
    from nicegui import ui

    clinic_rotations = sorted(
        (rotation for rotation in instance.rotations if rotation.kind is RotationKind.CLINIC),
        key=lambda rotation: rotation.code.casefold(),
    )
    with ui.column().classes("w-full gap-5"):
        with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
            with ui.column().classes("gap-0"):
                ui.label("Clinic block rules").classes("rbs-type-section-title")
                ui.label(
                    "Configure required block counts and lengths by training level, "
                    "plus optional scheduling limits."
                ).classes("rbs-type-caption rbs-text-muted")
            if clinic_rotations:
                ui.button(
                    "Edit rules",
                    icon="tune",
                    on_click=partial(
                        _open_clinic_block_rules_dialog,
                        instance,
                        clinic_rotations[0].id,
                        on_save=on_save,
                    ),
                ).props("outline no-caps")

        if not clinic_rotations:
            ui.label("No Clinic block rotation is configured.").classes(
                "rbs-type-body rbs-text-muted"
            )
            return

        for rotation in clinic_rotations:
            with ui.element("div").classes("rbs-clinic-rules-grid w-full"):
                with ui.column().classes("rbs-clinic-rule-scope is-overall gap-1 rounded p-4"):
                    ui.label("Overall").classes(
                        "rbs-type-caption uppercase rbs-font-semibold rbs-text-muted"
                    )
                    ui.label(
                        _clinic_capacity_range_label(
                            rotation.capacity.min_concurrent,
                            rotation.capacity.max_concurrent,
                        )
                    ).classes("rbs-font-semibold")
                    admin_count = (
                        rotation.clinic.admin_half_days_per_week
                        if rotation.clinic is not None
                        else 0
                    )
                    ui.label(
                        f"Max {_weeks_label(rotation.max_consecutive_weeks)} consecutive"
                        + (
                            f" · {admin_count} Admin half-day" + ("s" if admin_count != 1 else "")
                            if admin_count
                            else ""
                        )
                    ).classes("rbs-type-caption rbs-text-muted")
                by_pgy = {rule.pgy: rule for rule in rotation.pgy_rules}
                for pgy in instance.training_level_ids:
                    rule = by_pgy.get(pgy)
                    scope_classes = "rbs-clinic-rule-scope gap-1 rounded p-4"
                    if rule is None:
                        scope_classes += " is-inactive"
                    with ui.column().classes(scope_classes):
                        ui.label(instance.training_level_name(pgy)).classes(
                            "rbs-type-caption uppercase rbs-font-semibold rbs-text-muted"
                        )
                        if rule is None:
                            ui.label("Not eligible").classes("rbs-font-semibold")
                            ui.label("No block rules").classes("rbs-type-caption rbs-text-muted")
                        else:
                            block_mix = []
                            for config in rule.block_configs:
                                count = _clinic_requirement_count(
                                    instance,
                                    pgy,
                                    rotation.id,
                                    config.duration_weeks,
                                )
                                block_mix.append(
                                    _clinic_block_requirement_label(
                                        count,
                                        config.duration_weeks,
                                    )
                                )
                            with ui.row().classes("items-center gap-1 flex-wrap"):
                                for label in block_mix or ["No required blocks"]:
                                    ui.badge(label).props("outline").classes("rbs-muted-badge")
                            ui.label(
                                "Concurrent staffing: "
                                f"{_clinic_pgy_capacity_label(rule.model_dump())}"
                            ).classes("rbs-type-caption rbs-text-muted")


def _clinic_requirement_count(
    instance: SchedulerInput,
    pgy: int,
    rotation_id: str,
    duration_weeks: int,
) -> int:
    return sum(
        block.count
        for block in instance.curriculum_for(pgy).blocks
        if block.rotation_id == rotation_id and block.duration_weeks == duration_weeks
    )


def _clinic_block_requirement_label(count: int, duration_weeks: int) -> str:
    block_label = "block" if count == 1 else "blocks"
    return f"{count} × {duration_weeks}-week {block_label}"


def _open_clinic_block_rules_dialog(
    instance: SchedulerInput,
    rotation_id: str,
    *,
    on_save: SaveRotation,
) -> None:
    from nicegui import ui

    rotation = instance.rotation(rotation_id)
    draft = rotation_editor_state(rotation)
    if not isinstance(draft.get("clinic"), dict):
        draft["clinic"] = _default_clinic_rule()
    counts = {
        (rule.pgy, config.duration_weeks): _clinic_requirement_count(
            instance,
            rule.pgy,
            rotation_id,
            config.duration_weeks,
        )
        for rule in rotation.pgy_rules
        if rule.pgy in instance.training_level_ids
        for config in rule.block_configs
    }
    with (
        ui.dialog() as dialog,
        (
            ui.card()
            .classes("rbs-clinic-rules-dialog p-0 gap-0")
            .style(
                "width:calc(100vw - 64px);max-width:1280px;"
                "height:calc(100vh - 64px);max-height:900px"
            )
        ),
    ):
        with ui.row().classes("rbs-clinic-editor-header w-full items-center gap-5 px-5 py-4"):
            ui.label(f"Edit Clinic block rules · {rotation.name}").classes(
                "rbs-clinic-editor-title rbs-type-dialog-title whitespace-nowrap"
            )
            with (
                ui.tabs()
                .props("dense no-caps inline-label align=left mobile-arrows outside-arrows")
                .classes("rbs-clinic-editor-tabs min-w-0") as tabs
            ):
                overall_tab = ui.tab("clinic_rules_overall", label="Overall")
                pgy_tabs = {
                    pgy: ui.tab(
                        f"clinic_rules_pgy_{pgy}",
                        label=instance.training_level_label(pgy, compact=True),
                    )
                    for pgy in instance.training_level_ids
                }
            ui.space()
            ui.button(icon="close", on_click=dialog.close).props(
                "flat round dense aria-label='Close Clinic block rules'"
            )

        with (
            ui.tab_panels(tabs, value=overall_tab)
            .props("animated")
            .classes("rbs-clinic-editor-panels w-full flex-1 min-h-0")
        ):
            with ui.tab_panel(overall_tab).classes("h-full p-0"):
                with ui.scroll_area().classes("h-full w-full"):
                    with ui.column().classes("w-full gap-5 p-6"):
                        with ui.column().classes("gap-1"):
                            ui.label("Overall Clinic block rules").classes("rbs-type-section-title")
                            ui.label(
                                "These limits apply across all training levels. Clinic site "
                                "allocation and session capacity are configured below."
                            ).classes("rbs-type-caption rbs-text-muted")
                        rotation_color_palette(
                            draft,
                            instance.color_scheme.palette,
                            label="Clinic block schedule color",
                        )
                        capacity = draft["capacity"]
                        with ui.row().classes("w-full items-end gap-4 flex-wrap"):
                            minimum = (
                                ui.number(
                                    "Minimum total residents",
                                    value=_optional_float(capacity.get("min_concurrent")),
                                    min=0,
                                    precision=0,
                                    step=1,
                                    placeholder="No minimum",
                                )
                                .props("outlined clearable")
                                .classes("w-full sm:flex-1")
                            )
                            maximum = (
                                ui.number(
                                    "Maximum total residents",
                                    value=_optional_float(capacity.get("max_concurrent")),
                                    min=0,
                                    precision=0,
                                    step=1,
                                    placeholder="No maximum",
                                )
                                .props("outlined clearable")
                                .classes("w-full sm:flex-1")
                            )
                            consecutive = (
                                ui.select(
                                    _CONSECUTIVE_OPTIONS,
                                    value=int(draft["max_consecutive_weeks"]),
                                    label="Max consecutive weeks",
                                )
                                .props("outlined options-dense")
                                .classes("w-full sm:w-64")
                            )
                        minimum.bind_value(capacity, "min_concurrent", forward=_as_int)
                        maximum.bind_value(capacity, "max_concurrent", forward=_as_int)
                        consecutive.bind_value(draft, "max_consecutive_weeks", forward=_as_int)
                        admin = (
                            ui.number(
                                "Admin half-days per Clinic week",
                                value=int(
                                    draft["clinic"].get(
                                        "admin_half_days_per_week",
                                        0,
                                    )
                                ),
                                min=0,
                                max=len(draft["clinic"].get("slots") or []),
                                precision=0,
                                step=1,
                            )
                            .props("outlined")
                            .classes("rbs-admin-half-days-field w-full sm:w-80")
                        )
                        admin.bind_value(
                            draft["clinic"], "admin_half_days_per_week", forward=_as_int
                        )
                        ui.separator()
                        with ui.column().classes("gap-1"):
                            ui.label("Clinic block sessions").classes("rbs-type-section-title")
                            ui.label(
                                "Enable the weekly sessions worked during a Clinic block, "
                                "including weekends when applicable. Site allocation rules "
                                "apply unless a session is limited to selected sites."
                            ).classes("rbs-type-caption rbs-text-muted")
                        clinic_week_editor(
                            draft["clinic"],
                            academic_half_day=(
                                instance.clinic_policy.academic.weekday,
                                instance.clinic_policy.academic.session,
                            ),
                            site_options={
                                site.id: site.name for site in instance.clinic_policy.sites
                            },
                            default_site_ids=list(instance.clinic_policy.site_ids),
                        )

            for pgy, pgy_tab in pgy_tabs.items():
                with ui.tab_panel(pgy_tab).classes("h-full p-0"):
                    with ui.scroll_area().classes("h-full w-full"):
                        with ui.column().classes("w-full gap-5 p-6"):
                            _clinic_pgy_rule_panel(
                                instance,
                                draft,
                                rotation_id,
                                pgy,
                                counts,
                            )

        ui.separator()
        with ui.row().classes("w-full justify-end gap-3 p-4"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")

            def save_rules() -> None:
                try:
                    replacement = Rotation.model_validate(draft)
                    updated = replace_clinic_block_rules(
                        instance,
                        rotation_id,
                        replacement,
                        counts,
                    )
                    dialog.close()
                    ui.notify("Clinic block rules updated", type="positive")
                    on_save(updated, None)
                except (ValidationError, ValueError) as exc:
                    ui.notify(
                        _validation_message(exc),
                        type="negative",
                        multi_line=True,
                    )

            ui.button("Save rules", icon="save", on_click=save_rules).props("unelevated no-caps")
    dialog.open()


def _clinic_pgy_rule_panel(
    instance: SchedulerInput,
    draft: Draft,
    rotation_id: str,
    pgy: int,
    counts: dict[tuple[int, int], int],
) -> None:
    from nicegui import ui

    container = ui.column().classes("w-full gap-4")

    def render() -> None:
        container.clear()
        rule = next(
            (item for item in draft["pgy_rules"] if int(item["pgy"]) == pgy),
            None,
        )
        with container:
            level_name = instance.training_level_name(pgy)
            with ui.row().classes("w-full items-center justify-between gap-4"):
                ui.label(f"{level_name} Clinic blocks").classes("rbs-type-section-title")
                enabled = ui.checkbox(
                    f"Clinic required for {level_name}",
                    value=rule is not None,
                ).classes("shrink-0")
            enabled.on_value_change(partial(toggle_pgy_rule, draft, pgy, render))
            if rule is None:
                return

            with ui.row().classes("w-full items-center justify-between gap-3 pt-2"):
                ui.label("Required blocks").classes("rbs-type-control-label")
                add_button = ui.button(
                    "Add block duration",
                    icon="add",
                    on_click=partial(add_block_config, rule, render),
                ).props("flat dense no-caps")
                add_button.set_enabled(len(rule["block_configs"]) < len(_DURATION_OPTIONS))
            for index, config in enumerate(rule["block_configs"]):
                _clinic_block_config_editor(
                    rule,
                    config,
                    index,
                    render,
                    pgy,
                    counts,
                )

            with ui.expansion(
                "Advanced scheduling limits",
                caption=(
                    "Optional concurrency and placement guardrails · "
                    f"{_clinic_pgy_capacity_label(rule)}"
                ),
                icon="tune",
                value=False,
            ).classes("rbs-clinic-advanced-limits w-full"):
                with ui.column().classes("w-full gap-4 px-4 pb-4"):
                    ui.label(
                        "These are optional solver guardrails. Overall staffing, "
                        "Clinic session capacity, and other placement rules may be "
                        "more restrictive."
                    ).classes("rbs-type-caption rbs-text-muted")
                    with ui.column().classes("w-full gap-2"):
                        ui.label("Concurrent staffing").classes("rbs-type-control-label")
                        with ui.row().classes("w-full gap-3 flex-wrap"):
                            minimum = (
                                ui.number(
                                    "Minimum concurrent residents",
                                    value=_optional_float(rule.get("min_concurrent")),
                                    min=0,
                                    precision=0,
                                    step=1,
                                    placeholder="No minimum",
                                )
                                .props("outlined clearable")
                                .classes("w-full sm:flex-1")
                            )
                            maximum = (
                                ui.number(
                                    "Maximum concurrent residents",
                                    value=_optional_float(rule.get("max_concurrent")),
                                    min=0,
                                    precision=0,
                                    step=1,
                                    placeholder="No maximum",
                                )
                                .props("outlined clearable")
                                .classes("w-full sm:flex-1")
                            )
                        minimum.bind_value(rule, "min_concurrent", forward=_as_int)
                        maximum.bind_value(rule, "max_concurrent", forward=_as_int)

                    ui.separator()
                    with ui.column().classes("w-full gap-2"):
                        ui.label("Placement").classes("rbs-type-control-label")
                        with ui.row().classes("w-full items-start gap-3 flex-wrap"):
                            prerequisites = (
                                ui.select(
                                    prerequisite_options(
                                        instance,
                                        rotation_id,
                                        pgy,
                                    ),
                                    value=list(rule.get("prerequisite_rotation_ids") or []),
                                    label="Prerequisite rotations",
                                    multiple=True,
                                )
                                .props("outlined options-dense use-chips clearable")
                                .classes("w-full sm:flex-1")
                            )
                            earliest_start = _academic_block_start_for_week(
                                _as_int(rule.get("earliest_start_week")),
                                instance.calendar.weeks,
                            )
                            rule["earliest_start_week"] = earliest_start
                            earliest = (
                                ui.select(
                                    _academic_block_start_options(
                                        instance.calendar.first_week_start, instance.calendar.weeks
                                    ),
                                    value=earliest_start,
                                    label="Earliest start block",
                                )
                                .props("outlined options-dense clearable")
                                .classes("w-full sm:w-80")
                            )
                        prerequisites.bind_value(
                            rule, "prerequisite_rotation_ids", forward=_as_string_list
                        )
                        earliest.bind_value(rule, "earliest_start_week", forward=_as_int)

    render()


def _clinic_block_config_editor(
    rule: Draft,
    config: Draft,
    index: int,
    refresh: Callable[[], None],
    pgy: int,
    counts: dict[tuple[int, int], int],
) -> None:
    from nicegui import ui

    duration_weeks = int(config["duration_weeks"])
    vacation = config["vacation"]
    with (
        ui.card()
        .props("flat bordered")
        .classes("rbs-block-config rbs-clinic-block-config w-full p-3 gap-3")
    ):
        with ui.row().classes("w-full items-center gap-3 flex-wrap"):
            count = (
                ui.number(
                    "Blocks per resident",
                    value=counts.get((pgy, duration_weeks), 0),
                    min=0,
                    precision=0,
                    step=1,
                )
                .props("outlined dense")
                .classes("w-full sm:w-48")
            )
            duration = (
                ui.select(
                    _DURATION_OPTIONS,
                    value=duration_weeks,
                    label="Block length",
                )
                .props("outlined dense options-dense")
                .classes("w-full sm:w-48")
            )
            allowed = ui.checkbox(
                "Vacation may overlap",
                value=bool(vacation.get("allowed")),
            ).classes("rbs-clinic-block-checkbox w-full sm:w-56")
            maximum = None
            if vacation.get("allowed"):
                maximum = (
                    ui.number(
                        "Maximum vacation weeks",
                        value=int(vacation.get("max_weeks_per_block") or 1),
                        min=1,
                        max=duration_weeks,
                        precision=0,
                        step=1,
                    )
                    .props("outlined dense")
                    .classes("w-full sm:w-56")
                )
            remove = ui.button(
                icon="delete_outline",
                on_click=partial(
                    _remove_clinic_block_config,
                    rule,
                    index,
                    pgy,
                    duration_weeks,
                    counts,
                    refresh,
                ),
            ).props("flat round dense color=negative aria-label='Remove block configuration'")
            remove.set_enabled(len(rule["block_configs"]) > 1)

        duration.on_value_change(
            partial(
                _change_clinic_block_duration,
                config,
                pgy,
                duration_weeks,
                counts,
                refresh,
            )
        )
        count.on_value_change(
            partial(
                _set_clinic_block_count,
                rule,
                pgy,
                duration_weeks,
                counts,
            )
        )
        allowed.on_value_change(partial(set_block_vacation_allowed, vacation, refresh))
        if maximum is not None:
            maximum.bind_value(vacation, "max_weeks_per_block", forward=_as_int)


def _set_clinic_block_count(
    rule: Draft,
    pgy: int,
    duration_weeks: int,
    counts: dict[tuple[int, int], int],
    event,
) -> None:
    counts[pgy, duration_weeks] = max(
        0,
        int(event.value) if event.value is not None else 0,
    )


def _change_clinic_block_duration(
    config: Draft,
    pgy: int,
    previous_duration: int,
    counts: dict[tuple[int, int], int],
    refresh: Callable[[], None],
    event,
) -> None:
    new_duration = int(event.value)
    count = counts.pop((pgy, previous_duration), 0)
    counts[pgy, new_duration] = count
    config["duration_weeks"] = new_duration
    refresh()


def _remove_clinic_block_config(
    rule: Draft,
    index: int,
    pgy: int,
    duration_weeks: int,
    counts: dict[tuple[int, int], int],
    refresh: Callable[[], None],
) -> None:
    if len(rule["block_configs"]) <= 1:
        return
    counts.pop((pgy, duration_weeks), None)
    _remove_index(rule["block_configs"], index, refresh)


def _manual_clinic_blocks_configuration(
    instance: SchedulerInput,
    *,
    on_save: SaveRotation,
) -> None:
    from nicegui import ui

    eligible_residents = _manual_clinic_resident_options(instance)
    with ui.column().classes("w-full gap-5"):
        with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
            with ui.column().classes("gap-0"):
                ui.label("Manual Clinic blocks").classes("rbs-type-section-title")
                ui.label(
                    "Place an additional resident Clinic block by replacing a "
                    "same-length Elective block."
                ).classes("rbs-type-caption rbs-text-muted")
            if instance.manual_clinic_blocks:
                add_button = ui.button(
                    "Schedule clinic block",
                    icon="add",
                    on_click=partial(
                        _open_manual_clinic_block_dialog,
                        instance,
                        on_save=on_save,
                    ),
                ).props("unelevated no-caps")
                add_button.set_enabled(bool(eligible_residents))

        if not instance.manual_clinic_blocks:
            with ui.column().classes(
                "rbs-manual-clinic-empty w-full items-center justify-center gap-2 rounded p-6"
            ):
                ui.icon("event_available").classes("rbs-icon-lg rbs-text-disabled")
                ui.label("No manually scheduled Clinic blocks").classes("rbs-font-semibold")
                ui.label("Schedule one when a resident needs an additional Clinic block.").classes(
                    "rbs-type-body rbs-text-muted text-center"
                )
                add_button = ui.button(
                    "Schedule clinic block",
                    icon="add",
                    on_click=partial(
                        _open_manual_clinic_block_dialog,
                        instance,
                        on_save=on_save,
                    ),
                ).props("unelevated no-caps")
                add_button.set_enabled(bool(eligible_residents))
            return

        residents = instance.residents_by_id
        rotations = instance.rotations_by_id

        def remove(index: int) -> None:
            try:
                updated = remove_manual_clinic_block(instance, index)
                ui.notify("Manual Clinic block removed", type="positive")
                on_save(updated, None)
            except (ValidationError, ValueError) as exc:
                ui.notify(
                    _validation_message(exc),
                    type="negative",
                    multi_line=True,
                )

        with ui.column().classes("w-full gap-2"):
            for index, block in enumerate(instance.manual_clinic_blocks):
                resident = residents[block.resident_id]
                clinic = rotations[block.rotation_id]
                replaced = rotations[block.replaces_rotation_id]
                with ui.row().classes(
                    "rbs-manual-clinic-row w-full items-center gap-3 rounded px-4 py-3"
                ):
                    ui.icon("event_repeat").classes("rbs-text-primary")
                    with ui.column().classes("min-w-0 flex-1 gap-0"):
                        ui.label(
                            f"{resident.name} · {instance.training_level_name(resident.pgy)}"
                        ).classes("rbs-font-semibold")
                        ui.label(
                            f"{clinic.code} · {_manual_week_range_label(instance, block)} "
                            f"· replaces {replaced.code}"
                        ).classes("rbs-type-caption rbs-text-muted")
                    ui.button(
                        icon="delete_outline",
                        on_click=partial(remove, index),
                    ).props(
                        "flat round dense color=negative aria-label='Remove manual Clinic block'"
                    )


def _manual_clinic_resident_options(instance: SchedulerInput) -> dict[str, str]:
    options: dict[str, str] = {}
    for resident in sorted(instance.residents, key=lambda item: item.name.casefold()):
        if any(
            _manual_duration_options(instance, resident.id, rotation.id)
            for rotation in instance.rotations
            if rotation.kind is RotationKind.CLINIC
        ):
            options[resident.id] = f"{resident.name} · {instance.training_level_name(resident.pgy)}"
    return options


def _manual_clinic_rotation_options(
    instance: SchedulerInput,
    resident_id: str,
) -> dict[str, str]:
    return {
        rotation.id: f"{rotation.code} — {rotation.name}"
        for rotation in sorted(
            instance.rotations,
            key=lambda item: item.code.casefold(),
        )
        if rotation.kind is RotationKind.CLINIC
        and _manual_duration_options(instance, resident_id, rotation.id)
    }


def _manual_duration_options(
    instance: SchedulerInput,
    resident_id: str,
    clinic_rotation_id: str,
) -> dict[int, str]:
    resident = next(item for item in instance.residents if item.id == resident_id)
    clinic = instance.rotation(clinic_rotation_id)
    try:
        rule = clinic.pgy_rule(resident.pgy)
    except KeyError:
        return {}
    replacement_durations = {
        block.duration_weeks
        for block in instance.curriculum_for(resident.pgy).blocks
        if instance.rotation(block.rotation_id).kind is RotationKind.ELECTIVE
        and _remaining_replacement_count(
            instance,
            resident_id,
            block.rotation_id,
            block.duration_weeks,
        )
        > 0
    }
    return {
        config.duration_weeks: _weeks_label(config.duration_weeks)
        for config in rule.block_configs
        if config.duration_weeks in replacement_durations
    }


def _remaining_replacement_count(
    instance: SchedulerInput,
    resident_id: str,
    rotation_id: str,
    duration_weeks: int,
) -> int:
    resident = next(item for item in instance.residents if item.id == resident_id)
    available = sum(
        block.count
        for block in instance.curriculum_for(resident.pgy).blocks
        if block.rotation_id == rotation_id and block.duration_weeks == duration_weeks
    )
    used = sum(
        block.replaces_rotation_id == rotation_id and block.duration_weeks == duration_weeks
        for block in instance.manual_clinic_blocks
        if block.resident_id == resident_id
    )
    used += sum(
        override.replaces_rotation_id == rotation_id and override.duration_weeks == duration_weeks
        for override in instance.resident_rotation_overrides
        if override.resident_id == resident_id
    )
    return available - used


def _manual_replacement_options(
    instance: SchedulerInput,
    resident_id: str,
    duration_weeks: int,
) -> dict[str, str]:
    resident = next(item for item in instance.residents if item.id == resident_id)
    candidates = []
    seen: set[str] = set()
    for block in instance.curriculum_for(resident.pgy).blocks:
        if block.rotation_id in seen or block.duration_weeks != duration_weeks:
            continue
        rotation = instance.rotation(block.rotation_id)
        if rotation.kind is not RotationKind.ELECTIVE:
            continue
        remaining = _remaining_replacement_count(
            instance,
            resident_id,
            block.rotation_id,
            duration_weeks,
        )
        if remaining <= 0:
            continue
        seen.add(block.rotation_id)
        candidates.append((rotation, remaining))
    candidates.sort(key=lambda item: item[0].code.casefold())
    return {
        rotation.id: (
            f"{rotation.code} — {rotation.name}"
            + (f" ({remaining} available)" if remaining > 1 else "")
        )
        for rotation, remaining in candidates
    }


def _manual_start_options(
    instance: SchedulerInput,
    resident_id: str,
    clinic_rotation_id: str,
    duration_weeks: int,
) -> dict[int, str]:
    resident = next(item for item in instance.residents if item.id == resident_id)
    rotation = instance.rotation(clinic_rotation_id)
    config = rotation.block_config(resident.pgy, duration_weeks)
    earliest = rotation.pgy_rule(resident.pgy).earliest_start_week
    vacation = set(resident.vacation_weeks)
    options: dict[int, str] = {}
    last_start = instance.calendar.weeks - duration_weeks + 1
    for start in range(1, last_start + 1):
        if (start - 1) % instance.calendar.block_start_alignment:
            continue
        if earliest is not None and start < earliest:
            continue
        overlap = set(range(start, start + duration_weeks)) & vacation
        if overlap and not config.vacation.allowed:
            continue
        maximum = config.vacation.max_weeks_per_block
        if maximum is not None and len(overlap) > maximum:
            continue
        monday = instance.calendar.first_week_start + timedelta(weeks=start - 1)
        ending = monday + timedelta(weeks=duration_weeks, days=-1)
        week_label = (
            f"Week {start}"
            if duration_weeks == 1
            else f"Weeks {start}–{start + duration_weeks - 1}"
        )
        options[start] = (
            f"{week_label} · {monday:%b} {monday.day}–{ending:%b} {ending.day}, {ending.year}"
        )
    return options


def _manual_week_range_label(
    instance: SchedulerInput,
    block: ManualClinicBlock,
) -> str:
    monday = instance.calendar.first_week_start + timedelta(weeks=block.start_week - 1)
    ending = monday + timedelta(weeks=block.duration_weeks, days=-1)
    weeks = (
        f"Week {block.start_week}"
        if block.duration_weeks == 1
        else f"Weeks {block.start_week}–{block.start_week + block.duration_weeks - 1}"
    )
    return f"{weeks} ({monday:%b} {monday.day}–{ending:%b} {ending.day})"


def _open_manual_clinic_block_dialog(
    instance: SchedulerInput,
    *,
    on_save: SaveRotation,
) -> None:
    from nicegui import ui

    resident_options = _manual_clinic_resident_options(instance)
    if not resident_options:
        ui.notify("No residents have an eligible Clinic replacement block", type="warning")
        return
    initial_resident = next(iter(resident_options))
    clinic_options = _manual_clinic_rotation_options(instance, initial_resident)
    initial_clinic = next(iter(clinic_options))
    duration_options = _manual_duration_options(
        instance,
        initial_resident,
        initial_clinic,
    )
    initial_duration = _default_block_duration(duration_options)
    assert initial_duration is not None
    start_options = _manual_start_options(
        instance,
        initial_resident,
        initial_clinic,
        initial_duration,
    )

    with (
        ui.dialog() as dialog,
        ui.card().classes("rbs-manual-clinic-dialog w-full max-w-3xl p-0 gap-0"),
    ):
        with ui.row().classes("w-full items-center justify-between gap-3 px-5 py-4"):
            ui.label("Schedule clinic block").classes("rbs-type-dialog-title")
            ui.button(icon="close", on_click=dialog.close).props(
                "flat round dense aria-label='Close manual Clinic block dialog'"
            )
        ui.separator()
        with ui.column().classes("w-full gap-4 p-5"):
            ui.label(
                "The Clinic block is fixed to these weeks and replaces a same-length "
                "Elective block so the resident remains scheduled for 52 weeks."
            ).classes("rbs-type-body rbs-text-muted")
            resident_select = (
                ui.select(
                    resident_options,
                    value=initial_resident,
                    label="Resident",
                )
                .props("outlined options-dense use-input")
                .classes("w-full")
            )
            with ui.row().classes("w-full items-end gap-4 flex-wrap"):
                clinic_select = (
                    ui.select(
                        clinic_options,
                        value=initial_clinic,
                        label="Clinic block",
                    )
                    .props("outlined options-dense")
                    .classes("min-w-64 flex-1")
                )
                duration_select = (
                    ui.select(
                        duration_options,
                        value=initial_duration,
                        label="Block length",
                    )
                    .props("outlined options-dense")
                    .classes("w-48")
                )
            start_select = (
                ui.select(
                    start_options,
                    value=next(iter(start_options), None),
                    label="Weeks",
                )
                .props("outlined options-dense use-input")
                .classes("w-full")
            )

        def refresh_duration_fields() -> None:
            resident_id = str(resident_select.value)
            clinic_id = str(clinic_select.value)
            durations = _manual_duration_options(instance, resident_id, clinic_id)
            duration = (
                int(duration_select.value)
                if duration_select.value is not None and int(duration_select.value) in durations
                else _default_block_duration(durations)
            )
            duration_select.set_options(durations, value=duration)
            if duration is None:
                start_select.set_options({}, value=None)
                return
            starts = _manual_start_options(
                instance,
                resident_id,
                clinic_id,
                duration,
            )
            start_select.set_options(
                starts,
                value=(
                    start_select.value if start_select.value in starts else next(iter(starts), None)
                ),
            )

        def change_resident(_event) -> None:
            resident_id = str(resident_select.value)
            clinics = _manual_clinic_rotation_options(instance, resident_id)
            clinic_id = (
                clinic_select.value if clinic_select.value in clinics else next(iter(clinics), None)
            )
            clinic_select.set_options(clinics, value=clinic_id)
            refresh_duration_fields()

        resident_select.on_value_change(change_resident)
        clinic_select.on_value_change(lambda _event: refresh_duration_fields())
        duration_select.on_value_change(lambda _event: refresh_duration_fields())

        ui.separator()
        with ui.row().classes("w-full justify-end gap-3 p-4"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")

            def save_block() -> None:
                try:
                    if any(
                        value is None
                        for value in (
                            resident_select.value,
                            clinic_select.value,
                            duration_select.value,
                            start_select.value,
                        )
                    ):
                        raise ValueError("complete all manual Clinic block fields")
                    replacements = _manual_replacement_options(
                        instance,
                        str(resident_select.value),
                        int(duration_select.value),
                    )
                    replacement_id = next(iter(replacements), None)
                    if replacement_id is None:
                        raise ValueError("no same-length Elective block remains available")
                    updated = add_manual_clinic_block(
                        instance,
                        {
                            "resident_id": str(resident_select.value),
                            "rotation_id": str(clinic_select.value),
                            "duration_weeks": int(duration_select.value),
                            "start_week": int(start_select.value),
                            "replaces_rotation_id": replacement_id,
                        },
                    )
                    dialog.close()
                    ui.notify("Clinic block scheduled", type="positive")
                    on_save(updated, None)
                except (ValidationError, ValueError) as exc:
                    ui.notify(
                        _validation_message(exc),
                        type="negative",
                        multi_line=True,
                    )

            ui.button("Schedule block", icon="save", on_click=save_block).props(
                "unelevated no-caps"
            )
    dialog.open()


def _clinic_directory_configuration(
    instance: SchedulerInput,
    *,
    selected_rotation_id: str | None,
    on_save: SaveRotation,
) -> None:
    from nicegui import ui

    policy = instance.clinic_policy
    with ui.column().classes("w-full gap-5"):
        with ui.row().classes("w-full items-end justify-between gap-4 flex-wrap"):
            with ui.column().classes("gap-0"):
                ui.label("Clinic sites").classes("rbs-type-section-title")
                ui.label(
                    "Manage allocation targets, weekly capacity, date exceptions, and closures."
                ).classes("rbs-type-caption rbs-text-muted")
            with ui.row().classes("items-center gap-2 flex-wrap"):
                primary = (
                    ui.select(
                        {site.id: site.name for site in policy.sites},
                        value=policy.primary_site_id,
                        label="Primary clinic",
                    )
                    .props("outlined dense options-dense")
                    .classes("w-56")
                )

                def change_primary(event) -> None:
                    try:
                        updated = replace_primary_clinic(
                            instance,
                            str(event.value),
                        )
                        ui.notify("Primary clinic updated", type="positive")
                        on_save(updated, selected_rotation_id)
                    except (ValidationError, ValueError) as exc:
                        ui.notify(
                            _validation_message(exc),
                            type="negative",
                            multi_line=True,
                        )

                primary.on_value_change(change_primary)
                ui.button(
                    "Add clinic",
                    icon="add",
                    on_click=lambda: _open_clinic_editor_dialog(
                        instance,
                        original_id=None,
                        selected_rotation_id=selected_rotation_id,
                        on_save=on_save,
                    ),
                ).props("unelevated no-caps")

        with ui.element("div").classes("rbs-clinic-sites-grid w-full"):
            for clinic in policy.sites:
                allocation = policy.allocation(clinic.id)
                maximums = [
                    half_day.max_residents(clinic.residents_per_attending)
                    for half_day in clinic.half_days
                ]
                maximums.extend(
                    override.max_residents(clinic.residents_per_attending)
                    for override in clinic.capacity_overrides
                )
                exception_count = len(clinic.capacity_overrides) + len(clinic.closure_days)
                with (
                    ui.card()
                    .props("flat bordered")
                    .classes("rbs-clinic-config-card w-full h-full p-0 gap-0")
                    .style(f"--rbs-clinic-color:{clinic.color}")
                ):
                    with ui.row().classes("rbs-clinic-config-header w-full items-start gap-3 p-4"):
                        ui.element("span").classes(
                            "rbs-clinic-config-color shrink-0 rounded-full mt-1"
                        )
                        with ui.column().classes("min-w-0 flex-1 gap-0"):
                            with ui.row().classes("items-center gap-2 flex-wrap"):
                                ui.label(clinic.name).classes("rbs-type-section-title")
                                if clinic.id == policy.primary_site_id:
                                    ui.badge("Primary", color="primary")
                        with ui.row().classes("items-center gap-1 shrink-0"):
                            ui.button(
                                "Edit",
                                icon="edit",
                                on_click=partial(
                                    _open_clinic_editor_dialog,
                                    instance,
                                    original_id=clinic.id,
                                    selected_rotation_id=selected_rotation_id,
                                    on_save=on_save,
                                ),
                            ).props("outline dense no-caps")
                            with ui.button(icon="more_vert").props(
                                "flat round dense "
                                f"aria-label='More actions for {clinic.name} clinic'"
                            ):
                                ui.tooltip("More actions")
                                with ui.menu():
                                    remove = ui.menu_item(
                                        "Remove clinic",
                                        on_click=partial(
                                            _confirm_remove_clinic,
                                            instance,
                                            clinic.id,
                                            selected_rotation_id=selected_rotation_id,
                                            on_save=on_save,
                                        ),
                                    ).classes("rbs-text-danger")
                                    if len(policy.sites) <= 1:
                                        remove.props("disable")

                    with ui.element("div").classes("rbs-clinic-metrics w-full"):
                        _clinic_metric(
                            "Target",
                            f"{round(allocation.target_fraction * 100):g}%",
                        )
                        _clinic_metric("Weekly sessions", str(len(clinic.half_days)))
                        _clinic_metric(
                            "Max residents",
                            str(max(maximums)) if maximums else "—",
                        )
                        _clinic_metric("Exceptions", str(exception_count))

                    with ui.row().classes("rbs-clinic-closures w-full items-center gap-3 p-4"):
                        with ui.column().classes("min-w-0 flex-1 gap-0"):
                            ui.label("Closure days").classes(
                                "rbs-type-caption rbs-font-semibold uppercase rbs-text-muted"
                            )
                            ui.label(
                                ", ".join(
                                    f"{closure.date:%b} {closure.date.day} · "
                                    f"{closure.name or 'Closed'}"
                                    for closure in clinic.closure_days
                                )
                                or "No closure days configured."
                            ).classes("rbs-type-body rbs-text-muted")
                        ui.button(
                            "Add closure day",
                            icon="event_busy",
                            on_click=partial(
                                _open_clinic_editor_dialog,
                                instance,
                                original_id=clinic.id,
                                selected_rotation_id=selected_rotation_id,
                                on_save=on_save,
                            ),
                        ).props("flat dense no-caps")


def _clinic_metric(label: str, value: str) -> None:
    from nicegui import ui

    with ui.column().classes("rbs-clinic-metric min-w-0 gap-0 px-4 py-3"):
        ui.label(value).classes("rbs-type-section-title")
        ui.label(label).classes("rbs-type-caption rbs-text-muted")


def _open_clinic_editor_dialog(
    instance: SchedulerInput,
    *,
    original_id: str | None,
    selected_rotation_id: str | None,
    on_save: SaveRotation,
) -> None:
    from nicegui import ui

    existing = (
        instance.clinic_policy.site(original_id).model_dump(mode="json")
        if original_id is not None
        else _new_clinic_draft(instance)
    )
    draft = dict(existing)
    draft["half_days"] = [dict(item) for item in existing.get("half_days", [])]
    draft["capacity_overrides"] = [dict(item) for item in existing.get("capacity_overrides", [])]
    draft["closure_days"] = [dict(item) for item in existing.get("closure_days", [])]
    draft["allocation_rules"] = [dict(item) for item in existing.get("allocation_rules", [])]

    with (
        ui.dialog() as dialog,
        (
            ui.card()
            .classes("rbs-clinic-editor-dialog p-0 gap-0")
            .style(
                "width:calc(100vw - 48px);max-width:1600px;"
                "height:calc(100vh - 48px);max-height:1000px"
            )
        ),
    ):
        action_label = "Edit Clinic" if original_id else "Add Clinic"
        with ui.row().classes("rbs-clinic-editor-header w-full items-center gap-5 px-5 py-4"):
            clinic_heading = ui.label().classes(
                "rbs-clinic-editor-title rbs-type-dialog-title whitespace-nowrap"
            )
            clinic_heading.bind_text_from(
                draft,
                "name",
                backward=lambda name: (
                    f"{action_label} · {str(name or '').strip() or 'Untitled clinic'}"
                ),
            )
            with (
                ui.tabs()
                .props("dense no-caps inline-label align=left mobile-arrows outside-arrows")
                .classes("rbs-clinic-editor-tabs min-w-0") as editor_tabs
            ):
                details_tab = ui.tab("clinic_details", label="Details", icon="badge")
                allocation_tab = ui.tab(
                    "clinic_allocation",
                    label="Allocation",
                    icon="account_tree",
                )
                capacity_tab = ui.tab(
                    "clinic_capacity",
                    label="Weekly Capacity",
                    icon="groups",
                )
                exceptions_tab = ui.tab(
                    "clinic_exceptions",
                    label="Exceptions",
                    icon="event",
                )
            ui.space()
            ui.button(icon="close", on_click=dialog.close).props(
                "flat round dense aria-label='Close clinic editor'"
            )
        with (
            ui.tab_panels(editor_tabs, value=details_tab)
            .props("animated")
            .classes("rbs-clinic-editor-panels w-full flex-1 min-h-0")
        ):
            with ui.tab_panel(details_tab).classes("h-full p-0"):
                with ui.scroll_area().classes("h-full w-full"):
                    with ui.column().classes("w-full gap-5 p-6"):
                        with ui.column().classes("gap-1"):
                            ui.label("Clinic details").classes("rbs-type-section-title")
                            ui.label(
                                "Set the user-facing name, then choose an institutional "
                                "palette color or enter a custom hex value."
                            ).classes("rbs-type-caption rbs-text-muted")
                        with ui.row().classes("w-full items-end gap-4 flex-wrap"):
                            name = (
                                ui.input("Clinic name", value=str(draft["name"]))
                                .props("outlined")
                                .classes("min-w-72 flex-1")
                            )
                            with ui.column().classes("w-full sm:w-72"):
                                rotation_color_palette(
                                    draft,
                                    instance.color_scheme.palette,
                                    label="Schedule color",
                                    compact=True,
                                    allow_custom=True,
                                )

                        name.bind_value(draft, "name", forward=_as_text)

            with ui.tab_panel(allocation_tab).classes("h-full p-0"):
                with ui.scroll_area().classes("h-full w-full"):
                    with ui.column().classes("w-full gap-5 p-6"):
                        _clinic_owned_allocation_editor(draft, instance)

            with ui.tab_panel(capacity_tab).classes("h-full p-0"):
                with ui.scroll_area().classes("h-full w-full"):
                    with ui.column().classes("w-full gap-5 p-6"):
                        with ui.row().classes("w-full items-end justify-between gap-5 flex-wrap"):
                            with ui.column().classes("min-w-72 flex-1 gap-1"):
                                ui.label("Weekly staffing and capacity").classes(
                                    "rbs-type-section-title"
                                )
                                ui.label(
                                    "Choose staffed half-days from Monday through Sunday. "
                                    "Each maximum is attendings × residents per attending."
                                ).classes("rbs-type-caption rbs-text-muted")
                            ratio = (
                                ui.number(
                                    "Residents per attending",
                                    value=int(draft["residents_per_attending"]),
                                    min=1,
                                    step=1,
                                )
                                .props("outlined")
                                .classes("w-64")
                            )
                        capacity_refresh = _clinic_capacity_grid(draft)

            with ui.tab_panel(exceptions_tab).classes("h-full p-0"):
                with ui.scroll_area().classes("h-full w-full"):
                    with ui.column().classes("w-full gap-6 p-6"):
                        override_refresh = _clinic_capacity_overrides_editor(
                            draft,
                            instance,
                        )
                        ui.separator()
                        _clinic_site_closures_editor(draft, instance)

        def set_ratio(event) -> None:
            draft["residents_per_attending"] = int(event.value) if event.value is not None else 1
            capacity_refresh()
            override_refresh()

        ratio.on_value_change(set_ratio)

        ui.separator()
        with ui.row().classes("w-full justify-end gap-3 p-4"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")

            def save_clinic() -> None:
                try:
                    clinic = ClinicSiteConfig.model_validate(draft)
                    updated = (
                        replace_clinic(instance, original_id, clinic)
                        if original_id is not None
                        else add_clinic(instance, clinic)
                    )
                    dialog.close()
                    ui.notify(
                        "Clinic updated" if original_id else "Clinic added",
                        type="positive",
                    )
                    on_save(updated, selected_rotation_id)
                except (ValidationError, ValueError) as exc:
                    ui.notify(_validation_message(exc), type="negative", multi_line=True)

            ui.button(
                "Save clinic",
                icon="save",
                on_click=save_clinic,
            ).props("unelevated no-caps")
    dialog.open()


def _clinic_owned_allocation_editor(draft: Draft, instance: SchedulerInput) -> None:
    from nicegui import ui

    rules: list[Draft] = draft["allocation_rules"]
    overall = next(
        (rule for rule in rules if rule.get("pgy") is None and rule.get("resident_id") is None),
        None,
    )
    if overall is None:
        overall = {
            "clinic_id": draft["id"],
            "pgy": None,
            "resident_id": None,
            "min_fraction": 0.0,
            "target_fraction": 0.0,
            "max_fraction": 1.0,
        }
        rules.append(overall)

    ui.label("Allocation rules").classes("rbs-type-section-title")
    ui.label(
        "Set this clinic's overall allocation, then add optional training-level or "
        "resident overrides. A resident override takes precedence over its training "
        "level, which takes precedence over the overall rule. Targets are normalized "
        "across clinics."
    ).classes("rbs-type-caption rbs-text-muted")

    with ui.card().props("flat bordered").classes("w-full gap-3 p-4"):
        ui.label("Overall rule").classes("rbs-font-semibold")
        _clinic_allocation_fraction_inputs(overall)

    override_container = ui.column().classes("w-full gap-3")

    def remove_rule(rule: Draft) -> None:
        rules.remove(rule)
        render_overrides()

    def add_pgy_override() -> None:
        if pgy_select.value is None:
            ui.notify("Select a training level", type="warning")
            return
        pgy = int(pgy_select.value)
        if any(rule.get("pgy") == pgy for rule in rules):
            ui.notify(
                f"{instance.training_level_name(pgy)} already has an override",
                type="warning",
            )
            return
        rules.append(
            {
                **overall,
                "pgy": pgy,
                "resident_id": None,
            }
        )
        pgy_select.value = None
        render_overrides()

    def add_resident_override() -> None:
        if resident_select.value is None:
            ui.notify("Select a resident", type="warning")
            return
        resident_id = str(resident_select.value)
        if any(rule.get("resident_id") == resident_id for rule in rules):
            ui.notify("That resident already has an override", type="warning")
            return
        resident = next(item for item in instance.residents if item.id == resident_id)
        base = next(
            (rule for rule in rules if rule.get("pgy") == resident.pgy),
            overall,
        )
        rules.append(
            {
                **base,
                "pgy": None,
                "resident_id": resident_id,
            }
        )
        resident_select.value = None
        render_overrides()

    with ui.row().classes("w-full items-end gap-3 flex-wrap"):
        pgy_select = (
            ui.select(
                instance.training_level_options,
                value=None,
                label="Training-level override",
            )
            .props("outlined options-dense clearable")
            .classes("w-52")
        )
        ui.button(
            "Add training-level override",
            icon="add",
            on_click=add_pgy_override,
        ).props("outline no-caps")
        resident_select = (
            ui.select(
                {
                    resident.id: (f"{resident.name} · {instance.training_level_name(resident.pgy)}")
                    for resident in instance.residents
                },
                value=None,
                label="Resident override",
            )
            .props("outlined options-dense clearable use-input")
            .classes("min-w-72 flex-1")
        )
        ui.button(
            "Add resident override",
            icon="person_add",
            on_click=add_resident_override,
        ).props("outline no-caps")

    def render_overrides() -> None:
        override_container.clear()
        with override_container:
            pgy_rules = sorted(
                (rule for rule in rules if rule.get("pgy") is not None),
                key=lambda rule: instance.training_level_sort_key(int(rule["pgy"])),
            )
            resident_rules = sorted(
                (rule for rule in rules if rule.get("resident_id") is not None),
                key=lambda rule: str(rule["resident_id"]),
            )
            if not pgy_rules and not resident_rules:
                ui.label("No allocation overrides configured for this clinic.").classes(
                    "rbs-type-body rbs-text-muted"
                )
            if pgy_rules:
                ui.label("Training-level overrides").classes(
                    "rbs-type-caption rbs-font-semibold uppercase"
                )
            for rule in pgy_rules:
                with ui.card().props("flat bordered").classes("w-full gap-3 p-4"):
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label(instance.training_level_name(int(rule["pgy"]))).classes(
                            "rbs-font-semibold"
                        )
                        ui.button(
                            icon="delete_outline",
                            on_click=partial(remove_rule, rule),
                        ).props("flat round dense color=negative")
                    _clinic_allocation_fraction_inputs(rule)
            if resident_rules:
                ui.label("Resident overrides").classes(
                    "rbs-type-caption rbs-font-semibold uppercase"
                )
            residents_by_id = instance.residents_by_id
            for rule in resident_rules:
                resident_id = str(rule["resident_id"])
                resident = residents_by_id.get(resident_id)
                label = (
                    f"{resident.name} · {instance.training_level_name(resident.pgy)}"
                    if resident is not None
                    else resident_id
                )
                with ui.card().props("flat bordered").classes("w-full gap-3 p-4"):
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label(label).classes("rbs-font-semibold")
                        ui.button(
                            icon="delete_outline",
                            on_click=partial(remove_rule, rule),
                        ).props("flat round dense color=negative")
                    _clinic_allocation_fraction_inputs(rule)

    render_overrides()


def _clinic_allocation_fraction_inputs(rule: Draft) -> None:
    from nicegui import ui

    with ui.row().classes("w-full items-end gap-3 flex-wrap"):
        minimum = (
            ui.number(
                "Minimum %",
                value=float(rule.get("min_fraction", 0)) * 100,
                min=0,
                max=100,
                step=1,
            )
            .props("outlined suffix=%")
            .classes("w-40")
        )
        target = (
            ui.number(
                "Target %",
                value=float(rule.get("target_fraction", 0)) * 100,
                min=0,
                max=100,
                step=1,
            )
            .props("outlined suffix=%")
            .classes("w-40")
        )
        maximum = (
            ui.number(
                "Maximum %",
                value=float(rule.get("max_fraction", 1)) * 100,
                min=0,
                max=100,
                step=1,
            )
            .props("outlined suffix=%")
            .classes("w-40")
        )
        minimum.bind_value(rule, "min_fraction", forward=_as_percent, backward=_from_percent)
        target.bind_value(rule, "target_fraction", forward=_as_percent, backward=_from_percent)
        maximum.bind_value(rule, "max_fraction", forward=_as_percent, backward=_from_percent)


def _clinic_capacity_grid(draft: Draft) -> Callable[[], None]:
    from nicegui import ui

    capacity_week = (*WEEKDAYS_MF, Weekday.SATURDAY, Weekday.SUNDAY)
    maximum_labels: list[tuple[object, Weekday, Session]] = []

    def by_time() -> dict[tuple[str, str], Draft]:
        return {(str(item["weekday"]), str(item["session"])): item for item in draft["half_days"]}

    def refresh_maximums() -> None:
        rules = by_time()
        ratio = max(int(draft.get("residents_per_attending") or 1), 1)
        for label, weekday, session in maximum_labels:
            rule = rules.get((weekday.value, session.value))
            if rule is None:
                label.set_text("Not staffed")
            else:
                maximum = int(rule.get("attendings") or 1) * ratio
                label.set_text(f"Maximum {maximum} residents")

    with ui.element("div").classes("rbs-clinic-capacity-grid w-full"):
        for weekday in capacity_week:
            with (
                ui.card()
                .props("flat bordered")
                .classes("rbs-clinic-capacity-day min-w-0 gap-3 p-3")
            ):
                ui.label(weekday.value.title()).classes("rbs-font-semibold")
                for session in Session:
                    rule = by_time().get((weekday.value, session.value))
                    with ui.column().classes("w-full gap-2 rounded border p-2"):
                        enabled = ui.checkbox(
                            _SESSION_OPTIONS[session.value],
                            value=rule is not None,
                        ).props("dense")
                        with ui.row().classes("w-full items-end gap-2"):
                            attendings = (
                                ui.number(
                                    "Attendings",
                                    value=int(rule.get("attendings", 1)) if rule else 1,
                                    min=1,
                                    step=1,
                                )
                                .props("outlined dense")
                                .classes("min-w-24 flex-1")
                            )
                            minimum = (
                                ui.number(
                                    "Minimum residents",
                                    value=int(rule.get("min_residents", 0)) if rule else 0,
                                    min=0,
                                    step=1,
                                )
                                .props("outlined dense")
                                .classes("min-w-28 flex-1")
                            )
                        maximum = ui.label().classes("rbs-type-caption rbs-text-muted")
                        maximum_labels.append((maximum, weekday, session))
                        attendings.set_enabled(rule is not None)
                        minimum.set_enabled(rule is not None)

                        def toggle_half_day(
                            event,
                            *,
                            this_weekday=weekday,
                            this_session=session,
                            attending_input=attendings,
                            minimum_input=minimum,
                        ) -> None:
                            rules = by_time()
                            key = (this_weekday.value, this_session.value)
                            if bool(event.value) and key not in rules:
                                draft["half_days"].append(
                                    {
                                        "weekday": this_weekday.value,
                                        "session": this_session.value,
                                        "attendings": int(attending_input.value or 1),
                                        "min_residents": int(minimum_input.value or 0),
                                    }
                                )
                            elif not bool(event.value):
                                draft["half_days"] = [
                                    item
                                    for item in draft["half_days"]
                                    if (
                                        str(item["weekday"]),
                                        str(item["session"]),
                                    )
                                    != key
                                ]
                            attending_input.set_enabled(bool(event.value))
                            minimum_input.set_enabled(bool(event.value))
                            refresh_maximums()

                        def set_capacity_value(
                            field: str,
                            event,
                            *,
                            this_weekday=weekday,
                            this_session=session,
                        ) -> None:
                            item = by_time().get((this_weekday.value, this_session.value))
                            if item is not None:
                                fallback = 1 if field == "attendings" else 0
                                item[field] = int(event.value or fallback)
                            refresh_maximums()

                        enabled.on_value_change(toggle_half_day)
                        attendings.on_value_change(partial(set_capacity_value, "attendings"))
                        minimum.on_value_change(partial(set_capacity_value, "min_residents"))
    refresh_maximums()
    return refresh_maximums


def _clinic_capacity_overrides_editor(
    draft: Draft,
    instance: SchedulerInput,
) -> Callable[[], None]:
    from nicegui import ui

    overrides: list[Draft] = draft["capacity_overrides"]
    first_day = instance.calendar.first_week_start
    last_day = first_day + timedelta(days=instance.calendar.weeks * 7 - 1)
    container = ui.column().classes("w-full gap-3")
    maximum_labels: list[tuple[object, Draft]] = []

    def recurring_capacity(calendar_day: date, session: Session) -> Draft | None:
        weekday = tuple(Weekday)[calendar_day.weekday()]
        return next(
            (
                item
                for item in draft["half_days"]
                if str(item["weekday"]) == weekday.value and str(item["session"]) == session.value
            ),
            None,
        )

    def refresh_maximums() -> None:
        ratio = max(int(draft.get("residents_per_attending") or 1), 1)
        for label, override in maximum_labels:
            maximum = int(override.get("attendings") or 0) * ratio
            label.set_text(f"Maximum {maximum} residents")

    def add_override() -> None:
        used = {(str(item.get("date") or ""), str(item.get("session") or "")) for item in overrides}
        start = min(max(date.today(), first_day), last_day)
        selected: tuple[date, Session] | None = None
        for offset in range((last_day - first_day).days + 1):
            candidate = first_day + timedelta(
                days=((start - first_day).days + offset) % ((last_day - first_day).days + 1)
            )
            for session in Session:
                if (candidate.isoformat(), session.value) not in used:
                    selected = candidate, session
                    break
            if selected is not None:
                break
        if selected is None:
            ui.notify("Every date and session already has an override", type="warning")
            return
        calendar_day, session = selected
        recurring = recurring_capacity(calendar_day, session)
        overrides.append(
            {
                "date": calendar_day.isoformat(),
                "session": session.value,
                "attendings": int(recurring.get("attendings", 1)) if recurring else 1,
                "min_residents": (int(recurring.get("min_residents", 0)) if recurring else 0),
            }
        )
        render()

    def render() -> None:
        container.clear()
        maximum_labels.clear()
        with container:
            with ui.row().classes("w-full items-center justify-between gap-3"):
                with ui.column().classes("gap-0"):
                    ui.label("Specific-day capacity overrides").classes("rbs-type-section-title")
                    ui.label(
                        "Replace one date and half-day's recurring attending coverage and "
                        "minimum. Set attendings to zero to make that half-day unavailable."
                    ).classes("rbs-type-caption rbs-text-muted")
                ui.button(
                    "Add capacity override",
                    icon="add",
                    on_click=add_override,
                ).props(SECONDARY_BUTTON_PROPS)

            if not overrides:
                ui.label("No specific-day capacity overrides configured.").classes(
                    "rbs-type-body rbs-text-muted"
                )
            for index, override in enumerate(overrides):
                with (
                    ui.card()
                    .props("flat bordered")
                    .classes("rbs-clinic-capacity-override w-full p-4")
                ):
                    with ui.row().classes("w-full items-end gap-3 flex-wrap"):
                        override_date = (
                            ui.input(
                                "Override date",
                                value=str(override.get("date") or ""),
                            )
                            .props(
                                f"outlined type=date min={first_day.isoformat()} "
                                f"max={last_day.isoformat()}"
                            )
                            .classes("w-52")
                        )
                        session = (
                            ui.select(
                                _SESSION_OPTIONS,
                                value=str(override.get("session") or Session.MORNING.value),
                                label="Override session",
                            )
                            .props("outlined options-dense")
                            .classes("w-52")
                        )
                        attendings = (
                            ui.number(
                                "Override attendings",
                                value=int(override.get("attendings") or 0),
                                min=0,
                                step=1,
                            )
                            .props("outlined")
                            .classes("w-52")
                        )
                        minimum = (
                            ui.number(
                                "Override minimum residents",
                                value=int(override.get("min_residents") or 0),
                                min=0,
                                step=1,
                            )
                            .props("outlined")
                            .classes("w-60")
                        )
                        maximum = ui.label().classes("min-w-44 pb-3 rbs-type-body rbs-text-muted")
                        maximum_labels.append((maximum, override))
                        ui.button(
                            icon="delete_outline",
                            on_click=partial(
                                _remove_index,
                                overrides,
                                index,
                                render,
                            ),
                        ).props("flat round color=negative aria-label='Remove capacity override'")

                        override_date.bind_value(override, "date", forward=_as_text)
                        session.bind_value(override, "session", forward=_as_text)

                        def set_override_number(
                            field: str,
                            event,
                            *,
                            item=override,
                        ) -> None:
                            item[field] = int(event.value or 0)
                            refresh_maximums()

                        attendings.on_value_change(partial(set_override_number, "attendings"))
                        minimum.on_value_change(partial(set_override_number, "min_residents"))
        refresh_maximums()

    render()
    return refresh_maximums


def _clinic_site_closures_editor(draft: Draft, instance: SchedulerInput) -> None:
    from nicegui import ui

    closures = draft["closure_days"]
    container = ui.column().classes("w-full gap-2")

    def add_closure() -> None:
        first_day = instance.calendar.first_week_start
        last_day = first_day + timedelta(days=instance.calendar.weeks * 7 - 1)
        candidate = min(max(date.today(), first_day), last_day)
        used = {date.fromisoformat(str(item["date"])) for item in closures if item.get("date")}
        while candidate in used and candidate < last_day:
            candidate += timedelta(days=1)
        closures.append(
            {
                "date": candidate.isoformat() if candidate not in used else "",
                "name": "",
            }
        )
        render()

    def render() -> None:
        container.clear()
        with container:
            with ui.row().classes("w-full items-center justify-between gap-3"):
                with ui.column().classes("gap-0"):
                    ui.label("Closure days").classes("rbs-type-section-title")
                    ui.label("These dates close this clinic only.").classes(
                        "rbs-type-caption rbs-text-muted"
                    )
                ui.button(
                    "Add closure day",
                    icon="add",
                    on_click=add_closure,
                ).props("outline no-caps")
            if not closures:
                ui.label("No closure days configured.").classes("rbs-type-body rbs-text-muted")
            for index, closure in enumerate(closures):
                with ui.row().classes("w-full items-end gap-3"):
                    closure_date = (
                        ui.input("Date", value=str(closure.get("date") or ""))
                        .props("outlined type=date")
                        .classes("w-52")
                    )
                    name = (
                        ui.input(
                            "Closure name",
                            value=str(closure.get("name") or ""),
                        )
                        .props("outlined maxlength=120")
                        .classes("flex-1")
                    )
                    ui.button(
                        icon="delete_outline",
                        on_click=partial(_remove_index, closures, index, render),
                    ).props("flat round color=negative aria-label='Remove closure day'")
                    closure_date.bind_value(closure, "date", forward=_as_text)
                    name.bind_value(closure, "name", forward=_as_text)

    render()


def _confirm_remove_clinic(
    instance: SchedulerInput,
    clinic_id: str,
    *,
    selected_rotation_id: str | None,
    on_save: SaveRotation,
) -> None:
    from nicegui import ui

    clinic = instance.clinic_policy.site(clinic_id)
    with ui.dialog() as dialog, ui.card().classes("w-[min(92vw,480px)] p-5"):
        ui.label(f"Remove {clinic.name}?").classes("rbs-type-dialog-title")
        ui.label(
            "Its allocation rule, staffing coverage, and closures will be removed. "
            "Rotation rules that only reference this clinic will fall back to any "
            "remaining clinic."
        ).classes("rbs-type-body rbs-text-muted")
        with ui.row().classes("w-full justify-end gap-3 pt-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")

            def remove() -> None:
                try:
                    updated = remove_clinic(instance, clinic_id)
                    dialog.close()
                    ui.notify("Clinic removed", type="positive")
                    on_save(updated, selected_rotation_id)
                except (ValidationError, ValueError) as exc:
                    ui.notify(_validation_message(exc), type="negative", multi_line=True)

            ui.button(
                "Remove clinic",
                icon="delete_outline",
                on_click=remove,
            ).props("unelevated no-caps color=negative")
    dialog.open()
