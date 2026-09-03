"""Objective-term builders for the clinic objective.

Weekly load, evenness, PGY-mix, preferred-slot, and stability terms, plus the
small model helpers (sums, ranges) they share.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from rbs.models.enums import Weekday
from rbs.models.schedule import Schedule
from rbs.solver.core.context import ClinicDecision, PlanningContext

if TYPE_CHECKING:
    from rbs.solver.core.objective import _ClinicObjectiveState
from rbs.solver.reference import (
    reference_clinic_half_days,
    reference_locked_clinic_states,
    reference_rotation_grid,
)


def _add_week_objective_terms(
    context: PlanningContext,
    week: int,
    half_day_slots: list,
    pgys: list[int],
    slot_groups: tuple[dict, dict, dict, dict],
    state: _ClinicObjectiveState,
) -> None:
    _slots_by_resident, present_by_slot, primary_by_slot, present_by_pgy = slot_groups
    model = context.model
    policy = context.instance.clinic_policy
    ratio = policy.site(policy.primary_site_id).residents_per_attending
    n_residents = len(context.instance.residents)
    week_slot_load = []
    week_day_parts: dict[Any, list[Any]] = defaultdict(list)

    for slot in half_day_slots:
        weekday, session = slot.weekday, slot.session
        primary = primary_by_slot.get((weekday, session), [])
        primary_count = _sum_literals(
            model,
            f"primary_n:w{week}:{weekday}:{session}",
            primary,
            len(primary),
        )
        if primary:
            cap = policy.attendings_needed(len(primary))
            attendings = model.NewIntVar(
                0,
                cap,
                f"att:w{week}:{weekday}:{session}:primary",
            )
            model.Add(ratio * attendings >= primary_count)
            state.attending_variables.append(attendings)
            state.attending_upper_bound += cap
            state.attending_by_week[week].append(attendings)
        total = _sum_literals(
            model,
            f"slot_n:w{week}:{weekday}:{session}",
            present_by_slot.get((weekday, session), []),
            n_residents,
        )
        week_slot_load.append(total)
        week_day_parts[weekday].append(total)
        state.pgy_mix_variables.extend(
            _session_pgy_mix(
                model,
                week,
                weekday,
                session,
                pgys,
                present_by_pgy,
                present_by_slot,
            )
        )
    _append_week_spreads(model, week, week_slot_load, week_day_parts, n_residents, state)


def _append_week_spreads(
    model,
    week: int,
    week_slot_load: list,
    week_day_parts: dict,
    n_residents: int,
    state: _ClinicObjectiveState,
) -> None:
    slot_range = _range_var(
        model,
        f"weekslots:w{week}",
        week_slot_load,
        n_residents,
    )
    if slot_range is not None:
        state.within_week.append(slot_range)
    day_totals = [
        _sum_literals(
            model,
            f"day_n:w{week}:{day}",
            week_day_parts[day],
            n_residents * 2,
        )
        for day in Weekday
        if week_day_parts.get(day)
    ]
    day_range = _range_var(
        model,
        f"weekdays:w{week}",
        day_totals,
        n_residents * 2,
    )
    if day_range is not None:
        state.within_week.append(day_range)


def _quality_terms(
    context: PlanningContext,
    state: _ClinicObjectiveState,
    preferred_penalties: list[Any],
    preferred_bound: int,
    primary_evenness: list[Any],
    clinic_evenness: list[Any],
    kind_spread: list[Any],
    weekly_attending_bound: int,
    n_residents: int,
) -> tuple[Any, int]:
    weights = context.options.weights
    quality = (
        weights.attending_sessions * sum(state.attending_variables)
        + weights.preferred_clinic_slots * sum(preferred_penalties)
        + weights.primary_site_week_evenness * sum(primary_evenness)
        + weights.clinic_block_week_evenness * sum(clinic_evenness)
        + weights.within_week_evenness * sum(state.within_week)
        + weights.clinic_kind_pgy_spread * sum(kind_spread)
        + weights.session_pgy_mix * sum(state.pgy_mix_variables)
    )
    upper_bound = (
        weights.attending_sessions * state.attending_upper_bound
        + weights.preferred_clinic_slots * preferred_bound
        + weights.primary_site_week_evenness
        * weekly_attending_bound
        * len(primary_evenness)
        + weights.clinic_block_week_evenness
        * max(n_residents, 1)
        * len(clinic_evenness)
        + weights.within_week_evenness
        * max(n_residents * 2, 1)
        * len(state.within_week)
        + weights.clinic_kind_pgy_spread
        * max(n_residents, 1)
        * len(kind_spread)
        + weights.session_pgy_mix
        * max(n_residents, 1)
        * len(state.pgy_mix_variables)
    )
    return quality, upper_bound


def _preferred_slot_penalties(
    decisions: dict[str, ClinicDecision],
    *,
    excluded_keys: set[str] | None = None,
) -> tuple[list[Any], int]:
    """Return selected fallback literals for decisions with preferred slots.

    Every decision already selects exactly ``pick`` allowed slots. Minimizing
    its selected non-preferred literals therefore maximizes preferred choices
    without making any preferred slot a hard requirement.
    """
    penalties = []
    upper_bound = 0
    excluded_keys = excluded_keys or set()
    for key, decision in decisions.items():
        if key in excluded_keys:
            continue
        if not any(slot.preferred for slot in decision.domain):
            continue
        fallback_literals = [
            selected
            for slot, selected in zip(
                decision.domain,
                decision.selected,
                strict=True,
            )
            if not slot.preferred
        ]
        penalties.extend(fallback_literals)
        upper_bound += min(decision.pick, len(fallback_literals))
    return penalties, upper_bound


def _schedule_stability_cost(
    context: PlanningContext,
    in_clinic: dict,
    reference_schedule: Schedule | None,
):
    """Build a linear count of changed resident-weeks and clinic half-days."""
    reference_grid = reference_rotation_grid(context.instance, reference_schedule)
    if not reference_grid:
        return 0, 0

    cost = 0
    comparisons = 0
    for (resident_id, week), (rotation_id, elective) in reference_grid.items():
        matching = [
            context.placements[occurrence.key, start]
            for occurrence in context.by_resident.get(resident_id, [])
            if occurrence.rotation_id == rotation_id
            and occurrence.elective == elective
            for start in context.starts[occurrence.key]
            if start <= week < start + occurrence.duration_weeks
        ]
        cost += 1 - sum(matching)
        comparisons += 1

    reference_clinic = reference_clinic_half_days(
        context.instance,
        reference_schedule,
    )
    locked_clinic = reference_locked_clinic_states(
        context.instance,
        reference_schedule,
    )
    for (resident_id, week), entries in in_clinic.items():
        if (resident_id, week) not in reference_grid:
            continue
        by_half_day: dict[tuple, list[Any]] = defaultdict(list)
        for _keys, weekday, session, literal in entries:
            by_half_day[weekday, session].append(literal)
        for (weekday, session), literals in by_half_day.items():
            occupied = sum(literals)
            locked_state = locked_clinic.get(
                (resident_id, week, weekday, session)
            )
            if locked_state is not None:
                context.model.Add(occupied == int(locked_state))
            if (resident_id, week, weekday, session) in reference_clinic:
                cost += 1 - occupied
            else:
                cost += occupied
            comparisons += 1
    return cost, comparisons


def _sum_literals(model, name: str, literals: list, cap: int):
    variable = model.NewIntVar(0, max(cap, 0), name)
    model.Add(variable == (sum(literals) if literals else 0))
    return variable


def _range_var(model, name: str, counts: list, bound: int):
    if len(counts) < 2 or bound <= 0:
        return None
    maximum = model.NewIntVar(0, bound, f"{name}:mx")
    minimum = model.NewIntVar(0, bound, f"{name}:mn")
    for count in counts:
        model.Add(maximum >= count)
        model.Add(minimum <= count)
    spread = model.NewIntVar(0, bound, f"{name}:rng")
    model.Add(spread == maximum - minimum)
    return spread


def _session_pgy_mix(
    model,
    week,
    weekday,
    session,
    pgys: list[int],
    present_by_pgy,
    present_by_slot,
) -> list:
    people = present_by_slot.get((weekday, session), [])
    if len(people) < 2 or len(pgys) < 2:
        return []
    bound = len(people)
    counts = [
        _sum_literals(
            model,
            f"pgy{pgy}:w{week}:{weekday}:{session}",
            present_by_pgy.get((weekday, session, pgy), []),
            bound,
        )
        for pgy in pgys
    ]
    spread = _range_var(model, f"pgymix:w{week}:{weekday}:{session}", counts, bound)
    return [spread] if spread is not None else []


def _primary_site_week_evenness(model, weeks, attending_by_week, bound: int) -> list:
    totals = []
    for week in weeks:
        parts = attending_by_week.get(week, [])
        total = model.NewIntVar(0, bound, f"primary_att_week:{week}")
        model.Add(total == (sum(parts) if parts else 0))
        totals.append(total)
    spread = _range_var(model, "primary_att_weeks", totals, bound)
    return [spread] if spread is not None else []


def _clinic_block_week_evenness(model, weeks, clinic_kind_week, n_residents: int) -> list:
    """Spread of how many residents sit on a dedicated Clinic block each week.

    Site agnostic and summed across every PGY, which is what keeps the calendar
    flat: ``_clinic_kind_pgy_spread`` only levels each cohort on its own, so two
    cohorts can each look even while their peaks land in the same week.
    """
    bound = max(n_residents, 1)
    totals = []
    for week in weeks:
        parts = [
            literal
            for (_pgy, other_week), literals in clinic_kind_week.items()
            if other_week == week
            for literal in literals
        ]
        total = model.NewIntVar(0, bound, f"ckweek:w{week}")
        model.Add(total == (sum(parts) if parts else 0))
        totals.append(total)
    spread = _range_var(model, "ckweek_year", totals, bound)
    return [spread] if spread is not None else []


def _clinic_kind_pgy_spread(model, pgys, weeks, clinic_kind_week, instance) -> list:
    spread = []
    cohort = instance.cohort_counts()
    for pgy in pgys:
        bound = cohort.get(pgy, 0)
        counts = [
            _sum_literals(
                model,
                f"ck:pgy{pgy}:w{week}",
                clinic_kind_week.get((pgy, week), []),
                bound,
            )
            for week in weeks
        ]
        pgy_spread = _range_var(model, f"ckspread:pgy{pgy}", counts, bound)
        if pgy_spread is not None:
            spread.append(pgy_spread)
    return spread

