"""Focused explanations for mathematical infeasibility.

The full CP-SAT model has shared staffing, clinic, sequencing, and placement
rules, so there is no single generic explanation for every infeasible model.
These checks isolate smaller necessary subproblems. When one of those is itself
infeasible, the resulting explanation is conclusive and can name a useful edit.
"""

from __future__ import annotations

from rbs.models.instance import SolverConfig, SolverProblem
from rbs.models.resident import Resident
from rbs.models.schedule import SolverDiagnostic
from rbs.models.special import SpecialRotation, SpecialRotationKind
from rbs.solver.core import constraints
from rbs.solver.core.context import ModelBuildError, PlanningContext


def explain_infeasibility(
    problem: SolverProblem,
    options: SolverConfig,
) -> list[SolverDiagnostic]:
    """Return conclusive explanations for isolated infeasible subproblems."""
    diagnostics: list[SolverDiagnostic] = []
    for resident in problem.residents:
        feasible = _resident_curriculum_can_cover_year(problem, options, resident)
        if feasible is not False:
            continue

        vacation_weeks = sorted(problem.resident_scheduling_vacation_weeks(resident.id))
        without_vacations = _resident_curriculum_can_cover_year(
            problem,
            options,
            resident,
            ignore_vacations=True,
        )
        if vacation_weeks and without_vacations is True:
            diagnostics.append(
                _vacation_coverage_diagnostic(problem, resident, vacation_weeks)
            )
            continue

        diagnostics.append(
            SolverDiagnostic(
                code="resident_curriculum_coverage",
                message=(
                    f"{resident.name} cannot tile all "
                    f"{problem.calendar.weeks} academic weeks with the configured "
                    "curriculum block shapes and legal start weeks."
                ),
                resident_ids=[resident.id],
                suggestions=[
                    "Review this resident's fixed blocks and rotation locks.",
                    "Review earliest-start rules and required block lengths for "
                    f"{problem.training_level_label(resident.pgy, compact=True)}.",
                ],
            )
        )
    return diagnostics


def _resident_curriculum_can_cover_year(
    problem: SolverProblem,
    options: SolverConfig,
    resident: Resident,
    *,
    ignore_vacations: bool = False,
) -> bool | None:
    """Whether one resident's required blocks can exactly cover the calendar.

    This deliberately excludes shared capacity and clinic constraints. A false
    result is therefore a necessary contradiction belonging to this resident,
    while a true result makes no claim about the complete shared model.
    """
    try:
        from ortools.sat.python import cp_model
    except ImportError:  # pragma: no cover - the CP-SAT engine already requires it
        return None

    diagnostic_resident = (
        resident.model_copy(update={"vacation_weeks": []})
        if ignore_vacations
        else resident
    )
    relevant_specials = [
        special
        for special in problem.special_rotations
        if resident.id in special.resident_ids
        and not (ignore_vacations and special.kind is SpecialRotationKind.CONFERENCE)
    ]
    resident_problem = problem.model_copy(
        update={
            "residents": [diagnostic_resident],
            "locks": [lock for lock in problem.locks if lock.resident_id == resident.id],
            "manual_clinic_blocks": [
                block
                for block in problem.manual_clinic_blocks
                if block.resident_id == resident.id
            ],
            "resident_rotation_overrides": [
                override
                for override in problem.resident_rotation_overrides
                if override.resident_id == resident.id
            ],
            "special_rotations": relevant_specials,
        }
    )
    try:
        context = PlanningContext.compile(resident_problem, options, cp_model)
        constraints._place_groups(context)
        constraints._cover_each_week(context)
    except (ModelBuildError, ValueError):
        return False

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 2.0
    solver.parameters.num_search_workers = 1
    status = solver.Solve(context.model)
    if status == cp_model.INFEASIBLE:
        return False
    if status in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        return True
    return None


def _vacation_coverage_diagnostic(
    problem: SolverProblem,
    resident: Resident,
    vacation_weeks: list[int],
) -> SolverDiagnostic:
    conferences = list(
        problem.special_rotations_for_resident(
            resident.id,
            kind=SpecialRotationKind.CONFERENCE,
        )
    )
    conference_details = [
        f"{special.name} adds {_weeks_label(_special_weeks(problem, special))}"
        for special in conferences
    ]
    detail = f" {'; '.join(conference_details)}." if conference_details else ""
    suggestions: list[str] = []
    if conferences:
        names = ", ".join(special.name for special in conferences)
        suggestions.extend(
            [
                f"Change the dates or assigned residents for {names}.",
                "Shorten or split a conference that touches more than one academic week.",
            ]
        )
    suggestions.extend(
        [
            f"Move one of {resident.name}'s vacation weeks.",
            "Allow more vacation overlap on a compatible rotation block.",
        ]
    )
    return SolverDiagnostic(
        code="resident_vacation_coverage",
        message=(
            f"{resident.name} cannot fit all {problem.calendar.weeks} "
            f"curriculum weeks around vacation-like {_weeks_label(vacation_weeks)} "
            f"under the configured per-block vacation rules.{detail}"
        ),
        resident_ids=[resident.id],
        special_rotation_ids=[special.id for special in conferences],
        weeks=vacation_weeks,
        suggestions=suggestions,
    )


def _special_weeks(problem: SolverProblem, special: SpecialRotation) -> list[int]:
    first_day = problem.calendar.first_week_start
    return sorted(
        {
            (calendar_day - first_day).days // 7 + 1
            for calendar_day in special.dates()
            if calendar_day >= first_day
        }
    )


def _weeks_label(weeks: list[int]) -> str:
    if not weeks:
        return "no academic weeks"
    ranges: list[tuple[int, int]] = []
    start = previous = weeks[0]
    for week in weeks[1:]:
        if week == previous + 1:
            previous = week
            continue
        ranges.append((start, previous))
        start = previous = week
    ranges.append((start, previous))
    labels = [str(start) if start == end else f"{start}–{end}" for start, end in ranges]
    if len(labels) == 1:
        joined = labels[0]
    elif len(labels) == 2:
        joined = " and ".join(labels)
    else:
        joined = ", ".join(labels[:-1]) + f", and {labels[-1]}"
    return f"week{'s' if len(weeks) != 1 else ''} {joined}"


__all__ = ["explain_infeasibility"]
