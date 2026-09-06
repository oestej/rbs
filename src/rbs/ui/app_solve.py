"""Solver run flow: progress overlay, result handling, diagnostics."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from functools import partial
from time import monotonic

from rbs.logging import (
    get_logger,
)
from rbs.models.enums import RotationKind, SolverStatus
from rbs.models.instance import SolverProblem
from rbs.models.schedule import Schedule, SolverDiagnostic
from rbs.solver.readiness import (
    ReadinessIssue,
    ReadinessResult,
    check_solve_readiness,
)
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


@dataclass(frozen=True, slots=True)
class SolverOutcomePresentation:
    """User-facing meaning of a non-successful solver result."""

    title: str
    detail: str
    notification: str
    diagnostics: tuple[SolverDiagnostic, ...]


async def _solve(session: WorkspaceSession) -> None:
    from nicegui import ui

    if session.solving:
        return
    workspace = session.workspace()
    if workspace is None:
        return
    readiness = check_solve_readiness(SolverProblem.from_instance(workspace.instance))
    if not readiness.ready:
        _open_readiness_diagnostics(session, readiness)
        ui.notify(
            f"Solve did not start · {len(readiness.errors)} configuration "
            f"{'conflict' if len(readiness.errors) == 1 else 'conflicts'}",
            type="warning",
        )
        return
    documents = _document_io(session)
    document_generation = documents.generation if documents is not None else None
    draft_kept = False
    outcome: SolverOutcomePresentation | None = None
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
        ok = (
            schedule.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
            and not schedule.meta.validation_errors
        )
        draft_kept = bool(reference_schedule is not None and not ok)
        outcome = None if ok else _solver_outcome(schedule)
        # A post-processed candidate which fails validation is useful evidence,
        # but it is not a schedule the rest of the application should accept.
        if not draft_kept and not schedule.meta.validation_errors:
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
        elif schedule.meta.validation_errors:
            result_message += " · invalid result not saved"
        if outcome is not None:
            result_message = outcome.notification
            if draft_kept:
                result_message += " · current draft kept"
            elif schedule.meta.validation_errors:
                result_message += " · result not saved"
            result_message += " · explanation opened"
        ui.notify(
            result_message,
            type="positive" if ok else "warning",
        )
        logger.info(
            (
                "solver.result_rejected"
                if schedule.meta.validation_errors
                else "solver.result_applied"
            ),
            operation_id=operation_id,
            duration_ms=round((monotonic() - started_at) * 1000),
            outcome=schedule.meta.status.value,
            solver_outcome=(
                schedule.meta.solver_status.value
                if schedule.meta.solver_status is not None
                else None
            ),
            validation_error_count=len(schedule.meta.validation_errors),
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
        if outcome is not None:
            _open_solver_diagnostics(
                list(outcome.diagnostics),
                draft_kept=draft_kept,
                title=outcome.title,
                detail=outcome.detail,
                session=session,
            )


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
    title: str = "No feasible block schedule",
    detail: str | None = None,
    session: WorkspaceSession | None = None,
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
        ui.label(title).classes("rbs-overlay-title")
        if detail:
            ui.label(detail).classes("rbs-overlay-detail")
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
                if session is not None and diagnostic.code == "clinic_allocation_capacity":
                    ui.button(
                        "Open Clinic capacity",
                        icon="open_in_new",
                        on_click=partial(
                            _open_clinic_configuration,
                            session,
                            dialog,
                            section="clinic_sites",
                        ),
                    ).props("outline no-caps")
                if session is not None and diagnostic.resident_ids:
                    resident_id = diagnostic.resident_ids[0]
                    ui.button(
                        _resident_action_label(session, resident_id),
                        icon="open_in_new",
                        on_click=partial(
                            _open_resident_configuration,
                            session,
                            dialog,
                            resident_id=resident_id,
                        ),
                    ).props("outline no-caps")
                if session is not None and diagnostic.special_rotation_ids:
                    ui.button(
                        "Open Special events",
                        icon="open_in_new",
                        on_click=partial(
                            _open_rotation_section,
                            session,
                            dialog,
                            section="special_configuration",
                        ),
                    ).props("outline no-caps")
    dialog.open()


def _open_readiness_diagnostics(
    session: WorkspaceSession,
    readiness: ReadinessResult,
) -> None:
    """Explain every pre-solve conflict and link to the relevant editor."""
    from nicegui import ui

    issues = readiness.issues or tuple(
        ReadinessIssue(code="configuration_conflict", message=message)
        for message in readiness.errors
    )
    count = len(issues)
    noun = "conflict" if count == 1 else "conflicts"
    with (
        ui.dialog().classes("rbs-overlay-dialog") as dialog,
        ui.card().classes(
            "rbs-popout-dialog rbs-branded-dialog rbs-overlay-card "
            "rbs-solver-diagnostics w-full max-w-2xl p-0 gap-0"
        ),
    ):
        ui.button(icon="close", on_click=dialog.close).props(
            "flat round dense aria-label='Close solve readiness diagnostics'"
        ).classes("rbs-popout-close rbs-overlay-close")
        dialog_wordmark().classes("rbs-overlay-wordmark")
        ui.label(f"Cannot solve · {count} configuration {noun}").classes(
            "rbs-overlay-title"
        )
        ui.label(
            "Solve did not start. Fix these settings, then run Solve again."
        ).classes("rbs-overlay-detail")
        for issue in issues:
            with ui.column().classes("rbs-solver-diagnostic w-full gap-2"):
                ui.label(issue.message).classes("rbs-type-body-large")
                if issue.suggestions:
                    ui.label("Ways to resolve it").classes("rbs-type-control-label")
                    for suggestion in issue.suggestions:
                        ui.label(f"• {suggestion}").classes("rbs-type-body")
                ui.button(
                    _readiness_action_label(session, issue),
                    icon="open_in_new",
                    on_click=partial(
                        _navigate_to_readiness_issue,
                        session,
                        issue,
                        dialog,
                    ),
                ).props("outline no-caps")
    dialog.open()


def _readiness_action_label(
    session: WorkspaceSession,
    issue: ReadinessIssue,
) -> str:
    destination = _readiness_destination(session, issue)
    if destination == ("clinic", "clinic_block_rules"):
        return "Open Clinic block rules"
    if destination == ("rotations", "rotation_summary"):
        return "Open Rotations"
    if destination == ("rotations", "fmed_configuration"):
        return "Open FMED/Inpatient"
    if destination == ("rotations", "elective_configuration"):
        return "Open Electives"
    workspace = session.workspace()
    rotation = (
        workspace.instance.rotations_by_id.get(issue.rotation_id)
        if workspace is not None and issue.rotation_id is not None
        else None
    )
    return f"Open {rotation.name}" if rotation is not None else "Open rotation"


def _navigate_to_readiness_issue(
    session: WorkspaceSession,
    issue: ReadinessIssue,
    dialog: object,
) -> None:
    """Close the explanation and select the configuration surface it names."""
    active_tab, section = _readiness_destination(session, issue)
    dialog.close()
    session.active_tab = active_tab
    if active_tab == "clinic":
        session.clinic_section = section
        session.rotation_id = None
    else:
        session.rotation_section = section
        session.rotation_id = issue.rotation_id
    session.rebuild()


def _readiness_destination(
    session: WorkspaceSession,
    issue: ReadinessIssue,
) -> tuple[str, str]:
    if issue.code == "missing_elective_fallback":
        return "clinic", "clinic_block_rules"
    workspace = session.workspace()
    if issue.rotation_id is None or workspace is None:
        return "rotations", "rotation_summary"
    rotation = workspace.instance.rotations_by_id.get(issue.rotation_id)
    if rotation is None:
        return "rotations", "rotation_summary"
    if rotation.kind is RotationKind.CLINIC:
        return "clinic", "clinic_block_rules"
    if rotation.kind is RotationKind.FMED:
        return "rotations", "fmed_configuration"
    if rotation.kind is RotationKind.ELECTIVE:
        return "rotations", "elective_configuration"
    return "rotations", "standard_rotations"


def _open_clinic_configuration(
    session: WorkspaceSession,
    dialog: object,
    *,
    section: str,
) -> None:
    dialog.close()
    session.active_tab = "clinic"
    session.clinic_section = section
    session.rotation_id = None
    session.rebuild()


def _resident_action_label(session: WorkspaceSession, resident_id: str) -> str:
    workspace = session.workspace()
    resident = (
        workspace.instance.residents_by_id.get(resident_id)
        if workspace is not None
        else None
    )
    return f"Open {resident.name}" if resident is not None else "Open resident"


def _open_resident_configuration(
    session: WorkspaceSession,
    dialog: object,
    *,
    resident_id: str,
) -> None:
    dialog.close()
    session.active_tab = "residents"
    session.resident_id = resident_id
    session.resident_block_schedule_editing = False
    session.resident_schedule_editing = False
    session.resident_schedule_section = "resident_block_schedule"
    session.rebuild()


def _open_rotation_section(
    session: WorkspaceSession,
    dialog: object,
    *,
    section: str,
) -> None:
    dialog.close()
    session.active_tab = "rotations"
    session.rotation_section = section
    session.rotation_id = None
    session.rebuild()


def _solver_outcome(schedule: Schedule) -> SolverOutcomePresentation:
    """Separate model infeasibility, time limits, and post-solve invalidity."""
    raw_status = schedule.meta.solver_status or schedule.meta.status
    diagnostics = tuple(schedule.meta.diagnostics) or _fallback_diagnostics(schedule)
    has_clinic_failure = any(
        diagnostic.code == "clinic_allocation_capacity" for diagnostic in diagnostics
    )
    solved_model = raw_status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}

    if solved_model and schedule.meta.validation_errors:
        if has_clinic_failure:
            return SolverOutcomePresentation(
                title="Block schedule found, but clinic placement failed",
                detail=(
                    "The block model was feasible, but clinic-site assignment exceeded "
                    "configured capacity. The generated schedule was not accepted."
                ),
                notification="Clinic placement failed",
                diagnostics=diagnostics,
            )
        return SolverOutcomePresentation(
            title="Schedule found, but validation failed",
            detail=(
                "The solver found a block schedule, but the completed result violated "
                "one or more schedule rules and was not accepted."
            ),
            notification="Schedule validation failed",
            diagnostics=diagnostics,
        )

    if any(diagnostic.code == "model_build_error" for diagnostic in diagnostics):
        return SolverOutcomePresentation(
            title="Cannot build schedule model",
            detail="The configured rules could not be converted into a solver model.",
            notification="Schedule model could not be built",
            diagnostics=diagnostics,
        )

    timed_out = raw_status is SolverStatus.UNKNOWN and any(
        "within" in note and "no feasible schedule" in note.casefold()
        for note in schedule.meta.notes
    )
    if timed_out:
        return SolverOutcomePresentation(
            title="No schedule found in time",
            detail=(
                "The search reached its time limit without finding a feasible schedule. "
                "That does not prove the configuration is impossible."
            ),
            notification="Solve reached its time limit",
            diagnostics=diagnostics,
        )

    if raw_status is SolverStatus.INFEASIBLE:
        return SolverOutcomePresentation(
            title="No feasible block schedule",
            detail=(
                "The block-scheduling rules contradict one another. Resolve one of the "
                "conflicts below and solve again."
            ),
            notification="Block schedule is infeasible",
            diagnostics=diagnostics,
        )

    return SolverOutcomePresentation(
        title="Solver did not produce a schedule",
        detail="Review the explanation below before trying Solve again.",
        notification=f"Solver {schedule.meta.status.value}",
        diagnostics=diagnostics,
    )


def _fallback_diagnostics(schedule: Schedule) -> tuple[SolverDiagnostic, ...]:
    messages = list(dict.fromkeys(schedule.meta.validation_errors))
    if not messages:
        messages = list(dict.fromkeys(schedule.meta.notes))
    if not messages:
        messages = [f"The solver returned {schedule.meta.status.value} without a schedule."]
    return tuple(
        SolverDiagnostic(code="solver_outcome", message=message)
        for message in messages
    )
