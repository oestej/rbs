from datetime import timedelta
from types import SimpleNamespace

import pytest

from rbs.attending_schedule import attending_schedule_report, effective_attending_schedule_week
from rbs.catalog import blank_instance
from rbs.clinic_locks import attending_work_is_locked
from rbs.models.attending import (
    Attending,
    AttendingSchedule,
    AttendingVacation,
    AttendingWeeklyWorkSchedule,
    AttendingWorkHalfDay,
    AttendingWorkType,
)
from rbs.models.enums import Session, SolverStatus, Weekday
from rbs.models.instance import SolverProblem
from rbs.models.schedule import AssignedAttendingWork
from rbs.solver.attending_availability import attending_week_facts
from rbs.solver.validation import validate_schedule
from rbs.ui.attendings.ops import (
    attending_working_draft,
    move_attending_schedule_half_day,
    move_attending_work_half_day,
    set_attending_schedule_half_day,
    set_attending_schedule_locked,
    set_attending_work_half_day,
    set_attending_work_locked,
)


def _instance():
    instance = blank_instance()
    start = instance.calendar.first_week_start
    attending = Attending(
        id="attending-001", name="Ada Lovelace", half_days_per_week=2,
        schedule_start_date=start + timedelta(days=1),
        schedule_end_date=start + timedelta(days=13),
        vacation_ranges=[AttendingVacation(
            start_date=start + timedelta(days=3), end_date=start + timedelta(days=3),
        )],
    )
    return instance.revised(
        attendings=[attending, Attending(id="attending-002", name="Grace Hopper")],
        attending_schedules=[AttendingSchedule(
            attending_id=attending.id,
            weeks=[
                AttendingWeeklyWorkSchedule(
                    week=1, half_days_override=2,
                    half_days=[
                        AttendingWorkHalfDay(
                            weekday=Weekday.MONDAY, session=Session.MORNING,
                            description="Stored work before schedule dates",
                        ),
                        AttendingWorkHalfDay(
                            weekday=Weekday.TUESDAY, session=Session.MORNING,
                            description="Faculty reviews",
                        ),
                    ],
                ),
                AttendingWeeklyWorkSchedule(
                    week=2, half_days=[AttendingWorkHalfDay(
                        weekday=Weekday.MONDAY, session=Session.MORNING,
                        work_type=AttendingWorkType.PRECEPTING_CLINIC,
                        clinic_id=instance.clinic_policy.primary_site_id,
                    )],
                ),
            ],
        )],
    )


def _click(element):
    from nicegui.events import handle_event

    listener = next(value for value in element._event_listeners.values() if value.type == "click")
    handle_event(listener.handler, None)


def _texts(element):
    result = [str(element._text)] if getattr(element, "_text", None) else []
    for slot in element.slots.values():
        for child in slot.children:
            result.extend(_texts(child))
    return result


@pytest.fixture
def calendar_page(tmp_path):
    from nicegui import ui

    from rbs.store import Store
    from rbs.ui.app_shell import _render_tab
    from rbs.ui.session import WorkspaceSession

    store = Store(tmp_path / "calendar.sqlite")
    store.init()
    workspace = store.create("Attending calendar", _instance())
    session = WorkspaceSession(
        store=store, workspace_id=workspace.id, attending_id="attending-001",
        active_tab="attendings",
    )
    root = ui.column()
    session.panels["attendings"] = root
    session._render_tab = _render_tab
    before = set(root.client.elements)
    session.refresh_panel("attendings")

    def elements():
        return [element for key, element in root.client.elements.items()
                if key not in before and not element.is_deleted]

    def button(label):
        return next(element for element in elements()
                    if element.__class__.__name__ == "Button"
                    and element._props.get("label") == label)

    def accessible(name):
        return next(element for element in elements() if element._props.get("aria-label") == name)

    yield SimpleNamespace(
        store=store, workspace=workspace, session=session, root=root,
        elements=elements, button=button, accessible=accessible,
    )
    root.delete()


def _save_assignment(page, weekday, week, work_type=AttendingWorkType.ATTENDING_CLINIC):
    before = {element.id for element in page.elements()}
    _click(page.accessible(f"Assign {weekday.value.title()} Morning (AM) in week {week}"))
    created = [element for element in page.elements() if element.id not in before]
    work_type_control = next(element for element in created
                             if element.__class__.__name__ == "Select"
                             and element._props.get("label") == "Work type")
    work_type_control.set_value(work_type.value)
    _click(next(element for element in created
                if element.__class__.__name__ == "Button"
                and element._props.get("label") == "Assign half-day"))


def test_calendar_edits_persist_immediately_without_rebuilding_other_weeks(calendar_page):
    page = calendar_page
    directory = next(element for element in page.elements()
                     if "rbs-master-directory" in element._classes)
    _click(page.button("Edit schedule"))
    assert page.session.attending_schedule_editing
    weeks = [element for element in page.elements()
             if "rbs-resident-clinic-week" in element._classes]
    other_week = weeks[1]

    _save_assignment(page, Weekday.FRIDAY, 1)

    first_save = page.store.get(page.workspace.id)
    weeks_saved = first_save.instance.attending_schedule_weeks("attending-001")
    assert first_save.workspace_revision == page.workspace.workspace_revision + 1
    assert weeks_saved[0].half_days_override == 2
    assert weeks_saved[0].half_days[0].description == "Stored work before schedule dates"
    assert any(half_day.weekday is Weekday.FRIDAY for half_day in weeks_saved[0].half_days)
    assert not other_week.is_deleted
    assert not directory.is_deleted
    assert page.session.attending_schedule_editing
    assert "Return to view" == page.button("Return to view")._props["label"]
    assert "attendings" not in page.session.stale_panels
    assert "clinic_schedule" in page.session.stale_panels

    _click(page.accessible("Override half-day total for week 1"))
    overridden = page.store.get(page.workspace.id)
    assert overridden.instance.attending_schedule_weeks("attending-001")[0].half_days_override == 3

    target = page.accessible("Assign Wednesday Morning (AM) in week 1")
    drop = next(listener for listener in target._event_listeners.values()
                if listener.type == "drop")
    drop.handler(SimpleNamespace(args={
        "scope": "attending-attending-001", "week": 1,
        "weekday": Weekday.FRIDAY.value, "session": Session.MORNING.value,
    }))
    moved = page.store.get(page.workspace.id)
    first_week = moved.instance.attending_schedule_weeks("attending-001")[0]
    assert any(half_day.weekday is Weekday.WEDNESDAY for half_day in first_week.half_days)
    assert not any(half_day.weekday is Weekday.FRIDAY for half_day in first_week.half_days)
    assert first_week.half_days_override == 3

    _click(page.accessible("Edit Wednesday Morning (AM) in week 1"))
    _click(page.button("Remove assignment"))
    _click(page.accessible("Use default half-day total for week 1"))
    assert page.store.get(page.workspace.id).instance.attending_schedule_weeks(
        "attending-001",
    )[0].half_days_override is None

    # A week that has not been redrawn must use the latest saved snapshot.
    _save_assignment(page, Weekday.TUESDAY, 2)
    current = page.store.get(page.workspace.id)
    assert current.instance.attending_schedule_weeks("attending-001")[0].half_days_override is None
    assert current.instance.attending_schedule_weeks("attending-001")[1].half_days[-1].weekday == (
        Weekday.TUESDAY
    )
    assert not directory.is_deleted

    _click(page.button("Return to view"))
    assert not page.session.attending_schedule_editing
    assert page.store.get(page.workspace.id).workspace_revision == current.workspace_revision
    assert not any(element._props.get("draggable") == "true" for element in page.elements())

    # Opening the configuration form after calendar edits must preserve those edits.
    _click(page.button("Edit attending"))
    _click(page.button("Save changes"))
    assert page.store.get(page.workspace.id).instance.attending_schedules == (
        current.instance.attending_schedules
    )


def test_edit_mode_respects_reserved_and_unavailable_half_days(calendar_page):
    page = calendar_page
    _click(page.button("Edit schedule"))
    blocked = [element for element in page.elements()
               if "rbs-resident-clinic-session-cell" in element._classes
               and any(value in element._props.get("aria-label", "") for value in (
                   "Vacation", "Outside schedule dates", "Admin Time · Academic half-day",
               ))]
    assert blocked
    assert all(not element._event_listeners for element in blocked)
    assert all(element._props.get("role") != "button" for element in blocked)
    assert page.accessible("Edit Tuesday Morning (AM) in week 1")._props["draggable"] == "true"
    assert any(element._props.get("draggable") == "false" for element in page.elements())

    grace = next(element for element in page.elements()
                 if element.__class__.__name__ == "Item" and "Grace Hopper" in _texts(element))
    _click(grace)
    assert page.session.attending_id == "attending-002"
    assert not page.session.attending_schedule_editing
    page.button("Edit schedule")


def test_stale_assignment_dialog_does_not_overwrite_another_session(calendar_page, monkeypatch):
    from nicegui import ui

    page = calendar_page
    notifications = []
    monkeypatch.setattr(ui, "notify", lambda message, **_kwargs: notifications.append(message))
    _click(page.button("Edit schedule"))
    _click(page.accessible("Assign Friday Morning (AM) in week 1"))
    work_type = next(element for element in page.elements()
                     if element.__class__.__name__ == "Select"
                     and element._props.get("label") == "Work type")
    work_type.set_value(AttendingWorkType.ATTENDING_CLINIC.value)
    original = page.store.get(page.workspace.id)
    changed = page.store.save_instance(
        original.id,
        original.instance.revised(attendings=[
            original.instance.attendings[0].revised(name="Ada Byron"),
            original.instance.attendings[1],
        ]),
        expected_workspace_revision=original.workspace_revision,
    )

    _click(page.button("Assign half-day"))

    current = page.store.get(page.workspace.id)
    assert current.workspace_revision == changed.workspace_revision
    assert current.instance.attendings[0].name == "Ada Byron"
    assert current.instance.attending_schedules == original.instance.attending_schedules
    assert any("reload it before saving" in str(message) for message in notifications)
    assert "Ada Byron" in _texts(page.root)


@pytest.mark.parametrize("weekday,session,message", [
    (Weekday.MONDAY, Session.MORNING, "outside the attending's schedule dates"),
    (Weekday.THURSDAY, Session.MORNING, "on vacation"),
    (Weekday.WEDNESDAY, Session.AFTERNOON, "reserved as Admin Time"),
])
def test_calendar_operations_reject_protected_half_days(weekday, session, message):
    instance = _instance()
    with pytest.raises(ValueError, match=message):
        set_attending_work_half_day(
            instance, "attending-001", week=1, weekday=weekday, session=session,
            assignment=AttendingWorkHalfDay(weekday=weekday, session=session),
        )
    with pytest.raises(ValueError, match=message):
        move_attending_work_half_day(
            instance, "attending-001", week=1,
            source_weekday=Weekday.TUESDAY, source_session=Session.MORNING,
            target_weekday=weekday, target_session=session,
        )


def test_calendar_swap_preserves_descriptions_overrides_and_other_weeks():
    instance = _instance()
    added = set_attending_work_half_day(
        instance, "attending-001", week=1, weekday=Weekday.FRIDAY, session=Session.MORNING,
        assignment=AttendingWorkHalfDay(
            weekday=Weekday.FRIDAY, session=Session.MORNING,
            work_type=AttendingWorkType.ATTENDING_CLINIC,
        ),
    )
    swapped = move_attending_work_half_day(
        added, "attending-001", week=1,
        source_weekday=Weekday.TUESDAY, source_session=Session.MORNING,
        target_weekday=Weekday.FRIDAY, target_session=Session.MORNING,
    )
    weeks = swapped.attending_schedule_weeks("attending-001")
    assert weeks[0].half_days_override == 2
    assert weeks[0].half_days[-1].description == "Faculty reviews"
    assert weeks[0].half_days[1].work_type is AttendingWorkType.ATTENDING_CLINIC
    original = instance.attending_schedule_weeks("attending-001")
    assert weeks[0].half_days[0] == original[0].half_days[0]
    assert weeks[1] == instance.attending_schedule_weeks("attending-001")[1]


def _generated_schedule(instance):
    return attending_working_draft(instance, None).revised(attending_work=[
        AssignedAttendingWork(
            attending_id="attending-001", week=1,
            weekday=Weekday.FRIDAY, session=Session.MORNING,
            work_type=AttendingWorkType.ATTENDING_CLINIC,
        ),
        AssignedAttendingWork(
            attending_id="attending-002", week=1,
            weekday=Weekday.MONDAY, session=Session.MORNING,
            work_type=AttendingWorkType.INPATIENT_SERVICE,
        ),
    ])


def test_locked_manual_preceptor_still_counts_toward_generated_preceptor_limit():
    instance = _instance()
    raw = instance.model_dump(mode="json")
    clinic_id = instance.clinic_policy.primary_site_id
    site = next(site for site in raw["clinic_policy"]["sites"] if site["id"] == clinic_id)
    site["half_days"] = [{
        "weekday": Weekday.MONDAY.value, "session": Session.MORNING.value, "attendings": 1,
    }]
    instance = type(instance).model_validate(raw)
    updated, locked = set_attending_work_locked(
        instance, None, "attending-001", week=2,
        weekday=Weekday.MONDAY, session=Session.MORNING, locked=True,
    )
    assert not validate_schedule(updated, locked).errors
    overflow = locked.revised(attending_work=[
        *locked.attending_work,
        AssignedAttendingWork(
            attending_id="attending-002", week=2,
            weekday=Weekday.MONDAY, session=Session.MORNING,
            work_type=AttendingWorkType.PRECEPTING_CLINIC, clinic_id=clinic_id,
        ),
    ])
    assert any("2 attendings precept, but at most 1 may" in error
               for error in validate_schedule(updated, overflow).errors)


def test_case_work_lock_preserves_visible_work_and_round_trips_with_solver_lock(tmp_path):
    from rbs.store import Store
    from rbs.workspaces import WorkspaceController

    instance = _instance()
    original = _generated_schedule(instance)
    updated, schedule = set_attending_work_locked(
        instance, original, "attending-001", week=2,
        weekday=Weekday.MONDAY, session=Session.MORNING, locked=True,
    )
    attending = next(person for person in instance.attendings if person.id == "attending-001")
    assert effective_attending_schedule_week(instance, attending, 2, original).assignments == (
        effective_attending_schedule_week(updated, attending, 2, schedule).assignments
    )
    assert updated.attending_schedule_weeks(attending.id)[0] == (
        instance.attending_schedule_weeks(attending.id)[0]
    )
    assert not updated.attending_schedule_weeks(attending.id)[1].half_days
    assert all(item in schedule.attending_work for item in original.attending_work)
    assert len(schedule.attending_work) == len(original.attending_work) + 1
    assert original.attending_work[1] in schedule.attending_work  # other attending
    locked = next(item for item in schedule.attending_work if item.week == 2)
    assert locked.locked and locked.manual_override
    facts = attending_week_facts(SolverProblem.from_instance(updated), schedule)[2][0]
    assert locked in facts.locked_work
    assert (Weekday.MONDAY, Session.MORNING) not in facts.open_half_days

    store = Store(tmp_path / "source.sqlite")
    store.init()
    workspace = store.create("Attending locks", instance)
    saved = WorkspaceController(store).save_instance(workspace, updated, draft_schedule=schedule)
    assert saved.instance_revision == workspace.instance_revision + 1
    restored = Store(tmp_path / "restored.sqlite")
    restored.init()
    restored.restore_rbsc(store.export_rbsc())
    reloaded = restored.get(workspace.id)
    assert reloaded.schedule.attending_work == schedule.attending_work
    assert reloaded.instance.attending_schedules == updated.attending_schedules
    assert locked in attending_week_facts(
        SolverProblem.from_instance(reloaded.instance), reloaded.schedule,
    )[2][0].locked_work


def test_automatic_lock_unlock_and_relock_persist_exemptions():
    instance = _instance().revised(lock_through_today=True)
    schedule = _generated_schedule(instance)
    today = instance.calendar.first_week_start + timedelta(days=5)
    item = schedule.attending_work[0]
    assert attending_work_is_locked(instance, item, today=today)

    updated, unlocked = set_attending_work_locked(
        instance, schedule, item.attending_id, week=item.week,
        weekday=item.weekday, session=item.session, locked=False, today=today,
    )
    exempt = unlocked.attending_work[0]
    assert updated == instance
    assert exempt.automatic_lock_exempt and not exempt.locked
    assert not attending_work_is_locked(updated, exempt, today=today)
    facts = attending_week_facts(SolverProblem.from_instance(updated), unlocked, today=today)[1][0]
    assert exempt not in facts.locked_work
    assert facts.reference[exempt.weekday, exempt.session] == exempt
    assert set_attending_work_locked(
        updated, unlocked, item.attending_id, week=item.week,
        weekday=item.weekday, session=item.session, locked=False, today=today,
    ) == (updated, unlocked)

    _, relocked = set_attending_work_locked(
        updated, unlocked, item.attending_id, week=item.week,
        weekday=item.weekday, session=item.session, locked=True, today=today,
    )
    assert relocked.attending_work[0].locked
    assert not relocked.attending_work[0].automatic_lock_exempt

    # A dated unlock of hand-entered work must survive saving too.
    raw_updated, raw_unlocked = set_attending_work_locked(
        instance, None, "attending-001", week=2, weekday=Weekday.MONDAY,
        session=Session.MORNING, locked=False, today=today + timedelta(days=7),
    )
    assert not raw_updated.attending_schedule_weeks("attending-001")[1].half_days
    assert raw_unlocked.attending_work[0].automatic_lock_exempt


def test_lock_changes_preserve_an_existing_solved_schedule(tmp_path):
    from rbs.store import Store
    from rbs.workspaces import WorkspaceController

    instance = _instance()
    draft = _generated_schedule(instance)
    solved = draft.revised(meta=draft.meta.revised(
        status=SolverStatus.FEASIBLE, solver_status=SolverStatus.FEASIBLE,
    ))
    store = Store(tmp_path / "solved.sqlite")
    store.init()
    original = store.create("Solved calendar", instance, schedule=solved)
    updated, locked = set_attending_work_locked(
        instance, solved, "attending-001", week=2,
        weekday=Weekday.MONDAY, session=Session.MORNING, locked=True,
    )
    saved = WorkspaceController(store).save_instance(original, updated, draft_schedule=locked)
    assert saved.schedule.meta.status is SolverStatus.FEASIBLE
    assert not saved.solution_is_out_of_date
    assert saved.schedule.assignments == solved.assignments
    assert attending_schedule_report(updated, schedule=locked) == (
        attending_schedule_report(instance, schedule=solved)
    )


@pytest.mark.parametrize("action", ["edit", "remove", "move_source", "move_target"])
def test_locked_work_rejects_calendar_mutations(action):
    instance = _instance()
    instance, schedule = set_attending_work_locked(
        instance, _generated_schedule(instance), "attending-001", week=1,
        weekday=Weekday.FRIDAY, session=Session.MORNING, locked=True,
    )
    with pytest.raises(ValueError, match="locked. Unlock it"):
        if action in {"edit", "remove"}:
            set_attending_schedule_half_day(
                instance, schedule, "attending-001", week=1, weekday=Weekday.FRIDAY,
                session=Session.MORNING,
                assignment=None if action == "remove" else AttendingWorkHalfDay(
                    weekday=Weekday.FRIDAY, session=Session.MORNING,
                    work_type=AttendingWorkType.INPATIENT_SERVICE,
                ),
            )
        else:
            source, target = (
                (Weekday.FRIDAY, Weekday.TUESDAY) if action == "move_source"
                else (Weekday.TUESDAY, Weekday.FRIDAY)
            )
            move_attending_schedule_half_day(
                instance, schedule, "attending-001", week=1,
                source_weekday=source, source_session=Session.MORNING,
                target_weekday=target, target_session=Session.MORNING,
            )


def test_bulk_locks_skip_masked_work_and_academic_admin_and_preserve_other_attending():
    instance = _instance()
    original = _generated_schedule(instance)
    updated, schedule = set_attending_schedule_locked(
        instance, original, "attending-001", locked=True,
    )
    first_week = updated.attending_schedule_weeks("attending-001")[0]
    assert [item.description for item in first_week.half_days] == [
        "Stored work before schedule dates",
    ]
    assert updated.attending_schedule_weeks("attending-001")[0].half_days_override == 2
    own = [item for item in schedule.attending_work if item.attending_id == "attending-001"]
    assert len(own) == 3 and all(item.locked for item in own)
    assert not any(item.weekday is Weekday.WEDNESDAY and item.session is Session.AFTERNOON
                   for item in own)
    assert original.attending_work[1] in schedule.attending_work

    updated, unlocked = set_attending_schedule_locked(
        updated, schedule, "attending-001", locked=False,
    )
    assert all(not item.locked for item in unlocked.attending_work)
    assert original.attending_work[1] in unlocked.attending_work


def test_unlocked_generated_work_can_be_swapped_edited_and_removed_without_losing_other_work():
    instance = _instance()
    original = _generated_schedule(instance)
    updated, moved = move_attending_schedule_half_day(
        instance, original, "attending-001", week=1,
        source_weekday=Weekday.FRIDAY, source_session=Session.MORNING,
        target_weekday=Weekday.TUESDAY, target_session=Session.MORNING,
    )
    attending = next(person for person in updated.attendings if person.id == "attending-001")
    effective = effective_attending_schedule_week(updated, attending, 1, moved)
    assert effective.assignment_on(Weekday.TUESDAY, Session.MORNING).work_type is (
        AttendingWorkType.ATTENDING_CLINIC
    )
    assert effective.assignment_on(Weekday.FRIDAY, Session.MORNING).description == "Faculty reviews"
    assert original.attending_work[1] in moved.attending_work
    assert updated.attending_schedule_weeks("attending-001")[0].half_days_override == 2
    assert not moved.meta.validation_errors
    assert moved.meta.status is SolverStatus.UNKNOWN

    updated, changed = set_attending_schedule_half_day(
        updated, moved, "attending-001", week=1, weekday=Weekday.TUESDAY,
        session=Session.MORNING, assignment=AttendingWorkHalfDay(
            weekday=Weekday.TUESDAY, session=Session.MORNING,
            work_type=AttendingWorkType.ADMIN_TIME,
        ),
    )
    effective = effective_attending_schedule_week(updated, attending, 1, changed)
    assert effective.assignment_on(
        Weekday.TUESDAY, Session.MORNING,
    ).work_type is AttendingWorkType.ADMIN_TIME
    updated, removed = set_attending_schedule_half_day(
        updated, changed, "attending-001", week=1, weekday=Weekday.TUESDAY,
        session=Session.MORNING, assignment=None,
    )
    assert original.attending_work[1] in removed.attending_work
    effective = effective_attending_schedule_week(updated, attending, 1, removed)
    assert effective.assignment_on(
        Weekday.TUESDAY, Session.MORNING,
    ) is None


def test_calendar_locks_persist_and_protect_edit_controls(calendar_page):
    page = calendar_page
    directory = next(element for element in page.elements()
                     if "rbs-master-directory" in element._classes)
    _click(page.button("Edit schedule"))
    weeks = [element for element in page.elements()
             if "rbs-resident-clinic-week" in element._classes]
    other_week = weeks[1]
    _click(page.accessible("Lock work half-day: Tuesday Morning (AM) in week 1"))
    saved = page.store.get(page.workspace.id)
    assert saved.schedule.attending_work[0].locked
    assert saved.schedule.attending_work[0].description == "Faculty reviews"
    assert not directory.is_deleted and not other_week.is_deleted
    assert not any(element._props.get("aria-label") == "Edit Tuesday Morning (AM) in week 1"
                   and element._props.get("draggable") == "true" for element in page.elements())
    page.accessible("Unlock work half-day: Tuesday Morning (AM) in week 1")

    _click(page.button("Return to view"))
    assert any("is-locked" in element._classes
               and any("Faculty reviews" in text for text in _texts(element))
               for element in page.elements())
    _click(page.button("Edit schedule"))
    _click(page.accessible("Unlock work half-day: Tuesday Morning (AM) in week 1"))
    assert not page.store.get(page.workspace.id).schedule.attending_work[0].locked
    assert page.accessible("Edit Tuesday Morning (AM) in week 1")._props["draggable"] == "true"
    _save_assignment(page, Weekday.FRIDAY, 1)
    _click(page.button("Lock all work"))
    _click(page.button("Unlock all work"))
    latest = page.store.get(page.workspace.id)
    assert all(not item.locked for item in latest.schedule.attending_work)
    assert not directory.is_deleted
    assert page.session.attending_schedule_editing
    _click(page.button("Return to view"))
    _click(page.button("Edit attending"))
    _click(page.button("Save changes"))
    assert page.store.get(page.workspace.id).latest_schedule.attending_work == (
        latest.schedule.attending_work
    )


def test_lock_conflict_reloads_without_overwriting_another_session(calendar_page, monkeypatch):
    from nicegui import ui

    page = calendar_page
    notifications = []
    monkeypatch.setattr(ui, "notify", lambda message, **_kwargs: notifications.append(message))
    _click(page.button("Edit schedule"))
    snapshot = page.store.get(page.workspace.id)
    changed = page.store.save_instance(
        snapshot.id, snapshot.instance.revised(attendings=[
            snapshot.instance.attendings[0].revised(name="Ada Byron"),
            snapshot.instance.attendings[1],
        ]), expected_workspace_revision=snapshot.workspace_revision,
    )
    _click(page.accessible("Lock work half-day: Tuesday Morning (AM) in week 1"))
    current = page.store.get(page.workspace.id)
    assert current.workspace_revision == changed.workspace_revision
    assert current.instance.attending_schedules == snapshot.instance.attending_schedules
    assert current.schedule is None
    assert "Ada Byron" in _texts(page.root)
    assert any("reload it before saving" in str(message) for message in notifications)


def test_generated_work_is_included_in_schedule_checks_and_masks_stale_placements():
    instance = _instance()
    schedule = _generated_schedule(instance)
    attending = next(person for person in instance.attendings if person.id == "attending-001")
    effective = effective_attending_schedule_week(instance, attending, 1, schedule)
    assert effective.is_scheduled(Weekday.FRIDAY, Session.MORNING)
    assert effective.assigned_half_days == 3  # manual, generated, academic Admin
    report = attending_schedule_report(instance, attending_id=attending.id, schedule=schedule)
    assert any("3 of 2" in issue.message for issue in report.errors)
    holiday = attending.revised(vacation_ranges=[*attending.vacation_ranges, AttendingVacation(
        start_date=effective.week_start + timedelta(days=4),
        end_date=effective.week_start + timedelta(days=4),
    )])
    on_vacation = instance.revised(attendings=[
        holiday if person.id == holiday.id else person for person in instance.attendings
    ])
    assert effective_attending_schedule_week(on_vacation, holiday, 1, schedule).assignment_on(
        Weekday.FRIDAY, Session.MORNING,
    ) is None


def test_locks_keep_weekend_work_in_the_program_calendar_and_exports():
    from io import BytesIO

    from pypdf import PdfReader

    from rbs.ui.clinic.board import render_clinic_html
    from rbs.ui.clinic.projection import attending_occupancy
    from rbs.ui.clinic.schedule_csv import build_clinic_schedule_csv
    from rbs.ui.clinic.schedule_pdf import build_clinic_schedule_pdf

    instance = set_attending_work_half_day(
        _instance(), "attending-001", week=1, weekday=Weekday.SATURDAY,
        session=Session.MORNING, assignment=AttendingWorkHalfDay(
            weekday=Weekday.SATURDAY, session=Session.MORNING,
            description="Saturday faculty coverage",
        ),
    )
    original = _generated_schedule(instance)
    updated, locked = set_attending_schedule_locked(
        instance, original, "attending-001", locked=True,
    )

    assert attending_occupancy(updated, locked) == attending_occupancy(instance, original)
    assert render_clinic_html(updated, locked, view="attendings") == (
        render_clinic_html(instance, original, view="attendings")
    )
    exported = build_clinic_schedule_csv(updated, locked, view="attendings")
    assert exported == build_clinic_schedule_csv(instance, original, view="attendings")
    assert "Saturday faculty coverage" in exported and "Sat AM" in exported
    pdf = PdfReader(BytesIO(build_clinic_schedule_pdf(updated, locked, view="attendings")))
    first_page = " ".join(pdf.pages[0].extract_text().split())
    assert "Saturday faculty coverage" in first_page
    assert "SAT" in first_page


def test_generated_calendar_lock_changes_do_not_advance_instance_revision(calendar_page):
    page = calendar_page
    schedule = _generated_schedule(page.workspace.instance)
    saved = page.store.save_schedule(
        page.workspace.id, schedule,
        expected_workspace_revision=page.workspace.workspace_revision,
        expected_instance_revision=page.workspace.instance_revision,
    )
    page.session.refresh_panel("attendings")
    _click(page.button("Edit schedule"))
    _click(page.accessible("Lock work half-day: Friday Morning (AM) in week 1"))
    current = page.store.get(page.workspace.id)
    assert current.instance_revision == saved.instance_revision
    assert current.workspace_revision == saved.workspace_revision + 1
    assert current.schedule.attending_work[0].locked
    assert current.instance.attending_schedules == saved.instance.attending_schedules

    _click(page.accessible("Unlock work half-day: Friday Morning (AM) in week 1"))
    _click(page.accessible("Edit Friday Morning (AM) in week 1"))
    _click(page.button("Remove assignment"))
    removed = page.store.get(page.workspace.id)
    assert len(removed.schedule.attending_work) == 1  # other attending remains
    assert removed.instance_revision == saved.instance_revision
    assert removed.workspace_revision == saved.workspace_revision + 3


def test_locking_a_stale_schedule_after_vacation_edits_keeps_compatible_work(calendar_page):
    page = calendar_page
    prior = _generated_schedule(page.workspace.instance)
    saved = page.store.save_schedule(
        page.workspace.id, prior,
        expected_workspace_revision=page.workspace.workspace_revision,
        expected_instance_revision=page.workspace.instance_revision,
    )
    person = next(item for item in saved.instance.attendings if item.id == "attending-001")
    day = saved.instance.calendar.first_week_start + timedelta(days=4)
    vacationing = person.revised(vacation_ranges=[*person.vacation_ranges, AttendingVacation(
        start_date=day, end_date=day,
    )])
    page.store.save_instance(
        saved.id, saved.instance.revised(attendings=[
            vacationing if item.id == vacationing.id else item for item in saved.instance.attendings
        ]), expected_workspace_revision=saved.workspace_revision,
    )
    assert page.store.get(saved.id).solution_is_out_of_date
    page.session.refresh_panel("attendings")
    _click(page.button("Edit schedule"))
    _click(page.accessible("Lock work half-day: Tuesday Morning (AM) in week 1"))
    current = page.store.get(saved.id)
    assert not current.solution_is_out_of_date
    assert current.schedule.is_working_draft
    assert any(item.locked and item.weekday is Weekday.TUESDAY
               for item in current.schedule.attending_work)
    assert prior.attending_work[1] in current.schedule.attending_work  # other attending remains
    assert prior.attending_work[0] not in current.schedule.attending_work  # vacation masks it
    assert any("vacation" in note for note in current.schedule.meta.notes)
