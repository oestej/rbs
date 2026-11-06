"""Exact capacity for interchangeable sessions with the same sites and weight."""

from collections import defaultdict
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SiteDemand:
    sites: tuple[str, ...]
    points: int
    literals: tuple[Any, ...]


@dataclass(frozen=True)
class SiteSeating:
    demand: SiteDemand
    counts: dict[str, Any]


def solved_seating(plans: list[SiteSeating], solver) -> dict:
    """Keep the search's complete seating as a fallback for final allocation."""
    result = defaultdict(lambda: defaultdict(int))
    for plan in plans:
        # Choice order does not distinguish otherwise interchangeable groups.
        key = (frozenset(plan.demand.sites), plan.demand.points)
        for site, count in plan.counts.items():
            result[key][site] += int(solver.Value(count))
    return {key: dict(counts) for key, counts in result.items()}


def add_site_seating(
    model, demands: list[SiteDemand], seats: dict, upper: dict, *, prefix: str,
) -> list[SiteSeating]:
    """Assign whole residents to allowed clinics, conserving each group's count.

    Integer counts suffice: members of one group have identical site choices
    and capacity weights. A resident's capacity points cannot be split between
    clinics, even when the directory has enough points in total. The returned
    counts let allocation reuse this seating without another search.
    """
    # With unit-weight sessions that are either pinned or unrestricted, the
    # total and singleton bounds are already an exact matching condition.
    # Keep this common case free of additional allocation variables.
    if all(demand.points == 1 and (len(demand.sites) == 1 or set(demand.sites) == set(seats))
           for demand in demands):
        model.Add(sum(sum(demand.literals) for demand in demands) <= sum(seats.values()))
        pinned = defaultdict(list)
        for demand in demands:
            if len(demand.sites) == 1:
                pinned[demand.sites[0]].extend(demand.literals)
        for site, literals in pinned.items():
            model.Add(sum(literals) <= seats.get(site, 0))
        return []
    loads = defaultdict(list)
    plans = []
    for index, demand in enumerate(demands):
        count = sum(demand.literals)
        if len(demand.sites) == 1:
            loads[demand.sites[0]].append(demand.points * count)
            plans.append(SiteSeating(demand, {demand.sites[0]: count}))
            continue
        choices = []
        counts = {}
        for site in demand.sites:
            bound = min(len(demand.literals), upper.get(site, 0) // demand.points)
            if bound <= 0:
                continue
            value = model.NewIntVar(0, bound, f"{prefix}:g{index}:{site}")
            choices.append(value)
            counts[site] = value
            loads[site].append(demand.points * value)
        model.Add(sum(choices) == count)
        plans.append(SiteSeating(demand, counts))
    for site, terms in loads.items():
        model.Add(sum(terms) <= seats.get(site, 0))
    return plans
