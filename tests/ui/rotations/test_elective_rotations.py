import pytest
from pydantic import ValidationError

from rbs.catalog import sample_instance
from rbs.models.elective import ElectiveConfiguration
from rbs.models.enums import RotationKind, SolverEngineName, SolverStatus
from rbs.models.instance import SchedulerInput
from rbs.models.resident import ElectivePreferenceRequest
from rbs.models.rotation import DEFAULT_ROTATION_COLOR, Rotation
from rbs.models.schedule import Assignment, Schedule, ScheduleMeta
from rbs.solver.planning import expand_occurrences
from rbs.ui.grid import render_grid_html
from rbs.ui.residents.ops import resident_schedule_report_rows
from rbs.ui.rotations.editor import (
    _elective_rotation_editor,
    _open_elective_rotation_dialog,
    _open_fmed_pgy_rules_dialog,
    _rotation_detail_contents,
    _rotation_editor,
    render_rotations_tab,
)
from rbs.ui.rotations.ops import (
    add_elective_rotation,
    remove_elective_rotation,
    replace_elective_color,
    replace_standard_rotation,
    set_elective_eligibility,
)


def _standalone_elective() -> Rotation:
    return Rotation.model_validate(
        {
            "id": "addiction_medicine_elective",
            "code": "ADD",
            "name": "Addiction Medicine",
            "kind": RotationKind.ELECTIVE.value,
            "color": "#123456",
            "pgy_rules": [
                {
                    "pgy": 1,
                    "max_concurrent": 1,
                    "block_configs": [{"duration_weeks": 2}],
                },
                {
                    "pgy": 2,
                    "max_concurrent": 1,
                    "block_configs": [{"duration_weeks": 2}],
                },
            ],
        }
    )


def _instance_with_two_elective_block_sizes() -> SchedulerInput:
    raw = sample_instance().model_dump(mode="json")
    standalone = _standalone_elective()
    raw["rotations"].append(standalone.model_dump(mode="json"))
    rotations = {rotation["id"]: rotation for rotation in raw["rotations"]}
    for rotation_id in ("elective", "night_float", "clinic"):
        pgy1 = next(rule for rule in rotations[rotation_id]["pgy_rules"] if rule["pgy"] == 1)
        pgy1["block_configs"].append(
            {
                "duration_weeks": 4,
                "vacation": {
                    "allowed": False,
                    "max_weeks_per_block": None,
                },
            }
        )
    pgy1_curriculum = next(
        curriculum for curriculum in raw["requirements"] if curriculum["pgy"] == 1
    )
    next(
        block for block in pgy1_curriculum["blocks"] if block["rotation_id"] == "behavioral_health"
    )["rotation_id"] = "elective"
    raw["electives"]["rotation_options"] = [
        {"rotation_id": standalone.id, "eligible_block_sizes": [2]},
        {"rotation_id": "night_float", "eligible_block_sizes": [4]},
    ]
    raw["residents"][0]["elective_preferences"] = [
        {
            "rotation_id": "addiction_medicine_elective",
            "duration_weeks": 2,
        },
        {"rotation_id": "night_float", "duration_weeks": 4},
    ]
    return SchedulerInput.model_validate(raw)


def test_standalone_elective_uses_shared_color_and_is_immediately_eligible() -> None:
    instance = sample_instance()
    updated = add_elective_rotation(instance, _standalone_elective())

    configured = updated.rotation("addiction_medicine_elective")
    assert configured.color == updated.electives.color
    assert updated.is_elective_option(configured.id)
    assert updated.eligible_elective_block_sizes(configured.id) == (2,)
    assert updated.assignment_color(configured.id, elective=True) == updated.electives.color
    assert updated.assignment_label(configured.id, elective=True).endswith(
        "Addiction Medicine (Elec)"
    )

    recolored = replace_elective_color(updated, "#2B6F8A")
    assert recolored.electives.color == "#2B6F8A"
    assert recolored.rotation(configured.id).color == "#2B6F8A"
    assert recolored.rotation("elective").color == "#2B6F8A"


def test_mandatory_service_as_elective_shares_service_identity_and_capacity_pool() -> None:
    instance = set_elective_eligibility(
        sample_instance(),
        "night_float",
        eligible=True,
    )
    resident = instance.residents_by_id["resident-001"].model_copy(
        update={
            "elective_preferences": [
                ElectivePreferenceRequest(
                    rotation_id="night_float",
                    duration_weeks=2,
                )
            ]
        }
    )
    instance = instance.revised(
        residents=[resident if item.id == resident.id else item for item in instance.residents]
    )
    occurrences = [
        occurrence
        for occurrence in expand_occurrences(instance)
        if occurrence.resident_id == "resident-001" and occurrence.rotation_id == "night_float"
    ]

    assert any(not occurrence.elective for occurrence in occurrences)
    assert any(occurrence.elective for occurrence in occurrences)
    assert {occurrence.rotation_id for occurrence in occurrences} == {"night_float"}
    assert instance.eligible_elective_block_sizes("night_float") == (2,)
    assert (
        instance.assignment_color("night_float", elective=True)
        == instance.rotation("night_float").color
    )


def test_mandatory_elective_policy_filters_by_training_level_and_repeatability() -> None:
    instance = set_elective_eligibility(
        sample_instance(),
        "night_float",
        eligible=True,
        eligible_pgys=[2],
        eligible_block_sizes=[2],
        repeatable=False,
    )

    assert instance.eligible_elective_pgys("night_float") == (2,)
    assert not instance.elective_option_is_repeatable("night_float")
    assert "night_float" not in {rotation.id for rotation in instance.elective_options_for(1, 2)}
    assert "night_float" in {rotation.id for rotation in instance.elective_options_for(2, 2)}


def test_fmed_as_elective_keeps_custom_kind_and_shared_capacity_identity() -> None:
    instance = sample_instance()
    resident = instance.residents_by_id["resident-009"].model_copy(
        update={
            "elective_preferences": [
                ElectivePreferenceRequest(rotation_id="fmed", duration_weeks=2)
            ]
        }
    )
    instance = instance.revised(
        residents=[resident if item.id == resident.id else item for item in instance.residents]
    )

    occurrences = [
        occurrence
        for occurrence in expand_occurrences(instance)
        if occurrence.resident_id == resident.id and occurrence.rotation_id == "fmed"
    ]

    assert instance.rotation("fmed").kind is RotationKind.FMED
    assert any(not occurrence.elective for occurrence in occurrences)
    assert any(occurrence.elective for occurrence in occurrences)
    assert instance.eligible_elective_block_sizes("fmed") == (2,)


def test_fmed_rules_dialog_owns_elective_availability() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)

    _open_fmed_pgy_rules_dialog(
        instance,
        "fmed",
        selected_rotation_id="fmed",
        on_save=lambda _instance, _rotation_id: None,
    )

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    elective_toggles = {
        getattr(element, "_text", None): element
        for element in created
        if element.__class__.__name__ == "Checkbox"
        and str(getattr(element, "_text", "")).endswith("as an elective")
    }
    repeatable = next(
        element
        for element in created
        if element.__class__.__name__ == "Checkbox"
        and getattr(element, "_text", None) == "Can be taken more than once as an elective"
    )
    block_sizes = next(
        element
        for element in created
        if element.__class__.__name__ == "Select"
        and element._props.get("label") == "Eligible elective block sizes"
    )

    assert elective_toggles["Available to PGY 1 as an elective"].value is False
    assert elective_toggles["Available to PGY 2 as an elective"].value is True
    assert elective_toggles["Available to PGY 3 as an elective"].value is False
    assert repeatable.value is True
    assert block_sizes.value == [2]


def test_elective_marker_and_inherited_color_appear_on_calendars() -> None:
    instance = set_elective_eligibility(
        sample_instance(),
        "night_float",
        eligible=True,
    )
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
                rotation_id="night_float",
                elective=True,
                start_week=1,
                end_week=2,
                weeks=[1, 2],
            )
        ],
    )

    markup = render_grid_html(instance, schedule)
    rows = resident_schedule_report_rows(instance, schedule, resident.id)

    assert "Night Float (Elec)" in markup
    assert "night_float (Elec)" not in markup
    assert f"--rbs-rotation-color:{instance.rotation('night_float').color}" in markup
    assert rows[0]["rotation"] == "NF · Night Float (Elec)"
    assert rows[0]["color"] == instance.rotation("night_float").color


def test_each_elective_option_only_fills_its_selected_block_sizes() -> None:
    instance = _instance_with_two_elective_block_sizes()

    assert instance.elective_block_sizes == (2, 4)
    assert [rotation.id for rotation in instance.elective_options_for(1, 2)] == [
        "addiction_medicine_elective"
    ]
    assert [rotation.id for rotation in instance.elective_options_for(1, 4)] == ["night_float"]

    elective_occurrences = [
        occurrence
        for occurrence in expand_occurrences(instance)
        if occurrence.resident_id == "resident-001" and occurrence.elective
    ]
    assert {
        (
            occurrence.rotation_id,
            occurrence.duration_weeks,
            occurrence.elective_fallback,
        )
        for occurrence in elective_occurrences
    } == {
        ("addiction_medicine_elective", 2, False),
        ("night_float", 4, False),
        ("clinic", 2, True),
        ("clinic", 4, True),
    }


def test_legacy_rotation_id_lists_are_rejected() -> None:
    raw = sample_instance().model_dump(mode="json")
    raw["electives"] = {
        "color": raw["electives"]["color"],
        "eligible_rotation_ids": ["elective"],
    }

    with pytest.raises(ValidationError, match="eligible_rotation_ids"):
        SchedulerInput.model_validate(raw)


def test_elective_configuration_without_a_color_uses_the_stable_default() -> None:
    configuration = ElectiveConfiguration.model_validate({"rotation_options": []})

    assert configuration.color == DEFAULT_ROTATION_COLOR
    assert configuration.rotation_options == []


def test_elective_option_rejects_a_size_the_service_cannot_fill() -> None:
    raw = sample_instance().model_dump(mode="json")
    raw["electives"]["rotation_options"].append(
        {"rotation_id": "sports_med", "eligible_block_sizes": [2]}
    )

    with pytest.raises(
        ValidationError,
        match="must select at least one eligible training level",
    ):
        SchedulerInput.model_validate(raw)


def test_elective_configuration_ui_has_shared_properties_and_option_menus() -> None:
    from nicegui import ui

    instance = set_elective_eligibility(
        add_elective_rotation(sample_instance(), _standalone_elective()),
        "night_float",
        eligible=True,
    )
    before = set(ui.context.client.elements)
    render_rotations_tab(
        instance,
        selected_rotation_id=None,
        on_select=lambda _rotation_id: None,
        on_save=lambda _instance, _rotation_id: None,
        active_section="elective_configuration",
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

    assert "Shared elective properties" in text
    assert "Available Electives" in text
    assert "Electives by code" in text
    assert "Addiction Medicine" in text
    assert "Night Float" in text
    assert "Elective" not in text
    assert "Standalone elective · PGY1, PGY2 · 2 weeks · once per resident" in text
    assert "Mandatory service · PGY1, PGY2 · 2 weeks · repeatable" in text
    assert "Select an elective" in text
    assert "Standalone elective rotations" not in text
    assert "Mandatory elective options" not in text
    assert "Applied to standalone electives" not in text
    assert (
        "Standalone elective services use this color. A Mandatory service used as an "
        "elective keeps its Mandatory color." not in text
    )
    assert (
        "Set shared Elective properties, then choose the services that may fill "
        "Elective time." not in text
    )
    assert "New elective" in button_labels
    assert any(
        element.__class__.__name__ == "Input" and element._props.get("label") == "Search electives"
        for element in created
    )
    assert any("rbs-master-directory" in getattr(element, "_classes", []) for element in created)
    assert "Mandatory rotation group" not in text


def test_mandatory_rotation_owns_its_elective_availability() -> None:
    from nicegui import ui

    instance = set_elective_eligibility(
        sample_instance(),
        "night_float",
        eligible=True,
    )
    rotation = instance.rotation("night_float")
    before = set(ui.context.client.elements)
    _rotation_editor(
        instance,
        rotation,
        on_cancel=lambda: None,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    checkboxes = {
        getattr(element, "_text", None): element
        for element in created
        if element.__class__.__name__ == "Checkbox"
    }
    block_size_select = next(
        element
        for element in created
        if element.__class__.__name__ == "Select"
        and element._props.get("label") == "Eligible elective block sizes"
    )

    assert checkboxes["Available to PGY 1 as an elective"].value is True
    assert checkboxes["Available to PGY 2 as an elective"].value is True
    assert checkboxes["Available to PGY 3 as an elective"].value is False
    assert checkboxes["Can be taken more than once as an elective"].value is True
    assert block_size_select.value == [2]
    assert any(getattr(element, "_text", None) == "Elective availability" for element in created)

    disabled = replace_standard_rotation(
        instance,
        rotation.id,
        rotation,
        eligible_as_elective=False,
    )
    assert not disabled.is_elective_option(rotation.id)


def test_elective_editor_uses_wide_responsive_dialog_layout() -> None:
    from nicegui import ui

    before = set(ui.context.client.elements)
    _open_elective_rotation_dialog(
        sample_instance(),
        selected_rotation_id=None,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    dialog_card = next(
        element
        for element in created
        if "rbs-elective-editor-dialog" in getattr(element, "_classes", [])
    )
    editor_scroll = next(
        element
        for element in created
        if "rbs-elective-editor-scroll" in getattr(element, "_classes", [])
    )
    editor_panels = next(
        element
        for element in created
        if "rbs-elective-editor-panels" in getattr(element, "_classes", [])
    )
    block_size_select = next(
        element
        for element in created
        if element.__class__.__name__ == "Select"
        and element._props.get("label") == "Eligible elective block sizes"
    )
    tabs = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Tab"
    }
    text = {getattr(element, "_text", None) for element in created}
    total_weeks = next(
        element
        for element in created
        if element.__class__.__name__ == "Number"
        and element._props.get("label") == "Maximum total weeks"
    )
    total_week_fields = [
        element
        for element in created
        if element.__class__.__name__ == "Number"
        and element._props.get("label") == "Maximum total weeks"
    ]
    consecutive = next(
        element
        for element in created
        if element.__class__.__name__ == "Select"
        and element._props.get("label") == "Max consecutive weeks"
    )
    header = next(
        element
        for element in created
        if "rbs-clinic-editor-header" in getattr(element, "_classes", [])
    )
    tab_bar = next(
        element
        for element in created
        if element.__class__.__name__ == "Tabs"
        and "rbs-clinic-editor-tabs" in getattr(element, "_classes", [])
    )
    no_clinic = next(
        element
        for element in created
        if element.__class__.__name__ == "Checkbox"
        and getattr(element, "_text", None) == "No clinic hours"
    )
    button_labels = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Button"
    }

    assert dialog_card._style["width"] == "calc(100vw - 32px)"
    assert dialog_card._style["max-width"] == "1200px"
    assert "w-full" in editor_scroll._classes
    assert {"w-full", "min-w-0", "max-w-full"} <= set(editor_panels._classes)
    assert tabs == {"General", "Training-level rules", "Clinic"}
    assert tab_bar.parent_slot.parent is header
    assert tab_bar._props.get("inline-label") is True
    assert "Cancel" not in button_labels
    assert no_clinic.value is False
    assert "Elective Rules" in text
    assert "Uses the shared Elective color. Configure service rules without grouping." not in text
    assert "Not required program-wide" not in text
    assert total_weeks.value is None
    # Rotation-level plus one per PGY rule cloned from the first configured
    # standalone elective (Geriatrics ships PGY1/PGY2 rules in the sample data).
    assert len(total_week_fields) == 3
    assert not consecutive._props.get("dense")
    assert not total_weeks._props.get("dense")
    assert block_size_select.value == [2]
    assert not any(
        str(getattr(element, "_text", "")).startswith("Shared Elective color ·")
        for element in created
    )


def test_existing_elective_uses_the_master_detail_editor() -> None:
    from nicegui import ui

    instance = add_elective_rotation(sample_instance(), _standalone_elective())
    before = set(ui.context.client.elements)
    _elective_rotation_editor(
        instance,
        instance.rotation("addiction_medicine_elective"),
        on_cancel=lambda: None,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]

    assert not any(element.__class__.__name__ == "Dialog" for element in created)
    assert any("rbs-master-detail" in getattr(element, "_classes", []) for element in created)
    assert any(
        element.__class__.__name__ == "Button" and element._props.get("label") == "Save elective"
        for element in created
    )
    assert any(
        element.__class__.__name__ == "Select"
        and element._props.get("label") == "Eligible elective block sizes"
        for element in created
    )
    assert any(
        element.__class__.__name__ == "Number"
        and element._props.get("label") == "Maximum total weeks"
        for element in created
    )
    assert {
        element._props.get("label") for element in created if element.__class__.__name__ == "Tab"
    } == {"General", "Training-level rules", "Clinic"}
    assert not any(
        element.__class__.__name__ == "Button" and element._props.get("label") == "Cancel"
        for element in created
    )
    assert not any(
        str(getattr(element, "_text", "")).startswith("Shared Elective color ·")
        for element in created
    )


def test_elective_detail_uses_elective_rules_without_requirement_pills() -> None:
    from nicegui import ui

    instance = add_elective_rotation(sample_instance(), _standalone_elective())
    rotation = instance.rotation("addiction_medicine_elective")
    before = set(ui.context.client.elements)

    _rotation_detail_contents(instance, rotation)

    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    text = {getattr(element, "_text", None) for element in created}
    badges = [element for element in created if element.__class__.__name__ == "Badge"]

    assert "Elective Rules" in text
    assert "PGY Rules" not in text
    assert "Required schedule, block shape, staffing, and placement in one place." not in text
    assert "Not required program-wide" not in text
    assert not any(
        getattr(element, "_text", None) == "Not required program-wide" for element in badges
    )


def test_schedule_validation_enforces_an_electives_maximum_total_weeks() -> None:
    rotation = _standalone_elective().model_copy(update={"max_total_weeks": 2})
    instance = add_elective_rotation(sample_instance(), rotation)
    resident = instance.residents[0]
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.UNKNOWN,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id=rotation.id,
                kind=RotationKind.ELECTIVE,
                elective=True,
                start_week=1,
                end_week=4,
                weeks=[1, 2, 3, 4],
            )
        ],
    )

    from rbs.solver.validation import validate_schedule

    errors = validate_schedule(instance, schedule).errors

    assert any(
        f"{resident.id} has 4 total weeks on {rotation.id}, exceeding its 2-week maximum" in error
        for error in errors
    )


def test_schedule_validation_enforces_a_pgy_maximum_total_weeks() -> None:
    raw = _standalone_elective().model_dump(mode="json")
    next(rule for rule in raw["pgy_rules"] if rule["pgy"] == 1)["max_total_weeks"] = 2
    rotation = Rotation.model_validate(raw)
    instance = add_elective_rotation(sample_instance(), rotation)
    resident = next(item for item in instance.residents if item.pgy == 1)
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.UNKNOWN,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id=rotation.id,
                kind=RotationKind.ELECTIVE,
                elective=True,
                start_week=1,
                end_week=4,
                weeks=[1, 2, 3, 4],
            )
        ],
    )

    from rbs.solver.validation import validate_schedule

    errors = validate_schedule(instance, schedule).errors

    assert any(
        f"{resident.id} has 4 total weeks on {rotation.id}, exceeding its PGY1 2-week maximum"
        in error
        for error in errors
    )


def test_default_configuration_has_four_elective_options() -> None:
    instance = sample_instance()

    assert {option.rotation_id for option in instance.electives.rotation_options} == {
        "fmed",
        "night_float",
        "geriatrics",
        "palliative_care",
    }
    assert not instance.is_elective_option("elective")
    resident_electives = [
        occurrence
        for occurrence in expand_occurrences(instance)
        if occurrence.resident_id == "resident-001" and occurrence.elective
    ]
    assert [
        (occurrence.rotation_id, occurrence.elective_fallback) for occurrence in resident_electives
    ] == [
        ("night_float", False),
        ("clinic", True),
    ]


def test_last_configured_elective_can_be_removed() -> None:
    configured = add_elective_rotation(sample_instance(), _standalone_elective())

    updated = remove_elective_rotation(
        configured,
        "addiction_medicine_elective",
    )

    assert {option.rotation_id for option in updated.electives.rotation_options} == {
        "fmed",
        "night_float",
        "geriatrics",
        "palliative_care",
    }
    assert "addiction_medicine_elective" not in updated.rotations_by_id


def test_empty_elective_directory_explains_how_to_add_an_option() -> None:
    from nicegui import ui

    raw = sample_instance().model_dump(mode="json")
    raw["electives"]["rotation_options"] = []
    instance = SchedulerInput.model_validate(raw)
    before = set(ui.context.client.elements)
    render_rotations_tab(
        instance,
        selected_rotation_id=None,
        on_select=lambda _rotation_id: None,
        on_save=lambda _instance, _rotation_id: None,
        active_section="elective_configuration",
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    text = {getattr(element, "_text", None) for element in created}

    assert "No electives configured" in text
    assert "Add an elective or enable one from its Mandatory rotation." in text
    assert "Elective" not in text
