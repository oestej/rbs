"""Sessions locked at an attending-managed clinic need preceptors reserved there.

Allocation never moves a locked session, so the block model and the staffing
polish must treat it as pinned to that clinic, or the solve staffs the clinic
only to its target share and the finished schedule is rejected as over capacity.

Compilation checks stay outside the ``solve`` marker group; only the end-to-end
tests start a real CP-SAT search.
"""

from __future__ import annotations

from collections import Counter

import pytest
from ortools.sat.python import cp_model

from rbs.catalog import sample_instance
from rbs.models.attending import (
    Attending,
    AttendingWeeklyShiftTarget,
    AttendingWeeklyTargetMode,
    AttendingWorkType,
)
from rbs.models.clinic import clinic_slot_date
from rbs.models.enums import Session, SolverEngineName, SolverStatus, Weekday
from rbs.models.instance import SchedulerInput, SolverProblem
from rbs.models.locks import LockedPlacement
from rbs.models.schedule import AssignedClinic, Assignment, Schedule, ScheduleMeta
from rbs.solver.attending_availability import schedule_capacity_view
from rbs.solver.core.compile import compile_problem

RESIDENT = "resident-001"
FRIDAY_MORNING = (Weekday.FRIDAY, Session.MORNING)
WEEKDAYS = (Weekday.MONDAY, Weekday.TUESDAY, Weekday.WEDNESDAY, Weekday.THURSDAY, Weekday.FRIDAY)


def _maple_half_days(maximum, overrides=None) -> list[dict]:
    """Max attendings at Maple, by half-day."""
    return [
        {
            "weekday": weekday.value,
            "session": session.value,
            "attendings": (overrides or {}).get((weekday, session), maximum),
        }
        for weekday in WEEKDAYS
        for session in Session
    ]


def _target(work_type, minimum, maximum, mode=AttendingWeeklyTargetMode.FLEXIBLE):
    return AttendingWeeklyShiftTarget(
        work_type=work_type,
        minimum_shifts_per_week=minimum,
        maximum_shifts_per_week=maximum,
        mode=mode,
    )


def _managed_program(locks=(), maple_half_days=None) -> SchedulerInput:
    raw = sample_instance().model_dump(mode="json")
    for site in raw["clinic_policy"]["sites"]:
        if site["id"] == "maple":
            site["staffing_mode"] = "attending_managed"
            if maple_half_days is not None:
                site["half_days"] = maple_half_days
    raw["attendings"] = [
        Attending(
            id=f"attending-{index:03d}",
            name=f"Attending {index:03d}",
            weekly_shift_targets=[
                _target(AttendingWorkType.ATTENDING_CLINIC, 3, 6),
                _target(AttendingWorkType.PRECEPTING_CLINIC, 1, 4),
                _target(AttendingWorkType.ADMIN_TIME, 1, 2, AttendingWeeklyTargetMode.FIXED),
            ],
        ).model_dump(mode="json")
        for index in range(6)
    ]
    raw["attending_schedules"] = []
    raw["locks"] = [lock.model_dump(mode="json") for lock in locks]
    return SchedulerInput.model_validate(raw)


def _reference(instance: SchedulerInput, *, locked: bool) -> Schedule:
    return Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.CP_SAT,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=RESIDENT,
                rotation_id="fmed",
                start_week=1,
                end_week=4,
                weeks=[1, 2, 3, 4],
                clinic_slots=[
                    AssignedClinic(
                        weekday=FRIDAY_MORNING[0],
                        session=FRIDAY_MORNING[1],
                        week=2,
                        site="maple",
                        locked=locked,
                    )
                ],
            )
        ],
    )


def _demand_at_friday(*, locked: bool):
    lock = LockedPlacement(
        resident_id=RESIDENT, rotation_id="fmed", weeks=[1, 2, 3, 4], exact_block=True,
    )
    instance = _managed_program([lock])
    compiled = compile_problem(
        SolverProblem.from_instance(instance),
        instance.solver,
        cp_model,
        reference_schedule=_reference(instance, locked=locked),
    )
    return compiled.context.attending_envelope._demand[2][FRIDAY_MORNING]


def _pinned_to_maple(demand) -> bool:
    return any(RESIDENT in str(literal) for literal in demand.pinned.get("maple", ()))


def test_a_locked_session_counts_as_pinned_to_its_managed_clinic() -> None:
    assert _pinned_to_maple(_demand_at_friday(locked=True))


def test_an_unlocked_session_at_the_same_clinic_stays_flexible() -> None:
    assert not _pinned_to_maple(_demand_at_friday(locked=False))


def _sessions_on(schedule: Schedule, week: int, weekday: Weekday, session: Session):
    return [
        (assignment, slot)
        for assignment in schedule.assignments
        for slot in assignment.clinic_slots
        if slot.week == week
        and slot.weekday is weekday
        and slot.session is session
        and not slot.admin
    ]


def _busiest_half_day(schedule: Schedule) -> tuple[int, Weekday, Session]:
    counts = Counter(
        (slot.week, slot.weekday, slot.session)
        for assignment in schedule.assignments
        for slot in assignment.clinic_slots
        if not slot.admin and slot.week is not None
    )
    return counts.most_common(1)[0][0]


def _locked_at_maple(schedule: Schedule, half_day, count: int) -> tuple[Schedule, set]:
    """Move ``count`` of a half-day's sessions to Maple by hand and lock them."""
    week, weekday, session = half_day
    chosen = {
        (assignment.resident_id, week, weekday, session)
        for assignment, _slot in _sessions_on(schedule, week, weekday, session)[:count]
    }
    assignments = []
    for assignment in schedule.assignments:
        slots = [
            slot.revised(site="maple", locked=True, manual_override=True)
            if (assignment.resident_id, slot.week, slot.weekday, slot.session) in chosen
            else slot
            for slot in assignment.clinic_slots
        ]
        assignments.append(assignment.revised(clinic_slots=slots))
    return schedule.revised(assignments=assignments), chosen


@pytest.mark.solve
def test_solve_staffs_a_managed_clinic_for_sessions_locked_there() -> None:
    from rbs.solver import solve_problem

    # Maple can seat many sessions, but its target share only calls for a few.
    instance = _managed_program(maple_half_days=_maple_half_days(4))
    problem = SolverProblem.from_instance(instance)
    options = instance.solver.revised(time_limit_seconds=120, random_seed=1)
    first = solve_problem(problem, options=options)
    assert first.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}

    half_day = _busiest_half_day(first)
    week, weekday, session = half_day
    points = instance.clinic_capacity_for_pgy(instance.residents[0].pgy)
    day = clinic_slot_date(instance.calendar.first_week_start, week, weekday)
    maximum = problem.clinic_max_attendings_on("maple", day, session)
    seats = maximum * instance.clinic_policy.site("maple").residents_per_attending
    count = min(len(_sessions_on(first, *half_day)), seats // points)
    assert count > instance.clinic_policy.site("maple").residents_per_attending // points
    locked, chosen = _locked_at_maple(first, half_day, count)

    second = solve_problem(problem, options=options, reference_solution=locked)

    assert second.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    assert second.meta.validation_errors == []
    held = {
        (assignment.resident_id, week, weekday, session): slot.site
        for assignment, slot in _sessions_on(second, *half_day)
    }
    assert {held.get(key) for key in chosen} == {"maple"}
    view = schedule_capacity_view(problem, second)
    assert view.clinic_max_capacity_on("maple", day, session) >= count * points


@pytest.mark.solve
def test_solve_names_the_clinic_when_locks_exceed_all_it_could_ever_seat() -> None:
    from rbs.solver import solve_problem

    first_program = _managed_program()
    problem = SolverProblem.from_instance(first_program)
    options = first_program.solver.revised(time_limit_seconds=30, random_seed=1)
    first = solve_problem(problem, options=options)
    assert first.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    half_day = _busiest_half_day(first)
    week, weekday, session = half_day
    count = len(_sessions_on(first, *half_day))
    points = first_program.clinic_capacity_for_pgy(first_program.residents[0].pgy)
    ratio = first_program.clinic_policy.site("maple").residents_per_attending
    assert count * points > ratio
    locked, _chosen = _locked_at_maple(first, half_day, count)

    # Now only one attending may precept at Maple on that half-day.
    one_preceptor = _managed_program(
        maple_half_days=_maple_half_days(4, {(weekday, session): 1}),
    )

    result = solve_problem(
        SolverProblem.from_instance(one_preceptor), options=options, reference_solution=locked,
    )

    assert result.meta.status not in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    assert any(
        "Maple" in error and "capacity exceeded" in error
        for error in result.meta.validation_errors
    ), result.meta.validation_errors
