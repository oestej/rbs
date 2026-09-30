import asyncio
import csv
from datetime import date, timedelta
from io import BytesIO, StringIO
from types import SimpleNamespace

import pytest
from pypdf import PdfReader

from rbs.catalog import sample_instance
from rbs.models.attending import (
    Attending,
    AttendingSchedule,
    AttendingVacation,
    AttendingWeeklyWorkSchedule,
    AttendingWorkHalfDay,
    AttendingWorkType,
)
from rbs.models.clinic import ClinicPolicy
from rbs.models.enums import Session, Weekday
from rbs.models.instance import SchedulerInput
from rbs.ui.clinic.board import render_clinic_html
from rbs.ui.clinic.projection import attending_occupancy
from rbs.ui.clinic.schedule_csv import build_clinic_schedule_csv, clinic_schedule_csv_filename
from rbs.ui.clinic.schedule_pdf import build_clinic_schedule_pdf, clinic_schedule_pdf_filename


@pytest.fixture
def attending_calendar() -> SchedulerInput:
    instance = sample_instance()
    monday = instance.calendar.first_week_start + timedelta(weeks=50)
    return instance.revised(
        attendings=[
            Attending(
                id="ada", name="Ada Lovelace", schedule_start_date=monday,
                schedule_end_date=monday + timedelta(days=7),
                vacation_ranges=[AttendingVacation(
                    start_date=monday + timedelta(days=1),
                    end_date=monday + timedelta(days=1),
                )],
            ),
        ],
        attending_schedules=[AttendingSchedule(attending_id="ada", weeks=[
            AttendingWeeklyWorkSchedule(week=51, half_days=[
                AttendingWorkHalfDay(
                    weekday=Weekday.MONDAY, session=Session.MORNING,
                    work_type=AttendingWorkType.PRECEPTING_CLINIC, clinic_id="maple",
                ),
                AttendingWorkHalfDay(
                    weekday=Weekday.TUESDAY, session=Session.MORNING,
                    work_type=AttendingWorkType.ATTENDING_CLINIC,
                ),
                AttendingWorkHalfDay(
                    weekday=Weekday.THURSDAY, session=Session.AFTERNOON,
                    work_type=AttendingWorkType.SPECIAL_OTHER, description="Teaching <&> rounds",
                ),
                AttendingWorkHalfDay(
                    weekday=Weekday.FRIDAY, session=Session.MORNING,
                    work_type=AttendingWorkType.INPATIENT_SERVICE,
                ),
                AttendingWorkHalfDay(
                    weekday=Weekday.SATURDAY, session=Session.MORNING,
                    work_type=AttendingWorkType.PRECEPTING_CLINIC, clinic_id="cedar",
                ),
            ]),
            AttendingWeeklyWorkSchedule(week=52, half_days=[
                AttendingWorkHalfDay(
                    weekday=Weekday.MONDAY, session=Session.MORNING,
                    work_type=AttendingWorkType.ATTENDING_CLINIC,
                ),
            ]),
        ])],
    )


def test_attending_projection_uses_effective_weeks_and_site_filter(attending_calendar) -> None:
    board = attending_occupancy(attending_calendar)
    assert not board[(50, Weekday.MONDAY, Session.MORNING)]
    assert board[(51, Weekday.MONDAY, Session.MORNING)][0].site == "maple"
    assert not board[(51, Weekday.TUESDAY, Session.MORNING)]  # vacation
    assert board[(51, Weekday.WEDNESDAY, Session.AFTERNOON)][0].work_type is (
        AttendingWorkType.ADMIN_TIME
    )
    assert board[(52, Weekday.MONDAY, Session.MORNING)][0].work_type is (
        AttendingWorkType.ATTENDING_CLINIC
    )
    assert not board[(52, Weekday.WEDNESDAY, Session.AFTERNOON)]  # schedule ended

    maple = attending_occupancy(attending_calendar, site="maple")
    assert sum(len(people) for people in maple.values()) == 1
    assert maple[(51, Weekday.MONDAY, Session.MORNING)][0].name == "Ada Lovelace"


def test_attending_calendar_displays_work_without_a_resident_solve(attending_calendar) -> None:
    monday = attending_calendar.calendar.first_week_start + timedelta(weeks=50)
    markup = render_clinic_html(
        attending_calendar, None, view="attendings", show_legend=False,
        show_past_weeks=False, today=monday,
    )
    assert 'datetime="2027-06-19"' in markup  # Saturday attending work
    assert "Ada <strong" in markup
    assert "Precepting Clinic · Maple" in markup
    assert "Attending Clinic" in markup
    assert "Admin Time" in markup
    assert "Inpatient Service" in markup
    assert "Teaching &lt;&amp;&gt; rounds" in markup
    assert "Teaching <&> rounds" not in markup
    assert "data-resident-id" not in markup
    assert attending_calendar.residents[0].name not in markup
    assert 'datetime="2027-06-07"' not in markup


def test_attending_calendar_uses_week_specific_academic_admin_policy(attending_calendar) -> None:
    raw = attending_calendar.model_dump(mode="json")
    raw["academic_half_day_overrides"] = [
        {"week": 51, "weekday": "sunday", "session": "morning"},
    ]
    instance = SchedulerInput.model_validate(raw)
    board = attending_occupancy(instance)
    assert not board[(51, Weekday.WEDNESDAY, Session.AFTERNOON)]
    assert board[(51, Weekday.SUNDAY, Session.MORNING)][0].work_type is (
        AttendingWorkType.ADMIN_TIME
    )
    assert 'datetime="2027-06-20"' in render_clinic_html(instance, None, view="attendings")

    raw["clinic_policy"]["academic_half_day_is_attending_admin_time"] = False
    instance = SchedulerInput.model_validate(raw)
    assert not attending_occupancy(instance)[(51, Weekday.SUNDAY, Session.MORNING)]
    assert 'datetime="2027-06-20"' not in render_clinic_html(instance, None, view="attendings")


def test_attending_calendar_closures_only_remove_work_at_the_closed_clinic(
    attending_calendar,
) -> None:
    monday = attending_calendar.calendar.first_week_start + timedelta(weeks=50)
    policy = attending_calendar.clinic_policy.model_dump(mode="json")
    policy["closure_days"] = [
        {"date": monday.isoformat(), "sites": ["maple"], "name": "Maple closed"},
        {"date": (monday + timedelta(days=4)).isoformat(),
         "sites": ["maple", "cedar"], "name": "Clinics closed"},
    ]
    instance = attending_calendar.revised(clinic_policy=ClinicPolicy.model_validate(policy))
    board = attending_occupancy(instance)
    assert not board[(51, Weekday.MONDAY, Session.MORNING)]
    assert board[(51, Weekday.FRIDAY, Session.MORNING)][0].work_type is (
        AttendingWorkType.INPATIENT_SERVICE
    )
    markup = render_clinic_html(instance, None, view="attendings", show_legend=False)
    assert "Precepting Clinic · Maple" not in markup
    assert "Inpatient Service" in markup
    assert "Maple closed" in markup


def test_attending_exports_follow_the_calendar_view_and_filters(attending_calendar) -> None:
    monday = attending_calendar.calendar.first_week_start + timedelta(weeks=50)
    options = dict(view="attendings", show_past_weeks=False, today=monday)
    rows = list(csv.DictReader(StringIO(build_clinic_schedule_csv(
        attending_calendar, None, **options,
    ))))
    assert [row["Academic Week"] for row in rows] == ["51", "52"]
    assert rows[0]["Mon AM"] == "Ada Lovelace · Precepting Clinic · Maple"
    assert rows[0]["Tue AM"] == ""
    assert rows[0]["Wed PM"] == "Ada Lovelace · Admin Time"
    assert rows[0]["Sat AM"] == "Ada Lovelace · Precepting Clinic · Cedar"
    assert rows[1]["Mon AM"] == "Ada Lovelace · Attending Clinic"

    pdf = build_clinic_schedule_pdf(attending_calendar, None, site="maple", **options)
    reader = PdfReader(BytesIO(pdf))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert reader.metadata.title == "Attending Schedule - Maple"
    assert "Ada Lovelace" in text and "Precepting Clinic" in text
    assert "Admin Time" not in text and "Inpatient Service" not in text
    assert attending_calendar.residents[0].name not in text


@pytest.mark.parametrize("filename", [clinic_schedule_csv_filename, clinic_schedule_pdf_filename])
def test_attending_export_filenames_identify_the_view(filename) -> None:
    name = filename(
        "2026-2027", site="maple", view="attendings", exported_on=date(2026, 9, 29),
    )
    assert name.startswith("attending-schedule-2026-2027-maple-exported-2026-09-29.")


def test_empty_attending_calendar_explains_where_to_start() -> None:
    instance = sample_instance().revised(attendings=[], attending_schedules=[])
    markup = render_clinic_html(instance, None, view="attendings")
    assert "No attendings yet" in markup
    assert "week-by-week schedules in Attendings" in markup


def test_switching_reuses_preloaded_calendars_and_filters_rebuild_them(
    attending_calendar, tmp_path, monkeypatch,
) -> None:
    from nicegui import ui

    from rbs.store import Store
    from rbs.ui import app_shell
    from rbs.ui.deferred_sections import DeferredSections
    from rbs.ui.session import WorkspaceSession

    store = Store(tmp_path / "cached-calendar.sqlite")
    store.init()
    workspace = store.create("Calendar", attending_calendar)
    session = WorkspaceSession(
        store=store, workspace_id=workspace.id, active_tab="clinic_schedule",
    )
    rendered = []
    original = app_shell.render_clinic_html

    def render(*args, **kwargs):
        rendered.append((kwargs["view"], kwargs["site"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(app_shell, "render_clinic_html", render)
    root = ui.column()
    try:
        before = set(root.client.elements)
        with root:
            app_shell._render_clinic_schedule(session, workspace)
        elements = [e for key, e in root.client.elements.items() if key not in before]
        toggle = next(e for e in elements if e.__class__.__name__ == "ClinicViewToggle")
        loader = next(e for e in elements if isinstance(e, DeferredSections))
        assert rendered == [("residents", None)]
        asyncio.run(loader._preload_next(SimpleNamespace(args=loader._props["generation"])))
        assert rendered == [("residents", None), ("attendings", None)]
        assert toggle.value == "residents"
        legend = next(e for e in elements if "rbs-clinic-toolbar-key" in e._classes)
        browser_handler = next(
            listener.handler for listener in toggle._event_listeners.values()
            if listener.type == "update:modelValue"
        )
        sent_updates = []
        with monkeypatch.context() as patches:
            for element in [toggle, legend, *loader._panels.values()]:
                patches.setattr(element, "update", lambda: sent_updates.append(True))
            browser_handler(SimpleNamespace(args="attendings"))
            assert session.clinic_schedule_view == "attendings"
            browser_handler(SimpleNamespace(args="residents"))
            assert session.clinic_schedule_view == "residents"
        assert sent_updates == []  # queued responses cannot undo a newer local click
        calendars = [e for e in root.client.elements.values() if (
            e.id not in before and e.__class__.__name__ == "Html"
            and "rbs-clinic-wrap" in e.content
        )]
        for _ in range(3):
            toggle.set_value("attendings")
            assert "is-inactive" in loader._panels["residents"]._classes
            assert loader._panels["residents"]._props["inert"]
            assert "inert" not in loader._panels["attendings"]._props
            toggle.set_value("residents")
        assert len(rendered) == 2
        assert calendars and all(not e.is_deleted for e in calendars)

        toggle.set_value("attendings")
        site = next(e for e in elements if e.__class__.__name__ == "Select")
        site.set_value("maple")
        assert rendered[-1] == ("attendings", "maple")
        assert loader.is_deleted
        assert all(e.is_deleted for e in calendars)
        asyncio.run(loader._preload_next(SimpleNamespace(args=loader._props["generation"])))
        assert len(rendered) == 3  # late request cannot recreate the old filter
        toggle.set_value("residents")
        toggle.set_value("attendings")
        assert rendered == [
            ("residents", None), ("attendings", None),
            ("attendings", "maple"), ("residents", "maple"),
        ]
        past = next(e for e in elements if e.__class__.__name__ == "Checkbox")
        past.set_value(True)
        assert rendered[-1] == ("attendings", "maple")
        assert len(rendered) == 5
        toggle.set_value("residents")
        toggle.set_value("attendings")
        assert len(rendered) == 6
        assert store.get(workspace.id) == workspace
    finally:
        root.delete()


def test_toggle_preserves_filters_and_workspace_and_exports_selected_view(
    attending_calendar, tmp_path, monkeypatch,
) -> None:
    from nicegui import ui

    from rbs.store import Store
    from rbs.ui import app_shell
    from rbs.ui.session import WorkspaceSession

    store = Store(tmp_path / "clinic.sqlite")
    store.init()
    workspace = store.create("Attending calendar", attending_calendar)
    session = WorkspaceSession(
        store=store, workspace_id=workspace.id, clinic_site="maple", show_past_clinic_weeks=True,
    )
    before = set(ui.context.client.elements)
    app_shell._render_clinic_schedule(session, workspace)
    elements = [
        element for key, element in ui.context.client.elements.items() if key not in before
    ]
    toggle = next(
        element for element in elements if element.__class__.__name__ == "ClinicViewToggle"
    )
    heading = next(element for element in elements if getattr(element, "_text", "") == (
        "Clinic schedule"
    ))
    assert toggle.value == "residents"
    toggle.set_value("attendings")
    assert session.clinic_schedule_view == "attendings"
    assert session.clinic_site == "maple" and session.show_past_clinic_weeks
    assert not heading._deleted
    assert store.get(workspace.id) == workspace
    html = "".join(
        element.content for element in ui.context.client.elements.values()
        if element.__class__.__name__ == "Html" and not element._deleted
        and element.id not in before
    )
    assert "Precepting Clinic · Maple" in html
    assert "data-resident-id" not in html

    csv_exports = []
    pdf_exports = []

    async def present_csv(_session, content, filename):
        csv_exports.append((content, filename))
        return True

    monkeypatch.setattr(app_shell, "_present_csv_export", present_csv)
    monkeypatch.setattr("nicegui.elements.button.handle_event", lambda callback, _event: callback())
    monkeypatch.setattr(app_shell, "_open_exported_pdf", lambda _session, content, filename: (
        pdf_exports.append((content, filename))
    ))
    for button in elements:
        if button.__class__.__name__ != "Button":
            continue
        handler = next(
            listener.handler for listener in button._event_listeners.values()
            if listener.type == "click"
        )
        if button._props.get("label") == "Export CSV":
            asyncio.run(handler(None))
        elif button._props.get("label") == "Export PDF":
            handler(None)
    assert "Ada Lovelace · Precepting Clinic · Maple" in csv_exports[0][0]
    assert csv_exports[0][1].startswith("attending-schedule-")
    assert pdf_exports[0][1].startswith("attending-schedule-")
    assert PdfReader(BytesIO(pdf_exports[0][0])).metadata.title == "Attending Schedule - Maple"

    toggle.set_value("residents")
    assert session.clinic_schedule_view == "residents"
    toggle.set_value("attendings")
    session.reset_navigation(None)
    assert session.clinic_schedule_view == "residents"
