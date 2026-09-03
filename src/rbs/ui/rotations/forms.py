"""Core rotation form: identity, staffing, blocks, clinic rules."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial

from pydantic import ValidationError

from rbs.models.color_scheme import DEFAULT_COLOR_SCHEME
from rbs.models.enums import WEEKDAYS_MF, RotationKind, Session, Weekday
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
from rbs.ui.clinic.ops import (
    _default_clinic_rule,
)
from rbs.ui.drafts import Draft
from rbs.ui.editor_common import (
    _CLINIC_WEEK,
    _CONSECUTIVE_OPTIONS,
    _DURATION_OPTIONS,
    _academic_block_start_for_week,
    _academic_block_start_options,
    _as_code,
    _as_int,
    _as_string_list,
    _as_text,
    _capacity_range_label,
    _optional_float,
    _pgy_capacity_label,
    _remove_index,
    _validation_message,
    _weeks_label,
)
from rbs.ui.rotations.ops import (
    replace_standard_rotation,
    rotation_editor_state,
    rotation_from_editor_state,
    rotation_group_members_by_pgy,
)
from rbs.ui.rotations.overrides import (
    _editor_manages_resident_override,
    _resident_rotation_overrides_editor,
    _resident_rotation_overrides_view,
)
from rbs.ui.rotations.summary import (
    _configured_duration_label,
    _elective_block_size_options,
    _elective_policy_summary_chips,
    _rotation_clinic_overview,
    _rotation_identity,
    _rotation_operational_overview,
    _rotation_pgy_overview,
    _rotation_requirement_label,
    _rotation_summary_chip,
)
from rbs.ui.rotations.types import (
    SaveRotation,
)
from rbs.ui.rotations.widgets import (
    add_block_config,
    clinic_week_editor,
    display_site_ids,
    prerequisite_options,
    rotation_color_palette,
    set_block_vacation_allowed,
    toggle_pgy_rule,
)


def _rotation_editor(
    instance: SchedulerInput,
    rotation: Rotation,
    *,
    on_cancel: Callable[[], None],
    on_save: SaveRotation,
) -> None:
    from nicegui import ui

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
    academic_half_day = (
        instance.clinic_policy.academic.weekday,
        instance.clinic_policy.academic.session,
    )
    site_options = {site.id: site.name for site in instance.clinic_policy.sites}
    default_site_ids = list(instance.clinic_policy.site_ids)
    resident_override_drafts = [
        override.model_dump(mode="json")
        for override in instance.resident_rotation_overrides
        if _editor_manages_resident_override(instance, override, rotation.id)
    ]
    group_draft = rotation_group_members_by_pgy(instance, rotation.id)
    clinic_editor = None

    def render_clinic_editor() -> None:
        if clinic_editor is None:
            return
        clinic_editor.clear()
        with clinic_editor:
            _clinic_rule_editor(
                draft,
                "clinic",
                enable_label="Schedule continuity clinic during this rotation",
                show_enable=False,
                disabled=bool(draft.get("no_clinic_hours")),
                academic_half_day=academic_half_day,
                site_options=site_options,
                default_site_ids=default_site_ids,
            )

    save_error = None

    def save() -> None:
        try:
            replacement = rotation_from_editor_state(draft)
            updated = replace_standard_rotation(
                instance,
                rotation.id,
                replacement,
                resident_overrides=resident_override_drafts,
                eligible_as_elective=bool(elective_draft["eligible"]),
                eligible_elective_pgys=[
                    int(pgy) for pgy in elective_draft.get("eligible_pgys", [])
                ],
                eligible_elective_block_sizes=[
                    int(size) for size in elective_draft.get("eligible_block_sizes", [])
                ],
                elective_repeatable=bool(elective_draft.get("repeatable")),
                group_members_by_pgy=group_draft,
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
                    "aria-label='Cancel rotation editing'",
                )
            ):
                ui.tooltip("Cancel rotation editing")
        ui.separator()
        with (
            ui.tabs()
            .props("dense no-caps align=left inline-label mobile-arrows outside-arrows")
            .classes("rbs-rotation-editor-tabs w-full") as editor_tabs
        ):
            general_tab = ui.tab("rotation_general", label="General", icon="tune")
            pgy_tab = ui.tab(
                "rotation_pgy",
                label="Training-level rules",
                icon="groups",
            )
            clinic_tab = ui.tab("rotation_clinic", label="Clinic", icon="event_available")
            advanced_tab = ui.tab("rotation_advanced", label="Advanced", icon="settings")

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
                    )
                    _mandatory_elective_availability(
                        elective_draft,
                        instance,
                    )

            with ui.tab_panel(pgy_tab).classes("p-0"):
                with ui.column().classes("w-full gap-4 p-5"):
                    ui.label("Training-level rules").classes("rbs-type-section-title")
                    _staffing_and_blocks(
                        instance,
                        draft,
                        rotation.id,
                        group_members_by_pgy=group_draft,
                    )

            with ui.tab_panel(clinic_tab).classes("p-0"):
                with ui.column().classes("w-full gap-4 p-5"):
                    with ui.column().classes("gap-0"):
                        ui.label("Continuity clinic").classes("rbs-type-section-title")
                        ui.label(
                            "Choose the weekly clinic workload, allowed half-days, sites, "
                            "and preferred times."
                        ).classes("rbs-type-caption rbs-text-muted")
                    clinic_editor = ui.column().classes("w-full")
                    render_clinic_editor()

            with ui.tab_panel(advanced_tab).classes("p-0"):
                with ui.column().classes("w-full gap-4 p-5"):
                    with ui.column().classes("gap-0"):
                        ui.label("Advanced rules").classes("rbs-type-section-title")
                        ui.label(
                            "Configure resident-specific exceptions without changing the "
                            "training-level curriculum."
                        ).classes("rbs-type-caption rbs-text-muted")
                    with ui.expansion(
                        "Individual exceptions",
                        caption=f"{len(resident_override_drafts)} configured",
                        icon="person_add",
                        value=bool(resident_override_drafts),
                    ).classes("rbs-rotation-section w-full"):
                        _resident_rotation_overrides_editor(
                            instance,
                            rotation,
                            resident_override_drafts,
                        )

        with ui.row().classes(
            "rbs-rotation-editor-actions w-full items-center justify-end gap-2 px-5 py-3"
        ):
            save_error = ui.label().classes(
                "rbs-rotation-save-error min-w-0 flex-1 rbs-type-caption rbs-text-danger"
            )
            ui.button("Cancel", on_click=on_cancel).props("flat no-caps")
            ui.button("Save rotation", icon="save", on_click=save).props("unelevated no-caps")


def _core_settings(
    draft: Draft,
    *,
    palette: Mapping[str, str] | None = None,
    on_clinic_availability_change: Callable[[], None],
    show_color: bool = True,
    show_max_total_weeks: bool = False,
) -> None:
    from nicegui import ui

    if draft.get("away") or not _draft_has_clinic_configuration(draft):
        draft["no_clinic_hours"] = True

    with ui.column().classes("w-full gap-3"):
        ui.label("Core settings").classes("rbs-type-section-title")
        with ui.row().classes("w-full items-start gap-4 flex-wrap lg:flex-nowrap"):
            with ui.column().classes("w-full sm:w-48 gap-1"):
                code = ui.input("Rotation code", value=str(draft.get("code") or ""))
                code.props(f"outlined maxlength={ROTATION_CODE_MAX_LENGTH} counter").classes(
                    "rbs-rotation-code-input w-full"
                )
            name = ui.input("Rotation name", value=str(draft.get("name") or ""))
            name.props("outlined").classes("w-full sm:flex-1")
            consecutive = (
                ui.select(
                    _CONSECUTIVE_OPTIONS,
                    value=int(draft.get("max_consecutive_weeks") or 4),
                    label="Max consecutive weeks",
                )
                .props("outlined options-dense")
                .classes("w-full sm:w-52 sm:shrink-0")
            )
            total = None
            if show_max_total_weeks:
                total = (
                    ui.number(
                        "Maximum total weeks",
                        value=_optional_float(draft.get("max_total_weeks")),
                        min=1,
                        max=52,
                        precision=0,
                        step=1,
                        placeholder="No maximum",
                    )
                    .props("outlined clearable")
                    .classes("w-full sm:w-52 sm:shrink-0")
                )
        code.bind_value(draft, "code", forward=_as_code)
        name.bind_value(draft, "name", forward=_as_text)
        consecutive.bind_value(draft, "max_consecutive_weeks", forward=_as_int)
        if total is not None:
            total.bind_value(draft, "max_total_weeks", forward=_as_int)

        if show_color:
            rotation_color_palette(
                draft,
                palette or DEFAULT_COLOR_SCHEME.palette,
                compact=True,
            )

        with ui.column().classes("rbs-rotation-flags w-full gap-2 rounded p-3"):
            ui.label("Rotation block options").classes("rbs-type-control-label")
            with ui.row().classes("w-full items-center gap-x-8 gap-y-2"):
                away = ui.checkbox("AWAY Rotation", value=bool(draft.get("away")))
                no_clinic = ui.checkbox(
                    "No clinic hours",
                    value=bool(draft.get("no_clinic_hours")),
                )
                no_weekend_call = ui.checkbox(
                    "No weekend call",
                    value=bool(draft.get("no_weekend_call")),
                )
            no_clinic.set_enabled(not bool(draft.get("away")))

        def set_away(event) -> None:
            value = bool(event.value)
            _apply_away_selection(draft, value)
            no_clinic.set_value(value)
            no_clinic.set_enabled(not value)
            on_clinic_availability_change()

        def set_no_clinic(event) -> None:
            value = bool(event.value)
            if draft.get("away"):
                value = True
                no_clinic.set_value(True)
            draft["no_clinic_hours"] = value
            if not value and not _draft_has_clinic_configuration(draft):
                draft["clinic"] = _default_clinic_rule()
            on_clinic_availability_change()

        away.on_value_change(set_away)
        no_clinic.on_value_change(set_no_clinic)
        no_weekend_call.bind_value(draft, "no_weekend_call")


def _staffing_and_blocks(
    instance: SchedulerInput,
    draft: Draft,
    rotation_id: str,
    *,
    requirement_counts: dict[tuple[int, int], int] | None = None,
    group_members_by_pgy: dict[int, list[str]] | None = None,
) -> None:
    from nicegui import ui

    capacity = draft["capacity"]
    with ui.column().classes("w-full gap-4 pt-2"):
        with ui.column().classes("rbs-rotation-editor-subsection w-full gap-3 rounded p-4"):
            with ui.column().classes("gap-0"):
                ui.label("Total concurrent staffing").classes("rbs-type-control-label")
                ui.label(
                    "Minimum and maximum residents across all training levels in each week."
                ).classes("rbs-type-caption rbs-text-muted")
            with ui.row().classes("w-full gap-3"):
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
            minimum.bind_value(capacity, "min_concurrent", forward=_as_int)
            maximum.bind_value(capacity, "max_concurrent", forward=_as_int)
        rules_container = ui.column().classes("w-full gap-3")

        def render_rules() -> None:
            rules_container.clear()
            with rules_container:
                with ui.column().classes("gap-0"):
                    ui.label("Rules by training level").classes("rbs-type-control-label")
                    ui.label(
                        "Year limits do not replace the overall rotation limits above. If a "
                        "training year has no maximum, Maximum total residents still applies. "
                        "Vacation allowance is set per block format."
                    ).classes("rbs-type-caption rbs-text-muted")
                for pgy in instance.training_level_ids:
                    _pgy_rule_editor(
                        instance,
                        draft,
                        rotation_id,
                        pgy,
                        render_rules,
                        requirement_counts=requirement_counts,
                        group_members_by_pgy=group_members_by_pgy,
                    )

        render_rules()


def _pgy_rule_editor(
    instance: SchedulerInput,
    draft: Draft,
    rotation_id: str,
    pgy: int,
    refresh: Callable[[], None],
    *,
    requirement_counts: dict[tuple[int, int], int] | None = None,
    group_members_by_pgy: dict[int, list[str]] | None = None,
) -> None:
    from nicegui import ui

    level_name = instance.training_level_name(pgy)
    rule = next((item for item in draft["pgy_rules"] if int(item["pgy"]) == pgy), None)
    caption = "Not configured"
    if rule is not None:
        durations = ", ".join(
            _weeks_label(int(config["duration_weeks"])) for config in rule["block_configs"]
        )
        details = [durations, _pgy_capacity_label(rule)]
        if rule.get("max_total_weeks") is not None:
            details.append(f"Max {_weeks_label(int(rule['max_total_weeks']))} total")
        if draft.get("kind") != RotationKind.ELECTIVE.value:
            details.insert(
                0,
                _draft_requirement_label(rule, pgy, requirement_counts)
                if requirement_counts is not None
                else _rotation_requirement_label(instance, rotation_id, pgy),
            )
        caption = " · ".join(details)
    with ui.expansion(
        level_name,
        caption=caption,
        icon="school",
        value=rule is not None,
    ).classes("rbs-pgy-rule w-full"):
        enabled = ui.checkbox(
            f"Available to {level_name}",
            value=rule is not None,
        )

        def toggle_rule(event) -> None:
            if not event.value and group_members_by_pgy is not None:
                group_members_by_pgy[pgy] = []
            toggle_pgy_rule(
                draft,
                pgy,
                instance.training_level_ids,
                refresh,
                event,
            )

        enabled.on_value_change(toggle_rule)
        if rule is None:
            return

        with ui.row().classes("w-full gap-3 pt-2"):
            minimum = (
                ui.number(
                    f"Minimum {level_name} residents",
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
                    f"Maximum {level_name} residents",
                    value=_optional_float(rule.get("max_concurrent")),
                    min=0,
                    precision=0,
                    step=1,
                    placeholder="No maximum",
                )
                .props("outlined clearable")
                .classes("w-full sm:flex-1")
            )
            total_weeks = None
            if draft.get("kind") == RotationKind.ELECTIVE.value:
                total_weeks = (
                    ui.number(
                        "Maximum total weeks",
                        value=_optional_float(rule.get("max_total_weeks")),
                        min=1,
                        max=52,
                        precision=0,
                        step=1,
                        placeholder="No maximum",
                    )
                    .props("outlined clearable")
                    .classes("w-full sm:flex-1")
                )
        minimum.bind_value(rule, "min_concurrent", forward=_as_int)
        maximum.bind_value(rule, "max_concurrent", forward=_as_int)
        if total_weeks is not None:
            total_weeks.bind_value(rule, "max_total_weeks", forward=_as_int)

        with ui.column().classes("rbs-rotation-editor-subsection w-full gap-3 rounded p-4"):
            with ui.column().classes("gap-0"):
                ui.label("Placement").classes("rbs-type-control-label")
                ui.label(
                    "Each selected prerequisite needs at least one completed block before "
                    "this rotation starts. Week numbers restart at 1 each academic year."
                ).classes("rbs-type-caption rbs-text-muted")
            with ui.row().classes("w-full items-start gap-3"):
                prerequisites = (
                    ui.select(
                        prerequisite_options(instance, rotation_id, pgy),
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
                            instance.calendar.first_week_start,
                            instance.calendar.weeks,
                        ),
                        value=earliest_start,
                        label="Earliest start block",
                    )
                    .props("outlined options-dense clearable")
                    .classes("w-full sm:w-80")
                )
            prerequisites.bind_value(rule, "prerequisite_rotation_ids", forward=_as_string_list)
            earliest.bind_value(rule, "earliest_start_week", forward=_as_int)

        if group_members_by_pgy is not None and draft.get("kind") == RotationKind.STANDARD.value:
            _rotation_group_editor(
                instance,
                rotation_id,
                pgy,
                group_members_by_pgy,
            )

        with ui.row().classes("w-full items-center justify-between gap-3 pt-2"):
            ui.label("Block configurations").classes("rbs-type-control-label")
            add_button = ui.button(
                "Add block configuration",
                icon="add",
                on_click=partial(add_block_config, rule, refresh),
            ).props("flat dense")
            add_button.set_enabled(len(rule["block_configs"]) < len(_DURATION_OPTIONS))

        for index, config in enumerate(rule["block_configs"]):
            _block_config_editor(
                rule,
                config,
                index,
                refresh,
                pgy=pgy,
                requirement_counts=requirement_counts,
            )


def _rotation_group_editor(
    instance: SchedulerInput,
    rotation_id: str,
    pgy: int,
    members_by_pgy: dict[int, list[str]],
) -> None:
    from nicegui import ui

    options = _rotation_group_member_options(instance, rotation_id, pgy)
    selected = [member for member in members_by_pgy.get(pgy, []) if member != rotation_id]
    with ui.column().classes("rbs-rotation-editor-subsection w-full gap-3 rounded p-4"):
        with ui.column().classes("gap-0"):
            ui.label("Mandatory rotation group").classes("rbs-type-control-label")
            ui.label(
                "Grouped blocks must be consecutive, with no gap. Their order is "
                "unrestricted here; use prerequisites above when order matters."
            ).classes("rbs-type-caption rbs-text-muted")
        members = (
            ui.select(
                options,
                value=selected,
                label="Keep contiguous with",
                multiple=True,
            )
            .props("outlined options-dense use-chips clearable")
            .classes("w-full")
        )
        members.set_enabled(bool(options))

        def update(event) -> None:
            companions = [str(item) for item in (event.value or [])]
            if companions:
                members_by_pgy[pgy] = [rotation_id, *companions]
            else:
                members_by_pgy[pgy] = []

        members.on_value_change(update)


def _rotation_group_member_options(
    instance: SchedulerInput,
    rotation_id: str,
    pgy: int,
) -> dict[str, str]:
    curriculum = instance.curriculum_for(pgy)
    target_count = sum(
        block.count for block in curriculum.blocks if block.rotation_id == rotation_id
    )
    current = instance.rotation_group_for(pgy, rotation_id)
    options = []
    for rotation in instance.rotations:
        if rotation.id == rotation_id or rotation.kind is not RotationKind.STANDARD:
            continue
        try:
            rotation.pgy_rule(pgy)
        except KeyError:
            continue
        count = sum(block.count for block in curriculum.blocks if block.rotation_id == rotation.id)
        other_group = instance.rotation_group_for(pgy, rotation.id)
        if (
            target_count > 0
            and count == target_count
            and (other_group is None or other_group is current)
        ):
            options.append((rotation.code.casefold(), rotation.id, rotation.name))
    return {
        rotation_id: f"{instance.rotation(rotation_id).code} — {name}"
        for _sort, rotation_id, name in sorted(options)
    }


def _draft_requirement_label(
    rule: Draft,
    pgy: int,
    requirement_counts: dict[tuple[int, int], int],
) -> str:
    labels = [
        f"{count} × {_weeks_label(int(config['duration_weeks']))}"
        for config in rule["block_configs"]
        if (count := requirement_counts.get((pgy, int(config["duration_weeks"])), 0))
    ]
    return "Required " + " + ".join(labels) if labels else "Not required program-wide"


def _block_config_editor(
    rule: Draft,
    config: Draft,
    index: int,
    refresh: Callable[[], None],
    *,
    pgy: int | None = None,
    requirement_counts: dict[tuple[int, int], int] | None = None,
) -> None:
    from nicegui import ui

    duration_weeks = int(config["duration_weeks"])
    vacation = config["vacation"]
    with ui.card().props("flat bordered").classes("rbs-block-config w-full p-3 gap-3"):
        with ui.row().classes("w-full items-center gap-3"):
            count = None
            if requirement_counts is not None and pgy is not None:
                count = (
                    ui.number(
                        "Blocks per resident",
                        value=requirement_counts.get((pgy, duration_weeks), 0),
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
                "Vacation may overlap this block",
                value=bool(vacation.get("allowed")),
            )
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
                    .props("outlined")
                    .classes("w-full sm:w-56")
                )
            remove = ui.button(
                icon="delete_outline",
                on_click=(
                    partial(
                        _remove_required_block_config,
                        rule,
                        index,
                        pgy,
                        duration_weeks,
                        requirement_counts,
                        refresh,
                    )
                    if requirement_counts is not None and pgy is not None
                    else partial(_remove_block_config, rule, index, refresh)
                ),
            ).props("flat round dense color=negative aria-label='Remove block configuration'")
            remove.set_enabled(len(rule["block_configs"]) > 1)
        if count is not None and requirement_counts is not None and pgy is not None:
            count.on_value_change(
                partial(
                    _set_required_block_count,
                    requirement_counts,
                    pgy,
                    duration_weeks,
                )
            )
            duration.on_value_change(
                partial(
                    _change_required_block_duration,
                    config,
                    pgy,
                    duration_weeks,
                    requirement_counts,
                    refresh,
                )
            )
        else:
            duration.bind_value(config, "duration_weeks", forward=_as_int)
        allowed.on_value_change(partial(set_block_vacation_allowed, vacation, refresh))
        if maximum is not None:
            maximum.bind_value(vacation, "max_weeks_per_block", forward=_as_int)


def _clinic_rule_editor(
    parent: Draft,
    key: str,
    *,
    enable_label: str,
    show_enable: bool = True,
    disabled: bool = False,
    academic_half_day: tuple[Weekday | None, Session | None] | None = None,
    site_options: dict[str, str],
    default_site_ids: list[str],
) -> None:
    from nicegui import ui

    if disabled:
        ui.label(
            "Clinic scheduling is disabled by No clinic hours. Saved clinic settings "
            "will be retained."
        ).classes("rbs-type-body rbs-text-muted")
        return
    if not show_enable and not isinstance(parent.get(key), dict):
        ui.label("No continuity clinic rule is configured.").classes("rbs-type-body rbs-text-muted")
        return

    enabled = ui.checkbox(enable_label, value=parent.get(key) is not None) if show_enable else None
    details = ui.column().classes("w-full min-w-0 max-w-full gap-4 pt-2")

    def render_details() -> None:
        details.clear()
        rule = parent.get(key)
        if not isinstance(rule, dict):
            return
        with details:
            with ui.row().classes(
                "w-full min-w-0 max-w-full flex-nowrap items-center gap-x-5 overflow-x-auto pb-1"
            ):
                half_days = (
                    ui.number(
                        "Half days per week",
                        value=float(rule.get("half_days_per_week", 1)),
                        min=0,
                        precision=0,
                        step=1,
                    )
                    .props("outlined dense")
                    .classes("w-40 shrink-0")
                )
                unique = ui.checkbox(
                    "Concurrent residents need different slots",
                    value=_as_int(rule.get("max_concurrent")) == 1,
                ).classes("shrink-0")
                admin = (
                    ui.number(
                        "Admin half-days per week",
                        value=int(rule.get("admin_half_days_per_week", 0)),
                        min=0,
                        max=len(rule.get("slots") or []),
                        precision=0,
                        step=1,
                    )
                    .props("outlined dense")
                    .classes("rbs-admin-half-days-field w-64 shrink-0")
                )
                no_academic = ui.checkbox(
                    "No academic day attendance",
                    value=bool(rule.get("no_academic_day_attendance")),
                ).classes("shrink-0")
            half_days.bind_value(rule, "half_days_per_week", forward=_as_int)
            unique.on_value_change(partial(_set_unique_clinic_slots, rule))
            admin.bind_value(rule, "admin_half_days_per_week", forward=_as_int)
            no_academic.bind_value(rule, "no_academic_day_attendance")

            slots = [
                slot
                for slot in rule.get("slots") or []
                if academic_half_day
                != (
                    Weekday(str(slot.get("weekday"))),
                    Session(str(slot.get("session"))),
                )
            ]
            slot_count = len(slots)
            slot_caption = (
                "No half-days configured"
                if slot_count == 0
                else f"{slot_count} half-day"
                if slot_count == 1
                else f"{slot_count} half-days"
            )
            with ui.expansion(
                "Allowed clinic half-days",
                caption=slot_caption,
                icon="calendar_view_week",
                value=True,
            ).classes("rbs-clinic-slots-expansion w-full min-w-0 max-w-full"):
                with ui.column().classes("w-full min-w-0 max-w-full gap-2 pt-2"):
                    ui.label(
                        "Preferred half-days are a soft solver goal; every enabled half-day "
                        "remains an allowed fallback."
                    ).classes("rbs-type-caption rbs-text-muted")
                    with ui.row().classes("w-full items-center justify-end gap-2 flex-wrap"):
                        ui.button(
                            "Edit all weekdays",
                            icon="date_range",
                            on_click=partial(
                                _open_bulk_clinic_sites_dialog,
                                rule,
                                tuple(WEEKDAYS_MF),
                                "Edit all weekdays",
                                render_details,
                                academic_half_day,
                                site_options,
                                default_site_ids,
                            ),
                        ).props("flat dense")
                        ui.button(
                            "Edit all days",
                            icon="calendar_month",
                            on_click=partial(
                                _open_bulk_clinic_sites_dialog,
                                rule,
                                _CLINIC_WEEK,
                                "Edit all days",
                                render_details,
                                academic_half_day,
                                site_options,
                                default_site_ids,
                            ),
                        ).props("flat dense")
                    clinic_week_editor(
                        rule,
                        academic_half_day=academic_half_day,
                        site_options=site_options,
                        default_site_ids=default_site_ids,
                    )

    def toggle(event) -> None:
        if event.value:
            parent[key] = _default_clinic_rule()
        else:
            parent[key] = None
        render_details()

    if enabled is not None:
        enabled.on_value_change(toggle)
    render_details()


def _set_unique_clinic_slots(rule: Draft, event) -> None:
    """Bridge the compact generic checkbox to the numeric concurrency model."""
    rule.pop("unique_among_concurrent", None)
    if event.value:
        rule["max_concurrent"] = 1
    elif _as_int(rule.get("max_concurrent")) == 1:
        rule["max_concurrent"] = None


def _apply_clinic_sites(
    rule: Draft,
    weekdays: tuple[Weekday, ...],
    sites: list[str],
    academic_half_day: tuple[Weekday | None, Session | None] | None = None,
) -> int:
    if not sites:
        raise ValueError("Select at least one clinic site.")
    allowed_days = {weekday.value for weekday in weekdays}
    updated = 0
    for slot in rule.get("slots", []):
        if slot.get("weekday") not in allowed_days:
            continue
        if academic_half_day == (
            Weekday(str(slot.get("weekday"))),
            Session(str(slot.get("session"))),
        ):
            continue
        slot["sites"] = list(dict.fromkeys(str(site) for site in sites))
        updated += 1
    return updated


def _open_bulk_clinic_sites_dialog(
    rule: Draft,
    weekdays: tuple[Weekday, ...],
    title: str,
    refresh: Callable[[], None],
    academic_half_day: tuple[Weekday | None, Session | None] | None,
    site_options: dict[str, str],
    default_site_ids: list[str],
) -> None:
    from nicegui import ui

    allowed_days = {weekday.value for weekday in weekdays}
    current = display_site_ids(
        list(
            dict.fromkeys(
                site
                for slot in rule.get("slots", [])
                if slot.get("weekday") in allowed_days
                for site in slot.get("sites", [])
            )
        ),
        default_site_ids,
    )
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-lg gap-4 p-5"):
        ui.label(title).classes("rbs-type-dialog-title")
        sites = (
            ui.select(
                site_options,
                value=current,
                label="Clinic sites",
                multiple=True,
            )
            .props("outlined options-dense use-chips")
            .classes("w-full")
        )
        status = ui.label().classes("rbs-type-caption rbs-text-danger")

        def apply() -> None:
            try:
                updated = _apply_clinic_sites(
                    rule,
                    weekdays,
                    [str(site) for site in (sites.value or [])],
                    academic_half_day,
                )
            except ValueError as exc:
                status.set_text(str(exc))
                return
            if updated == 0:
                status.set_text("Select at least one half-day in this range first.")
                return
            dialog.close()
            refresh()

        with ui.row().classes("w-full items-center justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props(TERTIARY_BUTTON_PROPS)
            ui.button("Apply sites", icon="done", on_click=apply).props(PRIMARY_BUTTON_PROPS)
    dialog.open()


def _draft_has_clinic_configuration(draft: Draft) -> bool:
    return isinstance(draft.get("clinic"), dict)


def _apply_away_selection(draft: Draft, away: bool) -> None:
    draft["away"] = away
    draft["no_clinic_hours"] = away
    if not away and not _draft_has_clinic_configuration(draft):
        draft["clinic"] = _default_clinic_rule()


def _remove_block_config(
    rule: Draft,
    index: int,
    refresh: Callable[[], None],
) -> None:
    if len(rule["block_configs"]) <= 1:
        return
    _remove_index(rule["block_configs"], index, refresh)


def _set_required_block_count(
    requirement_counts: dict[tuple[int, int], int],
    pgy: int,
    duration_weeks: int,
    event,
) -> None:
    requirement_counts[pgy, duration_weeks] = max(
        0,
        int(event.value) if event.value is not None else 0,
    )


def _change_required_block_duration(
    config: Draft,
    pgy: int,
    previous_duration: int,
    requirement_counts: dict[tuple[int, int], int],
    refresh: Callable[[], None],
    event,
) -> None:
    new_duration = int(event.value)
    count = requirement_counts.pop((pgy, previous_duration), 0)
    requirement_counts[pgy, new_duration] = count
    config["duration_weeks"] = new_duration
    refresh()


def _remove_required_block_config(
    rule: Draft,
    index: int,
    pgy: int,
    duration_weeks: int,
    requirement_counts: dict[tuple[int, int], int],
    refresh: Callable[[], None],
) -> None:
    if len(rule["block_configs"]) <= 1:
        return
    requirement_counts.pop((pgy, duration_weeks), None)
    _remove_index(rule["block_configs"], index, refresh)


def _mandatory_elective_availability(
    draft: Draft,
    instance: SchedulerInput,
) -> None:
    """Render explicit per-level eligibility and repeatability controls."""
    from nicegui import ui

    available_block_sizes = instance.elective_block_sizes
    draft["eligible_pgys"] = sorted(int(pgy) for pgy in draft.get("eligible_pgys", []))
    draft["eligible"] = bool(draft["eligible_pgys"])

    with ui.column().classes(
        "rbs-rotation-flags rbs-mandatory-elective-availability w-full gap-3 rounded p-3"
    ):
        ui.label("Elective availability").classes("rbs-type-control-label")
        ui.label(
            "Choose each training level that may use this service for direct Elective time."
        ).classes("rbs-type-caption rbs-text-muted")
        pgy_controls: dict[int, object] = {}
        with ui.row().classes("w-full items-center gap-x-5 gap-y-1 flex-wrap"):
            for pgy in instance.training_level_ids:
                control = ui.checkbox(
                    f"Available to {instance.training_level_name(pgy)} as an elective",
                    value=pgy in draft["eligible_pgys"],
                )
                control.set_enabled(bool(available_block_sizes))
                pgy_controls[pgy] = control
        block_sizes = (
            ui.select(
                _elective_block_size_options(available_block_sizes),
                value=list(draft.get("eligible_block_sizes") or []),
                label="Eligible elective block sizes",
                multiple=True,
            )
            .props("outlined options-dense use-chips")
            .classes("w-full")
        )
        block_sizes.bind_value(draft, "eligible_block_sizes")
        repeatable = ui.checkbox(
            "Can be taken more than once as an elective",
            value=bool(draft.get("repeatable")),
        )
        repeatable.bind_value(draft, "repeatable")

        def refresh_enabled() -> None:
            enabled = bool(draft.get("eligible_pgys"))
            draft["eligible"] = enabled
            block_sizes.set_enabled(enabled and bool(available_block_sizes))
            repeatable.set_enabled(enabled)

        def toggle_pgy(pgy: int, enabled: bool) -> None:
            selected = {int(value) for value in draft.get("eligible_pgys", [])}
            if enabled:
                selected.add(pgy)
            else:
                selected.discard(pgy)
            draft["eligible_pgys"] = sorted(selected)
            refresh_enabled()

        for pgy, control in pgy_controls.items():
            control.on_value_change(
                lambda event, selected_pgy=pgy: toggle_pgy(
                    selected_pgy,
                    bool(event.value),
                )
            )
        refresh_enabled()


def _direct_elective_weeks(instance: SchedulerInput, pgy: int) -> int:
    return sum(
        block.duration_weeks * block.count
        for block in instance.curriculum_for(pgy).blocks
        if instance.rotation(block.rotation_id).kind is RotationKind.ELECTIVE
    )


def _rotation_detail_contents(
    instance: SchedulerInput,
    rotation: Rotation,
    *,
    on_edit_pgy_rules: Callable[[], None] | None = None,
) -> None:
    from nicegui import ui

    with ui.column().classes("rbs-rotation-view w-full gap-5 p-5"):
        with ui.row().classes("rbs-rotation-summary-chips w-full gap-2 flex-wrap"):
            for rule in rotation.pgy_rules:
                _rotation_summary_chip(instance.training_level_label(rule.pgy, compact=True))
            for group in instance.rotation_groups:
                if rotation.id not in group.rotation_ids:
                    continue
                companions = ", ".join(
                    instance.rotation(member).code
                    for member in group.rotation_ids
                    if member != rotation.id
                )
                _rotation_summary_chip(
                    f"{instance.training_level_label(group.pgy, compact=True)} "
                    f"grouped with {companions}"
                )
            _rotation_summary_chip(_configured_duration_label(rotation))
            combined_capacity = _capacity_range_label(
                rotation.capacity.min_concurrent,
                rotation.capacity.max_concurrent,
            )
            if combined_capacity != "No minimum or maximum":
                _rotation_summary_chip(combined_capacity)
            if rotation.kind is RotationKind.STANDARD:
                if instance.is_elective_option(rotation.id):
                    _elective_policy_summary_chips(instance, rotation.id)
                else:
                    _rotation_summary_chip("Mandatory only")
            elif rotation.kind is RotationKind.FMED:
                if instance.is_elective_option(rotation.id):
                    _elective_policy_summary_chips(instance, rotation.id)
                else:
                    _rotation_summary_chip("FMED only")
            elif rotation.kind is RotationKind.ELECTIVE:
                _rotation_summary_chip("Standalone elective")
                _elective_policy_summary_chips(instance, rotation.id)
            if rotation.clinic_hours_disabled:
                _rotation_summary_chip("No clinic hours")
            elif rotation.clinic is not None:
                count = rotation.clinic.half_days_per_week
                _rotation_summary_chip(
                    f"Clinic {count} {'half-day' if count == 1 else 'half-days'} per week"
                )

        _rotation_pgy_overview(
            instance,
            rotation,
            on_edit=on_edit_pgy_rules,
        )
        with ui.element("div").classes("rbs-rotation-overview-grid w-full"):
            _rotation_clinic_overview(instance, rotation)
            _rotation_operational_overview(rotation)
        _resident_rotation_overrides_view(instance, rotation)
