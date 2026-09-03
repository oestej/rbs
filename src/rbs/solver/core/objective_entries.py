"""Weekly clinic entry collection for the clinic objective.

Entries describe, per resident-week-half-day, which block placement would
supply clinic coverage. Later stages group, materialize, and weigh them.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from rbs.models.clinic import ClinicPolicy, clinic_slot_date
from rbs.models.enums import RotationKind
from rbs.models.rotation import Rotation
from rbs.solver.core.context import ClinicDecision, PlanningContext, new_clinic_decision
from rbs.solver.core.objective_slots import _Conditional, _slot_literals
from rbs.solver.planning import covers, rotate_domain

if TYPE_CHECKING:
    from rbs.solver.core.objective import _ClinicObjectiveState

def _ensure_overlay_decisions(
    context: PlanningContext,
    decisions: dict[str, ClinicDecision],
    policy: ClinicPolicy,
) -> None:
    for rotation in context.instance.rotations:
        if rotation.kind is RotationKind.CLINIC or rotation.clinic_hours_disabled:
            continue
        domain, half_days = _overlay_domain(rotation, policy)
        if not domain or half_days <= 0 or half_days >= len(domain):
            continue
        for occurrence in context.by_rotation.get(rotation.id, []):
            if occurrence.key not in decisions:
                decisions[occurrence.key] = new_clinic_decision(
                    context.model,
                    f"ovclinic:{occurrence.key}",
                    rotate_domain(domain, occurrence.key),
                    pick=half_days,
                )


def _clinic_occurrences(
    context: PlanningContext,
    decisions: dict[str, ClinicDecision],
) -> list:
    result = []
    for occurrence in context.occurrences:
        rotation = context.rotations[occurrence.rotation_id]
        resident = context.residents[occurrence.resident_id]
        resident_slots = bool(resident.clinic_half_days) and not rotation.away
        if rotation.clinic_hours_disabled and not resident_slots:
            continue
        if _occurrence_has_clinic(rotation, occurrence.key in decisions, resident_slots):
            result.append(occurrence)
    return result


def _occurrence_has_clinic(
    rotation: Rotation,
    has_decision: bool,
    has_resident_slots: bool,
) -> bool:
    return bool(
        has_resident_slots
        or has_decision
        or rotation.kind is RotationKind.CLINIC
        or (rotation.clinic is not None and rotation.clinic.half_days_per_week > 0)
    )


def _collect_week_entries(
    context: PlanningContext,
    occurrences: list,
    week: int,
    decisions: dict[str, ClinicDecision],
    clinic_kind,
    state: _ClinicObjectiveState,
) -> list[tuple]:
    entries: list[tuple] = []
    for occurrence in occurrences:
        resident = context.residents[occurrence.resident_id]
        if week in resident.vacation_weeks:
            continue
        covering = _covering_literals(context, occurrence, week)
        if not covering:
            continue
        present = context.model.NewBoolVar(f"precept:{occurrence.key}:w{week}")
        context.model.Add(present == sum(covering))
        rotation = context.rotations[occurrence.rotation_id]
        entries.extend(
            _resident_clinic_entries(context, occurrence, rotation, week, present)
        )
        if rotation.clinic_hours_disabled:
            continue
        if rotation.kind is RotationKind.CLINIC:
            state.clinic_kind_week[occurrence.pgy, week].append(present)
            _clinic_kind_occupancy(
                context,
                occurrence,
                week,
                present,
                decisions,
                clinic_kind,
                entries,
            )
            continue
        entries.extend(
            _rotation_clinic_entries(
                context,
                occurrence,
                rotation,
                week,
                present,
                decisions,
            )
        )
    return _deduplicate_entries(entries)


def _covering_literals(context: PlanningContext, occurrence, week: int) -> list[Any]:
    return [
        context.placements[occurrence.key, start]
        for start in context.starts[occurrence.key]
        if covers(start, occurrence.duration_weeks, week)
    ]


def _resident_clinic_entries(
    context: PlanningContext,
    occurrence,
    rotation: Rotation,
    week: int,
    present,
) -> list[tuple]:
    if rotation.away:
        return []
    instance = context.instance
    policy = instance.clinic_policy
    resident = context.residents[occurrence.resident_id]
    entries = []
    for half_day in resident.clinic_half_days:
        if instance.is_academic_half_day(week, half_day.weekday, half_day.session):
            continue
        calendar_day = clinic_slot_date(
            instance.calendar.first_week_start,
            week,
            half_day.weekday,
        )
        allowed = policy.resolve_site_ids(half_day.sites) or list(policy.site_ids)
        open_sites = [
            site_id
            for site_id in policy.open_site_ids(calendar_day, allowed)
            if policy.max_capacity_on(site_id, calendar_day, half_day.session) > 0
        ]
        if open_sites:
            entries.append(
                (
                    occurrence,
                    half_day.weekday,
                    half_day.session,
                    present,
                    _pinned_site(policy, open_sites),
                )
            )
    return entries


def _rotation_clinic_entries(
    context: PlanningContext,
    occurrence,
    rotation: Rotation,
    week: int,
    present,
    decisions: dict[str, ClinicDecision],
) -> list[tuple]:
    if occurrence.key in decisions:
        return _decision_clinic_entries(
            context,
            occurrence,
            week,
            present,
            decisions[occurrence.key],
        )
    if rotation.clinic is None:
        return []
    return _fixed_clinic_entries(context, occurrence, week, present, rotation.clinic)


def _decision_clinic_entries(
    context: PlanningContext,
    occurrence,
    week: int,
    present,
    decision: ClinicDecision,
) -> list[tuple]:
    entries = []
    for index, slot in enumerate(decision.domain):
        if slot.weekday is None or slot.session is None:
            continue
        if context.instance.is_academic_half_day(week, slot.weekday, slot.session):
            continue
        entries.append(
            (
                occurrence,
                slot.weekday,
                slot.session,
                _Conditional(
                    present,
                    decision.selected[index],
                    pick=decision.pick,
                    domain_size=len(decision.domain),
                    negated=False,
                ),
                _pinned_site(context.instance.clinic_policy, slot.sites),
            )
        )
    return entries


def _fixed_clinic_entries(
    context: PlanningContext,
    occurrence,
    week: int,
    present,
    rule,
) -> list[tuple]:
    entries = []
    for slot in rule.expanded_slots():
        if slot.weekday is None or slot.session is None:
            continue
        if context.instance.is_academic_half_day(week, slot.weekday, slot.session):
            continue
        entries.append(
            (
                occurrence,
                slot.weekday,
                slot.session,
                present,
                _pinned_site(context.instance.clinic_policy, slot.sites),
            )
        )
    return entries


def _deduplicate_entries(entries: list[tuple]) -> list[tuple]:
    unique: dict[tuple[str, object, object], tuple] = {}
    for entry in entries:
        occurrence, weekday, session, _literal, _pinned = entry
        unique.setdefault((occurrence.key, weekday, session), entry)
    return list(unique.values())


def _available_week_entries(
    context: PlanningContext,
    week: int,
    entries: list[tuple],
) -> tuple[dict, dict]:
    grouped: dict[tuple, list[tuple]] = defaultdict(list)
    surviving: dict[str, list[tuple]] = defaultdict(list)
    for entry in entries:
        occurrence, weekday, session, literal, pinned = entry
        if context.instance.resident_clinic_is_blocked(
            occurrence.resident_id,
            week,
            weekday,
            session,
        ):
            continue
        grouped[occurrence.resident_id, weekday, session, pinned].append(entry)
        surviving[occurrence.key].append((occurrence, literal))
    return grouped, surviving


def _materialize_week_entries(
    context: PlanningContext,
    week: int,
    grouped: dict,
    state: _ClinicObjectiveState,
) -> tuple[dict, dict, dict, dict]:
    slots_by_resident: dict[str, list[Any]] = defaultdict(list)
    present_by_slot: dict[tuple, list[Any]] = defaultdict(list)
    primary_by_slot: dict[tuple, list[Any]] = defaultdict(list)
    present_by_pgy: dict[tuple, list[Any]] = defaultdict(list)

    for (resident_id, weekday, session, pinned), members in grouped.items():
        pgy = members[0][0].pgy
        keys = tuple(dict.fromkeys(member[0].key for member in members))
        literals = _slot_literals(
            context.model,
            f"clinic:{resident_id}:w{week}:{weekday}:{session}",
            members,
        )
        for literal in literals:
            state.in_clinic[resident_id, week].append((keys, weekday, session, literal))
            slots_by_resident[resident_id].append(literal)
            present_by_slot[weekday, session].append(literal)
            present_by_pgy[weekday, session, pgy].append(literal)
            if _counts_at_primary_site(
                context,
                week,
                weekday,
                session,
                pinned,
                literal,
            ):
                primary_by_slot[weekday, session].append(literal)
    return slots_by_resident, present_by_slot, primary_by_slot, present_by_pgy


def _counts_at_primary_site(
    context: PlanningContext,
    week: int,
    weekday,
    session,
    pinned: str | None,
    literal,
) -> bool:
    policy = context.instance.clinic_policy
    if pinned is None:
        return True
    calendar_day = clinic_slot_date(
        context.instance.calendar.first_week_start,
        week,
        weekday,
    )
    if policy.max_capacity_on(pinned, calendar_day, session) <= 0:
        context.model.Add(literal == 0)
    return pinned == policy.primary_site_id


def _clinic_kind_occupancy(
    context,
    occurrence,
    week,
    present,
    decisions,
    clinic_kind,
    entries,
) -> None:
    """Dedicated Clinic block: enabled sessions except Academic and Admin."""
    rotation = context.rotations[occurrence.rotation_id]
    if occurrence.key in decisions:
        decision = decisions[occurrence.key]
        selected_by_slot = {
            (slot.weekday, slot.session): decision.selected[index]
            for index, slot in enumerate(decision.domain)
            if slot.weekday is not None and slot.session is not None
        }
        for slot in clinic_kind.week_domain(context.instance, week, rotation):
            if slot.weekday is None or slot.session is None:
                continue
            selected = selected_by_slot.get((slot.weekday, slot.session))
            pinned = _pinned_site(context.instance.clinic_policy, slot.sites)
            if selected is None:
                entries.append((occurrence, slot.weekday, slot.session, present, pinned))
                continue
            in_clinic = _Conditional(
                present,
                selected.Not(),
                pick=decision.pick,
                domain_size=len(decision.domain),
                negated=True,
            )
            entries.append((occurrence, slot.weekday, slot.session, in_clinic, pinned))
        return
    for slot in clinic_kind.week_domain(context.instance, week, rotation):
        entries.append(
            (
                occurrence,
                slot.weekday,
                slot.session,
                present,
                _pinned_site(context.instance.clinic_policy, slot.sites),
            )
        )


def _overlay_domain(rotation: Rotation, policy: ClinicPolicy) -> tuple[list, int]:
    if rotation.clinic_hours_disabled:
        return [], 0
    rule = rotation.clinic
    if rule is None:
        return [], 0
    domain = [slot for slot in rule.expanded_slots() if not policy.is_academic(slot)]
    return domain, rule.half_days_per_week


def _pinned_site(policy: ClinicPolicy, site_ids: list[str]) -> str | None:
    resolved = policy.resolve_site_ids(site_ids)
    return resolved[0] if len(resolved) == 1 else None

