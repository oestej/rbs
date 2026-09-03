"""Solver run flow: progress overlay, result handling, diagnostics."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from time import monotonic

from rbs.logging import (
    get_logger,
)
from rbs.models.enums import SolverStatus
from rbs.models.schedule import SolverDiagnostic
from rbs.solver.reference import changed_resident_weeks
from rbs.ui.app_branding import dialog_wordmark
from rbs.ui.app_documents import _document_io
from rbs.ui.app_status import _refresh_status_chips
from rbs.ui.locks import refresh_locks_through_today
from rbs.ui.session import WorkspaceSession
from rbs.workspaces import WorkspaceController


@dataclass(slots=True)
class SolverProgressOverlay:
    """The progress dialog and its timer, which must stop together."""

    dialog: object
    timer: object

    def close(self) -> None:
        self.timer.cancel()
        self.dialog.close()


async def _solve(session: WorkspaceSession) -> None:
    from nicegui import ui

    if session.solving:
        return
    workspace = session.workspace()
    if workspace is None:
        return
    documents = _document_io(session)
    document_generation = documents.generation if documents is not None else None
    diagnostics: list[SolverDiagnostic] = []
    draft_kept = False
    operation_id = str(uuid.uuid4())
    started_at = monotonic()
    logger = get_logger("ui.solver")
    logger.info(
        "solver.requested",
        operation_id=operation_id,
        engine=workspace.instance.solver.engine.value,
        num_workers=workspace.instance.solver.num_workers,
        time_limit_seconds=workspace.instance.solver.time_limit_seconds,
    )
    session.solving = True
    progress = _open_solver_progress()
    try:
        today = date.today()
        instance = workspace.instance
        if instance.lock_through_today and workspace.schedule is not None:
            refreshed = refresh_locks_through_today(instance, workspace.schedule, today)
            if refreshed != instance:
                workspace = WorkspaceController(session.store).save_instance(
                    workspace,
                    refreshed,
                    preserve_schedule=True,
                )
                instance = workspace.instance
        reference_schedule = workspace.latest_schedule
        schedule = await session.workspace_host.solve(
            session.principal,
            instance,
            reference_schedule=reference_schedule,
        )
        if documents is not None and documents.generation != document_generation:
            logger.info(
                "solver.result_discarded",
                operation_id=operation_id,
                duration_ms=round((monotonic() - started_at) * 1000),
                reason="document_changed",
            )
            progress.close()
            ui.notify(
                "The document changed while the solver was running; its result was discarded.",
                type="warning",
            )
            return
        diagnostics = schedule.meta.diagnostics
        ok = schedule.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
        draft_kept = bool(reference_schedule is not None and not ok)
        if not draft_kept:
            workspace = WorkspaceController(session.store).save_schedule(workspace, schedule)
        if ok and instance.lock_through_today:
            refreshed = refresh_locks_through_today(instance, schedule, today)
            if refreshed != instance:
                workspace = WorkspaceController(session.store).save_instance(
                    workspace,
                    refreshed,
                    preserve_schedule=True,
                )
        progress.close()
        changed_weeks, compared_weeks = changed_resident_weeks(
            instance,
            reference_schedule,
            schedule,
        )
        result_message = f"Solver {schedule.meta.status.value}"
        if ok and compared_weeks:
            result_message += f" · {changed_weeks} resident-weeks changed"
        elif draft_kept:
            result_message += " · current draft kept"
        if diagnostics:
            result_message += " · explanation opened"
        ui.notify(
            result_message,
            type="positive" if ok else "warning",
        )
        logger.info(
            "solver.result_applied",
            operation_id=operation_id,
            duration_ms=round((monotonic() - started_at) * 1000),
            outcome=schedule.meta.status.value,
            engine=schedule.meta.engine.value,
        )
    except Exception as exc:
        logger.error(
            "solver.request_failed",
            operation_id=operation_id,
            duration_ms=round((monotonic() - started_at) * 1000),
            error_code=getattr(exc, "code", type(exc).__name__),
            exc_info=True,
        )
        progress.close()
        ui.notify(str(exc), type="negative")
    finally:
        session.solving = False
        session.mark_stale()
        session.refresh_visible()
        _refresh_status_chips(session)
        if diagnostics:
            _open_solver_diagnostics(diagnostics, draft_kept=draft_kept)


def _format_elapsed(seconds: float) -> str:
    """Format elapsed time as minutes, seconds, and tenths without rounding ahead."""
    total_tenths = int(max(0.0, seconds) * 10)
    minutes, within_minute = divmod(total_tenths, 600)
    whole_seconds, tenths = divmod(within_minute, 10)
    return f"{minutes}:{whole_seconds:02d}.{tenths}"


def _open_solver_progress() -> SolverProgressOverlay:
    """Show the solver's non-dismissible progress overlay."""
    from nicegui import ui

    started_at = monotonic()
    with (
        ui.dialog().props("persistent").classes("rbs-overlay-dialog") as dialog,
        ui.card().classes(
            "rbs-popout-dialog rbs-branded-dialog rbs-overlay-card rbs-solver-progress p-0 gap-0"
        ),
    ):
        dialog_wordmark().classes("rbs-overlay-wordmark")
        ui.label(
            "Running Solve... Please be patient, this process can take a few minutes."
        ).classes("rbs-overlay-message text-center")
        with ui.row().classes(
            "rbs-spinner-status rbs-solver-progress-status items-center justify-center"
        ):
            ui.element("div").classes("rbs-overlay-spinner")
            elapsed = (
                ui.label("0:00.0")
                .props("aria-hidden=true")
                .classes("rbs-elapsed-time rbs-solver-elapsed")
            )
    dialog.open()

    def update_elapsed() -> None:
        elapsed.set_text(_format_elapsed(monotonic() - started_at))

    timer = ui.timer(0.1, update_elapsed, immediate=False)
    return SolverProgressOverlay(dialog=dialog, timer=timer)


def _open_solver_diagnostics(
    diagnostics: list[SolverDiagnostic],
    *,
    draft_kept: bool,
) -> None:
    """Keep an actionable solve explanation visible until the user dismisses it."""
    from nicegui import ui

    with (
        ui.dialog().classes("rbs-overlay-dialog") as dialog,
        ui.card().classes(
            "rbs-popout-dialog rbs-branded-dialog rbs-overlay-card "
            "rbs-solver-diagnostics w-full max-w-2xl p-0 gap-0"
        ),
    ):
        ui.button(icon="close", on_click=dialog.close).props(
            "flat round dense aria-label='Close solver diagnostics'"
        ).classes("rbs-popout-close rbs-overlay-close")
        dialog_wordmark().classes("rbs-overlay-wordmark")
        ui.label("No feasible schedule").classes("rbs-overlay-title")
        if draft_kept:
            ui.label(
                "Your current draft was kept. Resolve one of the conflicts below and solve again."
            ).classes("rbs-overlay-detail")
        for diagnostic in diagnostics:
            with ui.column().classes("rbs-solver-diagnostic w-full gap-2"):
                ui.label(diagnostic.message).classes("rbs-type-body-large")
                if diagnostic.suggestions:
                    ui.label("Ways to resolve it").classes("rbs-type-control-label")
                    for suggestion in diagnostic.suggestions:
                        ui.label(f"• {suggestion}").classes("rbs-type-body")
    dialog.open()
