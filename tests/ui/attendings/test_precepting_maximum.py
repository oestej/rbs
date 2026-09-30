"""Hand edits may put a clinic over its Max attendings, with an explanation."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from rbs.catalog import blank_instance
from rbs.models.attending import (
    Attending,
    AttendingSchedule,
    AttendingWeeklyWorkSchedule,
    AttendingWorkHalfDay,
    AttendingWorkType,
)
from rbs.models.clinic_site import ClinicStaffingMode
from rbs.models.enums import Session, SolverStatus, Weekday
from rbs.models.schedule import AssignedAttendingWork
from rbs.ui.attendings.ops import (
    attending_working_draft,
    precepting_maximum_marker,
    precepting_maximum_warning,
    precepting_over_maximum,
    set_attending_schedule_half_day,
    set_attending_work_locked,
)

MONDAY_AM = (Weekday.MONDAY, Session.MORNING)
TUESDAY_AM = (Weekday.TUESDAY, Session.MORNING)


def _precepting(weekday: Weekday, session: Session) -> AttendingWorkHalfDay:
    return AttendingWorkHalfDay(
        weekday=weekday, session=session,
        work_type=AttendingWorkType.PRECEPTING_CLINIC, clinic_id="clinic",
    )


def _instance(
    *,
    staffing_mode: ClinicStaffingMode = ClinicStaffingMode.ATTENDING_MANAGED,
    maximum: int = 1,
    override: int | None = None,
    closed: bool = False,
):
    """Maple Clinic allows ``maximum`` preceptors on Monday and Tuesday mornings.

    Grace already precepts there on Monday of week 1; Ada precepts on Tuesday.
    """
    instance = blank_instance()
    start = instance.calendar.first_week_start
    raw = instance.model_dump(mode="json")
    site = raw["clinic_policy"]["sites"][0]
    site.update(
        name="Maple Clinic",
        staffing_mode=staffing_mode.value,
        half_days=[
            {"weekday": weekday.value, "session": session.value, "attendings": maximum}
            for weekday, session in (MONDAY_AM, TUESDAY_AM)
        ],
        capacity_overrides=(
            [{"date": start.isoformat(), "session": "morning", "attendings": override}]
            if override is not None else []
        ),
    )
    raw["clinic_policy"]["closure_days"] = (
        [{"date": start.isoformat(), "sites": ["clinic"], "name": "Staff retreat"}]
        if closed else []
    )
    raw["attendings"] = [
        Attending(id="attending-001", name="Ada Lovelace").model_dump(mode="json"),
        Attending(id="attending-002", name="Grace Hopper").model_dump(mode="json"),
    ]
    raw["attending_schedules"] = [
        AttendingSchedule(attending_id="attending-001", weeks=[AttendingWeeklyWorkSchedule(
            week=1, half_days=[_precepting(*TUESDAY_AM)],
        )]).model_dump(mode="json"),
        AttendingSchedule(attending_id="attending-002", weeks=[AttendingWeeklyWorkSchedule(
            week=1, half_days=[_precepting(*MONDAY_AM)],
        )]).model_dump(mode="json"),
    ]
    return type(instance).model_validate(raw)


def _assign_ada_monday(instance, schedule=None):
    return set_attending_schedule_half_day(
        instance, schedule, "attending-001", week=1,
        weekday=Weekday.MONDAY, session=Session.MORNING,
        assignment=_precepting(*MONDAY_AM),
    )


def _warning(instance, schedule, attending_id="attending-001"):
    return precepting_maximum_warning(
        instance, schedule, attending_id, week=1, weekday=Weekday.MONDAY, session=Session.MORNING,
    )


def test_over_maximum_counts_every_attendings_hand_entered_and_schedule_work():
    instance = _instance()
    assert precepting_over_maximum(instance, None, 1) == {}

    updated, _schedule = _assign_ada_monday(instance)
    assert precepting_over_maximum(updated, None, 1) == {
        ("clinic", Weekday.MONDAY, Session.MORNING): (2, 1),
    }
    assert precepting_over_maximum(updated, None, 2) == {}

    # Work in the solved schedule counts the same way as hand-entered work.
    schedule = attending_working_draft(instance, None).revised(attending_work=[
        AssignedAttendingWork(attending_id="attending-001", week=1, **_precepting(
            *MONDAY_AM,
        ).model_dump()),
    ])
    assert precepting_over_maximum(instance, schedule, 1) == {
        ("clinic", Weekday.MONDAY, Session.MORNING): (2, 1),
    }
    assert precepting_over_maximum(_instance(maximum=2), schedule, 1) == {}


def test_warning_explains_a_hand_entered_override_and_how_to_allow_it():
    instance, schedule = _assign_ada_monday(_instance())

    assert _warning(instance, schedule) == (
        "Maple Clinic allows at most 1 attending to precept on Mon, Jun 29 AM, but 2 "
        "attendings precept there. Saved as a manual override. To allow it, raise Maple "
        "Clinic's Max attendings for that date with an exception in the Clinic tab."
    )
    # Grace's work is at the same over-full half-day, so it is explained too.
    assert _warning(instance, schedule, "attending-002") == _warning(instance, schedule)
    # Ada's Tuesday work is within the maximum.
    assert precepting_maximum_warning(
        instance, schedule, "attending-001", week=1,
        weekday=Weekday.TUESDAY, session=Session.MORNING,
    ) is None

    capacity_managed, _ = _assign_ada_monday(
        _instance(staffing_mode=ClinicStaffingMode.CAPACITY_MANAGED),
    )
    assert "raise Maple Clinic's attending coverage for that date" in _warning(
        capacity_managed, None,
    )


def test_warning_asks_to_lock_unlocked_schedule_work_before_the_next_solve():
    instance = _instance()
    generated = attending_working_draft(instance, None).revised(attending_work=[
        AssignedAttendingWork(
            attending_id="attending-001", week=1, weekday=Weekday.MONDAY,
            session=Session.MORNING, work_type=AttendingWorkType.INPATIENT_SERVICE,
        ),
    ])
    instance, schedule = _assign_ada_monday(instance, generated)
    edited = schedule.attending_work[0]
    assert edited.manual_override and not edited.locked

    assert _warning(instance, schedule) == (
        "Maple Clinic allows at most 1 attending to precept on Mon, Jun 29 AM, but 2 "
        "attendings precept there. Saved as a manual override. Lock this work half-day to "
        "keep it through the next solve, or raise Maple Clinic's Max attendings for that "
        "date with an exception in the Clinic tab to allow it."
    )

    instance, schedule = set_attending_work_locked(
        instance, schedule, "attending-001", week=1,
        weekday=Weekday.MONDAY, session=Session.MORNING, locked=True,
    )
    assert _warning(instance, schedule).endswith(
        "Saved as a manual override. To allow it, raise Maple Clinic's Max attendings "
        "for that date with an exception in the Clinic tab."
    )


def test_dated_exceptions_and_closures_set_the_maximum_for_one_date():
    raised, _ = _assign_ada_monday(_instance(override=2))
    assert precepting_over_maximum(raised, None, 1) == {}

    stopped, _ = _assign_ada_monday(_instance(override=0))
    assert precepting_over_maximum(stopped, None, 1) == {
        ("clinic", Weekday.MONDAY, Session.MORNING): (2, 0),
    }
    assert _warning(stopped, None).startswith(
        "Maple Clinic allows no precepting on Mon, Jun 29 AM, but 2 attendings precept there."
    )

    closed, _ = _assign_ada_monday(_instance(closed=True))
    # A closure has no maximum to raise, so the message only reports it.
    assert _warning(closed, None) == (
        "Maple Clinic is closed on Mon, Jun 29 AM, but 2 attendings precept there. "
        "Saved as a manual override."
    )
    first_monday = closed.calendar.first_week_start
    over = precepting_over_maximum(closed, None, 1)
    assert precepting_maximum_marker(
        closed, over, "clinic", first_monday, Session.MORNING,
    ) == "Maple Clinic is closed on this half-day, but 2 attendings precept there."
    assert precepting_maximum_marker(
        closed, over, "clinic", first_monday + timedelta(days=1), Session.MORNING,
    ) is None


def test_warning_ignores_work_that_does_not_precept():
    instance, schedule = set_attending_schedule_half_day(
        _instance(maximum=1), None, "attending-001", week=1,
        weekday=Weekday.MONDAY, session=Session.MORNING,
        assignment=AttendingWorkHalfDay(
            weekday=Weekday.MONDAY, session=Session.MORNING,
            work_type=AttendingWorkType.ADMIN_TIME,
        ),
    )
    assert precepting_over_maximum(instance, schedule, 1) == {}
    assert _warning(instance, schedule) is None


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


def _open_calendar(tmp_path, monkeypatch, instance, schedule=None):
    """Ada's calendar in the Attendings tab of a stored workspace."""
    from nicegui import ui

    from rbs.store import Store
    from rbs.ui.app_shell import _render_tab
    from rbs.ui.session import WorkspaceSession

    notifications: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        ui, "notify",
        lambda message, **kwargs: notifications.append((str(message), kwargs.get("type"))),
    )
    store = Store(tmp_path / "maple.sqlite")
    store.init()
    workspace = store.create("Maple precepting", instance)
    if schedule is not None:
        workspace = store.save_schedule(
            workspace.id, schedule,
            expected_workspace_revision=workspace.workspace_revision,
            expected_instance_revision=workspace.instance_revision,
        )
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

    def cell(weekday: Weekday, week: int = 1):
        """The calendar cell holding ``weekday`` morning work in ``week``."""
        event = accessible(f"Edit {weekday.value.title()} Morning (AM) in week {week}")
        return next(
            element for element in elements()
            if "rbs-resident-clinic-session-cell" in element._classes
            and event in element.descendants()
        )

    def assign(weekday: Weekday, work_type: AttendingWorkType, week: int = 1):
        before = {element.id for element in elements()}
        _click(accessible(f"Assign {weekday.value.title()} Morning (AM) in week {week}"))
        dialog = [element for element in elements() if element.id not in before]
        next(element for element in dialog
             if element.__class__.__name__ == "Select"
             and element._props.get("label") == "Work type").set_value(work_type.value)
        _click(next(element for element in dialog
                    if element.__class__.__name__ == "Button"
                    and element._props.get("label") == "Assign half-day"))

    return SimpleNamespace(
        store=store, workspace=workspace, root=root, notifications=notifications,
        elements=elements, button=button, accessible=accessible, cell=cell, assign=assign,
    )


@pytest.fixture
def maple_page(tmp_path, monkeypatch):
    page = _open_calendar(tmp_path, monkeypatch, _instance())
    yield page
    page.root.delete()


def _maximum_markers(cell) -> list[str]:
    return [
        text
        for icon in cell.descendants()
        if "rbs-resident-clinic-conflict-icon" in icon._classes
        for text in _texts(icon)
    ]


def test_assigning_precepting_over_the_maximum_saves_warns_and_marks_the_calendar(maple_page):
    page = maple_page
    _click(page.button("Edit schedule"))
    page.assign(Weekday.MONDAY, AttendingWorkType.PRECEPTING_CLINIC)

    # The override is saved rather than rejected.
    saved = page.store.get(page.workspace.id).instance
    assert _precepting(*MONDAY_AM) in saved.attending_schedule_weeks("attending-001")[0].half_days
    assert page.notifications == [(_warning(saved, None), "warning")]
    assert "Saved as a manual override" in page.notifications[0][0]

    marker = (
        "Maple Clinic allows at most 1 attending to precept on this half-day, but 2 "
        "attendings precept there."
    )
    monday = page.cell(Weekday.MONDAY)
    assert "has-manual-override" in monday._classes
    assert _maximum_markers(monday) == [marker]
    assert marker in monday._props["aria-label"]
    tuesday = page.cell(Weekday.TUESDAY)
    assert "has-manual-override" not in tuesday._classes
    assert _maximum_markers(tuesday) == []

    # Viewing the schedule keeps the explanation.
    _click(page.button("Return to view"))
    assert _maximum_markers(next(
        element for element in page.elements()
        if "rbs-resident-clinic-session-cell" in element._classes
        and marker in element._props.get("aria-label", "")
    )) == [marker]


def test_moving_precepting_over_the_maximum_warns_instead_of_confirming(maple_page):
    page = maple_page
    _click(page.button("Edit schedule"))
    target = page.accessible("Assign Monday Morning (AM) in week 1")
    drop = next(listener for listener in target._event_listeners.values()
                if listener.type == "drop")
    drop.handler(SimpleNamespace(args={
        "scope": "attending-attending-001", "week": 1,
        "weekday": Weekday.TUESDAY.value, "session": Session.MORNING.value,
    }))

    saved = page.store.get(page.workspace.id).instance
    assert saved.attending_schedule_weeks("attending-001")[0].half_days == [
        _precepting(*MONDAY_AM),
    ]
    assert page.notifications == [(_warning(saved, None), "warning")]
    assert _maximum_markers(page.cell(Weekday.MONDAY))

    # Moving it back is within the maximum, so the move is simply confirmed.
    target = page.accessible("Assign Tuesday Morning (AM) in week 1")
    drop = next(listener for listener in target._event_listeners.values()
                if listener.type == "drop")
    drop.handler(SimpleNamespace(args={
        "scope": "attending-attending-001", "week": 1,
        "weekday": Weekday.MONDAY.value, "session": Session.MORNING.value,
    }))
    assert page.notifications[-1] == ("Work half-day moved", "positive")
    assert not _maximum_markers(page.cell(Weekday.TUESDAY))


def test_precepting_beside_solved_work_is_saved_as_a_draft_override(tmp_path, monkeypatch):
    # Grace precepts at Maple on Monday morning in the solved schedule.
    instance = _instance().revised(attending_schedules=[])
    solved = attending_working_draft(instance, None).revised(attending_work=[
        AssignedAttendingWork(attending_id="attending-002", week=1, **_precepting(
            *MONDAY_AM,
        ).model_dump()),
    ])
    solved = solved.revised(meta=solved.meta.revised(
        status=SolverStatus.FEASIBLE, solver_status=SolverStatus.FEASIBLE,
    ))
    page = _open_calendar(tmp_path, monkeypatch, instance, solved)
    try:
        _click(page.button("Edit schedule"))
        page.assign(Weekday.MONDAY, AttendingWorkType.PRECEPTING_CLINIC)

        saved = page.store.get(page.workspace.id)
        assert _precepting(*MONDAY_AM) in (
            saved.instance.attending_schedule_weeks("attending-001")[0].half_days
        )
        # Grace's work stays in the draft; the next solve decides whether it moves.
        assert saved.schedule.is_working_draft
        assert saved.schedule.attending_work == solved.attending_work
        assert page.notifications == [(_warning(saved.instance, saved.schedule), "warning")]
        assert _maximum_markers(page.cell(Weekday.MONDAY))
    finally:
        page.root.delete()


def test_unlocking_a_solved_over_maximum_override_keeps_the_schedule_valid(tmp_path):
    from rbs.solver.validation import validate_schedule
    from rbs.store import Store
    from rbs.workspaces import WorkspaceController

    # Ada's second preceptor at Maple on Monday is a hand edit of solved work,
    # locked through a solve that therefore had to keep it over the maximum.
    instance = _instance()
    generated = attending_working_draft(instance, None).revised(attending_work=[
        AssignedAttendingWork(
            attending_id="attending-001", week=1, weekday=Weekday.MONDAY,
            session=Session.MORNING, work_type=AttendingWorkType.INPATIENT_SERVICE,
        ),
    ])
    instance, schedule = _assign_ada_monday(instance, generated)
    instance, locked = set_attending_work_locked(
        instance, schedule, "attending-001", week=1,
        weekday=Weekday.MONDAY, session=Session.MORNING, locked=True,
    )
    solved = locked.revised(meta=locked.meta.revised(
        status=SolverStatus.FEASIBLE, solver_status=SolverStatus.FEASIBLE,
    ))
    assert not validate_schedule(instance, solved).errors

    store = Store(tmp_path / "maximum.sqlite")
    store.init()
    workspace = store.create("Maple maximum", instance, schedule=solved)
    instance, unlocked = set_attending_work_locked(
        instance, solved, "attending-001", week=1,
        weekday=Weekday.MONDAY, session=Session.MORNING, locked=False,
    )
    assert precepting_over_maximum(instance, unlocked, 1)
    assert not validate_schedule(instance, unlocked).errors
    saved = WorkspaceController(store).save_schedule(workspace, unlocked)
    assert saved.schedule.meta.status is SolverStatus.FEASIBLE
    assert not saved.schedule.attending_work[0].locked


def test_solved_precepting_the_solve_placed_is_still_held_to_the_maximum():
    from rbs.solver.validation import validate_schedule

    instance = _instance()
    placed = attending_working_draft(instance, None).revised(attending_work=[
        AssignedAttendingWork(attending_id="attending-001", week=1, **_precepting(
            *MONDAY_AM,
        ).model_dump()),
    ])
    solved = placed.revised(meta=placed.meta.revised(
        status=SolverStatus.FEASIBLE, solver_status=SolverStatus.FEASIBLE,
    ))
    assert any(
        "2 attendings precept, but at most 1 may" in error
        for error in validate_schedule(instance, solved).errors
    )
