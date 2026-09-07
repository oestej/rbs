from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from rbs.catalog import blank_instance, sample_instance
from rbs.models.enums import WEEKDAYS_MF, RotationKind, Session, Weekday
from rbs.models.rotation import ALL_CLINIC_SITES, ClinicRule, Rotation
from rbs.solver.planning import expand_occurrences
from rbs.ui.clinic.ops import (
    add_clinic,
    cancel_academic_half_day_for_week,
    copy_clinic_closure_days,
    disable_academic_half_day,
    remove_academic_half_day_override,
    remove_clinic,
    replace_academic_half_day,
    replace_clinic,
    replace_clinic_closure_days,
    set_academic_half_day_override,
)
from rbs.ui.clinic.tab import (
    _manual_clinic_resident_options,
    _manual_funding_options,
    _open_clinic_block_rules_dialog,
    _open_clinic_editor_dialog,
    _open_manual_clinic_block_dialog,
    _open_manual_clinic_waiver_dialog,
    render_clinic_tab,
)
from rbs.ui.editor_common import (
    _academic_block_start_for_week,
    _academic_block_start_options,
)
from rbs.ui.rotations.editor import (
    NEW_MANDATORY_ROTATION_ID,
    _apply_away_selection,
    _apply_clinic_sites,
    _clinic_rule_editor,
    _confirm_remove_mandatory_rotation,
    _core_settings,
    _open_fmed_pgy_rules_dialog,
    _open_resident_rotation_override_dialog,
    _resident_override_group_bundle,
    _rotation_detail_contents,
    _rotation_editor,
    _rotation_kind_label,
    _rotation_list_item,
    _rotation_summary_html,
    render_rotations_tab,
)
from rbs.ui.rotations.ops import (
    add_mandatory_rotation,
    add_manual_clinic_block,
    add_resident_rotation_waiver,
    next_mandatory_rotation_id,
    remove_mandatory_rotation,
    remove_manual_clinic_block,
    remove_resident_rotation_waiver,
    replace_clinic_block_rules,
    replace_fmed_pgy_rules,
    replace_rotation_color,
    replace_standard_rotation,
    resident_missing_mandatory_rotations,
    resident_rotation_week_totals,
    rotation_editor_state,
    rotation_from_editor_state,
    rotation_group_members_by_pgy,
    set_elective_allocation,
    special_rotations,
    standard_rotations,
)
from rbs.ui.rotations.widgets import (
    add_block_config,
    rotation_color_palette,
    set_clinic_slot_preferred,
    toggle_pgy_rule,
)


def test_rotation_editor_partitions_standard_and_special_rotations() -> None:
    instance = sample_instance()

    editable = standard_rotations(instance)
    special = special_rotations(instance)

    assert editable
    assert [rotation.code for rotation in editable] == sorted(
        (rotation.code for rotation in editable),
        key=str.casefold,
    )
    assert all(rotation.kind is RotationKind.STANDARD for rotation in editable)
    assert [rotation.code for rotation in special] == sorted(
        (rotation.code for rotation in special),
        key=str.casefold,
    )
    assert {rotation.id for rotation in special} == {
        "clinic",
        "fmed",
        "elective",
        "geriatrics",
        "palliative_care",
    }
    assert {rotation.id for rotation in editable}.isdisjoint(rotation.id for rotation in special)


def test_add_and_remove_mandatory_rotation_moves_unscheduled_weeks() -> None:
    instance = sample_instance()
    rotation = Rotation.model_validate(
        {
            "id": "addiction_medicine",
            "code": "ADDICT",
            "name": "Addiction Medicine",
            "kind": RotationKind.STANDARD.value,
            "pgy_rules": [
                {
                    "pgy": 1,
                    "block_configs": [{"duration_weeks": 2}],
                }
            ],
        }
    )

    # A fully allocated curriculum funds the new requirement from Elective time.
    assert instance.unallocated_weeks(1) == 0

    added = add_mandatory_rotation(instance, rotation, {(1, 2): 1})

    assert added.rotation(rotation.id) == rotation
    assert added.curriculum_for(1).required_weeks() == 52
    assert added.unallocated_weeks(1) == 0
    assert any(
        block.rotation_id == rotation.id and block.duration_weeks == 2 and block.count == 1
        for block in added.curriculum_for(1).blocks
    )
    assert not any(block.rotation_id == "elective" for block in added.curriculum_for(1).blocks)

    removed = remove_mandatory_rotation(added, rotation.id)

    # The weeks return to the unscheduled pool rather than being backfilled.
    assert rotation.id not in removed.rotations_by_id
    assert removed.unallocated_weeks(1) == 2
    assert not any(block.rotation_id == "elective" for block in removed.curriculum_for(1).blocks)


def test_remove_mandatory_rotation_repairs_references_and_prerequisites() -> None:
    instance = sample_instance()
    icu = instance.rotation("icu")
    icu_rules = [
        rule.model_copy(
            update={
                "prerequisite_rotation_ids": [
                    *rule.prerequisite_rotation_ids,
                    "night_float",
                ]
            }
        )
        if rule.pgy == 1
        else rule
        for rule in icu.pgy_rules
    ]
    configured = replace_standard_rotation(
        instance,
        icu.id,
        icu.model_copy(update={"pgy_rules": icu_rules}),
    )
    raw = configured.model_dump(mode="json")
    raw["locks"].append(
        {
            "resident_id": "resident-001",
            "rotation_id": "night_float",
            "weeks": [5, 6],
        }
    )
    raw["resident_rotation_overrides"].append(
        {
            "resident_id": "resident-001",
            "rotation_id": "night_float",
            "duration_weeks": 2,
            "replaces_rotation_id": "elective",
        }
    )
    configured = type(configured).from_payload(raw)

    removed = remove_mandatory_rotation(configured, "night_float")

    assert "night_float" not in removed.rotations_by_id
    assert "night_float" not in removed.rotation("icu").pgy_rule(1).prerequisite_rotation_ids
    assert removed.unallocated_weeks(1) == instance.curriculum_for(1).required_weeks() - (
        removed.curriculum_for(1).required_weeks()
    )
    assert removed.curriculum_for(1).required_weeks() < 52
    assert all(lock.rotation_id != "night_float" for lock in removed.locks)
    assert all(
        override.rotation_id != "night_float" and override.replaces_rotation_id != "night_float"
        for override in removed.resident_rotation_overrides
    )


def test_add_mandatory_rotation_requires_enough_unscheduled_time() -> None:
    instance = sample_instance()
    rotation = Rotation.model_validate(
        {
            "id": "addiction_medicine",
            "code": "ADDICT",
            "name": "Addiction Medicine",
            "pgy_rules": [
                {
                    "pgy": 1,
                    "block_configs": [{"duration_weeks": 4}],
                }
            ],
        }
    )

    # PGY1 is fully allocated and holds only 2 direct Elective weeks to give up.
    with pytest.raises(
        ValueError,
        match="only 0 unscheduled and 2 direct Elective weeks",
    ):
        add_mandatory_rotation(instance, rotation, {(1, 4): 1})


def test_add_mandatory_rotation_rejects_an_impossible_consecutive_limit() -> None:
    instance = sample_instance()
    rotation = Rotation.model_validate(
        {
            "id": "addiction_medicine",
            "code": "ADDICT",
            "name": "Addiction Medicine",
            "max_consecutive_weeks": 1,
            "pgy_rules": [
                {
                    "pgy": 1,
                    "block_configs": [{"duration_weeks": 2}],
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="exceeds its 1-week consecutive limit"):
        add_mandatory_rotation(instance, rotation, {(1, 2): 1})


def test_next_mandatory_rotation_id_is_readable_and_unique() -> None:
    instance = sample_instance()

    assert next_mandatory_rotation_id(instance, "Addiction Medicine") == "addiction_medicine"
    assert next_mandatory_rotation_id(instance, "Behavioral Health") == "behavioral_health_2"


def test_mandatory_directory_exposes_new_rotation_editor() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)
    render_rotations_tab(
        instance,
        selected_rotation_id=NEW_MANDATORY_ROTATION_ID,
        on_select=lambda _rotation_id: None,
        on_save=lambda _instance, _rotation_id: None,
        active_section="standard_rotations",
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    text = {getattr(element, "_text", None) for element in created}
    button_labels = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Button"
    }
    input_labels = {
        element._props.get("label")
        for element in created
        if element.__class__.__name__ in {"Input", "Number"}
    }
    editor_tabs = next(
        element
        for element in created
        if element.__class__.__name__ == "Tabs"
        and "rbs-rotation-editor-tabs" in getattr(element, "_classes", [])
    )
    tabs = {
        element._props.get("label")
        for element in created
        if element.__class__.__name__ == "Tab"
        and element.parent_slot is not None
        and element.parent_slot.parent is editor_tabs
    }

    # No separate add screen: creation shares the full-screen editor.
    assert not any(element.__class__.__name__ == "Dialog" for element in created)
    assert "New mandatory rotation" in text
    assert "Creating" in text
    assert "Available to PGY 1" in text
    assert "Required for PGY 1" not in text
    assert "New rotation" in button_labels
    assert "Add rotation" in button_labels
    assert {"Rotation code", "Rotation name"} <= input_labels
    assert tabs == {"General", "Training-level rules", "Clinic"}


def test_new_rotation_block_configurations_default_to_two_weeks() -> None:
    draft = {"pgy_rules": []}
    refreshes: list[bool] = []

    toggle_pgy_rule(
        draft,
        2,
        (1, 2, 3),
        lambda: refreshes.append(True),
        SimpleNamespace(value=True),
    )

    rule = draft["pgy_rules"][0]
    assert rule["block_configs"][0]["duration_weeks"] == 2

    rule["block_configs"] = [{"duration_weeks": 1}]
    add_block_config(rule, lambda: refreshes.append(True))

    assert [config["duration_weeks"] for config in rule["block_configs"]] == [1, 2]
    assert refreshes == [True, True]


def test_mandatory_rotation_remove_requires_confirmation() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)
    _confirm_remove_mandatory_rotation(
        instance,
        instance.rotation("icu"),
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    text = {getattr(element, "_text", None) for element in created}

    assert "Remove ICU?" in text
    assert any(
        element.__class__.__name__ == "Button"
        and element._props.get("label") == "Remove rotation"
        and element._props.get("color") == "negative"
        for element in created
    )


def test_rotation_detail_and_editor_omit_curriculum_usage_and_rotation_notes() -> None:
    from nicegui import ui

    instance = sample_instance()
    rotation = instance.rotation("outpatient_gyn")

    before = set(ui.context.client.elements)
    _rotation_detail_contents(instance, rotation)
    detail_elements = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    detail_text = {getattr(element, "_text", None) for element in detail_elements}

    assert "Curriculum usage" not in detail_text
    assert "Rotation notes" not in detail_text

    before = set(ui.context.client.elements)
    _rotation_editor(
        instance,
        rotation,
        on_cancel=lambda: None,
        on_save=lambda _instance, _rotation_id: None,
    )
    editor_elements = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    editor_text = {getattr(element, "_text", None) for element in editor_elements}

    assert "Curriculum usage" not in editor_text
    assert "Rotation notes" not in editor_text
    assert not any(
        element.__class__.__name__ == "Textarea" and element._props.get("label") == "Rotation notes"
        for element in editor_elements
    )


def test_rotation_viewer_prioritizes_structured_schedule_rules() -> None:
    from nicegui import ui

    instance = sample_instance()
    rotation = instance.rotation("behavioral_health")
    before = set(ui.context.client.elements)

    _rotation_detail_contents(instance, rotation)

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    text = {getattr(element, "_text", None) for element in created}

    assert {"Training-level rules", "Continuity clinic", "Operational rules"} <= text
    assert "Required schedule, block shape, staffing, and placement in one place." not in text
    assert "Required 1 × 4 weeks" in text
    assert rotation.color in text
    assert "Rotation code" not in text
    assert "Block configurations" not in text
    assert any("rbs-rotation-pgy-card" in getattr(element, "_classes", []) for element in created)


def test_rotation_editor_uses_task_focused_tabs_and_sticky_actions() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)

    _rotation_editor(
        instance,
        instance.rotation("behavioral_health"),
        on_cancel=lambda: None,
        on_save=lambda _instance, _rotation_id: None,
    )

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    tabs = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Tab"
    }
    text = {getattr(element, "_text", None) for element in created}
    field_labels = {
        element._props.get("label")
        for element in created
        if element.__class__.__name__ == "Number"
    }

    assert tabs == {"General", "Training-level rules", "Clinic", "Advanced"}
    assert (
        "Configure availability, staffing, placement, and block formats for each training year."
        not in text
    )
    assert (
        "A training-level minimum applies in every academic week. Year limits do not replace "
        "the overall rotation limits above. If a training year has no maximum, Maximum total "
        "residents still applies. Vacation allowance is set per block format." in text
    )
    assert "Minimum total residents each week" in field_labels
    assert any(
        isinstance(label, str)
        and label.startswith("Minimum ")
        and label.endswith(" residents each week")
        for label in field_labels
    )
    assert any(
        "rbs-rotation-editor-actions" in getattr(element, "_classes", []) for element in created
    )
    assert not any(
        getattr(element, "_text", None) == "PGY placement, staffing, and block configurations"
        for element in created
    )


def test_earliest_start_uses_four_week_academic_block_dropdown() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)

    _rotation_editor(
        instance,
        instance.rotation("outpatient_gyn"),
        on_cancel=lambda: None,
        on_save=lambda _instance, _rotation_id: None,
    )

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    earliest = [
        element for element in created if element._props.get("label") == "Earliest start block"
    ]
    options = _academic_block_start_options(
        instance.calendar.first_week_start,
        instance.calendar.weeks,
    )

    assert list(options) == list(range(1, 50, 4))
    assert options[1] == "Block A/1 · Jun 29"
    assert options[21] == "Block F/6 · Nov 16"
    assert options[49] == "Block M/13 · May 31"
    assert _academic_block_start_for_week(2, instance.calendar.weeks) == 5
    assert earliest
    assert all(element.__class__.__name__ == "Select" for element in earliest)
    assert any(element.value == 21 for element in earliest)
    assert not any(
        element.__class__.__name__ == "Number"
        and element._props.get("label") == "Earliest start week"
        for element in created
    )


def test_rotation_block_options_use_requested_labels_and_lock_away_clinic() -> None:
    from nicegui import ui

    rotation = sample_instance().rotation("peds_community").model_copy(update={"away": True})
    draft = rotation_editor_state(rotation)
    before = set(ui.context.client.elements)

    _core_settings(draft, on_clinic_availability_change=lambda: None)

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    checkboxes = {
        element._text: element._props
        for element in created
        if element.__class__.__name__ == "Checkbox"
    }
    assert set(checkboxes) >= {"AWAY Rotation", "No clinic hours", "No weekend call"}
    assert "Away from home clinic" not in checkboxes
    assert checkboxes["AWAY Rotation"]["model-value"] is True
    assert checkboxes["No clinic hours"]["model-value"] is True
    assert checkboxes["No clinic hours"]["disable"] is True
    consecutive = next(
        element
        for element in created
        if element.__class__.__name__ == "Select"
        and element._props.get("label") == "Max consecutive weeks"
    )
    assert consecutive._props["model-value"]["label"] == "4 weeks"
    assert [option["label"] for option in consecutive._props["options"]] == [
        "1 week",
        "2 weeks",
        "3 weeks",
        "4 weeks",
        "5 weeks",
        "6 weeks",
    ]
    name = next(
        element
        for element in created
        if element.__class__.__name__ == "Input" and element._props.get("label") == "Rotation name"
    )
    name.set_value("Updated Pediatrics")
    assert draft["name"] == "Updated Pediatrics"
    weekend = next(
        element
        for element in created
        if element.__class__.__name__ == "Checkbox" and element._text == "No weekend call"
    )
    weekend.set_value(True)
    assert draft["no_weekend_call"] is True
    color_buttons = [
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and "rbs-rotation-color-choice" in element._classes
    ]
    assert len(color_buttons) == len(sample_instance().color_scheme.palette)
    assert sum("is-selected" in element._classes for element in color_buttons) == 1
    assert all("schedule color" in element._props["aria-label"] for element in color_buttons)


def test_rotation_color_update_changes_only_the_selected_rotation() -> None:
    instance = sample_instance()
    original = instance.rotation("fmed")

    updated = replace_rotation_color(instance, original.id, "#2B6F8A")

    assert updated.rotation(original.id).color == "#2B6F8A"
    assert updated.rotation(original.id).model_dump(exclude={"color"}) == original.model_dump(
        exclude={"color"}
    )
    assert updated.rotation("elective") == instance.rotation("elective")


def test_mandatory_rotation_code_avatar_uses_the_schedule_color() -> None:
    from nicegui import ui

    rotation = sample_instance().rotation("icu")
    before = set(ui.context.client.elements)

    _rotation_list_item(rotation, rotation.id, lambda _rotation_id: None)

    avatar = next(
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
        and "rbs-rotation-code-avatar" in getattr(element, "_classes", [])
    )
    assert avatar._style["--rbs-rotation-code-color"] == rotation.color


def test_rotation_palette_uses_the_workspace_scheme() -> None:
    from nicegui import ui

    draft = {"color": "#123A67"}
    palette = ("#123A67", "#EAAA00")
    saved: list[str] = []
    before = set(ui.context.client.elements)

    rotation_color_palette(draft, palette, on_change=saved.append)
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    text = {getattr(element, "_text", None) for element in created}
    gold = next(
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and element._props.get("aria-label") == "Select #EAAA00 schedule color"
    )
    next(iter(gold._event_listeners.values())).handler(None)

    assert draft["color"] == "#EAAA00"
    assert saved == ["#EAAA00"]
    assert "Selected: #123A67" not in text
    assert "Select from the institutional palette defined in Settings → Colors." not in text


def test_compact_rotation_palette_accepts_a_custom_hex_color() -> None:
    from nicegui import ui

    draft = {"color": "#123A67"}
    before = set(ui.context.client.elements)

    rotation_color_palette(
        draft,
        ("#123A67", "#EAAA00"),
        compact=True,
        allow_custom=True,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    custom = next(
        element
        for element in created
        if element.__class__.__name__ == "Input" and element._props.get("label") == "Custom hex"
    )
    apply = next(
        element
        for element in created
        if element.__class__.__name__ == "Button" and element._props.get("label") == "Apply"
    )
    status = next(element for element in created if element._props.get("role") == "alert")

    custom.set_value("#123")
    next(iter(apply._event_listeners.values())).handler(None)
    assert draft["color"] == "#123A67"
    assert status._text == "Enter six hex digits, such as #2B6F8A."

    custom.set_value("123abc")
    next(iter(apply._event_listeners.values())).handler(None)

    assert draft["color"] == "#123ABC"


def test_standard_rotation_header_omits_redundant_kind_label() -> None:
    assert _rotation_kind_label(sample_instance().rotation("sports_med")) is None


def test_clinic_overlay_primary_controls_share_one_row() -> None:
    from nicegui import ui

    instance = sample_instance()
    draft = rotation_editor_state(instance.rotation("sports_med"))
    before = set(ui.context.client.elements)

    _clinic_rule_editor(
        draft,
        "clinic",
        enable_label="Schedule continuity clinic",
        show_enable=False,
        academic_half_day=(Weekday.WEDNESDAY, Session.AFTERNOON),
        site_options={site.id: site.name for site in instance.clinic_policy.sites},
        default_site_ids=list(instance.clinic_policy.site_ids),
    )

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    primary = [
        element
        for element in created
        if element._props.get("label") == "Half days per week"
        or element._props.get("label") == "Admin half-days per week"
        or getattr(element, "_text", None)
        in {
            "Concurrent residents need different slots",
            "No academic day attendance",
        }
    ]
    assert len(primary) == 4
    assert len({element.parent_slot.parent.id for element in primary}) == 1


def test_allowed_clinic_half_days_render_as_monday_through_sunday_week() -> None:
    from nicegui import ui

    instance = sample_instance()
    draft = rotation_editor_state(instance.rotation("sports_med"))
    before = set(ui.context.client.elements)

    _clinic_rule_editor(
        draft,
        "clinic",
        enable_label="Schedule continuity clinic",
        show_enable=False,
        academic_half_day=(Weekday.WEDNESDAY, Session.AFTERNOON),
        site_options={site.id: site.name for site in instance.clinic_policy.sites},
        default_site_ids=list(instance.clinic_policy.site_ids),
    )

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    labels = [getattr(element, "_text", None) for element in created]
    button_labels = [
        element._props.get("label") for element in created if element.__class__.__name__ == "Button"
    ]
    day_labels = [
        weekday.value.title() for weekday in (*WEEKDAYS_MF, Weekday.SATURDAY, Weekday.SUNDAY)
    ]
    clinic_grid = next(
        element for element in created if "rbs-clinic-week-grid" in getattr(element, "_classes", [])
    )
    assert {"w-full", "min-w-0", "max-w-full"} <= set(clinic_grid._classes)
    assert all(label in labels for label in day_labels)
    day_positions = [labels.index(label) for label in day_labels]
    assert day_positions == sorted(day_positions), day_labels
    assert labels.count("Morning") == 7
    assert labels.count("Afternoon") == 7
    assert "Add half-day" not in button_labels
    assert "Edit all weekdays" in button_labels
    assert "Edit all days" in button_labels
    slot_edit_buttons = [
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and str(element._props.get("aria-label", "")).startswith("Edit ")
        and str(element._props.get("aria-label", "")).endswith(" clinic slot")
    ]
    assert len(slot_edit_buttons) == 9
    assert labels.count("Maple · Cedar") == 9
    assert not any(
        element.__class__.__name__ == "Select" and element._props.get("label") == "Clinic sites"
        for element in created
    )
    academic_label = next(
        element for element in created if getattr(element, "_text", None) == "Academic Day"
    )
    assert "rbs-academic-day-label" in academic_label._classes
    academic_checkbox = next(
        element
        for element in created
        if element.__class__.__name__ == "Checkbox"
        and getattr(element, "_text", None) == "Afternoon"
        and element._props.get("disable")
    )
    assert academic_checkbox._props.get("model-value") is True
    assert not any(
        "Any weekday expands" in str(getattr(element, "_text", "")) for element in created
    )


def test_bulk_clinic_site_helpers_update_enabled_slots_only() -> None:
    rule = rotation_editor_state(sample_instance().rotation("sports_med"))["clinic"]
    assert rule is not None

    updated = _apply_clinic_sites(
        rule,
        tuple(WEEKDAYS_MF),
        ["maple", "cedar"],
        (Weekday.WEDNESDAY, Session.AFTERNOON),
    )

    assert updated == 9
    assert all(
        slot["sites"] == ["maple", "cedar"]
        for slot in rule["slots"]
        if (slot["weekday"], slot["session"]) != (Weekday.WEDNESDAY.value, Session.AFTERNOON.value)
    )
    academic = next(
        slot
        for slot in rule["slots"]
        if (slot["weekday"], slot["session"]) == (Weekday.WEDNESDAY.value, Session.AFTERNOON.value)
    )
    assert academic["sites"] == [ALL_CLINIC_SITES]
    with pytest.raises(ValueError, match="at least one clinic site"):
        _apply_clinic_sites(rule, tuple(WEEKDAYS_MF), [])


def test_clinic_rule_rejects_enabled_half_day_without_a_site() -> None:
    with pytest.raises(ValidationError, match="at least one site"):
        ClinicRule.model_validate(
            {
                "half_days_per_week": 1,
                "slots": [
                    {
                        "weekday": Weekday.MONDAY.value,
                        "session": Session.MORNING.value,
                        "sites": [],
                    }
                ],
            }
        )


def test_legacy_clinic_wildcard_preserves_specific_preference() -> None:
    rule = ClinicRule.model_validate(
        {
            "half_days_per_week": 1,
            "slots": [
                {
                    "weekday": None,
                    "session": None,
                    "site": None,
                    "preferred": False,
                },
                {
                    "weekday": Weekday.TUESDAY.value,
                    "session": Session.MORNING.value,
                    "site": None,
                    "preferred": True,
                },
            ],
        }
    )

    assert len(rule.slots) == 10
    monday_morning = next(
        slot
        for slot in rule.slots
        if slot.weekday is Weekday.MONDAY and slot.session is Session.MORNING
    )
    tuesday_morning = next(
        slot
        for slot in rule.slots
        if slot.weekday is Weekday.TUESDAY and slot.session is Session.MORNING
    )
    assert monday_morning.sites == [ALL_CLINIC_SITES]
    assert not monday_morning.preferred
    assert tuesday_morning.preferred
    assert any(slot["preferred"] for slot in rule.model_dump(mode="json")["slots"])


def test_clinic_slot_preference_editor_updates_the_typed_draft() -> None:
    rule = rotation_editor_state(sample_instance().rotation("behavioral_health"))["clinic"]
    assert rule is not None

    set_clinic_slot_preferred(
        rule,
        Weekday.TUESDAY,
        Session.MORNING,
        SimpleNamespace(value=True),
    )

    preferred = next(
        slot
        for slot in rule["slots"]
        if (slot["weekday"], slot["session"]) == (Weekday.TUESDAY.value, Session.MORNING.value)
    )
    assert preferred["preferred"] is True


def test_academic_tab_updates_the_system_wide_half_day() -> None:
    from nicegui import ui

    instance = sample_instance().model_copy(update={"academic_half_day_overrides": []})
    updated = replace_academic_half_day(instance, Weekday.THURSDAY, Session.MORNING)
    assert updated.clinic_policy.academic.weekday is Weekday.THURSDAY
    assert updated.clinic_policy.academic.session is Session.MORNING

    before = set(ui.context.client.elements)
    render_rotations_tab(
        instance,
        selected_rotation_id=None,
        on_select=lambda _rotation_id: None,
        on_save=lambda _instance, _rotation_id: None,
        active_section="academic_configuration",
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    tabs = [element for element in created if element.__class__.__name__ == "Tab"]
    assert [tab._props.get("label") for tab in tabs] == [
        "Summary",
        "Mandatory",
        "FMED/Inpatient",
        "Electives",
        "Special",
        "Academic",
    ]
    panels = next(element for element in created if element.__class__.__name__ == "TabPanels")
    assert panels._props.get("model-value") == "academic_configuration"
    selects = [element for element in created if element.__class__.__name__ == "Select"]
    assert sum(element._props.get("label") == "Day" for element in selects) == 2
    assert sum(element._props.get("label") == "Time" for element in selects) == 2
    assert any(element._props.get("label") == "Week" for element in selects)
    assert any(
        element.__class__.__name__ == "Button" and element._props.get("label") == "Save default"
        for element in created
    )
    assert any(
        element.__class__.__name__ == "Button" and element._props.get("label") == "Save override"
        for element in created
    )
    assert any(getattr(element, "_text", None) == "No overrides." for element in created)
    labels = {getattr(element, "_text", None) for element in created}
    assert "Default academic half-day" in labels
    assert "Academic half-day — specific-date overrides" in labels
    assert "Academic Half Day" not in labels
    assert "Set the weekly default and any one-week changes." not in labels
    assert "Changes require a new solve." not in labels


def test_fmed_tab_starts_directly_with_its_rotation_cards() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)

    render_rotations_tab(
        instance,
        selected_rotation_id=None,
        on_select=lambda _rotation_id: None,
        on_save=lambda _instance, _rotation_id: None,
        active_section="fmed_configuration",
    )

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    text = {getattr(element, "_text", None) for element in created}
    assert "FMED/Inpatient" not in text
    assert "Choose its block-schedule color; inpatient rules remain read-only." not in text
    assert "Dedicated FMED configuration" not in text
    assert "Dedicated configuration" not in text
    assert instance.rotation("fmed").name in text
    assert any(
        element.__class__.__name__ == "Button" and element._props.get("label") == "Edit rules"
        for element in created
    )


def test_blank_workspace_fmed_tab_shows_the_default_inpatient_rotation() -> None:
    from nicegui import ui

    instance = blank_instance()
    before = set(ui.context.client.elements)

    render_rotations_tab(
        instance,
        selected_rotation_id=None,
        on_select=lambda _rotation_id: None,
        on_save=lambda _instance, _rotation_id: None,
        active_section="fmed_configuration",
    )

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    text = {getattr(element, "_text", None) for element in created}
    assert instance.rotation("fmed").name in text
    assert "No configuration found" not in text
    assert "Import a constraint catalog containing this rotation type." not in text
    assert any(
        element.__class__.__name__ == "Button" and element._props.get("label") == "Edit rules"
        for element in created
    )


def test_blank_workspace_clinic_tab_shows_the_default_block_rotation() -> None:
    from nicegui import ui

    instance = blank_instance()
    before = set(ui.context.client.elements)

    render_clinic_tab(
        instance,
        on_save=lambda _instance, _rotation_id: None,
        active_section="clinic_block_rules",
    )

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    text = {getattr(element, "_text", None) for element in created}
    assert "No Clinic block rotation is configured." not in text
    assert any(
        element.__class__.__name__ == "Button" and element._props.get("label") == "Edit rules"
        for element in created
    )


def test_fmed_pgy_rule_editor_exposes_required_block_controls() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)

    _open_fmed_pgy_rules_dialog(
        instance,
        "fmed",
        selected_rotation_id=None,
        on_save=lambda _instance, _rotation_id: None,
    )

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    text = {getattr(element, "_text", None) for element in created}
    labels = {element._props.get("label") for element in created}
    tabs = {
        element._props.get("label")
        for element in created
        if element.__class__.__name__ == "Tab"
    }
    block_counts = [
        element.value
        for element in created
        if element.__class__.__name__ == "Number"
        and element._props.get("label") == "Blocks per resident"
    ]

    assert f"Edit FMED rules · {instance.rotation('fmed').name}" in text
    assert {"Available to PGY 1", "Available to PGY 2", "Available to PGY 3"} <= text
    assert "Add block configuration" in labels
    assert "Save rules" in labels
    assert "Maximum residents in clinic at one time" in labels
    assert {
        "Maximum PGY 1 residents in clinic at one time",
        "Maximum PGY 2 residents in clinic at one time",
        "Maximum PGY 3 residents in clinic at one time",
    } <= labels
    assert "Inpatient clinic concurrency" in text
    assert tabs == {"General", "Training-level rules", "Clinic", "Resident overrides"}
    assert sorted(block_counts) == [1, 2, 2, 2]
    assert "Rotation code" not in labels
    assert "Rotation name" not in labels
    tab_bar = next(
        element
        for element in created
        if element.__class__.__name__ == "Tabs"
        and "rbs-fmed-rules-tabs" in getattr(element, "_classes", [])
    )
    assert tab_bar._props.get("inline-label") is True
    dialog_card = next(
        element
        for element in created
        if "rbs-fmed-rules-dialog" in getattr(element, "_classes", [])
    )
    assert dialog_card._style["width"] == "calc(100vw - 48px)"
    assert dialog_card._style["max-width"] == "1600px"
    exception_buttons = {
        element._props.get("label"): element
        for element in created
        if element.__class__.__name__ == "Button"
        and element._props.get("label") in {"Add resident override", "Waive requirement"}
    }
    assert set(exception_buttons) == {"Add resident override", "Waive requirement"}
    assert all(button.enabled for button in exception_buttons.values())


def test_fmed_rules_dialog_owns_the_block_color() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)

    _open_fmed_pgy_rules_dialog(
        instance,
        "fmed",
        selected_rotation_id=None,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    swatches = [
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and "rbs-rotation-color-choice" in element._classes
    ]

    assert len(swatches) == len(instance.color_scheme.palette)
    assert sum("is-selected" in element._classes for element in swatches) == 1


def test_recoloring_fmed_alone_keeps_the_solved_schedule() -> None:
    """Rule edits invalidate a schedule; recoloring blocks does not."""
    from nicegui import ui
    from nicegui.events import ClickEventArguments

    instance = sample_instance()
    saves: list = []
    colors: list = []
    before = set(ui.context.client.elements)

    _open_fmed_pgy_rules_dialog(
        instance,
        "fmed",
        selected_rotation_id=None,
        on_save=lambda updated, rotation_id: saves.append((updated, rotation_id)),
        on_color_save=lambda updated, rotation_id: colors.append((updated, rotation_id)),
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]

    def click(button) -> None:
        for listener in list(button._event_listeners.values()):
            if listener.type == "click":
                listener.handler(ClickEventArguments(sender=button, client=button.client))
                return

    swatches = [
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and "rbs-rotation-color-choice" in element._classes
    ]
    click(swatches[5])
    click(
        next(
            element
            for element in created
            if element.__class__.__name__ == "Button"
            and element._props.get("label") == "Save rules"
        )
    )

    assert not saves
    updated, _rotation_id = colors[-1]
    assert updated.rotation("fmed").color == instance.color_scheme.palette[5]
    # Requirements are untouched; replace_fmed_pgy_rules only reorders them.
    assert sorted(
        (block.rotation_id, block.duration_weeks, block.count)
        for block in updated.curriculum_for(1).blocks
    ) == sorted(
        (block.rotation_id, block.duration_weeks, block.count)
        for block in instance.curriculum_for(1).blocks
    )
    assert updated.rotation("fmed").pgy_rules == instance.rotation("fmed").pgy_rules


def test_rotation_summary_is_the_first_and_default_workspace_tab() -> None:
    from nicegui import ui

    before = set(ui.context.client.elements)
    render_rotations_tab(
        sample_instance(),
        selected_rotation_id=None,
        on_select=lambda _rotation_id: None,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]

    tabs = [element for element in created if element.__class__.__name__ == "Tab"]
    assert tabs[0]._props.get("label") == "Summary"
    panels = next(element for element in created if element.__class__.__name__ == "TabPanels")
    assert panels._props.get("model-value") == "rotation_summary"
    # FMED and Elective block colors are chosen in their pop-out editors, so the
    # tab itself reports the current color rather than offering a palette.
    dedicated_color_buttons = [
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and "rbs-rotation-color-choice" in element._classes
    ]
    assert dedicated_color_buttons == []
    assert {"Edit rules", "Edit shared properties"} <= {
        element._props.get("label") for element in created
    }
    assert (
        sum(getattr(element, "_text", None) == "Schedule color editable" for element in created)
        == 0
    )

    markup = _rotation_summary_html(sample_instance())
    assert "rbs-pgy-label" in markup
    assert "PGY1" in markup
    assert "PGY2" in markup
    assert "Mandatory" in markup
    assert "Missing mandatory" in markup
    assert markup.index("Mandatory") < markup.index("Missing mandatory")
    assert "Time Off (included)" in markup
    assert "✅ 52 weeks" in markup
    assert markup.count('<col class="time">') == 5
    assert all(
        sum(resident_rotation_week_totals(sample_instance(), resident.id).values()) == 52
        for resident in sample_instance().residents
    )


def test_rotation_summary_lists_missing_mandatory_blocks_from_current_schedule() -> None:
    from rbs.models.enums import SolverEngineName, SolverStatus
    from rbs.models.schedule import Assignment, Schedule, ScheduleMeta

    instance = sample_instance()
    resident = instance.residents[0]
    before_count, before_labels = resident_missing_mandatory_rotations(
        instance,
        None,
        resident.id,
    )
    assert before_count == 13
    assert "ICU (4 wk)" in before_labels
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id="icu",
                start_week=20,
                end_week=23,
                weeks=[20, 21, 22, 23],
                block_start_week=20,
                block_duration_weeks=4,
            )
        ],
    )

    count, labels = resident_missing_mandatory_rotations(
        instance,
        schedule,
        resident.id,
    )
    assert count == before_count - 1
    assert "ICU (4 wk)" not in labels
    markup = _rotation_summary_html(instance, schedule=schedule)
    assert f"<strong>{count}</strong>" in markup
    assert 'class="rbs-missing-mandatory-count"' in markup
    assert 'class="rbs-missing-mandatory-list"' in markup
    assert 'class="rbs-missing-mandatory-item"' in markup


def test_rotation_summary_resident_names_link_to_the_resident_editor() -> None:
    markup = _rotation_summary_html(
        sample_instance(),
        resident_edit_url="/",
    )

    assert (
        '<a class="rbs-resident-link" href="/?resident=resident-001" '
        'title="Edit resident Avery Chen" aria-label="Edit resident Avery Chen">'
        "Avery Chen</a>"
    ) in markup


def test_academic_override_row_has_edit_and_delete_actions() -> None:
    from nicegui import ui

    instance = set_academic_half_day_override(
        sample_instance(),
        12,
        Weekday.TUESDAY,
        Session.MORNING,
    )
    before = set(ui.context.client.elements)
    render_rotations_tab(
        instance,
        selected_rotation_id=None,
        on_select=lambda _rotation_id: None,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]

    assert any(element._props.get("aria-label") == "Edit week 12 override" for element in created)
    assert any(element._props.get("aria-label") == "Delete week 12 override" for element in created)
    assert any(getattr(element, "_text", None) == "Tuesday · Morning" for element in created)


def test_academic_screen_offers_a_switch_to_turn_the_half_day_off() -> None:
    from nicegui import ui

    before = set(ui.context.client.elements)
    render_rotations_tab(
        sample_instance(),
        selected_rotation_id=None,
        on_select=lambda _rotation_id: None,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]

    switches = [
        element
        for element in created
        if element.__class__.__name__ == "Switch"
        and getattr(element, "_text", None) == "Program runs a recurring academic half-day"
    ]
    assert len(switches) == 1
    assert switches[0].value is True

    assert any(
        element.__class__.__name__ == "Checkbox"
        and getattr(element, "_text", None) == "No academic half-day this week"
        for element in created
    )


def test_academic_screen_reflects_a_program_that_runs_none() -> None:
    from nicegui import ui

    instance = disable_academic_half_day(
        sample_instance().model_copy(update={"academic_half_day_overrides": []})
    )
    before = set(ui.context.client.elements)
    render_rotations_tab(
        instance,
        selected_rotation_id=None,
        on_select=lambda _rotation_id: None,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]

    switch = next(
        element
        for element in created
        if element.__class__.__name__ == "Switch"
        and getattr(element, "_text", None) == "Program runs a recurring academic half-day"
    )
    assert switch.value is False


def test_a_cancelled_week_renders_as_no_academic_half_day() -> None:
    from nicegui import ui

    instance = cancel_academic_half_day_for_week(sample_instance(), 12)
    before = set(ui.context.client.elements)
    render_rotations_tab(
        instance,
        selected_rotation_id=None,
        on_select=lambda _rotation_id: None,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]

    assert any(element._props.get("aria-label") == "Edit week 12 override" for element in created)
    assert any(getattr(element, "_text", None) == "No academic half-day" for element in created)


def test_academic_override_can_be_added_updated_and_removed_for_one_week() -> None:
    instance = sample_instance()
    seeded = instance.academic_half_day_overrides

    added = set_academic_half_day_override(
        instance,
        12,
        Weekday.TUESDAY,
        Session.MORNING,
    )
    assert len(added.academic_half_day_overrides) == len(seeded) + 1
    assert added.academic_half_day_for_week(12) == (
        Weekday.TUESDAY,
        Session.MORNING,
    )
    assert added.academic_half_day_for_week(11) == (
        Weekday.WEDNESDAY,
        Session.AFTERNOON,
    )

    updated = set_academic_half_day_override(
        added,
        12,
        Weekday.THURSDAY,
        Session.AFTERNOON,
    )
    assert len(updated.academic_half_day_overrides) == len(seeded) + 1
    assert updated.academic_half_day_for_week(12) == (
        Weekday.THURSDAY,
        Session.AFTERNOON,
    )

    removed = remove_academic_half_day_override(updated, 12)
    assert removed.academic_half_day_overrides == seeded
    assert removed.academic_half_day_for_week(12) == (
        Weekday.WEDNESDAY,
        Session.AFTERNOON,
    )


def test_academic_override_requires_a_valid_week_and_different_day() -> None:
    instance = sample_instance()

    with pytest.raises(ValidationError, match="different day"):
        set_academic_half_day_override(
            instance,
            12,
            Weekday.WEDNESDAY,
            Session.MORNING,
        )
    with pytest.raises(ValidationError, match="exceeds calendar"):
        set_academic_half_day_override(
            instance,
            53,
            Weekday.TUESDAY,
            Session.MORNING,
        )


def test_clinic_tab_edits_site_specific_holidays_and_closure_days() -> None:
    from nicegui import ui

    instance = sample_instance()
    updated = replace_clinic_closure_days(
        instance,
        [
            {
                "date": "2026-12-24",
                "name": "Christmas Eve",
                "sites": ["maple"],
            }
        ],
    )
    assert updated.clinic_policy.closure_days[0].name == "Christmas Eve"
    assert updated.clinic_policy.closure_days[0].sites == ["maple"]

    before = set(ui.context.client.elements)
    render_clinic_tab(
        instance,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    assert any(getattr(element, "_text", None) == "Closure days" for element in created)
    assert any(
        element.__class__.__name__ == "Button" and element._props.get("label") == "Add closure day"
        for element in created
    )
    assert not any(
        element.__class__.__name__ == "Select"
        and element._props.get("label") == "Closed clinic sites"
        for element in created
    )


def test_copy_clinic_closure_days_merges_without_replacing_destination_dates() -> None:
    instance = replace_clinic_closure_days(
        sample_instance(),
        [
            {
                "date": "2026-12-25",
                "name": "Christmas",
                "sites": ["maple", "cedar"],
            },
            {
                "date": "2027-01-01",
                "name": "New Year",
                "sites": ["maple"],
            },
            {
                "date": "2027-02-01",
                "name": "Cedar only",
                "sites": ["cedar"],
            },
        ],
    )

    updated = copy_clinic_closure_days(instance, "maple", "cedar")

    assert [
        (closure.date.isoformat(), closure.name)
        for closure in updated.clinic_policy.site("cedar").closure_days
    ] == [
        ("2026-12-25", "Christmas"),
        ("2027-01-01", "New Year"),
        ("2027-02-01", "Cedar only"),
    ]
    assert updated.clinic_policy.site("maple").closure_days == (
        instance.clinic_policy.site("maple").closure_days
    )
    assert updated.clinic_policy.closure_on(date(2027, 1, 1)).sites == ["maple", "cedar"]

    with pytest.raises(ValueError, match="different clinics"):
        copy_clinic_closure_days(instance, "maple", "maple")


def test_clinic_tab_copies_closure_days_between_clinics() -> None:
    from nicegui import ui
    from nicegui.events import ClickEventArguments

    instance = replace_clinic_closure_days(
        sample_instance(),
        [
            {
                "date": "2026-12-25",
                "name": "Christmas",
                "sites": ["maple", "cedar"],
            },
            {
                "date": "2027-01-01",
                "name": "New Year",
                "sites": ["maple"],
            },
        ],
    )
    saved: list = []
    before = set(ui.context.client.elements)
    render_clinic_tab(
        instance,
        on_save=lambda updated, rotation_id: saved.append((updated, rotation_id)),
    )

    def click(button) -> None:
        listener = next(
            listener
            for listener in button._event_listeners.values()
            if listener.type == "click"
        )
        listener.handler(ClickEventArguments(sender=button, client=button.client))

    copy_button = next(
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
        and element.__class__.__name__ == "Button"
        and element._props.get("label") == "Copy closure days"
    )
    assert copy_button.enabled

    dialog_before = set(ui.context.client.elements)
    click(copy_button)
    dialog_elements = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in dialog_before
    ]
    source = next(
        element
        for element in dialog_elements
        if element.__class__.__name__ == "Select"
        and element._props.get("label") == "Source clinic"
    )
    destination = next(
        element
        for element in dialog_elements
        if element.__class__.__name__ == "Select"
        and element._props.get("label") == "Destination clinic"
    )
    source.set_value("maple")
    destination.set_value("cedar")
    click(
        next(
            element
            for element in dialog_elements
            if element.__class__.__name__ == "Button"
            and element._props.get("label") == "Copy closure days"
        )
    )

    updated, selected_rotation_id = saved[-1]
    assert selected_rotation_id is None
    assert [
        closure.date.isoformat()
        for closure in updated.clinic_policy.site("cedar").closure_days
    ] == ["2026-12-25", "2027-01-01"]


def test_clinic_crud_round_trips_weekend_capacity_color_and_closures() -> None:
    instance = sample_instance()
    added = add_clinic(
        instance,
        {
            "id": "weekend",
            "code": "WKND",
            "name": "Weekend Clinic",
            "color": "#123ABC",
            "residents_per_attending": 3,
            "half_days": [
                {
                    "weekday": "saturday",
                    "session": "morning",
                    "attendings": 2,
                    "min_residents": 1,
                }
            ],
            "capacity_overrides": [
                {
                    "date": "2026-08-02",
                    "session": "afternoon",
                    "attendings": 3,
                    "min_residents": 2,
                }
            ],
            "closure_days": [{"date": "2026-08-01", "name": "Weekend closure"}],
        },
    )
    weekend = added.clinic_policy.site("weekend")
    assert weekend.color == "#123ABC"
    assert weekend.max_capacity(Weekday.SATURDAY, Session.MORNING) == 6
    assert (
        weekend.max_capacity_on(
            date(2026, 8, 2),
            Session.AFTERNOON,
        )
        == 9
    )
    assert (
        weekend.min_capacity_on(
            date(2026, 8, 2),
            Session.AFTERNOON,
        )
        == 2
    )
    assert weekend.closure_days[0].name == "Weekend closure"

    draft = weekend.model_dump(mode="json")
    draft["name"] = "Extended Hours Clinic"
    draft["color"] = "#AABBCC"
    edited = replace_clinic(added, "weekend", draft)
    assert edited.clinic_policy.site("weekend").name == "Extended Hours Clinic"
    assert edited.clinic_policy.site("weekend").color == "#AABBCC"

    removed = remove_clinic(edited, "weekend")
    assert "weekend" not in removed.clinic_policy.site_ids
    assert {rule.clinic_id for rule in removed.clinic_policy.allocation_rules} == {
        "maple",
        "cedar",
    }


def test_clinic_editor_is_large_and_keeps_internal_id_hidden() -> None:
    from nicegui import ui

    instance = sample_instance()
    maple = instance.clinic_policy.site("maple").model_dump(mode="json")
    maple["capacity_overrides"] = [
        {
            "date": "2026-08-04",
            "session": "morning",
            "attendings": 2,
            "min_residents": 1,
        }
    ]
    instance = replace_clinic(instance, "maple", maple)
    before = set(ui.context.client.elements)
    _open_clinic_editor_dialog(
        instance,
        original_id="maple",
        selected_rotation_id=None,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    inputs = [element for element in created if element.__class__.__name__ == "Input"]
    assert not any(element._props.get("label") == "Clinic ID" for element in inputs)
    clinic_name = next(
        element for element in inputs if element._props.get("label") == "Clinic name"
    )
    assert clinic_name.value == "Maple"
    assert not any(element._props.get("label") == "Clinic code" for element in inputs)
    clinic_editor_tabs = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Tab"
    }
    assert {"Details", "Allocation", "Weekly Capacity", "Exceptions"} <= (clinic_editor_tabs)
    assert any(getattr(element, "_text", None) == "Edit Clinic · Maple" for element in created)
    tab_bar = next(
        element
        for element in created
        if element.__class__.__name__ == "Tabs"
        and "rbs-clinic-editor-tabs" in getattr(element, "_classes", [])
    )
    assert tab_bar._props.get("inline-label") is True
    assert not any(
        element
        for element in created
        if element.__class__.__name__ == "Select"
        and element._props.get("label") == "Schedule color"
    )
    color_choices = [
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and "rbs-rotation-color-choice" in element._classes
    ]
    current_swatch = next(
        element
        for element in created
        if "rbs-rotation-color-swatch-large" in getattr(element, "_classes", [])
    )
    custom_color = next(
        element
        for element in created
        if element.__class__.__name__ == "Input" and element._props.get("label") == "Custom hex"
    )
    assert len(color_choices) == len(instance.color_scheme.palette)
    assert current_swatch._style["--rbs-rotation-choice-color"] == "#6D6BC2"
    assert custom_color.value == "#6D6BC2"
    assert any(getattr(element, "_text", None) == "Saturday" for element in created)
    assert any(getattr(element, "_text", None) == "Sunday" for element in created)
    assert any(
        getattr(element, "_text", None) == "Specific-day capacity overrides" for element in created
    )
    assert any(
        element.__class__.__name__ == "Button"
        and element._props.get("label") == "Add capacity override"
        for element in created
    )
    assert any(element._props.get("label") == "Override date" for element in inputs)
    assert any(getattr(element, "_text", None) == "Overall rule" for element in created)
    assert any(
        element.__class__.__name__ == "Button"
        and element._props.get("label") == "Add training-level override"
        for element in created
    )
    assert any(
        element.__class__.__name__ == "Button"
        and element._props.get("label") == "Add resident override"
        for element in created
    )
    dialog_card = next(
        element
        for element in created
        if "rbs-clinic-editor-dialog" in getattr(element, "_classes", [])
    )
    assert dialog_card._style["width"] == "calc(100vw - 48px)"


def test_add_closure_day_opens_the_clinic_editor_on_exceptions() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)
    _open_clinic_editor_dialog(
        instance,
        original_id="maple",
        selected_rotation_id=None,
        on_save=lambda _instance, _rotation_id: None,
    )
    default_panels = next(
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
        and element.__class__.__name__ == "TabPanels"
        and "rbs-clinic-editor-panels" in getattr(element, "_classes", [])
    )
    assert default_panels._props.get("model-value") == "clinic_details"

    tab_before = set(ui.context.client.elements)
    render_clinic_tab(
        instance,
        on_save=lambda _instance, _rotation_id: None,
    )
    add_closure = next(
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in tab_before
        and element.__class__.__name__ == "Button"
        and element._props.get("label") == "Add closure day"
    )
    handlers = [
        listener.handler
        for listener in add_closure._event_listeners.values()
        if listener.type == "click"
    ]
    assert len(handlers) == 1

    dialog_before = set(ui.context.client.elements)
    handlers[0](None)
    panels = next(
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in dialog_before
        and element.__class__.__name__ == "TabPanels"
        and "rbs-clinic-editor-panels" in getattr(element, "_classes", [])
    )
    assert panels._props.get("model-value") == "clinic_exceptions"


def test_clinic_tab_omits_redundant_title_block_and_rotation_subtitle() -> None:
    from nicegui import ui

    before = set(ui.context.client.elements)
    render_clinic_tab(
        sample_instance(),
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    text = {getattr(element, "_text", None) for element in created}
    assert (
        "View rotation rules, edit host-service configurations, and review system-wide "
        "clinic policy."
    ) not in text
    assert "Manage clinic locations, allocation rules, staffing capacity, and closures." not in text
    assert "Clinic rotation read only" not in text
    assert "Dedicated Clinic configuration" not in text
    assert not any(
        isinstance(label, str) and "total ·" in label and "standard" in label for label in text
    )


def test_manual_clinic_block_replaces_elective_and_round_trips() -> None:
    instance = sample_instance()

    updated = add_manual_clinic_block(
        instance,
        {
            "resident_id": "resident-001",
            "rotation_id": "clinic",
            "start_week": 1,
            "duration_weeks": 2,
            "replaces_rotation_id": "elective",
        },
    )

    assert len(updated.manual_clinic_blocks) == 1
    manual = updated.manual_clinic_blocks[0]
    assert manual.rotation_id == "clinic"
    assert manual.replaces_rotation_id == "elective"
    assert updated.scheduling_case().manual_clinic_blocks == [manual]
    assert remove_manual_clinic_block(updated, 0).manual_clinic_blocks == []


def test_manual_clinic_block_can_use_resident_unallocated_time() -> None:
    freed, _rotation = _free_night_float_pgy1_weeks(sample_instance())
    before = resident_rotation_week_totals(freed, "resident-001")

    updated = add_manual_clinic_block(
        freed,
        {
            "resident_id": "resident-001",
            "rotation_id": "clinic",
            "start_week": 1,
            "duration_weeks": 2,
            "replaces_rotation_id": None,
        },
    )

    manual = updated.manual_clinic_blocks[0]
    assert manual.replaces_rotation_id is None
    assert updated.resident_unallocated_weeks("resident-001") == 0
    after = resident_rotation_week_totals(updated, "resident-001")
    assert after["clinic"] == before["clinic"] + 2
    assert after["elective"] == before["elective"]
    occurrence = next(
        item
        for item in expand_occurrences(updated, require_configured_electives=False)
        if item.resident_id == "resident-001" and "manual-clinic" in item.key
    )
    assert occurrence.fixed_start_week == 1

    with pytest.raises(ValidationError, match="resident additions use 4 unallocated weeks"):
        add_manual_clinic_block(
            updated,
            {
                "resident_id": "resident-001",
                "rotation_id": "clinic",
                "start_week": 3,
                "duration_weeks": 2,
                "replaces_rotation_id": None,
            },
        )


def test_clinic_resident_exemption_frees_time_for_a_manual_block() -> None:
    instance = sample_instance()
    before = resident_rotation_week_totals(instance, "resident-001")

    exempted = add_resident_rotation_waiver(
        instance,
        {
            "resident_id": "resident-001",
            "rotation_id": "clinic",
            "duration_weeks": 2,
        },
    )

    assert exempted.resident_unallocated_weeks("resident-001") == 2
    exempted_totals = resident_rotation_week_totals(exempted, "resident-001")
    assert exempted_totals["clinic"] == before["clinic"] - 2

    repositioned = add_manual_clinic_block(
        exempted,
        {
            "resident_id": "resident-001",
            "rotation_id": "clinic",
            "start_week": 1,
            "duration_weeks": 2,
            "replaces_rotation_id": None,
        },
    )
    assert repositioned.resident_unallocated_weeks("resident-001") == 0
    assert resident_rotation_week_totals(repositioned, "resident-001")["clinic"] == before[
        "clinic"
    ]

    with pytest.raises(ValidationError, match="only 0 are available"):
        remove_resident_rotation_waiver(repositioned, 0)

    without_block = remove_manual_clinic_block(repositioned, 0)
    restored = remove_resident_rotation_waiver(without_block, 0)
    assert restored.resident_rotation_waivers == []
    assert restored.resident_unallocated_weeks("resident-001") == 0


def test_standalone_clinic_tab_uses_tabs_and_structured_site_cards() -> None:
    from nicegui import ui

    before = set(ui.context.client.elements)
    render_clinic_tab(
        sample_instance(),
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    labels = {getattr(element, "_text", None) for element in created}
    tabs = [
        element._props.get("label") for element in created if element.__class__.__name__ == "Tab"
    ]
    panels = next(element for element in created if element.__class__.__name__ == "TabPanels")
    clinic_page = next(
        element
        for element in created
        if "rbs-configuration-page" in getattr(element, "_classes", [])
    )
    site_grid = next(
        element
        for element in created
        if "rbs-clinic-sites-grid" in getattr(element, "_classes", [])
    )
    site_cards = [
        element
        for element in created
        if "rbs-clinic-config-card" in getattr(element, "_classes", [])
    ]
    add_clinic = next(
        element
        for element in created
        if element.__class__.__name__ == "Button" and element._props.get("label") == "Add clinic"
    )

    assert tabs == ["Clinics", "Block rules", "Resident exceptions (0)"]
    assert panels._props.get("model-value") == "clinic_sites"
    assert {"max-w-7xl", "mx-auto"} <= set(clinic_page._classes)
    assert site_grid is not None
    assert len(site_cards) == len(sample_instance().clinic_policy.sites)
    assert {card._style.get("--rbs-clinic-color") for card in site_cards} == {
        site.color for site in sample_instance().clinic_policy.sites
    }
    assert {"Clinic", "Clinic sites", "Clinic block rules", "Resident Clinic exceptions"} <= labels
    assert {"Target", "Weekly sessions", "Max residents", "Exceptions", "Primary"} <= labels
    assert add_clinic._props.get("unelevated") is True
    assert "Constrained by clinic capacity only" in labels
    assert "No minimum or maximum" not in labels
    assert "Dedicated Clinic configuration" not in labels
    assert any(
        element.__class__.__name__ == "Button"
        and element._props.get("label") == "Schedule clinic block"
        for element in created
    )
    assert any(
        element.__class__.__name__ == "Button"
        and element._props.get("label") == "Exempt resident"
        for element in created
    )


def test_clinic_tab_restores_the_selected_subsection() -> None:
    from nicegui import ui

    before = set(ui.context.client.elements)
    render_clinic_tab(
        sample_instance(),
        on_save=lambda _instance, _rotation_id: None,
        active_section="clinic_block_rules",
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    panels = next(element for element in created if element.__class__.__name__ == "TabPanels")

    assert panels._props.get("model-value") == "clinic_block_rules"


def test_clinic_block_rules_dialog_uses_clinic_name_and_compact_pgy_controls() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)
    _open_clinic_block_rules_dialog(
        instance,
        "clinic",
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    text = {getattr(element, "_text", None) for element in created}
    assert "Edit Clinic block rules · Clinic" in text
    assert "Clinic block schedule color" in text
    assert sum(
        element.__class__.__name__ == "Button" and "rbs-rotation-color-choice" in element._classes
        for element in created
    ) == len(instance.color_scheme.palette)
    assert "Clinic required for PGY 1" in text
    assert "4 Clinic weeks per resident" not in text
    assert "Count changes automatically add or remove Elective blocks." not in text
    assert not any(
        isinstance(label, str) and label.startswith("Set eligibility, concurrent staffing")
        for label in text
    )
    assert "Required blocks" in text
    advanced = [
        element
        for element in created
        if element.__class__.__name__ == "Expansion"
        and element._props.get("label") == "Advanced scheduling limits"
    ]
    assert len(advanced) == len(instance.rotation("clinic").pgy_rules)
    assert all(element.value is False for element in advanced)
    assert "Block configurations" not in text
    assert "Add block duration" in {element._props.get("label") for element in created}
    count_position = next(
        index
        for index, element in enumerate(created)
        if element._props.get("label") == "Blocks per resident"
    )
    staffing_position = next(
        index
        for index, element in enumerate(created)
        if element._props.get("label") == "Minimum residents each week"
    )
    assert count_position < staffing_position
    assert any(
        isinstance(label, str)
        and "A minimum applies in every academic week" in label
        for label in text
    )
    assert all(
        "Constrained by clinic capacity only" in str(element._props.get("caption") or "")
        for element in advanced
    )
    assert not any(
        "No minimum or maximum" in str(element._props.get("caption") or "") for element in advanced
    )
    admin = next(
        element
        for element in created
        if element._props.get("label") == "Admin half-days per Clinic week"
    )
    assert "rbs-admin-half-days-field" in admin._classes
    maximums = [
        element for element in created if element._props.get("label") == "Maximum vacation weeks"
    ]
    assert maximums
    assert all(element._props.get("clearable") is not True for element in maximums)
    assert all(element.value == 1 for element in maximums)
    earliest = [
        element for element in created if element._props.get("label") == "Earliest start block"
    ]
    assert len(earliest) == len(instance.rotation("clinic").pgy_rules)
    assert all(element.__class__.__name__ == "Select" for element in earliest)
    assert not any(element._props.get("label") == "Earliest start week" for element in created)


def test_manual_clinic_dialog_prefers_unallocated_time() -> None:
    from nicegui import ui

    freed, _rotation = _free_night_float_pgy1_weeks(sample_instance())
    before = set(ui.context.client.elements)
    _open_manual_clinic_block_dialog(
        freed,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    labels = {element._props.get("label") for element in created}
    text = {getattr(element, "_text", None) for element in created}
    funding = next(
        element
        for element in created
        if element.__class__.__name__ == "Select" and element._props.get("label") == "Funded by"
    )
    resident = next(
        element
        for element in created
        if element.__class__.__name__ == "Select" and element._props.get("label") == "Resident"
    )
    assert "Elective block replaced" not in labels
    resident.set_value("resident-001")
    assert funding.value == "unallocated"
    assert funding._props["options"][0]["label"].startswith("Unallocated time")
    assert any(
        isinstance(label, str) and "uses the resident's unallocated time" in label for label in text
    )

    options = _manual_funding_options(freed, "resident-001", 2)
    assert next(iter(options)) == "unallocated"
    assert "elective" in options

    unallocated_only = set_elective_allocation(freed, 1, {})
    assert _manual_funding_options(unallocated_only, "resident-001", 2) == {
        "unallocated": "Unallocated time (4 weeks available)"
    }
    assert "resident-001" in _manual_clinic_resident_options(unallocated_only)


def test_clinic_resident_exemption_is_available_on_manual_blocks_screen() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)
    _open_manual_clinic_waiver_dialog(
        instance,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    text = {getattr(element, "_text", None) for element in created}
    labels = {
        element._props.get("label")
        for element in created
        if element.__class__.__name__ == "Select"
    }

    assert "Exempt resident from Clinic" in text
    assert {"Resident", "Clinic requirement", "Block length"} <= labels
    assert any(
        isinstance(label, str) and "freed weeks become that resident's unallocated time" in label
        for label in text
    )


def test_rotation_editor_state_round_trips_every_nested_configuration() -> None:
    instance = sample_instance()
    original = instance.rotation("outpatient_gyn")

    draft = rotation_editor_state(original)
    draft["code"] = "GYN-OB"
    draft["name"] = "GYN and Obstetrics"
    draft["pgy_rules"][0]["block_configs"][0]["vacation"]["allowed"] = True
    draft["pgy_rules"][0]["min_concurrent"] = 0
    draft["pgy_rules"][0]["max_total_weeks"] = 6
    draft["pgy_rules"][0]["prerequisite_rotation_ids"] = ["fmed"]
    draft["pgy_rules"][0]["earliest_start_week"] = 24
    draft["clinic"]["slots"][0]["sites"] = ["maple"]
    draft["clinic"]["slots"][0]["preferred"] = True
    draft["clinic"]["no_academic_day_attendance"] = True
    draft["no_weekend_call"] = True

    replacement = rotation_from_editor_state(draft)
    updated = replace_standard_rotation(instance, original.id, replacement)
    saved = updated.rotation(original.id)

    assert saved.code == "GYN-OB"
    assert saved.name == "GYN and Obstetrics"
    assert saved.block_config(1, 2).vacation.allowed
    assert saved.pgy_rule(1).min_concurrent == 0
    assert saved.pgy_rule(1).max_total_weeks == 6
    assert saved.pgy_rule(1).prerequisite_rotation_ids == ["fmed"]
    assert saved.pgy_rule(1).earliest_start_week == 24
    assert saved.clinic is not None
    assert saved.clinic.slots[0].sites == ["maple"]
    assert saved.clinic.slots[0].preferred
    assert saved.clinic.no_academic_day_attendance
    assert updated.rotation_group_for(1, saved.id) is not None
    assert saved.no_weekend_call
    assert "notes" not in draft
    assert "notes" not in saved.model_dump(mode="json")
    assert instance.rotation(original.id).name == "Outpatient GYN"


def test_standard_rotation_editor_can_clear_level_specific_grouping() -> None:
    instance = sample_instance()
    rotation = instance.rotation("outpatient_gyn")

    assert rotation_group_members_by_pgy(instance, rotation.id) == {
        1: ["outpatient_gyn", "inpatient_ld"],
        2: ["outpatient_gyn", "inpatient_ld"],
        3: [],
    }

    updated = replace_standard_rotation(
        instance,
        rotation.id,
        rotation,
        group_members_by_pgy={1: [], 2: [], 3: []},
    )

    assert updated.rotation_group_for(1, rotation.id) is None
    assert updated.rotation_group_for(2, rotation.id) is None


def test_rotation_editor_normalizes_code_to_uppercase() -> None:
    original = sample_instance().rotation("outpatient_gyn")
    draft = rotation_editor_state(original)
    draft["code"] = "gyn ob"

    assert rotation_from_editor_state(draft).code == "GYN OB"


def test_rotation_editor_away_flag_forces_no_clinic_hours() -> None:
    original = sample_instance().rotation("sports_med")
    draft = rotation_editor_state(original)
    draft["away"] = True
    draft["no_clinic_hours"] = False

    replacement = rotation_from_editor_state(draft)

    assert replacement.away
    assert replacement.no_clinic_hours
    assert replacement.clinic is not None


def test_unchecking_away_also_enables_clinic_hours() -> None:
    draft = rotation_editor_state(sample_instance().rotation("peds_community"))

    _apply_away_selection(draft, False)

    assert draft["away"] is False
    assert draft["no_clinic_hours"] is False
    assert draft["clinic"] is not None


def test_replacing_rotation_validates_curriculum_duration_references() -> None:
    instance = sample_instance()
    original = instance.rotation("icu")
    raw = original.model_dump(mode="json")
    raw["pgy_rules"][0]["block_configs"][0]["duration_weeks"] = 3
    replacement = Rotation.model_validate(raw)

    with pytest.raises(ValidationError, match="duration 4 not in"):
        replace_standard_rotation(instance, original.id, replacement)


@pytest.mark.parametrize("rotation_id", ["clinic", "fmed", "elective"])
def test_generic_editor_rejects_special_rotations(rotation_id: str) -> None:
    instance = sample_instance()
    special = instance.rotation(rotation_id)

    with pytest.raises(ValueError, match="dedicated configuration section"):
        replace_standard_rotation(instance, rotation_id, special)


def test_generic_editor_keeps_rotation_id_and_kind_immutable() -> None:
    instance = sample_instance()
    original = instance.rotation("icu")
    renamed_id = original.model_copy(update={"id": "icu-new"})
    special_kind = original.model_copy(update={"kind": RotationKind.CLINIC})

    with pytest.raises(ValueError, match="system key"):
        replace_standard_rotation(instance, original.id, renamed_id)
    with pytest.raises(ValueError, match="cannot be changed into"):
        replace_standard_rotation(instance, original.id, special_kind)


def test_clinic_count_changes_spend_and_release_unscheduled_weeks() -> None:
    instance = sample_instance()
    clinic = instance.rotation("clinic")
    counts = {
        (pgy, config.duration_weeks): sum(
            block.count
            for block in instance.curriculum_for(pgy).blocks
            if block.rotation_id == clinic.id and block.duration_weeks == config.duration_weeks
        )
        for pgy in range(1, 4)
        for rule in clinic.pgy_rules
        if rule.pgy == pgy
        for config in rule.block_configs
    }
    counts[1, 2] += 1

    increased = replace_clinic_block_rules(instance, clinic.id, clinic, counts)

    pgy1 = increased.curriculum_for(1)
    assert pgy1.required_weeks() == 52
    assert increased.unallocated_weeks(1) == 0
    assert (
        sum(
            block.count
            for block in pgy1.blocks
            if block.rotation_id == "clinic" and block.duration_weeks == 2
        )
        == 3
    )
    assert not any(block.rotation_id == "elective" for block in pgy1.blocks)

    counts[1, 2] -= 1
    restored = replace_clinic_block_rules(increased, clinic.id, clinic, counts)
    assert restored.curriculum_for(1).required_weeks() == 50
    assert restored.unallocated_weeks(1) == 2
    assert not any(block.rotation_id == "elective" for block in restored.curriculum_for(1).blocks)


def _clinic_counts(instance, rotation_id: str) -> dict:
    rotation = instance.rotation(rotation_id)
    return {
        (rule.pgy, config.duration_weeks): sum(
            block.count
            for block in instance.curriculum_for(rule.pgy).blocks
            if block.rotation_id == rotation_id and block.duration_weeks == config.duration_weeks
        )
        for rule in rotation.pgy_rules
        for config in rule.block_configs
    }


def test_clinic_rule_removal_releases_orphaned_locks() -> None:
    instance = sample_instance()
    clinic = instance.rotation("clinic")
    assert any(
        lock.resident_id == "resident-001" and lock.rotation_id == "clinic"
        for lock in instance.locks
    )
    raw = clinic.model_dump(mode="json")
    raw["pgy_rules"] = [rule for rule in raw["pgy_rules"] if rule["pgy"] != 1]
    replacement = Rotation.model_validate(raw)
    counts = _clinic_counts(instance, clinic.id)

    updated = replace_clinic_block_rules(instance, clinic.id, replacement, counts)

    assert [rule.pgy for rule in updated.rotation("clinic").pgy_rules] == [2, 3]
    assert not any(block.rotation_id == "clinic" for block in updated.curriculum_for(1).blocks)
    assert not any(lock.rotation_id == "clinic" for lock in updated.locks)
    assert any(lock.rotation_id == "fmed" for lock in updated.locks)


def test_clinic_zeroed_blocks_release_unsatisfiable_lock() -> None:
    instance = sample_instance()
    clinic = instance.rotation("clinic")
    counts = _clinic_counts(instance, clinic.id)
    for key in [key for key in counts if key[0] == 1]:
        counts[key] = 0

    updated = replace_clinic_block_rules(instance, clinic.id, clinic, counts)

    assert [rule.pgy for rule in updated.rotation("clinic").pgy_rules] == [1, 2, 3]
    assert not any(block.rotation_id == "clinic" for block in updated.curriculum_for(1).blocks)
    assert not any(lock.rotation_id == "clinic" for lock in updated.locks)
    assert any(lock.rotation_id == "fmed" for lock in updated.locks)


def test_clinic_reduction_keeps_satisfiable_lock() -> None:
    instance = sample_instance()
    clinic = instance.rotation("clinic")
    counts = _clinic_counts(instance, clinic.id)
    assert counts[1, 2] == 2
    counts[1, 2] = 1

    updated = replace_clinic_block_rules(instance, clinic.id, clinic, counts)

    assert any(
        lock.resident_id == "resident-001" and lock.rotation_id == "clinic"
        for lock in updated.locks
    )


def test_fmed_pgy_rule_changes_spend_weeks_and_preserve_service_identity() -> None:
    instance = sample_instance()
    original = instance.rotation("fmed")
    raw = original.model_dump(mode="json")
    raw["code"] = "OTHER"
    raw["name"] = "Changed outside the FMED editor"
    raw["color"] = "#000000"
    raw["no_weekend_call"] = not original.no_weekend_call
    raw["capacity"]["max_concurrent"] = 4
    raw["clinic"]["max_concurrent"] = 2
    raw["clinic"]["max_concurrent_by_pgy"] = {"1": 1}
    raw["pgy_rules"][0]["earliest_start_week"] = 2
    raw["pgy_rules"][0]["block_configs"].append(
        {
            "duration_weeks": 2,
            "vacation": {"allowed": True, "max_weeks_per_block": 1},
        }
    )
    replacement = Rotation.model_validate(raw)
    counts = {
        (pgy, config.duration_weeks): sum(
            block.count
            for block in instance.curriculum_for(pgy).blocks
            if block.rotation_id == original.id and block.duration_weeks == config.duration_weeks
        )
        for pgy in range(1, 4)
        for rule in replacement.pgy_rules
        if rule.pgy == pgy
        for config in rule.block_configs
    }
    counts[1, 2] = 1

    updated = replace_fmed_pgy_rules(
        instance,
        original.id,
        replacement,
        counts,
    )
    saved = updated.rotation(original.id)

    assert updated.curriculum_for(1).required_weeks() == 52
    assert updated.unallocated_weeks(1) == 0
    assert saved.allows_duration(2, pgy=1)
    assert saved.block_config(1, 2).vacation.allowed
    assert saved.pgy_rule(1).earliest_start_week == 2
    assert saved.capacity.max_concurrent == 4
    assert saved.code == original.code
    assert saved.name == original.name
    assert saved.color == original.color
    assert saved.clinic is not None
    assert original.clinic is not None
    assert saved.clinic.max_concurrent == 2
    assert saved.clinic.max_concurrent_by_pgy == {1: 1}
    assert saved.clinic.slots == original.clinic.slots
    assert saved.clinic.half_days_per_week == original.clinic.half_days_per_week
    assert saved.no_weekend_call == original.no_weekend_call
    assert not any(block.rotation_id == "elective" for block in updated.curriculum_for(1).blocks)

    counts[1, 2] = 0
    restored = replace_fmed_pgy_rules(
        updated,
        original.id,
        saved,
        counts,
    )
    assert restored.curriculum_for(1).required_weeks() == 50
    assert restored.unallocated_weeks(1) == 2
    assert not any(block.rotation_id == "elective" for block in restored.curriculum_for(1).blocks)


def test_fmed_day_edits_save_through_dedicated_editor() -> None:
    instance = sample_instance()
    original = instance.rotation("fmed")
    assert original.clinic is not None
    raw = original.model_dump(mode="json")
    raw["clinic"]["slots"] = [
        slot for slot in raw["clinic"]["slots"] if slot["weekday"] != "friday"
    ]
    assert len(raw["clinic"]["slots"]) < len(original.clinic.slots)
    replacement = Rotation.model_validate(raw)
    counts = {
        (pgy, config.duration_weeks): sum(
            block.count
            for block in instance.curriculum_for(pgy).blocks
            if block.rotation_id == original.id and block.duration_weeks == config.duration_weeks
        )
        for pgy in range(1, 4)
        for rule in replacement.pgy_rules
        if rule.pgy == pgy
        for config in rule.block_configs
    }

    updated = replace_fmed_pgy_rules(instance, original.id, replacement, counts)
    saved = updated.rotation(original.id)

    assert saved.clinic is not None
    assert all(slot.weekday.value != "friday" for slot in saved.clinic.slots)
    assert len(saved.clinic.slots) == len(raw["clinic"]["slots"])
    assert saved.clinic.max_concurrent == original.clinic.max_concurrent
    assert saved.clinic.half_days_per_week == original.clinic.half_days_per_week
    assert any(
        lock.resident_id == "resident-009" and lock.rotation_id == "fmed" for lock in updated.locks
    )


def test_fmed_rules_save_named_resident_additions_and_exemptions() -> None:
    instance = set_elective_allocation(sample_instance(), 2, {2: 3})
    assert instance.unallocated_weeks(2) == 2
    rotation = instance.rotation("fmed")
    counts = {
        (rule.pgy, config.duration_weeks): sum(
            block.count
            for block in instance.curriculum_for(rule.pgy).blocks
            if block.rotation_id == rotation.id
            and block.duration_weeks == config.duration_weeks
        )
        for rule in rotation.pgy_rules
        for config in rule.block_configs
    }

    updated = replace_fmed_pgy_rules(
        instance,
        rotation.id,
        rotation,
        counts,
        resident_overrides=[
            {
                "resident_id": "resident-009",
                "rotation_id": rotation.id,
                "duration_weeks": 2,
                "replaces_rotation_id": None,
            }
        ],
        resident_waivers=[
            {
                "resident_id": "resident-009",
                "rotation_id": rotation.id,
                "duration_weeks": 4,
            }
        ],
    )

    assert [override.resident_id for override in updated.resident_rotation_overrides] == [
        "resident-009"
    ]
    assert updated.resident_rotation_overrides[0].replaces_rotation_id is None
    assert updated.resident_unallocated_weeks("resident-009") == 4
    assert [waiver.resident_id for waiver in updated.resident_rotation_waivers] == [
        "resident-009"
    ]
    resident_fmed = [
        occurrence
        for occurrence in expand_occurrences(updated, require_configured_electives=False)
        if occurrence.resident_id == "resident-009"
        and occurrence.rotation_id == rotation.id
        and not occurrence.elective
    ]
    assert sum(item.duration_weeks == 2 for item in resident_fmed) == 2
    assert sum(item.duration_weeks == 4 for item in resident_fmed) == 1


def test_fmed_rules_dialog_shows_eligible_clinic_days() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)

    _open_fmed_pgy_rules_dialog(
        instance,
        "fmed",
        selected_rotation_id=None,
        on_save=lambda _instance, _rotation_id: None,
    )

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    text = {getattr(element, "_text", None) for element in created}
    day_boxes = [
        element
        for element in created
        if element.__class__.__name__ == "Checkbox"
        and getattr(element, "_text", None) in {"Morning", "Afternoon"}
    ]

    assert "Eligible clinic days" in text
    assert day_boxes
    assert any(box.value is True for box in day_boxes)
    assert any(box.value is False for box in day_boxes)


def test_manual_clinic_block_rejects_non_elective_replacement() -> None:
    with pytest.raises(ValueError, match="must replace Elective"):
        add_manual_clinic_block(
            sample_instance(),
            {
                "resident_id": "resident-001",
                "rotation_id": "clinic",
                "start_week": 1,
                "duration_weeks": 2,
                "replaces_rotation_id": "night_float",
            },
        )


def test_resident_mandatory_override_replaces_elective_and_updates_summary() -> None:
    instance = sample_instance()
    rotation = instance.rotation("night_float")
    before = resident_rotation_week_totals(instance, "resident-001")

    updated = replace_standard_rotation(
        instance,
        rotation.id,
        rotation,
        resident_overrides=[
            {
                "resident_id": "resident-001",
                "rotation_id": rotation.id,
                "duration_weeks": 2,
                "replaces_rotation_id": "elective",
            }
        ],
    )

    override = updated.resident_rotation_overrides[0]
    assert override.rotation_id == "night_float"
    assert override.replaces_rotation_id == "elective"
    assert updated.scheduling_case().resident_rotation_overrides == [override]
    after = resident_rotation_week_totals(updated, "resident-001")
    assert after["mandatory"] == before["mandatory"] + 2
    assert after["elective"] == before["elective"] - 2


def _free_night_float_pgy1_weeks(instance):
    rotation = instance.rotation("night_float")
    freed = replace_standard_rotation(
        instance,
        rotation.id,
        rotation,
        counts={(1, 2): 0, (2, 2): 0},
    )
    assert freed.unallocated_weeks(1) == 2
    return freed, rotation


def test_resident_override_can_use_unallocated_time() -> None:
    freed, rotation = _free_night_float_pgy1_weeks(sample_instance())
    before = resident_rotation_week_totals(freed, "resident-001")

    updated = replace_standard_rotation(
        freed,
        rotation.id,
        rotation,
        resident_overrides=[
            {
                "resident_id": "resident-001",
                "rotation_id": rotation.id,
                "duration_weeks": 2,
                "replaces_rotation_id": None,
            }
        ],
    )

    override = updated.resident_rotation_overrides[0]
    assert override.replaces_rotation_id is None
    assert updated.resident_unallocated_weeks("resident-001") == 0
    assert updated.scheduling_case().resident_rotation_overrides == [override]
    after = resident_rotation_week_totals(updated, "resident-001")
    assert after["mandatory"] == before["mandatory"] + 2
    assert after["elective"] == before["elective"]
    extras = [
        occurrence
        for occurrence in expand_occurrences(updated, require_configured_electives=False)
        if "resident-override" in occurrence.key and occurrence.resident_id == "resident-001"
    ]
    assert len(extras) == 1

    spent = [
        {
            "resident_id": "resident-001",
            "rotation_id": rotation.id,
            "duration_weeks": 2,
            "replaces_rotation_id": None,
        }
    ]
    with pytest.raises(ValidationError, match="unallocated weeks"):
        replace_standard_rotation(
            freed,
            rotation.id,
            rotation,
            resident_overrides=[
                *spent,
                {
                    "resident_id": "resident-001",
                    "rotation_id": rotation.id,
                    "duration_weeks": 2,
                    "replaces_rotation_id": None,
                },
            ],
        )


def test_unallocated_override_budget_is_managed_per_resident() -> None:
    freed, rotation = _free_night_float_pgy1_weeks(sample_instance())
    resident_ids = [resident.id for resident in freed.residents if resident.pgy == 1][:2]

    updated = replace_standard_rotation(
        freed,
        rotation.id,
        rotation,
        resident_overrides=[
            {
                "resident_id": resident_id,
                "rotation_id": rotation.id,
                "duration_weeks": 2,
                "replaces_rotation_id": None,
            }
            for resident_id in resident_ids
        ],
    )

    assert all(updated.resident_unallocated_weeks(resident_id) == 0 for resident_id in resident_ids)


def test_resident_waiver_skips_a_mandatory_block() -> None:
    from nicegui import ui

    instance = sample_instance()
    rotation = instance.rotation("night_float")
    before = resident_rotation_week_totals(instance, "resident-001")
    before_occurrences = [
        occurrence
        for occurrence in expand_occurrences(instance, require_configured_electives=False)
        if occurrence.resident_id == "resident-001"
        and occurrence.rotation_id == "night_float"
        and occurrence.duration_weeks == 2
        and not occurrence.elective
    ]
    assert len(before_occurrences) == 1

    updated = replace_standard_rotation(
        instance,
        rotation.id,
        rotation,
        resident_waivers=[
            {
                "resident_id": "resident-001",
                "rotation_id": rotation.id,
                "duration_weeks": 2,
            }
        ],
    )

    waiver = updated.resident_rotation_waivers[0]
    assert waiver.rotation_id == "night_float"
    assert updated.scheduling_case().resident_rotation_waivers == [waiver]
    after = resident_rotation_week_totals(updated, "resident-001")
    assert after["mandatory"] == before["mandatory"] - 2
    assert after["elective"] == before["elective"]
    after_occurrences = [
        occurrence
        for occurrence in expand_occurrences(updated, require_configured_electives=False)
        if occurrence.resident_id == "resident-001"
        and occurrence.rotation_id == "night_float"
        and occurrence.duration_weeks == 2
        and not occurrence.elective
    ]
    assert after_occurrences == []

    before_elements = set(ui.context.client.elements)
    _rotation_detail_contents(updated, updated.rotation("night_float"))
    detail_text = {
        getattr(element, "_text", None)
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before_elements
    }
    assert "Individual exceptions" in detail_text
    assert any(isinstance(label, str) and "excused · skips" in label for label in detail_text)

    with pytest.raises(ValidationError, match="are available"):
        replace_standard_rotation(
            instance,
            rotation.id,
            rotation,
            resident_waivers=[
                {
                    "resident_id": "resident-001",
                    "rotation_id": rotation.id,
                    "duration_weeks": 2,
                },
                {
                    "resident_id": "resident-001",
                    "rotation_id": rotation.id,
                    "duration_weeks": 2,
                },
            ],
        )

    with pytest.raises(ValidationError, match="does not allow"):
        replace_standard_rotation(
            instance,
            rotation.id,
            rotation,
            resident_waivers=[
                {
                    "resident_id": "resident-001",
                    "rotation_id": rotation.id,
                    "duration_weeks": 4,
                }
            ],
        )

    freed, _rotation = _free_night_float_pgy1_weeks(instance)
    with pytest.raises(ValidationError, match="no direct .* block to waive"):
        replace_standard_rotation(
            freed,
            rotation.id,
            rotation,
            resident_waivers=[
                {
                    "resident_id": "resident-001",
                    "rotation_id": rotation.id,
                    "duration_weeks": 2,
                }
            ],
        )


def test_grouped_resident_override_requires_a_complete_linked_bundle() -> None:
    instance = sample_instance()
    rotation = instance.rotation("outpatient_gyn")
    bundle = _resident_override_group_bundle(
        instance,
        rotation,
        [],
        "resident-009",
        2,
    )

    assert bundle is not None
    assert [item["rotation_id"] for item in bundle] == [
        "outpatient_gyn",
        "inpatient_ld",
    ]
    assert len({item["group_instance_id"] for item in bundle}) == 1

    updated = replace_standard_rotation(
        instance,
        rotation.id,
        rotation,
        resident_overrides=bundle,
    )
    assert len(updated.resident_rotation_overrides) == 2
    grouped_extras = [
        occurrence
        for occurrence in expand_occurrences(updated, require_configured_electives=False)
        if "resident-override" in occurrence.key
    ]
    assert {item.rotation_group_instance_id for item in grouped_extras} == {
        f"resident-009:override-group:{bundle[0]['group_instance_id']}"
    }

    with pytest.raises(ValidationError, match="exactly one block from every member"):
        replace_standard_rotation(
            instance,
            rotation.id,
            rotation,
            resident_overrides=bundle[:1],
        )

    unmatched = replace_standard_rotation(
        instance,
        rotation.id,
        rotation,
        resident_overrides=[
            {
                "resident_id": "resident-009",
                "rotation_id": rotation.id,
                "duration_weeks": 2,
                "replaces_rotation_id": "elective",
            }
        ],
    )
    unmatched_extra = next(
        occurrence
        for occurrence in expand_occurrences(
            unmatched,
            require_configured_electives=False,
        )
        if "resident-override" in occurrence.key
    )
    assert unmatched_extra.rotation_group_key is None
    assert unmatched_extra.rotation_group_instance_id is None


def test_override_funding_prefers_unallocated_time() -> None:
    from rbs.ui.rotations.overrides import (
        _resident_override_funding_options,
        _resident_override_resident_options,
    )

    freed, rotation = _free_night_float_pgy1_weeks(sample_instance())
    funding = _resident_override_funding_options(freed, rotation, [], "resident-001", 2)

    assert funding[0] == (None, "Unallocated time (2 weeks available)")
    assert funding[1][0] == "elective"

    spent = [
        {
            "resident_id": "resident-001",
            "rotation_id": rotation.id,
            "duration_weeks": 2,
            "replaces_rotation_id": None,
            "group_instance_id": None,
        }
    ]
    assert _resident_override_funding_options(freed, rotation, spent, "resident-001", 2) == [
        (funding[1][0], funding[1][1])
    ]

    unallocated_only = set_elective_allocation(freed, 1, {})
    assert _resident_override_funding_options(
        unallocated_only,
        rotation,
        [],
        "resident-001",
        2,
    ) == [(None, "Unallocated time (4 weeks available)")]
    assert "resident-001" in _resident_override_resident_options(
        unallocated_only,
        rotation,
        [],
    )

    waived_funding = _resident_override_funding_options(
        sample_instance(),
        rotation,
        [],
        "resident-001",
        2,
        [
            {
                "resident_id": "resident-001",
                "rotation_id": rotation.id,
                "duration_weeks": 2,
            }
        ],
    )
    assert waived_funding[0] == (None, "Unallocated time (2 weeks available)")


def test_override_dialog_offers_a_funded_by_choice() -> None:
    from nicegui import ui

    freed, rotation = _free_night_float_pgy1_weeks(sample_instance())
    before = set(ui.context.client.elements)

    _open_resident_rotation_override_dialog(
        freed,
        rotation,
        [],
        lambda: None,
    )

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    funding = next(
        element
        for element in created
        if element.__class__.__name__ == "Select" and element._props.get("label") == "Funded by"
    )
    resident = next(
        element
        for element in created
        if element.__class__.__name__ == "Select" and element._props.get("label") == "Resident"
    )

    assert funding._props["options"]
    resident.set_value("resident-001")
    assert funding.value == "unallocated"


def test_waiver_dialog_and_editor_round_trip_a_draft() -> None:
    from nicegui import ui

    from rbs.ui.rotations.overrides import (
        _open_resident_rotation_waiver_dialog,
        _resident_rotation_overrides_editor,
        _resident_waiver_duration_options,
    )

    instance = sample_instance()
    rotation = instance.rotation("night_float")
    assert set(_resident_waiver_duration_options(instance, rotation, [], "resident-001")) == {2}
    assert (
        _resident_waiver_duration_options(
            instance,
            rotation,
            [{"resident_id": "resident-001", "duration_weeks": 2}],
            "resident-001",
        )
        == {}
    )

    before = set(ui.context.client.elements)
    _open_resident_rotation_waiver_dialog(instance, rotation, [], lambda: None)
    dialog_elements = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    text = {getattr(element, "_text", None) for element in dialog_elements}
    assert f"Waive requirement · {rotation.name}" in text
    assert {
        element._props.get("label")
        for element in dialog_elements
        if element.__class__.__name__ == "Select"
    } >= {"Resident", "Block length"}

    waiver_drafts: list = [
        {"resident_id": "resident-001", "rotation_id": rotation.id, "duration_weeks": 2}
    ]
    before_editor = set(ui.context.client.elements)
    _resident_rotation_overrides_editor(instance, rotation, [], waiver_drafts)
    editor_elements = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before_editor
    ]
    editor_text = {getattr(element, "_text", None) for element in editor_elements}
    editor_buttons = {
        element._props.get("label")
        for element in editor_elements
        if element.__class__.__name__ == "Button"
    }
    assert "Waive requirement" in editor_buttons
    assert "Add resident override" in editor_buttons
    assert any(isinstance(label, str) and "excused" in label for label in editor_text)


def test_grouped_resident_override_dialog_prompts_for_group_or_unmatched_extra() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)

    _open_resident_rotation_override_dialog(
        instance,
        instance.rotation("outpatient_gyn"),
        [],
        lambda: None,
    )

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    radio = next(element for element in created if element.__class__.__name__ == "Radio")
    labels = {option["label"] for option in radio._props["options"]}
    text = {getattr(element, "_text", None) for element in created}

    assert labels == {
        "Add the complete rotation group",
        "Add an unmatched extra",
    }
    assert any(
        isinstance(label, str) and "Grouped extras are scheduled contiguously" in label
        for label in text
    )


def test_removing_group_member_removes_complete_linked_override_bundle() -> None:
    instance = sample_instance()
    rotation = instance.rotation("outpatient_gyn")
    bundle = _resident_override_group_bundle(
        instance,
        rotation,
        [],
        "resident-009",
        2,
    )
    assert bundle is not None
    configured = replace_standard_rotation(
        instance,
        rotation.id,
        rotation,
        resident_overrides=bundle,
    )

    removed = remove_mandatory_rotation(configured, "inpatient_ld")

    assert not removed.resident_rotation_overrides
    assert removed.rotation_group_for(1, "outpatient_gyn") is None
    assert removed.rotation_group_for(2, "outpatient_gyn") is None


def test_standard_rotation_mandatory_counts_are_editable_per_block() -> None:
    instance = sample_instance()
    rotation = instance.rotation("night_float")
    assert [
        (block.duration_weeks, block.count)
        for block in instance.curriculum_for(1).blocks
        if block.rotation_id == "night_float"
    ] == [(2, 1)]

    elective_only = replace_standard_rotation(
        instance,
        rotation.id,
        rotation,
        counts={(1, 2): 0, (2, 2): 0},
    )
    assert [
        block
        for block in elective_only.curriculum_for(1).blocks
        if block.rotation_id == "night_float"
    ] == []
    assert elective_only.unallocated_weeks(1) == instance.unallocated_weeks(1) + 2

    restored = replace_standard_rotation(
        elective_only,
        rotation.id,
        rotation,
        counts={(1, 2): 1, (2, 2): 0},
    )
    assert [
        (block.duration_weeks, block.count)
        for block in restored.curriculum_for(1).blocks
        if block.rotation_id == "night_float"
    ] == [(2, 1)]

    untouched = replace_standard_rotation(instance, rotation.id, rotation)
    assert [
        (block.duration_weeks, block.count)
        for block in untouched.curriculum_for(1).blocks
        if block.rotation_id == "night_float"
    ] == [(2, 1)]


def test_block_length_options_exclude_sibling_durations() -> None:
    """Duplicate durations cannot be saved, so they are not offered."""
    from nicegui import ui

    instance = sample_instance()
    assert sorted(
        config.duration_weeks
        for rule in instance.rotation("fmed").pgy_rules
        if rule.pgy == 2
        for config in rule.block_configs
    ) == [2, 4]
    before = set(ui.context.client.elements)
    _open_fmed_pgy_rules_dialog(
        instance,
        "fmed",
        selected_rotation_id=None,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    length_selects = [
        element
        for element in created
        if element.__class__.__name__ == "Select" and element._props.get("label") == "Block length"
    ]
    by_value: dict[int, list[list[str]]] = {}
    for select in length_selects:
        labels = [option["label"] for option in select._props["options"]]
        by_value.setdefault(int(select.value), []).append(labels)

    # PGY 2's 2-week row cannot switch to the sibling 4-week duration.
    assert len(by_value[2]) == 1
    assert "2 weeks" in by_value[2][0]
    assert "4 weeks" not in by_value[2][0]
    # PGY 2's 4-week row cannot switch to the sibling 2-week duration.
    assert any("2 weeks" not in labels for labels in by_value[4])


def test_standard_rotation_editor_saves_a_changed_block_length() -> None:
    from nicegui import ui
    from nicegui.events import ClickEventArguments

    instance = sample_instance()
    rotation = instance.rotation("icu")
    saved: list = []
    before = set(ui.context.client.elements)
    _rotation_editor(
        instance,
        rotation,
        on_cancel=lambda: None,
        on_save=lambda updated, rotation_id: saved.append((updated, rotation_id)),
    )

    def current_elements() -> list:
        return [
            element
            for element_id, element in ui.context.client.elements.items()
            if element_id not in before
        ]

    block_length = next(
        element
        for element in current_elements()
        if element.__class__.__name__ == "Select"
        and element._props.get("label") == "Block length"
    )
    assert block_length.value == 4

    block_length.set_value(2)

    refreshed_length = next(
        element
        for element in current_elements()
        if element.__class__.__name__ == "Select"
        and element._props.get("label") == "Block length"
    )
    assert refreshed_length.value == 2
    save = next(
        element
        for element in current_elements()
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Save rotation"
    )
    listener = next(
        listener for listener in save._event_listeners.values() if listener.type == "click"
    )
    listener.handler(ClickEventArguments(sender=save, client=save.client))

    updated, rotation_id = saved[-1]
    assert rotation_id == rotation.id
    assert [
        config.duration_weeks for config in updated.rotation(rotation.id).pgy_rule(1).block_configs
    ] == [2]
    assert [
        (block.duration_weeks, block.count)
        for block in updated.curriculum_for(1).blocks
        if block.rotation_id == rotation.id
    ] == [(2, 1)]


def test_block_config_rows_stay_on_one_line() -> None:
    from nicegui import ui

    from rbs.ui.rotations.editor import _rotation_editor

    instance = sample_instance()
    before = set(ui.context.client.elements)
    _rotation_editor(
        instance,
        instance.rotation("night_float"),
        on_cancel=lambda: None,
        on_save=lambda _instance, _rotation_id: None,
    )
    rows = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
        and element.__class__.__name__ == "Row"
        and "rbs-block-config-row" in getattr(element, "_classes", [])
    ]

    assert rows
    assert all("flex-nowrap" in getattr(element, "_classes", []) for element in rows)


def test_standard_rotation_editor_offers_a_per_year_weeks_cap() -> None:
    from nicegui import ui

    from rbs.ui.rotations.editor import _rotation_editor

    instance = sample_instance()
    before = set(ui.context.client.elements)
    _rotation_editor(
        instance,
        instance.rotation("night_float"),
        on_cancel=lambda: None,
        on_save=lambda _instance, _rotation_id: None,
    )
    caps = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
        and element.__class__.__name__ == "Number"
        and element._props.get("label") == "Maximum total weeks"
    ]

    assert len(caps) == len(instance.rotation("night_float").pgy_rules)


def test_per_year_cap_cannot_undercut_mandatory_weeks() -> None:
    instance = sample_instance()
    rotation = instance.rotation("night_float")
    assert [
        (b.duration_weeks, b.count)
        for b in instance.curriculum_for(1).blocks
        if b.rotation_id == "night_float"
    ] == [(2, 1)]

    capped = rotation.model_copy(
        update={
            "pgy_rules": [
                rule.model_copy(update={"max_total_weeks": 4}) if rule.pgy == 1 else rule
                for rule in rotation.pgy_rules
            ]
        }
    )
    updated = replace_standard_rotation(
        instance,
        rotation.id,
        capped,
        counts={(1, 2): 1, (2, 2): 0},
    )
    assert updated.rotation("night_float").max_total_weeks_for_pgy(1) == 4

    too_tight = rotation.model_copy(
        update={
            "pgy_rules": [
                rule.model_copy(update={"max_total_weeks": 1}) if rule.pgy == 1 else rule
                for rule in rotation.pgy_rules
            ]
        }
    )
    with pytest.raises(ValueError, match="exceeding its 1-week maximum"):
        replace_standard_rotation(
            instance,
            rotation.id,
            too_tight,
            counts={(1, 2): 1, (2, 2): 0},
        )


def test_rotation_required_nowhere_shows_a_soft_hint() -> None:
    from nicegui import ui

    from rbs.ui.rotations.editor import _rotation_detail_contents

    instance = sample_instance()
    rotation = instance.rotation("night_float")
    elective_only = replace_standard_rotation(
        instance,
        rotation.id,
        rotation,
        counts={(1, 2): 0, (2, 2): 0},
    )
    before = set(ui.context.client.elements)
    _rotation_detail_contents(elective_only, elective_only.rotation("night_float"))
    text = {
        getattr(element, "_text", None)
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    }

    assert "Not required anywhere" in text
    assert "Mandatory only" not in text


def _new_test_rotation(instance, code="TEST", name="Test Rotation"):
    from rbs.ui.rotations.forms import _new_mandatory_rotation_draft

    draft = _new_mandatory_rotation_draft(instance)
    draft.update(
        {
            "code": code,
            "name": name,
            "id": next_mandatory_rotation_id(instance, name),
        }
    )
    draft["pgy_rules"] = [
        {
            "pgy": 1,
            "min_concurrent": None,
            "max_concurrent": None,
            "max_total_weeks": None,
            "prerequisite_rotation_ids": [],
            "earliest_start_week": None,
            "block_configs": [
                {
                    "duration_weeks": 2,
                    "vacation": {"allowed": False, "max_weeks_per_block": None},
                }
            ],
        }
    ]
    return rotation_from_editor_state(draft)


def test_elective_shapes_derive_after_funding() -> None:
    from rbs.ui.rotations.ops import add_mandatory_rotation

    freed, _rotation = _free_night_float_pgy1_weeks(sample_instance())
    replacement = _new_test_rotation(freed)
    updated = add_mandatory_rotation(
        freed,
        replacement,
        {(1, 2): 1},
        elective_shapes={(1, 2)},
        elective_repeatable=False,
    )
    assert updated.eligible_elective_pgys(replacement.id) == (1,)
    assert updated.eligible_elective_block_sizes(replacement.id) == (2,)

    # Growing the requirement consumes PGY 1's last Elective slot, so the
    # marked shape no longer fills anything: a clear error, not stale sizes.
    doomed = _new_test_rotation(sample_instance())
    with pytest.raises(ValueError, match="do not match any Elective curriculum time"):
        add_mandatory_rotation(
            sample_instance(),
            doomed,
            {(1, 2): 1},
            elective_shapes={(1, 2)},
            elective_repeatable=False,
        )


def test_standard_rotation_editor_shows_blocks_per_resident() -> None:
    from nicegui import ui

    from rbs.ui.rotations.editor import _rotation_editor

    instance = sample_instance()
    before = set(ui.context.client.elements)
    _rotation_editor(
        instance,
        instance.rotation("night_float"),
        on_cancel=lambda: None,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    block_counts = [
        element
        for element in created
        if element.__class__.__name__ == "Number"
        and element._props.get("label") == "Blocks per resident"
    ]

    assert block_counts
    toggles = [element for element in created if element.__class__.__name__ == "Toggle"]
    assert {toggle.value for toggle in toggles} == {"Both", "Elective"}
    assert all(
        [option["label"] for option in toggle._props["options"]]
        == ["Mandatory", "Elective", "Both"]
        for toggle in toggles
    )
    assert any(
        getattr(element, "_text", None) == "Set each block shape to Mandatory, Elective, or Both, "
        "then set how many mandatory blocks each resident takes."
        for element in created
    )
    assert not any(
        str(getattr(element, "_text", None)).startswith("Available to")
        and str(getattr(element, "_text", None)).endswith("as an elective")
        for element in created
        if element.__class__.__name__ == "Checkbox"
    )


def _fire_click(element) -> None:
    from nicegui.events import ClickEventArguments

    listener = next(
        registered
        for registered in element._event_listeners.values()
        if registered.type == "click"
    )
    listener.handler(ClickEventArguments(sender=element, client=element.client))


def _editor_elements_since(before: set) -> list:
    from nicegui import ui

    return [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]


def _editor_close_button(created: list):
    return next(
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and element._props.get("icon") == "close"
        and not element._props.get("label")
    )


def test_mandatory_editor_keeps_save_beside_close_without_cancel() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)
    _rotation_editor(
        instance,
        instance.rotation("icu"),
        on_cancel=lambda: None,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = _editor_elements_since(before)
    button_labels = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Button"
    }

    assert "Cancel" not in button_labels
    save = next(
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Save rotation"
    )
    close = _editor_close_button(created)
    assert save.parent_slot.parent is close.parent_slot.parent


def test_mandatory_editor_close_without_changes_skips_confirmation() -> None:
    from nicegui import ui

    instance = sample_instance()
    cancels: list = []
    before = set(ui.context.client.elements)
    _rotation_editor(
        instance,
        instance.rotation("icu"),
        on_cancel=lambda: cancels.append(True),
        on_save=lambda _instance, _rotation_id: None,
    )
    created = _editor_elements_since(before)
    clicked = set(ui.context.client.elements)
    _fire_click(_editor_close_button(created))

    assert cancels == [True]
    assert not any(
        element.__class__.__name__ == "Dialog" for element in _editor_elements_since(clicked)
    )


def test_mandatory_editor_close_with_changes_confirms_before_discarding() -> None:
    from nicegui import ui

    instance = sample_instance()
    cancels: list = []
    before = set(ui.context.client.elements)
    _rotation_editor(
        instance,
        instance.rotation("icu"),
        on_cancel=lambda: cancels.append(True),
        on_save=lambda _instance, _rotation_id: None,
    )
    created = _editor_elements_since(before)
    name = next(
        element
        for element in created
        if element.__class__.__name__ == "Input" and element._props.get("label") == "Rotation name"
    )
    name.set_value("Intensive Care Renamed")
    clicked = set(ui.context.client.elements)
    _fire_click(_editor_close_button(created))

    assert cancels == []
    confirm = _editor_elements_since(clicked)
    assert any(element.__class__.__name__ == "Dialog" for element in confirm)
    assert "Discard unsaved changes?" in {getattr(element, "_text", None) for element in confirm}
    discard = next(
        element
        for element in confirm
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Discard changes"
    )
    _fire_click(discard)

    assert cancels == [True]


def _item_texts(element) -> list[str]:
    texts = [element._text] if getattr(element, "_text", None) else []
    for slot in element.slots.values():
        for child in slot.children:
            texts.extend(_item_texts(child))
    return [str(text) for text in texts]


def _directory_item_for(created: list, name: str, *, elective_option: bool = False):
    matches = [
        element
        for element in created
        if element.__class__.__name__ == "Item" and name in _item_texts(element)
    ]
    if elective_option:
        matches = [
            element
            for element in matches
            if any("service" in text.casefold() for text in _item_texts(element))
        ]
    else:
        matches = [
            element
            for element in matches
            if not any("service" in text.casefold() for text in _item_texts(element))
        ]
    assert matches, f"no directory item found for {name!r}"
    return matches[0]


def test_guarded_navigation_proceeds_when_editor_is_clean() -> None:
    from nicegui import ui

    from rbs.ui.rotations.forms import RotationEditorGuard, confirm_guarded_navigation

    proceeded: list = []
    guard = RotationEditorGuard()
    before = set(ui.context.client.elements)
    confirm_guarded_navigation(guard, lambda: proceeded.append("night_float"))

    assert proceeded == ["night_float"]
    assert not any(
        element.__class__.__name__ == "Dialog" for element in _editor_elements_since(before)
    )


def test_guarded_navigation_confirms_and_discards_changes_on_one_line() -> None:
    from nicegui import ui

    from rbs.ui.rotations.forms import RotationEditorGuard, confirm_guarded_navigation

    proceeded: list = []
    guard = RotationEditorGuard(
        is_dirty=lambda: True,
        save=lambda: True,
        subject="ICU — Intensive Care",
        save_label="Save rotation",
        save_icon="save",
    )
    before = set(ui.context.client.elements)
    confirm_guarded_navigation(guard, lambda: proceeded.append("night_float"))

    assert proceeded == []
    confirm = _editor_elements_since(before)
    assert any(element.__class__.__name__ == "Dialog" for element in confirm)
    keep = next(
        element
        for element in confirm
        if element.__class__.__name__ == "Button" and element._props.get("label") == "Keep editing"
    )
    discard = next(
        element
        for element in confirm
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Discard changes"
    )
    save_button = next(
        element
        for element in confirm
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Save rotation"
    )
    actions = save_button.parent_slot.parent
    assert keep.parent_slot.parent is actions
    assert discard.parent_slot.parent is actions
    assert "flex-nowrap" in getattr(actions, "_classes", [])
    _fire_click(discard)

    assert proceeded == ["night_float"]
    assert guard.is_dirty is None


@pytest.mark.parametrize(
    ("saved_ok", "expected"),
    [(False, []), (True, ["night_float"])],
)
def test_guarded_navigation_save_continues_only_on_success(saved_ok: bool, expected: list) -> None:
    from nicegui import ui

    from rbs.ui.rotations.forms import RotationEditorGuard, confirm_guarded_navigation

    proceeded: list = []
    guard = RotationEditorGuard(
        is_dirty=lambda: True,
        save=lambda: saved_ok,
        subject="ICU — Intensive Care",
        save_label="Save rotation",
        save_icon="save",
    )
    before = set(ui.context.client.elements)
    confirm_guarded_navigation(guard, lambda: proceeded.append("night_float"))
    confirm = _editor_elements_since(before)
    save_button = next(
        element
        for element in confirm
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Save rotation"
    )
    _fire_click(save_button)

    assert proceeded == expected


def test_mandatory_directory_click_without_changes_navigates_directly() -> None:
    from nicegui import ui

    instance = sample_instance()
    selections: list = []
    before = set(ui.context.client.elements)
    render_rotations_tab(
        instance,
        selected_rotation_id="icu",
        on_select=selections.append,
        on_save=lambda _instance, _rotation_id: None,
        active_section="standard_rotations",
    )
    created = _editor_elements_since(before)
    clicked = set(ui.context.client.elements)
    _fire_click(_directory_item_for(created, "Night Float"))

    assert selections == ["night_float"]
    assert not any(
        element.__class__.__name__ == "Dialog" for element in _editor_elements_since(clicked)
    )


def test_mandatory_directory_click_with_changes_confirms_before_leaving() -> None:
    from nicegui import ui

    instance = sample_instance()
    selections: list = []
    before = set(ui.context.client.elements)
    render_rotations_tab(
        instance,
        selected_rotation_id="icu",
        on_select=selections.append,
        on_save=lambda _instance, _rotation_id: None,
        active_section="standard_rotations",
    )
    created = _editor_elements_since(before)
    edit = next(
        element
        for element in created
        if element.__class__.__name__ == "Button" and element._props.get("label") == "Edit"
    )
    _fire_click(edit)
    refreshed = _editor_elements_since(before)
    name = next(
        element
        for element in refreshed
        if element.__class__.__name__ == "Input" and element._props.get("label") == "Rotation name"
    )
    name.set_value("Intensive Care Renamed")
    clicked = set(ui.context.client.elements)
    _fire_click(_directory_item_for(refreshed, "Night Float"))

    assert selections == []
    confirm = _editor_elements_since(clicked)
    assert "Discard unsaved changes?" in {getattr(element, "_text", None) for element in confirm}
    discard = next(
        element
        for element in confirm
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Discard changes"
    )
    _fire_click(discard)

    assert selections == ["night_float"]


def test_section_switch_preserves_dirty_editor() -> None:
    """Switching sections hides panels without destroying the editor.

    The tab change only records the section server-side, so unsaved edits
    survive the switch and still prompt on close or directory navigation.
    """
    from nicegui import ui
    from nicegui.events import ValueChangeEventArguments

    instance = sample_instance()
    selections: list = []
    sections: list = []
    before = set(ui.context.client.elements)
    render_rotations_tab(
        instance,
        selected_rotation_id="icu",
        on_select=selections.append,
        on_save=lambda _instance, _rotation_id: None,
        active_section="standard_rotations",
        on_section_change=lambda event: sections.append(event.value),
    )
    created = _editor_elements_since(before)
    edit = next(
        element
        for element in created
        if element.__class__.__name__ == "Button" and element._props.get("label") == "Edit"
    )
    _fire_click(edit)
    refreshed = _editor_elements_since(before)
    name = next(
        element
        for element in refreshed
        if element.__class__.__name__ == "Input" and element._props.get("label") == "Rotation name"
    )
    name.set_value("Intensive Care Renamed")
    section_tabs = next(
        element
        for element in created
        if element.__class__.__name__ == "Tabs"
        and "rbs-configuration-tabs" in getattr(element, "_classes", [])
    )
    switched = set(ui.context.client.elements)
    event = ValueChangeEventArguments(
        sender=section_tabs,
        client=section_tabs.client,
        value="elective_configuration",
        previous_value="standard_rotations",
    )
    for handler in list(section_tabs._change_handlers):
        handler(event)

    assert sections[-1] == "elective_configuration"
    assert selections == []
    assert not any(
        element.__class__.__name__ == "Dialog" for element in _editor_elements_since(switched)
    )
    _fire_click(_editor_close_button(refreshed))

    assert selections == []
    assert "Discard unsaved changes?" in {
        getattr(element, "_text", None) for element in _editor_elements_since(switched)
    }


def test_directory_click_with_two_dirty_editors_confirms_twice() -> None:
    """Editors can stay dirty in both sections; navigation checks each guard."""
    from nicegui import ui

    instance = sample_instance()
    selections: list = []
    before = set(ui.context.client.elements)
    render_rotations_tab(
        instance,
        selected_rotation_id="night_float",
        on_select=selections.append,
        on_save=lambda _instance, _rotation_id: None,
        active_section="standard_rotations",
    )
    created = _editor_elements_since(before)
    edits = [
        element
        for element in created
        if element.__class__.__name__ == "Button" and element._props.get("label") == "Edit"
    ]
    assert len(edits) == 2
    for edit in edits:
        _fire_click(edit)
    names = [
        element
        for element in _editor_elements_since(before)
        if element.__class__.__name__ == "Input" and element._props.get("label") == "Rotation name"
    ]
    assert len(names) == 2
    for index, name in enumerate(names):
        name.set_value(f"Night Float {index}")
    clicked = set(ui.context.client.elements)
    _fire_click(_directory_item_for(_editor_elements_since(before), "ICU"))

    assert selections == []
    confirm = _editor_elements_since(clicked)
    assert any(element.__class__.__name__ == "Dialog" for element in confirm)
    assert "Changes to Night Float (NF) will be lost unless you save them." in {
        getattr(element, "_text", None) for element in confirm
    }
    assert any(
        element.__class__.__name__ == "Card" and "max-w-lg" in getattr(element, "_classes", [])
        for element in confirm
    )
    after_first = set(ui.context.client.elements)
    _fire_click(
        next(
            element
            for element in confirm
            if element.__class__.__name__ == "Button"
            and element._props.get("label") == "Discard changes"
        )
    )

    assert selections == []
    second = _editor_elements_since(after_first)
    assert any(element.__class__.__name__ == "Dialog" for element in second)
    _fire_click(
        next(
            element
            for element in second
            if element.__class__.__name__ == "Button"
            and element._props.get("label") == "Discard changes"
        )
    )

    assert selections == ["icu"]


def test_mandatory_editor_close_with_changes_can_keep_editing() -> None:
    from nicegui import ui

    instance = sample_instance()
    cancels: list = []
    saves: list = []
    before = set(ui.context.client.elements)
    _rotation_editor(
        instance,
        instance.rotation("icu"),
        on_cancel=lambda: cancels.append(True),
        on_save=lambda updated, rotation_id: saves.append((updated, rotation_id)),
    )
    created = _editor_elements_since(before)
    name = next(
        element
        for element in created
        if element.__class__.__name__ == "Input" and element._props.get("label") == "Rotation name"
    )
    name.set_value("Intensive Care Renamed")
    clicked = set(ui.context.client.elements)
    _fire_click(_editor_close_button(created))

    confirm = _editor_elements_since(clicked)
    keep = next(
        element
        for element in confirm
        if element.__class__.__name__ == "Button" and element._props.get("label") == "Keep editing"
    )
    _fire_click(keep)

    assert cancels == []
    assert saves == []
