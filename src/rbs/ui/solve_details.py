"""Persistent solve notes and live warnings about protected attending work."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rbs.attending_schedule import preserved_attending_work_issues
from rbs.ui.buttons import ICON_BUTTON_PROPS, SECONDARY_BUTTON_PROPS, button_props

if TYPE_CHECKING:
    from rbs.ui.session import WorkspaceSession


def render_solver_notes(notes: list[str]) -> None:
    from nicegui import ui

    with ui.expansion("Solver notes", icon="notes").classes("w-full"):
        if notes:
            for note in dict.fromkeys(notes):
                ui.label(note).classes("rbs-type-body whitespace-pre-wrap")
        else:
            ui.label("No solve notes for this schedule.").classes("rbs-type-caption rbs-text-muted")


def open_solve_details(session: WorkspaceSession) -> None:
    from nicegui import ui

    workspace = session.workspace()
    if workspace is None:
        return
    schedule = workspace.latest_schedule
    warnings = preserved_attending_work_issues(workspace.instance, schedule)
    with (
        ui.dialog() as dialog,
        ui.card()
        .classes("rbs-popout-dialog w-full max-w-2xl p-5 gap-4")
        .style("max-height:85vh;overflow-y:auto"),
    ):
        with ui.row().classes("w-full items-center justify-between gap-3"):
            ui.label("Solve details").classes("rbs-type-dialog-title")
            ui.button(icon="close", on_click=dialog.close).props(
                button_props(
                    ICON_BUTTON_PROPS,
                    "aria-label='Close solve details'",
                )
            ).tooltip("Close solve details")
        if workspace.solution_is_out_of_date:
            ui.label(
                "Inputs have changed since this schedule was saved. "
                "The warnings below reflect the current inputs."
            ).classes("rbs-type-caption rbs-text-muted")
        if warnings:
            ui.label("Protected attending work").classes("rbs-type-section-title")
            for issue in warnings:
                with ui.row().classes("w-full items-start gap-2 no-wrap"):
                    ui.icon("warning_amber").classes("rbs-text-warning")
                    ui.label(issue.message).classes("rbs-type-body")
        if schedule is not None:
            for warning in schedule.meta.validation_warnings:
                ui.label(warning).classes("rbs-type-body")
            for diagnostic in schedule.meta.diagnostics:
                ui.label(diagnostic.message).classes("rbs-type-body")
            render_solver_notes(schedule.meta.notes)
        else:
            ui.label("No saved solve results yet.").classes("rbs-type-body rbs-text-muted")
    dialog.open()


def solve_details_button(session: WorkspaceSession) -> None:
    from nicegui import ui

    session.solve_details_button = ui.button(
        "Solve details",
        icon="info_outline",
        on_click=lambda: open_solve_details(session),
    ).props(button_props(
        SECONDARY_BUTTON_PROPS, "color=white", "aria-label='Solve details'",
    )).classes("rbs-solve-details-button")
    with session.solve_details_button:
        ui.tooltip().bind_text_from(session.solve_details_button, "text")
    refresh_solve_details_button(session)


def refresh_solve_details_button(session: WorkspaceSession) -> None:
    button = getattr(session, "solve_details_button", None)
    if button is None:
        return
    workspace = session.workspace()
    if workspace is None:
        button.set_visibility(False)
        return
    schedule = workspace.latest_schedule
    warnings = preserved_attending_work_issues(workspace.instance, schedule)
    count = len(warnings)
    button.set_visibility(schedule is not None or bool(warnings))
    button.set_text(
        f"Solve details · {count} {'warning' if count == 1 else 'warnings'}"
        if count
        else "Solve details"
    )
    button.props("icon=warning_amber" if count else "icon=info_outline")
    button.props(f"aria-label='{button.text}'")
