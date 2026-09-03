"""FMED kind: residency-managed inpatient teaching service.

Clinic sessions come entirely from the configured rule. Overall and training-level
limits bound how many residents assigned concurrently may use the same half-day.
"""

from collections import defaultdict

from rbs.models.enums import RotationKind
from rbs.models.instance import SolverProblem
from rbs.models.rotation import Rotation
from rbs.models.schedule import AssignedClinic
from rbs.solver.core.context import ClinicDecision, PlanningContext, new_clinic_decision
from rbs.solver.planning import covers

NOTES = [
    "fmed kind: configured clinic slots exclude the academic half day",
]


def _fmed_rotations(instance: SolverProblem) -> list[Rotation]:
    return [rotation for rotation in instance.rotations if rotation.kind is RotationKind.FMED]


def unique_clinic(context: PlanningContext) -> dict[str, ClinicDecision]:
    """Create FMED clinic choices and enforce configured half-day concurrency caps.

    The historical function name is retained for callers that predate configurable
    limits, when the only supported behavior was an overall cap of one.
    """
    chosen: dict[str, ClinicDecision] = {}
    for rotation in _fmed_rotations(context.instance):
        if rotation.clinic_hours_disabled:
            continue
        rule = rotation.clinic
        if rule is None:
            continue
        domain = [
            slot
            for slot in rule.expanded_slots()
            if not context.instance.clinic_policy.is_academic(slot)
        ]
        if not domain:
            continue
        occs = context.by_rotation.get(rotation.id, [])
        domain_index = {
            (slot.weekday, slot.session): index for index, slot in enumerate(domain)
        }
        clinic_times = set(domain_index)
        for occ in occs:
            clinic_times.update(
                (half_day.weekday, half_day.session)
                for half_day in context.residents[occ.resident_id].clinic_half_days
            )
        decisions = {}
        for occ in occs:
            decision = new_clinic_decision(
                context.model,
                f"clinic:{occ.key}",
                domain,
                pick=rule.half_days_per_week,
            )
            decisions[occ.key] = decision
            chosen[occ.key] = decision
        if rule.max_concurrent is None and not rule.max_concurrent_by_pgy:
            continue
        for week in context.weeks:
            present_by_occ = {}
            for occ in occs:
                covering = [
                    context.placements[occ.key, start]
                    for start in context.starts[occ.key]
                    if covers(start, occ.duration_weeks, week)
                ]
                if not covering:
                    continue
                present = context.model.NewBoolVar(f"clinic:{occ.key}:w{week}")
                context.model.Add(present == sum(covering))
                present_by_occ[occ.key] = present
            for weekday, session in sorted(clinic_times):
                concurrent = []
                concurrent_by_pgy = defaultdict(list)
                for occ in occs:
                    present = present_by_occ.get(occ.key)
                    if present is None:
                        continue
                    resident = context.residents[occ.resident_id]
                    if (
                        week in resident.vacation_weeks
                        or context.instance.resident_clinic_is_blocked(
                            resident.id,
                            week,
                            weekday,
                            session,
                        )
                        or context.instance.is_academic_half_day(
                            week,
                            weekday,
                            session,
                        )
                    ):
                        continue
                    fixed = any(
                        half_day.weekday is weekday and half_day.session is session
                        for half_day in resident.clinic_half_days
                    )
                    if fixed:
                        in_clinic = present
                    else:
                        index = domain_index.get((weekday, session))
                        if index is None:
                            continue
                        in_clinic = context.model.NewBoolVar(
                            f"clinic:{occ.key}:w{week}:on{index}"
                        )
                        context.model.Add(in_clinic <= present)
                        selected = decisions[occ.key].selected[index]
                        context.model.Add(in_clinic <= selected)
                        context.model.Add(in_clinic >= present + selected - 1)
                    concurrent.append(in_clinic)
                    concurrent_by_pgy[occ.pgy].append(in_clinic)
                if rule.max_concurrent is not None and concurrent:
                    context.model.Add(sum(concurrent) <= rule.max_concurrent)
                for pgy, maximum in rule.max_concurrent_by_pgy.items():
                    pgy_concurrent = concurrent_by_pgy.get(pgy, [])
                    if pgy_concurrent:
                        context.model.Add(sum(pgy_concurrent) <= maximum)
    return chosen


def clinic_slots(rotation: Rotation, solved: list[AssignedClinic] | None) -> list[AssignedClinic]:
    if solved:
        return solved
    return []
