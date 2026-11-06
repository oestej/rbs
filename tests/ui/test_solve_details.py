"""Warnings remain discoverable after edits, solves and reopening a workspace."""

import asyncio
from types import SimpleNamespace

from rbs.catalog import blank_instance
from rbs.models.attending import Attending, AttendingVacation, AttendingWorkType
from rbs.models.enums import Session, SolverStatus, Weekday
from rbs.models.schedule import AssignedAttendingWork
from rbs.store import Store
from rbs.ui.attendings.ops import attending_working_draft
from rbs.ui.session import WorkspaceSession
from rbs.ui.solve_details import (
    open_solve_details,
    refresh_solve_details_button,
    solve_details_button,
)
from rbs.workspaces import WorkspaceController


def _workspace(tmp_path, *, vacation=True):
    instance = blank_instance()
    first = instance.calendar.first_week_start
    person = Attending(
        id="one",
        name="Ada Lovelace",
        vacation_ranges=[
            AttendingVacation(start_date=first, end_date=first),
        ]
        if vacation
        else [],
    )
    instance = instance.revised(attendings=[person])
    work = AssignedAttendingWork(
        attending_id=person.id,
        week=1,
        weekday=Weekday.MONDAY,
        session=Session.MORNING,
        work_type=AttendingWorkType.ADMIN_TIME,
        hand_entered=True,
        manual_override=True,
        locked=True,
    )
    draft = attending_working_draft(instance, None).revised(attending_work=[work])
    draft = draft.revised(meta=draft.meta.revised(notes=["Saved solver note", "Saved solver note"]))
    store = Store(tmp_path / "details.sqlite")
    store.init()
    workspace = WorkspaceController(store).save_schedule(store.create("Details", instance), draft)
    return store, workspace


def _new_elements(before):
    from nicegui import ui

    return [
        element
        for key, element in ui.context.client.elements.items()
        if key not in before and not element.is_deleted
    ]


def test_details_show_named_warnings_and_collapsed_deduplicated_notes_after_reopen(tmp_path):
    from nicegui import ui

    store, workspace = _workspace(tmp_path)
    reopened = store.import_workspace_rbsc(store.export_workspace_rbsc(workspace.id))[0]
    session = WorkspaceSession(store, workspace_id=reopened.id)
    solve_details_button(session)
    assert session.solve_details_button.text == "Solve details · 1 warning"
    before = set(ui.context.client.elements)
    open_solve_details(session)
    elements = _new_elements(before)
    labels = [(getattr(element, "_text", "") or "") for element in elements]
    assert any(
        "Ada Lovelace" in text and "vacation" in text and "preserved" in text for text in labels
    )
    assert labels.count("Saved solver note") == 1
    expansion = next(element for element in elements if element.__class__.__name__ == "Expansion")
    assert expansion.value is False
    person = reopened.instance.attendings[0].revised(vacation_ranges=[])
    WorkspaceController(store).save_instance(
        reopened, reopened.instance.revised(attendings=[person])
    )
    refresh_solve_details_button(session)
    assert session.solve_details_button.text == "Solve details"


def test_input_edit_warns_once_when_it_first_hides_protected_work(tmp_path, monkeypatch):
    from nicegui import ui

    store, workspace = _workspace(tmp_path, vacation=False)
    session = WorkspaceSession(store, workspace_id=workspace.id)
    notifications = []
    monkeypatch.setattr(
        ui, "notify", lambda message, **kwargs: notifications.append((message, kwargs))
    )
    first = workspace.instance.calendar.first_week_start
    person = workspace.instance.attendings[0].revised(
        vacation_ranges=[
            AttendingVacation(start_date=first, end_date=first),
        ]
    )
    saved = session.persist_instance(
        workspace, workspace.instance.revised(attendings=[person]), refresh=False
    )
    assert len(notifications) == 1
    assert "Ada Lovelace" in notifications[0][0] and "preserved" in notifications[0][0]
    assert notifications[0][1]["type"] == "warning"
    session.persist_instance(saved, saved.instance, refresh=False)
    assert len(notifications) == 1


def test_successful_solve_with_hidden_work_shows_warning_and_keeps_the_work(tmp_path, monkeypatch):
    from nicegui import ui

    from rbs.ui import app_solve

    store, workspace = _workspace(tmp_path)
    solved = workspace.schedule.revised(
        meta=workspace.schedule.meta.revised(
            status=SolverStatus.FEASIBLE,
            solver_status=SolverStatus.FEASIBLE,
        )
    )

    class Host:
        document_io = None

        def principal(self, _request):
            return None

        async def solve(self, *_args, **_kwargs):
            return solved

    session = WorkspaceSession(store, workspace_host=Host(), workspace_id=workspace.id)
    notices = []
    monkeypatch.setattr(ui, "notify", lambda message, **kwargs: notices.append((message, kwargs)))
    monkeypatch.setattr(
        app_solve, "_open_solver_progress", lambda: SimpleNamespace(close=lambda: None)
    )
    asyncio.run(app_solve._solve(session))
    assert store.get(workspace.id).schedule.attending_work == solved.attending_work
    assert any(
        "review Solve details" in text and options["type"] == "warning" for text, options in notices
    )


def test_failed_solve_explanation_also_exposes_its_notes():
    from nicegui import ui

    from rbs.models.schedule import SolverDiagnostic
    from rbs.ui.app_solve import _open_solver_diagnostics

    before = set(ui.context.client.elements)
    _open_solver_diagnostics(
        [SolverDiagnostic(code="example", message="Clinic coverage could not be completed.")],
        draft_kept=True, notes=["Earlier draft retained after this attempt."],
    )
    elements = _new_elements(before)
    assert any(getattr(element, "_text", None) == "Earlier draft retained after this attempt."
               for element in elements)
    assert any(element.__class__.__name__ == "Expansion" for element in elements)
