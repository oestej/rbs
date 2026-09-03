"""Slot literals and occupancy floors for the clinic objective.

One collapsed Boolean stands for every candidate occurrence of the same
clinic half-day; the occupancy floor states how many of a resident's weekly
sessions must be occupied once a block is placed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class _Conditional:
    """A clinic literal that equals ``value`` whenever ``present`` holds.

    Deferring this product until the per-resident collapse lets one variable
    stand for every candidate occurrence of the same half-day, instead of one
    reified AND per occurrence.

    ``pick``/``domain_size``/``negated`` describe the decision ``value`` is
    drawn from, which is what lets :func:`_occupancy_floor` state how many of a
    resident's sessions must be occupied in a week.
    """

    present: Any
    value: Any
    pick: int
    domain_size: int
    negated: bool


def _occupancy_floor(surviving: int, pick: int, domain_size: int, negated: bool) -> int:
    """Least clinic sessions an occurrence must occupy this week.

    ``surviving`` counts the half-days that survived this week's academic,
    closure, and day-off filtering. A decision selects exactly ``pick`` of its
    ``domain_size`` slots, so at worst every excluded slot absorbed one
    selection (``negated`` decisions - the dedicated Clinic block's Admin
    sessions - instead subtract the picks from what survived).
    """
    if negated:
        return max(0, surviving - pick)
    return max(0, pick - (domain_size - surviving))


def _add_occupancy_floor(
    model,
    surviving: dict[str, list[tuple]],
    slots_by_resident: dict[str, list[Any]],
) -> None:
    """Tie a resident's weekly clinic sessions to the block they are placed on.

    For each resident: ``sum(sessions this week) >= sum(floor_o * present_o)``.
    Exactly one occurrence is present per resident-week, so the right-hand side
    reduces to the floor of whichever block the solver picks.

    A tighter per-occurrence form (sessions >= the chosen slots that survived,
    relaxed when the occurrence is not placed) was measured and rejected: it is
    exact where the floor collapses to zero, but the extra ~11.7k rows cost more
    search than the tighter relaxation returned (bound 88k -> 50k over 60s).
    """
    floors: dict[str, list[tuple[int, Any]]] = defaultdict(list)
    for members in surviving.values():
        occurrence = members[0][0]
        products = [literal for _occ, literal in members if isinstance(literal, _Conditional)]
        plain = len(members) - len(products)
        if products:
            product = products[0]
            floor = plain + _occupancy_floor(
                len(products), product.pick, product.domain_size, product.negated
            )
            present = product.present
        else:
            # Every surviving half-day is unconditional, but only literals that
            # are the presence variable itself are guaranteed when placed.
            guaranteed = [literal for _occ, literal in members]
            if len({id(literal) for literal in guaranteed}) != 1:
                continue
            floor, present = plain, guaranteed[0]
        if floor > 0:
            floors[occurrence.resident_id].append((floor, present))

    for resident_id, terms in floors.items():
        literals = slots_by_resident.get(resident_id)
        if not literals:
            continue
        model.Add(sum(literals) >= sum(floor * present for floor, present in terms))


def _slot_literals(model, name: str, members: list[tuple]) -> list[Any]:
    """Return the literals representing one resident's clinic half-day.

    Groups made only of plain literals are passed through untouched — they cost
    no variables today. A group holding any deferred product collapses to a
    single Boolean, fully determined because a resident occupies exactly one
    rotation per week.
    """
    plain = [member[3] for member in members if not isinstance(member[3], _Conditional)]
    conditional = [member[3] for member in members if isinstance(member[3], _Conditional)]
    if not conditional:
        return plain

    slot = model.NewBoolVar(name)
    caps = list(plain)
    for literal in plain:
        model.Add(slot >= literal)
    for product in conditional:
        model.Add(slot == product.value).OnlyEnforceIf(product.present)
        caps.append(product.present)
    # No contributing occurrence placed here -> the session is empty.
    model.Add(slot <= sum(caps))
    return [slot]

