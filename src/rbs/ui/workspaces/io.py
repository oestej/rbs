"""The Workspace panel: what this workspace is, what else is open, and its files.

Everything about a workspace's identity and lifecycle in one place - naming it,
opening another, closing this one, and moving any of them in or out as files.

Two file paths with very different blast radii live here. Opening a workspace
file *merges* it onto the desk and is the everyday action. Replacing the whole
database wipes it and is a desktop-only support tool, kept apart and labelled as
such.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from rbs.logging import get_logger
from rbs.models.rbsc import RBSCState
from rbs.repository import WorkspaceRepository
from rbs.ui.host import DEFAULT_UPLOAD_MAX_BYTES
from rbs.ui.session import WorkspaceSession

# Uploads are fully validated into pydantic models, so the size guard has to come
# before parsing rather than after it.
MAX_UPLOAD_BYTES = DEFAULT_UPLOAD_MAX_BYTES


def _workspace_tab(
    store: WorkspaceRepository,
    workspace,
    state: WorkspaceSession,
    redraw,
    *,
    dialog=None,
    settings_only: bool = False,
) -> None:
    """Render the workspace panel.

    ``dialog`` is the container this is shown in, when there is one. Anything
    that remounts the page has to dismiss it first, or it outlives the page it
    belongs to.
    """
    from nicegui import events, ui

    def dismiss() -> None:
        if dialog is not None:
            dialog.close()

    async def handle_open(event: events.UploadEventArguments) -> None:
        dismiss()
        await _open_workspace_file(store, state, redraw, event)

    async def handle_restore(event: events.UploadEventArguments) -> None:
        dismiss()
        await _stage_rbsc_restore(store, state, redraw, event)

    def download_rbsc() -> None:
        try:
            ui.download.content(
                store.export_rbsc(),
                _rbsc_filename(),
                "application/json",
            )
        except Exception as exc:
            get_logger("documents").error(
                "database.export_failed",
                error_code=type(exc).__name__,
                exc_info=True,
            )
            ui.notify(_safe_error(exc), type="negative")
        else:
            get_logger("documents").info("database.exported")

    documents = getattr(getattr(state, "workspace_host", None), "document_io", None)
    with ui.column().classes("w-full max-w-4xl gap-5"):
        _identity_section(
            store,
            workspace,
            state,
            redraw,
            dismiss=dismiss,
            document_mode=documents is not None,
            settings_only=settings_only,
        )
        if settings_only:
            return
        if documents is not None:
            _desktop_document_section(state, documents, dismiss=dismiss)
            return
        _desk_section(store, state, redraw, dismiss=dismiss)
        with ui.card().props("flat bordered").classes("w-full p-5 gap-3"):
            ui.label("Open a workspace file").classes("rbs-type-section-title")
            ui.label(
                "Adds the workspace in a .rbsc file to your desk. Nothing you "
                "already have open is changed."
            ).classes("rbs-type-body rbs-text-muted")
            _rbsc_restore_upload(handle_open, label="Open workspace file")

        with ui.card().props("flat bordered").classes("w-full p-5 gap-3"):
            if allows_database_restore(state):
                ui.label("Whole database").classes("rbs-type-section-title")
                ui.label(
                    "Support and migration tools. Restoring replaces every workspace "
                    "and catalog you currently have."
                ).classes("rbs-type-body rbs-text-muted")
            else:
                ui.label("All workspaces").classes("rbs-type-section-title")
                ui.label(
                    "Every open workspace in a single file, for keeping or moving "
                    "elsewhere. Opening it later restores them alongside whatever "
                    "you have then."
                ).classes("rbs-type-body rbs-text-muted")
            with ui.row().classes("rbs-rbsc-actions items-center gap-2 flex-wrap"):
                ui.button(
                    "Download whole database"
                    if allows_database_restore(state)
                    else "Download all workspaces",
                    icon="download",
                    on_click=download_rbsc,
                ).props("outline no-caps")
                if allows_database_restore(state):
                    _rbsc_restore_upload(handle_restore, label="Replace database")


def _identity_section(
    store: WorkspaceRepository,
    workspace,
    state,
    redraw,
    *,
    dismiss=lambda: None,
    document_mode: bool = False,
    settings_only: bool = False,
) -> None:
    """Name this workspace, start another, or close this one."""
    from contextlib import nullcontext

    from nicegui import ui
    from pydantic import ValidationError

    from rbs.academic_year import academic_year_choices
    from rbs.ui.settings.view import _open_workspace_delete_dialog, save_general_workspace_settings

    if workspace is None:
        return
    today = date.today()

    def save_workspace() -> None:
        try:
            _saved, changed = save_general_workspace_settings(
                store,
                workspace,
                name=str(workspace_name.value or "").strip() or "Untitled",
                academic_year=str(academic_year.value or ""),
            )
            if not changed:
                return
            ui.notify(
                "Workspace settings updated"
                if document_mode or settings_only
                else "Workspace settings saved",
                type="positive",
            )
            dismiss()
            redraw()
        except (ValidationError, ValueError) as exc:
            ui.notify(_safe_error(exc), type="negative")

    section = (
        nullcontext()
        if settings_only
        else ui.card().props("flat bordered").classes("w-full p-5 gap-4")
    )
    with section:
        if not settings_only:
            ui.label("This workspace").classes("rbs-type-section-title")
        with ui.row().classes("rbs-workspace-settings-fields w-full"):
            workspace_name = (
                ui.input("Workspace name", value=workspace.name)
                .props("outlined")
                .classes("rbs-workspace-name-field w-72")
            )
            academic_year = (
                ui.select(
                    academic_year_choices(workspace.academic_year, today=today),
                    value=workspace.academic_year,
                    label="Academic year",
                )
                .props("outlined options-dense")
                .classes("rbs-workspace-year-field w-44")
            )
            ui.button(
                "Apply changes" if document_mode or settings_only else "Save workspace",
                icon="check",
                on_click=save_workspace,
            ).props("outline no-caps").classes("rbs-workspace-apply-button")
        if not document_mode and not settings_only:
            with ui.row().classes("items-center gap-2 flex-wrap"):
                ui.button(
                    "New workspace",
                    icon="add",
                    on_click=lambda: (dismiss(), state.create_blank()),
                ).props("outline no-caps")
                ui.button(
                    "Close workspace",
                    icon="delete_outline",
                    on_click=lambda: _open_workspace_delete_dialog(store, workspace, state, redraw),
                ).props("flat no-caps color=negative")


def _desktop_document_section(state, documents, *, dismiss=lambda: None) -> None:
    """Render native document ownership without browser upload/download controls."""
    from nicegui import ui

    from rbs.ui.workspaces.native_documents import (
        new_native_document,
        open_native_document,
        save_native_document,
    )

    with ui.card().props("flat bordered").classes("w-full p-5 gap-3"):
        ui.label("Desktop document").classes("rbs-type-section-title")
        if documents.path is None:
            ui.label("This workspace has not been saved to an .rbsc file yet.").classes(
                "rbs-type-body rbs-text-muted"
            )
        else:
            ui.label(str(documents.path)).classes("rbs-type-body rbs-text-muted break-all")

        async def new_document() -> None:
            dismiss()
            await new_native_document(state)

        async def open_document() -> None:
            dismiss()
            await open_native_document(state)

        async def save_as() -> None:
            if await save_native_document(state, save_as=True):
                dismiss()

        with ui.row().classes("items-center gap-2 flex-wrap"):
            ui.button("New…", icon="note_add", on_click=new_document).props("outline no-caps")
            ui.button("Open…", icon="folder_open", on_click=open_document).props("outline no-caps")
            ui.button("Save as…", icon="save_as", on_click=save_as).props("outline no-caps")


def allows_database_restore(state) -> bool:
    """Whether this packaging offers whole-database replace.

    Callers predating the session object are desktop-era by definition, so they
    keep the behaviour they have always had.
    """
    host = getattr(state, "workspace_host", None)
    if host is None:
        return True
    return bool(getattr(host, "allows_database_restore", True))


def _desk_section(  # noqa: ARG001 - redraw unused here
    store: WorkspaceRepository,
    state: WorkspaceSession,
    redraw,
    *,
    dismiss=lambda: None,
) -> None:
    """Show the desk when there is a live session to act on."""
    from rbs.ui.workspaces.list import render_desk

    render_desk(store, state, dismiss=dismiss)


async def _open_workspace_file(
    store: WorkspaceRepository,
    state: WorkspaceSession,
    redraw,
    event,
) -> None:
    """Merge the workspaces in an uploaded file onto this desk."""
    from nicegui import ui

    try:
        payload, _filename = await _upload_text(event, max_bytes=_upload_limit(state))
        imported = store.import_workspace_rbsc(payload)
    except Exception as exc:
        get_logger("documents").error(
            "workspace.import_failed",
            error_code=type(exc).__name__,
            exc_info=True,
        )
        ui.notify(_safe_error(exc), type="negative", multi_line=True)
        return
    state.reset_navigation(imported[0].id)
    get_logger("documents").info("workspace.imported", count=len(imported))
    noun = "workspace" if len(imported) == 1 else "workspaces"
    ui.notify(f"Opened {len(imported)} {noun}", type="positive")
    redraw()


def _safe_error(exc: Exception) -> str:
    """A message safe to show, without echoing the payload back.

    Pydantic validation errors quote the offending input, which for RBS means
    resident names and leave dates.
    """
    text = str(exc)
    if len(text) > 300 or "\n" in text:
        return "That file could not be read as an RBS workspace file."
    return text


def _rbsc_restore_upload(
    on_upload,
    *,
    label: str = "Restore RBSC",
    in_header: bool = False,
    in_menu: bool = False,
):
    from nicegui import ui

    button_props = 'flat color="white"' if in_header else 'outline color="primary"'
    upload = (
        ui.upload(
            label=label,
            auto_upload=True,
            max_files=1,
            on_upload=on_upload,
        )
        .props("accept=.rbsc no-thumbnails")
        .classes("rbs-rbsc-upload")
    )
    if in_menu:
        upload.classes(add="rbs-rbsc-upload--menu")
        upload.add_slot(
            "header",
            f"""
            <q-item clickable v-close-popup class="rbs-rbsc-upload-menu-item">
              <q-uploader-add-trigger></q-uploader-add-trigger>
              <q-item-section>{label}</q-item-section>
            </q-item>
            """,
        )
    else:
        upload.add_slot(
            "header",
            f"""
            <q-btn {button_props} no-caps icon="upload_file" label="{label}">
              <q-uploader-add-trigger></q-uploader-add-trigger>
            </q-btn>
            """,
        )
    upload.add_slot("list", "<span></span>")
    return upload


async def _stage_rbsc_restore(
    store: WorkspaceRepository,
    state: WorkspaceSession,
    redraw,
    event,
) -> None:
    from nicegui import ui

    try:
        payload, filename = await _upload_text(event, max_bytes=_upload_limit(state))
        rbsc = store.inspect_rbsc(payload)
        _open_rbsc_restore_dialog(
            store,
            state,
            redraw,
            payload=payload,
            filename=filename,
            rbsc=rbsc,
        )
    except Exception as exc:
        get_logger("documents").error(
            "database.restore_validation_failed",
            error_code=type(exc).__name__,
            exc_info=True,
        )
        ui.notify(_safe_error(exc), type="negative")


def _open_rbsc_restore_dialog(
    store: WorkspaceRepository,
    state: WorkspaceSession,
    redraw,
    *,
    payload: str,
    filename: str,
    rbsc: RBSCState,
) -> None:
    from nicegui import ui

    current_name = next(
        (
            workspace.name
            for workspace in rbsc.workspaces
            if workspace.id == rbsc.current_workspace_id
        ),
        "None selected",
    )
    with (
        ui.dialog() as dialog,
        ui.card().classes("rbs-rbsc-restore-dialog w-full max-w-2xl p-0 gap-0"),
    ):
        with ui.row().classes("w-full items-center justify-between gap-3 px-5 py-4"):
            ui.label("Restore RBSC database").classes("rbs-type-dialog-title")
            ui.button(icon="close", on_click=dialog.close).props(
                "flat round dense aria-label='Close RBSC restore dialog'"
            )
        ui.separator()
        with ui.column().classes("w-full gap-4 p-5"):
            ui.label(Path(filename).name or "database.rbsc").classes("rbs-font-semibold")
            with ui.row().classes("w-full gap-3 flex-wrap"):
                ui.badge(f"{len(rbsc.workspaces)} workspaces", color="primary").props("outline")
                ui.badge(f"{len(rbsc.catalogs)} catalogs", color="secondary").props("outline")
            ui.label(f"Selected workspace after restore: {current_name}").classes("rbs-type-body")
            ui.label(f"Exported: {rbsc.exported_at}").classes("rbs-type-caption rbs-text-muted")
            with ui.row().classes("rbs-rbsc-warning w-full items-start gap-3 rounded p-3"):
                ui.icon("warning").classes("rbs-text-warning")
                ui.label(
                    f"This will replace all {len(store.list())} workspaces and every "
                    "constraint catalog currently in this database."
                ).classes("rbs-type-body")

        def restore() -> None:
            try:
                restored = store.restore_rbsc(payload)
                workspace_id = restored.current_workspace_id
                if workspace_id is None and restored.workspaces:
                    workspace_id = restored.workspaces[0].id
                state.reset_navigation(workspace_id)
                dialog.close()
                ui.notify(
                    f"RBSC restored · {len(restored.workspaces)} workspaces",
                    type="positive",
                )
                get_logger("documents").info(
                    "database.restored",
                    count=len(restored.workspaces),
                )
                redraw()
            except Exception as exc:
                get_logger("documents").error(
                    "database.restore_failed",
                    error_code=type(exc).__name__,
                    exc_info=True,
                )
                ui.notify(str(exc), type="negative", multi_line=True)

        ui.separator()
        with ui.row().classes("w-full items-center justify-end gap-2 px-5 py-4"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
            ui.button(
                "Replace database",
                icon="restore",
                on_click=restore,
            ).props("unelevated no-caps color=negative")
    dialog.open()


def _rbsc_filename(today: date | None = None) -> str:
    return f"rbs-database-{today or date.today():%Y-%m-%d}.rbsc"


def _upload_limit(state) -> int:
    host = getattr(state, "workspace_host", None)
    return int(getattr(host, "upload_max_bytes", MAX_UPLOAD_BYTES))


async def _upload_text(
    event,
    *,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> tuple[str, str]:
    file = getattr(event, "file", None)
    if file is not None and hasattr(file, "text"):
        text = await file.text()
        return _within_limit(text, max_bytes=max_bytes), getattr(file, "name", "imported.json")
    raw = event.content.read()
    text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    return _within_limit(text, max_bytes=max_bytes), getattr(event, "name", "imported.json")


def _within_limit(text: str, *, max_bytes: int = MAX_UPLOAD_BYTES) -> str:
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError(f"that file is larger than the {max_bytes // (1024 * 1024)} MB limit")
    return text
