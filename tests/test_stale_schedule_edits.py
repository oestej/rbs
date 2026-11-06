"""Editing a schedule whose inputs have moved on, and carrying it in a file.

An input edit leaves the prior schedule out of date. The user can still edit it
directly, and the result is a working draft. Work that only stopped fitting stays
in that draft, hidden until the next solve, so undoing the input edit brings it
back and a lock that no longer fits still fails that solve by name. The draft
gives up only what saved data can no longer point at, and says so. Work an edit
adds is still held to every rule. A saved document likewise opens whatever the
application-owned locking setting says.

The weekly attending pass is deterministic post-processing, so only the
end-to-end test at the bottom runs the block search.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from ortools.sat.python import cp_model

from rbs.catalog import blank_instance, sample_instance
from rbs.clinic_locks import locked_clinic_states
from rbs.models.attending import (
    Attending,
    AttendingVacation,
    AttendingWorkHalfDay,
    AttendingWorkType,
)
from rbs.models.enums import Session, SolverEngineName, SolverStatus, Weekday
from rbs.models.instance import SchedulerInput, SolverProblem
from rbs.models.schedule import (
    AssignedAttendingWork,
    AssignedClinic,
    Assignment,
    Schedule,
    ScheduleMeta,
)
from rbs.solver.attending_availability import attending_week_facts
from rbs.solver.core.attending_assignment import schedule_attending_work
from rbs.solver.validation import validate_schedule
from rbs.solver.working_draft import LeftOutOfDraft, left_out_of_draft, working_draft_for_inputs
from rbs.store import Store
from rbs.ui.attendings.ops import (
    remove_attending,
    set_attending_schedule_half_day,
    set_attending_work_locked,
)
from rbs.ui.clinic.ops import remove_clinic
from rbs.ui.clinic.projection import occupancy
from rbs.ui.locks import ScheduleBlock, clear_schedule_block
from rbs.ui.residents.ops import add_resident_clinic_slot
from rbs.ui.session import WorkspaceSession
from rbs.workspaces import InstanceEditImpact, WorkspaceController

MON, TUE, THU, FRI = Weekday.MONDAY, Weekday.TUESDAY, Weekday.THURSDAY, Weekday.FRIDAY
AM = Session.MORNING
PC = AttendingWorkType.PRECEPTING_CLINIC
AC = AttendingWorkType.ATTENDING_CLINIC
ADMIN = AttendingWorkType.ADMIN_TIME
CLINIC = "clinic"


def _store(tmp_path, name: str = "desk.sqlite") -> Store:
    store = Store(tmp_path / name)
    store.init()
    return store


# Direct edits on an out-of-date schedule -----------------------------------


ATTENDING_ID = "attending-003"
EDITED, KEPT, GONE = "resident-002", "resident-003", "resident-004"


def _duration(instance: SchedulerInput, resident_id: str) -> int:
    resident = instance.residents_by_id[resident_id]
    return instance.rotations_by_id["fmed"].pgy_rule(resident.pgy).block_configs[0].duration_weeks


def _open_start(instance: SchedulerInput, resident_id: str) -> int:
    """The first week a no-vacation block fits this sample resident's time away."""
    duration = _duration(instance, resident_id)
    away = instance.resident_scheduling_vacation_weeks(resident_id)
    return next(
        start for start in range(1, 40) if not set(range(start, start + duration)) & away
    )


def _block(instance: SchedulerInput, resident_id: str, slots=()) -> Assignment:
    rotation = instance.rotations_by_id["fmed"]
    duration = _duration(instance, resident_id)
    start = _open_start(instance, resident_id)
    return Assignment(
        resident_id=resident_id,
        rotation_id="fmed",
        kind=rotation.kind,
        start_week=start,
        end_week=start + duration - 1,
        weeks=list(range(start, start + duration)),
        block_start_week=start,
        block_duration_weeks=duration,
        clinic_slots=list(slots),
    )


def _session(week: int, weekday: Weekday, site: str, **fields) -> AssignedClinic:
    return AssignedClinic(weekday=weekday, session=AM, site=site, week=week, **fields)


def _program() -> SchedulerInput:
    """The sample program, with no attending tied to Maple so it can be removed."""
    raw = sample_instance().model_dump(mode="json")
    raw["attending_schedules"] = []
    for attending in raw["attendings"]:
        for field in ("preferred_weekly_schedule_half_days", "schedule_template_half_days"):
            attending[field] = [
                half_day for half_day in attending.get(field, [])
                if half_day.get("clinic_id") != "maple"
            ]
    return SchedulerInput.model_validate(raw)


def _draft(instance: SchedulerInput, assignments=(), attending_work=(), unassigned=()) -> Schedule:
    return Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.CP_SAT,
            status=SolverStatus.UNKNOWN,
            solver_status=SolverStatus.UNKNOWN,
        ),
        assignments=list(assignments),
        attending_work=list(attending_work),
        unassigned=list(unassigned),
    )


def _first_week(instance: SchedulerInput) -> int:
    return _open_start(instance, KEPT)


def _program_with_draft(tmp_path, *, session_fields=None, extra_work=()):
    """A workspace holding blocks, clinic sessions, and attending work."""
    instance = _program()
    week = _first_week(instance)
    schedule = _draft(
        instance,
        assignments=[
            _block(instance, EDITED),
            _block(
                instance,
                KEPT,
                slots=[
                    _session(week, MON, "maple", **(session_fields or {})),
                    _session(week, TUE, "cedar"),
                ],
            ),
            _block(instance, GONE),
        ],
        attending_work=[
            AssignedAttendingWork(
                attending_id=ATTENDING_ID, week=1, weekday=FRI, session=AM, work_type=AC,
            ),
            AssignedAttendingWork(
                attending_id=ATTENDING_ID, week=2, weekday=FRI, session=AM, work_type=AC,
            ),
            *extra_work,
        ],
    )
    store = _store(tmp_path)
    workspace = store.create("Program", instance)
    return store, WorkspaceController(store).save_schedule(workspace, schedule)


def _edit_inputs(store: Store, workspace, instance: SchedulerInput):
    saved = WorkspaceController(store).save_instance(workspace, instance)
    assert saved.solution_is_out_of_date
    return saved


def _clear_edited_block(store: Store, workspace):
    assignment = next(
        item for item in workspace.latest_schedule.assignments if item.resident_id == EDITED
    )
    return WorkspaceController(store).save_schedule_edit(
        workspace,
        clear_schedule_block(
            workspace.latest_schedule,
            ScheduleBlock(EDITED, "fmed", assignment.start_week, len(assignment.weeks)),
        ),
    )


def _sessions(schedule: Schedule, resident_id: str) -> dict[Weekday, str | None]:
    return {
        slot.weekday: slot.site
        for assignment in schedule.assignments
        if assignment.resident_id == resident_id
        for slot in assignment.clinic_slots
    }


def _with_vacation(instance: SchedulerInput, attending_id: str, day) -> SchedulerInput:
    return instance.revised(attendings=[
        person.revised(vacation_ranges=[
            *person.vacation_ranges,
            AttendingVacation(start_date=day, end_date=day),
        ]) if person.id == attending_id else person
        for person in instance.attendings
    ])


def _friday_of_week_one(instance: SchedulerInput):
    return instance.calendar.first_week_start + timedelta(days=4)


def _visible_on(workspace, week: int, weekday: Weekday):
    person = next(item for item in workspace.instance.attendings if item.id == ATTENDING_ID)
    return workspace.instance.effective_attending_week(
        person, week, scheduled_work=[
            item for item in workspace.latest_schedule.attending_work
            if item.attending_id == ATTENDING_ID and item.week == week
        ],
    ).assignment_on(weekday, AM)


def _academic_on_monday(instance: SchedulerInput) -> SchedulerInput:
    raw = instance.model_dump(mode="json")
    raw["clinic_policy"]["academic"]["weekday"] = MON.value
    raw["clinic_policy"]["academic"]["session"] = AM.value
    raw["academic_half_day_overrides"] = []
    return SchedulerInput.model_validate(raw)


def test_a_block_edit_after_an_attending_vacation_hides_their_work_until_it_is_undone(
    tmp_path,
) -> None:
    store, workspace = _program_with_draft(tmp_path)
    original = workspace.instance
    stale = _edit_inputs(
        store, workspace, _with_vacation(original, ATTENDING_ID, _friday_of_week_one(original)),
    )

    saved = _clear_edited_block(store, stale)

    assert not saved.solution_is_out_of_date
    assert saved.schedule.is_working_draft
    assert not any(item.resident_id == EDITED for item in saved.schedule.assignments)
    # Nothing is gone, so nothing is reported as gone.
    assert len(saved.schedule.attending_work) == 2
    assert not any("left out" in note for note in saved.schedule.meta.notes)
    assert _visible_on(saved, 1, FRI) is None
    assert _visible_on(saved, 2, FRI) is not None

    undone = WorkspaceController(store).save_instance(saved, original)
    assert _visible_on(undone, 1, FRI) is not None


def test_a_block_edit_after_removing_an_attending_drops_only_their_work(tmp_path) -> None:
    store, workspace = _program_with_draft(tmp_path)
    stale = _edit_inputs(store, workspace, remove_attending(workspace.instance, ATTENDING_ID))

    saved = _clear_edited_block(store, stale)

    assert saved.schedule.attending_work == []
    assert _sessions(saved.schedule, KEPT) == {MON: "maple", TUE: "cedar"}
    assert any(
        note.startswith("2 attending half-days left out of this working draft")
        for note in saved.schedule.meta.notes
    )


def test_a_block_edit_after_removing_a_clinic_keeps_each_session_without_its_site(
    tmp_path,
) -> None:
    precepting = AssignedAttendingWork(
        attending_id=ATTENDING_ID, week=3, weekday=FRI, session=AM,
        work_type=PC, clinic_id="maple",
    )
    store, workspace = _program_with_draft(tmp_path, extra_work=[precepting])
    stale = _edit_inputs(store, workspace, remove_clinic(workspace.instance, "maple"))

    saved = _clear_edited_block(store, stale)

    assert _sessions(saved.schedule, KEPT) == {MON: None, TUE: "cedar"}
    assert precepting not in saved.schedule.attending_work
    assert len(saved.schedule.attending_work) == 2
    notes = " ".join(saved.schedule.meta.notes)
    assert "1 attending half-day left out of this working draft" in notes
    assert "maple" not in notes


def test_a_block_edit_after_moving_the_academic_half_day_hides_the_overlapping_session(
    tmp_path,
) -> None:
    store, workspace = _program_with_draft(tmp_path)
    original = workspace.instance
    stale = _edit_inputs(store, workspace, _academic_on_monday(original))

    saved = _clear_edited_block(store, stale)

    assert _sessions(saved.schedule, KEPT) == {MON: "maple", TUE: "cedar"}
    week = _first_week(original)

    def on_board(instance: SchedulerInput, schedule: Schedule, weekday: Weekday) -> list[str]:
        return [person.resident_id for person in occupancy(instance, schedule)[week, weekday, AM]]

    assert KEPT not in on_board(saved.instance, saved.schedule, MON)
    assert KEPT in on_board(saved.instance, saved.schedule, TUE)

    undone = WorkspaceController(store).save_instance(saved, original)
    assert KEPT in on_board(undone.instance, undone.latest_schedule, MON)


def test_a_lock_that_stopped_fitting_stays_in_the_draft_so_the_next_solve_still_names_it(
    tmp_path,
) -> None:
    """Dropping it would let the next solve succeed with the lock silently gone."""
    store, workspace = _program_with_draft(tmp_path, session_fields={"locked": True})
    stale = _edit_inputs(store, workspace, _academic_on_monday(workspace.instance))

    saved = _clear_edited_block(store, stale)

    week = _first_week(saved.instance)
    assert locked_clinic_states(saved.instance, saved.schedule) == {(KEPT, week, MON, AM): True}


def test_a_block_that_stopped_fitting_stays_in_the_draft(tmp_path) -> None:
    store, workspace = _program_with_draft(tmp_path)
    instance = workspace.instance
    week = _first_week(instance)
    stale = _edit_inputs(store, workspace, instance.revised(residents=[
        resident.revised(vacation_weeks=sorted({*resident.vacation_weeks, week}))
        if resident.id == KEPT else resident
        for resident in instance.residents
    ]))

    saved = _clear_edited_block(store, stale)

    assert any(item.resident_id == KEPT for item in saved.schedule.assignments)
    assert not any("left out" in note for note in saved.schedule.meta.notes)


def test_a_later_edit_and_a_saved_file_both_accept_a_draft_with_hidden_work(tmp_path) -> None:
    store, workspace = _program_with_draft(tmp_path)
    original = workspace.instance
    stale = _edit_inputs(store, workspace, _academic_on_monday(
        _with_vacation(original, ATTENDING_ID, _friday_of_week_one(original)),
    ))
    draft = _clear_edited_block(store, stale)

    # The draft is current now, and the next edit inherits from it.
    other = next(item for item in draft.schedule.assignments if item.resident_id == GONE)
    edited = WorkspaceController(store).save_schedule_edit(
        draft,
        clear_schedule_block(
            draft.schedule, ScheduleBlock(GONE, "fmed", other.start_week, len(other.weeks)),
        ),
    )
    assert _sessions(edited.schedule, KEPT) == {MON: "maple", TUE: "cedar"}

    reopened = _store(tmp_path, "reopened.sqlite")
    state = reopened.restore_rbsc(store.export_rbsc())

    restored = reopened.get(state.workspaces[0].id)
    assert not restored.solution_is_out_of_date
    assert restored.schedule.attending_work == edited.schedule.attending_work
    assert _sessions(restored.schedule, KEPT) == {MON: "maple", TUE: "cedar"}


def test_an_edit_keeping_a_solved_status_becomes_a_draft_when_out_of_date(tmp_path) -> None:
    store, workspace = _program_with_draft(tmp_path)
    instance = workspace.instance
    stale = _edit_inputs(
        store, workspace, _with_vacation(instance, ATTENDING_ID, _friday_of_week_one(instance)),
    )
    solved_looking = stale.latest_schedule.revised(meta=stale.latest_schedule.meta.revised(
        status=SolverStatus.FEASIBLE, solver_status=SolverStatus.FEASIBLE,
    ))

    saved = WorkspaceController(store).save_schedule_edit(stale, solved_looking)

    assert saved.schedule.is_working_draft


def test_a_solve_result_still_goes_through_full_validation(tmp_path) -> None:
    """The solve path keeps its own strict save; nothing is quietly repaired."""
    store, workspace = _program_with_draft(tmp_path)
    instance = workspace.instance
    stale = _edit_inputs(
        store, workspace, _with_vacation(instance, ATTENDING_ID, _friday_of_week_one(instance)),
    )
    solved_looking = stale.latest_schedule.revised(meta=stale.latest_schedule.meta.revised(
        status=SolverStatus.FEASIBLE, solver_status=SolverStatus.FEASIBLE,
    ))

    with pytest.raises(ValueError, match="schedule does not match instance"):
        WorkspaceController(store).save_schedule(stale, solved_looking)


def test_a_clinic_edit_on_an_out_of_date_schedule_saves(tmp_path) -> None:
    store, workspace = _program_with_draft(tmp_path)
    instance = workspace.instance
    stale = _edit_inputs(
        store, workspace, _with_vacation(instance, ATTENDING_ID, _friday_of_week_one(instance)),
    )

    updated = add_resident_clinic_slot(
        stale.instance, stale.latest_schedule,
        resident_id=KEPT, week=_first_week(instance), weekday=THU, session=AM, site_id="cedar",
    )
    saved = WorkspaceController(store).save_schedule_edit(stale, updated)

    assert saved.schedule.is_working_draft
    assert _sessions(saved.schedule, KEPT) == {MON: "maple", TUE: "cedar", THU: "cedar"}
    assert len(saved.schedule.attending_work) == 2


# Work an edit adds must still fit ------------------------------------------


def _stale_after_vacation(tmp_path):
    store, workspace = _program_with_draft(tmp_path)
    instance = workspace.instance
    return store, _edit_inputs(
        store, workspace, _with_vacation(instance, ATTENDING_ID, _friday_of_week_one(instance)),
    )


def test_a_new_block_over_a_vacation_is_still_rejected(tmp_path) -> None:
    store, stale = _stale_after_vacation(tmp_path)
    instance = stale.instance
    away = min(instance.resident_scheduling_vacation_weeks(EDITED))
    duration = _duration(instance, EDITED)
    start = max(1, away - duration + 1)
    new_block = Assignment(
        resident_id=EDITED,
        rotation_id="fmed",
        kind=instance.rotations_by_id["fmed"].kind,
        start_week=start,
        end_week=start + duration - 1,
        weeks=list(range(start, start + duration)),
        block_start_week=start,
        block_duration_weeks=duration,
    )
    schedule = stale.latest_schedule
    draft = schedule.revised(assignments=[
        new_block,
        *(item for item in schedule.assignments if item.resident_id != EDITED),
    ])

    with pytest.raises(ValueError, match="non-vacationable"):
        WorkspaceController(store).save_schedule_edit(stale, draft)


@pytest.mark.parametrize(
    ("weekday", "site", "fragment"),
    [
        (Weekday.WEDNESDAY, "cedar", "academic half day"),
        (THU, "retired-clinic", "not configured"),
    ],
    ids=["academic-half-day", "unknown-clinic"],
)
def test_a_new_session_that_does_not_fit_is_still_rejected(
    tmp_path, weekday, site, fragment,
) -> None:
    store, stale = _stale_after_vacation(tmp_path)
    schedule = stale.latest_schedule
    week = _first_week(stale.instance)
    session = AssignedClinic(
        weekday=weekday,
        session=Session.AFTERNOON if weekday is Weekday.WEDNESDAY else AM,
        site=site,
        week=week,
    )
    draft = schedule.revised(assignments=[
        item.revised(clinic_slots=[*item.clinic_slots, session])
        if item.resident_id == KEPT else item
        for item in schedule.assignments
    ])

    with pytest.raises(ValueError, match=fragment):
        store.save_schedule(
            stale.id, draft.revised(meta=draft.meta.revised(
                status=SolverStatus.UNKNOWN, solver_status=SolverStatus.UNKNOWN,
            )),
            expected_workspace_revision=stale.workspace_revision,
            expected_instance_revision=stale.instance_revision,
        )


def test_new_attending_work_on_a_vacation_day_is_still_rejected(tmp_path) -> None:
    store, stale = _stale_after_vacation(tmp_path)
    schedule = stale.latest_schedule
    new_work = AssignedAttendingWork(
        attending_id=ATTENDING_ID, week=1, weekday=FRI, session=Session.AFTERNOON, work_type=AC,
    )

    with pytest.raises(ValueError, match="on vacation"):
        WorkspaceController(store).save_schedule_edit(
            stale, schedule.revised(attending_work=[*schedule.attending_work, new_work]),
        )


# Attending edits after an unrelated resident or rotation change ------------


def _without_resident(instance: SchedulerInput, resident_id: str) -> SchedulerInput:
    return instance.revised(residents=[
        resident for resident in instance.residents if resident.id != resident_id
    ])


@pytest.mark.parametrize(
    "assignment",
    [
        AttendingWorkHalfDay(
            weekday=TUE, session=AM, work_type=AttendingWorkType.SPECIAL_OTHER,
            description="Faculty meeting",
        ),
        AttendingWorkHalfDay(weekday=TUE, session=AM, work_type=ADMIN),
    ],
    ids=["special-other", "admin-time"],
)
def test_an_attending_edit_after_removing_a_resident_saves_without_their_blocks(
    tmp_path, assignment,
) -> None:
    store, workspace = _program_with_draft(tmp_path)
    stale = _edit_inputs(store, workspace, _without_resident(workspace.instance, GONE))

    updated, draft = set_attending_schedule_half_day(
        stale.instance, stale.latest_schedule, ATTENDING_ID,
        week=1, weekday=TUE, session=AM, assignment=assignment,
    )
    saved = WorkspaceController(store).save_instance(stale, updated, draft_schedule=draft)

    assert not saved.solution_is_out_of_date
    assert not any(item.resident_id == GONE for item in saved.schedule.assignments)
    assert _sessions(saved.schedule, KEPT) == {MON: "maple", TUE: "cedar"}
    assert saved.instance.attending_schedule_weeks(ATTENDING_ID)[0].half_days[0] == assignment
    notes = " ".join(saved.schedule.meta.notes)
    assert GONE not in notes
    assert "1 block left out of this working draft" in notes


def test_an_attending_lock_after_removing_a_resident_saves_without_their_blocks(tmp_path) -> None:
    store, workspace = _program_with_draft(tmp_path)
    stale = _edit_inputs(store, workspace, _without_resident(workspace.instance, GONE))

    updated, draft = set_attending_work_locked(
        stale.instance, stale.latest_schedule, ATTENDING_ID,
        week=1, weekday=FRI, session=AM, locked=True,
    )
    saved = WorkspaceController(store).save_schedule_edit(stale, draft)

    assert updated == stale.instance
    assert any(item.locked for item in saved.schedule.attending_work)
    assert not any(item.resident_id == GONE for item in saved.schedule.assignments)


# Telling the user what is gone --------------------------------------------


def _session_for(store: Store, workspace) -> WorkspaceSession:
    return WorkspaceSession(store=store, workspace_id=workspace.id, active_tab="residents")


@pytest.fixture
def notifications(monkeypatch) -> list[tuple[str, str]]:
    from nicegui import ui

    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ui, "notify", lambda message, **options: shown.append((message, options.get("type"))),
    )
    return shown


def test_an_edit_that_leaves_work_out_warns_with_counts(tmp_path, notifications) -> None:
    store, workspace = _program_with_draft(tmp_path)
    instance = remove_attending(_without_resident(workspace.instance, GONE), ATTENDING_ID)
    stale = _edit_inputs(store, workspace, instance)
    assignment = next(
        item for item in stale.latest_schedule.assignments if item.resident_id == EDITED
    )

    _session_for(store, stale).persist_schedule(
        stale,
        clear_schedule_block(
            stale.latest_schedule,
            ScheduleBlock(EDITED, "fmed", assignment.start_week, len(assignment.weeks)),
        ),
        refresh=False,
    )

    assert notifications == [(
        "1 block and 2 attending half-days left out of this working draft because the "
        "resident, rotation, block length, attending, or clinic was removed.",
        "warning",
    )]


def test_an_instance_edit_with_a_draft_warns_too(tmp_path, notifications) -> None:
    store, workspace = _program_with_draft(tmp_path)
    stale = _edit_inputs(store, workspace, _without_resident(workspace.instance, GONE))

    updated, draft = set_attending_schedule_half_day(
        stale.instance, stale.latest_schedule, ATTENDING_ID,
        week=1, weekday=TUE, session=AM,
        assignment=AttendingWorkHalfDay(weekday=TUE, session=AM, work_type=ADMIN),
    )
    _session_for(store, stale).persist_instance(
        stale, updated, draft_schedule=draft, refresh=False,
    )

    assert [kind for _message, kind in notifications] == ["warning"]
    assert notifications[0][0].startswith("1 block left out")


def test_hidden_work_causes_no_warning(tmp_path, notifications) -> None:
    store, workspace = _program_with_draft(tmp_path)
    instance = workspace.instance
    stale = _edit_inputs(
        store, workspace, _with_vacation(instance, ATTENDING_ID, _friday_of_week_one(instance)),
    )
    assignment = next(
        item for item in stale.latest_schedule.assignments if item.resident_id == EDITED
    )

    _session_for(store, stale).persist_schedule(
        stale,
        clear_schedule_block(
            stale.latest_schedule,
            ScheduleBlock(EDITED, "fmed", assignment.start_week, len(assignment.weeks)),
        ),
        refresh=False,
    )

    assert notifications == []


def test_the_second_edit_does_not_warn_again(tmp_path, notifications) -> None:
    store, workspace = _program_with_draft(tmp_path)
    stale = _edit_inputs(store, workspace, _without_resident(workspace.instance, GONE))
    session = _session_for(store, stale)
    assignment = next(
        item for item in stale.latest_schedule.assignments if item.resident_id == EDITED
    )
    saved = session.persist_schedule(
        stale,
        clear_schedule_block(
            stale.latest_schedule,
            ScheduleBlock(EDITED, "fmed", assignment.start_week, len(assignment.weeks)),
        ),
        refresh=False,
    )
    assert len(notifications) == 1

    session.persist_schedule(saved, saved.schedule, refresh=False)

    assert len(notifications) == 1


# What a working draft keeps --------------------------------------------------


def test_a_draft_that_still_fits_is_only_marked_unsolved() -> None:
    instance = _program()
    block = _block(instance, KEPT, slots=[_session(_first_week(instance), MON, "maple")])
    solved = _draft(instance, [block]).revised(meta=ScheduleMeta(
        academic_year=instance.academic_year,
        engine=SolverEngineName.CP_SAT,
        status=SolverStatus.FEASIBLE,
        solver_status=SolverStatus.FEASIBLE,
        notes=["Earlier note"],
    ))

    draft = working_draft_for_inputs(instance, solved)

    assert draft.is_working_draft
    assert draft.assignments == solved.assignments
    assert draft.meta.notes == ["Earlier note"]
    assert left_out_of_draft(instance, solved) == LeftOutOfDraft()
    # Reconciling again changes nothing.
    assert working_draft_for_inputs(instance, draft) == draft


def test_a_draft_loses_blocks_for_a_rotation_or_block_length_that_no_longer_exists() -> None:
    instance = _program()
    kept = _block(instance, KEPT, slots=[_session(_first_week(instance), MON, "maple")])
    retired = _block(instance, EDITED, slots=[_session(1, TUE, "cedar")]).revised(
        rotation_id="retired_rotation",
    )
    other_length = _block(instance, GONE).revised(
        start_week=30, end_week=30, weeks=[30], block_start_week=30, block_duration_weeks=1,
    )
    schedule = _draft(instance, [kept, retired, other_length])

    draft = working_draft_for_inputs(instance, schedule)

    assert draft.assignments == [kept]
    assert left_out_of_draft(instance, schedule) == LeftOutOfDraft(blocks=2, clinic_sessions=1)


def test_a_draft_forgets_unassigned_residents_who_left_the_program() -> None:
    instance = _program()

    draft = working_draft_for_inputs(instance, _draft(instance, unassigned=[KEPT, "resident-gone"]))

    assert draft.unassigned == [KEPT]


def test_a_draft_for_another_academic_year_is_refused() -> None:
    instance = _program()
    other = _draft(instance).revised(meta=_draft(instance).meta.revised(academic_year="1999-2000"))

    with pytest.raises(ValueError, match="different academic year"):
        working_draft_for_inputs(instance, other)


def test_left_out_messages_read_as_sentences() -> None:
    assert LeftOutOfDraft().message is None
    assert LeftOutOfDraft(blocks=1).message.startswith("1 block left out")
    assert LeftOutOfDraft(blocks=2, clinic_sessions=1, attending_half_days=3).message.startswith(
        "2 blocks, 1 clinic session and 3 attending half-days left out"
    )


# A saved document opens whatever the locking setting says -------------------


def _one_week(attending_id: str, name: str) -> Attending:
    first = blank_instance().calendar.first_week_start
    return Attending(
        id=attending_id, name=name, half_days_per_week=1, eligible_clinic_ids=["clinic"],
        schedule_start_date=first, schedule_end_date=first + timedelta(days=6),
    )


def _clinic_program(maximum: int, *, lock_through_today: bool) -> SchedulerInput:
    raw = blank_instance().model_dump(mode="json")
    raw["clinic_policy"]["sites"][0]["staffing_mode"] = "attending_managed"
    raw["clinic_policy"]["sites"][0]["half_days"] = [
        {"weekday": MON.value, "session": AM.value, "attendings": maximum},
    ]
    raw["attendings"] = [
        _one_week(f"attending-00{index}", f"Attending {index}").model_dump(mode="json")
        for index in (1, 2)
    ]
    raw["lock_through_today"] = lock_through_today
    return SchedulerInput.model_validate(raw)


def _solved(instance: SolverProblem, work) -> Schedule:
    return Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.CP_SAT,
            status=SolverStatus.FEASIBLE,
        ),
        attending_work=list(work),
    )


def _past_precepting() -> list[AssignedAttendingWork]:
    """Two preceptors the solve placed on a half-day that is now in the past."""
    return [
        AssignedAttendingWork(
            attending_id=f"attending-00{index}", week=1, weekday=MON, session=AM,
            work_type=PC, clinic_id=CLINIC,
        )
        for index in (1, 2)
    ]


def _resolve_after_lowering_the_maximum(today) -> tuple[SchedulerInput, Schedule]:
    """Lower Max attendings to 1, then solve with the past half-day locked."""
    lowered = _clinic_program(1, lock_through_today=True)
    problem = SolverProblem.from_instance(lowered, today=today)
    reference = _solved(problem, _past_precepting())
    result = schedule_attending_work(
        problem, _solved(problem, ()), attending_week_facts(problem, reference), cp_model,
    )
    solved = _solved(problem, result.work)
    assert validate_schedule(problem, solved).valid
    return lowered, solved


def _today():
    return blank_instance().calendar.first_week_start + timedelta(days=30)


def test_kept_work_over_a_lowered_maximum_is_recorded_as_an_override() -> None:
    _lowered, solved = _resolve_after_lowering_the_maximum(_today())

    precepting = [item for item in solved.attending_work if item.work_type is PC]
    assert len(precepting) == 2
    assert all(item.manual_override and not item.hand_entered for item in precepting)


def test_kept_work_within_the_maximum_stays_as_the_solve_placed_it() -> None:
    instance = _clinic_program(2, lock_through_today=True)
    problem = SolverProblem.from_instance(instance, today=_today())
    reference = _solved(problem, _past_precepting())

    facts = attending_week_facts(problem, reference)

    kept = [item for weekly in facts.values() for one in weekly for item in one.locked_work]
    assert kept == reference.attending_work


def test_validation_does_not_depend_on_the_locking_setting_or_the_date() -> None:
    lowered, solved = _resolve_after_lowering_the_maximum(_today())
    without_setting = lowered.revised(lock_through_today=False)

    assert validate_schedule(lowered, solved).valid
    assert validate_schedule(without_setting, solved).valid
    assert validate_schedule(
        SolverProblem.from_instance(lowered, today=_today() + timedelta(days=200)), solved,
    ).valid


def test_a_saved_schedule_with_kept_work_over_a_maximum_reopens_solved(tmp_path) -> None:
    lowered, solved = _resolve_after_lowering_the_maximum(_today())
    store = _store(tmp_path)
    workspace = store.create("Program", lowered)
    saved = WorkspaceController(store).save_schedule(workspace, solved)
    payload = store.export_rbsc()

    reopened = _store(tmp_path, "reopened.sqlite")
    state = reopened.restore_rbsc(payload)

    workspace = reopened.get(state.workspaces[0].id)
    assert workspace.instance.lock_through_today is False
    assert not workspace.solution_is_out_of_date
    assert workspace.schedule is not None
    assert not workspace.schedule.is_working_draft
    assert workspace.schedule.attending_work == saved.schedule.attending_work


def test_turning_off_the_locking_setting_keeps_the_solved_schedule(tmp_path) -> None:
    lowered, solved = _resolve_after_lowering_the_maximum(_today())
    store = _store(tmp_path)
    workspace = WorkspaceController(store).save_schedule(store.create("Program", lowered), solved)

    saved = WorkspaceController(store).save_instance(
        workspace,
        workspace.instance.revised(lock_through_today=False),
        impact=InstanceEditImpact.APPLICATION_PREFERENCE,
    )

    assert not saved.solution_is_out_of_date
    assert not saved.schedule.is_working_draft


def test_unlocking_kept_work_over_a_maximum_keeps_the_solved_schedule(tmp_path) -> None:
    lowered = _clinic_program(1, lock_through_today=False)
    problem = SolverProblem.from_instance(lowered)
    reference = _solved(problem, [item.revised(locked=True) for item in _past_precepting()])
    result = schedule_attending_work(
        problem, _solved(problem, ()), attending_week_facts(problem, reference), cp_model,
    )
    store = _store(tmp_path)
    workspace = WorkspaceController(store).save_schedule(
        store.create("Program", lowered), _solved(problem, result.work),
    )

    updated, unlocked = set_attending_work_locked(
        workspace.instance, workspace.schedule, "attending-001",
        week=1, weekday=MON, session=AM, locked=False,
    )
    saved = WorkspaceController(store).save_schedule_edit(workspace, unlocked)

    assert updated == workspace.instance
    assert not saved.schedule.is_working_draft
    assert not saved.solution_is_out_of_date


def test_a_file_saved_before_this_was_recorded_opens_out_of_date_with_nothing_lost(
    tmp_path,
) -> None:
    """Older builds wrote kept work without the override, so the file must still open."""
    within = _clinic_program(2, lock_through_today=False)
    solved = _solved(SolverProblem.from_instance(within), _past_precepting())
    store = _store(tmp_path)
    WorkspaceController(store).save_schedule(store.create("Program", within), solved)
    document = json.loads(store.export_rbsc())
    # What that older build saved: a current schedule over a since-lowered maximum.
    for catalog in document["catalogs"]:
        site = catalog["catalog"]["clinic_policy"]["sites"][0]
        site["half_days"] = [{"weekday": MON.value, "session": AM.value, "attendings": 1}]

    reopened = _store(tmp_path, "reopened.sqlite")
    state = reopened.restore_rbsc(json.dumps(document))

    workspace = reopened.get(state.workspaces[0].id)
    assert workspace.solution_is_out_of_date
    assert workspace.schedule is None
    assert workspace.stale_schedule.attending_work == solved.attending_work


def test_a_file_whose_schedule_has_dangling_references_is_still_rejected(tmp_path) -> None:
    within = _clinic_program(2, lock_through_today=False)
    solved = _solved(SolverProblem.from_instance(within), _past_precepting())
    store = _store(tmp_path)
    WorkspaceController(store).save_schedule(store.create("Program", within), solved)
    document = json.loads(store.export_rbsc())
    document["workspaces"][0]["schedule"]["attending_work"][0]["attending_id"] = "attending-gone"

    with pytest.raises(ValueError, match="unknown attending"):
        _store(tmp_path, "reopened.sqlite").restore_rbsc(json.dumps(document))


# End to end --------------------------------------------------------------------


@pytest.mark.solve
def test_a_real_solved_schedule_survives_edits_after_inputs_change_and_a_new_solve(
    tmp_path,
) -> None:
    from rbs.solver import solve_problem

    store = _store(tmp_path)
    instance = sample_instance()
    workspace = store.create("Program", instance)
    options = instance.solver.revised(time_limit_seconds=30, random_seed=1)
    solved = solve_problem(SolverProblem.from_instance(instance), options=options)
    assert solved.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    workspace = WorkspaceController(store).save_schedule(workspace, solved)

    # A week of attending vacation and a departing resident make it out of date.
    work = next(item for item in solved.attending_work if item.week > 2)
    person = next(item for item in instance.attendings if item.id == work.attending_id)
    first = instance.calendar.first_week_start + timedelta(weeks=work.week - 1)
    locked_residents = {lock.resident_id for lock in instance.locks}
    leaving = next(item.id for item in instance.residents if item.id not in locked_residents)
    edited = _without_resident(
        instance.revised(attendings=[
            item.revised(vacation_ranges=[
                *item.vacation_ranges,
                AttendingVacation(start_date=first, end_date=first + timedelta(days=4)),
            ]) if item.id == person.id else item
            for item in instance.attendings
        ]),
        leaving,
    )
    stale = _edit_inputs(store, workspace, edited)

    # The program lead clears a block for someone else, then edits an attending.
    target = next(item for item in solved.assignments if item.resident_id != leaving)
    cleared = WorkspaceController(store).save_schedule_edit(
        stale,
        clear_schedule_block(
            stale.latest_schedule,
            ScheduleBlock(
                target.resident_id,
                target.rotation_id,
                target.block_start_week or target.start_week,
                target.block_duration_weeks or len(target.weeks),
                elective=target.elective,
            ),
        ),
    )
    assert cleared.schedule.is_working_draft
    assert not any(item.resident_id == leaving for item in cleared.schedule.assignments)
    assert work in cleared.schedule.attending_work  # hidden under the vacation, not gone

    other = next(item for item in cleared.instance.attendings if item.id != person.id)
    updated, draft = set_attending_schedule_half_day(
        cleared.instance, cleared.schedule, other.id,
        week=40, weekday=TUE, session=AM,
        assignment=AttendingWorkHalfDay(weekday=TUE, session=AM, work_type=ADMIN),
    )
    edited_again = WorkspaceController(store).save_instance(cleared, updated, draft_schedule=draft)

    # The next solve builds on that draft and replaces it with a valid schedule.
    resolved = solve_problem(
        SolverProblem.from_instance(edited_again.instance),
        options=options,
        reference_solution=edited_again.schedule,
    )
    assert resolved.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    final = WorkspaceController(store).save_schedule(edited_again, resolved)
    assert final.schedule is not None
    assert not final.schedule.is_working_draft
    assert work not in final.schedule.attending_work
