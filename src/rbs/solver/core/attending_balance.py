"""Soft precepting workload costs for weekly placement and annual refinement.

Squared loads make another half-day more expensive for someone already doing
more. Dividing by precepting opportunity gives part-time and partially available
attendings a proportionately smaller share. These costs never constrain coverage.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from math import gcd

from rbs.models.attending import AttendingWorkType
from rbs.models.instance import SolverProblem
from rbs.models.schedule import AssignedAttendingWork
from rbs.solver.attending_availability import AttendingWeekFacts, precepting_allowed
from rbs.solver.core.attending_rules import AttendingWeekModel

_PC = AttendingWorkType.PRECEPTING_CLINIC


@dataclass
class PreceptingBalance:
    coefficients: dict[str, int]
    other_weeks: Counter[str] = field(default_factory=Counter)
    # Achieved coverage, required-rule, staffing, stability, range, and placement
    # costs. Annual refinement may improve balance only within these ceilings.
    quality_limits: tuple[int, ...] | None = None
    hint: tuple[AssignedAttendingWork, ...] = ()


def precepting_opportunities(problem: SolverProblem, facts: AttendingWeekFacts) -> int:
    """Workload share suggested by schedule dates, available slots, and targets."""
    fixed = facts.fixed_count(_PC)
    if not facts.can_precept or not facts.remaining:
        return fixed
    slots = sum(
        any(
            precepting_allowed(problem, clinic, facts.date_of(day), session)
            for clinic in facts.precepting_clinics
        )
        for day, session in facts.open_half_days
    )
    other_minimums = sum(
        max(0, minimum - facts.fixed_count(work_type))
        for work_type, (minimum, _maximum) in facts.fixed_ranges
        if work_type is not _PC and facts.full_week
    )
    capacity = fixed + min(slots, max(0, facts.remaining - other_minimums))
    configured = dict((*facts.flexible_ranges, *facts.fixed_ranges)).get(_PC)
    if configured is not None:
        capacity = min(capacity, max(fixed, configured[1]))
    # Flexible zero targets can still supply required coverage. Keep a small
    # positive share when their available work can actually precept.
    return max(capacity, int(slots > 0 and facts.remaining > other_minimums))


def workload_coefficients(opportunities: dict[str, int]) -> dict[str, int]:
    """Bounded integer approximation to inverse opportunity, without huge LCMs."""
    positive = {key: value for key, value in opportunities.items() if value > 0}
    if not positive:
        return {}
    divisor = gcd(*positive.values())
    weights = {key: value // divisor for key, value in positive.items()}
    scale = max(weights.values()) ** 2
    return {key: max(1, (scale + weight // 2) // weight) for key, weight in weights.items()}


def precepting_counts(work) -> Counter[str]:
    return Counter(item.attending_id for item in work if item.work_type is _PC)


def workload_cost(counts: Counter[str], coefficients: dict[str, int]) -> int:
    return sum(coefficient * counts[key] ** 2 for key, coefficient in coefficients.items())


def could_improve(counts: Counter[str], coefficients: dict[str, int]) -> bool:
    """Skip refinement if even an unrestricted transfer cannot improve cost."""
    if len(coefficients) < 2:
        return False
    adding = min(value * (2 * counts[key] + 1) for key, value in coefficients.items())
    removing = max(
        (value * (2 * counts[key] - 1) for key, value in coefficients.items() if counts[key]),
        default=0,
    )
    return removing > adding


def add_workload_terms(model, handles: list[AttendingWeekModel], balance: PreceptingBalance):
    """Return convex workload costs; fixed and locked precepting count too."""
    if len(balance.coefficients) < 2:
        return []
    terms = []
    for handle in handles:
        facts = handle.facts
        coefficient = balance.coefficients.get(facts.attending_id)
        choices = handle.generated.get(_PC, ())
        if coefficient is None or not choices:
            continue
        fixed = facts.fixed_count(_PC)
        upper = fixed + min(facts.remaining, len(handle.by_half_day))
        count = model.NewIntVar(fixed, upper, f"{facts.attending_id}:precepting_count")
        model.Add(count == fixed + sum(choices))
        prior = balance.other_weeks[facts.attending_id]
        costs = [coefficient * (2 * prior * value + value * value) for value in range(upper + 1)]
        cost = model.NewIntVar(0, costs[-1], f"{facts.attending_id}:precepting_balance")
        model.AddElement(count, costs, cost)
        terms.append((cost, costs[-1]))
    return terms
