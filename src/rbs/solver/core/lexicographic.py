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
only on the input, never on machine speed or load. The wall-clock limit is
only a safety net far above what the deterministic budget uses.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

DEFAULT_TIER_WORK_LIMIT = 0.1
"""CP-SAT deterministic time per tier: enough to find, not always prove, the best."""
_SAFETY_TIME_LIMIT_SECONDS = 10.0


def solve_in_tiers(
    model,
    tiers: Sequence[Sequence[Any]],
    hint_variables: Iterable[Any],
    cp_model,
    *,
    work_limit: float = DEFAULT_TIER_WORK_LIMIT,
):
    """Minimize each non-empty tier's sum in order; return the last solver.

    Returns ``None`` when the model has no solution at all. If a later tier
    fails to improve within its time limit, the previous tier's solution is
    kept, since it already satisfies every tier fixed so far.
    """
    hints = list(hint_variables)
    solver = None
    for tier in tiers:
        if not tier:
            continue
        expression = sum(tier)
        model.Minimize(expression)
        attempt = solve_small(model, cp_model, work_limit=work_limit)
        if attempt is None:
            return solver
        solver = attempt
        model.Add(expression <= int(round(attempt.ObjectiveValue())))
        model.ClearHints()
        for variable in hints:
            model.AddHint(variable, attempt.Value(variable))
    if solver is None:
        model.ClearObjective()
        solver = solve_small(model, cp_model, work_limit=work_limit)
    return solver


def solve_small(model, cp_model, *, work_limit: float = DEFAULT_TIER_WORK_LIMIT):
    """Solve a small model reproducibly; return the solver, or ``None`` if unsolved."""
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    solver.parameters.max_deterministic_time = work_limit
    solver.parameters.max_time_in_seconds = _SAFETY_TIME_LIMIT_SECONDS
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    return solver


__all__ = ["DEFAULT_TIER_WORK_LIMIT", "solve_in_tiers", "solve_small"]
