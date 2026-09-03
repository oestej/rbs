"""The desk: what you have open, and how you put something away.

A list of workspaces is the feature most likely to turn a support tool into a
system of record, and what stops it here is that **Close** is a real deletion
rather than an archive. So the close path deliberately makes the state of the
user's own file the thing it asks about: a workspace with unsaved changes cannot
be closed casually, and a closed one does not come back.
"""

from __future__ import annotations

from rbs.models.workspace import Workspace
from rbs.repository import WorkspaceRepository
from rbs.ui.workspaces.close import close_workspace
from rbs.ui.workspaces.status import download_summary, pill_classes


def render_desk(
    store: WorkspaceRepository,
    session,
    *,
    dismiss=lambda: None,
) -> None:
    """Everything currently open, with its save state and a way to close it."""
    from nicegui import ui

    workspaces = [item for item in store.list() if item.id != session.workspace_id]
    if not workspaces:
        return

    with ui.card().props("flat bordered").classes("w-full p-5 gap-3"):
        ui.label("Other open workspaces").classes("rbs-type-section-title")
        ui.label(
            "Closing a workspace permanently deletes this server's copy. Your "
            "saved files are not affected."
        ).classes("rbs-type-body rbs-text-muted")
        for workspace in workspaces:
            _render_row(session, workspace, dismiss)


def _render_row(session, workspace: Workspace, dismiss) -> None:
    from nicegui import ui

    label, tone = download_summary(workspace)
    with ui.row().classes("rbs-desk-row w-full items-center justify-between gap-3 flex-wrap"):
        with ui.column().classes("gap-1 min-w-0"):
            ui.label(workspace.name).classes("rbs-type-body-large rbs-font-semibold")
            with ui.row().classes("items-center gap-2"):
                ui.label(workspace.academic_year).classes("rbs-type-caption rbs-text-muted")
                ui.badge(label, color=None).classes(pill_classes(tone))
        with ui.row().classes("items-center gap-2"):
            if workspace.id != session.workspace_id:
                ui.button(
                    "Open",
                    on_click=lambda _e=None, w=workspace: (
                        dismiss(),
                        session.switch_workspace(w.id),
                    ),
                ).props("outline no-caps dense")
            ui.button(
                "Close",
                icon="delete_outline",
                on_click=lambda _e=None, w=workspace: (
                    dismiss(),
                    close_workspace(session, w),
                ),
            ).props("flat no-caps dense color=negative")
