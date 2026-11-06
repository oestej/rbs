"""Temporary conflicts must not consume protected attending input or its lock."""

from datetime import timedelta
from time import perf_counter

import pytest

from rbs.attending_schedule import (
    effective_attending_schedule_week,
    preserved_attending_work_issues,
)
from rbs.catalog import blank_instance
from rbs.models.attending import (
    Attending,
    AttendingVacation,
    AttendingWeeklyShiftTarget,
    AttendingWorkHalfDay,
    AttendingWorkType,
)
from rbs.models.clinic import ClinicStaffingMode
from rbs.models.enums import Session, SolverEngineName, SolverStatus, Weekday
from rbs.models.instance import SolverProblem
from rbs.models.schedule import AssignedAttendingWork, Schedule, ScheduleMeta
from rbs.solver.attending_availability import attending_week_facts, potential_attending_coverage
from rbs.solver.core.cp_sat import _with_attending_work
from rbs.solver.validation import validate_schedule
from rbs.store import Store
from rbs.ui.attendings.ops import set_attending_work_half_day, set_attending_work_locked
from rbs.workspaces import WorkspaceController


def _setup(hand_entered=True, precepting=False):
    instance = blank_instance()
    first = instance.calendar.first_week_start
    person = Attending(
        id="one",
        name="Ada Lovelace",
        half_days_per_week=2,
        schedule_start_date=first,
        schedule_end_date=first + timedelta(days=6),
        eligible_clinic_ids=[instance.clinic_policy.primary_site_id] if precepting else [],
        weekly_shift_targets=[
            AttendingWeeklyShiftTarget(
                work_type=AttendingWorkType.ADMIN_TIME,
                minimum_shifts_per_week=0,
                maximum_shifts_per_week=2,
            )
        ],
    )
    policy = (
        instance.clinic_policy.revised(
            sites=[
                site.revised(staffing_mode=ClinicStaffingMode.ATTENDING_MANAGED)
                for site in instance.clinic_policy.sites
            ]
        )
        if precepting
        else instance.clinic_policy
    )
    instance = instance.revised(attendings=[person], clinic_policy=policy)
    assignment = AttendingWorkHalfDay(
        weekday=Weekday.MONDAY,
        session=Session.MORNING,
        work_type=AttendingWorkType.PRECEPTING_CLINIC
        if precepting
        else AttendingWorkType.ADMIN_TIME,
        clinic_id=instance.clinic_policy.primary_site_id if precepting else None,
    )
    if hand_entered:
        instance = set_attending_work_half_day(
            instance,
            person.id,
            week=1,
            weekday=assignment.weekday,
            session=assignment.session,
            assignment=assignment,
        )
        instance, draft = set_attending_work_locked(
            instance,
            None,
            person.id,
            week=1,
            weekday=assignment.weekday,
            session=assignment.session,
            locked=True,
            today=first - timedelta(days=1),
        )
    else:
        draft = _empty(instance).revised(
            attending_work=[
                AssignedAttendingWork(
                    attending_id=person.id,
                    week=1,
                    **assignment.model_dump(),
                    locked=True,
                )
            ]
        )
    return instance, draft


def _empty(instance):
    return Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.CP_SAT,
            status=SolverStatus.FEASIBLE,
        )
    )


def _resolve(instance, reference):
    problem = SolverProblem.from_instance(instance)
    return _with_attending_work(
        problem,
        _empty(problem),
        attending_week_facts(problem, reference),
        options=instance.solver.revised(num_workers=1, time_limit_seconds=5),
        deadline=perf_counter() + 5,
    )


def _mask(instance, kind):
    person = instance.attendings[0]
    first = instance.calendar.first_week_start
    if kind == "vacation":
        person = person.revised(
            vacation_ranges=[AttendingVacation(start_date=first, end_date=first)]
        )
    else:
        person = person.revised(
            schedule_start_date=first + timedelta(days=7),
            schedule_end_date=first + timedelta(days=13),
        )
    return instance.revised(attendings=[person])


@pytest.mark.parametrize("hand_entered", [True, False], ids=["hand-entered", "solver-placed"])
@pytest.mark.parametrize("conflict", ["vacation", "schedule dates"])
def test_masked_work_and_lock_survive_solves_file_round_trip_and_restoration(
    tmp_path,
    hand_entered,
    conflict,
):
    original, reference = _setup(hand_entered)
    locked = reference.attending_work[0]
    masked = _mask(original, conflict)
    solved = _resolve(masked, reference)
    assert validate_schedule(SolverProblem.from_instance(masked), solved).valid
    assert locked in solved.attending_work
    assert any("preserved while unavailable" in note for note in solved.meta.notes)
    assert (
        effective_attending_schedule_week(masked, masked.attendings[0], 1, solved).assignment_on(
            Weekday.MONDAY,
            Session.MORNING,
        )
        is None
    )
    issues = preserved_attending_work_issues(masked, solved)
    assert len(issues) == 1 and conflict in issues[0].message
    assert "Ada Lovelace" in issues[0].message and "AM" in issues[0].message
    store = Store(tmp_path / "roundtrip.sqlite")
    store.init()
    controller = WorkspaceController(store)
    workspace = controller.save_schedule(store.create("Preserved", masked), solved)
    imported = store.import_workspace_rbsc(store.export_workspace_rbsc(workspace.id))[0]
    assert locked in imported.schedule.attending_work
    restored = controller.save_instance(imported, original)
    assert (
        effective_attending_schedule_week(
            original,
            original.attendings[0],
            1,
            restored.latest_schedule,
        ).assignment_on(Weekday.MONDAY, Session.MORNING)
        is not None
    )
    resolved = _resolve(original, restored.latest_schedule)
    assert locked in resolved.attending_work
    assert not preserved_attending_work_issues(original, resolved)


def test_hidden_precepting_does_not_supply_capacity_or_count_in_metrics():
    instance, reference = _setup(precepting=True)
    instance = _mask(instance, "vacation")
    problem = SolverProblem.from_instance(instance)
    facts = attending_week_facts(problem, reference)
    first = instance.calendar.first_week_start
    assert not any(item.date == first for item in potential_attending_coverage(problem, facts))
    solved = _resolve(instance, reference)
    assert reference.attending_work[0] in solved.attending_work
    assert solved.meta.metrics.attending_precepting_half_days == 0
    assert validate_schedule(problem, solved).valid


@pytest.mark.solve
def test_preservation_crosses_the_public_solver_process_boundary():
    from rbs.solver import solve_problem

    original, reference = _setup()
    masked = _mask(original, "vacation")
    options = original.solver.revised(num_workers=1, time_limit_seconds=5, solve_attempts=1)
    solved = solve_problem(
        SolverProblem.from_instance(masked), options=options, reference_solution=reference
    )
    assert solved.meta.status in {SolverStatus.FEASIBLE, SolverStatus.OPTIMAL}
    assert reference.attending_work[0] in solved.attending_work
    restored = solve_problem(
        SolverProblem.from_instance(original), options=options, reference_solution=solved
    )
    assert reference.attending_work[0] in restored.attending_work
    assert not preserved_attending_work_issues(original, restored)
