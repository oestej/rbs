"""Clinic removal protects saved attending work and keeps draft edits usable."""

from datetime import timedelta

import pytest

from rbs.attending_schedule import effective_attending_schedule_week
from rbs.catalog import blank_instance
from rbs.models.attending import (
    Attending,
    AttendingVacation,
    AttendingWorkHalfDay,
    AttendingWorkType,
)
from rbs.models.clinic import ClinicStaffingMode
from rbs.models.enums import Session, SolverEngineName, SolverStatus, Weekday
from rbs.models.schedule import AssignedAttendingWork, Schedule, ScheduleMeta
from rbs.store import Store
from rbs.ui.attendings.ops import set_attending_schedule_half_day, set_attending_work_locked
from rbs.ui.clinic.ops import add_clinic, clinic_removal_attending_names, remove_clinic
from rbs.ui.clinic.tab import _confirm_remove_clinic, render_clinic_tab
from rbs.workspaces import WorkspaceController

MON, THU = Weekday.MONDAY, Weekday.THURSDAY
AM, PM = Session.MORNING, Session.AFTERNOON
PC = AttendingWorkType.PRECEPTING_CLINIC


def _example(*, locked=True, hand_entered=False, hidden=False):
    instance = blank_instance()
    clinic = instance.clinic_policy.sites[0].revised(
        staffing_mode=ClinicStaffingMode.ATTENDING_MANAGED,
    )
    instance = instance.revised(clinic_policy=instance.clinic_policy.revised(sites=[clinic]))
    instance = add_clinic(instance, clinic.revised(id="other", name="Other", allocation_rules=[]))
    first = instance.calendar.first_week_start
    person = Attending(
        id="ada", name="Ada Lovelace", eligible_clinic_ids=[clinic.id, "other"],
        vacation_ranges=[AttendingVacation(start_date=first, end_date=first)] if hidden else [],
    )
    instance = instance.revised(attendings=[person], lock_through_today=False)
    work = AssignedAttendingWork(
        attending_id=person.id, week=1, weekday=MON, session=AM, work_type=PC,
        clinic_id=clinic.id, locked=locked, hand_entered=hand_entered,
        manual_override=hand_entered,
    )
    other = AssignedAttendingWork(
        attending_id=person.id, week=1, weekday=THU, session=PM, work_type=PC,
        clinic_id="other", locked=True,
    )
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year, engine=SolverEngineName.CP_SAT,
            status=SolverStatus.UNKNOWN, solver_status=SolverStatus.UNKNOWN,
        ),
        attending_work=[work, other],
    )
    return instance, schedule, work, other


def _elements_since(before):
    from nicegui import ui

    return [element for key, element in ui.context.client.elements.items()
            if key not in before and not element.is_deleted]


def _click(element):
    from nicegui.events import ClickEventArguments

    for listener in element._event_listeners.values():
        if listener.type == "click":
            listener.handler(ClickEventArguments(sender=element, client=element.client))


@pytest.mark.parametrize("locked,hand_entered", [(True, False), (False, True), (False, False)])
@pytest.mark.parametrize("hidden", [False, True])
def test_removal_checks_saved_precepting_even_when_hidden(locked, hand_entered, hidden):
    instance, schedule, work, _ = _example(
        locked=locked, hand_entered=hand_entered, hidden=hidden,
    )
    if hidden:
        assert effective_attending_schedule_week(
            instance, instance.attendings[0], 1, schedule,
        ).assignment_on(MON, AM) is None
    # Duplicate references name an attending once, and other clinics do not interfere.
    assert clinic_removal_attending_names(instance, work.clinic_id, schedule=schedule) == [
        "Ada Lovelace",
    ]
    with pytest.raises(ValueError, match="reassign Precepting Clinic work.*Ada Lovelace"):
        remove_clinic(instance, work.clinic_id, schedule=schedule)
    cleared = schedule.revised(attending_work=[item for item in schedule.attending_work
                                             if item.clinic_id != work.clinic_id])
    assert work.clinic_id not in remove_clinic(
        instance, work.clinic_id, schedule=cleared,
    ).clinic_policy.site_ids


def test_clinic_tab_shows_blockers_before_offering_removal(tmp_path):
    from nicegui import ui

    instance, schedule, work, _ = _example(hidden=True)
    store = Store(tmp_path / "removal.sqlite")
    store.init()
    original = store.create("Removal", instance, schedule)
    stale = WorkspaceController(store).save_instance(
        original,
        instance.revised(attendings=[instance.attendings[0].revised(half_days_per_week=8)]),
    )
    assert stale.solution_is_out_of_date
    saved = []
    before = set(ui.context.client.elements)
    render_clinic_tab(
        stale.instance, schedule=stale.latest_schedule,
        on_save=lambda *args: saved.append(args),
    )
    removal_label = next(element for element in _elements_since(before)
                         if getattr(element, "_text", "") == "Remove clinic")
    before = set(ui.context.client.elements)
    _click(removal_label.parent_slot.parent)
    dialog_elements = _elements_since(before)
    text = " ".join(getattr(element, "_text", "") or "" for element in dialog_elements)
    assert "Reassign Precepting Clinic work" in text and "Ada Lovelace" in text
    assert "vacation" in text and "Unlock locked half-days" in text
    assert not any(element.__class__.__name__ == "Button"
                   and element._props.get("label") == "Remove clinic"
                   for element in dialog_elements)
    assert saved == []
    unchanged = store.get(original.id)
    assert unchanged.workspace_revision == stale.workspace_revision
    assert work in unchanged.latest_schedule.attending_work
    assert work.clinic_id in unchanged.instance.clinic_policy.site_ids


def test_clear_work_then_remove_clinic_and_edit_draft_round_trip(tmp_path):
    instance, schedule, work, other = _example()
    store = Store(tmp_path / "removal.sqlite")
    store.init()
    controller = WorkspaceController(store)
    workspace = store.create("Removal", instance, schedule)
    today = instance.calendar.first_week_start - timedelta(days=1)
    updated, unlocked = set_attending_work_locked(
        instance, schedule, "ada", week=1, weekday=MON, session=AM, locked=False, today=today,
    )
    updated, cleared = set_attending_schedule_half_day(
        updated, unlocked, "ada", week=1, weekday=MON, session=AM, assignment=None, today=today,
    )
    workspace = controller.save_instance(workspace, updated, draft_schedule=cleared)
    removed = remove_clinic(workspace.instance, work.clinic_id, schedule=workspace.latest_schedule)
    workspace = controller.save_instance(workspace, removed)
    assert workspace.solution_is_out_of_date
    updated, edited = set_attending_schedule_half_day(
        workspace.instance, workspace.latest_schedule, "ada", week=2, weekday=MON, session=AM,
        assignment=AttendingWorkHalfDay(
            weekday=MON, session=AM, work_type=AttendingWorkType.ADMIN_TIME,
        ),
        today=today,
    )
    workspace = controller.save_instance(workspace, updated, draft_schedule=edited)
    reopened = store.import_workspace_rbsc(store.export_workspace_rbsc(workspace.id))[0]
    assert reopened.latest_schedule.attending_work == [other]
    assert reopened.instance.effective_attending_week(
        reopened.instance.attendings[0], 2,
    ).assignment_on(MON, AM) is not None
    assert not any("left out" in note for note in reopened.latest_schedule.meta.notes)


def test_stale_removal_dialog_cannot_overwrite_newly_locked_work(tmp_path, monkeypatch):
    from nicegui import ui

    instance, schedule, work, _ = _example()
    store = Store(tmp_path / "removal.sqlite")
    store.init()
    workspace = store.create("Removal", instance, schedule.revised(attending_work=[]))
    notifications = []
    monkeypatch.setattr(
        ui, "notify", lambda message, **kwargs: notifications.append((message, kwargs)),
    )
    before = set(ui.context.client.elements)
    _confirm_remove_clinic(
        workspace.instance, work.clinic_id, schedule=workspace.latest_schedule,
        selected_rotation_id=None,
        on_save=lambda updated, _: WorkspaceController(store).save_instance(workspace, updated),
    )
    elements = _elements_since(before)
    remove = next(element for element in elements if element.__class__.__name__ == "Button"
                  and element._props.get("label") == "Remove clinic")
    dialog = next(element for element in elements if element.__class__.__name__ == "Dialog")
    newer = WorkspaceController(store).save_schedule(workspace, schedule)
    _click(remove)
    assert dialog.value
    assert store.get(workspace.id).workspace_revision == newer.workspace_revision
    assert work in store.get(workspace.id).latest_schedule.attending_work
    assert work.clinic_id in store.get(workspace.id).instance.clinic_policy.site_ids
    assert notifications and all(options["type"] == "negative" for _, options in notifications)


@pytest.mark.parametrize("hidden", [False, True])
def test_edit_repairs_a_stale_schedule_from_an_earlier_clinic_removal(tmp_path, hidden):
    instance, schedule, work, other = _example(hidden=hidden)
    store = Store(tmp_path / "legacy-removal.sqlite")
    store.init()
    controller = WorkspaceController(store)
    workspace = store.create("Earlier removal", instance, schedule)
    # Reproduce a clinic removed by the earlier case-only check. The saved
    # schedule remains stale until the user next edits it.
    legacy = remove_clinic(instance, work.clinic_id)
    stale = controller.save_instance(workspace, legacy)
    assert work in stale.latest_schedule.attending_work
    updated, edited = set_attending_schedule_half_day(
        stale.instance, stale.latest_schedule, "ada", week=2, weekday=MON, session=AM,
        assignment=AttendingWorkHalfDay(
            weekday=MON, session=AM, work_type=AttendingWorkType.ADMIN_TIME,
        ),
        today=instance.calendar.first_week_start - timedelta(days=1),
    )
    saved = controller.save_instance(stale, updated, draft_schedule=edited)
    reopened = store.import_workspace_rbsc(store.export_workspace_rbsc(saved.id))[0]
    assert reopened.latest_schedule.attending_work == [other]
    assert any("1 attending half-day left out" in note
               for note in reopened.latest_schedule.meta.notes)
