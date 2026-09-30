"""The weekly attending pass: name the attendings a solved schedule needs.

After the block solve, every resident clinic session has a site, so each
attending-managed clinic half-day needs a known number of preceptors. The
envelope in :mod:`rbs.solver.core.attending_envelope` already proved that
many could be supplied under every attending's rules. This pass decides who,
and fills the rest of each attending's week, one small model per academic
week. Every attending rule is weekly, so the weeks are independent and the
year never becomes one large model.

Precepting coverage is a hard requirement. The attending rules use the same
builder as the envelope, in relaxed form: a Fixed minimum or an exact weekly
total that cannot be met (for example because hand-entered work already
contradicts it) becomes a counted violation instead of an empty schedule.
The objective is strictly lexicographic:

1. violations of Fixed ranges, weekly totals, and Attending Clinic days;
2. precepting beyond what residents need, so a Flexible range, a preference,
   or the previous schedule never books a preceptor with no one to precept;
3. changes to the previous schedule's attending work;
4. distance outside Flexible ranges;
5. preferred weekly schedule half-days not matched;
6. half-days left unfilled in vacation and boundary weeks.
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from math import ceil
from typing import Any

from rbs.models.attending import AttendingWorkType
from rbs.models.clinic import clinic_slot_date
from rbs.models.enums import Session, Weekday
from rbs.models.instance import SolverProblem
from rbs.models.schedule import AssignedAttendingWork, Schedule
from rbs.solver.attending_availability import AttendingWeekFacts, precepting_allowed
from rbs.solver.core.attending_rules import AttendingWeekModel, add_attending_week
from rbs.solver.core.lexicographic import solve_in_tiers

PreceptingKey = tuple[str, int, Weekday, Session]
_WEEK_THREADS = max(1, min(8, os.cpu_count() or 1))


@dataclass(frozen=True)
class AttendingPassResult:
    """Attending work for one schedule, plus what could not be honored."""

    work: tuple[AssignedAttendingWork, ...] = ()
    relaxed_weeks: tuple[int, ...] = ()
    uncovered: tuple[tuple[PreceptingKey, int], ...] = ()
    released_locks: tuple[AssignedAttendingWork, ...] = ()
    scheduled_half_days: int = 0
    precepting_half_days: int = 0


@dataclass
class _WeekTerms:
    tiers: list[list[tuple[Any, int]]] = field(default_factory=lambda: [[] for _ in range(6)])

    def add(self, tier: int, expression, upper_bound: int) -> None:
        if upper_bound > 0:
            self.tiers[tier].append((expression, upper_bound))


_VIOLATIONS, _IDLE, _STABILITY, _FLEXIBLE, _PREFERRED, _FILL = range(6)


def precepting_demand(problem: SolverProblem, schedule: Schedule) -> Counter[PreceptingKey]:
    """Preceptors each attending-managed clinic half-day needs for its residents."""
    managed = problem.attending_managed_clinic_ids
    if not managed:
        return Counter()
    residents = problem.residents_by_id
    points: Counter[PreceptingKey] = Counter()
    for assignment in schedule.assignments:
        resident = residents.get(assignment.resident_id)
        weight = problem.clinic_capacity_for_pgy(resident.pgy if resident is not None else None)
        for slot in assignment.clinic_slots:
            if slot.admin or slot.site not in managed:
                continue
            weeks = [slot.week] if slot.week is not None else assignment.weeks
            for week in weeks:
                points[slot.site, week, slot.weekday, slot.session] += weight
    policy = problem.clinic_policy
    return Counter(
        {
            key: ceil(total / policy.site(key[0]).residents_per_attending)
            for key, total in points.items()
            if total > 0
        }
    )


def schedule_attending_work(
    problem: SolverProblem,
    schedule: Schedule,
    facts_by_week: dict[int, list[AttendingWeekFacts]],
    cp_model,
) -> AttendingPassResult:
    """Place every attending's work around the solved resident schedule."""
    demand = precepting_demand(problem, schedule)
    managed = problem.attending_managed_clinic_ids
    fixed: Counter[PreceptingKey] = Counter()
    for week, weekly in facts_by_week.items():
        for facts in weekly:
            for item in facts.fixed:
                if (
                    item.work_type is AttendingWorkType.PRECEPTING_CLINIC
                    and item.clinic_id in managed
                ):
                    fixed[item.clinic_id, week, item.weekday, item.session] += 1
    work: list[AssignedAttendingWork] = []
    placed = 0
    relaxed_weeks: list[int] = []
    uncovered: list[tuple[PreceptingKey, int]] = []
    released: list[AssignedAttendingWork] = []
    weeks = sorted(set(facts_by_week) | {key[1] for key in demand})
    needs = {
        week: {
            (key[0], key[2], key[3]): max(0, count - fixed[key])
            for key, count in demand.items()
            if key[1] == week and count - fixed[key] > 0
        }
        for week in weeks
    }
    # Weeks are independent and CP-SAT releases the GIL, so they solve in
    # parallel. Each week is still a single-worker, fixed-seed search with a
    # deterministic budget, so the result does not depend on the thread count.
    with ThreadPoolExecutor(max_workers=_WEEK_THREADS) as pool:
        solved = list(
            pool.map(
                lambda week: _solve_week(
                    problem,
                    week,
                    facts_by_week.get(week, []),
                    needs[week],
                    cp_model,
                ),
                weeks,
            )
        )
    for week, (week_work, violated, short) in zip(weeks, solved, strict=True):
        weekly = facts_by_week.get(week, [])
        work.extend(week_work)
        placed += len(week_work)
        if violated:
            relaxed_weeks.append(week)
        uncovered.extend(
            ((clinic_id, week, weekday, session), missing)
            for (clinic_id, weekday, session), missing in short.items()
        )
        for facts in weekly:
            work.extend(facts.locked_work)
            released.extend(facts.released_locks)
    return AttendingPassResult(
        work=tuple(work),
        relaxed_weeks=tuple(relaxed_weeks),
        uncovered=tuple(uncovered),
        released_locks=tuple(released),
        scheduled_half_days=placed,
        precepting_half_days=sum(
            item.work_type is AttendingWorkType.PRECEPTING_CLINIC
            and item.clinic_id in managed
            for item in work
        ),
    )


def _solve_week(
    problem: SolverProblem,
    week: int,
    weekly: list[AttendingWeekFacts],
    need: dict[tuple[str, Weekday, Session], int],
    cp_model,
) -> tuple[list[AssignedAttendingWork], bool, dict[tuple[str, Weekday, Session], int]]:
    placeable = [facts for facts in weekly if facts.open_half_days]
    if not placeable and not need:
        return [], False, {}
    precepting_already = Counter(
        (item.clinic_id, item.weekday, item.session)
        for facts in weekly
        for item in facts.fixed
        if item.work_type is AttendingWorkType.PRECEPTING_CLINIC
    )
    result = _build_and_solve(
        problem,
        week,
        placeable,
        need,
        precepting_already,
        cp_model,
        hard_coverage=True,
    )
    if result is None:
        # The envelope makes this unreachable for the rules it models, and
        # the search escalates its budget before giving up. If a rule outside
        # the envelope ever blocks coverage, keep every other placement and
        # let validation name the uncovered clinic sessions.
        result = _build_and_solve(
            problem,
            week,
            placeable,
            need,
            precepting_already,
            cp_model,
            hard_coverage=False,
        )
    if result is None:
        # Placing nothing always satisfies the relaxed model, so only a
        # search that ran out of budget ends here. Report the week rather
        # than abort the whole solve.
        return [], bool(placeable), dict(need)
    return result


def _build_and_solve(
    problem: SolverProblem,
    week: int,
    placeable: list[AttendingWeekFacts],
    need: dict[tuple[str, Weekday, Session], int],
    precepting_already: Counter[tuple[str | None, Weekday, Session]],
    cp_model,
    *,
    hard_coverage: bool,
):
    model = cp_model.CpModel()
    first_week_start = problem.calendar.first_week_start

    def clinic_open(clinic_id: str, weekday: Weekday, session: Session) -> bool:
        return precepting_allowed(
            problem,
            clinic_id,
            clinic_slot_date(first_week_start, week, weekday),
            session,
        )

    handles = [
        add_attending_week(
            model,
            facts,
            prefix=f"w{week}:a{index}",
            clinic_open=clinic_open,
            relaxed=True,
        )
        for index, facts in enumerate(placeable)
    ]
    terms = _WeekTerms()
    precepting: dict[tuple[str, Weekday, Session], list[Any]] = defaultdict(list)
    every_clinic: dict[tuple[str, Weekday, Session], list[Any]] = defaultdict(list)
    managed = problem.attending_managed_clinic_ids
    for week_model in handles:
        for (half_day, work_type, clinic_id), literal in week_model.choices.items():
            if work_type is not AttendingWorkType.PRECEPTING_CLINIC or clinic_id is None:
                continue
            every_clinic[clinic_id, half_day[0], half_day[1]].append(literal)
            if clinic_id in managed:
                precepting[clinic_id, half_day[0], half_day[1]].append(literal)
    for (clinic_id, weekday, session), literals in every_clinic.items():
        # No clinic half-day may have more attendings precepting than its
        # maximum, whether it is capacity- or attending-managed.
        maximum = problem.clinic_max_attendings_on(
            clinic_id,
            clinic_slot_date(first_week_start, week, weekday),
            session,
        )
        room = max(0, maximum - precepting_already[clinic_id, weekday, session])
        if room < len(literals):
            model.Add(sum(literals) <= room)

    shortfalls: dict[tuple[str, Weekday, Session], Any] = {}
    for key, required in need.items():
        literals = precepting.get(key, [])
        if hard_coverage:
            if len(literals) < required:
                return None
            model.Add(sum(literals) >= required)
        else:
            missing = model.NewIntVar(0, required, f"w{week}:uncovered:{key[0]}")
            if literals:
                model.Add(sum(literals) + missing >= required)
            else:
                model.Add(missing == required)
            shortfalls[key] = missing
    for literals in precepting.values():
        # Precepting beyond need is idle faculty time; the need itself is
        # constant, so minimizing the count minimizes the excess.
        terms.add(_IDLE, sum(literals), len(literals))

    for week_model in handles:
        _add_attending_terms(model, week_model, terms)

    # Tiers are listed most important first. Uncovered sessions, which only
    # the fallback allows, outrank everything.
    tiers = [
        [(variable, need[key]) for key, variable in shortfalls.items()],
        *terms.tiers,
    ]
    solver = solve_in_tiers(
        model,
        [[expression for expression, _bound in tier] for tier in tiers],
        (
            literal
            for week_model in handles
            for literal in week_model.choices.values()
        ),
        cp_model,
    )
    if solver is None:
        return None

    work: list[AssignedAttendingWork] = []
    violated = False
    for week_model in handles:
        facts = week_model.facts
        for (half_day, work_type, clinic_id), literal in week_model.choices.items():
            if not solver.Value(literal):
                continue
            previous = facts.reference.get(half_day)
            retained = (
                previous is not None
                and previous.work_type == work_type and previous.clinic_id == clinic_id
            )
            work.append(
                AssignedAttendingWork(
                    attending_id=facts.attending_id,
                    week=week,
                    weekday=half_day[0],
                    session=half_day[1],
                    work_type=work_type,
                    clinic_id=clinic_id,
                    automatic_lock_exempt=bool(retained and previous.automatic_lock_exempt),
                    manual_override=bool(retained and previous.manual_override),
                )
            )
        violated = violated or any(
            _value(solver, expression) > 0 for expression, _bound in week_model.violations
        )
    short = {
        key: _value(solver, variable)
        for key, variable in shortfalls.items()
        if _value(solver, variable) > 0
    }
    return work, violated, short


def _add_attending_terms(model, week_model: AttendingWeekModel, terms: _WeekTerms) -> None:
    facts = week_model.facts
    for expression, bound in week_model.violations:
        terms.add(_VIOLATIONS, expression, bound)

    for half_day, previous in facts.reference.items():
        literal = week_model.choices.get((half_day, previous.work_type, previous.clinic_id))
        if literal is not None:
            terms.add(_STABILITY, 1 - literal, 1)

    most = len(facts.fixed) + facts.remaining
    for work_type, (minimum, maximum) in facts.flexible_ranges:
        count = facts.fixed_count(work_type) + week_model.generated_count(work_type)
        if facts.full_week and minimum > facts.fixed_count(work_type):
            below = model.NewIntVar(0, minimum, f"{facts.attending_id}:{work_type.value}:below")
            model.Add(count + below >= minimum)
            terms.add(_FLEXIBLE, below, minimum)
        if most > maximum:
            above = model.NewIntVar(
                0,
                most - maximum,
                f"{facts.attending_id}:{work_type.value}:above",
            )
            model.Add(count - above <= maximum)
            terms.add(_FLEXIBLE, above, most - maximum)

    for half_day, preferred in facts.preferred.items():
        literal = week_model.choices.get((half_day, preferred.work_type, preferred.clinic_id))
        if literal is not None:
            terms.add(_PREFERRED, 1 - literal, 1)
        # A preference the attending can no longer take (for example a
        # category a Fixed range now excludes) is simply unmatched.

    if not facts.full_week and facts.fill_goal > 0:
        generated = list(week_model.choices.values())
        if generated:
            unfilled = model.NewIntVar(0, facts.fill_goal, f"{facts.attending_id}:unfilled")
            model.Add(sum(generated) + unfilled >= facts.fill_goal)
            terms.add(_FILL, unfilled, facts.fill_goal)


def _value(solver, expression) -> int:
    if isinstance(expression, int):
        return expression
    return int(solver.Value(expression))


__all__ = [
    "AttendingPassResult",
    "precepting_demand",
    "schedule_attending_work",
]
