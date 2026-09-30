"""Conclusive contradictions between required clinic sessions and site seats.

A rotation clinic template can pin sessions to one site, and the compiler
turns a pinned session at a site with no seats into a hard ``literal == 0``
(see ``_counts_at_primary_site`` in ``objective_entries.py``). Required
sessions still count toward the occupancy floor (see ``_occupancy_floor`` in
``objective_slots.py``), so when every week a block could cover needs more
sessions than its live slots supply, the block is unplaceable and the whole
model is infeasible. The classic trigger is an attending-managed clinic with
no precepting coverage: every one of its half-days has zero seats.

This module replays that compiler logic with static inputs only: template
domains, site pinning, per-week seat counts, vacation weeks, days off, and
special rotations. It runs no search, so it stays deterministic and fast. It
mirrors the compiler entry builders exactly:

- resident continuity entries (``_resident_clinic_entries``), including the
  open-site capacity filter,
- overlay and FMED decisions (``_ensure_overlay_decisions``,
  ``fmed_kind.unique_clinic``) as conditional entries,
- fixed standard templates (``_fixed_clinic_entries``) as plain entries,
- dedicated Clinic blocks (``_clinic_kind_occupancy``) as plain entries, or
  negated conditional entries where an admin decision applies
  (``clinic_kind.constraints``),
- academic-half-day, day-off, and special-rotation filtering
  (``_available_week_entries``), entry order and deduplication
  (``_deduplicate_entries``), and the zero-seat forcing rule.

Firing implies the compiled model is infeasible for the same reason, so both
the pre-solve readiness check and the post-solve infeasibility diagnostics
can report it as conclusive. Anything the mirror cannot decide stays silent
by design: clinic-kind admin subtleties it does not model, the one-off
sessions a reference schedule's locked clinic sessions can add (their
resident-weeks are never called contradictory), and capacity upper bounds,
which can only make a silent case infeasible, never rescue a firing one.

Template facts do not change from week to week, so each rotation's are
computed once per probe, and a resident-week is judged once however many
occurrences can cover it.
"""

from __future__ import annotations

from collections import defaultdict

from rbs.clinic_locks import locked_clinic_states
from rbs.models.clinic import clinic_slot_date
from rbs.models.enums import RotationKind
from rbs.models.instance import SolverProblem
from rbs.models.schedule import Schedule, SolverDiagnostic
from rbs.solver.attending_availability import attending_week_facts, potential_capacity_view
from rbs.solver.planning import (
    Occurrence,
    covers,
    expand_occurrences,
    legal_starts,
    weeks_covered,
)

CODE = "uncoverable_clinic_session"


def uncoverable_clinic_sessions(
    problem: SolverProblem,
    *,
    allow_boundary_spans: bool,
    reference_schedule: Schedule | None = None,
) -> list[SolverDiagnostic]:
    """Name placement groups no week can host for lack of clinic seats.

    An attending-managed clinic counts every attending who could precept on
    a half-day, since the solve schedules them; only a half-day nobody could
    staff has no seats. ``reference_schedule`` is the schedule a re-solve
    starts from: its locked attending work precepts too, and its locked
    clinic sessions may add sessions to their resident's week.
    """
    rescued = {
        (resident_id, week)
        for (resident_id, week, _weekday, _session), in_clinic in locked_clinic_states(
            problem, reference_schedule
        ).items()
        if in_clinic
    }
    facts = (
        attending_week_facts(problem, reference_schedule)
        if problem.attending_managed_clinic_ids
        else None
    )
    problem = potential_capacity_view(problem, facts)
    try:
        occurrences = expand_occurrences(problem)
    except ValueError:
        # A missing elective fallback (or similar) is owned by the
        # compile-time configuration error, not by this probe.
        return []
    probe = _Probe(problem, rescued)
    starts = _starts_by_occurrence(problem, occurrences, probe, allow_boundary_spans)
    if starts is None:
        return []
    by_group: dict[str, list[Occurrence]] = defaultdict(list)
    for occurrence in occurrences:
        if occurrence.key not in starts:
            continue
        by_group[occurrence.group_id].append(occurrence)
    merged: dict[tuple[tuple[str, ...], tuple[str, ...]], dict] = {}
    for group_id in sorted(by_group):
        members = by_group[group_id]
        dead = probe.doomed_group_dead_sites(members, starts)
        if not dead:
            continue
        rotation_ids = tuple(sorted({member.rotation_id for member in members}))
        key = (rotation_ids, tuple(sorted(dead)))
        entry = merged.setdefault(key, {"resident_ids": set(), "members": members})
        entry["resident_ids"].update(member.resident_id for member in members)
    return [
        _diagnostic(problem, entry["members"], key[0], key[1], sorted(entry["resident_ids"]))
        for key, entry in sorted(merged.items())
    ]


def _starts_by_occurrence(
    problem: SolverProblem,
    occurrences: list[Occurrence],
    probe: _Probe,
    allow_boundary_spans: bool,
) -> dict[str, list[int]] | None:
    """Mirror ``PlanningContext`` start domains, including its drop rule."""
    starts: dict[str, list[int]] = {}
    for occurrence in occurrences:
        rotation = probe.rotations[occurrence.rotation_id]
        resident = probe.residents[occurrence.resident_id]
        legal = legal_starts(
            occurrence,
            resident,
            rotation,
            problem.calendar,
            vacation_weeks=problem.resident_scheduling_vacation_weeks(resident.id),
            allow_blocks_to_span_four_week_boundaries=allow_boundary_spans,
        )
        if not legal:
            # An unavailable elective candidate simply loses to another
            # requested service in its shared group; anything else is a
            # compile-time configuration error owned elsewhere.
            if occurrence.preference_managed and not occurrence.elective_fallback:
                continue
            return None
        starts[occurrence.key] = legal
    return starts


class _Probe:
    """One probe run's view of the problem, with its week-independent facts cached."""

    def __init__(self, problem: SolverProblem, rescued: set[tuple[str, int]]) -> None:
        self.problem = problem
        self.policy = problem.clinic_policy
        self.residents = problem.residents_by_id
        self.rotations = problem.rotations_by_id
        self.rescued = rescued
        self._slots: dict[str, list] = {}
        self._decisions: dict[str, tuple[str, int, list, bool] | None] = {}
        self._pinned: dict[tuple[str, ...], str | None] = {}
        self._dead: dict[tuple[str, str, int], set[str] | None] = {}

    def doomed_group_dead_sites(
        self,
        members: list[Occurrence],
        starts: dict[str, list[int]],
    ) -> set[str]:
        """Sites whose missing seats doom every placement of the group, if any.

        The compiler places exactly one member of the group, so the group is
        doomed only when every legal start of every member touches a week whose
        required clinic sessions exceed its schedulable ones. A start covering
        only vacation weeks carries no clinic requirement and always escapes,
        as does one covering only weeks a reference clinic lock may rescue.
        """
        weekly: list[set[str]] = []
        for occurrence in members:
            resident = self.residents[occurrence.resident_id]
            rotation = self.rotations[occurrence.rotation_id]
            vacation = set(resident.vacation_weeks)
            contradicted: dict[int, set[str]] = {}
            for week in range(1, self.problem.calendar.weeks + 1):
                if week in vacation or (resident.id, week) in self.rescued:
                    continue
                if not any(
                    covers(start, occurrence.duration_weeks, week)
                    for start in starts[occurrence.key]
                ):
                    continue
                hit = self.week_dead_sites(resident, rotation, week)
                if hit:
                    contradicted[week] = hit
            for start in starts[occurrence.key]:
                if not any(
                    contradicted.get(week)
                    for week in weeks_covered(start, occurrence.duration_weeks)
                    if week not in vacation
                ):
                    return set()
            weekly.extend(contradicted.values())
        # Report the sites behind every contradictory week. A site that is only
        # incidentally closed in some of those weeks did not cause the doom, so
        # it stays out of the message unless nothing else explains a week.
        reported = set.intersection(*weekly) if weekly else set()
        if not reported:
            reported = set.union(*weekly) if weekly else set()
        return reported

    def week_dead_sites(self, resident, rotation, week: int) -> set[str] | None:
        """Dead sites behind this resident-week contradiction, if contradictory."""
        key = (resident.id, rotation.id, week)
        if key not in self._dead:
            self._dead[key] = self._week_dead_sites(resident, rotation, week)
        return self._dead[key]

    def _week_dead_sites(self, resident, rotation, week: int) -> set[str] | None:
        shapes, params = self._week_shapes(resident, rotation, week)
        if not shapes:
            return None
        seen: set[tuple] = set()
        unique: list[tuple] = []
        for shape in shapes:
            if (shape[0], shape[1]) not in seen:
                seen.add((shape[0], shape[1]))
                unique.append(shape)
        instance = self.problem
        dead_slots = {
            (weekday, session)
            for weekday, session, pinned, _conditional in unique
            if pinned is not None
            and instance.clinic_max_capacity_on(
                pinned,
                clinic_slot_date(instance.calendar.first_week_start, week, weekday),
                session,
            )
            <= 0
        }
        # Without a dead site the week is never reported (see below), so most
        # resident-weeks stop here before the per-half-day availability checks.
        if not dead_slots:
            return None
        forced: set[str] = set()
        forced_plain = False
        plain = 0
        conditionals = 0
        live = 0
        for weekday, session, pinned, conditional in unique:
            if instance.resident_clinic_is_blocked(resident.id, week, weekday, session):
                continue
            if (weekday, session) in dead_slots:
                assert pinned is not None
                forced.add(pinned)
                if conditional is None:
                    forced_plain = True
                continue
            live += 1
            if conditional is None:
                plain += 1
            else:
                conditionals += 1
        if params is not None and conditionals:
            pick, domain_size, negated = params
            floor = plain + _occupancy_floor(conditionals, pick, domain_size, negated)
        else:
            floor = plain
        if not forced_plain and floor <= live:
            return None
        # A contradiction without a dead site belongs to another explanation;
        # the floor can never exceed its surviving entries otherwise.
        return forced or None

    def _week_shapes(
        self,
        resident,
        rotation,
        week: int,
    ) -> tuple[list[tuple], tuple[int, int, bool] | None]:
        """Static entry shapes mirroring the compiler's week entry builders."""
        instance = self.problem
        resident_slots = bool(resident.clinic_half_days) and not rotation.away
        if rotation.clinic_hours_disabled and not resident_slots:
            return [], None
        shapes: list[tuple] = []
        if not rotation.away:
            shapes.extend(self._resident_shapes(resident, week))
        decision = self._decision(rotation)
        if decision is not None:
            kind, pick, domain, negated = decision
            if kind == "admin":
                admin_index = {(slot.weekday, slot.session) for slot in domain}
                for slot in self._clinic_week_slots(rotation, week):
                    shapes.append(
                        (
                            slot.weekday,
                            slot.session,
                            self._pinned_site(slot.sites),
                            (pick, len(domain), True)
                            if (slot.weekday, slot.session) in admin_index
                            else None,
                        )
                    )
                return shapes, (pick, len(domain), True)
            for slot in domain:
                if slot.weekday is None or slot.session is None:
                    continue
                if instance.is_academic_half_day(week, slot.weekday, slot.session):
                    continue
                shapes.append(
                    (
                        slot.weekday,
                        slot.session,
                        self._pinned_site(slot.sites),
                        (pick, len(domain), False),
                    )
                )
            return shapes, (pick, len(domain), False)
        if rotation.kind is RotationKind.CLINIC:
            for slot in self._clinic_week_slots(rotation, week):
                shapes.append(
                    (slot.weekday, slot.session, self._pinned_site(slot.sites), None)
                )
            return shapes, None
        if rotation.clinic is None:
            return shapes, None
        for slot in self._expanded_slots(rotation):
            if instance.is_academic_half_day(week, slot.weekday, slot.session):
                continue
            shapes.append((slot.weekday, slot.session, self._pinned_site(slot.sites), None))
        return shapes, None

    def _resident_shapes(self, resident, week: int) -> list[tuple]:
        """Mirror ``_resident_clinic_entries``, including its open-site filter."""
        if not resident.clinic_half_days:
            return []
        instance = self.problem
        policy = self.policy
        shapes: list[tuple] = []
        for half_day in resident.clinic_half_days:
            if instance.is_academic_half_day(week, half_day.weekday, half_day.session):
                continue
            calendar_day = clinic_slot_date(
                instance.calendar.first_week_start, week, half_day.weekday
            )
            allowed = policy.resolve_site_ids(half_day.sites) or list(policy.site_ids)
            open_sites = [
                site_id
                for site_id in policy.open_site_ids(calendar_day, allowed)
                if instance.clinic_max_capacity_on(site_id, calendar_day, half_day.session) > 0
            ]
            if open_sites:
                shapes.append(
                    (half_day.weekday, half_day.session, self._pinned_site(open_sites), None)
                )
        return shapes

    def _expanded_slots(self, rotation) -> list:
        """The rotation template's concrete weekday/session choices."""
        if rotation.id not in self._slots:
            rule = rotation.clinic
            self._slots[rotation.id] = (
                [
                    slot
                    for slot in rule.expanded_slots()
                    if slot.weekday is not None and slot.session is not None
                ]
                if rule is not None
                else []
            )
        return self._slots[rotation.id]

    def _decision(self, rotation) -> tuple[str, int, list, bool] | None:
        if rotation.id not in self._decisions:
            self._decisions[rotation.id] = self._template_decision(rotation)
        return self._decisions[rotation.id]

    def _template_decision(self, rotation) -> tuple[str, int, list, bool] | None:
        """Mirror overlay, FMED, and admin decision existence and parameters."""
        if rotation.clinic_hours_disabled:
            return None
        rule = rotation.clinic
        policy = self.policy
        if rotation.kind is RotationKind.CLINIC:
            if rule is None or rule.admin_half_days_per_week <= 0:
                return None
            domain = self._admin_domain(rotation)
            if not domain:
                return None
            return ("admin", rule.admin_half_days_per_week, domain, True)
        if rotation.kind is RotationKind.FMED and rule is not None:
            domain = [
                slot for slot in self._expanded_slots(rotation) if not policy.is_academic(slot)
            ]
            if domain:
                return ("fmed", rule.half_days_per_week, domain, False)
        if rule is None:
            return None
        domain = [
            slot for slot in self._expanded_slots(rotation) if not policy.is_academic(slot)
        ]
        pick = rule.half_days_per_week
        if not domain or pick <= 0 or pick >= len(domain):
            return None
        return ("overlay", pick, domain, False)

    def _admin_domain(self, rotation) -> list:
        """Mirror ``clinic_kind.admin_domain`` with its academic exclusions."""
        policy = self.policy
        blocked = {(policy.academic.weekday, policy.academic.session)}
        blocked.update(
            (override.weekday, override.session)
            for override in self.problem.academic_half_day_overrides
        )
        return [
            slot
            for slot in self._expanded_slots(rotation)
            if (slot.weekday, slot.session) not in blocked
        ]

    def _clinic_week_slots(self, rotation, week: int) -> list:
        """Mirror ``clinic_kind.week_domain`` for one dedicated block rotation."""
        if rotation.clinic_hours_disabled:
            return []
        return [
            slot
            for slot in self._expanded_slots(rotation)
            if not self.problem.is_academic_half_day(week, slot.weekday, slot.session)
        ]

    def _pinned_site(self, site_ids: list[str]) -> str | None:
        """Mirror ``_pinned_site``: a lone resolved site, else no pinning."""
        key = tuple(site_ids)
        if key not in self._pinned:
            resolved = self.policy.resolve_site_ids(site_ids)
            self._pinned[key] = resolved[0] if len(resolved) == 1 else None
        return self._pinned[key]


def _occupancy_floor(surviving: int, pick: int, domain_size: int, negated: bool) -> int:
    """Mirror ``objective_slots._occupancy_floor`` exactly (kept in sync by test)."""
    if negated:
        return max(0, surviving - pick)
    return max(0, pick - (domain_size - surviving))


def _diagnostic(
    problem: SolverProblem,
    members: list[Occurrence],
    rotation_ids: tuple[str, ...],
    dead_ids: tuple[str, ...],
    resident_ids: list[str],
) -> SolverDiagnostic:
    residents = problem.residents_by_id
    rotations = problem.rotations_by_id
    policy = problem.clinic_policy
    names = sorted(residents[resident_id].name for resident_id in resident_ids)
    rotation_names = sorted(rotations[rotation_id].name for rotation_id in rotation_ids)
    who = names[0] if len(names) == 1 else ", ".join(names)
    what = rotation_names[0] if len(rotation_names) == 1 else "clinic blocks"
    reasons = []
    for site_id in dead_ids:
        site_name = policy.site_name(site_id)
        site = policy.site(site_id)
        coverage = [
            record
            for record in problem.attending_coverage
            if record.clinic_id == site_id
        ]
        if site.staffing_mode.value == "attending_managed" and not coverage:
            reasons.append(f"no attending is available to precept at {site_name}")
        elif site.staffing_mode.value == "attending_managed":
            reasons.append(
                f"no attending is available to precept at {site_name} on the affected "
                "half-days"
            )
        else:
            reasons.append(f"{site_name} has no open seats on the affected half-days")
    suggestions = []
    managed = [
        policy.site_name(site_id)
        for site_id in dead_ids
        if policy.site(site_id).staffing_mode.value == "attending_managed"
    ]
    if managed:
        sites = ", ".join(managed)
        suggestions.append(
            f"Make sure an attending can precept at {sites} on those half-days: check "
            "their schedule dates, vacation, weekly totals, and Fixed ranges, or add "
            "an attending."
        )
        suggestions.append(
            f"Allow precepting at {sites} on those half-days by setting its maximum "
            "attendings in the weekly schedule, or with an exception for the dates."
        )
        suggestions.append(f"Or switch {sites} back to capacity-managed staffing.")
    else:
        sites = ", ".join(policy.site_name(site_id) for site_id in dead_ids)
        suggestions.append(f"Raise clinic capacity at {sites} on the affected half-days.")
    if len(rotation_names) == 1:
        suggestions.append(
            f"Move the {rotation_names[0]} clinic sessions to a site with open seats."
        )
    else:
        suggestions.append("Move the affected clinic sessions to sites with open seats.")
    return SolverDiagnostic(
        code=CODE,
        message=(
            f"{who} cannot place {what}: {'; '.join(reasons)}, leaving fewer usable "
            "clinic sessions than required."
        ),
        resident_ids=resident_ids,
        suggestions=suggestions,
    )


__all__ = ["CODE", "uncoverable_clinic_sessions"]
