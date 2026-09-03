from datetime import date, timedelta

import pytest

from rbs.catalog import sample_instance
from rbs.models.resident import ElectivePreferenceRequest, Resident
from rbs.ui.residents.electives import (
    elective_preference_options,
    replace_elective_preferences,
)
from rbs.ui.residents.ops import (
    add_resident,
    add_resident_clinic_slot,
    change_resident_clinic_slot_site,
    day_off_date,
    day_off_is_selectable,
    move_resident_clinic_slot,
    next_resident_id,
    remove_resident_clinic_slot,
    replace_resident,
    resident_clinic_available_site_ids,
    resident_clinic_schedule_report_rows,
    resident_clinic_slot,
    resident_clinic_slot_locked,
    resident_clinic_target_conflicts,
    resident_clinic_week_override_delta,
    resident_schedule_report_rows,
    set_resident_clinic_slot_locked,
    vacation_monday,
    vacation_monday_is_selectable,
    vacation_month_dates,
    vacation_range_for_monday,
    vacation_week_for_monday,
)
from rbs.ui.residents.schedule import _resident_schedule_workspace
from rbs.ui.residents.tab import _resident_view, render_residents_tab


def test_elective_preferences_preserve_order_duplicates_and_prune_incompatible() -> None:
    instance = sample_instance()
    resident = instance.residents_by_id["resident-009"]
    preferences = [
        ElectivePreferenceRequest(rotation_id="fmed", duration_weeks=2),
        ElectivePreferenceRequest(rotation_id="night_float", duration_weeks=2),
        ElectivePreferenceRequest(rotation_id="fmed", duration_weeks=2),
    ]

    updated = replace_elective_preferences(instance, resident.id, preferences)

    assert updated.residents_by_id[resident.id].elective_preferences == preferences
    raw = updated.model_dump(mode="json")
    saved = next(item for item in raw["residents"] if item["id"] == resident.id)
    saved["elective_preferences"].extend(
        [
            {"rotation_id": "missing", "duration_weeks": 2},
            {"rotation_id": "night_float", "duration_weeks": 4},
        ]
    )
    normalized = type(instance).model_validate(raw)
    assert normalized.residents_by_id[resident.id].elective_preferences == preferences


def test_nonrepeatable_elective_prunes_duplicate_resident_requests() -> None:
    from rbs.ui.rotations.ops import set_elective_eligibility

    instance = set_elective_eligibility(
        sample_instance(),
        "fmed",
        eligible=True,
        eligible_pgys=[2],
        repeatable=False,
    )
    resident = instance.residents_by_id["resident-009"]
    request = ElectivePreferenceRequest(rotation_id="fmed", duration_weeks=2)

    updated = replace_elective_preferences(
        instance,
        resident.id,
        [request, request],
    )

    assert updated.residents_by_id[resident.id].elective_preferences == [request]


def test_elective_preference_options_pair_services_with_direct_inventory() -> None:
    instance = sample_instance()

    pgy1 = elective_preference_options(instance, instance.residents_by_id["resident-001"])
    pgy2 = elective_preference_options(instance, instance.residents_by_id["resident-009"])

    assert pgy1 == {
        "night_float|2": "NF · Night Float · 2 weeks",
        "geriatrics|2": "GERI · Geriatrics · 2 weeks",
        "palliative_care|2": "PALL · Palliative Care · 2 weeks",
    }
    assert set(pgy2) == {"night_float|2", "fmed|2", "geriatrics|2", "palliative_care|2"}


def test_elective_preference_tab_renders_empty_fallback_state_and_stays_active() -> None:
    from nicegui import ui

    instance = sample_instance()
    resident = instance.residents[0].model_copy(update={"elective_preferences": []})
    before = set(ui.context.client.elements)

    _resident_schedule_workspace(
        instance,
        None,
        resident,
        on_schedule_save=lambda _instance, _resident_id, _preserve: None,
        active_section="resident_elective_preference",
    )

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    labels = {getattr(element, "_text", None) for element in created}
    panels = next(element for element in created if element.__class__.__name__ == "TabPanels")
    buttons = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Button"
    }

    assert panels._props["model-value"] == "resident_elective_preference"
    assert "Elective preferences" in labels
    assert "No services ranked" in labels
    assert (
        "All direct Elective blocks will use Clinic (Elective fallback) on the next solve."
        in labels
    )
    assert {"Add request", "Save preferences"} <= buttons


def test_elective_preference_tab_reports_solved_fallback_blocks() -> None:
    from nicegui import ui

    from rbs.models.enums import RotationKind, SolverEngineName, SolverStatus
    from rbs.models.schedule import Assignment, Schedule, ScheduleMeta

    instance = sample_instance()
    resident = instance.residents[0]
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id="clinic",
                kind=RotationKind.CLINIC,
                elective=True,
                elective_fallback=True,
                start_week=9,
                end_week=10,
                weeks=[9, 10],
                block_start_week=9,
                block_duration_weeks=2,
            )
        ],
    )
    before = set(ui.context.client.elements)

    _resident_schedule_workspace(instance, schedule, resident)

    labels = {
        getattr(element, "_text", None)
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    }
    assert "1 Clinic (Elective fallback) block" in labels
    assert "Current schedule · weeks 9–10" in labels


def test_resident_directory_owns_the_new_resident_action() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)

    render_residents_tab(
        instance,
        selected_resident_id=None,
        on_select=lambda _resident_id: None,
        on_save=lambda _instance, _resident_id: None,
    )

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    labels = {getattr(element, "_text", None) for element in created}
    directory_label = next(
        element for element in created if getattr(element, "_text", None) == "Resident directory"
    )
    new_resident = next(
        element
        for element in created
        if element.__class__.__name__ == "Button" and element._props.get("label") == "New resident"
    )
    master_split = next(
        element for element in created if "rbs-master-split" in getattr(element, "_classes", [])
    )

    assert "Residents" in labels
    assert any("rbs-master-page" in getattr(element, "_classes", []) for element in created)
    assert "16 total" not in labels
    assert "Review individual schedules and manage resident information and time off." not in labels
    assert new_resident.parent_slot.parent is directory_label.parent_slot.parent
    assert "items-stretch" in master_split._classes
    assert "items-start" not in master_split._classes
    assert "rbs-master-no-selection" in master_split._classes


def test_selected_resident_uses_the_compact_detail_layout() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)

    render_residents_tab(
        instance,
        selected_resident_id=instance.residents[0].id,
        on_select=lambda _resident_id: None,
        on_save=lambda _instance, _resident_id: None,
    )

    master_split = next(
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before and "rbs-master-split" in getattr(element, "_classes", [])
    )

    assert "rbs-master-has-selection" in master_split._classes


def test_resident_view_prioritizes_schedule_and_keeps_edit_secondary() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)

    _resident_view(
        instance,
        None,
        instance.residents[0],
        on_edit=lambda: None,
        on_select=lambda _resident_id: None,
    )

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    buttons = {
        element._props.get("label"): element._props
        for element in created
        if element.__class__.__name__ == "Button"
    }
    labels = {getattr(element, "_text", None) for element in created}
    tab_labels = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Tab"
    }
    large_avatars = [
        element
        for element in created
        if element.__class__.__name__ == "Avatar"
        and "rbs-resident-avatar-large" in element._classes
    ]
    schedule_tabs = next(element for element in created if element.__class__.__name__ == "Tabs")
    export_button = next(
        element for element in created if element._props.get("label") == "Export to PDF"
    )
    back_button = next(
        element
        for element in created
        if element.__class__.__name__ == "Button" and element._props.get("icon") == "arrow_back"
    )
    assert "Schedule" not in buttons
    assert buttons["Edit info/time off"]["outline"]
    assert back_button._props["aria-label"] == "Back to resident directory"
    assert buttons["Export to PDF"]["outline"]
    assert "Avery Chen's schedule" not in labels
    assert "PGY 1 · 4 vacation weeks · No individual days off" in labels
    assert "Resident details" not in labels
    assert "Vacation and Other Days Off (single days)" not in labels
    assert "No block schedule available" in labels
    assert "No clinic schedule available" in labels
    assert tab_labels == {
        "Block Schedule",
        "Clinic Schedule",
        "Elective preferences",
    }
    assert not large_avatars
    assert "inline-label" in schedule_tabs._props
    assert "rbs-resident-schedule-header" in schedule_tabs.parent_slot.parent._classes
    schedule_actions = export_button.parent_slot.parent
    assert "rbs-resident-schedule-actions" in schedule_actions._classes
    assert schedule_actions.parent_slot.parent is schedule_tabs.parent_slot.parent
    assert not any(element.__class__.__name__ == "Dialog" for element in created)
    assert "Close" not in buttons
    show_completed = [
        element
        for element in created
        if element.__class__.__name__ == "Checkbox"
        and getattr(element, "_text", None) == "Show completed"
    ]
    assert len(show_completed) == 2
    assert all(checkbox._props["model-value"] is False for checkbox in show_completed)


def test_block_schedule_edit_mode_contains_add_and_lock_tools() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)
    _resident_view(
        instance,
        None,
        instance.residents[0],
        on_edit=lambda: None,
        on_select=lambda _resident_id: None,
        on_schedule_save=lambda _instance, _resident_id, _preserve: None,
        block_schedule_editing=True,
        on_block_schedule_editing_change=lambda _editing: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    button_labels = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Button"
    }
    tab_labels = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Tab"
    }
    labels = {getattr(element, "_text", None) for element in created}
    schedule_panels = next(
        element for element in created if element.__class__.__name__ == "TabPanels"
    )

    assert tab_labels == {
        "Block Schedule",
        "Clinic Schedule",
        "Elective preferences",
    }
    assert "Manual Override Blocks" not in tab_labels
    assert schedule_panels._props["model-value"] == "resident_block_schedule"
    assert "Schedule tools" not in labels
    assert "Hardcode, edit, lock, or unlock this resident's blocks" not in labels
    assert not any(
        isinstance(label, str) and label.startswith("Hardcoded blocks are exact")
        for label in labels
    )
    assert {
        "Add block",
        "Done editing",
        "Lock current schedule",
        "Unlock all manual",
        "Unlock",
    } <= button_labels
    assert "Hardcode block" not in button_labels
    assert "Delete" not in button_labels


def test_block_schedule_edit_mode_toggles_tools_inline() -> None:
    from nicegui import ui

    instance = sample_instance()
    editing_changes: list[bool] = []
    before = set(ui.context.client.elements)
    _resident_schedule_workspace(
        instance,
        None,
        instance.residents[0],
        on_schedule_save=lambda _instance, _resident_id, _preserve: None,
        on_block_schedule_editing_change=editing_changes.append,
    )

    def current_buttons():
        return [
            element
            for element_id, element in ui.context.client.elements.items()
            if element_id not in before
            and element.__class__.__name__ == "Button"
            and not element._deleted
        ]

    edit = next(
        element for element in current_buttons() if element._props.get("label") == "Edit schedule"
    )
    edit_actions = edit.parent_slot.parent
    show_completed = next(
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
        and element.__class__.__name__ == "Checkbox"
        and getattr(element, "_text", None) == "Show completed"
        and element.parent_slot.parent is edit_actions
    )
    assert show_completed.id < edit.id

    next(iter(edit._event_listeners.values())).handler(None)

    assert editing_changes == [True]
    assert {"Done editing", "Add block"} <= {
        element._props.get("label") for element in current_buttons()
    }

    done = next(
        element for element in current_buttons() if element._props.get("label") == "Done editing"
    )
    next(iter(done._event_listeners.values())).handler(None)

    assert editing_changes == [True, False]
    assert "Add block" not in {element._props.get("label") for element in current_buttons()}


def test_locked_hardcoded_block_must_be_unlocked_before_deletion() -> None:
    from nicegui import ui

    from rbs.models.enums import RotationKind, SolverEngineName, SolverStatus
    from rbs.models.schedule import Assignment, Schedule, ScheduleMeta
    from rbs.ui.locks import replace_manual_block

    instance = sample_instance()
    resident = instance.residents[8]
    instance = replace_manual_block(
        instance,
        resident_id=resident.id,
        rotation_id="fmed",
        start_week=1,
        duration_weeks=4,
    )
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id="fmed",
                kind=RotationKind.FMED,
                start_week=1,
                end_week=4,
                weeks=[1, 2, 3, 4],
                block_start_week=1,
                block_duration_weeks=4,
            )
        ],
    )
    before = set(ui.context.client.elements)
    _resident_view(
        instance,
        schedule,
        resident,
        on_edit=lambda: None,
        on_select=lambda _resident_id: None,
        on_schedule_save=lambda _instance, _resident_id, _preserve: None,
        block_schedule_editing=True,
        on_block_schedule_editing_change=lambda _editing: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    button_labels = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Button"
    }

    assert "Unlock" in button_labels
    assert "Delete" not in button_labels


def test_unlocked_populated_block_can_be_deleted_from_outdated_schedule() -> None:
    from nicegui import ui

    from rbs.models.enums import RotationKind, SolverEngineName, SolverStatus
    from rbs.models.schedule import Assignment, Schedule, ScheduleMeta

    instance = sample_instance()
    resident = instance.residents[2]
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id="fmed",
                kind=RotationKind.FMED,
                start_week=1,
                end_week=4,
                weeks=[1, 2, 3, 4],
                block_start_week=1,
                block_duration_weeks=4,
            )
        ],
    )
    before = set(ui.context.client.elements)
    _resident_view(
        instance,
        schedule,
        resident,
        on_edit=lambda: None,
        on_select=lambda _resident_id: None,
        on_schedule_save=lambda _instance, _resident_id, _preserve: None,
        on_schedule_change=lambda _schedule, _resident_id, _refresh: None,
        schedule_is_current=False,
        block_schedule_editing=True,
        on_block_schedule_editing_change=lambda _editing: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    buttons = {
        element._props.get("label"): element
        for element in created
        if element.__class__.__name__ == "Button"
    }

    assert {"Lock", "Delete", "Edit", "Add block", "Done editing"} <= buttons.keys()
    assert not buttons["Delete"]._props.get("disable")
    assert "Unlock" not in buttons


def test_inline_resident_schedule_is_a_chronological_report_for_one_resident() -> None:
    from nicegui import ui

    from rbs.models.enums import (
        RotationKind,
        Session,
        SolverEngineName,
        SolverStatus,
        Weekday,
    )
    from rbs.models.schedule import AssignedClinic, Assignment, Schedule, ScheduleMeta

    instance = sample_instance()
    avery, jordan = instance.residents[:2]
    tuesday_off = instance.calendar.first_week_start + timedelta(weeks=10, days=1)
    avery = avery.model_copy(update={"days_off": [tuesday_off]})
    residents = [avery if resident.id == avery.id else resident for resident in instance.residents]
    instance = instance.model_copy(update={"residents": residents})
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=avery.id,
                rotation_id="clinic",
                start_week=11,
                end_week=14,
                weeks=[11, 12, 13, 14],
                clinic_slots=[
                    AssignedClinic(
                        weekday=Weekday.WEDNESDAY,
                        session=Session.MORNING,
                        site=instance.clinic_policy.site_ids[0],
                        week=11,
                    )
                ],
            ),
            Assignment(
                resident_id=avery.id,
                rotation_id="fmed",
                kind=RotationKind.FMED,
                start_week=1,
                end_week=1,
                weeks=[1],
                clinic_slots=[
                    AssignedClinic(
                        weekday=Weekday.MONDAY,
                        session=Session.MORNING,
                        site=instance.clinic_policy.site_ids[0],
                        week=1,
                    ),
                    AssignedClinic(
                        weekday=Weekday.TUESDAY,
                        session=Session.AFTERNOON,
                        admin=True,
                        week=1,
                    ),
                ],
            ),
            Assignment(
                resident_id=jordan.id,
                rotation_id="clinic",
                start_week=1,
                end_week=1,
                weeks=[1],
                clinic_slots=[
                    AssignedClinic(
                        weekday=Weekday.FRIDAY,
                        session=Session.MORNING,
                        site=instance.clinic_policy.primary_site_id,
                        week=1,
                    )
                ],
            ),
        ],
    )
    rows = resident_schedule_report_rows(instance, schedule, avery.id)

    assert [row["weeks"] for row in rows] == ["1", "11", "12–13", "14"]
    assert rows[1]["period"] == "Week 11 (Sep 7–13, 2026)"
    assert rows[1]["rotation"].startswith("CLINIC · Clinic")
    assert "Day off: Sep 8, 2026" in rows[1]["rotation"]
    assert rows[2]["kind"] == "vacation"
    assert rows[2]["rotation"] == "Vacation"
    assert rows[2]["period"] == "Weeks 12–13 (Sep 14–27, 2026)"
    assert rows[3]["rotation"].startswith("CLINIC · Clinic (Cont.)")
    assert all("locked" not in row for row in rows)
    assert all(jordan.name not in str(row) for row in rows)
    visible_rows = resident_schedule_report_rows(
        instance,
        schedule,
        avery.id,
        show_completed=False,
        today=date(2026, 8, 23),
    )
    assert [row["weeks"] for row in visible_rows] == ["11", "12–13", "14"]
    clinic_rows = resident_clinic_schedule_report_rows(instance, schedule, avery.id)
    assert [row["week"] for row in clinic_rows] == [str(week) for week in range(1, 53)]
    clinic_by_week = {row["week"]: row for row in clinic_rows}
    assert clinic_by_week["1"]["dates"] == "Jun 29–Jul 5, 2026"
    assert clinic_by_week["1"]["rotation"] == "FMED · Family Med Education Service"
    assert clinic_by_week["1"]["monday_date"] == "Jun 29"
    assert clinic_by_week["1"]["monday_morning"] == "Maple"
    assert clinic_by_week["1"]["monday_morning_kind"] == "site"
    assert clinic_by_week["1"]["tuesday_afternoon"] == "Admin"
    assert clinic_by_week["1"]["tuesday_afternoon_kind"] == "admin"
    assert clinic_by_week["1"]["sessions"] == "Mon AM · Maple\nTue PM · Admin"
    assert clinic_by_week["2"]["sessions"] == ""
    assert clinic_by_week["2"]["rotation"] == "—"
    assert clinic_by_week["11"]["wednesday_morning"] == "Maple"
    visible_clinic_rows = resident_clinic_schedule_report_rows(
        instance,
        schedule,
        avery.id,
        show_completed=False,
        today=date(2026, 8, 23),
    )
    assert [row["week"] for row in visible_clinic_rows] == [str(week) for week in range(8, 53)]

    before = set(ui.context.client.elements)

    _resident_schedule_workspace(
        instance,
        schedule,
        avery,
        today=date(2026, 8, 23),
    )

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    labels = {getattr(element, "_text", None) for element in created}
    tab_labels = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Tab"
    }
    block_timelines = [
        element
        for element in created
        if "rbs-resident-block-timeline" in getattr(element, "_classes", [])
    ]
    block_lanes = [
        element
        for element in created
        if "rbs-resident-block-lane" in getattr(element, "_classes", [])
    ]
    block_bands = [
        element
        for element in created
        if "rbs-resident-block-band" in getattr(element, "_classes", [])
    ]
    assert len(block_timelines) == 1
    assert block_timelines[0]._props["role"] == "list"
    assert len(block_lanes) == len(visible_rows)
    assert len(block_bands) == len(visible_rows)
    assert sum("is-vacation" in band._classes for band in block_bands) == 1
    assert "Week 11" in labels
    assert "Sep 7–13, 2026" in labels
    assert "Weeks 12–13" in labels
    assert "Sep 14–27, 2026" in labels
    assert "Vacation" in labels
    assert "Clinic (Cont.)" in labels
    assert not any(
        "rbs-resident-rotation-code" in getattr(element, "_classes", []) for element in created
    )
    clinic_weeks = [
        element
        for element in created
        if "rbs-resident-clinic-week" in getattr(element, "_classes", [])
    ]
    clinic_grids = [
        element
        for element in created
        if "rbs-resident-clinic-week-grid" in getattr(element, "_classes", [])
    ]
    assert len(clinic_weeks) == 45
    assert len(clinic_grids) == 45
    assert not any(
        element.__class__.__name__ == "Table"
        and "rbs-resident-block-report" in getattr(element, "_classes", [])
        for element in created
    )
    assert "Chronological block report" not in labels
    assert not any(
        isinstance(label, str) and label.endswith("schedule entries") for label in labels
    )
    assert not any(
        isinstance(label, str) and "alongside each assignment" in label for label in labels
    )
    assert "Weekly clinic report" not in labels
    assert not any(isinstance(label, str) and label.endswith("clinic weeks") for label in labels)
    assert "Week 8" in labels
    assert "Week 11" in labels
    assert "Maple" in labels
    assert (
        "Every week in the selected range is listed. Weeks without assigned clinic "
        "dates remain blank; vacation weeks and individual days off are also blank, "
        "and Special events replace clinic in their scheduled half-days." in labels
    )
    assert tab_labels == {
        "Block Schedule",
        "Clinic Schedule",
        "Elective preferences",
    }
    assert not any(element.__class__.__name__ == "Dialog" for element in created)
    buttons = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Button"
    }
    assert "Close" not in buttons
    show_completed = [
        element
        for element in created
        if element.__class__.__name__ == "Checkbox"
        and getattr(element, "_text", None) == "Show completed"
    ]
    assert len(show_completed) == 2
    assert all(checkbox._props["model-value"] is False for checkbox in show_completed)


def test_resident_report_combines_adjacent_vacation_across_rotation_boundaries() -> None:
    from rbs.models.enums import RotationKind, SolverEngineName, SolverStatus
    from rbs.models.schedule import Assignment, Schedule, ScheduleMeta

    instance = sample_instance()
    resident = instance.residents[0].model_copy(update={"vacation_weeks": [2, 3]})
    instance = instance.model_copy(
        update={
            "residents": [
                resident if item.id == resident.id else item for item in instance.residents
            ]
        }
    )
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id="fmed",
                kind=RotationKind.FMED,
                start_week=1,
                end_week=2,
                weeks=[1, 2],
            ),
            Assignment(
                resident_id=resident.id,
                rotation_id="clinic",
                start_week=3,
                end_week=4,
                weeks=[3, 4],
            ),
        ],
    )

    rows = resident_schedule_report_rows(instance, schedule, resident.id)

    assert [(row["weeks"], row["kind"]) for row in rows] == [
        ("1", "rotation"),
        ("2–3", "vacation"),
        ("4", "rotation"),
    ]
    assert rows[1]["period"] == "Weeks 2–3 (Jul 6–19, 2026)"
    assert rows[2]["rotation"] == "CLINIC · Clinic"
    assert rows[2]["continuation"] == "false"


def test_resident_clinic_blocks_can_be_locked_and_moved_within_the_week() -> None:
    from rbs.models.enums import RotationKind, Session, SolverEngineName, SolverStatus, Weekday
    from rbs.models.schedule import AssignedClinic, Assignment, Schedule, ScheduleMeta

    instance = sample_instance()
    resident = instance.residents[0]
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.UNKNOWN,
            solver_status=SolverStatus.UNKNOWN,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id="fmed",
                kind=RotationKind.FMED,
                start_week=1,
                end_week=1,
                weeks=[1],
                clinic_slots=[
                    AssignedClinic(
                        weekday=Weekday.MONDAY,
                        session=Session.AFTERNOON,
                        site=instance.clinic_policy.primary_site_id,
                        week=1,
                    )
                ],
            )
        ],
    )

    locked = set_resident_clinic_slot_locked(
        instance,
        schedule,
        resident_id=resident.id,
        week=1,
        weekday=Weekday.MONDAY,
        session=Session.AFTERNOON,
        locked=True,
    )

    assert resident_clinic_slot_locked(
        locked,
        resident.id,
        1,
        Weekday.MONDAY,
        Session.AFTERNOON,
    )
    with pytest.raises(ValueError, match="locked"):
        move_resident_clinic_slot(
            instance,
            locked,
            resident_id=resident.id,
            source_week=1,
            source_weekday=Weekday.MONDAY,
            source_session=Session.AFTERNOON,
            target_week=1,
            target_weekday=Weekday.TUESDAY,
            target_session=Session.AFTERNOON,
        )

    moved = move_resident_clinic_slot(
        instance,
        schedule,
        resident_id=resident.id,
        source_week=1,
        source_weekday=Weekday.MONDAY,
        source_session=Session.AFTERNOON,
        target_week=1,
        target_weekday=Weekday.TUESDAY,
        target_session=Session.AFTERNOON,
    )

    assert [
        (slot.week, slot.weekday, slot.session, slot.site)
        for slot in moved.assignments[0].clinic_slots
    ] == [
        (
            1,
            Weekday.TUESDAY,
            Session.AFTERNOON,
            instance.clinic_policy.primary_site_id,
        )
    ]


def test_automatic_locking_covers_past_clinic_sessions_but_allows_manual_unlock() -> None:
    from rbs.clinic_locks import locked_clinic_states
    from rbs.models.enums import RotationKind, Session, SolverEngineName, SolverStatus, Weekday
    from rbs.models.schedule import AssignedClinic, Assignment, Schedule, ScheduleMeta

    instance = sample_instance().revised(lock_through_today=True)
    resident = instance.residents[0]
    today = instance.calendar.first_week_start
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.UNKNOWN,
            solver_status=SolverStatus.UNKNOWN,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id="fmed",
                kind=RotationKind.FMED,
                start_week=1,
                end_week=1,
                weeks=[1],
                clinic_slots=[
                    AssignedClinic(
                        weekday=Weekday.MONDAY,
                        session=Session.AFTERNOON,
                        site=instance.clinic_policy.primary_site_id,
                        week=1,
                    )
                ],
            )
        ],
    )

    assert resident_clinic_slot_locked(
        schedule,
        resident.id,
        1,
        Weekday.MONDAY,
        Session.AFTERNOON,
        instance=instance,
        today=today,
    )
    assert locked_clinic_states(instance, schedule, today=today) == {
        (resident.id, 1, Weekday.MONDAY, Session.AFTERNOON): True
    }
    with pytest.raises(ValueError, match="Unlock it before moving"):
        move_resident_clinic_slot(
            instance,
            schedule,
            resident_id=resident.id,
            source_week=1,
            source_weekday=Weekday.MONDAY,
            source_session=Session.AFTERNOON,
            target_week=1,
            target_weekday=Weekday.TUESDAY,
            target_session=Session.AFTERNOON,
            today=today,
        )

    unlocked = set_resident_clinic_slot_locked(
        instance,
        schedule,
        resident_id=resident.id,
        week=1,
        weekday=Weekday.MONDAY,
        session=Session.AFTERNOON,
        locked=False,
        today=today,
    )
    unlocked_slot = resident_clinic_slot(
        instance,
        unlocked,
        resident.id,
        1,
        Weekday.MONDAY,
        Session.AFTERNOON,
    )
    assert unlocked_slot.automatic_lock_exempt
    assert not resident_clinic_slot_locked(
        unlocked,
        resident.id,
        1,
        Weekday.MONDAY,
        Session.AFTERNOON,
        instance=instance,
        today=today,
    )
    assert locked_clinic_states(instance, unlocked, today=today) == {}

    moved = move_resident_clinic_slot(
        instance,
        unlocked,
        resident_id=resident.id,
        source_week=1,
        source_weekday=Weekday.MONDAY,
        source_session=Session.AFTERNOON,
        target_week=1,
        target_weekday=Weekday.TUESDAY,
        target_session=Session.AFTERNOON,
        today=today,
    )
    moved_slot = resident_clinic_slot(
        instance,
        moved,
        resident.id,
        1,
        Weekday.TUESDAY,
        Session.AFTERNOON,
    )
    assert moved_slot.automatic_lock_exempt


def test_resident_clinic_drop_swaps_occupied_blocks_and_identical_blocks_are_a_noop() -> None:
    from rbs.models.enums import RotationKind, Session, SolverEngineName, SolverStatus, Weekday
    from rbs.models.schedule import AssignedClinic, Assignment, Schedule, ScheduleMeta

    instance = sample_instance()
    resident = instance.residents[0]

    def schedule_with(second_site: str, *, second_locked: bool = False) -> Schedule:
        return Schedule(
            meta=ScheduleMeta(
                academic_year=instance.academic_year,
                engine=SolverEngineName.STUB,
                status=SolverStatus.UNKNOWN,
                solver_status=SolverStatus.UNKNOWN,
            ),
            assignments=[
                Assignment(
                    resident_id=resident.id,
                    rotation_id="fmed",
                    kind=RotationKind.FMED,
                    start_week=1,
                    end_week=1,
                    weeks=[1],
                    clinic_slots=[
                        AssignedClinic(
                            weekday=Weekday.TUESDAY,
                            session=Session.AFTERNOON,
                            site="cedar",
                            week=1,
                        ),
                        AssignedClinic(
                            weekday=Weekday.THURSDAY,
                            session=Session.AFTERNOON,
                            site=second_site,
                            locked=second_locked,
                            week=1,
                        ),
                    ],
                )
            ],
        )

    schedule = schedule_with("maple")
    swapped = move_resident_clinic_slot(
        instance,
        schedule,
        resident_id=resident.id,
        source_week=1,
        source_weekday=Weekday.TUESDAY,
        source_session=Session.AFTERNOON,
        target_week=1,
        target_weekday=Weekday.THURSDAY,
        target_session=Session.AFTERNOON,
    )
    sites_by_day = {slot.weekday: slot.site for slot in swapped.assignments[0].clinic_slots}
    assert sites_by_day == {
        Weekday.TUESDAY: "maple",
        Weekday.THURSDAY: "cedar",
    }

    identical = schedule_with("cedar")
    assert (
        move_resident_clinic_slot(
            instance,
            identical,
            resident_id=resident.id,
            source_week=1,
            source_weekday=Weekday.TUESDAY,
            source_session=Session.AFTERNOON,
            target_week=1,
            target_weekday=Weekday.THURSDAY,
            target_session=Session.AFTERNOON,
        )
        is identical
    )

    with pytest.raises(ValueError, match="destination block is locked"):
        move_resident_clinic_slot(
            instance,
            schedule_with("maple", second_locked=True),
            resident_id=resident.id,
            source_week=1,
            source_weekday=Weekday.TUESDAY,
            source_session=Session.AFTERNOON,
            target_week=1,
            target_weekday=Weekday.THURSDAY,
            target_session=Session.AFTERNOON,
        )


def test_resident_clinic_conflicts_explain_fixed_academic_and_rotation_rules() -> None:
    from rbs.models.enums import RotationKind, Session, SolverEngineName, SolverStatus, Weekday
    from rbs.models.schedule import AssignedClinic, Assignment, Schedule, ScheduleMeta

    instance = sample_instance()
    resident = instance.residents[0]
    source = AssignedClinic(
        weekday=Weekday.MONDAY,
        session=Session.AFTERNOON,
        site=instance.clinic_policy.primary_site_id,
        week=1,
    )
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.UNKNOWN,
            solver_status=SolverStatus.UNKNOWN,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id="fmed",
                kind=RotationKind.FMED,
                start_week=1,
                end_week=1,
                weeks=[1],
                clinic_slots=[source],
            )
        ],
    )

    academic = resident_clinic_target_conflicts(
        instance,
        schedule,
        resident_id=resident.id,
        week=1,
        weekday=Weekday.WEDNESDAY,
        session=Session.AFTERNOON,
        source_slot=source,
    )
    disallowed = resident_clinic_target_conflicts(
        instance,
        schedule,
        resident_id=resident.id,
        week=1,
        weekday=Weekday.MONDAY,
        session=Session.MORNING,
        source_slot=source,
    )

    assert any("Academic Half Day is fixed" in reason for reason in academic)
    assert any("clinic rules do not allow Monday AM" in reason for reason in disallowed)


def test_resident_clinic_conflicts_apply_overall_and_pgy_inpatient_limits() -> None:
    from rbs.models.enums import RotationKind, Session, SolverEngineName, SolverStatus, Weekday
    from rbs.models.instance import SchedulerInput
    from rbs.models.schedule import AssignedClinic, Assignment, Schedule, ScheduleMeta
    from rbs.solver.validation import validate_schedule

    raw = sample_instance().model_dump(mode="json")
    fmed = next(rotation for rotation in raw["rotations"] if rotation["id"] == "fmed")
    fmed["clinic"]["max_concurrent"] = 2
    fmed["clinic"]["max_concurrent_by_pgy"] = {"1": 1}
    instance = SchedulerInput.model_validate(raw)
    pgy1_a, pgy1_b = instance.residents[:2]
    pgy2 = next(resident for resident in instance.residents if resident.pgy == 2)
    target = (Weekday.TUESDAY, Session.AFTERNOON)

    def slot(weekday, session):
        return AssignedClinic(
            weekday=weekday,
            session=session,
            site=instance.clinic_policy.primary_site_id,
            week=1,
        )

    assignments = [
        Assignment(
            resident_id=resident.id,
            rotation_id="fmed",
            kind=RotationKind.FMED,
            start_week=1,
            end_week=1,
            weeks=[1],
            clinic_slots=[
                slot(
                    target[0] if resident.pgy == 1 else Weekday.MONDAY,
                    target[1],
                )
            ],
        )
        for resident in (pgy1_a, pgy1_b, pgy2)
    ]
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.UNKNOWN,
            solver_status=SolverStatus.UNKNOWN,
        ),
        assignments=assignments,
    )

    pgy_reasons = resident_clinic_target_conflicts(
        instance,
        schedule,
        resident_id=pgy1_b.id,
        week=1,
        weekday=target[0],
        session=target[1],
        source_slot=assignments[1].clinic_slots[0],
    )
    overall_reasons = resident_clinic_target_conflicts(
        instance,
        schedule,
        resident_id=pgy2.id,
        week=1,
        weekday=target[0],
        session=target[1],
        source_slot=assignments[2].clinic_slots[0],
    )

    assert any("at most 1 PGY1 resident" in reason for reason in pgy_reasons)
    assert not any("at most 2 residents" in reason for reason in pgy_reasons)
    assert any("at most 2 residents" in reason for reason in overall_reasons)
    assert any(
        "FMED PGY1 clinic concurrency exceeded" in error
        for error in validate_schedule(instance, schedule).errors
    )

    assignments[2] = assignments[2].model_copy(
        update={
            "clinic_slots": [slot(target[0], target[1])],
        }
    )
    overfull = schedule.model_copy(update={"assignments": assignments})
    assert any(
        "FMED clinic concurrency exceeded" in error
        for error in validate_schedule(instance, overfull).errors
    )


def test_prohibited_clinic_drop_is_persisted_as_a_manual_override() -> None:
    from rbs.models.enums import RotationKind, Session, SolverEngineName, SolverStatus, Weekday
    from rbs.models.schedule import AssignedClinic, Assignment, Schedule, ScheduleMeta
    from rbs.solver.validation import validate_schedule

    instance = sample_instance()
    resident = instance.residents[0]
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.UNKNOWN,
            solver_status=SolverStatus.UNKNOWN,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id="fmed",
                kind=RotationKind.FMED,
                start_week=1,
                end_week=1,
                weeks=[1],
                clinic_slots=[
                    AssignedClinic(
                        weekday=Weekday.MONDAY,
                        session=Session.AFTERNOON,
                        site=instance.clinic_policy.primary_site_id,
                        week=1,
                    )
                ],
            )
        ],
    )

    moved = move_resident_clinic_slot(
        instance,
        schedule,
        resident_id=resident.id,
        source_week=1,
        source_weekday=Weekday.MONDAY,
        source_session=Session.AFTERNOON,
        target_week=1,
        target_weekday=Weekday.MONDAY,
        target_session=Session.MORNING,
    )
    override = resident_clinic_slot(
        instance,
        moved,
        resident.id,
        1,
        Weekday.MONDAY,
        Session.MORNING,
    )

    assert override.manual_override is True
    assert any(
        "clinic rules do not allow Monday AM" in reason
        for reason in resident_clinic_target_conflicts(
            instance,
            moved,
            resident_id=resident.id,
            week=1,
            weekday=Weekday.MONDAY,
            session=Session.MORNING,
            source_slot=override,
        )
    )
    locked = set_resident_clinic_slot_locked(
        instance,
        moved,
        resident_id=resident.id,
        week=1,
        weekday=Weekday.MONDAY,
        session=Session.MORNING,
        locked=True,
    )
    assert validate_schedule(instance, locked).valid

    with pytest.raises(ValueError, match="Academic Half Day is fixed"):
        move_resident_clinic_slot(
            instance,
            moved,
            resident_id=resident.id,
            source_week=1,
            source_weekday=Weekday.MONDAY,
            source_session=Session.MORNING,
            target_week=1,
            target_weekday=Weekday.WEDNESDAY,
            target_session=Session.AFTERNOON,
        )


def test_manual_clinic_add_remove_and_site_change_track_the_weekly_delta() -> None:
    from rbs.models.enums import RotationKind, Session, SolverEngineName, SolverStatus, Weekday
    from rbs.models.schedule import AssignedClinic, Assignment, Schedule, ScheduleMeta
    from rbs.solver.validation import validate_schedule

    instance = sample_instance()
    resident = instance.residents[0]
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.UNKNOWN,
            solver_status=SolverStatus.UNKNOWN,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id="fmed",
                kind=RotationKind.FMED,
                start_week=1,
                end_week=1,
                weeks=[1],
                clinic_slots=[
                    AssignedClinic(
                        weekday=Weekday.TUESDAY,
                        session=Session.AFTERNOON,
                        site="cedar",
                        allowed_sites=["cedar"],
                        week=1,
                    )
                ],
            )
        ],
    )

    available = resident_clinic_available_site_ids(
        instance,
        schedule,
        resident_id=resident.id,
        week=1,
        weekday=Weekday.THURSDAY,
        session=Session.MORNING,
    )
    assert {"maple", "cedar"} <= set(available)

    added = add_resident_clinic_slot(
        instance,
        schedule,
        resident_id=resident.id,
        week=1,
        weekday=Weekday.THURSDAY,
        session=Session.MORNING,
        site_id="maple",
    )
    extra = resident_clinic_slot(
        instance,
        added,
        resident.id,
        1,
        Weekday.THURSDAY,
        Session.MORNING,
    )
    assert extra.manual_override is True
    assert extra.manual_override_added is True
    assert resident_clinic_week_override_delta(instance, added, resident.id, 1) == 1
    assert validate_schedule(instance, added).valid
    reloaded = Schedule.model_validate_json(added.model_dump_json())
    assert reloaded.assignments[0].manual_clinic_baselines == {1: 1}
    assert any(
        slot.manual_override and slot.manual_override_added
        for slot in reloaded.assignments[0].clinic_slots
    )

    changed = change_resident_clinic_slot_site(
        instance,
        schedule,
        resident_id=resident.id,
        week=1,
        weekday=Weekday.TUESDAY,
        session=Session.AFTERNOON,
        site_id="maple",
    )
    changed_slot = resident_clinic_slot(
        instance,
        changed,
        resident.id,
        1,
        Weekday.TUESDAY,
        Session.AFTERNOON,
    )
    assert changed_slot.site == "maple"
    assert changed_slot.manual_override is True
    assert changed_slot.manual_override_original_site == "cedar"
    assert validate_schedule(instance, changed).valid

    reset = change_resident_clinic_slot_site(
        instance,
        changed,
        resident_id=resident.id,
        week=1,
        weekday=Weekday.TUESDAY,
        session=Session.AFTERNOON,
        site_id="cedar",
    )
    reset_slot = resident_clinic_slot(
        instance,
        reset,
        resident.id,
        1,
        Weekday.TUESDAY,
        Session.AFTERNOON,
    )
    assert reset_slot.site == "cedar"
    assert reset_slot.manual_override_original_site is None
    assert reset_slot.manual_override is False
    assert validate_schedule(instance, reset).valid

    locked = set_resident_clinic_slot_locked(
        instance,
        schedule,
        resident_id=resident.id,
        week=1,
        weekday=Weekday.TUESDAY,
        session=Session.AFTERNOON,
        locked=True,
    )
    with pytest.raises(ValueError, match="Unlock it before deleting"):
        remove_resident_clinic_slot(
            instance,
            locked,
            resident_id=resident.id,
            week=1,
            weekday=Weekday.TUESDAY,
            session=Session.AFTERNOON,
        )
    with pytest.raises(ValueError, match="Unlock it before changing its site"):
        change_resident_clinic_slot_site(
            instance,
            locked,
            resident_id=resident.id,
            week=1,
            weekday=Weekday.TUESDAY,
            session=Session.AFTERNOON,
            site_id="maple",
        )

    removed = remove_resident_clinic_slot(
        instance,
        schedule,
        resident_id=resident.id,
        week=1,
        weekday=Weekday.TUESDAY,
        session=Session.AFTERNOON,
    )
    assert resident_clinic_week_override_delta(instance, removed, resident.id, 1) == -1
    removed_rows = resident_clinic_schedule_report_rows(
        instance,
        removed,
        resident.id,
    )
    assert [row["week"] for row in removed_rows] == [str(week) for week in range(1, 53)]
    assert removed_rows[0]["sessions"] == ""
    assert validate_schedule(instance, removed).valid


def test_manual_clinic_edits_still_require_preceptor_availability() -> None:
    from rbs.models.enums import RotationKind, Session, SolverEngineName, SolverStatus, Weekday
    from rbs.models.schedule import AssignedClinic, Assignment, Schedule, ScheduleMeta

    instance = sample_instance()
    resident = instance.residents[0]
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.UNKNOWN,
            solver_status=SolverStatus.UNKNOWN,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id="fmed",
                kind=RotationKind.FMED,
                start_week=1,
                end_week=1,
                weeks=[1],
                clinic_slots=[
                    AssignedClinic(
                        weekday=Weekday.TUESDAY,
                        session=Session.AFTERNOON,
                        site="maple",
                        week=1,
                    )
                ],
            )
        ],
    )

    assert "maple" not in resident_clinic_available_site_ids(
        instance,
        schedule,
        resident_id=resident.id,
        week=1,
        weekday=Weekday.MONDAY,
        session=Session.MORNING,
    )
    with pytest.raises(ValueError, match="no attending coverage"):
        move_resident_clinic_slot(
            instance,
            schedule,
            resident_id=resident.id,
            source_week=1,
            source_weekday=Weekday.TUESDAY,
            source_session=Session.AFTERNOON,
            target_week=1,
            target_weekday=Weekday.MONDAY,
            target_session=Session.MORNING,
        )
    with pytest.raises(ValueError, match="Academic Half Day is fixed"):
        add_resident_clinic_slot(
            instance,
            schedule,
            resident_id=resident.id,
            week=1,
            weekday=Weekday.WEDNESDAY,
            session=Session.AFTERNOON,
            site_id="cedar",
        )


def test_outdated_resident_clinic_edit_mode_renders_all_edit_controls(
    monkeypatch,
) -> None:
    from nicegui import ui

    from rbs.models.enums import RotationKind, Session, SolverEngineName, SolverStatus, Weekday
    from rbs.models.schedule import AssignedClinic, Assignment, Schedule, ScheduleMeta
    from rbs.ui.residents import ops as resident_ops
    from rbs.ui.residents import schedule as resident_schedule

    instance = sample_instance()
    resident = instance.residents[0]
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.UNKNOWN,
            solver_status=SolverStatus.UNKNOWN,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id="fmed",
                kind=RotationKind.FMED,
                start_week=1,
                end_week=1,
                weeks=[1],
                clinic_slots=[
                    AssignedClinic(
                        weekday=Weekday.MONDAY,
                        session=Session.AFTERNOON,
                        site=instance.clinic_policy.primary_site_id,
                        week=1,
                    ),
                    AssignedClinic(
                        weekday=Weekday.TUESDAY,
                        session=Session.AFTERNOON,
                        site="maple",
                        locked=True,
                        week=1,
                    ),
                ],
            )
        ],
    )
    before = set(ui.context.client.elements)
    occupancy_calls = {"report": 0, "fallback": 0}
    real_occupancy = resident_schedule.occupancy

    def report_occupancy(*args, **kwargs):
        occupancy_calls["report"] += 1
        return real_occupancy(*args, **kwargs)

    def fallback_occupancy(*args, **kwargs):
        occupancy_calls["fallback"] += 1
        return real_occupancy(*args, **kwargs)

    monkeypatch.setattr(resident_schedule, "occupancy", report_occupancy)
    monkeypatch.setattr(resident_ops, "occupancy", fallback_occupancy)

    _resident_schedule_workspace(
        instance,
        schedule,
        resident,
        today=instance.calendar.first_week_start,
        on_schedule_change=lambda _schedule, _resident_id, _refresh: None,
        schedule_is_current=False,
        schedule_editing=True,
        on_schedule_editing_change=lambda _editing: None,
    )

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    events = [
        element
        for element in created
        if "rbs-resident-clinic-event" in getattr(element, "_classes", [])
        and "academic" not in getattr(element, "_classes", [])
    ]
    events_by_day = {element._props.get("data-weekday"): element for element in events}
    buttons = {
        element._props.get("aria-label"): element
        for element in created
        if element.__class__.__name__ == "Button"
    }
    labels = {getattr(element, "_text", None) for element in created}
    schedule_panels = next(
        element for element in created if element.__class__.__name__ == "TabPanels"
    )
    academic_event = next(
        element
        for element in created
        if "rbs-resident-clinic-event" in getattr(element, "_classes", [])
        and "academic" in getattr(element, "_classes", [])
    )
    academic_cell = academic_event.parent_slot.parent

    assert events_by_day["monday"]._props["draggable"] == "true"
    assert events_by_day["tuesday"]._props["draggable"] == "false"
    assert "Lock clinic block" in buttons
    assert "Unlock clinic block" in buttons
    assert "Done editing" in {
        element._props.get("label") for element in created if element.__class__.__name__ == "Button"
    }
    assert "Academic Half Day" in labels
    assert schedule_panels._props["model-value"] == "resident_clinic_schedule"
    assert any(
        "rbs-resident-clinic-conflict-icon" in getattr(element, "_classes", [])
        for element in created
    )
    assert any(
        "academic" in getattr(element, "_classes", [])
        and "is-locked" in getattr(element, "_classes", [])
        for element in created
    )
    assert not any(
        "rbs-resident-clinic-conflict-icon" in getattr(element, "_classes", [])
        and element.parent_slot.parent is academic_cell
        for element in created
    )
    assert occupancy_calls == {"report": 1, "fallback": 0}


def test_resident_clinic_manual_override_renders_context_actions_and_week_callout() -> None:
    from nicegui import ui

    from rbs.models.enums import RotationKind, Session, SolverEngineName, SolverStatus, Weekday
    from rbs.models.schedule import AssignedClinic, Assignment, Schedule, ScheduleMeta

    instance = sample_instance()
    resident = instance.residents[0]
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.UNKNOWN,
            solver_status=SolverStatus.UNKNOWN,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id="fmed",
                kind=RotationKind.FMED,
                start_week=1,
                end_week=1,
                weeks=[1],
                clinic_slots=[
                    AssignedClinic(
                        weekday=Weekday.TUESDAY,
                        session=Session.AFTERNOON,
                        site="maple",
                        allowed_sites=["cedar"],
                        manual_override=True,
                        manual_override_original_site="cedar",
                        week=1,
                    )
                ],
                manual_clinic_baselines={1: 0},
            )
        ],
    )
    before = set(ui.context.client.elements)

    _resident_schedule_workspace(
        instance,
        schedule,
        resident,
        today=instance.calendar.first_week_start,
        on_schedule_change=lambda _schedule, _resident_id, _refresh: None,
        schedule_editing=True,
        on_schedule_editing_change=lambda _editing: None,
    )

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    manual_event = next(
        element for element in created if "manual-override" in getattr(element, "_classes", [])
    )
    manual_cell = manual_event.parent_slot.parent
    academic_event = next(
        element for element in created if "academic" in getattr(element, "_classes", [])
    )
    academic_cell = academic_event.parent_slot.parent
    labels = {getattr(element, "_text", None) for element in created}
    context_menus = [element for element in created if element.__class__.__name__ == "ContextMenu"]

    assert "Manual override" in labels
    assert "1 extra clinic half-day than usual this week · manual override" in labels
    assert "Add extra block" in labels
    assert "Change clinic site" in labels
    assert "Delete clinic block" in labels
    assert "Reset to Cedar" in labels
    assert "has-manual-override" in manual_cell._classes
    assert not any(
        "rbs-resident-clinic-conflict-icon" in getattr(element, "_classes", [])
        and element.parent_slot.parent is manual_cell
        for element in created
    )
    assert context_menus
    assert not any(menu.parent_slot.parent is academic_cell for menu in context_menus)


def test_replace_resident_updates_information() -> None:
    instance = sample_instance()
    original = instance.residents[0]
    replacement = Resident(
        id=original.id,
        name="Dr. Avery Chen",
        pgy=original.pgy,
        vacation_weeks=[1, 14, 27, 40],
        days_off=[date(2026, 8, 18), date(2027, 2, 5)],
    )

    updated = replace_resident(instance, original.id, replacement)

    assert updated.residents[0] == replacement
    assert instance.residents[0] == original


def test_replace_resident_id_updates_referencing_locks() -> None:
    instance = sample_instance()
    original = instance.residents[0]
    replacement = original.model_copy(update={"id": "new-resident-id"})

    updated = replace_resident(instance, original.id, replacement)

    assert updated.residents[0].id == "new-resident-id"
    assert all(lock.resident_id != original.id for lock in updated.locks)
    assert any(lock.resident_id == "new-resident-id" for lock in updated.locks)


def test_add_resident_validates_the_complete_instance() -> None:
    instance = sample_instance()
    resident = Resident(
        id="resident-025",
        name="Robin Shah",
        pgy=1,
        vacation_weeks=[6, 18, 30, 42],
    )

    updated = add_resident(instance, resident)

    assert updated.residents[-1] == resident


def test_next_resident_id_is_neutral_and_does_not_encode_pgy() -> None:
    instance = sample_instance()

    assert next_resident_id(instance) == "resident-025"
    assert all("pgy" not in resident.id.lower() for resident in instance.residents)


def test_vacation_monday_round_trips_to_week_number() -> None:
    instance = sample_instance()

    assert vacation_monday(instance, 1).isoformat() == "2026-06-29"
    assert vacation_monday(instance, 52).isoformat() == "2027-06-21"
    assert vacation_week_for_monday(instance, "2026-09-14") == 12


def test_vacation_calendar_rejects_dates_that_are_not_mondays() -> None:
    with pytest.raises(ValueError, match="must be Mondays"):
        vacation_week_for_monday(sample_instance(), "2026-09-15")


def test_vacation_calendar_highlights_the_full_monday_through_sunday_week() -> None:
    assert vacation_range_for_monday("2026-09-14") == {
        "from": "2026-09-14",
        "to": "2026-09-20",
    }
    september = vacation_month_dates(2026, 9)
    assert september[0].isoformat() == "2026-08-30"
    assert september[-1].isoformat() == "2026-10-03"
    assert len(september) % 7 == 0
    assert all(day.weekday() == 6 for day in september[::7])

    with pytest.raises(ValueError, match="must be Mondays"):
        vacation_range_for_monday("2026-09-13")


def test_vacation_calendar_enables_only_academic_year_mondays() -> None:
    instance = sample_instance()

    assert vacation_monday_is_selectable(instance, vacation_monday(instance, 1))
    assert vacation_monday_is_selectable(instance, vacation_monday(instance, 52))
    assert not vacation_monday_is_selectable(
        instance, vacation_monday(instance, 1) - timedelta(days=1)
    )
    assert not vacation_monday_is_selectable(
        instance, vacation_monday(instance, 1) + timedelta(days=1)
    )
    assert not vacation_monday_is_selectable(
        instance, vacation_monday(instance, 52) + timedelta(days=7)
    )


def test_individual_day_off_calendar_accepts_any_date_in_academic_year() -> None:
    instance = sample_instance()
    first_day = instance.calendar.first_week_start
    last_day = first_day + timedelta(days=instance.calendar.weeks * 7 - 1)

    assert day_off_date(instance, "2026-09-15") == date(2026, 9, 15)
    assert day_off_is_selectable(instance, first_day)
    assert day_off_is_selectable(instance, last_day)
    assert not day_off_is_selectable(instance, first_day - timedelta(days=1))
    assert not day_off_is_selectable(instance, last_day + timedelta(days=1))


def test_individual_days_off_are_sorted_and_unique() -> None:
    resident = Resident(
        id="resident-017",
        name="Robin Shah",
        pgy=1,
        days_off=[date(2027, 1, 4), date(2026, 9, 15)],
    )

    assert resident.days_off == [date(2026, 9, 15), date(2027, 1, 4)]
    with pytest.raises(ValueError, match="days_off must be unique"):
        Resident(
            id="resident-018",
            name="Lee Chen",
            pgy=1,
            days_off=[date(2026, 9, 15), date(2026, 9, 15)],
        )
