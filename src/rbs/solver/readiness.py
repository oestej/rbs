"""Pre-solve configuration checks for an in-progress workspace.

Curricula are built up rather than swapped, so a workspace is editable long
before it is solvable: a training level may still hold unscheduled weeks that
nothing has claimed. Model validation deliberately allows that state — these
checks are what refuse to solve it, in the program's language, rather than
letting the engine fail deep inside block placement.
"""

from __future__ import annotations

from dataclasses import dataclass

from rbs.models.instance import SolverProblem

__all__ = [
    "ReadinessResult",
    "unallocated_weeks_by_level",
    "check_solve_readiness",
]


@dataclass(frozen=True)
class ReadinessResult:
    """Why a workspace cannot be solved yet, if it cannot."""

    errors: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.errors


def unallocated_weeks_by_level(instance: SolverProblem) -> dict[int, int]:
    """Return unscheduled weeks per training level, omitting fully allocated ones."""
    return {
        curriculum.pgy: remaining
        for curriculum in instance.requirements
        if (remaining := instance.unallocated_weeks(curriculum.pgy)) > 0
    }


def check_solve_readiness(instance: SolverProblem) -> ReadinessResult:
    """Report the configuration gaps that would stop this workspace solving.

    Only levels with residents block a solve: a level nobody is enrolled in
    never reaches block placement, so leaving it unbuilt is legitimate.
    """
    staffed = {resident.pgy for resident in instance.residents}
    errors = [
        f"{instance.training_level_label(pgy, compact=True)} has {weeks} "
        f"unscheduled {'week' if weeks == 1 else 'weeks'} to allocate"
        for pgy, weeks in sorted(unallocated_weeks_by_level(instance).items())
        if pgy in staffed
    ]
    return ReadinessResult(errors=tuple(errors))
