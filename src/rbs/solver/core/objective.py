"""Clinic objective orchestration.

Slot-literal construction lives in
:mod:`rbs.solver.core.objective_slots`, weekly entry collection in
:mod:`rbs.solver.core.objective_entries`, and term builders in
:mod:`rbs.solver.core.objective_terms`. This module keeps the
:func:`add_clinic_objective` entry point plus a compatibility re-export
surface so existing imports keep working; new code should import from the
submodules directly.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from rbs.models.enums import RotationKind, Weekday
from rbs.models.schedule import Schedule
from rbs.solver.core.context import ClinicDecision, ClinicModelState, PlanningContext
from rbs.solver.core.objective_entries import (
    _available_week_entries,
    _clinic_kind_occupancy,
    _clinic_occurrences,
    _collect_week_entries,
    _counts_at_primary_site,
    _covering_literals,
    _decision_clinic_entries,
    _deduplicate_entries,
    _ensure_overlay_decisions,
    _fixed_clinic_entries,
    _materialize_week_entries,
    _occurrence_has_clinic,
    _overlay_domain,
    _pinned_site,
    _resident_clinic_entries,
    _rotation_clinic_entries,
)
from rbs.solver.core.objective_slots import (
    _add_occupancy_floor,
    _Conditional,
    _occupancy_floor,
    _slot_literals,
)
from rbs.solver.core.objective_terms import (
    _add_week_objective_terms,
    _append_week_spreads,
    _clinic_block_week_evenness,
    _clinic_kind_pgy_spread,
    _preferred_slot_penalties,
    _primary_site_week_evenness,
    _quality_terms,
    _range_var,
    _schedule_stability_cost,
    _session_pgy_mix,
    _sum_literals,
)

__all__ = [
    "add_clinic_objective",
    "_ClinicObjectiveState",
    "_Conditional",
    "_add_occupancy_floor",
    "_add_week_objective_terms",
    "_append_week_spreads",
    "_available_week_entries",
    "_clinic_block_week_evenness",
    "_clinic_kind_occupancy",
    "_clinic_kind_pgy_spread",
    "_clinic_occurrences",
    "_collect_week_entries",
    "_counts_at_primary_site",
    "_covering_literals",
    "_decision_clinic_entries",
    "_deduplicate_entries",
    "_ensure_overlay_decisions",
    "_finish_clinic_objective",
    "_fixed_clinic_entries",
    "_materialize_week_entries",
    "_occurrence_has_clinic",
    "_occupancy_floor",
    "_overlay_domain",
    "_pinned_site",
    "_preferred_slot_penalties",
    "_primary_site_week_evenness",
    "_quality_terms",
    "_range_var",
    "_resident_clinic_entries",
    "_rotation_clinic_entries",
    "_schedule_stability_cost",
    "_session_pgy_mix",
    "_slot_literals",
    "_sum_literals",
]

@dataclass
class _ClinicObjectiveState:
    in_clinic: dict = field(default_factory=lambda: defaultdict(list))
    attending_variables: list[Any] = field(default_factory=list)
    attending_upper_bound: int = 0
    attending_by_week: dict = field(default_factory=lambda: defaultdict(list))
    pgy_mix_variables: list[Any] = field(default_factory=list)
    clinic_kind_week: dict = field(default_factory=lambda: defaultdict(list))
    within_week: list[Any] = field(default_factory=list)


def add_clinic_objective(
    context: PlanningContext,
    decisions: dict[str, ClinicDecision],
    *,
    reference_schedule: Schedule | None = None,
) -> ClinicModelState:
    """Place clinic sessions and assemble independently calculated objective terms."""
    from rbs.solver.core.kinds import clinic as clinic_kind

    policy = context.instance.clinic_policy
    _ensure_overlay_decisions(context, decisions, policy)
    admin_keys = {
        occurrence.key
        for occurrence in context.occurrences
        if context.rotations[occurrence.rotation_id].kind is RotationKind.CLINIC
    }
    preferred_penalties, preferred_bound = _preferred_slot_penalties(
        decisions,
        excluded_keys=admin_keys,
    )
    occurrences = _clinic_occurrences(context, decisions)
    state = _ClinicObjectiveState()
    pgys = [
        pgy
        for pgy, count in sorted(context.instance.cohort_counts().items())
        if count > 0
    ]

    for week in context.weeks:
        entries = _collect_week_entries(
            context,
            occurrences,
            week,
            decisions,
            clinic_kind,
            state,
        )
        grouped, surviving = _available_week_entries(context, week, entries)
        slot_groups = _materialize_week_entries(context, week, grouped, state)
        _add_occupancy_floor(context.model, surviving, slot_groups[0])
        _add_week_objective_terms(
            context,
            week,
            clinic_kind.week_domain(context.instance, week),
            pgys,
            slot_groups,
            state,
        )

    return _finish_clinic_objective(
        context,
        decisions,
        state,
        preferred_penalties,
        preferred_bound,
        pgys,
        reference_schedule,
    )


def _finish_clinic_objective(
    context: PlanningContext,
    decisions: dict[str, ClinicDecision],
    state: _ClinicObjectiveState,
    preferred_penalties: list[Any],
    preferred_bound: int,
    pgys: list[int],
    reference_schedule: Schedule | None,
) -> ClinicModelState:
    instance = context.instance
    policy = instance.clinic_policy
    n_residents = len(instance.residents)
    kind_spread = _clinic_kind_pgy_spread(
        context.model,
        pgys,
        context.weeks,
        state.clinic_kind_week,
        instance,
    )
    weekly_attending_bound = max(
        policy.attendings_needed(max(n_residents, 1)) * (len(Weekday) * 2 - 1),
        1,
    )
    primary_evenness = _primary_site_week_evenness(
        context.model,
        context.weeks,
        state.attending_by_week,
        weekly_attending_bound,
    )
    clinic_evenness = _clinic_block_week_evenness(
        context.model,
        context.weeks,
        state.clinic_kind_week,
        n_residents,
    )
    stability_cost, comparisons = _schedule_stability_cost(
        context,
        state.in_clinic,
        reference_schedule,
    )
    quality, quality_bound = _quality_terms(
        context,
        state,
        preferred_penalties,
        preferred_bound,
        primary_evenness,
        clinic_evenness,
        kind_spread,
        weekly_attending_bound,
        n_residents,
    )
    has_quality = any(
        (
            state.attending_variables,
            preferred_penalties,
            clinic_evenness,
            primary_evenness,
            kind_spread,
            state.pgy_mix_variables,
            state.within_week,
        )
    )
    has_objective = has_quality or comparisons > 0
    return ClinicModelState(
        decisions=decisions,
        in_clinic=dict(state.in_clinic),
        attending_variables=state.attending_variables,
        has_objective=has_objective,
        stability_comparisons=comparisons,
        stability_cost=stability_cost,
        quality_cost=quality,
        quality_bound=quality_bound,
    )

