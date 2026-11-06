"""Tier-by-tier optimization for the small models around the block solve.

One weighted objective would need coefficients spanning many orders of
magnitude, which makes even a small model slow to prove optimal. Minimizing
one tier at a time, holding every more important tier at its achieved value,
keeps each objective small, and the previous solution warm-starts the next
tier.

These models are small but can be slow to *prove* optimal: identically
configured attendings are interchangeable, and CP-SAT typically finds the
best schedule almost at once and then spends its time closing the bound.
Each tier therefore gets a deterministic work budget rather than a
wall-clock one. With a single worker and a fixed seed the result depends
only on the input unless the caller's wall-clock deadline interrupts the
search. Without a deadline, the wall-clock limit is only a safety net.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from rbs.solver.budget import remaining

DEFAULT_TIER_WORK_LIMIT = 0.1
"""CP-SAT deterministic time per tier: enough to find, not always prove, the best."""
FIRST_SOLUTION_BUDGET_MULTIPLIERS = (1, 10, 100)
"""Budgets, as multiples of the tier budget, tried in turn for a first solution."""
_SAFETY_TIME_LIMIT_SECONDS = 10.0


@dataclass(frozen=True)
class TierResult:
    """Cost and bound when this tier finished, before later improvements."""
    index: int
    value: float
    bound: float
    proven: bool


@dataclass(frozen=True)
class PhaseResult:
    """Internal result: a retained incumbent and why the latest search stopped.

    FEASIBLE tiers are bounded improvements, not proofs of lexicographic
    optimality. Each later tier preserves the costs already achieved.
    """
    status: Literal["optimal", "feasible", "infeasible", "model_invalid", "unknown", "deadline"]
    solver: Any | None = None
    tiers: tuple[TierResult, ...] = ()

    @property
    def completed_tiers(self) -> int:
        return len(self.tiers)

    def checked_solver(self):
        if self.status == "model_invalid":
            raise RuntimeError("invalid internal optimization model")
        return self.solver


def solve_in_tiers(
    model, tiers: Sequence[Sequence[Any]], hint_variables: Iterable[Any], cp_model,
    *, work_limit: float = DEFAULT_TIER_WORK_LIMIT, deadline: float | None = None,
) -> PhaseResult:
    """Optimize tiers with bounded effort, retaining the incumbent on timeout."""
    hints = list(hint_variables)
    incumbent = None
    completed = []
    status = "unknown"
    for index, tier in enumerate(tiers):
        if not tier:
            continue
        expression = sum(tier)
        model.Minimize(expression)
        result = (solve_small(model, cp_model, work_limit=work_limit, deadline=deadline)
                  if incumbent is not None else
                  _first_solution(model, cp_model, work_limit, deadline=deadline))
        status = result.status
        attempt = result.solver
        if attempt is None:
            return PhaseResult(status, incumbent, tuple(completed))
        incumbent = attempt
        completed.append(TierResult(index, attempt.ObjectiveValue(),
                                    attempt.BestObjectiveBound(), status == "optimal"))
        model.Add(expression <= int(round(attempt.ObjectiveValue())))
        model.ClearHints()
        for variable in hints:
            model.AddHint(variable, attempt.Value(variable))
    if incumbent is None:
        model.ClearObjective()
        return _first_solution(model, cp_model, work_limit, deadline=deadline)
    return PhaseResult(status, incumbent, tuple(completed))


def _first_solution(model, cp_model, work_limit: float, *, deadline: float | None = None):
    result = PhaseResult("unknown")
    for multiplier in FIRST_SOLUTION_BUDGET_MULTIPLIERS:
        result = _solve(model, cp_model, work_limit * multiplier, deadline=deadline)
        if (result.solver is not None
                or result.status in {"infeasible", "model_invalid", "deadline"}):
            return result
    return result


def solve_small(model, cp_model, *, work_limit: float = DEFAULT_TIER_WORK_LIMIT,
                deadline: float | None = None) -> PhaseResult:
    return _solve(model, cp_model, work_limit, deadline=deadline)


def _solve(model, cp_model, work_limit: float, *, deadline: float | None = None):
    seconds = remaining(deadline, cap=_SAFETY_TIME_LIMIT_SECONDS)
    if seconds <= 0:
        return PhaseResult("deadline")
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    solver.parameters.max_deterministic_time = work_limit
    solver.parameters.max_time_in_seconds = seconds
    status = solver.Solve(model)
    name = {cp_model.OPTIMAL: "optimal", cp_model.FEASIBLE: "feasible",
            cp_model.INFEASIBLE: "infeasible", cp_model.MODEL_INVALID: "model_invalid",
            cp_model.UNKNOWN: "unknown"}[status]
    return PhaseResult(name, solver if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None)
