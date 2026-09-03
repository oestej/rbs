"""Allocate solved clinic sessions across any configured clinic directory.

Block placement determines when a resident attends clinic. This post-process
selects a clinic for every flexible session while honoring rotation-specific
clinic choices, clinic closures, recurring half-day availability, derived
staffing capacity, and resident-level allocation targets.

"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from math import ceil, floor

from rbs.models.clinic import ClinicPolicy, clinic_slot_date
from rbs.models.enums import RotationKind
from rbs.models.instance import SolverProblem
from rbs.models.rotation import Rotation
from rbs.models.schedule import AssignedClinic, Assignment, Schedule
from rbs.solver.reference import (
    reference_clinic_metadata,
    reference_clinic_sites,
    reference_locked_clinic_sites,
)

_SlotKey = tuple[int, object, object]


@dataclass(frozen=True)
class ClinicAllocationResult:
    """Per-clinic targets and the assignments produced by allocation."""

    flex_sessions: int
    target_sessions: int
    assigned_sessions: int
    target_by_clinic: dict[str, int] = field(default_factory=dict)
    assigned_by_clinic: dict[str, int] = field(default_factory=dict)

    @property
    def postprocessed(self) -> bool:
        return bool(self.assigned_by_clinic) or self.assigned_sessions > 0

    @property
    def shortfall(self) -> int:
        return max(self.target_sessions - self.assigned_sessions, 0)


@dataclass
class _Candidate:
    assignment: Assignment
    slot: AssignedClinic
    resident_id: str
    pgy: int | None
    clinic_ids: list[str]
    calendar_day: date
    preferred_clinic_id: str | None = None
    locked_clinic_id: str | None = None

    @property
    def key(self) -> _SlotKey:
        assert self.slot.week is not None
        return self.slot.week, self.slot.weekday, self.slot.session


def assign_clinic_sites(
    instance: SolverProblem,
    assignments: list[Assignment],
    *,
    reference_schedule: Schedule | None = None,
) -> ClinicAllocationResult:
    policy = instance.clinic_policy
    residents = instance.residents_by_id
    preferred_sites = reference_clinic_sites(instance, reference_schedule)
    protected_sites = reference_locked_clinic_sites(instance, reference_schedule)
    reference_metadata = reference_clinic_metadata(instance, reference_schedule)
    candidates: list[_Candidate] = []

    for assignment in assignments:
        rotation = instance.rotation(assignment.rotation_id)
        if rotation.away:
            assignment.clinic_slots = []
            continue
        retained: list[AssignedClinic] = []
        for slot in assignment.clinic_slots:
            occurrence_key = (
                assignment.resident_id,
                slot.week,
                slot.weekday,
                slot.session,
            )
            reference_slot = reference_metadata.get(occurrence_key)
            if reference_slot is not None:
                slot.locked = reference_slot.locked
                slot.automatic_lock_exempt = reference_slot.automatic_lock_exempt
                slot.manual_override = reference_slot.manual_override
                slot.manual_override_added = reference_slot.manual_override_added
                slot.manual_override_original_site = (
                    reference_slot.manual_override_original_site
                )
            if slot.admin or slot.week is None:
                retained.append(slot)
                continue
            if instance.is_academic_half_day(slot.week, slot.weekday, slot.session):
                continue
            resident = residents.get(assignment.resident_id)
            if resident is not None and instance.resident_clinic_is_blocked(
                resident.id,
                slot.week,
                slot.weekday,
                slot.session,
            ):
                continue
            allowed = _allowed_sites(rotation, slot, policy)
            if not allowed:
                allowed = list(policy.site_ids)
            calendar_day = clinic_slot_date(
                instance.calendar.first_week_start,
                slot.week,
                slot.weekday,
            )
            allowed = [
                clinic_id
                for clinic_id in policy.open_site_ids(calendar_day, allowed)
                if policy.max_capacity_on(clinic_id, calendar_day, slot.session) > 0
            ]
            if not allowed:
                # A closure or a clinic with no coverage removes this occurrence,
                # just as an individual day off does.
                continue
            retained.append(slot)
            candidates.append(
                _Candidate(
                    assignment=assignment,
                    slot=slot,
                    resident_id=assignment.resident_id,
                    pgy=resident.pgy if resident is not None else None,
                    clinic_ids=allowed,
                    calendar_day=calendar_day,
                    preferred_clinic_id=preferred_sites.get(
                        occurrence_key
                    ),
                    locked_clinic_id=protected_sites.get(occurrence_key),
                )
            )
        assignment.clinic_slots = retained

    by_resident: dict[str, list[_Candidate]] = defaultdict(list)
    for candidate in candidates:
        by_resident[candidate.resident_id].append(candidate)

    targets: dict[tuple[str, str], int] = {}
    target_by_clinic: dict[str, int] = defaultdict(int)
    for resident_id, resident_candidates in by_resident.items():
        resident_targets = _allocation_targets(
            policy,
            len(resident_candidates),
            pgy=resident_candidates[0].pgy,
            resident_id=resident_id,
        )
        for clinic_id, count in resident_targets.items():
            targets[resident_id, clinic_id] = count
            target_by_clinic[clinic_id] += count

    filled: dict[tuple[str, int, object, object], int] = defaultdict(int)
    assigned_by_resident: dict[tuple[str, str], int] = defaultdict(int)
    assigned_by_resident_day: dict[tuple[str, int, object, str], int] = defaultdict(int)
    assigned_by_resident_week: dict[tuple[str, int, str], int] = defaultdict(int)
    assigned_by_clinic: dict[str, int] = defaultdict(int)
    weekly_by_clinic: dict[tuple[str, int], int] = defaultdict(int)
    unassigned: list[_Candidate] = []

    # Source-pinned and single-option sessions are immutable. They consume
    # coverage before flexible sessions are considered.
    for candidate in candidates:
        clinic_id = _fixed_site(candidate)
        if clinic_id is None:
            candidate.slot.site = None
            unassigned.append(candidate)
            continue
        _assign(
            candidate,
            clinic_id,
            filled,
            assigned_by_resident,
            assigned_by_resident_day,
            assigned_by_resident_week,
            assigned_by_clinic,
            weekly_by_clinic,
        )

    flex_count = len(unassigned)
    primary_clinic = policy.primary_site_id
    ordered_rules = sorted(
        policy.allocation_rules_for(),
        key=lambda rule: (
            rule.clinic_id == primary_clinic,
            rule.target_fraction,
            policy.site_ids.index(rule.clinic_id),
        ),
    )

    # First satisfy every clinic's resident-level target. Choosing the
    # least-used week spreads assignments across the year; preferring weeks
    # with more primary-clinic demand smooths attending needs.
    for rule in ordered_rules:
        clinic_id = rule.clinic_id
        while True:
            eligible = [
                candidate
                for candidate in unassigned
                if clinic_id in candidate.clinic_ids
                and _under_capacity(policy, clinic_id, candidate, filled)
                and assigned_by_resident[candidate.resident_id, clinic_id]
                < targets.get((candidate.resident_id, clinic_id), 0)
            ]
            if not eligible:
                break
            best = min(
                eligible,
                key=lambda candidate: _target_assignment_key(
                    candidate,
                    clinic_id,
                    primary_clinic,
                    policy,
                    targets,
                    filled,
                    assigned_by_resident,
                    assigned_by_resident_day,
                    assigned_by_resident_week,
                    weekly_by_clinic,
                ),
            )
            _assign(
                best,
                clinic_id,
                filled,
                assigned_by_resident,
                assigned_by_resident_day,
                assigned_by_resident_week,
                assigned_by_clinic,
                weekly_by_clinic,
            )
            unassigned.remove(best)

    # Fill every remaining session with the clinic that is furthest below its
    # target, then by the lightest relative half-day load.
    for candidate in sorted(
        unassigned,
        key=lambda item: (
            item.slot.week or 0,
            item.slot.weekday.value,
            item.slot.session.value,
            item.resident_id,
        ),
    ):
        available = [
            clinic_id
            for clinic_id in candidate.clinic_ids
            if _under_capacity(policy, clinic_id, candidate, filled)
            and _under_allocation_max(
                policy,
                candidate.resident_id,
                candidate.pgy,
                clinic_id,
                len(by_resident[candidate.resident_id]),
                assigned_by_resident,
            )
        ]
        if not available:
            available = [
                clinic_id
                for clinic_id in candidate.clinic_ids
                if _under_capacity(policy, clinic_id, candidate, filled)
            ]
        if not available:
            # Preserve the required clinic session and make any true staffing
            # shortfall visible to schedule validation.
            available = list(candidate.clinic_ids)
        clinic_id = min(
            available,
            key=lambda candidate_clinic: _remainder_assignment_key(
                candidate,
                candidate_clinic,
                policy,
                targets,
                filled,
                assigned_by_resident,
                assigned_by_resident_day,
                assigned_by_resident_week,
                weekly_by_clinic,
            ),
        )
        _assign(
            candidate,
            clinic_id,
            filled,
            assigned_by_resident,
            assigned_by_resident_day,
            assigned_by_resident_week,
            assigned_by_clinic,
            weekly_by_clinic,
        )

    target_sessions = sum(target_by_clinic.values())
    assigned_target_sessions = sum(
        min(target, assigned_by_clinic.get(clinic_id, 0))
        for clinic_id, target in target_by_clinic.items()
    )
    return ClinicAllocationResult(
        flex_sessions=flex_count,
        target_sessions=target_sessions,
        assigned_sessions=assigned_target_sessions,
        target_by_clinic=dict(target_by_clinic),
        assigned_by_clinic=dict(assigned_by_clinic),
    )


def _allocation_targets(
    policy: ClinicPolicy,
    total: int,
    *,
    pgy: int | None,
    resident_id: str,
) -> dict[str, int]:
    """Round percentage targets to integer counts while preserving the total."""
    if total <= 0:
        return {clinic_id: 0 for clinic_id in policy.site_ids}
    targets: dict[str, int] = {}
    remainders: list[tuple[float, str]] = []
    rules = policy.allocation_rules_for(pgy=pgy, resident_id=resident_id)
    target_total = sum(rule.target_fraction for rule in rules)
    for rule in rules:
        normalized_target = (
            rule.target_fraction / target_total
            if target_total > 0
            else 1.0 / len(rules)
        )
        raw = normalized_target * total
        minimum = ceil(rule.min_fraction * total - 1e-9)
        maximum = floor(rule.max_fraction * total + 1e-9)
        base = min(max(floor(raw), minimum), maximum)
        targets[rule.clinic_id] = base
        remainders.append((raw - floor(raw), rule.clinic_id))
    remaining = total - sum(targets.values())
    if remaining > 0:
        while remaining > 0:
            changed = False
            for _remainder, clinic_id in sorted(
                remainders,
                key=lambda item: (-item[0], policy.site_ids.index(item[1])),
            ):
                rule = policy.allocation(
                    clinic_id,
                    pgy=pgy,
                    resident_id=resident_id,
                )
                maximum = floor(rule.max_fraction * total + 1e-9)
                if targets[clinic_id] >= maximum:
                    continue
                targets[clinic_id] += 1
                remaining -= 1
                changed = True
                if remaining <= 0:
                    break
            if not changed:
                break
    if remaining < 0:
        while remaining < 0:
            changed = False
            for _remainder, clinic_id in sorted(
                remainders,
                key=lambda item: (item[0], -policy.site_ids.index(item[1])),
            ):
                rule = policy.allocation(
                    clinic_id,
                    pgy=pgy,
                    resident_id=resident_id,
                )
                minimum = ceil(rule.min_fraction * total - 1e-9)
                if targets[clinic_id] <= minimum:
                    continue
                targets[clinic_id] -= 1
                remaining += 1
                changed = True
                if remaining >= 0:
                    break
            if not changed:
                break
    return targets


def _fixed_site(candidate: _Candidate) -> str | None:
    if candidate.locked_clinic_id in candidate.clinic_ids:
        return candidate.locked_clinic_id
    if len(candidate.clinic_ids) == 1:
        return candidate.clinic_ids[0]
    if candidate.slot.site in candidate.clinic_ids:
        return candidate.slot.site
    return None


def _assign(
    candidate: _Candidate,
    clinic_id: str,
    filled: dict[tuple[str, int, object, object], int],
    assigned_by_resident: dict[tuple[str, str], int],
    assigned_by_resident_day: dict[tuple[str, int, object, str], int],
    assigned_by_resident_week: dict[tuple[str, int, str], int],
    assigned_by_clinic: dict[str, int],
    weekly_by_clinic: dict[tuple[str, int], int],
) -> None:
    week, weekday, session = candidate.key
    candidate.slot.site = clinic_id
    filled[clinic_id, week, weekday, session] += 1
    assigned_by_resident[candidate.resident_id, clinic_id] += 1
    assigned_by_resident_day[
        candidate.resident_id,
        week,
        weekday,
        clinic_id,
    ] += 1
    assigned_by_resident_week[candidate.resident_id, week, clinic_id] += 1
    assigned_by_clinic[clinic_id] += 1
    weekly_by_clinic[clinic_id, week] += 1


def _under_capacity(
    policy: ClinicPolicy,
    clinic_id: str,
    candidate: _Candidate,
    filled: dict[tuple[str, int, object, object], int],
) -> bool:
    week, weekday, session = candidate.key
    return (
        filled[clinic_id, week, weekday, session]
        < policy.max_capacity_on(clinic_id, candidate.calendar_day, session)
    )


def _under_allocation_max(
    policy: ClinicPolicy,
    resident_id: str,
    pgy: int | None,
    clinic_id: str,
    total: int,
    assigned_by_resident: dict[tuple[str, str], int],
) -> bool:
    maximum = floor(
        policy.allocation(
            clinic_id,
            pgy=pgy,
            resident_id=resident_id,
        ).max_fraction
        * total
        + 1e-9
    )
    return assigned_by_resident[resident_id, clinic_id] < maximum


def _target_assignment_key(
    candidate: _Candidate,
    clinic_id: str,
    primary_clinic: str,
    policy: ClinicPolicy,
    targets: dict[tuple[str, str], int],
    filled: dict[tuple[str, int, object, object], int],
    assigned_by_resident: dict[tuple[str, str], int],
    assigned_by_resident_day: dict[tuple[str, int, object, str], int],
    assigned_by_resident_week: dict[tuple[str, int, str], int],
    weekly_by_clinic: dict[tuple[str, int], int],
) -> tuple:
    week, weekday, session = candidate.key
    deficit = targets.get((candidate.resident_id, clinic_id), 0) - assigned_by_resident[
        candidate.resident_id, clinic_id
    ]
    primary_count = filled[primary_clinic, week, weekday, session]
    primary_attendings = policy.attendings_needed(primary_count, primary_clinic)
    return (
        candidate.preferred_clinic_id != clinic_id,
        weekly_by_clinic[clinic_id, week],
        -primary_attendings,
        filled[clinic_id, week, weekday, session],
        -deficit,
        -assigned_by_resident_day[
            candidate.resident_id,
            week,
            weekday,
            clinic_id,
        ],
        -assigned_by_resident_week[candidate.resident_id, week, clinic_id],
        week,
        weekday.value,
        session.value,
        candidate.resident_id,
    )


def _remainder_assignment_key(
    candidate: _Candidate,
    clinic_id: str,
    policy: ClinicPolicy,
    targets: dict[tuple[str, str], int],
    filled: dict[tuple[str, int, object, object], int],
    assigned_by_resident: dict[tuple[str, str], int],
    assigned_by_resident_day: dict[tuple[str, int, object, str], int],
    assigned_by_resident_week: dict[tuple[str, int, str], int],
    weekly_by_clinic: dict[tuple[str, int], int],
) -> tuple:
    week, weekday, session = candidate.key
    deficit = targets.get((candidate.resident_id, clinic_id), 0) - assigned_by_resident[
        candidate.resident_id, clinic_id
    ]
    maximum = max(
        policy.max_capacity_on(clinic_id, candidate.calendar_day, session),
        1,
    )
    used = filled[clinic_id, week, weekday, session]
    return (
        candidate.preferred_clinic_id != clinic_id,
        -deficit,
        used / maximum,
        weekly_by_clinic[clinic_id, week],
        -assigned_by_resident_day[
            candidate.resident_id,
            week,
            weekday,
            clinic_id,
        ],
        -assigned_by_resident_week[candidate.resident_id, week, clinic_id],
        policy.site_ids.index(clinic_id),
    )


def clinic_weekly_attendings(
    instance: SolverProblem,
    assignments: list[Assignment],
    clinic_id: str,
) -> dict[int, int]:
    """Attending-sessions by week for one configured clinic."""
    counts: dict[_SlotKey, int] = defaultdict(int)
    residents = instance.residents_by_id
    for assignment in assignments:
        for slot in assignment.clinic_slots:
            if slot.admin or slot.week is None or slot.site != clinic_id:
                continue
            if instance.is_academic_half_day(slot.week, slot.weekday, slot.session):
                continue
            resident = residents.get(assignment.resident_id)
            if resident is not None and instance.resident_clinic_is_blocked(
                resident.id,
                slot.week,
                slot.weekday,
                slot.session,
            ):
                continue
            counts[slot.week, slot.weekday, slot.session] += 1
    by_week: dict[int, int] = defaultdict(int)
    for (week, _weekday, _session), resident_count in counts.items():
        by_week[week] += instance.clinic_policy.attendings_needed(
            resident_count,
            clinic_id,
        )
    return dict(by_week)


def clinic_block_weekly_headcount(
    instance: SolverProblem,
    assignments: list[Assignment],
) -> dict[int, int]:
    """Residents sitting on a dedicated Clinic block, by week."""
    by_week: dict[int, int] = dict.fromkeys(range(1, instance.calendar.weeks + 1), 0)
    for assignment in assignments:
        if assignment.kind is not RotationKind.CLINIC:
            continue
        for week in assignment.weeks:
            by_week[week] = by_week.get(week, 0) + 1
    return by_week


def clinic_weekly_sessions(
    instance: SolverProblem,
    assignments: list[Assignment],
) -> dict[int, int]:
    """Resident clinic half-days per week, across every site.

    Site agnostic, so it reports the calendar-level clinic volume a program
    feels as "a lot of people are in clinic this week".
    """
    residents = instance.residents_by_id
    by_week: dict[int, int] = dict.fromkeys(range(1, instance.calendar.weeks + 1), 0)
    for assignment in assignments:
        for slot in assignment.clinic_slots:
            if slot.admin or slot.week is None:
                continue
            if instance.is_academic_half_day(slot.week, slot.weekday, slot.session):
                continue
            resident = residents.get(assignment.resident_id)
            if resident is not None and instance.resident_clinic_is_blocked(
                resident.id,
                slot.week,
                slot.weekday,
                slot.session,
            ):
                continue
            by_week[slot.week] = by_week.get(slot.week, 0) + 1
    return by_week


def clinic_attending_total(
    instance: SolverProblem,
    assignments: list[Assignment],
    clinic_id: str,
) -> int:
    return sum(clinic_weekly_attendings(instance, assignments, clinic_id).values())


def _allowed_sites(
    rotation: Rotation,
    slot: AssignedClinic,
    policy: ClinicPolicy,
) -> list[str]:
    if slot.allowed_sites:
        return policy.resolve_site_ids(slot.allowed_sites)
    rules = []
    if rotation.clinic is not None:
        rules.append(rotation.clinic)
    for rule in rules:
        for item in rule.slots:
            if item.weekday is slot.weekday and item.session is slot.session:
                return policy.resolve_site_ids(item.sites)
    return []
