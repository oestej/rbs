"""Header status chips, banners, and workspace-state summaries."""

from __future__ import annotations

from rbs.models.workspace import DownloadState, Workspace
from rbs.ui.app_documents import (
    _document_io,
    _notify_recovery_error,
    document_summary,
)
from rbs.ui.session import WorkspaceSession
from rbs.ui.workspaces import file_handle
from rbs.ui.workspaces.status import (
    PILL_ALERT,
    PILL_MUTED,
    PILL_WARN,
    download_summary,
    pill_classes,
)


def solve_summary(workspace: Workspace) -> tuple[str, str] | None:
    """A compact read on whether the schedule reflects the current inputs.

    ``None`` where there is nothing worth saying - a solved, complete schedule
    needs no pill, and a header that is always full of badges stops being read.
    """
    if workspace.solution_is_out_of_date:
        return "Solver out of date", PILL_WARN
    if workspace.schedule is None:
        return (
            ("Solver out of date", PILL_WARN)
            if workspace.stale_schedule
            else (
                "Not solved",
                PILL_MUTED,
            )
        )
    if _workspace_open_week_count(workspace):
        return "Needs solve", PILL_WARN
    return None


def _solve_chip(session: WorkspaceSession, workspace: Workspace) -> None:
    from nicegui import ui

    summary = solve_summary(workspace)
    if summary is None:
        session.solve_chip = None
        return
    label, tone = summary
    session.solve_chip = (
        ui.badge(label, color=None)
        .classes(f"{pill_classes(tone)} shrink-0")
        .tooltip(_workspace_status(workspace))
    )


def _download_chip(session: WorkspaceSession, workspace: Workspace) -> None:
    from nicegui import ui

    documents = _document_io(session)
    if workspace.is_sample:
        label, tone = "Sample Data", PILL_ALERT
        file_is_out_of_date = False
    elif documents is None:
        label, tone = download_summary(workspace)
        file_is_out_of_date = workspace.download_state is not DownloadState.CURRENT
    else:
        label, tone = document_summary(documents)
        file_is_out_of_date = documents.dirty
        _notify_recovery_error(session, documents)
    session.download_chip = ui.badge(label, color=None).classes(f"{pill_classes(tone)} shrink-0")
    if (
        not workspace.is_sample
        and documents is None
        and workspace.download_state is DownloadState.NEVER
    ):
        session.download_chip.set_visibility(False)
    if documents is not None and documents.path is not None:
        session.download_chip.tooltip(str(documents.path))
    file_handle.set_unsaved(
        ui,
        _should_warn_before_leave(session, workspace, file_is_out_of_date),
    )


def _refresh_status_chips(session: WorkspaceSession) -> None:
    """Update the header pills in place, without remounting the page."""
    _refresh_download_chip(session)
    workspace = session.workspace()
    if workspace is None or session.solve_chip is None:
        return
    summary = solve_summary(workspace)
    if summary is None:
        session.solve_chip.set_visibility(False)
        return
    label, tone = summary
    session.solve_chip.set_visibility(True)
    session.solve_chip.text = label
    session.solve_chip.classes(replace=f"{pill_classes(tone)} shrink-0")


def _refresh_download_chip(session: WorkspaceSession) -> None:
    from nicegui import ui

    workspace = session.workspace()
    if workspace is None or session.download_chip is None:
        return
    documents = _document_io(session)
    if workspace.is_sample:
        label, tone = "Sample Data", PILL_ALERT
        file_is_out_of_date = False
    elif documents is None:
        label, tone = download_summary(workspace)
        file_is_out_of_date = workspace.download_state is not DownloadState.CURRENT
    else:
        label, tone = document_summary(documents)
        file_is_out_of_date = documents.dirty
        _notify_recovery_error(session, documents)
    show_chip = (
        workspace.is_sample
        or documents is not None
        or workspace.download_state is not DownloadState.NEVER
    )
    session.download_chip.text = label
    session.download_chip.classes(
        replace=f"{pill_classes(tone)} shrink-0" + ("" if show_chip else " hidden")
    )
    session.download_chip.set_visibility(show_chip)
    file_handle.set_unsaved(
        ui,
        _should_warn_before_leave(session, workspace, file_is_out_of_date),
    )


def _should_warn_before_leave(
    session: WorkspaceSession,
    workspace: Workspace,
    file_is_out_of_date: bool,
) -> bool:
    """Warn only for changes made after this page opened.

    Download state describes the user's file, not whether this browser page has
    changed anything. Treating a never-downloaded or previously stale workspace
    as a page edit caused a warning on every reload. The first rendered revision
    is the quiet baseline; a later revision can arm the guard until the file is
    current again.
    """
    current = (workspace.id, workspace.workspace_revision)
    baseline = session._leave_guard_baseline
    if baseline is None or baseline[0] != workspace.id:
        session._leave_guard_baseline = current
        return False
    return file_is_out_of_date and current[1] != baseline[1]


def _workspace_status(workspace: Workspace) -> str:
    if workspace.solution_is_out_of_date:
        return "Solution out of date"
    schedule = workspace.schedule
    if schedule is None:
        return "No schedule"
    open_weeks = _workspace_open_week_count(workspace)
    if open_weeks:
        noun = "week" if open_weeks == 1 else "weeks"
        return f"Needs solve · {open_weeks} schedule {noun} open"
    status = f"{schedule.meta.status.value} · {schedule.meta.engine}"
    if (
        schedule.meta.solver_status is not None
        and schedule.meta.solver_status is not schedule.meta.status
    ):
        status += f" · solver {schedule.meta.solver_status.value}"
    if schedule.meta.wall_time_seconds is not None:
        status += f" · {schedule.meta.wall_time_seconds:.2f}s"
    return status


def _workspace_open_week_count(workspace: Workspace) -> int:
    schedule = workspace.schedule
    if schedule is None:
        return 0
    expected = set(range(1, workspace.instance.calendar.weeks + 1))
    grid = schedule.week_grid
    return sum(
        len(expected - {int(week) for week in grid.get(resident.id, {})})
        for resident in workspace.instance.residents
    )


def _retention_banner(session: WorkspaceSession) -> None:
    """Warn about a desk that is approaching its retention window.

    Rendered only where a host actually expires things, so the desktop build
    shows no retention chrome at all rather than a disabled version of it.
    """
    from datetime import UTC, datetime

    from nicegui import ui

    status = session.workspace_host.session_status(session.principal)
    now = datetime.now(UTC)
    if status is None or not status.should_warn(now):
        return
    days = max(0, status.remaining(now).days)
    with ui.row().classes("rbs-retention-banner w-full items-center gap-2 rounded p-3"):
        ui.icon("schedule").classes("rbs-text-warning")
        ui.label(
            f"This desk is closed automatically in {days} day{'s' if days != 1 else ''} "
            "if nothing changes. Save anything you want to keep."
        ).classes("rbs-type-body")


def _eviction_banner(session: WorkspaceSession) -> None:
    """Tell a returning user what happened, instead of showing an empty desk."""
    from nicegui import ui

    notice = session.workspace_host.eviction_notice(session.principal)
    if notice is None:
        return
    count = notice.workspace_count
    noun = "workspace" if count == 1 else "workspaces"
    with ui.row().classes("rbs-eviction-banner w-full items-start gap-3 rounded p-3"):
        ui.icon("info").classes("rbs-text-warning")
        ui.label(
            f"Your {count} {noun} {'was' if count == 1 else 'were'} closed on "
            f"{notice.evicted_at:%d %B %Y} after going unused. Open a saved file "
            "to pick up where you left off."
        ).classes("rbs-type-body")
        acknowledge = getattr(session.workspace_host, "acknowledge_eviction", None)
        if acknowledge is not None:
            ui.button(
                "Dismiss",
                on_click=lambda: (acknowledge(session.principal), session.rebuild()),
            ).props("flat dense no-caps")
