"""Focused explanations for mathematical infeasibility.

The full CP-SAT model has shared staffing, clinic, sequencing, and placement
rules, so there is no single generic explanation for every infeasible model.
These checks isolate smaller necessary subproblems. When one of those is itself
infeasible, the resulting explanation is conclusive and can name a useful edit.
"""

from __future__ import annotations

from collections import defaultdict

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
    diagnostics.extend(_locked_capacity_conflicts(problem))
    diagnostics.extend(_locked_elective_repeats(problem))
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


def _locked_capacity_conflicts(problem: SolverProblem) -> list[SolverDiagnostic]:
    """Name weeks where locked placements alone exceed a rotation maximum.

    Locks are hard equality constraints and maximums are hard upper bounds,
    so distinct locked residents above a maximum are conclusive: no schedule
    can satisfy both. Identical duplicate locks pin the same resident twice
    and count once.
    """
    by_week_rotation: dict[tuple[int, str], set[str]] = defaultdict(set)
    for lock in problem.locks:
        for week in lock.weeks:
            by_week_rotation[week, lock.rotation_id].add(lock.resident_id)
    residents = problem.residents_by_id
    diagnostics: list[SolverDiagnostic] = []
    # Group weeks that lock the same residents so one message covers one
    # conflict instead of one message per week.
    overall: dict[tuple[str, frozenset[str]], set[int]] = defaultdict(set)
    by_level: dict[tuple[str, int, frozenset[str]], set[int]] = defaultdict(set)
    for (week, rotation_id), resident_ids in by_week_rotation.items():
        overall[rotation_id, frozenset(resident_ids)].add(week)
        seen_pgys: set[int] = set()
        for resident_id in resident_ids:
            resident = residents.get(resident_id)
            if resident is None or resident.pgy in seen_pgys:
                continue
            seen_pgys.add(resident.pgy)
            by_level[rotation_id, resident.pgy, frozenset(
                peer
                for peer in resident_ids
                if residents.get(peer) is not None
                and residents[peer].pgy == resident.pgy
            )].add(week)
    reported: set[tuple[str, frozenset[str], tuple[int, ...]]] = set()
    for (rotation_id, locked), weeks in sorted(
        overall.items(), key=lambda item: (item[0][0], sorted(item[0][1]))
    ):
        rotation = problem.rotations_by_id.get(rotation_id)
        maximum = rotation.capacity.max_concurrent if rotation is not None else None
        if rotation is None or maximum is None or len(locked) <= maximum:
            continue
        names = sorted(residents[resident_id].name for resident_id in sorted(locked))
        reported.add((rotation_id, locked, tuple(sorted(weeks))))
        diagnostics.append(
            SolverDiagnostic(
                code="locked_capacity_conflict",
                message=(
                    f"{rotation.name} {_weeks_label(sorted(weeks))}: "
                    f"{len(names)} residents are locked there "
                    f"({', '.join(names)}), but at most {maximum} "
                    f"{'resident' if maximum == 1 else 'residents'} can take "
                    f"{rotation.name} at a time."
                ),
                resident_ids=sorted(locked),
                weeks=sorted(weeks),
                suggestions=(
                    "Move or delete one of the locked blocks on those weeks.",
                    f"Raise the maximum number of residents who can take "
                    f"{rotation.name} at once.",
                ),
            )
        )
    for (rotation_id, pgy, locked), weeks in sorted(
        by_level.items(), key=lambda item: (item[0][0], item[0][1], sorted(item[0][2]))
    ):
        rotation = problem.rotations_by_id.get(rotation_id)
        rule = rotation.pgy_rule(pgy) if rotation is not None else None
        maximum = rule.max_concurrent if rule is not None else None
        if (
            rotation is None
            or maximum is None
            or len(locked) <= maximum
            # The overall conflict above already names this exact group.
            or (rotation_id, locked, tuple(sorted(weeks))) in reported
        ):
            continue
        level = problem.training_level_label(pgy, compact=True)
        names = sorted(residents[resident_id].name for resident_id in sorted(locked))
        diagnostics.append(
            SolverDiagnostic(
                code="locked_capacity_conflict",
                message=(
                    f"{rotation.name} {_weeks_label(sorted(weeks))}: "
                    f"{len(names)} {level} residents are locked there "
                    f"({', '.join(names)}), but at most {maximum} {level} "
                    f"{'resident' if maximum == 1 else 'residents'} can take "
                    f"{rotation.name} at a time."
                ),
                resident_ids=sorted(locked),
                weeks=sorted(weeks),
                suggestions=(
                    "Move or delete one of the locked blocks on those weeks.",
                    f"Raise the {level} maximum for {rotation.name}.",
                ),
            )
        )
    return diagnostics


def _locked_elective_repeats(problem: SolverProblem) -> list[SolverDiagnostic]:
    """Name residents locked to the same non-repeatable elective twice.

    A non-repeatable service allows one elective block per resident, and two
    locks on disjoint weeks need two separate blocks, so the pair is
    conclusive. Identical duplicate locks describe a single block and are
    ignored, as are elective-fallback placements, which the repeat limit
    does not count.
    """
    nonrepeatable = {
        option.rotation_id
        for option in problem.electives.rotation_options
        if not option.repeatable
    }
    if not nonrepeatable:
        return []
    by_resident_rotation: dict[tuple[str, str], list[tuple[int, ...]]] = defaultdict(list)
    for lock in problem.locks:
        if not lock.elective or lock.rotation_id not in nonrepeatable:
            continue
        resident = problem.residents_by_id.get(lock.resident_id)
        if resident is None or problem.is_elective_fallback_rotation(
            lock.rotation_id, resident.pgy
        ):
            continue
        weeks = tuple(lock.weeks)
        if weeks not in by_resident_rotation[lock.resident_id, lock.rotation_id]:
            by_resident_rotation[lock.resident_id, lock.rotation_id].append(weeks)
    diagnostics: list[SolverDiagnostic] = []
    for (resident_id, rotation_id), ranges in sorted(by_resident_rotation.items()):
        if len(ranges) < 2:
            continue
        ordered = sorted(ranges)
        pair = next(
            (
                (first, second)
                for first, second in zip(ordered, ordered[1:], strict=False)
                if first[-1] + 1 < second[0]
            ),
            None,
        )
        if pair is None:
            continue
        resident = problem.residents_by_id[resident_id]
        rotation = problem.rotations_by_id[rotation_id]
        first, second = pair
        diagnostics.append(
            SolverDiagnostic(
                code="locked_elective_repeat",
                message=(
                    f"{resident.name} is locked to {rotation.name} elective blocks in "
                    f"{_weeks_label(list(first))} and {_weeks_label(list(second))}, "
                    f"but {rotation.name} may be taken only once as an elective."
                ),
                resident_ids=[resident_id],
                weeks=sorted({week for weeks in ranges for week in weeks}),
                suggestions=(
                    "Remove one of the locked elective blocks or change it to "
                    "another rotation.",
                    "Use a repeatable elective service for the second block.",
                ),
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
