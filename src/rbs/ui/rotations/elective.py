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
    direct_elective_counts,
    elective_rotations,
    next_mandatory_rotation_id,
    remove_elective_rotation,
    replace_elective_color,
    replace_elective_rotation,
    rotation_editor_state,
    rotation_from_editor_state,
    set_elective_allocation,
)
from rbs.ui.rotations.summary import (
    _elective_block_size_options,
    _rotation_identity,
    _rotation_overview_row,
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

    creating = selected_rotation_id == NEW_ELECTIVE_ROTATION_ID

    with ui.column().classes("w-full gap-4"):
        with master_detail.detail_card():
            with ui.column().classes("w-full gap-3 p-5"):
                with ui.row().classes("w-full items-start justify-between gap-3"):
                    with ui.column().classes("gap-0"):
                        ui.label("Shared elective properties").classes("rbs-type-section-title")
                    ui.button(
                        "Edit shared properties",
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
            )


def _elective_shared_summary(instance: SchedulerInput) -> None:
    """Report the shared Elective color and each level's committed Elective time.

    Elective time is the one requirement a program allocates without naming a
    service, so it has no rotation of its own to be edited from. Without this
    surface a level's Elective blocks could only be created as a side effect of
    adding a Mandatory requirement or enabling an option.
    """
    from nicegui import ui

    with ui.column().classes("rbs-elective-shared-summary w-full gap-4"):
        with ui.column().classes("gap-1"):
            ui.label("Block schedule color").classes(
                "rbs-type-caption rbs-font-semibold uppercase rbs-text-muted"
            )
            with ui.row().classes("items-center gap-2"):
                ui.element("span").classes("rbs-rotation-color-swatch").style(
                    f"--rbs-rotation-choice-color:{instance.electives.color}"
                )
                ui.label(instance.electives.color).classes("rbs-type-body")
        with ui.column().classes("w-full gap-2"):
            ui.label("Elective time").classes(
                "rbs-type-caption rbs-font-semibold uppercase rbs-text-muted"
            )
            if not instance.requirements:
                ui.label("No training levels are configured yet.").classes(
                    "rbs-type-body rbs-text-muted"
                )
                return
            with ui.element("div").classes("rbs-rotation-pgy-grid w-full"):
                for curriculum in instance.requirements:
                    _elective_time_level_summary(instance, curriculum.pgy)


def _elective_time_level_summary(instance: SchedulerInput, pgy: int) -> None:
    """One training level's committed Elective blocks and its unscheduled time."""
    from nicegui import ui

    committed = direct_elective_counts(instance, pgy)
    with (
        ui.card()
        .props("flat bordered")
        .classes("rbs-rotation-overview-card rbs-rotation-pgy-card gap-3 p-4")
    ):
        with ui.row().classes("w-full items-start justify-between gap-3"):
            ui.label(instance.training_level_name(pgy)).classes("rbs-type-control-label")
            ui.badge(
                _weeks_label(sum(duration * count for duration, count in committed.items())),
                color="secondary",
            ).props("outline")
        _rotation_overview_row(
            "Elective blocks",
            "; ".join(
                f"{count} × {_weeks_label(duration)}" for duration, count in committed.items()
            )
            or "None committed",
            icon="view_week",
        )
        _rotation_overview_row(
            "Unscheduled",
            _weeks_label(instance.unallocated_weeks(pgy)),
            icon="event_available",
        )


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
    footer: dict[str, object] = {"save": None}

    def refresh_save() -> None:
        save = footer["save"]
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
            dialog.close()
            ui.notify("Shared elective properties updated", type="positive")
            # A color-only edit leaves a solved schedule valid.
            if updated == recolored and on_color_save is not None:
                on_color_save(updated, selected_rotation_id)
            else:
                on_save(updated, selected_rotation_id)
        except (ValidationError, ValueError) as exc:
            ui.notify(_validation_message(exc), type="negative", multi_line=True)

    with (
        ui.dialog() as dialog,
        ui.card()
        .classes("rbs-elective-properties-dialog p-0 gap-0")
        .style("width:calc(100vw - 64px);max-width:720px;max-height:calc(100vh - 64px)"),
    ):
        with ui.row().classes("w-full items-center justify-between gap-3 px-5 py-4"):
            ui.label("Edit shared elective properties").classes("rbs-type-dialog-title")
            ui.button(icon="close", on_click=dialog.close).props(
                button_props(ICON_BUTTON_PROPS, "aria-label='Close shared elective properties'")
            )
        ui.separator()
        # The card is sized by its content, so a flex-1 scroll area would have no
        # height to resolve against. Cap and scroll the content column instead.
        with (
            ui.column()
            .classes("w-full gap-4 p-5")
            .style("overflow-y:auto;max-height:calc(100vh - 220px)")
        ):
            rotation_color_palette(
                color_draft,
                instance.color_scheme.palette,
            )
            with ui.column().classes("w-full gap-3"):
                with ui.column().classes("gap-0"):
                    ui.label("Elective time").classes("rbs-type-control-label")
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
                    )
        ui.separator()
        with ui.row().classes("w-full justify-end gap-3 p-4"):
            ui.button("Cancel", on_click=dialog.close).props(TERTIARY_BUTTON_PROPS)
            footer["save"] = ui.button(
                "Save elective properties",
                icon="save",
                on_click=save_properties,
            ).props(PRIMARY_BUTTON_PROPS)
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
) -> None:
    """Edit one training level's Elective slots against the weeks it can spend."""
    from nicegui import ui

    with (
        ui.card()
        .props("flat bordered")
        .classes("rbs-rotation-nested-card rbs-elective-time-level w-full gap-3 p-4")
    ):
        with ui.row().classes("w-full items-center justify-between gap-3"):
            ui.label(instance.training_level_name(pgy)).classes("rbs-font-semibold")
            budget = ui.label().classes("rbs-type-caption rbs-text-muted")
        body = ui.column().classes("w-full gap-3")

        def planned_weeks() -> int:
            return sum(int(row["duration_weeks"]) * int(row["count"]) for row in rows)

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
            budget.set_text(f"{max(remaining, 0)} unscheduled · {planned} Elective")
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
                _elective_rotation_editor(
                    instance,
                    None,
                    on_cancel=partial(on_select, None),
                    on_save=on_save,
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
    size_draft: Draft = {
        "eligible_block_sizes": (
            list(instance.eligible_elective_block_sizes(rotation.id))
            if rotation is not None
            else list(instance.elective_block_sizes)
        ),
    }
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

    def save() -> None:
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
                int(size) for size in size_draft.get("eligible_block_sizes", [])
            ]
            if not eligible_block_sizes:
                raise ValueError("select at least one eligible Elective block size")
            updated = (
                add_elective_rotation(
                    instance,
                    replacement,
                    eligible_block_sizes=eligible_block_sizes,
                )
                if creating
                else replace_elective_rotation(
                    instance,
                    rotation.id,
                    replacement,
                    eligible_pgys=_elective_rule_pgys(draft),
                    eligible_block_sizes=eligible_block_sizes,
                )
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
            if creating:
                with ui.column().classes("min-w-0 gap-1"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label("New elective").classes("rbs-type-page-title")
                        ui.badge("Creating", color="secondary").props("outline")
            else:
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
                    _staffing_and_blocks(
                        instance,
                        draft,
                        rotation.id if rotation is not None else str(draft["id"]),
                    )

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
