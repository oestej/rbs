"""Document save/open/about surface for native and browser packagings."""

from __future__ import annotations

from rbs import __version__
from rbs.logging import (
    get_logger,
)
from rbs.models.workspace import Workspace
from rbs.product import ProductConfig
from rbs.ui.app_branding import (
    ABOUT_COPYRIGHT_NOTICE,
    ABOUT_LICENSE_NOTICE,
    dialog_wordmark,
)
from rbs.ui.buttons import (
    ICON_BUTTON_PROPS,
    TERTIARY_BUTTON_PROPS,
    button_props,
)
from rbs.ui.session import WorkspaceSession
from rbs.ui.workspaces.close import save_binding
from rbs.ui.workspaces.io import (
    _open_workspace_file,
    _rbsc_restore_upload,
    _workspace_tab,
)
from rbs.ui.workspaces.native_documents import (
    close_native_document,
    new_native_document,
    open_native_document,
    save_native_document,
)
from rbs.ui.workspaces.status import (
    PILL_ALERT,
    PILL_OK,
    PILL_WARN,
)


def _document_io(session: WorkspaceSession):
    """Return this packaging's native document capability, when it has one."""
    return getattr(session.workspace_host, "document_io", None)


def document_summary(documents) -> tuple[str, str]:
    """Describe native document state without browser/download terminology."""
    if documents.path is None:
        return "Not saved", PILL_ALERT
    if documents.dirty:
        return "Changes to save", PILL_WARN
    return "Saved", PILL_OK


def _notify_recovery_error(session: WorkspaceSession, documents) -> None:
    """Surface desktop persistence failures once without blocking document edits."""
    from nicegui import ui

    recovery_error = getattr(documents, "recovery_error", None)
    settings_error = getattr(documents, "settings_error", None)
    signature = " | ".join(error for error in (recovery_error, settings_error) if error)
    if not signature:
        session._recovery_error = None
        return
    if signature == session._recovery_error:
        return
    session._recovery_error = signature
    if recovery_error:
        get_logger("desktop.recovery").error(
            "document.recovery_failed",
            error_code="checkpoint_failed",
        )
        message = (
            f"Automatic draft recovery is unavailable. Save this document now. {recovery_error}"
        )
    else:
        get_logger("settings").error(
            "settings.persistence_failed",
            error_code="settings_write_failed",
        )
        message = (
            "Application settings could not be saved; the workspace document is "
            f"unaffected. {settings_error}"
        )
    ui.notify(
        message,
        type="negative",
        multi_line=True,
    )


def _save_button(
    session: WorkspaceSession,
    workspace: Workspace,
    *,
    apply_theme=None,
) -> None:
    """Render the selected workspace's save, close, and overflow actions."""
    from nicegui import ui

    from rbs.ui.workspaces.close import close_workspace

    documents = _document_io(session)

    async def handle_open(event) -> None:
        await _open_workspace_file(session.store, session, session.rebuild, event)

    def close_current():
        if documents is None:
            close_workspace(session, workspace)
            return None
        return close_native_document(session)

    with (
        ui.button(
            "Settings",
            icon="settings",
            on_click=lambda: open_workspace_dialog(
                session,
                workspace,
                apply_theme=apply_theme,
            ),
        )
        .props(
            button_props(
                TERTIARY_BUTTON_PROPS,
                "color=white",
                "aria-label='Workspace settings'",
            )
        )
        .classes("rbs-workspace-settings-button whitespace-nowrap")
    ):
        ui.tooltip("Workspace settings")

    if documents is None:
        record, js = save_binding(session, workspace)
        save_button = (
            ui.button("Save", icon="save")
            .props(button_props(TERTIARY_BUTTON_PROPS, "color=white", "aria-label='Save'"))
            .classes("rbs-save-button whitespace-nowrap")
            .on("click", handler=record, js_handler=js)
        )
        pick_record, pick_js = save_binding(
            session,
            workspace,
            force_picker=True,
            on_saved=session.rebuild if workspace.is_sample else None,
        )
    else:
        save_button = (
            ui.button(
                "Save",
                icon="save",
                on_click=lambda: save_native_document(session),
            )
            .props(button_props(TERTIARY_BUTTON_PROPS, "color=white", "aria-label='Save'"))
            .classes("rbs-save-button whitespace-nowrap")
        )
    if workspace.is_sample:
        save_button.props("disable")

    ui.button(
        "Close",
        icon="close",
        on_click=close_current,
    ).props(
        button_props(
            TERTIARY_BUTTON_PROPS,
            "color=white",
            "aria-label='Close workspace'",
        )
    ).classes("rbs-close-button whitespace-nowrap")

    # Less frequent document/workspace commands stay with Save and Close, not
    # beside the views into the schedule.
    with ui.button(icon="more_vert").props(
        button_props(
            ICON_BUTTON_PROPS,
            "color=white",
            "aria-label='More workspace actions'",
        )
    ):
        with ui.menu():
            if documents is not None:
                ui.menu_item("New", on_click=lambda: new_native_document(session))
                ui.menu_item("Open", on_click=lambda: open_native_document(session))
            else:
                ui.menu_item("New", on_click=session.create_blank)
                _rbsc_restore_upload(handle_open, label="Open", in_menu=True)
            ui.separator()
            if documents is not None:
                ui.menu_item(
                    "Save as…",
                    on_click=lambda: save_native_document(session, save_as=True),
                )
            else:
                ui.menu_item("Save as…").on("click", handler=pick_record, js_handler=pick_js)


def _open_about_dialog(product: ProductConfig) -> None:
    """Show the wordmark, installed version, and legal notices."""
    from nicegui import ui

    with (
        ui.dialog() as dialog,
        ui.card().classes("rbs-popout-dialog rbs-branded-dialog rbs-about-dialog p-0 gap-0"),
    ):
        ui.button(icon="close", on_click=dialog.close).props(
            "flat round dense aria-label='Close about dialog'"
        ).classes("rbs-popout-close rbs-about-close")
        dialog_wordmark()
        ui.label(f"Version {__version__}").classes("rbs-about-version")
        ui.button(
            "View release notes",
            icon="new_releases",
            on_click=_open_release_notes_dialog,
        ).props("flat no-caps").classes("rbs-about-release-notes-button")
        ui.label(ABOUT_COPYRIGHT_NOTICE).classes("rbs-about-copyright")
        with ui.column().classes("rbs-about-licensing gap-3"):
            with ui.row().classes("rbs-about-licensing-heading items-center gap-2"):
                ui.icon("balance").classes("rbs-about-licensing-icon")
                ui.label("Licensing & notices").classes("rbs-about-licensing-title")
            ui.label(ABOUT_LICENSE_NOTICE).classes("rbs-about-licensing-copy")
            ui.button(
                "View license",
                icon="description",
                on_click=_open_application_license_dialog,
            ).props("flat no-caps").classes("rbs-about-license-button")
            if product.bundles_third_party_licenses:
                ui.separator().classes("rbs-about-license-divider")
                with ui.column().classes("rbs-about-license-item gap-0"):
                    ui.label("Third-party components").classes("rbs-about-license-name")
                    ui.button(
                        "View third-party licenses",
                        icon="description",
                        on_click=_open_third_party_licenses_dialog,
                    ).props("flat no-caps").classes("rbs-about-notices-button")
    dialog.open()


def _open_application_license_dialog() -> None:
    """Show the full license governing RBS in the in-app text viewer."""
    from nicegui import ui

    # Resolve through app.py so tests and alternate packagings can replace the
    # loader at the same stable seam used by the other bundled documents.
    from rbs.ui import app as app_module

    license_text = app_module.load_application_license()
    with (
        ui.dialog() as dialog,
        ui.card().classes(
            "rbs-popout-dialog rbs-third-party-dialog rbs-application-license-dialog p-0 gap-0"
        ),
    ):
        with ui.row().classes("rbs-third-party-header items-start no-wrap"):
            with ui.column().classes("gap-1"):
                ui.label("Open Software License 3.0").classes("rbs-third-party-title")
                ui.label("License terms governing RBS.").classes("rbs-third-party-intro")
            ui.button(icon="close", on_click=dialog.close).props(
                "flat round dense aria-label='Close RBS license'"
            ).classes("rbs-popout-close rbs-third-party-close")
        ui.separator().classes("rbs-third-party-divider")
        with (
            ui.scroll_area()
            .props("role=region aria-label='RBS license text'")
            .classes("rbs-third-party-scroll")
        ):
            ui.label(license_text).classes("rbs-third-party-text")
    dialog.open()


def _open_release_notes_dialog() -> None:
    """Show the changelog bundled with this application build."""
    from nicegui import ui

    # Resolved through the app namespace (not a direct import) so the loader
    # stays patchable where it was always patched: ``rbs.ui.app``.
    from rbs.ui import app as app_module

    notes = app_module.load_release_notes()
    with (
        ui.dialog() as dialog,
        ui.card().classes("rbs-popout-dialog rbs-release-notes-dialog p-0 gap-0"),
    ):
        with ui.row().classes("rbs-release-notes-header items-start no-wrap"):
            with ui.column().classes("gap-1"):
                ui.label("Release notes").classes("rbs-release-notes-title")
                ui.label("Notable changes included with this build of RBS.").classes(
                    "rbs-release-notes-intro"
                )
            ui.button(icon="close", on_click=dialog.close).props(
                "flat round dense aria-label='Close release notes'"
            ).classes("rbs-popout-close rbs-release-notes-close")
        ui.separator().classes("rbs-release-notes-divider")
        with (
            ui.scroll_area()
            .props("role=region aria-label='RBS release notes'")
            .classes("rbs-release-notes-scroll")
        ):
            ui.markdown(notes, sanitize=True).classes("rbs-release-notes-markdown")
    dialog.open()


def _open_third_party_licenses_dialog() -> None:
    """Show the generated dependency license and attribution files."""
    from nicegui import ui

    # Same patchable-namespace seam as _open_release_notes_dialog.
    from rbs.ui import app as app_module

    notices = app_module.load_third_party_licenses()
    with (
        ui.dialog() as dialog,
        ui.card().classes("rbs-popout-dialog rbs-third-party-dialog p-0 gap-0"),
    ):
        with ui.row().classes("rbs-third-party-header items-start no-wrap"):
            with ui.column().classes("gap-1"):
                ui.label("Third-party licenses").classes("rbs-third-party-title")
                ui.label(
                    "License terms and attributions for components bundled with RBS Desktop."
                ).classes("rbs-third-party-intro")
            ui.button(icon="close", on_click=dialog.close).props(
                "flat round dense aria-label='Close third-party licenses'"
            ).classes("rbs-popout-close rbs-third-party-close")
        ui.separator().classes("rbs-third-party-divider")
        with (
            ui.scroll_area()
            .props("role=region aria-label='Third-party license notices'")
            .classes("rbs-third-party-scroll")
        ):
            ui.label(notices).classes("rbs-third-party-text")
    dialog.open()


def open_workspace_dialog(
    session: WorkspaceSession,
    workspace: Workspace,
    *,
    apply_theme=None,
) -> None:
    """Edit settings for the selected workspace."""
    from nicegui import ui

    from rbs.ui.settings.view import _colors_settings

    with (
        ui.dialog() as dialog,
        ui.card().classes("rbs-popout-dialog rbs-workspace-dialog p-0 gap-0"),
    ):
        with ui.row().classes(
            "rbs-workspace-dialog-header w-full items-start justify-between gap-3"
        ):
            with ui.column().classes("rbs-workspace-dialog-heading gap-1"):
                ui.label("Workspace Settings").classes("rbs-workspace-dialog-title")
                ui.label("Manage workspace identity and its visual theme.").classes(
                    "rbs-workspace-dialog-intro"
                )
            ui.button(icon="close", on_click=dialog.close).props(
                "flat round dense aria-label='Close workspace panel'"
            ).classes("rbs-popout-close rbs-workspace-dialog-close")
        with (
            ui.tabs()
            .props("dense no-caps align=left")
            .classes("rbs-workspace-dialog-tabs w-full") as tabs
        ):
            general_tab = ui.tab("workspace_general", label="General")
            colors_tab = ui.tab("workspace_colors", label="Colors")
        with (
            ui.tab_panels(tabs, value=general_tab)
            .props("animated")
            .classes("rbs-workspace-dialog-panels w-full")
        ):
            with ui.tab_panel(general_tab).classes("rbs-workspace-dialog-panel"):
                _workspace_tab(
                    session.store,
                    workspace,
                    session,
                    session.rebuild,
                    dialog=dialog,
                    settings_only=True,
                )
            with ui.tab_panel(colors_tab).classes(
                "rbs-workspace-dialog-panel rbs-workspace-dialog-colors"
            ):
                _colors_settings(
                    workspace,
                    session.persist_instance,
                    apply_theme,
                    schedule_is_current=workspace.schedule is not None,
                )
    session.workspace_dialog = dialog
    dialog.open()
