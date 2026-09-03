"""Save-aware workspace closing, independent of the application compositor."""

from __future__ import annotations

from rbs.models.workspace import DownloadState, Workspace, WorkspaceConflictError
from rbs.ui.session import WorkspaceSession
from rbs.ui.workspaces.file_handle import (
    WORKSPACE_DOWNLOAD_ROUTE,
    describe_outcome,
    handle_key,
    save_js_handler,
    saved_successfully,
    workspace_filename,
)
from rbs.workspaces import WorkspaceController

CLOSE_SUMMARY = {
    DownloadState.CURRENT: (
        "You have a saved file matching this workspace. Closing removes the "
        "server's copy; your file is unaffected."
    ),
    DownloadState.STALE: (
        "This workspace has changed since you last saved it. Closing discards "
        "those changes permanently."
    ),
    DownloadState.NEVER: (
        "This workspace has never been saved to a file. Closing discards all of it permanently."
    ),
}


def save_binding(
    session: WorkspaceSession,
    workspace: Workspace,
    *,
    force_picker: bool = False,
    on_saved=None,
):
    """Build browser and server handlers for a revision-pinned workspace save."""
    url = (
        WORKSPACE_DOWNLOAD_ROUTE.format(workspace_id=workspace.id)
        + f"?revision={workspace.workspace_revision}"
        + ("&save_as=true" if force_picker else "")
    )
    filename = workspace_filename(workspace.name, workspace.academic_year)
    key = handle_key(session.principal.subject, workspace.id)

    def record(event) -> None:
        from nicegui import ui

        raw = event.args
        outcome = str(raw if isinstance(raw, str) else (raw or [""])[0])
        message, level = describe_outcome(outcome)
        save_is_current = saved_successfully(outcome)
        if save_is_current:
            try:
                WorkspaceController(session.store).mark_exported(
                    workspace,
                    clear_sample=force_picker,
                )
            except KeyError:
                save_is_current = False
            except WorkspaceConflictError:
                save_is_current = False
                message = "Saved, but the workspace changed during the save. Save it again."
                level = "warning"
            session.touch()
        ui.notify(message, type=level, multi_line=level == "negative")
        if save_is_current and on_saved is not None:
            on_saved()

    return record, save_js_handler(url, filename, key, force_picker=force_picker)


def save_workspace_from_dialog(
    session: WorkspaceSession,
    workspace: Workspace,
    *,
    on_saved,
):
    """Render a Save action that closes only after a current file was written."""
    from nicegui import ui

    record, js = save_binding(session, workspace, on_saved=on_saved)
    return (
        ui.button("Save and close", icon="save")
        .props("unelevated no-caps color=primary")
        .on("click", handler=record, js_handler=js)
    )


CLOSE_CONFIRM_DELAY_SECONDS = 5


def open_close_dialog(
    session: WorkspaceSession,
    workspace: Workspace,
    on_closed,
) -> None:
    """Close unchanged workspaces at once; pause before discarding changes.

    A workspace with nothing the user would lose — already saved, or new
    and never modified — closes immediately without a dialog. Anything
    with unsaved changes keeps a dialog, but the destructive action stays
    disabled behind a short countdown instead of asking for a typed
    workspace name.
    """
    from nicegui import ui

    if not workspace.has_unsaved_changes:
        on_closed(workspace.id)
        return

    state = workspace.download_state

    with ui.dialog() as dialog, ui.card().classes(
        "rbs-close-dialog rbs-popout-dialog w-full max-w-md p-0 gap-0"
    ):
        armed = {"ready": False}

        def close_now() -> None:
            timer.deactivate()
            dialog.close()
            on_closed(workspace.id)

        def cancel() -> None:
            timer.deactivate()
            dialog.close()

        def guarded_close() -> None:
            if armed["ready"]:
                close_now()

        with ui.row().classes("w-full items-center justify-between gap-3 p-5 pb-4"):
            with ui.row().classes("items-center gap-3"):
                ui.icon("warning_amber").props("size=30px").classes("rbs-text-warning")
                with ui.column().classes("gap-0"):
                    ui.label("Close workspace?").classes("rbs-type-dialog-title")
                    ui.label(workspace.name).classes("rbs-type-body-large")
            ui.button(icon="close", on_click=cancel).props(
                "flat round dense aria-label='Dismiss close dialog'"
            )
        with ui.column().classes("w-full gap-2 px-5 pb-5"):
            ui.label(CLOSE_SUMMARY[state]).classes("rbs-type-body rbs-text-muted")
            ui.label("Saving first keeps a copy of your latest changes.").classes(
                "rbs-type-body rbs-text-muted"
            )

        ui.separator()
        with ui.row().classes("w-full items-center justify-end gap-2 px-5 py-4"):
            ui.button("Cancel", on_click=cancel).props("flat no-caps")
            save_workspace_from_dialog(session, workspace, on_saved=close_now)
            close_button = (
                ui.button(
                    f"Close workspace ({CLOSE_CONFIRM_DELAY_SECONDS})",
                    icon="delete_outline",
                    on_click=guarded_close,
                )
                .props("unelevated no-caps color=negative")
                .classes("rbs-close-delayed")
            )
            close_button.disable()

        remaining = {"seconds": CLOSE_CONFIRM_DELAY_SECONDS}

        def tick() -> None:
            if not dialog.value:
                timer.deactivate()
                return
            remaining["seconds"] -= 1
            if remaining["seconds"] <= 0:
                armed["ready"] = True
                close_button.set_text("Close workspace")
                close_button.enable()
                timer.deactivate()
            else:
                close_button.set_text(f"Close workspace ({remaining['seconds']})")

        timer = ui.timer(1.0, tick, immediate=False)
    dialog.open()


def close_workspace(session: WorkspaceSession, workspace: Workspace) -> None:
    """Remove the snapshot, confirming only when unsaved changes exist."""

    def closed(workspace_id: int) -> None:
        from nicegui import ui

        if session.workspace_dialog is not None:
            session.workspace_dialog.close()
            session.workspace_dialog = None
        WorkspaceController(session.store).delete(workspace)
        remaining = session.store.list()
        if session.workspace_id == workspace_id:
            session.reset_navigation(remaining[0].id if remaining else None)
        ui.notify(f"Closed {workspace.name}", type="positive")
        session.rebuild()

    if workspace.is_sample or not workspace.has_unsaved_changes:
        closed(workspace.id)
        return
    open_close_dialog(session, workspace, closed)
