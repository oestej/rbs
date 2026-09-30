"""One user-facing report for a failed solve, shared by every surface.

CP-SAT itself returns only a status code (infeasible, unknown, and so on) and
never a reason. The descriptive errors come from the RBS wrappers around that
status: the compile-time configuration check, the focused infeasibility
probes, the reference-lock barriers, and post-solve validation. The schedule
already carries those as ``meta.diagnostics``; this module turns a
non-successful schedule into a single report so the workspace dialog and the
``rbs schedule`` command tell the same story.
"""

from __future__ import annotations

from dataclasses import dataclass

from rbs.models.enums import SolverStatus
from rbs.models.schedule import Schedule, SolverDiagnostic


@dataclass(frozen=True, slots=True)
class SolverFailureReport:
    """User-facing meaning of a non-successful solver result."""

    title: str
    detail: str
    notification: str
    diagnostics: tuple[SolverDiagnostic, ...]


def describe_solver_failure(schedule: Schedule) -> SolverFailureReport | None:
    """Return the user-facing report for a failed solve, if it failed.

    A schedule with a feasible status and no validation errors is a success
    and yields no report. Everything else maps to one of the same outcomes
    the workspace dialog shows: model build errors, search timeouts,
    infeasible block models, invalid post-solve results, and clinic
    placement failures.
    """
    if schedule.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE} and (
        not schedule.meta.validation_errors
    ):
        return None
    raw_status = schedule.meta.solver_status or schedule.meta.status
    diagnostics = tuple(schedule.meta.diagnostics) or _fallback_diagnostics(schedule)
    has_clinic_failure = any(
        diagnostic.code == "clinic_allocation_capacity" for diagnostic in diagnostics
    )
    solved_model = raw_status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}

    if solved_model and schedule.meta.validation_errors:
        if has_clinic_failure:
            return SolverFailureReport(
                title="Block schedule found, but clinic placement failed",
                detail=(
                    "The block model was feasible, but clinic-site assignment exceeded "
                    "configured capacity. The generated schedule was not accepted."
                ),
                notification="Clinic placement failed",
                diagnostics=diagnostics,
            )
        return SolverFailureReport(
            title="Schedule found, but validation failed",
            detail=(
                "The solver found a block schedule, but the completed result violated "
                "one or more schedule rules and was not accepted."
            ),
            notification="Schedule validation failed",
            diagnostics=diagnostics,
        )

    if any(diagnostic.code == "model_build_error" for diagnostic in diagnostics):
        return SolverFailureReport(
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
        return SolverFailureReport(
            title="No schedule found in time",
            detail=(
                "The search reached its time limit without finding a feasible schedule. "
                "That does not prove the configuration is impossible."
            ),
            notification="Solve reached its time limit",
            diagnostics=diagnostics,
        )

    if raw_status is SolverStatus.INFEASIBLE:
        return SolverFailureReport(
            title="No feasible block schedule",
            detail=(
                "The block-scheduling rules contradict one another. Resolve one of the "
                "conflicts below and solve again."
            ),
            notification="Block schedule is infeasible",
            diagnostics=diagnostics,
        )

    return SolverFailureReport(
        title="Solver did not produce a schedule",
        detail="Review the explanation below before trying Solve again.",
        notification=f"Solver {schedule.meta.status.value}",
        diagnostics=diagnostics,
    )


def format_report_text(report: SolverFailureReport) -> str:
    """Render a report as plain text with the same copy the dialog shows."""
    lines = [report.title, report.detail]
    for diagnostic in report.diagnostics:
        lines.append(f"- {diagnostic.message}")
        if diagnostic.suggestions:
            lines.append("  Ways to resolve it:")
            lines.extend(f"  - {suggestion}" for suggestion in diagnostic.suggestions)
    return "\n".join(lines)


def _fallback_diagnostics(schedule: Schedule) -> tuple[SolverDiagnostic, ...]:
    """Explain a failure that arrived with no solver diagnostics.

    Validation errors are descriptive already, so they pass through. Anything
    else falls back to a plain-language message: the raw schedule notes name
    solver internals (engine status words, attempt counts, objective terms)
    that are not actionable, so they are deliberately never echoed here.
    """
    messages = list(dict.fromkeys(schedule.meta.validation_errors))
    if messages:
        return tuple(
            SolverDiagnostic(code="solver_outcome", message=message) for message in messages
        )
    if (schedule.meta.solver_status or schedule.meta.status) is SolverStatus.INFEASIBLE:
        return (
            SolverDiagnostic(
                code="solver_outcome",
                message=(
                    "The block-scheduling rules contradict one another, but no single "
                    "conflict could be isolated."
                ),
                suggestions=[
                    "Review locked blocks, vacation weeks, and rotation maximums, "
                    "then solve again.",
                    "If the solve keeps failing, revert one recent edit at a time "
                    "until it succeeds.",
                ],
            ),
        )
    return (
        SolverDiagnostic(
            code="solver_outcome",
            message=f"The solver returned {schedule.meta.status.value} without a schedule.",
        ),
    )


__all__ = ["SolverFailureReport", "describe_solver_failure", "format_report_text"]
