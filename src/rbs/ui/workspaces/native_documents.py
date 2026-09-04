"""Native document commands shared by chrome and the Workspace panel."""

from __future__ import annotations

from rbs.logging import get_logger
from rbs.ui.session import WorkspaceSession


def document_io(session: WorkspaceSession):
    return getattr(session.workspace_host, "document_io", None)


async def save_native_document(
    session: WorkspaceSession,
    *,
    save_as: bool = False,
    on_saved=None,
) -> bool:
    from nicegui import ui

    documents = document_io(session)
    if documents is None:
        return False
    workspace = getattr(documents, "workspace", None)
    if not save_as and workspace is not None and workspace.is_sample:
        ui.notify("Use Save as… to make a copy of Sample Data.", type="info")
        return False
    converting_sample = bool(save_as and workspace is not None and workspace.is_sample)
    try:
        destination = await (documents.save_as() if save_as else documents.save())
    except Exception as exc:
        get_logger("documents").error(
            "document.save_failed",
            error_code=type(exc).__name__,
            exc_info=True,
        )
        ui.notify(f"Save failed: {exc}", type="negative", multi_line=True)
        return False
    if destination is None:
        get_logger("documents").info("document.save_cancelled")
        ui.notify("Save cancelled - nothing was written", type="info")
        return False
    session.touch()
    get_logger("documents").info(
        "document.saved",
        source="save_as" if save_as else "save",
    )
    set_native_document_title(documents)
    ui.notify(f"Saved to {destination.name}", type="positive")
    if on_saved is not None:
        on_saved()
    elif converting_sample:
        session.rebuild()
    return True


async def open_native_document(session: WorkspaceSession) -> None:
    """Open a native document after guarding the current unsaved work."""
    from nicegui import ui

    documents = document_io(session)
    if documents is None:
        return
    if session.solving:
        ui.notify("Wait for the current solve to finish before opening a file.", type="warning")
        return

    async def choose_and_open(confirm_dialog=None) -> None:
        try:
            workspace = await documents.open()
        except Exception as exc:
            get_logger("documents").error(
                "document.open_failed",
                error_code=type(exc).__name__,
                exc_info=True,
            )
            ui.notify(f"Open failed: {exc}", type="negative", multi_line=True)
            return
        if workspace is None:
            get_logger("documents").info("document.open_cancelled")
            return
        if confirm_dialog is not None:
            confirm_dialog.close()
        session.reset_navigation(workspace.id)
        get_logger("documents").info("document.opened")
        session.rebuild()
        set_native_document_title(documents)
        ui.notify(f"Opened {documents.path.name}", type="positive")

    if not documents.dirty:
        await choose_and_open()
        return

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-md gap-4 p-5"):
        ui.label("Save changes before opening another file?").classes("rbs-type-dialog-title")
        ui.label("Opening another .rbsc file replaces the document in this window.").classes(
            "rbs-type-body rbs-text-muted"
        )

        async def save_then_open() -> None:
            if await save_native_document(session):
                await choose_and_open(dialog)

        async def discard_then_open() -> None:
            await choose_and_open(dialog)

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
            ui.button("Discard and open", on_click=discard_then_open).props(
                "flat no-caps color=negative"
            )
            ui.button("Save and open", icon="save", on_click=save_then_open).props(
                "unelevated no-caps"
            )
    dialog.open()


async def new_native_document(
    session: WorkspaceSession,
    *,
    sample: bool = False,
) -> None:
    """Start a blank or sample native document after guarding the current draft."""
    from nicegui import ui

    documents = document_io(session)
    if documents is None:
        return
    if session.solving:
        ui.notify(
            "Wait for the current solve to finish before starting a new file.",
            type="warning",
        )
        return

    async def create_document(confirm_dialog=None) -> None:
        try:
            workspace = documents.new(sample=True) if sample else documents.new()
        except Exception as exc:
            get_logger("documents").error(
                "document.new_failed",
                error_code=type(exc).__name__,
                exc_info=True,
            )
            ui.notify(f"New document failed: {exc}", type="negative", multi_line=True)
            return
        if confirm_dialog is not None:
            confirm_dialog.close()
        session.reset_navigation(workspace.id)
        get_logger("documents").info("document.created")
        session.rebuild()
        set_native_document_title(documents)

    if not documents.dirty:
        await create_document()
        return

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-md gap-4 p-5"):
        ui.label("Save changes before starting a new file?").classes("rbs-type-dialog-title")
        ui.label("A new document replaces this draft in the window.").classes(
            "rbs-type-body rbs-text-muted"
        )

        async def save_then_create() -> None:
            if await save_native_document(session):
                await create_document(dialog)

        async def discard_then_create() -> None:
            await create_document(dialog)

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
            ui.button("Discard and create", on_click=discard_then_create).props(
                "flat no-caps color=negative"
            )
            ui.button("Save and create", icon="save", on_click=save_then_create).props(
                "unelevated no-caps"
            )
    dialog.open()


async def close_native_document(session: WorkspaceSession) -> None:
    """Close the native document, guarding unsaved work without touching its file."""
    from nicegui import ui

    documents = document_io(session)
    if documents is None or getattr(documents, "workspace", None) is None:
        return
    if session.solving:
        ui.notify("Wait for the current solve to finish before closing the file.", type="warning")
        return

    def close_document(confirm_dialog=None) -> None:
        try:
            documents.close()
        except Exception as exc:
            get_logger("documents").error(
                "document.close_failed",
                error_code=type(exc).__name__,
                exc_info=True,
            )
            ui.notify(f"Close failed: {exc}", type="negative", multi_line=True)
            return
        if confirm_dialog is not None:
            confirm_dialog.close()
        session.reset_navigation(None)
        get_logger("documents").info("document.closed")
        session.rebuild()
        set_native_document_title(documents)

    if documents.workspace.is_sample or not documents.dirty:
        close_document()
        return

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-xl gap-4 p-5"):
        ui.label("Save changes before closing this workspace?").classes("rbs-type-dialog-title")
        ui.label(
            "Closing leaves the saved .rbsc file untouched. Unsaved changes will be lost."
        ).classes("rbs-type-body rbs-text-muted")

        async def save_then_close() -> None:
            if await save_native_document(session):
                close_document(dialog)

        with ui.row().classes("w-full items-center justify-end gap-2 no-wrap"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps no-wrap")
            ui.button(
                "Close without saving",
                on_click=lambda: close_document(dialog),
            ).props("flat no-caps no-wrap color=negative")
            ui.button("Save and close", icon="save", on_click=save_then_close).props(
                "unelevated no-caps no-wrap"
            )
    dialog.open()


def set_native_document_title(documents) -> None:
    from nicegui import app

    window = app.native.main_window
    if window is None:
        return
    if getattr(documents, "workspace", object()) is None:
        window.set_title(documents.application_name)
        return
    name = documents.path.name if documents.path is not None else "Untitled"
    window.set_title(f"{name} — {documents.application_name}")
