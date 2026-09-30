"""Workspace names must remain text, never NiceGUI property expressions."""

from types import SimpleNamespace

import pytest

from rbs.catalog import sample_instance
from rbs.models.enums import RotationKind
from rbs.models.instance import SchedulerInput
from rbs.models.rotation import Rotation
from rbs.models.special import SpecialRotation, SpecialRotationKind
from rbs.ui.clinic.tab import _clinic_directory_configuration
from rbs.ui.rotations.elective import _elective_rotation_view
from rbs.ui.rotations.mandatory import _rotation_view
from rbs.ui.rotations.ops import add_elective_rotation
from rbs.ui.rotations.special import _special_rotation_row
from rbs.ui.settings.training_levels import training_level_settings


@pytest.mark.parametrize(
    "name",
    [
        "Children's \"community\" clinic",
        "Name' :label=\"1+1\" data-injected='",
        "Name' @click=\"1+1\" data-injected='",
    ],
)
def test_workspace_names_are_literal_accessible_labels(name: str) -> None:
    from nicegui import ui

    instance = sample_instance()
    raw = instance.model_dump(mode="json")
    next(rotation for rotation in raw["rotations"] if rotation["id"] == "icu")["name"] = name
    raw["clinic_policy"]["sites"][0]["name"] = name
    raw["requirements"][0]["label"] = name
    event = SpecialRotation(
        id="label-test-event",
        name=name,
        kind=SpecialRotationKind.EVENT,
        start_date=instance.calendar.first_week_start,
        end_date=instance.calendar.first_week_start,
        resident_ids=[instance.residents[0].id],
    )
    raw["special_rotations"] = [event.model_dump(mode="json")]
    instance = add_elective_rotation(
        SchedulerInput.model_validate(raw),
        Rotation(
            id="label_test_elective",
            code="TEST",
            name=name,
            kind=RotationKind.ELECTIVE,
            pgy_rules=instance.rotation("elective").pgy_rules,
        ),
    )
    # Exercise names after serialization and full model validation, as on import.
    instance = SchedulerInput.model_validate_json(instance.model_dump_json())
    before = set(ui.context.client.elements)

    for render, rotation_id in (
        (_rotation_view, "icu"),
        (_elective_rotation_view, "label_test_elective"),
    ):
        render(
            instance,
            instance.rotation(rotation_id),
            on_edit=lambda: None,
            on_select=lambda _rotation_id: None,
            on_save=lambda _instance, _rotation_id: None,
        )
    _special_rotation_row(
        instance,
        instance.special_rotations[0],
        selected_rotation_id=None,
        on_save=lambda _instance, _rotation_id: None,
    )
    _clinic_directory_configuration(
        instance,
        selected_rotation_id=None,
        on_save=lambda _instance, _rotation_id: None,
    )
    training_level_settings(
        SimpleNamespace(instance=instance),
        persist_instance=lambda *_args, **_kwargs: None,
    )

    buttons = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before and isinstance(element, ui.button)
    ]
    assert {
        f"More actions for {name}",
        f"Delete elective rotation {name}",
        f"Edit special rotation {name}",
        f"Delete special rotation {name}",
        f"More actions for {name} clinic",
        f"Delete {name}",
    } <= {button.props.get("aria-label") for button in buttons}
    for button in buttons:
        assert "data-injected" not in button.props
        assert not any(key.startswith((":", "@")) for key in button.props)
