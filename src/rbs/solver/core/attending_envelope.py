"""Precepting supply for the block solve, without per-attending variables.

Resident capacity at an attending-managed clinic is the number of attendings
precepting there times the clinic's capacity points per attending. The block
solve must know how many preceptors a half-day can have, but not who they
are. So in each week, attendings who are interchangeable for precepting (the
same open half-days, the same weekly limits, the same Attending Clinic day
rule) form a class, and the model decides only how many members of each
class precept at each half-day. The weekly attending pass after the solve
then names them (see :mod:`rbs.solver.core.attending_assignment`).

Each class limit comes from exact single-attending solves built by
:func:`rbs.solver.core.attending_rules.add_attending_week`, the same rules the
weekly pass enforces:

- which half-days an attending could precept at all;
- the most precepting half-days they could take in the week;
- how many days they could spend entirely on precepting while still
  meeting their Attending Clinic day minimum.

Members of a class are interchangeable, so these per-class bounds are exact
for the class as a whole, and the explicit per-class supply variables make
the flow between classes exact as well. The model therefore grows with how
differently attendings are configured, not with how many there are.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from functools import partial
from typing import Any

from rbs.models.attending import AttendingWorkType
from rbs.models.clinic import clinic_slot_date
from rbs.models.enums import Session, Weekday
from rbs.models.instance import SolverProblem
from rbs.models.schedule import Schedule
from rbs.solver.attending_availability import (
    AttendingWeekFacts,
    HalfDay,
    half_day_sort_key,
    precepting_allowed,
    schedule_attending_coverage,
)
from rbs.solver.core.attending_rules import add_attending_week
from rbs.solver.core.lexicographic import solve_in_tiers

_PROFILE_WORK_LIMIT = 1.0
_PROFILE_SAFETY_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class PreceptingProfile:
    """What one attending-week could contribute to attending-managed clinics."""

    capacity: int
    half_days: frozenset[HalfDay]
    relaxed: bool = False


NO_PRECEPTING = PreceptingProfile(capacity=0, half_days=frozenset())


@dataclass(frozen=True, slots=True)
class ClinicPreceptors:
    """Preceptors at one attending-managed clinic half-day."""

    clinic_id: str
    ratio: int
    fixed: int
    generated: Any | None = None
    generated_upper_bound: int = 0

    def capacity_points(self):
        preceptors = self.fixed + (self.generated if self.generated is not None else 0)
        return self.ratio * preceptors


@dataclass(frozen=True, slots=True)
class HalfDayDemand:
    """Resident sessions one half-day holds, as literals of the block model.

    ``occupied`` and ``pinned`` repeat a literal once per capacity point.
    ``share`` pairs each flexible session with its resident's target percent
    at a clinic, times its capacity points.
    """

    occupied: tuple[Any, ...]
    fixed_capacity: int
    pinned: dict[str, tuple[Any, ...]]
    share: dict[str, tuple[tuple[int, Any], ...]]


class AttendingEnvelope:
    """Lazily built precepting supply for the weeks the block solve asks about."""

    def __init__(
        self,
        problem: SolverProblem,
        facts_by_week: dict[int, list[AttendingWeekFacts]],
        model,
        cp_model,
        *,
        reference: Schedule | None = None,
        profiles: dict[tuple, PreceptingProfile] | None = None,
    ) -> None:
        self.problem = problem
        self.facts_by_week = facts_by_week
        self.model = model
        self._cp_model = cp_model
        policy = problem.clinic_policy
        self.managed = {
            site_id: policy.site(site_id)
            for site_id in sorted(problem.attending_managed_clinic_ids)
        }
        self.fixed: Counter[tuple[str, int, Weekday, Session]] = Counter()
        for week, weekly in facts_by_week.items():
            for facts in weekly:
                for item in facts.fixed:
                    if (
                        item.work_type is AttendingWorkType.PRECEPTING_CLINIC
                        and item.clinic_id in self.managed
                        and self.clinic_open(item.clinic_id, week, item.weekday)
                    ):
                        self.fixed[item.clinic_id, week, item.weekday, item.session] += 1
        self.generated: dict[tuple[str, int, Weekday, Session], Any] = {}
        self.generated_upper_bounds: dict[tuple[str, int, Weekday, Session], int] = {}
        self._profiles: dict[tuple, PreceptingProfile] = profiles if profiles is not None else {}
        self._demand: dict[int, dict[HalfDay, HalfDayDemand]] = defaultdict(dict)
        self._reference_counts: dict[tuple[str, date, Session], int] = (
            {
                (item.clinic_id, item.date, item.session): item.attendings
                for item in schedule_attending_coverage(problem, reference)
            }
            if reference is not None and reference.attending_work
            else {}
        )

    @property
    def active(self) -> bool:
        return bool(self.managed)

    def clinic_open(self, clinic_id: str, week: int, weekday: Weekday) -> bool:
        policy = self.problem.clinic_policy
        if clinic_id not in policy.site_ids:
            return False
        calendar_day = clinic_slot_date(self.problem.calendar.first_week_start, week, weekday)
        return not policy.site(clinic_id).is_closed(calendar_day)

    def maximum(self, clinic_id: str, week: int, weekday: Weekday, session: Session) -> int:
        """Most attendings who may precept at a clinic on one half-day."""
        calendar_day = clinic_slot_date(self.problem.calendar.first_week_start, week, weekday)
        return self.problem.clinic_max_attendings_on(clinic_id, calendar_day, session)

    def week_preceptors(
        self,
        week: int,
        half_days: set[HalfDay],
    ) -> dict[HalfDay, list[ClinicPreceptors]]:
        """Preceptor handles for the half-days of one week that may hold residents."""
        if not self.managed or not half_days:
            return {}
        supply: dict[HalfDay, list[tuple[Any, int]]] = defaultdict(list)
        for index, (profile, members) in enumerate(self._classes(week)):
            usable = sorted(profile.half_days & half_days, key=half_day_sort_key)
            if not usable or profile.capacity <= 0:
                continue
            size = len(members)
            counts = {
                half_day: self.model.NewIntVar(
                    0,
                    size,
                    f"precept_supply:w{week}:k{index}:{half_day[0].value}:{half_day[1].value}",
                )
                for half_day in usable
            }
            if profile.capacity < len(usable):
                self.model.Add(sum(counts.values()) <= size * profile.capacity)
            if not profile.relaxed:
                self._limit_precepting_days(members[0], counts, size, week, index)
            for half_day, count in counts.items():
                supply[half_day].append((count, size))

        preceptors: dict[HalfDay, list[ClinicPreceptors]] = {}
        for half_day in sorted(half_days, key=half_day_sort_key):
            weekday, session = half_day
            open_clinics = [
                clinic_id
                for clinic_id in self.managed
                if self.clinic_open(clinic_id, week, weekday)
            ]
            if not open_clinics:
                continue
            terms = supply.get(half_day, [])
            supply_limit = sum(size for _count, size in terms)
            handles: list[ClinicPreceptors] = []
            generated: list[Any] = []
            for clinic_id in open_clinics:
                fixed = self.fixed[clinic_id, week, weekday, session]
                # The clinic's maximum for this half-day bounds everyone who
                # precepts there, hand-entered work included.
                room = max(0, self.maximum(clinic_id, week, weekday, session) - fixed)
                upper = min(supply_limit, room)
                variable = None
                if upper > 0:
                    variable = self.model.NewIntVar(
                        0,
                        upper,
                        f"precept:{clinic_id}:w{week}:{weekday.value}:{session.value}",
                    )
                    key = (clinic_id, week, weekday, session)
                    self.generated[key] = variable
                    self.generated_upper_bounds[key] = upper
                    generated.append(variable)
                    self._hint(variable, clinic_id, week, weekday, session, fixed, upper)
                handles.append(
                    ClinicPreceptors(
                        clinic_id=clinic_id,
                        ratio=self.managed[clinic_id].residents_per_attending,
                        fixed=fixed,
                        generated=variable,
                        generated_upper_bound=upper if variable is not None else 0,
                    )
                )
            if generated:
                # Class members counted as precepting here are exactly the
                # preceptors the clinics receive, so no clinic's maximum can be
                # met by attendings who are not there.
                self.model.Add(sum(generated) == sum(count for count, _size in terms))
            elif terms:
                self.model.Add(sum(count for count, _size in terms) == 0)
            preceptors[half_day] = handles
        return preceptors

    def record_half_day(
        self,
        week: int,
        half_day: HalfDay,
        *,
        occupied: list[Any],
        fixed_capacity: int,
        pinned: dict[str, list[Any]],
        share: dict[str, list[tuple[int, Any]]],
    ) -> None:
        """Remember what one half-day holds so staffing can be polished later."""
        self._demand[week][half_day] = HalfDayDemand(
            occupied=tuple(occupied),
            fixed_capacity=fixed_capacity,
            pinned={clinic_id: tuple(items) for clinic_id, items in pinned.items()},
            share={clinic_id: tuple(items) for clinic_id, items in share.items()},
        )

    def staffed_counts(self, solver) -> dict[tuple[str, int, Weekday, Session], int]:
        """Preceptors each attending-managed half-day gets for a solved placement.

        The block search settles where residents are; it has little time left
        to also perfect how many preceptors each half-day gets. With the
        placement fixed that choice is a small exact problem, solved here in
        three tiers:

        1. open each clinic on every half-day its residents with a target
           share there are in clinic, the way a capacity-managed clinic is
           always open;
        2. open seats in proportion to those residents' target percents;
        3. use as few preceptors as possible.

        Capacity stays a hard constraint, and the search's own counts
        satisfy it, so this never makes a placement uncoverable.
        """
        searched = {key: int(solver.Value(variable)) for key, variable in self.generated.items()}
        if not self._demand:
            return searched
        cp_model = self._cp_model
        model = cp_model.CpModel()
        polish = AttendingEnvelope(
            self.problem,
            self.facts_by_week,
            model,
            cp_model,
            profiles=self._profiles,
        )
        unstaffed_terms: list[Any] = []
        shortfall_terms: list[Any] = []
        for week in sorted(self._demand):
            records = self._demand[week]
            preceptors = polish.week_preceptors(week, set(records))
            for half_day, record in records.items():
                handles = preceptors.get(half_day, [])
                variable_handles = [handle for handle in handles if handle.generated is not None]
                if not variable_handles:
                    continue
                occupied = sum(solver.Value(literal) for literal in record.occupied)
                model.Add(
                    record.fixed_capacity
                    + sum(handle.capacity_points() for handle in handles)
                    >= occupied
                )
                for handle in variable_handles:
                    pinned = sum(
                        solver.Value(literal) for literal in record.pinned.get(handle.clinic_id, ())
                    )
                    if pinned:
                        model.Add(handle.capacity_points() >= pinned)
                    weighted = sum(
                        weight * solver.Value(literal)
                        for weight, literal in record.share.get(handle.clinic_id, ())
                    )
                    if weighted <= 0:
                        continue
                    if handle.fixed < 1:
                        unstaffed = model.NewBoolVar(
                            f"unstaffed:{handle.clinic_id}:w{week}:{half_day[0].value}:"
                            f"{half_day[1].value}"
                        )
                        model.Add(handle.generated + unstaffed >= 1)
                        unstaffed_terms.append(unstaffed)
                    most = (weighted + 99) // 100
                    shortfall = model.NewIntVar(
                        0,
                        most,
                        f"share_short:{handle.clinic_id}:w{week}:{half_day[0].value}:"
                        f"{half_day[1].value}",
                    )
                    model.Add(100 * (handle.capacity_points() - pinned + shortfall) >= weighted)
                    shortfall_terms.append(shortfall)
        generated = list(polish.generated.values())
        for key, variable in polish.generated.items():
            model.AddHint(variable, searched.get(key, 0))
        polished = solve_in_tiers(
            model,
            [unstaffed_terms, shortfall_terms, generated],
            generated,
            cp_model,
        )
        if polished is None:
            return searched
        return {
            key: int(polished.Value(variable)) for key, variable in polish.generated.items()
        }

    def _hint(
        self,
        variable,
        clinic_id: str,
        week: int,
        weekday: Weekday,
        session: Session,
        fixed: int,
        upper: int,
    ) -> None:
        if not self._reference_counts:
            return
        calendar_day = clinic_slot_date(self.problem.calendar.first_week_start, week, weekday)
        previous = self._reference_counts.get((clinic_id, calendar_day, session), 0)
        self.model.AddHint(variable, max(0, min(upper, previous - fixed)))

    def _classes(self, week: int) -> list[tuple[PreceptingProfile, list[AttendingWeekFacts]]]:
        grouped: dict[tuple, list[AttendingWeekFacts]] = {}
        profiles: dict[tuple, PreceptingProfile] = {}
        for facts in self.facts_by_week.get(week, []):
            key, profile = self._profile(facts, week)
            if profile.capacity <= 0:
                continue
            grouped.setdefault(key, []).append(facts)
            profiles[key] = profile
        return [
            (profiles[key], members)
            for key, members in sorted(
                grouped.items(),
                key=lambda item: item[1][0].attending_id,
            )
        ]

    def _limit_precepting_days(
        self,
        facts: AttendingWeekFacts,
        counts: dict[HalfDay, Any],
        size: int,
        week: int,
        index: int,
    ) -> None:
        """Keep enough days free for each member's Attending Clinic day minimum."""
        needed = facts.clinic_day_minimum - len(facts.fixed_clinic_days)
        if (
            not facts.full_week
            or needed <= 0
            or AttendingWorkType.ATTENDING_CLINIC not in facts.work_types
        ):
            return
        candidate_days = [
            weekday
            for weekday in Weekday
            if weekday not in facts.fixed_clinic_days
            and any(half_day[0] is weekday for half_day in facts.open_half_days)
        ]
        blocked = []
        for weekday in candidate_days:
            day_half_days = [
                half_day for half_day in facts.open_half_days if half_day[0] is weekday
            ]
            if not all(half_day in counts for half_day in day_half_days):
                # Some open half-day that day never precepts here, so the day
                # always keeps room for Attending Clinic.
                continue
            fully_precepted = self.model.NewIntVar(
                0,
                size,
                f"precept_day:w{week}:k{index}:{weekday.value}",
            )
            self.model.Add(
                fully_precepted
                >= sum(counts[half_day] for half_day in day_half_days)
                - (len(day_half_days) - 1) * size
            )
            blocked.append(fully_precepted)
        if blocked:
            self.model.Add(sum(blocked) <= size * (len(candidate_days) - needed))

    def _profile(
        self,
        facts: AttendingWeekFacts,
        week: int,
    ) -> tuple[tuple, PreceptingProfile]:
        clinic_open = partial(self._precepting_allowed_in_week, week)
        open_pairs = frozenset(
            (half_day, clinic_id)
            for half_day in facts.open_half_days
            for clinic_id in facts.precepting_clinics
            if clinic_open(clinic_id, half_day[0], half_day[1])
        )
        managed_half_days = frozenset(
            half_day for half_day, clinic_id in open_pairs if clinic_id in self.managed
        )
        key = (facts.feasibility_signature(), open_pairs)
        if not facts.can_precept or facts.remaining < 1 or not managed_half_days:
            return key, NO_PRECEPTING
        cached = self._profiles.get(key)
        if cached is not None:
            return key, cached
        relaxed = False
        best = self._precepting_solve(facts, clinic_open, relaxed=False)
        if best is None:
            relaxed = True
            best = self._precepting_solve(facts, clinic_open, relaxed=True)
        if best is None:
            profile = NO_PRECEPTING
        else:
            capacity, used = best
            possible = set(used)
            for half_day in sorted(managed_half_days - possible, key=half_day_sort_key):
                if self._precepting_solve(
                    facts,
                    clinic_open,
                    relaxed=relaxed,
                    required=half_day,
                ) is not None:
                    possible.add(half_day)
            profile = PreceptingProfile(
                capacity=capacity if possible else 0,
                half_days=frozenset(possible),
                relaxed=relaxed,
            )
        self._profiles[key] = profile
        return key, profile

    def _precepting_allowed_in_week(
        self,
        week: int,
        clinic_id: str,
        weekday: Weekday,
        session: Session,
    ) -> bool:
        return precepting_allowed(
            self.problem,
            clinic_id,
            clinic_slot_date(self.problem.calendar.first_week_start, week, weekday),
            session,
        )

    def _precepting_solve(
        self,
        facts: AttendingWeekFacts,
        clinic_open,
        *,
        relaxed: bool,
        required: HalfDay | None = None,
    ) -> tuple[int, set[HalfDay]] | None:
        """Most managed-clinic precepting one attending-week allows, if feasible."""
        cp_model = self._cp_model
        model = cp_model.CpModel()
        handles = add_attending_week(
            model,
            facts,
            prefix="profile",
            clinic_open=clinic_open,
            relaxed=relaxed,
        )
        managed = [
            (half_day, literal)
            for (half_day, work_type, clinic_id), literal in handles.choices.items()
            if work_type is AttendingWorkType.PRECEPTING_CLINIC and clinic_id in self.managed
        ]
        if required is not None:
            at = [literal for half_day, literal in managed if half_day == required]
            if not at:
                return None
            model.Add(sum(at) >= 1)
        else:
            model.Maximize(sum(literal for _half_day, literal in managed))
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 0
        solver.parameters.max_deterministic_time = _PROFILE_WORK_LIMIT
        solver.parameters.max_time_in_seconds = _PROFILE_SAFETY_SECONDS
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return None
        used = {half_day for half_day, literal in managed if solver.Value(literal)}
        if required is not None:
            return 0, used
        # Single-attending models prove optimal almost at once. If one ever
        # stops early, the value found is a safe limit: under-stating what an
        # attending can precept only narrows the block solve, while the
        # objective bound could promise coverage nobody can provide.
        return int(round(solver.ObjectiveValue())), used


__all__ = [
    "AttendingEnvelope",
    "ClinicPreceptors",
    "HalfDayDemand",
    "NO_PRECEPTING",
    "PreceptingProfile",
]
