from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from rbs.academic_year import rebase_academic_year
from rbs.catalog import sample_instance
from rbs.models.enums import RotationKind, Session, SolverEngineName, SolverStatus, Weekday
from rbs.models.instance import SchedulerInput
from rbs.models.schedule import AssignedClinic, Assignment, Schedule, ScheduleMeta
from rbs.models.special import SpecialRotation, SpecialRotationKind
from rbs.ui.clinic.board import occupancy, render_clinic_html
from rbs.ui.clinic.schedule_csv import clinic_schedule_csv_rows
from rbs.ui.grid import render_grid_html
from rbs.ui.residents.ops import (
    resident_clinic_schedule_report_rows,
    resident_schedule_report_rows,
)
from rbs.ui.rotations.editor import _open_special_rotation_dialog, render_rotations_tab
from rbs.ui.rotations.special_ops import (
    add_special_rotation,
    next_special_rotation_id,
    remove_special_rotation,
    replace_special_rotation,
)


def _special(
    instance: SchedulerInput,
    *,
    special_id: str,
    name: str,
    kind: SpecialRotationKind,
    week: int,
    day_offset: int,
    resident_id: str,
    duration_days: int = 1,
    session: Session | None = None,
) -> SpecialRotation:
    start = instance.calendar.first_week_start + timedelta(
        weeks=week - 1,
        days=day_offset,
    )
    return SpecialRotation(
        id=special_id,
        name=name,
        kind=kind,
        start_date=start,
        end_date=start + timedelta(days=duration_days - 1),
        session=session,
        resident_ids=[resident_id],
    )


def _clinic_schedule(instance: SchedulerInput, resident_id: str, week: int) -> Schedule:
    slots = [
        AssignedClinic(
            weekday=weekday,
            session=session,
            site=instance.clinic_policy.primary_site_id,
            week=week,
        )
        for weekday in (Weekday.MONDAY, Weekday.TUESDAY, Weekday.WEDNESDAY)
        for session in Session
    ]
    return Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=resident_id,
                rotation_id="clinic",
                kind=RotationKind.CLINIC,
                start_week=week,
                end_week=week,
                weeks=[week],
                clinic_slots=slots,
            )
        ],
    )


def test_special_rotation_crud_round_trips_through_the_scheduling_case() -> None:
    instance = sample_instance()
    resident = instance.residents[0]
    conference = _special(
        instance,
        special_id=next_special_rotation_id(instance),
        name="Statewide Conference",
        kind=SpecialRotationKind.CONFERENCE,
        week=8,
        day_offset=1,
        duration_days=3,
        resident_id=resident.id,
    )

    added = add_special_rotation(instance, conference)
    assert added.special_rotations == [conference, *instance.special_rotations]
    assert added.scheduling_case().special_rotations == [conference, *instance.special_rotations]
    assert SchedulerInput.model_validate(
        added.model_dump(mode="json")
    ).special_rotations == [conference, *instance.special_rotations]

    renamed = conference.model_copy(update={"name": "Updated Conference"})
    replaced = replace_special_rotation(added, conference.id, renamed)
    assert replaced.special_rotations[0].name == "Updated Conference"
    assert remove_special_rotation(replaced, conference.id).special_rotations == (
        instance.special_rotations
    )


def test_special_rotations_validate_shape_dates_residents_and_overlaps() -> None:
    instance = sample_instance()
    resident = instance.residents[0]
    event = _special(
        instance,
        special_id="special-001",
        name="Interview Workshop",
        kind=SpecialRotationKind.EVENT,
        week=9,
        day_offset=1,
        resident_id=resident.id,
        session=Session.MORNING,
    )
    morning = add_special_rotation(instance, event)
    afternoon = event.model_copy(
        update={"id": "special-002", "session": Session.AFTERNOON}
    )
    assert len(add_special_rotation(morning, afternoon).special_rotations) == len(
        instance.special_rotations
    ) + 2

    with pytest.raises(ValidationError, match="overlap"):
        add_special_rotation(
            morning,
            event.model_copy(update={"id": "special-003", "name": "Conflict"}),
        )
    with pytest.raises(ValidationError, match="single date"):
        SpecialRotation(
            **event.model_dump(exclude={"end_date"}),
            end_date=event.end_date + timedelta(days=1),
        )
    with pytest.raises(ValidationError, match="unknown resident"):
        add_special_rotation(
            instance,
            event.model_copy(update={"resident_ids": ["missing-resident"]}),
        )


def test_conference_is_vacation_like_for_blocks_but_blocks_only_its_exact_dates() -> None:
    instance = sample_instance()
    resident = instance.residents[0]
    conference = _special(
        instance,
        special_id="special-001",
        name="Board Review Conference",
        kind=SpecialRotationKind.CONFERENCE,
        week=7,
        day_offset=1,
        duration_days=2,
        resident_id=resident.id,
    )
    updated = add_special_rotation(instance, conference)

    assert 7 in updated.resident_scheduling_vacation_weeks(resident.id)
    assert not updated.resident_is_unavailable(
        resident.id,
        7,
        Weekday.MONDAY,
        Session.MORNING,
    )
    assert updated.resident_is_unavailable(
        resident.id,
        7,
        Weekday.TUESDAY,
        Session.AFTERNOON,
    )

    schedule = _clinic_schedule(instance, resident.id, 7)
    board = occupancy(updated, schedule)
    assert board[(7, Weekday.TUESDAY, Session.MORNING)] == []
    assert board[(7, Weekday.WEDNESDAY, Session.AFTERNOON)] == []
    assert [
        person.resident_id
        for person in board[(7, Weekday.MONDAY, Session.MORNING)]
    ] == [resident.id]

    grid = render_grid_html(updated, schedule)
    assert "rbs-special-marker" in grid
    assert "Board Review Conference" in grid
    block_rows = resident_schedule_report_rows(updated, schedule, resident.id)
    assert any(
        row["kind"] == "special" and row["rotation_name"] == conference.name
        for row in block_rows
    )


def test_regular_block_resumes_as_a_continuation_after_a_conference() -> None:
    instance = sample_instance()
    resident = instance.residents[0]
    conference = _special(
        instance,
        special_id="special-001",
        name="Board Review Conference",
        kind=SpecialRotationKind.CONFERENCE,
        week=6,
        day_offset=1,
        duration_days=2,
        resident_id=resident.id,
    )
    updated = add_special_rotation(instance, conference)
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id="elective",
                kind=RotationKind.STANDARD,
                start_week=5,
                end_week=7,
                weeks=[5, 6, 7],
            )
        ],
    )

    rows = resident_schedule_report_rows(updated, schedule, resident.id)

    assert [(row["weeks"], row["kind"]) for row in rows] == [
        ("5–6", "rotation"),
        ("6", "special"),
        ("6–7", "rotation"),
    ]
    assert rows[0]["dates"] == "Jul 27–Aug 3, 2026"
    assert rows[0]["continuation"] == "false"
    assert rows[1]["dates"] == "Aug 4–5, 2026"
    assert rows[2]["dates"] == "Aug 6–16, 2026"
    assert rows[2]["rotation"].endswith("(Cont.)")
    assert rows[2]["continuation"] == "true"


def test_conference_keeps_exact_date_rotation_segments_without_continuing_the_next_block(
) -> None:
    instance = sample_instance()
    resident = instance.residents[2]
    conference = _special(
        instance,
        special_id="special-001",
        name="Test Conference",
        kind=SpecialRotationKind.CONFERENCE,
        week=9,
        day_offset=5,
        duration_days=3,
        resident_id=resident.id,
    )
    updated = add_special_rotation(instance, conference)
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id="elective",
                kind=RotationKind.ELECTIVE,
                elective=True,
                start_week=7,
                end_week=10,
                weeks=[7, 8, 9, 10],
            ),
            Assignment(
                resident_id=resident.id,
                rotation_id="fmed",
                kind=RotationKind.FMED,
                start_week=11,
                end_week=14,
                weeks=[11, 12, 13, 14],
            ),
        ],
    )

    rows = resident_schedule_report_rows(
        updated,
        schedule,
        resident.id,
        show_completed=False,
        today=date(2026, 8, 29),
    )

    assert [(row["weeks"], row["kind"]) for row in rows] == [
        ("9", "rotation"),
        ("9–10", "special"),
        ("10", "rotation"),
        ("11–14", "rotation"),
    ]
    assert rows[0]["dates"] == "Aug 24–28, 2026"
    assert rows[0]["rotation"] == "ELEC · Elective (Elec)"
    assert rows[1]["dates"] == "Aug 29–31, 2026"
    assert rows[2]["dates"] == "Sep 1–6, 2026"
    assert rows[2]["rotation"] == "ELEC · Elective (Elec) (Cont.)"
    assert rows[3]["rotation"] == "FMED · Family Med Education Service"
    assert rows[3]["continuation"] == "false"


def test_event_replaces_only_its_clinic_calendar_period_and_appears_in_exports() -> None:
    instance = sample_instance()
    resident = instance.residents[0]
    event = _special(
        instance,
        special_id="special-001",
        name="Advocacy Workshop",
        kind=SpecialRotationKind.EVENT,
        week=7,
        day_offset=0,
        resident_id=resident.id,
        session=Session.MORNING,
    )
    updated = add_special_rotation(instance, event)
    schedule = _clinic_schedule(instance, resident.id, 7)

    board = occupancy(updated, schedule)
    assert board[(7, Weekday.MONDAY, Session.MORNING)] == []
    assert [
        person.resident_id
        for person in board[(7, Weekday.MONDAY, Session.AFTERNOON)]
    ] == [resident.id]

    markup = render_clinic_html(updated, schedule)
    assert 'class="rbs-clinic-special-event"' in markup
    assert "Advocacy Workshop" in markup
    assert resident.name in markup

    resident_rows = resident_clinic_schedule_report_rows(
        updated,
        schedule,
        resident.id,
    )
    from rbs.ui.schedule_styles import SPECIAL_EVENT_COLOR, SPECIAL_EVENT_TINT

    week = next(row for row in resident_rows if row["week"] == "7")
    assert week["monday_morning"] == "Advocacy Workshop"
    assert week["monday_morning_kind"] == "special-event"
    assert week["monday_morning_color"] == SPECIAL_EVENT_COLOR
    assert week["monday_morning_tint"] == SPECIAL_EVENT_TINT
    assert week["monday_afternoon"] == instance.clinic_policy.site_name(
        instance.clinic_policy.primary_site_id
    )

    csv_week = clinic_schedule_csv_rows(updated, schedule)[6]
    assert "Advocacy Workshop" in csv_week["monday_morning"]
    assert resident.name in csv_week["monday_morning"]


def test_special_tab_presents_both_date_first_assignment_flows() -> None:
    from nicegui import ui

    before = set(ui.context.client.elements)
    render_rotations_tab(
        sample_instance().model_copy(update={"special_rotations": []}),
        selected_rotation_id=None,
        on_select=lambda _rotation_id: None,
        on_save=lambda _instance, _rotation_id: None,
        active_section="special_configuration",
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    labels = {getattr(element, "_text", None) for element in created}
    assert "Special Rotations" not in labels
    assert "Conference/Multi-Day" in labels
    assert "Events (Half/Single Day)" in labels
    assert "Overrides block schedule and suppresses continuity clinic." in labels
    assert (
        "A half-day or full-day shift that replaces clinic/block sections for its "
        "assigned residents."
        in labels
    )
    assert "No special rotations scheduled." in labels
    assert any(
        element.__class__.__name__ == "Button"
        and element._props.get("label") == "Add conference"
        for element in created
    )

    instance = sample_instance()
    resident = instance.residents[0]
    configured = add_special_rotation(
        add_special_rotation(
            instance,
            _special(
                instance,
                special_id="special-001",
                name="Conference",
                kind=SpecialRotationKind.CONFERENCE,
                week=2,
                day_offset=0,
                resident_id=resident.id,
            ),
        ),
        _special(
            instance,
            special_id="special-002",
            name="Event",
            kind=SpecialRotationKind.EVENT,
            week=3,
            day_offset=0,
            resident_id=resident.id,
        ),
    )
    before = set(ui.context.client.elements)
    render_rotations_tab(
        configured,
        selected_rotation_id=None,
        on_select=lambda _rotation_id: None,
        on_save=lambda _instance, _rotation_id: None,
        active_section="special_configuration",
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    badge_labels = {
        element._text
        for element in created
        if element.__class__.__name__ == "Badge"
    }
    assert {"Conference/Multi-Day", "Event"} <= badge_labels
    assert any(
        element.__class__.__name__ == "Button"
        and element._props.get("label") == "Add event"
        for element in created
    )


def test_special_dialog_uses_a_real_resident_multi_select() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)
    _open_special_rotation_dialog(
        instance,
        SpecialRotationKind.CONFERENCE,
        selected_rotation_id=None,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    residents = next(
        element
        for element in created
        if element.__class__.__name__ == "Select"
        and element._props.get("label") == "Residents"
    )
    assert residents.multiple is True
    assert residents._props.get("multiple") is True
    assert residents._props.get("use-input") is True


def test_rebasing_academic_year_moves_special_rotation_dates() -> None:
    instance = sample_instance()
    resident = instance.residents[0]
    event = _special(
        instance,
        special_id="special-001",
        name="Simulation Day",
        kind=SpecialRotationKind.EVENT,
        week=10,
        day_offset=4,
        resident_id=resident.id,
    )
    configured = add_special_rotation(instance, event)

    rebased = rebase_academic_year(configured, "2028-2029")

    assert rebased.special_rotations[0].start_date == event.start_date.replace(
        year=event.start_date.year + 2
    )
    assert rebased.special_rotations[0].end_date == event.end_date.replace(
        year=event.end_date.year + 2
    )
