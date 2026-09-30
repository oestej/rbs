"""Attendings scheduled by the solve: availability, envelope, weekly pass, output.

The weekly attending pass and the envelope's single-attending limits run
small CP-SAT models with a deterministic work budget, one worker, and a fixed
seed, so their answers do not depend on machine speed. Like site allocation,
they are deterministic post-processing and stay outside the ``solve`` group.
Only the end-to-end test at the bottom runs the block search.
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import date, timedelta

import pytest
from ortools.sat.python import cp_model
from pydantic import ValidationError

from rbs.catalog import blank_instance, sample_instance
from rbs.models.attending import (
    Attending,
    AttendingSchedule,
    AttendingVacation,
    AttendingWeeklyShiftTarget,
    AttendingWeeklyTargetMode,
    AttendingWeeklyWorkSchedule,
    AttendingWorkHalfDay,
    AttendingWorkType,
)
from rbs.models.enums import (
    WEEKDAYS_MF,
    RotationKind,
    Session,
    SolverEngineName,
    SolverStatus,
    Weekday,
)
from rbs.models.instance import SchedulerInput, SolverProblem
from rbs.models.schedule import (
    AssignedAttendingWork,
    AssignedClinic,
    Assignment,
    Schedule,
    ScheduleMeta,
)
from rbs.solver.attending_availability import (
    attending_week_facts,
    potential_attending_coverage,
    schedule_capacity_view,
)
from rbs.solver.core.attending_assignment import _solve_week, schedule_attending_work
from rbs.solver.core.attending_envelope import AttendingEnvelope
from rbs.solver.validation import validate_schedule
from rbs.ui.edit_policy import instance_edit_impact
from rbs.workspaces import InstanceEditImpact

CLINIC = "clinic"
MON, TUE, WED, THU, FRI, SAT = (
    Weekday.MONDAY,
    Weekday.TUESDAY,
    Weekday.WEDNESDAY,
    Weekday.THURSDAY,
    Weekday.FRIDAY,
    Weekday.SATURDAY,
)
AM, PM = Session.MORNING, Session.AFTERNOON
AC = AttendingWorkType.ATTENDING_CLINIC
PC = AttendingWorkType.PRECEPTING_CLINIC
ADMIN = AttendingWorkType.ADMIN_TIME
IS = AttendingWorkType.INPATIENT_SERVICE
FIXED = AttendingWeeklyTargetMode.FIXED
FLEXIBLE = AttendingWeeklyTargetMode.FLEXIBLE


def _target(work_type, minimum, maximum, mode=FLEXIBLE) -> AttendingWeeklyShiftTarget:
    return AttendingWeeklyShiftTarget(
        work_type=work_type,
        minimum_shifts_per_week=minimum,
        maximum_shifts_per_week=maximum,
        mode=mode,
    )


def _work(weekday, session, work_type, clinic_id=None, description=None) -> AttendingWorkHalfDay:
    return AttendingWorkHalfDay(
        weekday=weekday,
        session=session,
        work_type=work_type,
        clinic_id=clinic_id,
        description=description,
    )


def _configured(
    attendings: list[Attending],
    *,
    schedules: list[AttendingSchedule] | None = None,
    managed: bool = True,
    closures: list[date] | None = None,
    grid: list[tuple[Weekday, Session, int]] | None = None,
    exceptions: list[tuple[date, Session, int]] | None = None,
) -> SchedulerInput:
    raw = blank_instance().model_dump(mode="json")
    policy = raw["clinic_policy"]
    # By default four attendings may work the clinic on any weekday half-day.
    policy["sites"][0]["half_days"] = [
        {"weekday": weekday.value, "session": session.value, "attendings": maximum}
        for weekday, session, maximum in (
            grid
            if grid is not None
            else [(weekday, session, 4) for weekday in WEEKDAYS_MF for session in Session]
        )
    ]
    policy["sites"][0]["capacity_overrides"] = [
        {"date": day.isoformat(), "session": session.value, "attendings": maximum}
        for day, session, maximum in exceptions or []
    ]
    if managed:
        policy["sites"][0]["staffing_mode"] = "attending_managed"
    policy["closure_days"] = [
        {"date": closure.isoformat(), "sites": [CLINIC], "name": "Holiday"}
        for closure in closures or []
    ]
    raw["attendings"] = [attending.model_dump(mode="json") for attending in attendings]
    raw["attending_schedules"] = [
        schedule.model_dump(mode="json") for schedule in schedules or []
    ]
    return SchedulerInput.model_validate(raw)


def _problem(attendings: list[Attending], **options) -> SolverProblem:
    return SolverProblem.from_instance(_configured(attendings, **options))


def _first_day() -> date:
    return blank_instance().calendar.first_week_start


def _one_week(attending_id: str, name: str, **fields) -> Attending:
    """An attending whose schedule dates cover only academic week 1."""
    first = _first_day()
    return Attending(
        id=attending_id,
        name=name,
        schedule_start_date=first,
        schedule_end_date=first + timedelta(days=6),
        **fields,
    )


def _schedule(problem: SolverProblem, work=(), assignments=()) -> Schedule:
    return Schedule(
        meta=ScheduleMeta(
            academic_year=problem.academic_year,
            engine=SolverEngineName.CP_SAT,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=list(assignments),
        attending_work=list(work),
    )


def _demand(problem: SolverProblem, residents_by_half_day: dict) -> Schedule:
    """A schedule whose clinic sessions need preceptors at the managed clinic."""
    assignments = []
    for (week, weekday, session), residents in residents_by_half_day.items():
        for index in range(residents):
            assignments.append(
                Assignment(
                    resident_id=f"r-{week}-{weekday.value}-{session.value}-{index}",
                    rotation_id="clinic",
                    kind=RotationKind.CLINIC,
                    start_week=week,
                    end_week=week,
                    weeks=[week],
                    clinic_slots=[
                        AssignedClinic(
                            weekday=weekday,
                            session=session,
                            site=CLINIC,
                            week=week,
                        )
                    ],
                )
            )
    return _schedule(problem, assignments=assignments)


def _counts(work, attending_id, work_type=None) -> int:
    return sum(
        item.attending_id == attending_id
        and (work_type is None or item.work_type is work_type)
        for item in work
    )


# The solver boundary -------------------------------------------------------


def test_attendings_cross_the_solver_boundary_without_their_template() -> None:
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        weekly_shift_targets=[_target(AC, 2, 4)],
        preferred_weekly_schedule_half_days=[_work(MON, AM, AC)],
        schedule_template_half_days=[_work(TUE, AM, ADMIN)],
    )
    configured = _configured([attending])

    payload = SolverProblem.from_instance(configured).model_dump(mode="json")

    [projected] = payload["attendings"]
    assert projected["preferred_weekly_schedule_half_days"]
    assert "schedule_template_half_days" not in projected
    assert payload["attending_schedules"] == []
    assert "attending_coverage" not in payload
    assert payload["clinic_policy"]["academic_half_day_is_attending_admin_time"] is True


def test_attending_rules_make_a_schedule_stale_but_names_and_templates_do_not() -> None:
    attending = Attending(id="attending-001", name="Ada Lovelace")
    configured = _configured([attending])

    renamed = configured.revised(attendings=[attending.revised(name="Grace Hopper")])
    templated = configured.revised(
        attendings=[attending.revised(schedule_template_half_days=[_work(MON, AM, ADMIN)])]
    )
    vacationing = configured.revised(
        attendings=[
            attending.revised(
                vacation_ranges=[
                    AttendingVacation(start_date=_first_day(), end_date=_first_day())
                ]
            )
        ]
    )

    assert instance_edit_impact(configured, renamed) is (
        InstanceEditImpact.COMPATIBLE_CONFIGURATION
    )
    assert instance_edit_impact(configured, templated) is (
        InstanceEditImpact.COMPATIBLE_CONFIGURATION
    )
    assert instance_edit_impact(configured, vacationing) is InstanceEditImpact.SOLVER_INPUT


# Week-by-week availability -------------------------------------------------


def test_week_facts_open_only_half_days_the_solve_may_fill() -> None:
    first = _first_day()
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        half_days_per_week=10,
        vacation_ranges=[
            AttendingVacation(
                start_date=first + timedelta(days=9),
                end_date=first + timedelta(days=11),
            )
        ],
        weekly_shift_targets=[_target(ADMIN, 1, 2)],
        preferred_weekly_schedule_half_days=[_work(SAT, AM, ADMIN)],
    )

    facts = attending_week_facts(_problem([attending]))

    full = facts[1][0]
    assert full.full_week
    assert [item.work_type for item in full.fixed] == [ADMIN]
    assert (WED, PM) not in full.open_half_days
    assert (SAT, AM) in full.open_half_days
    assert (SAT, PM) not in full.open_half_days
    assert len(full.open_half_days) == 10
    assert full.remaining == 9
    assert full.fill_goal == 9

    partial = facts[2][0]
    assert not partial.full_week
    assert {weekday for weekday, _session in partial.open_half_days} == {MON, TUE, SAT}
    # Two of five weekdays remain, so the goal is two-fifths of ten.
    assert partial.fill_goal == 4


def test_week_facts_place_configured_categories_plus_needed_precepting() -> None:
    configured = Attending(
        id="attending-001",
        name="Ada Lovelace",
        weekly_shift_targets=[_target(AC, 2, 4), _target(ADMIN, 1, 1, FIXED)],
        preferred_weekly_schedule_half_days=[_work(MON, AM, IS)],
    )
    no_precepting = configured.revised(
        id="attending-002",
        name="Grace Hopper",
        weekly_shift_targets=[
            *configured.weekly_shift_targets,
            _target(PC, 0, 0, FIXED),
        ],
    )

    managed = attending_week_facts(_problem([configured, no_precepting]))[1]
    unmanaged = attending_week_facts(_problem([configured], managed=False))[1]

    by_id = {facts.attending_id: facts for facts in managed}
    assert by_id["attending-001"].work_types == {AC, ADMIN, IS, PC}
    assert by_id["attending-001"].precepting_clinics == (CLINIC,)
    assert by_id["attending-002"].work_types == {AC, ADMIN, IS}
    assert unmanaged[0].work_types == {AC, ADMIN, IS}


def test_locked_and_special_work_is_kept_and_other_work_only_guides() -> None:
    first = _first_day()
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        weekly_shift_targets=[_target(AC, 2, 6)],
        vacation_ranges=[
            AttendingVacation(
                start_date=first + timedelta(days=9),
                end_date=first + timedelta(days=9),
            )
        ],
    )
    problem = _problem([attending])
    locked = AssignedAttendingWork(
        attending_id=attending.id, week=1, weekday=MON, session=AM, work_type=AC, locked=True
    )
    guide = AssignedAttendingWork(
        attending_id=attending.id, week=1, weekday=TUE, session=AM, work_type=PC,
        clinic_id=CLINIC,
    )
    meeting = AssignedAttendingWork(
        attending_id=attending.id, week=1, weekday=THU, session=PM,
        work_type=AttendingWorkType.SPECIAL_OTHER, description="Board meeting",
    )
    on_vacation = AssignedAttendingWork(
        attending_id=attending.id, week=2, weekday=WED, session=AM, work_type=AC, locked=True
    )

    facts = attending_week_facts(
        problem,
        _schedule(problem, [locked, guide, meeting, on_vacation]),
    )

    week_one = facts[1][0]
    assert (MON, AM) not in week_one.open_half_days
    assert (THU, PM) not in week_one.open_half_days
    assert week_one.locked_work == (locked, meeting)
    assert week_one.reference == {(TUE, AM): guide}
    assert facts[2][0].released_locks == (on_vacation,)


def test_work_inside_the_automatic_lock_window_is_kept() -> None:
    attending = Attending(
        id="attending-001", name="Ada Lovelace", weekly_shift_targets=[_target(AC, 1, 6)]
    )
    problem = _problem([attending]).model_copy(
        update={"clinic_lock_cutoff_date": _first_day() + timedelta(days=1)}
    )
    past = AssignedAttendingWork(
        attending_id=attending.id, week=1, weekday=MON, session=AM, work_type=AC
    )
    unlocked_past = AssignedAttendingWork(
        attending_id=attending.id, week=1, weekday=TUE, session=PM, work_type=AC,
        automatic_lock_exempt=True,
    )
    future = AssignedAttendingWork(
        attending_id=attending.id, week=1, weekday=WED, session=AM, work_type=AC
    )

    facts = attending_week_facts(problem, _schedule(problem, [past, unlocked_past, future]))[1][0]

    assert facts.locked_work == (past,)
    assert set(facts.reference) == {(TUE, PM), (WED, AM)}


# Capacity views ------------------------------------------------------------


def test_potential_coverage_counts_every_attending_who_could_precept() -> None:
    first = _first_day()
    available = Attending(id="attending-001", name="Ada Lovelace")
    away_monday = Attending(
        id="attending-002",
        name="Grace Hopper",
        vacation_ranges=[AttendingVacation(start_date=first, end_date=first)],
    )
    holiday = first + timedelta(days=1)
    problem = _problem([available, away_monday], closures=[holiday])

    coverage = {
        (item.date, item.session): item.attendings
        for item in potential_attending_coverage(problem)
    }

    assert coverage[first, AM] == 1
    assert coverage[first + timedelta(days=3), AM] == 2
    assert (holiday, AM) not in coverage
    # The automatic academic Admin Time is never precepting time.
    assert (first + timedelta(days=2), PM) not in coverage


def test_schedule_view_counts_scheduled_precepting_where_it_can_happen() -> None:
    first = _first_day()
    hand_entered = Attending(id="attending-001", name="Ada Lovelace")
    scheduled = Attending(
        id="attending-002",
        name="Grace Hopper",
        vacation_ranges=[
            AttendingVacation(
                start_date=first + timedelta(days=2),
                end_date=first + timedelta(days=2),
            )
        ],
    )
    problem = _problem(
        [hand_entered, scheduled],
        schedules=[
            AttendingSchedule(
                attending_id=hand_entered.id,
                weeks=[
                    AttendingWeeklyWorkSchedule(
                        week=1, half_days=[_work(MON, AM, PC, CLINIC)]
                    )
                ],
            )
        ],
    )

    def precepting(attending, weekday, session) -> AssignedAttendingWork:
        return AssignedAttendingWork(
            attending_id=attending.id,
            week=1,
            weekday=weekday,
            session=session,
            work_type=PC,
            clinic_id=CLINIC,
        )

    schedule = _schedule(
        problem,
        [
            precepting(hand_entered, MON, AM),  # under hand-entered work
            precepting(scheduled, TUE, AM),
            precepting(scheduled, WED, AM),  # vacation
        ],
    )

    view = schedule_capacity_view(problem, schedule)

    assert view.clinic_attending_count_on(CLINIC, first, AM) == 1
    assert view.clinic_attending_count_on(CLINIC, first + timedelta(days=1), AM) == 1
    assert view.clinic_attending_count_on(CLINIC, first + timedelta(days=2), AM) == 0
    assert schedule_capacity_view(problem, _schedule(problem)) is problem

    # The same count the calendar would show: every effective week, with the
    # schedule's work filling what hand-entered work leaves open.
    grouped = problem.scheduled_attending_work(schedule)
    assert view.attending_coverage == problem.attending_coverage_for(
        problem.effective_attending_week(
            attending, week, scheduled_work=grouped.get((attending.id, week), ()),
        )
        for attending in problem.attendings
        for week in range(1, problem.calendar.weeks + 1)
    )
    # Views are built on demand, never kept between calls.
    assert schedule_capacity_view(problem, schedule) is not view


# Clinic maximums -----------------------------------------------------------


@pytest.mark.parametrize("managed", [True, False])
def test_no_clinic_half_day_gets_more_preceptors_than_its_maximum(managed: bool) -> None:
    first = _first_day()
    attendings = [
        _one_week(
            f"attending-00{index}",
            f"Attending {index}",
            half_days_per_week=2,
            weekly_shift_targets=[_target(PC, 1, 1, FIXED)],
            preferred_weekly_schedule_half_days=[_work(TUE, AM, PC, CLINIC)],
        )
        for index in range(1, 4)
    ]
    problem = _problem(
        attendings,
        managed=managed,
        grid=[(TUE, AM, 1), (THU, AM, 2), (FRI, AM, 1)],
        # A dated exception replaces Thursday's usual maximum for one day.
        exceptions=[(first + timedelta(days=3), AM, 1)],
    )

    result = schedule_attending_work(
        problem, _schedule(problem), attending_week_facts(problem), cp_model
    )

    precepting = Counter(
        (item.weekday, item.session) for item in result.work if item.work_type is PC
    )
    assert precepting == {(TUE, AM): 1, (THU, AM): 1, (FRI, AM): 1}
    assert result.relaxed_weeks == ()


def test_precepting_capacity_stops_at_the_clinic_maximum() -> None:
    attendings = [
        Attending(id=f"attending-00{index}", name=f"Attending {index}")
        for index in range(1, 4)
    ]
    problem = _problem(attendings, grid=[(TUE, AM, 2)])
    tuesday = _first_day() + timedelta(days=1)

    potential = {
        (item.date, item.session): item.attendings
        for item in potential_attending_coverage(problem)
    }
    model = cp_model.CpModel()
    envelope = AttendingEnvelope(problem, attending_week_facts(problem), model, cp_model)
    [handle] = envelope.week_preceptors(1, {(TUE, AM), (MON, AM)})[TUE, AM]

    assert potential[tuesday, AM] == 2
    assert (tuesday - timedelta(days=1), AM) not in potential
    assert handle.generated_upper_bound == 2


def test_validation_rejects_precepting_beyond_a_clinics_maximum() -> None:
    attendings = [
        Attending(id=f"attending-00{index}", name=f"Attending {index}")
        for index in range(1, 3)
    ]
    problem = _problem(attendings, grid=[(TUE, AM, 1)])
    schedule = _schedule(
        problem,
        [
            AssignedAttendingWork(
                attending_id=attending.id,
                week=1,
                weekday=TUE,
                session=AM,
                work_type=PC,
                clinic_id=CLINIC,
            )
            for attending in attendings
        ],
    )

    errors = validate_schedule(problem, schedule).errors

    assert any("2 attendings precept, but at most 1 may" in error for error in errors)


def test_work_kept_by_hand_may_exceed_a_maximum_the_solve_still_respects() -> None:
    attendings = [
        _one_week(
            f"attending-00{index}",
            f"Attending {index}",
            half_days_per_week=1,
            preferred_weekly_schedule_half_days=[_work(TUE, AM, PC, CLINIC)],
        )
        for index in range(1, 4)
    ]
    # Attending 1 precepts on Tuesday by hand and attending 2 by a lock, so
    # the clinic's maximum of one is already overridden.
    problem = _problem(
        attendings,
        grid=[(TUE, AM, 1)],
        schedules=[
            AttendingSchedule(
                attending_id="attending-001",
                weeks=[AttendingWeeklyWorkSchedule(week=1, half_days=[_work(TUE, AM, PC, CLINIC)])],
            )
        ],
    )
    locked = AssignedAttendingWork(
        attending_id="attending-002", week=1, weekday=TUE, session=AM,
        work_type=PC, clinic_id=CLINIC, locked=True, manual_override=True,
    )
    demand = _demand(problem, {(1, TUE, AM): 1})

    result = schedule_attending_work(
        problem, demand, attending_week_facts(problem, _schedule(problem, [locked])), cp_model,
    )

    # The solve keeps the override and adds no third preceptor beside it.
    assert [
        item for item in result.work
        if item.work_type is PC and (item.weekday, item.session) == (TUE, AM)
    ] == [locked]
    # Validate the attending work alone; the demand's residents are placeholders.
    solved = _schedule(problem, result.work)

    def maximum_errors(schedule: Schedule) -> list[str]:
        errors = validate_schedule(problem, schedule).errors
        return [error for error in errors if "at most" in error]

    assert maximum_errors(solved) == []
    # A solve that placed one more there would still be rejected.
    extra = AssignedAttendingWork(
        attending_id="attending-003", week=1, weekday=TUE, session=AM,
        work_type=PC, clinic_id=CLINIC,
    )
    over = solved.revised(attending_work=[*solved.attending_work, extra])
    assert any("3 attendings precept, but at most 1 may" in error for error in maximum_errors(over))
    # A working draft may hold hand edits over the maximum until the next solve.
    draft = over.revised(meta=over.meta.revised(
        status=SolverStatus.UNKNOWN, solver_status=SolverStatus.UNKNOWN,
    ))
    assert maximum_errors(draft) == []


# Output validation ---------------------------------------------------------


def test_schedule_orders_attending_work_and_rejects_a_doubled_half_day() -> None:
    problem = _problem([Attending(id="attending-001", name="Ada Lovelace")])
    later = AssignedAttendingWork(
        attending_id="attending-001", week=2, weekday=MON, session=AM, work_type=AC
    )
    earlier = later.model_copy(update={"week": 1})

    ordered = _schedule(problem, [later, earlier])

    assert [item.week for item in ordered.attending_work] == [1, 2]
    with pytest.raises(ValidationError, match="at most once"):
        _schedule(problem, [later, later.model_copy(update={"work_type": ADMIN})])


def test_validation_rejects_attending_work_that_could_never_be_scheduled() -> None:
    first = _first_day()
    attending = Attending(
        id="attending-001",
        name="Ada Lovelace",
        vacation_ranges=[AttendingVacation(start_date=first, end_date=first)],
    )
    problem = _problem([attending])

    def work(**fields) -> AssignedAttendingWork:
        values = {
            "attending_id": attending.id,
            "week": 1,
            "weekday": TUE,
            "session": AM,
            "work_type": AC,
            **fields,
        }
        return AssignedAttendingWork(**values)

    result = validate_schedule(
        problem,
        _schedule(
            problem,
            [
                work(attending_id="attending-404"),
                work(weekday=MON),
                work(weekday=WED, session=PM),
                work(weekday=THU),
            ],
        ),
    )

    assert len(result.errors) == 3
    assert any("unknown attending 'attending-404'" in error for error in result.errors)
    assert any("on vacation" in error for error in result.errors)
    assert any("Admin Time" in error for error in result.errors)


# The weekly attending pass -------------------------------------------------


def test_weekly_pass_covers_residents_and_keeps_every_required_rule() -> None:
    clinic_lead = _one_week(
        "attending-001",
        "Ada Lovelace",
        weekly_shift_targets=[
            _target(AC, 3, 5, FIXED),
            _target(PC, 0, 4),
            _target(ADMIN, 1, 2, FIXED),
        ],
        minimum_attending_clinic_days_per_week=3,
    )
    preceptor = _one_week(
        "attending-002",
        "Grace Hopper",
        half_days_per_week=6,
        weekly_shift_targets=[_target(PC, 2, 3, FIXED), _target(ADMIN, 1, 2)],
    )
    hospitalist = _one_week(
        "attending-003",
        "Katherine Johnson",
        weekly_shift_targets=[
            _target(IS, 2, 2, FIXED),
            _target(AC, 4, 7),
            _target(PC, 0, 3),
        ],
    )
    problem = _problem([clinic_lead, preceptor, hospitalist])
    demand = {(1, MON, AM): 5, (1, TUE, PM): 3, (1, THU, AM): 8}

    result = schedule_attending_work(
        problem,
        _demand(problem, demand),
        attending_week_facts(problem),
        cp_model,
    )

    work = result.work
    for (week, weekday, session), residents in demand.items():
        preceptors = sum(
            item.week == week
            and item.weekday is weekday
            and item.session is session
            and item.work_type is PC
            for item in work
        )
        assert preceptors * 4 >= residents
    assert result.relaxed_weeks == ()
    assert result.uncovered == ()
    # Each weekly total is met exactly, beside the academic Admin Time.
    assert _counts(work, clinic_lead.id) == 9
    assert _counts(work, preceptor.id) == 5
    assert _counts(work, hospitalist.id) == 9
    assert 3 <= _counts(work, clinic_lead.id, AC) <= 5
    assert len({item.weekday for item in work if item.attending_id == clinic_lead.id
                and item.work_type is AC}) >= 3
    assert 2 <= _counts(work, preceptor.id, PC) <= 3
    assert _counts(work, hospitalist.id, IS) == 2


def test_weekly_pass_never_books_a_preceptor_without_residents() -> None:
    keen = _one_week(
        "attending-001",
        "Ada Lovelace",
        weekly_shift_targets=[_target(PC, 3, 5), _target(AC, 0, 9)],
    )
    contracted = _one_week(
        "attending-002",
        "Grace Hopper",
        weekly_shift_targets=[_target(PC, 2, 2, FIXED), _target(AC, 0, 9)],
    )
    problem = _problem([keen, contracted])

    result = schedule_attending_work(
        problem, _schedule(problem), attending_week_facts(problem), cp_model
    )

    # A Flexible range is only a preference; a Fixed one is a requirement.
    assert _counts(result.work, keen.id, PC) == 0
    assert _counts(result.work, contracted.id, PC) == 2


def test_weekly_pass_keeps_previous_work_and_honors_preferences() -> None:
    attending = _one_week(
        "attending-001",
        "Ada Lovelace",
        half_days_per_week=4,
        weekly_shift_targets=[_target(AC, 0, 4), _target(ADMIN, 0, 4)],
        preferred_weekly_schedule_half_days=[_work(MON, AM, AC)],
    )
    problem = _problem([attending])
    previous = AssignedAttendingWork(
        attending_id=attending.id, week=1, weekday=FRI, session=PM, work_type=ADMIN
    )

    result = schedule_attending_work(
        problem,
        _schedule(problem),
        attending_week_facts(problem, _schedule(problem, [previous])),
        cp_model,
    )

    placed = {(item.weekday, item.session, item.work_type) for item in result.work}
    assert (FRI, PM, ADMIN) in placed
    assert (MON, AM, AC) in placed


def test_weekly_pass_preserves_an_explicit_past_date_unlock_when_work_stays_put() -> None:
    from rbs.ui.attendings.ops import set_attending_work_locked

    attending = _one_week(
        "attending-001", "Ada Lovelace", half_days_per_week=2,
        weekly_shift_targets=[_target(AC, 1, 1, FIXED)],
    )
    instance = _configured([attending], managed=False).revised(lock_through_today=True)
    previous = AssignedAttendingWork(
        attending_id=attending.id, week=1, weekday=MON, session=AM, work_type=AC,
    )
    today = _first_day() + timedelta(days=6)
    instance, reference = set_attending_work_locked(
        instance, _schedule(SolverProblem.from_instance(instance), [previous]), attending.id,
        week=1, weekday=MON, session=AM, locked=False, today=today,
    )
    problem = SolverProblem.from_instance(instance)
    result = schedule_attending_work(
        problem, _schedule(problem),
        attending_week_facts(problem, reference, today=today), cp_model,
    )
    [kept] = result.work
    assert kept.key == previous.key
    assert kept.automatic_lock_exempt and not kept.locked
    solved = _schedule(problem, result.work)
    facts = attending_week_facts(problem, solved, today=today)[1][0]
    assert kept not in facts.locked_work
    assert facts.reference[MON, AM] == kept


def test_weekly_pass_reports_a_week_whose_required_rules_contradict() -> None:
    attending = _one_week(
        "attending-001",
        "Ada Lovelace",
        half_days_per_week=4,
        weekly_shift_targets=[_target(AC, 4, 4, FIXED)],
        minimum_attending_clinic_days_per_week=4,
    )
    problem = _problem([attending])

    result = schedule_attending_work(
        problem, _schedule(problem), attending_week_facts(problem), cp_model
    )

    # Four Attending Clinic half-days on four different days need four open
    # days, but the academic Admin Time already uses one of four half-days.
    assert result.relaxed_weeks == (1,)
    assert _counts(result.work, attending.id) == 3


def _random_attending(rng: random.Random, index: int) -> Attending:
    first = _first_day()
    targets = []
    for work_type in (PC, IS, ADMIN, AC):
        if rng.random() < 0.45:
            minimum = rng.randint(0, 3)
            targets.append(
                _target(
                    work_type,
                    minimum,
                    minimum + rng.randint(0, 3),
                    rng.choice([FIXED, FLEXIBLE]),
                )
            )
    vacation = []
    if rng.random() < 0.35:
        start = first + timedelta(days=rng.randint(0, 4))
        vacation.append(
            AttendingVacation(
                start_date=start,
                end_date=start + timedelta(days=rng.randint(0, 2)),
            )
        )
    total = rng.choice([10, 10, 8, 6, 4])
    return _one_week(
        f"attending-{index:03d}",
        f"Attending {index:03d}",
        half_days_per_week=total,
        weekly_shift_targets=targets,
        minimum_attending_clinic_days_per_week=min(rng.choice([0, 0, 1, 2, 3]), total),
        vacation_ranges=vacation,
    )


def test_precepting_envelope_never_promises_more_than_the_weekly_pass_can_staff() -> None:
    """Push demand to the envelope's limit and staff it with no rule broken.

    This is the guarantee that lets the block solve reason about classes of
    interchangeable attendings instead of individuals: whatever preceptor
    counts the envelope allows, the weekly pass can name attendings for
    without relaxing any required rule.
    """
    rng = random.Random(20260929)
    checked = 0
    for _trial in range(90):
        attendings = []
        for index in range(rng.choice([3, 4, 5])):
            try:
                attendings.append(_random_attending(rng, index))
            except ValidationError:
                continue
        problem = _problem(attendings)
        facts = attending_week_facts(problem).get(1, [])
        model = cp_model.CpModel()
        envelope = AttendingEnvelope(problem, {1: facts}, model, cp_model)
        preceptors = envelope.week_preceptors(
            1,
            {(weekday, session) for weekday in WEEKDAYS_MF for session in Session},
        )
        variables = [
            handle.generated
            for handles in preceptors.values()
            for handle in handles
            if handle.generated is not None
        ]
        _placed, contradictory, _uncovered = _solve_week(problem, 1, facts, {}, cp_model)
        if not variables or contradictory:
            # A setup that breaks its own rules even with no residents belongs
            # to the relaxed path tested above; here only coverage may matter.
            continue
        model.Maximize(sum(rng.randint(1, 9) * variable for variable in variables))
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 0
        assert solver.Solve(model) in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        need = {
            (CLINIC, weekday, session): solver.Value(handle.generated)
            for (weekday, session), handles in preceptors.items()
            for handle in handles
            if handle.generated is not None and solver.Value(handle.generated) > 0
        }

        _work_placed, violated, uncovered = _solve_week(problem, 1, facts, need, cp_model)

        assert uncovered == {}
        assert not violated
        checked += 1
    assert checked >= 20


# The block model -----------------------------------------------------------


def _managed_sample(attendings: int) -> SolverProblem:
    raw = sample_instance().model_dump(mode="json")
    for site in raw["clinic_policy"]["sites"]:
        if site["id"] == "maple":
            site["staffing_mode"] = "attending_managed"
    raw["attendings"] = [
        Attending(
            id=f"attending-{index:03d}",
            name=f"Attending {index:03d}",
            weekly_shift_targets=[
                _target(AC, 3, 6),
                _target(PC, 1, 4),
                _target(ADMIN, 1, 2, FIXED),
            ],
            minimum_attending_clinic_days_per_week=2,
        ).model_dump(mode="json")
        for index in range(attendings)
    ]
    raw["attending_schedules"] = []
    return SolverProblem.from_instance(SchedulerInput.model_validate(raw))


def test_compile_reuses_the_facts_and_readiness_a_solve_already_built(monkeypatch) -> None:
    from rbs.solver.core import compile as compile_module
    from rbs.solver.core.compile import compile_problem
    from rbs.solver.readiness import check_solve_readiness

    problem = _managed_sample(3)
    facts = attending_week_facts(problem)
    readiness = check_solve_readiness(problem)

    def recomputed(*_args, **_kwargs):
        raise AssertionError("compile recomputed what the solve passed in")

    monkeypatch.setattr(compile_module, "attending_week_facts", recomputed)
    monkeypatch.setattr(compile_module, "check_solve_readiness", recomputed)

    compiled = compile_problem(
        problem, sample_instance().solver, cp_model,
        attending_facts=facts, readiness=readiness,
    )
    assert compiled.context.attending_envelope.facts_by_week is facts


def test_block_model_grows_with_attending_setups_not_headcount() -> None:
    from rbs.solver.core.compile import compile_problem

    small = compile_problem(_managed_sample(3), sample_instance().solver, cp_model)
    large = compile_problem(_managed_sample(12), sample_instance().solver, cp_model)

    assert small.context.attending_envelope.generated
    assert len(small.context.model.Proto().variables) == len(
        large.context.model.Proto().variables
    )


@pytest.mark.solve
def test_solve_schedules_attendings_who_cover_an_attending_managed_clinic() -> None:
    from rbs.solver import solve_problem

    problem = _managed_sample(6)
    options = sample_instance().solver.model_copy(
        update={"time_limit_seconds": 30, "random_seed": 1}
    )

    schedule = solve_problem(problem, options=options)

    assert schedule.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    assert schedule.meta.validation_errors == []
    assert schedule.attending_work
    assert schedule.meta.metrics.attending_precepting_half_days > 0
    assert not any(
        "Maple target shortfall" in warning for warning in schedule.meta.validation_warnings
    )
    # Validation already proves coverage; restate it for the managed clinic.
    view = schedule_capacity_view(problem, schedule)
    for assignment in schedule.assignments:
        for slot in assignment.clinic_slots:
            if slot.site == "maple" and not slot.admin:
                day = problem.calendar.first_week_start + timedelta(
                    weeks=slot.week - 1, days=list(Weekday).index(slot.weekday)
                )
                assert view.clinic_max_capacity_on("maple", day, slot.session) > 0


# Search budgets and rule parity ---------------------------------------------


class _ScriptedCpModel:
    """A stand-in for ``cp_model`` whose solver returns scripted statuses."""

    OPTIMAL, FEASIBLE, INFEASIBLE, MODEL_INVALID, UNKNOWN = range(5)

    def __init__(self, statuses) -> None:
        self.statuses = list(statuses)
        self.budgets: list[float] = []

    def CpSolver(self):  # noqa: N802 - mirrors the OR-Tools name
        scripted = self

        class _Solver:
            def __init__(self) -> None:
                self.parameters = type("Parameters", (), {})()

            def Solve(self, _model):  # noqa: N802
                scripted.budgets.append(self.parameters.max_deterministic_time)
                return scripted.statuses.pop(0)

            def ObjectiveValue(self):  # noqa: N802
                return 0

            def Value(self, _variable):  # noqa: N802
                return 0

        return _Solver()


class _Model:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


@pytest.mark.parametrize(
    ("statuses", "found", "budgets"),
    [
        # A first search that only ran out of budget is retried with more.
        (["UNKNOWN", "FEASIBLE"], True, [0.1, 1.0]),
        (["UNKNOWN", "UNKNOWN", "OPTIMAL"], True, [0.1, 1.0, 10.0]),
        # Proof that nothing exists ends the search at once.
        (["INFEASIBLE"], False, [0.1]),
        (["UNKNOWN", "UNKNOWN", "UNKNOWN"], False, [0.1, 1.0, 10.0]),
    ],
)
def test_tiered_search_does_not_mistake_a_spent_budget_for_infeasibility(
    statuses, found, budgets,
) -> None:
    from rbs.solver.core.lexicographic import solve_in_tiers

    scripted = _ScriptedCpModel([getattr(_ScriptedCpModel, name) for name in statuses])
    solver = solve_in_tiers(_Model(), [[1]], [], scripted)

    assert (solver is not None) is found
    assert scripted.budgets == pytest.approx(budgets)


def test_a_week_the_search_cannot_finish_is_reported_instead_of_aborting(monkeypatch) -> None:
    from rbs.solver.core import attending_assignment

    attending = _one_week("attending-001", "Ada Lovelace", weekly_shift_targets=[_target(PC, 0, 4)])
    problem = _problem([attending])
    facts = attending_week_facts(problem)[1]
    need = {(CLINIC, MON, AM): 1}
    monkeypatch.setattr(attending_assignment, "_build_and_solve", lambda *_a, **_k: None)

    work, violated, short = _solve_week(problem, 1, facts, need, cp_model)

    assert work == [] and violated and short == need


def test_attending_clinic_day_minimum_holds_in_a_week_with_only_weekend_vacation() -> None:
    from rbs.attending_schedule import attending_schedule_report

    first = _first_day()
    # Ada prefers Inpatient Service everywhere, so only the clinic-day rule
    # makes the weekly pass place Attending Clinic on three different days.
    attending = _one_week(
        "attending-001",
        "Ada Lovelace",
        weekly_shift_targets=[_target(AC, 0, 3), _target(IS, 0, 10)],
        minimum_attending_clinic_days_per_week=3,
        preferred_weekly_schedule_half_days=[
            _work(weekday, session, IS) for weekday in WEEKDAYS_MF for session in Session
        ],
        vacation_ranges=[AttendingVacation(
            start_date=first + timedelta(days=5), end_date=first + timedelta(days=5),
        )],
    )
    instance = _configured([attending], managed=False)
    problem = SolverProblem.from_instance(instance)
    (facts,) = attending_week_facts(problem)[1]
    assert not facts.full_week and facts.clinic_day_minimum == 3

    result = schedule_attending_work(problem, _schedule(problem), {1: [facts]}, cp_model)

    clinic_days = {item.weekday for item in result.work if item.work_type is AC}
    assert len(clinic_days) >= 3
    assert result.relaxed_weeks == ()
    report = attending_schedule_report(instance, schedule=_schedule(problem, result.work))
    assert not [issue for issue in report.errors if issue.code == "attending_clinic_day_minimum"]


def test_a_draft_holding_only_locked_attending_work_still_guides_the_solve() -> None:
    from rbs.solver.core.cp_sat import _compatible_reference

    attending = _one_week("attending-001", "Ada Lovelace")
    problem = _problem([attending])
    locked = AssignedAttendingWork(
        attending_id=attending.id, week=1, weekday=MON, session=AM,
        work_type=ADMIN, locked=True, manual_override=True, hand_entered=True,
    )
    draft = _schedule(problem, work=[locked])

    assert draft.is_empty()
    assert _compatible_reference(problem, draft) is draft
    assert _compatible_reference(problem, _schedule(problem)) is None


@pytest.mark.solve
def test_attending_only_solve_preserves_locked_work_and_fills_open_half_days() -> None:
    from rbs.solver import solve_problem

    attending = _one_week(
        "attending-001", "Ada Lovelace", half_days_per_week=4,
        weekly_shift_targets=[_target(AC, 2, 2)],
    )
    problem = _problem([attending])
    locked = AssignedAttendingWork(
        attending_id=attending.id, week=1, weekday=MON, session=AM,
        work_type=ADMIN, locked=True, manual_override=True, hand_entered=True,
    )
    draft = _schedule(problem, work=[locked])
    options = blank_instance().solver.revised(
        time_limit_seconds=5, num_workers=1, solve_attempts=1,
    )

    schedule = solve_problem(problem, options=options, reference_solution=draft)

    assert problem.residents == []
    assert schedule.assignments == []
    assert schedule.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    assert schedule.meta.validation_errors == []
    assert locked in schedule.attending_work
    assert _counts(schedule.attending_work, attending.id, AC) == 2
    assert validate_schedule(problem, schedule).valid
