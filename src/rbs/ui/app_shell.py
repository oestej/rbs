"""Application shell: header/body mount, navigation, and tab rendering."""

from __future__ import annotations

import json
from dataclasses import dataclass

from rbs.logging import (
    get_logger,
)
from rbs.models.color_scheme import (
    DEFAULT_INK_COLOR,
    ColorScheme,
    accessible_text_color,
    contrasting_text_color,
)
from rbs.models.instance import SchedulerInput
from rbs.models.schedule import Schedule
from rbs.models.workspace import Workspace
from rbs.ui import page_shells
from rbs.ui.app_branding import (
    DANGER_COLOR,
    EMPTY_WORKSPACE_ARROW,
    FAVICON_URL,
    SUCCESS_COLOR,
    WARNING_COLOR,
)
from rbs.ui.app_documents import _document_io, _open_about_dialog, _save_button
from rbs.ui.app_solve import _solve
from rbs.ui.app_status import (
    _download_chip,
    _eviction_banner,
    _retention_banner,
    _solve_chip,
)
from rbs.ui.buttons import (
    ICON_BUTTON_PROPS,
    PRIMARY_BUTTON_PROPS,
    SECONDARY_BUTTON_PROPS,
    TERTIARY_BUTTON_PROPS,
    button_props,
)
from rbs.ui.clinic.board import render_clinic_html, render_clinic_legend_html
from rbs.ui.clinic.schedule_csv import build_clinic_schedule_csv, clinic_schedule_csv_filename
from rbs.ui.clinic.schedule_pdf import build_clinic_schedule_pdf, clinic_schedule_pdf_filename
from rbs.ui.clinic.tab import render_clinic_tab
from rbs.ui.grid import render_grid_html
from rbs.ui.residents.tab import render_residents_tab
from rbs.ui.rotations.editor import render_rotations_tab
from rbs.ui.session import TAB_NAMES, WorkspaceSession
from rbs.ui.settings.view import (
    _settings_tab,
)
from rbs.ui.workspaces.io import (
    _open_workspace_file,
    _rbsc_restore_upload,
)


@dataclass(frozen=True, slots=True)
class WorkspaceNavigation:
    tabs: object
    block_schedule: object
    clinic_schedule: object
    residents: object
    rotations: object
    clinic: object
    settings: object


def _mount_shell(session: WorkspaceSession) -> None:
    from nicegui import ui

    session.header.clear()
    session.body.clear()
    session.panels.clear()
    session.navigation = None
    workspace = session.workspace()
    scheme = workspace.instance.color_scheme if workspace else ColorScheme()
    product_name = "RBS Desktop" if _document_io(session) is not None else "RBS"
    _set_nicegui_theme(session, scheme)
    with session.header:
        with ui.row().classes("rbs-header-primary w-full items-center no-wrap"):
            with (
                ui.button(on_click=lambda: _open_about_dialog(session.product))
                .props(
                    button_props(
                        TERTIARY_BUTTON_PROPS,
                        "color=white",
                        f"aria-label='About {product_name}'",
                    )
                )
                .classes("rbs-header-brand shrink-0")
            ):
                ui.image(FAVICON_URL).props("fit=contain").classes("rbs-header-mark")
                ui.label(product_name).classes("rbs-header-product-name rbs-type-brand")
            if workspace is None:
                _empty_header_actions(session)
            else:
                # File/workspace controls stay together at the far right of the
                # primary tier, away from schedule navigation and solve state.
                with ui.row().classes("rbs-workspace-controls items-center gap-1 no-wrap shrink-0"):
                    _workspace_selector(session)
                    _save_button(
                        session,
                        workspace,
                        apply_theme=lambda scheme: _set_nicegui_theme(session, scheme),
                    )
        if workspace is not None:
            # Navigation, state, and Solve form the scheduling tier. The tabs
            # get all remaining width instead of competing with file controls.
            with ui.row().classes("rbs-header-secondary w-full items-center gap-3 no-wrap"):
                session.navigation = _workspace_navigation(session.on_tab_change)
                with ui.row().classes("rbs-header-actions items-center gap-2 shrink-0"):
                    _download_chip(session, workspace)
                    _solve_chip(session, workspace)
                    solve_button = (
                        ui.button(
                            "Solve",
                            icon="play_arrow",
                            on_click=lambda: _solve(session),
                        )
                        .props(
                            button_props(
                                PRIMARY_BUTTON_PROPS,
                                "color=white",
                                "text-color=dark",
                                "aria-label='Solve'",
                            )
                        )
                        .classes("rbs-solve-button whitespace-nowrap")
                    )
                    if session.solving:
                        solve_button.props("disable")
    with session.body:
        _eviction_banner(session)
        _retention_banner(session)
        if workspace is None:
            _empty_workspace_page()
            return
        assert session.navigation is not None
        active = getattr(
            session.navigation,
            session.active_tab,
            session.navigation.block_schedule,
        )
        with ui.tab_panels(session.navigation.tabs, value=active).classes(
            "w-full min-w-0 max-w-full"
        ):
            for name in TAB_NAMES:
                tab = getattr(session.navigation, name)
                classes = "min-w-0 max-w-full" if name == "block_schedule" else ""
                with ui.tab_panel(tab).classes(classes):
                    session.panels[name] = ui.column().classes("w-full min-w-0")
        # Rendering every editor up front delays hydration and makes the visible
        # controls appear ready before their event connection is usable. Populate
        # only the selected panel now and render the others on first selection.
        session.mark_stale()
        session.refresh_visible()


def _workspace_navigation(on_change=None) -> WorkspaceNavigation:
    from nicegui import ui

    with (
        ui.tabs(on_change=on_change)
        .props("dense no-caps align=left outside-arrows mobile-arrows")
        .classes("rbs-workspace-navigation min-w-0 flex-1") as tabs
    ):
        block_schedule = ui.tab("block_schedule", label="Block Schedule")
        clinic_schedule = ui.tab("clinic_schedule", label="Clinic Schedule")
        residents = ui.tab("residents", label="Residents")
        rotations = ui.tab("rotations", label="Rotations")
        clinic = ui.tab("clinic", label="Clinic")
        settings = ui.tab("settings", label="Configuration")
    return WorkspaceNavigation(
        tabs=tabs,
        block_schedule=block_schedule,
        clinic_schedule=clinic_schedule,
        residents=residents,
        rotations=rotations,
        clinic=clinic,
        settings=settings,
    )


def _workspace_selector(session: WorkspaceSession) -> None:
    from nicegui import ui

    workspaces = session.store.list()
    options = {ws.id: f"{ws.name} ({ws.academic_year})" for ws in workspaces}
    ui.select(
        options,
        value=session.workspace_id,
        label="Workspace",
        on_change=lambda e: _switch(session, e.value),
    ).props("dense outlined options-dense").classes("rbs-workspace-selector w-64 shrink-0")


def _empty_header_actions(session: WorkspaceSession) -> None:
    from nicegui import ui

    from rbs.ui.workspaces.native_documents import (
        new_native_document,
        open_native_document,
    )

    async def handle_upload(event) -> None:
        # Opening merges; it never replaces. The desk may be empty now, but the
        # button should not mean something different depending on when it is used.
        await _open_workspace_file(session.store, session, session.rebuild, event)

    async def open_sample_data() -> None:
        if _document_io(session) is None:
            session.create_sample()
        else:
            await new_native_document(session, sample=True)

    with ui.row().classes(
        "rbs-workspace-controls rbs-empty-workspace-controls items-center no-wrap"
    ):
        documents = _document_io(session)
        with ui.row().classes("rbs-empty-workspace-primary-actions items-center no-wrap"):
            ui.button(
                "New",
                icon="add",
                on_click=(
                    session.create_blank
                    if documents is None
                    else lambda: new_native_document(session)
                ),
            ).props("flat no-caps color=white")
            if documents is None:
                _rbsc_restore_upload(handle_upload, label="Open", in_header=True)
            else:
                ui.button(
                    "Open",
                    icon="folder_open",
                    on_click=lambda: open_native_document(session),
                ).props("flat no-caps color=white")
        with (
            ui.button(icon="more_vert")
            .props(
                button_props(
                    ICON_BUTTON_PROPS,
                    "color=white",
                    "aria-label='More workspace actions'",
                )
            )
            .classes("rbs-empty-workspace-more")
        ):
            with ui.menu().classes("rbs-empty-workspace-menu"):
                ui.menu_item("Open Sample Data", on_click=open_sample_data)


def _empty_workspace_page() -> None:
    from nicegui import ui

    with ui.column().classes("rbs-empty-workspace w-full"):
        _html(EMPTY_WORKSPACE_ARROW, classes="rbs-empty-workspace-arrow")
        ui.label("Create or import a workspace to get started").classes(
            "rbs-empty-workspace-copy rbs-type-page-title"
        )


def _render_tab(session: WorkspaceSession, name: str) -> None:
    workspace = session.workspace()
    if workspace is None:
        return
    if name == "block_schedule":
        _render_block_schedule(session, workspace)
    elif name == "clinic_schedule":
        _render_clinic_schedule(session, workspace)
    elif name == "rotations":
        _render_rotations(session, workspace)
    elif name == "clinic":

        def persist_clinic(updated: SchedulerInput, _rotation_id: str | None = None) -> None:
            session.active_tab = "clinic"
            session.persist_instance(updated)

        render_clinic_tab(
            workspace.instance,
            on_save=persist_clinic,
            active_section=session.clinic_section,
            on_section_change=lambda event: _remember_clinic_section(session, event.value),
        )
    elif name == "residents":
        _render_residents(session, workspace)
    elif name == "settings":
        _settings_tab(
            session.store,
            workspace,
            session,
            session.persist_instance,
            session.rebuild,
            schedule_is_current=workspace.schedule is not None,
            active_section=session.settings_section,
            on_section_change=lambda event: _remember_settings_section(session, event.value),
            apply_theme=lambda scheme: _set_nicegui_theme(session, scheme),
        )


def _render_block_schedule(session: WorkspaceSession, workspace: Workspace) -> None:
    from nicegui import ui

    instance = workspace.instance
    schedule = workspace.latest_schedule
    with page_shells.schedule_canvas(
        "Block schedule",
        subtitle="Review weekly block assignments across the academic year.",
    ):
        with page_shells.toolbar():
            ui.space()
            with page_shells.toolbar_actions():
                block_past = ui.checkbox(
                    "Show past weeks",
                    value=bool(session.show_past_block_weeks),
                ).props("dense")
        grid = ui.column().classes("w-full min-w-0 gap-2")

        def render_grid() -> None:
            grid.clear()
            with grid:
                _html(
                    render_grid_html(
                        instance,
                        schedule,
                        resident_edit_url="/",
                        show_past_weeks=bool(session.show_past_block_weeks),
                    )
                )

        def toggle_block_past(event) -> None:
            session.show_past_block_weeks = bool(event.value)
            render_grid()

        block_past.on_value_change(toggle_block_past)
        render_grid()


def _render_clinic_schedule(session: WorkspaceSession, workspace: Workspace) -> None:
    from nicegui import ui

    instance = workspace.instance
    schedule = workspace.latest_schedule

    def selected_clinic_site() -> str | None:
        selected = str(session.clinic_site or "all")
        if selected == "all":
            return None
        if selected in instance.clinic_policy.site_ids:
            return selected
        session.clinic_site = "all"
        return None

    def render_board() -> None:
        clinic_schedule.clear()
        with clinic_schedule:
            _html(
                render_clinic_html(
                    instance,
                    schedule,
                    show_past_weeks=bool(session.show_past_clinic_weeks),
                    site=selected_clinic_site(),
                    show_legend=False,
                )
            )

    def toggle_clinic_past(event) -> None:
        session.show_past_clinic_weeks = bool(event.value)
        render_board()

    def filter_clinic_site(event) -> None:
        session.clinic_site = str(event.value or "all")
        render_board()

    def export_clinic_schedule(extension: str) -> None:
        try:
            site = selected_clinic_site()
            export_options = {
                "show_past_weeks": bool(session.show_past_clinic_weeks),
                "site": site,
            }
            if extension == "csv":
                content = build_clinic_schedule_csv(
                    instance,
                    schedule,
                    **export_options,
                )
                filename = clinic_schedule_csv_filename(
                    instance.academic_year,
                    site=site,
                )
            else:
                content = build_clinic_schedule_pdf(
                    instance,
                    schedule,
                    **export_options,
                )
                filename = clinic_schedule_pdf_filename(
                    instance.academic_year,
                    site=site,
                )
            ui.download.content(content, filename)
            get_logger("documents").info(
                "schedule.exported",
                source=extension,
            )
        except Exception as exc:
            get_logger("documents").error(
                "schedule.export_failed",
                source=extension,
                error_code=type(exc).__name__,
                exc_info=True,
            )
            ui.notify(str(exc), type="negative")

    with page_shells.schedule_canvas(
        "Clinic schedule",
        subtitle="Review clinic staffing by week, site, and half-day.",
    ):
        with page_shells.toolbar(extra_classes="rbs-clinic-toolbar"):
            _html(
                render_clinic_legend_html(instance.clinic_policy),
                classes="rbs-clinic-toolbar-key min-w-0",
            )
            with page_shells.toolbar_actions(extra_classes="rbs-clinic-toolbar-controls"):
                clinic_past = ui.checkbox(
                    "Show past weeks",
                    value=bool(session.show_past_clinic_weeks),
                ).props("dense")
                clinic_site = (
                    ui.select(
                        {
                            "all": "All sites",
                            **{site.id: site.name for site in instance.clinic_policy.sites},
                        },
                        value=str(session.clinic_site or "all"),
                        label="Site",
                    )
                    .props("dense outlined options-dense")
                    .classes("w-40")
                )
                ui.button(
                    "Export CSV",
                    icon="table_view",
                    on_click=lambda: export_clinic_schedule("csv"),
                ).props(button_props(SECONDARY_BUTTON_PROPS, "dense"))
                ui.button(
                    "Export PDF",
                    icon="picture_as_pdf",
                    on_click=lambda: export_clinic_schedule("pdf"),
                ).props(button_props(SECONDARY_BUTTON_PROPS, "dense"))

        clinic_schedule = ui.column().classes("w-full min-w-0 gap-2")
        clinic_past.on_value_change(toggle_clinic_past)
        clinic_site.on_value_change(filter_clinic_site)
        render_board()


def _render_rotations(session: WorkspaceSession, workspace: Workspace) -> None:
    def select_rotation(rotation_id: str | None) -> None:
        session.rotation_id = rotation_id
        session.active_tab = "rotations"
        session.refresh_panel("rotations")

    def persist_rotation(updated: SchedulerInput, rotation_id: str | None) -> None:
        session.rotation_id = rotation_id
        session.active_tab = "rotations"
        session.persist_instance(updated)

    def persist_rotation_color(updated: SchedulerInput, rotation_id: str | None) -> None:
        session.rotation_id = rotation_id
        session.active_tab = "rotations"
        session.persist_instance(updated, preserve_schedule=True)

    render_rotations_tab(
        workspace.instance,
        schedule=workspace.latest_schedule,
        selected_rotation_id=session.rotation_id,
        on_select=select_rotation,
        on_save=persist_rotation,
        on_color_save=persist_rotation_color,
        active_section=session.rotation_section,
        on_section_change=lambda event: _remember_rotation_section(session, event.value),
        resident_edit_url="/",
    )


def _render_residents(session: WorkspaceSession, workspace: Workspace) -> None:
    def select_resident(resident_id: str | None) -> None:
        if resident_id != session.resident_id:
            session.resident_block_schedule_editing = False
            session.resident_schedule_editing = False
            session.resident_schedule_section = "resident_block_schedule"
        session.resident_id = resident_id
        session.active_tab = "residents"
        session.refresh_panel("residents")

    def persist_resident(updated: SchedulerInput, resident_id: str) -> None:
        session.resident_block_schedule_editing = False
        session.resident_schedule_editing = False
        session.resident_id = resident_id
        session.active_tab = "residents"
        session.persist_instance(updated)

    def persist_resident_schedule(
        updated: SchedulerInput,
        resident_id: str,
        preserve_schedule: bool,
    ) -> None:
        session.resident_schedule_editing = False
        session.resident_id = resident_id
        session.active_tab = "residents"
        session.persist_instance(updated, preserve_schedule=preserve_schedule)

    def persist_resident_schedule_change(
        updated: Schedule,
        resident_id: str,
        refresh: bool,
    ) -> None:
        session.resident_id = resident_id
        session.active_tab = "residents"
        session.persist_schedule(updated, refresh=refresh)

    def set_block_schedule_editing(editing: bool) -> None:
        session.resident_block_schedule_editing = editing
        if editing:
            session.resident_schedule_editing = False

    def set_clinic_schedule_editing(editing: bool) -> None:
        session.resident_schedule_editing = editing
        if editing:
            session.resident_block_schedule_editing = False

    def remember_schedule_section(event) -> None:
        value = getattr(event, "value", event)
        name = getattr(value, "name", value)
        if name in {
            "resident_block_schedule",
            "resident_clinic_schedule",
            "resident_elective_preference",
        }:
            session.resident_schedule_section = name

    render_residents_tab(
        workspace.instance,
        workspace.latest_schedule,
        selected_resident_id=session.resident_id,
        on_select=select_resident,
        on_save=persist_resident,
        on_schedule_save=persist_resident_schedule,
        on_schedule_change=persist_resident_schedule_change,
        schedule_is_current=workspace.schedule is not None,
        block_schedule_editing=session.resident_block_schedule_editing,
        on_block_schedule_editing_change=set_block_schedule_editing,
        schedule_editing=session.resident_schedule_editing,
        on_schedule_editing_change=set_clinic_schedule_editing,
        active_schedule_section=session.resident_schedule_section,
        on_schedule_section_change=remember_schedule_section,
    )


def _remember_active_tab(state, value) -> None:
    name = getattr(value, "name", value)
    if name in TAB_NAMES:
        state.active_tab = name


def _remember_settings_section(state, value) -> None:
    name = getattr(value, "name", value)
    if name in {
        "settings_general",
        "settings_training_levels",
        "settings_advanced",
    }:
        state.settings_section = name


def _remember_clinic_section(state, value) -> None:
    name = getattr(value, "name", value)
    if name in {"clinic_sites", "clinic_block_rules", "clinic_manual_blocks"}:
        state.clinic_section = name


def _remember_rotation_section(state, value) -> None:
    name = getattr(value, "name", value)
    if name in {
        "rotation_summary",
        "standard_rotations",
        "fmed_configuration",
        "elective_configuration",
        "special_configuration",
        "academic_configuration",
    }:
        state.rotation_section = name


def _switch(session: WorkspaceSession, workspace_id) -> None:
    if workspace_id is None:
        return
    workspace_id = int(workspace_id)
    if workspace_id == session.workspace_id:
        return
    session.switch_workspace(workspace_id)


def _nicegui_theme_colors(scheme: ColorScheme) -> dict[str, str]:
    """Translate the saved application roles into NiceGUI/Quasar colors."""
    return {
        "primary": scheme.primary.color,
        "secondary": scheme.secondary.color,
        "accent": scheme.neutral.color,
        "dark": DEFAULT_INK_COLOR,
        "positive": SUCCESS_COLOR,
        "negative": DANGER_COLOR,
        "warning": WARNING_COLOR,
    }


def _set_nicegui_theme(session: WorkspaceSession, scheme: ColorScheme) -> None:
    """Create or update the page-level theme without remounting the UI shell."""
    from nicegui import ui

    colors = _nicegui_theme_colors(scheme)
    contrast_colors = {
        "rbs_on_primary": contrasting_text_color(scheme.primary.color),
        "rbs_on_secondary": contrasting_text_color(scheme.secondary.color),
        "rbs_primary_text": accessible_text_color(scheme.primary.color),
        "rbs_secondary_text": accessible_text_color(scheme.secondary.color),
    }
    if session.theme is None:
        session.theme = ui.colors(**colors, **contrast_colors)
        return
    for name, value in colors.items():
        session.theme.props[name.replace("_", "-")] = value
    session.theme.props["custom-colors"] = contrast_colors
    css_variables = {f"--q-{name.replace('_', '-')}": value for name, value in colors.items()}
    css_variables.update(
        {
            "--q-rbs-on-primary": contrast_colors["rbs_on_primary"],
            "--q-rbs-on-secondary": contrast_colors["rbs_on_secondary"],
            "--q-rbs-primary-text": contrast_colors["rbs_primary_text"],
            "--q-rbs-secondary-text": contrast_colors["rbs_secondary_text"],
            "--rbs-on-primary": contrast_colors["rbs_on_primary"],
            "--rbs-on-secondary": contrast_colors["rbs_on_secondary"],
            "--rbs-primary-text": contrast_colors["rbs_primary_text"],
            "--rbs-secondary-text": contrast_colors["rbs_secondary_text"],
        }
    )
    session.theme.client.run_javascript(
        "for (const [name, value] of Object.entries("
        f"{json.dumps(css_variables)}"
        ")) document.body.style.setProperty(name, value);"
    )


def _html(content: str, *, classes: str = "w-full min-w-0 max-w-full") -> object:
    from nicegui import ui

    try:
        element = ui.html(content, sanitize=False)
    except TypeError:
        element = ui.html(content)
    element.classes(classes)
    return element
