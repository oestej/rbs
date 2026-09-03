"""Scheduling configuration and the reusable workspace color editor."""

from __future__ import annotations

from datetime import date

from pydantic import ValidationError

from rbs.academic_year import rebase_academic_year, rebase_week_start, week_start_choices
from rbs.clinic_locks import automatic_clinic_lock_count
from rbs.logging import get_logger
from rbs.models.color_scheme import (
    ColorScheme,
    generate_accent_colors,
    normalize_hex_color,
)
from rbs.models.instance import ObjectiveWeights, SolverConfig
from rbs.models.workspace import Workspace
from rbs.repository import WorkspaceRepository
from rbs.solver.validation import validate_schedule_or_raise
from rbs.ui import page_shells
from rbs.ui.locks import THROUGH_TODAY_SOURCE, set_lock_through_today
from rbs.ui.settings.color_scheme import replace_color_scheme
from rbs.ui.settings.training_levels import training_level_settings
from rbs.workspaces import WorkspaceController

_WEIGHT_FIELDS = (
    ("clinic_block_week_evenness", "Even Clinic blocks across the year"),
    ("clinic_kind_pgy_spread", "Even Clinic blocks within each training level"),
    ("attending_sessions", "Attending sessions to staff"),
    ("preferred_clinic_slots", "Honor preferred clinic half-days"),
    ("within_week_evenness", "Even clinic load inside a week"),
    ("primary_site_week_evenness", "Even primary-site attendings per week"),
    ("session_pgy_mix", "Mix training years within a session"),
)


def _settings_tab(
    store: WorkspaceRepository,
    workspace: Workspace,
    state: dict,
    persist_instance,
    redraw,
    *,
    schedule_is_current: bool = True,
    active_section: str = "settings_general",
    on_section_change=None,
    apply_theme=None,
) -> None:
    from nicegui import ui

    with page_shells.configuration(
        "Configuration",
        subtitle="Configure scheduling behavior, training levels, and solver options.",
        max_width="max-w-4xl",
    ):
        with (
            ui.tabs(on_change=on_section_change)
            .props("dense no-caps align=left")
            .classes("rbs-configuration-tabs w-full") as tabs
        ):
            general_tab = ui.tab("settings_general", label="General")
            training_levels_tab = ui.tab(
                "settings_training_levels",
                label="Training levels",
            )
            advanced_tab = ui.tab("settings_advanced", label="Advanced")
        sections = {
            "settings_general": general_tab,
            "settings_training_levels": training_levels_tab,
            "settings_advanced": advanced_tab,
        }
        with (
            ui.tab_panels(tabs, value=sections.get(active_section, general_tab))
            .props("animated")
            .classes("rbs-configuration-panels w-full")
        ):
            with ui.tab_panel(general_tab).classes("p-0 pt-4"):
                _general_settings(
                    store,
                    workspace,
                    state,
                    persist_instance,
                    redraw,
                    apply_theme,
                    schedule_is_current=schedule_is_current,
                )
            with ui.tab_panel(training_levels_tab).classes("p-0 pt-4"):
                training_level_settings(
                    workspace,
                    persist_instance,
                    schedule_is_current=schedule_is_current,
                )
            with ui.tab_panel(advanced_tab).classes("p-0 pt-4"):
                _advanced_settings(
                    workspace,
                    persist_instance,
                    schedule_is_current=schedule_is_current,
                    automatic_num_workers=_application_settings_io(state) is not None,
                )


def _general_settings(
    store: WorkspaceRepository,
    workspace: Workspace,
    state: dict,
    persist_instance,
    redraw,
    apply_theme,
    *,
    schedule_is_current: bool,
) -> None:
    """Settings that shape the schedule. Workspace identity lives on its own tab."""
    from nicegui import ui

    instance = workspace.instance
    schedule = workspace.schedule
    today = date.today()
    automatic_count = sum(lock.source == THROUGH_TODAY_SOURCE for lock in instance.locks)
    automatic_clinic_count = automatic_clinic_lock_count(
        instance,
        schedule,
        today=today,
    )
    settings_io = _application_settings_io(state)

    async def save_application_settings() -> None:
        if settings_io is None:
            return
        try:
            destination = await settings_io.save_settings()
        except Exception as exc:
            get_logger("settings").error(
                "settings.export_failed",
                error_code=type(exc).__name__,
                exc_info=True,
            )
            ui.notify(f"Save settings failed: {exc}", type="negative", multi_line=True)
            return
        if destination is None:
            get_logger("settings").info("settings.export_cancelled")
            ui.notify("Save settings cancelled - nothing was written", type="info")
            return
        get_logger("settings").info("settings.exported")
        ui.notify(f"Settings saved to {destination.name}", type="positive")

    async def load_application_settings() -> None:
        if settings_io is None:
            return
        if bool(getattr(state, "solving", False)):
            ui.notify(
                "Wait for the current solve to finish before loading settings.",
                type="warning",
            )
            return
        try:
            loaded = await settings_io.load_settings()
        except Exception as exc:
            get_logger("settings").error(
                "settings.import_failed",
                error_code=type(exc).__name__,
                exc_info=True,
            )
            ui.notify(f"Load settings failed: {exc}", type="negative", multi_line=True)
            return
        if loaded is None:
            get_logger("settings").info("settings.import_cancelled")
            ui.notify("Load settings cancelled - nothing was changed", type="info")
            return
        mark_stale = getattr(state, "mark_stale", None)
        if callable(mark_stale):
            mark_stale()
        touch = getattr(state, "touch", None)
        if callable(touch):
            touch()
        if apply_theme is not None:
            apply_theme(loaded.instance.color_scheme)
        get_logger("settings").info("settings.imported")
        ui.notify("Settings loaded", type="positive")
        refresh_visible = getattr(state, "refresh_visible", None)
        if callable(refresh_visible):
            refresh_visible()
        else:
            redraw()

    def toggle_lock_through_today(event) -> None:
        enabled = bool(event.value)
        try:
            updated = set_lock_through_today(
                instance,
                schedule,
                today,
                enabled=enabled,
            )
            persist_instance(updated, preserve_schedule=schedule_is_current)
        except (ValidationError, ValueError) as exc:
            ui.notify(str(exc), type="negative")

    current_start = instance.calendar.first_week_start
    start_options = week_start_choices(
        current_start, academic_year=instance.academic_year
    )

    def change_week_start(event) -> None:
        try:
            day = date.fromisoformat(str(event.value))
        except ValueError:
            ui.notify("Pick a Monday from the list", type="warning")
            week_start.value = current_start.isoformat()
            return
        try:
            saved, changed = save_general_workspace_settings(
                store,
                workspace,
                name=workspace.name,
                academic_year=instance.academic_year,
                first_week_start=day,
            )
        except (ValidationError, ValueError) as exc:
            ui.notify(str(exc), type="negative")
            week_start.value = current_start.isoformat()
            return
        if not changed:
            return
        touch = getattr(state, "touch", None)
        if callable(touch):
            touch()
        mark_stale = getattr(state, "mark_stale", None)
        if callable(mark_stale):
            mark_stale()
        if saved.solution_is_out_of_date:
            ui.notify(
                "Annual calendar start date saved — the schedule is now out of date",
                type="positive",
            )
        else:
            ui.notify("Annual calendar start date saved", type="positive")
        refresh_visible = getattr(state, "refresh_visible", None)
        if callable(refresh_visible):
            refresh_visible()
        else:
            redraw()

    with ui.column().classes("w-full gap-4"):
        with ui.card().props("flat bordered").classes("w-full p-0"):
            with ui.column().classes("w-full gap-3 p-5"):
                ui.label("Annual calendar").classes("rbs-type-section-title")
                week_start = ui.select(
                    {
                        day.isoformat(): day.strftime("%a %b %d, %Y")
                        for day in start_options
                    },
                    value=current_start.isoformat(),
                    label="Annual calendar start date",
                    on_change=change_week_start,
                ).props("outlined").classes("w-full sm:w-64")
                ui.label(
                    "Week 1 begins on the selected Monday. Dates you entered keep "
                    "their calendar dates while week numbers re-map around the new start."
                ).classes("rbs-type-body rbs-text-muted")
        if settings_io is not None:
            with ui.row().classes("w-full items-center gap-2"):
                ui.button(
                    "Save settings",
                    icon="download",
                    on_click=save_application_settings,
                ).props("outline no-caps")
                ui.button(
                    "Load settings",
                    icon="upload_file",
                    on_click=load_application_settings,
                ).props("outline no-caps")
        with ui.card().props("flat bordered").classes("w-full p-0"):
            with ui.column().classes("w-full gap-3 p-5"):
                ui.label("Automatic schedule locking").classes("rbs-type-section-title")
                ui.checkbox(
                    "Automatically lock blocks and clinic sessions through today",
                    value=instance.lock_through_today,
                    on_change=toggle_lock_through_today,
                )
                ui.label(
                    f"Every solved block beginning on or before {today:%b %d, %Y} is "
                    "locked for its complete span, including the current block. Clinic "
                    "sessions on or before that date are locked individually and can be "
                    "manually unlocked before moving them."
                ).classes("rbs-type-body rbs-text-muted")
                if instance.lock_through_today:
                    status = (
                        f"{automatic_count} rotation blocks and {automatic_clinic_count} "
                        "clinic sessions are currently locked automatically."
                        if schedule is not None
                        else "Solve once to create automatic schedule locks."
                    )
                    with ui.row().classes(
                        "rbs-settings-lock-status w-full items-center gap-2 rounded p-3"
                    ):
                        ui.icon("lock_clock").classes("rbs-text-secondary")
                        ui.label(status).classes("rbs-type-body")


def _application_settings_io(state):
    """Return native settings-file commands without importing desktop modules."""
    host = getattr(state, "workspace_host", None)
    documents = getattr(host, "document_io", None)
    if not bool(getattr(documents, "supports_application_settings", False)):
        return None
    if not callable(getattr(documents, "save_settings", None)):
        return None
    if not callable(getattr(documents, "load_settings", None)):
        return None
    return documents


def save_general_workspace_settings(
    store: WorkspaceRepository,
    workspace: Workspace,
    *,
    name: str,
    academic_year: str,
    first_week_start: date | None = None,
) -> tuple[Workspace, bool]:
    """Persist General settings and revive a compatible prior solve when possible."""
    updated_instance = rebase_academic_year(workspace.instance, academic_year)
    if first_week_start is not None:
        updated_instance = rebase_week_start(updated_instance, first_week_start)
    saved = workspace
    changed = False
    if updated_instance != workspace.instance:
        prior_schedule = workspace.latest_schedule
        saved = WorkspaceController(store).save_instance(workspace, updated_instance)
        changed = True
        if prior_schedule is not None:
            try:
                validate_schedule_or_raise(updated_instance, prior_schedule)
            except ValueError:
                pass
            else:
                saved = WorkspaceController(store).save_schedule(saved, prior_schedule)
    normalized_name = name.strip() or "Untitled"
    if normalized_name != saved.name:
        saved = WorkspaceController(store).rename(saved, normalized_name)
        changed = True
    return saved, changed


def _colors_settings(
    workspace: Workspace,
    persist_instance,
    apply_theme,
    *,
    schedule_is_current: bool,
) -> None:
    """Define the institutional theme and shared schedule-selector palette."""
    from nicegui import ui

    instance = workspace.instance
    draft = instance.color_scheme.model_dump(mode="json")
    token_inputs: list[tuple[dict, object]] = []
    institutional_inputs: dict[str, object] = {}
    accent_inputs: list[tuple[object, object]] = []

    def swatch(color: str):
        return (
            ui.element("span")
            .props("aria-hidden=true")
            .classes("rbs-color-scheme-swatch shrink-0")
            .style(f"--rbs-color-scheme-value:{color}")
        )

    def update_swatch(event, target) -> None:
        target.style(f"--rbs-color-scheme-value:{str(event.value)}")

    def color_row(role: str, token: dict, scope: str) -> tuple[object, object]:
        with ui.row().classes("rbs-color-scheme-row w-full items-center gap-3 py-3 flex-wrap"):
            marker = swatch(str(token["color"]))
            with ui.column().classes("min-w-44 flex-1 gap-0"):
                ui.label(role).classes("rbs-font-semibold")
                ui.label(scope).classes("rbs-type-caption rbs-text-muted")
            color_value = (
                ui.color_input("Hex color", value=str(token["color"]))
                .props("outlined dense")
                .classes("rbs-color-token-value-input w-full sm:w-48")
            )
            color_value.on_value_change(lambda event, target=marker: update_swatch(event, target))
            token_inputs.append((token, color_value))
            return color_value, marker

    def generate_accents() -> None:
        try:
            primary = normalize_hex_color(str(institutional_inputs["primary"].value or ""))
            secondary = normalize_hex_color(str(institutional_inputs["secondary"].value or ""))
            neutral = normalize_hex_color(str(institutional_inputs["neutral"].value or ""))
            generated = generate_accent_colors(
                primary,
                secondary,
                neutral,
                count=len(accent_inputs),
            )
            for (color_input, marker), color in zip(
                accent_inputs,
                generated,
                strict=True,
            ):
                color_input.set_value(color)
                marker.style(f"--rbs-color-scheme-value:{color}")
            ui.notify(
                "Matching accents generated — save the color scheme to apply them",
                type="info",
            )
        except ValueError as exc:
            ui.notify(str(exc), type="negative")

    def save() -> None:
        try:
            draft["name"] = str(scheme_name.value or "")
            for token, color_input in token_inputs:
                token["color"] = str(color_input.value or "")
            scheme = ColorScheme.model_validate(draft)
            revised = replace_color_scheme(instance, scheme)
            if revised == instance:
                ui.notify("Color scheme is already up to date", type="info")
                return
            ui.notify("Institutional color scheme saved", type="positive")
            if apply_theme is not None:
                apply_theme(scheme)
            persist_instance(revised, preserve_schedule=schedule_is_current)
        except (ValidationError, ValueError) as exc:
            ui.notify(str(exc), type="negative")

    with ui.column().classes("rbs-colors-settings w-full gap-4"):
        with ui.card().props("flat bordered").classes("w-full p-0"):
            with ui.column().classes("w-full gap-4 p-5"):
                with ui.column().classes("gap-1"):
                    ui.label("Institutional color scheme").classes("rbs-type-section-title")
                    ui.label(
                        "Set the colors for your institution. The primary, secondary, "
                        "and neutral roles theme the application and are also available to "
                        "schedule color selectors."
                    ).classes("rbs-type-body rbs-text-muted")
                scheme_name = (
                    ui.input("Scheme name", value=instance.color_scheme.name)
                    .props("outlined")
                    .classes("rbs-color-scheme-name-input w-full sm:w-96")
                )
                with ui.column().classes("w-full gap-0"):
                    institutional_inputs["primary"], _ = color_row(
                        "Primary", draft["primary"], "Page theme + selectors"
                    )
                    institutional_inputs["secondary"], _ = color_row(
                        "Secondary", draft["secondary"], "Page theme + selectors"
                    )
                    institutional_inputs["neutral"], _ = color_row(
                        "Neutral", draft["neutral"], "Page theme + selectors"
                    )

        with ui.card().props("flat bordered").classes("w-full p-0"):
            with ui.column().classes("w-full gap-4 p-5"):
                with ui.row().classes("w-full items-start justify-between gap-3 flex-wrap"):
                    with ui.column().classes("gap-1"):
                        ui.label("Schedule accents").classes("rbs-type-section-title")
                        ui.label(
                            "These complete the shared palette shown in rotation and clinic "
                            "color selectors. Generate a matching set or adjust individual "
                            "colors before saving."
                        ).classes("rbs-type-body rbs-text-muted")
                    ui.button(
                        "Generate matching accents",
                        icon="auto_awesome",
                        on_click=generate_accents,
                    ).props("outline no-caps")
                with ui.column().classes("w-full gap-0"):
                    for index, token in enumerate(draft["accents"], start=1):
                        accent_inputs.append(
                            color_row(f"Accent {index}", token, "Schedule selectors")
                        )

        with ui.row().classes("items-center gap-2"):
            ui.button("Save color scheme", icon="palette", on_click=save).props(
                "unelevated no-caps color=primary"
            )


def advanced_solver_config(solver: SolverConfig, values: dict) -> SolverConfig:
    """Build a solver config from the Advanced tab's raw input values.

    Blank clinic-balance fields mean "no bound"; blank weights mean zero.
    """

    def whole(name: str, *, blank_is_none: bool = False) -> int | None:
        raw = values.get(name)
        if raw is None or raw == "":
            return None if blank_is_none else 0
        return int(round(float(raw)))

    # model_validate, not model_copy: copying skips validators, so an inverted
    # clinic band would only surface later from the whole-instance revalidation.
    payload = solver.model_dump(mode="json")
    payload.update(
        {
            "time_limit_seconds": float(values.get("time_limit_seconds") or 60),
            "num_workers": (
                solver.num_workers
                if "num_workers" not in values
                else max(1, whole("num_workers") or 1)
            ),
            "solve_attempts": max(1, whole("solve_attempts") or 1),
            "allow_blocks_to_span_four_week_boundaries": bool(
                values.get("allow_blocks_to_span_four_week_boundaries", False)
            ),
            "auto_balance_clinic_blocks": bool(values.get("auto_balance_clinic_blocks", True)),
            "min_clinic_blocks_per_week": whole("min_clinic_blocks_per_week", blank_is_none=True),
            "max_clinic_blocks_per_week": whole("max_clinic_blocks_per_week", blank_is_none=True),
        }
    )
    payload["weights"] = {
        **payload.get("weights", {}),
        **{name: whole(name) for name, _label in _WEIGHT_FIELDS},
    }
    return SolverConfig.model_validate(payload)


def _advanced_settings(
    workspace: Workspace,
    persist_instance,
    *,
    schedule_is_current: bool,
    automatic_num_workers: bool = False,
) -> None:
    """Solver budget, clinic balance rules, and objective weights."""
    from nicegui import ui

    instance = workspace.instance
    solver = instance.solver
    weights = solver.weights
    metrics = workspace.schedule.meta.metrics if workspace.schedule else None
    inputs: dict[str, object] = {}

    def save() -> None:
        try:
            updated_solver = advanced_solver_config(
                solver,
                {name: element.value for name, element in inputs.items()},
            )
            revised = instance.revised(solver=updated_solver)
            # notify before persisting: persist_instance redraws, which deletes
            # the slot this handler is running in.
            ui.notify("Advanced settings saved", type="positive")
            persist_instance(revised, preserve_schedule=schedule_is_current)
        except (ValidationError, ValueError) as exc:
            ui.notify(str(exc), type="negative")

    def restore_defaults() -> None:
        try:
            revised = instance.revised(
                solver=solver.model_copy(
                    update={
                        "weights": ObjectiveWeights(),
                        "allow_blocks_to_span_four_week_boundaries": False,
                        "auto_balance_clinic_blocks": True,
                        "min_clinic_blocks_per_week": None,
                        "max_clinic_blocks_per_week": None,
                    }
                )
            )
            ui.notify("Advanced settings restored to defaults", type="positive")
            persist_instance(revised, preserve_schedule=schedule_is_current)
        except (ValidationError, ValueError) as exc:
            ui.notify(str(exc), type="negative")

    with ui.column().classes("w-full gap-4"):
        with ui.card().props("flat bordered").classes("w-full p-0"):
            with ui.column().classes("w-full gap-3 p-5"):
                ui.label("Solve budget").classes("rbs-type-section-title")
                with ui.row().classes("items-end gap-4 flex-wrap"):
                    inputs["time_limit_seconds"] = ui.number(
                        "Time limit (seconds)",
                        value=solver.time_limit_seconds,
                        min=1,
                        step=5,
                        format="%.0f",
                    ).classes("w-44")
                    if not automatic_num_workers:
                        inputs["num_workers"] = ui.number(
                            "CPU workers",
                            value=solver.num_workers,
                            min=1,
                            step=1,
                            format="%.0f",
                        ).classes("w-36")
                    inputs["solve_attempts"] = ui.number(
                        "Parallel attempts",
                        value=solver.solve_attempts,
                        min=1,
                        step=1,
                        format="%.0f",
                    ).classes("w-40")
        with ui.card().props("flat bordered").classes("w-full p-0"):
            with ui.column().classes("w-full gap-3 p-5"):
                ui.label("Block boundaries").classes("rbs-type-section-title")
                inputs["allow_blocks_to_span_four_week_boundaries"] = ui.checkbox(
                    "Allow rotation blocks to span four-week boundaries",
                    value=solver.allow_blocks_to_span_four_week_boundaries,
                )
                ui.label(
                    "Off keeps every rotation assignment inside Block A/1 through "
                    "Block M/13. Turning it on can mean a more complex schedule, but "
                    "possibly a more optimal one."
                ).classes("rbs-type-body rbs-text-muted")

        with ui.card().props("flat bordered").classes("w-full p-0"):
            with ui.column().classes("w-full gap-3 p-5"):
                ui.label("Clinic balance").classes("rbs-type-section-title")
                ui.label(
                    "How many residents may sit on a dedicated Clinic block in the same "
                    "week. This is a hard rule, so it holds every solve, unlike the "
                    "weights below which only express a preference."
                ).classes("rbs-type-body rbs-text-muted")
                inputs["auto_balance_clinic_blocks"] = ui.checkbox(
                    "Balance Clinic blocks automatically",
                    value=solver.auto_balance_clinic_blocks,
                )
                ui.label(
                    "Derived from the curriculum's Clinic weeks divided across the "
                    "calendar. If a year cannot satisfy it, the solve drops it and says "
                    "so rather than returning nothing."
                ).classes("rbs-type-caption rbs-text-muted")
                with ui.row().classes("items-end gap-4 flex-wrap"):
                    inputs["min_clinic_blocks_per_week"] = ui.number(
                        "Fewest per week",
                        value=solver.min_clinic_blocks_per_week,
                        min=0,
                        step=1,
                        format="%.0f",
                    ).classes("w-44")
                    inputs["max_clinic_blocks_per_week"] = ui.number(
                        "Most per week",
                        value=solver.max_clinic_blocks_per_week,
                        min=1,
                        step=1,
                        format="%.0f",
                    ).classes("w-44")
                ui.label(
                    "Leave both blank to use the automatic band. Setting either one "
                    "overrides it; too narrow a band can make the year unschedulable."
                ).classes("rbs-type-caption rbs-text-muted")
                if metrics is not None and metrics.clinic_block_weekly_min is not None:
                    with ui.row().classes(
                        "rbs-settings-lock-status w-full items-center gap-2 rounded p-3"
                    ):
                        ui.icon("insights").classes("rbs-text-secondary")
                        ui.label(
                            f"Current schedule runs {metrics.clinic_block_weekly_min}"
                            f"–{metrics.clinic_block_weekly_max} residents on Clinic per "
                            f"week (spread {metrics.clinic_block_weekly_spread})."
                        ).classes("rbs-type-body")

        with ui.card().props("flat bordered").classes("w-full p-0"):
            with ui.column().classes("w-full gap-3 p-5"):
                ui.label("Objective weights").classes("rbs-type-section-title")
                ui.label(
                    "Only the ratios matter. Weights trade attending coverage, preferred "
                    "half-days, and the different clinic-balance goals against one another."
                ).classes("rbs-type-body rbs-text-muted")
                fields = ObjectiveWeights.model_fields
                for name, label in _WEIGHT_FIELDS:
                    with ui.row().classes("w-full items-start gap-4 flex-wrap"):
                        inputs[name] = ui.number(
                            label,
                            value=getattr(weights, name),
                            min=0,
                            step=10,
                            format="%.0f",
                        ).classes("w-64")
                        ui.label(fields[name].description or "").classes(
                            "rbs-type-caption rbs-text-muted flex-1 min-w-0 pt-4"
                        )

        with ui.row().classes("items-center gap-2"):
            ui.button("Save advanced settings", icon="check", on_click=save).props(
                "unelevated no-caps color=primary"
            )
            ui.button("Restore defaults", icon="restart_alt", on_click=restore_defaults).props(
                "flat no-caps"
            )
        ui.label("Saving keeps the current schedule; re-solve to apply the new settings.").classes(
            "rbs-type-caption rbs-text-muted"
        )


def _open_workspace_delete_dialog(
    store: WorkspaceRepository,  # noqa: ARG001 - retained for the renderer seam
    workspace: Workspace,
    state,
    redraw,  # noqa: ARG001 - the session owns rebuilding
) -> None:
    """Close a workspace, asking about the user's own file first.

    Closing is a real deletion - there is no archive behind it - so the dialog
    is built around whether the user already holds a saved file, and refuses a
    casual click when they do not.
    """
    from rbs.ui.workspaces.close import close_workspace

    close_workspace(state, workspace)
