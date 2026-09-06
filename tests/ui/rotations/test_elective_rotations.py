import pytest
from pydantic import ValidationError

from rbs.catalog import blank_instance, sample_instance
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
    _elective_shared_summary,
    _open_elective_properties_dialog,
    _open_fmed_pgy_rules_dialog,
    _rotation_detail_contents,
    _rotation_editor,
    render_rotations_tab,
)
from rbs.ui.rotations.ops import (
    add_elective_rotation,
    direct_elective_counts,
    remove_elective_rotation,
    replace_elective_color,
    replace_standard_rotation,
    set_elective_allocation,
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


def test_mandatory_elective_sizes_derive_from_training_level_rules() -> None:
    instance = _instance_with_two_elective_block_sizes()

    # Fixture narrows night_float to 4-week fills via raw JSON (still honored).
    assert instance.eligible_elective_block_sizes("night_float") == (4,)

    # Omitting sizes derives from PGY1 Training-level rules (2+4) and the
    # PGY1 Elective curriculum (2+4) instead of a second manual setting.
    derived = set_elective_eligibility(
        instance,
        "night_float",
        eligible=True,
        eligible_pgys=[1],
        eligible_block_sizes=None,
    )
    assert derived.eligible_elective_block_sizes("night_float") == (2, 4)

    # Explicit sizes remain honored for backward-compatible raw imports.
    narrowed = set_elective_eligibility(
        instance,
        "night_float",
        eligible=True,
        eligible_pgys=[1],
        eligible_block_sizes=[4],
    )
    assert narrowed.eligible_elective_block_sizes("night_float") == (4,)


def test_toggle_shapes_derive_exact_elective_sizes() -> None:
    from rbs.ui.rotations.ops import elective_pgys_sizes_for_shapes

    instance = _instance_with_two_elective_block_sizes()
    rotation = instance.rotation("night_float")

    # Both shapes marked: every fillable combination is eligible.
    assert elective_pgys_sizes_for_shapes(instance, rotation, {(1, 2), (1, 4)}) == (
        [1],
        [2, 4],
    )
    # One shape marked: sizes narrow to it (the per-shape Both/Elective flag).
    assert elective_pgys_sizes_for_shapes(instance, rotation, {(1, 4)}) == ([1], [4])
    # No shapes marked: not an elective option at all.
    assert elective_pgys_sizes_for_shapes(instance, rotation, set()) == ([], [])

    # Shapes with no matching Elective curriculum time fail clearly.
    with pytest.raises(ValueError, match="do not match any Elective curriculum time"):
        elective_pgys_sizes_for_shapes(instance, rotation, {(3, 2)})


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
    checkboxes = {
        getattr(element, "_text", None): element
        for element in created
        if element.__class__.__name__ == "Checkbox"
    }
    toggles = [element for element in created if element.__class__.__name__ == "Toggle"]
    size_controls = [
        element
        for element in created
        if element.__class__.__name__ == "Select"
        and element._props.get("label") == "Eligible elective block sizes"
    ]

    # One three-way toggle per block shape; only the PGY 2 2-week shape is
    # elective-marked. No per-year elective checkbox remains.
    assert {toggle.value for toggle in toggles} == {"Mandatory", "Both"}
    assert all(
        [option["label"] for option in toggle._props["options"]]
        == ["Mandatory", "Elective", "Both"]
        for toggle in toggles
    )
    assert not any(
        str(text).startswith("Available to") and str(text).endswith("as an elective")
        for text in checkboxes
    )
    assert checkboxes["Can be taken more than once as an elective"].value is True
    # Sizes follow Training-level rules; no manual size control remains.
    assert size_controls == []
    assert instance.eligible_elective_block_sizes("fmed") == (2,)


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
    toggles = [element for element in created if element.__class__.__name__ == "Toggle"]
    size_controls = [
        element
        for element in created
        if element.__class__.__name__ == "Select"
        and element._props.get("label") == "Eligible elective block sizes"
    ]

    # One three-way toggle per block shape: PGY 1 is required and elective
    # (Both), PGY 2 is elective-only. No per-year elective checkbox remains.
    assert {toggle.value for toggle in toggles} == {"Both", "Elective"}
    assert all(
        [option["label"] for option in toggle._props["options"]]
        == ["Mandatory", "Elective", "Both"]
        for toggle in toggles
    )
    assert not any(
        str(text).startswith("Available to") and str(text).endswith("as an elective")
        for text in checkboxes
    )
    assert checkboxes["Can be taken more than once as an elective"].value is True
    assert size_controls == []
    assert instance.eligible_elective_block_sizes("night_float") == (2,)
    assert any(getattr(element, "_text", None) == "Elective availability" for element in created)

    disabled = replace_standard_rotation(
        instance,
        rotation.id,
        rotation,
        eligible_as_elective=False,
    )
    assert not disabled.is_elective_option(rotation.id)


def test_new_elective_uses_the_full_screen_editor() -> None:
    from nicegui import ui

    instance = sample_instance()
    before = set(ui.context.client.elements)
    _elective_rotation_editor(
        instance,
        None,
        on_cancel=lambda: None,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
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
    no_clinic = next(
        element
        for element in created
        if element.__class__.__name__ == "Checkbox"
        and getattr(element, "_text", None) == "No clinic hours"
    )
    button_labels = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Button"
    }

    # No separate add dialog: creation shares the master-detail editor.
    assert not any(element.__class__.__name__ == "Dialog" for element in created)
    assert any("rbs-master-detail" in getattr(element, "_classes", []) for element in created)
    assert tabs == {"General", "Training-level rules", "Clinic"}
    assert "Save elective" in button_labels
    assert "Cancel" not in button_labels
    assert "New elective" in text
    assert "Creating" in text
    assert no_clinic.value is False
    assert "Elective Rules" in text
    assert "Not required program-wide" not in text
    # Rotation-level plus one per PGY rule cloned from the first configured
    # standalone elective (Geriatrics ships PGY1/PGY2 rules in the sample data).
    assert len(total_week_fields) == 3
    assert not consecutive._props.get("dense")
    # A new elective starts eligible for every configured Elective block size.
    assert block_size_select.value == [2]
    assert not any(
        str(getattr(element, "_text", "")).startswith("Shared Elective color ·")
        for element in created
    )


def test_new_elective_detail_panel_renders_the_full_screen_editor() -> None:
    from nicegui import ui

    from rbs.ui.rotations.elective import _elective_detail_panel

    instance = sample_instance()
    before = set(ui.context.client.elements)
    _elective_detail_panel(
        instance,
        rotation=None,
        creating=True,
        missing_id=None,
        on_select=lambda _rotation_id: None,
        on_save=lambda _instance, _rotation_id: None,
    )
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    text = {getattr(element, "_text", None) for element in created}

    assert not any(element.__class__.__name__ == "Dialog" for element in created)
    assert "New elective" in text


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


# ---- shared elective properties ----------------------------------------


def _click(button) -> None:
    """Fire one click. The handler may rebuild the button, so snapshot first."""
    from nicegui.events import ClickEventArguments

    for listener in list(button._event_listeners.values()):
        if listener.type == "click":
            listener.handler(ClickEventArguments(sender=button, client=button.client))
            return


def _created_since(before: set) -> list:
    from nicegui import ui

    return [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]


def _button(created: list, label: str):
    return next(
        element
        for element in created
        if element.__class__.__name__ == "Button" and element._props.get("label") == label
    )


def _fields(created: list, class_name: str, label: str) -> list:
    return [
        element
        for element in created
        if element.__class__.__name__ == class_name and element._props.get("label") == label
    ]


def _open_properties(instance: SchedulerInput, saves: list, colors: list) -> set:
    """Open the shared-properties dialog and return the snapshot taken before it."""
    from nicegui import ui

    before = set(ui.context.client.elements)
    _open_elective_properties_dialog(
        instance,
        selected_rotation_id=None,
        on_save=lambda updated, rotation_id: saves.append((updated, rotation_id)),
        on_color_save=lambda updated, rotation_id: colors.append((updated, rotation_id)),
    )
    return before


def test_shared_elective_properties_summarize_color_and_elective_time() -> None:
    from nicegui import ui

    before = set(ui.context.client.elements)
    _elective_shared_summary(sample_instance())
    created = _created_since(before)
    text = {getattr(element, "_text", None) for element in created}

    assert "Block schedule color" in text
    assert "Elective time" in text
    assert {"PGY 1", "PGY 2", "PGY 3"} <= text
    # PGY 1 holds one 2-week Elective block, PGY 2 holds four, PGY 3 holds none.
    assert "1 × 2 weeks" in text
    assert "4 × 2 weeks" in text
    assert "None committed" in text
    # The summary reads; it never edits.
    assert not [
        element
        for element in created
        if element.__class__.__name__ in {"Select", "Number", "Button"}
    ]


def test_the_elective_properties_dialog_edits_color_and_elective_time() -> None:
    created = _created_since(_open_properties(sample_instance(), [], []))

    assert any(element.__class__.__name__ == "Dialog" for element in created)
    assert "Edit shared elective properties" in {
        getattr(element, "_text", None) for element in created
    }
    assert any(
        "rbs-rotation-color-palette" in getattr(element, "_classes", []) for element in created
    )
    # One editable row per level that has committed Elective time.
    assert len(_fields(created, "Select", "Elective block length")) == 2
    assert len(_fields(created, "Number", "Blocks per resident")) == 2
    assert (
        len([element for element in created if element._props.get("label") == "Add block length"])
        == 3
    )


def test_elective_time_can_be_added_to_a_level_that_has_none() -> None:
    saves: list = []
    before = _open_properties(blank_instance(), saves, [])
    created = _created_since(before)
    assert not _fields(created, "Number", "Blocks per resident")

    _click(_button(created, "Add block length"))
    created = _created_since(before)
    _fields(created, "Number", "Blocks per resident")[0].set_value(3)
    _click(_button(created, "Save elective properties"))

    updated, rotation_id = saves[-1]
    assert rotation_id is None
    assert direct_elective_counts(updated, 1) == {2: 3}
    assert updated.unallocated_weeks(1) == 46


def test_elective_time_can_be_reduced_and_returns_the_weeks() -> None:
    saves: list = []
    created = _created_since(_open_properties(sample_instance(), saves, []))

    count = _fields(created, "Number", "Blocks per resident")[1]
    assert count.value == 4
    count.set_value(2)
    _click(_button(created, "Save elective properties"))

    updated, _rotation_id = saves[-1]
    assert direct_elective_counts(updated, 2) == {2: 2}
    assert updated.unallocated_weeks(2) == 4


def test_a_color_only_edit_keeps_the_solved_schedule() -> None:
    """Curriculum edits invalidate a schedule; recoloring blocks does not."""
    instance = sample_instance()
    saves: list = []
    colors: list = []
    created = _created_since(_open_properties(instance, saves, colors))

    swatches = [
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and "rbs-rotation-color-choice" in getattr(element, "_classes", [])
    ]
    _click(swatches[3])
    _click(_button(created, "Save elective properties"))

    assert not saves
    updated, _rotation_id = colors[-1]
    assert updated.electives.color == instance.color_scheme.palette[3]
    assert direct_elective_counts(updated, 2) == direct_elective_counts(instance, 2)


def test_elective_time_beyond_a_levels_budget_cannot_be_saved() -> None:
    saves: list = []
    before = _open_properties(blank_instance(), saves, [])

    _click(_button(_created_since(before), "Add block length"))
    created = _created_since(before)
    _fields(created, "Number", "Blocks per resident")[0].set_value(30)

    assert _button(created, "Save elective properties").enabled is False
    assert "8 weeks more than PGY1 has left to spend" in {
        getattr(element, "_text", None) for element in created
    }
    assert not saves


def test_a_block_length_already_committed_is_not_offered_twice() -> None:
    # One training level keeps the two rows unambiguous.
    instance = set_elective_allocation(blank_instance(), 1, {2: 1})
    before = _open_properties(instance, [], [])
    created = _created_since(before)

    # PGY 1 already spends its Elective time in 2-week blocks.
    lengths = _fields(created, "Select", "Elective block length")[0]
    assert [option["label"] for option in lengths._props["options"]] == [
        "1 week",
        "2 weeks",
        "3 weeks",
        "4 weeks",
        "5 weeks",
    ]

    _click(_button(created, "Add block length"))
    created = _created_since(before)
    kept, added = _fields(created, "Select", "Elective block length")
    assert [option["label"] for option in kept._props["options"]] == [
        "2 weeks",
        "3 weeks",
        "4 weeks",
        "5 weeks",
    ]
    assert [option["label"] for option in added._props["options"]] == [
        "1 week",
        "3 weeks",
        "4 weeks",
        "5 weeks",
    ]
